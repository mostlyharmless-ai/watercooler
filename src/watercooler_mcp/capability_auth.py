"""Capability grants and hosted authorization.

Provides hosted-surface authorization: before executing a capability-backed
tool, the wrapper calls ``CapabilityAuthorizer.ensure()`` to verify that the
user's plan grants the required capability.

Local surfaces do not use hosted authorization.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from .request_trace import trace_stage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Grant cache
# ---------------------------------------------------------------------------

@dataclass
class _CachedGrant:
    capabilities: set[str]
    fetched_at: float


# ---------------------------------------------------------------------------
# CapabilityGrantService
# ---------------------------------------------------------------------------


class CapabilityGrantService:
    """Fetch and cache capability grants from the watercooler-site API.

    Uses the same service credential model from ``auth.py``
    (``WATERCOOLER_TOKEN_API_URL``, ``WATERCOOLER_TOKEN_API_KEY``).
    """

    def __init__(
        self,
        api_url: str = "",
        api_key: str = "",
        vercel_bypass_secret: str = "",
        cache_ttl: float = 300.0,
    ) -> None:
        self._api_url = api_url.rstrip("/") if api_url else ""
        self._api_key = api_key
        self._vercel_bypass_secret = vercel_bypass_secret
        self._cache_ttl = cache_ttl
        # Maximum age for stale-while-revalidate: after this window
        # a stale entry is discarded and fail-closed returns empty set.
        self._stale_max = cache_ttl * 3  # 15 min default
        self._cache: dict[str, _CachedGrant] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> CapabilityGrantService:
        """Create a service from environment / auth config."""
        from .auth import get_auth_config
        config = get_auth_config()
        return cls(
            api_url=config["token_api_url"],
            api_key=config["token_api_key"],
            vercel_bypass_secret=config.get("vercel_bypass_secret", ""),
        )

    def get_capabilities(self, user_id: str) -> set[str]:
        """Return the set of enabled capability ids for *user_id*.

        Results are cached for ``cache_ttl`` seconds.  On fetch failure
        a stale cache entry is returned if available; otherwise an empty
        set is returned (fail-closed).

        This method performs a synchronous HTTP fetch on cache miss.
        Async callers (e.g. the MCP middleware ``on_call_tool``) MUST
        use :meth:`get_capabilities_async` to avoid blocking the event
        loop on cache miss.
        """
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(user_id)
            if cached and (now - cached.fetched_at) < self._cache_ttl:
                return cached.capabilities

        try:
            caps = self._fetch_capabilities(user_id)
            with self._lock:
                self._cache[user_id] = _CachedGrant(capabilities=caps, fetched_at=now)
            return caps
        except Exception as exc:
            logger.warning("Capability grant fetch failed for %s: %s", user_id, exc)
            # Stale-while-revalidate: return stale cache if within the
            # maximum staleness window; otherwise fail-closed.
            if cached and (now - cached.fetched_at) < self._stale_max:
                return cached.capabilities
            return set()

    async def get_capabilities_async(self, user_id: str) -> set[str]:
        """Async-safe variant of :meth:`get_capabilities`.

        Identical caching, stale-while-revalidate, and fail-closed
        semantics as the sync version, but the blocking ``urlopen``
        call on cache miss runs in a worker thread via
        :func:`asyncio.to_thread` so the event loop stays responsive.

        Args:
            user_id: The user identifier whose capabilities to resolve.

        Returns:
            The set of enabled capability ids.  Empty set on fetch
            failure with no usable stale cache (fail-closed).
        """
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(user_id)
            if cached and (now - cached.fetched_at) < self._cache_ttl:
                return cached.capabilities

        # Capture dispatch time so the success path can detect a
        # concurrent fresher write that landed *after* this call
        # dispatched its fetch (PR #744 round 5 MED).
        dispatch_time = time.monotonic()

        try:
            caps = await asyncio.to_thread(self._fetch_capabilities, user_id)
            # PR #744 review (MED): capture ``fetched_at`` AFTER the
            # network round-trip completes. Using the pre-dispatch
            # ``now`` would shave the actual fetch duration off the
            # cache TTL, causing avoidable over-fetching for slow
            # responders. (The sync ``get_capabilities`` has the same
            # pre-existing bug; track separately.)
            fetched_at = time.monotonic()

            # PR #744 round 5 (MED): success-path concurrent-write
            # guard. Two coroutines can both miss cache and dispatch
            # fetches; if A dispatches at T=0 and completes at T=8,
            # while B dispatches at T=5.1 (after a server-side
            # revocation) and completes at T=7, B's correct revoked
            # entry would be silently overwritten by A's stale-grants
            # entry simply because A's ``fetched_at`` is larger. Only
            # write if no entry has appeared, the entry is the same
            # snapshot we started with, OR our dispatch was no later
            # than the concurrent entry's fetch — i.e., we don't
            # downgrade a fresher concurrent revoke.
            with self._lock:
                current = self._cache.get(user_id)
                if (
                    current is None
                    or current is cached
                    or current.fetched_at <= dispatch_time
                ):
                    self._cache[user_id] = _CachedGrant(
                        capabilities=caps, fetched_at=fetched_at
                    )
                    return caps
                # Concurrent caller dispatched AFTER us and already
                # wrote a fresher entry — honour theirs.
                return current.capabilities
        except Exception as exc:
            logger.warning(
                "Capability grant fetch failed for %s: %s", user_id, exc
            )
            # PR #744 round 3 (LOW): re-read the clock here. The
            # ``now`` captured pre-dispatch is up to ``urlopen``-timeout
            # seconds in the past, so using it for the stale check would
            # effectively widen the stale window by the fetch duration —
            # serving entries that have aged past ``stale_max`` while
            # the failed fetch was in flight.
            now_after_failure = time.monotonic()

            # PR #744 round 3 (MED): re-read the cache under lock before
            # falling back to ``cached``. A concurrent successful fetch
            # may have written a fresh (possibly revoked) entry while
            # this call's fetch was in flight — preferring the stale
            # pre-dispatch snapshot would re-grant a capability that has
            # already been revoked by the concurrent path. ``cache_ttl``
            # is the freshness bar for the concurrent-fresh case;
            # ``stale_max`` is the bar for the original snapshot when no
            # fresher entry exists.
            with self._lock:
                current = self._cache.get(user_id)
            if (
                current is not None
                and current is not cached
                and (now_after_failure - current.fetched_at) < self._cache_ttl
            ):
                return current.capabilities

            if cached and (now_after_failure - cached.fetched_at) < self._stale_max:
                return cached.capabilities
            return set()

    def _fetch_capabilities(self, user_id: str) -> set[str]:
        """Fetch capabilities from ``GET /api/mcp/capabilities?userId=...``."""
        if not self._api_url:
            return set()

        url = f"{self._api_url}/api/mcp/capabilities?userId={urllib.parse.quote(user_id)}"
        req = urllib.request.Request(url, method="GET")
        if self._api_key:
            req.add_header("x-api-key", self._api_key)
        if self._vercel_bypass_secret:
            req.add_header("x-vercel-protection-bypass", self._vercel_bypass_secret)
        req.add_header("Accept", "application/json")

        with trace_stage("auth.capability_fetch", cache_hit=False):
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        return set(data.get("capabilities", []))


# ---------------------------------------------------------------------------
# CapabilityAuthorizer
# ---------------------------------------------------------------------------


class CapabilityAuthorizer:
    """Gate capability execution on hosted surfaces.

    Before executing a capability-backed implementation, the wrapper
    calls ``ensure(capability, user_id)``.  Returns ``None`` on success,
    or a JSON error string on denial.
    """

    def __init__(self, grant_service: CapabilityGrantService) -> None:
        self._grant_service = grant_service

    def ensure(
        self,
        capability: str,
        user_id: str,
        preloaded_capabilities: Optional[frozenset[str]] = None,
    ) -> Optional[str]:
        """Check that *user_id* is allowed to execute *capability*.

        If *preloaded_capabilities* is provided (from the credentials
        response), it is used directly without a network round-trip.
        Otherwise falls back to ``CapabilityGrantService``.

        This method runs the cache-miss fallback synchronously.  Async
        callers (notably the MCP capability middleware) MUST use
        :meth:`ensure_async` so the blocking grant fetch runs in a
        worker thread instead of pinning the event loop.

        Returns:
            ``None`` on success, or a JSON error string on denial.
        """
        if not user_id:
            return self._no_user_denial(capability)

        # Use preloaded capabilities from credentials response first
        # (eliminates the second control-plane round-trip).
        if preloaded_capabilities is not None:
            if capability in preloaded_capabilities:
                return None
        else:
            # Fallback: fetch from capability grant service
            with trace_stage("auth.capability_check", source="fallback"):
                grants = self._grant_service.get_capabilities(user_id)
            if capability in grants:
                return None

        return self._not_enabled_denial(capability)

    async def ensure_async(
        self,
        capability: str,
        user_id: str,
        preloaded_capabilities: Optional[frozenset[str]] = None,
    ) -> Optional[str]:
        """Async-safe variant of :meth:`ensure`.

        Identical semantics to :meth:`ensure` but the cache-miss
        fallback dispatches the blocking ``urlopen`` to a worker thread
        via :meth:`CapabilityGrantService.get_capabilities_async`.  Use
        this from any ``async def`` callsite (e.g. the FastMCP
        capability middleware) so the event loop is not blocked on the
        first request per user per ``cache_ttl`` window.

        Args:
            capability: Capability id required by the tool.
            user_id: Identity of the requesting user.
            preloaded_capabilities: Optional set of capabilities sourced
                from the credentials response (Bearer token path).

        Returns:
            ``None`` on success, or a JSON error string on denial.
        """
        if not user_id:
            return self._no_user_denial(capability)

        if preloaded_capabilities is not None:
            if capability in preloaded_capabilities:
                return None
        else:
            with trace_stage("auth.capability_check", source="fallback"):
                grants = await self._fetch_grants_async(user_id)
            if capability in grants:
                return None

        return self._not_enabled_denial(capability)

    async def _fetch_grants_async(self, user_id: str) -> set[str]:
        """Resolve grants without blocking the event loop.

        Prefers the grant service's async variant when available; if a
        test double only implements the sync ``get_capabilities`` or
        returns something non-awaitable from the async hook, fall back
        to dispatching the sync call to a worker thread so the loop
        still stays responsive.

        PR #744 review (LOW): the previous fallback returned the
        ``async_fetch(user_id)`` result directly when not awaitable.
        For an unspecced ``MagicMock`` that returns a plain ``Mock``,
        ``Mock.__contains__`` is truthy, which silently grants every
        capability in the downstream ``if capability in grants`` check.
        Now: a non-awaitable response is treated as a stub mismatch
        and we route through the sync path instead.
        """
        async_fetch = getattr(
            self._grant_service, "get_capabilities_async", None
        )
        if async_fetch is not None:
            try:
                result = async_fetch(user_id)
            except Exception:
                # PR #744 round 5 (LOW): a real ``async def`` always
                # returns a coroutine when called — it doesn't raise
                # synchronously. But a test double (or a mis-shaped
                # implementation) might raise on the call itself
                # rather than from inside the coroutine. Treat that
                # as a stub mismatch and fall through to the sync path
                # rather than letting the exception bubble out of
                # ``ensure_async`` into the middleware.
                pass
            else:
                if inspect.isawaitable(result):
                    return await result
                # Non-awaitable response — likely an unspecced test
                # double. Don't trust the value; fall through to the
                # sync path so a truthy ``Mock.__contains__`` cannot
                # bypass capability gating.
        return await asyncio.to_thread(
            self._grant_service.get_capabilities, user_id
        )

    @staticmethod
    def _no_user_denial(capability: str) -> str:
        return json.dumps({
            "error": "capability_not_enabled",
            "capability": capability,
            "message": "No user identity available for capability check.",
        })

    @staticmethod
    def _not_enabled_denial(capability: str) -> str:
        return json.dumps({
            "error": "capability_not_enabled",
            "capability": capability,
            "message": f"Capability '{capability}' is not enabled for this user.",
        })
