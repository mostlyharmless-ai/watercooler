"""Daemon telemetry — lightweight counters for service calls and tokens.

Tracks per-service call counts, error counts, latency, and token usage
across all daemons. Thread-safe, zero-dependency (stdlib only).

Counters are module-global and accumulate for the process lifetime.
They are surfaced through ``daemon_status`` and ``watercooler_health``.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class _ServiceCounters:
    """Counters for a single service (LLM, embedding, GitHub API, etc.)."""
    calls: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0
    # Token counters (LLM-specific, ignored for other services)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Cache counters (GitHub API / hosted_data)
    cache_hits: int = 0
    cache_misses: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "calls": self.calls,
            "errors": self.errors,
            "avg_latency_ms": round(self.total_latency_ms / self.calls, 1) if self.calls else 0.0,
        }
        total_tokens = self.prompt_tokens + self.completion_tokens
        if total_tokens > 0:
            d["prompt_tokens"] = self.prompt_tokens
            d["completion_tokens"] = self.completion_tokens
            d["total_tokens"] = total_tokens
        if self.cache_hits or self.cache_misses:
            d["cache_hits"] = self.cache_hits
            d["cache_misses"] = self.cache_misses
            total = self.cache_hits + self.cache_misses
            d["cache_hit_rate"] = round(self.cache_hits / total, 3) if total else 0.0
        return d


# ---------------------------------------------------------------------------
# Module-global state
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_services: Dict[str, _ServiceCounters] = {}

# Well-known service names (use these for consistency)
SVC_LLM = "llm"
SVC_EMBEDDING = "embedding"
SVC_GITHUB_API = "github_api"
SVC_FALKORDB = "falkordb"
SVC_GIT_FETCH = "git_fetch"


def _get(service: str) -> _ServiceCounters:
    """Get or create counters for a service. Caller must hold _lock."""
    if service not in _services:
        _services[service] = _ServiceCounters()
    return _services[service]


# ---------------------------------------------------------------------------
# Recording API
# ---------------------------------------------------------------------------

def record_call(
    service: str,
    *,
    latency_ms: float = 0.0,
    error: bool = False,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """Record a service call with optional latency and token counts."""
    with _lock:
        c = _get(service)
        c.calls += 1
        c.total_latency_ms += latency_ms
        if error:
            c.errors += 1
        c.prompt_tokens += prompt_tokens
        c.completion_tokens += completion_tokens


def record_cache(service: str, *, hit: bool) -> None:
    """Record a cache hit or miss for a service."""
    with _lock:
        c = _get(service)
        if hit:
            c.cache_hits += 1
        else:
            c.cache_misses += 1


class track_call:
    """Context manager that times a service call and records telemetry.

    Usage::

        with track_call("llm") as t:
            result = client.post(...)
            t.prompt_tokens = usage.get("prompt_tokens", 0)
            t.completion_tokens = usage.get("completion_tokens", 0)

    On exit, records call count, latency, error status, and any tokens set.
    """

    def __init__(self, service: str) -> None:
        self.service = service
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self._error: bool = False
        self._start: float = 0.0

    def __enter__(self) -> "track_call":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        latency_ms = (time.perf_counter() - self._start) * 1000.0
        record_call(
            self.service,
            latency_ms=latency_ms,
            error=exc_type is not None or self._error,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )
        return None  # don't suppress exceptions

    def mark_error(self) -> None:
        """Mark this call as errored (for non-exception failures)."""
        self._error = True


# ---------------------------------------------------------------------------
# Reporting API
# ---------------------------------------------------------------------------

def get_telemetry() -> Dict[str, Any]:
    """Return a snapshot of all service telemetry as a dict.

    Suitable for inclusion in daemon_status or health responses.
    """
    with _lock:
        return {
            name: counters.to_dict()
            for name, counters in sorted(_services.items())
        }


def reset() -> None:
    """Reset all counters. Primarily for testing."""
    with _lock:
        _services.clear()
