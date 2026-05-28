"""DaemonManager — registry, lifecycle, health, and event dispatch.

Manages all registered daemons: start/stop, health queries, findings
aggregation, and event fan-out for future CE runner integration.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import BaseDaemon
from .errors import DaemonAlreadyRegisteredError
from .state import Finding, load_findings

logger = logging.getLogger(__name__)


class DaemonManager:
    """Registry and lifecycle manager for all daemons.

    Usage:
        manager = DaemonManager()
        manager.register(ThreadAuditorDaemon(...))
        manager.start_all()
        # ... server runs ...
        manager.stop_all()

    ``registration_errors`` accumulates structured per-daemon failures
    captured by ``init_daemons`` when a daemon fails to construct or
    register. Each entry is ``{"daemon": str, "error": str}``. Surfaced
    via ``watercooler_daemon_status`` so MCP clients can see why a
    daemon didn't register without paging through process logs — mirrors
    the hosted-side pattern shipped in PR #755.

    ``repo_key`` records which repo this manager's daemons watch.  In
    local mode it's the SHA-1-derived key from ``derive_repo_key()``
    over the CWD's resolved code root; empty string when CWD doesn't
    resolve to a watercooler repo (legacy single-fleet fallback).  In
    hosted mode the per-scope manager constructed by
    ``HostedDaemonCoordinator`` does not set this field — its scope
    identity lives on ``_ScopeEntry.key`` instead.  Used by
    ``watercooler_daemon_status`` to distinguish sibling fleets across
    concurrent MCP servers on one machine.  Per cloud Design (local)
    entry ``01KR5RCWK0F0EM1YVKWRJPD239``.
    """

    def __init__(self, *, repo_key: str = "") -> None:
        self._daemons: Dict[str, BaseDaemon] = {}
        self.registration_errors: List[Dict[str, str]] = []
        self.repo_key: str = repo_key

    def register(self, daemon: BaseDaemon) -> None:
        """Register a daemon. Raises if name is already taken."""
        if daemon.name in self._daemons:
            raise DaemonAlreadyRegisteredError(
                message=f"Daemon '{daemon.name}' already registered",
                context={"daemon_name": daemon.name},
            )
        self._daemons[daemon.name] = daemon
        logger.info("DAEMON_MANAGER: registered '%s'", daemon.name)

    def record_registration_failure(
        self, daemon_name: str, exc: BaseException
    ) -> None:
        """Record a structured per-daemon registration failure.

        Captures the exception class and message so ``watercooler_daemon_status``
        can surface it. Logs at warning level so operators still see it in
        process logs. Idempotent — duplicate `daemon_name` entries are
        appended (matching the hosted-side pattern), since a real
        registration loop only attempts each daemon once per init.
        """
        msg = f"{type(exc).__name__}: {exc}"
        self.registration_errors.append({"daemon": daemon_name, "error": msg})
        logger.warning(
            "DAEMON_MANAGER: failed to register '%s': %s", daemon_name, msg
        )

    def start_all(self) -> None:
        """Start all enabled daemons."""
        for name, daemon in self._daemons.items():
            try:
                daemon.start()
            except Exception as exc:
                logger.warning(
                    "DAEMON_MANAGER: failed to start '%s': %s", name, exc,
                )

    def stop_all(self, timeout: float = 10.0) -> None:
        """Stop all running daemons."""
        for name, daemon in self._daemons.items():
            try:
                daemon.stop(timeout=timeout)
            except Exception as exc:
                logger.warning(
                    "DAEMON_MANAGER: failed to stop '%s': %s", name, exc,
                )

    def get_daemon(self, name: str) -> Optional[BaseDaemon]:
        """Return a daemon by name, or None if not found."""
        return self._daemons.get(name)

    def status_all(self) -> Dict[str, Dict[str, Any]]:
        """Return health summaries for all registered daemons."""
        return {name: d.status_summary() for name, d in self._daemons.items()}

    def get_all_findings(
        self,
        *,
        limit: int = 100,
        daemon: Optional[str] = None,
        severity: Optional[str] = None,
        category: Optional[str] = None,
        topic: Optional[str] = None,
        unacknowledged_only: bool = False,
    ) -> List[Finding]:
        """Aggregate findings across all (or one) daemon.

        Results are sorted newest-first across all daemons, then capped at limit.

        Note: When ``daemon`` is None, reads JSONL files for every registered
        daemon (O(N×daemons) I/O). With the current single-daemon setup this
        is negligible; reassess if many daemons are added.
        """
        if daemon:
            d = self._daemons.get(daemon)
            if d is not None:
                return d.get_findings(
                    limit=limit,
                    severity=severity,
                    category=category,
                    topic=topic,
                    unacknowledged_only=unacknowledged_only,
                )
            # Daemon not registered in this process — read from disk
            # (cross-process case: another process owns the daemon PID lock).
            # Local single-tenant mode only: the
            # HostedDaemonCoordinator owns the hosted-mode findings
            # read path with explicit scope, so this fallback should
            # never be reached in a multi-tenant deployment.
            #
            # PR #730 round 1 MED: explicitly opt out of the strict
            # namespace gate via ``_allow_unscoped=True`` (the
            # documented escape hatch — see
            # ``state._daemon_dir`` docstring). Underscore prefix
            # makes the audit gate greppable so a future reader
            # can review every escape-hatch call site. Asserting
            # we're not in hosted mode would be too late (the
            # hosted-mode coordinator binding is set elsewhere); the
            # cross-process-fallback contract is documented above.
            return load_findings(
                daemon,
                limit=limit,
                severity=severity,
                category=category,
                topic=topic,
                unacknowledged_only=unacknowledged_only,
                _allow_unscoped=True,
            )

        all_findings: List[Finding] = []
        for name, d in self._daemons.items():
            try:
                all_findings.extend(
                    d.get_findings(
                        limit=limit,
                        severity=severity,
                        category=category,
                        topic=topic,
                        unacknowledged_only=unacknowledged_only,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "DAEMON_MANAGER: findings error for '%s': %s", name, exc,
                )
        # Sort newest-first across all daemons
        all_findings.sort(key=lambda f: f.created_at, reverse=True)
        return all_findings[:limit]

    # ------------------------------------------------------------------ #
    # Event dispatch (reserved for CE runner integration)
    # ------------------------------------------------------------------ #

    def dispatch_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Fan out an event to all registered daemons.

        Reserved for future MCP write-path integration (not currently wired).
        Will be called when noteworthy events occur: thread status change,
        new entry, PR merge, etc.

        Each daemon's on_event() method decides whether to act.
        """
        for name, daemon in self._daemons.items():
            try:
                daemon.on_event(event_type, payload)
            except Exception as exc:
                logger.warning(
                    "DAEMON_MANAGER: event dispatch error for '%s': %s",
                    name, exc,
                )

    @property
    def daemon_names(self) -> List[str]:
        return list(self._daemons.keys())
