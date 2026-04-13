"""Authentication module for hosted MCP service.

Provides token resolution for GitHub API calls when running as a hosted service.
Tokens are fetched from the watercooler-site token service API, enabling the
MCP server to authenticate with GitHub on behalf of users.

Environment variables:
- WATERCOOLER_TOKEN_API_URL: Base URL of the token service (e.g., https://watercoolerdev.com)
- WATERCOOLER_TOKEN_API_KEY: API key for authenticating with the token service
- WATERCOOLER_MODE: "local" (default) or "hosted" (preferred over WATERCOOLER_AUTH_MODE)
- WATERCOOLER_AUTH_MODE: Legacy alias for WATERCOOLER_MODE
- VERCEL_AUTOMATION_BYPASS_SECRET: Optional secret to bypass Vercel preview auth
  (required when token service is on a Vercel preview deployment with auth enabled)

Token Resolution Flow (hosted mode):
    1. Request arrives with user context (user_id or session)
    2. MCP calls token service: GET /api/github/token?userId={user_id}
    3. Token service decrypts and returns GitHub OAuth token
    4. MCP uses token for GitHub API calls

Usage:
    from watercooler_mcp.auth import get_github_token, is_hosted_mode

    if is_hosted_mode():
        token = get_github_token(user_id="user_123")
        if token:
            # Use token for GitHub API calls
            ...
    else:
        # Use local git credentials (ssh key, credential helper, etc.)
        ...
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable, List, Tuple

from .request_trace import trace_stage

logger = logging.getLogger(__name__)

# Token cache TTL in seconds (default 1 hour, configurable via env)
# TTL for cached tokens - 5 minutes default to balance API calls vs security
# Shorter TTL ensures token revocations propagate reasonably fast
TOKEN_CACHE_TTL = int(os.getenv("WATERCOOLER_TOKEN_CACHE_TTL", "300"))

# Stale-while-revalidate: extend cache by this factor when token service fails
# e.g., 6x means a 300s TTL extends to 1800s (30 min) during outages
STALE_EXTENSION_FACTOR = int(os.getenv("WATERCOOLER_STALE_EXTENSION_FACTOR", "6"))


@dataclass
class CachedToken:
    """Token with cache metadata for TTL-based eviction."""
    token_info: "GitHubTokenInfo"
    cached_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        """Check if the cached token has expired (primary TTL)."""
        return (time.time() - self.cached_at) > TOKEN_CACHE_TTL

    def is_stale_expired(self) -> bool:
        """Check if the stale-while-revalidate window has also expired."""
        return (time.time() - self.cached_at) > (TOKEN_CACHE_TTL * STALE_EXTENSION_FACTOR)


# Cache for GitHub tokens (cache_key -> CachedToken)
# Tokens are cached with TTL for automatic expiration
_github_token_cache: Dict[str, CachedToken] = {}


# =============================================================================
# Circuit Breaker for Token Service
# =============================================================================

# Circuit breaker states
_CB_CLOSED = "closed"      # Normal operation
_CB_OPEN = "open"          # Failing, reject requests
_CB_HALF_OPEN = "half_open"  # Testing if service recovered

# Circuit breaker thresholds (configurable via env)
_CB_FAILURE_THRESHOLD = int(os.getenv("WATERCOOLER_CB_FAILURE_THRESHOLD", "5"))
_CB_RECOVERY_TIMEOUT = float(os.getenv("WATERCOOLER_CB_RECOVERY_TIMEOUT", "60.0"))


@dataclass
class _CircuitBreakerState:
    """Thread-safe circuit breaker for the token service.

    All state mutations are protected by a threading.Lock so this is safe
    to call from asyncio.to_thread worker threads.
    """

    state: str = _CB_CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    _half_open_probe_active: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def record_success(self) -> None:
        """Record a successful call — reset to closed."""
        with self._lock:
            self.state = _CB_CLOSED
            self.failure_count = 0
            self.last_success_time = time.time()
            self._half_open_probe_active = False

    def record_failure(self) -> None:
        """Record a failed call — maybe trip to open."""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            self._half_open_probe_active = False
            if self.state == _CB_HALF_OPEN or self.failure_count >= _CB_FAILURE_THRESHOLD:
                self.state = _CB_OPEN
                logger.warning(
                    f"Token service circuit breaker OPEN after {self.failure_count} failures"
                )

    def should_allow_request(self) -> bool:
        """Check if the circuit breaker allows a request through.

        Thread-safe: the lock protects the check-then-set on
        _half_open_probe_active and the OPEN→HALF_OPEN transition.
        """
        with self._lock:
            if self.state == _CB_CLOSED:
                return True
            if self.state == _CB_OPEN:
                if (time.time() - self.last_failure_time) >= _CB_RECOVERY_TIMEOUT:
                    if self._half_open_probe_active:
                        return False
                    self._half_open_probe_active = True
                    self.state = _CB_HALF_OPEN
                    self.failure_count = 0
                    logger.info("Token service circuit breaker HALF-OPEN, testing recovery")
                    return True
            return False


_circuit_breaker = _CircuitBreakerState()


@dataclass
class GitHubTokenInfo:
    """GitHub token with metadata."""

    token: str
    user_id: str
    github_username: Optional[str] = None
    scopes: Optional[str] = None
    expires_at: Optional[str] = None
    capabilities: Optional[List[str]] = None  # Bundled from credentials response


# =============================================================================
# Shared Token Service Call
# =============================================================================


def _call_token_service(
    *,
    cache_key: str,
    url: str,
    headers: Dict[str, str],
    parse_response: Callable[[Dict[str, Any]], Optional[GitHubTokenInfo]],
    use_cache: bool = True,
    label: str = "token service",
) -> Optional[GitHubTokenInfo]:
    """Shared implementation for all token service calls.

    Handles the full lifecycle: cache → circuit breaker → HTTP → parse →
    cache write → error handling → stale fallback. Both get_github_token
    and resolve_api_key delegate here so the cache/CB/error behavior is
    identical by construction.

    Args:
        cache_key: Key into _github_token_cache.
        url: Full URL to call.
        headers: HTTP headers for the request.
        parse_response: Function that extracts a GitHubTokenInfo from the
            JSON response body, or returns None if the response is valid
            but contains no token (e.g., user not found).
        use_cache: Whether to check the cache before calling.
        label: Human-readable label for log messages.

    Returns:
        GitHubTokenInfo if found, None otherwise.
    """
    # 1. Cache check — always look up for stale fallback, but only
    #    return fresh hits when use_cache is True
    cached = _github_token_cache.get(cache_key)
    if use_cache and cached and not cached.is_expired():
        logger.debug(f"Cache hit for {label} (key={cache_key[:16]}...)")
        return cached.token_info

    # 2. Early exit if service not configured (before CB, to avoid
    #    consuming the half-open probe slot without resolving it).
    #    Do NOT serve stale cache here — if an admin removed the URL to
    #    disable hosted auth, cached tokens should not extend access.
    if not url:
        logger.debug(f"{label}: token service not configured")
        return None

    # 3. Circuit breaker — serve stale or reject
    if not _circuit_breaker.should_allow_request():
        if cached and not cached.is_stale_expired():
            logger.info(f"Circuit breaker open, serving stale cache for {label}")
            return cached.token_info
        logger.warning(f"Circuit breaker open, no stale cache for {label}")
        return None

    # 4. HTTP call
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=10.0) as response:
            data = json.loads(response.read().decode("utf-8"))

        # 5. Parse response
        token_info = parse_response(data)
        if token_info:
            _github_token_cache[cache_key] = CachedToken(token_info=token_info)
            _circuit_breaker.record_success()
            return token_info

        # Service responded but no token — service is reachable
        logger.warning(f"{label}: response contained no token")
        _circuit_breaker.record_success()
        return None

    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            # Service is reachable — application-level rejection
            logger.info(f"{label}: rejected (HTTP {e.code})")
            _circuit_breaker.record_success()
            return None
        body = e.read().decode("utf-8") if e.fp else ""
        logger.error(f"{label}: HTTP error {e.code}: {body}")
        _circuit_breaker.record_failure()

    except urllib.error.URLError as e:
        logger.error(f"{label}: connection error: {e.reason}")
        _circuit_breaker.record_failure()

    except json.JSONDecodeError as e:
        logger.error(f"{label}: invalid JSON: {e}")
        _circuit_breaker.record_failure()

    except Exception as e:
        logger.error(f"{label}: unexpected error: {e}")
        _circuit_breaker.record_failure()

    # 6. Service failure — serve stale cache if available
    if cached and not cached.is_stale_expired():
        logger.info(
            f"{label}: serving stale cache "
            f"(age: {time.time() - cached.cached_at:.0f}s)"
        )
        return cached.token_info

    return None


# =============================================================================
# Configuration
# =============================================================================


def get_auth_config() -> Dict[str, str]:
    """Get authentication configuration from environment.

    Returns:
        Dict with auth_mode, token_api_url, token_api_key, and vercel_bypass_secret
    """
    # Resolve mode from unified config facade (WATERCOOLER_MODE env > TOML > default)
    # Fall back to legacy WATERCOOLER_AUTH_MODE for backwards compatibility
    try:
        from watercooler.config_facade import config as wc_config
        mode = wc_config.get_mode()
    except Exception:
        mode = os.getenv("WATERCOOLER_MODE", "local")

    # Legacy alias: WATERCOOLER_AUTH_MODE is checked only when
    # WATERCOOLER_MODE is absent from the environment. If WATERCOOLER_MODE
    # is explicitly set (even to "local"), the legacy alias is ignored —
    # otherwise it could silently override an explicit mode choice.
    if mode == "local" and not os.getenv("WATERCOOLER_MODE") and os.getenv("WATERCOOLER_AUTH_MODE"):
        legacy = os.getenv("WATERCOOLER_AUTH_MODE", "").lower().strip()
        if legacy in ("local", "hosted"):
            mode = legacy
        else:
            logger.warning(
                f"Invalid WATERCOOLER_AUTH_MODE={legacy!r}, "
                f"expected 'local' or 'hosted'. Ignoring."
            )

    return {
        "auth_mode": mode,
        "token_api_url": os.getenv("WATERCOOLER_TOKEN_API_URL", ""),
        "token_api_key": os.getenv("WATERCOOLER_TOKEN_API_KEY", ""),
        "vercel_bypass_secret": os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET", ""),
    }


def _get_service_config() -> Tuple[str, str, str]:
    """Get token service URL, API key, and Vercel bypass secret.

    Returns:
        (api_url, api_key, vercel_bypass_secret) — any may be empty.
    """
    config = get_auth_config()
    return config["token_api_url"], config["token_api_key"], config["vercel_bypass_secret"]


def _build_service_headers(api_key: str, vercel_bypass_secret: str) -> Dict[str, str]:
    """Build common headers for token service requests."""
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
    }
    if vercel_bypass_secret:
        headers["x-vercel-protection-bypass"] = vercel_bypass_secret
    return headers


def is_hosted_mode() -> bool:
    """Check if running in hosted mode (using token service).

    Returns:
        True if mode is "hosted" and token service is configured
    """
    config = get_auth_config()
    return (
        config["auth_mode"] == "hosted"
        and bool(config["token_api_url"])
        and bool(config["token_api_key"])
    )


def is_token_service_configured() -> bool:
    """Check if the token service is configured.

    Returns:
        True if both API URL and key are set
    """
    config = get_auth_config()
    return bool(config["token_api_url"]) and bool(config["token_api_key"])


# =============================================================================
# Public Token Resolution Functions
# =============================================================================


def get_github_token(
    user_id: str,
    use_cache: bool = True,
) -> Optional[GitHubTokenInfo]:
    """Fetch GitHub token for a user from the token service.

    Delegates to _call_token_service for cache, circuit breaker, and
    stale-while-revalidate handling.

    Args:
        user_id: User identifier (from session or request context)
        use_cache: Whether to use cached tokens (default: True)

    Returns:
        GitHubTokenInfo if found, None otherwise
    """
    if not user_id:
        logger.warning("get_github_token called with empty user_id")
        return None

    api_url, api_key, vercel_bypass = _get_service_config()

    url = (
        f"{api_url.rstrip('/')}/api/github/token?{urllib.parse.urlencode({'userId': user_id})}"
        if api_url and api_key else ""
    )
    headers = _build_service_headers(api_key, vercel_bypass) if api_url and api_key else {}

    def _parse(data: Dict[str, Any]) -> Optional[GitHubTokenInfo]:
        token = data.get("token")
        if not token:
            return None
        info = GitHubTokenInfo(
            token=token,
            user_id=user_id,
            github_username=data.get("githubUsername"),
            scopes=data.get("scopes"),
            expires_at=data.get("expiresAt"),
        )
        logger.info(
            f"Retrieved GitHub token for user {user_id} "
            f"(github: {info.github_username or 'unknown'})"
        )
        return info

    return _call_token_service(
        cache_key=user_id,
        url=url,
        headers=headers,
        parse_response=_parse,
        use_cache=use_cache,
        label=f"get_github_token({user_id})",
    )


def resolve_api_key(api_key: str) -> Optional[GitHubTokenInfo]:
    """Resolve an agent API key to a user identity and GitHub token.

    Agent API keys allow coding agents (Claude Code, Cursor, Codex) to
    authenticate with the hosted MCP endpoint using Bearer tokens instead
    of dashboard session cookies.

    Delegates to _call_token_service for cache, circuit breaker, and
    stale-while-revalidate handling.

    Args:
        api_key: The Bearer token from Authorization header

    Returns:
        GitHubTokenInfo if key is valid, None otherwise
    """
    if not api_key:
        return None

    import hashlib
    cache_key = f"apikey:{hashlib.sha256(api_key.encode()).hexdigest()}"

    api_url, service_key, vercel_bypass = _get_service_config()

    if api_url and service_key:
        url = f"{api_url.rstrip('/')}/api/mcp/credentials"
        headers = _build_service_headers(service_key, vercel_bypass)
        headers["X-Agent-Api-Key"] = api_key
    else:
        url = ""
        headers = {}

    def _parse(data: Dict[str, Any]) -> Optional[GitHubTokenInfo]:
        token = data.get("token")
        user_id = data.get("userId")
        if not token or not user_id:
            return None
        info = GitHubTokenInfo(
            token=token,
            user_id=user_id,
            github_username=data.get("githubUsername"),
            scopes=data.get("scopes"),
            expires_at=data.get("expiresAt"),
            capabilities=data.get("capabilities"),
        )
        cap_count = len(info.capabilities) if info.capabilities else 0
        logger.info(f"Resolved API key to user {user_id} ({cap_count} capabilities)")
        return info

    with trace_stage("auth.token_service"):
        return _call_token_service(
            cache_key=cache_key,
            url=url,
            headers=headers,
            parse_response=_parse,
            use_cache=True,
            label="resolve_api_key",
        )


# =============================================================================
# Cache & Circuit Breaker Management
# =============================================================================


def clear_token_cache() -> None:
    """Clear all cached tokens.

    Useful for testing or when tokens may have been rotated.
    Does NOT reset the circuit breaker — use reset_circuit_breaker() for that.
    """
    _github_token_cache.clear()
    logger.debug("GitHub token cache cleared")


def reset_circuit_breaker() -> None:
    """Reset circuit breaker to closed state.

    Resets the existing singleton in-place (under the lock) so that
    in-flight threads holding a reference to the same instance see
    the reset state. Does NOT replace the singleton.
    """
    with _circuit_breaker._lock:
        _circuit_breaker.state = _CB_CLOSED
        _circuit_breaker.failure_count = 0
        _circuit_breaker._half_open_probe_active = False
    logger.debug("Circuit breaker reset to closed")


def get_circuit_breaker_status() -> Dict[str, Any]:
    """Get circuit breaker state for health/diagnostic endpoints.

    Returns:
        Dict with state, failure_count, and timing information.
    """
    return {
        "state": _circuit_breaker.state,
        "failure_count": _circuit_breaker.failure_count,
        "last_failure_age_s": (
            round(time.time() - _circuit_breaker.last_failure_time, 1)
            if _circuit_breaker.last_failure_time > 0 else None
        ),
        "last_success_age_s": (
            round(time.time() - _circuit_breaker.last_success_time, 1)
            if _circuit_breaker.last_success_time > 0 else None
        ),
    }


def invalidate_user_token(user_id: str) -> None:
    """Remove a specific user's token from cache.

    Call this if a token is rejected by GitHub API to force a refresh.

    Args:
        user_id: User ID to invalidate
    """
    if user_id in _github_token_cache:
        del _github_token_cache[user_id]
        logger.debug(f"Invalidated cached token for user {user_id}")


def get_auth_headers(user_id: str) -> Optional[Dict[str, str]]:
    """Get HTTP headers for authenticated GitHub API requests.

    Convenience method that returns properly formatted headers for
    GitHub API authentication.

    Args:
        user_id: User identifier

    Returns:
        Dict with Authorization header, or None if token not available
    """
    token_info = get_github_token(user_id)
    if token_info:
        return {
            "Authorization": f"token {token_info.token}",
            "Accept": "application/vnd.github.v3+json",
        }
    return None


# =============================================================================
# Request Context (for extracting user_id from HTTP requests)
# =============================================================================


@dataclass
class RequestContext:
    """Context extracted from an incoming HTTP request.

    In hosted mode, MCP tools need to know which user is making the request
    to fetch the appropriate GitHub token.
    """

    user_id: Optional[str] = None
    session_id: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None


def extract_request_context(
    headers: Dict[str, str],
    query_params: Optional[Dict[str, str]] = None,
) -> RequestContext:
    """Extract request context from HTTP headers and query params.

    The token service expects requests to include user identification:
    - X-User-ID header: User identifier from session
    - X-Session-ID header: Session identifier
    - repo query param: Repository context
    - branch query param: Branch context

    Args:
        headers: HTTP request headers
        query_params: Optional query parameters

    Returns:
        RequestContext with extracted values
    """
    query_params = query_params or {}

    return RequestContext(
        user_id=headers.get("X-User-ID") or headers.get("x-user-id"),
        session_id=headers.get("X-Session-ID") or headers.get("x-session-id"),
        repo=query_params.get("repo"),
        branch=query_params.get("branch"),
    )
