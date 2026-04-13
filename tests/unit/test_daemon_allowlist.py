"""Tests for LOCAL_DAEMON_NAMES allowlist — premium daemon process gate.

Verifies that premium daemons (project_coordinator, pulse/analysis/trend
snapshots) are NOT registered in the local init_daemons() path unless the
dev override WATERCOOLER_DEV_LOCAL_DAEMONS=1 is set, and that local daemons
(decision_detector, decision_extractor, thread_auditor, sync_guard,
content_scout, content_refiner) register normally.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import watercooler_mcp.daemons as _daemons_module
from watercooler_mcp.daemons import LOCAL_DAEMON_NAMES, init_daemons


def _make_config(*, enable: set[str] | None = None) -> MagicMock:
    """Return a config stub with only *enable* daemons turned on.

    All daemon sub-configs default to ``enabled = False`` so the
    registration loop only attempts the specific daemons we ask for.
    """
    enable = enable or set()
    daemons_cfg = MagicMock()
    daemons_cfg.enabled = True

    # Explicitly disable all known daemons
    for name in (
        "thread_auditor", "content_scout", "content_refiner",
        "decision_detector", "decision_extractor",
        "project_coordinator",
        "pulse_snapshot", "pulse_report",
        "analysis_snapshot", "trend_snapshot",
        "sync_guard",
    ):
        getattr(daemons_cfg, name).enabled = name in enable

    cfg = MagicMock()
    cfg.mcp.daemons = daemons_cfg
    return cfg


@pytest.fixture(autouse=True)
def _reset_manager():
    """Reset the singleton so init_daemons() runs fresh."""
    orig = _daemons_module._manager
    _daemons_module._manager = None
    with (
        patch("watercooler_mcp.daemons._try_acquire_daemon_lock", return_value=True),
        patch("watercooler_mcp.auth.is_hosted_mode", return_value=False),
    ):
        yield
    _daemons_module._manager = orig


class TestLocalDaemonAllowlist:
    """LOCAL_DAEMON_NAMES allowlist blocks premium daemons from registering locally."""

    def test_local_daemon_registers_normally(self) -> None:
        """thread_auditor (in LOCAL_DAEMON_NAMES) registers when enabled."""
        mock_cfg = _make_config(enable={"thread_auditor"})

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "thread_auditor" in mgr.daemon_names

    def test_premium_daemon_blocked_locally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_coordinator (NOT in LOCAL_DAEMON_NAMES) is blocked without dev override."""
        mock_cfg = _make_config(enable={"project_coordinator"})
        monkeypatch.delenv("WATERCOOLER_DEV_LOCAL_DAEMONS", raising=False)

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "project_coordinator" not in mgr.daemon_names

    def test_premium_daemon_allowed_with_dev_override(self) -> None:
        """project_coordinator registers when WATERCOOLER_DEV_LOCAL_DAEMONS=1."""
        mock_cfg = _make_config(enable={"project_coordinator"})

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch.dict("os.environ", {"WATERCOOLER_DEV_LOCAL_DAEMONS": "1"}),
        ):
            mgr = init_daemons(start=False)

        assert "project_coordinator" in mgr.daemon_names

    def test_premium_blocked_logs_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Blocking a premium daemon logs via _allowed_locally (verified by mock)."""
        mock_cfg = _make_config(enable={"project_coordinator"})
        monkeypatch.delenv("WATERCOOLER_DEV_LOCAL_DAEMONS", raising=False)

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons.logger") as mock_logger,
        ):
            mgr = init_daemons(start=False)

        assert "project_coordinator" not in mgr.daemon_names
        # Verify info was logged about the premium daemon being skipped
        info_calls = [
            call for call in mock_logger.info.call_args_list
            if "premium" in str(call) and "project_coordinator" in str(call)
        ]
        assert len(info_calls) >= 1

    def test_sync_guard_local_daemon(self) -> None:
        """sync_guard (in LOCAL_DAEMON_NAMES) registers when enabled."""
        mock_cfg = _make_config(enable={"sync_guard"})

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "sync_guard" in mgr.daemon_names

    def test_remaining_premium_daemons_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Truly premium daemons (project_coordinator, pulse_snapshot) remain blocked locally."""
        premium = {"project_coordinator", "pulse_snapshot"}
        mock_cfg = _make_config(enable=premium)
        monkeypatch.delenv("WATERCOOLER_DEV_LOCAL_DAEMONS", raising=False)

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        for name in premium:
            assert name not in mgr.daemon_names, f"{name} should be blocked locally"

    def test_decision_daemons_are_local(self) -> None:
        """decision_detector and decision_extractor are in LOCAL_DAEMON_NAMES."""
        decision_daemons = {"decision_detector", "decision_extractor"}
        assert decision_daemons <= LOCAL_DAEMON_NAMES, (
            f"Missing from LOCAL_DAEMON_NAMES: {decision_daemons - LOCAL_DAEMON_NAMES}"
        )

    def test_pulse_daemons_are_premium(self) -> None:
        """pulse_snapshot, pulse_report, analysis_snapshot, trend_snapshot are NOT in LOCAL_DAEMON_NAMES."""
        pulse_daemons = {
            "pulse_snapshot", "pulse_report", "analysis_snapshot", "trend_snapshot"
        }
        assert not (pulse_daemons & LOCAL_DAEMON_NAMES), (
            f"Should not be in LOCAL_DAEMON_NAMES: {pulse_daemons & LOCAL_DAEMON_NAMES}"
        )

    def test_project_coordinator_is_premium(self) -> None:
        """project_coordinator is NOT in LOCAL_DAEMON_NAMES (premium — Railway-only)."""
        assert "project_coordinator" not in LOCAL_DAEMON_NAMES

    def test_local_daemon_names_is_frozenset(self) -> None:
        """LOCAL_DAEMON_NAMES is immutable."""
        assert isinstance(LOCAL_DAEMON_NAMES, frozenset)
        with pytest.raises(AttributeError):
            LOCAL_DAEMON_NAMES.add("foo")  # type: ignore[attr-defined]
