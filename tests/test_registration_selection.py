"""Tests for selective tool registration (Step 3)."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from watercooler_mcp.tools.graph import (
    TOOL_BUILDERS as GRAPH_BUILDERS,
    register_graph_tools,
)
from watercooler_mcp.tools.memory import (
    TOOL_BUILDERS as MEMORY_BUILDERS,
    register_memory_tools,
)
from watercooler_mcp.tools.migration import (
    TOOL_BUILDERS as MIGRATION_BUILDERS,
    register_migration_tools,
)
from watercooler_mcp.tools import graph as graph_mod
from watercooler_mcp.tools import memory as memory_mod


def _tool_names(mcp: FastMCP) -> set[str]:
    """Extract registered tool names from a FastMCP server."""
    tools = asyncio.run(mcp.list_tools())
    return {t.name for t in tools}


# ---------------------------------------------------------------------------
# Graph tools
# ---------------------------------------------------------------------------


class TestGraphRegistration:
    def test_register_all_when_selected_is_none(self):
        mcp = FastMCP(name="test")
        register_graph_tools(mcp)
        names = _tool_names(mcp)
        for tool_name in GRAPH_BUILDERS:
            assert tool_name in names, f"Expected {tool_name} to be registered"

    def test_register_selected_subset(self):
        mcp = FastMCP(name="test")
        selected = {"watercooler_search", "watercooler_access_stats"}
        register_graph_tools(mcp, selected=selected)
        names = _tool_names(mcp)
        assert "watercooler_search" in names
        assert "watercooler_access_stats" in names
        # Should NOT have registered others
        assert "watercooler_baseline_graph" not in names
        assert "watercooler_graph_enrich" not in names

    def test_module_globals_populated(self):
        mcp = FastMCP(name="test")
        register_graph_tools(mcp, selected={"watercooler_search"})
        assert graph_mod.search_graph_tool is not None

    def test_empty_selected_registers_nothing(self):
        mcp = FastMCP(name="test")
        register_graph_tools(mcp, selected=set())
        names = _tool_names(mcp)
        for tool_name in GRAPH_BUILDERS:
            assert tool_name not in names


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


class TestMemoryRegistration:
    def test_register_all_when_selected_is_none(self):
        mcp = FastMCP(name="test")
        register_memory_tools(mcp)
        names = _tool_names(mcp)
        for tool_name in MEMORY_BUILDERS:
            assert tool_name in names, f"Expected {tool_name} to be registered"

    def test_register_selected_subset(self):
        mcp = FastMCP(name="test")
        selected = {"watercooler_smart_query", "watercooler_graph_trace"}
        register_memory_tools(mcp, selected=selected)
        names = _tool_names(mcp)
        assert "watercooler_smart_query" in names
        assert "watercooler_graph_trace" in names
        assert "watercooler_bulk_index" not in names

    def test_module_globals_populated(self):
        mcp = FastMCP(name="test")
        register_memory_tools(mcp, selected={"watercooler_smart_query"})
        assert memory_mod.smart_query is not None


# ---------------------------------------------------------------------------
# Migration tools
# ---------------------------------------------------------------------------


class TestMigrationRegistration:
    def test_no_migration_tools_registered(self):
        """PR4a retired both migration tools — migration_preflight folded into
        watercooler_bulk_index(preflight_only=True), migrate_to_memory_backend
        superseded by watercooler_bulk_index — so the module registers nothing."""
        mcp = FastMCP(name="test")
        register_migration_tools(mcp)
        assert _tool_names(mcp) == set()
        assert MIGRATION_BUILDERS == {}


# ---------------------------------------------------------------------------
# TOOL_BUILDERS consistency
# ---------------------------------------------------------------------------


class TestToolBuildersConsistency:
    def test_graph_builders_keys_match_capabilities(self):
        from watercooler_mcp.capabilities import GRAPH_TOOL_NAMES
        assert set(GRAPH_BUILDERS.keys()) == GRAPH_TOOL_NAMES

    def test_memory_builders_cover_memory_tools(self):
        """Memory builders should contain the 9 memory tools (excluding migration)."""
        from watercooler_mcp.capabilities import (
            REMOTE_CAPABLE_MEMORY_TOOL_NAMES,
            HYBRID_DISABLED_TOOL_NAMES,
        )
        # Memory builders = remote-capable minus migration tools
        migration_tools = {"watercooler_migration_preflight", "watercooler_migrate_to_memory_backend"}
        expected = REMOTE_CAPABLE_MEMORY_TOOL_NAMES - migration_tools
        assert set(MEMORY_BUILDERS.keys()) == expected

    def test_migration_builders_is_empty(self):
        """PR4a folded/retired both migration tools — migration.py registers none."""
        assert MIGRATION_BUILDERS == {}
