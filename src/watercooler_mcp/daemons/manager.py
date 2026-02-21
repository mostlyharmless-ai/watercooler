"""DaemonManager — registry, lifecycle, health, and event dispatch.

Manages all registered daemons: start/stop, health queries, findings
aggregation, and event fan-out for future CE runner integration.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import BaseDaemon
from .errors import DaemonAlreadyRegisteredError, DaemonNotFoundError
from .state import Finding

logger = logging.getLogger(__name__)


class DaemonManager:
    """Registry and lifecycle manager for all daemons.

    Usage:
        manager = DaemonManager()
        manager.register(ThreadAuditorDaemon(...))
        manager.start_all()
        # ... server runs ...
        manager.stop_all()
    """

    def __init__(self) -> None:
        self._daemons: Dict[str, BaseDaemon] = {}

    def register(self, daemon: BaseDaemon) -> None:
        """Register a daemon. Raises if name is already taken."""
        if daemon.name in self._daemons:
            raise DaemonAlreadyRegisteredError(
                message=f"Daemon '{daemon.name}' already registered",
                context={"daemon_name": daemon.name},
            )
        self._daemons[daemon.name] = daemon
        logger.info("DAEMON_MANAGER: registered '%s'", daemon.name)

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
            if d is None:
                raise DaemonNotFoundError(
                    message=f"Daemon '{daemon}' not found",
                    context={"daemon_name": daemon},
                )
            return d.get_findings(
                limit=limit,
                severity=severity,
                category=category,
                topic=topic,
                unacknowledged_only=unacknowledged_only,
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
