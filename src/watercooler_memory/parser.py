"""Parser for converting watercooler threads into graph nodes.

Reads thread data from the baseline graph (meta.json + entries.jsonl)
and produces memory-graph nodes with temporal sequencing.

The parser produces:
- ThreadNode for the thread
- EntryNode for each entry (with FOLLOWS edges between sequential entries)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from watercooler.baseline_graph import storage
from watercooler.baseline_graph.storage import get_graph_dir
from watercooler.baseline_graph.writer import (
    get_thread_from_graph,
    get_entries_for_thread,
)

from .schema import (
    ThreadNode,
    EntryNode,
    Edge,
)

logger = logging.getLogger(__name__)


def parse_thread_to_nodes(
    threads_dir: Path,
    topic: str,
    branch_context: Optional[str] = None,
) -> tuple[ThreadNode, list[EntryNode], list[Edge]]:
    """Parse a thread from graph into memory-graph nodes and edges.

    Args:
        threads_dir: Path to the threads directory.
        topic: Thread topic identifier.
        branch_context: Optional git branch name for context.

    Returns:
        Tuple of (thread_node, entry_nodes, edges)

    Raises:
        FileNotFoundError: If thread not found in graph.
    """
    thread_meta = get_thread_from_graph(threads_dir, topic)
    if thread_meta is None:
        raise FileNotFoundError(f"Thread not found in graph: {topic}")

    graph_entries = get_entries_for_thread(threads_dir, topic)

    title = thread_meta.get("title", topic)
    status = thread_meta.get("status", "OPEN")
    ball = thread_meta.get("ball", "")
    last_update = thread_meta.get("last_updated", "")

    # Convert entries to EntryNodes
    entry_nodes: list[EntryNode] = []
    entry_id_list: list[str] = []

    for i, entry in enumerate(graph_entries):
        entry_id = entry.get("entry_id", "") or f"{topic}:{i}"
        entry_id_list.append(entry_id)

        entry_node = EntryNode(
            entry_id=entry_id,
            thread_id=topic,
            index=entry.get("index", i),
            agent=entry.get("agent", ""),
            role=entry.get("role", ""),
            entry_type=entry.get("entry_type", "Note"),
            title=entry.get("title", ""),
            timestamp=entry.get("timestamp", ""),
            body=entry.get("body", ""),
            sequence_index=i,
        )
        entry_nodes.append(entry_node)

    # Create thread node
    created_at = graph_entries[0].get("timestamp", "") if graph_entries else last_update
    thread_node = ThreadNode(
        thread_id=topic,
        title=title,
        status=status.upper(),
        ball=ball,
        created_at=created_at or "",
        updated_at=last_update,
        entry_ids=entry_id_list,
        branch_context=branch_context,
    )

    # Build edges
    edges: list[Edge] = []

    # CONTAINS edges: Thread → Entry
    for entry_node in entry_nodes:
        edges.append(
            Edge.contains(
                parent_id=thread_node.node_id,
                child_id=entry_node.node_id,
                event_time=entry_node.timestamp,
            )
        )

    # FOLLOWS edges: Entry → Entry (sequential)
    for i in range(len(entry_nodes) - 1):
        edges.append(
            Edge.follows(
                preceding_id=entry_nodes[i].node_id,
                following_id=entry_nodes[i + 1].node_id,
                event_time=entry_nodes[i + 1].timestamp,
            )
        )

    return thread_node, entry_nodes, edges


def parse_threads_directory(
    threads_dir: Path,
    branch_context: Optional[str] = None,
    thread_filter: Optional[list[str]] = None,
) -> tuple[list[ThreadNode], list[EntryNode], list[Edge]]:
    """Parse all threads in a directory into graph nodes.

    Args:
        threads_dir: Path to the threads directory.
        branch_context: Optional git branch name for context.
        thread_filter: Optional list of thread topic names to process (None = all).

    Returns:
        Tuple of (thread_nodes, entry_nodes, edges)
    """
    if not threads_dir.exists():
        return [], [], []

    all_threads: list[ThreadNode] = []
    all_entries: list[EntryNode] = []
    all_edges: list[Edge] = []

    # Determine which topics to process
    if thread_filter:
        # Process only specified topics (strip .md extension if present)
        topics = [t.removesuffix(".md") for t in thread_filter]
    else:
        # List all topics from graph
        graph_dir = get_graph_dir(threads_dir)
        topics = storage.list_thread_topics(graph_dir)

    for topic in topics:
        # Skip index or other non-thread names
        if topic.startswith("_") or topic == "index":
            continue

        try:
            thread, entries, edges = parse_thread_to_nodes(
                threads_dir, topic, branch_context
            )
            all_threads.append(thread)
            all_entries.extend(entries)
            all_edges.extend(edges)
        except Exception as e:
            # Log but continue with other threads
            logger.warning("Failed to parse %s: %s", topic, e)

    return all_threads, all_entries, all_edges
