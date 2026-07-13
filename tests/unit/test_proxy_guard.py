"""Tests for the proxy-transport repo-scope guard (amendment A2).

Contract (completion plan v3, thread
audit-transport-modes-hosted-db-2026-07; ratified Decision): in proxy
mode a tool call whose ``code_path``-derived repo slug conflicts with
the session's pinned repo is rejected with a structured
``proxy_repo_mismatch`` error — never silently served from the pinned
repo's graphs. Calls with no derivable repo pass through (pinned-repo
semantics preserved for single-repo use).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from watercooler_mcp.proxy_guard import ProxyRepoScopeMiddleware, _derive_repo

_PINNED = "mostlyharmless-ai/watercooler"
_FOREIGN = "mostlyharmless-ai/watercooler-site"


def _context(tool_name: str = "watercooler_say", **arguments):
    return SimpleNamespace(
        message=SimpleNamespace(name=tool_name, arguments=arguments)
    )


def _mw() -> ProxyRepoScopeMiddleware:
    return ProxyRepoScopeMiddleware(_PINNED)


@pytest.mark.anyio
async def test_conflicting_repo_rejected_with_structured_error() -> None:
    call_next = AsyncMock()
    with patch(
        "watercooler.path_resolver.derive_repo_slug", return_value=_FOREIGN
    ):
        result = await _mw().on_call_tool(
            _context(code_path="/path/to/site"), call_next
        )
    payload = json.loads(result.content[0].text)
    assert payload["error"] == "proxy_repo_mismatch"
    assert payload["pinned_repo"] == _PINNED
    assert payload["requested_repo"] == _FOREIGN
    assert payload["tool"] == "watercooler_say"
    call_next.assert_not_awaited()


@pytest.mark.anyio
async def test_matching_repo_passes_through() -> None:
    call_next = AsyncMock(return_value="forwarded")
    with patch(
        "watercooler.path_resolver.derive_repo_slug", return_value=_PINNED
    ):
        result = await _mw().on_call_tool(
            _context(code_path="/path/to/cloud"), call_next
        )
    assert result == "forwarded"
    call_next.assert_awaited_once()


@pytest.mark.anyio
async def test_repo_comparison_is_case_insensitive() -> None:
    call_next = AsyncMock(return_value="forwarded")
    with patch(
        "watercooler.path_resolver.derive_repo_slug",
        return_value="MostlyHarmless-AI/Watercooler-Cloud",
    ):
        result = await _mw().on_call_tool(
            _context(code_path="/path/to/cloud"), call_next
        )
    assert result == "forwarded"


@pytest.mark.anyio
async def test_absent_code_path_passes_through() -> None:
    call_next = AsyncMock(return_value="forwarded")
    result = await _mw().on_call_tool(_context(), call_next)
    assert result == "forwarded"
    call_next.assert_awaited_once()


@pytest.mark.anyio
async def test_underivable_code_path_passes_through() -> None:
    """The guard only acts on a positively derived repo — a path that
    doesn't resolve (not a git repo, no origin remote) forwards
    unchanged rather than guessing."""
    call_next = AsyncMock(return_value="forwarded")
    with patch(
        "watercooler.path_resolver.derive_repo_slug", return_value=None
    ):
        result = await _mw().on_call_tool(
            _context(code_path="/not/a/repo"), call_next
        )
    assert result == "forwarded"


@pytest.mark.anyio
async def test_derivation_exception_passes_through() -> None:
    call_next = AsyncMock(return_value="forwarded")
    with patch(
        "watercooler.path_resolver.derive_repo_slug",
        side_effect=OSError("git missing"),
    ):
        result = await _mw().on_call_tool(
            _context(code_path="/somewhere"), call_next
        )
    assert result == "forwarded"


@pytest.mark.anyio
async def test_empty_pinned_repo_never_rejects() -> None:
    call_next = AsyncMock(return_value="forwarded")
    mw = ProxyRepoScopeMiddleware("")
    with patch(
        "watercooler.path_resolver.derive_repo_slug", return_value=_FOREIGN
    ):
        result = await mw.on_call_tool(
            _context(code_path="/path/to/site"), call_next
        )
    assert result == "forwarded"


def test_derive_repo_rejects_non_string_input() -> None:
    assert _derive_repo(None) == ""
    assert _derive_repo(123) == ""
    assert _derive_repo("") == ""


def test_derive_repo_real_git_repo(tmp_path) -> None:
    """End-to-end against a real git repo with an origin remote."""
    import subprocess

    repo = tmp_path / "clone"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "git@github.com:example-org/example-repo.git",
        ],
        check=True,
    )
    assert _derive_repo(str(repo)) == "example-org/example-repo"


# ---------------------------------------------------------------------------
# Wave 2 — pool-routed multi-repo proxy (#1082; plan 01KX0AMDMN0R0VX335PQYDW4M3,
# Codex constraints + acceptance additions 01KX0B3EZB166VXBEE87DSWT9G).
# With a pool, a positively-derived foreign repo ROUTES on that repo's
# pooled client (reads AND writes); failures surface as proxy_route_error —
# never a silent fallback to the pinned client. Without a pool the guard
# behavior above is unchanged (the pre-Wave-2 tests run as-is).
# ---------------------------------------------------------------------------

from fastmcp.tools.tool import ToolResult as _ToolResult
from mcp.types import TextContent as _TextContent


class _FakePooledClient:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def call_tool_result(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakePool:
    def __init__(self, client):
        self._client = client
        self.requests = []

    def client_for_repo(self, repo_slug, *, repo_root=None):
        self.requests.append((repo_slug, repo_root))
        if isinstance(self._client, Exception):
            raise self._client
        return self._client


def _routed_mw(client_or_exc) -> tuple[ProxyRepoScopeMiddleware, _FakePool]:
    pool = _FakePool(
        client_or_exc
        if isinstance(client_or_exc, Exception)
        else _FakePooledClient(client_or_exc)
        if not isinstance(client_or_exc, _FakePooledClient)
        else client_or_exc
    )
    return ProxyRepoScopeMiddleware(_PINNED, pool=pool), pool


@pytest.mark.anyio
async def test_derived_foreign_write_routes_on_pooled_client() -> None:
    """Acceptance: a pinned-A session WRITES to repo B when code_path
    derives B — routed, not refused, not silently landed in A."""
    expected = _ToolResult(
        content=[_TextContent(type="text", text='{"ok": true}')]
    )
    mw, pool = _routed_mw(expected)
    call_next = AsyncMock()

    with patch(
        "watercooler.path_resolver.derive_repo_slug", return_value=_FOREIGN
    ):
        result = await mw.on_call_tool(
            _context("watercooler_say", code_path="/path/to/site", body="x"),
            call_next,
        )

    assert result is expected
    call_next.assert_not_awaited()
    # Routed via the derived slug directly (client_for_repo), with the
    # call's repo_root for branch resolution — NOT select_pool_client.
    assert pool.requests == [(_FOREIGN, __import__("pathlib").Path("/path/to/site"))]
    assert pool._client.calls[0][0] == "watercooler_say"


@pytest.mark.anyio
async def test_routed_result_fidelity_multi_content_and_error_state() -> None:
    """Acceptance (non-happy-path fidelity): multi-content, structured
    content, and is_error survive the routed leg verbatim."""
    rich = _ToolResult(
        content=[
            _TextContent(type="text", text="part one"),
            _TextContent(type="text", text="part two"),
        ],
        structured_content={"denied": True, "reason": "unclaimed repo"},
        is_error=True,
    )
    mw, _ = _routed_mw(rich)

    with patch(
        "watercooler.path_resolver.derive_repo_slug", return_value=_FOREIGN
    ):
        result = await mw.on_call_tool(
            _context(code_path="/path/to/site"), AsyncMock()
        )

    assert result is rich
    assert result.is_error is True
    assert len(result.content) == 2
    assert result.structured_content == {"denied": True, "reason": "unclaimed repo"}


@pytest.mark.anyio
async def test_unclaimed_repo_refusal_comes_from_hosted_check_not_fallback() -> None:
    """Acceptance: unclaimed repo C is refused by the hosted ownership
    check (is_error result passed through) — never served from A."""
    hosted_refusal = _ToolResult(
        content=[
            _TextContent(
                type="text",
                text='{"error": "repo_not_authorized", "repo": "other/c"}',
            )
        ],
        is_error=True,
    )
    mw, pool = _routed_mw(hosted_refusal)
    call_next = AsyncMock()

    with patch(
        "watercooler.path_resolver.derive_repo_slug", return_value="other/c"
    ):
        result = await mw.on_call_tool(
            _context(code_path="/path/to/c"), call_next
        )

    assert result is hosted_refusal
    call_next.assert_not_awaited()


@pytest.mark.anyio
async def test_pool_failure_surfaces_no_silent_fallback() -> None:
    """Codex constraint 1: a failure on the routed leg surfaces as
    proxy_route_error — the call is never served by the pinned client."""
    mw, _ = _routed_mw(RuntimeError("pool exploded"))
    call_next = AsyncMock()

    with patch(
        "watercooler.path_resolver.derive_repo_slug", return_value=_FOREIGN
    ):
        result = await mw.on_call_tool(
            _context(code_path="/path/to/site"), call_next
        )

    payload = json.loads(result.content[0].text)
    assert payload["error"] == "proxy_route_error"
    assert "NOT" in payload["message"]
    assert result.is_error is True
    call_next.assert_not_awaited()


@pytest.mark.anyio
async def test_pinned_and_underivable_calls_never_touch_the_pool() -> None:
    """Acceptance: no-code_path / underivable / pinned-repo calls stay on
    the pinned session — the pool is not consulted."""
    mw, pool = _routed_mw(_ToolResult(content=[]))
    call_next = AsyncMock(return_value="forwarded")

    # absent code_path
    assert await mw.on_call_tool(_context(), call_next) == "forwarded"
    # underivable code_path
    with patch("watercooler.path_resolver.derive_repo_slug", return_value=""):
        assert (
            await mw.on_call_tool(_context(code_path="/nowhere"), call_next)
            == "forwarded"
        )
    # pinned-repo code_path
    with patch(
        "watercooler.path_resolver.derive_repo_slug", return_value=_PINNED
    ):
        assert (
            await mw.on_call_tool(_context(code_path="/cloud"), call_next)
            == "forwarded"
        )

    assert pool.requests == []
