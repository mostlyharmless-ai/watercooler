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


def route_search(
    ctx: Context,
    threads_dir: Path,
    query: str,
    backend: str,
    mode: str,
    **kwargs: Any,
) -> str:
    """Route search to the appropriate backend based on tier and mode.

    Args:
        ctx: MCP context
        threads_dir: Path to threads directory
        query: Search query
        backend: Resolved backend ("baseline", "graphiti", "leanrag")
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
                    return _search_graphiti_nodes_impl(
                        ctx=ctx,
                        threads_dir=threads_dir,
                        query=query,
                        **kwargs,
                    )
                else:  # episodes
                    return _search_graphiti_episodes_impl(
                        ctx=ctx,
                        threads_dir=threads_dir,
                        query=query,
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
            return _search_graphiti_impl(
                ctx=ctx,
                threads_dir=threads_dir,
                query=query,
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


def _search_graphiti_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search Graphiti memory backend for facts/episodes.

    Routes to watercooler_search_memory_facts for entries search in Graphiti.
    """
    from .. import memory as mem

    config = mem.load_graphiti_config()
    if not config:
        raise RuntimeError("Graphiti backend not enabled")

    backend = mem.get_graphiti_backend()
    if not backend:
        raise RuntimeError("Graphiti backend unavailable")

    # Use Graphiti's search_memory_facts for entry-level search
    import asyncio

    async def do_search():
        return await backend.search_facts(query=query, max_facts=limit)

    results = asyncio.get_event_loop().run_until_complete(do_search())

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


def _search_graphiti_nodes_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search Graphiti for entity nodes."""
    from .. import memory as mem

    config = mem.load_graphiti_config()
    if not config:
        raise RuntimeError("Graphiti backend not enabled")

    backend = mem.get_graphiti_backend()
    if not backend:
        raise RuntimeError("Graphiti backend unavailable")

    import asyncio

    async def do_search():
        return await backend.search_nodes(query=query, max_nodes=limit)

    results = asyncio.get_event_loop().run_until_complete(do_search())

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


def _search_graphiti_episodes_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search Graphiti for episodes."""
    from .. import memory as mem

    config = mem.load_graphiti_config()
    if not config:
        raise RuntimeError("Graphiti backend not enabled")

    backend = mem.get_graphiti_backend()
    if not backend:
        raise RuntimeError("Graphiti backend unavailable")

    import asyncio

    async def do_search():
        return await backend.get_episodes(query=query, max_episodes=limit)

    results = asyncio.get_event_loop().run_until_complete(do_search())

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


def _search_graph_impl(
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

    Supports keyword search, semantic search with embeddings, time-based
    filtering, and metadata filters. Routes to appropriate backend based on
    tier (free/paid) and mode (entries/entities/episodes).

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
            - entities: Search entity nodes (requires Graphiti)
            - episodes: Search episodes (requires Graphiti)
        backend: Search backend - "auto", "baseline", "graphiti", or "leanrag".
            - auto: Use WATERCOOLER_MEMORY_BACKEND env var, fallback to baseline
            - baseline: Free tier - baseline graph only
            - graphiti: Paid tier - Graphiti memory backend
            - leanrag: Paid tier - LeanRAG hierarchical clusters

    Returns:
        JSON with search results including matched nodes and metadata.
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
        return route_search(
            ctx=ctx,
            threads_dir=threads_dir,
            query=query,
            backend=resolved_backend,
            mode=resolved_mode,
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
) -> str:
    """Check graph synchronization health and report any issues.

    Reports the status of all threads in the graph:
    - Synced threads (graph matches markdown)
    - Stale threads (need sync)
    - Error threads (sync failed)
    - Pending threads (sync in progress)

    Use this to diagnose graph sync issues before running reconcile.

    Args:
        code_path: Path to code repository (for resolving threads dir).

    Returns:
        JSON health report with thread statuses and recommendations.
    """
    try:
        from watercooler.baseline_graph.sync import check_graph_health
        from watercooler.baseline_graph.reader import is_graph_available

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

        # Get health report
        health = check_graph_health(threads_dir)

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


def register_graph_tools(mcp):
    """Register graph tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global baseline_graph_stats, baseline_graph_build, search_graph_tool
    global find_similar_entries_tool, graph_health_tool, reconcile_graph_tool
    global backfill_graph_tool, access_stats_tool

    # Register tools and store references for testing
    baseline_graph_stats = mcp.tool(name="watercooler_baseline_graph_stats")(_baseline_graph_stats_impl)
    baseline_graph_build = mcp.tool(name="watercooler_baseline_graph_build")(_baseline_graph_build_impl)
    search_graph_tool = mcp.tool(name="watercooler_search")(_search_graph_impl)
    find_similar_entries_tool = mcp.tool(name="watercooler_find_similar")(_find_similar_entries_impl)
    graph_health_tool = mcp.tool(name="watercooler_graph_health")(_graph_health_impl)
    reconcile_graph_tool = mcp.tool(name="watercooler_reconcile_graph")(_reconcile_graph_impl)
    backfill_graph_tool = mcp.tool(name="watercooler_backfill_graph")(_backfill_graph_impl)
    access_stats_tool = mcp.tool(name="watercooler_access_stats")(_access_stats_impl)
