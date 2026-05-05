"""Tests for capability grants and hosted authorization (Step 8)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

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

    @pytest.mark.anyio
    async def test_async_stale_window_uses_post_fetch_clock(self):
        """PR #744 round 3 (LOW): if the async fetch takes a long time
        to fail, the stale-fallback check must use a fresh clock — NOT
        the pre-dispatch ``now`` — so an entry that aged past
        ``stale_max`` during the in-flight fetch is correctly rejected
        rather than served stale.
        """
        import asyncio
        import time
        from watercooler_mcp.capability_auth import _CachedGrant

        svc = CapabilityGrantService(api_url="https://example.com", api_key="key")
        # Tighten the stale window to keep the test fast.
        svc._cache_ttl = 1.0
        svc._stale_max = 5.0

        # Pre-dispatch time. Cache entry will be 4.9s old at dispatch
        # (within stale_max=5s) but >5s old AFTER the slow fetch fails.
        t0 = time.monotonic()
        svc._cache["user1"] = _CachedGrant(
            capabilities={"threads_core"},
            fetched_at=t0 - 4.9,
        )

        # Drive the clock forward by 0.6s during the simulated failing
        # fetch so the cache entry is 5.5s old at the post-fetch stale
        # check (> stale_max=5s). PR #744 round 5 (LOW): the prior
        # iteration of this test had a leftover ``async def _slow_fail``
        # that was never wired in — this version is the only one used.
        advance = {"delta": 0.0}
        real_monotonic = time.monotonic

        def fake_monotonic():
            return real_monotonic() + advance["delta"]

        def _sync_fail_advancing_clock(_uid):
            advance["delta"] = 0.6  # cache entry is now 5.5s old
            raise RuntimeError("simulated connection timeout")

        svc._fetch_capabilities = _sync_fail_advancing_clock

        with patch(
            "watercooler_mcp.capability_auth.time.monotonic",
            side_effect=fake_monotonic,
        ):
            result = await svc.get_capabilities_async("user1")

        # Entry was 4.9s old at dispatch → within stale; but 5.5s old
        # at post-fetch check → outside stale_max=5s. Correct behavior:
        # NOT served stale.
        assert result == set(), (
            "Stale fallback must use a post-fetch clock; using the "
            "pre-dispatch ``now`` here would have served the cached "
            "{threads_core} despite the entry being older than stale_max."
        )

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
        # The async middleware path uses get_capabilities_async; mirror
        # the grant set so the regression test exercises the same flow.
        svc.get_capabilities_async = AsyncMock(return_value={"threads_core"})
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
        svc.get_capabilities_async = AsyncMock(
            return_value={"threads_core", "baseline_search"}
        )
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


# ---------------------------------------------------------------------------
# Issue #521: cache-miss fallback must not block the event loop
# ---------------------------------------------------------------------------


class TestCapabilityAuthDoesNotBlockEventLoop:
    """Regression guard for issue #521.

    ``CapabilityGrantService._fetch_capabilities`` performs a synchronous
    ``urlopen`` with a 10s timeout.  On a cache miss in the async
    middleware path, calling it directly would pin the event loop for
    up to that timeout window.  These tests pin the async-safe shape
    so any future regression to ``await`` against blocking I/O is caught
    immediately.
    """

    @pytest.mark.anyio
    async def test_get_capabilities_async_yields_during_blocking_fetch(self):
        """``get_capabilities_async`` must dispatch ``_fetch_capabilities``
        to a worker thread so the event loop can run other tasks.

        We patch ``_fetch_capabilities`` to sleep on the calling thread
        for a meaningful interval (well below the 10s timeout but long
        enough that a blocked loop would prevent the parallel ticker
        from running).  Then we run a parallel asyncio task that
        increments a counter every short tick.  If the loop is not
        blocked, the ticker advances multiple times during the fetch.
        """
        import time as _time

        svc = CapabilityGrantService(
            api_url="https://example.invalid", api_key="key"
        )

        # Synchronous "fetch" that blocks the calling thread.
        fetch_duration = 0.30  # seconds
        tick_interval = 0.02   # seconds

        def _slow_fetch(_user_id: str) -> set[str]:
            _time.sleep(fetch_duration)
            return {"threads_core"}

        svc._fetch_capabilities = _slow_fetch  # type: ignore[assignment]

        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            try:
                while True:
                    await asyncio.sleep(tick_interval)
                    ticks += 1
            except asyncio.CancelledError:
                return

        ticker_task = asyncio.create_task(_ticker())
        try:
            wall_start = _time.monotonic()
            caps = await svc.get_capabilities_async("user_521")
            wall_elapsed = _time.monotonic() - wall_start
        finally:
            ticker_task.cancel()
            try:
                await ticker_task
            except asyncio.CancelledError:
                pass

        # Sanity: the fetch produced the expected result and actually
        # took at least the simulated network duration.
        assert caps == {"threads_core"}
        assert wall_elapsed >= fetch_duration * 0.8

        # The headline assertion: the parallel ticker got multiple turns
        # during the fetch.  If the event loop had been blocked by the
        # sync urlopen, ticks would be 0 or 1.  We require at least 3
        # ticks to leave generous slack for slow CI without admitting a
        # truly blocked loop.
        assert ticks >= 3, (
            f"Event loop appears blocked during get_capabilities_async: "
            f"only {ticks} ticks fired during a {fetch_duration:.2f}s fetch "
            f"(expected >= 3 ticks at {tick_interval:.2f}s interval)."
        )

    @pytest.mark.anyio
    async def test_ensure_async_uses_async_fetch_path(self):
        """``CapabilityAuthorizer.ensure_async`` on cache miss MUST
        delegate through the async fetch helper, not the blocking sync
        ``get_capabilities``.  We confirm by registering distinct grant
        sets on each path and observing which one is honoured.
        """
        svc = MagicMock(spec=CapabilityGrantService)
        # If the async path is used (correct), the call is allowed.
        svc.get_capabilities_async = AsyncMock(
            return_value={"memory_query"}
        )
        # If the sync path is mistakenly used (regression), the call is
        # denied because this set lacks "memory_query".
        svc.get_capabilities.return_value = {"threads_core"}

        auth = CapabilityAuthorizer(svc)
        denial = await auth.ensure_async("memory_query", "user_521")

        assert denial is None, (
            "ensure_async must consult get_capabilities_async on cache "
            "miss; falling back to the sync call would block the event "
            "loop and is the regression issue #521 fixes."
        )
        svc.get_capabilities_async.assert_awaited_once_with("user_521")

    @pytest.mark.anyio
    async def test_async_failure_prefers_concurrent_fresh_over_stale_snapshot(self):
        """PR #744 round 3 (MED): if a concurrent successful fetch wrote
        a fresh — possibly revoked — entry to the cache while THIS
        call's fetch was failing, the except-handler must prefer the
        fresh entry over the pre-dispatch ``cached`` snapshot.

        Without this guard, a capability that was just revoked (and
        re-cached fresh) would be silently re-granted from the older
        snapshot for one request, within ``stale_max``.
        """
        import time
        from watercooler_mcp.capability_auth import _CachedGrant

        svc = CapabilityGrantService(api_url="https://example.com", api_key="key")
        # Tight ttl so the early-return cache-hit path doesn't fire and
        # we exercise the fetch + failure path. Wide stale_max so the
        # snapshot would otherwise be served stale (the bug scenario).
        svc._cache_ttl = 1.0
        svc._stale_max = 600.0

        # Pre-dispatch state: cached entry is past cache_ttl (so we
        # attempt a fetch) but within stale_max (so the failure path
        # would normally serve it stale). This is the snapshot the
        # failing fetch will see.
        t0 = time.monotonic()
        svc._cache["user1"] = _CachedGrant(
            capabilities={"threads_core", "memory_query"},
            fetched_at=t0 - 10.0,  # past ttl=1s, within stale_max=600s
        )

        # Failing fetch installs a CONCURRENT fresh write into the
        # cache during the in-flight failure — simulating a revoke.
        # The fresh entry omits ``memory_query``.
        def _fail_after_concurrent_revoke(_uid):
            svc._cache["user1"] = _CachedGrant(
                capabilities={"threads_core"},  # memory_query revoked
                fetched_at=time.monotonic(),
            )
            raise RuntimeError("simulated transient failure on this caller")

        svc._fetch_capabilities = _fail_after_concurrent_revoke

        result = await svc.get_capabilities_async("user1")

        # Must reflect the CONCURRENT fresh entry (revocation
        # honoured), NOT the pre-dispatch snapshot (which still had
        # memory_query). Without the round-3 fix this assertion fails.
        assert "memory_query" not in result, (
            "Concurrent fresh cache entry must dominate the stale "
            "pre-dispatch snapshot — otherwise revocations are "
            "silently bypassed within stale_max."
        )
        assert "threads_core" in result

    @pytest.mark.anyio
    async def test_async_success_path_does_not_overwrite_fresher_concurrent_revoke(self):
        """PR #744 round 5 (MED): when two coroutines both miss cache
        and dispatch overlapping fetches, the slower one's ``fetched_at``
        is later — but if the faster one observed a server-side
        revocation, blindly overwriting on success would re-grant the
        revoked capability.

        Models the documented race: A dispatches at T=0, server revokes
        at T=5, B dispatches at T=5.1 and writes ``{threads_core}``
        (post-revocation) at T=7. A completes last at T=8 with stale
        ``{threads_core, memory_query}``. A must NOT overwrite B.
        """
        import time
        from watercooler_mcp.capability_auth import _CachedGrant

        svc = CapabilityGrantService(api_url="https://example.com", api_key="key")
        svc._cache_ttl = 60.0
        svc._stale_max = 600.0

        # No initial cache entry — A and B both miss.
        # Coroutine A's fetch returns the pre-revoke set.
        def _slow_a_fetch(_uid):
            # Simulate B's faster fetch landing while A is in flight.
            # Stamp B's entry with a fetched_at strictly between A's
            # dispatch_time and A's own (to-be) fetched_at.
            svc._cache["user1"] = _CachedGrant(
                capabilities={"threads_core"},  # post-revocation
                fetched_at=time.monotonic(),
            )
            return {"threads_core", "memory_query"}  # A's stale view

        svc._fetch_capabilities = _slow_a_fetch

        result = await svc.get_capabilities_async("user1")

        # A must have honoured B's fresher revoked entry instead of
        # overwriting with its stale grants.
        assert "memory_query" not in result, (
            "Slower coroutine's later fetched_at must not overwrite a "
            "fresher concurrent revoke. Got %r." % (result,)
        )
        # And the cache itself reflects B's write, not A's.
        assert svc._cache["user1"].capabilities == {"threads_core"}

    @pytest.mark.anyio
    async def test_async_fetch_synchronous_raise_falls_through_to_sync(self):
        """PR #744 round 5 (LOW): if a stub's ``get_capabilities_async``
        raises *synchronously* (on the call itself, not from inside a
        returned coroutine), wrap-and-fall-through to the sync path
        rather than propagating the exception out of ``ensure_async``
        into the middleware. Real ``async def`` implementations cannot
        do this, but defending against shape mismatches keeps the
        middleware uniformly fail-closed.
        """
        from unittest.mock import MagicMock

        svc = MagicMock(spec=CapabilityGrantService)
        # Async hook raises synchronously on call.
        svc.get_capabilities_async = MagicMock(
            side_effect=RuntimeError("synchronous failure from stub")
        )
        # Sync fallback returns a usable result.
        svc.get_capabilities.return_value = {"threads_core"}

        auth = CapabilityAuthorizer(svc)
        # Should NOT propagate the RuntimeError; sync fallback returns
        # {threads_core}, which lacks "memory_query" → denial.
        denial = await auth.ensure_async("memory_query", "user_521")
        assert denial is not None, (
            "Sync raise from async hook must fall through to sync path "
            "and produce a normal denial — not propagate to middleware."
        )
        svc.get_capabilities.assert_called_once_with("user_521")

    @pytest.mark.anyio
    async def test_async_fetch_non_awaitable_falls_through_to_sync(self):
        """PR #744 review (LOW): if a test double exposes
        ``get_capabilities_async`` but it returns a non-awaitable (e.g.,
        an unspecced ``MagicMock``), DO NOT trust the value — fall
        through to the sync ``get_capabilities`` via ``asyncio.to_thread``.

        Without this guard, ``Mock.__contains__`` is truthy, so the
        downstream ``if capability in grants`` check silently grants
        every capability.
        """
        svc = MagicMock(spec=CapabilityGrantService)
        # Async hook exists but returns a plain Mock (not an awaitable).
        svc.get_capabilities_async = MagicMock(return_value=MagicMock())
        # The fallback sync path is the source of truth — it returns a
        # set that does NOT contain ``forbidden_cap``.
        svc.get_capabilities.return_value = {"threads_core"}

        auth = CapabilityAuthorizer(svc)
        denial = await auth.ensure_async("forbidden_cap", "user_521")

        # The fall-through must produce a denial — the plain Mock from
        # the async hook must NOT be used as the grants set.
        assert denial is not None, (
            "Non-awaitable async-hook response must not bypass capability "
            "gating. Expected the sync fallback to deny 'forbidden_cap'."
        )
        # Confirm the sync path was actually consulted.
        svc.get_capabilities.assert_called_once_with("user_521")

    @pytest.mark.anyio
    async def test_ensure_async_preloaded_caps_skip_fetch(self):
        """When the Bearer-token preloaded set is supplied (the primary
        path), neither sync nor async fetch should fire.  This pins the
        documented zero-roundtrip shape so the new async helper does
        not introduce an extra fetch.
        """
        svc = MagicMock(spec=CapabilityGrantService)
        svc.get_capabilities_async = AsyncMock(
            return_value=set()
        )
        svc.get_capabilities.return_value = set()

        auth = CapabilityAuthorizer(svc)
        denial = await auth.ensure_async(
            "memory_query",
            "user_521",
            preloaded_capabilities=frozenset({"memory_query"}),
        )

        assert denial is None
        svc.get_capabilities_async.assert_not_awaited()
        svc.get_capabilities.assert_not_called()
