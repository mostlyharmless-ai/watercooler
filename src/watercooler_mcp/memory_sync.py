"""Memory backend sync implementations.

This module contains the sync callbacks for memory backends (Graphiti, LeanRAG).
Callbacks are registered at MCP startup via init_memory_sync_callbacks().

Architecture:
    - Callbacks follow the signature defined in baseline_graph.sync.register_memory_sync_callback
    - Each callback handles syncing a single entry to its respective backend
    - Callbacks run in a ThreadPoolExecutor (fire-and-forget)
    - Errors are logged but don't block the main sync flow

Issue #83: This module extracts Graphiti-specific code from baseline_graph/sync.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone as tz
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Lock for thread-safe queue file writes
_queue_lock = threading.Lock()


# ============================================================================
# Graphiti Sync Callback
# ============================================================================


async def _call_graphiti_add_episode(
    content: str,
    topic: str,
    entry_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    title: Optional[str] = None,
    code_path: str = "",
) -> Dict[str, Any]:
    """Call graphiti_add_episode to sync entry to Graphiti.

    This is the internal async implementation that interfaces with
    the Graphiti backend. Uses unified project group_id (config.database)
    instead of per-thread group_ids, allowing entities to be shared across
    threads within the same project.

    Args:
        content: Entry body text
        topic: Thread topic (included in source_description for traceability)
        entry_id: Entry ID for provenance tracking
        timestamp: Entry timestamp (ISO 8601)
        title: Entry title
        code_path: Path to code repository (for database name derivation)

    Returns:
        Result dict with success status and episode_uuid
    """
    try:
        from watercooler_mcp import memory as mem

        config = mem.load_graphiti_config(code_path=code_path)
        if config is None:
            return {"success": False, "error": "Graphiti not enabled"}

        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            error_msg = "Graphiti backend unavailable"
            if isinstance(backend, dict):
                error_msg = backend.get("message", error_msg)
            return {"success": False, "error": error_msg}

        # Use unified project group_id (derived from code_path via config)
        unified_group_id = config.database

        # Parse timestamp
        if timestamp:
            try:
                ref_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                ref_time = datetime.now(tz.utc)
        else:
            ref_time = datetime.now(tz.utc)

        # Create episode title
        episode_title = title if title else content[:50] + ("..." if len(content) > 50 else "")

        # Include thread topic in source_description for traceability
        source_desc = f"thread:{topic} | Sync from baseline graph"

        # Add episode directly to Graphiti
        result = await backend.add_episode_direct(
            name=episode_title,
            episode_body=content,
            source_description=source_desc,
            reference_time=ref_time,
            group_id=unified_group_id,
        )

        episode_uuid = result.get("episode_uuid", "unknown")

        # Track entry-episode mapping if entry_id provided
        if entry_id and episode_uuid != "unknown":
            backend.index_entry_as_episode(entry_id, episode_uuid, unified_group_id)

        logger.debug(f"MEMORY: Synced entry {entry_id} as episode {episode_uuid}")

        return {
            "success": True,
            "episode_uuid": episode_uuid,
            "entities_extracted": result.get("entities_extracted", []),
        }

    except ImportError as e:
        return {"success": False, "error": f"Memory module unavailable: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _call_graphiti_add_episode_chunked(
    content: str,
    topic: str,
    entry_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    title: Optional[str] = None,
    code_path: str = "",
    max_tokens: int = 768,
    overlap: int = 64,
) -> Dict[str, Any]:
    """Call graphiti_add_episode with chunking for large entries.

    Splits the entry body into chunks and creates separate episodes for each,
    linking them via previous_episode_uuids for temporal ordering.

    Args:
        content: Entry body text
        topic: Thread topic (included in source_description for traceability)
        entry_id: Entry ID for provenance tracking
        timestamp: Entry timestamp (ISO 8601)
        title: Entry title
        code_path: Path to code repository (for database name derivation)
        max_tokens: Maximum tokens per chunk
        overlap: Token overlap between chunks

    Returns:
        Result dict with success status, episode_uuids list, and chunk_count
    """
    try:
        from watercooler_memory.chunker import ChunkerConfig, chunk_text
        from watercooler_mcp import memory as mem

        config = mem.load_graphiti_config(code_path=code_path)
        if config is None:
            return {"success": False, "error": "Graphiti not enabled"}

        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            error_msg = "Graphiti backend unavailable"
            if isinstance(backend, dict):
                error_msg = backend.get("message", error_msg)
            return {"success": False, "error": error_msg}

        # Configure chunking
        chunker_config = ChunkerConfig(
            max_tokens=max_tokens,
            overlap=overlap,
        )

        # Chunk the content
        chunks = chunk_text(content, chunker_config)

        # If single chunk or no chunking needed, fall back to simple sync
        if len(chunks) <= 1:
            return await _call_graphiti_add_episode(
                content=content,
                topic=topic,
                entry_id=entry_id,
                timestamp=timestamp,
                title=title,
                code_path=code_path,
            )

        # Use unified project group_id
        unified_group_id = config.database

        # Parse timestamp
        if timestamp:
            try:
                ref_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                ref_time = datetime.now(tz.utc)
        else:
            ref_time = datetime.now(tz.utc)

        total_chunks = len(chunks)
        episode_uuids: list[str] = []
        entities_extracted: list[str] = []
        previous_episode_uuids: list[str] = []
        failed_chunks: list[int] = []

        for i, (chunk_text_content, token_count) in enumerate(chunks):
            chunk_num = i + 1

            # Create chunk-specific title
            chunk_title = f"{title} [chunk {chunk_num}/{total_chunks}]" if title else f"Entry chunk {chunk_num}/{total_chunks}"

            # Include chunk info in source_description
            source_desc = f"thread:{topic} | entry:{entry_id} | chunk:{chunk_num}/{total_chunks}"

            try:
                # Add episode with link to previous chunks
                result = await backend.add_episode_direct(
                    name=chunk_title,
                    episode_body=chunk_text_content,
                    source_description=source_desc,
                    reference_time=ref_time,
                    group_id=unified_group_id,
                    previous_episode_uuids=previous_episode_uuids.copy() if previous_episode_uuids else None,
                )

                episode_uuid = result.get("episode_uuid", "unknown")
                if episode_uuid != "unknown":
                    episode_uuids.append(episode_uuid)
                    # Link next chunk to this one
                    previous_episode_uuids = [episode_uuid]

                    # Track chunk mapping if entry_id provided and index available
                    if entry_id and backend.entry_episode_index is not None:
                        # Generate a simple chunk_id based on entry_id and index
                        import hashlib
                        chunk_id = hashlib.sha256(
                            f"{entry_id}:{i}:{chunk_text_content[:100]}".encode()
                        ).hexdigest()[:16]

                        backend.entry_episode_index.add_chunk_mapping(
                            chunk_id=chunk_id,
                            episode_uuid=episode_uuid,
                            entry_id=entry_id,
                            thread_id=topic,
                            chunk_index=i,
                            total_chunks=total_chunks,
                        )

                entities = result.get("entities_extracted", [])
                if entities:
                    entities_extracted.extend(entities)

            except Exception as e:
                logger.warning(
                    f"MEMORY: Failed to sync chunk {chunk_num}/{total_chunks} "
                    f"for {topic}/{entry_id}: {e}"
                )
                failed_chunks.append(chunk_num)
                continue

        # Save index after all chunks (if any were successful)
        if episode_uuids and entry_id and backend.entry_episode_index is not None:
            try:
                backend.entry_episode_index.save()
            except Exception as e:
                logger.warning(f"MEMORY: Failed to save entry_episode_index: {e}")

        # Consider success if at least one chunk was indexed
        if episode_uuids:
            logger.debug(
                f"MEMORY: Synced entry {entry_id} as {len(episode_uuids)} "
                f"linked episodes (chunks)"
            )
            return {
                "success": True,
                "episode_uuids": episode_uuids,
                "chunk_count": len(episode_uuids),
                "total_chunks": total_chunks,
                "failed_chunks": failed_chunks,
                "entities_extracted": entities_extracted,
            }
        else:
            return {
                "success": False,
                "error": f"All {total_chunks} chunks failed to sync",
                "failed_chunks": failed_chunks,
            }

    except ImportError as e:
        return {"success": False, "error": f"Chunking module unavailable: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _graphiti_sync_callback(
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
    entry_summary: str = "",
) -> bool:
    """Sync entry to Graphiti backend.

    This callback is registered with baseline_graph.sync and invoked
    for each entry when WATERCOOLER_MEMORY_BACKEND=graphiti.

    Uses unified project group_id (derived from code_path) instead of
    per-thread group_ids, allowing entities to be shared across threads.

    Args:
        threads_dir: Threads directory (used to derive code_path)
        topic: Thread topic (included in source_description for traceability)
        entry_id: Entry ID for provenance tracking
        entry_body: Entry content to sync
        entry_title: Optional entry title
        timestamp: Entry timestamp (ISO 8601)
        agent: Agent name (unused by Graphiti)
        role: Agent role (unused by Graphiti)
        entry_type: Entry type (unused by Graphiti)
        backend_config: Backend configuration dict
        log: Logger instance
        dry_run: If True, simulate without actual sync
        entry_summary: Enriched summary from graph enrichment. Used as
            episode content instead of entry_body when use_summary is
            configured and summary is non-empty.

    Returns:
        True on success, False on failure
    """
    if dry_run:
        log.debug(f"MEMORY: [DRY RUN] Would sync {topic}/{entry_id} to Graphiti")
        return True

    try:
        # Import config helpers
        from watercooler.memory_config import (
            get_graphiti_chunk_config,
            get_graphiti_chunk_on_sync,
            get_graphiti_use_summary,
        )

        # Resolve content: use enriched summary if configured and available
        content = entry_body
        if get_graphiti_use_summary() and entry_summary:
            content = entry_summary
            log.debug(
                f"MEMORY: Using enriched summary for {topic}/{entry_id} "
                f"({len(entry_summary)} chars vs {len(entry_body)} raw)"
            )

        # Derive code_path from threads_dir
        # threads_dir: /path/to/project-threads -> code_path: /path/to/project
        threads_dir_str = str(threads_dir)
        if threads_dir_str.endswith("-threads"):
            code_path = threads_dir_str.removesuffix("-threads")
        else:
            # Warn about non-standard naming, use threads_dir as fallback
            log.warning(
                f"MEMORY: threads_dir '{threads_dir}' doesn't end with '-threads'. "
                f"Using it directly for code_path derivation."
            )
            code_path = threads_dir_str

        # Check if chunking is enabled
        chunk_on_sync = get_graphiti_chunk_on_sync()

        # Callbacks run in ThreadPoolExecutor workers which have no event loop,
        # so asyncio.run() is always safe here.
        if chunk_on_sync:
            max_tokens, overlap = get_graphiti_chunk_config()
            result = asyncio.run(
                _call_graphiti_add_episode_chunked(
                    content=content,
                    topic=topic,
                    entry_id=entry_id,
                    timestamp=timestamp,
                    title=entry_title,
                    code_path=code_path,
                    max_tokens=max_tokens,
                    overlap=overlap,
                )
            )
        else:
            result = asyncio.run(
                _call_graphiti_add_episode(
                    content=content,
                    topic=topic,
                    entry_id=entry_id,
                    timestamp=timestamp,
                    title=entry_title,
                    code_path=code_path,
                )
            )

        if not result.get("success", False):
            log.warning(
                f"MEMORY: Graphiti sync failed for {topic}/{entry_id}: "
                f"{result.get('error', 'unknown')}"
            )
            return False

        # Log chunk count if chunked
        chunk_count = result.get("chunk_count")
        if chunk_count and chunk_count > 1:
            log.debug(
                f"MEMORY: Synced {topic}/{entry_id} to Graphiti "
                f"({chunk_count} chunks)"
            )
        else:
            log.debug(f"MEMORY: Synced {topic}/{entry_id} to Graphiti")
        return True

    except Exception as e:
        log.exception(f"MEMORY: Graphiti sync error for {topic}/{entry_id}")
        return False


# ============================================================================
# LeanRAG Sync Callback
# ============================================================================


def _leanrag_sync_callback(
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
    entry_summary: str = "",
) -> bool:
    """Sync entry to LeanRAG backend.

    LeanRAG is a batch processing pipeline - individual entry syncs queue
    entries for later batch processing. The actual clustering happens via
    explicit pipeline runs (watercooler_leanrag_run_pipeline MCP tool).

    Entries are appended to a queue file (.leanrag_queue.jsonl) in the
    threads directory. Pipeline runs can check this file to know if there's
    fresh work to process.

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
        entry_summary: Enriched summary (unused by LeanRAG, protocol compliance)

    Returns:
        True on success, False on failure
    """
    if dry_run:
        log.debug(f"MEMORY: [DRY RUN] Would queue {topic}/{entry_id} for LeanRAG pipeline")
        return True

    try:
        # Build queue entry with all metadata
        queue_entry = {
            "entry_id": entry_id,
            "topic": topic,
            "timestamp": timestamp or datetime.now(tz.utc).isoformat(),
            "queued_at": datetime.now(tz.utc).isoformat(),
            "entry_title": entry_title,
            "entry_body": entry_body,
            "agent": agent,
            "role": role,
            "entry_type": entry_type,
        }

        # Append to queue file (thread-safe)
        queue_file = Path(threads_dir) / ".leanrag_queue.jsonl"
        with _queue_lock:
            with open(queue_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(queue_entry) + "\n")

        log.debug(f"MEMORY: Entry {topic}/{entry_id} queued for LeanRAG pipeline")
        return True

    except Exception as e:
        log.exception(f"MEMORY: Failed to queue {topic}/{entry_id} for LeanRAG: {e}")
        return False


def get_leanrag_queue_path(threads_dir: Path) -> Path:
    """Get the path to the LeanRAG queue file.

    Args:
        threads_dir: Threads directory

    Returns:
        Path to .leanrag_queue.jsonl
    """
    return Path(threads_dir) / ".leanrag_queue.jsonl"


def read_leanrag_queue(threads_dir: Path) -> list[Dict[str, Any]]:
    """Read all entries from the LeanRAG queue.

    Args:
        threads_dir: Threads directory

    Returns:
        List of queued entry dicts
    """
    queue_file = get_leanrag_queue_path(threads_dir)
    if not queue_file.exists():
        return []

    entries = []
    with open(queue_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(f"MEMORY: Skipping malformed queue entry: {line[:50]}...")
    return entries


def clear_leanrag_queue(threads_dir: Path) -> int:
    """Clear the LeanRAG queue after processing.

    Atomically reads and clears the queue while holding the lock to prevent
    race conditions with concurrent writers.

    Args:
        threads_dir: Threads directory

    Returns:
        Number of entries cleared
    """
    queue_file = get_leanrag_queue_path(threads_dir)

    with _queue_lock:
        if not queue_file.exists():
            return 0

        # Count entries while holding lock
        count = 0
        try:
            with open(queue_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
        except (OSError, IOError):
            return 0

        # Delete while still holding lock
        try:
            queue_file.unlink()
        except FileNotFoundError:
            return 0

    logger.debug(f"MEMORY: Cleared {count} entries from LeanRAG queue")
    return count


# ============================================================================
# Callback Registration
# ============================================================================


_callbacks_initialized = False


def init_memory_sync_callbacks() -> None:
    """Register memory sync callbacks at MCP startup.

    This function is idempotent - safe to call multiple times.
    It registers callbacks for all supported memory backends.

    Should be called during MCP server initialization.
    """
    global _callbacks_initialized

    if _callbacks_initialized:
        logger.debug("MEMORY: Callbacks already initialized, skipping")
        return

    try:
        from watercooler.baseline_graph.sync import register_memory_sync_callback

        # Register Graphiti callback
        register_memory_sync_callback("graphiti", _graphiti_sync_callback)

        # Register LeanRAG callback
        register_memory_sync_callback("leanrag", _leanrag_sync_callback)

        _callbacks_initialized = True
        logger.info("MEMORY: Sync callbacks registered for backends: graphiti, leanrag")

    except ImportError as e:
        logger.warning(f"MEMORY: Could not register sync callbacks: {e}")
    except Exception as e:
        logger.exception(f"MEMORY: Error registering sync callbacks: {e}")


def reset_callbacks() -> None:
    """Reset callback registration state (for testing).

    This allows re-registration of callbacks in test scenarios.
    """
    global _callbacks_initialized
    _callbacks_initialized = False
