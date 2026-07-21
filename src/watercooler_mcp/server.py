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

# ---------------------------------------------------------------------------
# Transport-gated module initialization (modality-robustness plan Phase 2,
# audit-transport-modes-hosted-db-2026-07:68, Fork B / gap G2).
#
# Under an EFFECTIVE proxy — configured proxy WITH credentials — this process
# is a thin forwarder: building the local tool surface, starting the memory
# task queue's worker threads, and initializing the local daemon manager are
# pure waste, and an enabled daemon set would tick against the local worktree
# while user traffic goes hosted (the split-brain documented at audit :61).
#
# The gate is the EFFECTIVE transport, not the configured string: a
# credential-less proxy install resolves to stdio (#1128 fallback) and MUST
# keep the full local stack. Resolution fails open to "stdio" so a config or
# credential read error can never break a local import. Tests pin
# WATERCOOLER_MCP_TRANSPORT=stdio in tests/conftest.py, so library/test
# imports build the full stack regardless of operator configuration.
# ---------------------------------------------------------------------------


def _effective_import_transport() -> str:
    """The effective execution-routing mode at import time (fail-open stdio)."""
    try:
        from watercooler.config_facade import config as _facade

        from .config import effective_transport, get_mcp_transport_config

        # THE authoritative snapshot — the same env-override-aware source
        # main() dispatches from (review #1135 P1: get_watercooler_config()
        # alone misses WATERCOOLER_MCP_URL/_TRANSPORT, letting import and
        # dispatch disagree — the exact split-brain this gate prevents).
        tc = get_mcp_transport_config()
        return effective_transport(
            tc.get("transport", "stdio"),
            tc.get("url", "") or "",
            _facade.get_hosted_api_key() or "",
        )
    except Exception:  # noqa: BLE001 — import must never hard-fail on config IO
        return "stdio"


_IMPORT_TRANSPORT = _effective_import_transport()
_THIN_PROXY_IMPORT = _IMPORT_TRANSPORT == "proxy"

if _THIN_PROXY_IMPORT:
    # Thin proxy client: no local tool surface, no queue workers, no local
    # daemon manager. ``mcp`` stays None; main() dispatches to _run_proxy,
    # which builds its own forwarding server. Tool re-exports below resolve
    # to their module-level None placeholders.
    mcp = None
    # NOTE (review #1135 P1, round 3): the thin import deliberately does NOT
    # touch the user-global stance-producer sidecar — another repo on this
    # machine may have a live LOCAL daemon fleet that owns it
    # (_try_acquire_daemon_lock permits concurrent per-repo fleets). Staleness
    # relative to THIS repo is handled reader-side: the Stop hook resolves
    # the current repo's effective transport and skips local findings
    # sources entirely under proxy (stop_hook._local_findings_apply).
else:
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


def _preflight_hosted_auth(client, repo: str) -> None:
    """One authenticated round-trip against the hosted endpoint before serving.

    A definitive 4xx here is a configuration/authorisation failure — bad API
    key, or the repo missing from the token's ``repos`` claim
    (``repo_claim_mismatch``) — that FastMCP's proxy would otherwise surface
    to the agent CLI as a bare JSON-RPC -32603, discarding the backend's
    actionable error body (#1117). Exit with that body on stderr instead: the
    same startup-failure class as the missing-URL / missing-API-key checks in
    ``_run_proxy``, and strictly better than a live server whose every tool
    call 403s. Anything else (network failure, timeout, 5xx) only warns — the
    proxy builds a fresh session per request, so a transient startup failure
    recovers on its own.
    """
    import asyncio

    from .premium_client import summarize_http_error

    async def _probe() -> None:
        session = client.new()
        try:
            async with session:
                await session.ping()
        finally:
            try:
                # Same forced-teardown idiom as PremiumToolClient
                # _fresh_session: a failed connect handshake never reaches
                # __aexit__, so the partial session would leak without this.
                await session._disconnect(force=True)
            except Exception:
                pass

    try:
        asyncio.run(_probe())
    except Exception as exc:
        status, detail = summarize_http_error(exc)
        if status is not None and 400 <= status < 500:
            detail = detail or str(exc)
            lines = [
                f"Error: the hosted backend rejected this proxy session "
                f"(HTTP {status}).",
                "",
                f"  {detail}",
            ]
            if "repo_claim_mismatch" in detail:
                lines += [
                    "",
                    f"Connect {repo!r} in the dashboard, or set "
                    "[mcp].proxy_repo to a repo this token is authorised "
                    "for. If you just connected it, the authorisation cache "
                    "can take up to 5 minutes to refresh.",
                ]
            print("\n".join(lines), file=sys.stderr)
            sys.exit(1)
        print(
            f"Warning: hosted preflight failed ({exc}); starting the proxy "
            "anyway — per-request sessions will retry.",
            file=sys.stderr,
        )


def _resolve_effective_transport(transport: str, transport_config: dict) -> str:
    """Apply the hosted-first fallback to the configured transport.

    ``proxy`` is the shipped default, but proxy has no local fallback — it can
    only forward to a remote endpoint with an API key. A not-yet-authenticated
    or open-core install has no key, so rather than hard-exit we transparently
    fall back to local ``stdio`` (a working local instance), emitting a one-line
    hint. Any non-proxy transport, or proxy with real credentials, is returned
    unchanged.

    Args:
        transport: The configured transport (``stdio``/``http``/``proxy``/``hybrid``).
        transport_config: The resolved transport config (carries ``url``).

    Returns:
        The effective transport to run.
    """
    if transport != "proxy":
        return transport

    from .config import effective_transport

    url = transport_config.get("url", "")
    api_key = config.get_hosted_api_key()
    effective = effective_transport(transport, url, api_key)
    if effective != "proxy":
        print(
            "Watercooler: hosted (proxy) is the default, but no hosted API key "
            "was found in ~/.watercooler/credentials.toml — running locally "
            "instead. Run `watercooler login` to use the hosted services, or set "
            '[mcp].transport = "stdio" to make local-only explicit.',
            file=sys.stderr,
        )
    return effective


def _run_proxy(transport_config: dict, *, boot_cwd: Path | None = None) -> None:
    """Run the MCP server in proxy mode.

    Forwards all tool calls to a remote hosted MCP endpoint via FastMCP's
    built-in proxy. The process is a THIN client: no local services
    (llama-server, FalkorDB) are started, and — because module import is
    transport-gated on the effective transport (Phase 2, gap G2) — no local
    tool surface is built, no memory-queue workers run, and no local daemon
    manager is initialized in an authenticated proxy process.

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

    from fastmcp.server import create_proxy
    from .premium_client import build_premium_client, build_premium_headers

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

    # Shared builder bounds both the connect handshake and per-call reads (the
    # config defaults are None == infinite). This client is disconnected at
    # create_proxy, so the proxy builds a fresh session per request.
    client = build_premium_client(url, headers, api_key)

    # Fail fast, with the backend's own error body, on an auth/authz
    # rejection — see _preflight_hosted_auth (#1117).
    _preflight_hosted_auth(client, headers.get("X-Repo", ""))

    proxy = create_proxy(client, name="Watercooler Cloud (Proxy)")

    # Multi-repo routing (#1082, completion-sequence Wave 2, Codex-approved
    # 01KX0B3EZB166VXBEE87DSWT9G): a call whose code_path positively derives
    # a different repo is routed to that repo's pooled premium client (reads
    # AND writes); underivable/absent code_path stays on the pinned session;
    # an unclaimed derived repo is refused by the hosted ownership check.
    # If the pool cannot be constructed, degrade to the A2 guard (refuse
    # cross-repo calls deterministically) rather than run unguarded.
    from .premium_client import PremiumClientPool, PremiumToolClient
    from .proxy_guard import ProxyRepoScopeMiddleware

    pool = None
    try:
        boot_ptc = PremiumToolClient.from_transport_config(
            transport_config, boot_cwd=boot_cwd
        )
        pool = PremiumClientPool(transport_config, boot_ptc, boot_cwd=boot_cwd)
    except Exception as exc:
        print(
            f"Warning: multi-repo routing unavailable ({exc}); "
            "cross-repo calls will be refused (single-repo guard mode).",
            file=sys.stderr,
        )

    proxy.add_middleware(
        ProxyRepoScopeMiddleware(headers.get("X-Repo", ""), pool=pool)
    )
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
    from .premium_client import PremiumClientPool, PremiumToolClient
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

    # Per-(repo, branch) client pool: multi-repo sessions assert the
    # correct X-Repo per call instead of the boot repo on every request
    # (incident bug-hybrid-static-x-repo-cross-tenant-t2-scope).
    premium_pool = PremiumClientPool(
        transport_config, premium_client, boot_cwd=boot_cwd
    )

    # Build runtime and server
    runtime = ToolRuntime(
        surface="local_hybrid",
        capability_profile=profile,
        premium_client=premium_client,
        premium_pool=premium_pool,
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
    # Hosted-first default: proxy needs credentials; without them, fall back to
    # local stdio so a not-yet-authenticated / open-core install still runs.
    transport = _resolve_effective_transport(
        transport_config["transport"], transport_config
    )

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

    # Local dispatch requires the local surface. Import-time gating only
    # skips it under an EFFECTIVE proxy, and proxy dispatch returned above —
    # this can only trip if configuration changed between import and main()
    # (e.g. credentials appeared at import, vanished by dispatch). Fail loud
    # rather than crash on a None surface.
    if mcp is None:
        print(
            "Error: local transport requested but the local tool surface was "
            "not built at import (the process started as an authenticated "
            "proxy). Restart the server so import and dispatch agree.",
            file=sys.stderr,
        )
        sys.exit(1)

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
