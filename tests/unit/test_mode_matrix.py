"""Mode-coverage acceptance matrix (modality-robustness plan, Phase 1).

Plan of record: audit-transport-modes-hosted-db-2026-07:68
(01KXSARYD3HS8MAAMNGW7WWHND); verified-state audit at :61; design at :66.

One suite asserting, per execution-routing mode ("transport": stdio / http /
proxy / hybrid), the placement and initialization invariants of the DESIGN
(:66). Cells where current behavior deviates from the design are xfail-marked
with their gap ID (:67) — strict, so landing the fixing phase forces the
marker's removal. Everything else asserts current == design and acts as the
regression net for the later phases.

Gap keys used here:
- G1  — proxy daemon placement (Fork A: thread-analytic class hosted;
        local-infrastructure class never registered under proxy)
- G2  — proxy over-initialization (Fork B: transport-gated module init)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from watercooler_mcp.capabilities import HYBRID_DEFAULT_ROUTES
from watercooler_mcp.config import effective_transport
from watercooler_mcp.daemons import _PREMIUM_DAEMONS, daemon_execution_policy

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# The daemon inventory and its placement classes (design :66).
# ---------------------------------------------------------------------------

PREMIUM = (
    "project_coordinator",
    "coordinator_refiner",
    "pulse_snapshot",
    "pulse_report",
    "analysis_snapshot",
    "trend_snapshot",
    "t2_indexer",
    "enrich_supersession",
)

# Open-core daemons whose subject matter is thread content (they read/write
# the thread store, wherever it lives). Design: execution follows the data —
# hosted under proxy, local everywhere else. decision_stance is the stance
# fallback under the PCD registration mutex and reads threads, so it belongs
# to this class (the hosted coordinator's mutex keeps it off when PCD runs).
THREAD_ANALYTIC = (
    "learnings",
    "decision_detector",
    "decision_extractor",
    "thread_auditor",
    "decision_stance",
)

# Daemons whose FUNCTION is the local machine (worktree sync, local service
# probing, private content pipelines). Design: mode-inapplicable under proxy —
# never registered there.
LOCAL_INFRASTRUCTURE = (
    "committer",
    "sync_guard",
    "t2_health_probe",
    "content_scout",
    "content_refiner",
)

ALL_TRANSPORTS = ("stdio", "http", "proxy", "hybrid")


def _cfg(enabled: bool = True, route: str | None = "auto") -> SimpleNamespace:
    """A minimal daemon sub-config shape for daemon_execution_policy.

    Non-premium daemon configs carry no ``route`` field; pass route=None to
    model that (policy getattr-defaults it to "auto").
    """
    ns = SimpleNamespace(enabled=enabled)
    if route is not None:
        ns.route = route
    return ns


class TestPremiumSetIntegrity:
    def test_premium_set_matches_inventory(self):
        assert _PREMIUM_DAEMONS == frozenset(PREMIUM)

    def test_classes_are_disjoint_and_open_core_complete(self):
        classes = (set(PREMIUM), set(THREAD_ANALYTIC), set(LOCAL_INFRASTRUCTURE))
        union: set[str] = set()
        for c in classes:
            assert not (union & c), "placement classes must be disjoint"
            union |= c

        # Completeness (review #1134 P2): every BUILT-IN daemon must carry an
        # intentional placement classification. The authoritative name set is
        # DaemonsConfig's per-daemon sub-configs (excluding the ``external``
        # user-daemon registry and scalar settings) plus ``committer``, which
        # is gated on ``[mcp.sync] async_sync`` and has no DaemonsConfig
        # sub-model. Adding a daemon without classifying it here fails.
        from pydantic import BaseModel

        from watercooler.config_schema import DaemonsConfig

        authoritative = {
            name
            for name, field in DaemonsConfig.model_fields.items()
            if isinstance(field.annotation, type)
            and issubclass(field.annotation, BaseModel)
            and name != "external"
        } | {"committer"}
        assert union == authoritative


class TestPremiumDaemonPlacement:
    """Premium daemons: local under stdio/http, hosted under hybrid/proxy."""

    @pytest.mark.parametrize("name", PREMIUM)
    @pytest.mark.parametrize("transport", ALL_TRANSPORTS)
    def test_auto_route_placement(self, name, transport):
        got = daemon_execution_policy(
            name, _cfg(), transport=transport, in_hosted_coordinator=False
        )
        expected = "hosted" if transport in ("hybrid", "proxy") else "local"
        assert got == expected

    @pytest.mark.parametrize("name", PREMIUM)
    def test_hosted_coordinator_owns_premium(self, name):
        got = daemon_execution_policy(
            name, _cfg(), transport="hybrid", in_hosted_coordinator=True
        )
        assert got == "hosted"

    @pytest.mark.parametrize("name", PREMIUM)
    def test_explicit_routes_still_win(self, name):
        # route="local" is honored wherever a local daemon manager EXISTS...
        for transport in ("stdio", "http", "hybrid"):
            assert (
                daemon_execution_policy(
                    name,
                    _cfg(route="local"),
                    transport=transport,
                    in_hosted_coordinator=False,
                )
                == "local"
            )
        # ...but proxy is CATEGORICALLY remote (review #1135 P1 round 3): the
        # thin client runs no local daemons, so the override resolves as auto.
        assert (
            daemon_execution_policy(
                name, _cfg(route="local"), transport="proxy", in_hosted_coordinator=False
            )
            == "hosted"
        )
        assert (
            daemon_execution_policy(
                name, _cfg(route="disabled"), transport="stdio", in_hosted_coordinator=False
            )
            == "skip"
        )
        assert (
            daemon_execution_policy(
                name, _cfg(enabled=False), transport="stdio", in_hosted_coordinator=False
            )
            == "skip"
        )


class TestThreadAnalyticPlacement:
    """Thread-analytic open-core daemons: execution follows the data.

    Local under stdio/http/hybrid (thread store is local there). Under proxy
    the thread store is hosted, so the design places them hosted (Fork A /
    gap G1). Current behavior places them local — the split-brain the audit
    documented — hence strict xfail until Phase 3 lands.
    """

    @pytest.mark.parametrize("name", THREAD_ANALYTIC)
    @pytest.mark.parametrize("transport", ("stdio", "http", "hybrid"))
    def test_local_where_threads_are_local(self, name, transport):
        got = daemon_execution_policy(
            name, _cfg(route=None), transport=transport, in_hosted_coordinator=False
        )
        assert got == "local"

    @pytest.mark.parametrize("name", THREAD_ANALYTIC)
    def test_proxy_places_thread_analytic_hosted(self, name):
        """G1 (Fork A), landed in Phase 2: thread-analytic daemons never run
        against the local worktree under proxy."""
        got = daemon_execution_policy(
            name, _cfg(route=None), transport="proxy", in_hosted_coordinator=False
        )
        assert got == "hosted"


class TestLocalInfrastructurePlacement:
    """Local-infrastructure daemons: mode-inapplicable under proxy."""

    @pytest.mark.parametrize("name", LOCAL_INFRASTRUCTURE)
    @pytest.mark.parametrize("transport", ("stdio", "http", "hybrid"))
    def test_local_everywhere_threads_are_local(self, name, transport):
        got = daemon_execution_policy(
            name, _cfg(route=None), transport=transport, in_hosted_coordinator=False
        )
        assert got == "local"

    @pytest.mark.parametrize("name", LOCAL_INFRASTRUCTURE)
    def test_proxy_skips_local_infrastructure(self, name):
        """G1 guard (Fork A/B), landed in Phase 2: local-infrastructure
        daemons are mode-inapplicable under proxy."""
        got = daemon_execution_policy(
            name, _cfg(route=None), transport="proxy", in_hosted_coordinator=False
        )
        assert got == "skip"


class TestEffectiveTransportResolution:
    """The credential-gated proxy→stdio fallback (#1128) — current == design."""

    def test_proxy_with_credentials_stays_proxy(self):
        assert effective_transport("proxy", "https://x/mcp/", "wc_key") == "proxy"

    @pytest.mark.parametrize(
        "url,key", [("", ""), ("https://x/mcp/", ""), ("", "wc_key")]
    )
    def test_credential_less_proxy_falls_back_to_stdio(self, url, key):
        assert effective_transport("proxy", url, key) == "stdio"

    @pytest.mark.parametrize("transport", ("stdio", "http", "hybrid"))
    def test_other_transports_pass_through(self, transport):
        assert effective_transport(transport, "", "") == transport


# The design's expected hybrid route for EVERY capability (design :66,
# Fork D). Totality over the capability universe is asserted below —
# a new capability cannot slip past this matrix unclassified.
HYBRID_ROUTE_EXPECTATIONS: dict[str, str] = {
    "threads_core": "local",
    "thread_state_admin": "local",
    "annotation_admin": "local",
    "baseline_search": "local",
    "baseline_maintenance": "local",
    "federation_search": "local",
    "diagnostics": "local",
    "semantic_similarity": "remote",  # Fork D: T1 vectors hosted
    "memory_query": "remote",
    "memory_observe": "remote",
    "memory_ingest": "remote",
    "memory_admin_graph": "disabled",
    "memory_admin_cluster": "disabled",
    "memory_migration": "disabled",
    "daemon_observe": "remote",
    "daemon_control": "remote",
    # Grant-style capability kept in the table so routing stays total
    # over _ALL_CAPABILITY_IDS (review #1134 P2).
    "graph_admin": "local",
}


class TestHybridCapabilityRoutes:
    """Hybrid split (design :66, Fork D): T1 records local, T1 vectors +
    T2 remote, memory admin disabled, observability split by design."""

    @pytest.mark.parametrize(
        "capability,expected", sorted(HYBRID_ROUTE_EXPECTATIONS.items())
    )
    def test_hybrid_default_route(self, capability, expected):
        assert HYBRID_DEFAULT_ROUTES[capability] == expected

    def test_route_matrix_is_total(self):
        """Expectations cover exactly the route table, and the route table
        covers exactly the capability-id universe (review #1134 P2)."""
        from watercooler_mcp.capabilities import _ALL_CAPABILITY_IDS

        assert set(HYBRID_ROUTE_EXPECTATIONS) == set(HYBRID_DEFAULT_ROUTES)
        assert set(HYBRID_DEFAULT_ROUTES) == set(_ALL_CAPABILITY_IDS)


# ---------------------------------------------------------------------------
# Module-initialization invariants per mode (gap G2). Subprocess-based: the
# server module's import-time side effects can only be observed in a fresh
# interpreter.
#
# HERMETIC by construction (review #1134 P1): each probe runs in an isolated
# HOME with its own config.toml ([mcp.daemons] enabled=false in the config
# FILE — there is no env toggle for daemons) and a neutral cwd inside that
# HOME, so neither the operator's user config nor this checkout's project
# .watercooler/config.toml leaks in. "Authenticated proxy" is hermetic too:
# a credentials.toml with a syntactically valid key and an .invalid URL —
# import-time code never dials the endpoint (connection happens in main()).
# ---------------------------------------------------------------------------

_PROBE = r"""
import json, sys

import watercooler_mcp.server as server
import watercooler_mcp.daemons as daemons
from watercooler_mcp.memory_queue import get_queue

from watercooler_mcp.daemons.findings_source import ACTIVE_STANCE_PRODUCER_SIDECAR

sidecar = (
    ACTIVE_STANCE_PRODUCER_SIDECAR.read_text(encoding="utf-8")
    if ACTIVE_STANCE_PRODUCER_SIDECAR.exists()
    else None
)
print(json.dumps({
    "local_surface_built": server.mcp is not None,
    "memory_queue_started": get_queue() is not None,
    "daemon_manager_initialized": daemons.get_daemon_manager() is not None,
    "stance_sidecar": sidecar,
}))
"""

_FULL_LOCAL_STACK = {
    "local_surface_built": True,
    "memory_queue_started": True,
    "daemon_manager_initialized": True,
}


def _import_side_effects(
    tmp_path: Path,
    *,
    transport: str,
    authenticated: bool,
    url_in_config: bool = True,
    extra_env: dict[str, str] | None = None,
    seed_stale_sidecar: bool = False,
    daemons_enabled: bool = False,
    extra_config: str = "",
) -> dict:
    """Import the server module in a hermetic child under a given mode."""
    import json
    import os

    home = tmp_path / f"home-{transport}-{'auth' if authenticated else 'anon'}"
    wc_dir = home / ".watercooler"
    wc_dir.mkdir(parents=True)
    url_line = (
        'url = "https://mode-matrix.invalid/mcp/"\n' if url_in_config else 'url = ""\n'
    )
    (wc_dir / "config.toml").write_text(
        "[mcp]\n"
        f'transport = "{transport}"\n'
        f"{url_line}"
        "\n"
        "[mcp.daemons]\n"
        f"enabled = {'true' if daemons_enabled else 'false'}\n" + extra_config,
        encoding="utf-8",
    )
    if authenticated:
        (wc_dir / "credentials.toml").write_text(
            '[hosted]\napi_key = "wc_mode_matrix_hermetic_test_key"\n',
            encoding="utf-8",
        )
    workdir = home / "workdir"
    workdir.mkdir()
    if seed_stale_sidecar:
        # Simulate a preceding LOCAL-mode run that registered decision_stance
        # and left its sidecar behind (review #1135 P1, round 2).
        sidecar = wc_dir / "daemons" / "active_stance_producer"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("decision_stance", encoding="utf-8")

    env = {
        k: v for k, v in os.environ.items() if not k.startswith("WATERCOOLER_")
    }
    if extra_env:
        env.update(extra_env)
    env["HOME"] = str(home)
    # Windows resolves Path.home() via USERPROFILE (falling back to
    # HOMEDRIVE+HOMEPATH) and ignores HOME — redirect those too so the
    # isolation holds on every platform (review #1134 P2, round 2).
    env["USERPROFILE"] = str(home)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(workdir),
    )
    assert out.returncode == 0, f"probe failed: {out.stderr[-2000:]}"
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestModuleInitPerMode:
    def test_stdio_import_builds_local_stack(self, tmp_path):
        effects = _import_side_effects(
            tmp_path, transport="stdio", authenticated=False
        )
        assert {k: effects[k] for k in _FULL_LOCAL_STACK} == _FULL_LOCAL_STACK

    def test_credential_less_proxy_behaves_like_stdio(self, tmp_path):
        """A credential-less proxy install IS effectively stdio (#1128
        fallback) and must keep the full local stack — this cell must stay
        green through the Phase-2 gating refactor (review #1134 P1: gating
        on the configured string alone would strand the fallback)."""
        effects = _import_side_effects(
            tmp_path, transport="proxy", authenticated=False
        )
        assert {k: effects[k] for k in _FULL_LOCAL_STACK} == _FULL_LOCAL_STACK

    def test_authenticated_proxy_import_is_thin(self, tmp_path):
        """G2 (Fork B), landed in Phase 2: an AUTHENTICATED proxy import
        builds no local tool surface, starts no memory-queue workers, and
        initializes no local daemon manager — gated on the EFFECTIVE
        transport, so the credential-less cell above stays full-local."""
        effects = _import_side_effects(
            tmp_path, transport="proxy", authenticated=True
        )
        assert {k: effects[k] for k in _FULL_LOCAL_STACK} == {
            "local_surface_built": False,
            "memory_queue_started": False,
            "daemon_manager_initialized": False,
        }

    def test_thin_proxy_import_preserves_shared_stance_sidecar(self, tmp_path):
        """Review #1135 P1 (round 3): the stance sidecar is USER-GLOBAL and
        may be owned by ANOTHER repo's live local fleet — the thin proxy
        import must not touch it. Staleness relative to THIS repo is handled
        reader-side (the Stop hook resolves the current repo's effective
        transport and polls no local sources under proxy)."""
        effects = _import_side_effects(
            tmp_path,
            transport="proxy",
            authenticated=True,
            seed_stale_sidecar=True,
        )
        assert effects["local_surface_built"] is False
        assert effects["stance_sidecar"] == "decision_stance"

    def test_authenticated_proxy_with_local_route_override_stays_thin(self, tmp_path):
        """Review #1135 P1 (round 3), end-to-end: an explicit premium
        route="local" cannot resurrect local execution under an effective
        proxy — the import stays thin (and the policy resolves the override
        as auto → hosted, so no resolver promises an unregistered producer)."""
        effects = _import_side_effects(
            tmp_path,
            transport="proxy",
            authenticated=True,
            daemons_enabled=True,
            extra_config=(
                "\n[mcp.daemons.project_coordinator]\n"
                "enabled = true\n"
                'route = "local"\n'
            ),
        )
        assert {k: effects[k] for k in _FULL_LOCAL_STACK} == {
            "local_surface_built": False,
            "memory_queue_started": False,
            "daemon_manager_initialized": False,
        }

    def test_env_supplied_url_proxy_import_is_thin(self, tmp_path):
        """Review #1135 P1: the import gate must consume the same
        env-override-aware snapshot main() dispatches from. Config has
        transport=proxy with an EMPTY TOML url; the url arrives only via
        WATERCOOLER_MCP_URL. Credentials present → effective proxy → thin.
        (The pre-fix resolver read raw config, saw no url, resolved stdio,
        and built the full local stack while dispatch went proxy.)"""
        effects = _import_side_effects(
            tmp_path,
            transport="proxy",
            authenticated=True,
            url_in_config=False,
            extra_env={
                "WATERCOOLER_MCP_URL": "https://mode-matrix-env.invalid/mcp/"
            },
        )
        assert {k: effects[k] for k in _FULL_LOCAL_STACK} == {
            "local_surface_built": False,
            "memory_queue_started": False,
            "daemon_manager_initialized": False,
        }
