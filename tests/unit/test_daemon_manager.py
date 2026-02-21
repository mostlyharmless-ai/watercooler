"""Tests for DaemonManager: registry, lifecycle, health, event dispatch."""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from watercooler_mcp.daemons.base import BaseDaemon, DaemonStatus
from watercooler_mcp.daemons.errors import DaemonAlreadyRegisteredError, DaemonNotFoundError
from watercooler_mcp.daemons.manager import DaemonManager
from watercooler_mcp.daemons.state import Finding

from tests.unit.daemon_helpers import StubDaemon


class TestDaemonManager:
    def test_register_and_retrieve(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        mgr = DaemonManager()
        daemon = StubDaemon(name="d1", interval=1.0)
        mgr.register(daemon)
        assert mgr.get_daemon("d1") is daemon
        assert "d1" in mgr.daemon_names

    def test_register_duplicate_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        mgr = DaemonManager()
        mgr.register(StubDaemon(name="d1", interval=1.0))
        with pytest.raises(DaemonAlreadyRegisteredError):
            mgr.register(StubDaemon(name="d1", interval=1.0))

    def test_get_daemon_returns_none_for_unknown(self):
        mgr = DaemonManager()
        assert mgr.get_daemon("nonexistent") is None

    def test_start_all_and_stop_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        mgr = DaemonManager()
        d1 = StubDaemon(name="d1", interval=0.1)
        d2 = StubDaemon(name="d2", interval=0.1)
        mgr.register(d1)
        mgr.register(d2)

        mgr.start_all()
        assert d1.is_running
        assert d2.is_running

        mgr.stop_all()
        assert d1.status == DaemonStatus.STOPPED
        assert d2.status == DaemonStatus.STOPPED

    def test_status_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        mgr = DaemonManager()
        mgr.register(StubDaemon(name="d1", interval=1.0))
        mgr.register(StubDaemon(name="d2", interval=2.0, enabled=False))

        statuses = mgr.status_all()
        assert "d1" in statuses
        assert "d2" in statuses
        assert statuses["d1"]["status"] == "stopped"
        assert statuses["d2"]["enabled"] is False

    def test_dispatch_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        mgr = DaemonManager()
        d1 = StubDaemon(name="d1", interval=1.0)
        d2 = StubDaemon(name="d2", interval=1.0)
        mgr.register(d1)
        mgr.register(d2)

        mgr.dispatch_event("thread_updated", {"topic": "t1"})
        assert len(d1.events) == 1
        assert d1.events[0] == ("thread_updated", {"topic": "t1"})
        assert len(d2.events) == 1

    def test_get_all_findings_single_daemon(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        mgr = DaemonManager()
        d = StubDaemon(name="finder", interval=1.0)
        mgr.register(d)
        # No findings yet
        findings = mgr.get_all_findings(limit=10)
        assert findings == []

    def test_get_all_findings_daemon_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        mgr = DaemonManager()
        with pytest.raises(DaemonNotFoundError):
            mgr.get_all_findings(daemon="ghost", limit=10)

    def test_daemon_names_empty(self):
        mgr = DaemonManager()
        assert mgr.daemon_names == []

    def test_start_all_handles_error_gracefully(self, tmp_path, monkeypatch):
        """start_all should not crash if one daemon fails to start."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        mgr = DaemonManager()

        class BadDaemon(BaseDaemon):
            def start(self):
                raise RuntimeError("boom")
            def tick(self):
                return []

        d1 = BadDaemon(name="bad", interval=1.0)
        d2 = StubDaemon(name="good", interval=0.1)
        mgr.register(d1)
        mgr.register(d2)

        mgr.start_all()  # Should not raise
        assert d2.is_running
        mgr.stop_all()
