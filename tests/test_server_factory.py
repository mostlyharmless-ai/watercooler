"""Tests for the shared server factory (Step 4)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

from watercooler_mcp.capabilities import (
    GRAPH_TOOL_NAMES,
    HYBRID_DEFAULT_ROUTES,
    HYBRID_DISABLED_TOOL_NAMES,
    HYBRID_REMOTE_MOUNT_TOOLS,
    MIXED_TOOL_NAMES,
    PREMIUM_GRAPH_TOOL_NAMES,
    REMOTE_CAPABLE_MEMORY_TOOL_NAMES,
    CapabilityProfile,
)
from watercooler_mcp.tool_runtime import ToolRuntime
from watercooler_mcp.server_factory import (
    build_default_local_server,
    build_mcp_server,
    graph_tools_for_surface,
    memory_tools_for_surface,
    migration_tools_for_surface,
    mountable_remote_tools_for_hybrid,
)


def _tool_names(mcp: FastMCP) -> set[str]:
    tools = asyncio.run(mcp.list_tools())
    return {t.name for t in tools}


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------


class TestGraphToolsForSurface:
    def test_local_full_gets_all(self):
        rt = ToolRuntime(surface="local_full")
        assert graph_tools_for_surface(rt) == set(GRAPH_TOOL_NAMES)

    def test_hosted_premium_gets_premium_only(self):
        rt = ToolRuntime(surface="hosted_premium")
        assert graph_tools_for_surface(rt) == set(PREMIUM_GRAPH_TOOL_NAMES)

    def test_local_hybrid_gets_all(self):
        rt = ToolRuntime(surface="local_hybrid")
        assert graph_tools_for_surface(rt) == set(GRAPH_TOOL_NAMES)


class TestMemoryToolsForSurface:
    def test_local_full_gets_all_memory(self):
        rt = ToolRuntime(surface="local_full")
        result = memory_tools_for_surface(rt)
        assert result == set(REMOTE_CAPABLE_MEMORY_TOOL_NAMES)

    def test_local_hybrid_excludes_remote_mount_and_disabled(self):
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(surface="local_hybrid", capability_profile=profile)
        result = memory_tools_for_surface(rt)
        # Remote-mount tools should NOT be in the local registration
        assert not result & HYBRID_REMOTE_MOUNT_TOOLS
        # Disabled tools should NOT be in the local registration
        assert not result & HYBRID_DISABLED_TOOL_NAMES

    def test_local_hybrid_registers_bulk_index_as_mixed(self):
        """PR4a review fix — bulk_index is a mixed tool, so it registers
        locally in hybrid (with a per-call routing wrapper) rather than being
        proxy-mounted by bare name."""
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=profile,
            premium_client=MagicMock(),
        )
        assert "watercooler_bulk_index" in memory_tools_for_surface(rt)

    def test_local_hybrid_registers_pooled_reads_as_mixed(self):
        """R3 (#1063): the four memory read tools register locally in hybrid
        (per-call pooled wrappers) rather than being proxy-mounted by bare
        name — a bare mount froze the boot X-Repo for the session."""
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=profile,
            premium_client=MagicMock(),
        )
        assert {
            "watercooler_smart_query",
            "watercooler_diagnose_memory",
            "watercooler_graph_trace",
            "watercooler_memory_task_status",
        } <= memory_tools_for_surface(rt)


class TestMigrationToolsForSurface:
    def test_local_full_gets_none(self):
        """PR4a retired both migration tools — migration.py registers none."""
        rt = ToolRuntime(surface="local_full")
        assert migration_tools_for_surface(rt) == set()

    def test_local_hybrid_default_disabled(self):
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(surface="local_hybrid", capability_profile=profile)
        assert migration_tools_for_surface(rt) == set()


class TestMountableRemoteTools:
    def test_hybrid_mounts_nothing_since_r3(self):
        """R3 (#1063): every former bare mount is now a per-call mixed
        wrapper — a bare mount pins the boot X-Repo for the session. The
        mount machinery stays for future genuinely session-scoped tools."""
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=profile,
            premium_client=MagicMock(),
        )
        result = mountable_remote_tools_for_hybrid(rt)
        assert result == set(HYBRID_REMOTE_MOUNT_TOOLS) == set()

    def test_non_hybrid_returns_empty(self):
        rt = ToolRuntime(surface="local_full")
        assert mountable_remote_tools_for_hybrid(rt) == set()

    def test_daemon_findings_not_proxy_mounted(self):
        """PR5 D1 routed daemon_findings (carrying the acknowledge action)
        to Railway via proxy mount; R3 keeps the remote routing but through
        a per-call pooled wrapper, so it must no longer be bare-mounted."""
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=profile,
            premium_client=MagicMock(),
        )
        result = mountable_remote_tools_for_hybrid(rt)
        assert "watercooler_daemon_findings" not in result

    def test_bulk_index_not_proxy_mounted(self):
        """PR4a review fix — bulk_index is mixed; it must NOT be proxy-mounted.
        A bare mount would expose its preflight_only / run_pipeline modes,
        whose capabilities are disabled by default in hybrid."""
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=profile,
            premium_client=MagicMock(),
        )
        assert (
            "watercooler_bulk_index"
            not in mountable_remote_tools_for_hybrid(rt)
        )


class TestBulkIndexHybridWrapper:
    """PR4a review fix — the bulk_index hybrid wrapper resolves capability per
    (tool, args) so the cross-capability modes keep the hybrid contract the
    retired standalone tools had: default ingest is remote, but preflight_only=
    and run_pipeline= are disabled by default."""

    def _runtime(self):
        return ToolRuntime(
            surface="local_hybrid",
            capability_profile=CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES),
            premium_client=MagicMock(),
        )

    def test_preflight_only_disabled_in_default_hybrid(self):
        import json as _json
        from watercooler_mcp.tools.memory import _build_hybrid_bulk_index_wrapper

        wrapper = _build_hybrid_bulk_index_wrapper(self._runtime())
        result = asyncio.run(wrapper(MagicMock(), preflight_only=True))
        data = _json.loads(result.content[0].text)
        assert data["error"] == "capability_disabled"
        assert data["capability"] == "memory_migration"

    def test_run_pipeline_disabled_in_default_hybrid(self):
        import json as _json
        from watercooler_mcp.tools.memory import _build_hybrid_bulk_index_wrapper

        wrapper = _build_hybrid_bulk_index_wrapper(self._runtime())
        result = asyncio.run(wrapper(MagicMock(), run_pipeline=True))
        data = _json.loads(result.content[0].text)
        assert data["error"] == "capability_disabled"
        assert data["capability"] == "memory_admin_cluster"

    def test_default_ingest_routes_to_remote_proxy(self):
        import json as _json
        from unittest.mock import AsyncMock
        from watercooler_mcp.tools.memory import _build_hybrid_bulk_index_wrapper

        rt = self._runtime()
        rt.premium_client.call_tool_text = AsyncMock(
            return_value='{"action": "bulk_index"}'
        )
        wrapper = _build_hybrid_bulk_index_wrapper(rt)
        result = asyncio.run(wrapper(MagicMock(), backend="graphiti"))
        rt.premium_client.call_tool_text.assert_awaited_once()
        assert _json.loads(result.content[0].text) == {"action": "bulk_index"}


class TestListDecisionsHybridWrapper:
    """include_supersession=True routes list_decisions as memory_query in hybrid."""

    def _runtime(self, premium_client=True):
        client = MagicMock() if premium_client else None
        if client is not None:
            client.call_tool_text = AsyncMock(
                return_value='{"decisions": [], "source": "remote"}'
            )
        return ToolRuntime(
            surface="local_hybrid",
            capability_profile=CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES),
            premium_client=client,
        )

    def test_default_listing_stays_local(self):
        import json as _json
        from mcp.types import TextContent
        from fastmcp.tools.tool import ToolResult
        from watercooler_mcp.tools.decisions import _build_hybrid_list_decisions_wrapper

        wrapper = _build_hybrid_list_decisions_wrapper(self._runtime())
        with patch(
            "watercooler_mcp.tools.decisions._list_decisions_impl",
            return_value=ToolResult([
                TextContent(type="text", text='{"source": "local"}')
            ]),
        ) as local_impl:
            result = asyncio.run(wrapper(MagicMock(), include_supersession=False))

        local_impl.assert_called_once()
        assert _json.loads(result.content[0].text) == {"source": "local"}

    def test_supersession_listing_routes_remote(self):
        import json as _json
        from watercooler_mcp.tools.decisions import _build_hybrid_list_decisions_wrapper

        rt = self._runtime()
        wrapper = _build_hybrid_list_decisions_wrapper(rt)
        result = asyncio.run(wrapper(MagicMock(), include_supersession=True))

        rt.premium_client.call_tool_text.assert_awaited_once_with(
            "watercooler_list_decisions", {"include_supersession": True}
        )
        assert _json.loads(result.content[0].text) == {
            "decisions": [],
            "source": "remote",
        }

    def test_supersession_without_remote_client_reports_disabled(self):
        import json as _json
        from watercooler_mcp.tools.decisions import _build_hybrid_list_decisions_wrapper

        wrapper = _build_hybrid_list_decisions_wrapper(
            self._runtime(premium_client=False)
        )
        result = asyncio.run(wrapper(MagicMock(), include_supersession=True))
        data = _json.loads(result.content[0].text)
        assert data["error"] == "capability_disabled"
        assert data["capability"] == "memory_query"


# ---------------------------------------------------------------------------
# build_mcp_server
# ---------------------------------------------------------------------------


class TestBuildMcpServer:
    def test_local_full_has_all_core_tools(self):
        rt = ToolRuntime(surface="local_full")
        mcp = build_mcp_server(rt)
        names = _tool_names(mcp)
        # Should have thread tools
        assert "watercooler_list_threads" in names
        assert "watercooler_say" in names
        # Should have graph tools
        assert "watercooler_search" in names
        assert "watercooler_access_stats" in names
        # Should have memory tools
        assert "watercooler_smart_query" in names
        # Should have diagnostics
        assert "watercooler_health" in names

    def test_hosted_premium_has_premium_graph_and_memory(self):
        rt = ToolRuntime(surface="hosted_premium")
        mcp = build_mcp_server(rt)
        names = _tool_names(mcp)
        # Premium graph tools
        assert "watercooler_search" in names
        # Memory tools
        assert "watercooler_smart_query" in names
        # Should NOT have thread tools
        assert "watercooler_list_threads" not in names
        assert "watercooler_say" not in names
        assert "watercooler_write" not in names

    def test_watercooler_write_present_on_thread_capable_surfaces(self):
        for surface in ("local_full", "hosted_full"):
            rt = ToolRuntime(surface=surface)
            mcp = build_mcp_server(rt)
            names = _tool_names(mcp)
            assert "watercooler_write" in names, f"watercooler_write missing from {surface}"

    def test_watercooler_write_absent_from_hosted_premium(self):
        rt = ToolRuntime(surface="hosted_premium")
        mcp = build_mcp_server(rt)
        names = _tool_names(mcp)
        assert "watercooler_write" not in names

    def test_local_hybrid_does_not_locally_register_remote_only_tools(self):
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        mock_client = MagicMock()
        mock_proxy = MagicMock()
        mock_client.proxy_server.return_value = mock_proxy
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=profile,
            premium_client=mock_client,
        )
        mcp = build_mcp_server(rt)
        names = _tool_names(mcp)
        # Thread tools should be present (local)
        assert "watercooler_list_threads" in names
        # Mixed tools should be present (locally registered with wrappers)
        assert "watercooler_search" in names
        assert "watercooler_bulk_index" in names
        assert "watercooler_list_decisions" in names
        # Disabled tools should NOT be present
        for tool in HYBRID_DISABLED_TOOL_NAMES:
            assert tool not in names, f"{tool} should be disabled in hybrid"


class TestPremiumContainment:
    """B7: premium tools must be a subset of full tools."""

    def test_premium_tools_subset_of_full(self):
        full_rt = ToolRuntime(surface="hosted_full")
        premium_rt = ToolRuntime(surface="hosted_premium")
        full_mcp = build_mcp_server(full_rt)
        premium_mcp = build_mcp_server(premium_rt)
        full_names = _tool_names(full_mcp)
        premium_names = _tool_names(premium_mcp)
        assert premium_names, "Premium surface should have at least one tool"
        assert premium_names.issubset(
            full_names
        ), f"Premium tools not in full: {premium_names - full_names}"


class TestReindexRetired:
    """PR4b — watercooler_reindex retired (superseded by the graph-first
    watercooler_list_threads); it must not be registered on any surface."""

    @pytest.mark.parametrize(
        "surface", ["local_full", "hosted_full", "hosted_premium"]
    )
    def test_reindex_not_registered(self, surface):
        mcp = build_mcp_server(ToolRuntime(surface=surface))
        assert "watercooler_reindex" not in _tool_names(mcp)

    def test_reindex_not_registered_local_hybrid(self):
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        mock_client = MagicMock()
        mock_client.proxy_server.return_value = MagicMock()
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=profile,
            premium_client=mock_client,
        )
        mcp = build_mcp_server(rt)
        assert "watercooler_reindex" not in _tool_names(mcp)


class TestBuildDefaultLocalServer:
    def test_returns_fastmcp(self):
        mcp = build_default_local_server()
        assert isinstance(mcp, FastMCP)

    def test_has_core_tools(self):
        mcp = build_default_local_server()
        names = _tool_names(mcp)
        assert "watercooler_search" in names
        assert "watercooler_say" in names
        assert "watercooler_health" in names
