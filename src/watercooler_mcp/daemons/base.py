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
        name: Unique daemon identifier (used for storage and logging)
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
    ) -> None:
        self.name = name
        self.interval = interval
        self.enabled = enabled
        self.tick_on_interval = tick_on_interval

        self._status = DaemonStatus.DISABLED if not enabled else DaemonStatus.STOPPED
        self._status_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = threading.Event()
        self._paused.set()  # Not paused initially
        self._checkpoint = load_checkpoint(name)
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
        with self._status_lock:
            self._status = DaemonStatus.RUNNING
        logger.debug("DAEMON[%s]: loop entered", self.name)

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

                # Run one tick
                start = time.monotonic()
                findings = self.tick()
                duration = time.monotonic() - start

                # Persist findings
                if findings:
                    append_findings(self.name, findings)

                # Update checkpoint
                self._checkpoint.last_run = time.time()
                self._checkpoint.last_run_duration = duration
                self._checkpoint.findings_produced = len(findings)
                save_checkpoint(self._checkpoint)

                self._total_ticks += 1
                self._total_findings += len(findings)
                self._last_error = None

                logger.debug(
                    "DAEMON[%s]: tick completed in %.2fs, %d findings",
                    self.name, duration, len(findings),
                )

            except Exception as exc:
                self._checkpoint.error_count += 1
                save_checkpoint(self._checkpoint)
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("DAEMON[%s]: tick error: %s", self.name, exc)
                # Brief sleep to avoid tight loop on persistent errors
                time.sleep(min(5.0, self.interval / 10))

        with self._status_lock:
            self._status = DaemonStatus.STOPPED
        logger.debug("DAEMON[%s]: loop exited", self.name)

    # ------------------------------------------------------------------ #
    # Health reporting
    # ------------------------------------------------------------------ #

    def status_summary(self) -> Dict[str, Any]:
        """Return a health summary dict for MCP tools."""
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
        )
