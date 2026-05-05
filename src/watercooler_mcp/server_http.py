"""HTTP server module for hosted MCP deployment.

This module provides an HTTP-based entry point for the Watercooler MCP server,
designed for deployment as:
- Vercel serverless function (Python runtime)
- Standalone HTTP service (Railway, Fly.io, etc.)
- Docker container

The HTTP server integrates:
- FastMCP with HTTP transport
- Token-based authentication (via auth.py)
- HMAC request signing (v3 per-key registry — sole supported scheme)
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
- WATERCOOLER_HMAC_REQUIRE_V3: "warn" or "enforce" (Move 2.5 — the
  HMAC auth scheme; v3 keys live in the per-key registry loaded by
  ``auth/hmac_keys.py``). The legacy v1/v2 single-secret verification
  path was deleted (#733); only v3 verification remains.
- WATERCOOLER_REQUIRE_HMAC_OR_BEARER: "warn" (default) or "enforce".
  Plan v5.1 verification-audit residual: when "enforce", the hosted
  ``mode="identity"`` path (X-User-ID without Bearer / HMAC v3) is
  rejected with a generic 401 before the privileged token-service
  lookup. Default "warn" preserves existing behaviour and emits
  ``hosted_identity_auth_used`` telemetry so operators can confirm
  zero traffic before flipping to "enforce". See
  ``_identity_auth_mode``.
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
                k
                for k, v in self._windows.items()
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

    ``mode`` values:
        ``"hmac"``     — HMAC v3 signature successfully verified.
        ``"bearer"``   — `Authorization: Bearer wc_...` resolved by the
                         token service to a user identity + GitHub token.
        ``"identity"`` — Hosted X-User-ID + token-lookup path with no
                         HMAC signature. **Deprecated** under
                         ``WATERCOOLER_REQUIRE_HMAC_OR_BEARER=enforce``
                         (plan v5.1 verification audit residual finding;
                         see ``_identity_auth_mode``). The dashboard
                         proxy migrated to HMAC v3 in Sprint 3 so this
                         path has no live legitimate caller; slated for
                         deletion in a follow-up PR after enforce flips
                         in production. Until then it is distinct from
                         ``"hmac"`` so future code that gates on
                         signature presence cannot misclassify.
        ``"skip"``     — Non-MCP path (e.g., `/health`) or non-hosted
                         pass-through.
    """

    mode: str  # "hmac", "bearer", "identity", "skip"
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
        request.headers.get("X-Request-ID") or request.headers.get("x-request-id") or ""
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
            content={"error": f"Request too large. Maximum size is {max_size} bytes."},
            headers={"X-Request-ID": request_id},
        )
    return None


async def _attempt_hmac_v3_auth(
    request: Any,
    request_id: str,
    *,
    auth_header: str,
    body: bytes,
    timestamp: str,
    ctx: Any,
    require_v3: str,
    hmac_registry: Optional[Any],
    get_github_token_fn: Callable,
) -> Optional[Union["_AuthResult", Any]]:
    """Try HMAC v3 verification.

    Returns:
        None — ``parse_v3_authorization_header`` rejected the
            header (malformed kid, non-hex sig, missing fields).
            **Caller contract:** when the request reached this
            function via the ``HMAC-SHA256``-prefixed branch in
            ``_stage_authenticate``, a ``None`` return MUST
            result in a 401. The caller MUST NOT fall through
            to v2 HMAC verification. PR #703 round 7+5 MED: the
            previous wording said "caller falls through to v2",
            which was the opposite of the contract the existing
            caller enforces (and would be an authentication
            bypass — v2 has no Authorization-header parser, so a
            malformed v3 request would silently invoke v2
            processing of unrelated headers).
        ``_AuthResult`` — v3 verification succeeded.
        ``JSONResponse`` — v3 attempted but failed (401/403).

    Plan v5.1 reference: Move 2.5 (HMAC v3). The canonical string
    binds method/path/key_id/X-Repo/X-Branch in addition to the v2
    fields, so header tampering invalidates the signature. The
    registry lookup enforces subject-binding (which X-User-ID may
    sign) and repo-authorisation (which X-Repo may be accessed)
    independently of the signature verification.

    No ``is_hosted`` gate: v3 is intentionally available in both
    hosted and non-hosted deployments. Service keys may be
    configured locally (CI smokes, ops scripts), and per-user keys
    may be issued by a self-hosted dashboard. The registry's
    presence (``hmac_registry is None`` → 503) is the only
    deployment-level gate.
    """
    from fastapi.responses import JSONResponse

    from .auth import is_hosted_mode
    from .auth.hmac_keys import (
        build_v3_canonical_string,
        check_repo_authorisation,
        check_subject_binding,
        parse_v3_authorization_header,
        verify_v3_signature,
    )
    from .observability import log_action

    parsed = parse_v3_authorization_header(auth_header)
    if parsed is None:
        return None
    key_id, signature_hex = parsed

    if hmac_registry is None:
        # PR #703 round 6 LOW: 503 + a distinct message would
        # tell unauthenticated callers whether the v3 registry
        # is deployed. Collapse to the same 401 + generic message
        # used by every other v3 pre-auth failure. Operators see
        # the deployment-misconfiguration in telemetry
        # (``hmac_v3_no_registry``) rather than via response
        # fingerprinting.
        log_action(
            "hmac_v3_no_registry",
            request_id=request_id,
            key_id=key_id,
        )
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing HMAC v3 credentials"},
            headers={"X-Request-ID": request_id},
        )

    # Validate the timestamp BEFORE looking up the key. The lookup
    # outcome is what the registry leaks — distinguishable error
    # messages between "unknown kid" and "missing timestamp" let an
    # unauthenticated probe enumerate registered key_ids by sending
    # ``Authorization: HMAC-SHA256 v=3 kid=<probe> sig=x`` without a
    # timestamp. Returning the same generic 401 for both
    # "missing required field" cases closes that oracle.
    if not timestamp:
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing HMAC v3 credentials"},
            headers={"X-Request-ID": request_id},
        )

    # PR #703 round 7+3 MED: the replay-window check used to live
    # only in the outer ``_stage_authenticate``. ``_attempt_hmac_v3_auth``
    # took ``timestamp`` as a parameter and assumed the caller had
    # already enforced the window — an undocumented precondition.
    # Future callers (a WebSocket transport, a refactored auth
    # path, a test helper) invoking the function in isolation
    # would have skipped replay protection silently. Run the
    # window check inside the function so the precondition is
    # explicit and the function is safe to call standalone. The
    # specific ``ts_err`` message ("Request timestamp expired
    # (347s old, max 300s)") is preserved verbatim — same surface
    # the v2 path uses, useful for ops debugging clock-skew, and
    # does not enable key enumeration (the timestamp validity is
    # caller-controlled, not server state).
    ts_err = _validate_hmac_timestamp(timestamp)
    if ts_err:
        return JSONResponse(
            status_code=401,
            content={"error": ts_err},
            headers={"X-Request-ID": request_id},
        )

    # PR #709 round 2 HIGH: ``KeyRegistry.lookup`` may delegate to
    # the ``HttpResolver`` fallback chain, which uses synchronous
    # ``httpx.Client``. A direct call from this ``async def`` would
    # block the asyncio event loop for up to ``timeout_s`` (5s)
    # on every resolver cache miss. Wrap with ``asyncio.to_thread``
    # so the blocking I/O runs on a worker thread — matches the
    # discipline already used elsewhere in this file
    # (``server_http.py`` lines ~482, 612, 773 wrap their blocking
    # I/O the same way). Cheap when the registry hits in-memory
    # (no I/O, but the thread-pool dispatch adds maybe 100µs);
    # essential when the resolver fetches.
    import asyncio

    key_info = await asyncio.to_thread(hmac_registry.lookup, key_id)
    if key_info is None:
        log_action(
            "hmac_v3_unknown_key",
            request_id=request_id,
            key_id=key_id,
        )
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing HMAC v3 credentials"},
            headers={"X-Request-ID": request_id},
        )

    method = (request.method or "").upper()
    path = request.url.path or ""
    user_id = ctx.user_id or ""
    x_repo = request.headers.get("X-Repo") or request.headers.get("x-repo") or ""
    x_branch = request.headers.get("X-Branch") or request.headers.get("x-branch") or ""

    try:
        canonical = build_v3_canonical_string(
            method=method,
            path=path,
            timestamp=timestamp,
            key_id=key_id,
            user_id=user_id,
            body=body,
            x_repo=x_repo,
            x_branch=x_branch,
        )
    except ValueError:
        # PR #703 round 7+3 LOW: a CR/LF-containing user_id/x_repo/
        # x_branch (rejected by ``build_v3_canonical_string`` as a
        # field-boundary-injection guard). Treat as a generic v3
        # credential failure rather than leak which field offended.
        log_action(
            "hmac_v3_canonical_field_invalid",
            request_id=request_id,
            key_id=key_id,
        )
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing HMAC v3 credentials"},
            headers={"X-Request-ID": request_id},
        )
    if not verify_v3_signature(
        canonical=canonical,
        signature_hex=signature_hex,
        secret=key_info.secret,
    ):
        # PR #703 round 6 MED: previously returned a distinct
        # ``"Invalid HMAC v3 signature"`` message that let an
        # unauthenticated probe distinguish "kid registered,
        # wrong sig" from "kid unknown" (which used the generic
        # message). Collapse to the same generic 401 — the
        # detail lives in telemetry (``hmac_v3_invalid_signature``).
        log_action(
            "hmac_v3_invalid_signature",
            request_id=request_id,
            key_id=key_id,
            key_type=key_info.key_type,
        )
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing HMAC v3 credentials"},
            headers={"X-Request-ID": request_id},
        )

    # Subject-binding: who may this key sign for? In multi-tenant mode
    # we additionally refuse wildcard ``per_user`` keys
    # (``bound_user_id is None``) — the HTTP-resolver-issued path that
    # ``hmac_v3_startup_fail_fast_check`` cannot see at startup. PR #741
    # review.
    subj_err = check_subject_binding(
        key=key_info,
        signed_user_id=user_id,
        is_multi_tenant=is_hosted_mode(),
    )
    if subj_err is not None:
        # Subject-binding details (per_user vs service-delegation
        # policy mismatch) leak server-side configuration. Use
        # the generic credential-failure message; the structured
        # reason stays in telemetry only.
        log_action(
            "hmac_v3_subject_mismatch",
            request_id=request_id,
            key_id=key_id,
            key_type=key_info.key_type,
            reason=subj_err,
        )
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing HMAC v3 credentials"},
            headers={"X-Request-ID": request_id},
        )

    # Repo-authorisation: bring in the per-user repos claim if applicable.
    per_user_repo_claim = None
    token_info = None
    if key_info.key_type == "per_user":
        # PR #703 round 7+1 LOW: an empty signed ``X-User-ID`` paired
        # with the legacy global key (bound_user_id=None wildcard)
        # passes subject-binding, then short-circuits the token fetch
        # because ``if user_id:`` is False, then returns a 403 "No
        # GitHub token found for user". For every other pre-auth
        # failure (unknown kid, invalid sig) we return a generic 401.
        # The asymmetry lets a holder of the global secret distinguish
        # "valid signature over empty user_id" from "invalid sig" by
        # the status code. Practical impact is negligible (holding
        # the global secret already implies full access), but
        # collapse to the same generic 401 for consistency.
        if not user_id:
            log_action(
                "hmac_v3_empty_user_id",
                request_id=request_id,
                key_id=key_id,
                key_type=key_info.key_type,
            )
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or missing HMAC v3 credentials"},
                headers={"X-Request-ID": request_id},
            )
        import asyncio

        token_info = await asyncio.to_thread(get_github_token_fn, user_id)
        if token_info is None:
            return JSONResponse(
                status_code=403,
                content={"error": "No GitHub token found for user"},
                headers={"X-Request-ID": request_id},
            )
        per_user_repo_claim = getattr(token_info, "repos", None)

    repo_err = check_repo_authorisation(
        key=key_info, x_repo=x_repo, per_user_repo_claim=per_user_repo_claim
    )
    if repo_err is not None:
        # The plan v5.1 warn-mode is meant to absorb expected
        # mismatches during caller migration — not to paper over
        # operator misconfigurations that flip the security
        # posture (empty allow-list, missing repos claim, missing
        # X-Repo). ``RepoAuthError.fatal=True`` flags the latter
        # and is rejected unconditionally.
        if repo_err.fatal or require_v3 == "enforce":
            # PR #703 round 6 LOW: ``repo_err.message`` includes
            # the literal X-Repo and structural detail
            # (``"X-Repo 'org/repo' not in service allow-list"``).
            # That text leaks server-side allow-list configuration
            # to the unauthenticated caller. Return a generic
            # 403 body and keep the detail in telemetry only.
            log_action(
                "hmac_v3_repo_unauthorised",
                request_id=request_id,
                key_id=key_id,
                key_type=key_info.key_type,
                x_repo=x_repo,
                fatal=repo_err.fatal,
                reason=repo_err.message,
            )
            return JSONResponse(
                status_code=403,
                content={"error": "Repo not authorized"},
                headers={"X-Request-ID": request_id},
            )
        # warn-mode + non-fatal: log but accept (Sprint 2 observation window)
        logger.warning(
            "HMAC v3 repo-authorisation failure (warn-mode): %s", repo_err.message
        )

    # Telemetry: successful v3 verification
    log_action(
        "hmac_v3_verified",
        request_id=request_id,
        key_id=key_id,
        key_type=key_info.key_type,
        signature_scheme="v3",
    )

    github_token = ""
    if token_info is not None:
        github_token = getattr(token_info, "token", "") or ""

    return _AuthResult(
        mode="hmac",
        user_id=user_id,
        github_token=github_token,
        repo=x_repo,
        branch=x_branch,
    )


async def _stage_authenticate(
    request: Any,
    request_id: str,
    *,
    is_hosted: bool,
    resolve_api_key_fn: Callable,
    get_github_token_fn: Callable,
    extract_context_fn: Callable,
    hmac_registry: Optional[Any] = None,
    require_v3: str = "",
) -> Union["_AuthResult", Any]:
    """Stage 3: Authenticate the request (HMAC v3 / Bearer / skip).

    Decision tree:
        Non-MCP path? → skip (extract context from headers)
        MCP path + Bearer non-empty? → resolve API key → valid: bearer | invalid: 401
        MCP path + ``Authorization: HMAC-SHA256``? → verify v3 → invalid: 401
        MCP path + no Bearer + hosted? → require X-User-ID + token lookup
        Otherwise → skip

    Issue #733 deleted the legacy v1/v2 single-secret HMAC verification
    path. v3 (``Authorization: HMAC-SHA256 v=3 kid=<id> sig=<hex>``) is
    the sole supported HMAC scheme.

    Args:
        request: Starlette/FastAPI Request object.
        request_id: Correlation ID from stage 1.
        is_hosted: Whether hosted mode is active.
        resolve_api_key_fn: auth.resolve_api_key callable.
        get_github_token_fn: auth.get_github_token callable.
        extract_context_fn: auth.extract_request_context callable.
        hmac_registry: HMAC v3 per-key registry (None disables v3).
        require_v3: ``WATERCOOLER_HMAC_REQUIRE_V3`` value
            (``"warn"`` / ``"enforce"`` / unset).

    Returns:
        _AuthResult on success, or JSONResponse(401/403) on failure.
    """
    from fastapi.responses import JSONResponse

    is_mcp_path = request.url.path in ("/mcp", "/mcp/") or request.url.path.startswith(
        "/mcp/"
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
        auth_header_raw.startswith("Bearer ") and len(auth_header_raw[7:].strip()) > 0
    )

    import asyncio

    if has_bearer:
        api_key = auth_header_raw[7:].strip()
        token_info = await asyncio.to_thread(resolve_api_key_fn, api_key)
        if token_info:
            x_repo = headers.get("X-Repo") or headers.get("x-repo")
            # Move 2 Phase 2a (security consolidation plan v5.1):
            # check the token's ``repos`` claim against X-Repo.
            #   - warn-mode + claim absent: log + accept (Phase 2a-observe).
            #   - warn-mode + claim present + mismatch: 403.
            #   - enforce-mode: 403 if claim absent OR mismatch.
            from .auth import check_repo_claim

            claim_err = check_repo_claim(token_info, x_repo)
            if claim_err is not None:
                return JSONResponse(
                    status_code=403,
                    content={"error": claim_err},
                    headers={"X-Request-ID": request_id},
                )
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
                repo=x_repo,
                branch=headers.get("X-Branch") or headers.get("x-branch"),
                capabilities=caps,
            )
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid API key"},
            headers={"X-Request-ID": request_id},
        )

    # --- No Bearer: HMAC v3 verification (sole supported HMAC scheme) ---
    # Issue #733 deleted the legacy v1/v2 single-secret verification
    # path. v3 is detected by Authorization header shape
    # (``HMAC-SHA256 v=3 kid=<id> sig=<hex>``); a request without a v3
    # Authorization header proceeds straight to the hosted identity
    # gate (which 401s any unauthenticated request).
    timestamp_for_v3 = (
        request.headers.get("X-Request-Timestamp")
        or request.headers.get("x-request-timestamp")
        or ""
    )
    if auth_header_raw and auth_header_raw.startswith("HMAC-SHA256 "):
        # ``Authorization: HMAC-SHA256 ...`` is a v3-exclusive
        # indicator. We MUST NOT fall through if the v3 parse fails:
        # the legacy v2 path is gone and there is nothing else to
        # try (PR #703 round 4 MED — control-flow tightening; the
        # precondition is now load-bearing rather than just defensive
        # because no legacy fallback exists).
        # PR #703 round 7+3 MED: timestamp window check lives
        # inside ``_attempt_hmac_v3_auth`` so the precondition is
        # explicit at the inner-function boundary.
        body_for_v3 = await request.body()
        v3_outcome = await _attempt_hmac_v3_auth(
            request,
            request_id,
            auth_header=auth_header_raw,
            body=body_for_v3,
            timestamp=timestamp_for_v3,
            ctx=ctx,
            require_v3=require_v3,
            hmac_registry=hmac_registry,
            get_github_token_fn=get_github_token_fn,
        )
        if v3_outcome is not None:
            return v3_outcome
        # ``_attempt_hmac_v3_auth`` only returns None when
        # ``parse_v3_authorization_header`` rejects the header
        # (wrong version, malformed kid, non-hex sig). Reject
        # the request explicitly.
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing HMAC v3 credentials"},
            headers={"X-Request-ID": request_id},
        )

    # --- Hosted mode: require user identity + GitHub token ---
    if is_hosted:
        if not ctx.user_id:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Authentication required: provide "
                    "Authorization: Bearer <api-key> or HMAC v3"
                },
                headers={"X-Request-ID": request_id},
            )
        # Plan v5.1 verification audit (entry ``01KQNWPX3YJTBWQJGNQGD71CH7``
        # of ``security-audit-2026-04-28``) flagged the residual identity-
        # mode path as a HIGH-severity bypass: anyone supplying a victim's
        # ``X-User-ID`` plus an ``X-Repo`` in the victim's ``repos`` claim
        # impersonates them, since ``check_repo_claim`` was the only
        # mitigation and the cloud privately resolves the GitHub token
        # via the privileged token-service. The dashboard proxy migrated
        # to HMAC v3 in Sprint 3 (entry ``01KQG74EB1ASGQT89K3N6NYHWC``);
        # this path has no legitimate live caller.
        #
        # Telemetry fires in BOTH modes so warn-mode observation is
        # meaningful — operators must confirm zero traffic before the
        # flag flip. The enforce-mode rejection happens BEFORE the
        # privileged token-service lookup so unauthenticated probes can
        # neither fingerprint the gate (generic 401, same body as
        # bearer/v3 failures) nor burn a token-service round-trip per
        # request.
        from .observability import log_action

        identity_mode = _identity_auth_mode()
        log_action(
            "hosted_identity_auth_used",
            request_id=request_id,
            user_id=ctx.user_id,
            mode=identity_mode,
        )
        if identity_mode == "enforce":
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Authentication required: provide "
                    "Authorization: Bearer <api-key> or HMAC v3"
                },
                headers={"X-Request-ID": request_id},
            )
        # warn-mode: existing behaviour preserved. One-line WARNING so
        # deploy logs reflect the deprecated path until the flag flips.
        logger.warning(
            "hosted identity-mode auth used (request_id=%s, user_id=%s); "
            "this path is deprecated and rejected under "
            "WATERCOOLER_REQUIRE_HMAC_OR_BEARER=enforce.",
            request_id,
            ctx.user_id,
        )
        token_info = await asyncio.to_thread(get_github_token_fn, ctx.user_id)
        if not token_info:
            return JSONResponse(
                status_code=403,
                content={"error": "No GitHub token found for user"},
                headers={"X-Request-ID": request_id},
            )
        x_repo = headers.get("X-Repo") or headers.get("x-repo")
        # Move 2 Phase 2a applies to bearer/X-User-ID identified
        # requests: the repos claim narrows what the authenticated
        # caller can act on. With v1/v2 HMAC removed (#733), this
        # branch only fires for requests that present X-User-ID
        # without an HMAC v3 signature; the v3 path applies its
        # own X-Repo authorisation inside ``_attempt_hmac_v3_auth``
        # (signed X-Repo / X-Branch closes the within-claim bypass
        # the legacy v2 path could not).
        from .auth import check_repo_claim

        claim_err = check_repo_claim(token_info, x_repo)
        if claim_err is not None:
            return JSONResponse(
                status_code=403,
                content={"error": claim_err},
                headers={"X-Request-ID": request_id},
            )
        # No HMAC was verified on this branch — only X-User-ID + token
        # lookup. Use ``mode="identity"`` so downstream code that gates
        # on signature presence can distinguish this from the verified-
        # signature ``"hmac"`` path. (PR #741 round 2 review caught the
        # prior misnomer; the field had no live readers but the wrong
        # label was a footgun for future code.)
        return _AuthResult(
            mode="identity",
            user_id=ctx.user_id,
            github_token=token_info.token,
            repo=x_repo,
            branch=headers.get("X-Branch") or headers.get("x-branch"),
        )

    # No Bearer, no HMAC v3, non-hosted — pass through unauthenticated.
    # Issue #733 deleted the legacy v2 verifier; ``mode="skip"`` is the
    # only outcome for a request that reaches this point.
    return _AuthResult(
        mode="skip",
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
    daemon_config_json = request.headers.get("X-Daemon-Config") or request.headers.get(
        "x-daemon-config"
    )

    # Set context variable for MCP tools when we have a GitHub token
    if auth_result.github_token:
        request.state.github_token = auth_result.github_token
        set_http_context(
            HttpRequestContext(
                user_id=auth_result.user_id,
                repo=auth_result.repo,
                branch=auth_result.branch,
                github_token=auth_result.github_token,
                request_id=request_id,
                capabilities=auth_result.capabilities,
                session_id=session_id,
                daemon_config_json=daemon_config_json,
            )
        )


# Maximum age (seconds) for HMAC timestamps. Requests older than this are
# rejected to prevent replay attacks. Used by HMAC v3 (the v1/v2 verifier
# was deleted in #733; v3 carries its own ``X-Request-Timestamp`` checked
# inside ``_attempt_hmac_v3_auth``).
HMAC_WINDOW = int(os.getenv("WATERCOOLER_HMAC_WINDOW", "300"))


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


# Per-process cache of raw env values we've already logged as
# unrecognised. Without this, a misconfigured env var would emit one
# WARNING per request (the helper is on the auth hot path). The set is
# unbounded by design: the cardinality of distinct typos in any
# operator's lifetime is small, and the deployment-time-only nature of
# the env var means production never accumulates entries beyond a
# handful. Lock-free lookup is intentional — the race ("two concurrent
# requests both miss the cache and both log") yields at most a few
# duplicate WARNINGs per misconfiguration, which is preferable to
# adding a threading import for a self-correcting cosmetic race.
_identity_auth_unknown_value_warned: set[str] = set()


def _identity_auth_mode() -> str:
    """Return the configured policy for hosted ``mode="identity"`` requests.

    Plan v5.1 verification audit (entry ``01KQNWPX3YJTBWQJGNQGD71CH7`` of
    ``security-audit-2026-04-28``) flagged the residual identity-mode path
    in :func:`_stage_authenticate` as a HIGH-severity bypass surface: a
    request with ``X-User-ID`` but no Bearer / no HMAC v3 still resolves
    the user's GitHub token via the privileged token-service in hosted
    mode, with ``check_repo_claim`` as the only mitigation. Per audit
    entry ``01KQG74EB1ASGQT89K3N6NYHWC`` the dashboard proxy migrated to
    HMAC v3 in Sprint 3, so the path has no legitimate live caller; the
    rollout discipline mirrors :func:`repo_claim_mode` (Move 2) and the
    ``WATERCOOLER_HMAC_REQUIRE_V3`` flag (Move 2.5):

    * ``"warn"`` (default), empty, or unset — identity-mode auth
      proceeds with telemetry so operators can confirm zero traffic
      before flipping to enforce.
    * ``"enforce"`` — identity-mode is rejected with a generic 401
      BEFORE the privileged token-service lookup, matching the bearer
      / v3 failure surface so unauthenticated probes cannot fingerprint
      the gate. The truthy aliases ``"1"``, ``"true"``, ``"yes"``,
      ``"on"`` are also accepted as ``enforce``; convention inherited
      from :func:`repo_claim_mode` so an operator setting the var via
      a generic boolean-flag system gets the secure interpretation.

    PR #748 review (HIGH): an unrecognised value (typo such as
    ``"enforec"``, or a stale ``"false"`` / ``"0"`` / ``"no"`` /
    ``"off"`` from an unrelated boolean flag) defaults to ``"warn"``
    and emits a warn-once-per-process WARNING naming the bad value.
    Without this branch, an operator who intends to flip enforce but
    typos the env var would silently get warn-mode — the exact failure
    mode this gate is designed to prevent. The warn-once cache scope
    is the helper's process; a redeploy with a corrected value clears
    it naturally.
    """
    raw = os.getenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", "warn").strip().lower()
    if raw in ("enforce", "1", "true", "yes", "on"):
        return "enforce"
    if raw not in ("warn", ""):
        # Unrecognised value — warn-once-per-process. The helper runs
        # on the auth hot path, so a vanilla unconditional ``logger.warning``
        # would spam at request rate until the operator notices.
        if raw not in _identity_auth_unknown_value_warned:
            _identity_auth_unknown_value_warned.add(raw)
            logger.warning(
                "WATERCOOLER_REQUIRE_HMAC_OR_BEARER=%r is not a recognised "
                "value (accepted: 'warn', 'enforce', or truthy aliases "
                "'1'/'true'/'yes'/'on'); defaulting to 'warn'. If you "
                "intended to flip enforce, fix the env var and redeploy "
                "— the gate is currently NOT active.",
                raw,
            )
    return "warn"


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
    "reason": None,
}


def _initialize_warmup_state(is_hosted: bool) -> None:
    """Reset the module-level Graphiti warmup state for app startup.

    Mutates ``_graphiti_warm_state`` in place rather than rebinding so the
    diagnostic surface (which imports the dict) and the warmup thread (which
    writes into it) observe the same object.

    Hosted multi-tenant deployments do not run a startup warmup probe: the
    canonical T2 database name comes from per-request ``X-Repo`` and there
    is no single tenant to warm. The state is set to ``"skipped"`` with a
    human-readable ``reason`` so ``/health`` doesn't display a misleading
    ``"failed"``. Self-hosted single-tenant deployments fall through to the
    pending state so ``_run_warmup_probe`` can fill it in.

    Args:
        is_hosted: Whether the process is running in hosted multi-tenant mode.
    """
    if is_hosted:
        _graphiti_warm_state.update(
            {
                "state": "skipped",
                "duration_ms": 0,
                "error": None,
                "host": None,
                "port": None,
                "database": None,
                "reason": (
                    "multi-tenant scope-bound; warmup deferred to first "
                    "per-scope request"
                ),
            }
        )
    else:
        _graphiti_warm_state.update(
            {
                "state": "disabled",
                "duration_ms": 0,
                "error": None,
                "host": None,
                "port": None,
                "database": None,
                "reason": None,
            }
        )


def _run_warmup_probe() -> None:
    """Run the Graphiti warmup probe and update ``_graphiti_warm_state``.

    Pulled out of the previously-nested ``_warmup_graphiti`` closure inside
    ``create_http_app()`` so it is importable and unit-testable. Behaviour
    is unchanged from the prior implementation; only callable from
    self-hosted single-tenant deployments where a single canonical database
    name is known at startup. Hosted multi-tenant deployments must not call
    this — see :func:`_initialize_warmup_state` for that path.
    """
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
            _graphiti_warm_state["reason"] = (
                "load_graphiti_config returned None"
            )
    except Exception as e:
        _graphiti_warm_state["state"] = "failed"
        _graphiti_warm_state["error"] = str(e)
        _graphiti_warm_state["reason"] = str(e)
        logger.warning(
            "Graphiti warmup failed (host=%s:%s db=%s): %s",
            _graphiti_warm_state.get("host"),
            _graphiti_warm_state.get("port"),
            _graphiti_warm_state.get("database"),
            e,
        )
    _graphiti_warm_state["duration_ms"] = round(
        (time.monotonic() - start) * 1000
    )
    logger.info(
        "Graphiti warmup: %s in %dms (host=%s:%s db=%s)",
        _graphiti_warm_state["state"],
        _graphiti_warm_state["duration_ms"],
        _graphiti_warm_state.get("host"),
        _graphiti_warm_state.get("port"),
        _graphiti_warm_state.get("database"),
    )


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
        from watercooler.memory_config import (
            get_queue_max_workers,
            get_queue_task_timeout,
        )

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

    # Background Graphiti warmup for T2+ profiles. Initializes the backend
    # in a background thread so the first tool call doesn't pay the
    # cold-start cost. Non-blocking — does not delay app startup or
    # health-check readiness.
    #
    # Hosted multi-tenant deployments DO NOT run a startup warmup probe
    # (PR #660 regression fix): the canonical T2 database name comes from
    # per-request ``X-Repo`` so there is no single tenant to warm. The
    # state is set to ``"skipped"`` so ``/health`` reflects "scope-bound,
    # not run at startup" instead of a misleading ``"failed"``. The first
    # per-scope request still pays its own cold-start cost; in practice
    # this is bounded by FalkorDB pool reuse across same-tenant requests.
    #
    # ``_is_hosted`` is captured ONCE so the state initialiser and the
    # thread-launch gate cannot diverge if ``is_hosted_mode()`` ever
    # transitions during this section (review #737 round 1, MED #1).
    _is_hosted = is_hosted_mode()
    _initialize_warmup_state(is_hosted=_is_hosted)

    if (
        not _is_hosted
        and _deployment_availability
        and _deployment_availability.effective_profile in ("t2", "t2t3")
    ):
        import threading

        warmup_thread = threading.Thread(target=_run_warmup_probe, daemon=True)
        warmup_thread.start()
        logger.info(
            "Graphiti background warmup started (profile=%s)",
            _deployment_availability.effective_profile,
        )
    elif _is_hosted:
        logger.info(
            "Graphiti background warmup skipped (hosted multi-tenant; "
            "scope-bound database name resolved per-request)"
        )

    # --- Hosted JSON-RPC adapter (replaces mounted FastMCP http_app sub-apps) ---
    # Instead of mounting FastMCP's StreamableHTTP transport (which has
    # session-manager + task-group requirements that fail under Railway's
    # memory constraints), we use a thin JSON-RPC adapter that calls into
    # the FastMCP surfaces via their public API (list_tools, call_tool, etc.).
    from .hosted_rpc import (
        HostedSurfaceSpec,
        dispatch_hosted_request,
        handle_hosted_delete,
    )

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

    # Move 2.5 (HMAC v3): build the per-key registry and run the
    # multi-tenant fail-fast invariant check at startup.
    #
    # Issue #733 deleted the legacy v1/v2 verification path, so the
    # server no longer reads the legacy global HMAC secret for request
    # authentication. The H13 helper signature is preserved for the
    # unit-test surface but ``has_global_secret=False`` is passed
    # unconditionally — the legacy secret cannot influence request
    # auth any more, so that branch of the fail-fast guard is a
    # tautology at the call site (kept structurally so a future
    # re-introduction of the env-var read would be caught by the
    # helper's contract). PR #741 review repurposed the helper to do
    # real work: it scans the loaded registry for wildcard per_user
    # keys (``bound_user_id is None``) and refuses to boot if any are
    # statically configured in multi-tenant mode. HTTP-resolver-issued
    # wildcards are caught at request time by
    # ``check_subject_binding(is_multi_tenant=True)``.
    from .auth.hmac_keys import (
        hmac_v3_startup_fail_fast_check,
        load_default_registry,
    )

    _hmac_require_v3 = os.getenv("WATERCOOLER_HMAC_REQUIRE_V3", "").strip().lower()
    _hmac_registry = load_default_registry()
    fail_fast_msg = hmac_v3_startup_fail_fast_check(
        require_v3=_hmac_require_v3,
        is_multi_tenant=is_hosted_mode(),
        has_global_secret=False,
        registry=_hmac_registry,
    )
    if fail_fast_msg is not None:
        # H13 invariant — refuse to boot.
        raise RuntimeError(fail_fast_msg)

    # PR #748 review round 3 (MED): log the resolved identity-mode
    # policy at startup so operators have a single boot-time signal
    # for "what mode is this process running in", instead of having
    # to grep ``hosted_identity_auth_used`` telemetry. Mirrors the
    # ``HMAC v3: registry loaded with N keys`` startup log emitted
    # from ``load_default_registry``. The mode is still re-read per
    # request inside ``_identity_auth_mode`` for consistency with
    # ``repo_claim_mode`` (Move 2) and ``WATERCOOLER_HMAC_REQUIRE_V3``
    # (Move 2.5) — those flags also re-read per call, and operators
    # are fluent in the per-call pattern. A live env mutation without
    # redeploy is unsupported on Railway (env changes auto-redeploy)
    # and the per-call read keeps tests' ``monkeypatch.setenv`` shape
    # working without a reset hook.
    _initial_identity_mode = _identity_auth_mode()
    logger.info(
        "Hosted identity-mode policy: %s "
        "(env WATERCOOLER_REQUIRE_HMAC_OR_BEARER; %s)",
        _initial_identity_mode,
        "X-User-ID-only requests rejected with 401"
        if _initial_identity_mode == "enforce"
        else "X-User-ID-only requests accepted with telemetry; "
        "flip to 'enforce' once warn-mode hits drop to zero",
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
        allow_headers=[
            "X-User-ID",
            "X-Repo",
            "X-Branch",
            "X-Request-ID",
            "X-Request-Signature",
            "X-Request-Timestamp",
            "Content-Type",
            "Authorization",
        ],
        expose_headers=["X-Request-ID"],
    )

    @app.get("/health")
    async def health_check():
        """Health check endpoint for load balancers and diagnostics."""
        hosted = is_hosted_mode()
        health: dict = {
            "status": "healthy",
            "mode": "hosted" if hosted else "local",
            "cache": (
                cache.stats() if hasattr(cache, "stats") else {"backend": "unknown"}
            ),
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

            # Stage 3: Authentication (HMAC v3 / Bearer / skip)
            with trace_stage("auth.resolve"):
                auth = await _stage_authenticate(
                    request,
                    request_id,
                    is_hosted=is_hosted_mode(),
                    resolve_api_key_fn=resolve_api_key,
                    get_github_token_fn=get_github_token,
                    extract_context_fn=extract_request_context,
                    hmac_registry=_hmac_registry,
                    require_v3=_hmac_require_v3,
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
                    content={
                        "error": f"Request timed out after {REQUEST_TIMEOUT} seconds"
                    },
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

    print(
        f"Starting Watercooler MCP HTTP Server on http://{host}:{port}", file=sys.stderr
    )
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
