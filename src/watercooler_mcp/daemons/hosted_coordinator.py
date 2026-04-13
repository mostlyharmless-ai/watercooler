"""Hosted scope coordinator for multi-tenant daemon management.

Instead of a single user-global ``DaemonManager``, the hosted coordinator
maintains one ``DaemonManager`` per *scope* (``user_id:repo``).  Each
scope gets its own daemon fleet that observes only that scope's threads.

A background reaper thread tears down idle scopes after a configurable TTL.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pathlib import Path

from .hosted_worktree import HostedWorktree
from .manager import DaemonManager
from .state import Finding

logger = logging.getLogger(__name__)

# Default idle TTL: 30 minutes
_DEFAULT_IDLE_TTL = 1800.0
# Reaper interval: check every 5 minutes
_DEFAULT_REAPER_INTERVAL = 300.0


@dataclass(frozen=True)
class HostedScopeKey:
    """Identifier for a hosted daemon scope."""

    user_id: str
    repo: str
    branch: str | None = None

    @property
    def scope_id(self) -> str:
        return f"{self.user_id}:{self.repo}"


@dataclass
class _ScopeEntry:
    """Internal bookkeeping for a live scope."""

    key: HostedScopeKey
    manager: DaemonManager
    last_touched: float = field(default_factory=time.monotonic)
    worktree: HostedWorktree | None = None


class HostedDaemonCoordinator:
    """Multi-tenant daemon coordinator for hosted deployments.

    Thread-safe: all mutations go through ``_lock``.
    """

    def __init__(
        self,
        idle_ttl: float = _DEFAULT_IDLE_TTL,
        reaper_interval: float = _DEFAULT_REAPER_INTERVAL,
    ) -> None:
        self._scopes: dict[str, _ScopeEntry] = {}
        self._lock = threading.Lock()
        self._idle_ttl = idle_ttl
        self._reaper_interval = reaper_interval
        self._stop_event = threading.Event()
        self._reaper_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_reaper(self) -> None:
        """Start the background reaper thread."""
        if self._reaper_thread and self._reaper_thread.is_alive():
            return
        self._stop_event.clear()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, daemon=True, name="hosted-coordinator-reaper"
        )
        self._reaper_thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the reaper and tear down all scopes."""
        self._stop_event.set()
        if self._reaper_thread:
            self._reaper_thread.join(timeout=timeout)
        with self._lock:
            for entry in self._scopes.values():
                try:
                    entry.manager.stop_all()
                except Exception:
                    pass
                if entry.worktree:
                    try:
                        entry.worktree.cleanup()
                    except Exception:
                        pass
            self._scopes.clear()

    # ------------------------------------------------------------------
    # Scope management
    # ------------------------------------------------------------------

    def ensure_scope(
        self,
        user_id: str,
        repo: str,
        branch: str | None = None,
        github_token: str | None = None,
    ) -> DaemonManager:
        """Ensure a daemon manager exists for the given scope.

        Idempotent: returns the existing manager if the scope is already
        active, or creates and configures a new one with daemons from config.

        Also sets worker context so that off-request code
        (daemon ticks, background tasks) can resolve the scope identity.
        """
        key = HostedScopeKey(user_id=user_id, repo=repo, branch=branch)
        scope_id = key.scope_id

        with self._lock:
            entry = self._scopes.get(scope_id)
            if entry is not None:
                entry.last_touched = time.monotonic()
                return entry.manager

            # Note: we do NOT call set_worker_context() here.  ContextVar
            # values set in the request coroutine are not visible to daemon
            # threads.  Daemons receive their identity via _scope_context
            # installed by _register_daemons_for_scope().

            manager = DaemonManager()
            self._scopes[scope_id] = _ScopeEntry(
                key=key, manager=manager, last_touched=time.monotonic()
            )

        # Register daemons from config (outside lock to avoid holding it
        # during potentially slow config loading).
        self._register_daemons_for_scope(manager, key, github_token=github_token)

        logger.info("Created daemon scope: %s (%d daemons)",
                     scope_id, len(manager.daemon_names))
        return manager

    def _register_daemons_for_scope(
        self, manager: DaemonManager, key: HostedScopeKey,
        github_token: str | None = None,
    ) -> None:
        """Register the 6 premium daemons into a scoped manager from config.

        Only premium daemons (t2_indexer, project_coordinator, pulse_snapshot,
        pulse_report, analysis_snapshot, trend_snapshot) run on Railway.
        Local daemons run on the developer's machine via init_daemons().

        Each daemon gets:
        - ``state_namespace`` set to the scope_id for isolated persistence
        - ``_scope_context`` set to an HttpRequestContext so daemon threads
          can resolve user/repo/token via ``get_effective_context()``
        - ``_threads_dir_override`` set to the local worktree path (when
          available), so daemons read from filesystem instead of GitHub API
        """
        scope_id = key.scope_id

        # Build scope context for daemon threads.
        from watercooler_mcp.context import HttpRequestContext
        scope_ctx = HttpRequestContext(
            user_id=key.user_id,
            repo=key.repo,
            branch=key.branch,
            github_token=github_token,
        )

        # Initialize local worktree for daemon reads.
        # This replaces the hosted_data → GitHub Contents API path with
        # a local git clone that daemons read identically to local mode.
        threads_dir: Path | None = None
        wt: HostedWorktree | None = None
        if github_token and key.repo:
            wt = HostedWorktree(
                repo=key.repo,
                github_token=github_token,
                scope_id=scope_id,
            )
            if wt.initialize():
                threads_dir = wt.path
                # Store on scope entry for lifecycle management
                with self._lock:
                    entry = self._scopes.get(scope_id)
                    if entry is not None:
                        entry.worktree = wt
                    else:
                        # Scope torn down concurrently — clean up worktree
                        wt.cleanup()
                        threads_dir = None
                logger.info(
                    "WORKTREE: scope %s using local worktree at %s",
                    scope_id, threads_dir,
                )
            else:
                logger.warning(
                    "WORKTREE: scope %s clone failed, falling back to GitHub API",
                    scope_id,
                )
                # Clean up ASKPASS temp file from failed clone attempt
                wt.cleanup()

        def _configure(daemon):
            """Inject scope namespace, context, and worktree path into a daemon."""
            daemon.state_namespace = scope_id
            daemon._scope_context = scope_ctx
            # Set worktree path so daemon uses local reads instead of GitHub API
            if threads_dir is not None:
                daemon._threads_dir_override = threads_dir
                daemon._hosted_worktree = wt
            # Reload checkpoint from the namespaced path.
            from .state import load_checkpoint
            daemon._checkpoint = load_checkpoint(daemon.name, namespace=scope_id)

        try:
            daemons_config = self._resolve_daemon_config()

            if not daemons_config.enabled:
                return

            # Only premium daemons register in hosted scopes.
            # Local daemons (thread_auditor, decision_detector/extractor,
            # sync_guard, content_scout/refiner) run on the dev machine.

            # T2 indexer — requires graphiti backend
            try:
                self._try_register_t2_indexer_hosted(manager, _configure)
            except Exception as exc:
                logger.warning("Could not register t2_indexer for hosted scope: %s", exc)

            if daemons_config.project_coordinator.enabled:
                from .project_coordinator import ProjectCoordinatorDaemon
                d = ProjectCoordinatorDaemon(
                    interval=daemons_config.project_coordinator.interval,
                    config=daemons_config.project_coordinator,
                )
                _configure(d)
                manager.register(d)

            if daemons_config.pulse_snapshot.enabled:
                try:
                    from .pulse_snapshot import PulseSnapshotDaemon
                except ImportError as exc:
                    logger.debug("PulseSnapshotDaemon not available (open-core build): %s", exc)
                else:
                    d = PulseSnapshotDaemon(
                        interval=daemons_config.pulse_snapshot.interval,
                        config=daemons_config.pulse_snapshot,
                    )
                    _configure(d)
                    manager.register(d)

            if daemons_config.pulse_report.enabled:
                try:
                    from .pulse_report import PulseReportDaemon
                except ImportError as exc:
                    logger.debug("PulseReportDaemon not available (open-core build): %s", exc)
                else:
                    d = PulseReportDaemon(
                        interval=daemons_config.pulse_report.interval,
                        config=daemons_config.pulse_report,
                    )
                    _configure(d)
                    manager.register(d)

            if daemons_config.analysis_snapshot.enabled:
                try:
                    from .analysis_snapshot import AnalysisSnapshotDaemon
                except ImportError as exc:
                    logger.debug("AnalysisSnapshotDaemon not available (open-core build): %s", exc)
                else:
                    d = AnalysisSnapshotDaemon(
                        interval=daemons_config.analysis_snapshot.interval,
                        config=daemons_config.analysis_snapshot,
                    )
                    _configure(d)
                    manager.register(d)

            if daemons_config.trend_snapshot.enabled:
                try:
                    from .trend_snapshot import TrendSnapshotDaemon
                except ImportError as exc:
                    logger.debug("TrendSnapshotDaemon not available (open-core build): %s", exc)
                else:
                    d = TrendSnapshotDaemon(
                        interval=daemons_config.trend_snapshot.interval,
                        config=daemons_config.trend_snapshot,
                    )
                    _configure(d)
                    manager.register(d)

            manager.start_all()

        except Exception as exc:
            logger.warning(
                "Could not register daemons for scope %s: %s", scope_id, exc
            )

    @staticmethod
    def _hosted_daemon_defaults() -> dict:
        """Return hosted defaults for the 5 config-gated premium daemons.

        Only premium daemons run on Railway.  Local daemons (thread_auditor,
        decision_detector, decision_extractor, sync_guard, content_scout,
        content_refiner) run on the developer's machine via init_daemons().

        t2_indexer is the 6th premium daemon but auto-registers when the
        graphiti backend is available — it has no DaemonsConfig toggle.

        The schema defaults are ``enabled=False`` (opt-in for local), so
        hosted mode needs its own baseline for the premium set.
        """
        return {
            "enabled": True,
            "project_coordinator": {"enabled": True},
            "pulse_snapshot": {"enabled": True},
            "pulse_report": {"enabled": True},
            "analysis_snapshot": {"enabled": True},
            "trend_snapshot": {"enabled": True},
        }

    @staticmethod
    def _resolve_daemon_config():
        """Resolve daemon config for hosted scopes.

        Builds a config by layering user overrides (sent via
        ``X-Daemon-Config`` header from the hybrid client) onto hosted
        defaults where all daemons are enabled.

        The hybrid client sends only explicitly-set values
        (``model_dump(exclude_unset=True)``), so unconfigured daemons
        get the hosted default (enabled) rather than the schema default
        (disabled).  Explicit ``enabled = false`` overrides ARE sent.

        Resolution: hosted defaults ← user overrides ← local config fallback.
        """
        import json as _json
        from watercooler.config_schema import DaemonsConfig

        hosted = HostedDaemonCoordinator._hosted_daemon_defaults()

        # 1. Try user overrides from the request context (hybrid client header)
        from watercooler_mcp.context import get_effective_context
        import copy
        ctx = get_effective_context()
        if ctx and ctx.daemon_config_json:
            try:
                overrides = _json.loads(ctx.daemon_config_json)
                # Deep merge on a copy: user overrides win over hosted defaults
                merged = copy.deepcopy(hosted)
                for key, value in overrides.items():
                    if isinstance(value, dict) and isinstance(merged.get(key), dict):
                        merged[key].update(value)
                    else:
                        merged[key] = value
                return DaemonsConfig.model_validate(merged)
            except Exception as exc:
                logger.warning("Could not parse X-Daemon-Config header: %s", exc)

        # 2. Fall back to local config file (if present on this host)
        try:
            from watercooler.config_facade import config
            return config.full().mcp.daemons
        except Exception:
            pass

        # 3. Hosted defaults (premium daemons enabled)
        return DaemonsConfig.model_validate(hosted)

    @staticmethod
    def _try_register_t2_indexer_hosted(manager: DaemonManager, _configure) -> None:
        """Register T2 indexer in a hosted scope if graphiti is available."""
        import os
        from typing import Optional

        from watercooler.memory_config import (
            get_memory_backend,
            is_memory_enabled,
        )

        try:
            backend = get_memory_backend()
        except ValueError:
            return
        if not (is_memory_enabled() and backend == "graphiti"):
            return

        try:
            from watercooler_memory.backends.graphiti import GraphitiBackend
            from watercooler_mcp import memory as mem
        except ImportError:
            logger.warning("DAEMONS: graphiti imports unavailable, skipping t2_indexer (hosted)")
            return

        code_root: Optional[Path] = None
        try:
            from watercooler_mcp.config import resolve_thread_context
            ctx = resolve_thread_context(Path.cwd())
            code_root = ctx.code_root
        except Exception:
            pass

        if code_root is None and not os.getenv("WATERCOOLER_GRAPHITI_DATABASE"):
            logger.warning(
                "DAEMONS: skipping t2_indexer (hosted) — could not resolve code_root "
                "and WATERCOOLER_GRAPHITI_DATABASE is not set."
            )
            return

        graphiti_config = mem.load_graphiti_config(code_path=code_root)
        if graphiti_config is None:
            return

        graphiti_backend = mem.get_graphiti_backend(graphiti_config)
        if not isinstance(graphiti_backend, GraphitiBackend):
            return

        try:
            from .t2_indexer import T2IndexerDaemon
        except ImportError as exc:
            logger.debug("T2IndexerDaemon not available (open-core build): %s", exc)
            return

        d = T2IndexerDaemon(backend=graphiti_backend, code_root=code_root)
        _configure(d)
        manager.register(d)

    def touch_scope(self, scope_id: str) -> None:
        """Update the last-touched timestamp for a scope."""
        with self._lock:
            entry = self._scopes.get(scope_id)
            if entry:
                entry.last_touched = time.monotonic()

    def teardown_scope(self, scope_id: str) -> None:
        """Stop daemons, clean up worktree, and remove a scope."""
        with self._lock:
            entry = self._scopes.pop(scope_id, None)
        if entry:
            try:
                entry.manager.stop_all()
            except Exception:
                pass
            if entry.worktree:
                try:
                    entry.worktree.cleanup()
                except Exception:
                    pass
            logger.info("Tore down daemon scope: %s", scope_id)

    def teardown_idle_scopes(self) -> int:
        """Tear down scopes that have been idle longer than ``idle_ttl``.

        Returns the number of scopes torn down.
        """
        now = time.monotonic()
        idle_ids: list[str] = []
        with self._lock:
            for scope_id, entry in self._scopes.items():
                if (now - entry.last_touched) > self._idle_ttl:
                    idle_ids.append(scope_id)

        count = 0
        for scope_id in idle_ids:
            self.teardown_scope(scope_id)
            count += 1
        return count

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def status(
        self,
        scope_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Return status for one or more scopes."""
        with self._lock:
            if scope_id:
                entry = self._scopes.get(scope_id)
                if not entry:
                    return {"error": f"scope {scope_id} not found"}
                return {
                    "scope_id": scope_id,
                    "daemons": entry.manager.status_all(),
                    "idle_seconds": time.monotonic() - entry.last_touched,
                }
            if user_id:
                result: dict[str, Any] = {}
                for sid, entry in self._scopes.items():
                    if entry.key.user_id == user_id:
                        result[sid] = {
                            "daemons": entry.manager.status_all(),
                            "idle_seconds": time.monotonic() - entry.last_touched,
                        }
                return result
            # All scopes
            return {
                "total_scopes": len(self._scopes),
                "scopes": {
                    sid: {
                        "user_id": e.key.user_id,
                        "repo": e.key.repo,
                        "idle_seconds": time.monotonic() - e.last_touched,
                    }
                    for sid, e in self._scopes.items()
                },
            }

    def get_findings(
        self,
        scope_id: str | None = None,
        **kwargs: Any,
    ) -> list[Finding]:
        """Aggregate findings from one or all scopes."""
        findings: list[Finding] = []
        with self._lock:
            entries = (
                [self._scopes[scope_id]]
                if scope_id and scope_id in self._scopes
                else list(self._scopes.values())
            )
            # Iterate inside the lock so the reaper cannot teardown a scope
            # between snapshot and read (TOCTOU fix).
            for entry in entries:
                findings.extend(entry.manager.get_all_findings(**kwargs))
        return findings

    # ------------------------------------------------------------------
    # Reaper loop
    # ------------------------------------------------------------------

    def _reaper_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._reaper_interval)
            if self._stop_event.is_set():
                break
            try:
                count = self.teardown_idle_scopes()
                if count:
                    logger.info("Reaper tore down %d idle scope(s)", count)
            except Exception as exc:
                logger.warning("Reaper error: %s", exc)
