"""Tests for watercooler_health(detail="graph") — graph observability.

PR 4 of the bug-hybrid-static-x-repo-cross-tenant-t2-scope plan (Jay's
health-tool request). Covers classification, the graph_admin tenancy gate
(user-no-grant / user-with-grant / service-key), enumeration output, and
hybrid forwarding.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watercooler_mcp.graph_detail import build_graph_detail, classify_graph
from watercooler_mcp.tools import diagnostic


class TestClassifyGraph:
    def test_canonical(self) -> None:
        bases = {"mostlyharmless_ai_watercooler_cloud"}
        assert classify_graph(
            "mostlyharmless_ai_watercooler_cloud_t2", bases
        ) == "canonical"
        assert classify_graph(
            "mostlyharmless_ai_watercooler_cloud_t1", bases
        ) == "canonical"

    def test_legacy_orphan_single_token_base(self) -> None:
        # The money-loop signature: cwd/threads_dir-basename fallbacks.
        assert classify_graph("app_t2", set()) == "legacy_orphan"
        assert classify_graph("watercooler_t2", set()) == "legacy_orphan"

    def test_legacy_orphan_tierless(self) -> None:
        assert classify_graph("some_random_graph", set()) == "legacy_orphan"

    def test_foreign_well_formed_other_tenant(self) -> None:
        bases = {"mostlyharmless_ai_watercooler_cloud"}
        assert classify_graph(
            "mostlyharmless_ai_watercooler_site_t2", bases
        ) == "foreign"


class _FakeResult:
    def __init__(self, value):
        self.result_set = [[value]]


class _FakeGraph:
    def __init__(self, nodes=10, edges=5, episodes=3, last="2026-07-01"):
        self._answers = {
            "MATCH (n) RETURN count(n)": nodes,
            "MATCH ()-[r]->() RETURN count(r)": edges,
            "MATCH (e:Episodic) RETURN count(e)": episodes,
            "MATCH (e:Episodic) RETURN max(e.created_at)": last,
        }

    def query(self, q):
        return _FakeResult(self._answers.get(q, 0))


class _FakeFalkor:
    def __init__(self, graphs):
        self._graphs = graphs

    def list_graphs(self):
        return list(self._graphs)

    def select_graph(self, name):
        return self._graphs[name]


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch):
    graphs = {
        "mostlyharmless_ai_watercooler_cloud_t2": _FakeGraph(episodes=100),
        "app_t2": _FakeGraph(nodes=5000, episodes=900),
        "mostlyharmless_ai_watercooler_site_t2": _FakeGraph(episodes=20),
    }
    monkeypatch.setattr(
        "watercooler_mcp.hosted_semantic._get_falkor_client",
        lambda: _FakeFalkor(graphs),
    )
    return graphs


class TestBuildGraphDetail:
    def test_full_enumeration_with_admin(self, fake_client) -> None:
        report = build_graph_detail(
            canonical_bases={"mostlyharmless_ai_watercooler_cloud"},
            include_all_scopes=True,
        )
        assert report["available"] is True
        by_name = {g["name"]: g for g in report["graphs"]}
        assert by_name["mostlyharmless_ai_watercooler_cloud_t2"]["flag"] == (
            "canonical"
        )
        assert by_name["app_t2"]["flag"] == "legacy_orphan"
        assert by_name["app_t2"]["episodes"] == 900
        assert by_name["mostlyharmless_ai_watercooler_site_t2"]["flag"] == (
            "foreign"
        )
        assert report["last_write"]["database"] is not None

    def test_restricted_view_aggregates_other_scopes(self, fake_client) -> None:
        report = build_graph_detail(
            canonical_bases={"mostlyharmless_ai_watercooler_cloud"},
            include_all_scopes=False,
        )
        names = {g["name"] for g in report["graphs"]}
        assert names == {"mostlyharmless_ai_watercooler_cloud_t2"}
        agg = report["other_scopes"]
        assert agg["graphs"] == 2
        assert agg["total_nodes"] == 5000 + 10

    def test_unreachable_falkordb_clean_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(
            "watercooler_mcp.hosted_semantic._get_falkor_client", _boom
        )
        report = build_graph_detail(
            canonical_bases=set(), include_all_scopes=True
        )
        assert report["available"] is False
        assert "falkordb_unreachable" in report["error"]


class TestGraphAdminGate:
    def _ctx(self, key_type=None, capabilities=None, user_id="u1"):
        http_ctx = MagicMock()
        http_ctx.auth_key_type = key_type
        http_ctx.capabilities = capabilities
        http_ctx.user_id = user_id
        return http_ctx

    def test_user_without_grant_restricted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #1064 review (P1): ensure() DENIES by returning a JSON
        string, not by raising — a denial must not read as allow."""
        monkeypatch.setattr(
            "watercooler_mcp.auth.is_hosted_mode", lambda: True
        )
        authorizer = MagicMock()
        authorizer.ensure.return_value = (
            '{"error": "capability_not_enabled", "capability": "graph_admin"}'
        )
        runtime = MagicMock()
        runtime.authorizer = authorizer
        monkeypatch.setattr(diagnostic, "_runtime", runtime)
        with patch(
            "watercooler_mcp.context.get_effective_context",
            return_value=self._ctx(capabilities=frozenset({"threads_core"})),
        ):
            assert diagnostic._graph_admin_allowed(MagicMock()) is False

    def test_real_authorizer_denial_string_is_deny(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same check through a REAL CapabilityAuthorizer (no mock of
        ensure): an empty grant set yields a denial string → restricted."""
        from watercooler_mcp.capability_auth import CapabilityAuthorizer

        monkeypatch.setattr(
            "watercooler_mcp.auth.is_hosted_mode", lambda: True
        )
        grant_service = MagicMock()
        grant_service.get_capabilities.return_value = frozenset()
        runtime = MagicMock()
        runtime.authorizer = CapabilityAuthorizer(grant_service)
        monkeypatch.setattr(diagnostic, "_runtime", runtime)
        with patch(
            "watercooler_mcp.context.get_effective_context",
            return_value=self._ctx(capabilities=None),
        ):
            assert diagnostic._graph_admin_allowed(MagicMock()) is False

    def test_real_authorizer_grant_allows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from watercooler_mcp.capability_auth import CapabilityAuthorizer

        monkeypatch.setattr(
            "watercooler_mcp.auth.is_hosted_mode", lambda: True
        )
        grant_service = MagicMock()
        grant_service.get_capabilities.return_value = frozenset(
            {"graph_admin"}
        )
        runtime = MagicMock()
        runtime.authorizer = CapabilityAuthorizer(grant_service)
        monkeypatch.setattr(diagnostic, "_runtime", runtime)
        with patch(
            "watercooler_mcp.context.get_effective_context",
            return_value=self._ctx(capabilities=None),
        ):
            assert diagnostic._graph_admin_allowed(MagicMock()) is True

    def test_authorizer_exception_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "watercooler_mcp.auth.is_hosted_mode", lambda: True
        )
        authorizer = MagicMock()
        authorizer.ensure.side_effect = RuntimeError("grant service down")
        runtime = MagicMock()
        runtime.authorizer = authorizer
        monkeypatch.setattr(diagnostic, "_runtime", runtime)
        with patch(
            "watercooler_mcp.context.get_effective_context",
            return_value=self._ctx(capabilities=None),
        ):
            assert diagnostic._graph_admin_allowed(MagicMock()) is False

    def test_user_with_grant_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "watercooler_mcp.auth.is_hosted_mode", lambda: True
        )
        runtime = MagicMock()
        monkeypatch.setattr(diagnostic, "_runtime", runtime)
        with patch(
            "watercooler_mcp.context.get_effective_context",
            return_value=self._ctx(
                capabilities=frozenset({"threads_core", "graph_admin"})
            ),
        ):
            assert diagnostic._graph_admin_allowed(MagicMock()) is True

    def test_user_with_grant_via_authorizer_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "watercooler_mcp.auth.is_hosted_mode", lambda: True
        )
        authorizer = MagicMock()
        authorizer.ensure.return_value = None  # None = allow (real contract)
        runtime = MagicMock()
        runtime.authorizer = authorizer
        monkeypatch.setattr(diagnostic, "_runtime", runtime)
        with patch(
            "watercooler_mcp.context.get_effective_context",
            return_value=self._ctx(capabilities=None),
        ):
            assert diagnostic._graph_admin_allowed(MagicMock()) is True
        authorizer.ensure.assert_called_once_with(
            "graph_admin", "u1", preloaded_capabilities=None
        )

    def test_service_key_bypasses_grant_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "watercooler_mcp.auth.is_hosted_mode", lambda: True
        )
        authorizer = MagicMock()
        authorizer.ensure.side_effect = AssertionError(
            "service keys must not hit the grant service"
        )
        runtime = MagicMock()
        runtime.authorizer = authorizer
        monkeypatch.setattr(diagnostic, "_runtime", runtime)
        with patch(
            "watercooler_mcp.context.get_effective_context",
            return_value=self._ctx(key_type="service"),
        ):
            assert diagnostic._graph_admin_allowed(MagicMock()) is True

    def test_non_hosted_always_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "watercooler_mcp.auth.is_hosted_mode", lambda: False
        )
        assert diagnostic._graph_admin_allowed(MagicMock()) is True


class TestHybridForwarding:
    def test_hybrid_forwards_detail_graph_to_hosted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hosted_report = {"available": True, "graphs": []}
        client = MagicMock()
        client.call_tool_text = AsyncMock(
            return_value=json.dumps(hosted_report)
        )
        pool = MagicMock()
        pool.default = client
        pool.client_for_path = MagicMock(return_value=client)
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = client
        runtime.premium_pool = pool
        monkeypatch.setattr(diagnostic, "_runtime", runtime)

        out = json.loads(
            diagnostic._health_graph_impl(MagicMock(), code_path="/repo")
        )
        assert out["routed_via"] == "hybrid_forward"
        assert "local_falkordb" in out
        client.call_tool_text.assert_awaited_once()
        name, args = client.call_tool_text.await_args.args
        assert name == "watercooler_health"
        assert args["detail"] == "graph"

    def test_hybrid_hosted_unreachable_clean_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.call_tool_text = AsyncMock(side_effect=RuntimeError("down"))
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = client
        runtime.premium_pool = None
        monkeypatch.setattr(diagnostic, "_runtime", runtime)

        out = json.loads(diagnostic._health_graph_impl(MagicMock()))
        assert out["available"] is False
        assert "unreachable" in out["error"]
