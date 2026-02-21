"""Thread Auditor Daemon — non-destructive thread hygiene scanner.

Scans thread files, produces findings for missing metadata, stale
threads, and classification suggestions. Never writes to thread files.

Checks performed:
- missing_status: Thread has no Status: header line
- missing_ball: Thread has no Ball: header line
- missing_entry_id: Entry has no Entry-ID comment
- missing_thread_summary: Thread node in graph has no summary
- missing_entry_summary: Entry node in graph has no summary
- stale_thread: Thread has no activity in N days
- classification_suggestion: Thread may belong in a different directory
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from watercooler.config_schema import ThreadAuditorConfig
from watercooler.fs import (
    CLOSED_STATES,
    THREAD_CATEGORIES,
    has_structured_layout,
    is_closed,
    thread_path,
)
from watercooler.baseline_graph import storage
from watercooler.baseline_graph.storage import get_graph_dir
from watercooler.baseline_graph.writer import (
    get_thread_from_graph,
    get_entries_for_thread,
)

from ulid import ULID

from .base import BaseDaemon
from .state import Finding, load_findings

logger = logging.getLogger(__name__)

# Seconds per day for stale thread calculation
_SECONDS_PER_DAY = 86400


def _make_finding_id() -> str:
    """Generate a unique, time-sortable finding ID (ULID)."""
    return str(ULID())


def _get_entry_field(entry: Any, field: str, default: Any = "") -> Any:
    """Get a field from an entry that may be a dict or object."""
    if isinstance(entry, dict):
        return entry.get(field, default)
    return getattr(entry, field, default)


class ThreadAuditorDaemon(BaseDaemon):
    """Non-destructive thread auditor.

    Reads threads, produces findings, never writes. Uses incremental
    processing via mtime + entry_count to skip unchanged threads.

    Args:
        interval: Seconds between scans
        config: ThreadAuditorConfig for check toggles
        threads_dir: Override threads directory (None = resolve at tick time)
    """

    def __init__(
        self,
        *,
        interval: float = 300.0,
        config: Optional[ThreadAuditorConfig] = None,
        threads_dir: Optional[Path] = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            name="thread_auditor",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._config = config or ThreadAuditorConfig()
        self._threads_dir_override = threads_dir
        self._resolved_threads_dir: Optional[Path] = None
        # Cache dedup keys across ticks to avoid re-reading JSONL every cycle
        self._existing_keys: set[tuple[str, str, str]] = set()

    def _resolve_threads_dir(self) -> Optional[Path]:
        """Resolve the threads directory for scanning.

        The result is cached after first successful resolution to avoid
        depending on Path.cwd() in a long-running background thread (CWD
        could drift after startup).
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
            logger.debug("DAEMON[thread_auditor]: could not resolve threads_dir: %s", exc)
            return None

    def tick(self) -> List[Finding]:
        """Run one audit cycle over all threads from graph."""
        threads_dir = self._resolve_threads_dir()
        if threads_dir is None or not threads_dir.exists():
            logger.debug("DAEMON[thread_auditor]: no threads_dir, skipping")
            return []

        graph_dir = get_graph_dir(threads_dir)
        topics = storage.list_thread_topics(graph_dir)
        if not topics:
            return []

        # Bootstrap dedup set from disk on first tick; subsequent ticks
        # reuse the in-memory set (new findings are added as they're created).
        # 50K limit is well above the JSONL compaction ceiling (10K lines,
        # kept at 5K) — ensures complete dedup even if thresholds change.
        if not self._existing_keys:
            existing = load_findings(self.name, limit=50_000, unacknowledged_only=True)
            self._existing_keys = {
                (f.topic, f.category, f.entry_id or "") for f in existing
            }

        cfg = self._config
        findings: List[Finding] = []
        processed = 0
        skipped = 0
        structured = has_structured_layout(threads_dir)

        for topic in topics:
            if len(findings) >= cfg.max_findings_per_run:
                break

            tp = thread_path(topic, threads_dir)

            # Get graph metadata — skip thread if meta is unavailable
            try:
                thread_node = get_thread_from_graph(threads_dir, topic)
                if thread_node is None:
                    logger.debug("DAEMON[thread_auditor]: no graph meta for %s, skipping", topic)
                    continue
                graph_entries = get_entries_for_thread(threads_dir, topic)
                entry_count = len(graph_entries)
            except (OSError, KeyError, ValueError) as exc:
                logger.debug("DAEMON[thread_auditor]: error reading graph for %s: %s", topic, exc)
                continue

            # Incremental: skip unchanged threads (use entry count as proxy)
            # Use mtime of meta.json if available, otherwise 0
            try:
                meta_file = storage.get_thread_graph_dir(graph_dir, topic) / "meta.json"
                mtime = meta_file.stat().st_mtime if meta_file.exists() else 0.0
            except OSError:
                mtime = 0.0

            if not self._checkpoint.is_thread_changed(topic, mtime, entry_count):
                skipped += 1
                continue

            processed += 1

            # Extract metadata from graph node
            title = thread_node.get("title", "") if thread_node else ""
            status = thread_node.get("status", "") if thread_node else ""
            ball = thread_node.get("ball", "") if thread_node else ""
            last_ts = thread_node.get("last_updated", "") if thread_node else ""

            # Check for missing fields directly from graph dict
            has_status_line = bool(status.strip())
            has_ball_line = bool(ball.strip())

            # Run configured checks
            thread_findings = self._audit_thread(
                topic=topic,
                thread_file=tp,
                threads_dir=threads_dir,
                title=title,
                status=status,
                ball=ball,
                last_ts=last_ts,
                entries=graph_entries,
                structured=structured,
                has_status_line=has_status_line,
                has_ball_line=has_ball_line,
                thread_node=thread_node,
            )

            # Deduplicate: skip findings that already exist unacknowledged
            for f in thread_findings:
                key = (f.topic, f.category, f.entry_id or "")
                if key not in self._existing_keys:
                    findings.append(f)
                    self._existing_keys.add(key)

            # Update checkpoint for this thread
            self._checkpoint.update_thread(topic, mtime, entry_count)

        self._checkpoint.threads_processed = processed
        self._checkpoint.threads_skipped = skipped

        logger.debug(
            "DAEMON[thread_auditor]: scanned %d threads (%d skipped), %d new findings",
            processed, skipped, len(findings),
        )
        return findings

    def _audit_thread(
        self,
        *,
        topic: str,
        thread_file: Path,
        threads_dir: Path,
        title: str,
        status: str,
        ball: str,
        last_ts: str,
        entries: List[Dict[str, Any]],
        structured: bool,
        has_status_line: bool = False,
        has_ball_line: bool = False,
        thread_node: Optional[Dict[str, Any]] = None,
    ) -> List[Finding]:
        """Run all configured checks on a single thread."""
        cfg = self._config
        findings: List[Finding] = []

        # Check missing status (uses raw regex match, not parsed default)
        if cfg.check_missing_status and not has_status_line:
            findings.append(Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="warning",
                category="missing_status",
                topic=topic,
                message=f"Thread '{topic}' has no Status: header",
                details={"thread_path": str(thread_file)},
            ))

        # Check missing ball (uses raw regex match, not parsed default)
        if cfg.check_missing_ball and not has_ball_line:
            findings.append(Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="info",
                category="missing_ball",
                topic=topic,
                message=f"Thread '{topic}' has no Ball: header",
                details={"thread_path": str(thread_file)},
            ))

        # Check missing entry IDs
        if cfg.check_missing_entry_ids:
            for entry in entries:
                eid = _get_entry_field(entry, "entry_id")
                idx = _get_entry_field(entry, "index", 0)
                agent = _get_entry_field(entry, "agent")
                ts = _get_entry_field(entry, "timestamp")
                if not eid:
                    findings.append(Finding(
                        finding_id=_make_finding_id(),
                        daemon_name=self.name,
                        severity="warning",
                        category="missing_entry_id",
                        topic=topic,
                        entry_id="",
                        message=f"Entry #{idx} in '{topic}' has no Entry-ID",
                        details={
                            "entry_index": idx,
                            "agent": agent,
                            "timestamp": ts,
                        },
                    ))

        # Check stale threads (using graph last_updated timestamp)
        if cfg.check_stale_threads and status.strip():
            thread_closed = is_closed(status)
            if not thread_closed and last_ts:
                stale_threshold = time.time() - (cfg.stale_days * _SECONDS_PER_DAY)
                try:
                    # Parse ISO 8601 timestamp from graph
                    ts_str = last_ts.replace("Z", "+00:00")
                    last_epoch = datetime.fromisoformat(ts_str).timestamp()
                except (ValueError, TypeError) as exc:
                    logger.debug(
                        "DAEMON[thread_auditor]: could not parse last_updated for %s: %s",
                        topic, exc,
                    )
                    last_epoch = 0.0

                if last_epoch > 0 and last_epoch < stale_threshold:
                    days_idle = int((time.time() - last_epoch) / _SECONDS_PER_DAY)
                    findings.append(Finding(
                        finding_id=_make_finding_id(),
                        daemon_name=self.name,
                        severity="info",
                        category="stale_thread",
                        topic=topic,
                        message=f"Thread '{topic}' has been idle for {days_idle} days",
                        details={
                            "days_idle": days_idle,
                            "stale_days_threshold": cfg.stale_days,
                            "status": status,
                        },
                    ))

        # Check classification (structured layout only)
        if cfg.check_classification and structured:
            findings.extend(self._check_classification(
                topic=topic,
                thread_file=thread_file,
                threads_dir=threads_dir,
                status=status,
            ))

        # Check missing graph summaries (best-effort, non-blocking)
        if cfg.check_missing_summaries:
            findings.extend(self._check_missing_summaries(
                topic=topic,
                thread_node=thread_node,
                entries=entries,
            ))

        return findings

    def _check_classification(
        self,
        *,
        topic: str,
        thread_file: Path,
        threads_dir: Path,
        status: str,
    ) -> List[Finding]:
        """Check if a thread is in the correct directory."""
        findings: List[Finding] = []

        # Determine current category from path
        try:
            relative = thread_file.relative_to(threads_dir)
            parts = relative.parts
            current_cat = parts[0] if len(parts) > 1 else "root"
        except ValueError:
            return findings

        thread_closed = is_closed(status)

        # Closed thread not in closed/ directory
        if thread_closed and current_cat != "closed":
            findings.append(Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="info",
                category="classification_suggestion",
                topic=topic,
                message=f"Thread '{topic}' is {status} but in '{current_cat}/' (suggest moving to closed/)",
                details={
                    "current_category": current_cat,
                    "suggested_category": "closed",
                    "status": status,
                },
            ))

        # Open/active thread sitting in closed/ directory
        if not thread_closed and current_cat == "closed":
            findings.append(Finding(
                finding_id=_make_finding_id(),
                daemon_name=self.name,
                severity="info",
                category="classification_suggestion",
                topic=topic,
                message=f"Thread '{topic}' is {status} but in 'closed/' (suggest moving to threads/)",
                details={
                    "current_category": current_cat,
                    "suggested_category": "threads",
                    "status": status,
                },
            ))

        return findings

    def _check_missing_summaries(
        self,
        *,
        topic: str,
        thread_node: Optional[Dict[str, Any]],
        entries: list,
    ) -> List[Finding]:
        """Check for missing graph summaries (best-effort).

        Args:
            thread_node: Thread dict from get_thread_from_graph() (already fetched by tick)
            entries: List of graph entry dicts from get_entries_for_thread()
        """
        findings: List[Finding] = []
        try:
            if thread_node is not None:
                summary = thread_node.get("summary", "")
                if not summary:
                    findings.append(Finding(
                        finding_id=_make_finding_id(),
                        daemon_name=self.name,
                        severity="info",
                        category="missing_thread_summary",
                        topic=topic,
                        message=f"Thread '{topic}' graph node has no summary",
                    ))

            for entry in entries:
                eid = _get_entry_field(entry, "entry_id")
                if eid and not _get_entry_field(entry, "summary"):
                    findings.append(Finding(
                        finding_id=_make_finding_id(),
                        daemon_name=self.name,
                        severity="info",
                        category="missing_entry_summary",
                        topic=topic,
                        entry_id=eid,
                        message=f"Entry '{eid}' in '{topic}' has no graph summary",
                    ))

        except (KeyError, TypeError) as exc:
            logger.debug("DAEMON[thread_auditor]: graph summary check failed for %s: %s", topic, exc)

        return findings
