"""Background daemon thread that processes tasks from the memory queue.

Mirrors AsyncSyncCoordinator's worker loop pattern: poll queue, execute
task, handle success/failure, repeat.  Adds retry-with-backoff and
dead-letter semantics.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from .queue import MemoryTaskQueue
from .task import MemoryTask, TaskStatus, TaskType

logger = logging.getLogger(__name__)

# Sentinel callbacks are never called; they simply tell the worker
# that a backend is "known" but has no handler registered yet.
_SENTINEL = object()


class MemoryTaskWorker:
    """Daemon thread that drains the memory task queue.

    Args:
        queue: The shared MemoryTaskQueue instance.
        poll_interval: Seconds between queue polls (default 5).
        stale_timeout: Seconds before a RUNNING task is considered stale
            and reset to PENDING (default 600 = 10 min).
    """

    def __init__(
        self,
        queue: MemoryTaskQueue,
        *,
        poll_interval: float = 5.0,
        stale_timeout: float = 600.0,
    ) -> None:
        self._queue = queue
        self._poll_interval = poll_interval
        self._stale_timeout = stale_timeout

        # Backend executors: backend_name → async callable(MemoryTask) → result dict
        self._executors: Dict[str, Callable[..., Any]] = {}

        # Thread state
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running = False

    # ------------------------------------------------------------------ #
    # Executor registration
    # ------------------------------------------------------------------ #

    def register_executor(
        self,
        backend: str,
        executor: Callable[[MemoryTask], Any],
    ) -> None:
        """Register an async callable that processes tasks for *backend*.

        The callable receives a :class:`MemoryTask` and should return a
        dict with ``episode_uuid``, ``entities_extracted``, and
        ``facts_extracted`` on success, or raise on failure.
        """
        self._executors[backend] = executor
        logger.debug("MEMORY_QUEUE: registered executor for '%s'", backend)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the background worker thread."""
        if self._running:
            return

        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="MemoryTaskWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info("MEMORY_QUEUE: worker started")

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the worker gracefully.

        Returns:
            True if the worker stopped within *timeout*, False otherwise.
        """
        if not self._running:
            return True

        self._stop.set()
        self._wake.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("MEMORY_QUEUE: worker did not stop within timeout")
                return False

        self._running = False
        self._thread = None
        logger.info("MEMORY_QUEUE: worker stopped")
        return True

    def wake(self) -> None:
        """Signal the worker to check the queue immediately."""
        self._wake.set()

    @property
    def is_running(self) -> bool:
        return self._running

    def has_executor(self, backend: str) -> bool:
        """Check if an executor is registered for *backend*."""
        return backend in self._executors

    # ------------------------------------------------------------------ #
    # Worker loop
    # ------------------------------------------------------------------ #

    def _loop(self) -> None:
        """Main worker loop (runs in daemon thread)."""
        logger.debug("MEMORY_QUEUE: worker loop entered")

        # Recover stale tasks on startup
        self._queue.recover_stale(self._stale_timeout)

        while not self._stop.is_set():
            try:
                task = self._queue.dequeue()
                if task is None:
                    # Nothing ready — wait for wake signal or poll interval
                    self._wake.wait(timeout=self._poll_interval)
                    self._wake.clear()
                    continue

                self._process_task(task)

            except Exception as exc:
                logger.exception("MEMORY_QUEUE: unexpected worker error: %s", exc)
                time.sleep(1)  # Avoid tight loop on persistent errors

        logger.debug("MEMORY_QUEUE: worker loop exited")

    def _process_task(self, task: MemoryTask) -> None:
        """Execute a single task synchronously (within the worker thread)."""
        executor = self._executors.get(task.backend)
        if executor is None:
            self._queue.fail(
                task.task_id,
                f"No executor registered for backend '{task.backend}'",
            )
            return

        try:
            # Run the async executor in a new event loop for this thread.
            # Each task gets its own loop to avoid cross-task interference.
            result = asyncio.run(executor(task))

            self._queue.complete(
                task.task_id,
                episode_uuid=result.get("episode_uuid", ""),
                entities=result.get("entities_extracted"),
                facts=result.get("facts_extracted", 0),
            )

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "MEMORY_QUEUE: task %s failed (attempt %d/%d): %s",
                task.task_id, task.attempt, task.max_attempts, error_msg,
            )
            self._queue.fail(task.task_id, error_msg)
