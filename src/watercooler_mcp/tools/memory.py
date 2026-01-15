"""Memory tools for watercooler MCP server (Graphiti backend).

Tools:
- watercooler_query_memory: Query memory backend
- watercooler_search_nodes: Search entity nodes
- watercooler_get_entity_edge: Get entity/edge details
- watercooler_search_memory_facts: Search facts
- watercooler_get_episodes: Get episodes
- watercooler_diagnose_memory: Diagnose memory backend
"""

import asyncio
import json
from typing import Optional, List

from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from ..observability import log_action, log_error, log_warning
from .. import validation  # Import module for runtime access (enables test patching)


# Module-level references to registered tools (populated by register_memory_tools)
query_memory = None
search_nodes = None
get_entity_edge = None
search_memory_facts = None
get_episodes = None
diagnose_memory = None

# Write tools (Milestone 5.1, 5.2)
graphiti_add_episode = None
leanrag_run_pipeline = None

# Cleanup tools
clear_graph_group = None


async def _query_memory_impl(
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
            from .. import memory as mem
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

        # Load configuration with project-based database name
        config = mem.load_graphiti_config(code_path=code_path)
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
        error, context = validation._require_context(code_path)
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


async def _search_nodes_impl(
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
        group_ids: Optional list of project group_ids to filter by. In the unified
            model, all threads share a single project group_id (e.g., "watercooler_cloud").
            If not specified, searches all accessible groups.
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
        from .. import memory as mem

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
        backend, error = mem.validate_memory_prerequisites("search_nodes", code_path=code_path)
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
        log_action("memory.search_nodes", query=query, max_nodes=max_nodes, group_ids=group_ids)

        try:
            results = await asyncio.to_thread(
                backend.search_nodes,
                query=query,
                group_ids=group_ids,
                max_results=max_nodes,  # Backend uses max_results parameter
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
        from .. import memory as mem

        log_error(f"MEMORY: Unexpected error in search_nodes: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "search_nodes",
            query=query,
            result_count=0,
            results=[],
        )


async def _get_entity_edge_impl(
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
        group_id: Project group_id (database name) where edge is stored.
                 In the unified model, all threads share one group_id per project
                 (e.g., "watercooler_cloud"). Searches default database if not provided.

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
        from .. import memory as mem

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
        backend, error = mem.validate_memory_prerequisites("get_entity_edge", code_path=code_path)
        if error:
            return error

        # Execute query
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
        from .. import memory as mem

        log_error(f"MEMORY: Unexpected error in get_entity_edge: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "get_entity_edge"
        )


async def _search_memory_facts_impl(
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
        group_ids: Optional list of project group_ids to filter by. In the unified
            model, all threads share a single project group_id (e.g., "watercooler_cloud").
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
        from .. import memory as mem

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
        backend, error = mem.validate_memory_prerequisites("search_memory_facts", code_path=code_path)
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
        from .. import memory as mem

        log_error(f"MEMORY: Unexpected error in search_memory_facts: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "search_memory_facts",
            query=query,
            result_count=0,
            results=[],
        )


async def _get_episodes_impl(
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
        group_ids: Optional list of project group_ids to filter by. In the unified
            model, all threads share a single project group_id (e.g., "watercooler_cloud").
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
            group_ids=["watercooler_cloud"],
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
        from .. import memory as mem

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
        backend, error = mem.validate_memory_prerequisites("get_episodes", code_path=code_path)
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
        from .. import memory as mem

        log_error(f"MEMORY: Unexpected error in get_episodes: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "get_episodes",
            result_count=0,
            results=[],
        )


def _diagnose_memory_impl(ctx: Context) -> ToolResult:
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
            from .. import memory as mem
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


async def _graphiti_add_episode_impl(
    content: str,
    group_id: str,
    ctx: Context,
    code_path: str = "",
    entry_id: str = "",
    timestamp: str = "",
    title: str = "",
    source_description: str = "",
    previous_episode_uuids: list[str] | None = None,
) -> ToolResult:
    """Add an episode directly to Graphiti temporal graph.

    This tool allows direct ingestion of content as a Graphiti episode,
    bypassing the normal thread-based workflow. Useful for:
    - Importing external knowledge
    - Adding custom context to the graph
    - Testing and development

    Args:
        content: The episode content/body text (required)
        group_id: Project group_id for graph partitioning (required). In the unified
            model, all threads in a project share the same group_id (e.g., "watercooler_cloud").
            Use the project database name, not individual thread topics.
        code_path: Path to code repository (for database name derivation)
        entry_id: Optional watercooler entry ID for provenance tracking
        timestamp: Optional ISO 8601 timestamp (defaults to now)
        title: Optional episode title (defaults to first 50 chars of content)
        source_description: Optional source metadata. Include thread topic here for traceability
            (e.g., "thread:auth-feature | Migration: Claude").
        previous_episode_uuids: Optional list of episode UUIDs this episode follows.
            Used for explicit temporal ordering when chunks share the same timestamp.

    Returns:
        JSON with episode_uuid, entities_extracted, and success status

    Example:
        graphiti_add_episode(
            content="We decided to use JWT tokens with RS256 signing",
            group_id="watercooler_cloud",
            entry_id="01ABC123",
            timestamp="2025-01-15T10:00:00Z",
            source_description="thread:auth-feature | Migration: Claude"
        )
    """
    try:
        # Validate required fields
        if not content or not content.strip():
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Content is required and cannot be empty",
                    "episode_uuid": None,
                }, indent=2)
            )])

        if not group_id or not group_id.strip():
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "group_id is required and cannot be empty",
                    "episode_uuid": None,
                }, indent=2)
            )])

        # Import memory module (lazy-load)
        try:
            from .. import memory as mem
        except ImportError as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Memory module unavailable: {e}",
                    "episode_uuid": None,
                }, indent=2)
            )])

        # Load configuration
        config = mem.load_graphiti_config(code_path=code_path)
        if config is None:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Graphiti not enabled. Set WATERCOOLER_GRAPHITI_ENABLED=1",
                    "episode_uuid": None,
                }, indent=2)
            )])

        # Get backend instance
        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            error_msg = "Graphiti backend unavailable"
            if isinstance(backend, dict):
                error_msg = backend.get("message", error_msg)
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": error_msg,
                    "episode_uuid": None,
                }, indent=2)
            )])

        # Prepare episode data
        from datetime import datetime, timezone

        if timestamp:
            try:
                ref_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            except ValueError:
                ref_time = datetime.now(timezone.utc)
        else:
            ref_time = datetime.now(timezone.utc)

        episode_title = title if title else content[:50] + ("..." if len(content) > 50 else "")
        source_desc = source_description if source_description else "Direct episode via MCP tool"

        # Add episode via backend
        try:
            result = await backend.add_episode_direct(
                name=episode_title,
                episode_body=content,
                source_description=source_desc,
                reference_time=ref_time,
                group_id=group_id,
                previous_episode_uuids=previous_episode_uuids,
            )

            episode_uuid = result.get("episode_uuid", "unknown")
            entities = result.get("entities_extracted", [])
            facts_count = result.get("facts_extracted", 0)

            # Track entry-episode mapping if entry_id provided
            if entry_id and episode_uuid != "unknown":
                backend.index_entry_as_episode(entry_id, episode_uuid, group_id)

            log_action(f"MEMORY: Added episode {episode_uuid} to group {group_id}")

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "episode_uuid": episode_uuid,
                    "group_id": group_id,
                    "entities_extracted": entities,
                    "facts_extracted": facts_count,
                    "entry_id": entry_id if entry_id else None,
                    "message": f"Episode added to {group_id}",
                }, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: Failed to add episode: {e}")
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to add episode: {e}",
                    "episode_uuid": None,
                }, indent=2)
            )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in graphiti_add_episode: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Unexpected error: {e}",
                "episode_uuid": None,
            }, indent=2)
        )])


async def _clear_graph_group_impl(
    group_id: str,
    ctx: Context,
    code_path: str = "",
    confirm: bool = False,
) -> ToolResult:
    """Clear all episodes for a specific project group_id.

    This is a destructive operation that removes all Graphiti episodes
    belonging to the specified group. Use for cleanup/testing purposes.

    IMPORTANT: In the unified model, all threads in a project share one group_id
    (e.g., "watercooler_cloud"). Clearing this group will remove ALL episodes
    from ALL threads in the project.

    IMPORTANT: This operation cannot be undone. Data will be permanently deleted.

    Note: Entity nodes and edges created from these episodes may still remain
    in the graph (Graphiti doesn't cascade delete). Only Episodic nodes are removed.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        group_id: Project group_id to clear episodes for (required). In the unified
            model, this is the project database name (e.g., "watercooler_cloud"),
            not individual thread topics.
        ctx: MCP context
        code_path: Path to code repository (for database name derivation)
        confirm: Must be True to execute deletion (safety check)

    Returns:
        JSON with operation results:
        - success: True if episodes were cleared
        - removed: Number of episodes deleted
        - group_id: The sanitized group ID used
        - message: Human-readable status message

    Example Response:
        {
          "success": true,
          "removed": 15,
          "group_id": "cursor_greeting",
          "message": "Removed 15 episodes"
        }

    Safety:
        Set confirm=True to actually execute deletion.
        Without confirm=True, returns error message explaining requirement.
    """
    try:
        # Import memory module (lazy-load)
        try:
            from .. import memory as mem
        except ImportError as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Memory module unavailable: {e}",
                    "removed": 0,
                }, indent=2)
            )])

        # Validate group_id
        if not group_id or not group_id.strip():
            return mem.create_error_response(
                "Invalid group_id",
                "group_id parameter is required and must be non-empty",
                "clear_graph_group",
                removed=0,
            )

        # Safety check - require explicit confirmation
        if not confirm:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Confirmation required",
                    "message": (
                        f"This will permanently delete all episodes for group '{group_id}'. "
                        "Set confirm=True to proceed. This operation cannot be undone."
                    ),
                    "group_id": group_id,
                    "removed": 0,
                }, indent=2)
            )])

        # Load configuration and backend
        config = mem.load_graphiti_config(code_path=code_path)
        if config is None:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Graphiti not enabled. Set WATERCOOLER_GRAPHITI_ENABLED=1",
                    "removed": 0,
                }, indent=2)
            )])

        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            error_msg = "Graphiti backend unavailable"
            if isinstance(backend, dict):
                error_msg = backend.get("message", error_msg)
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": error_msg,
                    "removed": 0,
                }, indent=2)
            )])

        # Execute cleanup
        log_action("memory.clear_graph_group", group_id=group_id, confirm=confirm)

        try:
            result = backend.clear_group_episodes(group_id=group_id)

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "removed": result.get("removed", 0),
                    "group_id": result.get("group_id", group_id),
                    "message": result.get("message", "Episodes cleared"),
                }, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: Failed to clear episodes: {e}")
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to clear episodes: {e}",
                    "removed": 0,
                }, indent=2)
            )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in clear_graph_group: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Unexpected error: {e}",
                "removed": 0,
            }, indent=2)
        )])


def _get_leanrag_backend(config=None):
    """Get LeanRAG backend instance if available.

    Returns:
        LeanRAGBackend instance or None if unavailable
    """
    try:
        from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig
        from pathlib import Path

        # Use provided config or defaults
        if config is None:
            config = LeanRAGConfig(
                leanrag_path=Path("external/LeanRAG"),
            )
        elif isinstance(config, dict):
            config = LeanRAGConfig(**config)

        return LeanRAGBackend(config)
    except ImportError as e:
        log_error(f"MEMORY: LeanRAG backend import failed: {e}")
        return None
    except Exception as e:
        log_error(f"MEMORY: LeanRAG backend init failed: {e}")
        return None


async def _leanrag_run_pipeline_impl(
    group_id: str,
    ctx: Context,
    start_date: str = "",
    end_date: str = "",
    dry_run: bool = False,
) -> ToolResult:
    """Run LeanRAG clustering pipeline on Graphiti episodes.

    Processes episodes from a thread group through the LeanRAG pipeline:
    1. Extract chunks from Graphiti episodes
    2. Generate embeddings (BGE-M3, 1024-d)
    3. Cluster semantically similar content
    4. Store cluster summaries back to graph

    Args:
        group_id: Thread/topic identifier to process (required)
        start_date: Optional start date filter (ISO 8601)
        end_date: Optional end date filter (ISO 8601)
        dry_run: If True, only report what would be done

    Returns:
        JSON with clusters_created, chunks_processed, and execution stats

    Example:
        leanrag_run_pipeline(
            group_id="auth-feature",
            start_date="2025-01-01",
            dry_run=True
        )
    """
    import asyncio
    import time

    start_time = time.time()

    try:
        # Validate required fields
        if not group_id or not group_id.strip():
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "group_id is required",
                    "clusters_created": 0,
                }, indent=2)
            )])

        # Get backend instance
        backend = _get_leanrag_backend()
        if backend is None:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "LeanRAG backend unavailable. Install with: pip install watercooler-cloud[memory]",
                    "clusters_created": 0,
                }, indent=2)
            )])

        # For dry_run, just report what would be done
        if dry_run:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "group_id": group_id,
                    "dry_run": True,
                    "clusters_created": 0,
                    "chunks_processed": 0,
                    "execution_time_ms": int((time.time() - start_time) * 1000),
                    "message": f"Dry run: Would process episodes from group '{group_id}'",
                }, indent=2)
            )])

        # Fetch episodes from Graphiti for this group
        try:
            from watercooler_memory.backends.graphiti import GraphitiBackend

            graphiti = GraphitiBackend()
            episodes_result = await graphiti.get_episodes(
                group_ids=[group_id],
                limit=1000,  # Reasonable limit
            )
            episodes = episodes_result.get("episodes", [])
        except ImportError:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Graphiti backend required to fetch episodes. Enable Graphiti first.",
                    "clusters_created": 0,
                }, indent=2)
            )])
        except Exception as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to fetch episodes from Graphiti: {e}",
                    "clusters_created": 0,
                }, indent=2)
            )])

        if not episodes:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "group_id": group_id,
                    "dry_run": False,
                    "clusters_created": 0,
                    "chunks_processed": 0,
                    "execution_time_ms": int((time.time() - start_time) * 1000),
                    "message": f"No episodes found for group '{group_id}'",
                }, indent=2)
            )])

        # Convert episodes to ChunkPayload format
        from watercooler_memory.backends import ChunkPayload
        import hashlib

        chunks = []
        for ep in episodes:
            content = ep.get("content", "")
            chunk_id = ep.get("uuid") or hashlib.md5(content.encode()).hexdigest()
            chunks.append({
                "id": chunk_id,
                "text": content,
                "metadata": {
                    "group_id": group_id,
                    "source": "graphiti_episode",
                },
            })

        chunk_payload = ChunkPayload(
            manifest_version="1.0",
            chunks=chunks,
        )

        # Run LeanRAG index via thread (ADR 0001 Sync Facade pattern)
        try:
            result = await asyncio.to_thread(backend.index, chunk_payload)

            execution_time_ms = int((time.time() - start_time) * 1000)
            log_action(f"MEMORY: LeanRAG pipeline completed for {group_id}: {len(chunks)} chunks")

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "group_id": group_id,
                    "dry_run": False,
                    "clusters_created": result.indexed_count,
                    "chunks_processed": len(chunks),
                    "execution_time_ms": execution_time_ms,
                    "message": result.message,
                }, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: LeanRAG pipeline failed: {e}")
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Pipeline failed: {e}",
                    "clusters_created": 0,
                }, indent=2)
            )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in leanrag_run_pipeline: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Unexpected error: {e}",
                "clusters_created": 0,
            }, indent=2)
        )])


def register_memory_tools(mcp):
    """Register memory tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global query_memory, search_nodes, get_entity_edge
    global search_memory_facts, get_episodes, diagnose_memory
    global graphiti_add_episode, leanrag_run_pipeline, clear_graph_group

    # Register tools and store references for testing
    query_memory = mcp.tool(name="watercooler_query_memory")(_query_memory_impl)
    search_nodes = mcp.tool(name="watercooler_search_nodes")(_search_nodes_impl)
    get_entity_edge = mcp.tool(name="watercooler_get_entity_edge")(_get_entity_edge_impl)
    search_memory_facts = mcp.tool(name="watercooler_search_memory_facts")(_search_memory_facts_impl)
    get_episodes = mcp.tool(name="watercooler_get_episodes")(_get_episodes_impl)
    diagnose_memory = mcp.tool(name="watercooler_diagnose_memory")(_diagnose_memory_impl)

    # Write tools (Milestone 5.1, 5.2)
    graphiti_add_episode = mcp.tool(name="watercooler_graphiti_add_episode")(_graphiti_add_episode_impl)
    leanrag_run_pipeline = mcp.tool(name="watercooler_leanrag_run_pipeline")(_leanrag_run_pipeline_impl)

    # Cleanup tools
    clear_graph_group = mcp.tool(name="watercooler_clear_graph_group")(_clear_graph_group_impl)
