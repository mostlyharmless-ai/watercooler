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

# Standard library imports
import json
import os
import time
from pathlib import Path
from typing import Optional, List

# Third-party imports
from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from ulid import ULID
from git import Repo, InvalidGitRepositoryError, GitCommandError

# Local application imports
from watercooler import commands, fs
from watercooler.config_facade import config
from watercooler.metadata import thread_meta
from watercooler.thread_entries import ThreadEntry
from .config import (
    ThreadContext,
    get_agent_name,
    get_threads_dir,
    get_version,
    get_git_sync_manager_from_context,
    get_watercooler_config,
    resolve_thread_context,
)
from .git_sync import (
    GitPushError,
    BranchPairingError,
    BranchMismatch,
    validate_branch_pairing,
    _find_main_branch,
)
from .branch_parity import (
    run_preflight,
    write_parity_state,
    get_branch_health,
    ensure_readable,
    ParityStatus,
)
from .observability import log_debug, log_action, log_warning, log_error

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
    _require_context,
    _dynamic_context_missing,
    # Branch helpers
    _attempt_auto_fix_divergence,
    _validate_and_sync_branches,
    _refresh_threads,
    # Thread parsing
    _normalize_status,
    _extract_thread_metadata,
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
# Re-export tools for test compatibility
from .tools import diagnostic as _diagnostic_tools
from .tools import thread_query as _thread_query_tools
from .tools import thread_write as _thread_write_tools
from .tools import sync as _sync_tools
from .tools import graph as _graph_tools
from .tools import branch_parity as _branch_parity_tools
from .tools import memory as _memory_tools


# Keep _validate_thread_context in server.py to allow test patching of _require_context
def _validate_thread_context(code_path: str) -> tuple[str | None, ThreadContext | None]:
    """Validate and resolve thread context for MCP tools.

    Note: This function is kept in server.py (not helpers.py) so that tests can
    patch _require_context and _dynamic_context_missing via the server module.

    Args:
        code_path: Path to code repository

    Returns:
        Tuple of (error_message, context). If error_message is not None,
        context will be None.
    """
    error, context = _require_context(code_path)
    if error:
        return (error, None)
    if context is None:
        return (
            "Error: Unable to resolve code context for the provided code_path.",
            None,
        )
    if _dynamic_context_missing(context):
        return (
            "Dynamic threads repo was not resolved from your git context.\n"
            "Run from inside your code repo or set "
            "WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO.",
            None,
        )
    return (None, context)

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
query_memory = _memory_tools.query_memory
search_nodes = _memory_tools.search_nodes
get_entity_edge = _memory_tools.get_entity_edge
search_memory_facts = _memory_tools.search_memory_facts
get_episodes = _memory_tools.get_episodes
diagnose_memory = _memory_tools.diagnose_memory


# ============================================================================
# Server Entry Point
# ============================================================================

def _check_first_run() -> None:
    """Check if this is first run and suggest config initialization."""
    try:
        from watercooler.config_loader import get_config_paths

        paths = get_config_paths()
        user_config = paths.get("user_config")
        project_config = paths.get("project_config")

        # Check if any config file exists
        has_config = (
            (user_config and user_config.exists()) or
            (project_config and project_config.exists())
        )

        if not has_config:
            _add_startup_warning(
                "No config file found. Create one to customize settings:\n"
                "  uvx watercooler-cloud config init --user\n"
                "Using built-in defaults for now."
            )
    except Exception:
        # Don't let config check errors break server startup
        pass


def _ensure_ollama_running():
    """Start Ollama if graph features are enabled and it's not running.

    This reduces friction for new users - if they have Ollama installed
    and graph features enabled, we'll start it automatically.
    """
    import subprocess
    import urllib.request
    import urllib.error

    try:
        from .config import get_watercooler_config
        config = get_watercooler_config()
        graph_config = config.mcp.graph

        # Only auto-start if graph features are enabled
        if not (graph_config.generate_summaries or graph_config.generate_embeddings):
            return

        # Check if Ollama is already responding
        try:
            req = urllib.request.Request(
                "http://localhost:11434/v1/models",
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return  # Already running
        except (urllib.error.URLError, TimeoutError, OSError):
            pass  # Not running, try to start

        # Try to start Ollama
        log_debug("Starting Ollama for graph features...")

        # Method 1: Try systemctl (Linux with systemd)
        try:
            result = subprocess.run(
                ["systemctl", "start", "ollama"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                # Wait for it to be ready
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        req = urllib.request.Request("http://localhost:11434/v1/models")
                        with urllib.request.urlopen(req, timeout=2):
                            log_debug("Ollama started successfully via systemctl.")
                            return
                    except (urllib.error.URLError, TimeoutError, OSError):
                        continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Method 2: Try ollama serve directly (macOS, or Linux without systemd)
        try:
            # Check if ollama command exists
            result = subprocess.run(
                ["which", "ollama"],
                capture_output=True,
                timeout=2
            )
            if result.returncode == 0:
                # Start ollama serve in background
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                # Wait for it to be ready
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        req = urllib.request.Request("http://localhost:11434/v1/models")
                        with urllib.request.urlopen(req, timeout=2):
                            log_debug("Ollama started successfully via ollama serve.")
                            return
                    except (urllib.error.URLError, TimeoutError, OSError):
                        continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # If we get here, couldn't start Ollama - give platform-aware guidance
        import platform
        system = platform.system().lower()

        if system == "windows":
            install_cmd = "winget install Ollama.Ollama"
            alt_msg = "Or download from: https://ollama.com/download/windows\n"
        elif system == "darwin":
            install_cmd = "brew install ollama"
            alt_msg = "Or: curl -fsSL https://ollama.com/install.sh | sh\n"
        else:  # Linux
            install_cmd = "curl -fsSL https://ollama.com/install.sh | sh"
            alt_msg = ""

        msg = (
            "Ollama not available - graph features (summaries/embeddings) disabled.\n"
            "To enable AI-powered summaries and semantic search:\n"
            f"  {install_cmd}\n"
        )
        if alt_msg:
            msg += f"  {alt_msg}"
        msg += (
            "Then pull models:\n"
            "  ollama pull llama3.2:3b\n"
            "  ollama pull nomic-embed-text\n"
            "Restart your IDE to reload the MCP server."
        )
        _add_startup_warning(msg)
    except Exception as e:
        # Don't let auto-start errors break server startup
        log_debug(f"Ollama auto-start check failed: {e}")


def main():
    """Entry point for watercooler-mcp command."""
    # Check for first-run and suggest config initialization
    _check_first_run()

    # Auto-start Ollama if graph features are enabled
    _ensure_ollama_running()

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
