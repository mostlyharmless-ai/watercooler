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
    EnrichmentResult,
    enrich_graph_entry,
    sync_entry_to_graph,  # Deprecated, use enrich_graph_entry
    sync_thread_to_graph,
    record_graph_sync_error,
    check_graph_health,
    reconcile_graph,
    backfill_missing,
    BackfillResult,
    # New tool suite (Fresh Suite Design)
    EnrichResult,
    RecoverResult,
    ClearResult,
    enrich_graph,
    recover_graph,
    clear_graph,
)

from .writer import (
    ThreadData,
    EntryData,
    upsert_thread_node,
    upsert_entry_node,
    update_thread_metadata,
    delete_entry_node,
    get_thread_from_graph,
    get_entry_node_from_graph,
    get_entries_for_thread,
    get_last_entry_id,
    get_next_entry_index,
    init_thread_in_graph,
)

from .projector import (
    project_entry_to_markdown,
    project_thread_to_markdown,
    project_thread_header_only,
    write_thread_markdown,
    project_and_write_thread,
    append_entry_and_project,
    update_header_and_write,
    create_thread_file,
    # New tool suite
    ProjectResult,
    project_graph,
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
    # Sync / Enrichment
    "EnrichmentResult",
    "enrich_graph_entry",
    "sync_entry_to_graph",  # Deprecated
    "sync_thread_to_graph",
    "record_graph_sync_error",
    "check_graph_health",
    "reconcile_graph",
    "backfill_missing",
    "BackfillResult",
    # New tool suite (Fresh Suite Design)
    "EnrichResult",
    "RecoverResult",
    "ClearResult",
    "ProjectResult",
    "enrich_graph",
    "recover_graph",
    "clear_graph",
    "project_graph",
    # Writer (graph-first mutations)
    "ThreadData",
    "EntryData",
    "upsert_thread_node",
    "upsert_entry_node",
    "update_thread_metadata",
    "delete_entry_node",
    "get_thread_from_graph",
    "get_entry_node_from_graph",
    "get_entries_for_thread",
    "get_last_entry_id",
    "get_next_entry_index",
    "init_thread_in_graph",
    # Projector (graph to MD)
    "project_entry_to_markdown",
    "project_thread_to_markdown",
    "project_thread_header_only",
    "write_thread_markdown",
    "project_and_write_thread",
    "append_entry_and_project",
    "update_header_and_write",
    "create_thread_file",
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
