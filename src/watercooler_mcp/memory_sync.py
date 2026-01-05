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
    group_id: str,
    entry_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Call graphiti_add_episode to sync entry to Graphiti.

    This is the internal async implementation that interfaces with
    the Graphiti backend.

    Args:
        content: Entry body text
        group_id: Thread topic identifier
        entry_id: Entry ID for provenance tracking
        timestamp: Entry timestamp (ISO 8601)
        title: Entry title

    Returns:
        Result dict with success status and episode_uuid
    """
    try:
        from watercooler_mcp import memory as mem

        config = mem.load_graphiti_config()
        if config is None:
            return {"success": False, "error": "Graphiti not enabled"}

        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            error_msg = "Graphiti backend unavailable"
            if isinstance(backend, dict):
                error_msg = backend.get("message", error_msg)
            return {"success": False, "error": error_msg}

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

        # Add episode directly to Graphiti
        result = await backend.add_episode_direct(
            name=episode_title,
            episode_body=content,
            source_description="Sync from baseline graph",
            reference_time=ref_time,
            group_id=group_id,
        )

        episode_uuid = result.get("episode_uuid", "unknown")

        # Track entry-episode mapping if entry_id provided
        if entry_id and episode_uuid != "unknown":
            backend.index_entry_as_episode(entry_id, episode_uuid, group_id)

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
) -> bool:
    """Sync entry to Graphiti backend.

    This callback is registered with baseline_graph.sync and invoked
    for each entry when WATERCOOLER_MEMORY_BACKEND=graphiti.

    Args:
        threads_dir: Threads directory
        topic: Thread topic (used as group_id)
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

    Returns:
        True on success, False on failure
    """
    if dry_run:
        log.debug(f"MEMORY: [DRY RUN] Would sync {topic}/{entry_id} to Graphiti")
        return True

    try:
        # Callbacks run in ThreadPoolExecutor workers which have no event loop,
        # so asyncio.run() is always safe here.
        result = asyncio.run(
            _call_graphiti_add_episode(
                content=entry_body,
                group_id=topic,
                entry_id=entry_id,
                timestamp=timestamp,
                title=entry_title,
            )
        )

        if not result.get("success", False):
            log.warning(
                f"MEMORY: Graphiti sync failed for {topic}/{entry_id}: "
                f"{result.get('error', 'unknown')}"
            )
            return False

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
