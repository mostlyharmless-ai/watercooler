"""State types for daemon management: findings, checkpoints, persistence.

Finding represents a single observation produced by a daemon tick.
DaemonCheckpoint tracks incremental processing state per daemon.
ThreadCheckpoint tracks per-thread scan state for efficient delta processing.

Storage layout:
    ~/.watercooler/daemons/<daemon_name>/
        checkpoint.json   — atomic write via temp+rename
        findings.jsonl    — append-only findings log
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default storage root
_DEFAULT_DAEMONS_DIR = Path.home() / ".watercooler" / "daemons"


def _daemons_dir() -> Path:
    """Return the daemons storage root, creating it if needed."""
    d = _DEFAULT_DAEMONS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _daemon_dir(daemon_name: str) -> Path:
    """Return the storage directory for a specific daemon.

    Raises:
        ValueError: If daemon_name contains path separators or traversal components.
    """
    # Guard against path traversal (e.g., "../../etc/passwd")
    if "/" in daemon_name or "\\" in daemon_name or daemon_name in (".", ".."):
        raise ValueError(f"Invalid daemon name: {daemon_name!r}")
    d = _daemons_dir() / daemon_name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ #
# Finding
# ------------------------------------------------------------------ #


@dataclass
class Finding:
    """A single observation produced by a daemon tick.

    Findings are informational — they describe issues or suggestions
    without taking action. They are persisted to JSONL for review.

    Attributes:
        finding_id: ULID or unique identifier
        daemon_name: Which daemon produced this finding
        severity: "info", "warning", or "error"
        category: Classification (e.g., "missing_status", "stale_thread")
        topic: Thread topic slug
        entry_id: Optional entry ULID if finding is entry-specific
        message: Human-readable description
        details: Structured payload with additional context
        created_at: Unix timestamp when finding was produced
        acknowledged: Whether a human has seen/dismissed this finding
    """

    finding_id: str
    daemon_name: str
    severity: str
    category: str
    topic: str
    entry_id: str = ""
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    acknowledged: bool = False

    def __post_init__(self) -> None:
        if self.created_at == 0.0:
            self.created_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Finding":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ------------------------------------------------------------------ #
# ThreadCheckpoint
# ------------------------------------------------------------------ #


@dataclass
class ThreadCheckpoint:
    """Per-thread scan state for incremental processing.

    Attributes:
        topic: Thread topic slug
        mtime: Last known modification time (file mtime)
        entry_count: Last known entry count
        last_audited: Unix timestamp of last successful audit
    """

    topic: str
    mtime: float = 0.0
    entry_count: int = 0
    last_audited: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ThreadCheckpoint":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ------------------------------------------------------------------ #
# DaemonCheckpoint
# ------------------------------------------------------------------ #


@dataclass
class DaemonCheckpoint:
    """Daemon-level checkpoint for incremental processing.

    Tracks overall daemon run state and per-thread scan state.

    Attributes:
        daemon_name: Which daemon this checkpoint belongs to
        last_run: Unix timestamp of last completed tick
        last_run_duration: Duration of last tick in seconds
        threads_processed: Count of threads processed in last tick
        threads_skipped: Count of unchanged threads skipped in last tick
        findings_produced: Count of findings produced in last tick
        error_count: Cumulative error count
        thread_state: Per-thread incremental tracking
    """

    daemon_name: str
    last_run: float = 0.0
    last_run_duration: float = 0.0
    threads_processed: int = 0
    threads_skipped: int = 0
    findings_produced: int = 0
    error_count: int = 0
    thread_state: Dict[str, ThreadCheckpoint] = field(default_factory=dict)

    def is_thread_changed(self, topic: str, mtime: float, entry_count: int) -> bool:
        """Check if a thread has changed since last audit."""
        tc = self.thread_state.get(topic)
        if tc is None:
            return True
        return tc.mtime != mtime or tc.entry_count != entry_count

    def update_thread(self, topic: str, mtime: float, entry_count: int) -> None:
        """Record that a thread was successfully audited."""
        self.thread_state[topic] = ThreadCheckpoint(
            topic=topic,
            mtime=mtime,
            entry_count=entry_count,
            last_audited=time.time(),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["thread_state"] = {
            k: v.to_dict() if isinstance(v, ThreadCheckpoint) else v
            for k, v in self.thread_state.items()
        }
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DaemonCheckpoint":
        ts_raw = d.get("thread_state", {})
        obj = cls(**{k: v for k, v in d.items()
                     if k in cls.__dataclass_fields__ and k != "thread_state"})
        obj.thread_state = {
            k: ThreadCheckpoint.from_dict(v) if isinstance(v, dict) else v
            for k, v in ts_raw.items()
        }
        return obj


# ------------------------------------------------------------------ #
# Persistence helpers
# ------------------------------------------------------------------ #


def save_checkpoint(checkpoint: DaemonCheckpoint) -> None:
    """Atomically write checkpoint to disk (temp + rename)."""
    d = _daemon_dir(checkpoint.daemon_name)
    path = d / "checkpoint.json"
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(checkpoint.to_dict(), f, indent=2)
        os.replace(tmp_path, str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_checkpoint(daemon_name: str) -> DaemonCheckpoint:
    """Load checkpoint from disk, returning a fresh one if not found."""
    path = _daemon_dir(daemon_name) / "checkpoint.json"
    if not path.exists():
        return DaemonCheckpoint(daemon_name=daemon_name)
    try:
        with open(path) as f:
            data = json.load(f)
        return DaemonCheckpoint.from_dict(data)
    except Exception as e:
        logger.warning("DAEMON[%s]: failed to load checkpoint: %s", daemon_name, e)
        return DaemonCheckpoint(daemon_name=daemon_name)


_findings_lock = threading.Lock()


def append_findings(daemon_name: str, findings: List[Finding]) -> None:
    """Append findings to the JSONL log file (thread-safe)."""
    if not findings:
        return
    path = _daemon_dir(daemon_name) / "findings.jsonl"
    with _findings_lock:
        with open(path, "a") as f:
            for finding in findings:
                f.write(json.dumps(finding.to_dict()) + "\n")


def load_findings(
    daemon_name: str,
    *,
    limit: int = 100,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    topic: Optional[str] = None,
    unacknowledged_only: bool = False,
) -> List[Finding]:
    """Load findings from JSONL log with optional filters.

    Returns findings in reverse chronological order (newest first).

    Note: Reads the entire JSONL file before filtering. On long-running
    instances with no rotation, this can grow large. A compaction/rotation
    mechanism should be added before production use.
    """
    path = _daemon_dir(daemon_name) / "findings.jsonl"
    if not path.exists():
        return []

    all_findings: List[Finding] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    finding = Finding.from_dict(data)
                    # Apply filters
                    if severity and finding.severity != severity:
                        continue
                    if category and finding.category != category:
                        continue
                    if topic and finding.topic != topic:
                        continue
                    if unacknowledged_only and finding.acknowledged:
                        continue
                    all_findings.append(finding)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("DAEMON[%s]: skipping malformed JSONL line: %s", daemon_name, e)
                    continue
    except Exception as e:
        logger.warning("DAEMON[%s]: failed to load findings: %s", daemon_name, e)

    # Reverse for newest-first, then apply limit
    all_findings.reverse()
    return all_findings[:limit]
