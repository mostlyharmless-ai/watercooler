"""Tests for the shared server factory (Step 4)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

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
        migration = {
            "watercooler_migration_preflight",
            "watercooler_migrate_to_memory_backend",
        }
        assert result == REMOTE_CAPABLE_MEMORY_TOOL_NAMES - migration

    def test_local_hybrid_excludes_remote_mount_and_disabled(self):
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(surface="local_hybrid", capability_profile=profile)
        result = memory_tools_for_surface(rt)
        # Remote-mount tools should NOT be in the local registration
        assert not result & HYBRID_REMOTE_MOUNT_TOOLS
        # Disabled tools should NOT be in the local registration
        assert not result & HYBRID_DISABLED_TOOL_NAMES


class TestMigrationToolsForSurface:
    def test_local_full_gets_all(self):
        rt = ToolRuntime(surface="local_full")
        assert migration_tools_for_surface(rt) == {
            "watercooler_migration_preflight",
            "watercooler_migrate_to_memory_backend",
        }

    def test_local_hybrid_default_disabled(self):
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(surface="local_hybrid", capability_profile=profile)
        assert migration_tools_for_surface(rt) == set()


class TestMountableRemoteTools:
    def test_hybrid_with_premium_client(self):
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=profile,
            premium_client=MagicMock(),
        )
        result = mountable_remote_tools_for_hybrid(rt)
        assert result == set(HYBRID_REMOTE_MOUNT_TOOLS)

    def test_non_hybrid_returns_empty(self):
        rt = ToolRuntime(surface="local_full")
        assert mountable_remote_tools_for_hybrid(rt) == set()

    def test_mountable_remote_tools_for_hybrid_includes_acknowledge_finding(self):
        profile = CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES)
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=profile,
            premium_client=MagicMock(),
        )
        result = mountable_remote_tools_for_hybrid(rt)
        assert "watercooler_acknowledge_finding" in result


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
        assert "watercooler_find_similar" in names
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
        assert "watercooler_find_similar" in names
        # Memory tools
        assert "watercooler_smart_query" in names
        # Should NOT have thread tools
        assert "watercooler_list_threads" not in names
        assert "watercooler_say" not in names

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
        assert "watercooler_find_similar" in names
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


class TestReindexHostedAbsence:
    """Sync tools (watercooler_reindex) must not appear on hosted surfaces."""

    def test_hosted_full_no_reindex(self):
        rt = ToolRuntime(surface="hosted_full")
        mcp = build_mcp_server(rt)
        names = _tool_names(mcp)
        assert "watercooler_reindex" not in names

    def test_hosted_premium_no_reindex(self):
        rt = ToolRuntime(surface="hosted_premium")
        mcp = build_mcp_server(rt)
        names = _tool_names(mcp)
        assert "watercooler_reindex" not in names

    def test_local_full_has_reindex(self):
        rt = ToolRuntime(surface="local_full")
        mcp = build_mcp_server(rt)
        names = _tool_names(mcp)
        assert "watercooler_reindex" in names

    def test_local_hybrid_has_reindex(self):
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
        assert "watercooler_reindex" in names


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
