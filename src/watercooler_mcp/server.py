# SPDX-License-Identifier: Apache-2.0
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
from pathlib import Path
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
    # Startup warnings
    _add_startup_warning,
    _get_startup_warnings,
    _format_warnings_for_response,
    # Context helpers
    _should_auto_branch,
    # Thread parsing
    _normalize_status,
    _resolve_format,
    # Entry loading
    _entry_header_payload,
    _entry_full_payload,
    # Graph helpers
    _use_graph_for_reads,
    _track_access,
    _graph_entry_to_thread_entry,
    _load_entries,
    _list_threads,
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
from .tools.memory import register_memory_tools
# Migration tools removed due to MCP SDK 60-second timeout limitation.
# Use scripts/index_graphiti.py for thread migration instead.
# See: https://github.com/modelcontextprotocol/typescript-sdk/issues/245
from .tools.federation import register_federation_tools
from .tools.roles import register_role_tools
# Re-export tools for test compatibility
from .tools import diagnostic as _diagnostic_tools
from .tools import thread_query as _thread_query_tools
from .tools import thread_write as _thread_write_tools
from .tools import sync as _sync_tools
from .tools import graph as _graph_tools
from .tools import memory as _memory_tools
from .tools import daemon as _daemon_tools
from .tools import federation as _federation_tools
from .tools import roles as _roles_tools


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

# Build the default local server via the shared factory.
# This replaces manual module-level assembly with a single factory call.
from .server_factory import build_default_local_server
mcp = build_default_local_server()

# Instrument FastMCP tool execution for observability
setup_instrumentation()

# Initialize memory sync callbacks (Issue #83 - callback registry pattern)
from .memory_sync import init_memory_sync_callbacks
init_memory_sync_callbacks()

# Initialize persistent memory task queue (recovery + retry for fire-and-forget tasks)
try:
    from .memory_queue import init_memory_queue
    from watercooler.memory_config import get_queue_max_workers, get_queue_task_timeout
    init_memory_queue(
        max_workers=get_queue_max_workers(),
        task_timeout=get_queue_task_timeout(),
    )
    # Register backend executors with the queue worker
    from .memory_sync import init_memory_queue_executors
    init_memory_queue_executors()

    # Inform operator when T2 features are configured but unavailable
    from .memory import _graphiti_importable
    if not _graphiti_importable():
        import logging as _startup_logging
        _startup_logging.getLogger(__name__).info(
            "Memory queue started but watercooler_memory not installed — "
            "T2 (Graphiti) features unavailable. T1 operations unaffected."
        )
except Exception as _mq_err:
    import logging as _mq_logging
    _mq_logging.getLogger(__name__).warning(
        "Could not initialise memory task queue: %s", _mq_err,
    )

# Initialize daemon management system (periodic thread scanning, hygiene)
try:
    from .daemons import init_daemons
    init_daemons()
except Exception as _dm_err:
    import logging as _dm_logging
    _dm_logging.getLogger(__name__).warning(
        "Could not initialise daemon manager: %s", _dm_err,
    )

# Re-export registered tools for test compatibility (must be after registration)
health = _diagnostic_tools.health
list_threads = _thread_query_tools.list_threads
read_thread = _thread_query_tools.read_thread
list_thread_entries = _thread_query_tools.list_thread_entries
get_thread_entry = _thread_query_tools.get_thread_entry
say = _thread_write_tools.say
ack = _thread_write_tools.ack
handoff = _thread_write_tools.handoff
set_status = _thread_write_tools.set_status
baseline_graph_tool = _graph_tools.baseline_graph_tool
search_graph_tool = _graph_tools.search_graph_tool
access_stats_tool = _graph_tools.access_stats_tool
# New graph tooling suite
graph_enrich_tool = _graph_tools.graph_enrich_tool
graph_project_tool = _graph_tools.graph_project_tool
# Memory tools (some tools removed - see replacement mappings in tools/memory.py)
graph_trace = _memory_tools.graph_trace
diagnose_memory = _memory_tools.diagnose_memory
# Daemon tools
daemon_status_tool = _daemon_tools.daemon_status
daemon_findings_tool = _daemon_tools.daemon_findings
# Federation tools (registered via register_federation_tools)


# ============================================================================
# Server Entry Point
# ============================================================================


def _run_proxy(transport_config: dict, *, boot_cwd: Path | None = None) -> None:
    """Run the MCP server in proxy mode.

    Forwards all tool calls to a remote hosted MCP endpoint via FastMCP's
    built-in proxy. No local services (llama-server, FalkorDB) are started.

    Args:
        transport_config: Dict with 'url' key for the remote endpoint.
    """
    url = transport_config.get("url", "")
    if not url:
        print(
            "Error: proxy transport requires a remote URL.\n"
            "Set [mcp].url in config.toml or WATERCOOLER_MCP_URL env var.",
            file=sys.stderr,
        )
        sys.exit(1)

    api_key = config.get_hosted_api_key()
    if not api_key:
        print(
            "Error: proxy transport requires an API key.\n"
            "Set [hosted].api_key in ~/.watercooler/credentials.toml.",
            file=sys.stderr,
        )
        sys.exit(1)

    from fastmcp.client import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server import create_proxy
    from .premium_client import build_premium_headers

    # Resolve repo/branch for X-Repo / X-Branch headers.
    # The hosted endpoint needs these to scope thread operations.
    # Priority: config/env (proxy_repo, proxy_branch) > local git context.
    try:
        headers = build_premium_headers(transport_config, boot_cwd=boot_cwd)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    header_info = ", ".join(f"{k}={v}" for k, v in headers.items()) or "no context"
    print(f"Starting Watercooler MCP Proxy → {url} ({header_info})", file=sys.stderr)

    transport = StreamableHttpTransport(url, headers=headers, auth=api_key)
    client = Client(transport)
    proxy = create_proxy(client, name="Watercooler Cloud (Proxy)")
    proxy.run()


def _run_hybrid(transport_config: dict, *, boot_cwd: Path | None = None) -> None:
    """Run the MCP server in hybrid mode.

    Local threads + baseline graph tools execute locally.
    Premium capabilities (memory, T2/T3) are mounted from the remote
    hosted MCP endpoint on Railway.

    Local services started:
    - LLM (llama-server): for baseline graph enrichment and summaries
    - Embedding (llama-server): for baseline semantic search

    FalkorDB is NOT started locally — it is a T2/T3 dependency hosted
    on Railway. Hybrid local search always uses the baseline JSON graph.

    Args:
        transport_config: Dict with 'url', 'capability_routes', etc.
    """
    from .capabilities import HYBRID_DEFAULT_ROUTES, CapabilityProfile, validate_capability_routes
    from .tool_runtime import ToolRuntime
    from .premium_client import PremiumToolClient
    from .server_factory import build_mcp_server

    # Build capability profile from defaults + user overrides
    routes = dict(HYBRID_DEFAULT_ROUTES)
    raw_overrides = transport_config.get("capability_routes", {})
    if raw_overrides:
        validated = validate_capability_routes(raw_overrides)
        routes.update(validated)
    profile = CapabilityProfile(routes=routes)

    # Build premium client
    try:
        premium_client = PremiumToolClient.from_transport_config(
            transport_config,
            boot_cwd=boot_cwd,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Build runtime and server
    runtime = ToolRuntime(
        surface="local_hybrid",
        capability_profile=profile,
        premium_client=premium_client,
    )

    # Start local T1 services only.
    # LLM: baseline graph enrichment (summaries, graph_enrich).
    # Embedding: baseline semantic search.
    # FalkorDB is NOT started — T2/T3 is hosted on Railway. Mark its
    # service status explicitly so the health surface reports
    # "disabled" instead of the initial "unknown" state (defect #29).
    check_first_run()
    ensure_llm_running()
    ensure_embedding_running()
    from .startup import _update_service_status, ServiceState
    _update_service_status(
        "falkordb", ServiceState.DISABLED,
        message="Hosted FalkorDB owns T1/T2 (transport=hybrid)",
    )

    # Build hybrid server and start daemons
    hybrid_mcp = build_mcp_server(runtime)

    # Initialize daemons for local daemon observability
    try:
        from .daemons import init_daemons
        init_daemons()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Could not initialise daemon manager: %s", e)

    url = transport_config.get("url", "")
    print(f"Starting Watercooler MCP Hybrid → {url}", file=sys.stderr)
    hybrid_mcp.run()


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

    boot_cwd = Path.cwd()

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
  WATERCOOLER_MCP_TRANSPORT    Transport type (stdio, http, proxy, or hybrid)
  WATERCOOLER_MCP_URL          Remote endpoint URL (proxy/hybrid transport)
  WATERCOOLER_MCP_HOST         HTTP host (default: 127.0.0.1)
  WATERCOOLER_MCP_PORT         HTTP port (default: 3000)
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

    # Get transport configuration from unified config system.
    #
    # Naming caveat: the `transport` key here is an execution-routing mode,
    # NOT the MCP agent↔mcp stdio pipe (which is always stdio). Values:
    #   stdio  — run every tool call in-process (fall-through below)
    #   proxy  — forward every tool call to a remote hosted MCP endpoint
    #   hybrid — local threads + baseline + local daemons; premium calls proxied
    #   http   — this process itself serves HTTP (hosted Railway deployment)
    # See docs/MCP-CLIENTS.md for the full table and naming-overlap caveat.
    from .config import get_mcp_transport_config

    transport_config = get_mcp_transport_config()
    transport = transport_config["transport"]

    # Proxy mode: forward all tool calls to a remote hosted MCP endpoint.
    # No local services start — the proxy handles everything.
    if transport == "proxy":
        _run_proxy(transport_config, boot_cwd=boot_cwd)
        return

    # Hybrid mode: local threads + remote premium capabilities.
    if transport == "hybrid":
        _run_hybrid(transport_config, boot_cwd=boot_cwd)
        return

    # Check for first-run and suggest config initialization
    check_first_run()

    # Auto-start llama-server for LLM if graph features are enabled
    ensure_llm_running()

    # Auto-start llama-server for embeddings if needed
    ensure_embedding_running()

    # Auto-start FalkorDB if Graphiti backend is enabled
    ensure_falkordb_running()

    if transport == "http":
        from .auth import is_hosted_mode
        if is_hosted_mode():
            # Hosted HTTP: delegate to server_http for dual-surface app.
            from .server_http import run_http_server
            host = transport_config["host"]
            port = transport_config["port"]
            run_http_server(host=host, port=port)
        else:
            # Local HTTP: single local_full surface.
            host = transport_config["host"]
            port = transport_config["port"]

            print(f"Starting Watercooler MCP Server on http://{host}:{port}", file=sys.stderr)
            print(f"Health check: http://{host}:{port}/health", file=sys.stderr)

            mcp.run(
                transport="http",
                host=host,
                port=port,
                stateless_http=True,
                json_response=True,
            )
    else:
        # stdio transport (default)
        mcp.run()


if __name__ == "__main__":
    main()
