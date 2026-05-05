"""HTTP-backed ``KeyResolver`` for per-user HMAC v3 keys.

Plan v5.1 Sprint 3 Stage 2 C1 — per-user keys are issued by the
dashboard, not env-configured, so they cannot live in the env-loaded
``KeyRegistry._keys`` map. This resolver fetches them from the
dashboard's ``GET /api/mcp/hmac-key/<kid>`` endpoint with positive +
negative TTL caching so the per-request hot path doesn't pay a
round-trip on every cache hit.

Configuration is via env vars (so the resolver is configured the
same way the rest of the cloud-side service-auth surface is
configured):

* ``WATERCOOLER_HMAC_KEY_RESOLVER_URL`` — base URL of the
  dashboard's API. The full endpoint is
  ``<base>/api/mcp/hmac-key/<kid>``. Unset → resolver disabled
  (no HTTP fetches; lookup falls through to env-only).
* ``WATERCOOLER_HMAC_KEY_RESOLVER_API_KEY`` — service auth secret;
  sent as ``X-API-Key``. Same shared secret the rest of the
  token-service surface uses (``WATERCOOLER_TOKEN_API_KEY`` on
  the dashboard side).
* ``WATERCOOLER_HMAC_KEY_RESOLVER_TIMEOUT_S`` — per-request
  timeout. Default 5s.
* ``WATERCOOLER_HMAC_KEY_RESOLVER_CACHE_TTL_S`` — positive cache
  TTL for resolved keys. Default 300s (5 min). Trade-off:
  shorter = faster propagation of revokes, longer = fewer
  dashboard hits.
* ``WATERCOOLER_HMAC_KEY_RESOLVER_NEGATIVE_TTL_S`` — negative
  cache TTL for missed kids (404). Default 30s. Short to keep
  unknown-kid storms from hammering the dashboard while not
  silently masking a kid that just got issued.

Cache eviction: lazy — entries past their expiry are removed on
the next ``resolve`` call for that kid. The cache is bounded by
the number of distinct kids; typical fleets have O(users) kids
which is fine in-memory. A future hard-bound LRU is a follow-up
if the cache grows unboundedly under adversarial-kid-storm.

Failure mode: HTTP 500 / connection error / timeout returns
``None`` (not cached) — the next request retries. This means a
brief dashboard outage doesn't poison the cache; it just makes
the cloud-side fall through to env-only (or 401 in enforce
mode) for the duration.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Optional, Tuple

from .hmac_keys import KeyInfo, KeyResolver, _is_valid_key_id

logger = logging.getLogger(__name__)


# Hex-string charset (lowercase or uppercase). The dashboard's
# issuance helper produces ``randomBytes(32).toString("hex")`` —
# 64 lowercase hex chars. The cloud-side accepts both cases here
# defensively in case a future issuer emits uppercase.
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


# Sentinel for negative-cache entries (kid was 404'd).
_NEGATIVE = object()

# Sentinel for "not in cache (or expired)" — distinct from None
# (which means "negative cache hit, kid is known-unknown") so the
# resolve() coordinator can distinguish "miss → fetch" from
# "definite-no, return None".
_CACHE_MISS = object()


# Minimum acceptable secret length (in characters of the hex
# plaintext). PR #709 round 2 LOW: a dashboard bug returning
# ``secret: ""`` would yield ``hmac.new(b"", ...)`` — a known
# zero-length-key HMAC tag is predictable for any input. The
# dashboard issues 32-byte (64-hex-char) secrets via
# ``randomBytes(32).toString("hex")``; reject anything shorter
# than 32 hex chars (16 bytes — the HMAC-SHA256 spec floor for
# meaningful security). A length floor is defence-in-depth: the
# real fix is making sure the dashboard never emits short
# secrets, but the resolver is the last layer between a buggy
# upstream and a request being authenticated.
_MIN_SECRET_HEX_LEN = 32


# Loopback hosts permitted for ``http://`` resolver URLs (PR #709
# round 5+1 LOW). The leak vector for plaintext-API-key requires
# network observability; loopback isn't observable from outside
# the host so allowing http://localhost during dev is safe.
#
# Values are what ``urllib.parse.urlsplit().hostname`` returns —
# brackets are already stripped from IPv6 literals (so ``[::1]``
# in the URL becomes ``"::1"`` here). PR #709 round 5+2 LOW: the
# previous version had ``"[::1]"`` in the tuple and an inline
# duplicate in ``_is_https_or_loopback``; both were dead code
# (the constant was never referenced) and the bracketed entry
# would never have matched. Now the function uses the constant
# as its single source of truth.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _scheme_and_host(url: str) -> str:
    """Extract just the scheme + host for diagnostic messages,
    avoiding accidental log-leak of the full path."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _is_https_or_loopback(url: str) -> bool:
    """True if the URL uses ``https://`` or is an ``http://`` URL
    pointing at a loopback host (localhost / 127.0.0.1 / ::1)."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    # Loopback exception. ``urlsplit().hostname`` strips brackets
    # from IPv6 literals and lowercases the host, so direct
    # membership in ``_LOOPBACK_HOSTS`` is the right check.
    host = parts.hostname or ""
    return host in _LOOPBACK_HOSTS


class _TransientFetchError(Exception):
    """Raised by ``_fetch`` for transient failures (500 / timeout /
    connection error / malformed response). The caller treats these
    as "no result, don't cache" — distinct from 404 which IS cached
    (negative TTL).
    """


class HttpResolver(KeyResolver):
    """Fetch per-user HMAC v3 keys from the dashboard with TTL cache.

    Args:
        base_url: Dashboard base URL. The endpoint is
            ``<base_url>/api/mcp/hmac-key/<kid>``.
        api_key: Service auth secret sent as ``X-API-Key``.
        timeout_s: Per-request timeout. Default 5.0.
        cache_ttl_s: Positive cache TTL. Default 300.
        negative_ttl_s: Negative cache TTL. Default 30.
        clock: Time source (``time.time`` by default; injected for tests).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float = 5.0,
        cache_ttl_s: float = 300.0,
        negative_ttl_s: float = 30.0,
        clock=time.time,
    ) -> None:
        if not base_url:
            raise ValueError("HttpResolver requires base_url")
        if not api_key:
            raise ValueError("HttpResolver requires api_key")
        # PR #709 round 5+1 LOW: enforce HTTPS for the resolver
        # URL. The ``X-API-Key`` service-auth secret is sent on
        # every per-kid fetch; an operator who configured an
        # ``http://`` base_url would leak that secret on every
        # request. The dashboard production URL is HTTPS by
        # construction (Vercel / production CDN); rejecting
        # plaintext at the constructor catches a misconfiguration
        # before the first request flows.
        #
        # Local-dev / test exception: allow ``http://localhost``
        # / ``http://127.0.0.1`` / ``http://[::1]`` (and their
        # port-suffixed forms). Loopback isn't observable on the
        # network so the leak vector doesn't apply, and forcing
        # operators to terminate SSL on their dev box is friction
        # without a security benefit.
        if not _is_https_or_loopback(base_url):
            raise ValueError(
                f"HttpResolver base_url must use https:// (got: "
                f"{_scheme_and_host(base_url)!r}). The X-API-Key "
                "service-auth secret is sent on every fetch and "
                "must not transit plaintext. Loopback http:// is "
                "permitted for local development only."
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._negative_ttl_s = negative_ttl_s
        self._clock = clock
        # Cache: kid -> (KeyInfo|_NEGATIVE, expiry_ts)
        self._cache: dict[str, Tuple[object, float]] = {}
        self._lock = threading.Lock()
        # In-flight fetch coordination per kid (PR #709 round 1
        # MED). N concurrent ``resolve`` calls for the same novel
        # kid would otherwise all see a cache miss and call
        # ``_fetch`` simultaneously, amplifying a burst of M
        # requests into M HTTP fetches against the dashboard. With
        # in-flight events, only the first request fetches; the
        # rest wait on the event and re-read the cache.
        self._inflight: dict[str, threading.Event] = {}
        # Revoked-during-flight kids (PR #709 round 2 MED).
        # Race: lookup misses, leader starts fetch; meanwhile
        # operator calls revoke + flush; cache is empty so
        # flush no-ops; fetch completes and would write the
        # now-revoked key into the cache for the full TTL.
        # Recording the kid here lets the post-fetch cache-write
        # see "revoked during this fetch — skip the write".
        # Membership is short-lived: once the leader observes
        # the entry and skips the write, it removes the kid.
        # In practice the set holds at most a handful of kids
        # at any moment (one per concurrent revoke-during-fetch).
        self._revoked_inflight: set[str] = set()

    def resolve(self, key_id: str) -> Optional[KeyInfo]:
        # Defence-in-depth: refuse to fetch for malformed kids. The
        # cloud-side ``parse_v3_authorization_header`` already
        # validates kid charset, but rejecting again here means a
        # future caller invoking ``resolve`` directly can't probe
        # the dashboard with arbitrary strings.
        if not _is_valid_key_id(key_id):
            return None

        now = self._clock()
        # Cache check (fast path).
        cached = self._read_cache(key_id, now)
        if cached is not _CACHE_MISS:
            return cached  # type: ignore[return-value]

        # Cache miss. Coordinate so concurrent requests for the
        # same kid don't all hit the dashboard. Leader-vs-follower
        # is decided atomically under ``_lock``: the request that
        # finds ``_inflight[kid]`` empty installs an Event and
        # becomes the leader; everyone else finds the Event and
        # becomes a follower.
        with self._lock:
            inflight = self._inflight.get(key_id)
            if inflight is None:
                inflight = threading.Event()
                self._inflight[key_id] = inflight
                am_leader = True
            else:
                am_leader = False

        if am_leader:
            try:
                self._do_fetch_and_cache(key_id)
            finally:
                # Remove the inflight marker BEFORE setting the
                # event so a follower waking up sees no inflight
                # entry (preventing it from waiting on a stale
                # event after its own re-read).
                #
                # PR #709 round 5 LOW: ALSO discard any
                # ``_revoked_inflight`` marker for this kid. Race
                # window: ``_do_fetch_and_cache`` has returned
                # (its own ``finally`` cleared the marker) but
                # ``_inflight[kid]`` is still set in the window
                # between fetch-end and this ``finally``. A
                # ``flush()`` arriving in that gap sees the
                # in-flight entry, adds the kid back to
                # ``_revoked_inflight``, and there's no live
                # fetch to clear it. The next leader would fetch
                # successfully but skip its cache write because
                # of the stale marker — denying one cycle of
                # legitimate auth. Discarding here guarantees the
                # marker's lifetime is bounded by the in-flight
                # window observable to flush().
                with self._lock:
                    self._inflight.pop(key_id, None)
                    self._revoked_inflight.discard(key_id)
                inflight.set()
            return self._read_cache_unconditional(key_id)

        # Follower: wait for the leader's fetch to complete, then
        # re-read the cache. Bound the wait to the leader's max
        # fetch time + a small grace; if the leader's thread dies
        # before setting the event, falling through to a cache
        # re-read is safe (likely returns None, which is the
        # correct miss-on-failure outcome).
        inflight.wait(timeout=self._timeout_s + 1.0)
        return self._read_cache_unconditional(key_id)

    def flush(self, key_id: str) -> bool:
        """Evict ``key_id`` from the cache. Returns True if a
        positive or negative entry was present, OR if a fetch was
        in-flight (so the post-fetch write will be suppressed).

        Plan v5.1 Sprint 3 Stage 2 C1 + PR #709 round 1 MED — the
        cloud-side revoke path calls this to close the up-to-TTL
        propagation window between dashboard revoke and cloud-side
        acceptance. Without flush, an operator who revokes a
        compromised kid via the dashboard would still have the
        kid honoured by the cloud for up to ``cache_ttl_s``
        seconds.

        PR #709 round 2 MED: if a fetch is currently in flight
        for this kid, mark it via ``_revoked_inflight`` so the
        leader's post-completion write skips this kid. Without
        the marker, a flush() during a fetch would no-op on the
        empty cache and the post-fetch write would install the
        revoked key for the full TTL.

        The marker is ONLY set when a fetch is in flight at
        flush-time. Fetches that start AFTER the flush returns
        get the cache-already-clear state and proceed normally
        — that's what an operator wants when they flush + immediately
        re-resolve to verify the kid is reachable.
        """
        with self._lock:
            had_cache = self._cache.pop(key_id, None) is not None
            had_inflight = key_id in self._inflight
            if had_inflight:
                # Only mark when a fetch is actually running. If
                # no fetch is in flight, the cache-evict above is
                # sufficient — and marking unconditionally would
                # block a future legitimate fetch from caching.
                self._revoked_inflight.add(key_id)
        return had_cache or had_inflight

    def _read_cache(self, key_id: str, now: float):
        """Return the cached value (or _NEGATIVE → None translation),
        or ``_CACHE_MISS`` sentinel for not-cached / expired."""
        with self._lock:
            entry = self._cache.get(key_id)
            if entry is None:
                return _CACHE_MISS
            value, expiry = entry
            if expiry <= now:
                # Expired — evict and treat as miss.
                del self._cache[key_id]
                return _CACHE_MISS
            if value is _NEGATIVE:
                return None
            if isinstance(value, KeyInfo):
                return value
            # Cache invariant violation. Better to fail loudly than
            # to return garbage. PR #709 round 1 LOW: previously
            # this branch used ``assert`` which is stripped under
            # ``-O``. Explicit raise so the invariant holds in
            # production builds too.
            raise RuntimeError(
                f"HttpResolver cache invariant violated for kid {key_id!r}: "
                f"value type {type(value).__name__}"
            )

    def _read_cache_unconditional(self, key_id: str) -> Optional[KeyInfo]:
        """After a fetch completes, re-read the cache without
        checking expiry. The leader just populated it; followers
        read whatever's there. Returns None if the leader's fetch
        produced a negative-cache entry or was a transient failure
        (no entry written).

        PR #709 round 4 MED: the lock is held through the FULL
        check-and-return so a concurrent ``flush()`` cannot evict
        the entry between the dict read and the return. Previously
        the entry tuple was unpacked outside the lock — semantically
        a just-revoked key could still be returned once to a
        follower thread before the cache eviction was observed.
        """
        with self._lock:
            entry = self._cache.get(key_id)
            if entry is None:
                # Transient fetch failure — no cache entry. Caller
                # treats as miss.
                return None
            value, _expiry = entry
            if value is _NEGATIVE:
                return None
            if isinstance(value, KeyInfo):
                return value
            raise RuntimeError(
                f"HttpResolver cache invariant violated for kid {key_id!r}: "
                f"value type {type(value).__name__}"
            )

    def _do_fetch_and_cache(self, key_id: str) -> None:
        """Fetch from the dashboard and update the cache. Called
        as the in-flight leader; followers will re-read the cache
        after this returns.

        PR #709 round 2 LOW (L2): expiry is computed from the
        clock value AFTER the fetch completes, not before — for a
        slow fetch against a tight TTL the pre-fetch ``now`` would
        produce a measurably-short effective TTL.
        """
        # Distinguish three outcomes:
        # - ``KeyInfo``: dashboard confirmed the kid → cache positive.
        # - ``None``: dashboard 404'd → cache negative (kid is unknown,
        #   don't hammer the dashboard re-checking).
        # - ``_TransientFetchError``: dashboard 500'd or the connection
        #   failed → DON'T cache; the next request retries. Transient
        #   failures must not poison the cache, otherwise a brief
        #   dashboard outage would lock out v3 for the whole TTL.
        try:
            try:
                result = self._fetch(key_id)
            except _TransientFetchError:
                return
            # Clock AFTER the fetch — see L2 docstring above.
            now = self._clock()
            with self._lock:
                # PR #709 round 2 MED: skip the cache write if a flush
                # arrived during the fetch. Without this, the
                # post-fetch write would install a freshly-revoked key
                # for the full TTL — exactly the bug flush() is
                # supposed to prevent.
                if key_id in self._revoked_inflight:
                    logger.info(
                        "HMAC v3 HttpResolver: kid %r was flushed during "
                        "in-flight fetch; skipping cache write to honour "
                        "the revoke",
                        key_id,
                    )
                    return
                if result is None:
                    self._cache[key_id] = (_NEGATIVE, now + self._negative_ttl_s)
                else:
                    self._cache[key_id] = (result, now + self._cache_ttl_s)
        finally:
            # PR #709 round 3 MED: discard the marker on EVERY exit
            # path, including the transient-error early return.
            # The previous code only cleared it in the success
            # branch — a transient error followed by a flush()
            # would leave the marker set, and the next valid
            # fetch would hit the check, skip the cache write,
            # and silently deny the kid. Cleaning up in ``finally``
            # makes the marker's lifetime exactly "this fetch's
            # in-flight window."
            with self._lock:
                self._revoked_inflight.discard(key_id)

    def _fetch(self, key_id: str) -> Optional[KeyInfo]:
        """One-shot fetch against the dashboard, no caching layer.

        Returns:
            ``KeyInfo`` — definite hit, dashboard confirmed kid.
            ``None`` — definite miss, dashboard 404'd. Caller
                negative-caches.

        Raises:
            ``_TransientFetchError`` — transient failure (500 /
                connection error / timeout / malformed response).
                Caller does NOT cache; next request retries.
        """
        try:
            import httpx  # local import — keeps the cloud's startup
            # path independent of httpx for tests / deployments
            # that disable the resolver.
        except ImportError:  # pragma: no cover
            logger.warning(
                "HMAC v3 HttpResolver: httpx unavailable; resolver disabled"
            )
            raise _TransientFetchError("httpx unavailable")

        url = f"{self._base_url}/api/mcp/hmac-key/{key_id}"
        headers = {"X-API-Key": self._api_key}
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                resp = client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            logger.warning(
                "HMAC v3 HttpResolver: fetch for kid %r failed: %s",
                key_id,
                exc,
            )
            raise _TransientFetchError(str(exc)) from exc

        if resp.status_code == 404:
            # Negative cache — dashboard says no such kid.
            return None
        if resp.status_code != 200:
            logger.warning(
                "HMAC v3 HttpResolver: dashboard returned %d for kid %r",
                resp.status_code,
                key_id,
            )
            raise _TransientFetchError(f"HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError:
            logger.warning(
                "HMAC v3 HttpResolver: malformed JSON for kid %r", key_id
            )
            raise _TransientFetchError("malformed JSON")

        # Required fields: kid, userId, secret. Validate shape.
        # Malformed responses are treated as TRANSIENT — a dashboard
        # that emits garbage is broken; retrying after the cache
        # window expires is reasonable, but caching the malformed
        # state would amplify the outage.
        #
        # PR #709 round 5+3 MED: ``isinstance(user_id, str)`` alone
        # passes empty strings. A dashboard bug returning
        # ``userId: ""`` would otherwise produce a per_user
        # ``KeyInfo`` with ``bound_user_id=""`` and cache it
        # positively for the full TTL — the key is functionally
        # unusable downstream (any non-empty X-User-ID fails the
        # binding check, and empty X-User-ID is rejected earlier
        # in ``server_http.py``), but the resolver should not
        # silently accept malformed dashboard data. Mirror the
        # ``secret`` length floor by requiring ``user_id`` to be
        # non-empty alongside the type check. ``kid`` doesn't need
        # the same treatment because the ``kid != key_id`` check
        # below catches empty-string kids (``key_id`` is already
        # validated non-empty by ``_is_valid_key_id``).
        kid = data.get("kid")
        user_id = data.get("userId")
        secret = data.get("secret")
        if not (
            isinstance(kid, str)
            and isinstance(user_id, str)
            and user_id != ""
            and isinstance(secret, str)
        ):
            logger.warning(
                "HMAC v3 HttpResolver: response for kid %r missing or empty "
                "required fields (kid=%r, userId=%r, secret=<%s>)",
                key_id,
                kid,
                user_id,
                "str" if isinstance(secret, str) else type(secret).__name__,
            )
            raise _TransientFetchError("malformed response shape")

        # Defence-in-depth: refuse if the dashboard echoes back a
        # different kid than we asked for (would indicate a bug or
        # aliasing on the dashboard side). Transient — let the next
        # request retry rather than caching a wrong-key signal.
        if kid != key_id:
            logger.warning(
                "HMAC v3 HttpResolver: response kid mismatch — asked for %r, "
                "got %r; rejecting",
                key_id,
                kid,
            )
            raise _TransientFetchError("kid mismatch")

        # PR #709 round 2 LOW: minimum secret length. A dashboard
        # bug returning ``secret: ""`` or some other short string
        # would yield ``hmac.new(b"", ...)`` — predictable HMAC
        # tags for any input, regardless of message content. The
        # dashboard issues 32-byte (64-hex-char) secrets; reject
        # anything shorter than ``_MIN_SECRET_HEX_LEN`` as a
        # defence-in-depth guard against upstream bugs.
        if len(secret) < _MIN_SECRET_HEX_LEN:
            logger.warning(
                "HMAC v3 HttpResolver: rejected kid %r — secret length %d "
                "below minimum %d (dashboard upstream bug?)",
                key_id,
                len(secret),
                _MIN_SECRET_HEX_LEN,
            )
            raise _TransientFetchError("secret below minimum length")

        # PR #709 round 3 LOW: validate the secret is hex. Length
        # alone passes through UUIDs, base64, truncated JSON — any
        # 32+ char string. The dashboard's issuance helper produces
        # hex via ``randomBytes(32).toString("hex")``; if the cloud
        # accepts a non-hex string and uses its raw UTF-8 bytes as
        # the HMAC key, but the agent expects to decode hex (or
        # vice versa), every signed request would 401 with no
        # actionable diagnostic. Catch the upstream-format bug
        # here at the boundary.
        if _HEX_RE.fullmatch(secret) is None:
            logger.warning(
                "HMAC v3 HttpResolver: rejected kid %r — secret is not "
                "valid hex (dashboard upstream format mismatch?)",
                key_id,
            )
            raise _TransientFetchError("secret not valid hex")

        # PR #709 round 4 HIGH: decode hex → 32 bytes. The dashboard
        # issues secrets as ``randomBytes(32).toString("hex")`` (64-char
        # hex string), and the agent signing side decodes the same way
        # before passing to HMAC. ``secret.encode("utf-8")`` would
        # store 64 bytes of ASCII hex characters and fail every
        # signature verification — a one-line bug that would have
        # silently 401'd every resolver-sourced request in production.
        #
        # Note the contrast with ``_load_service_keys_from_env``:
        # service keys interpret the env-var string as literal UTF-8
        # bytes (operators who want hex must decode themselves before
        # setting the var). Per-user keys can't follow that convention
        # because JSON can't carry raw bytes — the dashboard ships
        # hex and both sides decode. The HMAC_CALLER_INVENTORY.md
        # convention text covers the env-var path; per-user keys are
        # documented in the dashboard issuance helper
        # (``lib/hmacKeys.ts``).
        try:
            secret_bytes = bytes.fromhex(secret)
        except ValueError as exc:
            # Unreachable in practice — ``_HEX_RE.fullmatch`` above
            # already validated the charset. Belt-and-suspenders for
            # any future divergence between the regex and the
            # ``bytes.fromhex`` parser.
            logger.warning(
                "HMAC v3 HttpResolver: bytes.fromhex failed on validated "
                "hex secret for kid %r: %s",
                key_id,
                exc,
            )
            raise _TransientFetchError("hex decode failed") from exc

        return KeyInfo(
            key_id=kid,
            secret=secret_bytes,
            key_type="per_user",
            bound_user_id=user_id,
        )


def load_resolver_from_env(
    env: Optional[dict] = None,
) -> Optional[HttpResolver]:
    """Build an ``HttpResolver`` from process env, or return None.

    Returns ``None`` if either the URL or API key env var is unset,
    so the cloud can run in env-key-only mode (today's posture)
    without configuring the resolver. Once the dashboard is ready
    to issue per-user keys, set both env vars and the resolver
    chain goes hot transparently.
    """
    if env is None:
        env = dict(os.environ)
    url = env.get("WATERCOOLER_HMAC_KEY_RESOLVER_URL", "").strip()
    api_key = env.get("WATERCOOLER_HMAC_KEY_RESOLVER_API_KEY", "").strip()
    if not url or not api_key:
        return None

    def _opt_float(name: str, default: float) -> float:
        raw = env.get(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                "HMAC v3 HttpResolver: %s=%r is not a float; using default %s",
                name,
                raw,
                default,
            )
            return default

    return HttpResolver(
        base_url=url,
        api_key=api_key,
        timeout_s=_opt_float("WATERCOOLER_HMAC_KEY_RESOLVER_TIMEOUT_S", 5.0),
        cache_ttl_s=_opt_float(
            "WATERCOOLER_HMAC_KEY_RESOLVER_CACHE_TTL_S", 300.0
        ),
        negative_ttl_s=_opt_float(
            "WATERCOOLER_HMAC_KEY_RESOLVER_NEGATIVE_TTL_S", 30.0
        ),
    )
