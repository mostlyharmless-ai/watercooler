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

import atexit
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checkpoint import BulkCheckpoint, load_checkpoint, save_checkpoint
from .errors import (
    BackendUnavailableError,
    CheckpointError,
    DuplicateTaskError,
    MemoryQueueError,
    PermanentTaskError,
    QueueFullError,
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
    "PermanentTaskError",
    "QueueFullError",
    "TaskNotFoundError",
    # Constants
    "VALID_BACKENDS",
    # Helpers
    "truncate_utf8_to_bytes",
    # Singleton API
    "init_memory_queue",
    "get_queue",
    "get_worker",
    "enqueue_memory_task",
]


def truncate_utf8_to_bytes(s: str, *, max_bytes: int) -> str:
    """Truncate ``s`` to at most ``max_bytes`` UTF-8 bytes, codepoint-safe.

    PR #745 round 2 review (MED): the previous one-liner
    ``s.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')``
    has a data-loss failure mode for bodies composed entirely of
    multi-byte codepoints (CJK, emoji, etc.): if every slice boundary
    lands mid-codepoint, ``errors='ignore'`` discards every byte and
    the function returns ``""``. Downstream callers that classify an
    empty body as a skip then silently drop a non-empty entry.

    This helper drops trailing codepoints (not bytes) until the
    encoded form fits the cap. Always returns at least one codepoint
    when the input is non-empty.

    The same fix landed in PR #745 round 3 in two places:
      - ``scripts/backfill_hosted_t2.py``
      - ``src/watercooler_mcp/daemons/t2_indexer.py`` (4 call sites)
    Both now import this helper to prevent drift.

    PR #745 round 4 review (MED): the helper now enforces the byte cap
    *strictly*. The previous shape always returned at least one
    codepoint (to avoid the original data-loss bug where a multi-byte
    body could degrade to ``""``). For typical caps (64 KB / 500 B)
    that's always within budget — but for a future caller with a
    strict cap below max-codepoint-size (≤ 4 bytes), returning a
    single 4-byte codepoint would silently exceed the cap. Now: if
    even one codepoint can't fit, return ``""``. Callers that need a
    "preserve at least one codepoint" semantics should set a cap of
    ≥ 4 bytes and check for empty output.

    Args:
        s: The string to truncate. Empty strings pass through.
        max_bytes: The byte cap. Must be >= 1.

    Returns:
        ``s`` unchanged if it already fits; otherwise the longest
        prefix of ``s`` whose UTF-8 encoding is at most ``max_bytes``
        bytes. Returns ``""`` when not even one codepoint fits.
    """
    if not s:
        return ""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    # O(log n) outer pass: drop large chunks while we're far over.
    truncated = s
    while len(truncated.encode("utf-8")) > max_bytes and len(truncated) > 1:
        over = len(truncated.encode("utf-8")) - max_bytes
        # Conservative codepoint estimate (4 bytes/char max in UTF-8).
        drop = max(1, over // 4)
        truncated = truncated[:-drop] if drop < len(truncated) else truncated[:1]
    # Final tightening — last 1-3 chars may still push over. Step by 1.
    while len(truncated.encode("utf-8")) > max_bytes and len(truncated) > 1:
        truncated = truncated[:-1]
    # Strict-cap enforcement: if the remaining single codepoint is
    # still over budget, return empty rather than violate the cap.
    if len(truncated.encode("utf-8")) > max_bytes:
        return ""
    return truncated


# ------------------------------------------------------------------ #
# Module-level singletons
# ------------------------------------------------------------------ #

VALID_BACKENDS = frozenset({"graphiti", "leanrag"})

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
    max_depth: int = 5000,
    start_worker: bool = True,
    max_workers: int = 1,
    task_timeout: float = 300.0,
) -> MemoryTaskQueue:
    """Initialise the singleton memory queue and (optionally) start the worker.

    Idempotent — calling multiple times returns the existing instance.

    Args:
        queue_dir: Override persistence directory.
        poll_interval: Worker poll interval in seconds.
        stale_timeout: Seconds before a RUNNING task is considered stale.
        max_depth: Maximum number of tasks before backpressure kicks in.
        start_worker: Whether to start the background worker thread.
        max_workers: Number of concurrent worker threads (default 1 = serial).
        task_timeout: Base timeout (seconds) for a single task. Effective
            timeout escalates on retry: base * 2^(attempt-1), capped at
            stale_timeout. Tasks that exceed the effective timeout are failed
            and evict the thread-local backend.

    Returns:
        The global MemoryTaskQueue instance.
    """
    global _queue, _worker

    if _queue is not None:
        logger.debug("MEMORY_QUEUE: already initialised, skipping")
        return _queue

    _queue = MemoryTaskQueue(queue_dir=queue_dir, max_depth=max_depth)
    _worker = MemoryTaskWorker(
        _queue,
        poll_interval=poll_interval,
        stale_timeout=stale_timeout,
        max_workers=max_workers,
        task_timeout=task_timeout,
    )

    if start_worker:
        _worker.start()

    atexit.register(_shutdown_worker)

    logger.info(
        "MEMORY_QUEUE: initialised (queue_dir=%s, depth=%d)",
        _queue._dir, _queue.depth(),
    )
    return _queue


def _shutdown_worker() -> None:
    """Atexit hook: gracefully stop the worker thread and release resources."""
    if _worker is not None and _worker.is_running:
        logger.info("MEMORY_QUEUE: shutting down worker (atexit)")
        _worker.stop(timeout=5.0)
    if _queue is not None:
        _queue.close()


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
    code_path: str = "",
    max_attempts: int = 3,
    xrefs: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    vote_score: int = 0,
    pinned: bool = False,
) -> Optional[str]:
    """Convenience helper: create and enqueue a single-entry memory task.

    Args:
        code_path: Stored for provenance. The call site in sync.py passes the
            threads worktree path; this is NOT the code repo root. Callers
            should derive ``group_id`` via ``derive_group_id(...)`` before
            enqueuing.

    Returns:
        The task_id, or ``None`` if the queue is not initialised or the
        task is a duplicate.
    """
    if _queue is None:
        logger.debug("MEMORY_QUEUE: not initialised, skipping enqueue")
        return None

    if backend not in VALID_BACKENDS:
        logger.warning(
            "MEMORY_QUEUE: invalid backend %r (valid: %s), skipping enqueue",
            backend, ", ".join(sorted(VALID_BACKENDS)),
        )
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
        code_path=code_path,
        max_attempts=max_attempts,
        xrefs=xrefs or [],
        tags=tags or [],
        vote_score=vote_score,
        pinned=pinned,
    )

    try:
        task_id = _queue.enqueue(task)
    except DuplicateTaskError as e:
        logger.debug("MEMORY_QUEUE: skipped duplicate for %s: %s", entry_id, e)
        return None
    except QueueFullError as e:
        logger.warning("MEMORY_QUEUE: queue full, skipping enqueue: %s", e)
        return None

    # Wake worker to process immediately
    if _worker is not None:
        _worker.wake()

    return task_id
