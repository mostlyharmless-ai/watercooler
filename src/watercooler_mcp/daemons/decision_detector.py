"""Decision Detector Daemon — continuous decision candidate scoring.

Scans baseline graph entries using deterministic NLP scoring
(``watercooler.decision_scoring``) and produces Finding objects for
entries that exceed the configured ``min_score`` threshold.

Signals available in daemon context:
- Signal 1: title/type heuristics + phrase lexicons (always)
- Signal 2: inline keyword matching across title/body/summary/topic (always)
- Signal 3: T2 supersession proxy (deferred to v2)

This daemon is findings-only — it never writes to thread files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ulid import ULID

from watercooler.baseline_graph import storage
from watercooler.baseline_graph.storage import get_graph_dir
from watercooler.baseline_graph.writer import (
    get_entries_for_thread,
    get_thread_from_graph,
)
from watercooler.config_schema import DecisionDetectorConfig
from watercooler.decision_scoring import score_entry

from .base import BaseDaemon
from .state import Finding, load_findings

logger = logging.getLogger(__name__)

# Re-sync dedup cache from disk every N ticks to evict acknowledged/compacted keys
_DEDUP_RESYNC_INTERVAL = 12

# Hard cap on findings loaded for dedup — prevents unbounded memory growth.
# Note: load_findings reads the entire JSONL before slicing; this relies on
# periodic findings compaction to keep file size bounded.
_DEDUP_LIMIT = 50_000

# Signal 2 keyword list — matches SKILL.md:143.
# Multi-word tokens use spaces (not hyphens) to match natural-language text.
_DECISION_KEYWORDS: list[str] = [
    "decided",
    "resolved",
    "committed",
    "opted",
    "agreed",
    "chosen",
    "selected",
    "finalized",
    "going forward",
    "we will",
]

# Fields to check for keyword matching (same as _matches_keyword in search.py:465).
# Topic is checked separately via the `topic` parameter since entry dicts from
# get_entries_for_thread() don't include a topic field.
_KEYWORD_FIELDS = ("title", "body", "summary")


def _make_finding_id() -> str:
    """Generate a unique, time-sortable finding ID (ULID)."""
    return str(ULID())


def _compute_search_hit(entry: dict[str, Any]) -> bool:
    """Inline Signal 2: check if any decision keyword appears in entry fields.

    Matches entry content only (title/body/summary). Topic slug is not
    checked — a keyword in the slug would inflate scores for every entry
    in that thread regardless of content.
    """
    for field_name in _KEYWORD_FIELDS:
        value = entry.get(field_name, "")
        if not value:
            continue
        text = str(value).lower()
        if any(kw in text for kw in _DECISION_KEYWORDS):
            return True
    return False


class DetectDecisionsDaemon(BaseDaemon):
    """Non-destructive decision candidate scanner.

    Reads baseline graph entries, scores them for decision-candidate
    likelihood, and produces findings for entries above ``min_score``.

    Args:
        interval: Seconds between scans.
        config: DecisionDetectorConfig for scoring thresholds.
        threads_dir: Override threads directory (None = resolve at tick time).
        enabled: Whether this daemon is active.
    """

    def __init__(
        self,
        *,
        interval: float = 300.0,
        config: DecisionDetectorConfig | None = None,
        threads_dir: Path | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            name="decision_detector",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._config = config or DecisionDetectorConfig()
        self._threads_dir_override = threads_dir
        self._resolved_threads_dir: Path | None = None
        # Dedup cache: (topic, category, entry_id) -> already reported
        self._existing_keys: set[tuple[str, str, str]] = set()
        self._ticks_since_resync: int = 0
        # Per-tick metrics for status_summary()
        self._last_tick_scored: int = 0
        self._last_tick_findings: int = 0
        self._last_tick_skipped_threads: int = 0

    def _resolve_threads_dir(self) -> Path | None:
        """Resolve the threads directory for scanning.

        Cached after first successful resolution to avoid CWD drift.
        """
        if self._threads_dir_override is not None:
            return self._threads_dir_override

        if self._resolved_threads_dir is not None:
            return self._resolved_threads_dir

        try:
            from watercooler_mcp.config import resolve_thread_context
            ctx = resolve_thread_context(Path.cwd())
            self._resolved_threads_dir = ctx.threads_dir
            return self._resolved_threads_dir
        except Exception as exc:
            logger.debug(
                "DAEMON[decision_detector]: could not resolve threads_dir: %s", exc
            )
            return None

    def tick(self) -> list[Finding]:
        """Run one detection cycle over all threads from graph."""
        from .hosted_data import is_daemon_hosted_mode
        if is_daemon_hosted_mode() and self._threads_dir_override is None:
            return self._tick_hosted()

        threads_dir = self._resolve_threads_dir()
        if threads_dir is None or not threads_dir.exists():
            logger.debug("DAEMON[decision_detector]: no threads_dir, skipping")
            self._update_tick_metrics(0, 0, 0)
            return []

        graph_dir = get_graph_dir(threads_dir)
        topics = storage.list_thread_topics(graph_dir)
        if not topics:
            self._update_tick_metrics(0, 0, 0)
            return []

        # Bootstrap or periodically re-sync dedup set from disk
        self._ticks_since_resync += 1
        if not self._existing_keys or self._ticks_since_resync >= _DEDUP_RESYNC_INTERVAL:
            existing = load_findings(self.name, limit=_DEDUP_LIMIT, unacknowledged_only=True)
            if len(existing) >= _DEDUP_LIMIT:
                logger.warning(
                    "DAEMON[decision_detector]: dedup cache truncated at %d findings; "
                    "duplicates may occur",
                    _DEDUP_LIMIT,
                )
            self._existing_keys = {
                (f.topic, f.category, f.entry_id or "") for f in existing
            }
            self._ticks_since_resync = 0

        cfg = self._config
        findings: list[Finding] = []
        scored_total = 0
        skipped = 0

        for topic in topics:
            if len(findings) >= cfg.max_findings_per_run:
                break

            # Optionally skip closed threads
            if not cfg.scan_closed_threads:
                try:
                    thread_node = get_thread_from_graph(threads_dir, topic)
                    if thread_node:
                        status = thread_node.get("status", "")
                        if status.upper() in ("CLOSED", "RESOLVED", "MERGED", "DONE"):
                            skipped += 1
                            continue
                except (OSError, KeyError, ValueError):
                    pass

            # Get entries from graph
            try:
                entries = get_entries_for_thread(threads_dir, topic)
                entry_count = len(entries)
            except (OSError, KeyError, ValueError) as exc:
                logger.debug(
                    "DAEMON[decision_detector]: error reading graph for %s: %s",
                    topic, exc,
                )
                continue

            # Incremental: skip unchanged threads
            try:
                meta_file = storage.get_thread_graph_dir(graph_dir, topic) / "meta.json"
                mtime = meta_file.stat().st_mtime if meta_file.exists() else 0.0
            except OSError:
                mtime = 0.0

            if not self._checkpoint.is_thread_changed(topic, mtime, entry_count):
                skipped += 1
                continue

            # Score entries in this thread
            cap_hit = False
            for entry in entries:
                if len(findings) >= cfg.max_findings_per_run:
                    cap_hit = True
                    break

                # Per-entry exception isolation
                try:
                    # Coerce entry_id to str at ingestion boundary
                    raw_eid = entry.get("entry_id", "")
                    entry_id = str(raw_eid) if raw_eid else ""

                    # Skip daemon-written entries (prevent feedback loops)
                    entry_agent = str(entry.get("agent", ""))
                    if any(
                        entry_agent.startswith(prefix)
                        for prefix in cfg.exclude_agents
                    ):
                        continue

                    # Compute Signal 2 inline
                    search_hit = _compute_search_hit(entry)

                    # Build scoring input
                    scoring_input = {
                        "entry_id": entry_id,
                        "thread_topic": topic,
                        "entry_type": entry.get("entry_type", "Note"),
                        "title": entry.get("title", "") or "",
                        "summary": entry.get("summary", "") or "",
                        "search_hit": search_hit,
                    }

                    scored = score_entry(
                        scoring_input,
                        fuzzy_threshold=cfg.fuzzy_threshold,
                    )
                    scored_total += 1

                    if scored["score"] < cfg.min_score:
                        continue

                    # Dedup check
                    key = (topic, "decision_candidate", entry_id)
                    if key in self._existing_keys:
                        continue

                    findings.append(Finding(
                        finding_id=_make_finding_id(),
                        daemon_name=self.name,
                        severity="info",
                        category="decision_candidate",
                        topic=topic,
                        entry_id=entry_id,
                        message=(
                            f"Decision candidate in '{topic}': "
                            f"{scored['title'][:60] or '(untitled)'} "
                            f"(score={scored['score']}, tier={scored['tier']})"
                        ),
                        details={
                            "score": scored["score"],
                            "tier": scored["tier"],
                            "signals": scored["signals"],
                            "matched_phrases": scored["matched_phrases"],
                            "signals_available": [
                                "s1_title_type",
                                "s2_keyword_match",
                            ],
                        },
                    ))
                    self._existing_keys.add(key)

                except Exception as exc:
                    logger.debug(
                        "DAEMON[decision_detector]: error scoring entry in %s: %s",
                        topic, exc,
                    )
                    continue

            # Checkpoint safety: only update if we fully scanned the thread
            # (not interrupted by max_findings_per_run cap)
            if not cap_hit:
                self._checkpoint.update_thread(topic, mtime, entry_count)

        # Prune stale checkpoint entries for topics no longer in graph
        live_topics = set(topics)
        stale = [t for t in self._checkpoint.thread_state if t not in live_topics]
        for t in stale:
            del self._checkpoint.thread_state[t]

        self._update_tick_metrics(scored_total, len(findings), skipped)

        logger.debug(
            "DAEMON[decision_detector]: scored %d entries, %d findings, %d threads skipped",
            scored_total, len(findings), skipped,
        )
        return findings

    def _tick_hosted(self) -> list[Finding]:
        """Hosted mode tick — reads entries from GitHub API."""
        from .hosted_data import (
            list_topics_for_daemon,
            get_entries_for_daemon,
            get_thread_meta_for_daemon,
            get_thread_change_marker,
        )

        topics = list_topics_for_daemon()
        if not topics:
            self._update_tick_metrics(0, 0, 0)
            return []

        # Bootstrap or periodically re-sync dedup set from disk
        self._ticks_since_resync += 1
        if not self._existing_keys or self._ticks_since_resync >= _DEDUP_RESYNC_INTERVAL:
            existing = load_findings(self.name, limit=_DEDUP_LIMIT, unacknowledged_only=True)
            self._existing_keys = {
                (f.topic, f.category, f.entry_id or "") for f in existing
            }
            self._ticks_since_resync = 0

        cfg = self._config
        findings: list[Finding] = []
        scored_total = 0
        skipped = 0

        for topic in topics:
            if len(findings) >= cfg.max_findings_per_run:
                break

            # Optionally skip closed threads
            if not cfg.scan_closed_threads:
                meta = get_thread_meta_for_daemon(topic)
                if meta:
                    status = meta.get("status", "")
                    if status.upper() in ("CLOSED", "RESOLVED", "MERGED", "DONE"):
                        skipped += 1
                        continue

            entries = get_entries_for_daemon(topic)
            entry_count = len(entries)
            if not entries:
                continue

            # Incremental: skip unchanged threads
            mtime, _ = get_thread_change_marker(topic)
            if not self._checkpoint.is_thread_changed(topic, mtime, entry_count):
                skipped += 1
                continue

            cap_hit = False
            for entry in entries:
                if len(findings) >= cfg.max_findings_per_run:
                    cap_hit = True
                    break

                try:
                    raw_eid = entry.get("entry_id", "")
                    entry_id = str(raw_eid) if raw_eid else ""
                    search_hit = _compute_search_hit(entry)

                    scoring_input = {
                        "entry_id": entry_id,
                        "thread_topic": topic,
                        "entry_type": entry.get("entry_type", "Note"),
                        "title": entry.get("title", "") or "",
                        "summary": entry.get("summary", "") or "",
                        "search_hit": search_hit,
                    }

                    scored = score_entry(
                        scoring_input,
                        fuzzy_threshold=cfg.fuzzy_threshold,
                    )
                    scored_total += 1

                    if scored["score"] < cfg.min_score:
                        continue

                    key = (topic, "decision_candidate", entry_id)
                    if key in self._existing_keys:
                        continue

                    findings.append(Finding(
                        finding_id=_make_finding_id(),
                        daemon_name=self.name,
                        severity="info",
                        category="decision_candidate",
                        topic=topic,
                        entry_id=entry_id,
                        message=(
                            f"Decision candidate in '{topic}': "
                            f"{scored['title'][:60] or '(untitled)'} "
                            f"(score={scored['score']}, tier={scored['tier']})"
                        ),
                        details={
                            "score": scored["score"],
                            "tier": scored["tier"],
                            "signals": scored["signals"],
                            "matched_phrases": scored["matched_phrases"],
                            "signals_available": [
                                "s1_title_type",
                                "s2_keyword_match",
                            ],
                        },
                    ))
                    self._existing_keys.add(key)
                except Exception as exc:
                    logger.debug(
                        "DAEMON[decision_detector]: hosted error scoring entry in %s: %s",
                        topic, exc,
                    )
                    continue

            if not cap_hit:
                self._checkpoint.update_thread(topic, mtime, entry_count)

        self._update_tick_metrics(scored_total, len(findings), skipped)
        return findings

    def _update_tick_metrics(
        self, scored: int, findings_count: int, skipped: int
    ) -> None:
        """Update per-tick metrics for status_summary()."""
        self._last_tick_scored = scored
        self._last_tick_findings = findings_count
        self._last_tick_skipped_threads = skipped

    def status_summary(self) -> dict[str, Any]:
        """Health summary with decision-detector-specific metrics.

        Overrides BaseDaemon.status_summary() to expose per-tick scoring
        counts. Follows the T2IndexerDaemon pattern.
        """
        base = super().status_summary()
        base["last_tick_scored"] = self._last_tick_scored
        base["last_tick_findings"] = self._last_tick_findings
        base["last_tick_skipped_threads"] = self._last_tick_skipped_threads
        return base
