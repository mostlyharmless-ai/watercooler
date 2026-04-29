"""Tests for config-driven daemon registration in ``init_daemons()``.

Every daemon registers in the local ``init_daemons()`` path iff its
``enabled`` flag in config.toml is True and the process is not running in
hosted mode.  There is no separate allowlist or env-var gate — what
config.toml declares is what registers.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import watercooler_mcp.daemons as _daemons_module
from watercooler_mcp.daemons import init_daemons


# All daemons known to DaemonsConfig. The `_OPEN_CORE_DAEMONS` subset is
# guaranteed importable in every build (imported unconditionally at the
# top of ``watercooler_mcp.daemons``), so cross-daemon tests assert
# against that set.  Private daemons (content_scout, content_refiner,
# pulse_*, analysis_snapshot, trend_snapshot, coordinator_refiner) may
# be stripped by Copybara and are exercised only by per-daemon tests
# that tolerate ``ImportError``.
_ALL_DAEMON_NAMES: tuple[str, ...] = (
    "thread_auditor",
    "content_scout",
    "content_refiner",
    "decision_detector",
    "decision_extractor",
    "project_coordinator",
    "coordinator_refiner",
    "pulse_snapshot",
    "pulse_report",
    "analysis_snapshot",
    "trend_snapshot",
    "sync_guard",
)
_OPEN_CORE_DAEMONS: tuple[str, ...] = (
    "thread_auditor",
    "decision_detector",
    "decision_extractor",
    "project_coordinator",
    "sync_guard",
)


def _make_config(
    *,
    enable: set[str] | None = None,
    t2_indexer_enabled: bool = False,
    transport: str = "stdio",
    capability_routes: dict[str, str] | None = None,
    routes: dict[str, str] | None = None,
) -> MagicMock:
    """Return a config stub with only *enable* daemons turned on.

    ``t2_indexer`` is handled separately because it has no entry in
    ``_ALL_DAEMON_NAMES`` (its registration flows through
    ``_try_register_t2_indexer``, not the main loop).  Default False
    keeps registration-loop tests focused on the daemons they name.

    ``transport`` mirrors ``[mcp].transport`` and drives the
    hybrid/proxy ``route="auto"`` resolution in
    ``daemon_execution_policy``.

    ``routes`` is a per-daemon override for the ``route`` field (PR 4
    replaced capability_routes-based gating with explicit
    ``[mcp.daemons.<name>] route``).  Defaults to ``"auto"``.

    ``capability_routes`` is preserved only for tests that still need
    to exercise the split-brain warning in ``init_daemons``; it no
    longer participates in registration decisions.
    """
    enable = enable or set()
    routes = routes or {}
    daemons_cfg = MagicMock()
    daemons_cfg.enabled = True

    for name in _ALL_DAEMON_NAMES:
        sub = getattr(daemons_cfg, name)
        sub.enabled = name in enable
        sub.route = routes.get(name, "auto")
    daemons_cfg.t2_indexer.enabled = t2_indexer_enabled
    daemons_cfg.t2_indexer.route = routes.get("t2_indexer", "auto")

    cfg = MagicMock()
    cfg.mcp.daemons = daemons_cfg
    cfg.mcp.transport = transport
    cfg.mcp.capability_routes = capability_routes or {}
    return cfg


@pytest.fixture(autouse=True)
def _reset_manager():
    """Reset module-level singletons so init_daemons() runs fresh.

    Also patches ``_try_register_t2_indexer`` so tests focus on the
    config-driven registration path without dragging in the memory
    backend resolution machinery.
    """
    orig_manager = _daemons_module._manager
    orig_coordinator = _daemons_module._coordinator
    _daemons_module._manager = None
    _daemons_module._coordinator = None
    try:
        with (
            patch("watercooler_mcp.daemons._try_acquire_daemon_lock", return_value=True),
            patch("watercooler_mcp.auth.is_hosted_mode", return_value=False),
            patch("watercooler_mcp.daemons._try_register_t2_indexer"),
        ):
            yield
    finally:
        _daemons_module._manager = orig_manager
        _daemons_module._coordinator = orig_coordinator


class TestConfigDrivenRegistration:
    """Every daemon registers when its config flag is set — no allowlist."""

    def test_thread_auditor_registers_when_enabled(self) -> None:
        mock_cfg = _make_config(enable={"thread_auditor"})

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "thread_auditor" in mgr.daemon_names

    def test_project_coordinator_registers_when_enabled(self) -> None:
        """Previously premium — now registers on the enabled flag alone."""
        mock_cfg = _make_config(enable={"project_coordinator"})

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "project_coordinator" in mgr.daemon_names

    def test_coordinator_refiner_registers_when_enabled(self) -> None:
        mock_cfg = _make_config(enable={"coordinator_refiner"})

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "coordinator_refiner" in mgr.daemon_names

    def test_pulse_snapshot_registers_when_enabled(self) -> None:
        mock_cfg = _make_config(enable={"pulse_snapshot"})

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "pulse_snapshot" in mgr.daemon_names

    def test_sync_guard_registers_when_enabled(self) -> None:
        mock_cfg = _make_config(enable={"sync_guard"})

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "sync_guard" in mgr.daemon_names

    def test_daemon_skipped_when_disabled(self) -> None:
        """A daemon with ``enabled=False`` is not registered."""
        mock_cfg = _make_config(enable=set())  # all disabled

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        for name in _ALL_DAEMON_NAMES:
            assert name not in mgr.daemon_names

    def test_open_core_daemons_register_together(self) -> None:
        """Every open-core daemon registers when all are enabled.

        Scoped to the open-core set because private daemons (content_*,
        pulse_*, analysis_snapshot, trend_snapshot, coordinator_refiner)
        may be stripped by Copybara and silently skip registration via
        their ``except ImportError`` guards.
        """
        mock_cfg = _make_config(enable=set(_OPEN_CORE_DAEMONS))

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        for name in _OPEN_CORE_DAEMONS:
            assert name in mgr.daemon_names, f"{name} should register when enabled"

    def test_global_daemons_disabled_registers_none(self) -> None:
        """The top-level ``daemons.enabled=False`` shortcircuits all registration."""
        mock_cfg = _make_config(enable=set(_ALL_DAEMON_NAMES))
        mock_cfg.mcp.daemons.enabled = False

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert mgr.daemon_names == []


class TestHostedModeShortCircuit:
    """Hosted mode routes to HostedDaemonCoordinator and leaves _manager empty."""

    def test_hosted_mode_does_not_register_in_local_manager(self) -> None:
        mock_cfg = _make_config(enable=set(_ALL_DAEMON_NAMES))

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.auth.is_hosted_mode", return_value=True),
            patch(
                "watercooler_mcp.daemons.hosted_coordinator.HostedDaemonCoordinator"
            ) as mock_coord,
        ):
            mock_coord.return_value = MagicMock()
            mgr = init_daemons(start=False)

        assert mgr.daemon_names == []


class TestT2IndexerConfigGate:
    """t2_indexer is opt-in like every other daemon."""

    def test_t2_indexer_default_is_disabled(self) -> None:
        """Schema default keeps existing Graphiti users from auto-starting T2.

        Before the config-driven refactor, local registration required
        ``WATERCOOLER_DEV_LOCAL_DAEMONS=1``.  The new schema default
        preserves that behaviour — users opt in explicitly.
        """
        from watercooler.config_schema import T2IndexerConfig

        assert T2IndexerConfig().enabled is False

    def test_t2_indexer_disabled_skips_registration(self) -> None:
        """``_try_register_t2_indexer`` is not called when config disables it."""
        mock_cfg = _make_config(enable=set(), t2_indexer_enabled=False)

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons._try_register_t2_indexer") as mock_try,
        ):
            init_daemons(start=False)

        mock_try.assert_not_called()

    def test_t2_indexer_enabled_invokes_register(self) -> None:
        """``_try_register_t2_indexer`` is called when config enables it.

        The memory-backend guards inside the helper decide whether the
        daemon actually registers; this test only proves the config gate.
        """
        mock_cfg = _make_config(enable=set(), t2_indexer_enabled=True)

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons._try_register_t2_indexer") as mock_try,
        ):
            init_daemons(start=False)

        mock_try.assert_called_once()

    def test_t2_indexer_local_with_remote_ingest_warns(self) -> None:
        """``init_daemons`` warns when t2_indexer runs locally but the
        ``memory_ingest`` tool surface routes remote — the split-brain
        footgun the old dual-capability rule used to guard against.

        In the PR 4 design the user explicitly chooses
        ``[mcp.daemons.t2_indexer] route = "local"`` and is responsible
        for aligning ``capability_routes``; the warning surfaces the
        misalignment at startup so it is not a silent mystery.
        """
        import watercooler_mcp.daemons as daemons_mod

        mock_cfg = _make_config(
            enable=set(),
            t2_indexer_enabled=True,
            transport="hybrid",
            routes={"t2_indexer": "local"},  # explicit local route
            # capability_routes default: memory_ingest=remote → split-brain
        )

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons._try_register_t2_indexer"),
            patch.object(daemons_mod.logger, "warning") as mock_warn,
        ):
            init_daemons(start=False)

        messages = [
            (call.args[0] % call.args[1:]) if len(call.args) > 1 else call.args[0]
            for call in mock_warn.call_args_list
        ]
        assert any(
            "t2_indexer" in msg and "memory_ingest" in msg
            for msg in messages
        ), f"expected split-brain warning, got: {messages}"


class TestDeprecationShims:
    """PEP 562 ``__getattr__`` shim for names removed in PR #653."""

    def test_local_daemon_names_shim_returns_empty_frozenset(self) -> None:
        import warnings

        import watercooler_mcp.daemons as daemons_mod

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            value = daemons_mod.LOCAL_DAEMON_NAMES  # type: ignore[attr-defined]

        assert value == frozenset()
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "LOCAL_DAEMON_NAMES" in str(w.message)
            for w in caught
        ), "expected a DeprecationWarning for LOCAL_DAEMON_NAMES"

    def test_unknown_attribute_still_raises(self) -> None:
        import watercooler_mcp.daemons as daemons_mod

        with pytest.raises(AttributeError):
            daemons_mod.NOT_A_REAL_NAME  # noqa: B018 — intentional access


class TestServerFactoryDaemonToolsGate:
    """Regression tests for the ``local_hybrid`` daemon-tools gate.

    The server factory suppresses local ``register_daemon_tools`` when
    ``daemon_observe`` routes remote.  Override: when a *premium* daemon
    is pinned ``route="local"``, mount local tools AND suppress the
    proxy mount of daemon tools (otherwise the local tool
    implementations that query ``get_daemon_runtime()`` would silently
    shadow hosted daemons).  Non-premium daemons (sync_guard,
    thread_auditor, ...) always run local and do NOT trigger the
    override — they are not mirrored by proxy tools, so there is no
    shadowing risk.

    Uses real ``DaemonsConfig`` instances (not MagicMock) so the
    helper's field-access iteration works.
    """

    def _wrap(self, daemons_cfg, transport: str = "hybrid") -> MagicMock:
        cfg = MagicMock()
        cfg.mcp.daemons = daemons_cfg
        cfg.mcp.transport = transport
        return cfg

    def test_detects_pinned_local_premium(self) -> None:
        from watercooler.config_schema import DaemonsConfig, ProjectCoordinatorConfig
        from watercooler_mcp.server_factory import _premium_daemon_pinned_local

        daemons_cfg = DaemonsConfig(
            enabled=True,
            project_coordinator=ProjectCoordinatorConfig(
                enabled=True, route="local"
            ),
        )
        with patch(
            "watercooler.config_facade.config.full",
            return_value=self._wrap(daemons_cfg),
        ):
            assert _premium_daemon_pinned_local() is True

    def test_ignores_non_premium_local_daemons(self) -> None:
        """sync_guard / thread_auditor enabled locally must NOT force the override.

        Regression: the previous ``_any_daemon_registers_locally`` helper
        fired on any daemon with ``enabled=True``, including ``sync_guard``
        which defaults enabled=True.  That triggered shadowing of hosted
        daemon tools on every hybrid project with ``daemons.enabled=true``.
        """
        from watercooler.config_schema import (
            DaemonsConfig,
            SyncGuardConfig,
            ThreadAuditorConfig,
        )
        from watercooler_mcp.server_factory import _premium_daemon_pinned_local

        daemons_cfg = DaemonsConfig(
            enabled=True,
            sync_guard=SyncGuardConfig(enabled=True),
            thread_auditor=ThreadAuditorConfig(enabled=True),
        )
        with patch(
            "watercooler.config_facade.config.full",
            return_value=self._wrap(daemons_cfg),
        ):
            assert _premium_daemon_pinned_local() is False

    def test_ignores_compound_artifact_hook(self) -> None:
        """``compound`` is a callable-artifact config, not a daemon.

        Regression: the previous helper iterated *all* DaemonsConfig
        fields with ``.enabled`` and picked up ``compound`` as a
        "local daemon," forcing the tool-mount override with no actual
        daemon backing it.
        """
        from watercooler.config_schema import (
            CompoundConfig,
            DaemonsConfig,
            SyncGuardConfig,
            ThreadAuditorConfig,
        )
        from watercooler_mcp.server_factory import _premium_daemon_pinned_local

        daemons_cfg = DaemonsConfig(
            enabled=True,
            compound=CompoundConfig(enabled=True),
            # Silence the non-premium paths explicitly.
            sync_guard=SyncGuardConfig(enabled=False),
            thread_auditor=ThreadAuditorConfig(enabled=False),
        )
        with patch(
            "watercooler.config_facade.config.full",
            return_value=self._wrap(daemons_cfg),
        ):
            assert _premium_daemon_pinned_local() is False

    def test_false_when_premium_auto_routes_hosted(self) -> None:
        """Premium daemon with default ``route="auto"`` in hybrid → hosted.  No override."""
        from watercooler.config_schema import DaemonsConfig, ProjectCoordinatorConfig
        from watercooler_mcp.server_factory import _premium_daemon_pinned_local

        daemons_cfg = DaemonsConfig(
            enabled=True,
            project_coordinator=ProjectCoordinatorConfig(enabled=True, route="auto"),
        )
        with patch(
            "watercooler.config_facade.config.full",
            return_value=self._wrap(daemons_cfg),
        ):
            assert _premium_daemon_pinned_local() is False

    def test_false_when_daemons_globally_disabled(self) -> None:
        from watercooler.config_schema import DaemonsConfig, ProjectCoordinatorConfig
        from watercooler_mcp.server_factory import _premium_daemon_pinned_local

        daemons_cfg = DaemonsConfig(
            enabled=False,
            project_coordinator=ProjectCoordinatorConfig(enabled=True, route="local"),
        )
        with patch(
            "watercooler.config_facade.config.full",
            return_value=self._wrap(daemons_cfg),
        ):
            assert _premium_daemon_pinned_local() is False


class TestDaemonRuntimeLocation:
    """Public ``daemon_runtime_location`` helper used by diagnostic.py."""

    def test_returns_local_when_sub_config_route_is_local(self) -> None:
        """``route="local"`` on the daemon's sub-config overrides hybrid transport default."""
        from watercooler_mcp.daemons import daemon_runtime_location

        mock_cfg = _make_config(
            transport="hybrid",
            routes={"project_coordinator": "local"},
        )
        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            assert daemon_runtime_location("project_coordinator") == "local"

    def test_returns_hosted_when_hybrid_default(self) -> None:
        from watercooler_mcp.daemons import daemon_runtime_location

        mock_cfg = _make_config(transport="hybrid")
        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            assert daemon_runtime_location("project_coordinator") == "hosted"

    def test_returns_local_for_stdio(self) -> None:
        from watercooler_mcp.daemons import daemon_runtime_location

        mock_cfg = _make_config(transport="stdio")
        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            assert daemon_runtime_location("project_coordinator") == "local"

    def test_returns_local_when_config_facade_missing(self) -> None:
        """Unavailable config facade falls back to local — the safe default
        for diagnostic reporting when the facade cannot load."""
        from watercooler_mcp.daemons import daemon_runtime_location

        with patch(
            "watercooler.config_facade.config.full",
            side_effect=ImportError("simulated"),
        ):
            assert daemon_runtime_location("project_coordinator") == "local"


class TestOpenCoreImportResilience:
    """Lazy imports must swallow ImportError so partially-stripped builds
    still register the daemons that ARE present."""

    def test_coordinator_refiner_import_failure_does_not_abort_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stripped ``coordinator_refiner`` module must not block sync_guard.

        Simulates the open-core / Copybara build where
        ``coordinator_refiner.py`` is removed.  The registration loop
        should log-and-skip rather than raise and abort every daemon
        that follows it (sync_guard, t2_indexer).

        Uses the standard pytest idiom
        (``monkeypatch.setitem(sys.modules, <name>, None)``) — the
        sentinel ``None`` makes the next ``import`` raise ``ImportError``
        without having to intercept ``builtins.__import__`` or swap the
        real module object.  ``monkeypatch`` restores ``sys.modules`` at
        teardown, so other tests keep working against the real module.
        """
        import sys

        module_key = "watercooler_mcp.daemons.coordinator_refiner"
        monkeypatch.setitem(sys.modules, module_key, None)

        mock_cfg = _make_config(
            enable={"coordinator_refiner", "sync_guard", "thread_auditor"},
        )

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "coordinator_refiner" not in mgr.daemon_names
        assert "sync_guard" in mgr.daemon_names
        assert "thread_auditor" in mgr.daemon_names


class TestHostedDaemonDefaults:
    """Hosted coordinator defaults include every premium daemon."""

    def test_hosted_defaults_include_t2_indexer(self) -> None:
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        defaults = HostedDaemonCoordinator._hosted_daemon_defaults()

        assert defaults["t2_indexer"]["enabled"] is True, (
            "Hosted mode should enable t2_indexer by default — Railway relies "
            "on it for graphiti ingestion when no user override is sent."
        )

    def test_hosted_defaults_include_coordinator_refiner(self) -> None:
        """Refiner runs in hosted scopes (PR 1 of remediation plan)."""
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        defaults = HostedDaemonCoordinator._hosted_daemon_defaults()

        assert defaults["coordinator_refiner"]["enabled"] is True, (
            "Hosted mode should enable coordinator_refiner so hybrid users "
            "receive refined_coordinator_lead findings."
        )

    def test_hosted_registers_coordinator_refiner_when_enabled(self) -> None:
        """``_register_daemons_for_scope`` registers the refiner in the manager."""
        from watercooler.config_schema import DaemonsConfig
        from watercooler_mcp.daemons.hosted_coordinator import (
            HostedDaemonCoordinator,
            HostedScopeKey,
        )

        daemons_cfg = DaemonsConfig.model_validate(
            HostedDaemonCoordinator._hosted_daemon_defaults()
        )

        coord = HostedDaemonCoordinator()
        manager = MagicMock()
        key = HostedScopeKey(user_id="u1", repo="org/repo", branch="main")

        # Patch out t2 hosted registration (exercised elsewhere) to keep this
        # test focused on the refiner wiring.
        with (
            patch.object(coord, "_resolve_daemon_config", return_value=daemons_cfg),
            patch.object(
                HostedDaemonCoordinator, "_try_register_t2_indexer_hosted"
            ),
        ):
            coord._register_daemons_for_scope(manager, key, github_token=None)

        registered_types = [
            type(call.args[0]).__name__ for call in manager.register.call_args_list
        ]
        assert "CoordinatorRefinerDaemon" in registered_types

    def test_resolve_uses_hosted_default_when_local_config_omits_t2(self) -> None:
        """Case 2 fallback must NOT let the local schema default disable T2.

        Direct-hosted scopes arrive without an ``X-Daemon-Config`` header
        and with a Railway-side ``config.toml`` that typically does not
        override daemon defaults.  Without the merge-over-defaults fix,
        ``T2IndexerConfig.enabled=False`` (the new local schema default)
        would silently stop background Graphiti ingestion on every
        Railway worker.
        """
        from watercooler.config_schema import DaemonsConfig
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        # Simulate a deployment config that enables the daemon manager
        # but never sets [mcp.daemons.t2_indexer] — everything else uses
        # schema defaults.
        deployment_cfg = MagicMock()
        deployment_cfg.full.return_value.mcp.daemons = DaemonsConfig(enabled=True)

        with (
            patch("watercooler_mcp.context.get_effective_context", return_value=None),
            patch("watercooler.config_facade.config", deployment_cfg),
        ):
            resolved = HostedDaemonCoordinator._resolve_daemon_config()

        assert resolved.t2_indexer.enabled is True, (
            "Hosted default t2_indexer.enabled=True must survive the "
            "local-config fallback when no stanza is present."
        )

    def test_resolve_honors_explicit_local_disable(self) -> None:
        """Explicit ``enabled=false`` in deployment config must win over hosted default."""
        from watercooler.config_schema import DaemonsConfig, T2IndexerConfig
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        # Deployment explicitly disables t2_indexer.
        deployment_cfg = MagicMock()
        deployment_cfg.full.return_value.mcp.daemons = DaemonsConfig(
            enabled=True,
            t2_indexer=T2IndexerConfig(enabled=False),
        )

        with (
            patch("watercooler_mcp.context.get_effective_context", return_value=None),
            patch("watercooler.config_facade.config", deployment_cfg),
        ):
            resolved = HostedDaemonCoordinator._resolve_daemon_config()

        assert resolved.t2_indexer.enabled is False, (
            "An explicit local override must beat the hosted default."
        )

    def test_resolve_layers_header_over_deployment_over_hosted(self) -> None:
        """Header overrides must layer ON TOP OF deployment config, not replace it.

        Regression test for the review finding that a hybrid client
        sending any ``X-Daemon-Config`` header used to short-circuit
        out of deployment config — meaning Railway-side
        ``[mcp.daemons.pulse_report] enabled = false`` was silently
        ignored whenever the client had any daemon override.
        """
        import json as _json

        from watercooler.config_schema import (
            DaemonsConfig,
            PulseReportConfig,
            ProjectCoordinatorConfig,
        )
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        # Deployment disables pulse_report and enables project_coordinator.
        deployment_cfg = MagicMock()
        deployment_cfg.full.return_value.mcp.daemons = DaemonsConfig(
            enabled=True,
            pulse_report=PulseReportConfig(enabled=False),
            project_coordinator=ProjectCoordinatorConfig(enabled=True),
        )

        # Header only touches project_coordinator (disables it).
        ctx = MagicMock()
        ctx.daemon_config_json = _json.dumps(
            {"project_coordinator": {"enabled": False}}
        )

        with (
            patch("watercooler_mcp.context.get_effective_context", return_value=ctx),
            patch("watercooler.config_facade.config", deployment_cfg),
        ):
            resolved = HostedDaemonCoordinator._resolve_daemon_config()

        # Header wins for the field it set.
        assert resolved.project_coordinator.enabled is False
        # Deployment's pulse_report override survives the header merge.
        assert resolved.pulse_report.enabled is False, (
            "Deployment ``enabled=false`` must survive a header that does "
            "not mention pulse_report — previously the header short-circuited "
            "deployment config entirely."
        )

    def test_schema_invalid_header_preserves_deployment_layer(self) -> None:
        """A schema-invalid header must not drop deployment overrides.

        Regression: on ``ValidationError`` during final validation the
        resolver used to fall back to pure hosted defaults, discarding
        the deployment's explicit ``[mcp.daemons.X] enabled = false``.
        The fix retries validation WITHOUT the header before falling
        back, so hostile / corrupt request input cannot override
        operator config.
        """
        import json as _json

        from watercooler.config_schema import (
            DaemonsConfig,
            PulseReportConfig,
        )
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        # Deployment: pulse_report disabled.
        deployment_cfg = MagicMock()
        deployment_cfg.full.return_value.mcp.daemons = DaemonsConfig(
            enabled=True,
            pulse_report=PulseReportConfig(enabled=False),
        )

        # Header: syntactically valid JSON, passes size / depth / key
        # allowlist, but schema-invalid (interval must be a number).
        ctx = MagicMock()
        ctx.daemon_config_json = _json.dumps(
            {"project_coordinator": {"interval": "oops"}}
        )

        with (
            patch("watercooler_mcp.context.get_effective_context", return_value=ctx),
            patch("watercooler.config_facade.config", deployment_cfg),
        ):
            resolved = HostedDaemonCoordinator._resolve_daemon_config()

        # Header rejected → retry without it → deployment's pulse_report
        # override survives.  Previously the fallback jumped straight to
        # hosted defaults, re-enabling pulse_report.
        assert resolved.pulse_report.enabled is False, (
            "schema-invalid header must not discard the deployment's "
            "pulse_report override"
        )

    def test_resolve_uses_hosted_defaults_on_config_error(self) -> None:
        """``ConfigError`` (malformed TOML / validation) falls back to hosted defaults.

        Regression test for the review finding that the narrow
        ``except (ImportError, AttributeError, ValidationError)`` let a
        ``ConfigError`` bubble out of ``_resolve_daemon_config`` and
        crash the entire scope's daemon registration — so a single
        bad ``config.toml`` disabled the full hosted fleet for a repo.
        """
        from watercooler.config_loader import ConfigError
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        deployment_cfg = MagicMock()
        deployment_cfg.full.side_effect = ConfigError("bad TOML on disk")

        with (
            patch("watercooler_mcp.context.get_effective_context", return_value=None),
            patch("watercooler.config_facade.config", deployment_cfg),
        ):
            resolved = HostedDaemonCoordinator._resolve_daemon_config()

        # Hosted defaults preserved — scope still gets its daemons.
        assert resolved.project_coordinator.enabled is True
        assert resolved.pulse_snapshot.enabled is True

    def test_hosted_t2_indexer_config_gate_honored(self) -> None:
        """When hybrid client sends t2_indexer.enabled=false, hosted must skip it."""
        from unittest.mock import MagicMock, patch
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        # Build a daemons_config with t2_indexer disabled (override via
        # _resolve_daemon_config path).
        daemons_cfg = MagicMock()
        daemons_cfg.enabled = True
        for name in _ALL_DAEMON_NAMES:
            getattr(daemons_cfg, name).enabled = False
        daemons_cfg.t2_indexer.enabled = False

        coord = HostedDaemonCoordinator()
        manager = MagicMock()
        key = MagicMock()
        key.scope_id = "test-scope"
        key.user_id = "u1"
        key.repo = "org/repo"
        key.branch = "main"

        with (
            patch.object(coord, "_resolve_daemon_config", return_value=daemons_cfg),
            patch.object(
                HostedDaemonCoordinator, "_try_register_t2_indexer_hosted"
            ) as mock_hosted_t2,
        ):
            coord._register_daemons_for_scope(manager, key, github_token=None)

        mock_hosted_t2.assert_not_called()


class TestDaemonConfigHeaderParsing:
    """Unit tests for ``_parse_daemon_config_header`` — the guard that
    protects the hosted coordinator from malformed or hostile
    ``X-Daemon-Config`` payloads.

    Uses direct logger-spy (``patch.object(logger, "warning")``) rather
    than ``caplog`` because other tests in the suite install handlers
    or toggle propagation on the root logger, making caplog
    unreliable when the full suite runs.
    """

    def _assert_rejected_with_reason(
        self, payload: str, reason: str
    ) -> None:
        from watercooler_mcp.daemons import hosted_coordinator as hc

        with patch.object(hc.logger, "warning") as mock_warn:
            result = hc.HostedDaemonCoordinator._parse_daemon_config_header(payload)
        assert result is None
        messages = [
            (call.args[0] % call.args[1:]) if len(call.args) > 1 else call.args[0]
            for call in mock_warn.call_args_list
        ]
        assert any(f"reason={reason}" in msg for msg in messages), (
            f"expected reason={reason} in warning messages, got: {messages}"
        )

    def test_accepts_well_formed_override(self) -> None:
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        payload = '{"project_coordinator": {"enabled": false}}'
        assert HostedDaemonCoordinator._parse_daemon_config_header(payload) == {
            "project_coordinator": {"enabled": False}
        }

    def test_rejects_oversized_payload(self) -> None:
        from watercooler_mcp.daemons.hosted_coordinator import _MAX_DAEMON_CONFIG_BYTES

        payload = '{"x":"' + ("A" * (_MAX_DAEMON_CONFIG_BYTES + 100)) + '"}'
        self._assert_rejected_with_reason(payload, "size_cap")

    def test_rejects_invalid_json(self) -> None:
        self._assert_rejected_with_reason("{not-json", "invalid_json")

    def test_rejects_non_object_root(self) -> None:
        self._assert_rejected_with_reason("[]", "not_object")

    def test_rejects_excessive_depth(self) -> None:
        # depth 6 — well past the cap of 4
        payload = '{"a":{"b":{"c":{"d":{"e":{"f":1}}}}}}'
        self._assert_rejected_with_reason(payload, "depth_cap")

    def test_rejects_unknown_top_level_keys(self) -> None:
        """Typos / made-up daemon names must be rejected, not silently dropped."""
        payload = '{"project_cooridnator": {"enabled": false}}'  # note typo
        self._assert_rejected_with_reason(payload, "unknown_keys")

    def test_resolve_falls_through_on_rejected_header(self) -> None:
        """Rejected header → resolution uses local config / hosted defaults."""
        from watercooler.config_schema import DaemonsConfig
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        ctx = MagicMock()
        ctx.daemon_config_json = '{"unknown": {"enabled": true}}'

        # Simulate absence of local config — forces case 3 (hosted defaults).
        with (
            patch(
                "watercooler_mcp.context.get_effective_context",
                return_value=ctx,
            ),
            patch(
                "watercooler.config_facade.config.full",
                side_effect=ImportError("no local config"),
            ),
        ):
            resolved = HostedDaemonCoordinator._resolve_daemon_config()

        # Hosted defaults preserved — malicious header didn't land.
        assert isinstance(resolved, DaemonsConfig)
        assert resolved.project_coordinator.enabled is True


class TestHybridTransportRouting:
    """Hybrid/proxy transport routes hosted-offered daemons to Railway.

    Local registration skips the daemons in ``_HOSTED_OFFERED_DAEMONS``
    so they do not double-run.  Local-only daemons (thread_auditor,
    sync_guard, coordinator_refiner, ...) still register locally.
    """

    def test_hybrid_skips_hosted_offered_daemons(self) -> None:
        """Hybrid mode skips every hosted-offered daemon, including the refiner.

        ``coordinator_refiner`` is in ``_HOSTED_OFFERED_DAEMONS`` because its
        upstream (project_coordinator) runs hosted in this mode — keeping the
        refiner local would operate on stale or missing leads.
        """
        enabled = {
            "thread_auditor",
            "project_coordinator",
            "coordinator_refiner",
            "pulse_snapshot",
            "sync_guard",
        }
        mock_cfg = _make_config(
            enable=enabled, t2_indexer_enabled=True, transport="hybrid"
        )

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons._try_register_t2_indexer") as mock_try,
        ):
            mgr = init_daemons(start=False)

        assert "thread_auditor" in mgr.daemon_names
        assert "sync_guard" in mgr.daemon_names
        assert "coordinator_refiner" not in mgr.daemon_names
        assert "project_coordinator" not in mgr.daemon_names
        assert "pulse_snapshot" not in mgr.daemon_names
        mock_try.assert_not_called()

    def test_proxy_skips_hosted_offered_daemons(self) -> None:
        mock_cfg = _make_config(
            enable={"thread_auditor", "project_coordinator"},
            t2_indexer_enabled=True,
            transport="proxy",
        )

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons._try_register_t2_indexer") as mock_try,
        ):
            mgr = init_daemons(start=False)

        assert "thread_auditor" in mgr.daemon_names
        assert "project_coordinator" not in mgr.daemon_names
        mock_try.assert_not_called()

    def test_stdio_registers_hosted_offered_daemons_locally(self) -> None:
        """Default local mode keeps everything in-process (the PR's intent)."""
        mock_cfg = _make_config(
            enable={"project_coordinator", "pulse_snapshot"},
            t2_indexer_enabled=True,
            transport="stdio",
        )

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons._try_register_t2_indexer") as mock_try,
        ):
            mgr = init_daemons(start=False)

        assert "project_coordinator" in mgr.daemon_names
        assert "pulse_snapshot" in mgr.daemon_names
        mock_try.assert_called_once()

    def test_hybrid_explicit_local_route_keeps_daemons_local(self) -> None:
        """Hybrid + ``route="local"`` on each sub-config registers them locally.

        Under PR 4 the user opts in per-daemon via
        ``[mcp.daemons.<name>] route = "local"`` rather than via
        ``capability_routes``.  This keeps premium daemons in-process
        alongside the local tool surface.
        """
        mock_cfg = _make_config(
            enable={"project_coordinator", "pulse_snapshot"},
            t2_indexer_enabled=True,
            transport="hybrid",
            routes={
                "project_coordinator": "local",
                "pulse_snapshot": "local",
                # t2_indexer intentionally left auto → hosted in hybrid.
            },
        )

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons._try_register_t2_indexer") as mock_try,
        ):
            mgr = init_daemons(start=False)

        assert "project_coordinator" in mgr.daemon_names
        assert "pulse_snapshot" in mgr.daemon_names
        # t2_indexer route stays "auto" → hosted in hybrid → skipped locally.
        mock_try.assert_not_called()

    def test_hybrid_auto_route_skips_premium(self) -> None:
        """``route="auto"`` (default) in hybrid mode routes premium daemons hosted."""
        mock_cfg = _make_config(
            enable={"project_coordinator"},
            transport="hybrid",
            # routes omitted → all sub-configs default to "auto"
        )

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "project_coordinator" not in mgr.daemon_names

    def test_hybrid_t2_indexer_route_local_registers(self) -> None:
        """``[mcp.daemons.t2_indexer] route = "local"`` in hybrid pulls it in-process."""
        mock_cfg = _make_config(
            enable=set(),
            t2_indexer_enabled=True,
            transport="hybrid",
            routes={"t2_indexer": "local"},
        )

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons._try_register_t2_indexer") as mock_try,
        ):
            init_daemons(start=False)

        mock_try.assert_called_once()

    def test_hybrid_t2_indexer_auto_skips(self) -> None:
        """Default hybrid (``route="auto"``) skips local T2 indexer — runs hosted."""
        mock_cfg = _make_config(
            enable=set(),
            t2_indexer_enabled=True,
            transport="hybrid",
            # No routes override — fall through to auto → hosted in hybrid.
        )

        with (
            patch("watercooler.config_facade.config.full", return_value=mock_cfg),
            patch("watercooler_mcp.daemons._try_register_t2_indexer") as mock_try,
        ):
            init_daemons(start=False)

        mock_try.assert_not_called()

    def test_route_disabled_skips_daemon(self) -> None:
        """``route="disabled"`` is a soft-disable that blocks registration."""
        mock_cfg = _make_config(
            enable={"project_coordinator"},
            transport="stdio",
            routes={"project_coordinator": "disabled"},
        )

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "project_coordinator" not in mgr.daemon_names

    def test_route_hosted_in_stdio_skips_local_registration(self) -> None:
        """``route="hosted"`` in stdio mode skips local registration.

        The hosted coordinator only exists in hosted/hybrid/proxy modes,
        so a ``route="hosted"`` override in stdio means "don't run this
        anywhere" from the local process's perspective.
        """
        mock_cfg = _make_config(
            enable={"project_coordinator"},
            transport="stdio",
            routes={"project_coordinator": "hosted"},
        )

        with patch("watercooler.config_facade.config.full", return_value=mock_cfg):
            mgr = init_daemons(start=False)

        assert "project_coordinator" not in mgr.daemon_names


class TestPremiumRoutesRemote:
    """Compat shim tests for ``_premium_routes_remote``.

    PR 4 reduced this to a transport-only probe that asks
    ``daemon_execution_policy`` about an enabled+auto daemon.  The
    legacy per-capability inspection is gone — ``tools/diagnostic.py``
    consumes the shim via ``daemon_runtime_location``.
    """

    def _cfg(self) -> MagicMock:
        cfg = MagicMock()
        cfg.mcp.capability_routes = {}
        return cfg

    def test_proxy_always_routes_remote(self) -> None:
        from watercooler_mcp.daemons import _premium_routes_remote

        assert _premium_routes_remote("proxy", self._cfg(), "project_coordinator") is True

    def test_stdio_never_routes_remote(self) -> None:
        from watercooler_mcp.daemons import _premium_routes_remote

        assert _premium_routes_remote("stdio", self._cfg(), "project_coordinator") is False

    def test_http_never_routes_remote(self) -> None:
        from watercooler_mcp.daemons import _premium_routes_remote

        assert _premium_routes_remote("http", self._cfg(), "project_coordinator") is False

    def test_hybrid_premium_daemon_routes_remote(self) -> None:
        """Hybrid mode routes premium daemons hosted under the default ``auto`` probe."""
        from watercooler_mcp.daemons import _premium_routes_remote

        assert _premium_routes_remote("hybrid", self._cfg(), "project_coordinator") is True

    def test_hybrid_non_premium_stays_local(self) -> None:
        """Non-premium daemons (e.g. ``thread_auditor``) always resolve local."""
        from watercooler_mcp.daemons import _premium_routes_remote

        assert _premium_routes_remote("hybrid", self._cfg(), "thread_auditor") is False

    def test_hosted_offered_equals_premium_set(self) -> None:
        """Back-compat alias points at the one source of truth."""
        from watercooler_mcp.daemons import _HOSTED_OFFERED_DAEMONS, _PREMIUM_DAEMONS

        assert _HOSTED_OFFERED_DAEMONS == _PREMIUM_DAEMONS


class TestDaemonExecutionPolicy:
    """Unit tests for the single decision function (PR 4)."""

    def _sub(self, *, enabled: bool = True, route: str = "auto") -> MagicMock:
        sub = MagicMock()
        sub.enabled = enabled
        sub.route = route
        return sub

    def test_disabled_daemon_skipped(self) -> None:
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "project_coordinator", self._sub(enabled=False), "stdio", False
        ) == "skip"

    def test_route_disabled_skipped(self) -> None:
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "project_coordinator", self._sub(route="disabled"), "stdio", False
        ) == "skip"

    def test_route_local_registers_local(self) -> None:
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "project_coordinator", self._sub(route="local"), "hybrid", False
        ) == "local"

    def test_route_hosted_registers_hosted(self) -> None:
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "project_coordinator", self._sub(route="hosted"), "stdio", False
        ) == "hosted"

    def test_auto_stdio_premium_local(self) -> None:
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "project_coordinator", self._sub(route="auto"), "stdio", False
        ) == "local"

    def test_auto_hybrid_premium_hosted(self) -> None:
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "project_coordinator", self._sub(route="auto"), "hybrid", False
        ) == "hosted"

    def test_auto_proxy_premium_hosted(self) -> None:
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "t2_indexer", self._sub(route="auto"), "proxy", False
        ) == "hosted"

    def test_auto_non_premium_always_local(self) -> None:
        """Non-premium daemons (thread_auditor, sync_guard, ...) never route hosted."""
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "thread_auditor", self._sub(route="auto"), "hybrid", False
        ) == "local"

    def test_in_hosted_coordinator_auto_is_hosted(self) -> None:
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "project_coordinator", self._sub(route="auto"), "stdio", True
        ) == "hosted"

    def test_in_hosted_coordinator_respects_local_route(self) -> None:
        """``route="local"`` on the sub-config means the hosted coordinator skips."""
        from watercooler_mcp.daemons import daemon_execution_policy

        assert daemon_execution_policy(
            "project_coordinator", self._sub(route="local"), "hybrid", True
        ) == "local"

    def test_missing_route_attr_treated_as_auto(self) -> None:
        """Non-premium sub-configs have no ``route`` field — default to auto."""
        from watercooler_mcp.daemons import daemon_execution_policy

        sub = MagicMock(spec=["enabled"])
        sub.enabled = True
        assert daemon_execution_policy(
            "thread_auditor", sub, "stdio", False
        ) == "local"
