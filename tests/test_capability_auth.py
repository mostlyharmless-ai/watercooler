"""Tests for capability grants and hosted authorization (Step 8)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.capability_auth import (
    CapabilityAuthorizer,
    CapabilityGrantService,
)


# ---------------------------------------------------------------------------
# CapabilityGrantService
# ---------------------------------------------------------------------------


class TestCapabilityGrantService:
    def test_cached_result_returned(self):
        svc = CapabilityGrantService(api_url="https://example.com", api_key="key")
        # Pre-populate cache
        import time
        from watercooler_mcp.capability_auth import _CachedGrant
        svc._cache["user1"] = _CachedGrant(
            capabilities={"threads_core", "baseline_search"},
            fetched_at=time.monotonic(),
        )
        result = svc.get_capabilities("user1")
        assert result == {"threads_core", "baseline_search"}

    def test_stale_cache_returned_on_failure(self):
        import time
        svc = CapabilityGrantService(api_url="https://example.com", api_key="key")
        from watercooler_mcp.capability_auth import _CachedGrant
        # Expired past cache_ttl (300s) but within stale_max (900s).
        svc._cache["user1"] = _CachedGrant(
            capabilities={"threads_core"},
            fetched_at=time.monotonic() - 400,
        )
        # Mock fetch to fail
        svc._fetch_capabilities = MagicMock(side_effect=RuntimeError("connection error"))
        result = svc.get_capabilities("user1")
        assert result == {"threads_core"}

    def test_empty_set_on_failure_no_cache(self):
        svc = CapabilityGrantService(api_url="https://example.com", api_key="key")
        svc._fetch_capabilities = MagicMock(side_effect=RuntimeError("connection error"))
        result = svc.get_capabilities("user1")
        assert result == set()

    def test_no_api_url_returns_empty(self):
        svc = CapabilityGrantService(api_url="", api_key="key")
        result = svc._fetch_capabilities("user1")
        assert result == set()


# ---------------------------------------------------------------------------
# CapabilityAuthorizer
# ---------------------------------------------------------------------------


class TestCapabilityAuthorizer:
    def test_grant_hit_returns_none(self):
        svc = MagicMock()
        svc.get_capabilities.return_value = {"threads_core", "memory_query"}
        auth = CapabilityAuthorizer(svc)
        assert auth.ensure("memory_query", "user1") is None

    def test_deny_returns_error_json(self):
        svc = MagicMock()
        svc.get_capabilities.return_value = {"threads_core"}
        auth = CapabilityAuthorizer(svc)

        result = auth.ensure("memory_query", "user1")
        assert result is not None
        data = json.loads(result)
        assert data["error"] == "capability_not_enabled"
        assert data["capability"] == "memory_query"

    def test_no_user_id_returns_error(self):
        svc = MagicMock()
        auth = CapabilityAuthorizer(svc)
        result = auth.ensure("threads_core", "")
        data = json.loads(result)
        assert data["error"] == "capability_not_enabled"

    def test_hosted_search_facts_denied_without_memory_query(self):
        """Hosted watercooler_search(mode='facts') should be denied
        when memory_query is absent."""
        svc = MagicMock()
        svc.get_capabilities.return_value = {"threads_core", "baseline_search"}
        auth = CapabilityAuthorizer(svc)

        # Resolve the capability that mode=facts requires
        from watercooler_mcp.capabilities import resolve_search_capability
        cap = resolve_search_capability("facts")
        assert cap == "memory_query"

        result = auth.ensure(cap, "user1")
        assert result is not None
        data = json.loads(result)
        assert data["error"] == "capability_not_enabled"
        assert data["capability"] == "memory_query"


# ---------------------------------------------------------------------------
# End-to-end hosted denial via middleware
# ---------------------------------------------------------------------------


class TestHostedAuthMiddlewareE2E:
    """Verify that the auth middleware actually gates hosted tool execution."""

    @pytest.mark.anyio
    async def test_hosted_tool_denied_without_grant(self):
        """A hosted surface with an authorizer should deny tool calls
        when the user lacks the required capability."""
        from watercooler_mcp.capability_auth import CapabilityGrantService
        from watercooler_mcp.server_factory import build_mcp_server
        from watercooler_mcp.tool_runtime import ToolRuntime
        from watercooler_mcp.capabilities import CapabilityProfile
        from watercooler_mcp.context import (
            HttpRequestContext, set_http_context, clear_http_context,
        )

        # Create an authorizer that grants only threads_core
        svc = MagicMock(spec=CapabilityGrantService)
        svc.get_capabilities.return_value = {"threads_core"}
        auth = CapabilityAuthorizer(svc)

        rt = ToolRuntime(
            surface="hosted_full",
            capability_profile=CapabilityProfile(),
            authorizer=auth,
        )
        mcp = build_mcp_server(rt)

        # Set HTTP context so the middleware can resolve user_id
        set_http_context(HttpRequestContext(
            user_id="test_user",
            repo="org/repo",
            github_token="ghp_test",
        ))

        try:
            # Call a memory tool — should be denied
            result = await mcp.call_tool(
                "watercooler_smart_query",
                {"query": "test", "code_path": "."},
            )
            # Result is a ToolResult with .content list
            assert result.content and len(result.content) > 0
            text = result.content[0].text
            data = json.loads(text)
            assert data["error"] == "capability_not_enabled"
            assert data["capability"] == "memory_query"
        finally:
            clear_http_context()

    @pytest.mark.anyio
    async def test_search_auto_temporal_denied_without_memory_query(self):
        """watercooler_search(mode='auto') with a temporal query should be
        denied when the user has baseline_search but not memory_query,
        because auto inflates to facts mode."""
        from watercooler_mcp.capability_auth import CapabilityGrantService
        from watercooler_mcp.server_factory import build_mcp_server
        from watercooler_mcp.tool_runtime import ToolRuntime
        from watercooler_mcp.capabilities import CapabilityProfile
        from watercooler_mcp.context import (
            HttpRequestContext, set_http_context, clear_http_context,
        )

        svc = MagicMock(spec=CapabilityGrantService)
        svc.get_capabilities.return_value = {"threads_core", "baseline_search"}
        auth = CapabilityAuthorizer(svc)

        rt = ToolRuntime(
            surface="hosted_full",
            capability_profile=CapabilityProfile(),
            authorizer=auth,
        )
        mcp = build_mcp_server(rt)

        set_http_context(HttpRequestContext(
            user_id="test_user", repo="org/repo", github_token="ghp_test",
        ))
        try:
            result = await mcp.call_tool(
                "watercooler_search",
                {
                    "mode": "auto",
                    "query": "what changed from the old auth to the new auth",
                    "code_path": ".",
                },
            )
            assert result.content and len(result.content) > 0
            text = result.content[0].text
            data = json.loads(text)
            assert data["error"] == "capability_not_enabled"
            assert data["capability"] == "memory_query"
        finally:
            clear_http_context()

    @pytest.mark.anyio
    async def test_unregistered_tool_fails_closed(self):
        """A tool registered with FastMCP but absent from ``_TOOL_CAPABILITY_MAP``
        must be refused with ``capability_not_registered`` rather than silently
        bypassing the grant check (or crashing with UnboundLocalError).

        Regression guard for two bugs in the same code path:
          - P2.7: fall-through to ``call_next`` executed unregistered tools
            with no auth on hosted surfaces.
          - Follow-up: the initial fix shadowed ``TextContent`` / ``ToolResult``
            with inner re-imports, so the fail-closed branch raised
            ``UnboundLocalError`` instead of returning the denial payload.
        """
        from watercooler_mcp.capability_auth import CapabilityGrantService
        from watercooler_mcp.server_factory import build_mcp_server
        from watercooler_mcp.tool_runtime import ToolRuntime
        from watercooler_mcp.capabilities import CapabilityProfile
        from watercooler_mcp.context import (
            HttpRequestContext, set_http_context, clear_http_context,
        )

        svc = MagicMock(spec=CapabilityGrantService)
        svc.get_capabilities.return_value = {"threads_core"}
        auth = CapabilityAuthorizer(svc)

        rt = ToolRuntime(
            surface="hosted_full",
            capability_profile=CapabilityProfile(),
            authorizer=auth,
        )
        mcp = build_mcp_server(rt)

        # Register a tool that's intentionally absent from
        # _TOOL_CAPABILITY_MAP. FastMCP accepts it, the capability lookup
        # in the middleware raises ValueError, and the fail-closed branch
        # must return a denial rather than crash or pass through.
        @mcp.tool(name="watercooler_unregistered_probe")
        def _probe() -> str:
            return "should-never-execute"

        set_http_context(HttpRequestContext(
            user_id="test_user", repo="org/repo", github_token="ghp_test",
        ))
        try:
            result = await mcp.call_tool(
                "watercooler_unregistered_probe", {}
            )
            assert result.content and len(result.content) > 0
            text = result.content[0].text
            data = json.loads(text)
            assert data["error"] == "capability_not_registered"
            assert data["tool"] == "watercooler_unregistered_probe"
            assert "_TOOL_CAPABILITY_MAP" in data["message"]
        finally:
            clear_http_context()

    @pytest.mark.anyio
    async def test_unknown_tool_preserves_fastmcp_404(self):
        """A tool name that doesn't exist in the FastMCP registry must fall
        through to FastMCP's normal unknown-tool handling rather than
        being rewritten into a ``capability_not_registered`` payload.

        ``tool_capability`` raises ``ValueError`` for both the registered-
        but-unmapped case AND the truly-unknown-name case; the middleware
        must disambiguate so client typos surface as protocol 404s instead
        of misleading server-configuration errors.
        """
        from watercooler_mcp.capability_auth import CapabilityGrantService
        from watercooler_mcp.server_factory import build_mcp_server
        from watercooler_mcp.tool_runtime import ToolRuntime
        from watercooler_mcp.capabilities import CapabilityProfile
        from watercooler_mcp.context import (
            HttpRequestContext, set_http_context, clear_http_context,
        )

        svc = MagicMock(spec=CapabilityGrantService)
        svc.get_capabilities.return_value = {"threads_core"}
        auth = CapabilityAuthorizer(svc)

        rt = ToolRuntime(
            surface="hosted_full",
            capability_profile=CapabilityProfile(),
            authorizer=auth,
        )
        mcp = build_mcp_server(rt)

        set_http_context(HttpRequestContext(
            user_id="test_user", repo="org/repo", github_token="ghp_test",
        ))
        try:
            # Client typo — no such tool. Must surface as a FastMCP error,
            # not a ``capability_not_registered`` rewrite.
            with pytest.raises(Exception) as excinfo:
                await mcp.call_tool("totally_unknown_tool_name_xyz", {})
            # The error should not carry our config-error payload shape.
            assert "capability_not_registered" not in str(excinfo.value)
        finally:
            clear_http_context()
