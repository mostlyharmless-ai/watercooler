"""Watercooler MCP Server - Phase 1A MVP

FastMCP server exposing watercooler-cloud tools to AI agents.
All tools are namespaced as watercooler_* for provider compatibility.

Phase 1A features:
- 7 core tools + 2 diagnostic tools
- Markdown-only output (format param accepted but unused)
- Simple env-based config (WATERCOOLER_AGENT, WATERCOOLER_DIR)
- Basic error handling with helpful messages
"""

import sys
if sys.version_info < (3, 10):
    raise RuntimeError(
        f"Watercooler MCP requires Python 3.10+; found {sys.version.split()[0]}"
    )

# Third-party imports
from fastmcp import FastMCP

# Local application imports
from watercooler.config_facade import config
from .config import ThreadContext
from .startup import check_first_run, ensure_ollama_running, ensure_embedding_running

# Import validation functions (extracted to break circular imports)
from .validation import (
    _require_context,
    _dynamic_context_missing,
    _refresh_threads,
    _validate_thread_context,
)

# Import helpers (extracted for modularity)
from .helpers import (
    # Constants
    _ALLOWED_FORMATS,
    _MAX_LIMIT,
    _MAX_OFFSET,
    _CLOSED_STATES,
    # Startup warnings
    _add_startup_warning,
    _get_startup_warnings,
    _format_warnings_for_response,
    # Context helpers
    _should_auto_branch,
    # Branch helpers
    _attempt_auto_fix_divergence,
    _validate_and_sync_branches,
    # Thread parsing
    _normalize_status,
    _resolve_format,
    # Entry loading
    _load_thread_entries,
    _entry_header_payload,
    _entry_full_payload,
    # Graph helpers
    _use_graph_for_reads,
    _track_access,
    _graph_entry_to_thread_entry,
    _load_thread_entries_graph_first,
    _list_threads_graph_first,
    # Commit helpers
    _build_commit_footers,
)

# Import middleware (extracted for modularity)
from .middleware import (
    setup_instrumentation,
    run_with_sync,
    run_with_graph_sync,
)

# Import resources (extracted for modularity)
from .resources import register_resources

# Import tools (extracted for modularity)
from .tools.diagnostic import register_diagnostic_tools
from .tools.thread_query import register_thread_query_tools
from .tools.thread_write import register_thread_write_tools
from .tools.sync import register_sync_tools
from .tools.graph import register_graph_tools
from .tools.branch_parity import register_branch_parity_tools
from .tools.memory import register_memory_tools
# Migration tools removed due to MCP SDK 60-second timeout limitation.
# Use scripts/index_graphiti.py for thread migration instead.
# See: https://github.com/modelcontextprotocol/typescript-sdk/issues/245
# Re-export tools for test compatibility
from .tools import diagnostic as _diagnostic_tools
from .tools import thread_query as _thread_query_tools
from .tools import thread_write as _thread_write_tools
from .tools import sync as _sync_tools
from .tools import graph as _graph_tools
from .tools import branch_parity as _branch_parity_tools
from .tools import memory as _memory_tools


# Workaround for Windows stdio hang: Force auto-flush on every stdout write
# On Windows, FastMCP's stdio transport gets stuck after subprocess operations
# Auto-flushing after every write prevents response from getting stuck in buffer
if sys.platform == "win32":
    import io

    class AutoFlushWrapper(io.TextIOWrapper):
        def write(self, s):
            result = super().write(s)
            self.flush()
            return result

    # Wrap stdout with auto-flush
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = AutoFlushWrapper(
            sys.stdout.buffer,
            encoding=sys.stdout.encoding,
            errors=sys.stdout.errors,
            newline=None,
            line_buffering=False,
            write_through=True
        )

# Initialize FastMCP server with configurable transport
# WATERCOOLER_MCP_TRANSPORT: "http" or "stdio" (default: "stdio" for backward compatibility)
_TRANSPORT = config.env.get("WATERCOOLER_MCP_TRANSPORT", "stdio").lower()

# For HTTP transport, use stateless mode with JSON responses (no SSE/sessions)
# This enables simple JSON-RPC calls from clients like mcpClient.ts
if _TRANSPORT == "http":
    mcp = FastMCP(
        name="Watercooler Cloud",
        stateless_http=True,
        json_response=True,
    )
else:
    mcp = FastMCP(name="Watercooler Cloud")


# Instrument FastMCP tool execution for observability
setup_instrumentation()

# Register MCP resources and tools
register_resources(mcp)
register_diagnostic_tools(mcp)
register_thread_query_tools(mcp)
register_thread_write_tools(mcp)
register_sync_tools(mcp)
register_graph_tools(mcp)
register_branch_parity_tools(mcp)
register_memory_tools(mcp)
# register_migration_tools removed - use scripts/index_graphiti.py instead

# Initialize memory sync callbacks (Issue #83 - callback registry pattern)
from .memory_sync import init_memory_sync_callbacks
init_memory_sync_callbacks()

# Re-export registered tools for test compatibility (must be after registration)
health = _diagnostic_tools.health
list_threads = _thread_query_tools.list_threads
read_thread = _thread_query_tools.read_thread
list_thread_entries = _thread_query_tools.list_thread_entries
get_thread_entry = _thread_query_tools.get_thread_entry
get_thread_entry_range = _thread_query_tools.get_thread_entry_range
say = _thread_write_tools.say
ack = _thread_write_tools.ack
handoff = _thread_write_tools.handoff
set_status = _thread_write_tools.set_status
force_sync = _sync_tools.force_sync
reindex = _sync_tools.reindex
baseline_graph_stats = _graph_tools.baseline_graph_stats
baseline_graph_build = _graph_tools.baseline_graph_build
search_graph_tool = _graph_tools.search_graph_tool
find_similar_entries_tool = _graph_tools.find_similar_entries_tool
graph_health_tool = _graph_tools.graph_health_tool
reconcile_graph_tool = _graph_tools.reconcile_graph_tool
access_stats_tool = _graph_tools.access_stats_tool
validate_branch_pairing_tool = _branch_parity_tools.validate_branch_pairing_tool
sync_branch_state = _branch_parity_tools.sync_branch_state_tool
audit_branch_pairing = _branch_parity_tools.audit_branch_pairing_tool
recover_branch_state = _branch_parity_tools.recover_branch_state_tool
# Memory tools (some tools removed - see replacement mappings in tools/memory.py)
get_entity_edge = _memory_tools.get_entity_edge
diagnose_memory = _memory_tools.diagnose_memory


# ============================================================================
# Server Entry Point
# ============================================================================


def main():
    """Entry point for watercooler-mcp command."""
    # Check for first-run and suggest config initialization
    check_first_run()

    # Auto-start Ollama if graph features are enabled
    ensure_ollama_running()

    # Auto-start embedding service if needed (provides guidance for llama.cpp)
    ensure_embedding_running()

    # Get transport configuration from unified config system
    from .config import get_mcp_transport_config

    transport_config = get_mcp_transport_config()
    transport = transport_config["transport"]

    if transport == "http":
        host = transport_config["host"]
        port = transport_config["port"]

        print(f"Starting Watercooler MCP Server on http://{host}:{port}", file=sys.stderr)
        print(f"Health check: http://{host}:{port}/health", file=sys.stderr)

        mcp.run(transport="http", host=host, port=port)
    else:
        # stdio transport (default)
        mcp.run()


if __name__ == "__main__":
    main()
