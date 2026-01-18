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


def get_thread_graph_dir(graph_dir: Path, topic: str) -> Path:
    """Get per-thread graph directory path.

    Args:
        graph_dir: Base graph directory (graph/baseline)
        topic: Thread topic

    Returns:
        Path to graph/baseline/threads/<topic>/
    """
    return graph_dir / "threads" / topic


def _ensure_graph_dir(threads_dir: Path) -> Path:
    """Ensure graph directory exists and return path."""
    graph_dir = get_graph_dir(threads_dir)
    graph_dir.mkdir(parents=True, exist_ok=True)
    return graph_dir


def _ensure_thread_graph_dir(graph_dir: Path, topic: str) -> Path:
    """Ensure per-thread graph directory exists and return path."""
    thread_graph_dir = get_thread_graph_dir(graph_dir, topic)
    thread_graph_dir.mkdir(parents=True, exist_ok=True)
    return thread_graph_dir


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
# Per-Thread Graph Operations
# ============================================================================


def _load_thread_meta(thread_graph_dir: Path) -> Optional[Dict[str, Any]]:
    """Load thread metadata from meta.json.

    Args:
        thread_graph_dir: Path to thread's graph directory

    Returns:
        Thread node dict or None if not found
    """
    meta_file = thread_graph_dir / "meta.json"
    if not meta_file.exists():
        return None

    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to load thread meta from {meta_file}: {e}")
        return None


def _load_thread_entries(thread_graph_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load entry nodes from per-thread entries.jsonl.

    Args:
        thread_graph_dir: Path to thread's graph directory

    Returns:
        Dict of entry nodes keyed by ID
    """
    entries_file = thread_graph_dir / "entries.jsonl"
    entries: Dict[str, Dict[str, Any]] = {}

    if not entries_file.exists():
        return entries

    try:
        with open(entries_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    node = json.loads(line)
                    node_id = node.get("id", "")
                    if node_id:
                        entries[node_id] = node
    except Exception as e:
        logger.warning(f"Failed to load entries from {entries_file}: {e}")

    return entries


def _load_thread_edges(thread_graph_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load edges from per-thread edges.jsonl.

    Args:
        thread_graph_dir: Path to thread's graph directory

    Returns:
        Dict of edges keyed by source+target
    """
    edges_file = thread_graph_dir / "edges.jsonl"
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


def _write_thread_meta(thread_graph_dir: Path, meta: Dict[str, Any]) -> None:
    """Write thread metadata to meta.json atomically."""
    meta_file = thread_graph_dir / "meta.json"
    _atomic_write_json(meta_file, meta)


def _write_thread_entries(
    thread_graph_dir: Path,
    entries: Dict[str, Dict[str, Any]],
) -> None:
    """Write entry nodes to per-thread entries.jsonl atomically."""
    entries_file = thread_graph_dir / "entries.jsonl"
    _atomic_write_jsonl(entries_file, list(entries.values()))


def _write_thread_edges(
    thread_graph_dir: Path,
    edges: Dict[str, Dict[str, Any]],
) -> None:
    """Write edges to per-thread edges.jsonl atomically."""
    edges_file = thread_graph_dir / "edges.jsonl"
    _atomic_write_jsonl(edges_file, list(edges.values()))


def _write_thread_graph(
    thread_graph_dir: Path,
    meta: Dict[str, Any],
    entries: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, Any]],
) -> None:
    """Write all per-thread graph files atomically.

    Writes meta.json, entries.jsonl, and edges.jsonl for a single thread.

    Args:
        thread_graph_dir: Path to thread's graph directory
        meta: Thread node dict
        entries: Dict of entry nodes keyed by ID
        edges: Dict of edges keyed by source+target
    """
    thread_graph_dir.mkdir(parents=True, exist_ok=True)
    _write_thread_meta(thread_graph_dir, meta)
    _write_thread_entries(thread_graph_dir, entries)
    _write_thread_edges(thread_graph_dir, edges)


def _is_per_thread_format(graph_dir: Path) -> bool:
    """Check if graph uses per-thread format.

    Returns True if the threads/ directory exists with at least one thread.
    """
    threads_dir = graph_dir / "threads"
    if not threads_dir.exists():
        return False
    # Check for at least one thread directory
    try:
        return any(d.is_dir() for d in threads_dir.iterdir())
    except Exception:
        return False


# ============================================================================
# Search Index Operations
# ============================================================================


def _update_search_index(
    graph_dir: Path,
    data: EntryData,
    entry_node: Dict[str, Any],
) -> None:
    """Update the search index with a new/updated entry.

    The search index contains entry_id, thread_topic, and embedding for
    fast full-corpus vector search without loading all per-thread files.

    Args:
        graph_dir: Base graph directory
        data: Entry data
        entry_node: Built entry node dict (contains embedding if present)
    """
    # Only index entries with embeddings
    embedding = entry_node.get("embedding")
    if not embedding:
        return

    search_index_file = graph_dir / "search-index.jsonl"

    # Load existing index entries (excluding this entry if it exists)
    index_entries: List[Dict[str, Any]] = []
    entry_key = data.entry_id

    if search_index_file.exists():
        try:
            with open(search_index_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entry = json.loads(line)
                        # Skip if this is the same entry (will re-add below)
                        if entry.get("entry_id") != entry_key:
                            index_entries.append(entry)
        except Exception as e:
            logger.warning(f"Failed to load search index: {e}")

    # Add/update entry in index
    index_entries.append({
        "entry_id": data.entry_id,
        "thread_topic": data.thread_topic,
        "embedding": embedding,
    })

    # Write atomically
    _atomic_write_jsonl(search_index_file, index_entries)


def _remove_from_search_index(graph_dir: Path, entry_id: str) -> None:
    """Remove an entry from the search index.

    Args:
        graph_dir: Base graph directory
        entry_id: Entry ID to remove
    """
    search_index_file = graph_dir / "search-index.jsonl"

    if not search_index_file.exists():
        return

    # Load and filter out the entry
    index_entries: List[Dict[str, Any]] = []

    try:
        with open(search_index_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    if entry.get("entry_id") != entry_id:
                        index_entries.append(entry)
    except Exception as e:
        logger.warning(f"Failed to load search index for removal: {e}")
        return

    # Write atomically
    _atomic_write_jsonl(search_index_file, index_entries)


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
        thread_graph_dir = _ensure_thread_graph_dir(graph_dir, data.topic)

        now = _now_iso()

        # Load existing thread meta to preserve fields
        existing = _load_thread_meta(thread_graph_dir)

        # Preserve existing entry_count if not explicitly set
        if existing and data.entry_count == 0:
            data.entry_count = existing.get("entry_count", 0)

        # Preserve existing summary if not provided
        if existing and not data.summary:
            data.summary = existing.get("summary", "")

        # Build and write thread meta
        meta = _build_thread_node(data, last_updated=now)
        _write_thread_meta(thread_graph_dir, meta)

        # Update manifest
        _update_manifest(graph_dir, data.topic, None)

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
        thread_graph_dir = _ensure_thread_graph_dir(graph_dir, data.thread_topic)

        # Load per-thread data
        meta = _load_thread_meta(thread_graph_dir)
        entries = _load_thread_entries(thread_graph_dir)
        edges = _load_thread_edges(thread_graph_dir)

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

        # Write all per-thread files atomically
        _write_thread_graph(thread_graph_dir, meta, entries, edges)

        # Update search index with new entry
        _update_search_index(graph_dir, data, entries[entry_id])

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
        thread_graph_dir = get_thread_graph_dir(graph_dir, topic)

        meta = _load_thread_meta(thread_graph_dir)
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

        _write_thread_meta(thread_graph_dir, meta)

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
        thread_graph_dir = get_thread_graph_dir(graph_dir, topic)

        # Load per-thread data
        meta = _load_thread_meta(thread_graph_dir)
        entries = _load_thread_entries(thread_graph_dir)
        edges = _load_thread_edges(thread_graph_dir)

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
            _write_thread_graph(thread_graph_dir, meta, entries, edges)

        # Remove from search index
        _remove_from_search_index(graph_dir, entry_id)

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
    thread_graph_dir = get_thread_graph_dir(graph_dir, topic)
    return _load_thread_meta(thread_graph_dir)


def get_entry_from_graph(
    threads_dir: Path,
    entry_id: str,
    topic: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Read entry node from graph.

    Uses per-thread format. If topic is not provided, searches all thread
    directories (slower).

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
        thread_graph_dir = get_thread_graph_dir(graph_dir, topic)
        entries = _load_thread_entries(thread_graph_dir)
        return entries.get(entry_node_id)

    # Slow path: search all thread directories
    threads_base = graph_dir / "threads"
    if not threads_base.exists():
        return None

    for thread_dir in threads_base.iterdir():
        if thread_dir.is_dir():
            entries = _load_thread_entries(thread_dir)
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
    thread_graph_dir = get_thread_graph_dir(graph_dir, topic)
    entries = _load_thread_entries(thread_graph_dir)

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
    thread_graph_dir = get_thread_graph_dir(graph_dir, topic)

    # Check if thread already exists in per-thread format
    meta = _load_thread_meta(thread_graph_dir)
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
