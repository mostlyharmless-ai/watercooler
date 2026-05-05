"""Abstract base class for daemon threads.

Lifecycle mirrors MemoryTaskWorker: background daemon thread with
stop/wake events, periodic ticking, and findings persistence.

Supports two execution models:
- tick_on_interval=True:  periodic scanner (e.g., thread auditor)
- tick_on_interval=False: event-driven only, sleeps until wake() (future runners)
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .state import (
    DaemonCheckpoint,
    Finding,
    _findings_strict_namespace,
    append_findings,
    load_checkpoint,
    load_findings,
    save_checkpoint,
)

logger = logging.getLogger(__name__)


class DaemonStatus(str, enum.Enum):
    """Daemon lifecycle states."""

    DISABLED = "disabled"
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"


class BaseDaemon(ABC):
    """Abstract base for daemon threads.

    Subclasses implement tick() which returns a list of findings.
    The base class handles threading, sleep/wake, checkpoint persistence,
    and findings logging.

    Args:
        name: Unique daemon identifier (used for storage and logging).
            Must match ``^[A-Za-z0-9_-]+$`` (validated by state._daemon_dir).
        interval: Seconds between periodic ticks
        enabled: Whether this daemon is active
        tick_on_interval: If True, tick() runs periodically. If False,
            the daemon sleeps indefinitely until wake() is called.
    """

    def __init__(
        self,
        name: str,
        *,
        interval: float = 300.0,
        enabled: bool = True,
        tick_on_interval: bool = True,
        state_namespace: str = "",
        scope_context: Optional[Any] = None,
    ) -> None:
        self.name = name
        self.interval = interval
        self.enabled = enabled
        self.tick_on_interval = tick_on_interval
        self.state_namespace = state_namespace
        self._scope_context = scope_context  # HttpRequestContext for hosted daemons
        self._hosted_worktree: Optional[Any] = None  # HostedWorktree, set by coordinator

        self._status = DaemonStatus.DISABLED if not enabled else DaemonStatus.STOPPED
        self._status_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = threading.Event()
        self._paused.set()  # Not paused initially
        # Defer checkpoint load when constructed without a namespace under
        # WATERCOOLER_FINDINGS_STRICT_NAMESPACE=1 — this is the hosted
        # construction-pre-_configure path. The hosted coordinator's
        # _configure() step (hosted_coordinator.py:_register_daemons_for_scope)
        # sets state_namespace = scope_id and re-loads the checkpoint with
        # the correct namespace before manager.start_all(). Without this
        # defer, every premium-daemon's __init__ throws under strict mode
        # because load_checkpoint(name, namespace="") fails the strict
        # gate at state._daemon_dir() (Plan v5.1 Move 3 strict-mode contract).
        # Local mode (strict off, namespace empty) keeps the eager load —
        # local single-tenant operates with empty namespace by design and
        # the load returns a fresh checkpoint.
        if state_namespace or not _findings_strict_namespace():
            self._checkpoint: Optional[DaemonCheckpoint] = load_checkpoint(
                name, namespace=state_namespace
            )
        else:
            self._checkpoint = None
        self._last_error: Optional[str] = None
        self._total_ticks: int = 0
        self._total_findings: int = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the background daemon thread."""
        if not self.enabled:
            with self._status_lock:
                self._status = DaemonStatus.DISABLED
            logger.info("DAEMON[%s]: disabled, not starting", self.name)
            return

        with self._status_lock:
            if self._status == DaemonStatus.RUNNING:
                return
            self._status = DaemonStatus.STARTING
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"Daemon-{self.name}",
            daemon=True,
        )
        self._thread.start()
        logger.info("DAEMON[%s]: started (interval=%.1fs)", self.name, self.interval)

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the daemon gracefully.

        Returns:
            True if the daemon stopped within timeout, False otherwise.
        """
        with self._status_lock:
            if self._status in (DaemonStatus.STOPPED, DaemonStatus.DISABLED):
                return True

        self._stop.set()
        self._wake.set()
        self._paused.set()  # Unpause so loop can exit

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("DAEMON[%s]: did not stop within timeout", self.name)
                return False

        with self._status_lock:
            self._status = DaemonStatus.STOPPED
        self._thread = None
        logger.info("DAEMON[%s]: stopped", self.name)
        return True

    def wake(self) -> None:
        """Trigger an immediate tick (unblocks the sleep wait)."""
        self._wake.set()

    def pause(self) -> None:
        """Pause the daemon (next tick will block until resumed)."""
        self._paused.clear()
        with self._status_lock:
            self._status = DaemonStatus.PAUSED
        logger.info("DAEMON[%s]: paused", self.name)

    def resume(self) -> None:
        """Resume a paused daemon."""
        self._paused.set()
        with self._status_lock:
            if self._status == DaemonStatus.PAUSED:
                self._status = DaemonStatus.RUNNING
        logger.info("DAEMON[%s]: resumed", self.name)

    @property
    def status(self) -> DaemonStatus:
        with self._status_lock:
            return self._status

    @property
    def is_running(self) -> bool:
        with self._status_lock:
            return self._status == DaemonStatus.RUNNING

    # ------------------------------------------------------------------ #
    # Abstract interface
    # ------------------------------------------------------------------ #

    @abstractmethod
    def tick(self) -> List[Finding]:
        """Run one scan cycle. Return findings discovered.

        Called periodically (if tick_on_interval=True) or on wake().
        Must be safe to call from the daemon thread.
        """
        ...

    def on_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Handle a dispatched event (override in event-driven daemons).

        Default: wake() on any event.
        """
        self.wake()

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def _loop(self) -> None:
        """Main daemon loop (runs in background thread)."""
        # Defense-in-depth: catch the deferred-checkpoint contract violation
        # (BaseDaemon constructed with empty namespace under STRICT_NAMESPACE,
        # then start()ed without _configure() having injected a scope and
        # re-loaded the checkpoint). Without this assert, the contract
        # violation surfaces as AttributeError on `self._checkpoint.last_run`
        # mid-tick — much harder to trace than a clear failure here.
        # See base.py:97-102 for the deferred-load rationale.
        assert self._checkpoint is not None, (
            f"DAEMON[{self.name}]: _checkpoint not initialized before _loop. "
            "Hosted scopes must call _configure() before manager.start_all(); "
            "local mode must use a non-empty state_namespace or run with "
            "WATERCOOLER_FINDINGS_STRICT_NAMESPACE off."
        )

        # Install scope context on this daemon thread so hosted data layers
        # can resolve user/repo/token via get_effective_context().
        # contextvars do not propagate to plain threading.Thread workers,
        # so we must explicitly set it here.
        if self._scope_context is not None:
            try:
                from watercooler_mcp.context import set_worker_context
                set_worker_context(self._scope_context)
            except Exception:
                pass  # best-effort; local daemons don't need this

        with self._status_lock:
            self._status = DaemonStatus.RUNNING
        logger.info("DAEMON[%s]: loop entered, interval=%.1fs", self.name, self.interval)

        while not self._stop.is_set():
            try:
                # Wait for unpause
                self._paused.wait()
                if self._stop.is_set():
                    break

                # Sleep until next tick or wake
                if self.tick_on_interval:
                    self._wake.wait(timeout=self.interval)
                else:
                    # Event-driven: sleep indefinitely until wake()
                    self._wake.wait()
                self._wake.clear()

                if self._stop.is_set():
                    break

                # Refresh hosted worktree if stale (Railway mode)
                if self._hosted_worktree is not None:
                    logger.info("DAEMON[%s]: refreshing worktree", self.name)
                    self._hosted_worktree.refresh_if_stale()

                # Run one tick
                logger.info("DAEMON[%s]: tick starting", self.name)
                start = time.monotonic()
                findings = self.tick()
                duration = time.monotonic() - start

                # Persist findings
                if findings:
                    append_findings(self.name, findings, namespace=self.state_namespace)

                # Update checkpoint
                self._checkpoint.last_run = time.time()
                self._checkpoint.last_run_duration = duration
                self._checkpoint.findings_produced = len(findings)
                save_checkpoint(self._checkpoint, namespace=self.state_namespace)

                self._total_ticks += 1
                self._total_findings += len(findings)
                self._last_error = None

                logger.info(
                    "DAEMON[%s]: tick completed in %.2fs, %d findings",
                    self.name, duration, len(findings),
                )

            except Exception as exc:
                self._checkpoint.error_count += 1
                save_checkpoint(self._checkpoint, namespace=self.state_namespace)
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("DAEMON[%s]: tick error: %s", self.name, exc)
                # Brief sleep to avoid tight loop on persistent errors.
                # Floor of 0.5s prevents >2 retries/sec for low-interval daemons.
                time.sleep(min(5.0, max(0.5, self.interval / 10)))

        with self._status_lock:
            self._status = DaemonStatus.STOPPED
        logger.debug("DAEMON[%s]: loop exited", self.name)

    # ------------------------------------------------------------------ #
    # Health reporting
    # ------------------------------------------------------------------ #

    def status_summary(self) -> Dict[str, Any]:
        """Return a health summary dict for MCP tools.

        Note: Fields other than ``status`` (e.g., _total_ticks, _last_error,
        _checkpoint.*) are read without _status_lock. These are simple
        assignments in CPython (GIL-protected), so reads see a consistent
        value — never a torn write. This is an intentional trade-off to
        avoid holding the lock across the entire dict construction.
        """
        with self._status_lock:
            status_val = self._status.value
        return {
            "name": self.name,
            "status": status_val,
            "enabled": self.enabled,
            "tick_on_interval": self.tick_on_interval,
            "interval": self.interval,
            "total_ticks": self._total_ticks,
            "total_findings": self._total_findings,
            "last_run": self._checkpoint.last_run,
            "last_run_duration": self._checkpoint.last_run_duration,
            "last_findings_count": self._checkpoint.findings_produced,
            "error_count": self._checkpoint.error_count,
            "last_error": self._last_error,
            "threads_processed": self._checkpoint.threads_processed,
            "threads_skipped": self._checkpoint.threads_skipped,
        }

    def get_findings(
        self,
        *,
        limit: int = 100,
        severity: Optional[str] = None,
        category: Optional[str] = None,
        topic: Optional[str] = None,
        unacknowledged_only: bool = False,
    ) -> List[Finding]:
        """Return findings from the JSONL log with optional filters."""
        return load_findings(
            self.name,
            limit=limit,
            severity=severity,
            category=category,
            topic=topic,
            unacknowledged_only=unacknowledged_only,
            namespace=self.state_namespace,
        )
