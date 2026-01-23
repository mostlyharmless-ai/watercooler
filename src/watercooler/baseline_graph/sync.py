"""Graph sync module for enrichment and reconciliation.

In the graph-first architecture, the graph is the source of truth:

1. commands_graph.py writes entry/thread data to graph first
2. projector.py creates markdown as a derived projection
3. This module adds ENRICHMENT (summaries, embeddings) to graph entries

Key functions:
- enrich_graph_entry(): Add LLM summaries and embeddings to existing graph entry
- sync_thread_to_graph(): Full thread sync (for migration/reconciliation)
- record_graph_sync_error(): Track sync failures for later reconciliation
- get_graph_sync_state(): Check current sync state

DEPRECATED (MD-first era):
- sync_entry_to_graph(): Reads from markdown - use enrich_graph_entry() instead

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
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from watercooler.baseline_graph import storage
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
from watercooler.baseline_graph.writer import (
    get_thread_from_graph,
    get_entry_node_from_graph,
    get_entries_for_thread,
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
# Constants
# ============================================================================

# Default max characters for embedding text when no summary is available.
# This should be tuned based on the embedding model's token limit.
# Most models support 512-8192 tokens; 500 chars is ~100-150 tokens.
DEFAULT_EMBEDDING_TEXT_MAX_CHARS = 500


# ============================================================================
# Enrichment Result Type
# ============================================================================


@dataclass
class EnrichmentResult:
    """Result of an enrichment operation.

    Provides detailed status instead of just bool to distinguish:
    - success: Enrichment completed and data was written
    - noop: No enrichment needed (already exists or services unavailable)
    - error: Enrichment failed
    """

    success: bool
    summary_generated: bool = False
    embedding_generated: bool = False
    error_message: Optional[str] = None

    @property
    def is_noop(self) -> bool:
        """True if operation succeeded but no enrichment was generated."""
        return self.success and not self.summary_generated and not self.embedding_generated

    @classmethod
    def noop(cls) -> "EnrichmentResult":
        """Create a no-op result (success, but nothing generated)."""
        return cls(success=True)

    @classmethod
    def error(cls, message: str) -> "EnrichmentResult":
        """Create an error result."""
        return cls(success=False, error_message=message)


# ============================================================================
# Embedding Configuration & Generation
# ============================================================================


def _get_default_embedding_api_base() -> str:
    """Get default embedding API base from unified config (checks env vars first)."""
    from watercooler.memory_config import resolve_baseline_graph_embedding_config
    return resolve_baseline_graph_embedding_config().api_base


def _get_default_embedding_model() -> str:
    """Get default embedding model from unified config (checks env vars first)."""
    from watercooler.memory_config import resolve_baseline_graph_embedding_config
    return resolve_baseline_graph_embedding_config().model


@dataclass
class EmbeddingConfig:
    """Embedding server configuration for real-time sync.

    Settings are resolved via unified config with priority:
    1. Environment variables (EMBEDDING_API_BASE, EMBEDDING_MODEL)
    2. Legacy env vars (BASELINE_GRAPH_EMBEDDING_API_BASE, etc.)
    3. TOML config ([memory.embedding])
    4. Built-in defaults (localhost:8080 for llama.cpp)
    """

    api_base: str = field(default_factory=_get_default_embedding_api_base)
    model: str = field(default_factory=_get_default_embedding_model)
    timeout: float = 30.0
    max_text_chars: int = DEFAULT_EMBEDDING_TEXT_MAX_CHARS

    @classmethod
    def from_env(cls) -> "EmbeddingConfig":
        """Load from unified config (checks env vars, TOML, then defaults)."""
        from watercooler.memory_config import resolve_baseline_graph_embedding_config
        embed_config = resolve_baseline_graph_embedding_config()

        timeout = cls.timeout
        if timeout_str := os.environ.get("EMBEDDING_TIMEOUT"):
            try:
                timeout = float(timeout_str)
            except ValueError:
                pass

        return cls(
            api_base=embed_config.api_base,
            model=embed_config.model,
            timeout=timeout,
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
        from watercooler.memory_config import (
            resolve_baseline_graph_llm_config,
            resolve_baseline_graph_embedding_config,
        )
        from watercooler_memory.pipeline.server_manager import ServerManager

        # Use unified config for URLs instead of hardcoded defaults
        llm_config = resolve_baseline_graph_llm_config()
        embedding_config = resolve_baseline_graph_embedding_config()

        manager = ServerManager(
            llm_api_base=api_base if service_type == "llm" else llm_config.api_base,
            embedding_api_base=api_base if service_type == "embedding" else embedding_config.api_base,
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
    graph_dir = storage.get_graph_dir(threads_dir)
    meta = storage.load_thread_meta(graph_dir, topic)

    if meta:
        return meta.get("entry_count", 0), meta.get("summary")

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
        # Set readable permissions before rename (mkstemp creates with 0600)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================================
# Graph Sync Functions
# ============================================================================


def sync_entry_structure_only(
    threads_dir: Path,
    topic: str,
    entry_id: str,
) -> bool:
    """Create minimal graph node for entry without LLM/embedding generation.

    This is called BEFORE the git commit to ensure graph files are included
    in the same commit as the thread markdown file. It creates:
    - Entry node (with empty summary, no embedding)
    - Thread node (upsert)
    - Contains edge (thread -> entry)
    - Followed_by edge (prev_entry -> entry) if applicable

    Unlike sync_entry_to_graph(), this function:
    - NEVER calls LLM for summaries
    - NEVER generates embeddings
    - Is designed to be BLOCKING (errors should propagate)
    - Creates minimal but valid graph structure

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        entry_id: The entry ID to sync (required, not optional)

    Returns:
        True if sync succeeded, False otherwise
    """
    thread_path = threads_dir / f"{topic}.md"
    if not thread_path.exists():
        logger.warning(f"Thread file not found for structural sync: {thread_path}")
        return False

    try:
        # Parse thread
        parsed = parse_thread_file(
            thread_path,
            config=None,
            generate_summaries=False,
        )
        if not parsed:
            logger.warning(f"Failed to parse thread for structural sync: {topic}")
            return False

        # Find the entry
        entry = next((e for e in parsed.entries if e.entry_id == entry_id), None)
        if not entry:
            logger.warning(f"Entry {entry_id} not found in thread {topic}")
            return False

        # Get graph directory and load existing per-thread data
        graph_dir = storage.ensure_graph_dir(threads_dir)

        # Load existing data (or empty dicts if new thread)
        meta = storage.load_thread_meta(graph_dir, topic) or {}
        entries = storage.load_thread_entries_dict(graph_dir, topic)
        edges = storage.load_thread_edges(graph_dir, topic)

        thread_id = f"thread:{topic}"
        entry_node_id = f"entry:{entry.entry_id}"

        # Build entry node (no embedding, empty summary if not present)
        entry_node = entry_to_node(entry, topic)
        if "summary" not in entry_node:
            entry_node["summary"] = ""

        # Update entries dict
        entries[entry_node_id] = entry_node

        # Build/update thread meta
        thread_node = thread_to_node(parsed)
        meta.update(thread_node)

        # Add contains edge
        contains_edge_id = thread_id + entry_node_id
        edges[contains_edge_id] = {
            "source": thread_id,
            "target": entry_node_id,
            "type": "contains",
        }

        # Find previous entry for followed_by edge
        if entry.index > 0:
            prev_entry: Optional[ParsedEntry] = None
            prev_idx = entry.index - 1
            # First try direct list access if entries are in order
            if prev_idx < len(parsed.entries):
                candidate = parsed.entries[prev_idx]
                if candidate.index == prev_idx:
                    prev_entry = candidate
            # Fallback: search by index attribute
            if prev_entry is None:
                for e in parsed.entries:
                    if e.index == prev_idx:
                        prev_entry = e
                        break
            if prev_entry and prev_entry.entry_id:
                prev_entry_node_id = f"entry:{prev_entry.entry_id}"
                followed_by_edge_id = prev_entry_node_id + entry_node_id
                edges[followed_by_edge_id] = {
                    "source": prev_entry_node_id,
                    "target": entry_node_id,
                    "type": "followed_by",
                }

        # Write all per-thread files atomically
        storage.write_thread_graph(graph_dir, topic, meta, entries, edges)

        # Update manifest
        storage.update_manifest(graph_dir, topic, entry.entry_id)

        logger.debug(f"Structural graph sync complete for {topic}/{entry_id}")
        return True

    except Exception as e:
        logger.exception(f"Structural graph sync failed for {topic}/{entry_id}: {e}")
        return False


def enrich_graph_entry(
    threads_dir: Path,
    topic: str,
    entry_id: str,
    generate_summaries: bool = False,
    generate_embeddings: bool = False,
) -> EnrichmentResult:
    """Enrich an existing graph entry with summaries and embeddings.

    This function reads from the GRAPH (not markdown) and adds enrichment data.
    It is designed for the graph-first architecture where:
    1. Entry is already written to graph by commands_graph.py
    2. This function adds optional LLM summaries and embeddings
    3. Markdown is a projection, not a source

    Thread Safety:
        Uses advisory file locking to prevent race conditions when multiple
        processes enrich the same topic concurrently. The lock is held during
        the read-modify-write cycle.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        entry_id: Entry ID to enrich
        generate_summaries: Whether to generate LLM summary
        generate_embeddings: Whether to generate embedding vector

    Returns:
        EnrichmentResult with success status and details about what was generated
    """
    from watercooler.fs import lock_path_for_topic
    from watercooler.lock import AdvisoryLock

    try:
        # Read entry from graph (source of truth)
        entry_node = get_entry_node_from_graph(threads_dir, entry_id, topic)
        if not entry_node:
            logger.warning(f"Entry not found in graph for enrichment: {topic}/{entry_id}")
            return EnrichmentResult.error(f"Entry not found: {topic}/{entry_id}")

        # Extract fields we need for enrichment
        body = entry_node.get("body", "")
        title = entry_node.get("title", "")
        entry_type = entry_node.get("entry_type", "Note")
        existing_summary = entry_node.get("summary", "")

        summary_generated = False
        embedding_generated = False
        new_summary = existing_summary
        new_embedding = None

        # Generate summary if enabled and not already present
        if generate_summaries and not existing_summary:
            summarizer_config = create_summarizer_config()
            if is_llm_service_available(summarizer_config):
                new_summary = summarize_entry(
                    body,
                    entry_title=title,
                    entry_type=entry_type,
                    config=summarizer_config,
                )
                if new_summary:
                    logger.debug(f"Generated summary for entry {entry_id}")
                    summary_generated = True
            else:
                logger.debug(f"LLM service unavailable, skipping summary for {entry_id}")

        # Generate embedding if enabled
        embed_config = EmbeddingConfig.from_env()
        if generate_embeddings:
            if is_embedding_available(embed_config):
                # Use summary for embedding if available, otherwise truncated body
                max_chars = embed_config.max_text_chars
                embed_text = new_summary if new_summary else body[:max_chars]
                new_embedding = generate_embedding(embed_text)
                if new_embedding:
                    logger.debug(f"Generated embedding for entry {entry_id}")
                    embedding_generated = True
            else:
                logger.debug(f"Embedding service unavailable, skipping for {entry_id}")

        if not summary_generated and not embedding_generated:
            logger.debug(f"No enrichment generated for {entry_id}")
            return EnrichmentResult.noop()

        # Update entry node in graph with enrichment data
        # Use locking to prevent race conditions during read-modify-write
        lp = lock_path_for_topic(topic, threads_dir)
        with AdvisoryLock(lp, timeout=15, ttl=60, force_break=False):
            graph_dir = storage.ensure_graph_dir(threads_dir)
            entries = storage.load_thread_entries_dict(graph_dir, topic)
            entry_node_id = f"entry:{entry_id}"

            if entry_node_id in entries:
                if new_summary and new_summary != existing_summary:
                    entries[entry_node_id]["summary"] = new_summary
                if new_embedding:
                    entries[entry_node_id]["embedding"] = new_embedding

                # Load existing meta and edges (we only update entries)
                meta = storage.load_thread_meta(graph_dir, topic) or {}
                edges = storage.load_thread_edges(graph_dir, topic)

                # Write back atomically
                storage.write_thread_graph(graph_dir, topic, meta, entries, edges)

                # Update search index if embedding was generated
                if new_embedding:
                    storage.upsert_search_index_entry(graph_dir, entry_id, topic, new_embedding)

                logger.debug(f"Enrichment complete for {topic}/{entry_id}")

        return EnrichmentResult(
            success=True,
            summary_generated=summary_generated,
            embedding_generated=embedding_generated,
        )

    except Exception as e:
        logger.exception(f"Enrichment failed for {topic}/{entry_id}: {e}")
        return EnrichmentResult.error(str(e))


def sync_entry_to_graph(
    threads_dir: Path,
    topic: str,
    entry_id: Optional[str] = None,
    generate_summaries: bool = False,
    generate_embeddings: bool = False,
) -> bool:
    """DEPRECATED: Use enrich_graph_entry() instead.

    This function is from the MD-first era and reads from markdown files.
    In graph-first architecture, use enrich_graph_entry() which reads from
    the graph (source of truth) and only adds enrichment data.

    This function is preserved for backward compatibility with legacy repos
    that may not have graph data yet. It will be removed in a future version.

    Original docstring:
    Sync a single entry to the graph after an MCP write.

    This function:
    1. Parses the thread file (DEPRECATED - reads from MD not graph)
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
    warnings.warn(
        "sync_entry_to_graph() is deprecated. Use enrich_graph_entry() instead. "
        "This function reads from markdown; in graph-first architecture, "
        "enrich_graph_entry() reads from the graph (source of truth).",
        DeprecationWarning,
        stacklevel=2,
    )
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

        # Get graph directory and load existing per-thread data
        graph_dir = storage.ensure_graph_dir(threads_dir)

        # Load existing data (or empty dicts if new thread)
        meta = storage.load_thread_meta(graph_dir, topic) or {}
        entries = storage.load_thread_entries_dict(graph_dir, topic)
        edges = storage.load_thread_edges(graph_dir, topic)

        thread_id = f"thread:{topic}"
        entry_node_id = f"entry:{entry.entry_id}"

        # Build entry node with embedding if available
        entry_node = entry_to_node(entry, topic)
        if entry_embedding:
            entry_node["embedding"] = entry_embedding

        # Update entries dict
        entries[entry_node_id] = entry_node

        # Build/update thread meta
        thread_node = thread_to_node(parsed)
        meta.update(thread_node)

        # Add contains edge
        contains_edge_id = thread_id + entry_node_id
        edges[contains_edge_id] = {
            "source": thread_id,
            "target": entry_node_id,
            "type": "contains",
        }

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
                prev_entry_node_id = f"entry:{prev_entry.entry_id}"
                followed_by_edge_id = prev_entry_node_id + entry_node_id
                edges[followed_by_edge_id] = {
                    "source": prev_entry_node_id,
                    "target": entry_node_id,
                    "type": "followed_by",
                }

        # Write all per-thread files atomically
        storage.write_thread_graph(graph_dir, topic, meta, entries, edges)

        # Update search index if embedding was generated
        if entry_embedding:
            storage.upsert_search_index_entry(graph_dir, entry.entry_id, topic, entry_embedding)

        # Update manifest
        storage.update_manifest(graph_dir, topic, entry.entry_id)

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

        # Get graph directory
        graph_dir = storage.ensure_graph_dir(threads_dir)

        # Build thread meta
        meta = thread_to_node(parsed)

        # Build all entry nodes with optional embeddings
        entries: Dict[str, Dict[str, Any]] = {}
        search_index_updates: List[tuple[str, List[float]]] = []

        for entry in parsed.entries:
            entry_node = entry_to_node(entry, topic)
            entry_node_id = f"entry:{entry.entry_id}"

            # Generate embedding if enabled
            if generate_embeddings:
                embed_text = entry.summary if entry.summary else entry.body[:500]
                embedding = generate_embedding(embed_text)
                if embedding:
                    entry_node["embedding"] = embedding
                    search_index_updates.append((entry.entry_id, embedding))

            entries[entry_node_id] = entry_node

        if generate_embeddings:
            logger.debug(f"Generated embeddings for entries in {topic}")

        # Build all edges as dict keyed by source+target
        edges: Dict[str, Dict[str, Any]] = {}
        for edge in generate_edges(parsed):
            edge_id = edge.get("source", "") + edge.get("target", "")
            edges[edge_id] = edge

        # Write all per-thread files atomically
        storage.write_thread_graph(graph_dir, topic, meta, entries, edges)

        # Update search index for entries with embeddings
        for entry_id, embedding in search_index_updates:
            storage.upsert_search_index_entry(graph_dir, entry_id, topic, embedding)

        # Update manifest
        last_entry_id = parsed.entries[-1].entry_id if parsed.entries else None
        storage.update_manifest(graph_dir, topic, last_entry_id)

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


# ============================================================================
# Health Check & Reconciliation
# ============================================================================


@dataclass
class ParityMismatch:
    """Record of a mismatch between graph node and parsed markdown."""

    topic: str
    field: str  # "entry_count" or "last_updated"
    graph_value: Any  # Value in graph node
    actual_value: Any  # Value from parsing markdown
    difference: Optional[int] = None  # For entry_count: actual - graph


@dataclass
class GraphHealthReport:
    """Health report for graph sync status.

    Attributes:
        healthy: True if no errors, pending, or stale threads
        total_threads: Total number of thread markdown files
        synced_threads: Threads with 'ok' sync status
        error_threads: Threads with sync errors
        pending_threads: Threads with pending sync
        stale_threads: Threads not in sync state
        error_details: Error messages by topic
        parity_verified: Whether parity verification was performed
        parity_mismatches: List of entry_count/last_updated mismatches
    """

    healthy: bool = True
    total_threads: int = 0
    synced_threads: int = 0
    error_threads: int = 0
    pending_threads: int = 0
    stale_threads: List[str] = field(default_factory=list)
    error_details: Dict[str, str] = field(default_factory=dict)
    # Parity verification fields
    parity_verified: bool = False
    parity_mismatches: List[ParityMismatch] = field(default_factory=list)


def check_graph_health(
    threads_dir: Path,
    verify_parity: bool = False,
) -> GraphHealthReport:
    """Check graph sync health for all threads.

    Args:
        threads_dir: Path to the threads directory
        verify_parity: If True, parse each thread's markdown and compare
            entry_count and last_updated against graph node values.
            This is slower but catches data accuracy issues.

    Returns:
        GraphHealthReport with status of all threads and optional parity info
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

    # Parity verification (optional, slower)
    if verify_parity:
        report.parity_verified = True
        report.parity_mismatches = _verify_graph_parity(threads_dir, thread_files)
        # Parity mismatches affect health
        if report.parity_mismatches:
            report.healthy = False

    return report


def _verify_graph_parity(
    threads_dir: Path,
    thread_files: List[Path],
) -> List[ParityMismatch]:
    """Verify graph node data matches parsed markdown.

    Compares entry_count and last_updated for each thread node
    against values computed from parsing the markdown file.

    Args:
        threads_dir: Path to threads directory
        thread_files: List of thread markdown files

    Returns:
        List of ParityMismatch records for any discrepancies
    """
    mismatches: List[ParityMismatch] = []
    graph_dir = storage.get_graph_dir(threads_dir)

    if not storage.is_per_thread_format(graph_dir):
        logger.debug("No per-thread graph format found, skipping parity check")
        return mismatches

    # Compare each thread
    for thread_file in thread_files:
        topic = thread_file.stem
        graph_node = storage.load_thread_meta(graph_dir, topic)

        if not graph_node:
            # No graph node for this thread - not a parity issue, just missing
            continue

        try:
            # Parse the thread file
            parsed = parse_thread_file(thread_file)

            # Compare entry_count
            graph_count = graph_node.get("entry_count", 0)
            actual_count = parsed.entry_count

            if graph_count != actual_count:
                mismatches.append(ParityMismatch(
                    topic=topic,
                    field="entry_count",
                    graph_value=graph_count,
                    actual_value=actual_count,
                    difference=actual_count - graph_count,
                ))

            # Compare last_updated
            graph_updated = graph_node.get("last_updated", "")
            actual_updated = parsed.last_updated

            # Normalize timestamps for comparison (strip microseconds if needed)
            if graph_updated and actual_updated:
                # Only compare if both exist - timestamps might differ in precision
                # Consider them equal if the date/hour/minute/second match
                graph_prefix = graph_updated[:19] if len(graph_updated) >= 19 else graph_updated
                actual_prefix = actual_updated[:19] if len(actual_updated) >= 19 else actual_updated

                if graph_prefix != actual_prefix:
                    mismatches.append(ParityMismatch(
                        topic=topic,
                        field="last_updated",
                        graph_value=graph_updated,
                        actual_value=actual_updated,
                    ))

        except Exception as e:
            logger.warning(f"Error parsing {topic} for parity check: {e}")
            continue

    return mismatches


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


@dataclass
class BackfillResult:
    """Result of backfill operation."""

    threads_processed: int = 0
    threads_missing_summary: int = 0
    threads_summary_generated: int = 0
    entries_processed: int = 0
    entries_missing_summary: int = 0
    entries_summary_generated: int = 0
    entries_missing_embedding: int = 0
    entries_embedding_generated: int = 0
    errors: List[str] = field(default_factory=list)


def backfill_missing(
    threads_dir: Path,
    backfill_summaries: bool = True,
    backfill_embeddings: bool = True,
    batch_size: int = 10,
) -> BackfillResult:
    """Backfill missing summaries and embeddings in existing graph nodes.

    Unlike reconcile_graph which syncs stale threads from markdown, this function
    updates existing graph nodes that are missing summaries or embeddings.

    Args:
        threads_dir: Threads directory
        backfill_summaries: Generate missing summaries (thread + entry)
        backfill_embeddings: Generate missing entry embeddings
        batch_size: Number of items to process before writing (for progress)

    Returns:
        BackfillResult with counts of processed/generated items
    """
    result = BackfillResult()
    graph_dir = storage.get_graph_dir(threads_dir)

    if not storage.is_per_thread_format(graph_dir):
        result.errors.append(f"No per-thread graph format found at {graph_dir}")
        return result

    # Check service availability
    llm_available = False
    embedding_available = False

    if backfill_summaries:
        llm_available = is_llm_service_available()
        if not llm_available:
            logger.warning("LLM service not available, skipping summary backfill")

    if backfill_embeddings:
        embedding_available = is_embedding_available()
        if not embedding_available:
            logger.warning("Embedding service not available, skipping embedding backfill")

    if not llm_available and not embedding_available:
        result.errors.append("No services available for backfill")
        return result

    # Get summarizer config
    config = create_summarizer_config()

    # Process each thread
    for topic in storage.list_thread_topics(graph_dir):
        try:
            meta = storage.load_thread_meta(graph_dir, topic)
            entries = storage.load_thread_entries_dict(graph_dir, topic)
            edges = storage.load_thread_edges(graph_dir, topic)

            if not meta:
                continue

            result.threads_processed += 1
            thread_updated = False

            # Collect entry nodes for thread summary generation
            entry_list = list(entries.values())
            result.entries_processed += len(entry_list)

            # Backfill thread summary
            if backfill_summaries and llm_available:
                if not meta.get("summary"):
                    result.threads_missing_summary += 1
                    if entry_list:
                        # Format entries for summarization
                        entries_data = [
                            {
                                "body": e.get("body", ""),
                                "title": e.get("title", ""),
                                "type": e.get("entry_type", "Note"),
                            }
                            for e in sorted(entry_list, key=lambda x: x.get("index", 0))
                        ]
                        summary = summarize_thread(
                            entries_data,
                            thread_title=meta.get("title", topic),
                            config=config,
                        )
                        if summary:
                            meta["summary"] = summary
                            result.threads_summary_generated += 1
                            thread_updated = True
                            logger.debug(f"Generated summary for thread {topic}")

            # Backfill entry summaries and embeddings
            for entry_id, entry in entries.items():
                entry_raw_id = entry.get("entry_id", entry_id)

                # Summary backfill
                if backfill_summaries and llm_available:
                    if not entry.get("summary"):
                        result.entries_missing_summary += 1
                        try:
                            summary = summarize_entry(
                                entry_body=entry.get("body", ""),
                                entry_title=entry.get("title", ""),
                                entry_type=entry.get("entry_type", "Note"),
                                config=config,
                            )
                            if summary:
                                entry["summary"] = summary
                                result.entries_summary_generated += 1
                                thread_updated = True
                        except Exception as e:
                            result.errors.append(f"Entry {entry_raw_id} summary: {e}")

                # Embedding backfill
                if backfill_embeddings and embedding_available:
                    if not entry.get("embedding"):
                        result.entries_missing_embedding += 1
                        try:
                            text = entry.get("body", "")
                            if entry.get("title"):
                                text = f"{entry['title']}\n\n{text}"
                            embedding = generate_embedding(text)
                            if embedding:
                                entry["embedding"] = embedding
                                result.entries_embedding_generated += 1
                                thread_updated = True
                                # Also update search index
                                storage.upsert_search_index_entry(
                                    graph_dir, entry_raw_id, topic, embedding
                                )
                        except Exception as e:
                            result.errors.append(f"Entry {entry_raw_id} embedding: {e}")

            # Write updated thread data if changed
            if thread_updated:
                storage.write_thread_graph(graph_dir, topic, meta, entries, edges)
                logger.debug(f"Updated graph files for thread {topic}")

        except Exception as e:
            result.errors.append(f"Thread {topic}: {e}")
            continue

    logger.info(
        f"Backfill complete: {result.threads_summary_generated} thread summaries, "
        f"{result.entries_summary_generated} entry summaries, "
        f"{result.entries_embedding_generated} embeddings"
    )

    return result


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

    Auto-detection: If WATERCOOLER_GRAPHITI_ENABLED=1 is set but
    WATERCOOLER_MEMORY_BACKEND is not, defaults to "graphiti".

    Returns:
        Config dict with backend name, or None if disabled
    """
    # Check master disable switch first
    if is_memory_disabled():
        logger.debug("MEMORY: Disabled (WATERCOOLER_MEMORY_DISABLED=1)")
        return None

    backend = os.environ.get("WATERCOOLER_MEMORY_BACKEND", "").lower().strip()

    # Auto-detect: if WATERCOOLER_GRAPHITI_ENABLED=1 but no backend specified,
    # default to graphiti for automatic entry sync
    if not backend:
        graphiti_enabled = os.environ.get("WATERCOOLER_GRAPHITI_ENABLED", "").lower()
        if graphiti_enabled in ("1", "true", "yes"):
            backend = "graphiti"
            logger.debug("MEMORY: Auto-detected graphiti backend from WATERCOOLER_GRAPHITI_ENABLED=1")

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
    """Shutdown the sync executor on process exit.

    Uses wait=False for true fire-and-forget behavior. During process
    shutdown, waiting for background tasks causes issues because:
    1. The callback might be blocked on async operations (e.g., Graphiti LLM calls)
    2. Python's default executors are already shutting down
    3. Trying to schedule work in graphiti_core fails with
       "cannot schedule new futures after shutdown"

    With wait=False, we abandon incomplete background work gracefully
    rather than blocking and triggering cascading shutdown errors.
    """
    global _sync_executor
    if _sync_executor is not None:
        try:
            # Don't wait - let background tasks be abandoned on exit
            _sync_executor.shutdown(wait=False)
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


# ============================================================================
# Graph Format Migration (Monolithic -> Per-Thread)
# ============================================================================


@dataclass
class MigrationResult:
    """Result of graph format migration."""

    threads_migrated: int = 0
    entries_migrated: int = 0
    edges_migrated: int = 0
    search_index_entries: int = 0
    errors: List[str] = field(default_factory=list)
    monolithic_deleted: bool = False


def migrate_to_per_thread_format(
    threads_dir: Path,
    delete_monolithic: bool = True,
    build_search_index: bool = True,
) -> MigrationResult:
    """Migrate graph from monolithic to per-thread format.

    Converts:
    - graph/baseline/nodes.jsonl + edges.jsonl
    To:
    - graph/baseline/threads/<topic>/meta.json
    - graph/baseline/threads/<topic>/entries.jsonl
    - graph/baseline/threads/<topic>/edges.jsonl
    - graph/baseline/search-index.jsonl (if build_search_index=True)

    Args:
        threads_dir: Threads directory containing graph/baseline/
        delete_monolithic: If True, delete monolithic files after successful migration
        build_search_index: If True, build search-index.jsonl with embeddings

    Returns:
        MigrationResult with counts and any errors
    """
    result = MigrationResult()
    graph_dir = storage.get_graph_dir(threads_dir)
    nodes_file = graph_dir / "nodes.jsonl"
    edges_file = graph_dir / "edges.jsonl"

    # Check if monolithic files exist
    if not nodes_file.exists():
        result.errors.append(f"Monolithic nodes.jsonl not found at {nodes_file}")
        return result

    # Load all nodes
    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    thread_nodes: Dict[str, Dict[str, Any]] = {}  # topic -> thread node
    entry_nodes_by_topic: Dict[str, List[Dict[str, Any]]] = {}  # topic -> entries

    try:
        with open(nodes_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                node = json.loads(line)
                node_id = node.get("id", "")
                nodes_by_id[node_id] = node

                if node.get("type") == "thread":
                    topic = node.get("topic", "")
                    if topic:
                        thread_nodes[topic] = node
                elif node.get("type") == "entry":
                    topic = node.get("thread_topic", "")
                    if topic:
                        if topic not in entry_nodes_by_topic:
                            entry_nodes_by_topic[topic] = []
                        entry_nodes_by_topic[topic].append(node)
    except Exception as e:
        result.errors.append(f"Failed to load nodes.jsonl: {e}")
        return result

    # Load all edges
    edges_by_topic: Dict[str, List[Dict[str, Any]]] = {}  # topic -> edges

    if edges_file.exists():
        try:
            with open(edges_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    edge = json.loads(line)
                    # Determine topic from source or target
                    source = edge.get("source", "")
                    target = edge.get("target", "")

                    # Extract topic from thread:topic or entry node
                    topic = None
                    if source.startswith("thread:"):
                        topic = source.replace("thread:", "")
                    elif target.startswith("thread:"):
                        topic = target.replace("thread:", "")
                    elif source.startswith("entry:"):
                        # Look up entry node to get topic
                        entry_node = nodes_by_id.get(source)
                        if entry_node:
                            topic = entry_node.get("thread_topic")
                    elif target.startswith("entry:"):
                        entry_node = nodes_by_id.get(target)
                        if entry_node:
                            topic = entry_node.get("thread_topic")

                    if topic:
                        if topic not in edges_by_topic:
                            edges_by_topic[topic] = []
                        edges_by_topic[topic].append(edge)
                        result.edges_migrated += 1
        except Exception as e:
            result.errors.append(f"Failed to load edges.jsonl: {e}")
            return result

    # Collect all topics
    all_topics = set(thread_nodes.keys()) | set(entry_nodes_by_topic.keys())

    # Search index entries
    search_index_entries: List[Dict[str, Any]] = []

    # Migrate each topic
    for topic in all_topics:
        try:
            storage.ensure_thread_graph_dir(graph_dir, topic)

            # Prepare data for write
            thread_node = thread_nodes.get(topic, {})
            entries = entry_nodes_by_topic.get(topic, [])
            topic_edges = edges_by_topic.get(topic, [])

            # Convert entries list to dict keyed by node ID
            entries_dict: Dict[str, Dict[str, Any]] = {}
            for entry in entries:
                entry_id = entry.get("id", f"entry:{entry.get('entry_id', '')}")
                entries_dict[entry_id] = entry

                # Collect entries for search index
                if build_search_index:
                    embedding = entry.get("embedding")
                    if embedding:
                        search_index_entries.append({
                            "entry_id": entry.get("entry_id"),
                            "thread_topic": topic,
                            "embedding": embedding,
                        })

            # Convert edges list to dict keyed by source+target
            edges_dict: Dict[str, Dict[str, Any]] = {}
            for edge in topic_edges:
                edge_id = edge.get("source", "") + edge.get("target", "")
                edges_dict[edge_id] = edge

            # Write all per-thread files atomically
            if thread_node:
                storage.write_thread_graph(graph_dir, topic, thread_node, entries_dict, edges_dict)
                result.threads_migrated += 1
            elif entries_dict:
                # Entries without thread node - create minimal meta
                minimal_meta = {
                    "id": f"thread:{topic}",
                    "type": "thread",
                    "topic": topic,
                    "title": topic.replace("-", " ").title(),
                    "status": "OPEN",
                    "ball": "codex",
                    "entry_count": len(entries_dict),
                }
                storage.write_thread_graph(graph_dir, topic, minimal_meta, entries_dict, edges_dict)
                result.threads_migrated += 1

            result.entries_migrated += len(entries)

            logger.debug(f"Migrated thread {topic}: {len(entries)} entries, {len(topic_edges)} edges")

        except Exception as e:
            result.errors.append(f"Failed to migrate topic {topic}: {e}")
            continue

    # Build search index
    if build_search_index and search_index_entries:
        try:
            storage.atomic_write_jsonl(graph_dir / "search-index.jsonl", search_index_entries)
            result.search_index_entries = len(search_index_entries)
            logger.info(f"Built search index with {len(search_index_entries)} entries")
        except Exception as e:
            result.errors.append(f"Failed to write search index: {e}")

    # Delete monolithic files if requested and no errors
    if delete_monolithic and not result.errors:
        try:
            if nodes_file.exists():
                nodes_file.unlink()
            if edges_file.exists():
                edges_file.unlink()
            result.monolithic_deleted = True
            logger.info("Deleted monolithic nodes.jsonl and edges.jsonl")
        except Exception as e:
            result.errors.append(f"Failed to delete monolithic files: {e}")

    logger.info(
        f"Migration complete: {result.threads_migrated} threads, "
        f"{result.entries_migrated} entries, {result.edges_migrated} edges"
    )

    return result


def is_per_thread_format(threads_dir: Path) -> bool:
    """Check if graph already uses per-thread format.

    Args:
        threads_dir: Threads directory

    Returns:
        True if per-thread format is in use
    """
    graph_dir = storage.get_graph_dir(threads_dir)
    return storage.is_per_thread_format(graph_dir)


def needs_migration(threads_dir: Path) -> bool:
    """Check if graph needs migration from monolithic to per-thread.

    Args:
        threads_dir: Threads directory

    Returns:
        True if monolithic format exists and per-thread does not
    """
    graph_dir = storage.get_graph_dir(threads_dir)
    nodes_file = graph_dir / "nodes.jsonl"

    # Has monolithic format and not yet migrated
    has_monolithic = nodes_file.exists()
    has_per_thread = is_per_thread_format(threads_dir)

    return has_monolithic and not has_per_thread
