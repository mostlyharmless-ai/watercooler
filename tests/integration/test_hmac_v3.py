"""Integration tests for HMAC v3 inside the FastAPI auth middleware.

Covers:
- H6-H8 v2→v3 migration modes (warn / enforce; both schemes
  coexist during the transition)
- H9 replay-window timestamp enforcement
- End-to-end repo-authorization for service keys (H4-H5)

Pure-primitive tests (signature integrity, header parsing,
subject-binding, registry, fail-fast) live in
``tests/unit/test_hmac_v3_primitives.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from watercooler_mcp.auth.hmac_keys import (
    KeyInfo,
    KeyRegistry,
    build_v3_canonical_string,
)
from watercooler_mcp.server_http import _stage_authenticate, _AuthResult


# ------------------------------------------------------------------ #
# Test scaffolding — fake Request / context / token resolution
# ------------------------------------------------------------------ #


@dataclass
class _FakeURL:
    path: str = "/mcp/"


class _FakeRequest:
    """Minimal duck-typed Request for ``_stage_authenticate``."""

    def __init__(
        self,
        *,
        headers: dict[str, str],
        body: bytes = b"{}",
        method: str = "POST",
        path: str = "/mcp/",
    ) -> None:
        self.headers = headers
        self._body = body
        self.method = method
        self.url = _FakeURL(path=path)
        self.query_params: dict[str, str] = {}

    async def body(self) -> bytes:
        return self._body


def _fake_extract_context(headers: dict, query: dict) -> Any:
    return SimpleNamespace(
        user_id=headers.get("X-User-ID", ""),
        repo=headers.get("X-Repo", ""),
        branch=headers.get("X-Branch", ""),
    )


def _fake_resolve_api_key(api_key: str) -> Optional[Any]:
    # Bearer not under test in this file; return None so the v3 path
    # is exercised when Authorization isn't a Bearer.
    return None


def _fake_get_github_token(user_id: str) -> Optional[Any]:
    """Fake: any user has a token whose ``repos`` claim matches the user."""
    return SimpleNamespace(
        user_id=user_id,
        token=f"ghp_{user_id}_token",
        github_username=user_id,
        expires_at=None,
        capabilities=None,
        repos=frozenset({"org/repo", "org/other"}),
    )


def _sign_v3(
    *,
    key_id: str,
    secret: bytes,
    method: str,
    path: str,
    timestamp: str,
    user_id: str,
    body: bytes,
    x_repo: str,
    x_branch: str,
) -> str:
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
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _call_auth(
    request: _FakeRequest,
    *,
    registry: Optional[KeyRegistry] = None,
    require_v3: str = "warn",
    is_hosted: bool = True,
) -> Any:
    """Sync wrapper around the async ``_stage_authenticate``.

    Each test gets its own event loop via :func:`asyncio.run`, so the
    suite remains usable without ``pytest-asyncio``.

    Issue #733 deleted the legacy v1/v2 ``internal_secret`` parameter
    from ``_stage_authenticate``; this helper no longer accepts one.
    """
    return asyncio.run(
        _stage_authenticate(
            request,
            request_id="req-test",
            is_hosted=is_hosted,
            resolve_api_key_fn=_fake_resolve_api_key,
            get_github_token_fn=_fake_get_github_token,
            extract_context_fn=_fake_extract_context,
            hmac_registry=registry,
            require_v3=require_v3,
        )
    )


# ------------------------------------------------------------------ #
# H6/H7/H8 — v2 and v3 coexistence during migration
# ------------------------------------------------------------------ #


class TestV3MigrationCoexistence:
    def test_v3_request_succeeds_when_registry_has_key(self) -> None:
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="alice-key",
                secret=b"alice-secret",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        ts = _now_iso()
        body = b'{"jsonrpc":"2.0","method":"tools/list","params":{}}'
        sig = _sign_v3(
            key_id="alice-key",
            secret=b"alice-secret",
            method="POST",
            path="/mcp/",
            timestamp=ts,
            user_id="alice",
            body=body,
            x_repo="org/repo",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=alice-key sig={sig}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
                "X-Branch": "main",
            },
            body=body,
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert isinstance(result, _AuthResult)
        assert result.user_id == "alice"
        assert result.repo == "org/repo"
        assert result.mode == "hmac"

    def test_v3_signature_invalid_returns_401(self) -> None:
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="alice-key",
                secret=b"alice-secret",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        ts = _now_iso()
        request = _FakeRequest(
            headers={
                "Authorization": "HMAC-SHA256 v=3 kid=alice-key sig=" + "00" * 32,
                "X-Request-Timestamp": ts,
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"hello",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        # JSONResponse-ish; not an _AuthResult
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401

    def test_v3_no_registry_returns_generic_401(self) -> None:
        # PR #703 round 6 LOW: previously returned 503 +
        # "HMAC v3 registry not configured", which let an
        # unauthenticated probe distinguish "deployed without
        # registry" from "deployed and rejected my creds". Now
        # collapsed to the same generic 401 + message every
        # other v3 pre-auth failure uses.
        ts = _now_iso()
        sig = _sign_v3(
            key_id="kid",
            secret=b"s",
            method="POST",
            path="/mcp/",
            timestamp=ts,
            user_id="alice",
            body=b"",
            x_repo="org/repo",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=kid sig={sig}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=None, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401
        body = result.body.decode("utf-8")
        assert "Invalid or missing" in body

    def test_v3_unknown_key_returns_401(self) -> None:
        # H14: registry doesn't have this key_id
        registry = KeyRegistry()
        ts = _now_iso()
        sig = _sign_v3(
            key_id="never-registered",
            secret=b"s",
            method="POST",
            path="/mcp/",
            timestamp=ts,
            user_id="alice",
            body=b"",
            x_repo="org/repo",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=never-registered sig={sig}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401


# ------------------------------------------------------------------ #
# H10 — cross-subject assertion blocked via the auth pipeline
# ------------------------------------------------------------------ #


class TestCrossSubjectAssertionBlockedEndToEnd:
    def test_alice_key_asserting_bob_user_id_blocked(self) -> None:
        # H10 applied at the auth pipeline: even though the signature
        # is valid for the canonical string (which mentions "bob"),
        # the key is bound to alice and rejects.
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="alice-key",
                secret=b"alice-secret",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        ts = _now_iso()
        sig = _sign_v3(
            key_id="alice-key",
            secret=b"alice-secret",
            method="POST",
            path="/mcp/",
            timestamp=ts,
            user_id="bob",  # signed for bob
            body=b"",
            x_repo="org/repo",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=alice-key sig={sig}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "bob",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401


# ------------------------------------------------------------------ #
# H9 — timestamp replay window
# ------------------------------------------------------------------ #


class TestTimestampReplayWindow:
    def test_old_timestamp_rejected(self) -> None:
        # H9: timestamp older than HMAC_WINDOW (default 300s) → 401
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="alice-key",
                secret=b"alice-secret",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        old = (
            (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        sig = _sign_v3(
            key_id="alice-key",
            secret=b"alice-secret",
            method="POST",
            path="/mcp/",
            timestamp=old,
            user_id="alice",
            body=b"",
            x_repo="org/repo",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=alice-key sig={sig}",
                "X-Request-Timestamp": old,
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401
        body_str = (
            result.body.decode("utf-8")
            if isinstance(result.body, bytes)
            else str(result.body)
        )
        assert "expired" in body_str.lower()

    def test_missing_timestamp_rejected(self) -> None:
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="alice-key",
                secret=b"alice-secret",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        # Body irrelevant when timestamp absent — fail-fast
        request = _FakeRequest(
            headers={
                "Authorization": "HMAC-SHA256 v=3 kid=alice-key sig=" + "00" * 32,
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401


# ------------------------------------------------------------------ #
# H4-H5 service key path
# ------------------------------------------------------------------ #


class TestServiceKey:
    def test_service_key_with_correct_repo_allows(self) -> None:
        # H4
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="dashboard",
                secret=b"svc-secret",
                key_type="service",
                service_identity="dashboard",
                delegation_allow_list=None,  # no_user_delegation
                repo_allow_list=frozenset({"org/repo"}),
            )
        )
        ts = _now_iso()
        sig = _sign_v3(
            key_id="dashboard",
            secret=b"svc-secret",
            method="POST",
            path="/mcp/",
            timestamp=ts,
            user_id="dashboard",
            body=b"",
            x_repo="org/repo",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=dashboard sig={sig}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "dashboard",
                "X-Repo": "org/repo",
                "X-Branch": "main",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="enforce")
        assert isinstance(result, _AuthResult)
        assert result.user_id == "dashboard"

    def test_service_key_with_wrong_repo_denied_in_enforce(self) -> None:
        # H5 in enforce-mode: 403
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="dashboard",
                secret=b"svc-secret",
                key_type="service",
                service_identity="dashboard",
                delegation_allow_list=None,
                repo_allow_list=frozenset({"org/A"}),
            )
        )
        ts = _now_iso()
        sig = _sign_v3(
            key_id="dashboard",
            secret=b"svc-secret",
            method="POST",
            path="/mcp/",
            timestamp=ts,
            user_id="dashboard",
            body=b"",
            x_repo="org/B",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=dashboard sig={sig}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "dashboard",
                "X-Repo": "org/B",
                "X-Branch": "main",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="enforce")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 403

    def test_service_key_with_wrong_repo_warned_only_in_warn(self) -> None:
        # warn-mode: log + accept (Sprint 2 observation window)
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="dashboard",
                secret=b"svc-secret",
                key_type="service",
                service_identity="dashboard",
                delegation_allow_list=None,
                repo_allow_list=frozenset({"org/A"}),
            )
        )
        ts = _now_iso()
        sig = _sign_v3(
            key_id="dashboard",
            secret=b"svc-secret",
            method="POST",
            path="/mcp/",
            timestamp=ts,
            user_id="dashboard",
            body=b"",
            x_repo="org/B",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=dashboard sig={sig}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "dashboard",
                "X-Repo": "org/B",
                "X-Branch": "main",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        # warn-mode accepts despite the repo mismatch
        assert isinstance(result, _AuthResult)
        assert result.user_id == "dashboard"


class TestServiceKeyEmptyAllowListFailsClosed:
    """The HIGH finding from PR #703 review.

    A service key deployed without ``WATERCOOLER_HMAC_KEY_<id>_REPOS``
    must NEVER authenticate against any X-Repo, even in warn-mode.
    Honouring warn-mode for an empty allow-list would invert the
    security posture: an operator misconfiguration would silently
    grant universal access. ``RepoAuthError.fatal=True`` flags this
    case and is rejected unconditionally.
    """

    def _build(
        self,
        repo_allow_list: Optional[frozenset[str]],
    ) -> KeyRegistry:
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="dashboard",
                secret=b"svc-secret",
                key_type="service",
                service_identity="dashboard",
                delegation_allow_list=None,
                repo_allow_list=repo_allow_list,
            )
        )
        return registry

    def _sign_request(self, x_repo: str, x_branch: str = "main") -> _FakeRequest:
        ts = _now_iso()
        sig = _sign_v3(
            key_id="dashboard",
            secret=b"svc-secret",
            method="POST",
            path="/mcp/",
            timestamp=ts,
            user_id="dashboard",
            body=b"",
            x_repo=x_repo,
            x_branch=x_branch,
        )
        return _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=dashboard sig={sig}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "dashboard",
                "X-Repo": x_repo,
                "X-Branch": x_branch,
            },
            body=b"",
        )

    def test_empty_allow_list_rejects_in_warn_mode(self) -> None:
        registry = self._build(repo_allow_list=frozenset())
        result = _call_auth(
            self._sign_request("org/repo"), registry=registry, require_v3="warn"
        )
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 403

    def test_none_allow_list_rejects_in_warn_mode(self) -> None:
        registry = self._build(repo_allow_list=None)
        result = _call_auth(
            self._sign_request("org/repo"), registry=registry, require_v3="warn"
        )
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 403

    def test_empty_allow_list_rejects_in_enforce_mode(self) -> None:
        registry = self._build(repo_allow_list=frozenset())
        result = _call_auth(
            self._sign_request("org/repo"), registry=registry, require_v3="enforce"
        )
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 403


class TestKeyIdEnumerationOracleClosed:
    """The MEDIUM finding from PR #703 review.

    Sending an unknown ``kid`` with no timestamp must produce the
    same response as sending a known ``kid`` with no timestamp. If
    the two outcomes differ, an unauthenticated probe can enumerate
    registered key_ids by sending crafted requests and reading back
    the error message.
    """

    def test_unknown_kid_no_timestamp_returns_generic_401(self) -> None:
        # Empty registry
        registry = KeyRegistry()
        request = _FakeRequest(
            headers={
                "Authorization": "HMAC-SHA256 v=3 kid=probe-kid sig=" + "00" * 32,
                # NO X-Request-Timestamp
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401
        body_str = result.body.decode("utf-8")
        # Generic 401 — does NOT mention "Unknown" or "key" specifically
        assert "Invalid or missing" in body_str

    def test_known_kid_no_timestamp_returns_same_generic_401(self) -> None:
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="real-kid",
                secret=b"s",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        request = _FakeRequest(
            headers={
                "Authorization": "HMAC-SHA256 v=3 kid=real-kid sig=" + "00" * 32,
                # NO X-Request-Timestamp
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401
        body_str = result.body.decode("utf-8")
        assert "Invalid or missing" in body_str

    def test_authorization_hmac_v2_version_rejects_does_not_fall_through(
        self,
    ) -> None:
        """PR #703 round 4 MED: control-flow tightening.

        ``Authorization: HMAC-SHA256`` is a v3-exclusive indicator.
        Issue #733 deleted the legacy v2 verifier, so a malformed v3
        Authorization header MUST 401 explicitly — there is no
        fallback path that could accidentally accept the request.
        """
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="alice-key",
                secret=b"alice-secret",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        # ``v=2`` in a v3-shaped header — parse rejects.
        request = _FakeRequest(
            headers={
                "Authorization": "HMAC-SHA256 v=2 kid=alice-key sig=" + "00" * 32,
                "X-Request-Timestamp": _now_iso(),
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401

    def test_authorization_hmac_with_malformed_kid_does_not_fall_through(
        self,
    ) -> None:
        registry = KeyRegistry()
        # ``kid`` contains a literal newline — rejected at parse.
        request = _FakeRequest(
            headers={
                "Authorization": ("HMAC-SHA256 v=3 kid=evil\nkid sig=" + "00" * 32),
                "X-Request-Timestamp": _now_iso(),
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401

    def test_invalid_signature_returns_same_generic_401(self) -> None:
        # PR #703 round 6 MED: previously the invalid-signature
        # path returned ``"Invalid HMAC v3 signature"``, distinct
        # from ``"Invalid or missing HMAC v3 credentials"`` used
        # by the unknown-kid path. That let an unauthenticated
        # probe with any valid timestamp fingerprint registered
        # keys by sending bogus signatures and reading the
        # response body. Both paths now use the same generic
        # message; the detail lives in telemetry only.
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="real-kid",
                secret=b"s",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        request = _FakeRequest(
            headers={
                "Authorization": "HMAC-SHA256 v=3 kid=real-kid sig=" + "00" * 32,
                "X-Request-Timestamp": _now_iso(),
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401
        body = result.body.decode("utf-8")
        # Generic message — does NOT mention "signature" specifically.
        assert "Invalid or missing" in body
        assert "signature" not in body.lower()

    def test_v3_replay_window_check_lives_inside_attempt(self) -> None:
        # PR #703 round 7+3 MED: ``_attempt_hmac_v3_auth`` previously
        # had an undocumented precondition — replay-window check
        # done by the outer ``_stage_authenticate``. A future caller
        # invoking the function in isolation (WebSocket, refactored
        # auth path, test helper) would skip replay protection.
        # Verify a stale timestamp produces 401 with the explicit
        # timestamp error message; pins the inner-function contract.
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="alice-key",
                secret=b"alice-secret",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        stale_ts = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=1)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        body = b""
        sig = _sign_v3(
            key_id="alice-key",
            secret=b"alice-secret",
            method="POST",
            path="/mcp/",
            timestamp=stale_ts,
            user_id="alice",
            body=body,
            x_repo="org/repo",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=alice-key sig={sig}",
                "X-Request-Timestamp": stale_ts,
                "X-User-ID": "alice",
                "X-Repo": "org/repo",
                "X-Branch": "main",
            },
            body=body,
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401
        body_text = result.body.decode("utf-8")
        assert "timestamp" in body_text.lower() or "expired" in body_text.lower()

    def test_v3_canonical_field_newline_returns_generic_401(self) -> None:
        # PR #703 round 7+3 LOW: a CR/LF in X-User-ID / X-Repo /
        # X-Branch would shift downstream fields in the canonical
        # string. ``build_v3_canonical_string`` rejects with
        # ValueError; the auth path catches and returns the same
        # generic 401 used by every other v3 pre-auth failure.
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="alice-key",
                secret=b"alice-secret",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        ts = _now_iso()
        # The injected newline is what trips the guard; signature
        # value doesn't matter because canonical-build raises before
        # verification runs.
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=alice-key sig={'a' * 64}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "alice",
                "X-Repo": "org/repo\nmain",
                "X-Branch": "main",
            },
            body=b"",
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401
        assert "Invalid or missing" in result.body.decode("utf-8")

    def test_empty_user_id_returns_generic_401_not_403(self) -> None:
        # PR #703 round 7+1 LOW: a valid signature over an empty
        # ``X-User-ID`` paired with the legacy global key (which has
        # ``bound_user_id=None`` wildcard) previously passed
        # subject-binding, skipped the token fetch, and returned 403
        # "No GitHub token found for user". Every other pre-auth
        # failure returned 401. The asymmetry let a holder of the
        # global secret distinguish "valid signature over empty
        # user_id" from "invalid sig" by status code. Now both paths
        # return the same generic 401 and an attacker cannot
        # fingerprint signature validity through this side channel.
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="legacy-global",
                secret=b"global-secret",
                key_type="per_user",
                bound_user_id=None,  # wildcard back-compat
            )
        )
        ts = _now_iso()
        body = b""
        sig = _sign_v3(
            key_id="legacy-global",
            secret=b"global-secret",
            method="POST",
            path="/mcp/",
            timestamp=ts,
            user_id="",  # empty
            body=body,
            x_repo="org/repo",
            x_branch="main",
        )
        request = _FakeRequest(
            headers={
                "Authorization": f"HMAC-SHA256 v=3 kid=legacy-global sig={sig}",
                "X-Request-Timestamp": ts,
                "X-User-ID": "",
                "X-Repo": "org/repo",
                "X-Branch": "main",
            },
            body=body,
        )
        result = _call_auth(request, registry=registry, require_v3="warn")
        assert not isinstance(result, _AuthResult)
        assert result.status_code == 401
        assert "Invalid or missing" in result.body.decode("utf-8")
        # And NOT 403 with the no-token message:
        assert "No GitHub token" not in result.body.decode("utf-8")

    def test_responses_are_indistinguishable(self) -> None:
        """Bytes-level: an attacker probing with different kids and no
        timestamp should not be able to distinguish responses by message
        content."""
        empty_registry = KeyRegistry()
        populated_registry = KeyRegistry()
        populated_registry.add(
            KeyInfo(
                key_id="real-kid",
                secret=b"s",
                key_type="per_user",
                bound_user_id="alice",
            )
        )

        def _probe(registry: KeyRegistry, kid: str) -> bytes:
            request = _FakeRequest(
                headers={
                    "Authorization": f"HMAC-SHA256 v=3 kid={kid} sig={'00' * 32}",
                    "X-User-ID": "alice",
                    "X-Repo": "org/repo",
                },
                body=b"",
            )
            return _call_auth(request, registry=registry, require_v3="warn").body

        # Same response body for: empty registry + any kid, populated
        # registry + unknown kid, populated registry + known kid.
        a = _probe(empty_registry, "probe-kid")
        b = _probe(populated_registry, "probe-kid")
        c = _probe(populated_registry, "real-kid")
        assert a == b == c
