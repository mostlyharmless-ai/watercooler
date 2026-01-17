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
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
# Utilities
# ============================================================================


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def get_graph_dir(threads_dir: Path) -> Path:
    """Get graph directory path."""
    return threads_dir / "graph" / "baseline"


def _ensure_graph_dir(threads_dir: Path) -> Path:
    """Ensure graph directory exists and return path."""
    graph_dir = get_graph_dir(threads_dir)
    graph_dir.mkdir(parents=True, exist_ok=True)
    return graph_dir


def _load_nodes(graph_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load all nodes from JSONL file into dict keyed by ID."""
    nodes_file = graph_dir / "nodes.jsonl"
    nodes: Dict[str, Dict[str, Any]] = {}

    if not nodes_file.exists():
        return nodes

    try:
        with open(nodes_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    node = json.loads(line)
                    node_id = node.get("id", "")
                    if node_id:
                        nodes[node_id] = node
    except Exception as e:
        logger.warning(f"Failed to load nodes from {nodes_file}: {e}")

    return nodes


def _load_edges(graph_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load all edges from JSONL file into dict keyed by source+target."""
    edges_file = graph_dir / "edges.jsonl"
    edges: Dict[str, Dict[str, Any]] = {}

    if not edges_file.exists():
        return edges

    try:
        with open(edges_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    edge = json.loads(line)
                    edge_id = edge.get("source", "") + edge.get("target", "")
                    if edge_id:
                        edges[edge_id] = edge
    except Exception as e:
        logger.warning(f"Failed to load edges from {edges_file}: {e}")

    return edges


def _write_nodes(graph_dir: Path, nodes: Dict[str, Dict[str, Any]]) -> None:
    """Write nodes dict to JSONL file atomically."""
    nodes_file = graph_dir / "nodes.jsonl"
    _atomic_write_jsonl(nodes_file, list(nodes.values()))


def _write_edges(graph_dir: Path, edges: Dict[str, Dict[str, Any]]) -> None:
    """Write edges dict to JSONL file atomically."""
    edges_file = graph_dir / "edges.jsonl"
    _atomic_write_jsonl(edges_file, list(edges.values()))


def _atomic_write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    """Write items to JSONL file atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".tmp_",
        suffix=".jsonl",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, separators=(",", ":")) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON file atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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

    Args:
        threads_dir: Threads directory
        data: Thread data to write

    Returns:
        True if successful
    """
    try:
        graph_dir = _ensure_graph_dir(threads_dir)
        nodes = _load_nodes(graph_dir)

        thread_id = f"thread:{data.topic}"
        now = _now_iso()

        # Preserve existing entry_count if not explicitly set
        existing = nodes.get(thread_id)
        if existing and data.entry_count == 0:
            data.entry_count = existing.get("entry_count", 0)

        # Preserve existing summary if not provided
        if existing and not data.summary:
            data.summary = existing.get("summary", "")

        nodes[thread_id] = _build_thread_node(data, last_updated=now)
        _write_nodes(graph_dir, nodes)

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

    Creates:
    - Entry node
    - Updates thread node (entry_count, last_updated)
    - Contains edge (thread -> entry)
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
        nodes = _load_nodes(graph_dir)
        edges = _load_edges(graph_dir)

        thread_id = f"thread:{data.thread_topic}"
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
        nodes[entry_id] = _build_entry_node(
            data,
            file_refs=file_refs,
            pr_refs=pr_refs,
            commit_refs=commit_refs,
        )

        # Update thread node if exists, or create minimal one
        if thread_id in nodes:
            thread_node = nodes[thread_id]
            thread_node["entry_count"] = max(
                thread_node.get("entry_count", 0),
                data.index + 1,
            )
            thread_node["last_updated"] = now
        else:
            # Create minimal thread node (will be updated by project)
            nodes[thread_id] = {
                "id": thread_id,
                "type": "thread",
                "topic": data.thread_topic,
                "title": data.thread_topic.replace("-", " ").title(),
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

        # Write atomically
        _write_nodes(graph_dir, nodes)
        _write_edges(graph_dir, edges)

        # Update manifest
        _update_manifest(graph_dir, data.thread_topic, data.entry_id)

        logger.debug(f"Upserted entry node: {data.thread_topic}/{data.entry_id}")
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
        nodes = _load_nodes(graph_dir)

        thread_id = f"thread:{topic}"
        if thread_id not in nodes:
            logger.warning(f"Thread node not found for metadata update: {topic}")
            return False

        thread_node = nodes[thread_id]
        now = _now_iso()

        if status is not None:
            thread_node["status"] = status.upper()
        if ball is not None:
            thread_node["ball"] = ball
        if title is not None:
            thread_node["title"] = title
        if summary is not None:
            thread_node["summary"] = summary

        thread_node["last_updated"] = now

        _write_nodes(graph_dir, nodes)

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
        nodes = _load_nodes(graph_dir)
        edges = _load_edges(graph_dir)

        entry_node_id = f"entry:{entry_id}"
        thread_id = f"thread:{topic}"

        # Remove entry node
        if entry_node_id in nodes:
            del nodes[entry_node_id]

        # Remove edges involving this entry
        edges_to_remove = []
        for edge_id, edge in edges.items():
            if edge.get("source") == entry_node_id or edge.get("target") == entry_node_id:
                edges_to_remove.append(edge_id)

        for edge_id in edges_to_remove:
            del edges[edge_id]

        # Update thread entry_count
        if thread_id in nodes:
            # Count remaining entries for this thread
            remaining = sum(
                1 for n in nodes.values()
                if n.get("type") == "entry" and n.get("thread_topic") == topic
            )
            nodes[thread_id]["entry_count"] = remaining
            nodes[thread_id]["last_updated"] = _now_iso()

        _write_nodes(graph_dir, nodes)
        _write_edges(graph_dir, edges)

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

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        Thread node dict or None if not found
    """
    graph_dir = get_graph_dir(threads_dir)
    nodes = _load_nodes(graph_dir)
    thread_id = f"thread:{topic}"
    return nodes.get(thread_id)


def get_entry_from_graph(
    threads_dir: Path,
    entry_id: str,
) -> Optional[Dict[str, Any]]:
    """Read entry node from graph.

    Args:
        threads_dir: Threads directory
        entry_id: Entry ID

    Returns:
        Entry node dict or None if not found
    """
    graph_dir = get_graph_dir(threads_dir)
    nodes = _load_nodes(graph_dir)
    entry_node_id = f"entry:{entry_id}"
    return nodes.get(entry_node_id)


def get_entries_for_thread(
    threads_dir: Path,
    topic: str,
) -> List[Dict[str, Any]]:
    """Get all entry nodes for a thread, sorted by index.

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        List of entry node dicts sorted by index
    """
    graph_dir = get_graph_dir(threads_dir)
    nodes = _load_nodes(graph_dir)

    entries = [
        n for n in nodes.values()
        if n.get("type") == "entry" and n.get("thread_topic") == topic
    ]
    entries.sort(key=lambda e: e.get("index", 0))
    return entries


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


def _update_manifest(graph_dir: Path, topic: str, entry_id: Optional[str]) -> None:
    """Update the manifest file with sync metadata."""
    manifest_path = graph_dir / "manifest.json"
    now = _now_iso()

    manifest: Dict[str, Any] = {
        "schema_version": "1.0",
        "created_at": now,
        "last_updated": now,
        "topics_synced": {},
    }

    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    manifest["last_updated"] = now
    if "topics_synced" not in manifest:
        manifest["topics_synced"] = {}

    manifest["topics_synced"][topic] = {
        "last_entry_id": entry_id,
        "synced_at": now,
    }

    _atomic_write_json(manifest_path, manifest)


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
    nodes = _load_nodes(graph_dir)

    thread_id = f"thread:{topic}"
    if thread_id in nodes:
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
