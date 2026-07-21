"""Tests for the proxy startup auth preflight (#1117).

Contract: in proxy mode, a definitive 4xx from the hosted endpoint at
startup (bad API key, ``repo_claim_mismatch``) exits with the backend's
own error body on stderr — instead of letting FastMCP's proxy flatten it
to a bare JSON-RPC -32603 with the actionable body discarded. Anything
that is not a definitive 4xx (network failure, 5xx) warns and lets the
proxy start, because per-request sessions can recover on their own.
"""

from __future__ import annotations

import pytest

from watercooler_mcp.server import _preflight_hosted_auth

_REPO = "koan-analytics/proposal-intertek"

_CLAIM_MISMATCH_BODY = {
    "error": (
        f"repo_claim_mismatch: X-Repo '{_REPO}' is not in the token's "
        "authorised ``repos`` claim. Authorised: "
        "[koan-analytics/koan-nlp]. The token-issuing service determines "
        "which repos this token may act on; X-Repo can only narrow "
        "within that set."
    )
}


class _FakeResponse:
    def __init__(self, status_code, *, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class _HTTPStatusErrorLike(Exception):
    """Mimics httpx.HTTPStatusError: carries a ``.response``."""

    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


class _FakeSession:
    """Async-CM session whose connect handshake raises ``connect_exc``."""

    def __init__(self, connect_exc=None):
        self._connect_exc = connect_exc
        self.pinged = False
        self.force_disconnected = False

    async def __aenter__(self):
        if self._connect_exc is not None:
            raise self._connect_exc
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def ping(self):
        self.pinged = True
        return True

    async def _disconnect(self, force=False):
        self.force_disconnected = True


class _FakeClient:
    """Mimics the disconnected fastmcp Client: ``new()`` yields a session."""

    def __init__(self, connect_exc=None):
        self.session = _FakeSession(connect_exc)

    def new(self):
        return self.session


def _http_403(body=_CLAIM_MISMATCH_BODY):
    return _HTTPStatusErrorLike(
        "Client error '403 Forbidden' for url '.../mcp/'",
        _FakeResponse(403, body=body),
    )


def test_403_claim_mismatch_exits_with_backend_body_and_cache_hint(capsys):
    with pytest.raises(SystemExit) as exc_info:
        _preflight_hosted_auth(_FakeClient(_http_403()), _REPO)

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "HTTP 403" in err
    assert "repo_claim_mismatch" in err
    assert _REPO in err
    assert "koan-analytics/koan-nlp" in err  # the authorised list survives
    assert "5 minutes" in err  # token-cache hint
    assert "dashboard" in err


def test_wrapped_403_is_unwrapped(capsys):
    """The mcp SDK chains/groups the httpx error — the body must still
    surface (same walk as the hybrid path's summarize_http_error)."""
    outer = RuntimeError("transport wrapper")
    outer.__cause__ = _http_403()
    with pytest.raises(SystemExit):
        _preflight_hosted_auth(_FakeClient(outer), _REPO)
    assert "repo_claim_mismatch" in capsys.readouterr().err


def test_401_exits_without_cache_hint(capsys):
    exc = _HTTPStatusErrorLike(
        "Client error '401 Unauthorized' for url '.../mcp/'",
        _FakeResponse(401, body={"error": "invalid or expired API key"}),
    )
    with pytest.raises(SystemExit):
        _preflight_hosted_auth(_FakeClient(exc), _REPO)

    err = capsys.readouterr().err
    assert "HTTP 401" in err
    assert "invalid or expired API key" in err
    assert "5 minutes" not in err


def test_4xx_without_body_falls_back_to_str_exc(capsys):
    exc = _HTTPStatusErrorLike(
        "Client error '403 Forbidden' for url '.../mcp/'",
        _FakeResponse(403),  # no JSON body, empty text
    )
    with pytest.raises(SystemExit):
        _preflight_hosted_auth(_FakeClient(exc), _REPO)
    assert "403 Forbidden" in capsys.readouterr().err


def test_5xx_warns_and_does_not_exit(capsys):
    exc = _HTTPStatusErrorLike(
        "Server error '502 Bad Gateway' for url '.../mcp/'",
        _FakeResponse(502, text="upstream boom"),
    )
    _preflight_hosted_auth(_FakeClient(exc), _REPO)

    err = capsys.readouterr().err
    assert "Warning" in err
    assert "starting the proxy anyway" in err


def test_network_failure_warns_and_does_not_exit(capsys):
    _preflight_hosted_auth(_FakeClient(OSError("connection refused")), _REPO)

    err = capsys.readouterr().err
    assert "Warning" in err
    assert "connection refused" in err


def test_success_is_silent_and_tears_down(capsys):
    client = _FakeClient()
    _preflight_hosted_auth(client, _REPO)

    assert client.session.pinged
    assert client.session.force_disconnected
    assert capsys.readouterr().err == ""
