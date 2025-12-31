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
# Re-export tools for test compatibility
from .tools import diagnostic as _diagnostic_tools
from .tools import thread_query as _thread_query_tools
from .tools import thread_write as _thread_write_tools
from .tools import sync as _sync_tools
from .tools import graph as _graph_tools
from .tools import branch_parity as _branch_parity_tools


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


# ============================================================================
# Memory Tools (Graphiti)
# ============================================================================

@mcp.tool(name="watercooler_query_memory")
async def query_memory(
    query: str,
    ctx: Context,
    code_path: str = "",
    limit: int = 10,
    topic: Optional[str] = None,
) -> ToolResult:
    """Query thread history using Graphiti temporal graph memory.

    Searches indexed watercooler threads using semantic search and graph traversal.
    Returns relevant facts, entities, and relationships from thread history.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        query: Search query (e.g., "What authentication method was implemented?")
        code_path: Path to code repository (for resolving threads directory)
        limit: Maximum results to return (default: 10, range: 1-50)
        topic: Optional thread topic to restrict search (default: search all threads)

    Returns:
        JSON response with search results containing:
        - results: List of matching facts/entities with scores
        - query: Original query text
        - result_count: Number of results returned
        - message: Status/error message

    Example:
        query_memory(
            query="Who implemented OAuth2?",
            code_path=".",
            limit=5
        )

    Response Format:
        {
          "query": "Who implemented OAuth2?",
          "result_count": 2,
          "results": [
            {
              "content": "Claude implemented OAuth2 with JWT tokens",
              "score": 0.89,
              "metadata": {
                "thread_id": "auth-feature",
                "entry_id": "01ABC...",
                "valid_at": "2025-10-01T10:00:00Z"
              }
            }
          ],
          "message": "Found 2 results"
        }
    """
    try:
        # Import memory module (lazy-load)
        try:
            from . import memory as mem
        except ImportError as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Memory module unavailable",
                        "message": f"Install with: pip install watercooler-cloud[memory]. Details: {e}",
                        "query": query,
                        "result_count": 0,
                        "results": [],
                    },
                    indent=2,
                )
            )])

        # Load configuration
        config = mem.load_graphiti_config()
        if config is None:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Graphiti not enabled",
                        "message": (
                            "Set WATERCOOLER_GRAPHITI_ENABLED=1 and configure "
                            "OPENAI_API_KEY to enable memory queries."
                        ),
                        "query": query,
                        "result_count": 0,
                        "results": [],
                    },
                    indent=2,
                )
            )])

        # Validate limit parameter
        if limit < 1:
            limit = 10
        if limit > 50:
            limit = 50

        # Get backend instance
        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            if isinstance(backend, dict):
                # Structured error with details
                error_type = backend.get("error", "unknown")
                details = backend.get("details", "No details available")
                package_path = backend.get("package_path", "unknown")
                python_version = backend.get("python_version", "unknown")

                # Determine fix based on error type
                if "uv/archive" in package_path or "cache" in package_path:
                    fix_msg = (
                        f"Python {python_version} is loading from UV cache. "
                        "Fix: Ensure MCP server uses the correct Python environment, "
                        f"or install in Python {python_version} with: "
                        "uv pip install --reinstall --no-cache -e \".[memory,mcp]\""
                    )
                else:
                    fix_msg = "Check MCP server configuration and Python environment"

                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "error": f"Backend {error_type}",
                        "message": details,
                        "python_version": python_version,
                        "package_path": package_path,
                        "fix": fix_msg,
                        "query": query,
                        "result_count": 0,
                        "results": [],
                    }, indent=2)
                )])
            else:
                # Fallback for None (shouldn't happen with new code, but kept for safety)
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": "Backend initialization failed",
                            "message": "Check logs for Graphiti backend errors",
                            "query": query,
                            "result_count": 0,
                            "results": [],
                        },
                        indent=2,
                    )
                )])

        # Resolve threads directory (for context logging, not directly used in query)
        error, context = _require_context(code_path)
        if error:
            log_warning(f"MEMORY: Could not resolve context: {error}")
            # Continue anyway - query may work with existing index

        # Execute query
        log_action("memory.query", query=query, limit=limit, topic=topic)

        try:
            results, communities = await mem.query_memory(backend, query, limit, topic=topic)

            # Format response
            response = {
                "query": query,
                "result_count": len(results),
                "results": [
                    {
                        "content": r.get("content", ""),
                        "score": r.get("score", 0.0),
                        "metadata": r.get("metadata", {}),
                    }
                    for r in results
                ],
                "communities": communities,
                "message": f"Found {len(results)} results and {len(communities)} communities",
            }

            if topic:
                response["filtered_by_topic"] = topic

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: Query failed: {e}")
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Query execution failed",
                        "message": str(e),
                        "query": query,
                        "result_count": 0,
                        "results": [],
                    },
                    indent=2,
                )
            )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in query_memory: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(
                {
                    "error": "Internal error",
                    "message": str(e),
                    "query": query,
                    "result_count": 0,
                    "results": [],
                },
                indent=2,
            )
        )])




@mcp.tool(name="watercooler_search_nodes")
async def search_nodes(
    query: str,
    ctx: Context,
    code_path: str = "",
    group_ids: Optional[List[str]] = None,
    max_nodes: int = 10,
    entity_types: Optional[List[str]] = None,
) -> ToolResult:
    """Search for entity nodes using hybrid semantic search.

    Searches indexed watercooler threads for entity nodes (people, concepts, etc.)
    using Graphiti's hybrid search combining semantic embeddings, keyword search,
    and graph traversal.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        query: Search query (e.g., "authentication implementation")
        ctx: MCP context
        code_path: Path to code repository (for resolving threads directory)
        group_ids: Optional list of thread topics to filter by
        max_nodes: Maximum nodes to return (default: 10, max: 50)
        entity_types: Optional list of entity type names to filter

    Returns:
        JSON response with search results containing:
        - query: Original query text
        - result_count: Number of nodes returned
        - results: List of nodes with uuid, name, labels, summary, etc.
        - message: Status message

    Example:
        search_nodes(
            query="OAuth2 implementation",
            code_path=".",
            max_nodes=5
        )

    Response Format:
        {
          "query": "OAuth2 implementation",
          "result_count": 3,
          "results": [
            {
              "uuid": "01ABC...",
              "name": "OAuth2Provider",
              "labels": ["Class", "Authentication"],
              "summary": "OAuth2 provider implementation...",
              "created_at": "2025-10-01T10:00:00Z",
              "group_id": "auth-feature"
            }
          ],
          "message": "Found 3 nodes"
        }
    """
    try:
        from . import memory as mem
        
        # Validate query parameter
        if not query or not query.strip():
            return mem.create_error_response(
                "Invalid query",
                "Query parameter is required and must be non-empty",
                "search_nodes",
                query=query,
                result_count=0,
                results=[],
            )
        
        # Validate max_nodes parameter
        if max_nodes < 1:
            max_nodes = 10
        if max_nodes > 50:
            max_nodes = 50
        
        # Common validation (replaces ~100 lines of duplicated code)
        backend, error = mem.validate_memory_prerequisites("search_nodes")
        if error:
            # Add query/result fields to error response
            error_dict = json.loads(error.content[0].text)
            error_dict.update({
                "query": query,
                "result_count": 0,
                "results": [],
            })
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(error_dict, indent=2)
            )])
        
        # Execute search
        import asyncio
        from .observability import log_action, log_error
        
        log_action("memory.search_nodes", query=query, max_nodes=max_nodes, group_ids=group_ids)
        
        try:
            results = await asyncio.to_thread(
                backend.search_nodes,
                query=query,
                group_ids=group_ids,
                max_nodes=max_nodes,
                entity_types=entity_types,
            )
            
            # Format response
            response = {
                "query": query,
                "result_count": len(results),
                "results": results,
                "message": f"Found {len(results)} node(s)",
            }
            
            if group_ids:
                response["filtered_by_topics"] = group_ids
            
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])
            
        except Exception as e:
            log_error(f"MEMORY: Node search failed: {e}")
            return mem.create_error_response(
                "Search execution failed",
                str(e),
                "search_nodes",
                query=query,
                result_count=0,
                results=[],
            )
    
    except Exception as e:
        from .observability import log_error
        from . import memory as mem
        
        log_error(f"MEMORY: Unexpected error in search_nodes: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "search_nodes",
            query=query,
            result_count=0,
            results=[],
        )


@mcp.tool(name="watercooler_get_entity_edge")
async def get_entity_edge(
    uuid: str,
    ctx: Context,
    code_path: str = "",
    group_id: Optional[str] = None,
) -> ToolResult:
    """Get a specific entity edge (relationship) by UUID.

    Retrieves detailed information about a specific relationship between entities
    in the Graphiti knowledge graph.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        uuid: Edge UUID to retrieve
        ctx: MCP context
        code_path: Path to code repository (for resolving threads directory)
        group_id: Thread topic (database name) where edge is stored.
                 Required for multi-database setups. Searches default database if not provided.

    Returns:
        JSON response with edge details containing:
        - uuid: Edge UUID
        - fact: Description of the relationship
        - source_node_uuid: UUID of source entity
        - target_node_uuid: UUID of target entity
        - valid_at: When relationship became valid
        - invalid_at: When relationship became invalid (if applicable)
        - created_at: When edge was created
        - group_id: Thread topic this edge belongs to
        - message: Status message

    Example:
        get_entity_edge(
            uuid="01ABC123...",
            code_path="."
        )

    Response Format:
        {
          "uuid": "01ABC123...",
          "fact": "Claude implemented OAuth2 authentication",
          "source_node_uuid": "01DEF456...",
          "target_node_uuid": "01GHI789...",
          "valid_at": "2025-10-01T10:00:00Z",
          "created_at": "2025-10-01T10:00:00Z",
          "group_id": "auth-feature",
          "message": "Retrieved edge 01ABC123..."
        }
    """
    try:
        from . import memory as mem
        
        # Validate UUID parameter (tool-specific validation)
        if not uuid or not uuid.strip():
            return mem.create_error_response(
                "Invalid UUID",
                "UUID parameter is required and must be non-empty",
                "get_entity_edge"
            )
        
        # Sanitize UUID (limit length and characters)
        if len(uuid) > 100:
            return mem.create_error_response(
                "Invalid UUID",
                "UUID too long (max 100 characters)",
                "get_entity_edge",
                uuid=uuid[:50] + "..."
            )
        
        # Check for valid characters (alphanumeric, hyphen, underscore)
        if not all(c.isalnum() or c in '-_' for c in uuid):
            return mem.create_error_response(
                "Invalid UUID",
                "UUID contains invalid characters (only alphanumeric, hyphen, underscore allowed)",
                "get_entity_edge"
            )
        
        # Common validation (replaces ~100 lines of duplicated code)
        backend, error = mem.validate_memory_prerequisites("get_entity_edge")
        if error:
            return error
        
        # Execute query
        import asyncio
        from .observability import log_action, log_error
        
        log_action("memory.get_entity_edge", uuid=uuid, group_id=group_id)

        try:
            edge = await asyncio.to_thread(backend.get_entity_edge, uuid, group_id=group_id)
            
            # Handle None return (edge not found)
            if edge is None:
                return mem.create_error_response(
                    "Edge not found",
                    f"No edge found with UUID {uuid}",
                    "get_entity_edge",
                    uuid=uuid
                )
            
            # Format response
            response = {
                **edge,
                "message": f"Retrieved edge {uuid}",
            }
            
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])
            
        except Exception as e:
            log_error(f"MEMORY: Get entity edge failed: {e}")
            return mem.create_error_response(
                "Edge retrieval failed",
                str(e),
                "get_entity_edge",
                uuid=uuid
            )
    
    except Exception as e:
        from .observability import log_error
        from . import memory as mem
        
        log_error(f"MEMORY: Unexpected error in get_entity_edge: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "get_entity_edge"
        )


@mcp.tool(name="watercooler_search_memory_facts")
async def search_memory_facts(
    query: str,
    ctx: Context,
    code_path: str = "",
    group_ids: Optional[List[str]] = None,
    max_facts: int = 10,
    center_node_uuid: Optional[str] = None,
) -> ToolResult:
    """Search for facts (edges/relationships) with optional center-node traversal.

    Searches indexed watercooler threads for facts (relationships between entities)
    using Graphiti's hybrid search. Optionally centers the search around a specific
    entity node.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        query: Search query (e.g., "authentication decisions")
        ctx: MCP context
        code_path: Path to code repository (for resolving threads directory)
        group_ids: Optional list of thread topics to filter by
        max_facts: Maximum facts to return (default: 10, max: 50)
        center_node_uuid: Optional node UUID to center search around

    Returns:
        JSON response with search results containing:
        - query: Original query text
        - result_count: Number of facts returned
        - results: List of facts with uuid, fact text, source/target nodes, scores
        - message: Status message

    Example:
        search_memory_facts(
            query="OAuth2 implementation decisions",
            code_path=".",
            max_facts=5,
            center_node_uuid="01ABC..."
        )

    Response Format:
        {
          "query": "OAuth2 implementation decisions",
          "result_count": 2,
          "results": [
            {
              "uuid": "01ABC...",
              "fact": "Claude implemented OAuth2 with JWT tokens",
              "source_node_uuid": "01DEF...",
              "target_node_uuid": "01GHI...",
              "score": 0.89,
              "valid_at": "2025-10-01T10:00:00Z",
              "group_id": "auth-feature"
            }
          ],
          "message": "Found 2 fact(s)"
        }
    """
    try:
        from . import memory as mem
        
        # Validate query parameter
        if not query or not query.strip():
            return mem.create_error_response(
                "Invalid query",
                "Query parameter is required and must be non-empty",
                "search_memory_facts",
                query=query,
                result_count=0,
                results=[],
            )
        
        # Validate max_facts parameter
        if max_facts < 1:
            max_facts = 10
        if max_facts > 50:
            max_facts = 50
        
        # Common validation (replaces ~100 lines of duplicated code)
        backend, error = mem.validate_memory_prerequisites("search_memory_facts")
        if error:
            # Add query/result fields to error response
            error_dict = json.loads(error.content[0].text)
            error_dict.update({
                "query": query,
                "result_count": 0,
                "results": [],
            })
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(error_dict, indent=2)
            )])
        
        # Execute search
        import asyncio
        from .observability import log_action, log_error
        
        log_action(
            "memory.search_memory_facts",
            query=query,
            max_facts=max_facts,
            group_ids=group_ids,
            center_node_uuid=center_node_uuid,
        )
        
        try:
            results = await asyncio.to_thread(
                backend.search_memory_facts,
                query=query,
                group_ids=group_ids,
                max_facts=max_facts,
                center_node_uuid=center_node_uuid,
            )
            
            # Format response
            response = {
                "query": query,
                "result_count": len(results),
                "results": results,
                "message": f"Found {len(results)} fact(s)",
            }
            
            if group_ids:
                response["filtered_by_topics"] = group_ids
            if center_node_uuid:
                response["centered_on_node"] = center_node_uuid
            
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])
            
        except Exception as e:
            log_error(f"MEMORY: Fact search failed: {e}")
            return mem.create_error_response(
                "Search execution failed",
                str(e),
                "search_memory_facts",
                query=query,
                result_count=0,
                results=[],
            )
    
    except Exception as e:
        from .observability import log_error
        from . import memory as mem
        
        log_error(f"MEMORY: Unexpected error in search_memory_facts: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "search_memory_facts",
            query=query,
            result_count=0,
            results=[],
        )


@mcp.tool(name="watercooler_get_episodes")
async def get_episodes(
    query: str,
    ctx: Context,
    code_path: str = "",
    group_ids: Optional[List[str]] = None,
    max_episodes: int = 10,
) -> ToolResult:
    """Search for episodes from Graphiti memory using semantic search.

    Performs semantic search on episodic content from indexed watercooler threads.
    Note: Graphiti doesn't support listing all episodes; this tool requires a query
    string to perform semantic search.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        query: Search query string (required, must be non-empty)
        ctx: MCP context
        code_path: Path to code repository (for resolving threads directory)
        group_ids: Optional list of thread topics to filter by
        max_episodes: Maximum episodes to return (default: 10, max: 50)

    Returns:
        JSON response with episodes containing:
        - result_count: Number of episodes returned
        - results: List of episodes with uuid, name, content, timestamps
        - message: Status message

    Example:
        get_episodes(
            query="authentication implementation",
            code_path=".",
            group_ids=["auth-feature", "api-design"],
            max_episodes=5
        )

    Response Format:
        {
          "result_count": 2,
          "results": [
            {
              "uuid": "01ABC...",
              "name": "Entry 01ABC...",
              "content": "Implemented OAuth2 authentication...",
              "created_at": "2025-10-01T10:00:00Z",
              "source": "thread_entry",
              "source_description": "Watercooler thread entry",
              "group_id": "auth-feature",
              "valid_at": "2025-10-01T10:00:00Z"
            }
          ],
          "message": "Found 2 episode(s)",
          "filtered_by_topics": ["auth-feature", "api-design"]
        }
    """
    try:
        from . import memory as mem
        
        # Validate query parameter (tool-specific)
        if not query or not query.strip():
            return mem.create_error_response(
                "Invalid query",
                "Query parameter is required and must be non-empty for semantic search",
                "get_episodes",
                result_count=0,
                results=[],
            )
        
        # Validate max_episodes parameter
        if max_episodes < 1:
            max_episodes = 10
        if max_episodes > 50:
            max_episodes = 50
        
        # Common validation (replaces ~100 lines of duplicated code)
        backend, error = mem.validate_memory_prerequisites("get_episodes")
        if error:
            # Add result fields to error response
            error_dict = json.loads(error.content[0].text)
            error_dict.update({
                "result_count": 0,
                "results": [],
            })
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(error_dict, indent=2)
            )])
        
        # Execute query
        import asyncio
        from .observability import log_action, log_error
        
        log_action("memory.get_episodes", query=query, max_episodes=max_episodes, group_ids=group_ids)
        
        try:
            results = await asyncio.to_thread(
                backend.get_episodes,
                query=query,
                group_ids=group_ids,
                max_episodes=max_episodes,
            )
            
            # Format response
            response = {
                "result_count": len(results),
                "results": results,
                "message": f"Found {len(results)} episode(s)",
            }
            
            if group_ids:
                response["filtered_by_topics"] = group_ids
            
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])
            
        except Exception as e:
            log_error(f"MEMORY: Get episodes failed: {e}")
            return mem.create_error_response(
                "Episodes retrieval failed",
                str(e),
                "get_episodes",
                result_count=0,
                results=[],
            )
    
    except Exception as e:
        from .observability import log_error
        from . import memory as mem
        
        log_error(f"MEMORY: Unexpected error in get_episodes: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "get_episodes",
            result_count=0,
            results=[],
        )


@mcp.tool(name="watercooler_diagnose_memory")
def diagnose_memory(ctx: Context) -> ToolResult:
    """Diagnose Graphiti memory backend installation and configuration.

    Returns diagnostic information about package paths, imports, and configuration.
    Useful for debugging backend initialization issues.

    Returns:
        JSON with diagnostic information including:
        - Python version
        - watercooler_memory package path
        - GraphitiBackend import status
        - Configuration status
        - Backend initialization status

    Example:
        diagnose_memory()
    """
    try:
        # Import memory module (lazy-load)
        try:
            from . import memory as mem
        except ImportError as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Memory module unavailable",
                        "message": f"Install with: pip install watercooler-cloud[memory]. Details: {e}",
                    },
                    indent=2,
                )
            )])

        import sys
        diagnostics = {
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "python_executable": sys.executable,
        }

        # Check watercooler_memory import and path
        try:
            import watercooler_memory
            diagnostics["watercooler_memory_path"] = watercooler_memory.__file__
            diagnostics["watercooler_memory_version"] = getattr(
                watercooler_memory, "__version__", "unknown"
            )
        except ImportError as e:
            diagnostics["watercooler_memory_import"] = f"✗ Failed: {e}"

        # Check GraphitiBackend import
        try:
            from watercooler_memory.backends import GraphitiBackend
            diagnostics["graphiti_backend_import"] = "✓ Success"
            diagnostics["graphiti_backend_in_all"] = "GraphitiBackend" in getattr(
                __import__("watercooler_memory.backends"), "__all__", []
            )
        except ImportError as e:
            diagnostics["graphiti_backend_import"] = f"✗ Failed: {e}"

        # Check config
        config = mem.load_graphiti_config()
        diagnostics["graphiti_enabled"] = config is not None
        if config:
            diagnostics["openai_key_set"] = bool(config.openai_api_key)
        else:
            diagnostics["config_issue"] = "WATERCOOLER_GRAPHITI_ENABLED != '1' or OPENAI_API_KEY not set"

        # Check backend initialization
        if config:
            backend = mem.get_graphiti_backend(config)
            if isinstance(backend, dict):
                diagnostics["backend_init"] = f"✗ Failed: {backend.get('error', 'unknown')}"
                diagnostics["backend_error_details"] = backend
            elif backend is None:
                diagnostics["backend_init"] = "✗ Failed: Returned None"
            else:
                diagnostics["backend_init"] = "✓ Success"

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(diagnostics, indent=2)
        )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in diagnose_memory: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(
                {
                    "error": "Diagnostic failed",
                    "message": str(e),
                },
                indent=2,
            )
        )])


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
