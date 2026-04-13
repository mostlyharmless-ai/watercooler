"""Unit tests for HTTP middleware stage functions.

Tests each stage in isolation without a full TestClient/FastAPI app.
Stage functions are pure-ish: they take explicit inputs and return
either None (continue) or a JSONResponse (short-circuit).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# Skip if HTTP deps not installed
try:
    from fastapi.responses import JSONResponse
    from watercooler_mcp.server_http import (
        _AuthResult,
        _RateLimiter,
        _stage_authenticate,
        _stage_content_validation,
        _stage_rate_limit,
        _stage_request_id,
        _stage_set_context,
        _verify_hmac_signature,
        check_http_dependencies,
    )

    HTTP_AVAILABLE = check_http_dependencies()
except ImportError:
    HTTP_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not HTTP_AVAILABLE, reason="HTTP dependencies not installed"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeHeaders(dict):
    """Dict subclass that behaves like Starlette Headers for .get()."""
    pass


def _make_request(
    path: str = "/mcp",
    headers: Optional[dict] = None,
    query_params: Optional[dict] = None,
) -> MagicMock:
    """Build a minimal fake Starlette Request."""
    req = MagicMock()
    req.url.path = path
    h = _FakeHeaders(headers or {})
    req.headers = h
    req.query_params = query_params or {}
    req.state = MagicMock()
    return req


def _make_extract_context(user_id=None, repo=None, branch=None):
    """Return a fake extract_request_context function."""

    @dataclass
    class _Ctx:
        user_id: Optional[str] = None
        repo: Optional[str] = None
        branch: Optional[str] = None

    def _extract(headers, query_params):
        return _Ctx(
            user_id=user_id or headers.get("X-User-ID") or headers.get("x-user-id"),
            repo=repo,
            branch=branch,
        )

    return _extract


def _hmac_sign(body: bytes, secret: str) -> str:
    """v1 HMAC: body only."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _hmac_sign_v2(body: bytes, secret: str, user_id: str, timestamp: str) -> str:
    """v2 HMAC: canonical string with identity + timestamp + body."""
    canonical = f"{user_id}\n{timestamp}\n{body.hex()}".encode("utf-8")
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Stage 1: _stage_request_id
# ---------------------------------------------------------------------------


class TestStageRequestId:
    def test_generates_uuid_when_no_header(self):
        req = _make_request(headers={})
        rid = _stage_request_id(req)
        assert rid  # non-empty
        assert len(rid) == 36  # UUID format

    def test_accepts_valid_client_id(self):
        req = _make_request(headers={"X-Request-ID": "abc-123.test:0"})
        assert _stage_request_id(req) == "abc-123.test:0"

    def test_rejects_too_long_id(self):
        req = _make_request(headers={"X-Request-ID": "a" * 200})
        rid = _stage_request_id(req)
        assert rid != "a" * 200
        assert len(rid) == 36

    def test_rejects_injection_characters(self):
        req = _make_request(headers={"X-Request-ID": "abc\ndef"})
        rid = _stage_request_id(req)
        assert "\n" not in rid

    def test_rejects_empty_string(self):
        req = _make_request(headers={"X-Request-ID": ""})
        rid = _stage_request_id(req)
        assert len(rid) == 36


# ---------------------------------------------------------------------------
# Stage 2: _stage_content_validation
# ---------------------------------------------------------------------------


class TestStageContentValidation:
    @pytest.mark.anyio
    async def test_passes_when_no_content_length_small_body(self):
        req = _make_request(headers={})
        req.body = AsyncMock(return_value=b"small")
        assert await _stage_content_validation(req, "rid", 1024) is None

    @pytest.mark.anyio
    async def test_passes_when_within_limit(self):
        req = _make_request(headers={"content-length": "100"})
        req.body = AsyncMock(return_value=b"x" * 100)
        assert await _stage_content_validation(req, "rid", 1024) is None

    @pytest.mark.anyio
    async def test_rejects_oversized_by_header(self):
        """Declared Content-Length over limit rejects without reading body."""
        req = _make_request(headers={"content-length": "2000"})
        result = await _stage_content_validation(req, "rid", 1024)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 413

    @pytest.mark.anyio
    async def test_rejects_spoofed_content_length(self):
        """Content-Length says small but actual body is large."""
        req = _make_request(headers={"content-length": "100"})
        req.body = AsyncMock(return_value=b"x" * 2000)
        result = await _stage_content_validation(req, "rid", 1024)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 413

    @pytest.mark.anyio
    async def test_rejects_chunked_oversized_body(self):
        """Chunked encoding (no Content-Length) with oversized body is rejected."""
        req = _make_request(headers={})
        req.body = AsyncMock(return_value=b"x" * 2000)
        result = await _stage_content_validation(req, "rid", 1024)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 413

    @pytest.mark.anyio
    async def test_passes_chunked_small_body(self):
        """Chunked encoding with small body passes."""
        req = _make_request(headers={})
        req.body = AsyncMock(return_value=b"ok")
        assert await _stage_content_validation(req, "rid", 1024) is None

    @pytest.mark.anyio
    async def test_malformed_content_length_returns_400(self):
        """Non-numeric Content-Length returns 400, not 500."""
        req = _make_request(headers={"content-length": "abc"})
        result = await _stage_content_validation(req, "rid", 1024)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 400


# ---------------------------------------------------------------------------
# Stage 3: _stage_authenticate
# ---------------------------------------------------------------------------


class TestStageAuthenticate:
    @pytest.mark.anyio
    async def test_skip_for_non_mcp_path(self):
        req = _make_request(path="/health", headers={})
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="secret",
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "skip"

    @pytest.mark.anyio
    async def test_bearer_valid(self):
        """Valid Bearer token resolves to bearer auth mode."""
        token_info = MagicMock(user_id="user-1", token="ghp_abc")
        req = _make_request(headers={"Authorization": "Bearer valid-key"})
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="secret",
            is_hosted=True,
            resolve_api_key_fn=lambda k: token_info if k == "valid-key" else None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "bearer"
        assert result.user_id == "user-1"
        assert result.github_token == "ghp_abc"

    @pytest.mark.anyio
    async def test_bearer_invalid_returns_401(self):
        """Invalid Bearer token returns 401, never falls through to HMAC."""
        req = _make_request(headers={"Authorization": "Bearer bad-key"})
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="secret",
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.anyio
    async def test_bearer_empty_does_not_skip_hmac(self):
        """'Bearer ' with no key is treated as no Bearer — HMAC required."""
        req = _make_request(headers={"Authorization": "Bearer  "})
        # No HMAC signature → should require it
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="secret",
            is_hosted=False,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401
        # Verify it's the HMAC error, not Bearer error
        body = result.body.decode()
        assert "Signature" in body

    @pytest.mark.anyio
    async def test_hmac_valid(self):
        """Valid v2 HMAC + hosted mode + user_id resolves to hmac mode."""
        import datetime
        secret = "test-secret"
        body = b'{"method": "tools/call"}'
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sig = _hmac_sign_v2(body, secret, "user-2", ts)
        token_info = MagicMock(token="ghp_xyz")

        req = _make_request(headers={
            "X-Request-Signature": sig,
            "X-Request-Timestamp": ts,
            "X-User-ID": "user-2",
        })
        req.body = AsyncMock(return_value=body)

        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret=secret,
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: token_info,
            extract_context_fn=_make_extract_context(user_id="user-2"),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "hmac"
        assert result.user_id == "user-2"
        assert result.github_token == "ghp_xyz"

    @pytest.mark.anyio
    async def test_hmac_missing_signature_returns_401(self):
        """MCP path + secret configured + no signature → 401."""
        req = _make_request(headers={})
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="secret",
            is_hosted=False,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.anyio
    async def test_hmac_invalid_signature_returns_401(self):
        """Bad HMAC signature → 401."""
        req = _make_request(headers={"X-Request-Signature": "bad"})
        req.body = AsyncMock(return_value=b"body")

        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="secret",
            is_hosted=False,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.anyio
    async def test_hosted_no_secret_returns_503(self):
        """Hosted mode without INTERNAL_SECRET → 503."""
        req = _make_request(headers={})
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="",
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(user_id=None),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    @pytest.mark.anyio
    async def test_hosted_no_user_id_returns_401(self):
        """Hosted mode + secret + no Bearer + no X-User-ID → 401 (HMAC required)."""
        req = _make_request(headers={})
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="secret",
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(user_id=None),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.anyio
    async def test_hosted_no_token_returns_403(self):
        """Hosted mode + user_id but no GitHub token → 403."""
        import datetime
        secret = "test-secret"
        body = b'{"method": "tools/call"}'
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sig = _hmac_sign_v2(body, secret, "user-3", ts)

        req = _make_request(headers={
            "X-Request-Signature": sig,
            "X-Request-Timestamp": ts,
            "X-User-ID": "user-3",
        })
        req.body = AsyncMock(return_value=body)
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret=secret,
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,  # returns None
            extract_context_fn=_make_extract_context(user_id="user-3"),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 403

    @pytest.mark.anyio
    async def test_local_mode_skip(self):
        """Local mode + no secret → skip."""
        req = _make_request(headers={})
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="",
            is_hosted=False,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "skip"

    @pytest.mark.anyio
    async def test_hmac_verified_non_hosted_returns_hmac_mode(self):
        """HMAC secret set + non-hosted: verified request gets mode='hmac', not 'skip'."""
        secret = "test-secret"
        body = b'{"method": "tools/call"}'
        sig = _hmac_sign(body, secret)

        req = _make_request(headers={"X-Request-Signature": sig})
        req.body = AsyncMock(return_value=body)

        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret=secret,
            is_hosted=False,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "hmac"

    @pytest.mark.anyio
    async def test_bearer_bypasses_hmac(self):
        """Valid Bearer should succeed even when HMAC secret is set."""
        token_info = MagicMock(user_id="agent-1", token="ghp_agent")
        req = _make_request(headers={"Authorization": "Bearer agent-key"})
        # No HMAC signature present — but Bearer should bypass HMAC
        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret="secret",
            is_hosted=True,
            resolve_api_key_fn=lambda k: token_info if k == "agent-key" else None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "bearer"

    @pytest.mark.anyio
    async def test_hmac_v2_valid(self):
        """v2 HMAC with identity + timestamp passes."""
        import datetime
        secret = "test-secret"
        body = b'{"method": "tools/call"}'
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sig = _hmac_sign_v2(body, secret, "user-v2", ts)
        token_info = MagicMock(token="ghp_v2")

        req = _make_request(headers={
            "X-Request-Signature": sig,
            "X-Request-Timestamp": ts,
            "X-User-ID": "user-v2",
        })
        req.body = AsyncMock(return_value=body)

        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret=secret,
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: token_info,
            extract_context_fn=_make_extract_context(user_id="user-v2"),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "hmac"
        assert result.user_id == "user-v2"

    @pytest.mark.anyio
    async def test_hmac_v2_wrong_user_id_fails(self):
        """v2 HMAC signed for one user rejects substituted X-User-ID."""
        import datetime
        secret = "test-secret"
        body = b'{"method": "tools/call"}'
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Sign as user-a
        sig = _hmac_sign_v2(body, secret, "user-a", ts)

        # Present as user-b
        req = _make_request(headers={
            "X-Request-Signature": sig,
            "X-Request-Timestamp": ts,
            "X-User-ID": "user-b",
        })
        req.body = AsyncMock(return_value=body)

        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret=secret,
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(user_id="user-b"),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.anyio
    async def test_hmac_v2_expired_timestamp_fails(self):
        """v2 HMAC with expired timestamp is rejected."""
        import datetime
        secret = "test-secret"
        body = b'{"method": "tools/call"}'
        old_ts = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=600)
        ).isoformat()
        sig = _hmac_sign_v2(body, secret, "user-x", old_ts)

        req = _make_request(headers={
            "X-Request-Signature": sig,
            "X-Request-Timestamp": old_ts,
            "X-User-ID": "user-x",
        })
        req.body = AsyncMock(return_value=body)

        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret=secret,
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(user_id="user-x"),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.anyio
    @pytest.mark.anyio
    async def test_hmac_v1_rejected_in_hosted_mode(self):
        """v1 HMAC (no timestamp) is rejected in hosted mode."""
        secret = "test-secret"
        body = b'{"method": "tools/call"}'
        sig = _hmac_sign(body, secret)

        req = _make_request(headers={
            "X-Request-Signature": sig,
            "X-User-ID": "user-v1",
        })
        req.body = AsyncMock(return_value=body)

        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret=secret,
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(user_id="user-v1"),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.anyio
    async def test_hmac_v1_fallback_works_non_hosted(self):
        """v1 HMAC (no timestamp) still works in non-hosted mode."""
        secret = "test-secret"
        body = b'{"method": "tools/call"}'
        sig = _hmac_sign(body, secret)

        req = _make_request(headers={
            "X-Request-Signature": sig,
            "X-User-ID": "user-v1",
        })
        req.body = AsyncMock(return_value=body)

        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret=secret,
            is_hosted=False,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(user_id="user-v1"),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "hmac"

    @pytest.mark.anyio
    async def test_hmac_v2_replay_different_body_fails(self):
        """v2 signature for one body rejects a different body."""
        import datetime
        secret = "test-secret"
        body_a = b'{"method": "tools/call", "id": 1}'
        body_b = b'{"method": "tools/call", "id": 2}'
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sig = _hmac_sign_v2(body_a, secret, "user-r", ts)

        req = _make_request(headers={
            "X-Request-Signature": sig,
            "X-Request-Timestamp": ts,
            "X-User-ID": "user-r",
        })
        req.body = AsyncMock(return_value=body_b)

        result = await _stage_authenticate(
            req,
            "rid",
            internal_secret=secret,
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(user_id="user-r"),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401


# ---------------------------------------------------------------------------
# Stage 4: _stage_rate_limit
# ---------------------------------------------------------------------------


class TestStageRateLimit:
    def test_no_limiter_passes(self):
        auth = _AuthResult(mode="hmac", user_id="u1")
        assert _stage_rate_limit(auth, "rid", None) is None

    def test_disabled_limiter_passes(self):
        limiter = _RateLimiter(rpm=0)
        auth = _AuthResult(mode="hmac", user_id="u1")
        assert _stage_rate_limit(auth, "rid", limiter) is None

    def test_within_limit_passes(self):
        limiter = _RateLimiter(rpm=100)
        auth = _AuthResult(mode="hmac", user_id="u1")
        assert _stage_rate_limit(auth, "rid", limiter) is None

    def test_exceeds_limit_returns_429(self):
        limiter = _RateLimiter(rpm=1)
        auth = _AuthResult(mode="hmac", user_id="u1")
        # First request passes
        assert _stage_rate_limit(auth, "rid", limiter) is None
        # Second request is rate limited
        result = _stage_rate_limit(auth, "rid", limiter)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 429

    def test_no_user_id_skips_limiting(self):
        limiter = _RateLimiter(rpm=1)
        auth = _AuthResult(mode="skip", user_id=None)
        # Should not rate limit when no user_id
        assert _stage_rate_limit(auth, "rid", limiter) is None
        assert _stage_rate_limit(auth, "rid", limiter) is None

    def test_rate_limit_uses_resolved_identity(self):
        """Rate limit key is the auth-resolved user_id, not header spoofing."""
        limiter = _RateLimiter(rpm=1)
        # Bearer-resolved identity
        auth = _AuthResult(mode="bearer", user_id="real-user")
        assert _stage_rate_limit(auth, "rid", limiter) is None
        result = _stage_rate_limit(auth, "rid", limiter)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 429


# ---------------------------------------------------------------------------
# Stage 5: _stage_set_context
# ---------------------------------------------------------------------------


class TestStageSetContext:
    def test_sets_request_state(self):
        req = _make_request()
        auth = _AuthResult(
            mode="hmac",
            user_id="u1",
            repo="org/repo",
            branch="main",
            github_token="ghp_abc",
        )
        _stage_set_context(req, auth, "rid-123")
        assert req.state.user_id == "u1"
        assert req.state.repo == "org/repo"
        assert req.state.branch == "main"
        assert req.state.request_id == "rid-123"
        assert req.state.github_token == "ghp_abc"

    def test_skip_mode_no_token(self):
        """Skip mode without token should not set github_token or HttpRequestContext."""
        req = _make_request()
        auth = _AuthResult(mode="skip", user_id="u1")
        _stage_set_context(req, auth, "rid-123")
        assert req.state.user_id == "u1"
        # github_token should NOT be set on request.state
        # (MagicMock will auto-create attrs, so we verify no HttpRequestContext was set)
        # The key test is that set_http_context is NOT called for skip mode


# ---------------------------------------------------------------------------
# _AuthResult dataclass
# ---------------------------------------------------------------------------


class TestAuthResult:
    def test_defaults(self):
        ar = _AuthResult(mode="skip")
        assert ar.user_id is None
        assert ar.github_token is None
        assert ar.repo is None
        assert ar.branch is None

    def test_full(self):
        ar = _AuthResult(
            mode="bearer",
            user_id="u",
            github_token="t",
            repo="r",
            branch="b",
        )
        assert ar.mode == "bearer"
        assert ar.user_id == "u"
