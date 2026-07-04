"""Tests for PremiumToolClient (Step 2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from watercooler_mcp.premium_client import (
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_INIT_TIMEOUT,
    PremiumToolClient,
    build_premium_client,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_cm_client(**attrs) -> AsyncMock:
    """An AsyncMock FastMCP client wired for the fresh-session-per-call idiom.

    ``call_tool_text``/``list_tools`` now call ``client.new()`` (sync), enter
    the result as an async context manager, and force ``_disconnect`` in a
    ``finally``. ``new()`` returns the same mock so assertions can target it.
    """
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.new = MagicMock(return_value=mock_client)
    mock_client.is_connected = MagicMock(return_value=False)
    for key, value in attrs.items():
        setattr(mock_client, key, value)
    return mock_client


# ---------------------------------------------------------------------------
# from_transport_config
# ---------------------------------------------------------------------------


class TestFromTransportConfig:
    def test_missing_url_raises(self):
        with pytest.raises(ValueError, match="requires a remote URL"):
            PremiumToolClient.from_transport_config({"url": ""})

    @patch("watercooler_mcp.premium_client.Client")
    @patch("watercooler_mcp.premium_client.StreamableHttpTransport")
    def test_headers_built_from_config(self, mock_transport_cls, mock_client_cls):
        with patch("watercooler.config_facade.config") as mock_config:
            mock_config.get_hosted_api_key.return_value = "test-key"
            mock_config.context.return_value = MagicMock(
                code_repo="org/repo", code_branch="main"
            )

            client = PremiumToolClient.from_transport_config({
                "url": "https://example.com/mcp/premium",
                "proxy_repo": "org/repo",
                "proxy_branch": "feature-x",
            })

            # Transport should be created with url and headers
            mock_transport_cls.assert_called_once()
            call_args = mock_transport_cls.call_args
            assert call_args[0][0] == "https://example.com/mcp/premium"
            headers = call_args[1]["headers"]
            assert headers["X-Repo"] == "org/repo"
            assert headers["X-Branch"] == "feature-x"
            assert call_args[1]["auth"] == "test-key"

    @patch("watercooler_mcp.premium_client.Client")
    @patch("watercooler_mcp.premium_client.StreamableHttpTransport")
    def test_headers_fallback_to_git_context(
        self,
        mock_transport_cls,
        mock_client_cls,
        tmp_path: Path,
    ):
        with patch("watercooler.config_facade.config") as mock_config:
            mock_config.get_hosted_api_key.return_value = None
            mock_config.context.return_value = MagicMock(
                code_repo="discovered/repo", code_branch="discovered-branch"
            )

            PremiumToolClient.from_transport_config(
                {
                    "url": "https://example.com/mcp/premium",
                    "proxy_repo": "",
                    "proxy_branch": "",
                },
                boot_cwd=tmp_path,
            )

            headers = mock_transport_cls.call_args[1]["headers"]
            assert headers["X-Repo"] == "discovered/repo"
            assert headers["X-Branch"] == "discovered-branch"
            mock_config.context.assert_called_once_with(tmp_path)

    def test_missing_resolved_repo_raises_actionable_error(self, tmp_path: Path):
        with patch("watercooler.config_facade.config") as mock_config:
            mock_config.get_hosted_api_key.return_value = "test-key"
            mock_config.context.return_value = MagicMock(
                code_repo=None,
                code_branch="main",
            )

            with pytest.raises(ValueError, match=r"Set \[mcp\]\.proxy_repo"):
                PremiumToolClient.from_transport_config(
                    {
                        "url": "https://example.com/mcp/premium",
                        "proxy_repo": "",
                        "proxy_branch": "",
                    },
                    boot_cwd=tmp_path,
                )

    def test_missing_resolved_branch_raises_actionable_error(self, tmp_path: Path):
        with patch("watercooler.config_facade.config") as mock_config:
            mock_config.get_hosted_api_key.return_value = "test-key"
            mock_config.context.return_value = MagicMock(
                code_repo="org/repo",
                code_branch=None,
            )

            with pytest.raises(ValueError, match=r"Set \[mcp\]\.proxy_branch"):
                PremiumToolClient.from_transport_config(
                    {
                        "url": "https://example.com/mcp/premium",
                        "proxy_repo": "",
                        "proxy_branch": "",
                    },
                    boot_cwd=tmp_path,
                )

    def test_context_failure_reports_repo_and_branch_when_both_missing(
        self,
        tmp_path: Path,
    ):
        with patch("watercooler.config_facade.config") as mock_config:
            mock_config.get_hosted_api_key.return_value = "test-key"
            mock_config.context.side_effect = RuntimeError("git unavailable")

            with pytest.raises(ValueError, match=r"proxy_repo.*proxy_branch"):
                PremiumToolClient.from_transport_config(
                    {
                        "url": "https://example.com/mcp/premium",
                        "proxy_repo": "",
                        "proxy_branch": "",
                    },
                    boot_cwd=tmp_path,
                )

    def test_missing_branch_resolution_raises_when_repo_configured(self, tmp_path: Path):
        with patch("watercooler.config_facade.config") as mock_config:
            mock_config.get_hosted_api_key.return_value = "test-key"
            mock_config.context.side_effect = RuntimeError("git unavailable")

            with pytest.raises(ValueError, match=r"proxy_branch"):
                PremiumToolClient.from_transport_config(
                    {
                        "url": "https://example.com/mcp/premium",
                        "proxy_repo": "org/repo",
                        "proxy_branch": "",
                    },
                    boot_cwd=tmp_path,
                )


# ---------------------------------------------------------------------------
# call_tool_text
# ---------------------------------------------------------------------------


class TestCallToolText:
    @pytest.mark.anyio
    async def test_passes_through_tool_name_and_args(self):
        text_content = MagicMock()
        text_content.text = '{"result": "ok"}'
        result = MagicMock()
        result.content = [text_content]
        mock_client = _async_cm_client(call_tool=AsyncMock(return_value=result))

        pc = PremiumToolClient(mock_client)
        output = await pc.call_tool_text("watercooler_search", {"mode": "facts", "query": "test"})

        mock_client.call_tool.assert_called_once_with(
            "watercooler_search",
            {"mode": "facts", "query": "test"},
            timeout=pc._call_timeout,
        )
        assert output == '{"result": "ok"}'

    @pytest.mark.anyio
    async def test_returns_error_on_exception(self):
        mock_client = _async_cm_client(
            call_tool=AsyncMock(side_effect=RuntimeError("connection failed"))
        )

        pc = PremiumToolClient(mock_client)
        output = await pc.call_tool_text("watercooler_smart_query", {"query": "test"})

        data = json.loads(output)
        assert data["error"] == "remote_call_failed"
        assert data["tool"] == "watercooler_smart_query"

    @pytest.mark.anyio
    async def test_returns_http_response_body_on_direct_transport_exception(self):
        response = MagicMock(
            status_code=403,
            text="repo_claim_mismatch: Authorised: [org/a]",
        )
        exc = RuntimeError("Client error '403 Forbidden'")
        exc.response = response
        mock_client = _async_cm_client(call_tool=AsyncMock(side_effect=exc))

        pc = PremiumToolClient(mock_client)
        output = await pc.call_tool_text("watercooler_smart_query", {"query": "test"})

        data = json.loads(output)
        assert data["error"] == "remote_call_failed"
        assert data["status_code"] == 403
        assert data["remote_error"] == "repo_claim_mismatch: Authorised: [org/a]"

    @pytest.mark.anyio
    async def test_returns_http_response_body_from_nested_transport_exception(self):
        response = MagicMock(
            status_code=403,
            text="repo_claim_mismatch: X-Repo 'org/b'",
        )
        http_exc = RuntimeError("Client error '403 Forbidden'")
        http_exc.response = response
        wrapper = RuntimeError("FastMCP call failed")
        wrapper.__cause__ = http_exc
        mock_client = _async_cm_client(call_tool=AsyncMock(side_effect=wrapper))

        pc = PremiumToolClient(mock_client)
        output = await pc.call_tool_text("watercooler_smart_query", {"query": "test"})

        data = json.loads(output)
        assert data["error"] == "remote_call_failed"
        assert data["status_code"] == 403
        assert data["remote_error"] == "repo_claim_mismatch: X-Repo 'org/b'"

    @pytest.mark.anyio
    async def test_returns_error_on_empty_content(self):
        result = MagicMock()
        result.content = []
        mock_client = _async_cm_client(call_tool=AsyncMock(return_value=result))

        pc = PremiumToolClient(mock_client)
        output = await pc.call_tool_text("some_tool", {})

        data = json.loads(output)
        assert data["error"] == "empty_response"

    @pytest.mark.anyio
    async def test_uses_fresh_session_per_call(self):
        """Each call goes through ``new()``; the shared client is never entered."""
        good = MagicMock()
        good.content = [type("_C", (), {"text": "ok"})()]

        def _session():
            return _async_cm_client(call_tool=AsyncMock(return_value=good))

        shared = MagicMock()
        shared.new = MagicMock(side_effect=[_session(), _session()])

        pc = PremiumToolClient(shared)
        await pc.call_tool_text("t", {})
        await pc.call_tool_text("t", {})

        assert shared.new.call_count == 2
        # The shared instance itself is never used as a context manager —
        # only the fresh sessions returned by new() are entered.
        shared.__aenter__.assert_not_called()

    @pytest.mark.anyio
    async def test_wedged_prior_session_not_reused(self):
        """A first call whose session hangs/dies does not poison the next call."""
        good = MagicMock()
        good.content = [type("_C", (), {"text": "ok"})()]

        wedged = _async_cm_client(
            call_tool=AsyncMock(side_effect=RuntimeError("session wedged"))
        )
        fresh = _async_cm_client(call_tool=AsyncMock(return_value=good))

        shared = MagicMock()
        shared.new = MagicMock(side_effect=[wedged, fresh])

        pc = PremiumToolClient(shared)
        first = json.loads(await pc.call_tool_text("t", {}))
        assert first["error"] == "remote_call_failed"

        second = await pc.call_tool_text("t", {})
        assert second == "ok"
        assert shared.new.call_count == 2


# ---------------------------------------------------------------------------
# proxy_server
# ---------------------------------------------------------------------------


class TestProxyServer:
    @patch("watercooler_mcp.premium_client.create_proxy")
    def test_returns_fastmcp_proxy(self, mock_create_proxy):
        mock_proxy = MagicMock(spec=["run"])
        mock_create_proxy.return_value = mock_proxy

        mock_client = MagicMock()
        mock_client.is_connected.return_value = False
        pc = PremiumToolClient(mock_client)
        result = pc.proxy_server()

        mock_create_proxy.assert_called_once_with(mock_client, name="Watercooler Premium Proxy")
        assert result is mock_proxy

    @patch("watercooler_mcp.premium_client.create_proxy")
    def test_custom_name(self, mock_create_proxy):
        mock_client = MagicMock()
        mock_client.is_connected.return_value = False
        pc = PremiumToolClient(mock_client)
        pc.proxy_server(name="Custom Name")

        mock_create_proxy.assert_called_once_with(mock_client, name="Custom Name")

    @patch("watercooler_mcp.premium_client.create_proxy")
    def test_rejects_connected_client(self, mock_create_proxy):
        """A connected client would make create_proxy reuse one session
        (the wedge-prone path); guard against it loudly."""
        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        pc = PremiumToolClient(mock_client)

        with pytest.raises(RuntimeError, match="disconnected premium client"):
            pc.proxy_server()
        mock_create_proxy.assert_not_called()


# ---------------------------------------------------------------------------
# Real fastmcp stack — wedge-safety invariants
#
# These exercise the actual fastmcp Client/transport/proxy machinery (not
# mocks), so they fail loudly if a future fastmcp/transport change reintroduces
# shared session state, drops the timeout plumbing, or stops tearing down the
# throwaway per-call client.
# ---------------------------------------------------------------------------


class TestFreshSessionRealStack:
    def _disconnected_http_client(self) -> Client:
        transport = StreamableHttpTransport(
            "https://example.invalid/mcp/", headers={}, auth=None
        )
        return Client(transport, init_timeout=1.0, timeout=1.0)

    def test_new_yields_disconnected_client(self):
        """``.new()`` on the real HTTP transport starts a fresh, disconnected
        session — the invariant the whole fix relies on."""
        client = self._disconnected_http_client()
        fresh = client.new()
        assert fresh is not client
        assert fresh.is_connected() is False

    def test_proxy_uses_fresh_session_per_request(self):
        """The proxy's client factory yields a fresh client per request
        (never the shared instance) — Decision B guard."""
        pc = PremiumToolClient(self._disconnected_http_client())
        proxy = pc.proxy_server()

        c1 = proxy.client_factory()
        c2 = proxy.client_factory()
        assert c1 is not pc._client
        assert c2 is not pc._client
        assert c1 is not c2

    @pytest.mark.anyio
    async def test_timeout_does_not_leak_session_task(self):
        """A read-timeout against a genuinely hung tool must tear the
        throwaway session down, not leak its ``session_task``/socket.

        Uses a real in-memory MCP server + real fastmcp Client session (a
        mocked ``McpError`` has no ``session_task``, so it cannot catch the
        leak the ``finally: _disconnect(force=True)`` guards against).
        """
        server = FastMCP("leak-test")

        @server.tool
        async def slow() -> str:
            await asyncio.sleep(60)
            return "never"

        pc = PremiumToolClient(Client(server), call_timeout=0.5)
        sessions: list[Client] = []
        original_new = pc._client.new

        def _spy_new() -> Client:
            session = original_new()
            sessions.append(session)
            return session

        pc._client.new = _spy_new  # type: ignore[method-assign]

        out = json.loads(await pc.call_tool_text("slow", {}))

        assert out["error"] == "remote_call_failed"
        assert sessions, "expected a fresh session to be created"
        # is_connected() only tests `session is not None`; also assert the
        # background session_task itself was reclaimed, so a teardown that
        # cancels mid-cleanup (leaving session reset but the task dangling) is
        # caught.
        assert all(not s.is_connected() for s in sessions), (
            "throwaway session was not torn down after timeout"
        )
        assert all(
            s._session_state.session_task is None for s in sessions
        ), "throwaway session_task leaked after timeout"

    @pytest.mark.anyio
    async def test_success_path_real_inmemory_server(self):
        """End-to-end happy path through the real fastmcp session machinery."""
        server = FastMCP("ok-test")

        @server.tool
        async def echo() -> str:
            return "pong"

        pc = PremiumToolClient(Client(server), call_timeout=5.0)
        assert await pc.call_tool_text("echo", {}) == "pong"
        assert "echo" in await pc.list_tools()


class TestTimeoutPlumbing:
    @patch("watercooler_mcp.premium_client.Client")
    @patch("watercooler_mcp.premium_client.StreamableHttpTransport")
    def test_init_timeout_passed_to_client(self, mock_transport_cls, mock_client_cls):
        """``from_transport_config`` must pass an explicit ``init_timeout``
        (its config default is ``None`` == infinite)."""
        with patch("watercooler.config_facade.config") as mock_config:
            mock_config.get_hosted_api_key.return_value = "k"
            mock_config.context.return_value = MagicMock(
                code_repo="o/r", code_branch="m"
            )
            PremiumToolClient.from_transport_config(
                {
                    "url": "https://example.com/mcp/premium",
                    "proxy_repo": "o/r",
                    "proxy_branch": "b",
                }
            )

        kwargs = mock_client_cls.call_args.kwargs
        assert kwargs["init_timeout"] == DEFAULT_INIT_TIMEOUT
        assert kwargs["timeout"] is not None

    @patch("watercooler_mcp.premium_client.Client")
    @patch("watercooler_mcp.premium_client.StreamableHttpTransport")
    def test_build_premium_client_bounds_both_timeouts(
        self, mock_transport_cls, mock_client_cls
    ):
        """The shared builder (used by both the hybrid direct path and the
        pure-proxy path) bounds connect handshake AND per-call reads."""
        build_premium_client("https://x/mcp", {"X-Repo": "o/r"}, "key")

        mock_transport_cls.assert_called_once_with(
            "https://x/mcp", headers={"X-Repo": "o/r"}, auth="key"
        )
        kwargs = mock_client_cls.call_args.kwargs
        assert kwargs["init_timeout"] == DEFAULT_INIT_TIMEOUT
        assert kwargs["timeout"] == DEFAULT_CALL_TIMEOUT
