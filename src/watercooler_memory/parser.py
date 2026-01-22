"""Parser for converting watercooler threads into graph nodes.

This module builds on the existing thread_entries.parse_thread_entries()
function, adding graph-specific metadata and temporal sequencing.

The parser produces:
- ThreadNode for the thread
- EntryNode for each entry (with FOLLOWS edges between sequential entries)
- Hyperedge for thread membership
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from watercooler.thread_entries import ThreadEntry, parse_thread_entries, parse_thread_header

from .schema import (
    ThreadNode,
    EntryNode,
    Edge,
    Hyperedge,
)


def parse_thread_to_nodes(
    thread_path: Path,
    branch_context: Optional[str] = None,
) -> tuple[ThreadNode, list[EntryNode], list[Edge], list[Hyperedge]]:
    """Parse a thread file into graph nodes and edges.

    Args:
        thread_path: Path to the thread markdown file.
        branch_context: Optional git branch name for context.

    Returns:
        Tuple of (thread_node, entry_nodes, edges, hyperedges)

    Raises:
        FileNotFoundError: If thread file doesn't exist.
    """
    if not thread_path.exists():
        raise FileNotFoundError(f"Thread not found: {thread_path}")

    content = thread_path.read_text(encoding="utf-8")
    thread_id = thread_path.stem

    # Use existing metadata parser
    title, status, ball, last_update = parse_thread_header(thread_path)

    # Use existing entry parser
    entries = parse_thread_entries(content)

    # Convert entries to EntryNodes
    entry_nodes: list[EntryNode] = []
    entry_id_list: list[str] = []

    for i, entry in enumerate(entries):
        # Generate a stable entry_id if not present
        entry_id = entry.entry_id or f"{thread_id}:{i}"
        entry_id_list.append(entry_id)

        # Determine preceding/following entry IDs
        preceding_id = entry_id_list[i - 1] if i > 0 else None
        # following_id will be set in a second pass

        entry_node = EntryNode(
            entry_id=entry_id,
            thread_id=thread_id,
            index=entry.index,
            agent=entry.agent,
            role=entry.role,
            entry_type=entry.entry_type,
            title=entry.title,
            timestamp=entry.timestamp,
            body=entry.body,
            sequence_index=i,
            preceding_entry_id=preceding_id,
        )
        entry_nodes.append(entry_node)

    # Second pass: set following_entry_id
    for i, node in enumerate(entry_nodes):
        if i < len(entry_nodes) - 1:
            # Create new node with following_entry_id set
            entry_nodes[i] = EntryNode(
                entry_id=node.entry_id,
                thread_id=node.thread_id,
                index=node.index,
                agent=node.agent,
                role=node.role,
                entry_type=node.entry_type,
                title=node.title,
                timestamp=node.timestamp,
                body=node.body,
                chunk_ids=node.chunk_ids,
                summary=node.summary,
                embedding=node.embedding,
                sequence_index=node.sequence_index,
                preceding_entry_id=node.preceding_entry_id,
                following_entry_id=entry_id_list[i + 1],
                ingestion_time=node.ingestion_time,
            )

    # Create thread node
    created_at = entries[0].timestamp if entries else last_update
    thread_node = ThreadNode(
        thread_id=thread_id,
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

    # Build hyperedges
    hyperedges: list[Hyperedge] = []

    # Thread membership hyperedge
    if entry_id_list:
        hyperedges.append(
            Hyperedge.thread_membership(
                thread_id=thread_id,
                entry_ids=entry_id_list,
                event_time=created_at,
            )
        )

    return thread_node, entry_nodes, edges, hyperedges


def parse_threads_directory(
    threads_dir: Path,
    branch_context: Optional[str] = None,
    thread_filter: Optional[list[str]] = None,
) -> tuple[list[ThreadNode], list[EntryNode], list[Edge], list[Hyperedge]]:
    """Parse all threads in a directory into graph nodes.

    Args:
        threads_dir: Path to the threads directory.
        branch_context: Optional git branch name for context.
        thread_filter: Optional list of thread .md filenames to process (None = all).

    Returns:
        Tuple of (thread_nodes, entry_nodes, edges, hyperedges)
    """
    if not threads_dir.exists():
        return [], [], [], []

    all_threads: list[ThreadNode] = []
    all_entries: list[EntryNode] = []
    all_edges: list[Edge] = []
    all_hyperedges: list[Hyperedge] = []

    # Determine which thread files to process
    if thread_filter:
        # Process only specified threads
        thread_paths = []
        for filename in thread_filter:
            thread_path = threads_dir / filename
            if thread_path.exists():
                thread_paths.append(thread_path)
            else:
                print(f"Warning: Thread file not found: {thread_path}")
        thread_paths = sorted(thread_paths)
    else:
        # Process all *.md files in directory
        thread_paths = sorted(threads_dir.glob("*.md"))

    for thread_path in thread_paths:
        # Skip index.md or other non-thread files
        if thread_path.stem.startswith("_") or thread_path.stem == "index":
            continue

        try:
            thread, entries, edges, hyperedges = parse_thread_to_nodes(
                thread_path, branch_context
            )
            all_threads.append(thread)
            all_entries.extend(entries)
            all_edges.extend(edges)
            all_hyperedges.extend(hyperedges)
        except Exception as e:
            # Log but continue with other threads
            print(f"Warning: Failed to parse {thread_path}: {e}")

    return all_threads, all_entries, all_edges, all_hyperedges
