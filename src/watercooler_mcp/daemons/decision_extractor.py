"""Decision Extractor Daemon — LLM-powered decision extraction.

Consumes High-tier ``decision_candidate`` findings from
DetectDecisionsDaemon, applies the 8-gate validity checklist via LLM,
and writes structured Decision entries back to threads via
``daemon_write_entry()`` (P5).

Progressive cursor prevents re-processing of consumed findings.
Date-based daily rate limit controls LLM cost.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ulid import ULID

from watercooler.baseline_graph.annotations import AnnotationEvent, append_annotation
from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir
from watercooler.baseline_graph.writer import (
    get_entries_for_thread,
    get_entry_node_from_graph,
    get_thread_from_graph,
)
from watercooler.config_schema import DecisionExtractorConfig
from watercooler.decision_extraction import (
    DECISION_EXTRACTED_TAG,
    HAS_DECISIONS_TAG,
    extract_decision,
)

from .base import BaseDaemon
from .daemon_write import daemon_write_entry
from .llm_client import DaemonLLMClient
from .state import Finding, load_findings

logger = logging.getLogger(__name__)

# Cursor GC every 24 ticks (matches content_refiner)
_CURSOR_GC_INTERVAL = 24

# Finding categories
CAT_SUCCESS = "extraction_success"
CAT_PUSH_FAILED = "extraction_push_failed"
CAT_REJECTED = "extraction_rejected"
CAT_FAILED = "extraction_failed"
CAT_PARSE_FAILURE = "extraction_parse_failure"
CAT_RATE_LIMITED = "extraction_rate_limited"
CAT_CAP_REACHED = "extraction_cap_reached"

# P1.3: error_type strings that map to the LLM-attempt counter. Anything
# else is treated as infrastructure and maps to the write-failure counter.
_LLM_FAILURE_ERROR_TYPES = frozenset({"llm_unavailable", "empty_decision_body"})
_WRITE_FAILURE_ERROR_TYPES = frozenset({"write_failure"})


def _make_finding_id() -> str:
    return str(ULID())


def _build_decision_annotation_hook(
    source_entry_id: str,
    decision_entry_id: str,
) -> Callable[[str, Path, str], None]:
    """Build a post-write hook that annotates source entry and thread.

    Writes 4 annotation events:
    1. Tag source entry ``decision_extracted``
    2. Xref source entry -> decision entry
    3. Xref decision entry -> source entry
    4. Tag thread ``has_decisions``
    """

    def _annotate(topic: str, threads_dir: Path, entry_id: str) -> None:
        thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
        now = datetime.now(timezone.utc).isoformat()
        actor = "ExtractDecisionsDaemon"

        events = [
            AnnotationEvent(
                id=str(ULID()),
                target_id=source_entry_id,
                target_type="entry",
                kind="tag",
                value=DECISION_EXTRACTED_TAG,
                actor=actor,
                timestamp=now,
            ),
            AnnotationEvent(
                id=str(ULID()),
                target_id=source_entry_id,
                target_type="entry",
                kind="xref",
                value=decision_entry_id,
                actor=actor,
                timestamp=now,
            ),
            AnnotationEvent(
                id=str(ULID()),
                target_id=decision_entry_id,
                target_type="entry",
                kind="xref",
                value=source_entry_id,
                actor=actor,
                timestamp=now,
            ),
            AnnotationEvent(
                id=str(ULID()),
                target_id=topic,
                target_type="thread",
                kind="tag",
                value=HAS_DECISIONS_TAG,
                actor=actor,
                timestamp=now,
            ),
        ]

        for event in events:
            append_annotation(thread_dir, event)

    return _annotate


class ExtractDecisionsDaemon(BaseDaemon):
    """LLM-powered decision extraction from detector findings.

    Args:
        interval: Seconds between extraction cycles.
        config: DecisionExtractorConfig for thresholds and LLM settings.
        threads_dir: Override threads directory (None = resolve at tick time).
        llm_client: Optional DaemonLLMClient override (for testing).
        enabled: Whether this daemon is active.
    """

    def __init__(
        self,
        *,
        interval: float = 1800.0,
        config: DecisionExtractorConfig | None = None,
        threads_dir: Path | None = None,
        llm_client: DaemonLLMClient | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            name="decision_extractor",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._config = config or DecisionExtractorConfig()
        self._threads_dir_override = threads_dir
        self._llm_client = llm_client
        # Cached paths — stable after first resolution
        self._resolved_threads_dir: Optional[Path] = None
        self._resolved_code_root: Optional[Path] = None
        # Cursor GC counter
        self._ticks_since_gc: int = 0
        # Per-tick thread context cache (cleared at tick start)
        self._thread_context_cache: dict[str, str] = {}
        # Per-tick metrics
        self._last_tick_candidates: int = 0
        self._last_tick_extracted: int = 0
        self._last_tick_push_failed: int = 0
        self._last_tick_rejected: int = 0
        self._last_tick_failed: int = 0
        self._last_tick_rate_limited: int = 0

    # ------------------------------------------------------------------
    # LLM client
    # ------------------------------------------------------------------

    def _get_llm_client(self) -> DaemonLLMClient:
        if self._llm_client is None:
            self._llm_client = DaemonLLMClient(daemon_name="decision_extractor")
        return self._llm_client

    # ------------------------------------------------------------------
    # Path resolution — cache stable paths, refresh branch metadata per write
    # ------------------------------------------------------------------

    def _resolve_paths(self) -> tuple[Optional[Path], Optional[Path]]:
        """Resolve threads_dir and code_root.

        Returns (threads_dir, code_root). Caches stable paths after first
        successful resolution.
        """
        if self._threads_dir_override is not None:
            if self._resolved_code_root is None:
                try:
                    from watercooler_mcp.config import resolve_thread_context

                    ctx = resolve_thread_context(Path.cwd())
                    self._resolved_code_root = ctx.code_root
                except Exception as exc:
                    logger.debug(
                        "DAEMON[decision_extractor]: could not resolve code_root "
                        "for override threads_dir: %s",
                        exc,
                    )
            return self._threads_dir_override, self._resolved_code_root

        if self._resolved_threads_dir is not None:
            return self._resolved_threads_dir, self._resolved_code_root

        try:
            from watercooler_mcp.config import resolve_thread_context

            ctx = resolve_thread_context(Path.cwd())
            self._resolved_threads_dir = ctx.threads_dir
            self._resolved_code_root = ctx.code_root
            return ctx.threads_dir, ctx.code_root
        except Exception as exc:
            logger.debug(
                "DAEMON[decision_extractor]: could not resolve paths: %s", exc
            )
            return None, None

    # ------------------------------------------------------------------
    # Progressive cursor
    # ------------------------------------------------------------------

    def _get_processed_ids(self) -> list[str]:
        return self._checkpoint.extras.get("processed_finding_ids", [])

    def _set_processed_ids(self, ids: list[str]) -> None:
        self._checkpoint.extras["processed_finding_ids"] = ids

    def _get_processed_source_keys(self) -> list[str]:
        return self._checkpoint.extras.get("processed_source_keys", [])

    def _set_processed_source_keys(self, keys: list[str]) -> None:
        self._checkpoint.extras["processed_source_keys"] = keys

    @staticmethod
    def _source_key(topic: str, entry_id: str | None) -> str:
        return f"{topic}:{entry_id or ''}"

    def _append_unique(self, existing: list[str], new_items: list[str]) -> list[str]:
        return list(dict.fromkeys(existing + new_items))

    def _gc_processed_ids(self, live_ids: set[str]) -> None:
        current = self._get_processed_ids()
        pruned = [fid for fid in current if fid in live_ids]
        removed = len(current) - len(pruned)
        if removed > 0:
            logger.debug(
                "DAEMON[decision_extractor]: cursor GC pruned %d stale IDs",
                removed,
            )
            self._set_processed_ids(pruned)

    def _gc_processed_source_keys(self, live_keys: set[str]) -> None:
        current = self._get_processed_source_keys()
        pruned = [key for key in current if key in live_keys]
        removed = len(current) - len(pruned)
        if removed > 0:
            logger.debug(
                "DAEMON[decision_extractor]: source-key GC pruned %d stale keys",
                removed,
            )
            self._set_processed_source_keys(pruned)

    # ------------------------------------------------------------------
    # P1.3: per-entry retry counters (LLM vs infrastructure)
    # ------------------------------------------------------------------

    def _get_llm_attempts(self) -> dict[str, int]:
        return self._checkpoint.extras.setdefault("llm_extraction_attempts", {})

    def _get_write_attempts(self) -> dict[str, int]:
        return self._checkpoint.extras.setdefault("write_failure_attempts", {})

    def _increment_llm_attempts(self, source_key: str) -> int:
        counts = self._get_llm_attempts()
        counts[source_key] = counts.get(source_key, 0) + 1
        return counts[source_key]

    def _increment_write_attempts(self, source_key: str) -> int:
        counts = self._get_write_attempts()
        counts[source_key] = counts.get(source_key, 0) + 1
        return counts[source_key]

    def _gc_attempt_counters(self, live_keys: set[str]) -> None:
        """Prune per-entry counter dicts to keys that still exist upstream."""
        for dict_name in ("llm_extraction_attempts", "write_failure_attempts"):
            current = self._checkpoint.extras.get(dict_name, {})
            if not current:
                continue
            pruned = {k: v for k, v in current.items() if k in live_keys}
            removed = len(current) - len(pruned)
            if removed > 0:
                logger.debug(
                    "DAEMON[decision_extractor]: %s GC pruned %d stale keys",
                    dict_name,
                    removed,
                )
                self._checkpoint.extras[dict_name] = pruned

    # ------------------------------------------------------------------
    # Daily rate limit
    # ------------------------------------------------------------------

    def _get_daily_count(self, tick_date: str) -> int:
        daily = self._checkpoint.extras.get("daily_count", {})
        if daily.get("date") != tick_date:
            return 0
        return daily.get("count", 0)

    def _increment_daily_count(self, tick_date: str) -> None:
        daily = self._checkpoint.extras.get("daily_count", {})
        if daily.get("date") != tick_date:
            daily = {"date": tick_date, "count": 0}
        daily["count"] = daily.get("count", 0) + 1
        self._checkpoint.extras["daily_count"] = daily

    # ------------------------------------------------------------------
    # Thread context loading (cached per-tick)
    # ------------------------------------------------------------------

    def _load_thread_context(self, topic: str, threads_dir: Path) -> str:
        """Load thread summary + last 10 entries for LLM context.

        Cached by topic (per-tick cache, cleared at tick start).
        """
        if topic in self._thread_context_cache:
            return self._thread_context_cache[topic]

        context_parts: list[str] = []

        # Thread metadata
        thread = get_thread_from_graph(threads_dir, topic)
        if thread:
            context_parts.append(
                f"Thread: {topic}\n"
                f"Title: {thread.get('title', topic)}\n"
                f"Status: {thread.get('status', 'OPEN')}"
            )

        # Last 10 entries by index
        entries = get_entries_for_thread(threads_dir, topic)
        if entries:
            recent = entries[-10:]
            for e in recent:
                title = e.get("title", "(untitled)")
                summary = e.get("summary", "") or ""
                agent = e.get("agent", "unknown")
                ts = e.get("timestamp", "")
                context_parts.append(
                    f"\n---\n[{ts}] {agent}: {title}\n{summary}"
                )

        result = "\n".join(context_parts)
        self._thread_context_cache[topic] = result
        return result

    # ------------------------------------------------------------------
    # tick()
    # ------------------------------------------------------------------

    def tick(self) -> list[Finding]:
        """Run one extraction cycle."""
        tick_start = time.monotonic()
        tick_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Reset per-tick state
        self._thread_context_cache = {}
        self._last_tick_candidates = 0
        self._last_tick_extracted = 0
        self._last_tick_push_failed = 0
        self._last_tick_rejected = 0
        self._last_tick_failed = 0
        self._last_tick_rate_limited = 0
        self._ticks_since_gc += 1

        cfg = self._config

        # 1. Check LLM availability
        llm = self._get_llm_client()
        if not llm.is_available():
            logger.debug("DAEMON[decision_extractor]: LLM unavailable, skipping tick")
            return []

        # 2. Resolve paths — fail closed for writes if code_root missing
        threads_dir, code_root = self._resolve_paths()
        if threads_dir is None or not threads_dir.exists():
            logger.debug("DAEMON[decision_extractor]: no threads_dir, skipping")
            return []

        if code_root is None:
            logger.warning(
                "DAEMON[decision_extractor]: code_root not resolved — "
                "write-capable daemon skipping tick"
            )
            return []

        # 3. Load decision_detector findings
        detector_findings = load_findings(
            "decision_detector",
            limit=50_000,
            unacknowledged_only=True,
            category="decision_candidate",
        )
        if self._ticks_since_gc >= _CURSOR_GC_INTERVAL:
            gc_findings = load_findings(
                "decision_detector",
                limit=50_000,
                category="decision_candidate",
            )
            if gc_findings:
                live_ids = {f.finding_id for f in gc_findings}
                live_source_keys = {
                    self._source_key(f.topic, f.entry_id) for f in gc_findings
                }
                self._gc_processed_ids(live_ids)
                self._gc_processed_source_keys(live_source_keys)
                self._gc_attempt_counters(live_source_keys)
            self._ticks_since_gc = 0

        if not detector_findings:
            logger.debug("DAEMON[decision_extractor]: no detector findings")
            return []

        # 4. Filter by min_extraction_score
        scored_findings = [
            f for f in detector_findings
            if f.details.get("score", 0) >= cfg.min_extraction_score
        ]

        # 5. Sort: score DESC, created_at ASC
        scored_findings.sort(
            key=lambda f: (-f.details.get("score", 0), f.created_at),
        )

        # 6. Progressive cursor: exclude already-processed
        processed_set = set(self._get_processed_ids())
        processed_source_keys = set(self._get_processed_source_keys())
        candidates = [
            f for f in scored_findings
            if f.finding_id not in processed_set
            and self._source_key(f.topic, f.entry_id) not in processed_source_keys
        ]

        # 7. Daily rate limit
        daily_count = self._get_daily_count(tick_date)
        remaining_capacity = max(0, cfg.max_extractions_per_day - daily_count)

        if remaining_capacity == 0:
            if candidates:
                self._last_tick_rate_limited = len(candidates)
                return [
                    Finding(
                        finding_id=_make_finding_id(),
                        daemon_name=self.name,
                        severity="info",
                        category=CAT_RATE_LIMITED,
                        topic="",
                        message=f"Daily extraction cap reached ({cfg.max_extractions_per_day})",
                        details={"remaining_candidates": len(candidates)},
                    )
                ]
            return []

        # 8. Cap batch
        batch_limit = min(cfg.max_candidates_per_tick, remaining_capacity)
        batch = candidates[:batch_limit]

        findings: list[Finding] = []
        processed_this_tick: list[str] = []
        processed_source_keys_this_tick: list[str] = []

        llm_attempts = self._get_llm_attempts()
        write_attempts = self._get_write_attempts()

        # 9. Process each candidate
        for finding in batch:
            # Check tick duration
            elapsed = time.monotonic() - tick_start
            if elapsed >= cfg.max_tick_duration:
                logger.debug(
                    "DAEMON[decision_extractor]: max_tick_duration reached (%.1fs)",
                    elapsed,
                )
                break

            source_key = self._source_key(finding.topic, finding.entry_id)

            # P1.3: pre-processing cap gate. Short-circuit LLM/write calls
            # for entries that have already exhausted their attempt budget.
            llm_count = llm_attempts.get(source_key, 0)
            write_count = write_attempts.get(source_key, 0)
            if llm_count >= cfg.max_extraction_attempts:
                findings.append(Finding(
                    finding_id=_make_finding_id(),
                    daemon_name=self.name,
                    severity="warning",
                    category=CAT_CAP_REACHED,
                    topic=finding.topic,
                    entry_id=finding.entry_id,
                    message=(
                        f"LLM extraction cap reached ({llm_count} attempts); "
                        f"entry permanently skipped"
                    ),
                    details={
                        "source_entry_id": finding.entry_id,
                        "reason": "llm_failure",
                        "attempts": llm_count,
                        "cap": cfg.max_extraction_attempts,
                    },
                ))
                processed_this_tick.append(finding.finding_id)
                processed_source_keys_this_tick.append(source_key)
                continue
            if write_count >= cfg.max_write_failure_attempts:
                findings.append(Finding(
                    finding_id=_make_finding_id(),
                    daemon_name=self.name,
                    severity="warning",
                    category=CAT_CAP_REACHED,
                    topic=finding.topic,
                    entry_id=finding.entry_id,
                    message=(
                        f"Write failure cap reached ({write_count} attempts); "
                        f"entry permanently skipped"
                    ),
                    details={
                        "source_entry_id": finding.entry_id,
                        "reason": "write_failure",
                        "attempts": write_count,
                        "cap": cfg.max_write_failure_attempts,
                    },
                ))
                processed_this_tick.append(finding.finding_id)
                processed_source_keys_this_tick.append(source_key)
                continue

            self._last_tick_candidates += 1

            try:
                result_finding = self._process_candidate(
                    finding, threads_dir, code_root, llm, tick_date
                )
                if result_finding is not None:
                    findings.append(result_finding)
                    # Check if we should mark processed
                    cat = result_finding.category
                    if cat in (CAT_SUCCESS, CAT_PUSH_FAILED, CAT_REJECTED, CAT_PARSE_FAILURE):
                        processed_this_tick.append(finding.finding_id)
                        processed_source_keys_this_tick.append(source_key)
                    elif cat == CAT_FAILED:
                        # P1.3: increment the matching counter so repeated
                        # failures are capped. error_type is set by
                        # _process_candidate on each CAT_FAILED branch.
                        error_type = result_finding.details.get("error_type", "")
                        if error_type in _LLM_FAILURE_ERROR_TYPES:
                            self._increment_llm_attempts(source_key)
                        elif error_type in _WRITE_FAILURE_ERROR_TYPES:
                            self._increment_write_attempts(source_key)
                        # Unknown error_type: no counter increment — cursor
                        # still does not advance, so the entry retries.
            except Exception as exc:
                logger.warning(
                    "DAEMON[decision_extractor]: error processing %s: %s",
                    finding.finding_id,
                    exc,
                )
                findings.append(
                    Finding(
                        finding_id=_make_finding_id(),
                        daemon_name=self.name,
                        severity="warning",
                        category=CAT_FAILED,
                        topic=finding.topic,
                        entry_id=finding.entry_id,
                        message=f"Extraction error: {str(exc)[:200]}",
                        details={"source_entry_id": finding.entry_id, "error_type": type(exc).__name__},
                    )
                )
                self._last_tick_failed += 1

        # 10. Update cursor
        if processed_this_tick:
            updated = self._append_unique(self._get_processed_ids(), processed_this_tick)
            self._set_processed_ids(updated)
        if processed_source_keys_this_tick:
            updated_keys = self._append_unique(
                self._get_processed_source_keys(),
                processed_source_keys_this_tick,
            )
            self._set_processed_source_keys(updated_keys)

        # 11. Update checkpoint metrics
        self._checkpoint.threads_processed = self._last_tick_candidates

        return findings

    # ------------------------------------------------------------------
    # Per-candidate processing
    # ------------------------------------------------------------------

    def _process_candidate(
        self,
        finding: Finding,
        threads_dir: Path,
        code_root: Path,
        llm: DaemonLLMClient,
        tick_date: str,
    ) -> Optional[Finding]:
        """Process a single candidate. Returns a finding for observability."""
        cfg = self._config
        source_entry_id = finding.entry_id
        topic = finding.topic

        # a. Load entry fresh from graph (don't trust finding metadata)
        entry = get_entry_node_from_graph(threads_dir, source_entry_id, topic)
        if entry is None:
            # Stale reference — entry or thread deleted
            logger.debug(
                "DAEMON[decision_extractor]: Skipping finding %s — "
                "entry no longer exists in graph",
                finding.finding_id,
            )
            return Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="info",
                category=CAT_REJECTED,
                topic=topic,
                entry_id=source_entry_id,
                message="Skipped: entry no longer exists in graph",
                details={
                    "source_entry_id": source_entry_id,
                    "confidence": 0,
                    "rejection_reason": "stale_reference",
                },
            )

        # Enrich entry dict with thread_topic (needed by extraction)
        entry_dict = dict(entry)
        entry_dict.setdefault("thread_topic", topic)

        # b. Load thread context (cached)
        thread_context = self._load_thread_context(topic, threads_dir)

        # c. Call LLM extraction
        def _llm_complete(system: str, user: str) -> Optional[str]:
            return llm.complete(user, system=system)

        extraction_result = extract_decision(
            entry_dict,
            thread_context,
            llm_complete=_llm_complete,
            max_body_chars=cfg.max_body_chars,
            min_confidence=cfg.min_confidence,
        )

        # d. Handle result
        if extraction_result.rejection_reason == "llm_unavailable":
            self._last_tick_failed += 1
            return Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="warning",
                category=CAT_FAILED,
                topic=topic,
                entry_id=source_entry_id,
                message="LLM unavailable during extraction",
                details={"source_entry_id": source_entry_id, "error_type": "llm_unavailable"},
            )

        if extraction_result.rejection_reason == "llm_parse_failure":
            self._last_tick_failed += 1
            return Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="info",
                category=CAT_PARSE_FAILURE,
                topic=topic,
                entry_id=source_entry_id,
                message="LLM returned unparseable JSON",
                details={"source_entry_id": source_entry_id},
            )

        if not extraction_result.passed:
            self._last_tick_rejected += 1
            failed_gates = []
            if extraction_result.gate_results:
                failed_gates = [
                    g for g, r in extraction_result.gate_results.items()
                    if not r.get("passed", False)
                ]
            return Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="info",
                category=CAT_REJECTED,
                topic=topic,
                entry_id=source_entry_id,
                message=(
                    f"Extraction rejected: {extraction_result.rejection_reason} "
                    f"(confidence={extraction_result.confidence})"
                ),
                details={
                    "source_entry_id": source_entry_id,
                    "confidence": extraction_result.confidence,
                    "rejection_reason": extraction_result.rejection_reason,
                    "failed_gates": failed_gates,
                },
            )

        # e. Write Decision entry
        if extraction_result.extraction is None or not extraction_result.decision_body:
            self._last_tick_failed += 1
            return Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="warning",
                category=CAT_FAILED,
                topic=topic,
                entry_id=source_entry_id,
                message="Extraction produced no decision body",
                details={
                    "source_entry_id": source_entry_id,
                    "error_type": "empty_decision_body",
                },
            )

        decision_title = extraction_result.extraction.decision_statement or "Extracted Decision"
        if len(decision_title) > 80:
            decision_title = decision_title[:77] + "..."

        decision_entry_id = str(ULID())

        write_result = daemon_write_entry(
            topic,
            code_root=code_root,
            title=decision_title,
            body=extraction_result.decision_body,
            agent="ExtractDecisionsDaemon",
            role="scribe",
            entry_type="Decision",
            entry_id=decision_entry_id,
            agent_spec="decision-extractor",
            user_tag="system",
            post_write_hooks=[
                _build_decision_annotation_hook(source_entry_id, decision_entry_id),
            ],
        )

        if write_result.written:
            self._increment_daily_count(tick_date)

        if write_result.written and write_result.pushed:
            self._last_tick_extracted += 1
            return Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="info",
                category=CAT_SUCCESS,
                topic=topic,
                entry_id=source_entry_id,
                message=(
                    f"Decision extracted (confidence={extraction_result.confidence}) "
                    f"→ {write_result.entry_id}"
                ),
                details={
                    "entry_id": write_result.entry_id,
                    "source_entry_id": source_entry_id,
                    "confidence": extraction_result.confidence,
                    "topic": topic,
                },
            )

        if write_result.written and not write_result.pushed:
            self._last_tick_push_failed += 1
            return Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="warning",
                category=CAT_PUSH_FAILED,
                topic=topic,
                entry_id=source_entry_id,
                message=(
                    f"Decision written locally but push failed: "
                    f"{write_result.error}"
                ),
                details={
                    "entry_id": write_result.entry_id,
                    "source_entry_id": source_entry_id,
                    "confidence": extraction_result.confidence,
                    "topic": topic,
                    "error": write_result.error,
                },
            )

        # Write failed entirely — do NOT mark processed (retry)
        self._last_tick_failed += 1
        return Finding(
            finding_id=_make_finding_id(),
            daemon_name=self.name,
            severity="warning",
            category=CAT_FAILED,
            topic=topic,
            entry_id=source_entry_id,
            message=f"Write failed: {write_result.error}",
            details={
                "source_entry_id": source_entry_id,
                "error_type": "write_failure",
            },
        )

    # ------------------------------------------------------------------
    # Status summary
    # ------------------------------------------------------------------

    def status_summary(self) -> dict[str, Any]:
        base = super().status_summary()
        tick_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_count = self._get_daily_count(tick_date)
        base.update({
            "last_tick_candidates_evaluated": self._last_tick_candidates,
            "last_tick_extracted": self._last_tick_extracted,
            "last_tick_push_failed": self._last_tick_push_failed,
            "last_tick_rejected": self._last_tick_rejected,
            "last_tick_failed": self._last_tick_failed,
            "last_tick_rate_limited": self._last_tick_rate_limited,
            "daily_extractions_count": daily_count,
            "daily_extractions_remaining": max(
                0, self._config.max_extractions_per_day - daily_count
            ),
            "processed_ids_count": len(self._get_processed_ids()),
            "processed_source_keys_count": len(self._get_processed_source_keys()),
            "llm_extraction_attempts_count": len(self._get_llm_attempts()),
            "write_failure_attempts_count": len(self._get_write_attempts()),
        })
        return base
