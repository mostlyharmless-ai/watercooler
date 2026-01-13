"""Cache abstraction for hosted MCP service.

Provides a unified caching interface that supports:
- In-memory caching (default, per-process)
- Database caching (via watercooler-site API)
- TTL-based expiration
- Cache key prefixing for namespacing

The cache is used to reduce:
- GitHub API calls for frequently accessed data
- Graph JSONL parsing overhead
- Database queries in hosted mode

Environment variables:
- WATERCOOLER_CACHE_BACKEND: "memory" (default) or "database"
- WATERCOOLER_CACHE_TTL: Default TTL in seconds (default: 300)
- WATERCOOLER_CACHE_API_URL: Base URL for database cache API

Usage:
    from watercooler_mcp.cache import cache, CacheKey

    # Simple caching
    data = cache.get("thread:my-topic")
    if data is None:
        data = load_thread_data()
        cache.set("thread:my-topic", data, ttl=300)

    # Using CacheKey for structured keys
    key = CacheKey(resource="thread", topic="my-topic", branch="main")
    cache.set(str(key), data)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Cache Key Builder
# =============================================================================


@dataclass
class CacheKey:
    """Structured cache key builder.

    Creates consistent, namespaced cache keys for different resource types.

    Examples:
        CacheKey("thread", topic="auth") -> "thread:auth"
        CacheKey("entry", topic="auth", entry_id="01ABC") -> "entry:auth:01ABC"
        CacheKey("graph", repo="org/repo", branch="main") -> "graph:org/repo:main"
    """

    resource: str
    topic: Optional[str] = None
    entry_id: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    extra: Optional[str] = None

    def __str__(self) -> str:
        """Build cache key string."""
        parts = [self.resource]
        if self.repo:
            parts.append(self.repo)
        if self.branch:
            parts.append(self.branch)
        if self.topic:
            parts.append(self.topic)
        if self.entry_id:
            parts.append(self.entry_id)
        if self.extra:
            parts.append(self.extra)
        return ":".join(parts)


# =============================================================================
# Cache Entry
# =============================================================================


@dataclass
class CacheEntry(Generic[T]):
    """Cache entry with value and metadata."""

    value: T
    created_at: float = field(default_factory=time.time)
    ttl: Optional[float] = None  # seconds

    def is_expired(self) -> bool:
        """Check if entry has expired."""
        if self.ttl is None:
            return False
        return time.time() > (self.created_at + self.ttl)


# =============================================================================
# Cache Backend Interface
# =============================================================================


class CacheBackend(ABC):
    """Abstract cache backend interface."""

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set value in cache.

        Args:
            key: Cache key
            value: Value to cache (must be JSON-serializable for remote backends)
            ttl: Time-to-live in seconds (None = no expiration)
        """
        ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete value from cache.

        Args:
            key: Cache key

        Returns:
            True if key existed, False otherwise
        """
        ...

    @abstractmethod
    def clear(self) -> None:
        """Clear all cached values."""
        ...

    def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all keys matching a pattern.

        Default implementation iterates all keys. Override for efficiency.

        Args:
            pattern: Key prefix pattern (e.g., "thread:my-topic")

        Returns:
            Number of keys invalidated
        """
        # Default: no-op for backends without pattern support
        return 0


# =============================================================================
# In-Memory Cache Backend
# =============================================================================


class MemoryCache(CacheBackend):
    """Thread-safe in-memory cache with TTL support.

    This is the default backend, suitable for:
    - Local MCP (STDIO mode)
    - Single-process deployments
    - Development/testing

    For multi-process deployments (serverless), consider DatabaseCache.
    """

    def __init__(self, default_ttl: Optional[float] = None):
        """Initialize memory cache.

        Args:
            default_ttl: Default TTL for entries without explicit TTL
        """
        self._cache: Dict[str, CacheEntry[Any]] = {}
        self._lock = threading.RLock()
        self._default_ttl = default_ttl

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.is_expired():
                del self._cache[key]
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set value in cache."""
        if ttl is None:
            ttl = self._default_ttl
        entry = CacheEntry(value=value, ttl=ttl)
        with self._lock:
            self._cache[key] = entry

    def delete(self, key: str) -> bool:
        """Delete value from cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> None:
        """Clear all cached values."""
        with self._lock:
            self._cache.clear()

    def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all keys matching a prefix pattern."""
        count = 0
        with self._lock:
            keys_to_delete = [
                k for k in self._cache if k.startswith(pattern)
            ]
            for key in keys_to_delete:
                del self._cache[key]
                count += 1
        return count

    def cleanup_expired(self) -> int:
        """Remove expired entries.

        Call periodically to prevent memory growth from expired entries.

        Returns:
            Number of entries removed
        """
        count = 0
        with self._lock:
            expired = [
                k for k, v in self._cache.items()
                if v.is_expired()
            ]
            for key in expired:
                del self._cache[key]
                count += 1
        return count

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total = len(self._cache)
            expired = sum(1 for v in self._cache.values() if v.is_expired())
            return {
                "backend": "memory",
                "total_entries": total,
                "expired_entries": expired,
                "active_entries": total - expired,
            }


# =============================================================================
# Database Cache Backend (via watercooler-site API)
# =============================================================================


class DatabaseCache(CacheBackend):
    """Remote cache backend using watercooler-site database API.

    This backend is suitable for:
    - Serverless deployments (Vercel, AWS Lambda)
    - Multi-process/multi-instance deployments
    - Persistent caching across restarts

    The cache is stored in the watercooler-site PostgreSQL database,
    typically in the ConnectedRepo.graphNodes field or a dedicated cache table.

    Environment variables:
    - WATERCOOLER_CACHE_API_URL: Base URL for cache API
    - WATERCOOLER_TOKEN_API_KEY: API key for authentication
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        default_ttl: Optional[float] = None,
    ):
        """Initialize database cache.

        Args:
            api_url: Cache API base URL (defaults to env var)
            api_key: API key (defaults to env var)
            default_ttl: Default TTL in seconds
        """
        self._api_url = api_url or os.getenv("WATERCOOLER_CACHE_API_URL", "")
        self._api_key = api_key or os.getenv("WATERCOOLER_TOKEN_API_KEY", "")
        self._default_ttl = default_ttl
        # Local fallback for when API is unavailable
        self._local = MemoryCache(default_ttl)

    def _is_configured(self) -> bool:
        """Check if database cache is properly configured."""
        return bool(self._api_url) and bool(self._api_key)

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Make HTTP request to cache API."""
        if not self._is_configured():
            return None

        url = f"{self._api_url.rstrip('/')}{path}"

        try:
            body = json.dumps(data).encode("utf-8") if data else None
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "x-api-key": self._api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                method=method,
            )

            with urllib.request.urlopen(request, timeout=5.0) as response:
                return json.loads(response.read().decode("utf-8"))

        except Exception as e:
            logger.debug(f"Cache API error: {e}")
            return None

    def get(self, key: str) -> Optional[Any]:
        """Get value from database cache."""
        # Try remote first
        result = self._request("GET", f"/api/cache?key={key}")
        if result and "value" in result:
            return result["value"]
        # Fallback to local
        return self._local.get(key)

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Set value in database cache."""
        if ttl is None:
            ttl = self._default_ttl
        # Always set locally for immediate access
        self._local.set(key, value, ttl)
        # Try remote (fire-and-forget)
        self._request("POST", "/api/cache", {
            "key": key,
            "value": value,
            "ttl": ttl,
        })

    def delete(self, key: str) -> bool:
        """Delete value from database cache."""
        local_deleted = self._local.delete(key)
        self._request("DELETE", f"/api/cache?key={key}")
        return local_deleted

    def clear(self) -> None:
        """Clear local cache (remote clear not supported)."""
        self._local.clear()

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        local_stats = self._local.stats()
        local_stats["backend"] = "database"
        local_stats["configured"] = self._is_configured()
        return local_stats


# =============================================================================
# Global Cache Instance
# =============================================================================


def _create_cache() -> CacheBackend:
    """Create cache backend based on configuration."""
    backend = os.getenv("WATERCOOLER_CACHE_BACKEND", "memory").lower()
    default_ttl = float(os.getenv("WATERCOOLER_CACHE_TTL", "300"))

    if backend == "database":
        logger.info("Using database cache backend")
        return DatabaseCache(default_ttl=default_ttl)
    else:
        logger.debug("Using memory cache backend")
        return MemoryCache(default_ttl=default_ttl)


# Global cache instance
cache: CacheBackend = _create_cache()


def get_cache() -> CacheBackend:
    """Get the global cache instance."""
    return cache


def reset_cache() -> None:
    """Reset the global cache instance (for testing)."""
    global cache
    cache = _create_cache()
