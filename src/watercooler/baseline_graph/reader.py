"""Graph reader module for MCP read operations.

This module provides functions to read thread/entry data from the baseline graph
instead of parsing markdown files directly. Falls back to markdown parsing when
graph data is unavailable.

Key functions:
- list_threads_from_graph(): List threads from graph with metadata
- read_thread_from_graph(): Read full thread with entries from graph
- get_entry_from_graph(): Get specific entry by ID or index
- is_graph_available(): Check if graph data exists and is usable
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import storage

logger = logging.getLogger(__name__)


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class GraphThread:
    """Thread data from graph."""

    topic: str
    title: str
    status: str
    ball: str
    last_updated: str
    summary: str
    entry_count: int
    access_count: int = 0


@dataclass
class GraphEntry:
    """Entry data from graph."""

    entry_id: str
    thread_topic: str
    index: int
    agent: str
    role: str
    entry_type: str
    title: str
    timestamp: str
    summary: str
    body: Optional[str] = None  # Body may not be stored in graph
    file_refs: List[str] = None
    pr_refs: List[str] = None
    commit_refs: List[str] = None
    access_count: int = 0

    def __post_init__(self):
        if self.file_refs is None:
            self.file_refs = []
        if self.pr_refs is None:
            self.pr_refs = []
        if self.commit_refs is None:
            self.commit_refs = []


# ============================================================================
# Graph Availability (delegated to storage)
# ============================================================================

# Re-export from storage for backward compatibility
get_graph_dir = storage.get_graph_dir
get_thread_graph_dir = storage.get_thread_graph_dir
is_graph_available = storage.is_graph_available


def get_graph_staleness(threads_dir: Path) -> Optional[float]:
    """Get how stale the graph is in seconds.

    Compares manifest last_updated to now.

    Args:
        threads_dir: Threads directory

    Returns:
        Seconds since last graph update, or None if unknown
    """
    graph_dir = get_graph_dir(threads_dir)
    manifest_file = graph_dir / "manifest.json"

    if not manifest_file.exists():
        return None

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        last_updated = manifest.get("last_updated")
        if last_updated:
            last_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            now = datetime.now(last_dt.tzinfo)
            return (now - last_dt).total_seconds()
    except Exception:
        pass

    return None


# ============================================================================
# Graph Loading (delegated to storage)
# ============================================================================


def _node_to_thread(node: Dict[str, Any]) -> GraphThread:
    """Convert node dict to GraphThread."""
    return GraphThread(
        topic=node.get("topic", ""),
        title=node.get("title", ""),
        status=node.get("status", "OPEN"),
        ball=node.get("ball", ""),
        last_updated=node.get("last_updated", ""),
        summary=node.get("summary", ""),
        entry_count=node.get("entry_count", 0),
        access_count=node.get("access_count", 0),
    )


def _node_to_entry(node: Dict[str, Any]) -> GraphEntry:
    """Convert node dict to GraphEntry."""
    return GraphEntry(
        entry_id=node.get("entry_id", ""),
        thread_topic=node.get("thread_topic", ""),
        index=node.get("index", 0),
        agent=node.get("agent", ""),
        role=node.get("role", ""),
        entry_type=node.get("entry_type", "Note"),
        title=node.get("title", ""),
        timestamp=node.get("timestamp", ""),
        summary=node.get("summary", ""),
        body=node.get("body"),  # May not be present
        file_refs=node.get("file_refs", []),
        pr_refs=node.get("pr_refs", []),
        commit_refs=node.get("commit_refs", []),
        access_count=node.get("access_count", 0),
    )


# ============================================================================
# Read Operations
# ============================================================================


def list_threads_from_graph(
    threads_dir: Path,
    open_only: Optional[bool] = None,
) -> List[GraphThread]:
    """List threads from graph.

    Uses per-thread format: iterates through graph/baseline/threads/*/meta.json

    Args:
        threads_dir: Threads directory
        open_only: Filter by status (True=OPEN only, False=CLOSED only, None=all)

    Returns:
        List of GraphThread objects sorted by last_updated descending
    """
    graph_dir = get_graph_dir(threads_dir)
    threads = []

    # Iterate through per-thread directories
    for topic in storage.list_thread_topics(graph_dir):
        meta = storage.load_thread_meta(graph_dir, topic)
        if not meta:
            continue

        thread = _node_to_thread(meta)

        # Apply status filter
        if open_only is True and thread.status.upper() != "OPEN":
            continue
        if open_only is False and thread.status.upper() == "OPEN":
            continue

        threads.append(thread)

    # Sort by last_updated descending
    threads.sort(key=lambda t: t.last_updated or "", reverse=True)

    return threads


def read_thread_from_graph(
    threads_dir: Path,
    topic: str,
) -> Optional[Tuple[GraphThread, List[GraphEntry]]]:
    """Read thread with all entries from graph.

    Uses per-thread format: reads from graph/baseline/threads/<topic>/

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        Tuple of (thread, entries) or None if not found
    """
    graph_dir = get_graph_dir(threads_dir)

    # Load thread meta
    meta = storage.load_thread_meta(graph_dir, topic)
    if not meta:
        return None

    thread = _node_to_thread(meta)

    # Load entries
    entries: List[GraphEntry] = []
    for node in storage.load_thread_entries(graph_dir, topic):
        entries.append(_node_to_entry(node))

    # Sort entries by index
    entries.sort(key=lambda e: e.index)

    return thread, entries


def get_entry_from_graph(
    threads_dir: Path,
    topic: str,
    entry_id: Optional[str] = None,
    index: Optional[int] = None,
) -> Optional[GraphEntry]:
    """Get specific entry from graph.

    Uses per-thread format: reads from graph/baseline/threads/<topic>/entries.jsonl

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        entry_id: Entry ID (ULID)
        index: Entry index (0-based)

    Returns:
        GraphEntry or None if not found
    """
    if entry_id is None and index is None:
        return None

    graph_dir = get_graph_dir(threads_dir)

    for node in storage.load_thread_entries(graph_dir, topic):
        if entry_id and node.get("entry_id") == entry_id:
            return _node_to_entry(node)
        if index is not None and node.get("index") == index:
            return _node_to_entry(node)

    return None


def get_entries_range_from_graph(
    threads_dir: Path,
    topic: str,
    start_index: int = 0,
    end_index: Optional[int] = None,
) -> List[GraphEntry]:
    """Get range of entries from graph.

    Uses per-thread format: reads from graph/baseline/threads/<topic>/entries.jsonl

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        start_index: Starting index (inclusive)
        end_index: Ending index (inclusive), or None for all

    Returns:
        List of GraphEntry objects in index order
    """
    graph_dir = get_graph_dir(threads_dir)
    entries = []

    for node in storage.load_thread_entries(graph_dir, topic):
        idx = node.get("index", 0)
        if idx < start_index:
            continue
        if end_index is not None and idx > end_index:
            continue

        entries.append(_node_to_entry(node))

    # Sort by index
    entries.sort(key=lambda e: e.index)

    return entries


# ============================================================================
# Format Conversion
# ============================================================================


def thread_to_list_tuple(
    thread: GraphThread,
    path: Path,
    is_new: bool = False,
) -> Tuple[str, str, str, str, Path, bool]:
    """Convert GraphThread to tuple format expected by commands.list_threads.

    Returns:
        Tuple of (title, status, ball, updated, path, is_new)
    """
    return (
        thread.title,
        thread.status,
        thread.ball,
        thread.last_updated,
        path,
        is_new,
    )


def format_thread_markdown(
    thread: GraphThread,
    entries: List[GraphEntry],
) -> str:
    """Format thread and entries as markdown.

    Args:
        thread: Thread metadata
        entries: List of entries

    Returns:
        Markdown formatted string
    """
    lines = []

    # Header
    lines.append(f"# {thread.topic} — Thread")
    lines.append(f"Status: {thread.status}")
    lines.append(f"Ball: {thread.ball}")
    lines.append(f"Topic: {thread.topic}")
    lines.append(f"Created: {entries[0].timestamp if entries else 'Unknown'}")
    lines.append("")

    # Entries
    for entry in entries:
        lines.append("---")
        lines.append(f"Entry: {entry.agent} {entry.timestamp}")
        lines.append(f"Role: {entry.role}")
        lines.append(f"Type: {entry.entry_type}")
        lines.append(f"Title: {entry.title}")
        lines.append("")

        if entry.body:
            lines.append(entry.body)
        elif entry.summary:
            lines.append(f"*[Summary: {entry.summary}]*")

        if entry.entry_id:
            lines.append(f"<!-- Entry-ID: {entry.entry_id} -->")
        lines.append("")

    return "\n".join(lines)


def format_entry_json(entry: GraphEntry) -> Dict[str, Any]:
    """Format entry as JSON-serializable dict.

    Args:
        entry: GraphEntry object

    Returns:
        Dict ready for JSON serialization
    """
    return {
        "entry_id": entry.entry_id,
        "thread_topic": entry.thread_topic,
        "index": entry.index,
        "agent": entry.agent,
        "role": entry.role,
        "entry_type": entry.entry_type,
        "title": entry.title,
        "timestamp": entry.timestamp,
        "summary": entry.summary,
        "body": entry.body,
        "file_refs": entry.file_refs,
        "pr_refs": entry.pr_refs,
        "commit_refs": entry.commit_refs,
        "access_count": entry.access_count,
    }


# ============================================================================
# Odometer (Access Tracking)
# ============================================================================


def _get_counters_file(threads_dir: Path) -> Path:
    """Get path to access counters file."""
    return get_graph_dir(threads_dir) / "counters.json"


def _load_counters(threads_dir: Path) -> Dict[str, int]:
    """Load access counters from file.

    Returns:
        Dict mapping node_id to access_count
    """
    counters_file = _get_counters_file(threads_dir)
    if not counters_file.exists():
        return {}

    try:
        return json.loads(counters_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_counters(threads_dir: Path, counters: Dict[str, int]) -> None:
    """Save access counters to file atomically."""
    counters_file = _get_counters_file(threads_dir)
    counters_file.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write via temp file
    temp_file = counters_file.with_suffix(".tmp")
    try:
        temp_file.write_text(json.dumps(counters, indent=2), encoding="utf-8")
        temp_file.replace(counters_file)
    except Exception:
        if temp_file.exists():
            temp_file.unlink()
        raise


def increment_access_count(
    threads_dir: Path,
    node_type: str,
    node_id: str,
) -> int:
    """Increment access count for a node.

    Args:
        threads_dir: Threads directory
        node_type: "thread" or "entry"
        node_id: Topic (for threads) or entry_id (for entries)

    Returns:
        New access count
    """
    key = f"{node_type}:{node_id}"
    counters = _load_counters(threads_dir)
    counters[key] = counters.get(key, 0) + 1
    _save_counters(threads_dir, counters)
    return counters[key]


def get_access_count(
    threads_dir: Path,
    node_type: str,
    node_id: str,
) -> int:
    """Get access count for a node.

    Args:
        threads_dir: Threads directory
        node_type: "thread" or "entry"
        node_id: Topic (for threads) or entry_id (for entries)

    Returns:
        Access count (0 if not tracked)
    """
    key = f"{node_type}:{node_id}"
    counters = _load_counters(threads_dir)
    return counters.get(key, 0)


def get_most_accessed(
    threads_dir: Path,
    node_type: Optional[str] = None,
    limit: int = 10,
) -> List[tuple[str, str, int]]:
    """Get most accessed nodes.

    Args:
        threads_dir: Threads directory
        node_type: Filter by "thread" or "entry" (or None for all)
        limit: Maximum results

    Returns:
        List of (node_type, node_id, access_count) tuples sorted by count
    """
    counters = _load_counters(threads_dir)

    results = []
    for key, count in counters.items():
        if ":" not in key:
            continue
        n_type, n_id = key.split(":", 1)
        if node_type and n_type != node_type:
            continue
        results.append((n_type, n_id, count))

    # Sort by count descending
    results.sort(key=lambda x: x[2], reverse=True)

    return results[:limit]
