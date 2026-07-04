"""Authoritative tenant-scope resolution for hosted MCP requests.

Move 1 of the security consolidation plan.

Invariant
---------
Every code path that resolves a tenant scope returns a value derived
from authenticated request context. Caller-supplied identifiers are
advisory only — logged on mismatch, never authoritative.

The gold-standard resolver previously lived inline at
``tools/semantic.py:_scope_group_id_to_http_ctx``. This module
generalises it beyond T1 group_ids and centralises the discipline so
the same answer is produced everywhere a tool asks "which scope is
this request authorised for?"

Strict mode
-----------
``WATERCOOLER_STRICT_SCOPE=1`` flips warn-and-override behaviour to
hard-fail. Default off in v2.0; default on after test-cjh shakedown.

Federation escape hatch
-----------------------
Read tools that legitimately operate cross-namespace (federation
search, admin diagnostics) call
``resolve_unscoped_or_error(allow_unscoped=True, reason=...)`` and the
call is logged. There is no implicit fallthrough for unscoped
operation.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)


def compute_namespace(scope_id: str) -> str:
    """Canonical namespace derivation from ``scope_id``.

    Public so callers (``__post_init__`` enforcement, tests) can
    produce the same value the resolver does without duplicating the
    hash algorithm. 32 hex chars / 128 bits keeps birthday-collision
    probability negligible at hosted-service tenant counts.
    """
    return hashlib.sha256(scope_id.encode("utf-8")).hexdigest()[:32]


def strict_mode() -> bool:
    """Return True when ``WATERCOOLER_STRICT_SCOPE`` is set to a truthy value.

    Public so callers (tests, diagnostic surfaces) can branch on the
    same predicate the resolver uses.
    """
    return os.getenv("WATERCOOLER_STRICT_SCOPE", "0").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


class ScopeResolutionError(RuntimeError):
    """Raised when tenant scope cannot be derived from auth context.

    In hosted multi-tenant mode this means the request reached the tool
    layer without an X-Repo header (or with an empty user_id). In
    stdio mode it means neither HTTP nor worker context produced a
    complete scope.
    """


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    """Authoritative tenant scope for a single request.

    Constructed only by the resolver functions in this module. The
    fields are derivatives of ``user_id`` and ``repo`` — never accept
    caller-supplied values for these.

    Attributes:
        user_id: Authenticated user identifier (never caller-supplied).
        repo: Canonical "<org>/<repo>" — case-folded, ``.git`` suffix
            stripped.
        scope_id: ``f"{user_id}:{repo}"`` — audit token.
        project_group_id: T1 derivative, e.g.
            ``mostlyharmless_ai_watercooler_cloud``.
        namespace: Filesystem-safe; ``sha256(scope_id).hexdigest()[:32]``
            (32 hex chars / 128 bits, sized for negligible birthday-
            collision probability at hosted-service tenant counts).
        source: Where the auth context came from (HTTP request or
            worker/daemon context).
    """

    user_id: str
    repo: str
    scope_id: str
    project_group_id: str
    namespace: str
    # Move 1 shipped ``http_ctx`` and ``worker_ctx``. Move 3 (PR
    # α + canonical-stdio-namespace) adds ``stdio_local`` for the
    # local-development path where namespace derives from
    # ``code_path`` rather than auth context. Audit consumers
    # pattern-match on this field; the closed set is enforced at
    # runtime in ``__post_init__``.
    source: Literal["http_ctx", "worker_ctx", "stdio_local"]

    def __post_init__(self) -> None:
        # Validate ``source`` first — invalid values should fail before
        # any field-level checks. The Literal annotation is a
        # type-checker hint only; runtime construction with arbitrary
        # strings would otherwise produce a "valid" ResolvedScope with
        # an out-of-contract source field, which audit consumers
        # pattern-matching on the field would then mishandle.
        if self.source not in ("http_ctx", "worker_ctx", "stdio_local"):
            raise ValueError(
                f"ResolvedScope.source {self.source!r} not in "
                f"{{'http_ctx', 'worker_ctx', 'stdio_local'}}"
            )
        if not self.user_id:
            raise ValueError("ResolvedScope.user_id must be non-empty")
        if ":" in self.user_id:
            # ``scope_id = f"{user_id}:{repo}"`` uses ``:`` as the
            # separator. A user_id containing ``:`` would let two
            # distinct (user_id, repo) pairs map to the same scope_id,
            # weakening the uniqueness invariant the format implies.
            # GitHub user logins are alphanumeric+hyphen so this is
            # defence in depth.
            raise ValueError(
                "ResolvedScope.user_id must not contain ':' (separator collision)"
            )
        if not self.repo:
            raise ValueError("ResolvedScope.repo must be non-empty")
        expected = f"{self.user_id}:{self.repo}"
        if self.scope_id != expected:
            raise ValueError(
                f"ResolvedScope.scope_id {self.scope_id!r} inconsistent with "
                f"user_id+repo (expected {expected!r})"
            )
        if not self.project_group_id:
            # Empty project_group_id would silently route reads/writes to a
            # default-or-empty FalkorDB namespace downstream — fail closed
            # at the dataclass boundary so the audit token is always
            # accompanied by a usable graph identifier.
            raise ValueError("ResolvedScope.project_group_id must be non-empty")
        if not self.namespace:
            raise ValueError("ResolvedScope.namespace must be non-empty")
        # The namespace is documented as ``sha256(scope_id)[:32]``.
        # Enforce that invariant at the dataclass boundary so a
        # direct constructor call (test, future caller) cannot
        # silently misroute tenant storage by supplying an
        # arbitrary namespace string. ``compute_namespace`` is the
        # single source of truth for the derivation; anyone who
        # needs to construct a ResolvedScope must use it.
        expected_ns = compute_namespace(self.scope_id)
        if self.namespace != expected_ns:
            raise ValueError(
                f"ResolvedScope.namespace {self.namespace!r} does not "
                f"match the expected hash of scope_id (use "
                f"compute_namespace to derive it)"
            )


_STRIP_SUFFIXES = (".git",)


def _strip_suffixes(s: str) -> str:
    """Strip a trailing ``.git`` repeatedly until a fixed point.

    ``.git`` is git's clone-URL spelling; ``org/repo.git`` and
    ``org/repo`` denote the same repo, so the suffix is trimmed before
    any identity or namespace derivation. The fixed-point loop is bounded
    by the input length, so termination is guaranteed.
    """
    while True:
        original = s
        for suffix in _STRIP_SUFFIXES:
            if s.endswith(suffix):
                s = s.removesuffix(suffix)
        if s == original:
            return s


def canonical_repo(raw: str) -> str:
    """Canonicalise a repo identifier.

    - Lower-cases the input first so matching is case-insensitive (a
      header value like ``Org/Repo.GIT`` must collapse to ``org/repo``
      just like ``org/repo.git`` does).
    - Strips a trailing ``.git`` (git's clone-URL spelling) so
      ``org/repo.git`` and ``org/repo`` resolve to the same identity.

    The ``.git`` strip runs on the name segment when a ``/`` separator
    is present, and on the whole string for bare slugs. Bare-slug inputs
    are subsequently rejected by ``_build_scope``'s org-prefix guard, so
    the security invariant holds at the auth boundary regardless.

    A repo name is otherwise used verbatim — no companion-repo suffix is
    recognised or stripped (e.g. ``org/repo-threads`` stays
    ``org/repo-threads``).

    Examples::

        MostlyHarmless-AI/Watercooler-Cloud.git → mostlyharmless-ai/watercooler-cloud
        Org/Repo.GIT → org/repo
        org/repo-threads → org/repo-threads
        MyRepo.git → myrepo
    """
    s = raw.strip().lower()
    if "/" in s:
        owner, name = s.split("/", 1)
        return f"{owner}/{_strip_suffixes(name)}"
    return _strip_suffixes(s)


# --------------------------------------------------------------------- #
# Canonical-stdio-namespace pipeline (Move 3)
# --------------------------------------------------------------------- #


def strip_url_credentials(url: str) -> str:
    """Remove any ``user[:password]@`` segment from a URL-shaped string.

    A repo-clone URL of the form ``https://user:token@host/path`` would
    otherwise leak the token into any downstream log line that emitted
    the canonical form. We strip it deterministically before hashing
    so the namespace is independent of credential rotation AND raw
    URLs never need to surface in observability.

    Public so other modules (e.g., ``finding_store.py``) can sanitise
    URL-shaped values before logging or sending to telemetry without
    duplicating the algorithm.
    """
    # Plain ``scheme://user:pass@host/path`` form. ``rsplit`` on the
    # LAST ``@`` is load-bearing: a password containing ``@`` (RFC 3986
    # requires ``%40`` encoding but ``git config`` does not normalise,
    # so unencoded values are observed in the wild) would otherwise
    # split on the first ``@`` and leak the remaining credential
    # bytes into the canonical form. ``user:p@ss@github.com/repo``
    # must collapse to ``github.com/repo``, not ``ss@github.com/repo``.
    scheme_sep = "://"
    if scheme_sep in url:
        scheme, rest = url.split(scheme_sep, 1)
        if "@" in rest:
            rest = rest.rsplit("@", 1)[1]
        return f"{scheme}{scheme_sep}{rest}"
    # SSH ``user@host:path`` form. The LAST ``@`` is the auth/host
    # boundary so a password containing ``@`` (or ``:``) does not
    # leak any of its bytes into the canonical form. The path-half
    # check (``:`` AFTER the last @) is what distinguishes scp-syntax
    # from a bare ``user@host`` with no path.
    if "@" in url:
        at_idx = url.rfind("@")
        rest = url[at_idx + 1 :]
        if ":" in rest:
            return rest
    return url


def _normalise_remote_url(url: str) -> str:
    """Collapse equivalent remote-URL forms to one canonical string.

    All of the following must hash to the same namespace:
    - ``https://github.com/org/repo``
    - ``https://github.com/org/repo.git``
    - ``git@github.com:org/repo.git``
    - ``ssh://git@github.com/org/repo.git``
    - ``https://user:token@github.com/org/repo.git``

    The canonical output is ``<host>/<owner>/<repo>`` lower-cased.
    """
    stripped = strip_url_credentials(url).strip()
    # SSH scp-syntax ``host:path`` (after credential strip).
    if "://" not in stripped and ":" in stripped:
        host, _, path = stripped.partition(":")
        normalised = f"{host}/{path}"
    elif "://" in stripped:
        _, after = stripped.split("://", 1)
        normalised = after
    else:
        # Bare path or unrecognised form — return as-is (the hash
        # still produces a stable namespace; uniqueness is what
        # matters here, not pretty-printing).
        normalised = stripped
    # Lower-case and strip trailing ``.git`` and any double-slashes.
    normalised = normalised.lower().rstrip("/")
    if normalised.endswith(".git"):
        normalised = normalised[: -len(".git")]
    while "//" in normalised:
        normalised = normalised.replace("//", "/")
    return normalised


def _git_remote_or_path(code_path: Path) -> str:
    """Return a canonical remote-URL or fallback absolute-path identifier.

    Prefers ``git config --get remote.origin.url`` so a clone of the
    same repo from two different filesystem locations resolves to the
    same namespace. Falls back to the absolute ``code_path`` for
    non-git directories or detached worktrees, which still produces
    distinct namespaces per directory.
    """
    try:
        import subprocess

        result = subprocess.run(
            ["git", "-C", str(code_path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return str(code_path.resolve())


def derive_stdio_namespace(code_path: Path) -> str:
    """Derive the stdio-mode namespace from a code-repo path.

    Used when no auth context is available (local stdio dev mode)
    and findings/state must still be isolated per repo. Two clones
    of the same repo on the same OS user produce the same namespace;
    different repos produce different namespaces — so the previous
    ``_stdio_<getpass.getuser()>`` shape (which collapsed all repos
    for one OS user into a single bucket) is replaced by a
    repo-identity hash.

    Pipeline:
    1. Read git remote URL (or fall back to absolute code_path).
    2. Strip embedded credentials so the result is stable across
       PAT rotation AND the raw value never needs to be logged.
    3. Normalise URL forms — ``git@`` / ``ssh://`` / ``https://``
       /``.git``-suffixed all collapse to one form.
    4. SHA-256 prefix to 32 hex chars (matches auth-derived
       namespace width — 128 bits of collision resistance).

    The raw remote URL is NEVER logged. Only the hash leaves this
    function.
    """
    raw = _git_remote_or_path(code_path)
    canonical = _normalise_remote_url(raw)
    return "_stdio_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------- #


def _build_scope(user_id: str, repo: str, source: str) -> ResolvedScope:
    """Build a ResolvedScope from validated user+repo, deriving the rest."""
    canon = canonical_repo(repo)

    # Validate the canonical form is well-shaped before attempting to
    # derive downstream identifiers. Three shapes must fail closed
    # rather than fall through to ``derive_project_group_id``'s
    # empty-input fallback, which would route writes to the shared
    # default ``"watercooler"`` namespace:
    #
    # 1. ``""`` — input was entirely a strippable suffix (``".git"``).
    # 2. ``"<owner>/"`` or ``"/<name>"`` — one segment stripped empty
    #    (``"org/.git"``).
    # 3. ``"<bare>"`` (no ``/``) — header missing the org prefix
    #    (``"myrepo"``). Hosted X-Repo MUST be ``<org>/<repo>``;
    #    accepting a bare slug would route the request to the
    #    default namespace shared with unscoped stdio callers and
    #    constitute a tenant-isolation bypass.
    if not canon:
        raise ScopeResolutionError(
            f"canonical repo derived from {repo!r} is empty; "
            "header value consists entirely of strippable suffixes"
        )
    if "/" not in canon:
        raise ScopeResolutionError(
            f"canonical repo {canon!r} (from raw {repo!r}) has no org "
            "prefix; hosted X-Repo must be '<org>/<repo>' to identify "
            "the tenant"
        )
    if canon.count("/") != 1:
        # Multi-segment X-Repo (e.g., ``"org/repo/extra"``) survives
        # ``canonical_repo`` because its split-on-first-slash leaves the
        # additional segments inside ``name``. ``derive_project_group_id``
        # would then produce a slash-bearing identifier
        # (``"org_repo/extra"``) that FalkorDB rejects as an invalid
        # database name on the next scoped write — a controlled crash.
        # Reject up-front so the auth boundary fails closed instead.
        raise ScopeResolutionError(
            f"canonical repo {canon!r} (from raw {repo!r}) has more than "
            "one '/' separator; hosted X-Repo must be exactly "
            "'<org>/<repo>'"
        )
    owner_segment, name_segment = canon.split("/", 1)
    if not owner_segment or not name_segment:
        raise ScopeResolutionError(
            f"canonical repo {canon!r} (from raw {repo!r}) has an "
            "empty owner or name segment after suffix stripping"
        )

    scope_id = f"{user_id}:{canon}"

    try:
        from watercooler.path_resolver import derive_project_group_id

        project_group_id = derive_project_group_id(repo_slug=canon)
    except Exception as e:  # noqa: BLE001
        raise ScopeResolutionError(
            f"failed to derive project_group_id for {canon!r}: {e}"
        ) from e

    # ``compute_namespace`` is the single source of truth for the
    # 128-bit namespace derivation; ``ResolvedScope.__post_init__``
    # re-derives and asserts the same value, so storage keys cannot
    # drift even if a future caller builds a ResolvedScope outside
    # this helper.
    namespace = compute_namespace(scope_id)
    try:
        return ResolvedScope(
            user_id=user_id,
            repo=canon,
            scope_id=scope_id,
            project_group_id=project_group_id,
            namespace=namespace,
            source=source,  # type: ignore[arg-type]
        )
    except ValueError as e:
        # ``__post_init__`` may reject an input that survived the
        # earlier guards — most concretely, ``canonical_repo`` returns
        # ``""`` for inputs that consist entirely of a strippable suffix
        # (``".git"``, ``"org/.git"``), which then trips the ``repo`` non-empty
        # check. Same shape if ``derive_project_group_id`` returns
        # an empty string. Without this conversion the ``ValueError``
        # would escape the auth boundary as a 500/crash, because tool
        # handlers only catch ``ScopeResolutionError``. Converting at
        # this single point keeps the auth boundary's exception type
        # closed to ``ScopeResolutionError``.
        raise ScopeResolutionError(
            f"failed to construct ResolvedScope (raw repo={repo!r}, "
            f"canonical={canon!r}): {e}"
        ) from e


def resolve_scope_or_off_hosted() -> Optional[ResolvedScope]:
    """Atomic single-lookup resolver. Three return paths:

    - ``None`` — no HTTP or worker context (off-hosted, e.g. stdio).
      The caller is free to take its dev-mode fallback path.
    - ``ResolvedScope`` — context present and complete; auth-derived
      scope returned with the correct ``source`` tag.
    - ``ScopeResolutionError`` raised — context present but missing
      ``user_id`` or ``repo``; fail-closed.

    Race-tolerance: each context-var is read at most once, and the
    ``source`` is captured from the same read that populated ``ctx``.
    Reading ``get_http_context`` first eliminates the misattribution
    race the previous form had — when ``get_effective_context`` is
    consulted twice and the HTTP context-var is cleared between
    reads, the previous logic would have falsely tagged an HTTP
    request as ``worker_ctx``. The new pattern captures the source
    label inside the same conditional that captured ``ctx``, so the
    label always matches the context object actually used.

    The ``or_off_hosted`` indicator (returning ``None``) lets the
    semantic-tools wrapper take its off-hosted fallback from a single
    lookup, eliminating the asymmetric TOCTOU where an outer
    "is context absent?" check could be observed True while a
    later inner check sees a context that was set in between.
    """
    try:
        from ..context import get_http_context, get_worker_context
    except ImportError as e:
        raise ScopeResolutionError(
            "watercooler_mcp.context unavailable; cannot resolve scope"
        ) from e

    http_ctx = get_http_context()
    if http_ctx is not None:
        ctx = http_ctx
        source: Literal["http_ctx", "worker_ctx"] = "http_ctx"
    else:
        worker_ctx = get_worker_context()
        if worker_ctx is None:
            return None  # off-hosted
        ctx = worker_ctx
        source = "worker_ctx"

    if not ctx.user_id:
        raise ScopeResolutionError(
            "auth context missing user_id; bearer/HMAC headers were not parsed"
        )
    if not ctx.repo:
        raise ScopeResolutionError(
            "auth context missing repo; X-Repo header is required"
        )

    return _build_scope(ctx.user_id, ctx.repo, source=source)


def resolve_scope() -> ResolvedScope:
    """Resolve tenant scope from authenticated request context.

    Reads HTTP context (set by hosted middleware) first, then worker
    context (set by daemon background tasks). The ``source`` field
    of the returned ResolvedScope reflects which path produced the
    context, so audit trails can distinguish HTTP-driven requests
    from daemon-driven background work.

    Caller-supplied identifiers are NEVER consulted.

    Raises:
        ScopeResolutionError: When no complete auth-derived scope is
            available.
    """
    scope = resolve_scope_or_off_hosted()
    if scope is None:
        raise ScopeResolutionError(
            "no HTTP or worker context available; tool reached without auth"
        )
    return scope


def resolve_scope_or_none() -> Optional[ResolvedScope]:
    """Best-effort scope resolution. Returns None on failure, doesn't raise.

    Use for tools that want to operate scope-aware when possible but
    have a legitimate scope-free fallback (e.g., capability discovery,
    health checks). Tools that *write* should use ``resolve_scope()``
    and let the exception propagate.
    """
    try:
        return resolve_scope()
    except ScopeResolutionError as e:
        logger.debug("resolve_scope_or_none: scope unresolved: %s", e)
        return None


def resolve_unscoped_or_error(
    *, allow_unscoped: bool, reason: str
) -> Optional[ResolvedScope]:
    """Explicit escape hatch for tools that may legitimately run unscoped.

    Federation reads, admin diagnostics, and other cross-namespace
    operations call this with ``allow_unscoped=True`` and a human-
    readable ``reason`` for audit. The reason is logged at INFO level.

    When ``allow_unscoped=False``, behaves like ``resolve_scope()``.

    Args:
        allow_unscoped: Pass True only when the caller has an audit-
            traceable reason to operate without a scope.
        reason: Required, non-empty when ``allow_unscoped=True``.
            Logged for forensic review.

    Returns:
        ResolvedScope when one IS available regardless of
        ``allow_unscoped``. None when ``allow_unscoped=True`` and no
        scope is available.

    Raises:
        ScopeResolutionError: ``allow_unscoped=True`` with empty
            ``reason`` (validated up-front so the audit-trail
            requirement holds regardless of whether a scope is
            present), OR ``allow_unscoped=False`` and no scope
            available.
    """
    # Validate the audit-trail requirement BEFORE attempting to
    # resolve. The previous form only checked ``not reason`` inside
    # the except branch, so a caller passing
    # ``allow_unscoped=True, reason=""`` with a live HTTP context
    # silently received a scoped result — the audit invariant the
    # function advertises was bypassed whenever a scope happened to
    # be resolvable.
    if allow_unscoped and not reason:
        raise ScopeResolutionError(
            "resolve_unscoped_or_error called with allow_unscoped=True "
            "but empty reason; the audit trail requires a reason."
        )
    # Log every invocation of the escape hatch, regardless of whether
    # a scope ends up being resolved. The previous form only logged
    # inside the ``except`` branch, so on the most common production
    # path — fully-authenticated hosted requests where the scope
    # resolves cleanly — the function returned the scope with no
    # audit trace. Logging at entry covers all three downstream
    # outcomes (scope returned, off-hosted None returned, fail-closed
    # raise) so the audit log faithfully records every escape-hatch
    # call.
    if allow_unscoped:
        logger.info(
            "scope.resolve_unscoped_or_error: escape hatch invoked: %s",
            reason,
        )
    try:
        return resolve_scope()
    except ScopeResolutionError:
        if not allow_unscoped:
            raise
        return None


def warn_caller_hint_mismatch(
    *,
    scope: ResolvedScope,
    caller_supplied: str,
    field: Literal["group_id", "repo", "scope_id"] = "group_id",
) -> None:
    """Log WARN when a caller's advisory hint disagrees with auth-derived scope.

    The hint is *advisory*. This helper makes the divergence visible
    without changing behaviour — callers always use the auth-derived
    value, never the hint.

    In strict mode, mismatches escalate to ScopeResolutionError. This
    is the v2.1 default after test-cjh shakedown.
    """
    if not caller_supplied:
        return
    derived = {
        "group_id": scope.project_group_id,
        "repo": scope.repo,
        "scope_id": scope.scope_id,
    }[field]
    if caller_supplied == derived:
        return

    msg = (
        f"caller-supplied {field}={caller_supplied!r} does not match "
        f"auth-derived {field}={derived!r}; hosted scope wins (tenant isolation)."
    )
    if strict_mode():
        raise ScopeResolutionError(f"strict_mode: {msg}")
    logger.warning("scope.caller_hint_mismatch: %s", msg)


__all__ = [
    "ResolvedScope",
    "ScopeResolutionError",
    "compute_namespace",
    "derive_stdio_namespace",
    "resolve_scope",
    "resolve_scope_or_none",
    "resolve_scope_or_off_hosted",
    "resolve_unscoped_or_error",
    "strip_url_credentials",
    "warn_caller_hint_mismatch",
    "canonical_repo",
    "strict_mode",
]
