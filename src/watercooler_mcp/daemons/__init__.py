"""Daemon management — periodic thread scanning and hygiene.

Public API
----------
- ``init_daemons()`` — Initialise the singleton DaemonManager.
  Called once at MCP server startup, after init_memory_queue().
- ``get_daemon_manager()`` — Access the global DaemonManager instance.

Design follows MemoryTaskWorker: daemon threads with stop/wake events,
JSONL persistence, and atexit cleanup.
"""

from __future__ import annotations

import atexit
import logging
import threading
from typing import Optional

from .base import BaseDaemon, DaemonStatus
from .errors import (
    DaemonAlreadyRegisteredError,
    DaemonCheckpointError,
    DaemonError,
    DaemonLifecycleError,
    DaemonNotFoundError,
)
from .manager import DaemonManager
from .state import DaemonCheckpoint, Finding, ThreadCheckpoint

logger = logging.getLogger(__name__)

__all__ = [
    # Core types
    "BaseDaemon",
    "DaemonStatus",
    "DaemonManager",
    "Finding",
    "DaemonCheckpoint",
    "ThreadCheckpoint",
    # Errors
    "DaemonError",
    "DaemonAlreadyRegisteredError",
    "DaemonCheckpointError",
    "DaemonLifecycleError",
    "DaemonNotFoundError",
    # Singleton API
    "init_daemons",
    "get_daemon_manager",
]

# ------------------------------------------------------------------ #
# Module-level singleton
# ------------------------------------------------------------------ #

_manager: Optional[DaemonManager] = None
_init_lock = threading.Lock()


def get_daemon_manager() -> Optional[DaemonManager]:
    """Return the global DaemonManager instance (None if not initialised)."""
    return _manager


def init_daemons(*, start: bool = True) -> DaemonManager:
    """Initialise the singleton DaemonManager and register enabled daemons.

    Idempotent — calling multiple times returns the existing instance.

    Called once at MCP server startup, after init_memory_queue().

    Args:
        start: Whether to start all daemons immediately.

    Returns:
        The global DaemonManager instance.
    """
    global _manager

    with _init_lock:
        if _manager is not None:
            logger.debug("DAEMONS: already initialised, skipping")
            return _manager

        _manager = DaemonManager()

    # Load config to decide which daemons to register
    try:
        from watercooler.config_facade import config
        wc_config = config.get_config()
        daemons_config = wc_config.mcp.daemons

        if not daemons_config.enabled:
            logger.info("DAEMONS: globally disabled in config")
            return _manager

        # Register thread auditor if enabled
        if daemons_config.thread_auditor.enabled:
            from .auditor import ThreadAuditorDaemon

            auditor = ThreadAuditorDaemon(
                interval=daemons_config.thread_auditor.interval,
                config=daemons_config.thread_auditor,
            )
            _manager.register(auditor)

    except Exception as exc:
        logger.warning("DAEMONS: config load error, registering defaults: %s", exc)
        # Register thread auditor with defaults if config fails
        try:
            from .auditor import ThreadAuditorDaemon

            auditor = ThreadAuditorDaemon()
            _manager.register(auditor)
        except Exception as inner_exc:
            logger.warning("DAEMONS: failed to register default auditor: %s", inner_exc)

    if start:
        _manager.start_all()

    atexit.register(_shutdown_daemons)

    logger.info(
        "DAEMONS: initialised (%d daemons registered)",
        len(_manager.daemon_names),
    )
    return _manager


def _shutdown_daemons() -> None:
    """Atexit hook: gracefully stop all daemon threads."""
    if _manager is not None:
        logger.info("DAEMONS: shutting down (atexit)")
        _manager.stop_all(timeout=5.0)
