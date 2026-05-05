"""Hosted scope coordinator for multi-tenant daemon management.

Instead of a single user-global ``DaemonManager``, the hosted coordinator
maintains one ``DaemonManager`` per *scope* (``user_id:repo``).  Each
scope gets its own daemon fleet that observes only that scope's threads.

A background reaper thread tears down idle scopes after a configurable TTL.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from pathlib import Path

from pydantic import ValidationError

from .hosted_worktree import HostedWorktree
from .manager import DaemonManager
from .state import Finding

if TYPE_CHECKING:
    from watercooler.config_schema import DaemonsConfig

logger = logging.getLogger(__name__)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge *override* into a deep copy of *base*; nested dicts combine.

    Only one level of nesting is merged (daemon name -> field dict);
    leaf values always replace rather than merge.  Does not mutate
    *base*.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


# Maximum ``X-Daemon-Config`` payload size.  16 KB fits every legitimate
# per-scope override (the largest DaemonsConfig dump observed in testing
# is ~2 KB); payloads larger than this are rejected before ``json.loads``
# to avoid amplification attacks against the hosted coordinator.
_MAX_DAEMON_CONFIG_BYTES = 16_384

# Maximum accepted nesting depth for parsed override JSON.  ``DaemonsConfig``
# is flat-ish (``{daemon_name: {field: value}}`` = depth 2) so anything
# deeper than 4 is almost certainly a structural DoS attempt.
_MAX_DAEMON_CONFIG_DEPTH = 4


def _json_depth(value: Any, current: int = 0) -> int:
    """Return the maximum container-nesting depth of *value*.

    Used to cheaply reject deeply-nested ``X-Daemon-Config`` payloads
    before Pydantic validation burns CPU on them.
    """
    if isinstance(value, dict):
        return max((_json_depth(v, current + 1) for v in value.values()), default=current + 1)
    if isinstance(value, list):
        return max((_json_depth(v, current + 1) for v in value), default=current + 1)
    return current


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
    """Internal bookkeeping for a live scope.

    `registration_errors` accumulates structured per-daemon failures from
    `_register_daemons_for_scope`. Each entry is `{"daemon": str, "error": str}`.
    Surfaced via `watercooler_daemon_status` so MCP clients can see why a
    daemon didn't register without paging through Railway logs (replaces the
    pre-2026-05-04 silent `try/except Exception` pattern).
    """

    key: HostedScopeKey
    manager: DaemonManager
    last_touched: float = field(default_factory=time.monotonic)
    worktree: HostedWorktree | None = None
    registration_errors: list[dict[str, str]] = field(default_factory=list)


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

        logger.info(
            "Created daemon scope: %s (%d daemons)", scope_id, len(manager.daemon_names)
        )
        return manager

    def _register_daemons_for_scope(
        self,
        manager: DaemonManager,
        key: HostedScopeKey,
        github_token: str | None = None,
    ) -> None:
        """Register the 7 premium daemons into a scoped manager from config.

        Only premium daemons (t2_indexer, project_coordinator,
        coordinator_refiner, pulse_snapshot, pulse_report, analysis_snapshot,
        trend_snapshot) run on Railway. Local daemons run on the developer's
        machine via init_daemons().

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
                    scope_id,
                    threads_dir,
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

        # Capture per-daemon registration failures here; flushed onto
        # entry.registration_errors after the loop so daemon_status can
        # surface them. Replaces the pre-2026-05-04 silent
        # `try/except Exception` pattern that converted ValueError
        # (e.g., from STRICT_NAMESPACE) into "your daemons didn't
        # register and you have no way to know why". Filed against
        # thread `hosted-premium-daemons-zero-registration-2026-05-04`.
        registration_errors: list[dict[str, str]] = []

        def _record_failure(daemon_name: str, exc: BaseException) -> None:
            """Log + record a per-daemon registration failure."""
            msg = str(exc)
            registration_errors.append({"daemon": daemon_name, "error": msg})
            logger.warning(
                "Could not register daemon %s for hosted scope %s: %s",
                daemon_name,
                scope_id,
                msg,
            )

        try:
            daemons_config = self._resolve_daemon_config()
        except Exception as exc:
            # Config-resolution failure isn't per-daemon — record it as a
            # synthetic "_config" entry so daemon_status surfaces something
            # actionable, then return early (no daemons can be registered
            # without a config).
            _record_failure("_config", exc)
            self._publish_registration_errors(scope_id, registration_errors)
            return

        if not daemons_config.enabled:
            return

        # Only premium daemons register in hosted scopes.
        # Local daemons (thread_auditor, decision_detector/extractor,
        # sync_guard, content_scout/refiner) run on the dev machine.
        # ``daemon_execution_policy`` decides per-daemon; we pass
        # ``in_hosted_coordinator=True`` so ``route="auto"``
        # resolves to hosted and ``route="local"`` causes the
        # hosted path to skip (the local process will own it).
        from watercooler_mcp.daemons import daemon_execution_policy

        def _hosted_ok(name: str) -> bool:
            sub_cfg = getattr(daemons_config, name, None)
            if sub_cfg is None:
                return False
            return (
                daemon_execution_policy(
                    name, sub_cfg, transport="hybrid", in_hosted_coordinator=True
                )
                == "hosted"
            )

        # T2 indexer — requires graphiti backend and the config gate
        if _hosted_ok("t2_indexer"):
            try:
                self._try_register_t2_indexer_hosted(manager, _configure)
            except Exception as exc:
                _record_failure("t2_indexer", exc)

        if _hosted_ok("project_coordinator"):
            try:
                from .project_coordinator import ProjectCoordinatorDaemon
                from .base import BaseDaemon

                d: BaseDaemon = ProjectCoordinatorDaemon(
                    interval=daemons_config.project_coordinator.interval,
                    config=daemons_config.project_coordinator,
                )
                _configure(d)
                manager.register(d)
            except Exception as exc:
                _record_failure("project_coordinator", exc)

        if _hosted_ok("coordinator_refiner"):
            try:
                from .coordinator_refiner import CoordinatorRefinerDaemon
            except ImportError as exc:
                logger.debug(
                    "CoordinatorRefinerDaemon not available (open-core build): %s", exc
                )
            else:
                try:
                    d = CoordinatorRefinerDaemon(
                        interval=daemons_config.coordinator_refiner.interval,
                        config=daemons_config.coordinator_refiner,
                    )
                    _configure(d)
                    manager.register(d)
                except Exception as exc:
                    _record_failure("coordinator_refiner", exc)

        if _hosted_ok("pulse_snapshot"):
            try:
                from .pulse_snapshot import PulseSnapshotDaemon
            except ImportError as exc:
                logger.debug(
                    "PulseSnapshotDaemon not available (open-core build): %s", exc
                )
            else:
                try:
                    d = PulseSnapshotDaemon(
                        interval=daemons_config.pulse_snapshot.interval,
                        config=daemons_config.pulse_snapshot,
                    )
                    _configure(d)
                    manager.register(d)
                except Exception as exc:
                    _record_failure("pulse_snapshot", exc)

        if _hosted_ok("pulse_report"):
            try:
                from .pulse_report import PulseReportDaemon
            except ImportError as exc:
                logger.debug(
                    "PulseReportDaemon not available (open-core build): %s", exc
                )
            else:
                try:
                    d = PulseReportDaemon(
                        interval=daemons_config.pulse_report.interval,
                        config=daemons_config.pulse_report,
                    )
                    _configure(d)
                    manager.register(d)
                except Exception as exc:
                    _record_failure("pulse_report", exc)

        if _hosted_ok("analysis_snapshot"):
            try:
                from .analysis_snapshot import AnalysisSnapshotDaemon
            except ImportError as exc:
                logger.debug(
                    "AnalysisSnapshotDaemon not available (open-core build): %s",
                    exc,
                )
            else:
                try:
                    d = AnalysisSnapshotDaemon(
                        interval=daemons_config.analysis_snapshot.interval,
                        config=daemons_config.analysis_snapshot,
                    )
                    _configure(d)
                    manager.register(d)
                except Exception as exc:
                    _record_failure("analysis_snapshot", exc)

        if _hosted_ok("trend_snapshot"):
            try:
                from .trend_snapshot import TrendSnapshotDaemon
            except ImportError as exc:
                logger.debug(
                    "TrendSnapshotDaemon not available (open-core build): %s", exc
                )
            else:
                try:
                    d = TrendSnapshotDaemon(
                        interval=daemons_config.trend_snapshot.interval,
                        config=daemons_config.trend_snapshot,
                    )
                    _configure(d)
                    manager.register(d)
                except Exception as exc:
                    _record_failure("trend_snapshot", exc)

        # Always attempt start_all — even if some daemons failed registration,
        # the ones that succeeded should run. Pre-2026-05-04 this was inside
        # the outer try-block, so one daemon's failure aborted start_all and
        # nothing started.
        try:
            manager.start_all()
        except Exception as exc:
            _record_failure("_start_all", exc)

        # Publish registration errors onto the scope entry for daemon_status.
        self._publish_registration_errors(scope_id, registration_errors)

    def _publish_registration_errors(
        self, scope_id: str, errors: list[dict[str, str]]
    ) -> None:
        """Attach registration errors to the live scope entry for surfacing
        via `watercooler_daemon_status`. Idempotent under lock."""
        if not errors:
            return
        with self._lock:
            entry = self._scopes.get(scope_id)
            if entry is not None:
                entry.registration_errors = list(errors)

    @staticmethod
    def _hosted_daemon_defaults() -> dict[str, Any]:
        """Return hosted defaults for the 6 config-gated premium daemons.

        Only premium daemons run on Railway.  Local daemons (thread_auditor,
        decision_detector, decision_extractor, sync_guard, content_scout,
        content_refiner) run on the developer's machine via init_daemons().

        t2_indexer also runs hosted but is additionally gated by the
        graphiti memory backend — its registration short-circuits
        internally when graphiti is unavailable.

        The schema defaults are ``enabled=False`` (opt-in for local), so
        hosted mode needs its own baseline for the premium set.
        """
        return {
            "enabled": True,
            "project_coordinator": {"enabled": True},
            "coordinator_refiner": {"enabled": True},
            "pulse_snapshot": {"enabled": True},
            "pulse_report": {"enabled": True},
            "analysis_snapshot": {"enabled": True},
            "trend_snapshot": {"enabled": True},
            "t2_indexer": {"enabled": True},
        }

    @staticmethod
    def _parse_daemon_config_header(
        raw: str,
    ) -> Optional[dict[str, Any]]:
        """Parse and validate an ``X-Daemon-Config`` header.

        Returns the parsed override dict on success, or ``None`` if the
        payload was rejected.  Rejections are logged with
        ``X-Daemon-Config rejected`` so operators can grep for abuse.

        Enforces:
          * byte-size cap (``_MAX_DAEMON_CONFIG_BYTES``) before parsing
          * structural-depth cap (``_MAX_DAEMON_CONFIG_DEPTH``) post-parse
          * top-level key allowlist — only fields known to
            ``DaemonsConfig`` are permitted; nested sub-config fields
            remain ``extra="ignore"`` per existing permissive schema.
        """
        from watercooler.config_schema import DaemonsConfig

        size = len(raw.encode("utf-8"))
        if size > _MAX_DAEMON_CONFIG_BYTES:
            logger.warning(
                "X-Daemon-Config rejected (reason=size_cap, size=%d, limit=%d)",
                size,
                _MAX_DAEMON_CONFIG_BYTES,
            )
            return None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "X-Daemon-Config rejected (reason=invalid_json, size=%d, error=%s)",
                size,
                exc,
            )
            return None

        if not isinstance(parsed, dict):
            logger.warning(
                "X-Daemon-Config rejected (reason=not_object, size=%d, type=%s)",
                size,
                type(parsed).__name__,
            )
            return None

        depth = _json_depth(parsed)
        if depth > _MAX_DAEMON_CONFIG_DEPTH:
            logger.warning(
                "X-Daemon-Config rejected (reason=depth_cap, size=%d, depth=%d, limit=%d)",
                size,
                depth,
                _MAX_DAEMON_CONFIG_DEPTH,
            )
            return None

        known_fields = set(DaemonsConfig.model_fields)
        unknown = set(parsed) - known_fields
        if unknown:
            logger.warning(
                "X-Daemon-Config rejected (reason=unknown_keys, size=%d, keys=%s)",
                size,
                sorted(unknown),
            )
            return None

        return parsed

    @staticmethod
    def _resolve_daemon_config() -> "DaemonsConfig":
        """Resolve daemon config for hosted scopes.

        Builds a config by layering user overrides (sent via
        ``X-Daemon-Config`` header from the hybrid client) onto hosted
        defaults where premium daemons are enabled.

        The hybrid client sends only explicitly-set values
        (``model_dump(exclude_unset=True)``), so unconfigured daemons
        get the hosted default (enabled) rather than the schema default
        (disabled).  Explicit ``enabled = false`` overrides ARE sent.

        Resolution (highest priority first):
          1. ``X-Daemon-Config`` header (hybrid client explicit set),
             subject to size / depth / allowlist guards in
             ``_parse_daemon_config_header``.
          2. Local ``config.toml`` **explicit set** values, layered onto
             hosted defaults.  This preserves hosted-friendly defaults
             (``t2_indexer`` on, premium daemons on) when the deployment
             file omits a stanza — the schema default alone would
             silently disable background ingestion.
          3. Hosted defaults only.
        """
        from watercooler.config_schema import DaemonsConfig

        hosted = HostedDaemonCoordinator._hosted_daemon_defaults()

        # Layer 1 (lowest priority): start from hosted defaults.
        base = hosted

        # Layer 2: deployment-side ``config.toml`` explicit values.  Apply
        # *before* the request header so operators can ship config
        # changes (e.g. ``[mcp.daemons.pulse_report] enabled = false``)
        # without those being silently overridden every time a hybrid
        # client happens to send a ``X-Daemon-Config`` header.
        #
        # Catches a broad ``Exception`` here intentionally: this fallback
        # is the safe path that keeps the hosted scope alive when the
        # local config file is malformed (``ConfigError``), missing,
        # produces an unexpected shape, or fails validation.  Letting
        # the exception bubble out would abort the entire scope's
        # daemon registration, so prefer logging loudly and continuing
        # with the hosted defaults.
        try:
            from watercooler.config_facade import config

            local = config.full().mcp.daemons
            explicit = local.model_dump(exclude_unset=True)
            base = _deep_merge(base, explicit)
        except Exception as exc:  # noqa: BLE001 — see comment above
            logger.warning(
                "Deployment config fallback failed (%s: %s); "
                "using hosted defaults so the scope still gets daemons",
                type(exc).__name__,
                exc,
            )

        # Layer 3 (highest priority): request-scoped header from the
        # hybrid client.  Layered last so per-request overrides win,
        # but onto the merged hosted+deployment base — a header that
        # only sets ``project_coordinator.enabled = false`` should
        # leave the deployment's other overrides intact.
        from watercooler_mcp.context import get_effective_context

        ctx = get_effective_context()
        overrides: Optional[dict[str, Any]] = None
        if ctx and ctx.daemon_config_json:
            overrides = HostedDaemonCoordinator._parse_daemon_config_header(
                ctx.daemon_config_json
            )

        # Validation: try the full merge first.  On schema violation,
        # retry WITHOUT the header — hostile / malformed request input
        # must not be able to discard the operator's deployment layer.
        # Only if the deployment layer itself validates-fail do we fall
        # back to hosted defaults alone.
        if overrides is not None:
            try:
                return DaemonsConfig.model_validate(_deep_merge(base, overrides))
            except ValidationError as exc:
                logger.warning(
                    "X-Daemon-Config rejected (reason=schema_violation, error=%s); "
                    "retrying without header so deployment overrides survive",
                    exc,
                )

        try:
            return DaemonsConfig.model_validate(base)
        except ValidationError as exc:
            logger.warning(
                "Deployment daemon config invalid (reason=schema_violation, error=%s); "
                "falling back to hosted defaults",
                exc,
            )
            return DaemonsConfig.model_validate(hosted)

    @staticmethod
    def _try_register_t2_indexer_hosted(manager: DaemonManager, _configure) -> None:
        """Register T2 indexer in a hosted scope if graphiti is available."""
        import os

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
            logger.warning(
                "DAEMONS: graphiti imports unavailable, skipping t2_indexer (hosted)"
            )
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
        """Return status for one or more scopes.

        ``registration_errors`` is included on every per-scope payload so
        ``watercooler_daemon_status`` can surface daemons that failed to
        register (e.g., STRICT_NAMESPACE ValueError before scope was
        configured) without requiring callers to page through Railway logs.
        """
        with self._lock:
            if scope_id:
                entry = self._scopes.get(scope_id)
                if not entry:
                    return {"error": f"scope {scope_id} not found"}
                return {
                    "scope_id": scope_id,
                    "daemons": entry.manager.status_all(),
                    "idle_seconds": time.monotonic() - entry.last_touched,
                    "registration_errors": list(entry.registration_errors),
                }
            if user_id:
                result: dict[str, Any] = {}
                for sid, entry in self._scopes.items():
                    if entry.key.user_id == user_id:
                        result[sid] = {
                            "daemons": entry.manager.status_all(),
                            "idle_seconds": time.monotonic() - entry.last_touched,
                            "registration_errors": list(entry.registration_errors),
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
                        "registration_errors": list(e.registration_errors),
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

    def acknowledge_finding(
        self,
        scope_id: str | None,
        daemon_name: str,
        finding_id: str,
    ) -> bool:
        """Acknowledge a finding within a scope's namespace."""
        from .state import acknowledge_finding as _ack_finding

        namespace = scope_id or ""
        return _ack_finding(daemon_name, finding_id, namespace=namespace)

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
