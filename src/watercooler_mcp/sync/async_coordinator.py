"""Async coordination for background git sync operations.

This module provides:
- PendingCommit: Dataclass for queued commit entries
- AsyncConfig: Configuration for async sync behavior
- AsyncSyncCoordinator: Background worker for batched pushes

The coordinator batches multiple commits together and pushes them
in the background, reducing latency for write operations.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..observability import log_debug
from .primitives import push_with_retry
from .errors import PushError, SyncError


# =============================================================================
# Constants
# =============================================================================

QUEUE_FILE_NAME = "queue.jsonl"
DEFAULT_BATCH_WINDOW = 5.0  # seconds to wait for more commits
DEFAULT_MAX_DELAY = 30.0  # max seconds before forcing push
DEFAULT_MAX_BATCH_SIZE = 50  # max commits per batch
DEFAULT_SYNC_INTERVAL = 30.0  # background sync check interval


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class PendingCommit:
    """A commit waiting in the async queue.

    Attributes:
        sequence: Monotonic sequence number for ordering
        entry_id: Optional ULID of the entry (for tracking)
        topic: Optional thread topic
        commit_message: The commit message
        timestamp: ISO timestamp when queued
        created_ts: Unix timestamp for age calculations
    """

    sequence: int
    entry_id: Optional[str]
    topic: Optional[str]
    commit_message: str
    timestamp: str
    created_ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PendingCommit":
        """Create from dictionary."""
        return cls(
            sequence=data["sequence"],
            entry_id=data.get("entry_id"),
            topic=data.get("topic"),
            commit_message=data["commit_message"],
            timestamp=data["timestamp"],
            created_ts=data.get("created_ts", time.time()),
        )


@dataclass
class AsyncConfig:
    """Configuration for async sync behavior.

    Attributes:
        batch_window: Seconds to wait for more commits before pushing
        max_delay: Maximum seconds before forcing a push
        max_batch_size: Maximum commits to batch together
        sync_interval: Background sync check interval
        enabled: Whether async mode is enabled
    """

    batch_window: float = DEFAULT_BATCH_WINDOW
    max_delay: float = DEFAULT_MAX_DELAY
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE
    sync_interval: float = DEFAULT_SYNC_INTERVAL
    enabled: bool = True


@dataclass
class AsyncStatus:
    """Status of the async sync coordinator.

    Attributes:
        running: Whether the worker thread is running
        queue_depth: Number of pending commits
        oldest_commit_age: Age of oldest commit in seconds (None if empty)
        last_push_at: ISO timestamp of last successful push
        last_error: Last error message (if any)
        total_pushed: Total commits pushed since start
        total_failed: Total failed push attempts
    """

    running: bool = False
    queue_depth: int = 0
    oldest_commit_age: Optional[float] = None
    last_push_at: Optional[str] = None
    last_error: Optional[str] = None
    total_pushed: int = 0
    total_failed: int = 0


# =============================================================================
# Async Sync Coordinator
# =============================================================================


class AsyncSyncCoordinator:
    """Background worker for batched git pushes.

    This coordinator queues commits and pushes them in batches to reduce
    latency for write operations. It uses a background thread to monitor
    the queue and push when:
    - batch_window expires after the first commit
    - max_delay is reached for the oldest commit
    - max_batch_size is reached
    - flush_now() is called explicitly

    Usage:
        coordinator = AsyncSyncCoordinator(
            repo_path=Path("/path/to/repo"),
            config=AsyncConfig(batch_window=5.0),
        )
        coordinator.start()

        # Queue commits
        seq = coordinator.enqueue_commit(
            commit_message="Add entry",
            topic="feature-auth",
            entry_id="01ABC...",
        )

        # Force immediate push
        coordinator.flush_now(timeout=60.0)

        # Shutdown
        coordinator.shutdown(timeout=30.0)
    """

    def __init__(
        self,
        repo_path: Path,
        config: Optional[AsyncConfig] = None,
        *,
        queue_dir: Optional[Path] = None,
        on_push_success: Optional[Callable[[List[PendingCommit]], None]] = None,
        on_push_failure: Optional[Callable[[List[PendingCommit], Exception], None]] = None,
    ):
        """Initialize the async coordinator.

        Args:
            repo_path: Path to the git repository
            config: Async configuration (uses defaults if None)
            queue_dir: Directory for queue persistence (defaults to repo_path)
            on_push_success: Callback after successful push
            on_push_failure: Callback after failed push
        """
        self.repo_path = Path(repo_path)
        self.config = config or AsyncConfig()
        self.queue_dir = Path(queue_dir) if queue_dir else self.repo_path
        self.on_push_success = on_push_success
        self.on_push_failure = on_push_failure

        # Queue state
        self._queue: List[PendingCommit] = []
        self._sequence = 0
        self._lock = threading.Lock()
        self._queue_file = self.queue_dir / QUEUE_FILE_NAME

        # Worker state
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._flush_event = threading.Event()
        self._running = False

        # Statistics
        self._last_push_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._total_pushed = 0
        self._total_failed = 0

        # Load persisted queue
        self._load_queue()

    def start(self) -> None:
        """Start the background worker thread."""
        if self._running:
            return

        self._stop_event.clear()
        self._running = True
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="AsyncSyncCoordinator",
            daemon=True,
        )
        self._worker.start()
        log_debug("[ASYNC] Started background sync worker")

    def stop(self) -> None:
        """Stop the background worker (alias for shutdown with no timeout)."""
        self.shutdown(timeout=0)

    def shutdown(self, timeout: float = 30.0) -> bool:
        """Shutdown the coordinator gracefully.

        Args:
            timeout: Maximum seconds to wait for worker to finish

        Returns:
            True if shutdown completed cleanly, False if timed out
        """
        if not self._running:
            return True

        log_debug("[ASYNC] Shutting down background sync worker")
        self._stop_event.set()
        self._flush_event.set()  # Wake up worker

        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=timeout)
            if self._worker.is_alive():
                log_debug("[ASYNC] Worker did not stop within timeout")
                return False

        self._running = False
        self._worker = None
        return True

    def enqueue_commit(
        self,
        *,
        commit_message: str,
        topic: Optional[str] = None,
        entry_id: Optional[str] = None,
        priority_flush: bool = False,
    ) -> int:
        """Add a commit to the async queue.

        Args:
            commit_message: The commit message
            topic: Optional thread topic
            entry_id: Optional entry ULID for tracking
            priority_flush: If True, trigger immediate flush

        Returns:
            Sequence number of the queued commit
        """
        with self._lock:
            self._sequence += 1
            commit = PendingCommit(
                sequence=self._sequence,
                entry_id=entry_id,
                topic=topic,
                commit_message=commit_message,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            self._queue.append(commit)
            self._persist_queue()

            log_debug(
                f"[ASYNC] Queued commit #{commit.sequence}: "
                f"topic={topic}, entry_id={entry_id}"
            )

        if priority_flush or len(self._queue) >= self.config.max_batch_size:
            self._flush_event.set()

        return commit.sequence

    def flush_now(self, timeout: float = 60.0) -> bool:
        """Force immediate push of all queued commits.

        Args:
            timeout: Maximum seconds to wait for push to complete

        Returns:
            True if flush completed successfully, False otherwise
        """
        if not self._queue:
            return True

        log_debug(f"[ASYNC] Flush requested, {len(self._queue)} commits pending")
        self._flush_event.set()

        # Wait for queue to empty
        start = time.time()
        while self._queue and (time.time() - start) < timeout:
            time.sleep(0.1)

        if self._queue:
            log_debug(f"[ASYNC] Flush timed out, {len(self._queue)} commits remaining")
            return False

        return True

    def status(self) -> AsyncStatus:
        """Get current status of the coordinator."""
        with self._lock:
            oldest_age = None
            if self._queue:
                oldest_age = time.time() - self._queue[0].created_ts

            return AsyncStatus(
                running=self._running,
                queue_depth=len(self._queue),
                oldest_commit_age=oldest_age,
                last_push_at=self._last_push_at,
                last_error=self._last_error,
                total_pushed=self._total_pushed,
                total_failed=self._total_failed,
            )

    def get_queue(self) -> List[PendingCommit]:
        """Get a copy of the current queue."""
        with self._lock:
            return list(self._queue)

    def _worker_loop(self) -> None:
        """Background worker loop."""
        log_debug("[ASYNC] Worker loop started")

        while not self._stop_event.is_set():
            try:
                # Wait for flush event or timeout
                self._flush_event.wait(timeout=self.config.sync_interval)
                self._flush_event.clear()

                if self._stop_event.is_set():
                    break

                # Check if we should push
                if self._should_push():
                    self._do_push()

            except Exception as e:
                log_debug(f"[ASYNC] Worker loop error: {e}")
                self._last_error = str(e)
                time.sleep(1)  # Avoid tight loop on errors

        # Final flush on shutdown
        if self._queue:
            log_debug("[ASYNC] Final flush on shutdown")
            self._do_push()

        log_debug("[ASYNC] Worker loop exited")

    def _should_push(self) -> bool:
        """Check if we should push now."""
        with self._lock:
            if not self._queue:
                return False

            # Check max batch size
            if len(self._queue) >= self.config.max_batch_size:
                return True

            # Check max delay for oldest commit
            oldest_age = time.time() - self._queue[0].created_ts
            if oldest_age >= self.config.max_delay:
                return True

            # Check batch window (if we have commits)
            if oldest_age >= self.config.batch_window:
                return True

            return False

    def _do_push(self) -> None:
        """Execute the push operation.

        Includes safety guards for orphaned and diverged branches:
        - Diverged: branch is both ahead AND behind origin (skip push, risk of data loss)
        - These guards prevent the async sync from interfering with manual recovery

        TOCTOU Race Window:
            There is an inherent race between checking diverged state and pushing.
            We mitigate this by:
            1. Initial check before any push attempt
            2. Re-check immediately before push to minimize the window
            3. Relying on push_with_retry to fail safely if state changed

            A stronger guarantee would require --force-with-lease, but that adds
            complexity and would reject pushes when remote has new commits from
            other collaborators (which is often intentional). The current approach
            is a reasonable tradeoff: we detect most divergence, and the few
            cases that slip through fail safely on push rather than corrupting data.
        """
        with self._lock:
            if not self._queue:
                return

            batch = list(self._queue)
            log_debug(f"[ASYNC] Pushing batch of {len(batch)} commits")

        try:
            # Import git classes here to avoid circular imports
            from git import Repo
            from git.exc import GitCommandError
            from .primitives import get_ahead_behind

            repo = Repo(self.repo_path, search_parent_directories=True)
            branch = repo.active_branch.name if not repo.head.is_detached else None

            if branch:
                # Safety check: detect diverged state (ahead AND behind)
                # This prevents async sync from interfering with manual recovery
                #
                # Note: This check runs outside the queue lock because:
                # 1. Git state is external - locking our queue doesn't protect git
                # 2. If divergence occurs after our check, push_with_retry will fail safely
                # 3. Holding lock during git operations would block enqueue for too long
                try:
                    ahead, behind = get_ahead_behind(repo, branch)
                    if ahead > 0 and behind > 0:
                        log_debug(
                            f"[ASYNC] SKIPPING push - branch '{branch}' is diverged: "
                            f"{ahead} ahead, {behind} behind. Risk of data loss. "
                            f"Use 'recover' operation to fix."
                        )
                        with self._lock:
                            self._last_error = (
                                f"Push skipped: branch diverged ({ahead} ahead, {behind} behind). "
                                f"Run sync_branch_state with operation='recover'."
                            )
                        return
                except (GitCommandError, ValueError) as e:
                    # Expected: GitCommandError for missing upstream, ValueError for parse issues
                    log_debug(f"[ASYNC] Could not check ahead/behind (non-fatal): {e}")
                except Exception as e:
                    # Unexpected exception - log and record error, but continue
                    # (could indicate git corruption or filesystem issues)
                    log_debug(f"[ASYNC] WARNING: Unexpected error checking ahead/behind: {type(e).__name__}: {e}")
                    with self._lock:
                        self._last_error = f"Unexpected error checking branch state: {type(e).__name__}: {e}"

                # Re-check diverged state immediately before push to minimize TOCTOU window
                # (another process could have pushed between initial check and now)
                try:
                    ahead, behind = get_ahead_behind(repo, branch)
                    if ahead > 0 and behind > 0:
                        log_debug(
                            f"[ASYNC] SKIPPING push (re-check) - branch '{branch}' diverged"
                        )
                        with self._lock:
                            self._last_error = (
                                f"Push skipped: branch diverged on re-check. "
                                f"Run sync_branch_state with operation='recover'."
                            )
                        return
                except Exception:
                    pass  # Best-effort re-check, proceed with push

                success = push_with_retry(repo, branch)
                if success:
                    with self._lock:
                        # Remove pushed commits from queue
                        pushed_sequences = {c.sequence for c in batch}
                        self._queue = [
                            c for c in self._queue if c.sequence not in pushed_sequences
                        ]
                        self._persist_queue()
                        self._total_pushed += len(batch)
                        self._last_push_at = datetime.now(timezone.utc).isoformat()
                        self._last_error = None

                    log_debug(f"[ASYNC] Successfully pushed {len(batch)} commits")

                    if self.on_push_success:
                        try:
                            self.on_push_success(batch)
                        except Exception as e:
                            log_debug(f"[ASYNC] on_push_success callback error: {e}")
                else:
                    raise PushError("Push failed after retries")
            else:
                raise SyncError("Cannot push from detached HEAD")

        except Exception as e:
            log_debug(f"[ASYNC] Push failed: {e}")
            with self._lock:
                self._total_failed += 1
                self._last_error = str(e)

            if self.on_push_failure:
                try:
                    self.on_push_failure(batch, e)
                except Exception as cb_e:
                    log_debug(f"[ASYNC] on_push_failure callback error: {cb_e}")

    def _persist_queue(self) -> None:
        """Persist queue to disk (must hold lock)."""
        try:
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            with open(self._queue_file, "w", encoding="utf-8") as f:
                for commit in self._queue:
                    f.write(json.dumps(commit.to_dict()) + "\n")
        except Exception as e:
            log_debug(f"[ASYNC] Failed to persist queue: {e}")

    def _load_queue(self) -> None:
        """Load queue from disk."""
        if not self._queue_file.exists():
            return

        try:
            with open(self._queue_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        commit = PendingCommit.from_dict(data)
                        self._queue.append(commit)
                        self._sequence = max(self._sequence, commit.sequence)
                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                        # Skip corrupted lines but continue loading
                        log_debug(f"[ASYNC] Skipping corrupted queue line: {e}")
                        continue

            if self._queue:
                log_debug(f"[ASYNC] Loaded {len(self._queue)} pending commits from queue")
        except Exception as e:
            log_debug(f"[ASYNC] Failed to load queue file: {e}")
            self._queue = []


# =============================================================================
# Convenience Functions
# =============================================================================


def get_queue_file_path(repo_path: Path) -> Path:
    """Get path to the queue file."""
    return repo_path / QUEUE_FILE_NAME
