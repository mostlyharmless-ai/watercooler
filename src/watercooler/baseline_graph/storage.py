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
            if thread_dir.is_dir() and not thread_dir.is_symlink():
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


def _per_topic_search_index_path(graph_dir: Path, topic: str) -> Path:
    """Get per-topic search index path."""
    return get_thread_graph_dir(graph_dir, topic) / "search-index.jsonl"


def _load_jsonl_entries(path: Path) -> List[Dict[str, Any]]:
    """Load JSONL entries from a file, skipping malformed lines."""
    entries: List[Dict[str, Any]] = []
    if not path.exists():
        return entries
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
    return entries


def load_search_index(graph_dir: Path, topic: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    """Load search index entries (streaming), aggregating per-topic shards.

    Reads from per-topic ``threads/<topic>/search-index.jsonl`` files.
    Falls back to the legacy global ``search-index.jsonl`` if no per-topic
    shards exist.

    Args:
        graph_dir: Base graph directory
        topic: If provided, load only this topic's shard

    Yields:
        Search index entries with entry_id, thread_topic, embedding
    """
    if topic:
        # Single topic: read shard + global file, dedup by entry_id
        # (or content hash for ID-less entries).
        seen: set = set()
        shard = _per_topic_search_index_path(graph_dir, topic)
        if shard.exists():
            for entry in _load_jsonl_entries(shard):
                eid = entry.get("entry_id", "")
                key = eid if eid else json.dumps(entry, sort_keys=True, separators=(",", ":"))
                if key not in seen:
                    seen.add(key)
                    yield entry
        # Also check global file for pre-migration entries
        global_file = graph_dir / "search-index.jsonl"
        if global_file.exists():
            for entry in _load_jsonl_entries(global_file):
                if entry.get("thread_topic") == topic:
                    eid = entry.get("entry_id", "")
                    key = eid if eid else json.dumps(entry, sort_keys=True, separators=(",", ":"))
                    if key not in seen:
                        seen.add(key)
                        yield entry
        return

    # Aggregate all per-topic shards + legacy global file.
    # In a partially-migrated repo, some topics have shards while older
    # entries remain in the global file. We yield from both, using a set
    # to deduplicate by entry_id.
    seen: set = set()  # entry_id or content hash for dedup

    def _dedup_yield(entry: Dict[str, Any]):
        """Yield entry if not already seen, deduplicating by entry_id or content hash."""
        eid = entry.get("entry_id", "")
        key = eid if eid else json.dumps(entry, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            return entry
        return None

    threads_base = graph_dir / "threads"
    if threads_base.exists():
        for thread_dir in threads_base.iterdir():
            if not thread_dir.is_dir() or thread_dir.is_symlink():
                continue
            shard = thread_dir / "search-index.jsonl"
            if shard.exists():
                for entry in _load_jsonl_entries(shard):
                    deduped = _dedup_yield(entry)
                    if deduped is not None:
                        yield deduped

    # Also scan legacy global file for ALL entries not already yielded.
    # Don't filter by sharded_topics — a topic's shard only contains
    # entries written after sharding; pre-migration entries for that
    # topic still live in the global file until --migrate runs.
    global_file = graph_dir / "search-index.jsonl"
    if global_file.exists():
        for entry in _load_jsonl_entries(global_file):
            deduped = _dedup_yield(entry)
            if deduped is not None:
                yield deduped


def upsert_search_index_entry(
    graph_dir: Path,
    entry_id: str,
    topic: str,
    embedding: List[float],
) -> None:
    """Add or update an entry in the per-topic search index shard.

    Writes to ``threads/<topic>/search-index.jsonl`` (per-topic shard).

    Args:
        graph_dir: Base graph directory
        entry_id: Entry ID
        topic: Thread topic
        embedding: Embedding vector
    """
    shard_path = _per_topic_search_index_path(graph_dir, topic)
    shard_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing entries, excluding this one if present
    index_entries = [
        e for e in _load_jsonl_entries(shard_path)
        if e.get("entry_id") != entry_id
    ]

    # Add new/updated entry
    index_entries.append({
        "entry_id": entry_id,
        "thread_topic": topic,
        "embedding": embedding,
    })

    atomic_write_jsonl(shard_path, index_entries)


def remove_from_search_index(graph_dir: Path, entry_id: str, topic: Optional[str] = None) -> None:
    """Remove an entry from the search index.

    Checks the per-topic shard first (if topic provided), then falls
    back to the legacy global file.

    Args:
        graph_dir: Base graph directory
        entry_id: Entry ID to remove
        topic: Thread topic (optional, speeds up lookup)
    """
    if topic:
        shard_path = _per_topic_search_index_path(graph_dir, topic)
        if shard_path.exists():
            original = _load_jsonl_entries(shard_path)
            filtered = [e for e in original if e.get("entry_id") != entry_id]
            if len(filtered) < len(original):
                atomic_write_jsonl(shard_path, filtered)
    else:
        # No topic — scan all per-topic shards
        threads_base = graph_dir / "threads"
        if threads_base.exists():
            for thread_dir in threads_base.iterdir():
                if not thread_dir.is_dir() or thread_dir.is_symlink():
                    continue
                shard = thread_dir / "search-index.jsonl"
                if shard.exists():
                    original = _load_jsonl_entries(shard)
                    filtered = [e for e in original if e.get("entry_id") != entry_id]
                    if len(filtered) < len(original):
                        atomic_write_jsonl(shard, filtered)

    # Also check legacy global file — entry may predate per-topic migration
    global_file = graph_dir / "search-index.jsonl"
    if global_file.exists():
        original = _load_jsonl_entries(global_file)
        filtered = [e for e in original if e.get("entry_id") != entry_id]
        if len(filtered) < len(original):
            atomic_write_jsonl(global_file, filtered)


# ============================================================================
# Manifest Operations
# ============================================================================


import threading as _threading
import time as _time

_manifest_cache: Dict[str, tuple] = {}  # graph_dir -> (manifest, timestamp, generation)
_manifest_cache_lock = _threading.Lock()
_manifest_cache_gen: Dict[str, int] = {}  # graph_dir -> generation counter
_MANIFEST_CACHE_TTL = 2.0  # seconds


def load_manifest(graph_dir: Path) -> Dict[str, Any]:
    """Load graph manifest by scanning per-thread meta.json files.

    Uses a short-lived in-memory cache (2s TTL) to avoid repeated
    filesystem scans within tight loops, while still reflecting
    newly-created and deleted topics promptly. Thread-safe.

    Args:
        graph_dir: Base graph directory

    Returns:
        Manifest dict built from current thread state
    """
    import copy
    cache_key = str(graph_dir)
    with _manifest_cache_lock:
        if cache_key in _manifest_cache:
            cached, ts, gen = _manifest_cache[cache_key]
            if _time.monotonic() - ts < _MANIFEST_CACHE_TTL:
                return copy.deepcopy(cached)
        gen_before = _manifest_cache_gen.get(cache_key, 0)
    # Scan outside lock
    manifest = rebuild_manifest_from_scan(graph_dir)
    with _manifest_cache_lock:
        # Only store if no invalidation happened during scan
        if _manifest_cache_gen.get(cache_key, 0) == gen_before:
            _manifest_cache[cache_key] = (manifest, _time.monotonic(), gen_before)
    return copy.deepcopy(manifest)


def invalidate_manifest_cache(graph_dir: Path) -> None:
    """Evict cached manifest and bump generation to prevent stale refill."""
    cache_key = str(graph_dir)
    with _manifest_cache_lock:
        _manifest_cache.pop(cache_key, None)
        _manifest_cache_gen[cache_key] = _manifest_cache_gen.get(cache_key, 0) + 1


def rebuild_manifest_from_scan(graph_dir: Path) -> Dict[str, Any]:
    """Rebuild manifest by scanning per-thread meta.json files.

    Always scans from disk — no caching. This ensures newly-created
    and deleted topics are always reflected accurately.

    Args:
        graph_dir: Base graph directory

    Returns:
        Rebuilt manifest dict
    """
    topics: Dict[str, str] = {}
    last_updated_dt: Optional[datetime] = None
    last_updated = ""
    last_topic = ""
    last_entry_id = ""

    threads_base = graph_dir / "threads"
    if threads_base.exists():
        for thread_dir in threads_base.iterdir():
            if not thread_dir.is_dir() or thread_dir.is_symlink():
                continue
            meta_file = thread_dir / "meta.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                topic = thread_dir.name
                ts_raw = meta.get("last_updated", "")
                topics[topic] = ts_raw
                # Parse timestamp for comparison (handles +00:00, Z, and other offsets)
                try:
                    ts_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    ts_dt = None
                if ts_dt and (last_updated_dt is None or ts_dt > last_updated_dt):
                    last_updated_dt = ts_dt
                    last_updated = ts_raw
                    last_topic = topic
            except Exception:
                continue

    # Resolve last_entry_id from the most-recently-updated topic.
    # Note: this is advisory/informational only — no sync cursor depends
    # on it. After a reset --hard, append order may not match timestamp
    # order, so this is a best-effort heuristic.
    if last_topic:
        entries_file = threads_base / last_topic / "entries.jsonl"
        if entries_file.exists():
            try:
                # Read last line of entries.jsonl for the most recent entry
                last_line = ""
                with open(entries_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            last_line = line.strip()
                if last_line:
                    entry = json.loads(last_line)
                    last_entry_id = entry.get("entry_id", "")
            except Exception:
                pass

    manifest: Dict[str, Any] = {
        "last_updated": last_updated,
        "last_topic": last_topic,
        "topics": topics,
    }
    if last_entry_id:
        manifest["last_entry_id"] = last_entry_id

    return manifest


def update_manifest(
    graph_dir: Path,
    topic: str,
    entry_id: Optional[str] = None,
) -> None:
    """Update manifest with last sync info.

    .. deprecated::
        Manifest is now a derived cache rebuilt from per-thread meta.json.
        This function is retained only for backward compatibility and is
        a no-op. Call sites have been removed from writer.py and sync.py.
    """
    # No-op: manifest.json is now a derived cache rebuilt on demand
    # by load_manifest() → rebuild_manifest_from_scan().
    pass


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
            if thread_dir.is_dir() and not thread_dir.is_symlink():
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
