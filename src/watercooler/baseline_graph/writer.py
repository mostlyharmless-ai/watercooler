"""Graph writer module for direct graph mutations.

This module provides functions to write thread/entry data directly to the graph
as the source of truth. After graph mutations, use projector.py to regenerate
markdown files as derived projections.

Key functions:
- upsert_thread_node(): Create/update thread node
- upsert_entry_node(): Create/update entry node with edges
- update_thread_metadata(): Update status/ball/title on thread node
- delete_entry_node(): Remove entry from graph

This is the graph-first counterpart to the MD-first commands.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import storage

logger = logging.getLogger(__name__)


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class ThreadData:
    """Data for creating/updating a thread node."""

    topic: str
    title: str
    status: str = "OPEN"
    ball: str = "codex"
    summary: str = ""
    entry_count: int = 0


@dataclass
class EntryData:
    """Data for creating/updating an entry node."""

    entry_id: str
    thread_topic: str
    index: int
    agent: str
    role: str
    entry_type: str
    title: str
    body: str
    timestamp: Optional[str] = None
    summary: str = ""
    embedding: Optional[List[float]] = None


# ============================================================================
# Utilities (delegated to storage)
# ============================================================================


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


# Re-export from storage for backward compatibility
get_graph_dir = storage.get_graph_dir
get_thread_graph_dir = storage.get_thread_graph_dir
_ensure_graph_dir = storage.ensure_graph_dir
_ensure_thread_graph_dir = storage.ensure_thread_graph_dir
_is_per_thread_format = storage.is_per_thread_format


# ============================================================================
# Node Builders
# ============================================================================


def _build_thread_node(
    data: ThreadData,
    last_updated: Optional[str] = None,
) -> Dict[str, Any]:
    """Build thread node dict from ThreadData."""
    return {
        "id": f"thread:{data.topic}",
        "type": "thread",
        "topic": data.topic,
        "title": data.title,
        "status": data.status.upper(),
        "ball": data.ball,
        "last_updated": last_updated or _now_iso(),
        "summary": data.summary,
        "entry_count": data.entry_count,
    }


def _build_entry_node(
    data: EntryData,
    file_refs: Optional[List[str]] = None,
    pr_refs: Optional[List[int]] = None,
    commit_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build entry node dict from EntryData."""
    node = {
        "id": f"entry:{data.entry_id}",
        "type": "entry",
        "entry_id": data.entry_id,
        "thread_topic": data.thread_topic,
        "index": data.index,
        "agent": data.agent,
        "role": data.role,
        "entry_type": data.entry_type,
        "title": data.title,
        "timestamp": data.timestamp or _now_iso(),
        "body": data.body,
        "summary": data.summary,
        "file_refs": file_refs or [],
        "pr_refs": pr_refs or [],
        "commit_refs": commit_refs or [],
    }
    if data.embedding:
        node["embedding"] = data.embedding
    return node


# ============================================================================
# Write Operations
# ============================================================================


def upsert_thread_node(
    threads_dir: Path,
    data: ThreadData,
) -> bool:
    """Create or update a thread node in the graph.

    Uses per-thread format: writes to graph/baseline/threads/<topic>/meta.json

    Args:
        threads_dir: Threads directory
        data: Thread data to write

    Returns:
        True if successful
    """
    try:
        graph_dir = _ensure_graph_dir(threads_dir)

        now = _now_iso()

        # Load existing thread meta to preserve fields
        existing = storage.load_thread_meta(graph_dir, data.topic)

        # Preserve existing entry_count if not explicitly set
        if existing and data.entry_count == 0:
            data.entry_count = existing.get("entry_count", 0)

        # Preserve existing summary if not provided
        if existing and not data.summary:
            data.summary = existing.get("summary", "")

        # Build and write thread meta
        meta = _build_thread_node(data, last_updated=now)
        storage.write_thread_meta(graph_dir, data.topic, meta)

        # Update manifest
        storage.update_manifest(graph_dir, data.topic, None)

        logger.debug(f"Upserted thread node: {data.topic}")
        return True

    except Exception as e:
        logger.error(f"Failed to upsert thread node {data.topic}: {e}")
        return False


def upsert_entry_node(
    threads_dir: Path,
    data: EntryData,
    prev_entry_id: Optional[str] = None,
) -> bool:
    """Create or update an entry node with edges.

    Uses per-thread format: writes to graph/baseline/threads/<topic>/

    Creates:
    - Entry node in entries.jsonl
    - Updates thread node in meta.json (entry_count, last_updated)
    - Contains edge (thread -> entry) in edges.jsonl
    - Followed_by edge (prev_entry -> entry) if prev_entry_id provided

    Args:
        threads_dir: Threads directory
        data: Entry data to write
        prev_entry_id: Previous entry ID for followed_by edge

    Returns:
        True if successful
    """
    try:
        graph_dir = _ensure_graph_dir(threads_dir)
        topic = data.thread_topic

        # Load per-thread data
        meta = storage.load_thread_meta(graph_dir, topic)
        entries = storage.load_thread_entries_dict(graph_dir, topic)
        edges = storage.load_thread_edges(graph_dir, topic)

        thread_id = f"thread:{topic}"
        entry_id = f"entry:{data.entry_id}"
        now = _now_iso()

        # Extract references from body
        from .export import (
            _extract_file_refs,
            _extract_pr_refs,
            _extract_commit_refs,
        )
        file_refs = _extract_file_refs(data.body)
        pr_refs = _extract_pr_refs(data.body)
        commit_refs = _extract_commit_refs(data.body)

        # Create entry node
        entries[entry_id] = _build_entry_node(
            data,
            file_refs=file_refs,
            pr_refs=pr_refs,
            commit_refs=commit_refs,
        )

        # Update thread meta if exists, or create minimal one
        if meta:
            meta["entry_count"] = max(
                meta.get("entry_count", 0),
                data.index + 1,
            )
            meta["last_updated"] = now
        else:
            # Create minimal thread meta (will be updated by project)
            meta = {
                "id": thread_id,
                "type": "thread",
                "topic": topic,
                "title": topic.replace("-", " ").title(),
                "status": "OPEN",
                "ball": "codex",
                "last_updated": now,
                "summary": "",
                "entry_count": data.index + 1,
            }

        # Create contains edge
        contains_edge_id = thread_id + entry_id
        edges[contains_edge_id] = {
            "source": thread_id,
            "target": entry_id,
            "type": "contains",
        }

        # Create followed_by edge if prev_entry_id provided
        if prev_entry_id:
            prev_entry_node_id = f"entry:{prev_entry_id}"
            followed_by_edge_id = prev_entry_node_id + entry_id
            edges[followed_by_edge_id] = {
                "source": prev_entry_node_id,
                "target": entry_id,
                "type": "followed_by",
            }

        # Write all per-thread files atomically
        storage.write_thread_graph(graph_dir, topic, meta, entries, edges)

        # Dual-write: Also append to monolithic format for backward compatibility
        # Failures here should not fail the primary write (per-thread is canonical)
        try:
            monolithic_nodes = [meta, entries[entry_id]]
            monolithic_edges = list(edges.values())
            storage.append_to_monolithic_nodes(graph_dir, monolithic_nodes)
            storage.append_to_monolithic_edges(graph_dir, monolithic_edges)
        except Exception as dual_write_err:
            logger.warning(
                f"Dual-write to monolithic format failed for {topic}/{data.entry_id}: "
                f"{dual_write_err}. Per-thread format is canonical; continuing."
            )

        # Update search index with new entry (if has embedding)
        embedding = entries[entry_id].get("embedding")
        if embedding:
            storage.upsert_search_index_entry(graph_dir, data.entry_id, topic, embedding)

        # Update manifest
        storage.update_manifest(graph_dir, topic, data.entry_id)

        logger.debug(f"Upserted entry node: {topic}/{data.entry_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to upsert entry node {data.entry_id}: {e}")
        return False


def update_thread_metadata(
    threads_dir: Path,
    topic: str,
    *,
    status: Optional[str] = None,
    ball: Optional[str] = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
) -> bool:
    """Update metadata fields on an existing thread node.

    Uses per-thread format: updates graph/baseline/threads/<topic>/meta.json

    Only updates fields that are explicitly provided (not None).

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        status: New status (will be uppercased)
        ball: New ball owner
        title: New title
        summary: New summary

    Returns:
        True if successful, False if thread not found or error
    """
    try:
        graph_dir = _ensure_graph_dir(threads_dir)

        meta = storage.load_thread_meta(graph_dir, topic)
        if not meta:
            logger.warning(f"Thread node not found for metadata update: {topic}")
            return False

        now = _now_iso()

        if status is not None:
            meta["status"] = status.upper()
        if ball is not None:
            meta["ball"] = ball
        if title is not None:
            meta["title"] = title
        if summary is not None:
            meta["summary"] = summary

        meta["last_updated"] = now

        storage.write_thread_meta(graph_dir, topic, meta)

        logger.debug(f"Updated thread metadata: {topic}")
        return True

    except Exception as e:
        logger.error(f"Failed to update thread metadata {topic}: {e}")
        return False


def delete_entry_node(
    threads_dir: Path,
    topic: str,
    entry_id: str,
) -> bool:
    """Delete an entry node and its edges from the graph.

    Uses per-thread format: updates graph/baseline/threads/<topic>/ files

    Note: Also updates the thread node entry_count.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        entry_id: Entry ID to delete

    Returns:
        True if successful
    """
    try:
        graph_dir = _ensure_graph_dir(threads_dir)

        # Load per-thread data
        meta = storage.load_thread_meta(graph_dir, topic)
        entries = storage.load_thread_entries_dict(graph_dir, topic)
        edges = storage.load_thread_edges(graph_dir, topic)

        entry_node_id = f"entry:{entry_id}"

        # Remove entry node
        if entry_node_id in entries:
            del entries[entry_node_id]

        # Remove edges involving this entry
        edges_to_remove = []
        for edge_id, edge in edges.items():
            if edge.get("source") == entry_node_id or edge.get("target") == entry_node_id:
                edges_to_remove.append(edge_id)

        for edge_id in edges_to_remove:
            del edges[edge_id]

        # Update thread entry_count
        if meta:
            meta["entry_count"] = len(entries)
            meta["last_updated"] = _now_iso()

        # Write per-thread files
        if meta:
            storage.write_thread_graph(graph_dir, topic, meta, entries, edges)

        # Remove from search index
        storage.remove_from_search_index(graph_dir, entry_id)

        logger.debug(f"Deleted entry node: {topic}/{entry_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to delete entry node {entry_id}: {e}")
        return False


def get_thread_from_graph(
    threads_dir: Path,
    topic: str,
) -> Optional[Dict[str, Any]]:
    """Read thread node from graph.

    Uses per-thread format: reads from graph/baseline/threads/<topic>/meta.json

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        Thread node dict or None if not found
    """
    graph_dir = get_graph_dir(threads_dir)
    return storage.load_thread_meta(graph_dir, topic)


def get_entry_node_from_graph(
    threads_dir: Path,
    entry_id: str,
    topic: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Read entry node dict from graph (low-level).

    Uses per-thread format. If topic is not provided, searches all thread
    directories (slower).

    Note: For higher-level access returning GraphEntry, use reader.get_entry_from_graph().

    Args:
        threads_dir: Threads directory
        entry_id: Entry ID
        topic: Thread topic (optional, but recommended for performance)

    Returns:
        Entry node dict or None if not found
    """
    graph_dir = get_graph_dir(threads_dir)
    entry_node_id = f"entry:{entry_id}"

    if topic:
        # Fast path: look directly in the thread's entries
        entries = storage.load_thread_entries_dict(graph_dir, topic)
        return entries.get(entry_node_id)

    # Slow path: search all thread directories
    for t in storage.list_thread_topics(graph_dir):
        entries = storage.load_thread_entries_dict(graph_dir, t)
        if entry_node_id in entries:
            return entries[entry_node_id]

    return None


def get_entries_for_thread(
    threads_dir: Path,
    topic: str,
) -> List[Dict[str, Any]]:
    """Get all entry nodes for a thread, sorted by index.

    Uses per-thread format: reads from graph/baseline/threads/<topic>/entries.jsonl

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        List of entry node dicts sorted by index
    """
    graph_dir = get_graph_dir(threads_dir)
    entries = storage.load_thread_entries_dict(graph_dir, topic)

    entries_list = list(entries.values())
    entries_list.sort(key=lambda e: e.get("index", 0))
    return entries_list


def get_last_entry_id(
    threads_dir: Path,
    topic: str,
) -> Optional[str]:
    """Get the entry_id of the last entry in a thread.

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        Entry ID or None if no entries
    """
    entries = get_entries_for_thread(threads_dir, topic)
    if entries:
        return entries[-1].get("entry_id")
    return None


def get_next_entry_index(
    threads_dir: Path,
    topic: str,
) -> int:
    """Get the next available entry index for a thread.

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        Next index (0 if no entries)
    """
    entries = get_entries_for_thread(threads_dir, topic)
    if entries:
        return entries[-1].get("index", 0) + 1
    return 0


# ============================================================================
# Graph Initialization
# ============================================================================


def init_thread_in_graph(
    threads_dir: Path,
    topic: str,
    title: Optional[str] = None,
    status: str = "OPEN",
    ball: str = "codex",
) -> bool:
    """Initialize a new thread in the graph.

    Uses per-thread format: creates graph/baseline/threads/<topic>/meta.json

    Creates the thread node if it doesn't exist.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        title: Thread title (defaults to topic with hyphens replaced)
        status: Initial status
        ball: Initial ball owner

    Returns:
        True if created or already exists
    """
    graph_dir = get_graph_dir(threads_dir)

    # Check if thread already exists in per-thread format
    meta = storage.load_thread_meta(graph_dir, topic)
    if meta:
        logger.debug(f"Thread already exists in graph: {topic}")
        return True

    data = ThreadData(
        topic=topic,
        title=title or topic.replace("-", " ").strip(),
        status=status,
        ball=ball,
        summary="",
        entry_count=0,
    )

    return upsert_thread_node(threads_dir, data)
