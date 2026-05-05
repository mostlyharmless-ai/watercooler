"""HMAC v3 per-key registry for Move 2.5 of the security consolidation.

The pre-v3 signing scheme (deleted in #733) used a single shared
secret to sign ``(user_id, timestamp, body)``. ``X-Repo``,
``X-Branch``, and *which key* were not part of the authenticated
request — letting any holder of the secret assert any
``X-User-ID`` and any ``X-Repo``.

v3 closes that surface:

* Each verifying key has a unique ``key_id`` referenced in the
  ``Authorization`` header.
* The canonical string includes ``method``, ``path``, ``key_id``,
  ``X-Repo``, and ``X-Branch`` in addition to the legacy fields, so
  tampering with those headers invalidates the signature.
* Subject-binding (which ``X-User-ID`` a key may sign for) and
  repo-authorisation (which ``X-Repo`` a key may access) are
  enforced at lookup time, not derived from the request itself.

This module exposes:

* :class:`KeyInfo` — frozen dataclass describing a registered key.
* :class:`KeyRegistry` — thread-safe in-memory registry; the only
  surface callers need.
* :func:`load_default_registry` — builds a registry from process env
  vars (service keys; per-user keys are resolved via the dashboard
  HTTP resolver).
* :func:`build_v3_canonical_string` — the canonical-string builder.
* :func:`parse_v3_authorization_header` — parses
  ``HMAC-SHA256 v=3 kid=<id> sig=<hex>``.
* :func:`verify_v3_signature` — constant-time HMAC compare.
* :func:`hmac_v3_startup_fail_fast_check` — used by the server at
  startup to refuse to boot in a configuration that violates the
  multi-tenant invariant. With the legacy global secret retired,
  this is a structural guard the server passes
  ``has_global_secret=False`` against.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import FrozenSet, Literal, Optional, Tuple

logger = logging.getLogger(__name__)


# Header value used to identify v3 requests:
#   ``Authorization: HMAC-SHA256 v=3 kid=<key_id> sig=<hex>``
_V3_AUTH_SCHEME = "HMAC-SHA256"
_V3_VERSION = "3"


# Legacy v2 global-secret key_id. The plan v5.1 documents this as a
# back-compat shim: the global secret behaves like a per-user key
# whose ``bound_user_id`` matches whatever ``X-User-ID`` was signed.
# Only active when ``WATERCOOLER_HMAC_REQUIRE_V3`` is unset/warn.
GLOBAL_LEGACY_KEY_ID = "legacy-global-v2"


# Permitted key_id charset. Enforced at registry-load time so a
# malformed key_id can never enter the canonical string — that
# string is newline-delimited, and a key_id containing ``\n`` would
# silently corrupt field boundaries for any request using that key.
# Restricting to ``[A-Za-z0-9_-]`` matches the env-var naming
# convention and the contract documented in
# ``docs/HMAC_CALLER_INVENTORY.md``.
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Hex-only signature charset, exactly 64 chars (SHA-256 HMAC
# output is always 32 bytes = 64 hex chars). Rejected at parse
# time so length and charset mismatches are surfaced before any
# HMAC work. ``compare_digest`` would catch them anyway after a
# full compute; this is just an early-fail
# (PR #703 round 4 / round 6 LOW findings).
_SIG_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Reserved suffixes for the env-var metadata namespace. The loader
# scans for ``WATERCOOLER_HMAC_KEY_<id>_SECRET`` and extracts
# ``<id>``; if the extracted id itself ends with a reserved
# metadata suffix (e.g. operator set
# ``WATERCOOLER_HMAC_KEY_foo_TYPE_SECRET``), the loader cannot
# tell whether ``<id>=foo_TYPE`` is intended or whether it's the
# operator confusing the env-var schema. Reject up front
# (PR #703 round 4 LOW finding).
_RESERVED_KEY_ID_SUFFIXES = (
    "_TYPE",
    "_SERVICE_IDENTITY",
    "_DELEGATION",
    "_REPOS",
)


def _is_valid_key_id(key_id: str) -> bool:
    """Return True if *key_id* is safe to register.

    Rejects empty strings, the retired legacy-global sentinel
    (kept reserved so the constant cannot be reissued as an active
    v3 kid), any key containing characters outside
    ``[A-Za-z0-9_-]`` (notably whitespace, newlines, and ``=``/`` ``
    which would corrupt the Authorization-header parser), and any
    key whose name ends with a reserved metadata suffix (which
    would create env-var namespace ambiguity).
    """
    if not key_id or key_id == GLOBAL_LEGACY_KEY_ID:
        return False
    if _KEY_ID_RE.fullmatch(key_id) is None:
        return False
    if any(key_id.endswith(suffix) for suffix in _RESERVED_KEY_ID_SUFFIXES):
        return False
    return True


KeyType = Literal["per_user", "service"]


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """Registry entry for a single HMAC v3 verifying key.

    Attributes:
        key_id: Unique identifier carried in the Authorization header.
        secret: Raw bytes used in the HMAC-SHA256 computation.
        key_type: ``per_user`` (one user) or ``service`` (server-side).
        bound_user_id: For ``per_user``: the only X-User-ID this key
            may sign for. ``None`` only on the legacy global key.
        delegation_allow_list: For ``service``: the X-User-IDs the
            key may delegate for. ``None`` means
            ``no_user_delegation`` — X-User-ID must equal the
            ``service_identity`` (the key's own bound subject).
        service_identity: For ``service``: the X-User-ID used when
            ``no_user_delegation`` is in effect.
        repo_allow_list: For ``per_user``: usually ``None`` — the
            allowed repos are derived from the token's ``repos``
            claim at verification time. For ``service``: the
            server-configured allow_list of repos.
        revoked: Soft revocation; revoked keys are treated as missing.
    """

    key_id: str
    secret: bytes
    key_type: KeyType
    bound_user_id: Optional[str] = None
    delegation_allow_list: Optional[FrozenSet[str]] = None
    service_identity: Optional[str] = None
    repo_allow_list: Optional[FrozenSet[str]] = None
    revoked: bool = False

    def __post_init__(self) -> None:
        if not self.key_id:
            raise ValueError("KeyInfo.key_id required")
        if not isinstance(self.secret, (bytes, bytearray)):
            raise TypeError("KeyInfo.secret must be bytes")
        if self.key_type not in ("per_user", "service"):
            raise ValueError(
                f"KeyInfo.key_type must be per_user|service, got {self.key_type!r}"
            )
        if self.key_type == "service" and self.service_identity is None:
            raise ValueError("service keys require service_identity")


class KeyResolver:
    """Pluggable resolver for kids not in the in-memory registry.

    Plan v5.1 Sprint 3 Stage 2 C1 — per-user HMAC v3 keys are issued
    by the dashboard and not env-configured, so the env-loaded
    ``KeyRegistry._keys`` cannot hold them. ``KeyRegistry.lookup``
    consults its registered fallback resolver(s) when the in-memory
    map misses, allowing per-user keys to be fetched from the
    dashboard's ``/api/mcp/hmac-key/<kid>`` endpoint with TTL
    caching.

    Implementations MUST:
    - Return the resolved ``KeyInfo`` for known kids.
    - Return ``None`` for unknown kids (let the caller decide
      whether to try further resolvers or 401).
    - Be thread-safe — ``KeyRegistry.lookup`` may be called from
      multiple ASGI workers concurrently.
    - Cache aggressively — ``lookup`` is on the per-request hot
      path. A naive resolver that hits the dashboard on every
      lookup would add an HTTP round-trip per signed request.
    - Implement ``flush(key_id)`` so ``KeyRegistry.revoke`` can
      forcibly evict a cached entry when an operator revokes a
      compromised kid. Without this, a revoke-on-the-cloud-side
      would have no immediate effect on resolver-cached keys —
      the kid would stay valid until the resolver's TTL expired
      (PR #709 round 1 MED).
    """

    def resolve(self, key_id: str) -> Optional["KeyInfo"]:  # pragma: no cover
        raise NotImplementedError

    def flush(self, key_id: str) -> bool:  # pragma: no cover
        """Evict ``key_id`` from any local cache. Return True if
        the resolver had the kid cached (positive or negative).

        PR #709 round 4 LOW: raises ``NotImplementedError`` rather
        than defaulting to ``False``. A subclass that caches keys
        but forgets to override ``flush`` would otherwise silently
        report "no state changed" to ``KeyRegistry.revoke`` —
        and the cache would not be cleared. Cacheless subclasses
        must explicitly opt out by overriding to return ``False``.
        Matches the discipline ``resolve`` already enforces.
        """
        raise NotImplementedError


class KeyRegistry:
    """Thread-safe in-memory key registry with optional fallback resolvers.

    The lookup surface is the only behavior callers rely on. Loading
    is split out so test fixtures can build a registry without
    touching env vars, and so the dashboard fetch (Stage 2 C1) can
    populate additional keys at runtime via the resolver chain.

    Resolver chain semantics:
    - ``add(KeyInfo)`` populates the in-memory ``_keys`` (the env
      registry path).
    - ``add_resolver(KeyResolver)`` registers a fallback that
      ``lookup`` consults on a miss. Resolvers are tried in
      registration order; the first non-``None`` result wins.
    - Resolvers are NOT consulted for known kids in ``_keys``.
      Service keys configured via env vars take precedence over
      anything the dashboard might have for the same kid (rare,
      but the env path is the operator's explicit declaration).
    """

    def __init__(self) -> None:
        self._keys: dict[str, KeyInfo] = {}
        self._resolvers: list[KeyResolver] = []
        self._lock = threading.Lock()

    def add(self, info: KeyInfo) -> None:
        with self._lock:
            self._keys[info.key_id] = info

    def add_resolver(self, resolver: KeyResolver) -> None:
        """Register a fallback resolver for kids not in ``_keys``.

        Resolvers are tried in registration order on a miss; first
        non-``None`` result wins. Stage 2 C1 wires the
        ``HttpResolver`` (dashboard lookup) here from
        ``load_default_registry``; tests can install fakes.
        """
        with self._lock:
            self._resolvers.append(resolver)

    def lookup(self, key_id: str) -> Optional[KeyInfo]:
        """Return the key if registered and not revoked, else None.

        Fast path: in-memory ``_keys`` hit. Slow path: iterate
        registered resolvers in order; the first non-``None``
        result wins. The lookup is wrapped in a single lock
        acquire/release for the in-memory check; resolvers run
        WITHOUT the lock held to avoid serialising HTTP fetches
        across concurrent requests for different kids.
        """
        with self._lock:
            info = self._keys.get(key_id)
            if info is not None:
                if info.revoked:
                    return None
                return info
            resolvers = list(self._resolvers)
        # Run resolvers with the lock released.
        for resolver in resolvers:
            try:
                resolved = resolver.resolve(key_id)
            except Exception:
                logger.exception(
                    "HMAC v3 resolver %s raised on key_id %r — treating as miss",
                    resolver.__class__.__name__,
                    key_id,
                )
                continue
            if resolved is None:
                continue
            if resolved.revoked:
                # PR #709 round 3 MED: a revoked result from one
                # resolver MUST NOT halt the chain — a downstream
                # resolver may hold the current active entry for
                # the same kid (e.g. resolver R1's stale cache
                # vs R2's fresh fetch). Treat as a per-resolver
                # miss and continue.
                continue
            return resolved
        return None

    def revoke(self, key_id: str) -> bool:
        """Mark a key revoked. Returns True if this call changed state.

        Two layers:

        1. The in-memory ``_keys`` map (env-configured keys). A
           revoke marks the entry ``revoked=True`` so subsequent
           ``lookup`` returns None.
        2. The resolver chain (Stage 2 C1 + onwards). Any resolver
           in the chain may have the kid cached; ``flush(key_id)``
           tells each one to evict its local entry. The dashboard's
           server-side state (``UserHmacKey.revokedAt``) is what
           makes the next resolver fetch return 404 — flush is the
           cloud-side amplifier that closes the up-to-TTL window
           an operator would otherwise see between dashboard
           revoke and cloud-side acceptance.

        Returns True if EITHER layer had a state change:

        - In-memory entry transitioned active → revoked, OR
        - At least one resolver reported it had the kid cached.

        PR #703 round 7+4 LOW: a re-revoke of an already-revoked key
        previously returned ``True``. Now ``True`` iff this call
        actually changed the registered state OR flushed a cached
        resolver entry. False means "this call was a complete
        no-op" — neither layer had the kid.

        PR #709 round 1 MED: previously the in-memory branch was
        the only one consulted, so revoking a resolver-sourced kid
        silently no-op'd and the kid stayed valid until the
        resolver's TTL expired. Resolver flush closes that gap.
        """
        changed = False
        with self._lock:
            info = self._keys.get(key_id)
            if info is not None and not info.revoked:
                self._keys[key_id] = KeyInfo(
                    key_id=info.key_id,
                    secret=info.secret,
                    key_type=info.key_type,
                    bound_user_id=info.bound_user_id,
                    delegation_allow_list=info.delegation_allow_list,
                    service_identity=info.service_identity,
                    repo_allow_list=info.repo_allow_list,
                    revoked=True,
                )
                changed = True
            resolvers = list(self._resolvers)
        # Flush resolver caches with the lock released — the same
        # discipline ``lookup`` uses, so flush of a slow resolver
        # doesn't serialise other revokes.
        for resolver in resolvers:
            try:
                if resolver.flush(key_id):
                    changed = True
            except Exception:
                logger.exception(
                    "HMAC v3 resolver %s raised on flush of key_id %r",
                    resolver.__class__.__name__,
                    key_id,
                )
        return changed

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for k in self._keys.values() if not k.revoked)


# ------------------------------------------------------------------ #
# Canonical-string + signature primitives
# ------------------------------------------------------------------ #


def build_v3_canonical_string(
    *,
    method: str,
    path: str,
    timestamp: str,
    key_id: str,
    user_id: str,
    body: bytes,
    x_repo: str,
    x_branch: str,
) -> bytes:
    """Build the v3 canonical string per plan v5.1.

    Each field is newline-delimited; the body is hashed (sha256 hex)
    rather than embedded raw to keep the canonical string bounded.

        method
        path
        timestamp
        key_id
        X-User-ID
        sha256(body).hex()
        X-Repo
        X-Branch

    PR #703 round 7+3 LOW — defense-in-depth: ``key_id`` is already
    constrained by ``_is_valid_key_id`` to ``[A-Za-z0-9_-]+``, but
    the caller-controlled ``user_id``, ``x_repo``, and ``x_branch``
    fields are not. A newline-containing value would shift downstream
    fields and let a crafted request sign a different
    canonical string than the server reconstructs. HTTP/1.1 parsers
    reject bare CR/LF in header values so this is unreachable through
    standard transports, but the defense-in-depth rationale used to
    justify ``_is_valid_key_id`` applies equally. Raise ``ValueError``
    rather than silently corrupt; caller wraps with a generic 401.

    PR #703 round 7+5 LOW — extend the same guard to ``path``. A
    reverse proxy or middleware that percent-decodes ``%0a`` /
    ``%0d`` in the URL path before Starlette sees it could shift
    downstream fields the same way; ``path`` has no equivalent
    structural validation upstream. ``timestamp`` is excluded —
    the caller runs ``_validate_hmac_timestamp`` (strict ISO 8601
    + window check) before invoking this function, and ISO 8601
    cannot contain CR/LF. ``method`` is HTTP-uppercased by the
    caller and not user-controllable in any practical transport.
    ``key_id`` is already validated by ``_is_valid_key_id``.
    ``body_hash`` is sha256 hex output, no CR/LF possible.
    """
    for field_name, value in (
        ("user_id", user_id),
        ("x_repo", x_repo),
        ("x_branch", x_branch),
        ("path", path),
    ):
        if "\n" in value or "\r" in value:
            raise ValueError(
                f"v3 canonical string: {field_name} contains CR/LF "
                "(field-boundary injection attempt or malformed input)"
            )
    body_hash = hashlib.sha256(body or b"").hexdigest()
    parts = (method, path, timestamp, key_id, user_id, body_hash, x_repo, x_branch)
    return "\n".join(parts).encode("utf-8")


def verify_v3_signature(*, canonical: bytes, signature_hex: str, secret: bytes) -> bool:
    """Constant-time verification of a v3 signature.

    PR #703 round 7+2 MED: ``hexdigest()`` always returns lowercase
    but ``parse_v3_authorization_header`` accepts mixed-case hex
    (``[0-9a-fA-F]{64}``) and a unit test pins that contract. A
    case-sensitive ``compare_digest`` against ``signature_hex.upper()``
    or any uppercase-emitting library would always 401 despite a
    cryptographically correct sig. Lowercase the candidate before
    compare so verification is case-insensitive in input but still
    constant-time on the byte values.
    """
    try:
        expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_hex.lower())
    except (TypeError, ValueError, AttributeError):
        return False


def parse_v3_authorization_header(value: str) -> Optional[Tuple[str, str]]:
    """Parse ``HMAC-SHA256 v=3 kid=<key_id> sig=<hex>``.

    Returns ``(key_id, signature_hex)`` if the header is a valid v3
    Authorization; ``None`` if the header is empty / not v3 shape /
    has a malformed kid or sig.

    **Caller contract for ``None``:** when the header begins with
    ``HMAC-SHA256 `` the request is unambiguously claiming v3
    (v2 does NOT use the Authorization header for its signature).
    A ``None`` return for a v3-prefixed header therefore MUST
    result in a 401 — the caller MUST NOT fall through to v2
    HMAC verification. PR #703 round 7+5 MED: the previous
    wording said "caller falls through to v2", which was the
    opposite of the contract enforced by the only existing
    caller (``_stage_authenticate`` returns 401 explicitly).
    A future caller relying on the old wording could have
    introduced an authentication bypass — a ``HMAC-SHA256``-
    prefixed request with a malformed kid/sig falling through
    to v2 processing of unrelated headers. ``None`` for an
    *empty* or non-``HMAC-SHA256``-prefixed header is the
    "not a v3 request, try other auth methods" signal.

    Defence-in-depth: the parsed ``kid`` is validated against
    :data:`_KEY_ID_RE` before being returned. The registry-load
    path already rejects malformed key_ids, so a kid containing
    e.g. a literal newline cannot match a registered key — but
    refusing it at the parse layer too removes the implicit
    dependency on registry miss and makes the invariant local.
    """
    if not value:
        return None
    if not value.startswith(_V3_AUTH_SCHEME + " "):
        return None
    rest = value[len(_V3_AUTH_SCHEME) + 1 :].strip()
    parts: dict[str, str] = {}
    for token in rest.split():
        if "=" not in token:
            return None
        k, v = token.split("=", 1)
        parts[k] = v
    if parts.get("v") != _V3_VERSION:
        return None
    key_id = parts.get("kid")
    sig = parts.get("sig")
    if not key_id or not sig:
        return None
    if not _is_valid_key_id(key_id):
        # Reject at parse layer regardless of registry contents —
        # see docstring above.
        return None
    if not _SIG_HEX_RE.fullmatch(sig):
        # Hex-only sig (PR #703 round 4 LOW). compare_digest would
        # eventually return False for non-hex; rejecting at parse
        # avoids the unnecessary HMAC compute and surfaces
        # malformed headers earlier.
        return None
    return (key_id, sig)


# ------------------------------------------------------------------ #
# Authorisation checks
# ------------------------------------------------------------------ #


def check_subject_binding(
    *,
    key: KeyInfo,
    signed_user_id: str,
    is_multi_tenant: bool = False,
) -> Optional[str]:
    """Return error message if the key may not sign for ``signed_user_id``.

    Closes finding H10 from the plan: a key issued for userA cannot
    assert ``X-User-ID: userB``.

    Wildcard ``per_user`` keys (``bound_user_id is None``) are an
    H13-shaped cross-tenant impersonation risk in multi-tenant mode:
    a holder of such a key can sign for any ``X-User-ID`` and route
    into any tenant the key's repo allow-list permits. Per-PR-#741
    review, runtime-reject those keys when ``is_multi_tenant=True``,
    independent of the (now-tautological) ``has_global_secret``
    parameter on the startup check. Single-tenant deployments retain
    the wildcard semantics for service-account-style local use.

    Args:
        key: The resolved :class:`KeyInfo` for the request's ``kid``.
        signed_user_id: The ``X-User-ID`` header value the caller is
            asserting.
        is_multi_tenant: True if the server is running in multi-tenant
            hosted mode. Callers running inside ``server_http`` should
            pass ``is_hosted_mode()``.
    """
    if key.key_type == "per_user":
        if key.bound_user_id is None:
            # Wildcard per_user key — any signed user_id accepted.
            # In multi-tenant mode this is a cross-tenant impersonation
            # gap (the key could route into any tenant the repo
            # allow-list permits), so refuse at runtime regardless of
            # how the key was issued (env-var, dashboard, or HTTP
            # resolver). The startup helper catches statically-loaded
            # wildcard keys; this runtime check covers HTTP-resolver
            # responses that the startup helper cannot see.
            if is_multi_tenant:
                return (
                    "HMAC subject binding: wildcard per_user key "
                    "(bound_user_id=None) refused in multi-tenant mode"
                )
            return None
        if key.bound_user_id != signed_user_id:
            return (
                "HMAC subject mismatch: key bound to a different user than "
                "the signed X-User-ID"
            )
        return None
    # service
    if key.delegation_allow_list is None:
        # no_user_delegation: must equal service_identity
        if signed_user_id != key.service_identity:
            return (
                "HMAC subject mismatch: service key does not permit "
                "delegation to the signed X-User-ID"
            )
        return None
    if signed_user_id not in key.delegation_allow_list:
        return (
            "HMAC subject mismatch: signed X-User-ID is not in the "
            "service key's delegation allow-list"
        )
    return None


@dataclass(frozen=True, slots=True)
class RepoAuthError:
    """Result of a failed repo-authorisation check.

    The ``fatal`` flag distinguishes operator-misconfiguration
    failures (always reject regardless of warn/enforce mode) from
    request-mismatch failures (acceptable in warn-mode, rejected in
    enforce mode).

    The plan v5.1 warn-mode is meant as an *observation window* for
    expected mismatches during caller migration — not a license to
    paper over a key whose configuration silently authorises every
    repo. An empty / missing ``repo_allow_list`` on a service key
    is the operator equivalent of "no repos authorised"; honouring
    warn-mode there would invert the security posture.
    """

    message: str
    fatal: bool = False


def check_repo_authorisation(
    *, key: KeyInfo, x_repo: str, per_user_repo_claim: Optional[FrozenSet[str]]
) -> Optional[RepoAuthError]:
    """Return ``None`` if authorised, else a :class:`RepoAuthError`.

    For ``per_user`` keys the authoritative source is the token's
    ``repos`` claim (passed in as ``per_user_repo_claim``). For
    ``service`` keys the server-configured ``repo_allow_list`` is
    authoritative. Operator-misconfiguration cases (no claim at all,
    empty allow-list) return ``fatal=True`` so the auth pipeline
    refuses regardless of ``WATERCOOLER_HMAC_REQUIRE_V3`` mode.
    """
    if not x_repo:
        # Empty X-Repo is rejected by the higher repo-claim layer for
        # bearer; the v3 path mirrors that — no implicit "all repos".
        return RepoAuthError(
            "HMAC repo authorisation: X-Repo header required", fatal=True
        )

    # Late import: `auth/__init__.py` imports this module at startup
    # via `load_default_registry`, so a top-level import would create
    # a cycle. Both branches use the same strict canonicaliser the
    # bearer path uses.
    from . import _canonical_repo_for_claim

    normalised = _canonical_repo_for_claim(x_repo)
    if not normalised:
        return RepoAuthError(
            f"HMAC repo authorisation: X-Repo {x_repo!r} not canonicalisable",
            fatal=True,
        )

    if key.key_type == "per_user":
        # The token-issued repos claim is the source of truth.
        # An empty/None claim means the *issuer* did not authorise
        # any repos for this token — operator misconfiguration on
        # the token-issuing side. Reject unconditionally.
        if per_user_repo_claim is None or len(per_user_repo_claim) == 0:
            return RepoAuthError(
                "HMAC repo authorisation: token has no repos claim", fatal=True
            )
        # PR #703 round 7+5+1 MED: canonicalise the claim entries
        # at the membership test (defense-in-depth). The bearer
        # parse path in ``auth/__init__.py`` already runs entries
        # through ``_normalise_repos_claim`` / ``_canonical_repo_for_claim``,
        # but a future caller populating ``per_user_repo_claim``
        # from a different source (e.g. a custom token resolver, a
        # WebSocket transport) might not pre-canonicalise. Without
        # this defensive normalisation, a token-issuer that stored
        # ``["Org/Repo.git"]`` would silently fail every enforce-
        # mode request because the lower-cased + .git-stripped
        # ``X-Repo`` (``org/repo``) would never match the raw
        # claim entry. The normalisation is idempotent for
        # already-canonical inputs (the existing bearer-parse
        # path), so this is a no-op for current callers and a
        # safety net for future ones.
        normalised_claim = frozenset(
            c
            for c in (
                _canonical_repo_for_claim(r) for r in per_user_repo_claim
            )
            if c
        )
        if normalised not in normalised_claim:
            return RepoAuthError(
                f"HMAC repo authorisation: X-Repo {x_repo!r} not in token claim"
            )
        return None
    # service
    if key.repo_allow_list is None or len(key.repo_allow_list) == 0:
        # Empty allow-list on a service key is operator
        # misconfiguration — the deployer forgot the
        # ``WATERCOOLER_HMAC_KEY_<id>_REPOS`` env var. Honouring
        # warn-mode here would let the key authenticate against any
        # X-Repo, which is the opposite of fail-closed.
        return RepoAuthError(
            "HMAC repo authorisation: service key has no repo allow-list",
            fatal=True,
        )
    if normalised not in key.repo_allow_list:
        return RepoAuthError(
            f"HMAC repo authorisation: X-Repo {x_repo!r} not in service allow-list"
        )
    return None


# ------------------------------------------------------------------ #
# Registry loading
# ------------------------------------------------------------------ #


def _split_csv(value: str) -> FrozenSet[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    return frozenset(items)


def _load_service_keys_from_env(env: dict[str, str]) -> list[KeyInfo]:
    """Build service KeyInfos from env vars.

    Convention:
        WATERCOOLER_HMAC_KEY_<KEY_ID>_SECRET=<arbitrary-string>
        WATERCOOLER_HMAC_KEY_<KEY_ID>_TYPE=service
        WATERCOOLER_HMAC_KEY_<KEY_ID>_SERVICE_IDENTITY=<user_id>
        WATERCOOLER_HMAC_KEY_<KEY_ID>_DELEGATION=<csv user_ids OR "self">
        WATERCOOLER_HMAC_KEY_<KEY_ID>_REPOS=<csv canonical repos>

    The SECRET value is interpreted as the literal UTF-8 bytes of
    the env var string. Operators who want hex- or base64-encoded
    key material must decode it themselves before setting the var
    (or use a wrapper; the load path here does not decode). The
    same string-as-bytes convention applies on the signing side.

    KEY_IDs are extracted by scanning for the SECRET suffix; only
    fully-specified keys are loaded.
    """
    out: list[KeyInfo] = []
    suffix = "_SECRET"
    prefix = "WATERCOOLER_HMAC_KEY_"
    for env_key, env_value in env.items():
        if not env_key.startswith(prefix) or not env_key.endswith(suffix):
            continue
        key_id = env_key[len(prefix) : -len(suffix)]
        if not _is_valid_key_id(key_id):
            logger.warning(
                "HMAC v3: skipping key_id %r — must match %s and not be the "
                "reserved legacy sentinel",
                key_id,
                _KEY_ID_RE.pattern,
            )
            continue
        type_var = env.get(f"{prefix}{key_id}_TYPE")
        if type_var != "service":
            # Only service keys are env-configurable in Sprint 2; per-user
            # keys come from the dashboard (deferred to Sprint 3 PR β).
            #
            # PR #703 round 7+4 MED: warn loudly when a SECRET is set but
            # _TYPE is missing or misspelled (e.g. ``Service`` capital-S,
            # ``per_user``, empty string). The previous silent ``continue``
            # left an operator with no diagnostic for "key configured
            # but not loaded" — the only signal was a registry count
            # lower than expected, which requires knowing the target
            # count in advance. Now the misconfiguration shows up in
            # logs at startup.
            logger.warning(
                "HMAC v3: skipping key %s — WATERCOOLER_HMAC_KEY_%s_TYPE=%r "
                "(expected exactly 'service'; per-user keys are issued by "
                "the dashboard, not env vars). Key NOT registered; every "
                "request signed with this key_id will fail closed.",
                key_id,
                key_id,
                type_var,
            )
            continue
        service_identity = env.get(f"{prefix}{key_id}_SERVICE_IDENTITY", "").strip()
        if not service_identity:
            logger.warning(
                "HMAC v3: skipping service key %s — SERVICE_IDENTITY missing",
                key_id,
            )
            continue
        delegation_raw = env.get(f"{prefix}{key_id}_DELEGATION", "self").strip()
        if delegation_raw == "self":
            delegation: Optional[FrozenSet[str]] = None  # no_user_delegation
        else:
            delegation = _split_csv(delegation_raw)
            if not delegation:
                # PR #703 round 6 MED: an explicit ``DELEGATION=`` (empty
                # string, distinct from the omitted/``self`` default)
                # parses to ``frozenset()`` — non-None, non-empty-checking
                # in ``check_subject_binding`` falls through to the
                # ``signed_user_id not in <empty>`` branch, so EVERY
                # request fails subject-binding silently with a generic
                # 401. Mirror the empty-REPOS warning so operators
                # detect the misconfiguration at load time rather than
                # by reverse-engineering 401s in production.
                logger.warning(
                    "HMAC v3: service key %s registered with empty "
                    "delegation allow-list (WATERCOOLER_HMAC_KEY_%s_DELEGATION "
                    "set to empty string, distinct from omitted/'self') — "
                    "every request will fail closed at subject-binding. "
                    "Set the env var to 'self', a CSV of delegated user_ids, "
                    "or remove it entirely.",
                    key_id,
                    key_id,
                )
        repos_raw = env.get(f"{prefix}{key_id}_REPOS", "").strip()
        # PR #703 round 7+1 MED: normalise allow-list entries through
        # the same canonicaliser the request path uses
        # (``_canonical_repo_for_claim`` → lower-case, ``.git``-strip).
        # Without this, an env var like ``REPOS=Org/Repo`` stores the
        # raw mixed-case form, but ``check_repo_authorisation``
        # lowercases the request's X-Repo before the membership test —
        # so the entry silently never matches and the key fails closed
        # in enforce mode or warns-and-passes (effectively disabled)
        # in warn mode. Late import to avoid a startup cycle
        # (auth/__init__ imports this module).
        if repos_raw:
            from . import _canonical_repo_for_claim

            normalised: set[str] = set()
            for raw_repo in _split_csv(repos_raw):
                canonical = _canonical_repo_for_claim(raw_repo)
                if canonical:
                    normalised.add(canonical)
                else:
                    # PR #703 round 7+2 LOW: per-entry warning when
                    # an individual CSV entry is silently dropped.
                    # Without this, an operator who mistypes one
                    # entry (e.g. ``REPOS=Org/Repo, bad!!entry``)
                    # gets a partial allow-list with no load-time
                    # indication; ``bad!!entry`` requests then
                    # 403 in enforce mode and the only signal is
                    # the runtime rejection.
                    logger.warning(
                        "HMAC v3: service key %s — REPOS entry %r failed "
                        "canonicalisation and was dropped from the allow-list. "
                        "Requests for that repo will be rejected.",
                        key_id,
                        raw_repo,
                    )
            repo_allow = frozenset(normalised)
        else:
            repo_allow = frozenset()
        if not repo_allow:
            # PR #703 round 4 MED: log loud at load time, mirroring
            # the missing-SERVICE_IDENTITY warning. The key WILL
            # register and reject every request at call time
            # (RepoAuthError.fatal=True via empty allow-list),
            # but silence at load makes the misconfiguration
            # invisible to operators.
            logger.warning(
                "HMAC v3: service key %s registered with empty repo allow-list "
                "(WATERCOOLER_HMAC_KEY_%s_REPOS unset or empty) — every "
                "request will fail closed. Set the env var to a CSV of "
                "canonical repos or remove the key entirely.",
                key_id,
                key_id,
            )

        out.append(
            KeyInfo(
                key_id=key_id,
                secret=env_value.encode("utf-8"),
                key_type="service",
                service_identity=service_identity,
                delegation_allow_list=delegation,
                repo_allow_list=repo_allow,
            )
        )
    return out


def load_default_registry(
    env: Optional[dict[str, str]] = None,
) -> KeyRegistry:
    """Load the default HMAC v3 key registry from process environment.

    Sources:

    * Service keys from ``WATERCOOLER_HMAC_KEY_<KEY_ID>_*`` env vars.
    * Per-user keys (dashboard-issued) come from the optional
      :class:`HttpResolver` attached below when both
      ``WATERCOOLER_HMAC_KEY_RESOLVER_URL`` and ``_API_KEY`` are set.

    Issue #733 deleted the legacy global-secret loader: the legacy
    single-secret env var is no longer consulted here, and the
    registry no longer registers a wildcard back-compat key.
    """
    if env is None:
        env = dict(os.environ)
    registry = KeyRegistry()

    v3_reachable_added = 0
    for service_key in _load_service_keys_from_env(env):
        registry.add(service_key)
        v3_reachable_added += 1
    logger.info(
        "HMAC v3: registry loaded with %d v3-reachable key(s)",
        v3_reachable_added,
    )

    # Stage 2 C1 (plan v5.1): if the dashboard HTTP resolver is
    # configured (both ``WATERCOOLER_HMAC_KEY_RESOLVER_URL`` and
    # ``_API_KEY`` env vars set), wire it as a fallback resolver
    # for per-user kids the env registry doesn't know about.
    # Unset → no resolver, env-only behaviour preserved.
    # Lazy import to avoid pulling httpx into the cold-import path
    # for deployments that disable the resolver.
    from .hmac_resolver import load_resolver_from_env

    resolver = load_resolver_from_env(env)
    if resolver is not None:
        registry.add_resolver(resolver)
        logger.info(
            "HMAC v3: HttpResolver attached for per-user keys "
            "(WATERCOOLER_HMAC_KEY_RESOLVER_URL set)"
        )
    return registry


# ------------------------------------------------------------------ #
# Startup fail-fast
# ------------------------------------------------------------------ #


def hmac_v3_startup_fail_fast_check(
    *,
    require_v3: str,
    is_multi_tenant: bool,
    has_global_secret: bool,
    registry: KeyRegistry,
) -> Optional[str]:
    """Return an error string if the server must refuse to boot.

    The plan v5.1 H13 invariant: in multi-tenant deployments with v3
    enforcement on, no key configuration may grant cross-tenant
    impersonation. Two distinct failure modes are checked:

    1. **Legacy global secret configured** (``has_global_secret=True``)
       — pre-Sprint-4 risk; the env-var read was deleted in #733 and
       the production call site now passes ``False`` unconditionally.
       The branch is preserved as a structural guard so a future
       re-introduction of the env-var read would still be caught
       statically; it is currently unreachable in production.

    2. **Wildcard per_user key statically loaded**
       (``registry`` contains ``KeyInfo`` with ``key_type='per_user'``
       and ``bound_user_id is None``) — discovered in PR-#741 review.
       Such a key lets its holder sign for any ``X-User-ID``, which
       in multi-tenant mode reopens H13-shaped cross-tenant
       impersonation. HTTP-resolver-issued wildcard keys are caught
       at runtime by ``check_subject_binding(is_multi_tenant=True)``.

    PR-#741 round 2 review (MED #2) made ``registry`` required (no
    default ``None``) so a future caller cannot omit it and silently
    bypass the wildcard-key scan. Tests that don't care about the
    registry branch should pass an empty ``KeyRegistry()``.

    Args:
        require_v3: Value of ``WATERCOOLER_HMAC_REQUIRE_V3``
            ("warn", "enforce", or unset/empty).
        is_multi_tenant: True if the server is running in
            multi-tenant hosted mode.
        has_global_secret: True if a legacy global HMAC secret is
            configured (production passes ``False`` after #733).
        registry: Loaded :class:`KeyRegistry`. The check refuses to
            boot if ``is_multi_tenant`` is True and any statically-
            loaded ``per_user`` key has ``bound_user_id is None``.
    """
    if require_v3 != "enforce":
        return None
    if not is_multi_tenant:
        return None
    if has_global_secret:
        return (
            "Refusing to start: HMAC v3 enforce mode is on in "
            "multi-tenant deployment but a legacy global HMAC secret "
            "is still configured. The global secret reopens the "
            "cross-subject assertion vulnerability that v3 enforcement "
            "is meant to close. Remove the legacy secret from the "
            "environment, or downgrade HMAC v3 to warn during the "
            "migration window."
        )
    # Snapshot the statically-loaded keys under the registry lock;
    # resolver-issued keys (HTTP dashboard lookup) are caught at
    # request time by ``check_subject_binding(is_multi_tenant=True)``
    # because they aren't in ``_keys`` until first lookup, if ever.
    with registry._lock:
        static_items = list(registry._keys.items())
    offenders: list[str] = []
    for key_id, key_info in static_items:
        if key_info.key_type == "per_user" and key_info.bound_user_id is None:
            offenders.append(key_id)
    if offenders:
        sample = ", ".join(sorted(offenders)[:3])
        more = (
            f" (+ {len(offenders) - 3} more)"
            if len(offenders) > 3
            else ""
        )
        return (
            "Refusing to start: HMAC v3 enforce mode is on in "
            "multi-tenant deployment but the loaded key registry "
            "contains per_user key(s) with bound_user_id=None: "
            f"{sample}{more}. Wildcard per_user keys reopen the "
            "H13 cross-tenant impersonation gap. Either bind each "
            "such key to a specific user, convert it to a service "
            "key with an explicit DELEGATION allow-list, or "
            "remove it."
        )
    return None


__all__ = [
    "GLOBAL_LEGACY_KEY_ID",
    "KeyInfo",
    "KeyRegistry",
    "build_v3_canonical_string",
    "check_repo_authorisation",
    "check_subject_binding",
    "hmac_v3_startup_fail_fast_check",
    "load_default_registry",
    "parse_v3_authorization_header",
    "verify_v3_signature",
]
