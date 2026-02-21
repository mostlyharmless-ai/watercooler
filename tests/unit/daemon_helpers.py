"""Shared test fixtures for unit tests."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from watercooler_mcp.daemons.base import BaseDaemon
from watercooler_mcp.daemons.state import Finding


class StubDaemon(BaseDaemon):
    """Configurable test stub for daemon tests.

    Shared by test_daemon_base.py and test_daemon_manager.py.
    Supports configurable findings, tick delays, and error injection.
    """

    def __init__(self, *, findings=None, tick_delay=0, raise_on_tick=False, **kwargs):
        super().__init__(**kwargs)
        self._findings = findings or []
        self._tick_delay = tick_delay
        self._raise_on_tick = raise_on_tick
        self.tick_count = 0
        self.events_received: List[tuple] = []
        # Alias for backward compat with manager tests
        self.events = self.events_received

    def tick(self) -> List[Finding]:
        self.tick_count += 1
        if self._raise_on_tick:
            raise RuntimeError("tick error")
        if self._tick_delay:
            time.sleep(self._tick_delay)
        return list(self._findings)

    def on_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        self.events_received.append((event_type, payload))
        self.wake()
