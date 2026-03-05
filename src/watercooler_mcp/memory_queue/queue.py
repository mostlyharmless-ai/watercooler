"""Persistent JSONL-backed task queue for memory operations.

The queue stores all non-terminal tasks in a JSONL file and provides
thread-safe enqueue / dequeue / state-update operations.  Design follows
AsyncSyncCoordinator's JSONL persistence pattern.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import DuplicateTaskError, QueueFullError, TaskNotFoundError
from .task import MemoryTask, TaskStatus

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_DIR = Path.home() / ".watercooler" / "memory_queue"


class MemoryTaskQueue:
    """Thread-safe, JSONL-persisted task queue.

    Active tasks live in ``queue.jsonl``; dead-letter tasks are appended
    to ``dead_letter.jsonl``; cumulative statistics survive restarts in
    ``stats.json``.

    Args:
        queue_dir: Directory for persistence files (created if missing).
        max_depth: Maximum number of active tasks before rejecting enqueue.
    """

    def __init__(self, queue_dir: Path | None = None, *, max_depth: int = 5000) -> None:
        self._dir = Path(queue_dir) if queue_dir else DEFAULT_QUEUE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

        self._queue_file = self._dir / "queue.jsonl"
        self._dead_letter_file = self._dir / "dead_letter.jsonl"
        self._stats_file = self._dir / "stats.json"

        self._max_depth = max_depth
        self._lock = threading.Lock()
        self._tasks: Dict[str, MemoryTask] = {}

        # Cumulative statistics (survives restarts via stats.json)
        self._stats: Dict[str, int] = {
            "total_enqueued": 0,
            "total_completed": 0,
            "total_dead_lettered": 0,
            "total_retries": 0,
        }

        self._load()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def enqueue(self, task: MemoryTask, *, allow_duplicate: bool = False) -> str:
        """Add a task to the queue.

        Args:
            task: The task to enqueue.
            allow_duplicate: Skip dedup check if True.

        Returns:
            The task_id.

        Raises:
            DuplicateTaskError: If an equivalent non-terminal task exists.
        """
        with self._lock:
            if len(self._tasks) >= self._max_depth:
                raise QueueFullError(
                    message=f"Queue depth {len(self._tasks)} >= max {self._max_depth}",
                    context={"depth": len(self._tasks), "max_depth": self._max_depth},
                )

            if not allow_duplicate and task.entry_id:
                dup = self._find_duplicate(task)
                if dup is not None:
                    raise DuplicateTaskError(
                        message=f"Duplicate task for entry {task.entry_id}",
                        existing_task_id=dup.task_id,
                    )

            self._tasks[task.task_id] = task
            self._stats["total_enqueued"] += 1
            self._persist()

        logger.debug("MEMORY_QUEUE: enqueued %s (%s)", task.task_id, task.entry_id)
        return task.task_id

    def dequeue(self) -> Optional[MemoryTask]:
        """Pop the next ready task (oldest first) and mark it RUNNING.

        Returns:
            The task, or ``None`` if none are ready.
        """
        with self._lock:
            ready = sorted(
                (t for t in self._tasks.values() if t.is_ready),
                key=lambda t: t.created_at,
            )
            if not ready:
                return None

            task = ready[0]
            task.mark_running()
            self._persist()

        logger.debug(
            "MEMORY_QUEUE: dequeued %s (attempt %d)", task.task_id, task.attempt,
        )
        return task

    def complete(
        self,
        task_id: str,
        *,
        episode_uuid: str = "",
        entities: Optional[List[str]] = None,
        facts: int = 0,
    ) -> None:
        """Mark a task as completed and remove from active queue."""
        with self._lock:
            task = self._get_or_raise(task_id)
            task.mark_completed(episode_uuid=episode_uuid, entities=entities, facts=facts)
            self._stats["total_completed"] += 1
            # Remove terminal tasks from active queue (they're done)
            del self._tasks[task_id]
            self._persist()

        logger.debug("MEMORY_QUEUE: completed %s", task_id)

    def fail(self, task_id: str, error: str, *, backoff_base: float = 30.0) -> None:
        """Record a failure.  Task is retried or dead-lettered."""
        with self._lock:
            task = self._get_or_raise(task_id)
            task.mark_failed(error, backoff_base=backoff_base)

            if task.status == TaskStatus.DEAD_LETTER:
                self._stats["total_dead_lettered"] += 1
                self._append_dead_letter(task)
                del self._tasks[task_id]
            else:
                self._stats["total_retries"] += 1

            self._persist()

        logger.debug(
            "MEMORY_QUEUE: failed %s → %s (%s)",
            task_id, task.status, error[:80],
        )

    def get_task(self, task_id: str) -> Optional[MemoryTask]:
        """Retrieve a task by ID (returns None if not found)."""
        with self._lock:
            return self._tasks.get(task_id)

    def pending_count(self) -> int:
        """Number of tasks in PENDING state."""
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == TaskStatus.PENDING)

    def running_count(self) -> int:
        """Number of tasks in RUNNING state."""
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)

    def depth(self) -> int:
        """Total active (non-terminal) tasks."""
        with self._lock:
            return len(self._tasks)

    def status_summary(self) -> Dict[str, Any]:
        """Snapshot of queue state for MCP tool responses."""
        with self._lock:
            by_status: Dict[str, int] = {}
            for t in self._tasks.values():
                by_status[t.status] = by_status.get(t.status, 0) + 1

            oldest_age: Optional[float] = None
            if self._tasks:
                oldest_created = min(t.created_at for t in self._tasks.values())
                oldest_age = time.time() - oldest_created

            return {
                "queue_depth": len(self._tasks),
                "by_status": by_status,
                "oldest_task_age_s": round(oldest_age, 1) if oldest_age else None,
                "stats": dict(self._stats),
            }

    def recover_stale(self, stale_seconds: float = 600.0) -> int:
        """Reset RUNNING tasks that have been stuck for > *stale_seconds*.

        Returns:
            Number of tasks recovered.
        """
        now = time.time()
        recovered = 0
        with self._lock:
            for task in self._tasks.values():
                if (
                    task.status == TaskStatus.RUNNING
                    and (now - task.updated_at) > stale_seconds
                ):
                    task.status = TaskStatus.PENDING
                    task.updated_at = now
                    recovered += 1

            if recovered:
                self._persist()

        if recovered:
            logger.info("MEMORY_QUEUE: recovered %d stale tasks", recovered)
        return recovered

    def retry_dead_letters(self, max_count: int = 10) -> int:
        """Move dead-letter tasks back to the active queue.

        Reads from ``dead_letter.jsonl``, resets their status, and
        re-enqueues up to *max_count* of them.

        Returns:
            Number of tasks re-enqueued.
        """
        if not self._dead_letter_file.exists():
            return 0

        tasks: List[MemoryTask] = []
        remaining_lines: List[str] = []

        try:
            with open(self._dead_letter_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if len(tasks) < max_count:
                        try:
                            tasks.append(MemoryTask.from_json_line(line))
                        except (json.JSONDecodeError, KeyError, ValueError):
                            # ValueError: MemoryTask.__post_init__ rejects
                            # BULK tasks with empty group_id.
                            remaining_lines.append(line)
                    else:
                        remaining_lines.append(line)
        except OSError:
            return 0

        if not tasks:
            return 0

        with self._lock:
            for task in tasks:
                task.status = TaskStatus.PENDING
                task.attempt = 0
                task.next_retry_at = 0.0
                task.last_error = ""
                task.updated_at = time.time()
                self._tasks[task.task_id] = task

            self._persist()

        # Rewrite dead-letter file with remaining entries
        self._atomic_write(self._dead_letter_file, "\n".join(remaining_lines) + "\n" if remaining_lines else "")

        logger.info("MEMORY_QUEUE: re-enqueued %d dead-letter tasks", len(tasks))
        return len(tasks)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _persist(self) -> None:
        """Atomically rewrite queue.jsonl and stats.json.

        Must be called with ``self._lock`` held.
        """
        # Write active queue
        lines = [t.to_json_line() for t in self._tasks.values()]
        content = "\n".join(lines) + "\n" if lines else ""
        self._atomic_write(self._queue_file, content)

        # Write stats
        self._atomic_write(
            self._stats_file,
            json.dumps(self._stats, indent=2) + "\n",
        )

    def _load(self) -> None:
        """Load tasks from queue.jsonl and stats from stats.json."""
        # Load active queue
        if self._queue_file.exists():
            try:
                with open(self._queue_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            task = MemoryTask.from_json_line(line)
                            self._tasks[task.task_id] = task
                        except (json.JSONDecodeError, KeyError, ValueError) as e:
                            # ValueError: MemoryTask.__post_init__ rejects
                            # BULK tasks with empty group_id.
                            logger.warning("MEMORY_QUEUE: skipping corrupt line: %s", e)
            except OSError as e:
                logger.warning("MEMORY_QUEUE: could not load queue: %s", e)

        # Load stats
        if self._stats_file.exists():
            try:
                with open(self._stats_file, "r") as f:
                    loaded = json.load(f)
                    self._stats.update(loaded)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("MEMORY_QUEUE: could not load stats: %s", e)

        if self._tasks:
            logger.info(
                "MEMORY_QUEUE: loaded %d tasks from disk", len(self._tasks),
            )

    def _append_dead_letter(self, task: MemoryTask) -> None:
        """Append a task to dead_letter.jsonl.

        Must be called with ``self._lock`` held.
        """
        try:
            with open(self._dead_letter_file, "a") as f:
                f.write(task.to_json_line() + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            logger.warning("MEMORY_QUEUE: could not write dead letter: %s", e)

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write *content* to *path* via temp-file + fsync + rename."""
        try:
            fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning("MEMORY_QUEUE: atomic write failed for %s: %s", path, e)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _find_duplicate(self, task: MemoryTask) -> Optional[MemoryTask]:
        """Check for an equivalent non-terminal task (by dedup_key).

        Must be called with ``self._lock`` held.
        """
        key = task.dedup_key()
        for existing in self._tasks.values():
            if not existing.is_terminal and existing.dedup_key() == key:
                return existing
        return None

    def _get_or_raise(self, task_id: str) -> MemoryTask:
        """Fetch a task or raise TaskNotFoundError.

        Must be called with ``self._lock`` held.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(
                message=f"Task {task_id} not found in queue",
                context={"task_id": task_id},
            )
        return task
