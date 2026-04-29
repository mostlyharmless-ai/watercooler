"""Conflict-resolution registration tests for ``decision_stance`` daemon.

The premium ``project_coordinator`` and the open-core ``decision_stance`` both
emit ``Finding(category="stance_advisory", topic="stance:{role}")``. To avoid
double emission, ``decision_stance`` registers locally only when no coordinator
is configured to run anywhere (any route).

Mirrors the pattern in ``tests/unit/test_daemon_registration.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import watercooler_mcp.daemons as _daemons_module
from watercooler_mcp.daemons import _PREMIUM_DAEMONS, init_daemons

_ALL_DAEMON_NAMES: tuple[str, ...] = (
    "thread_auditor",
    "content_scout",
    "content_refiner",
    "decision_detector",
    "decision_extractor",
    "decision_stance",
    "project_coordinator",
    "coordinator_refiner",
    "pulse_snapshot",
    "pulse_report",
    "analysis_snapshot",
    "trend_snapshot",
    "sync_guard",
)


def _make_config(
    *,
    enable: set[str] | None = None,
    transport: str = "stdio",
    routes: dict[str, str] | None = None,
) -> MagicMock:
    """Return a config stub with only *enable* daemons turned on."""
    enable = enable or set()
    routes = routes or {}
    daemons_cfg = MagicMock()
    daemons_cfg.enabled = True

    for name in _ALL_DAEMON_NAMES:
        sub = getattr(daemons_cfg, name)
        sub.enabled = name in enable
        sub.route = routes.get(name, "auto")
    # decision_stance has no route field — it's open-core only.
    daemons_cfg.decision_stance.route = "auto"
    daemons_cfg.t2_indexer.enabled = False
    daemons_cfg.t2_indexer.route = "auto"

    cfg = MagicMock()
    cfg.mcp.daemons = daemons_cfg
    cfg.mcp.transport = transport
    cfg.mcp.capability_routes = {}
    return cfg


@pytest.fixture(autouse=True)
def _reset_manager():
    """Reset module-level singletons so init_daemons() runs fresh."""
    orig_manager = _daemons_module._manager
    orig_coordinator = _daemons_module._coordinator
    _daemons_module._manager = None
    _daemons_module._coordinator = None
    try:
        with (
            patch(
                "watercooler_mcp.daemons._try_acquire_daemon_lock",
                return_value=True,
            ),
            patch("watercooler_mcp.auth.is_hosted_mode", return_value=False),
            patch("watercooler_mcp.daemons._try_register_t2_indexer"),
        ):
            yield
    finally:
        _daemons_module._manager = orig_manager
        _daemons_module._coordinator = orig_coordinator


class TestDecisionStanceRegistration:
    """The conflict-resolution gate behaves as documented in the proposal."""

    def test_decision_stance_registers_when_coordinator_disabled(self) -> None:
        """Coordinator off → open-core stance daemon registers."""
        cfg = _make_config(enable={"decision_stance"})

        with patch("watercooler.config_facade.config.full", return_value=cfg):
            mgr = init_daemons(start=False)

        assert "decision_stance" in mgr.daemon_names
        assert "project_coordinator" not in mgr.daemon_names

    def test_decision_stance_skipped_when_coordinator_enabled_local(self) -> None:
        """Coordinator on (stdio → local) → open-core stance daemon stays out."""
        cfg = _make_config(
            enable={"project_coordinator", "decision_stance"},
            transport="stdio",
        )

        with patch("watercooler.config_facade.config.full", return_value=cfg):
            mgr = init_daemons(start=False)

        assert "project_coordinator" in mgr.daemon_names
        assert "decision_stance" not in mgr.daemon_names

    def test_decision_stance_skipped_when_coordinator_enabled_hosted(self) -> None:
        """Coordinator on with hybrid transport (routes hosted) → still skip
        the open-core stance daemon. The hosted coordinator's findings reach
        agents via the federation bridge; double emission must not occur."""
        cfg = _make_config(
            enable={"project_coordinator", "decision_stance"},
            transport="hybrid",
        )

        with patch("watercooler.config_facade.config.full", return_value=cfg):
            mgr = init_daemons(start=False)

        # In hybrid mode, project_coordinator routes hosted (skipped from local
        # registration), but ``decision_stance`` must still defer because the
        # coordinator is producing stance advisories elsewhere.
        assert "project_coordinator" not in mgr.daemon_names
        assert "decision_stance" not in mgr.daemon_names

    def test_decision_stance_runs_when_coordinator_route_disabled(self) -> None:
        """Coordinator explicitly route=disabled → open-core stance daemon
        registers even though ``project_coordinator.enabled`` is True."""
        cfg = _make_config(
            enable={"project_coordinator", "decision_stance"},
            routes={"project_coordinator": "disabled"},
        )

        with patch("watercooler.config_facade.config.full", return_value=cfg):
            mgr = init_daemons(start=False)

        assert "project_coordinator" not in mgr.daemon_names
        assert "decision_stance" in mgr.daemon_names

    def test_decision_stance_skipped_when_disabled(self) -> None:
        cfg = _make_config(enable=set())

        with patch("watercooler.config_facade.config.full", return_value=cfg):
            mgr = init_daemons(start=False)

        assert "decision_stance" not in mgr.daemon_names

    def test_decision_stance_not_in_premium_set(self) -> None:
        """decision_stance is open-core; never premium-gated."""
        assert "decision_stance" not in _PREMIUM_DAEMONS
