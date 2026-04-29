"""HTTP server module for hosted MCP deployment.

This module provides an HTTP-based entry point for the Watercooler MCP server,
designed for deployment as:
- Vercel serverless function (Python runtime)
- Standalone HTTP service (Railway, Fly.io, etc.)
- Docker container

The HTTP server integrates:
- FastMCP with HTTP transport
- Token-based authentication (via auth.py)
- HMAC request signing (via WATERCOOLER_INTERNAL_SECRET)
- Per-user rate limiting (via WATERCOOLER_RATE_LIMIT_RPM)
- Request correlation IDs (X-Request-ID)
- Agent API key auth (Authorization: Bearer)
- Response caching (via cache.py)
- Request context extraction

Environment variables:
- WATERCOOLER_MCP_TRANSPORT: Set to "http" to enable HTTP mode
- WATERCOOLER_MCP_HOST: HTTP host (default: "0.0.0.0")
- WATERCOOLER_MCP_PORT: HTTP port (default: 8080)
- WATERCOOLER_MODE: "local" or "hosted"
- WATERCOOLER_INTERNAL_SECRET: HMAC signing secret for request auth
- WATERCOOLER_RATE_LIMIT_RPM: Per-user requests per minute (0 = disabled)
- See auth.py and cache.py for additional env vars

Deployment Options:

1. Standalone HTTP Server:
   ```bash
   WATERCOOLER_MCP_TRANSPORT=http python -m watercooler_mcp
   ```

2. Vercel Serverless (api/mcp.py):
   ```python
   from watercooler_mcp.server_http import app
   # Vercel auto-discovers FastAPI/Starlette apps
   ```

3. Docker:
   ```dockerfile
   CMD ["python", "-m", "watercooler_mcp.server_http"]
   ```

Usage from clients:
    POST /mcp
    Content-Type: application/json
    X-User-ID: user_123

    {"method": "tools/call", "params": {"name": "watercooler_say", ...}}
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable, Any, Union

from starlette.requests import Request as StarletteRequest

logger = logging.getLogger(__name__)


class _RateLimiter:
    """In-memory per-user sliding window rate limiter.

    Tracks request timestamps per user_id and enforces a configurable
    requests-per-minute limit. Safe in single-threaded asyncio context.
    Not thread-safe: check-then-append is non-atomic under concurrent threads.
    """

    def __init__(self, rpm: int = 0):
        """Initialize rate limiter.

        Args:
            rpm: Requests per minute per user. 0 = disabled.
        """
        self.rpm = rpm
        self._windows: dict[str, list[float]] = {}

    def check(self, user_id: str) -> tuple[bool, int]:
        """Check if request is allowed under rate limit.

        Args:
            user_id: User identifier

        Returns:
            Tuple of (allowed, retry_after_seconds).
            If allowed is True, retry_after is 0.
        """
        if self.rpm <= 0 or not user_id:
            return (True, 0)

        import time
        now = time.time()
        window_start = now - 60.0

        # Get or create user's request log
        timestamps = self._windows.get(user_id, [])
        # Prune expired entries
        timestamps = [t for t in timestamps if t > window_start]

        # Write pruned timestamps back before eviction check so the
        # current user's entry is up-to-date during eviction scan
        self._windows[user_id] = timestamps

        # Evict to prevent unbounded memory growth (e.g., adversarial
        # unique X-User-ID floods). Two-phase: stale first, then LRU.
        _MAX_TRACKED_USERS = 10000
        if len(self._windows) > _MAX_TRACKED_USERS:
            # Phase 1: evict entries with no recent requests
            stale = [
                k for k, v in self._windows.items()
                if k != user_id and (not v or v[-1] < window_start)
            ]
            for k in stale:
                del self._windows[k]

            # Phase 2: if still over cap, evict oldest-last-request entries
            if len(self._windows) > _MAX_TRACKED_USERS:
                by_recency = sorted(
                    ((k, v) for k, v in self._windows.items() if k != user_id),
                    key=lambda kv: kv[1][-1] if kv[1] else 0,
                )
                to_evict = len(self._windows) - _MAX_TRACKED_USERS
                for k, _v in by_recency[:to_evict]:
                    del self._windows[k]

        if len(timestamps) >= self.rpm:
            # Rate limited — calculate retry-after from oldest entry in window
            retry_after = int(timestamps[0] - window_start) + 1
            return (False, max(retry_after, 1))

        timestamps.append(now)
        self._windows[user_id] = timestamps
        return (True, 0)


# Module-level rate limiter instance (lazy-initialized in create_http_app)
_rate_limiter: _RateLimiter | None = None


@dataclass
class _AuthResult:
    """Result of the authentication stage.

    Carries the resolved auth mode, user identity, and credentials
    through the pipeline so downstream stages don't re-parse headers.
    """

    mode: str  # "hmac", "bearer", "skip"
    user_id: Optional[str] = None
    github_token: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    capabilities: Optional[frozenset] = None  # Preloaded from credentials response


def _stage_request_id(request: Any) -> str:
    """Stage 1: Sanitize or generate X-Request-ID.

    Accepts a client-supplied request ID if it passes validation
    (alphanumeric + hyphens/dots/colons/underscores, max 128 chars).
    Otherwise generates a new UUID.

    Args:
        request: Starlette/FastAPI Request object.

    Returns:
        A safe, non-empty request correlation ID.
    """
    from .context import _generate_request_id

    raw_id = (
        request.headers.get("X-Request-ID")
        or request.headers.get("x-request-id")
        or ""
    )
    if raw_id and len(raw_id) <= 128 and re.fullmatch(r"[\w\-.:]+", raw_id):
        return raw_id
    return _generate_request_id()


async def _stage_content_validation(
    request: Any,
    request_id: str,
    max_size: int,
) -> Optional[Any]:
    """Stage 2: Reject oversized request bodies.

    Early-rejects via Content-Length when declared, then always reads
    the actual body to verify size. This prevents Content-Length spoofing
    where a client declares a small value but streams a large body.

    Starlette caches the body after the first read, so downstream stages
    (e.g. HMAC verification) re-read the same bytes without penalty.

    Args:
        request: Starlette/FastAPI Request object.
        request_id: Correlation ID from stage 1.
        max_size: Maximum allowed body size in bytes.

    Returns:
        JSONResponse(413) if too large, None to continue.
    """
    from fastapi.responses import JSONResponse

    # Fast reject: if Content-Length is declared and already too large,
    # reject without reading the body
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
            if length < 0:
                raise ValueError("negative")
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid Content-Length header"},
                headers={"X-Request-ID": request_id},
            )
        if length > max_size:
            return JSONResponse(
                status_code=413,
                content={
                    "error": f"Request too large. Maximum size is {max_size} bytes."
                },
                headers={"X-Request-ID": request_id},
            )

    # Always verify actual body size (prevents Content-Length spoofing
    # and handles chunked encoding)
    body = await request.body()
    if len(body) > max_size:
        return JSONResponse(
            status_code=413,
            content={
                "error": f"Request too large. Maximum size is {max_size} bytes."
            },
            headers={"X-Request-ID": request_id},
        )
    return None


async def _stage_authenticate(
    request: Any,
    request_id: str,
    *,
    internal_secret: str,
    is_hosted: bool,
    resolve_api_key_fn: Callable,
    get_github_token_fn: Callable,
    extract_context_fn: Callable,
) -> Union["_AuthResult", Any]:
    """Stage 3: Authenticate the request (HMAC / Bearer / skip).

    Decision tree:
        Non-MCP path? → skip (extract context from headers)
        MCP path + Bearer non-empty? → resolve API key → valid: bearer | invalid: 401
        MCP path + no Bearer + INTERNAL_SECRET? → verify HMAC → invalid: 401
        MCP path + no Bearer + hosted? → require X-User-ID + token lookup
        Otherwise → skip

    Args:
        request: Starlette/FastAPI Request object.
        request_id: Correlation ID from stage 1.
        internal_secret: WATERCOOLER_INTERNAL_SECRET value (may be empty).
        is_hosted: Whether hosted mode is active.
        resolve_api_key_fn: auth.resolve_api_key callable.
        get_github_token_fn: auth.get_github_token callable.
        extract_context_fn: auth.extract_request_context callable.

    Returns:
        _AuthResult on success, or JSONResponse(401/403) on failure.
    """
    from fastapi.responses import JSONResponse

    is_mcp_path = (
        request.url.path in ("/mcp", "/mcp/")
        or request.url.path.startswith("/mcp/")
    )

    # Extract context from headers (needed for all paths)
    headers = dict(request.headers)
    query_params = dict(request.query_params)
    ctx = extract_context_fn(headers, query_params)

    # Non-MCP paths skip auth entirely
    if not is_mcp_path:
        return _AuthResult(
            mode="skip",
            user_id=ctx.user_id,
            repo=ctx.repo,
            branch=ctx.branch,
        )

    # --- MCP path: check Bearer first (independent of HMAC) ---
    auth_header_raw = request.headers.get("Authorization") or ""
    has_bearer = (
        auth_header_raw.startswith("Bearer ")
        and len(auth_header_raw[7:].strip()) > 0
    )

    import asyncio

    if has_bearer:
        api_key = auth_header_raw[7:].strip()
        token_info = await asyncio.to_thread(resolve_api_key_fn, api_key)
        if token_info:
            # Distinguish None (field absent → consult grant service) from
            # empty list (explicit "no capabilities" → deny all gated tools).
            caps = (
                frozenset(token_info.capabilities)
                if token_info.capabilities is not None
                else None
            )
            return _AuthResult(
                mode="bearer",
                user_id=token_info.user_id,
                github_token=token_info.token,
                repo=headers.get("X-Repo") or headers.get("x-repo"),
                branch=headers.get("X-Branch") or headers.get("x-branch"),
                capabilities=caps,
            )
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid API key"},
            headers={"X-Request-ID": request_id},
        )

    # --- No Bearer: hosted mode REQUIRES INTERNAL_SECRET for HMAC auth ---
    # Evaluated per-request so env changes take effect without restart.
    if is_hosted and not internal_secret:
        return JSONResponse(
            status_code=503,
            content={"error": "Service misconfigured: HMAC secret required in hosted mode"},
            headers={"X-Request-ID": request_id},
        )

    # --- No Bearer: HMAC verification (if secret configured) ---
    if internal_secret:
        signature = (
            request.headers.get("X-Request-Signature")
            or request.headers.get("x-request-signature")
        )
        if not signature:
            return JSONResponse(
                status_code=401,
                content={"error": "X-Request-Signature header required"},
                headers={"X-Request-ID": request_id},
            )

        # v2 HMAC includes timestamp for replay protection.
        # Hosted mode REQUIRES v2 — v1 fallback is only for non-hosted
        # (local dev) where identity spoofing is not a concern.
        timestamp = (
            request.headers.get("X-Request-Timestamp")
            or request.headers.get("x-request-timestamp")
            or ""
        )
        if not timestamp and is_hosted:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "X-Request-Timestamp header required in hosted mode"
                },
                headers={"X-Request-ID": request_id},
            )
        if timestamp:
            ts_err = _validate_hmac_timestamp(timestamp)
            if ts_err:
                return JSONResponse(
                    status_code=401,
                    content={"error": ts_err},
                    headers={"X-Request-ID": request_id},
                )

        body = await request.body()
        if not _verify_hmac_signature(
            body,
            signature,
            internal_secret,
            user_id=ctx.user_id or "",
            timestamp=timestamp,
        ):
            logger.warning(
                "HMAC verification failed for request %s from user %s",
                request_id,
                ctx.user_id or "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid request signature"},
                headers={"X-Request-ID": request_id},
            )

    # --- Hosted mode: require user identity + GitHub token ---
    if is_hosted:
        if not ctx.user_id:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Authentication required: provide X-User-ID header "
                    "or Authorization: Bearer <api-key>"
                },
                headers={"X-Request-ID": request_id},
            )
        token_info = await asyncio.to_thread(get_github_token_fn, ctx.user_id)
        if not token_info:
            return JSONResponse(
                status_code=403,
                content={"error": "No GitHub token found for user"},
                headers={"X-Request-ID": request_id},
            )
        return _AuthResult(
            mode="hmac",
            user_id=ctx.user_id,
            github_token=token_info.token,
            repo=headers.get("X-Repo") or headers.get("x-repo"),
            branch=headers.get("X-Branch") or headers.get("x-branch"),
        )

    # No Bearer, non-hosted — pass through.
    # Use "hmac" mode if HMAC was verified, "skip" otherwise.
    return _AuthResult(
        mode="hmac" if internal_secret else "skip",
        user_id=ctx.user_id,
        repo=ctx.repo,
        branch=ctx.branch,
    )


def _stage_rate_limit(
    auth_result: _AuthResult,
    request_id: str,
    limiter: Optional[_RateLimiter],
) -> Optional[Any]:
    """Stage 4: Per-user rate limiting.

    Only applies when limiter is configured and auth resolved a user_id.
    Runs after auth so spoofed X-User-ID cannot burn a victim's budget.

    Args:
        auth_result: Resolved auth from stage 3.
        request_id: Correlation ID from stage 1.
        limiter: Rate limiter instance (may be None or disabled).

    Returns:
        JSONResponse(429) if rate limited, None to continue.
    """
    from fastapi.responses import JSONResponse

    if limiter and auth_result.user_id:
        allowed, retry_after = limiter.check(auth_result.user_id)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded"},
                headers={
                    "X-Request-ID": request_id,
                    "Retry-After": str(retry_after),
                },
            )
    return None


def _stage_set_context(
    request: Any,
    auth_result: _AuthResult,
    request_id: str,
) -> None:
    """Stage 5: Write request state and set HTTP context variable.

    Populates request.state for downstream handlers and sets the
    contextvars-based HttpRequestContext for MCP tools.

    Args:
        request: Starlette/FastAPI Request object.
        auth_result: Resolved auth from stage 3.
        request_id: Correlation ID from stage 1.
    """
    from .context import HttpRequestContext, set_http_context

    request.state.user_id = auth_result.user_id
    request.state.repo = auth_result.repo
    request.state.branch = auth_result.branch
    request.state.request_id = request_id

    # Extract session_id from MCP / proxy headers (checked in priority order)
    session_id = (
        request.headers.get("mcp-session-id")
        or request.headers.get("x-session-id")
        or request.headers.get("X-Session-ID")
    )

    # Extract daemon config from hybrid client header (if present)
    daemon_config_json = (
        request.headers.get("X-Daemon-Config")
        or request.headers.get("x-daemon-config")
    )

    # Set context variable for MCP tools when we have a GitHub token
    if auth_result.github_token:
        request.state.github_token = auth_result.github_token
        set_http_context(HttpRequestContext(
            user_id=auth_result.user_id,
            repo=auth_result.repo,
            branch=auth_result.branch,
            github_token=auth_result.github_token,
            request_id=request_id,
            capabilities=auth_result.capabilities,
            session_id=session_id,
            daemon_config_json=daemon_config_json,
        ))


# Maximum age (seconds) for HMAC timestamps. Requests older than this are
# rejected to prevent replay attacks. Only applies to v2 (timestamped) HMAC.
HMAC_WINDOW = int(os.getenv("WATERCOOLER_HMAC_WINDOW", "300"))


def _verify_hmac_signature(
    body: bytes,
    signature: str,
    secret: str,
    *,
    user_id: str = "",
    timestamp: str = "",
) -> bool:
    """Verify HMAC-SHA256 signature with identity binding and replay protection.

    Supports two formats:
    - **v2** (when timestamp is non-empty): signs "{user_id}\\n{timestamp}\\n{body_hex}".
      Binds the signature to the claimed identity and a timestamp, preventing
      header substitution and replay attacks.
    - **v1** (legacy fallback): signs raw body bytes only. Used when
      X-Request-Timestamp header is absent (proxy not yet updated).
      Logs a deprecation warning.

    Args:
        body: Raw request body bytes.
        signature: Hex-encoded HMAC signature from X-Request-Signature.
        secret: Shared secret (WATERCOOLER_INTERNAL_SECRET).
        user_id: X-User-ID header value (v2 only).
        timestamp: X-Request-Timestamp header value, ISO 8601 UTC (v2 only).

    Returns:
        True if signature is valid.
    """
    try:
        if timestamp:
            # v2: canonical string includes identity + timestamp + body
            canonical = f"{user_id}\n{timestamp}\n{body.hex()}".encode("utf-8")
            expected = hmac.new(
                secret.encode("utf-8"),
                canonical,
                hashlib.sha256,
            ).hexdigest()
        else:
            # v1 legacy: body-only (will be removed once proxy is updated)
            logger.warning(
                "HMAC v1 (body-only) used — upgrade proxy to send "
                "X-Request-Timestamp for identity-bound signatures"
            )
            expected = hmac.new(
                secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError):
        return False


def _validate_hmac_timestamp(timestamp: str) -> Optional[str]:
    """Validate an HMAC timestamp is within the allowed window.

    Args:
        timestamp: ISO 8601 UTC timestamp string.

    Returns:
        Error message if invalid/expired, None if valid.
    """
    import datetime

    try:
        ts = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            return "Invalid X-Request-Timestamp format: timezone required"
    except (ValueError, AttributeError):
        return "Invalid X-Request-Timestamp format"

    now = datetime.datetime.now(datetime.timezone.utc)
    age = (now - ts).total_seconds()
    # Reject future timestamps (with 30s tolerance for clock skew)
    if age < -30:
        return f"Request timestamp is in the future ({int(-age)}s ahead)"
    if age > HMAC_WINDOW:
        return f"Request timestamp expired ({int(age)}s old, max {HMAC_WINDOW}s)"
    return None


def check_http_dependencies() -> bool:
    """Check if HTTP dependencies are installed.

    The HTTP server requires the [http] extra:
        pip install watercooler[http]

    Returns:
        True if dependencies are available
    """
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        return True
    except ImportError:
        return False


# Module-level so `from .server_http import _graphiti_warm_state` (used by
# the diagnostic display in tools/diagnostic.py) actually resolves. The
# warmup thread inside create_http_app() mutates this dict in place rather
# than rebinding it, so both the /health closure and the diagnostic import
# observe the same state.
_graphiti_warm_state: dict = {
    "state": "disabled",
    "duration_ms": 0,
    "error": None,
    "host": None,
    "port": None,
    "database": None,
}


def create_http_app():
    """Create FastAPI application wrapping the MCP server.

    This function creates a FastAPI app that:
    1. Exposes the FastMCP server via HTTP
    2. Adds authentication middleware (HMAC + Bearer + X-User-ID)
    3. Adds per-user rate limiting
    4. Adds request correlation IDs
    5. Provides health check endpoints

    Returns:
        FastAPI application instance
    """
    if not check_http_dependencies():
        raise ImportError(
            "HTTP dependencies not installed. "
            "Install with: pip install watercooler[http]"
        )

    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    from .auth import (
        extract_request_context,
        is_hosted_mode,
        is_token_service_configured,
        get_github_token,
        get_circuit_breaker_status,
        resolve_api_key,
    )
    from .cache import cache
    from .request_trace import (
        RequestTrace,
        set_request_trace,
        clear_request_trace,
        trace_stage,
    )

    # Initialize subsystems that server.py module-level code used to trigger.
    # When server_http.py is the entry point (Railway hosted mode), these must
    # run here since we no longer do `from .server import mcp`.
    from .middleware import setup_instrumentation
    setup_instrumentation()

    from .memory_sync import init_memory_sync_callbacks
    init_memory_sync_callbacks()

    try:
        from .memory_queue import init_memory_queue
        from watercooler.memory_config import get_queue_max_workers, get_queue_task_timeout
        init_memory_queue(
            max_workers=get_queue_max_workers(),
            task_timeout=get_queue_task_timeout(),
        )
        from .memory_sync import init_memory_queue_executors
        init_memory_queue_executors()
    except Exception as _mq_err:
        logger.warning("Could not initialise memory task queue: %s", _mq_err)

    try:
        from .daemons import init_daemons
        init_daemons()
    except Exception as _dm_err:
        logger.warning("Could not initialise daemon manager: %s", _dm_err)

    # Instantiate hosted capability authorization.
    # The authorizer gates tool execution on hosted surfaces based on
    # per-user capability grants fetched from the watercooler-site API.
    from .capability_auth import CapabilityGrantService, CapabilityAuthorizer
    _grant_service = CapabilityGrantService.from_env()
    _authorizer = CapabilityAuthorizer(_grant_service)

    # Resolve deployment availability (profile-aware surface building).
    from .deployment_profile import resolve_deployment_availability
    _deployment_availability = resolve_deployment_availability()

    # Build hosted surfaces via the shared server factory.
    # hosted_full  → /mcp       (dashboard, all tools)
    # hosted_premium → /mcp/premium  (premium graph + memory tools only)
    from .server_factory import build_http_surfaces
    hosted_full_mcp, hosted_premium_mcp = build_http_surfaces(
        authorizer=_authorizer,
        deployment_availability=_deployment_availability,
    )

    # Background Graphiti warmup for T2+ profiles.
    # Initializes the backend in a background thread so the first tool
    # call doesn't pay the cold-start cost. Non-blocking — does not
    # delay app startup or health check readiness.
    # Mutate the module-level dict in place (don't rebind) so the
    # diagnostic display sees the same state via its module import.
    _graphiti_warm_state.update({
        "state": "disabled",
        "duration_ms": 0,
        "error": None,
        "host": None,
        "port": None,
        "database": None,
    })

    if _deployment_availability and _deployment_availability.effective_profile in ("t2", "t2t3"):
        import threading

        def _warmup_graphiti():
            import time
            _graphiti_warm_state["state"] = "warming"
            start = time.monotonic()
            try:
                from .memory import load_graphiti_config
                config = load_graphiti_config()
                if config:
                    _graphiti_warm_state["host"] = config.falkordb_host
                    _graphiti_warm_state["port"] = config.falkordb_port
                    _graphiti_warm_state["database"] = config.database
                    from .tools.graph import _get_or_create_graphiti_backend
                    _get_or_create_graphiti_backend(config)
                    _graphiti_warm_state["state"] = "ready"
                else:
                    _graphiti_warm_state["state"] = "failed"
                    _graphiti_warm_state["error"] = "load_graphiti_config returned None"
            except Exception as e:
                _graphiti_warm_state["state"] = "failed"
                _graphiti_warm_state["error"] = str(e)
                logger.warning(
                    "Graphiti warmup failed (host=%s:%s db=%s): %s",
                    _graphiti_warm_state.get("host"),
                    _graphiti_warm_state.get("port"),
                    _graphiti_warm_state.get("database"),
                    e,
                )
            _graphiti_warm_state["duration_ms"] = round((time.monotonic() - start) * 1000)
            logger.info(
                "Graphiti warmup: %s in %dms (host=%s:%s db=%s)",
                _graphiti_warm_state["state"],
                _graphiti_warm_state["duration_ms"],
                _graphiti_warm_state.get("host"),
                _graphiti_warm_state.get("port"),
                _graphiti_warm_state.get("database"),
            )

        warmup_thread = threading.Thread(target=_warmup_graphiti, daemon=True)
        warmup_thread.start()
        logger.info("Graphiti background warmup started (profile=%s)", _deployment_availability.effective_profile)

    # --- Hosted JSON-RPC adapter (replaces mounted FastMCP http_app sub-apps) ---
    # Instead of mounting FastMCP's StreamableHTTP transport (which has
    # session-manager + task-group requirements that fail under Railway's
    # memory constraints), we use a thin JSON-RPC adapter that calls into
    # the FastMCP surfaces via their public API (list_tools, call_tool, etc.).
    from .hosted_rpc import HostedSurfaceSpec, dispatch_hosted_request, handle_hosted_delete

    _dashboard_spec = HostedSurfaceSpec(
        name="dashboard",
        surface=hosted_full_mcp,
        path="/mcp",
    )
    _premium_spec = HostedSurfaceSpec(
        name="premium",
        surface=hosted_premium_mcp,
        path="/mcp/premium",
    )

    # Create FastAPI app (no mounted MCP sub-app lifespans needed)
    app = FastAPI(
        title="Watercooler MCP HTTP Server",
        description="HTTP interface for Watercooler MCP tools",
        version="1.0.0",
    )

    # Get HTTP config from unified config system
    def _get_http_config() -> tuple[str, int, int]:
        """Get HTTP config (cors_origins, max_request_size, request_timeout)."""
        cors = os.getenv("WATERCOOLER_CORS_ORIGINS", "")
        max_size_str = os.getenv("WATERCOOLER_MAX_REQUEST_SIZE", "")
        timeout_str = os.getenv("WATERCOOLER_REQUEST_TIMEOUT", "")

        # Fall back to TOML config
        try:
            from watercooler.config_facade import config
            http_cfg = config.full().mcp.http

            if not cors:
                cors = http_cfg.cors_origins
            if not max_size_str:
                max_size_str = str(http_cfg.max_request_size)
            if not timeout_str:
                timeout_str = str(http_cfg.request_timeout)
        except ImportError:
            pass

        # Apply defaults
        max_size = int(max_size_str) if max_size_str else 1024 * 1024
        timeout = int(timeout_str) if timeout_str else 30

        return cors, max_size, timeout

    cors_origins_config, MAX_REQUEST_SIZE, REQUEST_TIMEOUT = _get_http_config()

    # Initialize rate limiter
    global _rate_limiter
    rate_limit_rpm = int(os.getenv("WATERCOOLER_RATE_LIMIT_RPM", "0"))
    _rate_limiter = _RateLimiter(rpm=rate_limit_rpm)

    # Log startup warning if hosted mode lacks INTERNAL_SECRET.
    # Enforcement is per-request inside _stage_authenticate (not frozen at startup).
    if is_hosted_mode() and not os.getenv("WATERCOOLER_INTERNAL_SECRET"):
        logger.error(
            "SECURITY: Hosted mode active without WATERCOOLER_INTERNAL_SECRET. "
            "Non-Bearer /mcp requests will be rejected (503). "
            "Bearer (agent API key) auth still works independently."
        )

    # Configure CORS for browser-based clients
    # Security: When allow_credentials=True, origins must be explicit (not "*")
    if cors_origins_config and cors_origins_config != "*":
        # Explicit origins configured - safe to use credentials
        cors_origins = [o.strip() for o in cors_origins_config.split(",") if o.strip()]
        allow_credentials = True
    else:
        # No explicit origins or wildcard - disable credentials for security
        # This prevents CORS credential leakage vulnerabilities
        cors_origins = ["*"]
        allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["X-User-ID", "X-Repo", "X-Branch", "X-Request-ID", "X-Request-Signature", "X-Request-Timestamp", "Content-Type", "Authorization"],
        expose_headers=["X-Request-ID"],
    )

    @app.get("/health")
    async def health_check():
        """Health check endpoint for load balancers and diagnostics."""
        hosted = is_hosted_mode()
        health: dict = {
            "status": "healthy",
            "mode": "hosted" if hosted else "local",
            "cache": cache.stats() if hasattr(cache, "stats") else {"backend": "unknown"},
        }

        # Deployment profile (profile-aware surface building)
        if _deployment_availability is not None:
            health["hosted_profile"] = {
                "build": _deployment_availability.build_profile,
                "requested": _deployment_availability.requested_profile,
                "effective": _deployment_availability.effective_profile,
                "graphiti_available": _deployment_availability.graphiti_available,
                "leanrag_available": _deployment_availability.leanrag_available,
                "degraded_reasons": _deployment_availability.degraded_reasons,
                # Redact infrastructure topology from the unauthenticated
                # /health surface — host/port/database are only exposed in
                # the auth-gated MCP diagnostic tool. The `error` string
                # is also redacted (replaced with a boolean) because
                # connection-failure exceptions from redis/socket libs
                # routinely embed host:port in the message.
                "graphiti_warmup": {
                    "state": _graphiti_warm_state.get("state"),
                    "duration_ms": _graphiti_warm_state.get("duration_ms"),
                    "has_error": _graphiti_warm_state.get("error") is not None,
                },
            }

        if hosted:
            health["token_service"] = {
                "configured": is_token_service_configured(),
                "circuit_breaker": get_circuit_breaker_status(),
            }

        # Rate limiter status
        if _rate_limiter and _rate_limiter.rpm > 0:
            health["rate_limit"] = {
                "rpm": _rate_limiter.rpm,
                "tracked_users": len(_rate_limiter._windows),
            }

        return health

    @app.get("/")
    async def root():
        """Root endpoint with API information."""
        return {
            "service": "Watercooler MCP HTTP Server",
            "version": "1.0.0",
            "endpoints": {
                "/health": "Health check",
                "/mcp": "MCP protocol endpoint (POST) — full surface",
                "/mcp/premium": "MCP protocol endpoint (POST) — premium surface",
            },
            "auth_mode": "hosted" if is_hosted_mode() else "local",
        }

    @app.middleware("http")
    async def request_pipeline(request: Request, call_next):
        """Staged request pipeline: ID → size → auth → rate limit → context.

        Each stage is a pure-ish function with explicit inputs/outputs.
        Short-circuits on the first JSONResponse error return.
        """
        import asyncio

        # Stage 1: Request ID
        request_id = _stage_request_id(request)

        # Create request trace and bind to context
        trace = RequestTrace(request_id=request_id)
        trace_token = set_request_trace(trace)
        try:
            # Stage 2: Content size validation
            err = await _stage_content_validation(request, request_id, MAX_REQUEST_SIZE)
            if err:
                return err

            # Stage 3: Authentication (HMAC / Bearer / skip)
            with trace_stage("auth.resolve"):
                auth = await _stage_authenticate(
                    request,
                    request_id,
                    internal_secret=os.getenv("WATERCOOLER_INTERNAL_SECRET", ""),
                    is_hosted=is_hosted_mode(),
                    resolve_api_key_fn=resolve_api_key,
                    get_github_token_fn=get_github_token,
                    extract_context_fn=extract_request_context,
                )
            if isinstance(auth, JSONResponse):
                return auth

            # Record resolved user on the trace
            if auth.user_id:
                trace.user_id = auth.user_id

            # Stage 4: Rate limiting (uses resolved identity from auth)
            err = _stage_rate_limit(auth, request_id, _rate_limiter)
            if err:
                return err

            # Stage 5: Set request context for MCP tools
            _stage_set_context(request, auth, request_id)

            # Stage 6: Daemon scope creation removed from generic middleware.
            # Scope is now lazily ensured only by tools that need daemon/background
            # runtime state (write tools, daemon status, memory ingest). Read-only
            # thread operations no longer pay first-scope startup cost.

            # Dispatch — MCP routes own their own timeouts via the adapter;
            # non-MCP routes use the outer request timeout.
            is_mcp_route = request.url.path.rstrip("/").startswith("/mcp")
            try:
                with trace_stage("tool.dispatch"):
                    if is_mcp_route:
                        # MCP adapter owns per-tool timeouts
                        response = await call_next(request)
                    else:
                        response = await asyncio.wait_for(
                            call_next(request),
                            timeout=REQUEST_TIMEOUT,
                        )
                response.headers["X-Request-ID"] = request_id
                return response
            except asyncio.TimeoutError:
                return JSONResponse(
                    status_code=504,
                    content={"error": f"Request timed out after {REQUEST_TIMEOUT} seconds"},
                    headers={"X-Request-ID": request_id},
                )
        finally:
            trace.emit_log()
            clear_request_trace(trace_token)

    # --- Explicit JSON-RPC routes (replaces mounted FastMCP sub-apps) ---
    # Premium routes must be registered BEFORE dashboard routes because
    # FastAPI matches by specificity — /mcp/premium must not be swallowed by /mcp.

    @app.post("/mcp/premium/")
    async def mcp_premium_post(request: StarletteRequest):
        return await dispatch_hosted_request(_premium_spec, request)

    @app.delete("/mcp/premium/")
    async def mcp_premium_delete(request: StarletteRequest):
        return await handle_hosted_delete(_premium_spec, request)

    @app.post("/mcp/")
    async def mcp_dashboard_post(request: StarletteRequest):
        return await dispatch_hosted_request(_dashboard_spec, request)

    @app.delete("/mcp/")
    async def mcp_dashboard_delete(request: StarletteRequest):
        return await handle_hosted_delete(_dashboard_spec, request)

    # Also register without trailing slash (FastAPI doesn't auto-redirect POSTs)
    @app.post("/mcp/premium")
    async def mcp_premium_post_noslash(request: StarletteRequest):
        return await dispatch_hosted_request(_premium_spec, request)

    @app.post("/mcp")
    async def mcp_dashboard_post_noslash(request: StarletteRequest):
        return await dispatch_hosted_request(_dashboard_spec, request)

    logger.info("Registered hosted JSON-RPC routes at /mcp and /mcp/premium")

    return app


# Create app instance for import
# This allows deployment platforms to auto-discover the app:
#   from watercooler_mcp.server_http import app
try:
    app = create_http_app()
except ImportError:
    app = None


def run_http_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    reload: bool = False,
) -> None:
    """Run the HTTP server.

    Args:
        host: Host to bind to (default: 0.0.0.0)
        port: Port to bind to (default: 8080)
        reload: Enable auto-reload for development
    """
    if not check_http_dependencies():
        print(
            "HTTP dependencies not installed.\n"
            "Install with: pip install watercooler[http]",
            file=sys.stderr,
        )
        sys.exit(1)

    import uvicorn

    print(f"Starting Watercooler MCP HTTP Server on http://{host}:{port}", file=sys.stderr)
    print(f"Health check: http://{host}:{port}/health", file=sys.stderr)
    print(f"API docs: http://{host}:{port}/docs", file=sys.stderr)

    uvicorn.run(
        "watercooler_mcp.server_http:app",
        host=host,
        port=port,
        reload=reload,
    )


def main():
    """Entry point for running HTTP server directly.

    Usage:
        python -m watercooler_mcp.server_http
    """
    from .config import get_mcp_transport_config

    config = get_mcp_transport_config()
    run_http_server(
        host=config["host"],
        port=config["port"],
    )


if __name__ == "__main__":
    main()
