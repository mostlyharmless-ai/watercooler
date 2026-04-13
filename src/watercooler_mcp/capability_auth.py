"""Capability grants and hosted authorization.

Provides hosted-surface authorization: before executing a capability-backed
tool, the wrapper calls ``CapabilityAuthorizer.ensure()`` to verify that the
user's plan grants the required capability.

Local surfaces do not use hosted authorization.
"""

from __future__ import annotations

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

        Returns:
            ``None`` on success, or a JSON error string on denial.
        """
        if not user_id:
            return json.dumps({
                "error": "capability_not_enabled",
                "capability": capability,
                "message": "No user identity available for capability check.",
            })

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

        return json.dumps({
            "error": "capability_not_enabled",
            "capability": capability,
            "message": f"Capability '{capability}' is not enabled for this user.",
        })
