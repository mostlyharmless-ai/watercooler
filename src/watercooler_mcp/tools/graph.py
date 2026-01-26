"""Graph tools for watercooler MCP server.

Tools:
- watercooler_baseline_graph_stats: Graph statistics
- watercooler_baseline_graph_build: Build baseline graph
- watercooler_search: Search threads and entries (tier-aware routing)
- watercooler_find_similar: Find similar entries
- watercooler_graph_health: Graph sync health
- watercooler_reconcile_graph: Reconcile graph with markdown
- watercooler_backfill_graph: Backfill missing summaries/embeddings
- watercooler_access_stats: Access statistics
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastmcp import Context

from ..sync import BranchPairingError
from ..middleware import run_with_graph_sync
from .. import validation  # Import module for runtime access (enables test patching)

logger = logging.getLogger(__name__)


# =============================================================================
# Search Routing Helpers (Milestone 6: Tier-Aware Search Routing)
# =============================================================================


def get_search_backend(backend: str) -> str:
    """Determine which search backend to use.

    Args:
        backend: Requested backend - "auto", "baseline", "graphiti", or "leanrag"

    Returns:
        Resolved backend name: "baseline", "graphiti", or "leanrag"
    """
    # Explicit backends are respected (except unknown ones)
    if backend in ("baseline", "graphiti", "leanrag"):
        return backend

    # Auto mode: check WATERCOOLER_MEMORY_BACKEND env var
    if backend == "auto":
        memory_backend = os.environ.get("WATERCOOLER_MEMORY_BACKEND", "").lower().strip()
        if memory_backend in ("graphiti", "leanrag"):
            return memory_backend
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

    # Validate and constrain limit
    limit = max(1, min(limit, 100))

    # Build search query
    search_query = SearchQuery(
        query=query if query else None,
        semantic=semantic,
        semantic_threshold=max(0.0, min(1.0, semantic_threshold)),
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

    # Use Graphiti's search_memory_facts for entry-level search
    # Backend methods use asyncio.run() internally, so run in thread to avoid event loop conflict
    results = await asyncio.to_thread(backend.search_facts, query=query, max_facts=limit)

    output: Dict[str, Any] = {
        "count": len(results),
        "backend": "graphiti",
        "results": [
            {
                "type": "fact",
                "id": r.get("uuid", ""),
                "score": r.get("score", 0.0),
                "fact": r.get("fact", ""),
                "source_node": r.get("source_node_uuid", ""),
                "target_node": r.get("target_node_uuid", ""),
            }
            for r in results
        ],
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
    results = await asyncio.to_thread(backend.search_nodes, query=query, max_nodes=limit)

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

    # Backend methods use asyncio.run() internally, so run in thread to avoid event loop conflict
    results = await asyncio.to_thread(backend.get_episodes, query=query, max_episodes=limit)

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
baseline_graph_build = None
search_graph_tool = None
find_similar_entries_tool = None
graph_health_tool = None
reconcile_graph_tool = None
backfill_graph_tool = None
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


def _baseline_graph_build_impl(
    ctx: Context,
    code_path: str = "",
    output_dir: str = "",
    extractive_only: Optional[bool] = None,
    skip_closed: bool = False,
    generate_embeddings: Optional[bool] = None,
) -> str:
    """Build baseline graph from threads.

    DEPRECATED: This tool is deprecated. Use watercooler_graph_recover instead
    for rebuilding graph from markdown, which provides a cleaner API with modes:
    - mode="stale": Recover stale/error threads
    - mode="selective": Target specific topics
    - mode="all": Full rebuild

    This tool will continue to work but may be removed in a future version.

    Creates a lightweight knowledge graph using extractive summaries
    or local LLM. Output is JSONL format (nodes.jsonl, edges.jsonl).

    Default output is {threads_dir}/graph/baseline.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        output_dir: Output directory for graph files (optional).
        extractive_only: Use extractive summaries only (no LLM).
            Defaults to inverse of config generate_summaries from ~/.watercooler/config.toml.
        skip_closed: Skip closed threads. Default: False.
        generate_embeddings: Generate embedding vectors for entries.
            Defaults to config value from ~/.watercooler/config.toml.

    Returns:
        JSON manifest with export statistics.
    """
    try:
        from watercooler.baseline_graph import export_all_threads, SummarizerConfig
        from watercooler_mcp.config import get_watercooler_config

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Get config defaults for summary/embedding generation
        wc_config = get_watercooler_config()
        graph_config = wc_config.mcp.graph

        # Use config values if not explicitly provided
        # extractive_only is inverse of generate_summaries
        do_extractive = extractive_only if extractive_only is not None else not graph_config.generate_summaries
        do_embeddings = generate_embeddings if generate_embeddings is not None else graph_config.generate_embeddings

        # Default output to threads_dir/graph/baseline
        if output_dir:
            out_path = Path(output_dir)
        else:
            out_path = threads_dir / "graph" / "baseline"

        config = SummarizerConfig(prefer_extractive=do_extractive)

        # Define the build operation
        def _do_build() -> dict:
            return export_all_threads(
                threads_dir,
                out_path,
                config,
                skip_closed=skip_closed,
                generate_embeddings=do_embeddings,
            )

        # Run with full parity protocol (preflight + commit + push)
        manifest = run_with_graph_sync(
            context,
            _do_build,
            "graph: build baseline",
        )

        return json.dumps(manifest, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error building baseline graph: {str(e)}"


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
        limit = max(1, min(limit, 50))
        similarity_threshold = max(0.0, min(1.0, similarity_threshold))

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


def _graph_health_impl(
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

        # Get health report (with optional parity verification)
        health = check_graph_health(threads_dir, verify_parity=verify_parity)

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


def _reconcile_graph_impl(
    ctx: Context,
    code_path: str = "",
    topics: str = "",
    generate_summaries: Optional[bool] = None,
    generate_embeddings: Optional[bool] = None,
) -> str:
    """Reconcile graph with markdown files to fix sync issues.

    DEPRECATED: This tool is deprecated. Use the new tool suite instead:
    - watercooler_graph_recover: For rebuilding graph from markdown
    - watercooler_graph_enrich: For generating summaries/embeddings

    This tool will continue to work but may be removed in a future version.

    Rebuilds graph nodes and edges for threads that are stale, have errors,
    or are explicitly specified. This is the primary tool for ingesting
    legacy markdown-only threads into the graph representation.

    In hosted mode (Railway MCP), uses GitHub API to read markdown and write graph.
    In local mode, uses filesystem operations with git sync.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        topics: Comma-separated list of topics to reconcile. If empty,
                reconciles all stale/error topics (local) or all threads (hosted).
        generate_summaries: Whether to generate LLM summaries (slower).
            Defaults to config value from ~/.watercooler/config.toml.
            Note: Not supported in hosted mode.
        generate_embeddings: Whether to generate embedding vectors (slower).
            Defaults to config value from ~/.watercooler/config.toml.
            Note: Not supported in hosted mode.

    Returns:
        JSON report with reconciliation results per topic.
    """
    try:
        from watercooler.baseline_graph.sync import reconcile_graph
        from watercooler_mcp.config import get_watercooler_config
        from watercooler_mcp.hosted_ops import reconcile_graph_hosted
        from watercooler_mcp.validation import is_hosted_context

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None:
            return "Error: Unable to resolve context."

        # Parse topics list
        topic_list = None
        if topics:
            topic_list = [t.strip() for t in topics.split(",") if t.strip()]

        # =====================================================================
        # Hosted Mode Path (GitHub API)
        # =====================================================================
        if is_hosted_context(context):
            err, result = reconcile_graph_hosted(topics=topic_list)
            if err:
                return f"Error reconciling graph (hosted): {err}"

            # Note about summaries/embeddings
            if generate_summaries or generate_embeddings:
                result["warning"] = "Summary/embedding generation not supported in hosted mode"

            return json.dumps(result, indent=2)

        # =====================================================================
        # Local Mode Path (filesystem + git sync)
        # =====================================================================
        threads_dir = context.threads_dir
        if not threads_dir or not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Get config defaults for summary/embedding generation
        wc_config = get_watercooler_config()
        graph_config = wc_config.mcp.graph

        # Use config values if not explicitly provided
        do_summaries = generate_summaries if generate_summaries is not None else graph_config.generate_summaries
        do_embeddings = generate_embeddings if generate_embeddings is not None else graph_config.generate_embeddings

        # Define the reconcile operation
        def _do_reconcile() -> dict:
            return reconcile_graph(
                threads_dir=threads_dir,
                topics=topic_list,
                generate_summaries=do_summaries,
                generate_embeddings=do_embeddings,
            )

        # Run with full parity protocol (preflight + commit + push)
        results = run_with_graph_sync(
            context,
            _do_reconcile,
            f"graph: reconcile {topics or 'all'}",
        )

        # Build output
        successes = [t for t, ok in results.items() if ok]
        failures = [t for t, ok in results.items() if not ok]

        output = {
            "total_reconciled": len(results),
            "successes": len(successes),
            "failures": len(failures),
            "success_topics": successes,
            "failure_topics": failures,
        }

        return json.dumps(output, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error reconciling graph: {str(e)}"


def _backfill_graph_impl(
    ctx: Context,
    code_path: str = "",
    backfill_summaries: bool = True,
    backfill_embeddings: bool = True,
    batch_size: int = 10,
) -> str:
    """Backfill missing summaries and embeddings in existing graph nodes.

    DEPRECATED: This tool is deprecated. Use watercooler_graph_enrich instead,
    which provides a cleaner API with mode-based control:
    - mode="missing": Equivalent to this tool's behavior
    - mode="selective": Target specific topics
    - mode="all": Force regenerate everything

    This tool will continue to work but may be removed in a future version.

    Unlike reconcile_graph which syncs stale threads from markdown, this function
    updates existing graph nodes that are missing summaries or embeddings. This is
    useful after a graph build when services were unavailable, or for incremental
    enrichment of the graph.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        backfill_summaries: Generate missing thread and entry summaries.
            Requires LLM service (Ollama) to be running. Default: True.
        backfill_embeddings: Generate missing entry embeddings.
            Requires embedding service (llama.cpp) to be running. Default: True.
        batch_size: Number of items to process before writing (for progress).
            Default: 10.

    Returns:
        JSON report with counts of processed and generated items.
    """
    try:
        from watercooler.baseline_graph.sync import backfill_missing

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Define the backfill operation
        def _do_backfill() -> dict:
            result = backfill_missing(
                threads_dir=threads_dir,
                backfill_summaries=backfill_summaries,
                backfill_embeddings=backfill_embeddings,
                batch_size=max(1, min(batch_size, 100)),
            )
            return {
                "threads_processed": result.threads_processed,
                "threads_missing_summary": result.threads_missing_summary,
                "threads_summary_generated": result.threads_summary_generated,
                "entries_processed": result.entries_processed,
                "entries_missing_summary": result.entries_missing_summary,
                "entries_summary_generated": result.entries_summary_generated,
                "entries_missing_embedding": result.entries_missing_embedding,
                "entries_embedding_generated": result.entries_embedding_generated,
                "errors": result.errors[:10],  # Limit errors in output
                "error_count": len(result.errors),
            }

        # Run with full parity protocol (preflight + commit + push)
        output = run_with_graph_sync(
            context,
            _do_backfill,
            "graph: backfill summaries/embeddings",
        )

        return json.dumps(output, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error backfilling graph: {str(e)}"


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

        # Get most accessed
        results = get_most_accessed(
            threads_dir=threads_dir,
            node_type=filter_type,
            limit=max(1, min(limit, 100)),  # Clamp to 1-100
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


def _graph_enrich_impl(
    ctx: Context,
    code_path: str = "",
    summaries: bool = True,
    embeddings: bool = True,
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
        summaries: Whether to generate/regenerate summaries. Default: True.
        embeddings: Whether to generate/regenerate embeddings. Default: True.
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

        # Define the enrich operation
        def _do_enrich() -> dict:
            result = enrich_graph(
                threads_dir=threads_dir,
                summaries=summaries,
                embeddings=embeddings,
                mode=mode,
                topics=topic_list,
                batch_size=max(1, min(batch_size, 100)),
                dry_run=dry_run,
            )
            return result.to_dict()

        # For dry_run, don't wrap in git sync
        if dry_run:
            output = _do_enrich()
        else:
            # Run with full parity protocol (preflight + commit + push)
            output = run_with_graph_sync(
                context,
                _do_enrich,
                f"graph: enrich mode={mode}",
            )

        return json.dumps(output, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error enriching graph: {str(e)}"


def _graph_recover_impl(
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
        from watercooler.baseline_graph.sync import recover_graph

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

        # Define the recover operation
        def _do_recover() -> dict:
            result = recover_graph(
                threads_dir=threads_dir,
                mode=mode,
                topics=topic_list,
                generate_summaries=generate_summaries,
                generate_embeddings=generate_embeddings,
                dry_run=dry_run,
            )
            return result.to_dict()

        # For dry_run, don't wrap in git sync
        if dry_run:
            output = _do_recover()
        else:
            # Run with full parity protocol (preflight + commit + push)
            output = run_with_graph_sync(
                context,
                _do_recover,
                f"graph: recover mode={mode}",
            )

        return json.dumps(output, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error recovering graph: {str(e)}"


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


def _graph_clear_impl(
    ctx: Context,
    code_path: str = "",
    topics: str = "",
    confirm: bool = False,
    dry_run: bool = False,
) -> str:
    """Clear graph data for specific topics.

    WARNING: Destructive operation. Removes graph nodes/edges.
    Markdown files are NOT affected.

    Requires explicit topics - no "all" mode for safety.
    Use with graph_recover to rebuild from markdown after clearing.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        topics: Comma-separated list of topics to clear (required).
        confirm: Must be True to execute destructive operation. Default: False.
        dry_run: If True, return what would be cleared without changes.

    Returns:
        JSON with topics cleared, entries removed
    """
    try:
        from watercooler.baseline_graph.sync import clear_graph

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Parse topics list
        if not topics:
            return json.dumps({
                "error": "Topics list is required (no 'all' mode for safety)",
                "topics_cleared": 0,
                "entries_removed": 0,
            }, indent=2)

        topic_list = [t.strip() for t in topics.split(",") if t.strip()]

        # Define the clear operation
        def _do_clear() -> dict:
            result = clear_graph(
                threads_dir=threads_dir,
                topics=topic_list,
                confirm=confirm,
                dry_run=dry_run,
            )
            return result.to_dict()

        # For dry_run, don't wrap in git sync
        if dry_run:
            output = _do_clear()
        else:
            # Run with full parity protocol (preflight + commit + push)
            output = run_with_graph_sync(
                context,
                _do_clear,
                f"graph: clear topics={topics}",
            )

        return json.dumps(output, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error clearing graph: {str(e)}"


# Module-level references for new tools
graph_enrich_tool = None
graph_recover_tool = None
graph_project_tool = None
graph_clear_tool = None


def register_graph_tools(mcp):
    """Register graph tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global baseline_graph_stats, baseline_graph_build, search_graph_tool
    global find_similar_entries_tool, graph_health_tool, reconcile_graph_tool
    global backfill_graph_tool, access_stats_tool
    global graph_enrich_tool, graph_recover_tool, graph_project_tool, graph_clear_tool

    # Register tools and store references for testing
    baseline_graph_stats = mcp.tool(name="watercooler_baseline_graph_stats")(_baseline_graph_stats_impl)
    baseline_graph_build = mcp.tool(name="watercooler_baseline_graph_build")(_baseline_graph_build_impl)
    search_graph_tool = mcp.tool(name="watercooler_search")(_search_graph_impl)
    find_similar_entries_tool = mcp.tool(name="watercooler_find_similar")(_find_similar_entries_impl)
    graph_health_tool = mcp.tool(name="watercooler_graph_health")(_graph_health_impl)

    # Legacy tools (deprecated, but kept working)
    reconcile_graph_tool = mcp.tool(name="watercooler_reconcile_graph")(_reconcile_graph_impl)
    backfill_graph_tool = mcp.tool(name="watercooler_backfill_graph")(_backfill_graph_impl)

    access_stats_tool = mcp.tool(name="watercooler_access_stats")(_access_stats_impl)

    # New tool suite (Fresh Suite Design)
    graph_enrich_tool = mcp.tool(name="watercooler_graph_enrich")(_graph_enrich_impl)
    graph_recover_tool = mcp.tool(name="watercooler_graph_recover")(_graph_recover_impl)
    graph_project_tool = mcp.tool(name="watercooler_graph_project")(_graph_project_impl)
    graph_clear_tool = mcp.tool(name="watercooler_graph_clear")(_graph_clear_impl)
