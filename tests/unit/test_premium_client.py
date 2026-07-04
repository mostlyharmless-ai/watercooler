"""Unit tests for ``PremiumToolClient.call_tool_text`` error surfacing.

The hosted endpoint returns actionable 4xx bodies (e.g. ``repo_claim_mismatch``
naming the unauthorised X-Repo and the caller's authorised repo set). The
hybrid wrapper must surface that body to the caller instead of collapsing the
failure to a bare ``"Client error '403 Forbidden'"`` status line.
"""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from watercooler_mcp.premium_client import PremiumToolClient


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


class _SessionFake:
    """Async-CM session fake; ``new()`` returns a fresh session per call.

    Mirrors the FastMCP idiom the wrapper now uses: ``call_tool_text`` calls
    ``self._client.new()`` then enters it as an async context manager and
    force-disconnects in a ``finally``.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def new(self):
        return self

    def is_connected(self):
        return False

    async def _disconnect(self, force=False):
        return None


class _RaisingClient(_SessionFake):
    """Session fake whose ``call_tool`` raises."""

    def __init__(self, exc):
        self._exc = exc

    async def call_tool(self, name, arguments, *, timeout=None):
        raise self._exc


class _TextClient(_SessionFake):
    def __init__(self, text):
        self._text = text

    async def call_tool(self, name, arguments, *, timeout=None):
        content = type("_C", (), {"text": self._text})()
        return type("_R", (), {"content": [content]})()


def _client(raising_exc):
    return PremiumToolClient(_RaisingClient(raising_exc))


@pytest.mark.anyio
async def test_403_surfaces_remote_error_body():
    body = {
        "error": (
            "repo_claim_mismatch: X-Repo 'koan-analytics/koan-geo' is not in "
            "the token's authorised repos claim. Authorised: "
            "[koan-analytics/koan-nlp]."
        )
    }
    exc = _HTTPStatusErrorLike(
        "Client error '403 Forbidden' for url '.../mcp/premium/'",
        _FakeResponse(403, body=body),
    )
    out = json.loads(await _client(exc).call_tool_text("watercooler_search", {"q": "x"}))

    assert out["error"] == "remote_call_failed"
    assert out["status_code"] == 403
    assert "repo_claim_mismatch" in out["remote_error"]
    assert "koan-analytics/koan-geo" in out["remote_error"]


@pytest.mark.anyio
async def test_remote_error_on_exception_cause_is_surfaced():
    inner = _HTTPStatusErrorLike(
        "boom", _FakeResponse(403, body={"error": "Repo not authorized"})
    )
    outer = RuntimeError("wrapped transport error")
    outer.__cause__ = inner
    out = json.loads(await _client(outer).call_tool_text("t", {}))

    assert out["status_code"] == 403
    assert out["remote_error"] == "Repo not authorized"


@pytest.mark.anyio
async def test_non_http_exception_keeps_generic_shape():
    out = json.loads(await _client(ValueError("plain failure")).call_tool_text("t", {}))

    assert out["error"] == "remote_call_failed"
    assert out["message"] == "plain failure"
    assert "status_code" not in out
    assert "remote_error" not in out


@pytest.mark.anyio
async def test_non_json_body_falls_back_to_response_text():
    exc = _HTTPStatusErrorLike("err", _FakeResponse(502, text="upstream boom"))
    out = json.loads(await _client(exc).call_tool_text("t", {}))

    assert out["status_code"] == 502
    assert out["remote_error"] == "upstream boom"


@pytest.mark.anyio
async def test_success_path_returns_text_unchanged():
    out = await PremiumToolClient(_TextClient("hello")).call_tool_text("t", {})
    assert out == "hello"


# --- Real-stack exception shapes (the mcp SDK raises httpx.HTTPStatusError via
#     response.raise_for_status(), possibly chained or ExceptionGroup-wrapped) ---


def _real_http_status_error(status: int, body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example/mcp/premium/")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{request.url}'",
        request=request,
        response=response,
    )


@pytest.mark.anyio
async def test_real_httpx_status_error_surfaces_body():
    exc = _real_http_status_error(
        403, {"error": "repo_claim_mismatch: X-Repo 'koan-analytics/koan-geo' …"}
    )
    out = json.loads(await _client(exc).call_tool_text("watercooler_search", {}))

    assert out["status_code"] == 403
    assert "repo_claim_mismatch" in out["remote_error"]


@pytest.mark.anyio
async def test_response_on_exception_context_is_surfaced():
    inner = _real_http_status_error(429, {"error": "rate limited"})
    outer = RuntimeError("transport wrapper")
    outer.__context__ = inner  # implicit chaining (no `from`)
    out = json.loads(await _client(outer).call_tool_text("t", {}))

    assert out["status_code"] == 429
    assert out["remote_error"] == "rate limited"


@pytest.mark.anyio
@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup is 3.11+")
async def test_exception_group_wrapped_status_error_is_unwrapped():
    inner = _real_http_status_error(403, {"error": "Repo not authorized"})
    grouped = ExceptionGroup("anyio task group", [inner])  # noqa: F821 (3.11+)
    out = json.loads(await _client(grouped).call_tool_text("t", {}))

    assert out["status_code"] == 403
    assert out["remote_error"] == "Repo not authorized"
