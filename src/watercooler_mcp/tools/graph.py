"""Graph tools for watercooler MCP server.

Tools:
- watercooler_baseline_graph_stats: Graph statistics
- watercooler_baseline_graph_build: Build baseline graph
- watercooler_search: Search threads and entries
- watercooler_find_similar: Find similar entries
- watercooler_graph_health: Graph sync health
- watercooler_reconcile_graph: Reconcile graph with markdown
- watercooler_access_stats: Access statistics
"""

import json
from pathlib import Path
from typing import Optional

from fastmcp import Context

from ..sync import BranchPairingError
from ..middleware import run_with_graph_sync
from .. import validation  # Import module for runtime access (enables test patching)


# Module-level references to registered tools (populated by register_graph_tools)
baseline_graph_stats = None
baseline_graph_build = None
search_graph_tool = None
find_similar_entries_tool = None
graph_health_tool = None
reconcile_graph_tool = None
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
) -> str:
    """Unified search across threads and entries in the baseline graph.

    Supports keyword search, semantic search with embeddings, time-based
    filtering, and metadata filters. All filters can be combined with AND or OR logic.

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

    Returns:
        JSON with search results including matched nodes and metadata.
    """
    try:
        from watercooler.baseline_graph.search import SearchQuery, search_graph
        from watercooler.baseline_graph.reader import is_graph_available

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Check if graph is available
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
        output = {
            "count": results.count,
            "total_scanned": results.total_scanned,
            "results": [],
        }

        for result in results.results:
            item = {
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

    Args:
        code_path: Path to code repository (for resolving threads dir).
        topics: Comma-separated list of topics to reconcile. If empty,
                reconciles all stale/error topics.
        generate_summaries: Whether to generate LLM summaries (slower).
            Defaults to config value from ~/.watercooler/config.toml.
        generate_embeddings: Whether to generate embedding vectors (slower).
            Defaults to config value from ~/.watercooler/config.toml.

    Returns:
        JSON report with reconciliation results per topic.
    """
    try:
        from watercooler.baseline_graph.sync import reconcile_graph
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
        do_summaries = generate_summaries if generate_summaries is not None else graph_config.generate_summaries
        do_embeddings = generate_embeddings if generate_embeddings is not None else graph_config.generate_embeddings

        # Parse topics list
        topic_list = None
        if topics:
            topic_list = [t.strip() for t in topics.split(",") if t.strip()]

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
    global find_similar_entries_tool, graph_health_tool, reconcile_graph_tool, access_stats_tool

    # Register tools and store references for testing
    baseline_graph_stats = mcp.tool(name="watercooler_baseline_graph_stats")(_baseline_graph_stats_impl)
    baseline_graph_build = mcp.tool(name="watercooler_baseline_graph_build")(_baseline_graph_build_impl)
    search_graph_tool = mcp.tool(name="watercooler_search")(_search_graph_impl)
    find_similar_entries_tool = mcp.tool(name="watercooler_find_similar")(_find_similar_entries_impl)
    graph_health_tool = mcp.tool(name="watercooler_graph_health")(_graph_health_impl)
    reconcile_graph_tool = mcp.tool(name="watercooler_reconcile_graph")(_reconcile_graph_impl)
    access_stats_tool = mcp.tool(name="watercooler_access_stats")(_access_stats_impl)
