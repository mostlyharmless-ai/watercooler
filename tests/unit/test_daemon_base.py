"""Tests for BaseDaemon abstract class and DaemonStatus."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from watercooler_mcp.daemons.base import BaseDaemon, DaemonStatus
from watercooler_mcp.daemons.state import Finding


class StubDaemon(BaseDaemon):
    """Configurable test stub for daemon tests."""

    def __init__(self, *, findings=None, tick_delay=0, raise_on_tick=False, **kwargs):
        super().__init__(**kwargs)
        self._findings = findings or []
        self._tick_delay = tick_delay
        self._raise_on_tick = raise_on_tick
        self.tick_count = 0
        self.events_received: List[tuple] = []

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


class TestDaemonStatus:
    def test_enum_values(self):
        assert DaemonStatus.DISABLED.value == "disabled"
        assert DaemonStatus.RUNNING.value == "running"
        assert DaemonStatus.PAUSED.value == "paused"


class TestBaseDaemon:
    def test_disabled_daemon_does_not_start(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_disabled", enabled=False, interval=0.1)
        daemon.start()
        assert daemon.status == DaemonStatus.DISABLED
        assert not daemon.is_running

    def test_start_stop_lifecycle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_lifecycle", interval=0.1)
        daemon.start()
        assert daemon.is_running
        assert daemon.status == DaemonStatus.RUNNING

        stopped = daemon.stop(timeout=5.0)
        assert stopped
        assert daemon.status == DaemonStatus.STOPPED

    def test_double_start_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_double_start", interval=0.1)
        daemon.start()
        daemon.start()  # Should not crash
        assert daemon.is_running
        daemon.stop()

    def test_tick_is_called(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_tick", interval=0.05)
        daemon.start()
        # Wait for at least one tick
        time.sleep(0.3)
        daemon.stop()
        assert daemon.tick_count >= 1

    def test_wake_triggers_immediate_tick(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_wake", interval=60.0)  # Long interval
        daemon.start()
        time.sleep(0.05)  # Let loop start
        count_before = daemon.tick_count
        daemon.wake()
        time.sleep(0.2)
        assert daemon.tick_count > count_before
        daemon.stop()

    def test_pause_resume(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_pause", interval=0.05)
        daemon.start()
        # Poll for first tick instead of a fixed 0.2s sleep — CI runners can
        # stall well past 200ms of wall time and starve the daemon thread.
        deadline = time.monotonic() + 2.0
        while daemon.tick_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert daemon.tick_count >= 1

        daemon.pause()
        assert daemon.status == DaemonStatus.PAUSED
        # Brief grace period to let any in-flight tick finish
        time.sleep(0.1)
        count_at_pause = daemon.tick_count
        time.sleep(0.2)
        # Tick count should not increase while paused
        assert daemon.tick_count == count_at_pause

        daemon.resume()
        # Poll for tick after resume rather than relying on a fixed sleep.
        deadline = time.monotonic() + 2.0
        while daemon.tick_count <= count_at_pause and time.monotonic() < deadline:
            time.sleep(0.01)
        assert daemon.tick_count > count_at_pause
        daemon.stop()

    def test_tick_error_increments_error_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(
            name="test_error", interval=0.05, raise_on_tick=True,
        )
        daemon.start()
        time.sleep(0.3)
        daemon.stop()
        assert daemon._checkpoint.error_count >= 1
        assert daemon._last_error is not None
        assert "tick error" in daemon._last_error

    def test_findings_are_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        test_findings = [
            Finding(
                finding_id="f1",
                daemon_name="test_persist",
                severity="info",
                category="test",
                topic="t",
            )
        ]
        daemon = StubDaemon(
            name="test_persist", interval=0.05, findings=test_findings,
        )
        daemon.start()
        time.sleep(0.3)
        daemon.stop()

        # Check that findings were written
        loaded = daemon.get_findings(limit=100)
        assert len(loaded) >= 1
        assert loaded[0].category == "test"

    def test_status_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_summary", interval=10.0)
        summary = daemon.status_summary()
        assert summary["name"] == "test_summary"
        assert summary["status"] == "stopped"
        assert summary["interval"] == 10.0
        assert summary["enabled"] is True

    def test_on_event_default_wakes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_event", interval=60.0)
        daemon.start()
        time.sleep(0.05)
        daemon.on_event("test_event", {"key": "val"})
        time.sleep(0.2)
        assert daemon.events_received == [("test_event", {"key": "val"})]
        daemon.stop()

    def test_event_driven_daemon(self, tmp_path, monkeypatch):
        """tick_on_interval=False means daemon sleeps until wake()."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(
            name="test_event_driven", interval=0.05, tick_on_interval=False,
        )
        daemon.start()
        time.sleep(0.15)
        # Should NOT have ticked (no interval, no wake)
        assert daemon.tick_count == 0

        daemon.wake()
        time.sleep(0.1)
        assert daemon.tick_count >= 1
        daemon.stop()

    def test_stop_on_already_stopped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_stop_noop", interval=1.0)
        # Not started — stop should return True
        assert daemon.stop() is True


class TestStrictNamespaceConstruction:
    """Layer 1 of the hosted-premium-daemons-zero-registration-2026-05-04 fix:
    ``BaseDaemon.__init__`` must defer ``load_checkpoint`` when constructed
    with an empty namespace under ``WATERCOOLER_FINDINGS_STRICT_NAMESPACE=1``.

    Pre-fix, every premium daemon's ``__init__`` raised ``ValueError`` because
    ``load_checkpoint(name, namespace="")`` hits the strict gate at
    ``state._daemon_dir()`` before the hosted coordinator can call
    ``_configure(d)`` to inject the scope namespace. The hosted coordinator
    then swallowed the exception in its outer ``try/except`` so the operator
    saw an empty daemons dict with no error.
    """

    def test_strict_mode_with_empty_namespace_defers_checkpoint(
        self, tmp_path, monkeypatch
    ):
        """Construction must succeed (checkpoint deferred to ``None``)."""
        monkeypatch.setenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", "1")
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        # No exception expected — pre-fix this raised ValueError.
        daemon = StubDaemon(name="test_deferred", interval=10.0, state_namespace="")
        assert daemon._checkpoint is None

    def test_strict_mode_with_namespace_loads_eagerly(self, tmp_path, monkeypatch):
        """Non-empty namespace under strict mode keeps the eager load."""
        monkeypatch.setenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", "1")
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(
            name="test_eager", interval=10.0, state_namespace="u1:org/repo"
        )
        assert daemon._checkpoint is not None
        assert daemon._checkpoint.daemon_name == "test_eager"

    def test_non_strict_with_empty_namespace_loads_eagerly(
        self, tmp_path, monkeypatch
    ):
        """Local single-tenant mode (strict off, namespace empty) keeps the
        eager load — the load returns a fresh checkpoint."""
        monkeypatch.delenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", raising=False)
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        daemon = StubDaemon(name="test_local", interval=10.0, state_namespace="")
        assert daemon._checkpoint is not None
        assert daemon._checkpoint.daemon_name == "test_local"
