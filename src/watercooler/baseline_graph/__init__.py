"""Baseline graph module for free-tier knowledge graphs.

This module provides a lightweight knowledge graph built from threads
using locally-hosted LLMs (Ollama, llama.cpp) - no API costs required.

Key components:
- summarizer: LLM-based summarization with extractive fallback
- parser: Thread parsing and entity extraction
- export: JSONL export for graph storage
"""

from .summarizer import (
    summarize_entry,
    summarize_thread,
    extractive_summary,
    SummarizerConfig,
    create_summarizer_config,
)

from .parser import (
    ParsedEntry,
    ParsedThread,
    parse_thread_file,
    iter_threads,
    parse_all_threads,
    get_thread_stats,
)

from .export import (
    export_thread_graph,
    export_all_threads,
    load_nodes,
    load_edges,
    load_graph,
)

from .reader import (
    GraphThread,
    GraphEntry,
    is_graph_available,
    get_graph_staleness,
    list_threads_from_graph,
    read_thread_from_graph,
    get_entry_from_graph,
    get_entries_range_from_graph,
    format_thread_markdown,
    format_entry_json,
    increment_access_count,
    get_access_count,
    get_most_accessed,
)

from .sync import (
    sync_entry_to_graph,
    sync_thread_to_graph,
    record_graph_sync_error,
    check_graph_health,
    reconcile_graph,
    backfill_missing,
    BackfillResult,
)

from .search import (
    SearchQuery,
    SearchResult,
    SearchResults,
    search_graph,
    search_entries,
    search_threads,
    find_similar_entries,
    search_by_time_range,
)

__all__ = [
    # Summarizer
    "summarize_entry",
    "summarize_thread",
    "extractive_summary",
    "SummarizerConfig",
    "create_summarizer_config",
    # Parser
    "ParsedEntry",
    "ParsedThread",
    "parse_thread_file",
    "iter_threads",
    "parse_all_threads",
    "get_thread_stats",
    # Export
    "export_thread_graph",
    "export_all_threads",
    "load_nodes",
    "load_edges",
    "load_graph",
    # Reader
    "GraphThread",
    "GraphEntry",
    "is_graph_available",
    "get_graph_staleness",
    "list_threads_from_graph",
    "read_thread_from_graph",
    "get_entry_from_graph",
    "get_entries_range_from_graph",
    "format_thread_markdown",
    "format_entry_json",
    # Odometer (access tracking)
    "increment_access_count",
    "get_access_count",
    "get_most_accessed",
    # Sync
    "sync_entry_to_graph",
    "sync_thread_to_graph",
    "record_graph_sync_error",
    "check_graph_health",
    "reconcile_graph",
    "backfill_missing",
    "BackfillResult",
    # Search
    "SearchQuery",
    "SearchResult",
    "SearchResults",
    "search_graph",
    "search_entries",
    "search_threads",
    "find_similar_entries",
    "search_by_time_range",
]
