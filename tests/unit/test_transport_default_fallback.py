"""Hosted-first default transport + credential-gated fallback.

`proxy` is the shipped default (`McpConfig.transport`), with the hosted URL baked
in. Because proxy has no local fallback — it can only forward to a remote endpoint
with an API key — `_resolve_effective_transport` transparently downgrades a
credential-less proxy boot to local `stdio`, so an open-core / not-yet-authenticated
install still runs fully local instead of hard-exiting.
"""

from __future__ import annotations

from unittest.mock import patch

from watercooler.config_schema import McpConfig
from watercooler_mcp.server import _resolve_effective_transport

_HOSTED_URL = "https://watercooler-cloud-production.up.railway.app/mcp/"


class TestShippedDefaults:
    def test_transport_defaults_to_proxy(self):
        assert McpConfig().transport == "proxy"

    def test_hosted_url_is_baked_in(self):
        assert McpConfig().url == _HOSTED_URL


def _tc(transport: str, url: str = _HOSTED_URL) -> dict:
    return {"transport": transport, "url": url}


class TestResolveEffectiveTransport:
    def test_proxy_with_credentials_stays_proxy(self):
        with patch(
            "watercooler_mcp.server.config.get_hosted_api_key", return_value="wc_live"
        ):
            assert _resolve_effective_transport("proxy", _tc("proxy")) == "proxy"

    def test_proxy_without_api_key_falls_back_to_stdio(self, capsys):
        with patch(
            "watercooler_mcp.server.config.get_hosted_api_key", return_value=""
        ):
            assert _resolve_effective_transport("proxy", _tc("proxy")) == "stdio"
        # The fallback is announced on stderr, not silent.
        assert "running locally" in capsys.readouterr().err

    def test_proxy_without_url_falls_back_to_stdio(self):
        with patch(
            "watercooler_mcp.server.config.get_hosted_api_key", return_value="wc_live"
        ):
            assert (
                _resolve_effective_transport("proxy", _tc("proxy", url=""))
                == "stdio"
            )

    def test_stdio_is_returned_unchanged(self):
        # No credential lookup should even happen for a non-proxy transport.
        with patch(
            "watercooler_mcp.server.config.get_hosted_api_key",
            side_effect=AssertionError("should not be consulted"),
        ):
            assert _resolve_effective_transport("stdio", _tc("stdio")) == "stdio"

    def test_hybrid_is_returned_unchanged(self):
        with patch(
            "watercooler_mcp.server.config.get_hosted_api_key",
            side_effect=AssertionError("should not be consulted"),
        ):
            assert _resolve_effective_transport("hybrid", _tc("hybrid")) == "hybrid"

    def test_http_is_returned_unchanged(self):
        with patch(
            "watercooler_mcp.server.config.get_hosted_api_key",
            side_effect=AssertionError("should not be consulted"),
        ):
            assert _resolve_effective_transport("http", _tc("http")) == "http"
