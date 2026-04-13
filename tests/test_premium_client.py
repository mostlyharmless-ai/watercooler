"""Tests for PremiumToolClient (Step 2)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watercooler_mcp.premium_client import PremiumToolClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client() -> PremiumToolClient:
    """Create a PremiumToolClient with a mocked FastMCP client."""
    mock_client = MagicMock()
    return PremiumToolClient(mock_client)


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
    def test_headers_fallback_to_git_context(self, mock_transport_cls, mock_client_cls):
        with patch("watercooler.config_facade.config") as mock_config:
            mock_config.get_hosted_api_key.return_value = None
            mock_config.context.return_value = MagicMock(
                code_repo="discovered/repo", code_branch="discovered-branch"
            )

            PremiumToolClient.from_transport_config({
                "url": "https://example.com/mcp/premium",
                "proxy_repo": "",
                "proxy_branch": "",
            })

            headers = mock_transport_cls.call_args[1]["headers"]
            assert headers["X-Repo"] == "discovered/repo"
            assert headers["X-Branch"] == "discovered-branch"


# ---------------------------------------------------------------------------
# call_tool_text
# ---------------------------------------------------------------------------


class TestCallToolText:
    @pytest.mark.anyio
    async def test_passes_through_tool_name_and_args(self):
        mock_client = AsyncMock()
        text_content = MagicMock()
        text_content.text = '{"result": "ok"}'
        result = MagicMock()
        result.content = [text_content]
        mock_client.call_tool = AsyncMock(return_value=result)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        pc = PremiumToolClient(mock_client)
        output = await pc.call_tool_text("watercooler_search", {"mode": "facts", "query": "test"})

        mock_client.call_tool.assert_called_once_with(
            "watercooler_search", {"mode": "facts", "query": "test"}
        )
        assert output == '{"result": "ok"}'

    @pytest.mark.anyio
    async def test_returns_error_on_exception(self):
        mock_client = AsyncMock()
        mock_client.call_tool = AsyncMock(side_effect=RuntimeError("connection failed"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        pc = PremiumToolClient(mock_client)
        output = await pc.call_tool_text("watercooler_smart_query", {"query": "test"})

        data = json.loads(output)
        assert data["error"] == "remote_call_failed"
        assert data["tool"] == "watercooler_smart_query"

    @pytest.mark.anyio
    async def test_returns_error_on_empty_content(self):
        mock_client = AsyncMock()
        result = MagicMock()
        result.content = []
        mock_client.call_tool = AsyncMock(return_value=result)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        pc = PremiumToolClient(mock_client)
        output = await pc.call_tool_text("some_tool", {})

        data = json.loads(output)
        assert data["error"] == "empty_response"


# ---------------------------------------------------------------------------
# proxy_server
# ---------------------------------------------------------------------------


class TestProxyServer:
    @patch("watercooler_mcp.premium_client.create_proxy")
    def test_returns_fastmcp_proxy(self, mock_create_proxy):
        mock_proxy = MagicMock(spec=["run"])
        mock_create_proxy.return_value = mock_proxy

        mock_client = MagicMock()
        pc = PremiumToolClient(mock_client)
        result = pc.proxy_server()

        mock_create_proxy.assert_called_once_with(mock_client, name="Watercooler Premium Proxy")
        assert result is mock_proxy

    @patch("watercooler_mcp.premium_client.create_proxy")
    def test_custom_name(self, mock_create_proxy):
        mock_client = MagicMock()
        pc = PremiumToolClient(mock_client)
        pc.proxy_server(name="Custom Name")

        mock_create_proxy.assert_called_once_with(mock_client, name="Custom Name")
