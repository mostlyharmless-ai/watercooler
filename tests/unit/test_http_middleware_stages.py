"""Unit tests for HTTP middleware stage functions.

Tests each stage in isolation without a full TestClient/FastAPI app.
Stage functions are pure-ish: they take explicit inputs and return
either None (continue) or a JSONResponse (short-circuit).

Note: HMAC v1/v2 single-secret tests were removed in #733 when
``_verify_hmac_signature`` and the ``internal_secret`` parameter
were deleted. v3 verification has dedicated coverage in
``tests/integration/test_hmac_v3.py`` and
``tests/unit/test_hmac_v3_primitives.py``.
"""

from __future__ import annotations

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
        token_info = MagicMock(user_id="user-1", token="ghp_abc", repos={"org/repo"}, capabilities=None)
        req = _make_request(
            headers={"Authorization": "Bearer valid-key", "X-Repo": "org/repo"}
        )
        result = await _stage_authenticate(
            req,
            "rid",
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
        """Invalid Bearer token returns 401."""
        req = _make_request(headers={"Authorization": "Bearer bad-key"})
        result = await _stage_authenticate(
            req,
            "rid",
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.anyio
    async def test_bearer_empty_falls_through_to_unauthenticated(self):
        """``Bearer `` with no key is treated as no Bearer.

        With the v1/v2 verifier deleted in #733, a non-hosted request
        with no Bearer and no v3 Authorization header passes through
        as ``mode="skip"`` (no auth gate at this layer). The
        downstream-tool authorisation surface is the load-bearing
        check in non-hosted mode.
        """
        req = _make_request(headers={"Authorization": "Bearer  "})
        result = await _stage_authenticate(
            req,
            "rid",
            is_hosted=False,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "skip"

    @pytest.mark.anyio
    async def test_hosted_no_user_id_returns_401(self):
        """Hosted mode + no Bearer + no X-User-ID → 401."""
        req = _make_request(headers={})
        result = await _stage_authenticate(
            req,
            "rid",
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(user_id=None),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401

    @pytest.mark.anyio
    async def test_hosted_with_user_id_no_token_returns_403(self):
        """Hosted mode + X-User-ID but token lookup returns None → 403."""
        req = _make_request(headers={"X-User-ID": "user-3"})
        result = await _stage_authenticate(
            req,
            "rid",
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,  # returns None
            extract_context_fn=_make_extract_context(user_id="user-3"),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 403

    # ----------------------------------------------------------------- #
    # Plan v5.1 verification-audit residual:
    # WATERCOOLER_REQUIRE_HMAC_OR_BEARER gates the identity-mode path.
    # Audit entry: 01KQNWPX3YJTBWQJGNQGD71CH7 of
    # security-audit-2026-04-28. See server_http.py:_identity_auth_mode
    # for the rollout rationale (warn → observe → enforce, mirrors M2 /
    # M2.5).
    # ----------------------------------------------------------------- #

    @pytest.mark.anyio
    async def test_hosted_identity_warn_mode_accepts_x_user_id(
        self, monkeypatch
    ):
        """warn-mode (default): identity-mode auth proceeds and
        ``hosted_identity_auth_used`` telemetry fires.

        Default behaviour preservation is load-bearing for the rollout
        — the PR ships warn-mode so operators can confirm zero traffic
        before flipping to enforce. Regression on this test would mean
        the gate has changed runtime semantics without an env-var flip.
        """
        # Default warn (env unset). Use delenv to be robust against test
        # leakage from a prior test that set the var.
        monkeypatch.delenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", raising=False)
        token_info = MagicMock(
            user_id="user-warn",
            token="ghp_warn",
            # Wave 6: the repo-claim code default is enforce, so the
            # fixture carries a claim + matching X-Repo — this test's
            # subject is the HMAC/bearer identity gate, not repo claims.
            repos={"org/repo"},
            capabilities=None,
        )
        req = _make_request(
            headers={"X-User-ID": "user-warn", "X-Repo": "org/repo"}
        )

        captured: list[dict] = []

        def _capture_log_action(action: str, **kwargs) -> None:
            captured.append({"action": action, **kwargs})

        monkeypatch.setattr(
            "watercooler_mcp.observability.log_action", _capture_log_action
        )

        result = await _stage_authenticate(
            req,
            "rid-warn",
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: token_info,
            extract_context_fn=_make_extract_context(user_id="user-warn"),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "identity"
        assert result.user_id == "user-warn"
        # Telemetry must fire in warn-mode so the observation window is
        # meaningful. Without this assertion, an operator watching the
        # action counter cannot distinguish "no traffic" from "telemetry
        # broken."
        assert any(
            entry["action"] == "hosted_identity_auth_used"
            and entry.get("mode") == "warn"
            and entry.get("user_id") == "user-warn"
            and entry.get("request_id") == "rid-warn"
            for entry in captured
        ), f"warn-mode telemetry missing; captured={captured!r}"

    @pytest.mark.anyio
    async def test_hosted_identity_enforce_mode_rejects_before_token_lookup(
        self, monkeypatch
    ):
        """enforce mode: 401 BEFORE the privileged token-service lookup.

        The token-service call is the unauthenticated leak surface
        (``WATERCOOLER_TOKEN_API_KEY`` is privileged service-to-service
        auth that will fetch any user's GitHub token by user_id). The
        gate MUST reject before that call so an enforce-mode probe
        cannot burn token-service round-trips per request, and so the
        401 body carries no fingerprint of whether the user_id exists.
        """
        monkeypatch.setenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", "enforce")
        token_calls: list[str] = []

        def _track_token_fn(u: str):
            token_calls.append(u)
            return MagicMock(user_id=u, token="should-not-leak", repos=None)

        captured: list[dict] = []
        monkeypatch.setattr(
            "watercooler_mcp.observability.log_action",
            lambda action, **kwargs: captured.append({"action": action, **kwargs}),
        )

        req = _make_request(headers={"X-User-ID": "victim-id"})
        result = await _stage_authenticate(
            req,
            "rid-enforce",
            is_hosted=True,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=_track_token_fn,
            extract_context_fn=_make_extract_context(user_id="victim-id"),
        )
        assert isinstance(result, JSONResponse)
        assert result.status_code == 401
        # Load-bearing: token service must NOT be called in enforce
        # mode — the rejection is meant to short-circuit the privileged
        # lookup, not paper over it post-hoc.
        assert token_calls == [], (
            f"token service was called in enforce mode (got {token_calls!r}); "
            "the gate must reject BEFORE the token lookup"
        )
        # Telemetry must fire in enforce mode too so operators can see
        # how many would-be impersonation attempts the gate rejected.
        assert any(
            entry["action"] == "hosted_identity_auth_used"
            and entry.get("mode") == "enforce"
            and entry.get("user_id") == "victim-id"
            for entry in captured
        ), f"enforce-mode telemetry missing; captured={captured!r}"

    @pytest.mark.anyio
    async def test_hosted_identity_enforce_truthy_aliases_all_gate(
        self, monkeypatch
    ):
        """All documented truthy aliases for ``enforce`` reject identity.

        Pins the contract from ``_identity_auth_mode``: the env var
        accepts ``"enforce"`` plus the truthy aliases ``"1"`` / ``"true"``
        / ``"yes"`` / ``"on"`` (convention inherited from
        ``repo_claim_mode``). Documented in the docstring per PR #748
        review LOW. Without this test a future refactor that narrows
        the alias set would silently re-open the gate for any operator
        whose secret-management tool emits ``=true`` instead of
        ``=enforce``.
        """
        for alias in ("enforce", "1", "true", "yes", "on", "ENFORCE", "TRUE"):
            monkeypatch.setenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", alias)
            req = _make_request(headers={"X-User-ID": "victim-id"})
            result = await _stage_authenticate(
                req,
                f"rid-{alias}",
                is_hosted=True,
                resolve_api_key_fn=lambda k: None,
                get_github_token_fn=lambda u: None,
                extract_context_fn=_make_extract_context(user_id="victim-id"),
            )
            assert isinstance(result, JSONResponse), f"alias {alias!r}"
            assert result.status_code == 401, f"alias {alias!r}"

    def test_identity_auth_mode_unrecognised_value_logs_warn_once(
        self, monkeypatch
    ):
        """Typo / unrecognised value defaults to warn AND logs a WARNING.

        PR #748 review HIGH: an operator who typos the env var (e.g.,
        ``"enforec"`` instead of ``"enforce"``, or sets it to a stale
        ``"false"`` / ``"0"`` from an unrelated boolean flag) would
        previously fall through to warn-mode silently — the gate they
        believe is active is not. The fix logs a one-shot WARNING
        naming the bad value so the misconfiguration surfaces in
        deploy logs immediately.

        Note on the test mechanics: caplog cannot reliably observe
        ``watercooler_mcp.server_http.logger`` records because
        ``observability.log_action`` lazily flips
        ``watercooler_mcp.propagate`` to False when a file handler is
        configured (documented in ``auth/__init__.py:285-291``). Once
        propagate is False, caplog (which attaches at the root logger
        by default) sees nothing. Capturing the logger's ``warning``
        method directly is propagation-agnostic and tests the actual
        invariant we care about: "WARNING was emitted with the bad
        value named, exactly once."
        """
        from watercooler_mcp import server_http as _sh

        # Reset the warn-once cache so the test sees a fresh emission
        # regardless of whatever earlier tests in this process may have
        # logged. Direct module attribute access is the documented
        # test-reset hook (no public reset API; the cache is process-
        # scoped by design).
        _sh._identity_auth_unknown_value_warned.clear()

        captured: list[tuple[str, tuple]] = []

        def _capture_warning(msg, *args, **kwargs):
            captured.append((msg, args))

        monkeypatch.setattr(_sh.logger, "warning", _capture_warning)

        # First call with bad value: emits WARNING and falls through
        # to warn semantics.
        monkeypatch.setenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", "enforec")
        assert _sh._identity_auth_mode() == "warn"
        assert len(captured) == 1, f"expected 1 WARNING, got {captured!r}"
        msg, args = captured[0]
        assert "WATERCOOLER_REQUIRE_HMAC_OR_BEARER" in msg
        assert "not a recognised value" in msg
        assert "enforec" in args, (
            f"bad value not named in WARNING: msg={msg!r}, args={args!r}"
        )

        # Second call with the SAME bad value: cache suppresses (warn-once).
        # Without this, a misconfigured env var would spam at request rate.
        captured.clear()
        assert _sh._identity_auth_mode() == "warn"
        assert captured == [], (
            f"warn-once cache failed; subsequent call re-emitted: {captured!r}"
        )

        # A DIFFERENT bad value re-fires the WARNING (each typo is
        # independently surfaced).
        captured.clear()
        monkeypatch.setenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", "false")
        assert _sh._identity_auth_mode() == "warn"
        assert len(captured) == 1, (
            f"distinct bad value should re-warn; got {captured!r}"
        )
        assert "false" in captured[0][1]

    def test_identity_auth_mode_recognised_values_no_unknown_warning(
        self, monkeypatch
    ):
        """Recognised values (``warn``/``enforce`` + truthy aliases +
        empty/unset) do NOT emit the unknown-value WARNING. Defends
        against a future refactor that accidentally treats a normal
        default value as "unrecognised" and floods deploy logs at
        request rate, AND pins the docstring's accepted alias set.

        PR #748 review LOW: the truthy aliases (``"1"``, ``"true"``,
        ``"yes"``, ``"on"``) are an undocumented widening of the input
        contract inherited from ``repo_claim_mode``. Documenting them
        in the helper docstring AND pinning them with a test prevents
        a future tightening from silently re-opening the gate for any
        operator whose secret-management tool emits ``=true`` instead
        of ``=enforce``.
        """
        from watercooler_mcp import server_http as _sh

        _sh._identity_auth_unknown_value_warned.clear()

        captured: list[tuple[str, tuple]] = []

        def _capture_warning(msg, *args, **kwargs):
            captured.append((msg, args))

        monkeypatch.setattr(_sh.logger, "warning", _capture_warning)

        # warn / empty / case variants → mode "warn", no warning.
        for value, expected in (
            ("warn", "warn"),
            ("", "warn"),
            ("WARN", "warn"),
            ("  warn  ", "warn"),  # ``.strip()`` keeps these recognised
            # truthy aliases → mode "enforce", no warning.
            ("enforce", "enforce"),
            ("1", "enforce"),
            ("true", "enforce"),
            ("yes", "enforce"),
            ("on", "enforce"),
            ("ENFORCE", "enforce"),
            ("True", "enforce"),
        ):
            captured.clear()
            monkeypatch.setenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", value)
            assert _sh._identity_auth_mode() == expected, f"value {value!r}"
            unrecognised = [
                m for m, _a in captured if "not a recognised value" in m
            ]
            assert unrecognised == [], (
                f"value {value!r} unexpectedly produced unknown-value "
                f"WARNING: {unrecognised!r}"
            )

        # Unset env (no var present) → "warn", no warning.
        captured.clear()
        monkeypatch.delenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", raising=False)
        assert _sh._identity_auth_mode() == "warn"
        assert captured == [], (
            f"unset env unexpectedly produced WARNING: {captured!r}"
        )

    @pytest.mark.anyio
    async def test_hosted_identity_enforce_does_not_block_bearer(
        self, monkeypatch
    ):
        """enforce mode does NOT block a valid Bearer-authenticated
        request. The gate is identity-mode-specific.

        Defense-in-depth check: if a future refactor accidentally moved
        the gate above the Bearer branch (or made it a top-level
        rejection), every dashboard request would 401 the moment
        ``WATERCOOLER_REQUIRE_HMAC_OR_BEARER=enforce`` flips. This test
        pins the scope.
        """
        monkeypatch.setenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", "enforce")
        token_info = MagicMock(
            user_id="bearer-user",
            token="ghp_bearer",
            repos={"org/repo"},
            capabilities=None,
        )
        req = _make_request(
            headers={"Authorization": "Bearer agent-key", "X-Repo": "org/repo"}
        )
        result = await _stage_authenticate(
            req,
            "rid-bearer-enforce",
            is_hosted=True,
            resolve_api_key_fn=lambda k: token_info if k == "agent-key" else None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "bearer"
        assert result.user_id == "bearer-user"

    @pytest.mark.anyio
    async def test_local_mode_skip(self):
        """Local mode + no auth → skip."""
        req = _make_request(headers={})
        result = await _stage_authenticate(
            req,
            "rid",
            is_hosted=False,
            resolve_api_key_fn=lambda k: None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "skip"

    @pytest.mark.anyio
    async def test_bearer_bypasses_other_paths(self):
        """Valid Bearer succeeds without any HMAC headers."""
        token_info = MagicMock(user_id="agent-1", token="ghp_agent", repos={"org/repo"}, capabilities=None)
        req = _make_request(
            headers={"Authorization": "Bearer agent-key", "X-Repo": "org/repo"}
        )
        result = await _stage_authenticate(
            req,
            "rid",
            is_hosted=True,
            resolve_api_key_fn=lambda k: token_info if k == "agent-key" else None,
            get_github_token_fn=lambda u: None,
            extract_context_fn=_make_extract_context(),
        )
        assert isinstance(result, _AuthResult)
        assert result.mode == "bearer"


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
