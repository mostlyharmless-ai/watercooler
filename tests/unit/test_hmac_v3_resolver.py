"""Unit tests for the HMAC v3 KeyResolver chain (Stage 2 C1).

Covers:

- ``KeyRegistry.add_resolver`` + the resolver-fallback path in ``lookup``.
- The order/precedence contract: env-keys win over resolver hits.
- Resolver exceptions are caught + logged + treated as miss (don't
  break the lookup).
- Revoked resolver hits 404 (treated as miss, not as the resolved
  KeyInfo).
- ``HttpResolver`` cache behaviour (positive + negative TTLs, expiry).
- ``HttpResolver`` defence-in-depth (malformed kid → don't fetch;
  response kid mismatch → reject).

The ``HttpResolver`` HTTP-fetch path is exercised via a stub
``httpx.Client`` injected through monkeypatch — keeps the test
boundary tight (no real HTTP).
"""

from __future__ import annotations

from typing import Optional


from watercooler_mcp.auth.hmac_keys import KeyInfo, KeyRegistry, KeyResolver
from watercooler_mcp.auth.hmac_resolver import (
    HttpResolver,
    load_resolver_from_env,
)


# ------------------------------------------------------------------ #
# KeyRegistry resolver chain
# ------------------------------------------------------------------ #


class _StaticResolver(KeyResolver):
    """Test fixture — no internal cache, so ``flush`` is a no-op
    that always returns False. Per PR #709 round 4 LOW the base
    class raises NotImplementedError so cacheless subclasses must
    explicitly opt out."""

    def __init__(self, mapping: dict[str, KeyInfo]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []
        self.flushed: list[str] = []

    def resolve(self, key_id: str) -> Optional[KeyInfo]:
        self.calls.append(key_id)
        return self.mapping.get(key_id)

    def flush(self, key_id: str) -> bool:
        self.flushed.append(key_id)
        return False


class _RaisingResolver(KeyResolver):
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, key_id: str) -> Optional[KeyInfo]:
        self.calls += 1
        raise RuntimeError("simulated resolver crash")

    def flush(self, key_id: str) -> bool:
        return False


class TestKeyRegistryResolverChain:
    def _key(self, kid: str = "kid-1") -> KeyInfo:
        return KeyInfo(
            key_id=kid,
            secret=b"s",
            key_type="per_user",
            bound_user_id="u",
        )

    def test_resolver_consulted_on_in_memory_miss(self) -> None:
        registry = KeyRegistry()
        info = self._key("usr-abc")
        resolver = _StaticResolver({"usr-abc": info})
        registry.add_resolver(resolver)
        assert registry.lookup("usr-abc") is info
        assert resolver.calls == ["usr-abc"]

    def test_resolver_not_consulted_on_in_memory_hit(self) -> None:
        registry = KeyRegistry()
        info = self._key("svc-1")
        registry.add(info)
        resolver = _StaticResolver({"svc-1": self._key("ghost")})
        registry.add_resolver(resolver)
        assert registry.lookup("svc-1") is info
        assert resolver.calls == [], (
            "env-key hit must not consult resolver — env wins"
        )

    def test_revoked_in_memory_key_returns_none_no_resolver_fallback(self) -> None:
        # If the env registry has a revoked entry for the kid, the
        # resolver chain is NOT consulted — the operator's revoke
        # is the source of truth for that kid.
        registry = KeyRegistry()
        revoked = KeyInfo(
            key_id="kid-1",
            secret=b"s",
            key_type="per_user",
            bound_user_id="u",
            revoked=True,
        )
        registry.add(revoked)
        resolver = _StaticResolver({"kid-1": self._key("kid-1")})
        registry.add_resolver(resolver)
        assert registry.lookup("kid-1") is None
        assert resolver.calls == []

    def test_resolver_returning_revoked_treated_as_miss(self) -> None:
        registry = KeyRegistry()
        revoked = KeyInfo(
            key_id="kid-1",
            secret=b"s",
            key_type="per_user",
            bound_user_id="u",
            revoked=True,
        )
        resolver = _StaticResolver({"kid-1": revoked})
        registry.add_resolver(resolver)
        assert registry.lookup("kid-1") is None

    def test_revoked_resolver_result_continues_chain(self) -> None:
        # PR #709 round 3 MED: a revoked KeyInfo from one resolver
        # MUST NOT halt the chain — the next resolver may have
        # the current active entry for the same kid (e.g. R1's
        # stale cache vs R2's fresh fetch). Treat as per-resolver
        # miss and continue.
        registry = KeyRegistry()
        revoked = KeyInfo(
            key_id="kid-1",
            secret=b"s",
            key_type="per_user",
            bound_user_id="u",
            revoked=True,
        )
        active = KeyInfo(
            key_id="kid-1",
            secret=b"s",
            key_type="per_user",
            bound_user_id="u",
        )
        r1 = _StaticResolver({"kid-1": revoked})
        r2 = _StaticResolver({"kid-1": active})
        registry.add_resolver(r1)
        registry.add_resolver(r2)
        result = registry.lookup("kid-1")
        assert result is not None, (
            "revoked result from R1 must not block R2's active result"
        )
        assert result.revoked is False
        # R2 was consulted (chain continued past R1).
        assert r2.calls == ["kid-1"]

    def test_resolver_chain_first_hit_wins(self) -> None:
        registry = KeyRegistry()
        first = _StaticResolver({"kid-1": self._key("first")})
        second = _StaticResolver({"kid-1": self._key("second")})
        registry.add_resolver(first)
        registry.add_resolver(second)
        result = registry.lookup("kid-1")
        assert result is not None
        assert result.key_id == "first"
        # Second resolver is not consulted because first won.
        assert second.calls == []

    def test_resolver_chain_falls_through_on_miss(self) -> None:
        registry = KeyRegistry()
        first = _StaticResolver({})  # always misses
        second = _StaticResolver({"kid-1": self._key("from-second")})
        registry.add_resolver(first)
        registry.add_resolver(second)
        result = registry.lookup("kid-1")
        assert result is not None
        assert result.key_id == "from-second"

    def test_raising_resolver_treated_as_miss(self, caplog) -> None:
        # A resolver that raises must not break the lookup —
        # subsequent resolvers in the chain still run.
        import logging as _logging

        registry = KeyRegistry()
        bad = _RaisingResolver()
        good = _StaticResolver({"kid-1": self._key("ok")})
        registry.add_resolver(bad)
        registry.add_resolver(good)

        # caplog at the registry's logger namespace.
        caplog.set_level(_logging.ERROR, logger="watercooler_mcp.auth.hmac_keys")
        result = registry.lookup("kid-1")
        assert result is not None
        assert result.key_id == "ok"
        assert bad.calls == 1
        # Note: caplog might not see the log if propagate=False is set
        # on the namespace by observability init; the behaviour we
        # care about is "lookup returned without raising and the
        # next resolver ran". Both verified.

    def test_no_resolvers_returns_none_on_miss(self) -> None:
        registry = KeyRegistry()
        assert registry.lookup("nope") is None


class _FlushTrackingResolver(KeyResolver):
    """Resolver that records flush calls for the revoke-flush test."""

    def __init__(self, has_kid: str) -> None:
        self._has = has_kid
        self.flushed: list[str] = []

    def resolve(self, key_id: str) -> Optional[KeyInfo]:
        if key_id == self._has:
            return KeyInfo(
                key_id=key_id,
                secret=b"s",
                key_type="per_user",
                bound_user_id="u",
            )
        return None

    def flush(self, key_id: str) -> bool:
        self.flushed.append(key_id)
        return key_id == self._has


class TestRevokeFlushesResolverChain:
    """PR #709 round 1 MED 1: ``KeyRegistry.revoke`` must flush
    resolver caches so an operator-driven revoke takes effect
    immediately, not after the resolver's TTL expires."""

    def test_revoke_calls_flush_on_each_resolver(self) -> None:
        registry = KeyRegistry()
        r1 = _FlushTrackingResolver(has_kid="usr-cached")
        r2 = _FlushTrackingResolver(has_kid="other")
        registry.add_resolver(r1)
        registry.add_resolver(r2)
        # Revoke a kid not in env _keys but cached in r1.
        result = registry.revoke("usr-cached")
        assert result is True, (
            "revoke must report state-change when a resolver had the kid"
        )
        # Both resolvers were asked to flush, regardless of who
        # returned True.
        assert r1.flushed == ["usr-cached"]
        assert r2.flushed == ["usr-cached"]

    def test_revoke_returns_false_when_neither_layer_has_kid(self) -> None:
        registry = KeyRegistry()
        r1 = _FlushTrackingResolver(has_kid="other")
        registry.add_resolver(r1)
        assert registry.revoke("usr-nothing") is False
        # Flush still attempted (resolver is asked) — it just
        # reports no entry to flush.
        assert r1.flushed == ["usr-nothing"]

    def test_revoke_combines_in_memory_and_resolver_state(self) -> None:
        # In-memory hit + resolver hit both contribute to the
        # changed=True signal. This is the typical case for a
        # service key that's also been cached in a resolver.
        registry = KeyRegistry()
        registry.add(KeyInfo(
            key_id="svc-1",
            secret=b"s",
            key_type="per_user",
            bound_user_id="u",
        ))
        r1 = _FlushTrackingResolver(has_kid="svc-1")
        registry.add_resolver(r1)
        assert registry.revoke("svc-1") is True
        # Lookup now returns None — env entry is revoked + resolver
        # was flushed.
        assert registry.lookup("svc-1") is None

    def test_resolver_flush_exception_not_fatal(self) -> None:
        # A resolver that raises on flush must not break the
        # revoke. In-memory revoke still applies.
        registry = KeyRegistry()
        registry.add(KeyInfo(
            key_id="kid-1",
            secret=b"s",
            key_type="per_user",
            bound_user_id="u",
        ))

        class _RaisingFlush(KeyResolver):
            def resolve(self, kid: str) -> Optional[KeyInfo]:
                return None

            def flush(self, kid: str) -> bool:
                raise RuntimeError("flush boom")

        registry.add_resolver(_RaisingFlush())
        # In-memory state changes; resolver exception swallowed.
        assert registry.revoke("kid-1") is True


# ------------------------------------------------------------------ #
# HttpResolver — cache behaviour
# ------------------------------------------------------------------ #


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, url: str, headers: dict):
        self.requests.append((url, headers))
        if not self._responses:
            raise RuntimeError("no more responses queued")
        return self._responses.pop(0)


def _install_fake_httpx(monkeypatch, client: _FakeClient) -> None:
    """Make ``import httpx`` inside HttpResolver resolve to a stub."""
    import sys
    import types

    fake = types.SimpleNamespace(
        Client=lambda timeout: client,
        TimeoutException=type("TimeoutException", (Exception,), {}),
        HTTPError=type("HTTPError", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "httpx", fake)


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class TestHttpResolverCache:
    def test_positive_response_cached(self, monkeypatch) -> None:
        clock = _Clock()
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u1",
                    "secret": "deadbeef" * 8,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)

        resolver = HttpResolver(
            base_url="https://dash.example",
            api_key="svc-key",
            cache_ttl_s=300.0,
            clock=clock,
        )

        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        result1 = resolver.resolve(kid)
        assert result1 is not None
        assert result1.key_id == kid
        assert result1.bound_user_id == "u1"
        # PR #709 round 4 HIGH: secret is hex-decoded to bytes (not
        # stored as raw ASCII of the hex string).
        assert result1.secret == bytes.fromhex("deadbeef" * 8)
        assert len(client.requests) == 1

        # Within TTL — second call hits cache, no HTTP.
        clock.t += 100
        result2 = resolver.resolve(kid)
        assert result2 is result1
        assert len(client.requests) == 1, "cache hit should not refetch"

    def test_positive_cache_expires_and_refetches(self, monkeypatch) -> None:
        clock = _Clock()
        payload = {
            "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
            "userId": "u1",
            "secret": "deadbeef" * 8,
            "issuedAt": "2026-04-30T00:00:00Z",
        }
        client = _FakeClient([_FakeResponse(200, payload), _FakeResponse(200, payload)])
        _install_fake_httpx(monkeypatch, client)

        resolver = HttpResolver(
            base_url="https://dash.example",
            api_key="svc-key",
            cache_ttl_s=60.0,
            clock=clock,
        )
        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        resolver.resolve(kid)
        # Past TTL.
        clock.t += 61
        resolver.resolve(kid)
        assert len(client.requests) == 2, "expired cache should refetch"

    def test_404_negative_cached(self, monkeypatch) -> None:
        clock = _Clock()
        client = _FakeClient([_FakeResponse(404)])
        _install_fake_httpx(monkeypatch, client)

        resolver = HttpResolver(
            base_url="https://dash.example",
            api_key="svc-key",
            negative_ttl_s=30.0,
            clock=clock,
        )
        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        assert resolver.resolve(kid) is None
        # Within negative TTL — second call hits cache.
        clock.t += 10
        assert resolver.resolve(kid) is None
        assert len(client.requests) == 1, "negative cache hit should not refetch"

    def test_negative_cache_expires(self, monkeypatch) -> None:
        clock = _Clock()
        client = _FakeClient([
            _FakeResponse(404),
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u1",
                    "secret": "deadbeef" * 8,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            ),
        ])
        _install_fake_httpx(monkeypatch, client)

        resolver = HttpResolver(
            base_url="https://dash.example",
            api_key="svc-key",
            negative_ttl_s=30.0,
            clock=clock,
        )
        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        assert resolver.resolve(kid) is None
        clock.t += 31
        # Negative TTL expired; refetch picks up the now-existing key.
        assert resolver.resolve(kid) is not None


class TestHttpResolverInflightDedup:
    """PR #709 round 1 MED 2: N concurrent requests for the same
    uncached kid must NOT all hit the dashboard. Per-kid in-flight
    coordination collapses the burst to a single fetch.

    The leader fetches; followers wait on the inflight Event and
    re-read the cache after the leader populates it. Only one
    HTTP request is observed.
    """

    def test_concurrent_resolves_collapse_to_one_fetch(
        self, monkeypatch
    ) -> None:
        # Block the fetch so all threads pile up on the inflight
        # event, then release. Only one fetch should fire.
        import threading
        import time as _time

        kid = "usr-stampedekidstampedekidstam"  # 32 chars after prefix
        # Each request gets the same response payload — but only one
        # should actually be served by the fake client.
        gate = threading.Event()

        class _GatedClient:
            def __init__(self):
                self.requests: list = []
                self.fetch_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def get(self, url, headers):
                self.fetch_count += 1
                self.requests.append((url, headers))
                # Block until the test releases.
                gate.wait(timeout=5.0)
                return _FakeResponse(
                    200,
                    {
                        "kid": kid,
                        "userId": "u",
                        "secret": "deadbeef" * 8,
                        "issuedAt": "2026-04-30T00:00:00Z",
                    },
                )

        client = _GatedClient()
        _install_fake_httpx(monkeypatch, client)

        resolver = HttpResolver(
            base_url="https://dash.example",
            api_key="svc-key",
        )

        # Fire N concurrent resolves.
        N = 8
        results: list = [None] * N
        threads = []

        def _worker(idx: int) -> None:
            results[idx] = resolver.resolve(kid)

        for i in range(N):
            t = threading.Thread(target=_worker, args=(i,))
            t.start()
            threads.append(t)

        # Give threads a moment to enter the resolver and the
        # leader to start the gated fetch.
        _time.sleep(0.05)
        # Release the gate so the leader's fetch returns.
        gate.set()
        for t in threads:
            t.join(timeout=5.0)

        # All N requests got a result.
        assert all(r is not None for r in results), (
            f"some workers got None; results: {results}"
        )
        # All N got the SAME KeyInfo payload.
        assert all(r.bound_user_id == "u" for r in results)
        # CRITICAL: only ONE HTTP fetch fired despite N concurrent
        # requests for the same kid.
        assert client.fetch_count == 1, (
            f"in-flight dedup failed: {client.fetch_count} fetches for {N} "
            "concurrent requests on the same kid"
        )

    def test_different_kids_fetch_independently(
        self, monkeypatch
    ) -> None:
        # Concurrent requests for DIFFERENT kids must each fetch —
        # the dedup is per-kid, not global. This is a regression
        # guard so a future "global lock" simplification doesn't
        # serialise unrelated fetches.
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": f"usr-aaaaaaaaaaaaaaaaaaaaa{i:03d}",
                    "userId": "u",
                    "secret": "deadbeef" * 8,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
            for i in range(3)
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        for i in range(3):
            kid = f"usr-aaaaaaaaaaaaaaaaaaaaa{i:03d}"
            assert resolver.resolve(kid) is not None
        assert len(client.requests) == 3


class TestHttpResolverFlush:
    """PR #709 round 1 MED 1: ``HttpResolver.flush`` evicts a cached
    entry on operator revoke. Without it, a revoke-on-the-cloud-side
    has no effect on resolver-cached keys until the TTL expires."""

    def test_flush_evicts_positive_cache(self, monkeypatch) -> None:
        clock = _Clock()
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u",
                    "secret": "deadbeef" * 8,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            ),
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u",
                    "secret": "deadbeef" * 8,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            ),
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example",
            api_key="svc-key",
            cache_ttl_s=300.0,
            clock=clock,
        )
        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        # Populate cache.
        assert resolver.resolve(kid) is not None
        assert len(client.requests) == 1
        # Within TTL — second resolve hits cache.
        assert resolver.resolve(kid) is not None
        assert len(client.requests) == 1
        # Flush — eviction reported as True.
        assert resolver.flush(kid) is True
        # Next resolve refetches.
        assert resolver.resolve(kid) is not None
        assert len(client.requests) == 2

    def test_flush_evicts_negative_cache(self, monkeypatch) -> None:
        client = _FakeClient([_FakeResponse(404)])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example",
            api_key="svc-key",
        )
        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        assert resolver.resolve(kid) is None
        # Negative cache populated.
        assert resolver.flush(kid) is True

    def test_flush_returns_false_when_no_cache_entry(self) -> None:
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        assert resolver.flush("usr-not-cached") is False

    def test_flush_after_fetch_end_does_not_orphan_marker(
        self, monkeypatch
    ) -> None:
        # PR #709 round 5 LOW: the narrow race between
        # ``_do_fetch_and_cache`` returning (its finally clears
        # the marker) and ``resolve()``'s finally popping
        # ``_inflight[kid]``. A flush() in that window sees an
        # in-flight entry, re-adds the kid to ``_revoked_inflight``,
        # and there's no live fetch to clear it. The next leader
        # would skip its cache write — denying one cycle of
        # legitimate auth.
        #
        # Direct reproduction of this race requires interleaving at
        # specific points inside ``resolve()`` that aren't easily
        # exposed for testing. Instead, verify the cleanup contract
        # directly: plant the bad state (a stale marker), drive a
        # resolve through it, and confirm
        #   1. The first resolve correctly skips the cache write
        #      because of the stale marker (returns None — that's
        #      the WRONG outcome production-wise, but it's exactly
        #      the one-cycle denial the bug produces).
        #   2. The marker is cleaned up by ``resolve()``'s finally
        #      so the cleanup contract holds.
        #   3. The NEXT resolve succeeds (cache populated, kid
        #      returned) — confirming the marker doesn't leak past
        #      the cleanup. PR #709 round 5+2 LOW: previously this
        #      step was missing; the test only asserted None on
        #      first resolve and a comment claimed "should SUCCEED"
        #      that was never verified.
        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        payload = {
            "kid": kid,
            "userId": "u",
            "secret": "deadbeef" * 8,
            "issuedAt": "2026-04-30T00:00:00Z",
        }
        # Two responses queued — one for the marker-blocked attempt,
        # one for the post-cleanup attempt that should succeed.
        client = _FakeClient([
            _FakeResponse(200, payload),
            _FakeResponse(200, payload),
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )

        # Plant a stale marker to mirror the bad state the race
        # would have left behind.
        with resolver._lock:
            resolver._revoked_inflight.add(kid)

        # First resolve: leader fetches successfully but sees the
        # stale marker and SKIPS the cache write — so
        # ``_read_cache_unconditional`` returns None. That's the
        # observable cost of one cycle of the bug.
        result1 = resolver.resolve(kid)
        assert result1 is None, (
            "stale marker should force the leader to skip the cache "
            "write on this cycle"
        )
        # Marker MUST be cleared by ``resolve()``'s finally —
        # otherwise it'd persist past the in-flight window and
        # block subsequent fetches indefinitely.
        with resolver._lock:
            assert kid not in resolver._revoked_inflight, (
                "stale marker leaked past resolve()'s finally"
            )

        # Second resolve: with the marker gone, the leader fetches
        # again, the cache write is NOT skipped, and the kid
        # resolves normally. This is the contract that proves the
        # bug is bounded to one cycle of denial.
        result2 = resolver.resolve(kid)
        assert result2 is not None, (
            "post-cleanup resolve should succeed once the marker "
            "is gone"
        )
        assert result2.bound_user_id == "u"
        # Two HTTP fetches consumed — one per resolve.
        assert len(client.requests) == 2

    def test_revoked_inflight_cleared_on_transient_error(
        self, monkeypatch
    ) -> None:
        # PR #709 round 3 MED: ``_revoked_inflight`` was previously
        # only cleared in the success branch of ``_do_fetch_and_cache``.
        # A transient error followed by a flush() during the fetch
        # would leave the marker set; the next valid fetch would
        # see the marker, skip the cache write, and silently deny
        # a valid kid. The fix moves the discard into a ``finally``
        # so the marker's lifetime is bounded by the in-flight
        # window.
        import threading
        import time as _time

        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        gate = threading.Event()
        responses = [_FakeResponse(500), _FakeResponse(
            200,
            {
                "kid": kid,
                "userId": "u",
                "secret": "deadbeef" * 8,
                "issuedAt": "2026-04-30T00:00:00Z",
            },
        )]

        class _GatedClient:
            def __init__(self):
                self.requests: list = []

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def get(self, url, headers):
                # First call: gated 500. Second call: 200.
                self.requests.append((url, headers))
                if len(self.requests) == 1:
                    gate.wait(timeout=5.0)
                return responses.pop(0)

        client = _GatedClient()
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )

        # Phase 1: leader fetches, hits 500 (gated). Operator
        # flushes during the fetch. Then release the gate.
        leader_result = [None]

        def _leader():
            leader_result[0] = resolver.resolve(kid)

        leader_thread = threading.Thread(target=_leader)
        leader_thread.start()
        _time.sleep(0.05)
        flush_during_fetch_result = resolver.flush(kid)
        gate.set()
        leader_thread.join(timeout=5.0)

        # Flush observed the in-flight fetch.
        assert flush_during_fetch_result is True
        # Leader's first call returned None (transient error).
        assert leader_result[0] is None
        # Critical: the marker MUST be cleared even though the
        # error path was taken. Verify by looking at internal
        # state.
        with resolver._lock:
            assert kid not in resolver._revoked_inflight, (
                "transient-error path left marker set; round-3 cleanup"
                " in `finally` not working"
            )

        # Phase 2: a fresh resolve should succeed and cache —
        # the leftover marker would have made this fail.
        result2 = resolver.resolve(kid)
        assert result2 is not None, (
            "next valid fetch was denied because of leftover marker"
        )
        with resolver._lock:
            assert kid in resolver._cache

    def test_flush_during_inflight_fetch_suppresses_cache_write(
        self, monkeypatch
    ) -> None:
        # PR #709 round 2 MED: TOCTOU between leader fetch start
        # and fetch end. flush() during the fetch must cause the
        # post-completion write to be SKIPPED — otherwise the
        # revoked kid lives in cache for the full TTL.
        import threading
        import time as _time

        kid = "usr-flushinflightkidaaaaaaaaaa"
        gate = threading.Event()

        class _GatedClient:
            def __init__(self):
                self.requests: list = []

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def get(self, url, headers):
                self.requests.append((url, headers))
                gate.wait(timeout=5.0)
                return _FakeResponse(
                    200,
                    {
                        "kid": kid,
                        "userId": "u",
                        "secret": "deadbeef" * 8,
                        "issuedAt": "2026-04-30T00:00:00Z",
                    },
                )

        client = _GatedClient()
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )

        leader_result = [None]

        def _leader():
            leader_result[0] = resolver.resolve(kid)

        leader_thread = threading.Thread(target=_leader)
        leader_thread.start()
        # Give leader time to enter the fetch.
        _time.sleep(0.05)
        # Operator flushes WHILE fetch is in flight.
        flush_result = resolver.flush(kid)
        # Release the fetch.
        gate.set()
        leader_thread.join(timeout=5.0)

        # Flush correctly observed the in-flight fetch.
        assert flush_result is True, "flush() should report state-change"
        # Cache write was suppressed — next resolve refetches.
        # Verify by inspecting the cache directly: should be empty.
        with resolver._lock:
            assert kid not in resolver._cache, (
                "post-fetch write should have been suppressed by flush"
            )


class TestHttpResolverDefenceInDepth:
    def test_malformed_kid_not_fetched(self, monkeypatch) -> None:
        client = _FakeClient([])  # empty — would crash if called
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        # Newline in kid — would corrupt the canonical-string boundary.
        assert resolver.resolve("evil\nkid") is None
        assert resolver.resolve("") is None
        assert client.requests == []

    def test_response_kid_mismatch_rejected(self, monkeypatch) -> None:
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-DIFFERENT_KID_THAN_ASKED_FOR",
                    "userId": "u1",
                    "secret": "deadbeef" * 8,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        # Response says kid X but we asked for kid Y → reject.
        assert resolver.resolve("usr-aaaaaaaaaaaaaaaaaaaaaaaa") is None

    def test_malformed_response_returns_none(self, monkeypatch) -> None:
        client = _FakeClient([
            _FakeResponse(200, {"kid": "x"})  # missing userId, secret
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        assert resolver.resolve("usr-aaaaaaaaaaaaaaaaaaaaaaaa") is None

    def test_empty_user_id_rejected(self, monkeypatch) -> None:
        # PR #709 round 5+3 MED: ``isinstance(user_id, str)`` alone
        # passes empty strings; a dashboard bug returning
        # ``userId: ""`` would otherwise create a per_user KeyInfo
        # with ``bound_user_id=""`` and cache it for the full TTL.
        # Reject as transient, like the empty-secret case.
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "",  # empty
                    "secret": "deadbeef" * 8,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        assert resolver.resolve("usr-aaaaaaaaaaaaaaaaaaaaaaaa") is None
        # No cache entry written (transient path).
        with resolver._lock:
            assert "usr-aaaaaaaaaaaaaaaaaaaaaaaa" not in resolver._cache

    def test_500_returns_none_no_cache(self, monkeypatch) -> None:
        clock = _Clock()
        client = _FakeClient([
            _FakeResponse(500),
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u1",
                    "secret": "deadbeef" * 8,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            ),
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key", clock=clock
        )
        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        assert resolver.resolve(kid) is None
        # No cache on 500 — next call retries.
        assert resolver.resolve(kid) is not None
        assert len(client.requests) == 2

    def test_secret_below_minimum_length_rejected(self, monkeypatch) -> None:
        # PR #709 round 2 LOW (L1): a dashboard bug returning a
        # short ``secret`` would yield predictable HMAC tags.
        # Reject as a transient (let the next request retry —
        # if the dashboard fixes itself we recover).
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u",
                    "secret": "",  # empty
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        assert resolver.resolve("usr-aaaaaaaaaaaaaaaaaaaaaaaa") is None

    def test_secret_short_but_nonempty_also_rejected(self, monkeypatch) -> None:
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u",
                    "secret": "ab" * 8,  # 16 chars — below 32-min
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        assert resolver.resolve("usr-aaaaaaaaaaaaaaaaaaaaaaaa") is None

    def test_secret_non_hex_rejected(self, monkeypatch) -> None:
        # PR #709 round 3 LOW: length-only check passes UUIDs
        # (36 chars), base64 (40 chars for 32 bytes), truncated
        # JSON (any length). If the cloud uses raw UTF-8 bytes
        # but the agent decodes hex (or vice versa), every
        # request 401s with no actionable diagnostic. Catch the
        # format mismatch at the boundary.
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u",
                    # 64 chars but contains non-hex 'g' — passes
                    # length but fails hex validation.
                    "secret": "g" * 64,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        assert resolver.resolve("usr-aaaaaaaaaaaaaaaaaaaaaaaa") is None

    def test_secret_uuid_rejected(self, monkeypatch) -> None:
        # UUID has 36 chars — passes the 32 min — but contains '-'.
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u",
                    "secret": "12345678-1234-1234-1234-123456789012",
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        assert resolver.resolve("usr-aaaaaaaaaaaaaaaaaaaaaaaa") is None

    def test_signed_then_verified_via_resolver_sourced_key(
        self, monkeypatch
    ) -> None:
        """PR #709 round 4 HIGH regression: end-to-end sign-then-verify
        through a resolver-sourced key. Catches the
        ``secret.encode("utf-8")`` vs ``bytes.fromhex(secret)``
        bug — without this test, CI had no signal that
        resolver-fetched keys would produce verifiable signatures."""
        import hashlib
        import hmac as _hmac

        from watercooler_mcp.auth.hmac_keys import (
            build_v3_canonical_string,
            verify_v3_signature,
        )

        # 32 random bytes as hex (the dashboard issuance shape).
        secret_hex = "deadbeefcafebabe" * 4  # 64 hex chars = 32 bytes
        secret_bytes = bytes.fromhex(secret_hex)

        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": kid,
                    "userId": "alice",
                    "secret": secret_hex,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )

        # Resolve from the dashboard.
        resolved = resolver.resolve(kid)
        assert resolved is not None
        # The resolver decoded hex to 32 bytes (per round 4 fix).
        assert resolved.secret == secret_bytes
        assert len(resolved.secret) == 32

        # Sign using the same hex-decoded bytes the agent would
        # use (the agent calls ``bytes.fromhex(secret)`` before
        # passing to HMAC).
        canonical = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="2026-04-30T00:00:00Z",
            key_id=kid,
            user_id="alice",
            body=b'{"jsonrpc":"2.0","method":"tools/list"}',
            x_repo="org/repo",
            x_branch="main",
        )
        sig = _hmac.new(secret_bytes, canonical, hashlib.sha256).hexdigest()

        # Verify using the resolver-fetched secret. The whole point
        # of the round-4 fix: this MUST verify true.
        assert verify_v3_signature(
            canonical=canonical,
            signature_hex=sig,
            secret=resolved.secret,
        ) is True

    def test_secret_uppercase_hex_accepted(self, monkeypatch) -> None:
        # Defensive: accept both cases, in case a future issuer
        # emits uppercase hex. Pinned so a tightening to lowercase-
        # only would be visible.
        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u",
                    "secret": "DEADBEEF" * 8,  # 64 chars uppercase hex
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example", api_key="svc-key"
        )
        result = resolver.resolve("usr-aaaaaaaaaaaaaaaaaaaaaaaa")
        assert result is not None
        # Hex-decoded to bytes (PR #709 round 4 HIGH).
        assert result.secret == bytes.fromhex("DEADBEEF" * 8)

    def test_expiry_uses_post_fetch_clock(self, monkeypatch) -> None:
        # PR #709 round 2 LOW (L2): expiry is computed from the
        # clock value AFTER the HTTP fetch completes, not before.
        # Slow fetches against tight TTLs would otherwise produce
        # measurably-short effective TTLs.
        clock_values = iter([1000.0, 1005.0])  # +5s during the "fetch"

        def _stepping_clock():
            return next(clock_values)

        client = _FakeClient([
            _FakeResponse(
                200,
                {
                    "kid": "usr-aaaaaaaaaaaaaaaaaaaaaaaa",
                    "userId": "u",
                    "secret": "deadbeef" * 8,
                    "issuedAt": "2026-04-30T00:00:00Z",
                },
            )
        ])
        _install_fake_httpx(monkeypatch, client)
        resolver = HttpResolver(
            base_url="https://dash.example",
            api_key="svc-key",
            cache_ttl_s=300.0,
            clock=_stepping_clock,
        )
        kid = "usr-aaaaaaaaaaaaaaaaaaaaaaaa"
        # First call: read_cache uses clock=1000 (miss); fetch
        # runs; post-fetch clock is 1005; expiry = 1005 + 300 = 1305.
        assert resolver.resolve(kid) is not None
        with resolver._lock:
            entry = resolver._cache[kid]
        _value, expiry = entry
        assert expiry == 1305.0, (
            f"expected expiry computed from post-fetch clock (1005+300=1305), "
            f"got {expiry}"
        )


class TestLoadResolverFromEnv:
    def test_unconfigured_returns_none(self) -> None:
        # Both env vars unset → no resolver.
        assert load_resolver_from_env({}) is None

    def test_url_only_returns_none(self) -> None:
        assert load_resolver_from_env(
            {"WATERCOOLER_HMAC_KEY_RESOLVER_URL": "https://dash.example"}
        ) is None

    def test_api_key_only_returns_none(self) -> None:
        assert load_resolver_from_env(
            {"WATERCOOLER_HMAC_KEY_RESOLVER_API_KEY": "svc-key"}
        ) is None

    def test_both_set_builds_resolver(self) -> None:
        r = load_resolver_from_env({
            "WATERCOOLER_HMAC_KEY_RESOLVER_URL": "https://dash.example/",
            "WATERCOOLER_HMAC_KEY_RESOLVER_API_KEY": "svc-key",
        })
        assert isinstance(r, HttpResolver)
        # Trailing slash trimmed.
        assert r._base_url == "https://dash.example"

    def test_http_base_url_rejected(self) -> None:
        # PR #709 round 5+1 LOW: ``http://`` base_url would leak
        # the X-API-Key on every fetch. Reject at constructor.
        import pytest

        with pytest.raises(ValueError, match="https"):
            HttpResolver(
                base_url="http://dash.example",
                api_key="svc-key",
            )

    def test_https_base_url_accepted(self) -> None:
        # Sanity: the standard production case works.
        r = HttpResolver(
            base_url="https://dash.example",
            api_key="svc-key",
        )
        assert r._base_url == "https://dash.example"

    def test_http_localhost_allowed_for_dev(self) -> None:
        # Loopback exception — local-dev convenience; the leak
        # vector requires network observability and loopback
        # isn't observable.
        for url in (
            "http://localhost:3000",
            "http://127.0.0.1:8080",
            "http://[::1]:3000",
        ):
            r = HttpResolver(base_url=url, api_key="svc-key")
            assert r._base_url == url

    def test_non_loopback_http_still_rejected(self) -> None:
        import pytest

        # Hostnames containing "localhost" but not equal to it
        # do NOT bypass the check — defence-in-depth against a
        # crafted ``http://localhost.attacker.example`` that
        # would leak the API key off-host.
        with pytest.raises(ValueError, match="https"):
            HttpResolver(
                base_url="http://localhost.attacker.example",
                api_key="svc-key",
            )

    def test_unknown_scheme_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="https"):
            HttpResolver(
                base_url="ftp://dash.example",
                api_key="svc-key",
            )

    def test_bad_float_falls_back_to_default(self) -> None:
        r = load_resolver_from_env({
            "WATERCOOLER_HMAC_KEY_RESOLVER_URL": "https://dash.example",
            "WATERCOOLER_HMAC_KEY_RESOLVER_API_KEY": "svc-key",
            "WATERCOOLER_HMAC_KEY_RESOLVER_TIMEOUT_S": "not-a-float",
        })
        assert isinstance(r, HttpResolver)
        assert r._timeout_s == 5.0  # default
