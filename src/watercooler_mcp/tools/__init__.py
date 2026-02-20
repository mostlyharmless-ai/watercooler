"""MCP Tools package for watercooler server.

This package contains tool implementations organized by category:
- diagnostic: health, whoami
- thread_query: list_threads, read_thread, entry tools
- thread_write: say, ack, handoff, set_status
- sync: reindex
- graph: baseline graph tools (stats, build, search, etc.)
- memory: Graphiti memory tools
- migration: memory backend migration, orphan branch migration
- federation: cross-namespace federated search

Each module provides a register_*_tools(mcp) function to register its tools.
"""

from .diagnostic import register_diagnostic_tools
from .thread_query import register_thread_query_tools
from .thread_write import register_thread_write_tools
from .sync import register_sync_tools
from .graph import register_graph_tools
from .memory import register_memory_tools
from .migration import register_migration_tools
from .federation import register_federation_tools


def register_all_tools(mcp):
    """Register all MCP tools with the server.

    Args:
        mcp: The FastMCP server instance
    """
    register_diagnostic_tools(mcp)
    register_thread_query_tools(mcp)
    register_thread_write_tools(mcp)
    register_sync_tools(mcp)
    register_graph_tools(mcp)
    register_memory_tools(mcp)
    register_migration_tools(mcp)
    register_federation_tools(mcp)


__all__ = [
    "register_all_tools",
    "register_diagnostic_tools",
    "register_thread_query_tools",
    "register_thread_write_tools",
    "register_sync_tools",
    "register_graph_tools",
    "register_memory_tools",
    "register_migration_tools",
    "register_federation_tools",
]
