"""Graph sync module for atomic updates after MCP writes.

This module provides functions to sync thread/entry data to the baseline graph
atomically after markdown writes. It integrates with the MCP write pipeline
to keep the graph in sync with markdown files.

Key functions:
- sync_entry_to_graph(): Upsert entry + thread nodes/edges after a write
- sync_thread_to_graph(): Full thread sync (for rebuilds)
- record_graph_sync_error(): Track sync failures for later reconciliation
- get_graph_sync_state(): Check current sync state

Feature Configuration:
    The following features are configurable and may be disabled by default:

    LLM Summaries (generate_summaries):
        - When enabled: Generates semantic summaries via LLM for entries/threads
        - When disabled: Falls back to extractive summaries (truncated body text)
        - Requires: LLM server at [servers.llm] endpoint (e.g., Ollama)
        - Config: mcp.graph.generate_summaries (default: false)
        - Env: WATERCOOLER_GRAPH_SUMMARIES

    Embedding Vectors (generate_embeddings):
        - When enabled: Generates embedding vectors for semantic search
        - When disabled: Semantic search falls back to keyword matching
        - Requires: Embedding server at [servers.embedding] endpoint
        - Config: mcp.graph.generate_embeddings (default: false)
        - Env: WATERCOOLER_GRAPH_EMBEDDINGS

    Service Auto-Detection (auto_detect_services):
        - When enabled: Checks service availability before generation
        - Gracefully skips generation if services are unavailable
        - Config: mcp.graph.auto_detect_services (default: true)

    See config.example.toml for full configuration options.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from watercooler.baseline_graph.export import (
    entry_to_node,
    generate_edges,
    thread_to_node,
)
from watercooler.baseline_graph.parser import (
    ParsedEntry,
    ParsedThread,
    parse_thread_file,
)
from watercooler.baseline_graph.summarizer import (
    SummarizerConfig,
    create_summarizer_config,
    is_llm_service_available,
    summarize_entry,
    summarize_thread,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Embedding Configuration & Generation
# ============================================================================


@dataclass
class EmbeddingConfig:
    """Embedding server configuration for real-time sync.

    Consistent with pipeline.config.EmbeddingConfig defaults.
    """

    api_base: str = "http://localhost:8080/v1"
    model: str = "bge-m3"
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "EmbeddingConfig":
        """Load from environment or credentials config."""
        try:
            from watercooler.credentials import get_server_config
            config = get_server_config("embedding")
        except ImportError:
            config = {}

        return cls(
            api_base=config.get("api_base", os.environ.get("EMBEDDING_API_BASE", cls.api_base)),
            model=config.get("model", os.environ.get("EMBEDDING_MODEL", cls.model)),
            timeout=config.get("timeout", float(os.environ.get("EMBEDDING_TIMEOUT", cls.timeout))),
        )


def is_embedding_available(config: Optional[EmbeddingConfig] = None) -> bool:
    """Check if embedding service is available."""
    config = config or EmbeddingConfig.from_env()

    try:
        import httpx
        url = f"{config.api_base.rstrip('/')}/models"
        with httpx.Client(timeout=5.0) as client:
            response = client.get(url)
            return response.status_code == 200
    except Exception as e:
        logger.debug(f"Embedding service not available: {e}")
        return False


def generate_embedding(
    text: str,
    config: Optional[EmbeddingConfig] = None,
) -> Optional[List[float]]:
    """Generate embedding vector for text.

    Args:
        text: Text to embed (summary preferred, or truncated body)
        config: Embedding configuration

    Returns:
        Embedding vector or None on failure
    """
    config = config or EmbeddingConfig.from_env()

    try:
        import httpx
        url = f"{config.api_base.rstrip('/')}/embeddings"

        with httpx.Client(timeout=config.timeout) as client:
            response = client.post(url, json={
                "model": config.model,
                "input": text[:2000],
            })
            response.raise_for_status()
            data = response.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        logger.debug(f"Failed to generate embedding: {e}")
        return None


def _should_auto_start_services() -> bool:
    """Check if auto-start services is enabled via env var.

    Returns:
        True if WATERCOOLER_AUTO_START_SERVICES is set to a truthy value
    """
    return os.environ.get("WATERCOOLER_AUTO_START_SERVICES", "").lower() in ("1", "true", "yes")


def _try_auto_start_service(service_type: str, api_base: str) -> bool:
    """Attempt to auto-start a service using ServerManager.

    Args:
        service_type: "llm" or "embedding"
        api_base: API base URL for the service

    Returns:
        True if service was started or already running, False otherwise
    """
    if not _should_auto_start_services():
        return False

    try:
        from watercooler_memory.pipeline.server_manager import ServerManager
        manager = ServerManager(
            llm_api_base=api_base if service_type == "llm" else "http://localhost:11434/v1",
            embedding_api_base=api_base if service_type == "embedding" else "http://localhost:8080/v1",
            interactive=False,
            auto_approve=True,
            verbose=False,
        )
        if service_type == "llm":
            if manager.check_llm_server():
                return True
            return manager.start_llm_server()
        else:
            if manager.check_embedding_server():
                return True
            return manager.start_embedding_server()
    except ImportError:
        logger.debug(
            f"WATERCOOLER_AUTO_START_SERVICES is enabled but ServerManager not available. "
            f"Cannot auto-start {service_type} service."
        )
        return False
    except Exception as e:
        logger.debug(f"Failed to auto-start {service_type} service: {e}")
        return False


# ============================================================================
# Arc Change Detection for Thread Summary Updates
# ============================================================================


def should_update_thread_summary(
    parsed: ParsedThread,
    new_entry: ParsedEntry,
    previous_entry_count: int,
) -> bool:
    """Determine if thread summary should be regenerated.

    Thread summaries update when the arc changes significantly:
    - Closure entries (thread conclusion)
    - Decision entries (major milestones)
    - Significant growth (50%+ more entries)
    - First few entries (establishing context)

    Args:
        parsed: Parsed thread with all entries
        new_entry: The newly added entry
        previous_entry_count: Entry count before this addition

    Returns:
        True if thread summary should be regenerated
    """
    # Always generate for first 3 entries
    if len(parsed.entries) <= 3:
        return True

    # Arc-changing entry types
    arc_changing_types = {"Closure", "Decision", "Plan"}
    if new_entry.entry_type in arc_changing_types:
        return True

    # Significant growth (50% more entries)
    if previous_entry_count > 0:
        growth_ratio = len(parsed.entries) / previous_entry_count
        if growth_ratio >= 1.5:
            return True

    # Every 10th entry
    if len(parsed.entries) % 10 == 0:
        return True

    return False


def get_previous_thread_state(
    threads_dir: Path,
    topic: str,
) -> tuple[int, Optional[str]]:
    """Get previous thread state from graph.

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        Tuple of (entry_count, thread_summary) from existing graph
    """
    graph_dir = threads_dir / "graph" / "baseline"
    nodes_file = graph_dir / "nodes.jsonl"

    if not nodes_file.exists():
        return 0, None

    try:
        with open(nodes_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                node = json.loads(line)
                if node.get("type") == "thread" and node.get("topic") == topic:
                    return node.get("entry_count", 0), node.get("summary")
    except Exception as e:
        logger.debug(f"Failed to read previous thread state: {e}")

    return 0, None


# ============================================================================
# Graph Sync State
# ============================================================================


@dataclass
class GraphSyncState:
    """State of graph synchronization for a thread."""

    status: str = "ok"  # ok, error, pending
    last_synced_entry_id: Optional[str] = None
    last_sync_at: Optional[str] = None
    error_message: Optional[str] = None
    entries_synced: int = 0


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _get_state_file(threads_dir: Path) -> Path:
    """Get path to graph sync state file."""
    graph_dir = threads_dir / "graph" / "baseline"
    return graph_dir / "sync_state.json"


def get_graph_sync_state(threads_dir: Path, topic: str) -> Optional[GraphSyncState]:
    """Get graph sync state for a topic.

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        GraphSyncState or None if no state exists
    """
    state_file = _get_state_file(threads_dir)
    if not state_file.exists():
        return None

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        topic_state = data.get("topics", {}).get(topic)
        if topic_state:
            return GraphSyncState(
                status=topic_state.get("status", "ok"),
                last_synced_entry_id=topic_state.get("last_synced_entry_id"),
                last_sync_at=topic_state.get("last_sync_at"),
                error_message=topic_state.get("error_message"),
                entries_synced=topic_state.get("entries_synced", 0),
            )
    except Exception as e:
        logger.warning(f"Failed to read graph sync state: {e}")

    return None


def _update_graph_sync_state(
    threads_dir: Path,
    topic: str,
    state: GraphSyncState,
) -> None:
    """Update graph sync state for a topic.

    Uses atomic write (temp file + rename) to prevent corruption.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        state: New state
    """
    state_file = _get_state_file(threads_dir)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing state
    data: Dict[str, Any] = {"topics": {}, "last_updated": _now_iso()}
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Update topic state
    if "topics" not in data:
        data["topics"] = {}

    data["topics"][topic] = {
        "status": state.status,
        "last_synced_entry_id": state.last_synced_entry_id,
        "last_sync_at": state.last_sync_at,
        "error_message": state.error_message,
        "entries_synced": state.entries_synced,
    }
    data["last_updated"] = _now_iso()

    # Atomic write
    _atomic_write_json(state_file, data)


def record_graph_sync_error(
    threads_dir: Path,
    topic: str,
    entry_id: Optional[str],
    error: Exception,
) -> None:
    """Record a graph sync error for later reconciliation.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        entry_id: Entry ID that failed to sync (if known)
        error: The exception that occurred
    """
    existing = get_graph_sync_state(threads_dir, topic)
    state = GraphSyncState(
        status="error",
        last_synced_entry_id=existing.last_synced_entry_id if existing else None,
        last_sync_at=_now_iso(),
        error_message=str(error),
        entries_synced=existing.entries_synced if existing else 0,
    )
    _update_graph_sync_state(threads_dir, topic, state)
    logger.warning(f"Graph sync error recorded for {topic}: {error}")


# ============================================================================
# Atomic File Operations
# ============================================================================


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON file atomically using temp file + rename.

    Args:
        path: Target path
        data: Data to write as JSON
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file in same directory (for atomic rename)
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
        # Clean up temp file on error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_append_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    """Append items to JSONL file atomically.

    This reads existing content, appends new items, and writes atomically.
    For incremental sync, we need to handle deduplication.

    Args:
        path: Target JSONL path
        items: Items to append (will be deduplicated by 'id' field)
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing items
    existing: Dict[str, Dict[str, Any]] = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        item = json.loads(line)
                        item_id = item.get("id") or item.get("source", "") + item.get("target", "")
                        if item_id:
                            existing[item_id] = item
        except Exception as e:
            logger.warning(f"Failed to read existing JSONL {path}: {e}")

    # Merge new items (upsert)
    for item in items:
        item_id = item.get("id") or item.get("source", "") + item.get("target", "")
        if item_id:
            existing[item_id] = item

    # Write atomically
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".tmp_",
        suffix=".jsonl",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for item in existing.values():
                f.write(json.dumps(item) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================================
# Graph Sync Functions
# ============================================================================


def sync_entry_to_graph(
    threads_dir: Path,
    topic: str,
    entry_id: Optional[str] = None,
    generate_summaries: bool = False,
    generate_embeddings: bool = False,
) -> bool:
    """Sync a single entry to the graph after an MCP write.

    This function:
    1. Parses the thread file
    2. Generates entry summary (if enabled)
    3. Generates entry embedding (if enabled)
    4. Optionally updates thread summary (if arc changed)
    5. Upserts the entry node (and thread node)
    6. Updates edges (contains, followed_by)
    7. Updates sync state

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        entry_id: Specific entry ID to sync (or None for latest)
        generate_summaries: Whether to generate LLM summaries
        generate_embeddings: Whether to generate embedding vectors

    Returns:
        True if sync succeeded, False otherwise
    """
    thread_path = threads_dir / f"{topic}.md"
    if not thread_path.exists():
        logger.warning(f"Thread file not found for sync: {thread_path}")
        return False

    try:
        # Get previous thread state for arc change detection
        prev_entry_count, prev_thread_summary = get_previous_thread_state(threads_dir, topic)

        # Parse thread (without generating summaries during parse - we do it here)
        parsed = parse_thread_file(
            thread_path,
            config=None,
            generate_summaries=False,  # Generate summaries in sync, not parse
        )
        if not parsed:
            logger.warning(f"Failed to parse thread for sync: {topic}")
            return False

        # Find the entry to sync
        if entry_id:
            entry = next((e for e in parsed.entries if e.entry_id == entry_id), None)
            if not entry:
                # Entry ID not found, sync full thread
                logger.debug(f"Entry {entry_id} not found, syncing full thread")
                return sync_thread_to_graph(
                    threads_dir, topic, generate_summaries, generate_embeddings
                )
        else:
            # Sync latest entry
            entry = parsed.entries[-1] if parsed.entries else None

        if not entry:
            logger.warning(f"No entries found in thread: {topic}")
            return False

        # Generate entry summary if enabled
        summarizer_config = None
        llm_available = False
        if generate_summaries and not entry.summary:
            summarizer_config = create_summarizer_config()
            llm_available = is_llm_service_available(summarizer_config)

            # Try auto-start if unavailable and enabled
            if not llm_available and _try_auto_start_service("llm", summarizer_config.api_base):
                llm_available = is_llm_service_available(summarizer_config)

            if llm_available:
                entry.summary = summarize_entry(
                    entry.body,
                    entry_title=entry.title,
                    entry_type=entry.entry_type,
                    config=summarizer_config,
                )
                if entry.summary:
                    logger.debug(f"Generated summary for entry {entry.entry_id}")
            else:
                logger.warning(
                    f"LLM service unavailable at {summarizer_config.api_base}. "
                    "Skipping summary generation. To enable summaries: "
                    "1) Start Ollama: 'ollama serve' "
                    "2) Or set WATERCOOLER_AUTO_START_SERVICES=true"
                )

        # Generate entry embedding if enabled
        entry_embedding = None
        if generate_embeddings:
            embed_config = EmbeddingConfig.from_env()
            embed_available = is_embedding_available(embed_config)

            # Try auto-start if unavailable and enabled
            if not embed_available and _try_auto_start_service("embedding", embed_config.api_base):
                embed_available = is_embedding_available(embed_config)

            if embed_available:
                # Use summary for embedding if available, otherwise truncated body
                embed_text = entry.summary if entry.summary else entry.body[:500]
                entry_embedding = generate_embedding(embed_text)
                if entry_embedding:
                    logger.debug(f"Generated embedding for entry {entry.entry_id}")
            else:
                logger.warning(
                    f"Embedding service unavailable at {embed_config.api_base}. "
                    "Skipping embedding generation. To enable embeddings: "
                    "1) Start llama.cpp server with embedding model "
                    "2) Or set WATERCOOLER_AUTO_START_SERVICES=true"
                )

        # Check if thread summary needs update (arc change detection)
        update_thread_summary = False
        if generate_summaries:
            # Ensure we have config and availability check
            if summarizer_config is None:
                summarizer_config = create_summarizer_config()
                llm_available = is_llm_service_available(summarizer_config)

            update_thread_summary = should_update_thread_summary(
                parsed, entry, prev_entry_count
            )
            if update_thread_summary and llm_available:
                # Convert entries to dict format for summarize_thread
                entries_for_summary = [
                    {
                        "title": e.title,
                        "body": e.body,
                        "entry_type": e.entry_type,
                        "agent": e.agent,
                    }
                    for e in parsed.entries
                ]
                parsed.summary = summarize_thread(
                    entries_for_summary,
                    thread_title=parsed.title,
                    config=summarizer_config,
                )
                if parsed.summary:
                    logger.debug(f"Updated thread summary for {topic} (arc change)")
            elif prev_thread_summary:
                # Preserve existing thread summary
                parsed.summary = prev_thread_summary

        # Prepare graph output directory
        graph_dir = threads_dir / "graph" / "baseline"
        nodes_file = graph_dir / "nodes.jsonl"
        edges_file = graph_dir / "edges.jsonl"

        # Build entry node with embedding if available
        entry_node = entry_to_node(entry, topic)
        if entry_embedding:
            entry_node["embedding"] = entry_embedding

        # Build nodes (thread + entry)
        nodes = [
            thread_to_node(parsed),
            entry_node,
        ]

        # Build edges for this entry
        edges = []
        thread_id = f"thread:{topic}"
        entry_node_id = f"entry:{entry.entry_id}"

        # Thread contains entry
        edges.append({
            "source": thread_id,
            "target": entry_node_id,
            "type": "contains",
        })

        # Find previous entry for followed_by edge
        # Note: entry.index is the position in the thread (0-based)
        # We look for the entry at index-1 to create a followed_by edge
        if entry.index > 0:
            prev_entry: Optional[ParsedEntry] = None
            prev_idx = entry.index - 1
            # First try direct list access if entries are in order
            if prev_idx < len(parsed.entries):
                candidate = parsed.entries[prev_idx]
                if candidate.index == prev_idx:
                    prev_entry = candidate
            # Fallback: search by index attribute (handles sparse/reordered lists)
            if prev_entry is None:
                for e in parsed.entries:
                    if e.index == prev_idx:
                        prev_entry = e
                        break
            if prev_entry and prev_entry.entry_id:
                edges.append({
                    "source": f"entry:{prev_entry.entry_id}",
                    "target": entry_node_id,
                    "type": "followed_by",
                })

        # Atomic writes
        _atomic_append_jsonl(nodes_file, nodes)
        _atomic_append_jsonl(edges_file, edges)

        # Update manifest
        _update_manifest(graph_dir, topic, entry.entry_id)

        # Update sync state
        state = GraphSyncState(
            status="ok",
            last_synced_entry_id=entry.entry_id,
            last_sync_at=_now_iso(),
            error_message=None,
            entries_synced=(get_graph_sync_state(threads_dir, topic) or GraphSyncState()).entries_synced + 1,
        )
        _update_graph_sync_state(threads_dir, topic, state)

        logger.debug(f"Graph sync complete for {topic}/{entry.entry_id}")

        # Call memory backend hook (non-blocking - errors logged, never raise)
        sync_to_memory_backend(
            threads_dir=threads_dir,
            topic=topic,
            entry_id=entry.entry_id,
            entry_body=entry.body,
            entry_title=entry.title,
            timestamp=entry.timestamp,
        )

        return True

    except Exception as e:
        logger.error(f"Graph sync failed for {topic}: {e}")
        record_graph_sync_error(threads_dir, topic, entry_id, e)
        return False


def sync_thread_to_graph(
    threads_dir: Path,
    topic: str,
    generate_summaries: bool = False,
    generate_embeddings: bool = False,
) -> bool:
    """Sync an entire thread to the graph.

    This is a full resync - useful for reconciliation or initial build.
    Generates summaries and embeddings for all entries if enabled.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        generate_summaries: Whether to generate LLM summaries
        generate_embeddings: Whether to generate embedding vectors

    Returns:
        True if sync succeeded, False otherwise
    """
    thread_path = threads_dir / f"{topic}.md"
    if not thread_path.exists():
        logger.warning(f"Thread file not found for sync: {thread_path}")
        return False

    try:
        # Parse thread (summaries generated in sync, not parse)
        parsed = parse_thread_file(
            thread_path,
            config=None,
            generate_summaries=False,
        )
        if not parsed:
            logger.warning(f"Failed to parse thread for sync: {topic}")
            return False

        # Generate summaries for all entries if enabled
        if generate_summaries:
            summarizer_config = create_summarizer_config()
            for entry in parsed.entries:
                if not entry.summary:
                    entry.summary = summarize_entry(
                        entry.body,
                        entry_title=entry.title,
                        entry_type=entry.entry_type,
                        config=summarizer_config,
                    )

            # Generate thread summary
            entries_for_summary = [
                {
                    "title": e.title,
                    "body": e.body,
                    "entry_type": e.entry_type,
                    "agent": e.agent,
                }
                for e in parsed.entries
            ]
            parsed.summary = summarize_thread(
                entries_for_summary,
                thread_title=parsed.title,
                config=summarizer_config,
            )
            logger.debug(f"Generated summaries for {len(parsed.entries)} entries in {topic}")

        # Prepare graph output directory
        graph_dir = threads_dir / "graph" / "baseline"
        nodes_file = graph_dir / "nodes.jsonl"
        edges_file = graph_dir / "edges.jsonl"

        # Build all nodes with optional embeddings
        nodes = [thread_to_node(parsed)]
        for entry in parsed.entries:
            entry_node = entry_to_node(entry, topic)

            # Generate embedding if enabled
            if generate_embeddings:
                embed_text = entry.summary if entry.summary else entry.body[:500]
                embedding = generate_embedding(embed_text)
                if embedding:
                    entry_node["embedding"] = embedding

            nodes.append(entry_node)

        if generate_embeddings:
            logger.debug(f"Generated embeddings for entries in {topic}")

        # Build all edges
        edges = list(generate_edges(parsed))

        # Atomic writes
        _atomic_append_jsonl(nodes_file, nodes)
        _atomic_append_jsonl(edges_file, edges)

        # Update manifest
        last_entry_id = parsed.entries[-1].entry_id if parsed.entries else None
        _update_manifest(graph_dir, topic, last_entry_id)

        # Update sync state
        state = GraphSyncState(
            status="ok",
            last_synced_entry_id=last_entry_id,
            last_sync_at=_now_iso(),
            error_message=None,
            entries_synced=len(parsed.entries),
        )
        _update_graph_sync_state(threads_dir, topic, state)

        logger.debug(f"Full thread sync complete for {topic}: {len(parsed.entries)} entries")
        return True

    except Exception as e:
        logger.error(f"Thread sync failed for {topic}: {e}")
        record_graph_sync_error(threads_dir, topic, None, e)
        return False


def _update_manifest(graph_dir: Path, topic: str, entry_id: Optional[str]) -> None:
    """Update the manifest file with sync metadata.

    Args:
        graph_dir: Graph output directory
        topic: Thread topic that was synced
        entry_id: Last synced entry ID
    """
    manifest_path = graph_dir / "manifest.json"

    # Load existing manifest
    manifest: Dict[str, Any] = {
        "schema_version": "1.0",
        "created_at": _now_iso(),
        "last_updated": _now_iso(),
        "topics_synced": {},
    }
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Update
    manifest["last_updated"] = _now_iso()
    if "topics_synced" not in manifest:
        manifest["topics_synced"] = {}
    manifest["topics_synced"][topic] = {
        "last_entry_id": entry_id,
        "synced_at": _now_iso(),
    }

    # Atomic write
    _atomic_write_json(manifest_path, manifest)


# ============================================================================
# Health Check & Reconciliation
# ============================================================================


@dataclass
class GraphHealthReport:
    """Health report for graph sync status."""

    healthy: bool = True
    total_threads: int = 0
    synced_threads: int = 0
    error_threads: int = 0
    pending_threads: int = 0
    stale_threads: List[str] = field(default_factory=list)
    error_details: Dict[str, str] = field(default_factory=dict)


def check_graph_health(threads_dir: Path) -> GraphHealthReport:
    """Check graph sync health for all threads.

    Returns:
        GraphHealthReport with status of all threads
    """
    report = GraphHealthReport()

    # Count total threads
    thread_files = list(threads_dir.glob("*.md"))
    report.total_threads = len(thread_files)

    # Load sync state
    state_file = _get_state_file(threads_dir)
    if not state_file.exists():
        # No sync state = all threads need sync
        report.stale_threads = [f.stem for f in thread_files]
        report.healthy = False
        return report

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        topic_states = data.get("topics", {})
    except Exception as e:
        report.healthy = False
        report.error_details["state_file"] = str(e)
        return report

    # Check each thread
    for thread_file in thread_files:
        topic = thread_file.stem
        topic_state = topic_states.get(topic)

        if not topic_state:
            report.stale_threads.append(topic)
            continue

        status = topic_state.get("status", "ok")
        if status == "ok":
            report.synced_threads += 1
        elif status == "error":
            report.error_threads += 1
            report.error_details[topic] = topic_state.get("error_message", "Unknown error")
        elif status == "pending":
            report.pending_threads += 1

    report.healthy = (
        report.error_threads == 0
        and report.pending_threads == 0
        and len(report.stale_threads) == 0
    )

    return report


def reconcile_graph(
    threads_dir: Path,
    topics: Optional[List[str]] = None,
    generate_summaries: bool = False,
    generate_embeddings: bool = False,
) -> Dict[str, bool]:
    """Reconcile graph with markdown files.

    Rebuilds graph nodes/edges for specified topics or all stale/error topics.

    Args:
        threads_dir: Threads directory
        topics: Specific topics to reconcile (or None for all stale/error)
        generate_summaries: Whether to generate LLM summaries
        generate_embeddings: Whether to generate embedding vectors

    Returns:
        Dict mapping topic to success/failure
    """
    results: Dict[str, bool] = {}

    if topics is None:
        # Find all stale/error topics
        health = check_graph_health(threads_dir)
        topics = health.stale_threads + list(health.error_details.keys())

    for topic in topics:
        success = sync_thread_to_graph(
            threads_dir, topic, generate_summaries, generate_embeddings
        )
        results[topic] = success

    return results


# ============================================================================
# Memory Backend Sync Hook (Milestone 5.3)
# ============================================================================
# Memory Backend Callback Registry
# ============================================================================


class MemorySyncCallback(Protocol):
    """Protocol defining the memory sync callback signature.

    Callbacks are invoked by sync_to_memory_backend when an entry needs to be
    synced to a memory backend. This Protocol provides explicit type checking
    for callback implementations.
    """

    def __call__(
        self,
        threads_dir: Path,
        topic: str,
        entry_id: str,
        entry_body: str,
        entry_title: Optional[str],
        timestamp: Optional[str],
        agent: Optional[str],
        role: Optional[str],
        entry_type: Optional[str],
        backend_config: Dict[str, Any],
        log: logging.Logger,
        dry_run: bool = False,
    ) -> bool:
        """Sync an entry to a memory backend.

        Args:
            threads_dir: Threads directory
            topic: Thread topic (used as group_id)
            entry_id: Entry ID for provenance tracking
            entry_body: Entry content to sync
            entry_title: Optional entry title
            timestamp: Entry timestamp (ISO 8601)
            agent: Agent name
            role: Agent role
            entry_type: Entry type
            backend_config: Backend configuration dict
            log: Logger instance
            dry_run: If True, simulate without actual sync

        Returns:
            True on success, False on failure
        """
        ...


# Registry for memory backend sync callbacks
# Callbacks are registered by backend implementations (e.g., in watercooler_mcp.memory_sync)
_memory_sync_callbacks: Dict[str, MemorySyncCallback] = {}


def register_memory_sync_callback(
    backend_name: str,
    callback: MemorySyncCallback,
) -> None:
    """Register a sync callback for a memory backend.

    Callbacks are invoked by sync_to_memory_backend when an entry needs to be
    synced. This allows backend-specific implementations to be decoupled from
    the core baseline_graph module.

    Args:
        backend_name: Backend identifier (e.g., "graphiti", "leanrag")
        callback: Function implementing MemorySyncCallback protocol

    Example:
        def my_graphiti_sync(threads_dir, topic, entry_id, entry_body, ...):
            # Sync to Graphiti
            return True

        register_memory_sync_callback("graphiti", my_graphiti_sync)
    """
    _memory_sync_callbacks[backend_name] = callback
    logger.debug(f"MEMORY: Registered sync callback for backend '{backend_name}'")


def unregister_memory_sync_callback(backend_name: str) -> None:
    """Remove a registered sync callback.

    Args:
        backend_name: Backend identifier to remove
    """
    if backend_name in _memory_sync_callbacks:
        del _memory_sync_callbacks[backend_name]
        logger.debug(f"MEMORY: Unregistered sync callback for backend '{backend_name}'")


def get_registered_backends() -> list[str]:
    """Get list of registered backend names.

    Returns:
        List of backend names with registered callbacks
    """
    return list(_memory_sync_callbacks.keys())


# ============================================================================
# Memory Backend Configuration
# ============================================================================


def is_memory_disabled() -> bool:
    """Check if memory backends are disabled.

    When WATERCOOLER_MEMORY_DISABLED=1 is set, all memory backend functionality
    is bypassed. This is useful for:
    - Non-memory workflows that don't need graph backends
    - CI environments where memory servers aren't available
    - Quick local testing without server dependencies

    Returns:
        True if memory is disabled, False otherwise
    """
    return os.environ.get("WATERCOOLER_MEMORY_DISABLED", "").lower() in ("1", "true", "yes")


def get_memory_backend_config() -> Optional[Dict[str, Any]]:
    """Get memory backend configuration from environment.

    Configuration via WATERCOOLER_MEMORY_BACKEND env var:
    - "graphiti": Sync to Graphiti temporal graph
    - "leanrag": Trigger LeanRAG clustering pipeline

    Returns:
        Config dict with backend name, or None if disabled
    """
    # Check master disable switch first
    if is_memory_disabled():
        logger.debug("MEMORY: Disabled (WATERCOOLER_MEMORY_DISABLED=1)")
        return None

    backend = os.environ.get("WATERCOOLER_MEMORY_BACKEND", "").lower().strip()

    if not backend:
        return None

    if backend not in ("graphiti", "leanrag"):
        logger.warning(f"Unknown memory backend: {backend}. Supported: graphiti, leanrag")
        return None

    return {"backend": backend}


# NOTE: Graphiti-specific functions (_call_graphiti_add_episode, _sync_graphiti_blocking)
# have been moved to src/watercooler_mcp/memory_sync.py as part of Issue #83.
# See register_memory_sync_callback() for the new callback-based architecture.


# Module-level thread pool for fire-and-forget memory sync
# Lazy initialization to avoid creating threads if memory backend is never used
_sync_executor: Optional[ThreadPoolExecutor] = None
_sync_executor_lock = threading.Lock()  # Static lock - avoids race condition
_sync_executor_shutdown_registered = False


def _shutdown_sync_executor() -> None:
    """Gracefully shutdown the sync executor on process exit."""
    global _sync_executor
    if _sync_executor is not None:
        try:
            _sync_executor.shutdown(wait=True)
        except Exception:
            # Ignore errors during shutdown (process is exiting anyway)
            pass
        _sync_executor = None


def _get_sync_executor() -> ThreadPoolExecutor:
    """Get or create the sync executor (lazy initialization).

    Uses double-checked locking pattern with a static module-level lock.
    Registers an atexit handler on first executor creation for graceful shutdown.
    """
    global _sync_executor, _sync_executor_shutdown_registered

    if _sync_executor is None:
        with _sync_executor_lock:
            if _sync_executor is None:
                _sync_executor = ThreadPoolExecutor(
                    max_workers=2,
                    thread_name_prefix="memory_sync"
                )
                # Register shutdown handler only once
                if not _sync_executor_shutdown_registered:
                    atexit.register(_shutdown_sync_executor)
                    _sync_executor_shutdown_registered = True
    return _sync_executor


def sync_to_memory_backend(
    threads_dir: Path,
    topic: str,
    entry_id: str,
    entry_body: str,
    entry_title: Optional[str] = None,
    timestamp: Optional[str] = None,
    agent: Optional[str] = None,
    role: Optional[str] = None,
    entry_type: Optional[str] = None,
    dry_run: bool = False,
) -> bool:
    """Sync an entry to the configured memory backend using registered callbacks.

    This function dispatches to registered callbacks based on the configured
    backend. Work is submitted to a thread pool for fire-and-forget execution.
    Errors are logged but never raise.

    Args:
        threads_dir: Threads directory
        topic: Thread topic (used as group_id)
        entry_id: Entry ID for provenance tracking
        entry_body: Entry content to sync
        entry_title: Optional entry title
        timestamp: Optional entry timestamp (ISO 8601)
        agent: Optional agent name
        role: Optional agent role
        entry_type: Optional entry type (Note, Plan, etc.)
        dry_run: If True, simulate without actual sync

    Returns:
        True if sync was submitted/simulated, False if disabled or no callback
    """
    config = get_memory_backend_config()
    if config is None:
        return False

    backend = config["backend"]

    # Check if callback is registered
    if backend not in _memory_sync_callbacks:
        logger.debug(
            f"MEMORY: No callback registered for backend '{backend}'. "
            f"Registered: {list(_memory_sync_callbacks.keys())}"
        )
        return False

    callback = _memory_sync_callbacks[backend]

    try:
        # Submit to thread pool for fire-and-forget execution
        executor = _get_sync_executor()
        executor.submit(
            callback,
            threads_dir,
            topic,
            entry_id,
            entry_body,
            entry_title,
            timestamp,
            agent,
            role,
            entry_type,
            config,
            logger,
            dry_run,
        )
        logger.debug(f"MEMORY: Submitted {backend} sync for {topic}/{entry_id}")
        return True

    except Exception as e:
        logger.warning(f"MEMORY: Sync failed for {topic}/{entry_id}: {e}")
        return False
