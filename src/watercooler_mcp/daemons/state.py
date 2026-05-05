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

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from watercooler_mcp.sync.file_lock import file_lock

logger = logging.getLogger(__name__)

# Cross-process advisory lock timeouts. Daemon ticks acquiring these
# locks are not in a hot path; 30 s is generous enough to ride out a
# slow compaction or checkpoint write by a peer process while still
# failing fast on a stuck holder. Findings and checkpoints have
# distinct constants so each can be tuned without affecting the other.
_FINDINGS_LOCK_TIMEOUT_S = 30.0
_CHECKPOINT_LOCK_TIMEOUT_S = 30.0


def _findings_lock_path(
    daemon_name: str, namespace: str = "", *, _allow_unscoped: bool = False
) -> Path:
    """Return the per-daemon-namespace cross-process lock sentinel path."""
    return (
        _daemon_dir(daemon_name, namespace=namespace, _allow_unscoped=_allow_unscoped)
        / ".findings.lock"
    )


def _checkpoint_lock_path(
    daemon_name: str, namespace: str = "", *, _allow_unscoped: bool = False
) -> Path:
    """Return the per-daemon-namespace checkpoint cross-process lock path."""
    return (
        _daemon_dir(daemon_name, namespace=namespace, _allow_unscoped=_allow_unscoped)
        / ".checkpoint.lock"
    )


# Default storage root
_DEFAULT_DAEMONS_DIR = Path.home() / ".watercooler" / "daemons"


def _daemons_dir() -> Path:
    """Return the daemons storage root, creating it if needed."""
    d = _DEFAULT_DAEMONS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


_DAEMON_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _findings_strict_namespace() -> bool:
    """Return True when ``WATERCOOLER_FINDINGS_STRICT_NAMESPACE`` is set
    to a truthy value.

    Plan v5.1 Sprint 4 enforcement flag for Move 3. In strict mode an
    empty *namespace* is a hard error rather than a silent fallback to
    the un-namespaced root path. The plan rationale: a missing scope
    is the A1 cross-tenant attack surface (findings written to a
    shared root path lose tenant isolation), so under enforce mode
    an empty namespace must raise rather than be tolerated.

    Reads the env var on every call so tests can monkeypatch it. The
    cost is one ``os.getenv`` per ``_daemon_dir`` invocation, which
    is already on the I/O path (the function does an mkdir).
    """
    return os.getenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", "0").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _daemon_dir(
    daemon_name: str,
    namespace: str = "",
    *,
    _allow_unscoped: bool = False,
) -> Path:
    """Return the storage directory for a specific daemon.

    When *namespace* is non-empty the path becomes
    ``<daemons_root>/<sanitised_namespace>/<daemon_name>/``, providing
    per-scope isolation for hosted deployments.

    Args:
        daemon_name: Daemon identifier (must match
            ``[A-Za-z0-9_-]+``).
        namespace: Per-scope namespace. Empty in single-tenant local
            mode; required (or ``_allow_unscoped=True``) under
            ``WATERCOOLER_FINDINGS_STRICT_NAMESPACE=1``.
        _allow_unscoped: Explicit exemption for administrative or
            diagnostic call sites that legitimately need
            un-namespaced access (e.g., the ``daemon-reset`` admin
            tool that operates on a single-tenant local checkpoint
            outside any user scope). Greppable as the audit anchor
            — every use should be reviewable. Underscore prefix
            flags this as an internal escape hatch, not a normal
            API parameter.

    Raises:
        ValueError: If daemon_name is not a safe identifier OR if
            ``WATERCOOLER_FINDINGS_STRICT_NAMESPACE=1`` is set,
            *namespace* is empty, AND ``_allow_unscoped=False``
            (Move 3 strict-mode contract).
    """
    if not _DAEMON_NAME_RE.match(daemon_name):
        raise ValueError(f"Invalid daemon name: {daemon_name!r}")
    if (
        not namespace
        and _findings_strict_namespace()
        and not _allow_unscoped
    ):
        # Plan v5.1 Move 3: a missing scope is an A1 cross-tenant
        # attack surface in hosted multi-tenant deployments. Under
        # strict mode the un-namespaced root path is forbidden — the
        # caller must derive a real namespace from auth context (via
        # ``derive_namespace`` / ``ResolvedScope``) or pass
        # ``_allow_unscoped=True`` if the call site is a documented
        # admin / diagnostic exemption.
        raise ValueError(
            "WATERCOOLER_FINDINGS_STRICT_NAMESPACE=1: empty namespace "
            f"refused for daemon {daemon_name!r}; resolve scope via "
            "auth.scope.resolve_scope, provide an explicit namespace, "
            "or pass _allow_unscoped=True if this is a documented "
            "admin/diagnostic call site"
        )
    base = _daemons_dir()
    if namespace:
        # Sanitise namespace for filesystem safety: replace unsafe chars
        # (including dots — prevents ".." path traversal) with _.
        safe_ns = re.sub(r"[^A-Za-z0-9_:-]", "_", namespace)
        base = base / safe_ns
    d = base / daemon_name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ #
# Deterministic finding ID
# ------------------------------------------------------------------ #


def build_finding_id(
    scope_id: str,
    daemon_name: str,
    topic: str,
    category: str,
    entry_id: str = "",
    dedup_signature: str = "",
) -> str:
    """Build a deterministic finding ID from its dedup components.

    Uses a stable SHA-256 hex digest of the normalised tuple so that
    re-observed findings produce the same ID (for upsert dedup).
    """
    parts = (scope_id, daemon_name, topic, category, entry_id, dedup_signature)
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


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
    scope_id: str = ""
    user_id: str = ""
    repo: str = ""

    def __post_init__(self) -> None:
        if self.created_at == 0.0:
            self.created_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Finding":
        dropped = set(d.keys()) - set(cls.__dataclass_fields__)
        if dropped:
            logger.debug("Finding.from_dict: dropping unknown keys: %s", dropped)
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
        dropped = set(d.keys()) - set(cls.__dataclass_fields__)
        if dropped:
            logger.debug(
                "ThreadCheckpoint.from_dict: dropping unknown keys: %s", dropped
            )
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
        extras: Daemon-specific metrics (avoids repurposing thread-centric fields
            for daemons that don't operate on threads, e.g. T2IndexerDaemon).
    """

    daemon_name: str
    last_run: float = 0.0
    last_run_duration: float = 0.0
    threads_processed: int = 0
    threads_skipped: int = 0
    findings_produced: int = 0
    error_count: int = 0
    thread_state: Dict[str, ThreadCheckpoint] = field(default_factory=dict)
    extras: Dict[str, Any] = field(default_factory=dict)

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
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DaemonCheckpoint":
        ts_raw = d.get("thread_state", {})
        skip = {"thread_state", "extras"}
        obj = cls(
            **{
                k: v
                for k, v in d.items()
                if k in cls.__dataclass_fields__ and k not in skip
            }
        )
        obj.thread_state = {
            k: ThreadCheckpoint.from_dict(v) if isinstance(v, dict) else v
            for k, v in ts_raw.items()
        }
        obj.extras = dict(d.get("extras", {}))
        return obj


# ------------------------------------------------------------------ #
# Persistence helpers
# ------------------------------------------------------------------ #


def save_checkpoint(
    checkpoint: DaemonCheckpoint,
    namespace: str = "",
    *,
    _allow_unscoped: bool = False,
) -> None:
    """Atomically write checkpoint to disk (temp + rename, cross-process locked).

    See :func:`_daemon_dir` for ``_allow_unscoped`` semantics.

    Raises:
        FileLockError: When the per-daemon checkpoint lock cannot be
            acquired within the timeout. Callers must wrap or accept
            propagation. A failed save here means the daemon will
            reprocess entries on restart and may emit duplicate
            findings — fail-loud is preferred over silent loss.
        FileLockUnsupportedError: When neither ``fcntl`` (POSIX) nor
            ``msvcrt`` (Windows) is available on the platform.
        OSError: For underlying filesystem failures (disk full,
            permissions, etc.) propagated from the temp-write/rename.
        ValueError: Strict-namespace mode rejection (see
            :func:`_daemon_dir`).
    """
    d = _daemon_dir(
        checkpoint.daemon_name, namespace=namespace, _allow_unscoped=_allow_unscoped
    )
    path = d / "checkpoint.json"
    lock_path = _checkpoint_lock_path(
        checkpoint.daemon_name, namespace=namespace, _allow_unscoped=_allow_unscoped
    )
    with file_lock(lock_path, timeout=_CHECKPOINT_LOCK_TIMEOUT_S):
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(d), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(checkpoint.to_dict(), f, indent=2)
                # Class P storage hygiene (Move 6 plan v5.1): the
                # checkpoint carries scope-bound state (entry_id
                # high-water marks, scan progress). Force 0o600 on
                # the temp file BEFORE the atomic rename so the final
                # path inherits the tightened mode regardless of
                # process umask. ``mkstemp`` itself produces 0o600
                # on POSIX, but explicit chmod is robust to umask
                # changes between mkstemp and replace, and to the
                # already-existing-target overwrite case where mode
                # bits are inherited from the prior file.
                #
                # PR #705 round 7 LOW: chmod the open fd via
                # ``os.fchmod`` rather than the path. Path-based
                # chmod after the ``with`` block closes the fd
                # leaves a brief window where a symlink could be
                # swapped in at ``tmp_path`` between close and
                # chmod. The fd-based form is symlink-immune.
                if os.name == "posix":
                    try:
                        os.fchmod(f.fileno(), 0o600)
                    except OSError as exc:
                        logger.warning(
                            "could not set 0o600 on %s: %s", tmp_path, exc
                        )
            os.replace(tmp_path, str(path))
        except BaseException:
            # PR #705 round 7+5+2 LOW: ``_maybe_compact`` and
            # ``acknowledge_finding`` were widened to ``except
            # BaseException:`` so a ``KeyboardInterrupt`` /
            # ``SystemExit`` mid-replace still triggers tmp-file
            # cleanup before re-raise. ``save_checkpoint`` was
            # missed in that pass — same pattern, same fix.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def load_checkpoint(
    daemon_name: str,
    namespace: str = "",
    *,
    _allow_unscoped: bool = False,
) -> DaemonCheckpoint:
    """Load checkpoint from disk, returning a fresh one if not found.

    See :func:`_daemon_dir` for ``_allow_unscoped`` semantics.
    """
    path = (
        _daemon_dir(daemon_name, namespace=namespace, _allow_unscoped=_allow_unscoped)
        / "checkpoint.json"
    )
    if not path.exists():
        return DaemonCheckpoint(daemon_name=daemon_name)
    try:
        with open(path) as f:
            data = json.load(f)
        return DaemonCheckpoint.from_dict(data)
    except Exception as e:
        logger.warning("DAEMON[%s]: failed to load checkpoint: %s", daemon_name, e)
        return DaemonCheckpoint(daemon_name=daemon_name)


# Per-(daemon, namespace) thread locks for in-process JSONL serialisation.
# Previously a single module-global lock guarded all daemons; under the new
# cross-process file_lock contract that meant one daemon blocked on a 30 s
# file-lock acquire would freeze in-process writes to every other daemon
# until the timeout elapsed. Per-pair locks keep cross-daemon write paths
# independent while still serialising threads writing to the same file.
_findings_locks: Dict[str, threading.Lock] = {}
_findings_locks_dict_lock = threading.Lock()


def _sanitize_namespace_for_key(namespace: str) -> str:
    """Apply the same sanitisation ``_daemon_dir`` uses for the filesystem.

    Two raw namespaces that collapse to the same sanitised form share
    the same on-disk directory and lock file; they MUST therefore
    share the same in-process threading lock as well, otherwise
    threads from those distinct raw namespaces would pile up on the
    file lock instead of serialising cheaply in-process.
    """
    if not namespace:
        return ""
    return re.sub(r"[^A-Za-z0-9_:-]", "_", namespace)


def _get_findings_lock(daemon_name: str, namespace: str = "") -> threading.Lock:
    """Return the in-process ``threading.Lock`` for ``(daemon, namespace)``.

    The lookup itself is guarded by a brief per-process lock so concurrent
    callers can't double-create a lock for the same key. Once created,
    locks are cached for the lifetime of the process — daemon names and
    namespaces are bounded.

    The dict key uses the SANITISED namespace (matching ``_daemon_dir``)
    so any two callers that resolve to the same on-disk path also
    resolve to the same threading lock. Without the matching
    sanitisation a pair of raw namespaces like ``"foo|bar"`` and
    ``"foo_bar"`` would receive distinct in-process locks but compete
    on the same lock file — defeating the threading layer's purpose.
    """
    safe_ns = _sanitize_namespace_for_key(namespace)
    key = f"{safe_ns}|{daemon_name}"
    with _findings_locks_dict_lock:
        lock = _findings_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _findings_locks[key] = lock
        return lock


# Rotation threshold: compact the JSONL file when it exceeds this many lines.
_MAX_FINDINGS_LINES = 10_000
_COMPACT_KEEP_LINES = 5_000


def append_findings(
    daemon_name: str,
    findings: List[Finding],
    namespace: str = "",
    *,
    _allow_unscoped: bool = False,
) -> None:
    """Append findings to the JSONL log file.

    Cross-process safe: acquires ``file_lock`` on the per-daemon
    ``.findings.lock`` sentinel so concurrent MCP server processes
    cannot interleave appends. Thread safety within a single process
    is provided by a per-(daemon, namespace) ``threading.Lock`` so
    a slow file-lock acquire on one daemon does not block in-process
    writes to other daemons.

    Triggers rotation when the file exceeds _MAX_FINDINGS_LINES,
    keeping only the most recent _COMPACT_KEEP_LINES entries.

    Raises:
        FileLockError: When the per-daemon findings lock cannot be
            acquired within the timeout. A failed append here means
            the findings batch is lost; the function fails loud
            rather than swallow the error so the caller can decide
            whether to retry on the next tick or surface the
            failure. Daemon ticks should generally wrap the call in
            a try/except that logs the loss.
        FileLockUnsupportedError: When neither ``fcntl`` (POSIX) nor
            ``msvcrt`` (Windows) is available on the platform.
        OSError: For underlying filesystem failures (disk full,
            permissions, etc.) propagated from the JSONL write or
            rotation step.
    """
    if not findings:
        return
    path = (
        _daemon_dir(daemon_name, namespace=namespace, _allow_unscoped=_allow_unscoped)
        / "findings.jsonl"
    )
    lock_path = _findings_lock_path(
        daemon_name, namespace=namespace, _allow_unscoped=_allow_unscoped
    )
    with _get_findings_lock(daemon_name, namespace):
        with file_lock(lock_path, timeout=_FINDINGS_LOCK_TIMEOUT_S):
            # Class P storage hygiene (Move 6 plan v5.1): findings
            # JSONL carries scope-tagged primary user data.
            # PR #705 round 4 MED fix: open with
            # ``os.open(O_CREAT|O_APPEND|O_WRONLY, 0o600)`` so the
            # file is created with 0o600 at the kernel level — no
            # TOCTOU window where the file briefly exists at the
            # umask-default mode between ``open(path, "a")`` and
            # the explicit chmod. Then unconditionally chmod in
            # case the file pre-dates this hygiene step and was
            # created at 0o644 under an earlier deployment.
            if os.name == "posix":
                fd = os.open(
                    str(path),
                    os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                    0o600,
                )
                # PR #705 round 5 MED — fd-leak guard: ``os.fdopen``
                # takes ownership of the raw fd, but if anything in
                # this block raises before fdopen completes (chmod
                # OSError is swallowed so it can't trigger here, but
                # OOM / invalid-fd-after-signal paths can), the fd
                # would leak. Wrap with try/except + os.close so the
                # fd is released on any error path.
                try:
                    try:
                        # PR #705 round 7+2 LOW: ``os.fchmod(fd, ...)``
                        # rather than ``os.chmod(path, ...)``. The fd
                        # is already open on the correct file (the
                        # one ``os.open`` resolved); the path is now
                        # racy because between ``os.open`` and a
                        # path-based chmod a privileged actor with
                        # write to ``.watercooler`` could swap the
                        # path for a symlink and tighten a different
                        # file. Fd-based chmod is symlink-immune and
                        # consistent with the round-7 mkstemp fix.
                        os.fchmod(fd, 0o600)
                    except OSError as exc:
                        logger.warning(
                            "could not set 0o600 on %s: %s — Class P "
                            "hygiene depends on filesystem-level controls",
                            path,
                            exc,
                        )
                    f = os.fdopen(fd, "a")
                except BaseException:
                    os.close(fd)
                    raise
                with f:
                    for finding in findings:
                        f.write(json.dumps(finding.to_dict()) + "\n")
            else:
                # Windows: POSIX permission bits don't apply.
                with open(path, "a") as f:
                    for finding in findings:
                        f.write(json.dumps(finding.to_dict()) + "\n")
            # Rotate if file has grown too large.
            # TODO: _maybe_compact re-reads the file to count lines; tracking
            # line_count in the checkpoint would make this O(1).
            _maybe_compact(path, daemon_name)


def _maybe_compact(path: Path, daemon_name: str) -> None:
    """Compact findings JSONL if it exceeds the rotation threshold.

    Keeps the most recent _COMPACT_KEEP_LINES lines (newest entries are
    at the end of the file). Called under _findings_lock.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
        if len(lines) <= _MAX_FINDINGS_LINES:
            return
        keep = lines[-_COMPACT_KEEP_LINES:]
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.writelines(keep)
                # Class P storage hygiene (PR #705 round 6 MED): the
                # ``mkstemp`` default is 0o600 on POSIX, but the
                # ``save_checkpoint`` comment notes that relying on
                # that alone is fragile (it varies by platform and
                # umask interaction with ``os.replace``). Tighten
                # explicitly before the rename so the surviving file
                # is unambiguously 0o600 even if the tmp default
                # drifts in a future Python release.
                #
                # PR #705 round 7 LOW: chmod the open fd via
                # ``os.fchmod`` rather than the path. Path-based
                # chmod outside the ``with`` block leaves a brief
                # symlink-swap window between close and chmod.
                if os.name == "posix":
                    try:
                        os.fchmod(f.fileno(), 0o600)
                    except OSError as exc:
                        logger.warning(
                            "DAEMON[%s]: chmod 0o600 on compaction tmp failed: %s",
                            daemon_name,
                            exc,
                        )
            os.replace(tmp_path, str(path))
            logger.info(
                "DAEMON[%s]: compacted findings.jsonl from %d to %d lines",
                daemon_name,
                len(lines),
                len(keep),
            )
        except BaseException:
            # PR #705 round 7+3 LOW: catch ``BaseException`` (not just
            # ``Exception``) so a ``KeyboardInterrupt`` or
            # ``SystemExit`` during ``os.replace`` still triggers
            # the temp-file cleanup. An orphaned ``.tmp`` file in
            # the daemon directory survives across restarts and
            # accumulates over time on a flaky disk; closing the
            # window matches the existing fd-leak-guard pattern at
            # line 478 (round 5 fix).
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.warning("DAEMON[%s]: findings compaction failed: %s", daemon_name, e)


def acknowledge_finding(
    daemon_name: str,
    finding_id: str,
    *,
    namespace: str = "",
    _allow_unscoped: bool = False,
) -> bool:
    """Mark a finding acknowledged in its JSONL findings file.

    Reads the file, finds the entry by finding_id, rewrites with
    acknowledged=True, and uses the same write lock + replace pattern as
    _maybe_compact() for crash safety.

    Cross-process safe via ``file_lock`` on the per-daemon
    ``.findings.lock`` sentinel — this prevents the read-modify-rewrite
    cycle from racing with a concurrent ``append_findings`` or
    ``_maybe_compact`` in another process.

    Returns:
        ``True`` on a successful acknowledgement write.
        ``False`` when the findings file or target id is absent, when
        the cross-process lock cannot be acquired within the timeout
        (logged as WARNING), or when the read/write itself fails.

    The bool-only return contract is load-bearing — many callers do
    ``if not acknowledge_finding(...)`` and expect every failure mode
    to flow through that path. Lock-acquisition timeouts are caught
    here rather than propagated.
    """
    lock_path = _findings_lock_path(
        daemon_name, namespace=namespace, _allow_unscoped=_allow_unscoped
    )
    with _get_findings_lock(daemon_name, namespace):
        try:
            with file_lock(lock_path, timeout=_FINDINGS_LOCK_TIMEOUT_S):
                path = (
                    _daemon_dir(
                        daemon_name,
                        namespace=namespace,
                        _allow_unscoped=_allow_unscoped,
                    )
                    / "findings.jsonl"
                )
                if not path.exists():
                    return False
                try:
                    with open(path) as f:
                        lines = f.readlines()
                except Exception as e:
                    logger.warning(
                        "DAEMON[%s]: failed to read findings for ack: %s",
                        daemon_name,
                        e,
                    )
                    return False

                found = False
                new_lines: List[str] = []
                for line in lines:
                    stripped = line.rstrip("\n")
                    if not stripped:
                        new_lines.append(line)
                        continue
                    try:
                        data = json.loads(stripped)
                        if data.get("finding_id") == finding_id:
                            data["acknowledged"] = True
                            new_lines.append(json.dumps(data) + "\n")
                            found = True
                            continue
                    except (json.JSONDecodeError, TypeError):
                        pass
                    new_lines.append(line)

                if not found:
                    return False

                tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
                try:
                    with os.fdopen(tmp_fd, "w") as f:
                        f.writelines(new_lines)
                        # Class P storage hygiene (PR #705 round 6
                        # MED): explicit chmod before rename,
                        # mirroring ``_maybe_compact`` and
                        # ``save_checkpoint``. ``mkstemp`` default is
                        # 0o600 on POSIX but depending on it across
                        # the rename is fragile.
                        #
                        # PR #705 round 7 LOW: ``os.fchmod`` on the
                        # open fd inside the ``with`` block,
                        # eliminating the brief symlink-swap window
                        # path-based chmod after close would leave.
                        if os.name == "posix":
                            try:
                                os.fchmod(f.fileno(), 0o600)
                            except OSError as exc:
                                logger.warning(
                                    "DAEMON[%s]: chmod 0o600 on ack tmp failed: %s",
                                    daemon_name,
                                    exc,
                                )
                    os.replace(tmp_path, str(path))
                except BaseException as e:
                    # PR #705 round 7+4 MED: catch ``BaseException``
                    # (not just ``Exception``) so a ``KeyboardInterrupt``
                    # or ``SystemExit`` mid-replace still triggers the
                    # tmp-file cleanup. Mirrors the round-7+3
                    # ``_maybe_compact`` discipline.
                    #
                    # Two-branch behaviour: ``Exception`` is logged
                    # and we return False (preserving the bool-only
                    # failure contract — callers don't expect
                    # acknowledge_finding to raise on disk failure).
                    # KI/SE re-raise after cleanup so process-shutdown
                    # signals are not swallowed.
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    if isinstance(e, Exception):
                        logger.warning(
                            "DAEMON[%s]: failed to write ack: %s",
                            daemon_name,
                            e,
                        )
                        return False
                    raise
        except OSError as e:
            # Preserve the bool-only failure contract. ``FileLockError``
            # and ``FileLockUnsupportedError`` are both ``OSError``
            # subclasses; widening the catch to plain ``OSError`` also
            # absorbs unexpected lock-layer failures (ENOMEM on the
            # lock file, ENFILE from fd exhaustion, EACCES on a
            # corrupted lock file's permissions) that would otherwise
            # propagate and break callers doing
            # ``if not acknowledge_finding(...)``.
            logger.warning(
                "DAEMON[%s]: could not acquire findings lock for ack: %s",
                daemon_name,
                e,
            )
            return False

    return True


def load_findings(
    daemon_name: str,
    *,
    limit: Optional[int] = 100,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    topic: Optional[str] = None,
    unacknowledged_only: bool = False,
    namespace: str = "",
    order: Literal["newest", "oldest"] = "newest",
    _allow_unscoped: bool = False,
) -> List[Finding]:
    """Load findings from JSONL log with optional filters.

    Returns findings in reverse chronological order (newest first) by default.
    Pass ``order="oldest"`` to return oldest findings first.
    Pass ``limit=None`` to return all matching findings without truncation —
    needed by progressive-cursor daemons that apply their own batch cap after
    filtering so the limit does not cut off reachable entries.

    Note: Reads the entire JSONL file before filtering. The file is
    automatically compacted by append_findings() when it exceeds
    _MAX_FINDINGS_LINES (keeps most recent _COMPACT_KEEP_LINES).

    Reads without holding ``_findings_lock``. This is safe because
    ``_maybe_compact`` uses ``os.replace()`` (atomic on POSIX), so a
    concurrent reader sees either the old or new file, never a torn
    state. A reader that opens the file just before compaction may get
    a partial result, which is acceptable for a best-effort query.
    """
    path = (
        _daemon_dir(daemon_name, namespace=namespace, _allow_unscoped=_allow_unscoped)
        / "findings.jsonl"
    )
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
                    logger.warning(
                        "DAEMON[%s]: skipping malformed JSONL line: %s", daemon_name, e
                    )
                    continue
    except Exception as e:
        logger.warning("DAEMON[%s]: failed to load findings: %s", daemon_name, e)

    if order != "oldest":
        # File-append order is chronological; reverse for newest-first.
        all_findings.reverse()
    return all_findings if limit is None else all_findings[:limit]
