"""Daemon management — periodic thread scanning and hygiene.

Public API
----------
- ``init_daemons()`` — Initialise the singleton DaemonManager.
  Called once at MCP server startup, after init_memory_queue().
- ``get_daemon_manager()`` — Access the global DaemonManager instance.

Design follows MemoryTaskWorker: daemon threads with stop/wake events,
JSONL persistence, and atexit cleanup.

Cross-process singleton: only one MCP server process per machine runs
daemons. A PID lockfile at ``~/.watercooler/daemons/daemon.pid`` prevents
duplicate daemon instances when multiple agents (Claude, Cursor, Codex)
connect to separate MCP servers against the same repo.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # hosted_coordinator.py ships publicly — import only needed for type annotations.
    from .hosted_coordinator import HostedDaemonCoordinator

from .base import BaseDaemon, DaemonStatus
from .errors import (
    DaemonAlreadyRegisteredError,
    DaemonCheckpointError,
    DaemonError,
    DaemonLifecycleError,
    DaemonNotFoundError,
)
from .manager import DaemonManager
from .state import DaemonCheckpoint, Finding, ThreadCheckpoint
# Public daemon implementations — ship in the open-core build.
from .auditor import ThreadAuditorDaemon
from .decision_detector import DetectDecisionsDaemon
from .decision_extractor import ExtractDecisionsDaemon
from .project_coordinator import ProjectCoordinatorDaemon
from .sync_guard import SyncGuardDaemon

logger = logging.getLogger(__name__)

# Daemons that register in the local init_daemons() path.
# Premium daemons run only on Railway via HostedDaemonCoordinator.
# This gate prevents duplicate execution on dev machines where all
# modules are present.  Open-core builds have a separate ImportError
# guard (modules excluded by Copybara).
# Dev override: WATERCOOLER_DEV_LOCAL_DAEMONS=1
LOCAL_DAEMON_NAMES: frozenset[str] = frozenset({
    "thread_auditor",
    "decision_detector",
    "decision_extractor",
    "content_scout",
    "content_refiner",
    "sync_guard",
})

__all__ = [
    # Allowlist
    "LOCAL_DAEMON_NAMES",
    # Core types
    "BaseDaemon",
    "DaemonStatus",
    "DaemonManager",
    "Finding",
    "DaemonCheckpoint",
    "ThreadCheckpoint",
    # Errors
    "DaemonError",
    "DaemonAlreadyRegisteredError",
    "DaemonCheckpointError",
    "DaemonLifecycleError",
    "DaemonNotFoundError",
    # Singleton API
    "init_daemons",
    "get_daemon_manager",
    # Public daemon implementations (ships in open-core build)
    "ThreadAuditorDaemon",
    "DetectDecisionsDaemon",
    "ExtractDecisionsDaemon",
    "ProjectCoordinatorDaemon",
    "SyncGuardDaemon",
]

# ------------------------------------------------------------------ #
# Module-level singleton
# ------------------------------------------------------------------ #

_manager: Optional[DaemonManager] = None
_coordinator: Optional[HostedDaemonCoordinator] = None
_init_lock = threading.Lock()
_daemon_pidfile: Optional[Path] = None  # Set when this process owns daemons


def get_daemon_manager() -> Optional[DaemonManager]:
    """Return the global DaemonManager instance (None if not initialised)."""
    return _manager


def get_hosted_coordinator() -> Optional[HostedDaemonCoordinator]:
    """Return the global HostedDaemonCoordinator (None in local mode)."""
    return _coordinator


def get_daemon_runtime():
    """Return the active daemon runtime: DaemonManager or HostedDaemonCoordinator.

    Returns the coordinator in hosted mode, the manager in local mode,
    or ``None`` if neither is initialised.
    """
    return _coordinator or _manager


def ensure_hosted_scope_for_current_context(reason: str = "") -> None:
    """Lazily create/touch a hosted daemon scope from the current context.

    No-ops silently outside hosted mode (no coordinator) or when the
    effective context lacks user_id/repo.  This is the approved call site
    for hosted scope creation — do NOT call ``ensure_scope()`` from
    generic request middleware.

    Args:
        reason: Human-readable label for why the scope is needed
            (e.g., "hosted_say", "daemon_status"). Used for logging.
    """
    coord = get_hosted_coordinator()
    if coord is None:
        return

    from ..context import get_effective_context
    ctx = get_effective_context()
    if ctx is None or not ctx.user_id or not ctx.repo:
        return

    coord.ensure_scope(
        user_id=ctx.user_id,
        repo=ctx.repo,
        branch=ctx.branch,
        github_token=ctx.github_token,
    )
    if reason:
        logger.debug("Hosted scope ensured for %s:%s (reason=%s)", ctx.user_id, ctx.repo, reason)


# ------------------------------------------------------------------ #
# Cross-process PID lock
# ------------------------------------------------------------------ #

_PIDFILE_DIR = Path.home() / ".watercooler" / "daemons"
_PIDFILE_NAME = "daemon.pid"


def _pid_is_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = existence check, no signal sent
        return True
    except ProcessLookupError:
        return False  # No such process
    except PermissionError:
        return True  # Process exists but we can't signal it
    except OSError as e:
        # Windows: os.kill(pid, 0) raises OSError/WinError 87
        # ("The parameter is incorrect") for invalid/system-reserved PIDs.
        # Only suppress this specific error; re-raise unexpected codes.
        if getattr(e, "winerror", None) == 87:
            return False
        raise


def _try_acquire_daemon_lock() -> bool:
    """Try to acquire the cross-process daemon PID lock.

    Returns True if this process now owns daemons, False if another
    live process already holds the lock.

    Lock semantics:
    - PID file at ~/.watercooler/daemons/daemon.pid
    - Contains: pid=<PID> (plus metadata)
    - If the file exists and the PID is alive → another process owns daemons
    - If the file exists but the PID is dead → stale lock, take over
    - If the file doesn't exist → acquire
    """
    global _daemon_pidfile

    pidfile = _PIDFILE_DIR / _PIDFILE_NAME
    _PIDFILE_DIR.mkdir(parents=True, exist_ok=True)

    # Atomic exclusive-create: O_CREAT|O_EXCL is atomic on POSIX,
    # preventing TOCTOU races between concurrent process starts.
    try:
        fd = os.open(pidfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, f"pid={os.getpid()}\n".encode())
        os.close(fd)
        _daemon_pidfile = pidfile
        return True
    except FileExistsError:
        pass  # Lock file exists — check if holder is alive

    # Lock file exists — read and check liveness
    try:
        content = pidfile.read_text(encoding="utf-8").strip()
        if content.startswith("pid="):
            holder_pid = int(content.split()[0].split("=")[1])
        else:
            holder_pid = int(content)

        if holder_pid == os.getpid():
            # We already hold it (re-entrant call)
            return True

        if _pid_is_alive(holder_pid):
            logger.info(
                "DAEMONS: another process (PID %d) is running daemons, "
                "skipping daemon registration in this process (PID %d)",
                holder_pid, os.getpid(),
            )
            return False

        # Stale lock — holder is dead, take over
        logger.info(
            "DAEMONS: stale daemon lock from PID %d (dead), taking over",
            holder_pid,
        )
    except (ValueError, OSError) as exc:
        logger.warning("DAEMONS: could not read pidfile, removing: %s", exc)

    # Remove stale/corrupt lock and retry with exclusive create
    try:
        pidfile.unlink(missing_ok=True)
        fd = os.open(pidfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, f"pid={os.getpid()}\n".encode())
        os.close(fd)
        _daemon_pidfile = pidfile
        return True
    except (FileExistsError, OSError) as exc:
        # Another process won the race after we removed the stale lock
        logger.info("DAEMONS: lost lock race after stale removal: %s", exc)
        return False


def _release_daemon_lock() -> None:
    """Release the daemon PID lock if we hold it."""
    global _daemon_pidfile
    if _daemon_pidfile is not None:
        try:
            # Only remove if we still own it (check PID matches)
            if _daemon_pidfile.exists():
                content = _daemon_pidfile.read_text(encoding="utf-8").strip()
                # Match "pid=<our_pid>" as a complete token (not substring).
                # Handles both "pid=123" and "pid=123 extra=metadata".
                first_token = content.split()[0] if content else ""
                if first_token == f"pid={os.getpid()}":
                    _daemon_pidfile.unlink(missing_ok=True)
                    logger.debug("DAEMONS: released daemon pidfile")
        except OSError:
            pass
        _daemon_pidfile = None


def init_daemons(*, start: bool = True) -> DaemonManager:
    """Initialise the singleton DaemonManager and register enabled daemons.

    Idempotent — calling multiple times returns the existing instance.

    Cross-process safety: only one MCP server process per machine runs
    daemons. If another live process already holds the daemon PID lock,
    this process returns an empty DaemonManager (daemons are read-only
    via findings on disk — only one process needs to produce them).

    Called once at MCP server startup, after init_memory_queue().

    Args:
        start: Whether to start all daemons immediately.

    Returns:
        The global DaemonManager instance.
    """
    global _manager

    with _init_lock:
        if _manager is not None:
            logger.debug("DAEMONS: already initialised, skipping")
            return _manager

        _manager = DaemonManager()
        atexit.register(_shutdown_daemons)

    # In hosted mode, use the HostedDaemonCoordinator instead of a
    # process-global DaemonManager.  Individual scopes get their own
    # manager via ensure_scope() at request time.
    from ..auth import is_hosted_mode as _is_hosted
    _hosted = _is_hosted()
    if _hosted:
        global _coordinator
        # hosted_coordinator.py ships publicly (stdlib deps only) — no try/except needed here.
        from .hosted_coordinator import HostedDaemonCoordinator
        logger.info("DAEMONS: hosted mode — using HostedDaemonCoordinator")
        _coordinator = HostedDaemonCoordinator()
        _coordinator.start_reaper()
        atexit.register(lambda: _coordinator.stop() if _coordinator else None)
        return _manager  # _manager stays empty; coordinator owns scoped managers
    elif not _try_acquire_daemon_lock():
        # Cross-process singleton: only one process runs daemons
        logger.info(
            "DAEMONS: deferring to existing daemon owner — "
            "this process will read findings but not produce them"
        )
        return _manager

    # Load config to decide which daemons to register
    try:
        from watercooler.config_facade import config
        wc_config = config.full()
        daemons_config = wc_config.mcp.daemons

        if not daemons_config.enabled:
            logger.info("DAEMONS: globally disabled in config")
            return _manager

        _dev_local = os.getenv("WATERCOOLER_DEV_LOCAL_DAEMONS", "") == "1"

        def _allowed_locally(name: str) -> bool:
            """Check if a daemon is allowed to register in the local process."""
            if name in LOCAL_DAEMON_NAMES or _dev_local:
                return True
            logger.info(
                "DAEMONS: %s is premium — runs on Railway, skipped locally "
                "(WATERCOOLER_DEV_LOCAL_DAEMONS=1 to override)",
                name,
            )
            return False

        # Register thread auditor if enabled
        if daemons_config.thread_auditor.enabled:
            from .auditor import ThreadAuditorDaemon

            auditor = ThreadAuditorDaemon(
                interval=daemons_config.thread_auditor.interval,
                config=daemons_config.thread_auditor,
            )
            _manager.register(auditor)

        # Register content scout if enabled (private — not in open-core build)
        if daemons_config.content_scout.enabled:
            try:
                from .content_scout import ContentScoutDaemon
            except ImportError as exc:
                logger.debug("ContentScoutDaemon not available (open-core build): %s", exc)
            else:
                scout = ContentScoutDaemon(
                    interval=daemons_config.content_scout.interval,
                    config=daemons_config.content_scout,
                )
                _manager.register(scout)

        # Register content refiner if enabled (private — not in open-core build)
        if daemons_config.content_refiner.enabled:
            try:
                from .content_refiner import ContentRefinerDaemon
            except ImportError as exc:
                logger.debug("ContentRefinerDaemon not available (open-core build): %s", exc)
            else:
                refiner = ContentRefinerDaemon(
                    interval=daemons_config.content_refiner.interval,
                    config=daemons_config.content_refiner,
                )
                _manager.register(refiner)

        # Register decision detector if enabled (local + hosted — ships in open-core build)
        if daemons_config.decision_detector.enabled:
            from .decision_detector import DetectDecisionsDaemon
            detector = DetectDecisionsDaemon(
                interval=daemons_config.decision_detector.interval,
                config=daemons_config.decision_detector,
            )
            _manager.register(detector)

        # Register decision extractor if enabled (local + hosted — ships in open-core build)
        if daemons_config.decision_extractor.enabled:
            from .decision_extractor import ExtractDecisionsDaemon
            extractor = ExtractDecisionsDaemon(
                interval=daemons_config.decision_extractor.interval,
                config=daemons_config.decision_extractor,
            )
            _manager.register(extractor)

        # Register project coordinator if enabled (premium — Railway-only in prod)
        if daemons_config.project_coordinator.enabled and _allowed_locally("project_coordinator"):
            from .project_coordinator import ProjectCoordinatorDaemon
            coordinator = ProjectCoordinatorDaemon(
                interval=daemons_config.project_coordinator.interval,
                config=daemons_config.project_coordinator,
            )
            _manager.register(coordinator)

        # Register pulse snapshot daemon if enabled (premium — Railway-only in prod)
        if daemons_config.pulse_snapshot.enabled and _allowed_locally("pulse_snapshot"):
            try:
                from .pulse_snapshot import PulseSnapshotDaemon
            except ImportError as exc:
                logger.debug("PulseSnapshotDaemon not available (open-core build): %s", exc)
            else:
                pulse = PulseSnapshotDaemon(
                    interval=daemons_config.pulse_snapshot.interval,
                    config=daemons_config.pulse_snapshot,
                )
                _manager.register(pulse)

        # Register pulse report daemon if enabled (premium — Railway-only in prod)
        if daemons_config.pulse_report.enabled and _allowed_locally("pulse_report"):
            try:
                from .pulse_report import PulseReportDaemon
            except ImportError as exc:
                logger.debug("PulseReportDaemon not available (open-core build): %s", exc)
            else:
                pulse_report = PulseReportDaemon(
                    interval=daemons_config.pulse_report.interval,
                    config=daemons_config.pulse_report,
                )
                _manager.register(pulse_report)

        # Register analysis snapshot daemon if enabled (premium — Railway-only in prod)
        if daemons_config.analysis_snapshot.enabled and _allowed_locally("analysis_snapshot"):
            try:
                from .analysis_snapshot import AnalysisSnapshotDaemon
            except ImportError as exc:
                logger.debug("AnalysisSnapshotDaemon not available (open-core build): %s", exc)
            else:
                analysis = AnalysisSnapshotDaemon(
                    interval=daemons_config.analysis_snapshot.interval,
                    config=daemons_config.analysis_snapshot,
                )
                _manager.register(analysis)

        # Register trend snapshot daemon if enabled (premium — Railway-only in prod)
        if daemons_config.trend_snapshot.enabled and _allowed_locally("trend_snapshot"):
            try:
                from .trend_snapshot import TrendSnapshotDaemon
            except ImportError as exc:
                logger.debug("TrendSnapshotDaemon not available (open-core build): %s", exc)
            else:
                trend = TrendSnapshotDaemon(
                    interval=daemons_config.trend_snapshot.interval,
                    config=daemons_config.trend_snapshot,
                )
                _manager.register(trend)

        # Register sync guard if enabled (public — ships in open-core build)
        if daemons_config.sync_guard.enabled:
            from .sync_guard import SyncGuardDaemon
            guard = SyncGuardDaemon(interval=daemons_config.sync_guard.interval)
            _manager.register(guard)

        # Register T2 indexer if memory/queue/graphiti are all active (premium)
        if _allowed_locally("t2_indexer"):
            try:
                _try_register_t2_indexer(_manager)
            except Exception as t2_exc:
                logger.warning("DAEMONS: could not register t2_indexer: %s", t2_exc)

    except Exception as exc:
        logger.warning("DAEMONS: config load error, no daemons registered: %s", exc)
        # Don't register defaults — daemons are opt-in, and without config
        # we can't confirm the user opted in.

    if start:
        _manager.start_all()

    logger.info(
        "DAEMONS: initialised (%d daemons registered, PID %d owns lock)",
        len(_manager.daemon_names),
        os.getpid(),
    )
    return _manager


def _try_register_t2_indexer(manager: DaemonManager) -> None:
    """Register T2 indexer if memory/queue/graphiti are all active.

    Fail-closed: skips registration when code_root cannot be determined
    and WATERCOOLER_GRAPHITI_DATABASE env var is not set, preventing
    silent indexing into the wrong database (see todo-066).
    """
    from watercooler.memory_config import (
        get_memory_backend,
        is_memory_enabled,
        is_memory_queue_enabled,
    )

    try:
        backend = get_memory_backend()
    except ValueError:
        return
    if not (is_memory_enabled() and is_memory_queue_enabled() and backend == "graphiti"):
        return

    try:
        from watercooler_memory.backends.graphiti import GraphitiBackend
        from watercooler_mcp import memory as mem
    except ImportError:
        logger.warning("DAEMONS: graphiti imports unavailable, skipping t2_indexer")
        return

    # Resolve code_root from CWD so load_graphiti_config() derives the
    # correct per-project database name instead of defaulting to "watercooler".
    code_root: Optional[Path] = None
    try:
        from watercooler_mcp.config import resolve_thread_context
        ctx = resolve_thread_context(Path.cwd())
        code_root = ctx.code_root
    except Exception as exc:
        logger.debug("DAEMONS: could not resolve code_root from CWD: %s", exc)

    # Fail closed: require deterministic database name.
    # Allow an explicit env var override for deployments that cannot rely
    # on CWD resolution (e.g. servers launched from outside the project root).
    if code_root is None and not os.getenv("WATERCOOLER_GRAPHITI_DATABASE"):
        logger.warning(
            "DAEMONS: skipping t2_indexer — could not resolve code_root from CWD "
            "and WATERCOOLER_GRAPHITI_DATABASE is not set. "
            "Set the env var or run the MCP server from the project root."
        )
        return

    graphiti_config = mem.load_graphiti_config(code_path=code_root)
    if graphiti_config is None:
        logger.debug("DAEMONS: graphiti config unavailable, skipping t2_indexer")
        return

    graphiti_backend = mem.get_graphiti_backend(graphiti_config)
    if not isinstance(graphiti_backend, GraphitiBackend):
        logger.debug("DAEMONS: graphiti backend unavailable, skipping t2_indexer")
        return

    # Pre-seed code_root so enqueue uses the correct database name from
    # the very first tick. When None (env-var-only path), the daemon
    # resolves _resolved_code_root at tick time via CWD.
    try:
        from .t2_indexer import T2IndexerDaemon
    except ImportError as exc:
        logger.debug("T2IndexerDaemon not available (open-core build): %s", exc)
        return
    manager.register(
        T2IndexerDaemon(backend=graphiti_backend, code_root=code_root)
    )


def _shutdown_daemons() -> None:
    """Atexit hook: gracefully stop all daemon threads and release PID lock."""
    if _manager is not None:
        logger.info("DAEMONS: shutting down (atexit)")
        _manager.stop_all(timeout=5.0)
    _release_daemon_lock()
