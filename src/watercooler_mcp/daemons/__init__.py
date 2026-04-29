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
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    # hosted_coordinator.py ships publicly — import only needed for type annotations.
    from watercooler.config_schema import WatercoolerConfig

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

__all__ = [
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
    "get_hosted_coordinator",
    "get_daemon_runtime",
    "daemon_runtime_location",
    "daemon_execution_policy",
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

# Premium daemons whose ``route="auto"`` resolution depends on transport
# (hybrid/proxy → hosted; stdio/http → local).  This set is the ONE place
# that answers "is ``daemon_name`` a premium daemon?" — adding a new
# premium daemon only requires appending its name here and giving its
# config subclass a ``route`` field.
_PREMIUM_DAEMONS: frozenset[str] = frozenset(
    {
        "project_coordinator",
        "coordinator_refiner",
        "pulse_snapshot",
        "pulse_report",
        "analysis_snapshot",
        "trend_snapshot",
        "t2_indexer",
    }
)


def daemon_execution_policy(
    daemon_name: str,
    sub_config: Any,
    transport: str,
    in_hosted_coordinator: bool,
) -> str:
    """Decide where *daemon_name* runs: ``"local"``, ``"hosted"``, or ``"skip"``.

    This is the **single source of truth** for the old scattered
    questions ("is it in the allowlist?", "does its capability route
    remote?", "is this hosted mode?").  Callers must honour the return
    value — ``init_daemons`` skips local registration when the policy
    says ``"hosted"``/``"skip"``; the hosted coordinator skips when the
    policy says ``"local"``/``"skip"``.

    Resolution order:
      * ``sub_config.enabled is False`` → ``skip``
      * ``sub_config.route == "disabled"`` → ``skip``
      * ``sub_config.route == "local"`` → ``local``
      * ``sub_config.route == "hosted"`` → ``hosted``
      * ``sub_config.route == "auto"`` (default):
          - ``in_hosted_coordinator=True`` → ``hosted``
          - ``daemon_name`` is a premium daemon and transport is
            ``hybrid`` or ``proxy`` → ``hosted``
          - otherwise → ``local``

    Non-premium daemons (``sync_guard``, ``thread_auditor``, etc.) have
    no ``route`` field; they always register locally when
    ``enabled=True`` and the process is not running as the hosted
    coordinator.  ``sub_config.route`` attribute access uses
    ``getattr(..., "auto")`` to tolerate this.
    """
    if not getattr(sub_config, "enabled", False):
        return "skip"
    route = getattr(sub_config, "route", "auto")
    if route == "disabled":
        return "skip"
    if route == "local":
        return "local"
    if route == "hosted":
        return "hosted"
    # route == "auto"
    if in_hosted_coordinator:
        return "hosted"
    if daemon_name in _PREMIUM_DAEMONS and transport in ("hybrid", "proxy"):
        return "hosted"
    return "local"


# Backward-compat alias for the set retired in PR 4.  Use
# ``_PREMIUM_DAEMONS`` or ``daemon_execution_policy`` in new code.
_HOSTED_OFFERED_DAEMONS: frozenset[str] = _PREMIUM_DAEMONS


def _premium_routes_remote(
    transport: str, wc_config: "WatercoolerConfig", daemon_name: str
) -> bool:
    """Compatibility shim delegating to ``daemon_execution_policy``.

    Retained so legacy callers (``tools/diagnostic.py`` via
    ``daemon_runtime_location``) keep working — new code should call
    ``daemon_execution_policy`` directly.  Returns ``True`` iff the
    policy would route *daemon_name* to the hosted coordinator given
    the caller's config.

    For diagnostic reporting we answer the question under the implicit
    assumption that the daemon is enabled and uses its default route;
    using the real sub-config's ``enabled`` flag would make the "runs
    hosted?" hint flap with every config change.
    """

    class _AutoProbe:
        enabled = True
        route = "auto"

    sub_cfg = _AutoProbe()
    decision = daemon_execution_policy(
        daemon_name, sub_cfg, transport, in_hosted_coordinator=False
    )
    return decision == "hosted"


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


def daemon_runtime_location(daemon_name: str) -> str:
    """Return ``"local"`` or ``"hosted"`` for where *daemon_name* runs.

    Replaces cross-module private imports of ``_premium_routes_remote``
    with a narrow public helper.  Behaviour:

    * If the active runtime is the hosted coordinator → always
      ``"hosted"`` (that runtime owns every daemon it tracks).
    * If hybrid/proxy transport routes the daemon remotely per
      ``_premium_routes_remote`` → ``"hosted"`` (premium daemons
      offloaded to Railway).
    * Otherwise → ``"local"`` (the local DaemonManager owns it).

    Returns ``"local"`` as a safe default when the config facade is
    unavailable — consistent with stdio / local-HTTP fallbacks.
    """
    if _coordinator is not None:
        return "hosted"
    try:
        from watercooler.config_facade import config as _cfg

        full_cfg = _cfg.full()
        transport = getattr(full_cfg.mcp, "transport", "stdio")
        daemons_cfg = getattr(full_cfg.mcp, "daemons", None)
        real_sub = getattr(daemons_cfg, daemon_name, None) if daemons_cfg else None

        # Probe the policy as if the daemon were enabled — this helper
        # answers "where *would* X run?" for diagnostic labelling,
        # independent of whether the user has opted in via ``enabled``.
        # If the real sub-config provides a non-auto ``route``, honour
        # it so explicit overrides are reflected in the displayed label.
        class _Probe:
            enabled = True
            route = (
                getattr(real_sub, "route", "auto") if real_sub is not None else "auto"
            )

        decision = daemon_execution_policy(
            daemon_name, _Probe(), transport, in_hosted_coordinator=False
        )
        if decision == "hosted":
            return "hosted"
    except Exception:  # noqa: BLE001 — facade unavailable is non-fatal for reporting
        pass
    return "local"


# --------------------------------------------------------------------- #
# Deprecation shims
# --------------------------------------------------------------------- #


def __getattr__(name: str):
    """Deprecated-name re-exports (PEP 562).

    ``LOCAL_DAEMON_NAMES`` was the frozenset that used to gate premium
    daemon registration.  PR #653 replaced it with per-daemon ``enabled``
    flags plus capability-based routing; there is no longer a meaningful
    "local vs premium" partition to expose.  The shim returns an empty
    frozenset so legacy callers fail gracefully with a deprecation
    warning rather than an AttributeError.  Remove after two minor
    releases.
    """
    if name == "LOCAL_DAEMON_NAMES":
        import warnings

        warnings.warn(
            "watercooler_mcp.daemons.LOCAL_DAEMON_NAMES is deprecated and "
            "always returns an empty frozenset since PR #653 replaced the "
            "allowlist with per-daemon config flags. Use "
            "`daemon_runtime_location(name)` for runtime classification. "
            "This shim will be removed after two minor releases.",
            DeprecationWarning,
            stacklevel=2,
        )
        return frozenset()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        logger.debug(
            "Hosted scope ensured for %s:%s (reason=%s)", ctx.user_id, ctx.repo, reason
        )


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
                holder_pid,
                os.getpid(),
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

        # Registration is config-driven: each daemon's ``enabled`` flag
        # (and, for premium daemons, ``route``) in config.toml decides
        # whether and where it registers.  ``daemon_execution_policy`` is
        # the single decision function — it returns ``"local"``,
        # ``"hosted"``, or ``"skip"`` per daemon.  In stdio/http mode the
        # policy always says ``local`` (no hosted coordinator exists).
        # In hybrid/proxy mode premium daemons default to ``hosted`` so
        # the Railway-side coordinator owns them; users override with
        # ``[mcp.daemons.<name>] route = "local"``.
        _transport = getattr(wc_config.mcp, "transport", "stdio")

        def _local_ok(name: str) -> bool:
            sub_cfg = getattr(daemons_config, name, None)
            if sub_cfg is None:
                return True
            decision = daemon_execution_policy(
                name, sub_cfg, _transport, in_hosted_coordinator=False
            )
            if decision == "local":
                return True
            if decision == "hosted":
                logger.info(
                    "DAEMONS: %s routes to hosted coordinator "
                    "(transport=%s, route=%s)",
                    name,
                    _transport,
                    getattr(sub_cfg, "route", "auto"),
                )
            return False

        # Split-brain check for t2_indexer: running it locally while the
        # ``memory_ingest`` tool surface routes remote means the local
        # daemon writes to a backend the advertised memory tools do not
        # see.  Warn at startup rather than letting it fail silently.
        if (
            daemons_config.t2_indexer.enabled
            and _local_ok("t2_indexer")
            and _transport == "hybrid"
        ):
            user_routes = getattr(wc_config.mcp, "capability_routes", None) or {}
            ingest_route = (
                user_routes.get("memory_ingest", "remote")
                if isinstance(user_routes, dict)
                else "remote"
            )
            if ingest_route != "local":
                logger.warning(
                    "DAEMONS: t2_indexer runs locally (route=%s) but hybrid "
                    "``memory_ingest`` tools route remote — the advertised "
                    "memory tools will not see the indexer's output. To align, "
                    'also set ``[mcp.capability_routes] memory_ingest = "local"``.',
                    getattr(daemons_config.t2_indexer, "route", "auto"),
                )

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
                logger.debug(
                    "ContentScoutDaemon not available (open-core build): %s", exc
                )
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
                logger.debug(
                    "ContentRefinerDaemon not available (open-core build): %s", exc
                )
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

        # Register project coordinator if enabled
        if daemons_config.project_coordinator.enabled and _local_ok(
            "project_coordinator"
        ):
            from .project_coordinator import ProjectCoordinatorDaemon

            coordinator = ProjectCoordinatorDaemon(
                interval=daemons_config.project_coordinator.interval,
                config=daemons_config.project_coordinator,
            )
            _manager.register(coordinator)

        # Register the open-core decision-stance daemon if enabled and the
        # premium coordinator is not running anywhere (any route). This is
        # the conflict-resolution gate: when the coordinator is active in any
        # form (local or hosted), its richer signal mix wins and we skip the
        # open-core fallback to avoid double emission of stance_advisory
        # findings under the same ``stance:{role}`` topic.
        pc_cfg = daemons_config.project_coordinator
        coordinator_active = pc_cfg.enabled and (
            getattr(pc_cfg, "route", "auto") != "disabled"
        )
        if daemons_config.decision_stance.enabled and not coordinator_active:
            from .decision_stance import DecisionStanceDaemon

            stance = DecisionStanceDaemon(
                interval=daemons_config.decision_stance.interval,
                config=daemons_config.decision_stance,
            )
            _manager.register(stance)

        # Register coordinator refiner if enabled
        if daemons_config.coordinator_refiner.enabled and _local_ok(
            "coordinator_refiner"
        ):
            try:
                from .coordinator_refiner import CoordinatorRefinerDaemon
            except ImportError as exc:
                logger.debug(
                    "CoordinatorRefinerDaemon not available (open-core build): %s", exc
                )
            else:
                coord_refiner = CoordinatorRefinerDaemon(
                    interval=daemons_config.coordinator_refiner.interval,
                    config=daemons_config.coordinator_refiner,
                )
                _manager.register(coord_refiner)

        # Register pulse snapshot daemon if enabled
        if daemons_config.pulse_snapshot.enabled and _local_ok("pulse_snapshot"):
            try:
                from .pulse_snapshot import PulseSnapshotDaemon
            except ImportError as exc:
                logger.debug(
                    "PulseSnapshotDaemon not available (open-core build): %s", exc
                )
            else:
                pulse = PulseSnapshotDaemon(
                    interval=daemons_config.pulse_snapshot.interval,
                    config=daemons_config.pulse_snapshot,
                )
                _manager.register(pulse)

        # Register pulse report daemon if enabled
        if daemons_config.pulse_report.enabled and _local_ok("pulse_report"):
            try:
                from .pulse_report import PulseReportDaemon
            except ImportError as exc:
                logger.debug(
                    "PulseReportDaemon not available (open-core build): %s", exc
                )
            else:
                pulse_report = PulseReportDaemon(
                    interval=daemons_config.pulse_report.interval,
                    config=daemons_config.pulse_report,
                )
                _manager.register(pulse_report)

        # Register analysis snapshot daemon if enabled
        if daemons_config.analysis_snapshot.enabled and _local_ok("analysis_snapshot"):
            try:
                from .analysis_snapshot import AnalysisSnapshotDaemon
            except ImportError as exc:
                logger.debug(
                    "AnalysisSnapshotDaemon not available (open-core build): %s", exc
                )
            else:
                analysis = AnalysisSnapshotDaemon(
                    interval=daemons_config.analysis_snapshot.interval,
                    config=daemons_config.analysis_snapshot,
                )
                _manager.register(analysis)

        # Register trend snapshot daemon if enabled
        if daemons_config.trend_snapshot.enabled and _local_ok("trend_snapshot"):
            try:
                from .trend_snapshot import TrendSnapshotDaemon
            except ImportError as exc:
                logger.debug(
                    "TrendSnapshotDaemon not available (open-core build): %s", exc
                )
            else:
                trend = TrendSnapshotDaemon(
                    interval=daemons_config.trend_snapshot.interval,
                    config=daemons_config.trend_snapshot,
                )
                _manager.register(trend)

        # Register sync guard if enabled
        if daemons_config.sync_guard.enabled:
            from .sync_guard import SyncGuardDaemon

            guard = SyncGuardDaemon(interval=daemons_config.sync_guard.interval)
            _manager.register(guard)

        # Register T2 indexer if enabled — also gated internally by the
        # memory backend configuration (enabled + queue + graphiti).
        if daemons_config.t2_indexer.enabled and _local_ok("t2_indexer"):
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
    if not (
        is_memory_enabled() and is_memory_queue_enabled() and backend == "graphiti"
    ):
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
    manager.register(T2IndexerDaemon(backend=graphiti_backend, code_root=code_root))


def _shutdown_daemons() -> None:
    """Atexit hook: gracefully stop all daemon threads and release PID lock."""
    if _manager is not None:
        logger.info("DAEMONS: shutting down (atexit)")
        _manager.stop_all(timeout=5.0)
    _release_daemon_lock()
