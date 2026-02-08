"""Memory tools for watercooler MCP server (Graphiti backend).

Tools:
- watercooler_get_entity_edge: Get entity/edge details
- watercooler_diagnose_memory: Diagnose memory backend
- watercooler_graphiti_add_episode: Add episode to Graphiti
- watercooler_leanrag_run_pipeline: Run LeanRAG clustering pipeline
- watercooler_clear_graph_group: Clear episodes for a group
- watercooler_smart_query: Multi-tier intelligent query with auto-escalation
- watercooler_memory_task_status: Check queue health, poll task status, recover
- watercooler_bulk_index: Queue bulk thread indexing into memory backend

Removed (use replacements):
- watercooler_query_memory → watercooler_smart_query
- watercooler_search_nodes → watercooler_search(mode="entities")
- watercooler_search_memory_facts → watercooler_smart_query
- watercooler_get_episodes → watercooler_search(mode="episodes")
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
get_entity_edge = None
diagnose_memory = None

# Write tools (Milestone 5.1, 5.2)
graphiti_add_episode = None
leanrag_run_pipeline = None

# Cleanup tools
clear_graph_group = None

# Multi-tier orchestration
smart_query = None

# Memory task queue tools
memory_task_status = None
bulk_index = None


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


def _diagnose_memory_impl(ctx: Context, code_path: str = "") -> ToolResult:
    """Diagnose Graphiti memory backend installation and configuration.

    Returns diagnostic information about package paths, imports, and configuration.
    Useful for debugging backend initialization issues.

    Args:
        ctx: MCP context
        code_path: Path to code repository (for resolving database name)

    Returns:
        JSON with diagnostic information including:
        - Python version
        - watercooler_memory package path
        - GraphitiBackend import status
        - Configuration status
        - Backend initialization status

    Example:
        diagnose_memory(code_path="/path/to/project")
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
        config = mem.load_graphiti_config(code_path=code_path if code_path else None)
        diagnostics["graphiti_enabled"] = config is not None
        if config:
            # Check LLM API key (llm_api_key is the current field, openai_api_key is deprecated)
            llm_key = config.llm_api_key or config.openai_api_key
            diagnostics["llm_api_key_set"] = bool(llm_key)
            diagnostics["llm_api_base"] = config.llm_api_base or "https://api.openai.com/v1 (default)"
            diagnostics["llm_model"] = config.llm_model or "gpt-4o-mini (default)"
            # Validate key format (basic check - not a full auth test)
            if llm_key:
                if llm_key.startswith("sk-"):
                    diagnostics["llm_api_key_format"] = "valid (sk-...)"
                else:
                    diagnostics["llm_api_key_format"] = f"unusual format: {llm_key[:10]}..."
            # Legacy field check (for backwards compatibility awareness)
            diagnostics["openai_key_set"] = bool(config.openai_api_key)  # Deprecated field
        else:
            diagnostics["config_issue"] = (
                "Graphiti not enabled. Either set WATERCOOLER_GRAPHITI_ENABLED=1, "
                "or configure [memory] backend = 'graphiti' in config.toml. "
                "Also ensure API keys are configured via LLM_API_KEY / EMBEDDING_API_KEY "
                "env vars or ~/.watercooler/credentials.toml (see credentials.example.toml)."
            )

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

        # Fire-and-forget: spawn background task for the slow LLM+graph work.
        # The graphiti pipeline (DeepSeek LLM calls + FalkorDB writes) takes
        # 60-120s, which exceeds the middleware's default 50s tool timeout.
        # Cancellation mid-flight corrupts FalkorDB connections and causes
        # socket disconnects. By returning immediately, we avoid the timeout
        # while matching the fire-and-forget pattern used by middleware memory
        # sync (sync_to_memory_backend via ThreadPoolExecutor).
        async def _do_add_episode():
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

                # Track entry-episode mapping if entry_id provided
                if entry_id and episode_uuid != "unknown":
                    backend.index_entry_as_episode(entry_id, episode_uuid, group_id)

                log_action(
                    f"MEMORY: Background episode added {episode_uuid} "
                    f"to group {group_id} "
                    f"(entities={len(result.get('entities_extracted', []))}, "
                    f"facts={result.get('facts_extracted', 0)})"
                )
            except Exception as e:
                log_error(f"MEMORY: Background add_episode failed: {e}")

        asyncio.create_task(_do_add_episode())

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "success": True,
                "status": "submitted",
                "group_id": group_id,
                "entry_id": entry_id if entry_id else None,
                "message": "Episode submitted for background processing",
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
            import os
            leanrag_path = os.getenv("LEANRAG_PATH", "external/LeanRAG")
            config = LeanRAGConfig(
                leanrag_path=Path(leanrag_path),
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


async def _smart_query_impl(
    query: str,
    ctx: Context,
    code_path: str = "",
    threads_dir: str = "",
    max_tiers: int = 2,
    force_tier: Optional[str] = None,
    group_ids: Optional[List[str]] = None,
) -> ToolResult:
    """Execute intelligent multi-tier memory query with automatic escalation.

    Queries memory across three tiers with automatic escalation when lower tiers
    don't provide sufficient results:

    - **T1 (Baseline)**: JSONL graph with keyword/semantic search (cheapest, no LLM)
    - **T2 (Graphiti)**: FalkorDB temporal graph with hybrid search (medium cost)
    - **T3 (LeanRAG)**: Hierarchical clustering with multi-hop reasoning (expensive)

    The orchestrator follows the principle: "Always choose the cheapest tier that
    can satisfy the query intent." Escalation happens automatically when results
    are insufficient (fewer than min_results or low confidence).

    Prerequisites:
        - T1: threads_dir must exist with .graph/nodes.jsonl
        - T2: WATERCOOLER_GRAPHITI_ENABLED=1 + FalkorDB running
        - T3: LEANRAG_PATH set + WATERCOOLER_TIER_T3_ENABLED=1

    Environment Variables:
        WATERCOOLER_TIER_T1_ENABLED: "1" to enable T1 (default: "1")
        WATERCOOLER_TIER_T2_ENABLED: "1" to enable T2 (requires Graphiti)
        WATERCOOLER_TIER_T3_ENABLED: "1" to enable T3 (expensive, opt-in)
        WATERCOOLER_TIER_MAX_TIERS: Maximum tiers to query (default: "2")
        WATERCOOLER_TIER_MIN_RESULTS: Min results for sufficiency (default: "3")

    Args:
        query: Search query (e.g., "What authentication method was implemented?")
        code_path: Path to code repository (for T2/T3 database resolution)
        threads_dir: Path to threads directory (for T1 baseline graph). If empty,
            attempts to resolve from code_path.
        max_tiers: Maximum number of tiers to query (default: 2, max: 3)
        force_tier: Force query to specific tier ("T1", "T2", or "T3"). Disables
            escalation when set.
        group_ids: Optional list of project group_ids to filter results.

    Returns:
        JSON response with search results containing:
        - query: Original query text
        - result_count: Total evidence items found
        - tiers_queried: List of tiers that were queried (e.g., ["T1", "T2"])
        - primary_tier: The tier that provided best results
        - escalation_reason: Why escalation occurred (if applicable)
        - sufficient: Whether results met sufficiency criteria
        - evidence: List of evidence items from all tiers
        - message: Status message

    Example:
        smart_query(
            query="What error handling patterns did we use?",
            code_path=".",
            max_tiers=2
        )

    Response Format:
        {
          "query": "What error handling patterns did we use?",
          "result_count": 5,
          "tiers_queried": ["T1", "T2"],
          "primary_tier": "T2",
          "escalation_reason": "Only 2 results (need 3)",
          "sufficient": true,
          "evidence": [
            {
              "tier": "T1",
              "id": "01ABC...",
              "content": "Implemented try-catch patterns...",
              "score": 0.85,
              "name": "Error Handling Discussion",
              "provenance": {...},
              "metadata": {...}
            },
            ...
          ],
          "message": "Found 5 results from T2"
        }
    """
    try:
        from pathlib import Path
        from watercooler_memory.tier_strategy import (
            TierOrchestrator,
            load_tier_config,
            Tier,
        )

        # Resolve paths
        code_path_resolved = Path(code_path) if code_path else None
        threads_dir_resolved = Path(threads_dir) if threads_dir else None

        # If threads_dir not provided, use proper context resolution
        # This handles the {repo-name}-threads sibling directory convention
        if not threads_dir_resolved and code_path:
            error, context = validation._require_context(code_path)
            if error:
                log_error(f"MEMORY: smart_query context resolution failed: {error}")
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "query": query,
                        "result_count": 0,
                        "error": "Context resolution failed",
                        "message": error,
                        "available_tiers": [],
                    }, indent=2)
                )])

            if context and context.threads_dir:
                threads_dir_resolved = context.threads_dir
                if not code_path_resolved:
                    code_path_resolved = context.code_root
                log_action(
                    "memory.smart_query.resolved_context",
                    threads_dir=str(threads_dir_resolved),
                    code_root=str(code_path_resolved) if code_path_resolved else None,
                )

        # Load configuration
        config = load_tier_config(
            threads_dir=threads_dir_resolved,
            code_path=code_path_resolved,
        )

        # Apply max_tiers parameter
        if max_tiers:
            config.max_tiers = min(max(1, max_tiers), 3)

        # Create orchestrator
        orchestrator = TierOrchestrator(config)

        # Check for available tiers
        available = orchestrator.available_tiers
        if not available:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "query": query,
                    "result_count": 0,
                    "tiers_queried": [],
                    "primary_tier": None,
                    "sufficient": False,
                    "evidence": [],
                    "message": "No memory tiers available. Check configuration.",
                    "available_tiers": [],
                }, indent=2)
            )])

        # Convert force_tier string to Tier enum
        force_tier_enum = None
        if force_tier:
            try:
                force_tier_enum = Tier(force_tier.upper())
                if force_tier_enum not in available:
                    return ToolResult(content=[TextContent(
                        type="text",
                        text=json.dumps({
                            "query": query,
                            "result_count": 0,
                            "error": f"Tier {force_tier} not available",
                            "available_tiers": [t.value for t in available],
                            "message": f"Requested tier {force_tier} is not available",
                        }, indent=2)
                    )])
            except ValueError:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "query": query,
                        "result_count": 0,
                        "error": f"Invalid tier: {force_tier}",
                        "valid_tiers": ["T1", "T2", "T3"],
                        "message": f"Invalid tier '{force_tier}'. Valid options: T1, T2, T3",
                    }, indent=2)
                )])

        # Execute query
        log_action(
            "memory.smart_query",
            query=query,
            max_tiers=config.max_tiers,
            force_tier=force_tier,
            available_tiers=[t.value for t in available],
        )

        try:
            result = await asyncio.to_thread(
                orchestrator.query,
                query,
                group_ids=group_ids,
                force_tier=force_tier_enum,
            )

            # Build response
            response = result.to_dict()
            response["available_tiers"] = [t.value for t in available]

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: Smart query failed: {e}")
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "query": query,
                    "result_count": 0,
                    "error": "Query execution failed",
                    "message": str(e),
                }, indent=2)
            )])

    except ImportError as e:
        log_error(f"MEMORY: tier_strategy module unavailable: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "query": query,
                "result_count": 0,
                "error": "Multi-tier strategy module unavailable",
                "message": f"Import failed: {e}. Ensure watercooler_memory package is installed.",
            }, indent=2)
        )])
    except Exception as e:
        log_error(f"MEMORY: Unexpected error in smart_query: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "query": query,
                "result_count": 0,
                "error": "Internal error",
                "message": str(e),
            }, indent=2)
        )])


# ============================================================================
# Memory Task Queue Tools
# ============================================================================


async def _memory_task_status_impl(
    ctx: Context,
    task_id: str = "",
    recover: bool = False,
    retry_dead_letters: bool = False,
) -> ToolResult:
    """Check memory queue health, poll task status, or trigger recovery.

    Args:
        ctx: MCP context
        task_id: Optional task ID to check. Empty = queue summary.
        recover: If True, reset stale "running" tasks to "pending".
        retry_dead_letters: If True, move dead-letter tasks back to queue.

    Returns:
        JSON with queue status or specific task details.
    """
    try:
        from ..memory_queue import get_queue

        queue = get_queue()
        if queue is None:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": "Memory queue not initialised",
                    "hint": "Queue starts automatically with MCP server",
                }),
            )])

        # Recovery actions
        if recover:
            count = queue.recover_stale()
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "action": "recover_stale",
                    "recovered": count,
                }),
            )])

        if retry_dead_letters:
            count = queue.retry_dead_letters()
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "action": "retry_dead_letters",
                    "re_enqueued": count,
                }),
            )])

        # Specific task lookup
        if task_id:
            task = queue.get_task(task_id)
            if task is None:
                return ToolResult([TextContent(
                    type="text",
                    text=json.dumps({
                        "error": f"Task {task_id} not found in active queue",
                        "hint": "Completed tasks are removed from the active queue",
                    }),
                )])
            return ToolResult([TextContent(
                type="text",
                text=json.dumps(task.to_dict(), indent=2),
            )])

        # Queue summary
        summary = queue.status_summary()
        return ToolResult([TextContent(
            type="text",
            text=json.dumps(summary, indent=2),
        )])

    except Exception as e:
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({"error": str(e)}),
        )])


async def _bulk_index_impl(
    ctx: Context,
    code_path: str = "",
    backend: str = "graphiti",
    threads: str = "",
    max_entries: int = 0,
) -> ToolResult:
    """Queue bulk indexing of threads into memory backend (paid tier onboarding).

    Discovers threads, builds a manifest of entries, and enqueues them
    as individual tasks for persistent background processing with retry.

    Args:
        ctx: MCP context
        code_path: Repository root path (for group_id derivation).
        backend: Target backend ("graphiti" or "leanrag").
        threads: Comma-separated thread topics to index (empty = all).
        max_entries: Max entries to queue (0 = unlimited, for testing).

    Returns:
        JSON with task count and monitoring info.
    """
    try:
        from ..memory_queue import get_queue, MemoryTask, enqueue_memory_task, VALID_BACKENDS

        if backend not in VALID_BACKENDS:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Invalid backend {backend!r}",
                    "valid_backends": sorted(VALID_BACKENDS),
                }),
            )])

        queue = get_queue()
        if queue is None:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": "Memory queue not initialised",
                }),
            )])

        # Discover entries via watercooler library
        from watercooler.commands import list_entries
        from watercooler.path_resolver import resolve_threads_dir

        threads_dir = resolve_threads_dir(code_path) if code_path else None
        if threads_dir is None:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": "Could not resolve threads directory",
                    "hint": "Provide code_path to the repository root",
                }),
            )])

        # Get list of thread topics
        from watercooler.metadata import list_topics
        all_topics = list_topics(threads_dir)

        if threads:
            selected = [t.strip() for t in threads.split(",")]
            all_topics = [t for t in all_topics if t in selected]

        # Derive group_id from code_path
        threads_dir_str = str(threads_dir)
        group_id = (
            threads_dir_str.removesuffix("-threads")
            if threads_dir_str.endswith("-threads")
            else threads_dir_str
        )

        queued = 0
        skipped = 0
        errors = []

        for topic in all_topics:
            try:
                entries = list_entries(topic, threads_dir)
            except Exception as e:
                errors.append(f"{topic}: {e}")
                continue

            for entry in entries:
                if max_entries and queued >= max_entries:
                    break

                entry_id = entry.get("entry_id", "")
                content = entry.get("body", "")
                if not content:
                    skipped += 1
                    continue

                task_id = enqueue_memory_task(
                    entry_id=entry_id,
                    topic=topic,
                    group_id=group_id,
                    content=content,
                    backend=backend,
                    title=entry.get("title", ""),
                    timestamp=entry.get("timestamp", ""),
                    source_description=f"{group_id} | thread:{topic} | bulk_index",
                )
                if task_id:
                    queued += 1
                else:
                    skipped += 1

            if max_entries and queued >= max_entries:
                break

        summary = queue.status_summary()
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({
                "action": "bulk_index",
                "topics_scanned": len(all_topics),
                "entries_queued": queued,
                "entries_skipped": skipped,
                "errors": errors[:10],
                "queue": summary,
            }, indent=2),
        )])

    except ImportError as e:
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({
                "error": f"Missing dependency: {e}",
                "hint": "Bulk index requires the full watercooler package",
            }),
        )])
    except Exception as e:
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({"error": str(e)}),
        )])


def register_memory_tools(mcp):
    """Register memory tools with the MCP server.

    Args:
        mcp: The FastMCP server instance

    Note:
        The following tools have been removed (use replacements):
        - watercooler_query_memory → watercooler_smart_query
        - watercooler_search_nodes → watercooler_search(mode="entities")
        - watercooler_search_memory_facts → watercooler_smart_query
        - watercooler_get_episodes → watercooler_search(mode="episodes")
    """
    global get_entity_edge, diagnose_memory
    global graphiti_add_episode, leanrag_run_pipeline, clear_graph_group
    global smart_query
    global memory_task_status, bulk_index

    # Register tools and store references for testing
    get_entity_edge = mcp.tool(name="watercooler_get_entity_edge")(_get_entity_edge_impl)
    diagnose_memory = mcp.tool(name="watercooler_diagnose_memory")(_diagnose_memory_impl)

    # Write tools (Milestone 5.1, 5.2)
    graphiti_add_episode = mcp.tool(name="watercooler_graphiti_add_episode")(_graphiti_add_episode_impl)
    leanrag_run_pipeline = mcp.tool(name="watercooler_leanrag_run_pipeline")(_leanrag_run_pipeline_impl)

    # Cleanup tools
    clear_graph_group = mcp.tool(name="watercooler_clear_graph_group")(_clear_graph_group_impl)

    # Multi-tier orchestration
    smart_query = mcp.tool(name="watercooler_smart_query")(_smart_query_impl)

    # Memory task queue tools
    memory_task_status = mcp.tool(name="watercooler_memory_task_status")(_memory_task_status_impl)
    bulk_index = mcp.tool(name="watercooler_bulk_index")(_bulk_index_impl)
