"""Tests for the external-daemon loader (workstream J).

Configurable via ``[mcp.daemons.external] modules`` — each entry is
``"module.path:ClassName"``. The loader imports + instantiates + registers
each class via :meth:`DaemonManager.register`; per-entry failures are
captured via :meth:`DaemonManager.record_registration_failure` and surface
through the existing ``registration_errors`` list on the manager (already
serialised into ``watercooler_daemon_status``).
"""
from __future__ import annotations

import sys
import types

import pytest

from watercooler.config_schema import DaemonsConfig, ExternalDaemonsConfig
from watercooler_mcp.daemons import _register_external_daemons
from watercooler_mcp.daemons.base import BaseDaemon
from watercooler_mcp.daemons.manager import DaemonManager


# ============================================================================
# Stub external daemon (instantiable with no args)
# ============================================================================


class StubExternalDaemon(BaseDaemon):
    """A no-arg-instantiable BaseDaemon for loader tests."""

    def __init__(self) -> None:
        # Pick an interval well above test runtime; enabled=False keeps it
        # from starting threads under test even if the manager calls start_all.
        super().__init__(name="stub_external", interval=86_400, enabled=False)

    def tick(self) -> list:  # noqa: D401
        return []


class OtherStubDaemon(BaseDaemon):
    def __init__(self) -> None:
        super().__init__(name="other_stub", interval=86_400, enabled=False)

    def tick(self) -> list:
        return []


@pytest.fixture
def manager():
    return DaemonManager()


@pytest.fixture
def stub_module():
    """Inject a stub module into sys.modules so we can reference it by name.

    Cleaned up on teardown to avoid leaking state between tests.
    """
    mod_name = "wc_stub_external_daemon"
    mod = types.ModuleType(mod_name)
    mod.StubExternalDaemon = StubExternalDaemon
    mod.OtherStubDaemon = OtherStubDaemon
    sys.modules[mod_name] = mod
    yield mod_name
    sys.modules.pop(mod_name, None)


def _error_for(manager: DaemonManager, daemon_label: str) -> dict | None:
    """Return the registration_errors entry for *daemon_label*, or None."""
    for entry in manager.registration_errors:
        if entry["daemon"] == daemon_label:
            return entry
    return None


# ============================================================================
# Config schema
# ============================================================================


class TestConfigSchema:
    def test_external_defaults_to_empty(self):
        cfg = ExternalDaemonsConfig()
        assert cfg.modules == []

    def test_daemons_config_includes_external(self):
        cfg = DaemonsConfig()
        assert isinstance(cfg.external, ExternalDaemonsConfig)
        assert cfg.external.modules == []

    def test_external_modules_accept_list(self):
        cfg = ExternalDaemonsConfig(modules=["pkg.mod:Cls", "other.mod:Other"])
        assert cfg.modules == ["pkg.mod:Cls", "other.mod:Other"]


# ============================================================================
# Loader behaviour
# ============================================================================


class TestLoader:
    def test_no_modules_is_noop(self, manager):
        _register_external_daemons(manager, ExternalDaemonsConfig())
        assert manager.registration_errors == []
        assert manager.daemon_names == []

    def test_successful_load(self, manager, stub_module):
        cfg = ExternalDaemonsConfig(modules=[f"{stub_module}:StubExternalDaemon"])
        _register_external_daemons(manager, cfg)
        assert "stub_external" in manager.daemon_names
        assert manager.registration_errors == []

    def test_malformed_spec_no_colon(self, manager):
        cfg = ExternalDaemonsConfig(modules=["no_colon_here"])
        _register_external_daemons(manager, cfg)
        err = _error_for(manager, "external:no_colon_here")
        assert err is not None
        assert err["error"].startswith("ValueError:")
        assert manager.daemon_names == []

    def test_malformed_spec_empty_parts(self, manager):
        cfg = ExternalDaemonsConfig(modules=[":ClassName", "module:"])
        _register_external_daemons(manager, cfg)
        assert len(manager.registration_errors) == 2
        for entry in manager.registration_errors:
            assert entry["error"].startswith("ValueError:")

    def test_unimportable_module(self, manager):
        cfg = ExternalDaemonsConfig(
            modules=["wc_definitely_not_a_real_module_xyz:Whatever"]
        )
        _register_external_daemons(manager, cfg)
        err = _error_for(
            manager, "external:wc_definitely_not_a_real_module_xyz:Whatever"
        )
        assert err is not None
        assert err["error"].startswith("ModuleNotFoundError:")
        assert "wc_definitely_not_a_real_module_xyz" in err["error"]

    def test_missing_class_symbol(self, manager, stub_module):
        cfg = ExternalDaemonsConfig(modules=[f"{stub_module}:NonExistentClass"])
        _register_external_daemons(manager, cfg)
        err = _error_for(manager, f"external:{stub_module}:NonExistentClass")
        assert err is not None
        assert err["error"].startswith("AttributeError:")

    def test_mixed_success_and_failure(self, manager, stub_module):
        cfg = ExternalDaemonsConfig(
            modules=[
                f"{stub_module}:StubExternalDaemon",
                f"{stub_module}:Missing",
                "no_colon",
            ]
        )
        _register_external_daemons(manager, cfg)
        # The good one still registered despite the bad ones
        assert "stub_external" in manager.daemon_names
        # Both bad ones tracked
        assert len(manager.registration_errors) == 2
        error_strings = " | ".join(e["error"] for e in manager.registration_errors)
        assert "AttributeError" in error_strings
        assert "ValueError" in error_strings

    def test_failure_uses_daemon_name_when_register_raises(self, manager, stub_module):
        """If import + instantiation succeed but .register() raises (e.g.
        duplicate name), the failure label uses the daemon's real .name,
        not the bare spec."""
        cfg = ExternalDaemonsConfig(
            modules=[
                f"{stub_module}:StubExternalDaemon",
                f"{stub_module}:StubExternalDaemon",  # duplicate name on second call
            ]
        )
        _register_external_daemons(manager, cfg)
        # First one registered
        assert "stub_external" in manager.daemon_names
        # Duplicate registration failed against the real daemon name, not
        # the spec — because we got far enough to instantiate before
        # .register() raised DaemonAlreadyRegisteredError.
        err = _error_for(manager, "stub_external")
        assert err is not None

    def test_two_distinct_external_daemons(self, manager, stub_module):
        cfg = ExternalDaemonsConfig(
            modules=[
                f"{stub_module}:StubExternalDaemon",
                f"{stub_module}:OtherStubDaemon",
            ]
        )
        _register_external_daemons(manager, cfg)
        assert "stub_external" in manager.daemon_names
        assert "other_stub" in manager.daemon_names
        assert manager.registration_errors == []
