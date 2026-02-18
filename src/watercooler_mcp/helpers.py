"""Shared helper functions and constants for watercooler MCP server.

This module contains:
- Startup warnings system
- Context validation helpers
- Thread parsing and metadata extraction
- Entry loading and formatting
- Graph-canonical read helpers
- Commit footer building

These are extracted from server.py for modularity and testability.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

# Local application imports
from watercooler import commands, fs
from watercooler.config_facade import config
from watercooler.thread_entries import ThreadEntry, parse_thread_entries
from watercooler.baseline_graph.reader import (
    is_graph_available,
    list_threads_from_graph,
    read_thread_from_graph,
    get_entries_range_from_graph,
    increment_access_count,
    GraphEntry,
)
from .config import (
    ThreadContext,
    resolve_thread_context,
)
from .observability import log_debug


# ============================================================================
# Constants
# ============================================================================

_ALLOWED_FORMATS = {"markdown", "json"}

# Resource limits to prevent exhaustion
_MAX_LIMIT = 1000  # Maximum entries that can be requested in a single call
_MAX_OFFSET = 100000  # Maximum offset to prevent excessive memory usage

# Regex patterns for extracting thread metadata from content
_TITLE_RE = re.compile(r"^#\s*(?P<val>.+)$", re.MULTILINE)
_STAT_RE = re.compile(r"^Status:\s*(?P<val>.+)$", re.IGNORECASE | re.MULTILINE)
_BALL_RE = re.compile(r"^Ball:\s*(?P<val>.+)$", re.IGNORECASE | re.MULTILINE)
_ENTRY_RE = re.compile(
    r"^Entry:\s*(?P<who>.+?)\s+(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*$",
    re.MULTILINE,
)
_CLOSED_STATES = {"done", "closed", "merged", "resolved", "abandoned", "obsolete"}


# ============================================================================
# Startup Warnings System
# ============================================================================
# Store warnings at startup (missing config, unavailable services, etc.)
# These are surfaced in tool responses on first invocation, not stderr.

_startup_warnings: List[str] = []
_warnings_shown: bool = False


def _add_startup_warning(msg: str) -> None:
    """Add a warning message to be shown on first tool invocation."""
    global _startup_warnings
    if msg and msg not in _startup_warnings:
        _startup_warnings.append(msg)


def _get_startup_warnings() -> List[str]:
    """Get pending startup warnings and mark them as shown."""
    global _warnings_shown, _startup_warnings
    if _warnings_shown:
        return []
    _warnings_shown = True
    return list(_startup_warnings)


def _format_warnings_for_response(response: str) -> str:
    """Append any pending startup warnings to a tool response."""
    warnings = _get_startup_warnings()
    if not warnings:
        return response

    warning_block = "\n\n" + "─" * 60 + "\n"
    warning_block += "⚠️  Setup Notices:\n"
    for warning in warnings:
        # Indent each line of the warning
        indented = "\n".join("   " + line for line in warning.strip().split("\n"))
        warning_block += f"\n{indented}\n"
    warning_block += "─" * 60

    return response + warning_block


# ============================================================================
# Configuration Helpers
# ============================================================================


def _should_auto_branch() -> bool:
    return config.env.get_bool("WATERCOOLER_AUTO_BRANCH", True)


# ============================================================================
# Context Resolution Helpers (re-exported from validation.py)
# ============================================================================
# These functions are now defined in validation.py to break circular imports.
# They are re-exported here for backward compatibility.

from .validation import (
    _require_context,
    _dynamic_context_missing,
    _validate_thread_context,
)


# _refresh_threads is now in validation.py - re-export for backward compatibility
from .validation import _refresh_threads  # noqa: F401 (re-export)


# ============================================================================
# Thread Parsing Helpers
# ============================================================================


def _normalize_status(s: str) -> str:
    """Normalize status string to lowercase."""
    return s.strip().lower()


def _extract_thread_metadata_from_md(content: str, topic: str) -> tuple[str, str, str, str]:
    """Extract thread metadata from markdown content string.

    This MD-parsing version is needed for hosted mode where we only have
    GitHub API content, and as a fallback when graph data is unavailable.

    Args:
        content: Full thread markdown content
        topic: Thread topic (used as fallback for title)

    Returns:
        Tuple of (title, status, ball, last_entry_timestamp)
    """
    title_match = _TITLE_RE.search(content)
    title = title_match.group("val").strip() if title_match else topic

    status_match = _STAT_RE.search(content)
    status = _normalize_status(status_match.group("val") if status_match else "open")

    ball_match = _BALL_RE.search(content)
    ball = ball_match.group("val").strip() if ball_match else "unknown"

    # Extract last entry timestamp
    hits = list(_ENTRY_RE.finditer(content))
    last = hits[-1].group("ts").strip() if hits else fs.utcnow_iso()

    return title, status, ball, last


def _get_thread_metadata(
    threads_dir: Path, topic: str, content: str | None = None
) -> tuple[str, str, str, str]:
    """Get thread metadata from canonical graph.

    Reads from canonical graph JSONL. Returns defaults if graph data
    is unavailable (no markdown fallback).

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        content: Ignored (kept for call-site compat, will be removed)

    Returns:
        Tuple of (title, status, ball, last_entry_timestamp)
    """
    if _use_graph_for_reads(threads_dir):
        try:
            result = read_thread_from_graph(threads_dir, topic)
            if result:
                graph_thread, graph_entries = result
                last_ts = (
                    graph_entries[-1].timestamp
                    if graph_entries
                    else graph_thread.last_updated
                )
                return (
                    graph_thread.title,
                    _normalize_status(graph_thread.status),
                    graph_thread.ball,
                    last_ts,
                )
        except Exception as e:
            log_debug(f"[GRAPH] Failed to get metadata from graph: {e}")

    # No graph or thread not in graph — return defaults
    return topic, "open", "unknown", fs.utcnow_iso()


def _resolve_format(
    value: str | None, *, default: str = "markdown"
) -> tuple[str | None, str]:
    fmt = (value or "").strip().lower()
    if not fmt:
        return (None, default)
    if fmt not in _ALLOWED_FORMATS:
        allowed = ", ".join(sorted(_ALLOWED_FORMATS))
        return (
            f"Error: unsupported format '{value}'. Allowed formats: {allowed}.",
            default,
        )
    return (None, fmt)


# ============================================================================
# Entry Loading Helpers
# ============================================================================


def _load_entries_from_md(
    topic: str, context: ThreadContext
) -> tuple[str | None, list[ThreadEntry]]:
    """Load and parse thread entries from markdown on disk.

    Thread Safety Note:
        This function performs unlocked reads. This is safe because:
        - Write operations (say, ack, handoff) use AdvisoryLock for serialization
        - Reads may see partially written entries, but won't corrupt existing ones
        - Thread entry boundaries (---) ensure partial writes don't break parsing
        - File system guarantees atomic writes at the block level
        - MCP tool calls are typically infrequent enough that read/write races are rare

    For high-concurrency scenarios, consider adding shared/exclusive locking
    or caching with mtime-based invalidation.
    """
    threads_dir = context.threads_dir
    thread_path = fs.thread_path(topic, threads_dir)

    if not thread_path.exists():
        if threads_dir.exists():
            available_list = [p.stem for p in fs.discover_thread_files(threads_dir)]
            if len(available_list) > 10:
                available = (
                    ", ".join(available_list[:10])
                    + f" (and {len(available_list) - 10} more)"
                )
            else:
                available = ", ".join(available_list) if available_list else "none"
        else:
            available = "none"
        return (
            f"Error: Thread '{topic}' not found in {threads_dir}\n\n"
            f"Available threads: {available}",
            [],
        )

    content = fs.read_body(thread_path)
    entries = parse_thread_entries(content)
    return (None, entries)


def _entry_header_payload(entry: ThreadEntry, summary: str = "") -> Dict[str, object]:
    return {
        "index": entry.index,
        "entry_id": entry.entry_id,
        "agent": entry.agent,
        "timestamp": entry.timestamp,
        "role": entry.role,
        "type": entry.entry_type,
        "title": entry.title,
        "summary": summary,
    }


def _entry_full_payload(entry: ThreadEntry, summary: str = "") -> Dict[str, object]:
    """Convert ThreadEntry to full JSON payload including body content.

    Args:
        entry: ThreadEntry to convert
        summary: LLM-generated summary (1-2 sentences) from graph

    Returns:
        Dictionary with entry metadata, summary, and body
    """
    data = _entry_header_payload(entry, summary=summary)
    data["body"] = entry.body
    return data


# ============================================================================
# Graph-Canonical Read Helpers
# ============================================================================


def _use_graph_for_reads(threads_dir: Path) -> bool:
    """Check if graph should be used for read operations.

    Graph is always the source of truth when available. Returns False only
    when no graph directory exists (e.g. a brand-new repo without a graph).
    """
    return is_graph_available(threads_dir)


def _track_access(threads_dir: Path, node_type: str, node_id: str) -> None:
    """Safely track access to a node (thread or entry).

    This is a non-blocking operation - errors are logged but don't fail the read.
    Only tracks if graph features are enabled.

    Args:
        threads_dir: Threads directory
        node_type: "thread" or "entry"
        node_id: Topic (for threads) or entry_id (for entries)
    """
    # TODO: Counter writes disabled - they dirty the tree and block auto-sync.
    # See thread: graph-access-counters-sync-strategy for design discussion.
    # Re-enable once per-system counter files or deferred writes are implemented.
    return
    if not _use_graph_for_reads(threads_dir):
        return
    try:
        increment_access_count(threads_dir, node_type, node_id)
    except Exception as e:
        log_debug(f"[ODOMETER] Failed to track {node_type}:{node_id} access: {e}")


def _graph_entry_to_thread_entry(
    graph_entry: GraphEntry, full_body: str | None = None
) -> ThreadEntry:
    """Convert GraphEntry to ThreadEntry for compatibility with existing code.

    Args:
        graph_entry: Entry from graph
        full_body: Optional full body if retrieved from markdown
    """
    # Build header line in expected format
    header = f"Entry: {graph_entry.agent} {graph_entry.timestamp}\n"
    header += f"Role: {graph_entry.role}\n"
    header += f"Type: {graph_entry.entry_type}\n"
    header += f"Title: {graph_entry.title}"

    body = full_body if full_body else graph_entry.body or graph_entry.summary or ""

    return ThreadEntry(
        index=graph_entry.index,
        header=header,
        body=body,
        agent=graph_entry.agent,
        timestamp=graph_entry.timestamp,
        role=graph_entry.role,
        entry_type=graph_entry.entry_type,
        title=graph_entry.title,
        entry_id=graph_entry.entry_id,
        start_line=0,
        end_line=0,
        start_offset=0,
        end_offset=0,
    )


def _load_entries(
    topic: str,
    context: ThreadContext,
    code_branch: str | None = None,
) -> tuple[str | None, list[ThreadEntry], dict[str, str]]:
    """Load thread entries from canonical graph JSONL.

    Graph is the sole source of truth. Bodies are read directly from graph
    entries. If a graph entry lacks a body, its summary is used as fallback
    body text.

    If graph is unavailable (no graph dir), returns an error — no silent
    markdown fallback.

    Args:
        topic: Thread topic
        context: Thread context
        code_branch: Optional branch filter

    Returns:
        Tuple of (error_message, entries, summaries). Error is None on success.
        summaries maps entry_id → LLM-generated summary string.
    """
    threads_dir = context.threads_dir

    if not _use_graph_for_reads(threads_dir):
        return (
            f"Error: Graph data not available for '{topic}'. "
            "Run `watercooler graph-recover` to rebuild the graph from markdown.",
            [],
            {},
        )

    try:
        result = read_thread_from_graph(threads_dir, topic, code_branch=code_branch)
        if not result:
            return (
                f"Error: Thread '{topic}' not found in graph.",
                [],
                {},
            )

        graph_thread, graph_entries = result

        # Extract summaries
        summaries: dict[str, str] = {
            ge.entry_id: ge.summary
            for ge in graph_entries
            if ge.entry_id and ge.summary
        }

        # Convert graph entries to ThreadEntry objects (uses graph body directly)
        entries = [_graph_entry_to_thread_entry(ge) for ge in graph_entries]
        log_debug(
            f"[GRAPH] Loaded {len(entries)} entries from graph for {topic}"
        )
        return (None, entries, summaries)

    except Exception as e:
        log_debug(f"[GRAPH] Failed to load entries from graph: {e}")
        return (
            f"Error: Failed to read thread '{topic}' from graph: {e}",
            [],
            {},
        )


def _list_threads(
    threads_dir: Path,
    open_only: bool | None = None,
    agent: str | None = None,
) -> list[tuple[str, str, str, str, Path, bool, str, int]]:
    """List threads from canonical graph JSONL.

    Graph is the sole source of truth. No markdown fallback.

    Args:
        threads_dir: Threads directory
        open_only: Filter by status
        agent: Current agent name (optional). If provided, used to compute is_new
            flag based on whether the ball is held by someone else.

    Returns:
        List of thread tuples (title, status, ball, updated, path, is_new, summary, entry_count)
    """
    if not _use_graph_for_reads(threads_dir):
        log_debug("[GRAPH] Graph not available for list_threads")
        return []

    try:
        graph_threads = list_threads_from_graph(threads_dir, open_only)
        if not graph_threads:
            log_debug("[GRAPH] No threads in graph")
            return []

        result = []
        for gt in graph_threads:
            thread_file = fs.thread_path(gt.topic, threads_dir)
            # Compute is_new heuristic
            if agent and gt.ball:
                ball_lower = gt.ball.lower()
                agent_lower = agent.lower()
                is_new = agent_lower not in ball_lower
            else:
                is_new = False
            result.append(
                (
                    gt.title,
                    gt.status,
                    gt.ball,
                    gt.last_updated,
                    thread_file,
                    is_new,
                    gt.summary or "",
                    gt.entry_count,
                )
            )
        log_debug(f"[GRAPH] Listed {len(result)} threads from graph")
        return result

    except Exception as e:
        log_debug(f"[GRAPH] Failed to list from graph: {e}")
        return []


def _get_thread_summary(threads_dir: Path, topic: str) -> str:
    """Get thread summary from graph. Returns '' if unavailable."""
    if _use_graph_for_reads(threads_dir):
        try:
            result = read_thread_from_graph(threads_dir, topic)
            if result:
                graph_thread, _ = result
                return graph_thread.summary or ""
        except Exception as e:
            log_debug(f"[GRAPH] Failed to get thread summary: {e}")
    return ""


def _scan_thread_entries(
    threads_dir: Path,
    topics: list[str],
) -> dict[str, list[GraphEntry]]:
    """Load entry summaries for multiple threads in one pass (graph-only).

    Returns dict mapping topic → list[GraphEntry]. Topics without graph data
    are omitted (empty dict entry).

    Note: Loads all entries for every topic into memory. For repos with many
    threads and deep history, consider limiting the topics list via the
    ``open_only`` / ``limit`` filters on ``list_threads`` before calling scan.
    Each GraphEntry is lightweight (summary + metadata, no bodies), so typical
    repos (< 100 threads, < 50 entries each) stay well under 10 MB.
    """
    result: dict[str, list[GraphEntry]] = {}
    if not _use_graph_for_reads(threads_dir):
        return result
    for topic in topics:
        try:
            entries = get_entries_range_from_graph(threads_dir, topic)
            result[topic] = entries
        except Exception as e:
            log_debug(f"[GRAPH] Failed to scan entries for '{topic}': {e}")
            result[topic] = []
    return result


# ============================================================================
# Commit Footer Helpers
# ============================================================================


def _build_commit_footers(
    context: ThreadContext,
    *,
    topic: str | None = None,
    entry_id: str | None = None,
    agent_spec: str | None = None,
) -> list[str]:
    footers: list[str] = []
    if entry_id:
        footers.append(f"Watercooler-Entry-ID: {entry_id}")
    if topic:
        footers.append(f"Watercooler-Topic: {topic}")
    if context.code_repo:
        footers.append(f"Code-Repo: {context.code_repo}")
    if context.code_branch:
        footers.append(f"Code-Branch: {context.code_branch}")
    if context.code_commit:
        footers.append(f"Code-Commit: {context.code_commit}")
    if agent_spec:
        footers.append(f"Spec: {agent_spec}")
    return footers
