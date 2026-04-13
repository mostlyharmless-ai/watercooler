"""Background daemon thread that processes tasks from the memory queue.

Mirrors AsyncSyncCoordinator's worker loop pattern: poll queue, execute
task, handle success/failure, repeat.  Adds retry-with-backoff and
dead-letter semantics.

Architecture: each worker thread owns a persistent asyncio event loop for
its entire lifetime.  Backend objects (e.g. GraphitiBackend) hold asyncio
Lock objects that are bound to the loop at creation time; reusing the loop
across tasks allows reusing the backend — avoiding per-task connection
creation and the resulting connection exhaustion under concurrent load.

A keyed backend slot (one per thread, keyed by host:port:group_id) is
managed in thread-local state by the graphiti executor in memory_sync.py.
The worker evicts that slot (via _reset_thread_backend) when a task times
out or raises a transport-level error so the next retry creates a fresh
connection instead of reusing a poisoned one.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .errors import PermanentTaskError as _PermanentTaskError
from .queue import MemoryTaskQueue
from .task import MemoryTask

logger = logging.getLogger(__name__)

# Sentinel callbacks are never called; they simply tell the worker
# that a backend is "known" but has no handler registered yet.
_SENTINEL = object()


@dataclass
class _WorkerThreadState:
  """Per-worker-thread state stored in threading.local().

  Must only be accessed from within the owning worker thread.
  """

  graphiti_backend: Any = None        # Optional[GraphitiBackend]
  graphiti_backend_key: Optional[str] = None


class MemoryTaskWorker:
  """Daemon thread that drains the memory task queue.

  Args:
      queue: The shared MemoryTaskQueue instance.
      poll_interval: Seconds between queue polls (default 5).
      stale_timeout: Seconds before a RUNNING task is considered stale
          and reset to PENDING (default 600 = 10 min).
      task_timeout: Base timeout (seconds) for a single task execution.
          Effective timeout escalates on retry: base * 2^(attempt-1), capped
          at stale_timeout. Tasks that exceed the effective timeout are failed
          and evict the thread's cached backend so the next retry starts with
          a fresh connection. Default 300s (operators can raise via
          WATERCOOLER_MEMORY_QUEUE_TASK_TIMEOUT or config.toml).
  """

  def __init__(
    self,
    queue: MemoryTaskQueue,
    *,
    poll_interval: float = 5.0,
    stale_timeout: float = 600.0,
    max_workers: int = 1,
    task_timeout: float = 300.0,
  ) -> None:
    self._queue = queue
    self._poll_interval = poll_interval
    self._stale_timeout = stale_timeout
    self._max_workers = max(1, max_workers)
    self._task_timeout = task_timeout

    # Backend executors: backend_name → async callable(MemoryTask) → result dict
    self._executors: Dict[str, Callable[..., Any]] = {}

    # Thread state
    self._thread_local = threading.local()   # per-thread: .loop, .state
    self._threads: List[threading.Thread] = []
    self._stop = threading.Event()
    self._wake = threading.Event()
    self._running = False

    # Aggregate counters — _backend_count_lock guards active_backend_count
    # because max(0, x-1) is a read-modify-write that is not atomic under the GIL.
    self._backend_count_lock = threading.Lock()
    self.total_timeouts: int = 0
    self.active_backend_count: int = 0

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
    """Start the background worker thread(s).

    Refuses to start if old worker threads from a timed-out stop() are
    still alive — prevents duplicate concurrent workers.
    """
    if self._running:
      # After a timed-out stop(), _running stays True but threads may
      # have since exited on their own.  Detect this and auto-recover.
      alive = [t for t in self._threads if t.is_alive()]
      if alive:
        logger.warning(
          "MEMORY_QUEUE: refusing start — still running with %d live thread(s)",
          len(alive),
        )
        return
      # All threads from the timed-out stop have exited — reset state.
      self._running = False
      self._threads = []
      logger.info(
        "MEMORY_QUEUE: auto-recovered from timed-out stop — all threads exited"
      )

    # Guard: refuse restart if old threads are still alive (timed-out stop).
    alive = [t for t in self._threads if t.is_alive()]
    if alive:
      logger.warning(
        "MEMORY_QUEUE: refusing start — %d old worker thread(s) still alive: %s",
        len(alive),
        [t.name for t in alive],
      )
      return

    self._threads = []
    self._stop.clear()
    self._running = True

    # Recover stale tasks once before spawning threads — avoids N
    # concurrent recover_stale() calls racing at startup.
    self._queue.recover_stale(self._stale_timeout)

    for i in range(self._max_workers):
      name = (
        "MemoryTaskWorker"
        if self._max_workers == 1
        else f"MemoryTaskWorker-{i + 1}"
      )
      t = threading.Thread(target=self._loop, name=name, daemon=True)
      self._threads.append(t)
      t.start()

    logger.info(
      "MEMORY_QUEUE: worker started (%d thread%s)",
      self._max_workers,
      "s" if self._max_workers > 1 else "",
    )

  def stop(self, timeout: float = 10.0) -> bool:
    """Stop the worker gracefully.

    Returns:
        True if all worker threads stopped within *timeout*, False otherwise.

    Note: with task_timeout=120s, threads mid-task at stop() time will not
    exit until the task times out (up to task_timeout seconds). The existing
    warning path handles this — behavior is strictly better than the prior
    state where hung threads never exited at all.
    """
    if not self._running:
      return True

    self._stop.set()
    self._wake.set()

    all_stopped = True
    deadline = time.monotonic() + timeout
    for t in self._threads:
      remaining = max(0.0, deadline - time.monotonic())
      if t.is_alive():
        t.join(timeout=remaining)
        if t.is_alive():
          logger.warning(
            "MEMORY_QUEUE: worker thread %s did not stop within timeout",
            t.name,
          )
          all_stopped = False

    if all_stopped:
      self._running = False
      self._threads = []
      logger.info("MEMORY_QUEUE: worker stopped")
    else:
      # Keep _running=True and _threads intact so start() refuses to
      # spawn new threads while old ones are still alive.  _stop remains
      # asserted so old threads will exit when they next check it.
      logger.warning(
        "MEMORY_QUEUE: worker stop incomplete — %d thread(s) still alive",
        sum(1 for t in self._threads if t.is_alive()),
      )

    return all_stopped

  def wake(self) -> None:
    """Signal the worker to check the queue immediately."""
    self._wake.set()

  @property
  def is_running(self) -> bool:
    return self._running

  @property
  def worker_count(self) -> int:
    """Number of configured worker threads."""
    return self._max_workers

  def has_executor(self, backend: str) -> bool:
    """Check if an executor is registered for *backend*."""
    return backend in self._executors

  # ------------------------------------------------------------------ #
  # Thread-local state
  # ------------------------------------------------------------------ #

  def _get_thread_state(self) -> _WorkerThreadState:
    """Return per-thread state for the calling thread.

    Must only be called from within a worker thread — reads thread-local
    storage for the calling thread.  Initialises state on first call from
    each thread.
    """
    state = getattr(self._thread_local, "state", None)
    if state is None:
      state = _WorkerThreadState()
      self._thread_local.state = state
    return state

  # ------------------------------------------------------------------ #
  # Worker loop
  # ------------------------------------------------------------------ #

  def _loop(self) -> None:
    """Main worker loop (runs in daemon thread).

    Creates one asyncio event loop for this thread's entire lifetime so
    that backend objects (and their asyncio.Lock instances) are always
    bound to the same loop — enabling safe backend reuse across tasks.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    self._thread_local.loop = loop
    self._thread_local.state = _WorkerThreadState()

    logger.info(
      "MEMORY_QUEUE: worker thread started with persistent event loop (thread=%s)",
      threading.current_thread().name,
    )

    try:
      while not self._stop.is_set():
        try:
          task = self._queue.dequeue()
          if task is None:
            # Nothing ready — wait for wake signal or poll interval.
            # Clear before wait to prevent lost-wake TOCTOU race:
            # if set() fires between wait-return and clear, the signal
            # is lost.  Clearing first is safe — a set() that fires
            # between clear and wait will still unblock wait().
            # Known limitation (multi-worker): _wake is shared across
            # all N worker threads.  set() unblocks ALL waiters
            # (thundering herd — N-1 wasted dequeue attempts) and a
            # set() racing between one thread's clear() and another's
            # wait() can be lost (bounded by poll_interval, latency
            # only).  At recommended 3-5 workers the overhead is
            # negligible.  To eliminate both issues, replace Event
            # with threading.Condition + notify(1).
            # See: todo 099, PR #379 review round 3 (R3-2).
            self._wake.clear()
            self._wake.wait(timeout=self._poll_interval)
            continue

          self._process_task(task)

        except Exception as exc:
          logger.exception("MEMORY_QUEUE: unexpected worker error: %s", exc)
          time.sleep(1)  # Avoid tight loop on persistent errors
    finally:
      # Graceful shutdown: close per-thread backend then the loop.
      self._cleanup_thread_resources(loop)
      logger.debug("MEMORY_QUEUE: worker loop exited")

  def _process_task(self, task: MemoryTask) -> None:
    """Execute a single task synchronously (within the worker thread)."""
    executor = self._executors.get(task.backend)
    if executor is None:
      self._queue.fail(
        task.task_id,
        f"No executor registered for backend '{task.backend}'",
        permanent=True,
      )
      return

    # Escalate timeout on retry: base * 2^(attempt-1), capped at stale_timeout.
    # attempt==1 on first run (incremented by mark_running before _process_task).
    # With defaults (task_timeout=300s, stale_timeout=600s) this yields two
    # distinct values: 300s on attempt 1, 600s on attempt 2+.  Meaningful
    # multi-step escalation requires stale_timeout > 2 * task_timeout.
    effective_timeout = min(
      self._task_timeout * (2 ** (task.attempt - 1)),
      self._stale_timeout,
    )

    loop = self._thread_local.loop
    try:
      result = loop.run_until_complete(
        asyncio.wait_for(executor(task), timeout=effective_timeout)
      )

      self._queue.complete(
        task.task_id,
        episode_uuid=result.get("episode_uuid", ""),
        entities=result.get("entities_extracted"),
        facts=result.get("facts_extracted", 0),
      )
      logger.info(
        "MEMORY_QUEUE: task %s completed (entry=%s, topic=%s, uuid=%s)",
        task.task_id, task.entry_id, task.topic,
        result.get("episode_uuid", ""),
      )

    except asyncio.TimeoutError:
      self.total_timeouts += 1
      error_msg = f"TimeoutError: task exceeded {effective_timeout}s limit (attempt {task.attempt}, base {self._task_timeout}s)"
      logger.warning(
        "MEMORY_QUEUE: task %s timed out (attempt %d/%d, total_timeouts=%d)",
        task.task_id, task.attempt, task.max_attempts, self.total_timeouts,
      )
      self._reset_thread_backend(loop, reason="task_timeout")
      self._queue.fail(task.task_id, error_msg)

    except asyncio.CancelledError:
      # Not a task timeout — treat as generic failure without incrementing counter.
      # On Python 3.10 asyncio.wait_for could raise CancelledError in edge cases;
      # on 3.11+ this path is only reached by external loop cancellation.
      error_msg = "CancelledError: task was unexpectedly cancelled"
      logger.warning(
        "MEMORY_QUEUE: task %s cancelled (attempt %d/%d)",
        task.task_id, task.attempt, task.max_attempts,
      )
      self._reset_thread_backend(loop, reason="task_cancelled")
      self._queue.fail(task.task_id, error_msg)

    except Exception as exc:
      error_msg = f"{type(exc).__name__}: {exc}"
      is_permanent = (
        isinstance(exc, ImportError)
        or isinstance(exc, _PermanentTaskError)
      )
      logger.warning(
        "MEMORY_QUEUE: task %s failed (attempt %d/%d, permanent=%s): %s",
        task.task_id, task.attempt, task.max_attempts, is_permanent, error_msg,
      )
      self._reset_thread_backend(loop, reason="task_error")
      self._queue.fail(task.task_id, error_msg, permanent=is_permanent)

  def _reset_thread_backend(
    self, loop: asyncio.AbstractEventLoop, reason: str
  ) -> None:
    """Close and evict the current thread's cached backend.

    Called after a task timeout or transport error to ensure the next
    task retries with a fresh connection instead of a stale/poisoned one.

    Steps:
    1. Drain pending cancellation callbacks (left over from TimeoutError).
    2. Close the backend with a bounded 30s timeout.
    3. Clear thread-local state.
    4. Log the eviction for operator visibility.
    """
    state = self._get_thread_state()
    backend = state.graphiti_backend
    db_key = state.graphiti_backend_key

    if backend is not None:
      try:
        # Drain any pending cancellation callbacks first so they don't
        # interleave with aclose() if they hold asyncio locks.
        loop.run_until_complete(asyncio.sleep(0))
        # Bound aclose() — if FalkorDB is also hung, aclose can hang.
        loop.run_until_complete(
          asyncio.wait_for(backend.aclose(), timeout=30.0)
        )
      except (asyncio.TimeoutError, asyncio.CancelledError):
        logger.warning(
          "MEMORY_QUEUE: backend aclose timed out during %s (key=%s)",
          reason, db_key,
        )
      except Exception as exc:
        logger.warning(
          "MEMORY_QUEUE: backend aclose failed during %s: %s", reason, exc
        )
      with self._backend_count_lock:
        self.active_backend_count = max(0, self.active_backend_count - 1)

      log_level = logging.WARNING if reason == "task_timeout" else logging.INFO
      logger.log(
        log_level,
        "MEMORY_QUEUE: evicted thread-local backend (thread=%s, reason=%s, key=%s)",
        threading.current_thread().name,
        reason,
        db_key,
      )

    state.graphiti_backend = None
    state.graphiti_backend_key = None

  def _cleanup_thread_resources(self, loop: asyncio.AbstractEventLoop) -> None:
    """Close per-thread backend and shut down the event loop cleanly.

    Follows asyncio.Runner.close() sequence (CPython runners.py):
    1. Close the per-thread backend (via _reset_thread_backend).
    2. Cancel any remaining tasks on this loop (defensive).
    3. Close async generators registered with this loop.
    4. Shut down the default executor (joins asyncio.to_thread() calls).
    5. Clear the thread-local event loop reference.
    6. Close the loop.
    """
    # 1. Close per-thread backend.
    self._reset_thread_backend(loop, reason="worker_shutdown")

    # 2. Cancel any remaining pending tasks.
    to_cancel = asyncio.all_tasks(loop)
    if to_cancel:
      for t in to_cancel:
        t.cancel()
      loop.run_until_complete(asyncio.gather(*to_cancel, return_exceptions=True))

    # 3. Close async generators.
    loop.run_until_complete(loop.shutdown_asyncgens())

    # 4. Shut down default executor (joins any asyncio.to_thread() calls).
    loop.run_until_complete(loop.shutdown_default_executor())

    # 5. Clear the thread-local loop reference.
    asyncio.set_event_loop(None)

    # 6. Close the loop (releases selector FDs, self-pipe).
    try:
      loop.close()
    except Exception:
      pass
