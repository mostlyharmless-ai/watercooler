"""Learnings daemon — extract/propose tier of the Commons loop.

Scans CLOSED threads and, for each, decides whether the work *captured a learning*
(a solutions-dir write-up matched by PR number, or an in-thread lesson section) or
left a **capture gap** (referenced a merged PR with neither signal — only assessed
when the project demonstrably writes solution docs).
Structured as a five-stage pipeline — sources -> candidate-gen -> score/filter ->
authority-policy -> emit — so future analyzers become configs on the same engine
(design proposal on thread `workflow-packs-prepare-work-discovery-2026-05-29`).

The deterministic layer emits reversible L1 graph annotations (`has_learning` /
`solution-doc:<path>` tags, lesson xrefs) and daemon findings (`learning_extracted`,
`capture_gap`). With ``synthesize_notes`` on, the LLM drafts the missing learning
for a capture-gap thread. Under ``monitor`` that draft is a ``shadow_learning_note``
finding only; past ``monitor`` with ``emit_learning_notes`` it is also written as a
thread-visible **learning candidate Note** (Phase 2) — one per source thread.

Authority posture (guardrail ``01KS0JTK0RT4EC0M92PMX19XRA``): this daemon never
writes Decision / Closure / supersession / status. ``emit_mode="monitor"`` (default)
= reversible annotations + findings only; no thread-visible writes. Emission past
monitor produces only ``needs_human_confirmation`` / ``Authority: none`` candidate
Notes; L2→L3 promotion stays human (``update-agent-context`` /
``watercooler_promote_candidate``).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

from watercooler.baseline_graph import storage
from watercooler.baseline_graph.annotations import AnnotationEvent
from watercooler.baseline_graph.storage import get_graph_dir
from watercooler.baseline_graph.writer import (
    get_entries_for_thread,
    get_thread_from_graph,
)
from watercooler.config_schema import LearningsConfig
from watercooler.learning_extraction import (
    DEFAULT_CRITERIA,
    LearningAssessment,
    assess_thread_learning,
    build_solutions_index,
)
from watercooler.learning_synthesis import (
    SynthesisResult,
    format_learning_candidate_body,
    synthesize_learning,
)

from .base import BaseDaemon
from .daemon_write import daemon_annotate, daemon_write_entry
from .llm_client import DaemonLLMClient
from .state import Finding, load_findings

logger = logging.getLogger(__name__)

# L1 annotation tags (mirror decision_extraction's DECISION_EXTRACTED_TAG /
# HAS_DECISIONS_TAG).
LEARNING_EXTRACTED_TAG = "learning_extracted"
HAS_LEARNING_TAG = "has_learning"
SOLUTION_DOC_TAG_PREFIX = "solution-doc:"

# Finding categories.
CAT_LEARNING_EXTRACTED = "learning_extracted"
CAT_CAPTURE_GAP = "capture_gap"
# Shadow ("would-have-written") LLM-drafted learning note — a finding under
# monitor mode, never a thread Note. The candidate the precision eval measures.
CAT_SHADOW_LEARNING_NOTE = "shadow_learning_note"
# Phase 2: a thread-visible learning candidate Note was emitted for this thread.
# Doubles as the emission dedup key (one candidate per source thread).
CAT_LEARNING_CANDIDATE = "learning_candidate_emitted"

# L1 annotation tags for an emitted candidate (xref candidate Note ↔ source thread).
LEARNING_CANDIDATE_TAG = "learning_candidate"
HAS_LEARNING_CANDIDATE_TAG = "has_learning_candidate"

# Thread statuses treated as "resolved" for the closure scan.
_CLOSED_STATUSES = frozenset({"CLOSED", "RESOLVED", "MERGED", "DONE"})

# Hard cap on findings loaded for dedup (mirrors decision_detector).
_DEDUP_LIMIT = 50_000

_ACTOR = "ExtractLearningsDaemon"


class ExtractLearningsDaemon(BaseDaemon):
    """Periodic learnings extractor — deterministic capture-gap + index layer.

    Args:
        interval: Seconds between extraction cycles.
        config: LearningsConfig for emission mode and toggles.
        threads_dir: Override threads directory (None = resolve at tick time).
        code_root: Override code root for the solutions read (None = resolve).
        llm_client: Optional DaemonLLMClient override (reserved for the later
            synthesis layer).
        enabled: Whether this daemon is active. Defaults True, matching the
            sibling decision daemon; system default-off is enforced by the config
            gate ``mcp.daemons.learnings.enabled`` at registration.
        dry_run: When True, compute and return findings but write **no**
            annotations to the graph (findings-only). Defense-in-depth for the
            precision-eval phase and for any ad-hoc ``tick()`` against a live
            checkout — note that normal ``monitor`` mode DOES write (reversible)
            annotations, so it is not side-effect-free.
    """

    def __init__(
        self,
        *,
        interval: float = 1800.0,
        config: LearningsConfig | None = None,
        threads_dir: Path | None = None,
        code_root: Path | None = None,
        llm_client: DaemonLLMClient | None = None,
        enabled: bool = True,
        dry_run: bool = False,
    ) -> None:
        super().__init__(
            name="learnings",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._config = config or LearningsConfig()
        self._threads_dir_override = threads_dir
        self._code_root_override = code_root
        self._llm_client = llm_client
        self._dry_run = dry_run

    # -- sources --------------------------------------------------------------

    def _resolve_context(self) -> tuple[Path | None, Path | None]:
        """Resolve (threads_dir, code_root). Overrides win; else from cwd context."""
        if self._threads_dir_override is not None:
            return self._threads_dir_override, self._code_root_override
        try:
            from watercooler_mcp.config import resolve_thread_context

            ctx = resolve_thread_context(Path.cwd())
            return ctx.threads_dir, ctx.code_root
        except Exception as exc:  # noqa: BLE001 — best-effort resolution
            logger.debug("DAEMON[learnings]: could not resolve context: %s", exc)
            return None, None

    def _load_existing_keys(self) -> set[tuple[str, str, str]]:
        """Dedup cache of already-emitted findings (mirrors decision_detector)."""
        existing = load_findings(
            self.name,
            limit=_DEDUP_LIMIT,
            unacknowledged_only=True,
            namespace=self.state_namespace,
        )
        return {(f.topic, f.category, f.entry_id or "") for f in existing}

    def _get_llm_client(self) -> DaemonLLMClient:
        if self._llm_client is None:
            self._llm_client = DaemonLLMClient(daemon_name="learnings")
        return self._llm_client

    # -- pipeline -------------------------------------------------------------

    def tick(self) -> list[Finding]:
        """Scan CLOSED threads; emit L1 annotations + learning/capture_gap findings."""
        threads_dir, code_root = self._resolve_context()
        if threads_dir is None or not threads_dir.exists():
            logger.debug("DAEMON[learnings]: no threads_dir, skipping")
            return []

        graph_dir = get_graph_dir(threads_dir)
        topics = storage.list_thread_topics(graph_dir)
        if not topics:
            return []

        solutions_index: dict[int, str] = {}
        if self._config.index_solutions_docs and code_root is not None:
            for rel_dir in self._config.solutions_dirs:
                for pr, doc in build_solutions_index(
                    Path(code_root) / rel_dir
                ).items():
                    # Prefix the dir-relative path so solution-doc: tags stay
                    # unambiguous across solutions_dirs (e.g. docs/solutions vs
                    # dev_docs/solutions, which both yield bare "a.md"). Path join
                    # normalizes a configured trailing slash.
                    solutions_index.setdefault(pr, str(Path(rel_dir) / doc))

        # Capture-gap guard: an empty index can't distinguish "no write-up" from
        # "write-ups exist but the configured solutions_dirs missed them", so
        # flagging gaps would false-fire on well-documented work. Only assess gaps
        # when the project demonstrably writes solution docs (non-empty index);
        # solution-doc and in-thread-lesson positives are unaffected.
        criteria = DEFAULT_CRITERIA
        if not solutions_index:
            criteria = tuple(c for c in DEFAULT_CRITERIA if c.kind != "capture_gap")
            logger.debug(
                "DAEMON[learnings]: solutions index empty; capture_gap suppressed "
                "(no project solution docs found under %s)",
                self._config.solutions_dirs,
            )

        existing = self._load_existing_keys()
        findings: list[Finding] = []
        tick_start = time.monotonic()
        syntheses_this_tick = 0
        # is_available() is a live HTTP probe, so probe at most once per tick
        # (not per gap thread) — checking it per thread amplified into one
        # blocking probe per capture-gap thread against the endpoint. Resolved
        # lazily on the first synthesis-eligible gap so a gap-free (or
        # synthesis-off) tick never probes at all. None = not yet probed.
        llm_available: bool | None = None

        for topic in topics:
            # Tick-duration guard — bounds wall-clock on large corpora (F1).
            if time.monotonic() - tick_start >= self._config.max_tick_duration:
                logger.debug("DAEMON[learnings]: max_tick_duration reached; stopping scan")
                break

            thread_node = get_thread_from_graph(threads_dir, topic)
            status = str((thread_node or {}).get("status", "")).upper()
            if status not in _CLOSED_STATUSES:
                continue

            try:
                entries = get_entries_for_thread(threads_dir, topic)
            except (OSError, KeyError, ValueError) as exc:
                logger.debug("DAEMON[learnings]: read error for %s: %s", topic, exc)
                continue

            assessment = assess_thread_learning(entries, solutions_index, criteria)

            if assessment.status == "has_learning":
                key = (topic, CAT_LEARNING_EXTRACTED, "")
                if key in existing:
                    continue
                # Persist via the committing path — requires a code_root.
                # Without one we cannot commit/push, so skip rather than leave
                # an uncommitted worktree write (bug-sync-worktree-poisoning).
                # The finding is still emitted, so the learning is surfaced and
                # persistence retries on a later tick that has a code_root.
                if not self._dry_run:
                    if code_root is not None:
                        self._emit_learning_annotations(topic, Path(code_root), assessment)
                    else:
                        logger.debug(
                            "DAEMON[learnings]: no code_root; skipping has_learning "
                            "annotation for %s (finding still emitted)",
                            topic,
                        )
                findings.append(self._learning_finding(topic, assessment))
                existing.add(key)
            elif assessment.status == "capture_gap":
                gap_key = (topic, CAT_CAPTURE_GAP, "")
                if gap_key not in existing:
                    findings.append(self._capture_gap_finding(topic, assessment))
                    existing.add(gap_key)
                # Draft the missing learning (shadow) — independent of the
                # capture_gap dedup so a previously-flagged gap can still be drafted.
                # Per-tick synthesis cap bounds the LLM-call burst (F1).
                if (
                    self._config.synthesize_notes
                    and syntheses_this_tick < self._config.max_syntheses_per_tick
                ):
                    shadow_key = (topic, CAT_SHADOW_LEARNING_NOTE, "")
                    if shadow_key not in existing:
                        if llm_available is None:
                            llm_available = self._get_llm_client().is_available()
                        # Client known reachable => every call below is a real
                        # LLM call; charge the per-tick budget unconditionally (a
                        # draft rejected by the confidence floor still cost a
                        # call). An unreachable client charges nothing.
                        if llm_available:
                            syntheses_this_tick += 1
                            result = self._maybe_synthesize(topic, entries, assessment)
                            if result is not None:
                                findings.append(
                                    self._shadow_finding(topic, result, assessment)
                                )
                                existing.add(shadow_key)
                                # Phase 2: emit a thread-visible candidate Note,
                                # gated (emit_mode past monitor + emit_learning_notes)
                                # and deduped to one candidate per source thread.
                                cand_key = (topic, CAT_LEARNING_CANDIDATE, "")
                                if (
                                    self._should_emit_candidate()
                                    and code_root is not None
                                    and cand_key not in existing
                                ):
                                    emitted = self._emit_learning_candidate(
                                        topic,
                                        Path(code_root),
                                        result,
                                        assessment,
                                        # F1: immutable owner stamp = the source
                                        # thread's ball-holder at emission time.
                                        disposition_owner=str(
                                            (thread_node or {}).get("ball") or ""
                                        ).strip()
                                        or None,
                                    )
                                    if emitted is not None:
                                        findings.append(emitted)
                                        existing.add(cand_key)

        return findings

    # -- emit (L1 annotations; reversible, monitor-safe) ----------------------

    def _emit_learning_annotations(
        self, topic: str, code_root: Path, assessment: LearningAssessment
    ) -> None:
        """Tag the thread `has_learning` and record the index link (reversible).

        Persisted through ``daemon_annotate`` so the tags are committed + pushed
        in one transaction — never a bare ``append_annotation``, which would
        leave an uncommitted worktree write that wedges sync
        (``bug-sync-worktree-poisoning``).
        """
        now = datetime.now(timezone.utc).isoformat()
        events = [
            AnnotationEvent(
                id=str(ULID()),
                target_id=topic,
                target_type="thread",
                kind="tag",
                value=HAS_LEARNING_TAG,
                actor=_ACTOR,
                timestamp=now,
            )
        ]
        if assessment.matched_doc:
            events.append(
                AnnotationEvent(
                    id=str(ULID()),
                    target_id=topic,
                    target_type="thread",
                    kind="tag",
                    value=f"{SOLUTION_DOC_TAG_PREFIX}{assessment.matched_doc}",
                    actor=_ACTOR,
                    timestamp=now,
                )
            )
        if assessment.lesson_entry_id:
            events.append(
                AnnotationEvent(
                    id=str(ULID()),
                    target_id=assessment.lesson_entry_id,
                    target_type="entry",
                    kind="tag",
                    value=LEARNING_EXTRACTED_TAG,
                    actor=_ACTOR,
                    timestamp=now,
                )
            )
        result = daemon_annotate(
            topic,
            code_root=code_root,
            events=events,
            agent=_ACTOR,
            agent_spec="learnings",
        )
        if not result.written:
            logger.debug(
                "DAEMON[learnings]: annotation write failed for %s: %s",
                topic,
                result.error,
            )

    # -- findings -------------------------------------------------------------

    def _learning_finding(self, topic: str, a: LearningAssessment) -> Finding:
        source = a.matched_doc or "in-thread lesson"
        return Finding(
            finding_id=str(ULID()),
            daemon_name=self.name,
            severity=a.severity or "info",
            category=CAT_LEARNING_EXTRACTED,
            topic=topic,
            message=f"Learning captured in '{topic}' ({source})",
            details={
                "triggering_criterion_id": a.triggering_criterion_id,
                "pr_numbers": list(a.pr_numbers),
                "matched_doc": a.matched_doc,
                "lesson_entry_id": a.lesson_entry_id,
            },
        )

    def _capture_gap_finding(self, topic: str, a: LearningAssessment) -> Finding:
        prs = ", ".join(f"#{n}" for n in a.pr_numbers)
        return Finding(
            finding_id=str(ULID()),
            daemon_name=self.name,
            severity=a.severity or "warning",
            category=CAT_CAPTURE_GAP,
            topic=topic,
            message=f"Capture gap in '{topic}': merged {prs} with no solutions write-up",
            details={
                "triggering_criterion_id": a.triggering_criterion_id,
                "pr_numbers": list(a.pr_numbers),
            },
        )

    # -- synthesis (LLM draft of the missing learning; shadow finding) --------

    def _maybe_synthesize(
        self, topic: str, entries: list[dict], a: LearningAssessment
    ) -> SynthesisResult | None:
        """LLM-draft the missing learning for a capture-gap; None if it doesn't pass.

        The caller probes client availability once per tick and only calls this
        when the client is reachable, so this always issues the LLM call (the
        per-tick budget is charged by the caller, including a floor-rejected draft).
        """
        llm = self._get_llm_client()
        result = synthesize_learning(
            topic,
            entries,
            llm_complete=lambda system, user: llm.complete(user, system=system),
            min_confidence=self._config.min_confidence,
        )
        return result if result.passed else None

    def _shadow_finding(
        self, topic: str, result: SynthesisResult, a: LearningAssessment
    ) -> Finding:
        """The watch-only 'would-have-written' shadow finding for a passing draft."""
        return Finding(
            finding_id=str(ULID()),
            daemon_name=self.name,
            severity="info",
            category=CAT_SHADOW_LEARNING_NOTE,
            topic=topic,
            message=f"Shadow learning note drafted for '{topic}' (confidence {result.confidence}/5)",
            details={
                "confidence": result.confidence,
                "draft_body": result.draft_body,
                "triggering_criterion_id": a.triggering_criterion_id,
                "pr_numbers": list(a.pr_numbers),
            },
        )

    # -- emit (Phase 2: thread-visible learning candidate Notes) --------------

    def _should_emit_candidate(self) -> bool:
        """Emit thread-visible candidate Notes only past monitor + opt-in, never
        under dry_run (it writes a real entry)."""
        return (
            not self._dry_run
            and self._config.emit_mode != "monitor"
            and self._config.emit_learning_notes
        )

    def _emit_learning_candidate(
        self,
        topic: str,
        code_root: Path,
        result: SynthesisResult,
        a: LearningAssessment,
        *,
        disposition_owner: str | None = None,
    ) -> Finding | None:
        """Write a thread-visible learning *candidate* Note to the source thread.

        Authority-safe: an ``entry_type="Note"`` marked ``needs_human_confirmation``
        / ``Authority: none`` (never Decision/Closure/supersession/status). Returns
        a ``learning_candidate_emitted`` finding on a durable write, else None.
        ``disposition_owner`` stamps the F1 owner marker at emission.
        """
        body = format_learning_candidate_body(
            result,
            topic=topic,
            pr_numbers=list(a.pr_numbers),
            disposition_owner=disposition_owner,
        )
        candidate_entry_id = str(ULID())
        write = daemon_write_entry(
            topic,
            code_root=code_root,
            title=f"Learning candidate: {topic}",
            body=body,
            agent=_ACTOR,
            role="scribe",
            entry_type="Note",
            entry_id=candidate_entry_id,
            agent_spec="learnings",
            user_tag="system",
            annotation_events=self._candidate_annotation_events(
                topic, candidate_entry_id
            ),
        )
        if not write.written:
            logger.debug("DAEMON[learnings]: candidate write failed for %s: %s", topic, write.error)
            return None
        return Finding(
            finding_id=str(ULID()),
            daemon_name=self.name,
            severity="info",
            category=CAT_LEARNING_CANDIDATE,
            topic=topic,
            message=f"Learning candidate Note emitted for '{topic}' → {write.entry_id}",
            details={"entry_id": write.entry_id, "confidence": result.confidence},
        )

    def _candidate_annotation_events(
        self, topic: str, entry_id: str
    ) -> list[AnnotationEvent]:
        """Annotation events tagging the candidate Note + thread.

        Returned to ``daemon_write_entry(annotation_events=...)`` so they commit
        in the same transaction as the Note — never via a bare
        ``append_annotation`` (``bug-sync-worktree-poisoning``).
        """
        now = datetime.now(timezone.utc).isoformat()
        return [
            AnnotationEvent(
                id=str(ULID()),
                target_id=target_id,
                target_type=target_type,
                kind="tag",
                value=value,
                actor=_ACTOR,
                timestamp=now,
            )
            for target_id, target_type, value in (
                (entry_id, "entry", LEARNING_CANDIDATE_TAG),
                (topic, "thread", HAS_LEARNING_CANDIDATE_TAG),
            )
        ]
