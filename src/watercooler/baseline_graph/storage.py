"""Centralized storage primitives for per-thread graph format.

All file I/O for baseline graph goes through this module. Business logic stays
in reader.py, writer.py, sync.py.

Per-thread format structure:
    graph/baseline/
        manifest.json           # Global manifest
        search-index.jsonl      # Entry embeddings for cross-thread search
        threads/
            <topic>/
                meta.json       # Thread node
                entries.jsonl   # Entry nodes
                edges.jsonl     # Thread-local edges
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from watercooler.lock import AdvisoryLock

logger = logging.getLogger(__name__)


# ============================================================================
# Path Resolution
# ============================================================================


def get_graph_dir(threads_dir: Path) -> Path:
    """Get base graph directory path.

    Args:
        threads_dir: Threads repository directory

    Returns:
        Path to graph/baseline/
    """
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


def ensure_graph_dir(threads_dir: Path) -> Path:
    """Ensure graph directory exists and return path."""
    graph_dir = get_graph_dir(threads_dir)
    graph_dir.mkdir(parents=True, exist_ok=True)
    return graph_dir


def ensure_thread_graph_dir(graph_dir: Path, topic: str) -> Path:
    """Ensure per-thread graph directory exists and return path."""
    thread_graph_dir = get_thread_graph_dir(graph_dir, topic)
    thread_graph_dir.mkdir(parents=True, exist_ok=True)
    return thread_graph_dir


# ============================================================================
# Atomic Write Primitives
# ============================================================================


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON file atomically using temp file + rename.

    Args:
        path: Target file path
        data: JSON-serializable data
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        # Set readable permissions before rename (mkstemp creates with 0600)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_err:
            logger.warning(f"Failed to clean up temp file {tmp_path}: {cleanup_err}")
        raise


def atomic_write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    """Write items to JSONL file atomically using temp file + rename.

    Args:
        path: Target file path
        items: List of dicts to write as JSONL
    """
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
        # Set readable permissions before rename (mkstemp creates with 0600)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_err:
            logger.warning(f"Failed to clean up temp file {tmp_path}: {cleanup_err}")
        raise


# ============================================================================
# Thread Meta Operations
# ============================================================================


def load_thread_meta(graph_dir: Path, topic: str) -> Optional[Dict[str, Any]]:
    """Load thread metadata from per-thread meta.json.

    Args:
        graph_dir: Base graph directory
        topic: Thread topic

    Returns:
        Thread node dict or None if not found
    """
    meta_file = get_thread_graph_dir(graph_dir, topic) / "meta.json"
    if not meta_file.exists():
        return None

    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to load thread meta for {topic}: {e}")
        return None


def write_thread_meta(graph_dir: Path, topic: str, meta: Dict[str, Any]) -> None:
    """Write thread metadata to per-thread meta.json atomically.

    Args:
        graph_dir: Base graph directory
        topic: Thread topic
        meta: Thread node dict
    """
    thread_dir = ensure_thread_graph_dir(graph_dir, topic)
    atomic_write_json(thread_dir / "meta.json", meta)


def list_thread_topics(graph_dir: Path) -> List[str]:
    """List all thread topics in per-thread format.

    Args:
        graph_dir: Base graph directory

    Returns:
        List of thread topic names
    """
    threads_base = graph_dir / "threads"
    if not threads_base.exists():
        return []

    topics = []
    try:
        for thread_dir in threads_base.iterdir():
            if thread_dir.is_dir():
                meta_file = thread_dir / "meta.json"
                if meta_file.exists():
                    topics.append(thread_dir.name)
    except Exception as e:
        logger.warning(f"Failed to list thread topics: {e}")

    return topics


# ============================================================================
# Entry Operations
# ============================================================================


def load_thread_entries(graph_dir: Path, topic: str) -> Iterator[Dict[str, Any]]:
    """Load entry nodes from per-thread entries.jsonl (streaming).

    Args:
        graph_dir: Base graph directory
        topic: Thread topic

    Yields:
        Entry node dicts
    """
    entries_file = get_thread_graph_dir(graph_dir, topic) / "entries.jsonl"
    if not entries_file.exists():
        return

    with open(entries_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def load_thread_entries_dict(graph_dir: Path, topic: str) -> Dict[str, Dict[str, Any]]:
    """Load entry nodes from per-thread entries.jsonl into dict keyed by ID.

    Args:
        graph_dir: Base graph directory
        topic: Thread topic

    Returns:
        Dict of entry nodes keyed by entry ID
    """
    entries: Dict[str, Dict[str, Any]] = {}
    for node in load_thread_entries(graph_dir, topic):
        node_id = node.get("id", "")
        if node_id:
            entries[node_id] = node
    return entries


def write_thread_entries(
    graph_dir: Path,
    topic: str,
    entries: Dict[str, Dict[str, Any]],
) -> None:
    """Write entry nodes to per-thread entries.jsonl atomically.

    Args:
        graph_dir: Base graph directory
        topic: Thread topic
        entries: Dict of entry nodes keyed by ID
    """
    thread_dir = ensure_thread_graph_dir(graph_dir, topic)
    atomic_write_jsonl(thread_dir / "entries.jsonl", list(entries.values()))


# ============================================================================
# Edge Operations
# ============================================================================


def load_thread_edges(graph_dir: Path, topic: str) -> Dict[str, Dict[str, Any]]:
    """Load edges from per-thread edges.jsonl.

    Args:
        graph_dir: Base graph directory
        topic: Thread topic

    Returns:
        Dict of edges keyed by source+target
    """
    edges_file = get_thread_graph_dir(graph_dir, topic) / "edges.jsonl"
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
        logger.warning(f"Failed to load edges for {topic}: {e}")

    return edges


def write_thread_edges(
    graph_dir: Path,
    topic: str,
    edges: Dict[str, Dict[str, Any]],
) -> None:
    """Write edges to per-thread edges.jsonl atomically.

    Args:
        graph_dir: Base graph directory
        topic: Thread topic
        edges: Dict of edges keyed by source+target
    """
    thread_dir = ensure_thread_graph_dir(graph_dir, topic)
    atomic_write_jsonl(thread_dir / "edges.jsonl", list(edges.values()))


# ============================================================================
# Compound Write Operations
# ============================================================================


def write_thread_graph(
    graph_dir: Path,
    topic: str,
    meta: Dict[str, Any],
    entries: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, Any]],
) -> None:
    """Write all per-thread graph files atomically.

    Writes meta.json, entries.jsonl, and edges.jsonl for a single thread.

    Args:
        graph_dir: Base graph directory
        topic: Thread topic
        meta: Thread node dict
        entries: Dict of entry nodes keyed by ID
        edges: Dict of edges keyed by source+target
    """
    thread_dir = ensure_thread_graph_dir(graph_dir, topic)
    atomic_write_json(thread_dir / "meta.json", meta)
    atomic_write_jsonl(thread_dir / "entries.jsonl", list(entries.values()))
    atomic_write_jsonl(thread_dir / "edges.jsonl", list(edges.values()))


# ============================================================================
# Search Index Operations
# ============================================================================


def load_search_index(graph_dir: Path) -> Iterator[Dict[str, Any]]:
    """Load search index entries (streaming).

    Args:
        graph_dir: Base graph directory

    Yields:
        Search index entries with entry_id, thread_topic, embedding
    """
    search_index_file = graph_dir / "search-index.jsonl"
    if not search_index_file.exists():
        return

    with open(search_index_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def upsert_search_index_entry(
    graph_dir: Path,
    entry_id: str,
    topic: str,
    embedding: List[float],
) -> None:
    """Add or update an entry in the search index.

    Args:
        graph_dir: Base graph directory
        entry_id: Entry ID
        topic: Thread topic
        embedding: Embedding vector
    """
    search_index_file = graph_dir / "search-index.jsonl"

    # Load existing entries, excluding this one if present
    index_entries: List[Dict[str, Any]] = []
    if search_index_file.exists():
        try:
            with open(search_index_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entry = json.loads(line)
                        if entry.get("entry_id") != entry_id:
                            index_entries.append(entry)
        except Exception as e:
            logger.warning(f"Failed to load search index: {e}")

    # Add new/updated entry
    index_entries.append({
        "entry_id": entry_id,
        "thread_topic": topic,
        "embedding": embedding,
    })

    atomic_write_jsonl(search_index_file, index_entries)


def remove_from_search_index(graph_dir: Path, entry_id: str) -> None:
    """Remove an entry from the search index.

    Args:
        graph_dir: Base graph directory
        entry_id: Entry ID to remove
    """
    search_index_file = graph_dir / "search-index.jsonl"
    if not search_index_file.exists():
        return

    # Load and filter
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

    atomic_write_jsonl(search_index_file, index_entries)


# ============================================================================
# Manifest Operations
# ============================================================================


def load_manifest(graph_dir: Path) -> Dict[str, Any]:
    """Load graph manifest.

    Args:
        graph_dir: Base graph directory

    Returns:
        Manifest dict (empty dict if not found)
    """
    manifest_file = graph_dir / "manifest.json"
    if not manifest_file.exists():
        return {}

    try:
        return json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to load manifest: {e}")
        return {}


def update_manifest(
    graph_dir: Path,
    topic: str,
    entry_id: Optional[str] = None,
) -> None:
    """Update manifest with last sync info.

    Uses advisory locking to prevent race conditions in concurrent scenarios.

    Args:
        graph_dir: Base graph directory
        topic: Thread topic that was updated
        entry_id: Entry ID that was added/updated (optional)
    """
    lock_path = graph_dir / ".manifest.lock"

    try:
        # Use advisory lock for read-modify-write safety
        # Short timeout (5s) with stale lock cleanup (30s)
        with AdvisoryLock(lock_path, timeout=5, ttl=30):
            manifest = load_manifest(graph_dir)

            now = datetime.now(timezone.utc).isoformat()
            manifest["last_updated"] = now
            manifest["last_topic"] = topic

            if entry_id:
                manifest["last_entry_id"] = entry_id

            # Track per-topic timestamps
            if "topics" not in manifest:
                manifest["topics"] = {}
            manifest["topics"][topic] = now

            atomic_write_json(graph_dir / "manifest.json", manifest)

    except TimeoutError:
        # Lock timeout - log warning and proceed without update
        # This is a trade-off: we prefer not blocking indefinitely
        logger.warning(
            f"Failed to acquire manifest lock for {topic}, skipping manifest update"
        )


# ============================================================================
# Format Detection
# ============================================================================


def is_per_thread_format(graph_dir: Path) -> bool:
    """Check if graph uses per-thread format.

    Returns True if threads/ directory exists with at least one valid thread
    (valid meta.json that can be parsed).

    Args:
        graph_dir: Base graph directory

    Returns:
        True if per-thread format is present and valid
    """
    threads_base = graph_dir / "threads"
    if not threads_base.exists():
        return False

    try:
        for thread_dir in threads_base.iterdir():
            if thread_dir.is_dir():
                meta_file = thread_dir / "meta.json"
                if meta_file.exists():
                    # Verify it's valid JSON
                    content = meta_file.read_text(encoding="utf-8")
                    json.loads(content)
                    return True
    except Exception:
        pass

    return False


def is_graph_available(threads_dir: Path) -> bool:
    """Check if graph data exists and is usable.

    Args:
        threads_dir: Threads repository directory

    Returns:
        True if graph files exist and are readable
    """
    graph_dir = get_graph_dir(threads_dir)
    return is_per_thread_format(graph_dir)
