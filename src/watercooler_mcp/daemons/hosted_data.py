"""Hosted data access layer for daemons.

Routes daemon data reads to GitHub API (hosted mode) or local baseline graph
(local mode). Daemons import from this module instead of directly accessing
watercooler.baseline_graph when they need mode-aware data access.

All reads go through a short-lived TTL cache so that multiple daemons ticking
in the same window share a single GitHub API roundtrip per resource.  The
cache is thread-safe (module-level lock) and bounded by wall-clock TTL.

.. deprecated::
    The daemon-facing functions in this module (``list_topics_for_daemon``,
    ``get_thread_meta_for_daemon``, ``get_entries_for_daemon``,
    ``get_annotations_for_daemon``, ``get_thread_change_marker``) are
    superseded by the Railway worktree approach (``hosted_worktree.py``).
    When a worktree is available, daemons read from the local filesystem
    via ``baseline_graph.reader`` — identical to local mode.  These
    functions remain as the fallback path when worktree clone fails, and
    for non-daemon hosted operations (e.g., tool calls via GitHub API).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTL cache for GitHub API responses
# ---------------------------------------------------------------------------
# TTL for GitHub API response cache.  Increased from 120s to 900s as a
# stopgap to reduce API rate limit pressure.  With the Railway worktree
# (hosted_worktree.py) this cache is only used as a fallback when the
# worktree clone fails.
_CACHE_TTL = 900.0
_cache_lock = threading.Lock()

# Cache entries: keyed by scope_id to prevent cross-tenant contamination.
# scope_id → (timestamp, value) for topics
# (scope_id, topic) → (timestamp, value) for per-topic caches
_topics_cache: dict[str, tuple[float, list[str]]] = {}
_meta_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_entries_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_annotations_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _is_fresh(entry: Optional[tuple[float, Any]], ttl: float = _CACHE_TTL) -> bool:
    """Check if a cache entry exists and is within TTL."""
    if entry is None:
        return False
    return (time.monotonic() - entry[0]) < ttl


def _current_scope_id() -> str:
    """Get the current scope_id for cache keying.

    In hosted mode, raises RuntimeError if scope cannot be determined
    (prevents cross-tenant cache pollution). In local mode, returns "".
    """
    try:
        from watercooler_mcp.context import get_effective_context
        ctx = get_effective_context()
        if ctx and ctx.user_id and ctx.repo:
            return f"{ctx.user_id}:{ctx.repo}"
    except Exception:
        pass
    # In hosted mode, failing to resolve scope is a hard error —
    # caching under "" would leak data across tenants.
    if is_daemon_hosted_mode():
        raise RuntimeError(
            "hosted_data: cannot determine scope_id for cache keying"
        )
    return ""


def invalidate_cache() -> None:
    """Clear all cached data.  Called after writes or on demand."""
    with _cache_lock:
        _topics_cache.clear()
        _meta_cache.clear()
        _entries_cache.clear()
        _annotations_cache.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_daemon_hosted_mode() -> bool:
    """Check if daemons should use hosted data access (GitHub API).

    Uses auth.is_hosted_mode() which checks WATERCOOLER_MODE=hosted
    and token service configuration.
    """
    try:
        from watercooler_mcp.auth import is_hosted_mode
        return is_hosted_mode()
    except Exception:
        return False


def list_topics_for_daemon() -> list[str]:
    """List all thread topics — routes to GitHub API or local graph.

    .. deprecated::
        Superseded by Railway worktree (``hosted_worktree.py``).  Retained
        as fallback when worktree clone fails.

    Results are cached for up to _CACHE_TTL seconds so multiple daemons
    ticking in the same window share one API call.

    Returns:
        List of thread topic strings. Empty list on error.
    """
    if not is_daemon_hosted_mode():
        raise RuntimeError("list_topics_for_daemon() called outside hosted mode")

    scope = _current_scope_id()
    from .telemetry import record_cache, track_call, SVC_GITHUB_API

    with _cache_lock:
        cached = _topics_cache.get(scope)
        if _is_fresh(cached):
            record_cache(SVC_GITHUB_API, hit=True)
            return cached[1]  # type: ignore[index]

    record_cache(SVC_GITHUB_API, hit=False)
    try:
        from watercooler_mcp.hosted_ops import list_threads_hosted
        with track_call(SVC_GITHUB_API) as t:
            err, threads = list_threads_hosted()
            if err:
                t.mark_error()
                logger.warning("DAEMON[hosted_data]: list_threads_hosted failed: %s", err)
                return []
        topics = [t.topic for t in threads]
        with _cache_lock:
            _topics_cache[scope] = (time.monotonic(), topics)
        return topics
    except Exception as exc:
        logger.warning("DAEMON[hosted_data]: list_topics error: %s", exc)
        return []


def get_thread_meta_for_daemon(topic: str) -> dict[str, Any] | None:
    """Get thread metadata — routes to GitHub API in hosted mode.

    .. deprecated:: Superseded by Railway worktree.

    Results are cached for up to _CACHE_TTL seconds.

    Returns dict with keys like: topic, title, status, ball, last_updated, entry_count, summary.
    Returns None on error.
    """
    if not is_daemon_hosted_mode():
        raise RuntimeError("get_thread_meta_for_daemon() called outside hosted mode")

    scope = _current_scope_id()
    cache_key = (scope, topic)
    from .telemetry import record_cache, track_call, SVC_GITHUB_API

    with _cache_lock:
        cached = _meta_cache.get(cache_key)
        if _is_fresh(cached):
            record_cache(SVC_GITHUB_API, hit=True)
            return cached[1]  # type: ignore[index]

    record_cache(SVC_GITHUB_API, hit=False)
    try:
        from watercooler_mcp.hosted_ops import load_thread_metadata_hosted
        with track_call(SVC_GITHUB_API) as t:
            err, meta = load_thread_metadata_hosted(topic)
            if err:
                t.mark_error()
                logger.debug("DAEMON[hosted_data]: load_thread_metadata_hosted(%s) failed: %s", topic, err)
                return None
        with _cache_lock:
            _meta_cache[cache_key] = (time.monotonic(), meta)
        return meta
    except Exception as exc:
        logger.debug("DAEMON[hosted_data]: get_thread_meta error for %s: %s", topic, exc)
        return None


def get_entries_for_daemon(topic: str) -> list[dict[str, Any]]:
    """Get entries for a thread — routes to GitHub API in hosted mode.

    .. deprecated:: Superseded by Railway worktree.

    Results are cached for up to _CACHE_TTL seconds.

    Returns list of entry dicts. Empty list on error.
    """
    if not is_daemon_hosted_mode():
        raise RuntimeError("get_entries_for_daemon() called outside hosted mode")

    scope = _current_scope_id()
    cache_key = (scope, topic)
    from .telemetry import record_cache, track_call, SVC_GITHUB_API

    with _cache_lock:
        cached = _entries_cache.get(cache_key)
        if _is_fresh(cached):
            record_cache(SVC_GITHUB_API, hit=True)
            return cached[1]  # type: ignore[index]

    record_cache(SVC_GITHUB_API, hit=False)
    try:
        from watercooler_mcp.hosted_ops import load_entries_hosted
        with track_call(SVC_GITHUB_API) as t:
            err, entries = load_entries_hosted(topic)
            if err:
                t.mark_error()
                logger.debug("DAEMON[hosted_data]: load_entries_hosted(%s) failed: %s", topic, err)
                return []
        with _cache_lock:
            _entries_cache[cache_key] = (time.monotonic(), entries)
        return entries
    except Exception as exc:
        logger.debug("DAEMON[hosted_data]: get_entries error for %s: %s", topic, exc)
        return []


def get_thread_change_marker(topic: str) -> tuple[float, int]:
    """Get (mtime_proxy, entry_count) for incremental detection in hosted mode.

    In hosted mode, uses last_updated timestamp from meta as mtime proxy,
    and entry_count from meta.

    Returns:
        Tuple of (mtime_proxy, entry_count). (0.0, 0) on error.
    """
    meta = get_thread_meta_for_daemon(topic)
    if meta is None:
        return (0.0, 0)

    # Parse last_updated ISO timestamp to epoch
    mtime = 0.0
    last_updated = meta.get("last_updated", "")
    if last_updated:
        try:
            from datetime import datetime
            ts_str = last_updated.replace("Z", "+00:00")
            mtime = datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            pass

    entry_count = meta.get("entry_count", 0)
    if not isinstance(entry_count, int):
        try:
            entry_count = int(entry_count)
        except (ValueError, TypeError):
            entry_count = 0

    return (mtime, entry_count)


def get_annotations_for_daemon(topic: str) -> dict[str, Any]:
    """Get annotation states for a thread in hosted mode.

    .. deprecated:: Superseded by Railway worktree.

    Results are cached for up to _CACHE_TTL seconds.

    Returns dict of annotation states keyed by target_id (topic or entry_id).
    Empty dict on error.
    """
    if not is_daemon_hosted_mode():
        raise RuntimeError("get_annotations_for_daemon() called outside hosted mode")

    scope = _current_scope_id()
    cache_key = (scope, topic)
    from .telemetry import record_cache, track_call, SVC_GITHUB_API

    with _cache_lock:
        cached = _annotations_cache.get(cache_key)
        if _is_fresh(cached):
            record_cache(SVC_GITHUB_API, hit=True)
            return cached[1]  # type: ignore[index]

    record_cache(SVC_GITHUB_API, hit=False)
    try:
        from watercooler_mcp.hosted_ops import get_annotations_hosted
        with track_call(SVC_GITHUB_API) as t:
            err, annotations = get_annotations_hosted(topic)
            if err:
                t.mark_error()
                logger.debug("DAEMON[hosted_data]: get_annotations_hosted(%s) failed: %s", topic, err)
                return {}
        result = annotations if isinstance(annotations, dict) else {}
        with _cache_lock:
            _annotations_cache[cache_key] = (time.monotonic(), result)
        return result
    except Exception as exc:
        logger.debug("DAEMON[hosted_data]: get_annotations error for %s: %s", topic, exc)
        return {}
