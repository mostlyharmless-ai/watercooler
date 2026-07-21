"""Tests for watercooler_mcp.daemons.findings_source — the shared
stance-producer mutex resolver consumed by daemon registration and the
Stop hook."""

from __future__ import annotations

import sys
import types

from watercooler_mcp.daemons import findings_source


def _patch_config(
    monkeypatch,
    *,
    daemons_enabled: bool = True,
    coordinator_enabled: bool = False,
    coordinator_route: str = "auto",
    decision_stance_enabled: bool = False,
    transport: str = "stdio",
    hosted: bool = False,
    api_key: str = "",
):
    class _FakeCoordinatorCfg:
        enabled = coordinator_enabled
        route = coordinator_route

    class _FakeDecisionStanceCfg:
        enabled = decision_stance_enabled

    class _FakeDaemons:
        enabled = daemons_enabled
        project_coordinator = _FakeCoordinatorCfg()
        decision_stance = _FakeDecisionStanceCfg()

    _transport_value = transport

    class _FakeMcp:
        daemons = _FakeDaemons()
        transport = _transport_value
        url = "https://findings-source.invalid/mcp/"

    class _FakeFull:
        mcp = _FakeMcp()

    class _FakeConfig:
        @staticmethod
        def full():
            return _FakeFull()

        @staticmethod
        def get_hosted_api_key():
            # Hermetic credential source (review #1135 P1 round 2): "" =
            # credential-less; proxy-effective cases pass an explicit key.
            return api_key

    fake_module = types.ModuleType("watercooler.config_facade")
    fake_module.config = _FakeConfig()
    monkeypatch.setitem(sys.modules, "watercooler.config_facade", fake_module)
    monkeypatch.delenv("WATERCOOLER_MCP_URL", raising=False)
    # Deterministic hosted-mode gate (default local) so the resolver's runtime
    # hosted check does not depend on the real environment.
    import watercooler_mcp.auth as _auth

    monkeypatch.setattr(_auth, "is_hosted_mode", lambda: hosted)


def test_neither_daemon_enabled_resolves_none(monkeypatch):
    """Default config (both daemons off) — no local stance producer. This is
    the bundled-default open-core state: project_coordinator.enabled=False,
    decision_stance.enabled=False."""
    _patch_config(monkeypatch, coordinator_enabled=False, decision_stance_enabled=False)
    assert findings_source.resolve_active_stance_producer() is None


def test_decision_stance_enabled_alone_resolves_decision_stance(monkeypatch):
    _patch_config(monkeypatch, coordinator_enabled=False, decision_stance_enabled=True)
    assert findings_source.resolve_active_stance_producer() == "decision_stance"


def test_coordinator_enabled_local_route_resolves_coordinator(monkeypatch):
    _patch_config(
        monkeypatch,
        coordinator_enabled=True,
        coordinator_route="local",
        decision_stance_enabled=True,
        transport="stdio",
    )
    assert findings_source.resolve_active_stance_producer() == "project_coordinator"


def test_coordinator_enabled_auto_route_stdio_resolves_coordinator(monkeypatch):
    """route="auto" + stdio transport resolves to local per daemon_execution_policy."""
    _patch_config(
        monkeypatch,
        coordinator_enabled=True,
        coordinator_route="auto",
        decision_stance_enabled=True,
        transport="stdio",
    )
    assert findings_source.resolve_active_stance_producer() == "project_coordinator"


def test_coordinator_disabled_route_falls_back_to_decision_stance(monkeypatch):
    _patch_config(
        monkeypatch,
        coordinator_enabled=True,
        coordinator_route="disabled",
        decision_stance_enabled=True,
    )
    assert findings_source.resolve_active_stance_producer() == "decision_stance"


def test_coordinator_disabled_route_with_decision_stance_off_resolves_none(monkeypatch):
    """Coordinator routed off AND decision_stance not enabled → no local
    producer at all (regression case for the bug where decision_stance.enabled
    was never checked)."""
    _patch_config(
        monkeypatch,
        coordinator_enabled=True,
        coordinator_route="disabled",
        decision_stance_enabled=False,
    )
    assert findings_source.resolve_active_stance_producer() is None


def test_coordinator_hosted_route_resolves_none_not_decision_stance(monkeypatch):
    """Coordinator explicitly routed hosted: it's "active" (so decision_stance
    correctly stays suppressed, avoiding double emission) but produces no
    LOCAL findings — the resolver must not point at decision_stance OR at a
    project_coordinator path nothing local writes to."""
    _patch_config(
        monkeypatch,
        coordinator_enabled=True,
        coordinator_route="hosted",
        decision_stance_enabled=True,
    )
    assert findings_source.resolve_active_stance_producer() is None


def test_coordinator_auto_route_hybrid_transport_resolves_none(monkeypatch):
    """route="auto" + hybrid transport: project_coordinator is a premium
    daemon so daemon_execution_policy resolves "hosted", not "local"."""
    _patch_config(
        monkeypatch,
        coordinator_enabled=True,
        coordinator_route="auto",
        decision_stance_enabled=True,
        transport="hybrid",
    )
    assert findings_source.resolve_active_stance_producer() is None


def test_falls_back_to_decision_stance_on_config_load_failure(monkeypatch):
    fake_module = types.ModuleType("watercooler.config_facade")

    class _Boom:
        @staticmethod
        def full():
            raise RuntimeError("config unavailable")

    fake_module.config = _Boom()
    monkeypatch.setitem(sys.modules, "watercooler.config_facade", fake_module)
    assert findings_source.resolve_active_stance_producer() == "decision_stance"


def test_resolve_active_findings_sources_includes_extractor_and_stance_producer(
    monkeypatch,
):
    _patch_config(monkeypatch, coordinator_enabled=False, decision_stance_enabled=True)
    sources = findings_source.resolve_active_findings_sources()
    names = {s.daemon_name for s in sources}
    assert names == {"decision_extractor", "decision_stance"}
    assert len(sources) == 2
    for s in sources:
        assert s.findings_path.name == "findings.jsonl"


def test_resolve_active_findings_sources_switches_with_producer(monkeypatch):
    _patch_config(
        monkeypatch,
        coordinator_enabled=True,
        coordinator_route="local",
        decision_stance_enabled=True,
    )
    sources = findings_source.resolve_active_findings_sources()
    names = {s.daemon_name for s in sources}
    assert names == {"decision_extractor", "project_coordinator"}
    assert "decision_stance" not in names


def test_resolve_active_findings_sources_omits_stance_when_no_local_producer(
    monkeypatch,
):
    """When no daemon is locally active, only decision_extractor is polled —
    no stance source is fabricated."""
    _patch_config(monkeypatch, coordinator_enabled=False, decision_stance_enabled=False)
    sources = findings_source.resolve_active_findings_sources()
    names = {s.daemon_name for s in sources}
    assert names == {"decision_extractor"}


class TestMirrorsRealRegistrationGate:
    """Drift detector: resolve_active_stance_producer must agree with the
    actual registration decisions in daemons/__init__.py::init_daemons for a
    matrix of configs, so the two can never silently diverge again."""

    def _real_registration_decision(
        self,
        *,
        daemons_enabled,
        coordinator_enabled,
        coordinator_route,
        decision_stance_enabled,
        transport,
    ):
        """Reimplements the exact init_daemons gates (not imported, since
        init_daemons is a large side-effecting function) for comparison —
        INCLUDING the global ``daemons.enabled`` gate that returns before any
        daemon registers (daemons/__init__.py:661)."""
        from watercooler_mcp.daemons import daemon_execution_policy

        if not daemons_enabled:
            return None

        class _Cfg:
            enabled = coordinator_enabled
            route = coordinator_route

        pc_policy = daemon_execution_policy(
            "project_coordinator", _Cfg(), transport, in_hosted_coordinator=False
        )
        registers_locally = {}
        if coordinator_enabled and pc_policy == "local":
            registers_locally["project_coordinator"] = True

        class _DsCfg:
            enabled = decision_stance_enabled

        ds_policy = daemon_execution_policy(
            "decision_stance", _DsCfg(), transport, in_hosted_coordinator=False
        )
        coordinator_active = coordinator_enabled and coordinator_route != "disabled"
        if decision_stance_enabled and not coordinator_active and ds_policy == "local":
            registers_locally["decision_stance"] = True

        local_names = list(registers_locally.keys())
        assert len(local_names) <= 1, "mutex violated: both daemons registered locally"
        return local_names[0] if local_names else None

    def test_matrix_agreement(self, monkeypatch):
        matrix = [
            dict(daemons_enabled=True, coordinator_enabled=False, coordinator_route="auto", decision_stance_enabled=False, transport="stdio"),
            dict(daemons_enabled=True, coordinator_enabled=False, coordinator_route="auto", decision_stance_enabled=True, transport="stdio"),
            dict(daemons_enabled=True, coordinator_enabled=True, coordinator_route="local", decision_stance_enabled=True, transport="stdio"),
            dict(daemons_enabled=True, coordinator_enabled=True, coordinator_route="auto", decision_stance_enabled=True, transport="stdio"),
            dict(daemons_enabled=True, coordinator_enabled=True, coordinator_route="auto", decision_stance_enabled=True, transport="hybrid"),
            dict(daemons_enabled=True, coordinator_enabled=True, coordinator_route="hosted", decision_stance_enabled=True, transport="stdio"),
            dict(daemons_enabled=True, coordinator_enabled=True, coordinator_route="disabled", decision_stance_enabled=True, transport="stdio"),
            dict(daemons_enabled=True, coordinator_enabled=True, coordinator_route="disabled", decision_stance_enabled=False, transport="stdio"),
            # Global gate: daemons subsystem off, sub-daemons on → no producer.
            dict(daemons_enabled=False, coordinator_enabled=True, coordinator_route="local", decision_stance_enabled=True, transport="stdio"),
            dict(daemons_enabled=False, coordinator_enabled=False, coordinator_route="auto", decision_stance_enabled=True, transport="stdio"),
            # Proxy (review #1135 P1 round 2): thread-analytic decision_stance
            # routes hosted under EFFECTIVE proxy -> no LOCAL producer. These
            # matrix cases pass transport="proxy" straight into the mirror +
            # resolve_local_stance_producer (policy-level agreement).
            dict(daemons_enabled=True, coordinator_enabled=False, coordinator_route="auto", decision_stance_enabled=True, transport="proxy", api_key="wc_findings_test_key"),
            dict(daemons_enabled=True, coordinator_enabled=True, coordinator_route="auto", decision_stance_enabled=True, transport="proxy", api_key="wc_findings_test_key"),
            dict(daemons_enabled=True, coordinator_enabled=True, coordinator_route="local", decision_stance_enabled=True, transport="proxy", api_key="wc_findings_test_key"),
            # Credential-less proxy: EFFECTIVE stdio (#1128 fallback) — the
            # resolver must agree with the registration decision AT the
            # effective transport, i.e. the local producer exists.
            dict(daemons_enabled=True, coordinator_enabled=False, coordinator_route="auto", decision_stance_enabled=True, transport="proxy", api_key="", effective_transport="stdio"),
        ]
        for case in matrix:
            patch_kwargs = {
                k: v for k, v in case.items() if k != "effective_transport"
            }
            mirror_kwargs = {
                k: v
                for k, v in case.items()
                if k not in ("api_key", "effective_transport")
            }
            # The mirror models init_daemons, which gates on the EFFECTIVE
            # transport — substitute it where a case declares one.
            if "effective_transport" in case:
                mirror_kwargs["transport"] = case["effective_transport"]
            _patch_config(monkeypatch, **patch_kwargs)
            expected = self._real_registration_decision(**mirror_kwargs)
            actual = findings_source.resolve_active_stance_producer()
            assert actual == expected, f"mismatch for {case}: expected {expected}, got {actual}"


class TestGlobalGates:
    """The resolver must honor the global gates init_daemons enforces above
    the pc/ds axis (regression for the drift where these were omitted)."""

    def test_daemons_disabled_globally_resolves_none_despite_subdaemon_enabled(
        self, monkeypatch
    ):
        """[mcp.daemons] enabled=false with a sub-daemon enabled=true: no local
        producer registers, so the resolver must return None (not name a
        producer whose findings log nothing writes)."""
        _patch_config(
            monkeypatch,
            daemons_enabled=False,
            coordinator_enabled=True,
            coordinator_route="local",
            decision_stance_enabled=True,
        )
        assert findings_source.resolve_active_stance_producer() is None

    def test_daemons_disabled_globally_with_decision_stance_resolves_none(
        self, monkeypatch
    ):
        _patch_config(
            monkeypatch,
            daemons_enabled=False,
            coordinator_enabled=False,
            decision_stance_enabled=True,
        )
        assert findings_source.resolve_active_stance_producer() is None

    def test_hosted_mode_resolves_none_despite_local_producer_config(self, monkeypatch):
        """In hosted mode the local manager stays empty and no local findings
        log is written — the resolver returns None regardless of config."""
        _patch_config(
            monkeypatch,
            daemons_enabled=True,
            coordinator_enabled=True,
            coordinator_route="local",
            decision_stance_enabled=True,
            hosted=True,
        )
        assert findings_source.resolve_active_stance_producer() is None


class TestActiveStanceProducerSidecar:
    """The daemon owner serializes the resolved producer to a sidecar the Stop
    hook reads directly (fast path, avoiding a per-turn config build)."""

    def test_write_and_read_named_producer(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "active_stance_producer"
        monkeypatch.setattr(
            findings_source, "ACTIVE_STANCE_PRODUCER_SIDECAR", sidecar
        )
        findings_source.write_active_stance_producer_sidecar("decision_stance")
        assert sidecar.read_text(encoding="utf-8") == "decision_stance"

    def test_write_none_produces_empty_file(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "active_stance_producer"
        monkeypatch.setattr(
            findings_source, "ACTIVE_STANCE_PRODUCER_SIDECAR", sidecar
        )
        findings_source.write_active_stance_producer_sidecar(None)
        assert sidecar.read_text(encoding="utf-8") == ""

    def test_write_creates_parent_dir(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "nested" / "dir" / "active_stance_producer"
        monkeypatch.setattr(
            findings_source, "ACTIVE_STANCE_PRODUCER_SIDECAR", sidecar
        )
        findings_source.write_active_stance_producer_sidecar("project_coordinator")
        assert sidecar.is_file()

    def test_write_swallows_oserror(self, monkeypatch):
        # Point at a path whose parent cannot be created; must not raise.
        monkeypatch.setattr(
            findings_source,
            "ACTIVE_STANCE_PRODUCER_SIDECAR",
            findings_source.Path("/proc/nonexistent/active_stance_producer"),
        )
        findings_source.write_active_stance_producer_sidecar("decision_stance")


class TestStrictNamespaceExemption:
    """resolve_active_findings_sources must not raise under
    WATERCOOLER_FINDINGS_STRICT_NAMESPACE=1 with an empty scope — the Stop
    hook is a local, single-checkout reader, not a hosted multi-tenant
    data path, and is a documented exemption (_allow_unscoped=True)."""

    def test_no_raise_under_strict_namespace_empty_scope(self, monkeypatch):
        monkeypatch.setenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", "1")
        # No config patching needed — default config (decision_stance
        # disabled, project_coordinator disabled) still exercises the
        # _daemon_dir path for "decision_extractor" at minimum.
        sources = findings_source.resolve_active_findings_sources()
        assert any(s.daemon_name == "decision_extractor" for s in sources)

    def test_no_raise_under_strict_namespace_with_active_producer(self, monkeypatch):
        monkeypatch.setenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", "1")
        _patch_config(monkeypatch, coordinator_enabled=False, decision_stance_enabled=True)
        sources = findings_source.resolve_active_findings_sources()
        names = {s.daemon_name for s in sources}
        assert names == {"decision_extractor", "decision_stance"}
