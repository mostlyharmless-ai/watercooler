"""Graph tools for watercooler MCP server.

Tools:
- watercooler_baseline_graph_stats: Graph statistics
- watercooler_search: Search threads and entries (tier-aware routing)
- watercooler_find_similar: Find similar entries
- watercooler_graph_health: Graph sync health
- watercooler_access_stats: Access statistics

New Tool Suite (Fresh Suite Design):
- watercooler_graph_enrich: Generate/regenerate summaries and embeddings
- watercooler_graph_recover: Rebuild graph from markdown (emergency recovery)
- watercooler_graph_project: Generate markdown from graph (source of truth)
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastmcp import Context

from ..memory_queue import (
    DuplicateTaskError,
    MemoryTask,
    MemoryTaskQueue,
    MemoryTaskWorker,
    QueueFullError,
)
from ..sync import BranchPairingError
from ..middleware import run_with_graph_sync
from .. import validation  # Import module for runtime access (enables test patching)
from watercooler.path_resolver import derive_group_id

logger = logging.getLogger(__name__)


# =============================================================================
# Input Validation Helpers
# =============================================================================


# Validation bounds
MAX_LIMIT = 100
MAX_BATCH_SIZE = 100
MIN_SIMILARITY_THRESHOLD = 0.0
MAX_SIMILARITY_THRESHOLD = 1.0


def _validate_limit(limit: int, default: int = 10, max_value: int = MAX_LIMIT) -> int:
    """Validate and constrain a limit parameter.

    Args:
        limit: The user-provided limit value
        default: Default value if limit is invalid
        max_value: Maximum allowed value

    Returns:
        Validated limit between 1 and max_value
    """
    if not isinstance(limit, int) or limit < 1:
        return default
    return min(limit, max_value)


def _validate_threshold(threshold: float, default: float = 0.5) -> float:
    """Validate and constrain a similarity threshold.

    Args:
        threshold: The user-provided threshold value
        default: Default value if invalid

    Returns:
        Validated threshold between 0.0 and 1.0
    """
    if not isinstance(threshold, (int, float)):
        return default
    return max(MIN_SIMILARITY_THRESHOLD, min(float(threshold), MAX_SIMILARITY_THRESHOLD))


# =============================================================================
# Search Routing Helpers (Milestone 6: Tier-Aware Search Routing)
# =============================================================================


def get_search_backend(backend: str) -> str:
    """Determine which search backend to use.

    Priority (highest first):
        1. Explicit backend parameter ("baseline", "graphiti", "leanrag")
        2. WATERCOOLER_MEMORY_BACKEND env var
        3. TOML config (memory.backend)
        4. Default: "baseline"

    Args:
        backend: Requested backend - "auto", "baseline", "graphiti", or "leanrag"

    Returns:
        Resolved backend name: "baseline", "graphiti", or "leanrag"
    """
    # Explicit backends are respected (except unknown ones)
    if backend in ("baseline", "graphiti", "leanrag"):
        return backend

    # Auto mode: check env var first, then TOML config
    if backend == "auto":
        # Check env var
        memory_backend = os.environ.get("WATERCOOLER_MEMORY_BACKEND", "").lower().strip()
        if memory_backend in ("graphiti", "leanrag"):
            return memory_backend

        # Check TOML config
        try:
            from watercooler.memory_config import get_memory_backend
            toml_backend = get_memory_backend()
            if toml_backend in ("graphiti", "leanrag"):
                return toml_backend
        except ImportError:
            pass

        return "baseline"

    # Unknown backend falls back to baseline
    logger.warning(f"Unknown search backend: {backend}, falling back to baseline")
    return "baseline"


def infer_search_mode(mode: str, query: str, semantic: bool) -> str:
    """Infer the search mode based on the query and parameters.

    Args:
        mode: Requested mode - "auto", "entries", "entities", or "episodes"
        query: The search query
        semantic: Whether semantic search is enabled

    Returns:
        Resolved mode: "entries", "entities", or "episodes"
    """
    # Explicit modes are respected
    if mode in ("entries", "entities", "episodes"):
        return mode

    # Auto mode: infer from query characteristics
    # For now, default to entries mode (most common use case)
    # Future: could detect entity-like queries (proper nouns, names)
    return "entries"


async def route_search(
    ctx: Context,
    threads_dir: Path,
    query: str,
    backend: str,
    mode: str,
    code_path: str = "",
    **kwargs: Any,
) -> str:
    """Route search to the appropriate backend based on tier and mode.

    Args:
        ctx: MCP context
        threads_dir: Path to threads directory
        query: Search query
        backend: Resolved backend ("baseline", "graphiti", "leanrag")
        code_path: Path to code repository (for database name derivation)
        mode: Resolved mode ("entries", "entities", "episodes")
        **kwargs: Additional search parameters

    Returns:
        JSON string with search results
    """
    fallback_used = False
    fallback_reason = None

    # Entities/episodes modes require Graphiti
    if mode in ("entities", "episodes"):
        if backend == "baseline":
            # Can't do entities/episodes on baseline - fall back to entries
            logger.info(f"Mode {mode} requires Graphiti, but backend is baseline. Falling back to entries mode.")
            mode = "entries"
            fallback_used = True
            fallback_reason = f"{mode} requires memory backend"
        else:
            # Route to Graphiti entity/episode search
            try:
                if mode == "entities":
                    return await _search_graphiti_nodes_impl(
                        ctx=ctx,
                        threads_dir=threads_dir,
                        query=query,
                        code_path=code_path,
                        **kwargs,
                    )
                else:  # episodes
                    return await _search_graphiti_episodes_impl(
                        ctx=ctx,
                        threads_dir=threads_dir,
                        query=query,
                        code_path=code_path,
                        **kwargs,
                    )
            except Exception as e:
                logger.warning(f"Graphiti {mode} search failed: {e}. Falling back to baseline.")
                fallback_used = True
                fallback_reason = str(e)
                backend = "baseline"
                mode = "entries"

    # Entries mode - route based on backend
    if backend == "graphiti":
        try:
            return await _search_graphiti_impl(
                ctx=ctx,
                threads_dir=threads_dir,
                query=query,
                code_path=code_path,
                **kwargs,
            )
        except Exception as e:
            logger.warning(f"Graphiti search failed: {e}. Falling back to baseline.")
            fallback_used = True
            fallback_reason = str(e)
            backend = "baseline"

    if backend == "leanrag":
        try:
            return _search_leanrag_impl(
                ctx=ctx,
                threads_dir=threads_dir,
                query=query,
                **kwargs,
            )
        except Exception as e:
            logger.warning(f"LeanRAG search failed: {e}. Falling back to baseline.")
            fallback_used = True
            fallback_reason = str(e)
            backend = "baseline"

    # Baseline search (default fallback)
    result = _search_baseline_impl(
        ctx=ctx,
        threads_dir=threads_dir,
        query=query,
        **kwargs,
    )

    # Add fallback info if we had to fall back
    if fallback_used:
        try:
            result_data = json.loads(result)
            result_data["fallback_used"] = True
            result_data["fallback_reason"] = fallback_reason
            result = json.dumps(result_data, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass  # If result isn't JSON, just return as-is

    return result


def _search_baseline_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    semantic: bool = False,
    semantic_threshold: float = 0.5,
    start_time: str = "",
    end_time: str = "",
    thread_status: str = "",
    thread_topic: str = "",
    role: str = "",
    entry_type: str = "",
    agent: str = "",
    limit: int = 10,
    combine: str = "AND",
    include_threads: bool = True,
    include_entries: bool = True,
    **kwargs: Any,
) -> str:
    """Search the baseline graph (free tier).

    This is the core search implementation for baseline graph.
    """
    from watercooler.baseline_graph.search import SearchQuery, search_graph
    from watercooler.baseline_graph.reader import is_graph_available

    if not is_graph_available(threads_dir):
        return json.dumps({
            "error": "Graph not available",
            "message": "No baseline graph found. Run watercooler_baseline_graph_build first.",
            "results": [],
            "count": 0,
        })

    # Validate parameters
    limit = _validate_limit(limit, default=10)
    semantic_threshold = _validate_threshold(semantic_threshold, default=0.5)

    # Build search query (parameters already validated above)
    search_query = SearchQuery(
        query=query if query else None,
        semantic=semantic,
        semantic_threshold=semantic_threshold,
        start_time=start_time if start_time else None,
        end_time=end_time if end_time else None,
        thread_status=thread_status if thread_status else None,
        thread_topic=thread_topic if thread_topic else None,
        role=role if role else None,
        entry_type=entry_type if entry_type else None,
        agent=agent if agent else None,
        limit=limit,
        combine=combine.upper() if combine.upper() in ("AND", "OR") else "AND",
        include_threads=include_threads,
        include_entries=include_entries,
    )

    # Execute search
    results = search_graph(threads_dir, search_query)

    # Format results for JSON output
    output: Dict[str, Any] = {
        "count": results.count,
        "total_scanned": results.total_scanned,
        "backend": "baseline",
        "results": [],
    }

    for result in results.results:
        item: Dict[str, Any] = {
            "type": result.node_type,
            "id": result.node_id,
            "score": result.score,
            "matched_fields": result.matched_fields,
        }

        if result.thread:
            item["thread"] = {
                "topic": result.thread.topic,
                "title": result.thread.title,
                "status": result.thread.status,
                "ball": result.thread.ball,
                "last_updated": result.thread.last_updated,
                "entry_count": result.thread.entry_count,
                "summary": result.thread.summary,
            }

        if result.entry:
            item["entry"] = {
                "entry_id": result.entry.entry_id,
                "thread_topic": result.entry.thread_topic,
                "index": result.entry.index,
                "agent": result.entry.agent,
                "role": result.entry.role,
                "entry_type": result.entry.entry_type,
                "title": result.entry.title,
                "timestamp": result.entry.timestamp,
                "summary": result.entry.summary,
            }

        output["results"].append(item)

    return json.dumps(output, indent=2)


async def _search_graphiti_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    code_path: str = "",
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search Graphiti memory backend for facts/episodes.

    Routes to watercooler_search_memory_facts for entries search in Graphiti.
    """
    from .. import memory as mem

    config = mem.load_graphiti_config(code_path=code_path)
    if not config:
        raise RuntimeError("Graphiti backend not enabled")

    backend = mem.get_graphiti_backend(config)
    if not backend:
        raise RuntimeError("Graphiti backend unavailable")

    # Extract time filters from kwargs (passed through from route_search)
    start_time = kwargs.get("start_time", "")
    end_time = kwargs.get("end_time", "")
    has_time_filters = bool(start_time or end_time)

    # Over-fetch when time filters are active (post-filter reduces result count)
    fetch_limit = min(limit * 3, 50) if has_time_filters else limit

    # Use Graphiti's search_memory_facts for entry-level search
    # Backend methods use asyncio.run() internally, so run in thread to avoid event loop conflict
    results = await asyncio.to_thread(
        backend.search_facts,
        query=query,
        max_results=fetch_limit,
        start_time=start_time,
        end_time=end_time,
    )

    # Trim to requested limit after post-filtering
    results = results[:limit]

    output: Dict[str, Any] = {
        "count": len(results),
        "backend": "graphiti",
        "results": [
            {
                "type": "fact",
                "id": r.get("uuid", ""),
                "score": r.get("score", 0.0),
                "fact": r.get("fact", ""),
                "content": r.get("content", r.get("fact", "")),
                "name": r.get("name", ""),
                "source_node": r.get("source_node_uuid", ""),
                "target_node": r.get("target_node_uuid", ""),
            }
            for r in results
        ],
    }
    if has_time_filters:
        output["filters_applied"] = {
            "start_time": start_time or None,
            "end_time": end_time or None,
        }

    return json.dumps(output, indent=2)


async def _search_graphiti_nodes_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    code_path: str = "",
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search Graphiti for entity nodes."""
    from .. import memory as mem

    config = mem.load_graphiti_config(code_path=code_path)
    if not config:
        raise RuntimeError("Graphiti backend not enabled")

    backend = mem.get_graphiti_backend(config)
    if not backend:
        raise RuntimeError("Graphiti backend unavailable")

    # Backend methods use asyncio.run() internally, so run in thread to avoid event loop conflict
    results = await asyncio.to_thread(backend.search_nodes, query=query, max_results=limit)

    output: Dict[str, Any] = {
        "count": len(results),
        "backend": "graphiti",
        "mode": "entities",
        "results": [
            {
                "type": "entity",
                "id": r.get("uuid", ""),
                "name": r.get("name", ""),
                "labels": r.get("labels", []),
                "summary": r.get("summary", ""),
            }
            for r in results
        ],
    }

    return json.dumps(output, indent=2)


async def _search_graphiti_episodes_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    code_path: str = "",
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search Graphiti for episodes."""
    from .. import memory as mem

    config = mem.load_graphiti_config(code_path=code_path)
    if not config:
        raise RuntimeError("Graphiti backend not enabled")

    backend = mem.get_graphiti_backend(config)
    if not backend:
        raise RuntimeError("Graphiti backend unavailable")

    # Extract time filters from kwargs (passed through from route_search)
    start_time = kwargs.get("start_time", "")
    end_time = kwargs.get("end_time", "")
    has_time_filters = bool(start_time or end_time)

    # Over-fetch when time filters are active (post-filter reduces result count)
    fetch_limit = min(limit * 3, 50) if has_time_filters else limit

    # Backend methods use asyncio.run() internally, so run in thread to avoid event loop conflict
    results = await asyncio.to_thread(
        backend.get_episodes,
        query=query,
        max_episodes=fetch_limit,
        start_time=start_time,
        end_time=end_time,
    )

    # Trim to requested limit after post-filtering
    results = results[:limit]

    output: Dict[str, Any] = {
        "count": len(results),
        "backend": "graphiti",
        "mode": "episodes",
        "results": [
            {
                "type": "episode",
                "id": r.get("uuid", ""),
                "name": r.get("name", ""),
                "content": r.get("content", ""),
                "created_at": r.get("created_at", ""),
            }
            for r in results
        ],
    }
    if has_time_filters:
        output["filters_applied"] = {
            "start_time": start_time or None,
            "end_time": end_time or None,
        }

    return json.dumps(output, indent=2)


def _search_leanrag_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search LeanRAG hierarchical clusters.

    Uses the LeanRAG backend to search the hierarchical knowledge graph.
    Falls back to baseline if LeanRAG is not available or not indexed.

    Args:
        ctx: MCP context
        threads_dir: Path to threads directory
        query: Search query string
        limit: Maximum number of results
        **kwargs: Additional search parameters

    Returns:
        JSON string with search results
    """
    try:
        from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig
        from watercooler_memory.backends import QueryPayload

        # Configure backend with threads_dir as work_dir
        config = LeanRAGConfig(
            work_dir=threads_dir / "graph" / "leanrag",
            leanrag_path=Path("external/LeanRAG"),
        )

        backend = LeanRAGBackend(config)

        # Build query payload
        query_payload = QueryPayload(
            manifest_version="1.0",
            queries=[{"query": query, "limit": limit}],
        )

        # Execute query
        result = backend.query(query_payload)

        # Format results
        output = {
            "backend": "leanrag",
            "query": query,
            "result_count": len(result.results),
            "results": [],
        }

        for r in result.results:
            output["results"].append({
                "query": r.get("query", query),
                "answer": r.get("answer", ""),
                "context": r.get("context", ""),
                "topk": r.get("topk", limit),
            })

        return json.dumps(output, indent=2)

    except ImportError as e:
        # LeanRAG not available
        raise RuntimeError(f"LeanRAG backend not available: {e}")
    except Exception as e:
        # Any error triggers fallback to baseline
        raise RuntimeError(f"LeanRAG search failed: {e}")


# Module-level references to registered tools (populated by register_graph_tools)
baseline_graph_stats = None
search_graph_tool = None
find_similar_entries_tool = None
graph_health_tool = None
access_stats_tool = None


def _baseline_graph_stats_impl(
    ctx: Context,
    code_path: str = "",
) -> str:
    """Get statistics about threads for baseline graph.

    Returns thread counts, entry counts, and status breakdown.
    Useful for understanding the scope before building a baseline graph.

    Args:
        code_path: Path to code repository (for resolving threads dir).

    Returns:
        JSON with thread statistics.
    """
    try:
        from watercooler.baseline_graph import get_thread_stats

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        stats = get_thread_stats(threads_dir)
        return json.dumps(stats, indent=2)

    except Exception as e:
        return f"Error getting baseline graph stats: {str(e)}"



async def _search_graph_impl(
    ctx: Context,
    code_path: str = "",
    query: str = "",
    semantic: bool = False,
    semantic_threshold: float = 0.5,
    start_time: str = "",
    end_time: str = "",
    thread_status: str = "",
    thread_topic: str = "",
    role: str = "",
    entry_type: str = "",
    agent: str = "",
    limit: int = 10,
    combine: str = "AND",
    include_threads: bool = True,
    include_entries: bool = True,
    mode: str = "auto",
    backend: str = "auto",
) -> str:
    """Unified search across threads and entries with tier-aware routing.

    This is the primary search tool for watercooler threads and memory. It supports
    keyword search, semantic search with embeddings, time-based filtering, and
    metadata filters. Routes to the appropriate backend based on configuration.

    Mode Parameter (replaces removed tools):
        - mode="entries" (default): Search thread entries. This is the standard
          search mode for finding content in watercooler threads.
        - mode="entities": Search entity nodes extracted by Graphiti. Replaces
          the removed watercooler_search_nodes tool.
        - mode="episodes": Search episodic content from Graphiti. Replaces the
          removed watercooler_get_episodes tool.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        query: Search query (keyword or semantic depending on mode).
        semantic: If True, use semantic search with embedding cosine similarity.
            Requires embeddings to be generated. Falls back to keyword if unavailable.
        semantic_threshold: Minimum cosine similarity for semantic matches (0.0-1.0).
            Only used when semantic=True. Default: 0.5. Lower values return more results.
        start_time: Filter results after this ISO timestamp.
        end_time: Filter results before this ISO timestamp.
        thread_status: Filter threads by status (OPEN, CLOSED, etc.).
        thread_topic: Filter entries by specific thread topic.
        role: Filter entries by role (planner, implementer, etc.).
        entry_type: Filter entries by type (Note, Plan, Decision, etc.).
        agent: Filter entries by agent name (partial match).
        limit: Maximum results to return (default: 10, max: 100).
        combine: How to combine filters - "AND" or "OR" (default: AND).
        include_threads: Include thread nodes in results (default: True).
        include_entries: Include entry nodes in results (default: True).
        mode: Search mode - "auto", "entries", "entities", or "episodes".
            - auto: Infer from query (default is entries)
            - entries: Search thread entries (baseline graph or Graphiti facts)
            - entities: Search entity nodes (requires Graphiti backend). Use this
              mode instead of the removed watercooler_search_nodes tool.
            - episodes: Search episodes (requires Graphiti backend). Use this
              mode instead of the removed watercooler_get_episodes tool.
        backend: Search backend - "auto", "baseline", "graphiti", or "leanrag".
            - auto: Use WATERCOOLER_MEMORY_BACKEND env var, fallback to baseline
            - baseline: Free tier - baseline graph only
            - graphiti: Paid tier - Graphiti memory backend
            - leanrag: Paid tier - LeanRAG hierarchical clusters

    Returns:
        JSON with search results including matched nodes and metadata.

    Examples:
        # Search thread entries (default mode)
        watercooler_search(query="authentication", code_path=".")

        # Search entity nodes (replaces watercooler_search_nodes)
        watercooler_search(query="OAuth2", mode="entities", limit=10)

        # Search episodes (replaces watercooler_get_episodes)
        watercooler_search(query="implementation decisions", mode="episodes", limit=10)
    """
    try:
        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Validate parameters early (before any routing/processing)
        limit = _validate_limit(limit, default=10)
        semantic_threshold = _validate_threshold(semantic_threshold, default=0.5)

        # Resolve backend and mode
        resolved_backend = get_search_backend(backend)
        resolved_mode = infer_search_mode(mode, query, semantic)

        # Route to appropriate search implementation
        return await route_search(
            ctx=ctx,
            threads_dir=threads_dir,
            query=query,
            backend=resolved_backend,
            mode=resolved_mode,
            code_path=code_path,
            semantic=semantic,
            semantic_threshold=semantic_threshold,
            start_time=start_time,
            end_time=end_time,
            thread_status=thread_status,
            thread_topic=thread_topic,
            role=role,
            entry_type=entry_type,
            agent=agent,
            limit=limit,
            combine=combine,
            include_threads=include_threads,
            include_entries=include_entries,
        )

    except Exception as e:
        return f"Error searching graph: {str(e)}"


def _find_similar_entries_impl(
    ctx: Context,
    entry_id: str,
    code_path: str = "",
    limit: int = 5,
    similarity_threshold: float = 0.5,
    use_embeddings: bool = True,
) -> str:
    """Find entries similar to a given entry using embedding similarity.

    Uses cosine similarity with embedding vectors when available.
    Falls back to same-thread heuristic if embeddings are not available.

    Args:
        entry_id: The entry ID to find similar entries for.
        code_path: Path to code repository (for resolving threads dir).
        limit: Maximum number of similar entries to return (default: 5).
        similarity_threshold: Minimum cosine similarity (0.0-1.0, default: 0.5).
        use_embeddings: Try to use embedding similarity (default: True).

    Returns:
        JSON with similar entries and their similarity scores.
    """
    try:
        from watercooler.baseline_graph.search import find_similar_entries
        from watercooler.baseline_graph.reader import is_graph_available

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        if not is_graph_available(threads_dir):
            return json.dumps({
                "error": "Graph not available",
                "message": "No baseline graph found. Run watercooler_baseline_graph_build first.",
                "results": [],
            })

        # Validate parameters
        limit = _validate_limit(limit, default=5, max_value=50)
        similarity_threshold = _validate_threshold(similarity_threshold, default=0.5)

        # Find similar entries
        similar = find_similar_entries(
            threads_dir=threads_dir,
            entry_id=entry_id,
            limit=limit,
            use_embeddings=use_embeddings,
            similarity_threshold=similarity_threshold,
        )

        # Format results
        output = {
            "source_entry_id": entry_id,
            "count": len(similar),
            "method": "embedding_similarity" if use_embeddings else "same_thread_heuristic",
            "threshold": similarity_threshold,
            "results": [],
        }

        for entry in similar:
            output["results"].append({
                "entry_id": entry.entry_id,
                "thread_topic": entry.thread_topic,
                "title": entry.title,
                "agent": entry.agent,
                "role": entry.role,
                "timestamp": entry.timestamp,
                "summary": entry.summary,
            })

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error finding similar entries: {str(e)}"


async def _graph_health_impl(
    ctx: Context,
    code_path: str = "",
    verify_parity: bool = False,
) -> str:
    """Check graph synchronization health and report any issues.

    Reports the status of all threads in the graph:
    - Synced threads (graph matches markdown)
    - Stale threads (need sync)
    - Error threads (sync failed)
    - Pending threads (sync in progress)

    Optionally verifies data parity between graph nodes and parsed markdown:
    - entry_count: Does graph node count match actual entries in markdown?
    - last_updated: Does graph timestamp match latest entry timestamp?

    Use this to diagnose graph sync issues before running reconcile.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        verify_parity: If True, parse each thread's markdown and compare
            entry_count and last_updated against graph node values.
            This is slower but catches data accuracy issues that sync
            state alone doesn't detect.

    Returns:
        JSON health report with thread statuses and recommendations.
    """
    try:
        from watercooler.baseline_graph.sync import check_graph_health
        from watercooler.baseline_graph.reader import is_graph_available
        from dataclasses import asdict

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Check if graph exists at all
        graph_available = is_graph_available(threads_dir)

        # Get health report (with optional parity verification).
        # Run in thread to avoid blocking event loop (#128).
        health = await asyncio.to_thread(
            check_graph_health, threads_dir, verify_parity=verify_parity
        )

        output = {
            "graph_available": graph_available,
            "healthy": health.healthy,
            "total_threads": health.total_threads,
            "synced_threads": health.synced_threads,
            "stale_threads": health.stale_threads,
            "error_threads": health.error_threads,
            "pending_threads": health.pending_threads,
            "error_details": health.error_details,
            "recommendations": [],
        }

        # Add parity verification results if requested
        if verify_parity:
            output["parity_verified"] = health.parity_verified
            output["parity_mismatches"] = [
                asdict(m) for m in health.parity_mismatches
            ]

        # Add recommendations
        if not graph_available:
            output["recommendations"].append(
                "Graph not available. Run watercooler_baseline_graph_build to create it."
            )
        if health.stale_threads:
            output["recommendations"].append(
                f"{len(health.stale_threads)} threads need sync. Run watercooler_reconcile_graph."
            )
        if health.error_threads:
            output["recommendations"].append(
                f"{health.error_threads} threads have sync errors. Check error_details and run reconcile."
            )
        if health.parity_mismatches:
            count_mismatches = sum(
                1 for m in health.parity_mismatches if m.field == "entry_count"
            )
            ts_mismatches = sum(
                1 for m in health.parity_mismatches if m.field == "last_updated"
            )
            if count_mismatches:
                output["recommendations"].append(
                    f"{count_mismatches} threads have entry_count mismatches. Run watercooler_reconcile_graph."
                )
            if ts_mismatches:
                output["recommendations"].append(
                    f"{ts_mismatches} threads have last_updated mismatches. Run watercooler_reconcile_graph."
                )

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error checking graph health: {str(e)}"


def _access_stats_impl(
    ctx: Context,
    code_path: str = "",
    node_type: str = "",
    limit: int = 10,
) -> str:
    """Get access statistics from the graph odometer.

    Returns the most frequently accessed threads and entries, useful for
    understanding usage patterns and identifying popular content.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        node_type: Filter by "thread" or "entry". Empty string returns both.
        limit: Maximum number of results to return (default 10).

    Returns:
        JSON with most accessed nodes including type, id, and access count.
    """
    try:
        from watercooler.baseline_graph.reader import get_most_accessed

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Validate node_type
        filter_type = None
        if node_type:
            if node_type.lower() not in ("thread", "entry"):
                return f"Invalid node_type: {node_type}. Must be 'thread', 'entry', or empty."
            filter_type = node_type.lower()

        # Get most accessed (validate limit)
        results = get_most_accessed(
            threads_dir=threads_dir,
            node_type=filter_type,
            limit=_validate_limit(limit, default=10),
        )

        # Format output
        output = {
            "total_results": len(results),
            "filter": filter_type or "all",
            "stats": [
                {"type": t, "id": nid, "access_count": count}
                for t, nid, count in results
            ],
        }

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error getting access stats: {str(e)}"


# =============================================================================
# New Tool Suite (Fresh Suite Design)
# =============================================================================


async def _graph_enrich_impl(
    ctx: Context,
    code_path: str = "",
    summaries: bool = True,
    embeddings: bool = True,
    thread_summaries: bool = False,
    mode: str = "missing",
    topics: str = "",
    batch_size: int = 10,
    dry_run: bool = False,
) -> str:
    """Generate or regenerate summaries and embeddings.

    This is the unified enrichment tool that replaces backfill_graph with a cleaner,
    more consistent API. Use this for all enrichment operations.

    Modes:
    - "missing": Only fill missing values (default, safe)
    - "selective": Process only specified topics (force regenerate)
    - "all": Regenerate everything (global refresh, use with caution)

    Args:
        code_path: Path to code repository (for resolving threads dir).
        summaries: Whether to generate/regenerate entry summaries. Default: True.
        embeddings: Whether to generate/regenerate embeddings. Default: True.
        thread_summaries: Whether to regenerate thread summaries. When True with
            mode="missing", only generates for threads without summaries. With
            mode="selective" or mode="all", regenerates thread summaries regardless
            of existing values. Use this to force-regenerate summaries when many
            entries have been added, entry summaries have been improved, or you
            want a fresh summary reflecting current state. Default: False.
        mode: Processing mode - "missing", "selective", or "all". Default: "missing".
        topics: Comma-separated list of topics (required for "selective" mode).
        batch_size: Number of items to process before writing. Default: 10.
        dry_run: If True, return what would be processed without making changes.

    Returns:
        JSON with counts: processed, generated, skipped, errors

    Examples:
        # Fill missing embeddings only
        graph_enrich(embeddings=True, summaries=False, mode="missing")

        # Regenerate embeddings for specific topics (e.g., after dimension change)
        graph_enrich(embeddings=True, mode="selective", topics="topic-a,topic-b")

        # Full refresh of all embeddings
        graph_enrich(embeddings=True, summaries=False, mode="all")

        # Force regenerate thread summary for specific topic
        graph_enrich(thread_summaries=True, summaries=False, embeddings=False,
                     mode="selective", topics="my-topic")

        # Regenerate all thread summaries (batch refresh)
        graph_enrich(thread_summaries=True, summaries=False, embeddings=False, mode="all")
    """
    try:
        from watercooler.baseline_graph.sync import enrich_graph

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Parse topics list
        topic_list = None
        if topics:
            topic_list = [t.strip() for t in topics.split(",") if t.strip()]

        # Validate batch_size parameter
        validated_batch_size = _validate_limit(batch_size, default=10, max_value=MAX_BATCH_SIZE)

        # Define the enrich operation
        def _do_enrich() -> dict:
            result = enrich_graph(
                threads_dir=threads_dir,
                summaries=summaries,
                embeddings=embeddings,
                thread_summaries=thread_summaries,
                mode=mode,
                topics=topic_list,
                batch_size=validated_batch_size,
                dry_run=dry_run,
            )
            return result.to_dict()

        # Run in thread to avoid blocking event loop (#128).
        # Note: asyncio.to_thread() worker threads continue after timeout —
        # the operation completes in the background. This is acceptable since
        # the server survives and the work completes.
        if dry_run:
            output = await asyncio.to_thread(_do_enrich)
        else:
            # Run with full parity protocol (preflight + commit + push)
            output = await asyncio.to_thread(
                run_with_graph_sync,
                context,
                _do_enrich,
                f"graph: enrich mode={mode}",
            )

        return json.dumps(output, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error enriching graph: {str(e)}"


async def _graph_recover_impl(
    ctx: Context,
    code_path: str = "",
    mode: str = "stale",
    topics: str = "",
    generate_summaries: bool = True,
    generate_embeddings: bool = True,
    dry_run: bool = False,
) -> str:
    """Rebuild graph from markdown (emergency recovery).

    WARNING: This parses markdown to rebuild graph nodes. Use only when:
    - Graph data is corrupted or lost
    - Manual edits were made to markdown
    - Migrating from old format
    - Recovering stale/error threads

    In normal operation, the graph is the source of truth.
    This tool is the exception for recovery scenarios.

    When the memory task queue is available, recovery tasks are enqueued
    per-topic and processed asynchronously. Poll watercooler_memory_task_status
    for progress. Falls back to synchronous recovery if queue unavailable.

    Modes:
    - "stale": Recover only stale/error threads (auto-detected)
    - "selective": Recover specific topics only
    - "all": Full rebuild from all markdown (slow, destructive)

    Args:
        code_path: Path to code repository (for resolving threads dir).
        mode: Recovery mode - "stale", "selective", or "all". Default: "stale".
        topics: Comma-separated list of topics (required for "selective" mode).
        generate_summaries: Generate summaries during recovery. Default: True.
        generate_embeddings: Generate embeddings during recovery. Default: True.
        dry_run: If True, return what would be recovered without making changes.

    Returns:
        JSON with recovery results: threads recovered, entries parsed, errors
    """
    try:
        from watercooler.baseline_graph.sync import (
            recover_graph,
            resolve_recovery_targets,
        )

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Parse topics list
        topic_list = None
        if topics:
            topic_list = [t.strip() for t in topics.split(",") if t.strip()]

        # Resolve targets — may call check_graph_health() in "stale" mode,
        # so run in thread to avoid blocking event loop (#128).
        target_topics, resolve_errors = await asyncio.to_thread(
            resolve_recovery_targets, threads_dir, mode, topic_list
        )
        if resolve_errors:
            return json.dumps({"errors": resolve_errors}, indent=2)
        if not target_topics:
            return json.dumps(
                {"action": "graph_recover", "mode": mode, "message": "Nothing to recover."},
                indent=2,
            )

        # dry_run: delegate to recover_graph (no queue, no git sync)
        if dry_run:
            result = recover_graph(
                threads_dir=threads_dir,
                mode=mode,
                topics=topic_list,
                generate_summaries=generate_summaries,
                generate_embeddings=generate_embeddings,
                dry_run=True,
            )
            return json.dumps(result.to_dict(), indent=2)

        # Try queue path: enqueue one MemoryTask per topic
        queue, worker = _get_recover_queue()
        if queue is not None:
            return _enqueue_recovery_tasks(
                queue=queue,
                worker=worker,
                target_topics=target_topics,
                threads_dir=threads_dir,
                code_root=context.code_root,
                mode=mode,
                generate_summaries=generate_summaries,
                generate_embeddings=generate_embeddings,
            )

        # Fallback: synchronous recovery with git parity.
        # Run in thread to avoid blocking event loop (#128).
        # Note: asyncio.to_thread() worker threads continue after timeout —
        # the operation completes in the background. This is acceptable since
        # the server survives and the work completes.
        def _do_recover() -> dict:
            result = recover_graph(
                threads_dir=threads_dir,
                mode=mode,
                topics=topic_list,
                generate_summaries=generate_summaries,
                generate_embeddings=generate_embeddings,
                dry_run=False,
            )
            return result.to_dict()

        output = await asyncio.to_thread(
            run_with_graph_sync,
            context,
            _do_recover,
            f"graph: recover mode={mode}",
        )
        return json.dumps(output, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error recovering graph: {str(e)}"


def _get_recover_queue() -> tuple[Optional[MemoryTaskQueue], Optional[MemoryTaskWorker]]:
    """Return (queue, worker) if the memory task queue is available, else (None, None)."""
    try:
        from ..memory_queue import get_queue, get_worker

        queue = get_queue()
        worker = get_worker()
        if queue is None:
            return None, None
        return queue, worker
    except ImportError:
        return None, None


def _enqueue_recovery_tasks(
    *,
    queue: MemoryTaskQueue,
    worker: Optional[MemoryTaskWorker],
    target_topics: list[str],
    threads_dir: Path,
    code_root: Optional[Path],
    mode: str,
    generate_summaries: bool,
    generate_embeddings: bool,
) -> str:
    """Enqueue one MemoryTask per topic for async graph recovery.

    Returns JSON response with task_ids, skipped topics, and queue status.
    """
    group_id = derive_group_id(code_path=code_root) if code_root else derive_group_id(threads_dir=threads_dir)

    content_payload = json.dumps({
        "schema_version": 1,
        "threads_dir": str(threads_dir),
        "generate_summaries": generate_summaries,
        "generate_embeddings": generate_embeddings,
    })

    task_ids: list[str] = []
    enqueued_topics: list[str] = []
    skipped: list[dict[str, str]] = []

    for topic in target_topics:
        task = MemoryTask(
            backend="graph_recover",
            # Composite key prevents dedup collisions across repos/groups
            entry_id=f"{group_id}:{topic}",
            topic=topic,
            group_id=group_id,
            content=content_payload,
            title=f"graph_recover:{topic}",
            source_description=f"{threads_dir}|graph_recover|{mode}",
        )
        try:
            tid = queue.enqueue(task)
            task_ids.append(tid)
            enqueued_topics.append(topic)
        except DuplicateTaskError:
            skipped.append({"topic": topic, "reason": "duplicate"})
        except QueueFullError:
            skipped.append({"topic": topic, "reason": "queue_full"})

    # Wake worker to start processing
    if worker is not None and task_ids:
        worker.wake()

    if task_ids:
        message = (
            f"Recovery queued for {len(task_ids)} threads. "
            "Poll watercooler_memory_task_status for progress."
        )
    else:
        reasons = {s["reason"] for s in skipped}
        if reasons == {"duplicate"}:
            message = (
                f"All {len(skipped)} topics already queued or recently processed. "
                "Poll watercooler_memory_task_status for existing task progress."
            )
        else:
            message = (
                f"No tasks enqueued — {len(skipped)} topics skipped. "
                "Check 'skipped' for details."
            )

    output = {
        "action": "graph_recover",
        "mode": "queued" if task_ids else "all_skipped",
        "recovery_mode": mode,
        "tasks_enqueued": len(task_ids),
        "task_ids": task_ids,
        "topics": enqueued_topics,
        "skipped": skipped,
        "queue_status": queue.status_summary(),
        "message": message,
    }
    return json.dumps(output, indent=2)


def _graph_project_impl(
    ctx: Context,
    code_path: str = "",
    mode: str = "missing",
    topics: str = "",
    overwrite: bool = False,
    dry_run: bool = False,
) -> str:
    """Generate markdown files from graph (source of truth).

    Use this to regenerate markdown projections from graph data.
    The graph is the source of truth; this tool creates the derived markdown.

    Modes:
    - "missing": Only create markdown for topics without .md files
    - "selective": Project specific topics
    - "all": Regenerate all markdown (requires overwrite=True)

    Use cases:
    - Initial markdown generation after graph import
    - Regenerating corrupted markdown
    - Syncing after direct graph edits

    Args:
        code_path: Path to code repository (for resolving threads dir).
        mode: Processing mode - "missing", "selective", or "all". Default: "missing".
        topics: Comma-separated list of topics (required for "selective" mode).
        overwrite: Allow overwriting existing files (required for "all" mode).
        dry_run: If True, return what would be created/updated without changes.

    Returns:
        JSON with files created/updated, skipped, errors
    """
    try:
        from watercooler.baseline_graph.projector import project_graph

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Parse topics list
        topic_list = None
        if topics:
            topic_list = [t.strip() for t in topics.split(",") if t.strip()]

        # Define the project operation
        def _do_project() -> dict:
            result = project_graph(
                threads_dir=threads_dir,
                mode=mode,
                topics=topic_list,
                overwrite=overwrite,
                dry_run=dry_run,
            )
            return result.to_dict()

        # For dry_run, don't wrap in git sync
        if dry_run:
            output = _do_project()
        else:
            # Run with full parity protocol (preflight + commit + push)
            output = run_with_graph_sync(
                context,
                _do_project,
                f"graph: project mode={mode}",
            )

        return json.dumps(output, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error projecting graph: {str(e)}"


# Module-level references for new tools
graph_enrich_tool = None
graph_recover_tool = None
graph_project_tool = None


def register_graph_tools(mcp):
    """Register graph tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global baseline_graph_stats, search_graph_tool
    global find_similar_entries_tool, graph_health_tool, access_stats_tool
    global graph_enrich_tool, graph_recover_tool, graph_project_tool

    # Register tools and store references for testing
    baseline_graph_stats = mcp.tool(name="watercooler_baseline_graph_stats")(_baseline_graph_stats_impl)
    search_graph_tool = mcp.tool(name="watercooler_search")(_search_graph_impl)
    find_similar_entries_tool = mcp.tool(name="watercooler_find_similar")(_find_similar_entries_impl)
    graph_health_tool = mcp.tool(name="watercooler_graph_health")(_graph_health_impl)
    access_stats_tool = mcp.tool(name="watercooler_access_stats")(_access_stats_impl)

    # New tool suite (Fresh Suite Design)
    graph_enrich_tool = mcp.tool(name="watercooler_graph_enrich")(_graph_enrich_impl)
    graph_recover_tool = mcp.tool(name="watercooler_graph_recover")(_graph_recover_impl)
    graph_project_tool = mcp.tool(name="watercooler_graph_project")(_graph_project_impl)
