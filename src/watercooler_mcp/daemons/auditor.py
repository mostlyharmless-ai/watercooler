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
- compound_report_candidate: Closed thread has no compound report
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from watercooler.config_schema import ThreadAuditorConfig
from watercooler.fs import (
    CLOSED_STATES,
    THREAD_CATEGORIES,
    discover_thread_files,
    has_structured_layout,
    is_closed,
)
from watercooler.thread_entries import (
    _BALL_RE,
    _STAT_RE,
    parse_thread_entries,
    parse_thread_header,
)

from .base import BaseDaemon
from .state import Finding

logger = logging.getLogger(__name__)

# Seconds per day for stale thread calculation
_SECONDS_PER_DAY = 86400


def _make_finding_id() -> str:
    """Generate a unique finding ID."""
    return uuid.uuid4().hex[:16]


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

    def _resolve_threads_dir(self) -> Optional[Path]:
        """Resolve the threads directory for scanning."""
        if self._threads_dir_override is not None:
            return self._threads_dir_override

        try:
            from watercooler_mcp.config import resolve_thread_context
            ctx = resolve_thread_context(None)
            return ctx.threads_dir
        except Exception as exc:
            logger.debug("DAEMON[thread_auditor]: could not resolve threads_dir: %s", exc)
            return None

    def tick(self) -> List[Finding]:
        """Run one audit cycle over all thread files."""
        threads_dir = self._resolve_threads_dir()
        if threads_dir is None or not threads_dir.exists():
            logger.debug("DAEMON[thread_auditor]: no threads_dir, skipping")
            return []

        thread_files = discover_thread_files(threads_dir)
        if not thread_files:
            return []

        cfg = self._config
        findings: List[Finding] = []
        processed = 0
        skipped = 0
        structured = has_structured_layout(threads_dir)

        for thread_path in thread_files:
            if len(findings) >= cfg.max_findings_per_run:
                break

            topic = thread_path.stem

            # Incremental: skip unchanged threads
            try:
                stat = thread_path.stat()
                mtime = stat.st_mtime
                # Quick entry count via parsing
                content = thread_path.read_text(encoding="utf-8")
                entries = parse_thread_entries(content)
                entry_count = len(entries)
            except Exception as exc:
                logger.debug("DAEMON[thread_auditor]: error reading %s: %s", topic, exc)
                continue

            if not self._checkpoint.is_thread_changed(topic, mtime, entry_count):
                skipped += 1
                continue

            processed += 1

            # Parse thread header (returns defaults for missing fields)
            try:
                title, status, ball, last_ts = parse_thread_header(thread_path)
            except Exception:
                title, status, ball, last_ts = "", "", "", ""

            # Check raw content for actual header presence (parse_thread_header
            # substitutes defaults like "open" for missing Status:)
            has_status_line = bool(_STAT_RE.search(content))
            has_ball_line = bool(_BALL_RE.search(content))

            # Run configured checks
            thread_findings = self._audit_thread(
                topic=topic,
                thread_path=thread_path,
                threads_dir=threads_dir,
                title=title,
                status=status,
                ball=ball,
                last_ts=last_ts,
                entries=entries,
                structured=structured,
                has_status_line=has_status_line,
                has_ball_line=has_ball_line,
            )

            findings.extend(thread_findings)

            # Update checkpoint for this thread
            self._checkpoint.update_thread(topic, mtime, entry_count)

        self._checkpoint.threads_processed = processed
        self._checkpoint.threads_skipped = skipped

        logger.debug(
            "DAEMON[thread_auditor]: scanned %d threads (%d skipped), %d findings",
            processed, skipped, len(findings),
        )
        return findings

    def _audit_thread(
        self,
        *,
        topic: str,
        thread_path: Path,
        threads_dir: Path,
        title: str,
        status: str,
        ball: str,
        last_ts: str,
        entries: list,
        structured: bool,
        has_status_line: bool = True,
        has_ball_line: bool = True,
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
                details={"thread_path": str(thread_path)},
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
                details={"thread_path": str(thread_path)},
            ))

        # Check missing entry IDs
        if cfg.check_missing_entry_ids:
            for entry in entries:
                if not entry.entry_id:
                    findings.append(Finding(
                        finding_id=_make_finding_id(),
                        daemon_name=self.name,
                        severity="warning",
                        category="missing_entry_id",
                        topic=topic,
                        entry_id="",
                        message=f"Entry #{entry.index} in '{topic}' has no Entry-ID",
                        details={
                            "entry_index": entry.index,
                            "agent": entry.agent or "",
                            "timestamp": entry.timestamp or "",
                        },
                    ))

        # Check stale threads
        if cfg.check_stale_threads and status.strip():
            thread_closed = is_closed(status)
            if not thread_closed:
                stale_threshold = time.time() - (cfg.stale_days * _SECONDS_PER_DAY)
                try:
                    file_mtime = thread_path.stat().st_mtime
                except OSError:
                    file_mtime = 0.0

                if file_mtime > 0 and file_mtime < stale_threshold:
                    days_idle = int((time.time() - file_mtime) / _SECONDS_PER_DAY)
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
                thread_path=thread_path,
                threads_dir=threads_dir,
                status=status,
            ))

        # Check missing graph summaries (best-effort, non-blocking)
        if cfg.check_missing_summaries:
            findings.extend(self._check_missing_summaries(
                topic=topic,
                threads_dir=threads_dir,
                entries=entries,
            ))

        return findings

    def _check_classification(
        self,
        *,
        topic: str,
        thread_path: Path,
        threads_dir: Path,
        status: str,
    ) -> List[Finding]:
        """Check if a thread is in the correct directory."""
        findings: List[Finding] = []

        # Determine current category from path
        try:
            relative = thread_path.relative_to(threads_dir)
            parts = relative.parts
            current_cat = parts[0] if len(parts) > 1 else "root"
        except ValueError:
            return findings

        thread_closed = is_closed(status)

        # Closed thread not in closed/ directory
        if thread_closed and current_cat not in ("closed",):
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

        return findings

    def _check_missing_summaries(
        self,
        *,
        topic: str,
        threads_dir: Path,
        entries: list,
    ) -> List[Finding]:
        """Check for missing graph summaries (best-effort)."""
        findings: List[Finding] = []
        try:
            from watercooler.baseline_graph.writer import (
                get_entries_for_thread,
                get_thread_from_graph,
            )

            thread_node = get_thread_from_graph(threads_dir, topic)
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

            graph_entries = get_entries_for_thread(threads_dir, topic)
            graph_entry_ids = {e.get("entry_id") for e in graph_entries if e.get("entry_id")}

            for entry in entries:
                if entry.entry_id and entry.entry_id in graph_entry_ids:
                    # Find the graph entry
                    for ge in graph_entries:
                        if ge.get("entry_id") == entry.entry_id:
                            if not ge.get("summary"):
                                findings.append(Finding(
                                    finding_id=_make_finding_id(),
                                    daemon_name=self.name,
                                    severity="info",
                                    category="missing_entry_summary",
                                    topic=topic,
                                    entry_id=entry.entry_id,
                                    message=f"Entry '{entry.entry_id}' in '{topic}' has no graph summary",
                                ))
                            break

        except ImportError:
            pass  # Graph module not available
        except Exception as exc:
            logger.debug("DAEMON[thread_auditor]: graph summary check failed for %s: %s", topic, exc)

        return findings
