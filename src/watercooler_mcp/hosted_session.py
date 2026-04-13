"""Hosted session store for JSON-RPC adapter mode.

Tracks MCP session lifecycle (initialize → tool calls → shutdown) for
hosted connections where multiple concurrent sessions share a single
server process.  Each session is identified by a session_id derived
from the ``mcp-session-id`` / ``x-session-id`` request header.

Thread safety:
    All mutations are guarded by a ``threading.Lock`` so the store is
    safe to access from asyncio ``to_thread`` workers and background
    sweeps.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HostedSessionInfo:
    """Metadata for a single hosted MCP session.

    Attributes:
        session_id: Unique session identifier (from request header).
        client_id: MCP client_id reported during ``initialize``.
        client_info: Full ``clientInfo`` dict from ``initialize``.
        protocol_version: MCP protocol version negotiated at init.
        initialized: Whether the session completed ``initialize``.
        surface_name: UI surface that opened the session
            (e.g. ``"dashboard"`` or ``"premium"``).
        created_at: Monotonic timestamp of session creation.
        last_seen_at: Monotonic timestamp of last activity.
    """

    session_id: str
    client_id: Optional[str] = None
    client_info: Optional[dict[str, object]] = None
    protocol_version: Optional[str] = None
    initialized: bool = False
    surface_name: str = ""
    user_id: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    last_seen_at: float = field(default_factory=time.monotonic)


class HostedSessionStore:
    """Thread-safe in-memory store for hosted MCP sessions.

    Sessions are evicted when they exceed ``ttl`` seconds of inactivity
    (measured by ``last_seen_at``).

    Args:
        ttl: Maximum idle time in seconds before a session is eligible
            for eviction.  Defaults to 3600 (1 hour).
    """

    def __init__(self, ttl: float = 3600.0) -> None:
        self._sessions: dict[str, HostedSessionInfo] = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_or_replace(
        self,
        session_id: str,
        surface_name: str = "",
        **kwargs: object,
    ) -> HostedSessionInfo:
        """Create a new session or replace an existing one.

        Any extra ``kwargs`` are forwarded to the ``HostedSessionInfo``
        constructor (e.g. ``client_id``, ``protocol_version``).

        Args:
            session_id: Unique session identifier.
            surface_name: UI surface label.
            **kwargs: Additional fields for ``HostedSessionInfo``.

        Returns:
            The newly created ``HostedSessionInfo``.
        """
        info = HostedSessionInfo(
            session_id=session_id,
            surface_name=surface_name,
            **kwargs,  # type: ignore[arg-type]
        )
        with self._lock:
            self._sessions[session_id] = info
        return info

    def get(self, session_id: str) -> Optional[HostedSessionInfo]:
        """Look up a session by ID.

        Args:
            session_id: Session identifier.

        Returns:
            The ``HostedSessionInfo`` if found, ``None`` otherwise.
        """
        with self._lock:
            return self._sessions.get(session_id)

    def touch(self, session_id: str) -> None:
        """Update ``last_seen_at`` to the current monotonic time.

        No-op if the session does not exist.

        Args:
            session_id: Session identifier.
        """
        with self._lock:
            info = self._sessions.get(session_id)
            if info is not None:
                info.last_seen_at = time.monotonic()

    def mark_initialized(self, session_id: str) -> None:
        """Set the ``initialized`` flag to ``True``.

        No-op if the session does not exist.

        Args:
            session_id: Session identifier.
        """
        with self._lock:
            info = self._sessions.get(session_id)
            if info is not None:
                info.initialized = True

    def delete(self, session_id: str) -> None:
        """Remove a session from the store.

        No-op if the session does not exist.

        Args:
            session_id: Session identifier.
        """
        with self._lock:
            self._sessions.pop(session_id, None)

    def sweep_expired(self) -> int:
        """Remove sessions that have been idle longer than ``ttl``.

        Returns:
            The number of sessions removed.
        """
        now = time.monotonic()
        with self._lock:
            expired = [
                sid
                for sid, info in self._sessions.items()
                if (now - info.last_seen_at) > self._ttl
            ]
            for sid in expired:
                del self._sessions[sid]
        return len(expired)


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_store: Optional[HostedSessionStore] = None
_store_lock = threading.Lock()


def get_hosted_session_store() -> HostedSessionStore:
    """Return the process-wide ``HostedSessionStore`` singleton.

    The store is created lazily on first call.  TTL is read from the
    ``WATERCOOLER_MCP_SESSION_TTL_SECONDS`` environment variable
    (default ``3600``).

    Returns:
        The singleton ``HostedSessionStore``.
    """
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        ttl = float(os.getenv("WATERCOOLER_MCP_SESSION_TTL_SECONDS", "3600"))
        _store = HostedSessionStore(ttl=ttl)
        return _store
