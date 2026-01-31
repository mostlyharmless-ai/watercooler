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
from .startup import check_first_run, ensure_llm_running, ensure_embedding_running, ensure_falkordb_running

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
search_graph_tool = _graph_tools.search_graph_tool
find_similar_entries_tool = _graph_tools.find_similar_entries_tool
graph_health_tool = _graph_tools.graph_health_tool
access_stats_tool = _graph_tools.access_stats_tool
# New graph tooling suite
graph_enrich_tool = _graph_tools.graph_enrich_tool
graph_recover_tool = _graph_tools.graph_recover_tool
graph_project_tool = _graph_tools.graph_project_tool
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


def _reset_cache() -> None:
    """Clear watercooler caches (binaries and models).

    Clears:
    - ~/.watercooler/bin/ (llama-server and shared libraries)
    - ~/.watercooler/models/ (downloaded GGUF models)

    Also prints instructions for clearing uvx caches if needed.
    """
    import shutil
    from pathlib import Path

    watercooler_dir = Path.home() / ".watercooler"
    cleared = []

    # Clear binaries (llama-server, .so files)
    bin_dir = watercooler_dir / "bin"
    if bin_dir.exists():
        shutil.rmtree(bin_dir)
        cleared.append(f"  - {bin_dir}")

    # Clear downloaded models
    models_dir = watercooler_dir / "models"
    if models_dir.exists():
        shutil.rmtree(models_dir)
        cleared.append(f"  - {models_dir}")

    if cleared:
        print("Cleared watercooler caches:", file=sys.stderr)
        for path in cleared:
            print(path, file=sys.stderr)
    else:
        print("No watercooler caches to clear.", file=sys.stderr)

    # Print uvx cache instructions
    print("\nTo fully reset (including uvx package cache), also run:", file=sys.stderr)
    print("  rm -rf ~/.cache/uv/archive-v0/*watercooler* ~/.cache/uv/git-v0/checkouts/*/watercooler*", file=sys.stderr)
    print("\nOr for a complete uvx reset:", file=sys.stderr)
    print("  uv cache clean", file=sys.stderr)


def _warm_cache() -> None:
    """Pre-download llama-server binary and configured models.

    Downloads:
    - llama-server binary from GitHub releases (if not present)
    - LLM model GGUF file (if configured for local inference)
    - Embedding model GGUF file (if configured for local inference)

    This allows pre-warming the cache before starting the MCP server,
    avoiding download delays during first connection.
    """
    from .startup import (
        _find_llama_server,
        _download_llama_server,
        _is_localhost_url,
    )
    from watercooler.memory_config import (
        resolve_baseline_graph_llm_config,
        resolve_baseline_graph_embedding_config,
    )
    from watercooler.models import ensure_llm_model_available, ensure_model_available

    print("Warming watercooler cache...", file=sys.stderr)

    # 1. Download llama-server binary
    llama_server = _find_llama_server()
    if llama_server:
        print(f"  llama-server: {llama_server} (already installed)", file=sys.stderr)
    else:
        print("  llama-server: downloading from GitHub releases...", file=sys.stderr)
        llama_server = _download_llama_server()
        if llama_server:
            print(f"  llama-server: {llama_server} (downloaded)", file=sys.stderr)
        else:
            print("  llama-server: FAILED to download", file=sys.stderr)

    # 2. Download LLM model if configured for localhost
    try:
        llm_config = resolve_baseline_graph_llm_config()
        if _is_localhost_url(llm_config.api_base):
            print(f"  LLM model ({llm_config.model}): checking...", file=sys.stderr)
            model_path = ensure_llm_model_available(llm_config.model)
            if model_path:
                print(f"  LLM model: {model_path}", file=sys.stderr)
            else:
                print(f"  LLM model: not found in registry", file=sys.stderr)
        else:
            print(f"  LLM model: skipped (remote API: {llm_config.api_base})", file=sys.stderr)
    except Exception as e:
        print(f"  LLM model: error - {e}", file=sys.stderr)

    # 3. Download embedding model if configured for localhost
    try:
        emb_config = resolve_baseline_graph_embedding_config()
        if _is_localhost_url(emb_config.api_base):
            print(f"  Embedding model ({emb_config.model}): checking...", file=sys.stderr)
            model_path = ensure_model_available(emb_config.model)
            if model_path:
                print(f"  Embedding model: {model_path}", file=sys.stderr)
            else:
                print(f"  Embedding model: not found in registry", file=sys.stderr)
        else:
            print(f"  Embedding model: skipped (remote API: {emb_config.api_base})", file=sys.stderr)
    except Exception as e:
        print(f"  Embedding model: error - {e}", file=sys.stderr)

    print("\nCache warm complete. Ready to start server.", file=sys.stderr)


def main():
    """Entry point for watercooler-mcp command."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Watercooler MCP Server - AI agent collaboration tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  watercooler-mcp              Start MCP server (stdio transport)
  watercooler-mcp --warm       Pre-download binaries and models, then exit
  watercooler-mcp --reset-cache  Clear downloaded binaries and models

Environment variables:
  WATERCOOLER_DIR              Path to threads directory
  WATERCOOLER_AGENT            Default agent identity
  WATERCOOLER_TRANSPORT        Transport type (stdio or http)
  WATERCOOLER_HOST             HTTP host (default: 127.0.0.1)
  WATERCOOLER_PORT             HTTP port (default: 8765)
"""
    )
    parser.add_argument(
        "--reset-cache",
        action="store_true",
        help="Clear watercooler caches (binaries, models) and exit"
    )
    parser.add_argument(
        "--warm",
        action="store_true",
        help="Pre-download llama-server and models, then exit (use for cache warming)"
    )
    args = parser.parse_args()

    if args.reset_cache:
        _reset_cache()
        sys.exit(0)

    if args.warm:
        _warm_cache()
        sys.exit(0)

    # Check for first-run and suggest config initialization
    check_first_run()

    # Auto-start llama-server for LLM if graph features are enabled
    ensure_llm_running()

    # Auto-start llama-server for embeddings if needed
    ensure_embedding_running()

    # Auto-start FalkorDB if Graphiti backend is enabled
    ensure_falkordb_running()

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
