"""Memory task queue — persistent, recoverable background processing.

Public API
----------
- ``init_memory_queue()`` — Initialise the singleton queue + worker.
  Called once at MCP server startup.
- ``get_queue()`` / ``get_worker()`` — Access the global instances.
- ``enqueue_memory_task(...)`` — Convenience helper to create and enqueue
  a single-entry task.

Design follows AsyncSyncCoordinator: JSONL persistence, daemon worker
thread, exponential-backoff retries, and dead-letter parking.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checkpoint import BulkCheckpoint, load_checkpoint, save_checkpoint
from .errors import (
    BackendUnavailableError,
    CheckpointError,
    DuplicateTaskError,
    MemoryQueueError,
    TaskNotFoundError,
)
from .queue import DEFAULT_QUEUE_DIR, MemoryTaskQueue
from .task import MemoryTask, TaskStatus, TaskType
from .worker import MemoryTaskWorker

logger = logging.getLogger(__name__)

__all__ = [
    # Core types
    "MemoryTask",
    "TaskStatus",
    "TaskType",
    "MemoryTaskQueue",
    "MemoryTaskWorker",
    # Checkpoint
    "BulkCheckpoint",
    "load_checkpoint",
    "save_checkpoint",
    # Errors
    "MemoryQueueError",
    "BackendUnavailableError",
    "CheckpointError",
    "DuplicateTaskError",
    "TaskNotFoundError",
    # Singleton API
    "init_memory_queue",
    "get_queue",
    "get_worker",
    "enqueue_memory_task",
]

# ------------------------------------------------------------------ #
# Module-level singletons
# ------------------------------------------------------------------ #

_queue: Optional[MemoryTaskQueue] = None
_worker: Optional[MemoryTaskWorker] = None


def get_queue() -> Optional[MemoryTaskQueue]:
    """Return the global queue instance (None if not initialised)."""
    return _queue


def get_worker() -> Optional[MemoryTaskWorker]:
    """Return the global worker instance (None if not initialised)."""
    return _worker


def init_memory_queue(
    *,
    queue_dir: Optional[Path] = None,
    poll_interval: float = 5.0,
    stale_timeout: float = 600.0,
    start_worker: bool = True,
) -> MemoryTaskQueue:
    """Initialise the singleton memory queue and (optionally) start the worker.

    Idempotent — calling multiple times returns the existing instance.

    Args:
        queue_dir: Override persistence directory.
        poll_interval: Worker poll interval in seconds.
        stale_timeout: Seconds before a RUNNING task is considered stale.
        start_worker: Whether to start the background worker thread.

    Returns:
        The global MemoryTaskQueue instance.
    """
    global _queue, _worker

    if _queue is not None:
        logger.debug("MEMORY_QUEUE: already initialised, skipping")
        return _queue

    _queue = MemoryTaskQueue(queue_dir=queue_dir)
    _worker = MemoryTaskWorker(
        _queue,
        poll_interval=poll_interval,
        stale_timeout=stale_timeout,
    )

    if start_worker:
        _worker.start()

    logger.info(
        "MEMORY_QUEUE: initialised (queue_dir=%s, depth=%d)",
        _queue._dir, _queue.depth(),
    )
    return _queue


def enqueue_memory_task(
    *,
    entry_id: str,
    topic: str,
    group_id: str,
    content: str,
    backend: str = "graphiti",
    title: str = "",
    timestamp: str = "",
    source_description: str = "",
    max_attempts: int = 3,
) -> Optional[str]:
    """Convenience helper: create and enqueue a single-entry memory task.

    Returns:
        The task_id, or ``None`` if the queue is not initialised or the
        task is a duplicate.
    """
    if _queue is None:
        logger.debug("MEMORY_QUEUE: not initialised, skipping enqueue")
        return None

    task = MemoryTask(
        backend=backend,
        entry_id=entry_id,
        topic=topic,
        group_id=group_id,
        content=content,
        title=title,
        timestamp=timestamp,
        source_description=source_description,
        max_attempts=max_attempts,
    )

    try:
        task_id = _queue.enqueue(task)
    except DuplicateTaskError as e:
        logger.debug("MEMORY_QUEUE: skipped duplicate for %s: %s", entry_id, e)
        return e.existing_task_id

    # Wake worker to process immediately
    if _worker is not None:
        _worker.wake()

    return task_id
