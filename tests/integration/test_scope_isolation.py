"""TC-A1 — Cross-scope authority confusion test class for Move 1.

Covers the Move 1 invariant: every code path that resolves a tenant
scope returns an auth-derived value. Caller-supplied identifiers are
advisory only and never authoritative.

Test classes:

- ``TestResolvedScope``: dataclass invariants (frozen, slots, post_init).
- ``TestResolveScope``: HTTP/worker context resolution.
- ``TestSemanticToolWrapper``: backwards-compatible wrapper around the
  donor at ``tools/semantic.py:_scope_group_id_to_http_ctx``.
- ``TestEscapeHatch``: ``resolve_unscoped_or_error`` semantics.
- ``TestStrictMode``: WATERCOOLER_STRICT_SCOPE flag escalates mismatches.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator

import pytest

from watercooler_mcp.auth.scope import (
    ResolvedScope,
    ScopeResolutionError,
    canonical_repo,
    compute_namespace,
    resolve_scope,
    resolve_scope_or_none,
    resolve_unscoped_or_error,
    strict_mode,
    strip_url_credentials,
    warn_caller_hint_mismatch,
)
from watercooler_mcp.context import (
    HttpRequestContext,
    clear_http_context,
    set_http_context,
)


@pytest.fixture(autouse=True)
def _reset_http_ctx() -> Iterator[None]:
    """Each test starts with a clean HTTP context."""
    clear_http_context()
    yield
    clear_http_context()


@pytest.fixture(autouse=True)
def _restore_log_propagation() -> Iterator[None]:
    """Re-enable propagation on the watercooler_mcp logger.

    ``observability`` sets ``propagate=False`` on the package-level
    logger so file-handler output does not double-print. Pytest's
    ``caplog`` captures at the root logger, so suppressed
    propagation makes it look like our log calls produced nothing.
    Restore propagation for the duration of each test so caplog can
    see scope-related WARNING/INFO records.
    """
    ns_logger = logging.getLogger("watercooler_mcp")
    saved = ns_logger.propagate
    ns_logger.propagate = True
    try:
        yield
    finally:
        ns_logger.propagate = saved


@pytest.fixture
def _disable_strict() -> Iterator[None]:
    """Force strict mode OFF regardless of operator env."""
    prev = os.environ.pop("WATERCOOLER_STRICT_SCOPE", None)
    try:
        yield
    finally:
        if prev is not None:
            os.environ["WATERCOOLER_STRICT_SCOPE"] = prev


@pytest.fixture
def _enable_strict() -> Iterator[None]:
    """Force strict mode ON for the test."""
    prev = os.environ.get("WATERCOOLER_STRICT_SCOPE")
    os.environ["WATERCOOLER_STRICT_SCOPE"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("WATERCOOLER_STRICT_SCOPE", None)
        else:
            os.environ["WATERCOOLER_STRICT_SCOPE"] = prev


class TestResolvedScope:
    """Dataclass invariants for ResolvedScope."""

    def test_valid_scope_constructs(self) -> None:
        scope = ResolvedScope(
            user_id="alice",
            repo="org/repo",
            scope_id="alice:org/repo",
            project_group_id="org_repo",
            namespace=compute_namespace("alice:org/repo"),
            source="http_ctx",
        )
        assert scope.user_id == "alice"
        assert scope.repo == "org/repo"

    def test_inconsistent_scope_id_raises(self) -> None:
        with pytest.raises(ValueError, match="scope_id .* inconsistent"):
            ResolvedScope(
                user_id="alice",
                repo="org/repo",
                scope_id="bob:other/repo",  # mismatch
                project_group_id="org_repo",
                namespace="abc",
                source="http_ctx",
            )

    def test_empty_user_id_raises(self) -> None:
        with pytest.raises(ValueError, match="user_id must be non-empty"):
            ResolvedScope(
                user_id="",
                repo="org/repo",
                scope_id=":org/repo",
                project_group_id="org_repo",
                namespace="abc",
                source="http_ctx",
            )

    def test_empty_repo_raises(self) -> None:
        with pytest.raises(ValueError, match="repo must be non-empty"):
            ResolvedScope(
                user_id="alice",
                repo="",
                scope_id="alice:",
                project_group_id="",
                namespace="abc",
                source="http_ctx",
            )

    def test_empty_namespace_raises(self) -> None:
        with pytest.raises(ValueError, match="namespace must be non-empty"):
            ResolvedScope(
                user_id="alice",
                repo="org/repo",
                scope_id="alice:org/repo",
                project_group_id="org_repo",
                namespace="",
                source="http_ctx",
            )

    def test_empty_project_group_id_raises(self) -> None:
        # Empty project_group_id would silently route reads/writes to
        # an empty-or-default FalkorDB namespace downstream — fail
        # closed at the dataclass boundary.
        with pytest.raises(ValueError, match="project_group_id must be non-empty"):
            ResolvedScope(
                user_id="alice",
                repo="org/repo",
                scope_id="alice:org/repo",
                project_group_id="",
                namespace="abc",
                source="http_ctx",
            )

    def test_user_id_with_colon_raises(self) -> None:
        # The scope_id format ``f"{user_id}:{repo}"`` uses ``:`` as the
        # separator. A user_id containing ``:`` would let two distinct
        # (user_id, repo) pairs map to the same scope_id, weakening
        # uniqueness. GitHub user logins don't allow ``:`` so this is
        # defence in depth, but the validation should match the format.
        with pytest.raises(ValueError, match="must not contain ':'"):
            ResolvedScope(
                user_id="alice:bob",
                repo="x",
                scope_id="alice:bob:x",
                project_group_id="x",
                namespace="abc",
                source="http_ctx",
            )

    def test_invalid_source_raises(self) -> None:
        # The ``source`` Literal is a type-checker hint only; runtime
        # construction with an arbitrary string would otherwise pass
        # and produce an out-of-contract value. __post_init__ now
        # validates against the closed set.
        with pytest.raises(ValueError, match="source"):
            ResolvedScope(
                user_id="alice",
                repo="org/repo",
                scope_id="alice:org/repo",
                project_group_id="org_repo",
                namespace="abc",
                source="bogus",  # type: ignore[arg-type]
            )

    def test_stdio_local_source_accepted_after_move_3(self) -> None:
        # Move 3 enables ``stdio_local`` as a valid source value for
        # the canonical-stdio-namespace path (no auth context, namespace
        # derived from ``code_path``). Construction with it must
        # succeed; the closed Literal set is now
        # ``{"http_ctx", "worker_ctx", "stdio_local"}``.
        scope = ResolvedScope(
            user_id="alice",
            repo="org/repo",
            scope_id="alice:org/repo",
            project_group_id="org_repo",
            namespace=compute_namespace("alice:org/repo"),
            source="stdio_local",
        )
        assert scope.source == "stdio_local"

    def test_frozen_immutable(self) -> None:
        scope = ResolvedScope(
            user_id="alice",
            repo="org/repo",
            scope_id="alice:org/repo",
            project_group_id="org_repo",
            namespace=compute_namespace("alice:org/repo"),
            source="http_ctx",
        )
        with pytest.raises((AttributeError, TypeError)):
            scope.user_id = "bob"  # type: ignore[misc]

    def test_slots_no_extra_attrs(self) -> None:
        scope = ResolvedScope(
            user_id="alice",
            repo="org/repo",
            scope_id="alice:org/repo",
            project_group_id="org_repo",
            namespace=compute_namespace("alice:org/repo"),
            source="http_ctx",
        )
        with pytest.raises(AttributeError):
            scope.__dict__  # noqa: B018 — slots disable __dict__

    def test_namespace_must_match_scope_id_hash(self) -> None:
        # The dataclass enforces ``namespace == compute_namespace(scope_id)``
        # so a direct constructor call cannot supply an arbitrary namespace
        # that silently misroutes tenant storage. The earlier guards
        # (empty namespace, scope_id consistency) covered different
        # failure modes; this one closes the "valid-looking but wrong
        # value" gap.
        with pytest.raises(ValueError, match="namespace .* does not match"):
            ResolvedScope(
                user_id="alice",
                repo="org/repo",
                scope_id="alice:org/repo",
                project_group_id="org_repo",
                # 32 hex chars (passes the length check) but NOT the
                # actual hash of "alice:org/repo".
                namespace="0" * 32,
                source="http_ctx",
            )


class TestCanonicalRepo:
    """Repo canonicalisation rules."""

    def test_lower_case_owner_and_repo(self) -> None:
        assert canonical_repo("MostlyHarmless-AI/Watercooler-Cloud") == (
            "mostlyharmless-ai/watercooler"
        )

    def test_strip_dot_git_suffix(self) -> None:
        assert canonical_repo("org/repo.git") == "org/repo"

    def test_strip_threads_suffix(self) -> None:
        assert canonical_repo("org/repo-threads") == "org/repo"

    def test_combined_strip_and_lowercase(self) -> None:
        assert canonical_repo("Org/Repo-threads.git") == "org/repo"

    def test_no_slash_just_lowercases(self) -> None:
        assert canonical_repo("RepoOnly") == "repoonly"

    def test_bare_slug_strips_dot_git(self) -> None:
        # Malformed header without a "/" — strips still apply.
        assert canonical_repo("MyRepo.git") == "myrepo"

    def test_bare_slug_strips_threads_suffix(self) -> None:
        assert canonical_repo("MyRepo-threads") == "myrepo"

    def test_bare_slug_strips_combined_suffixes(self) -> None:
        assert canonical_repo("MyRepo-threads.git") == "myrepo"

    def test_uppercase_dot_git_suffix_stripped(self) -> None:
        # Suffix matching is case-insensitive — a header value like
        # `org/repo.GIT` must collapse the same way `.git` does.
        assert canonical_repo("Org/Repo.GIT") == "org/repo"

    def test_uppercase_threads_suffix_stripped(self) -> None:
        assert canonical_repo("Org/Repo-Threads") == "org/repo"

    def test_uppercase_combined_suffixes(self) -> None:
        # NOTE: The current implementation is case-insensitive on the
        # exact suffix tokens ``.git`` and ``-threads``; mixed-case
        # variants like "-THREADS.GIT" are normalised by
        # lower-casing the input before stripping.
        assert canonical_repo("Org/Repo-THREADS.GIT") == "org/repo"

    def test_suffix_order_threads_then_git(self) -> None:
        # ``-threads.git`` order: strip ``.git`` first, then ``-threads``.
        assert canonical_repo("org/repo-threads.git") == "org/repo"

    def test_suffix_order_git_then_threads(self) -> None:
        # Regression for the eighth-pass review: ``.git-threads`` order
        # (``.git`` is the inner suffix) requires iterative stripping.
        # A single-pass form would strip ``-threads`` and leave
        # ``.git`` dangling.
        assert canonical_repo("org/repo.git-threads") == "org/repo"

    def test_suffix_order_bare_slug_git_then_threads(self) -> None:
        # Same iterative-stripping requirement, bare-slug form.
        # Resulting canonical form is bare and will be rejected by
        # ``_build_scope``'s no-org-prefix guard, but ``canonical_repo``
        # itself must still strip both suffixes.
        assert canonical_repo("myrepo.git-threads") == "myrepo"


class TestStdioNamespace:
    """Move 3: canonical-stdio-namespace pipeline.

    ``derive_stdio_namespace(code_path)`` is the no-auth-context
    counterpart to the auth-derived namespace from M1. Two clones
    of the same repo at different filesystem locations must produce
    the same namespace; different repos must produce different
    namespaces; embedded credentials must NOT influence the result.
    """

    def test_url_credentials_stripped(self) -> None:
        from watercooler_mcp.auth.scope import strip_url_credentials

        assert (
            strip_url_credentials("https://user:token@github.com/org/repo")
            == "https://github.com/org/repo"
        )
        assert (
            strip_url_credentials("https://github.com/org/repo")
            == "https://github.com/org/repo"
        )
        # SCP-syntax SSH form
        assert (
            strip_url_credentials("git@github.com:org/repo.git")
            == "github.com:org/repo.git"
        )

    def test_remote_url_normalisation_collapses_equivalent_forms(self) -> None:
        from watercooler_mcp.auth.scope import _normalise_remote_url

        forms = [
            "https://github.com/org/repo",
            "https://github.com/org/repo.git",
            "https://github.com/org/repo/",
            "https://USER:TOKEN@github.com/Org/Repo.git",
            "git@github.com:org/repo.git",
            "ssh://git@github.com/org/repo.git",
        ]
        canonical_set = {_normalise_remote_url(f) for f in forms}
        assert len(canonical_set) == 1, (
            f"expected all forms to collapse to one canonical, " f"got {canonical_set}"
        )
        # Spot-check the canonical form looks reasonable.
        canonical = canonical_set.pop()
        assert "user" not in canonical.lower()
        assert "token" not in canonical.lower()
        assert ".git" not in canonical
        assert "github.com/org/repo" in canonical

    def test_derive_stdio_namespace_returns_stable_hash(self, tmp_path: Path) -> None:
        from watercooler_mcp.auth.scope import derive_stdio_namespace

        # Non-git directory falls back to absolute path; same path
        # twice yields the same namespace.
        ns1 = derive_stdio_namespace(tmp_path)
        ns2 = derive_stdio_namespace(tmp_path)
        assert ns1 == ns2
        assert ns1.startswith("_stdio_")
        # 32 hex chars after the ``_stdio_`` prefix matches the
        # auth-derived namespace width.
        assert len(ns1) == len("_stdio_") + 32

    def test_derive_stdio_namespace_different_paths_distinct(
        self, tmp_path: Path
    ) -> None:
        from watercooler_mcp.auth.scope import derive_stdio_namespace

        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        ns_a = derive_stdio_namespace(a)
        ns_b = derive_stdio_namespace(b)
        assert ns_a != ns_b

    def test_credential_bearing_remote_does_not_leak(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Regression for the plan v5.1 invariant: raw remote URLs may
        # carry credentials and MUST NOT influence the canonical form
        # OR surface in logs. The earlier form of this test passed
        # ``tmp_path`` (no git remote → fallback path), which never
        # exercised the credential-stripping logic. This version
        # monkeypatches ``_git_remote_or_path`` to return a
        # credential-bearing URL and asserts:
        #
        # 1. The canonical form (the string fed to SHA-256) contains
        #    none of the credential bytes.
        # 2. No log output emitted from the function or its callees
        #    contains the credentials, the username, or the host.
        # 3. The resulting namespace is identical to the namespace
        #    that the same URL without credentials would produce —
        #    proving stability across PAT rotation.
        from watercooler_mcp.auth import scope as scope_mod

        url_with_creds = "https://alice:secret-token@github.com/org/repo.git"
        url_clean = "https://github.com/org/repo.git"

        monkeypatch.setattr(scope_mod, "_git_remote_or_path", lambda p: url_with_creds)
        canonical_creds = scope_mod._normalise_remote_url(url_with_creds)
        assert "alice" not in canonical_creds
        assert "secret-token" not in canonical_creds
        assert "@" not in canonical_creds

        # Structural no-log invariant: ``derive_stdio_namespace`` and
        # its callees MUST emit zero log records on the credential-
        # bearing path. Asserting on substring absence in
        # ``caplog.text`` would have been vacuous (the function
        # currently logs nothing); ``records == []`` fails loud if a
        # future change adds ANY log call that could carry credential
        # bytes, regardless of message content.
        caplog.set_level(logging.DEBUG, logger="watercooler_mcp.auth.scope")
        caplog.clear()
        ns_creds = scope_mod.derive_stdio_namespace(tmp_path)
        assert caplog.records == [], (
            f"derive_stdio_namespace must not log on the credential-"
            f"bearing path; got {len(caplog.records)} record(s): "
            f"{[r.getMessage() for r in caplog.records]}"
        )

        # Stability across credential rotation:
        monkeypatch.setattr(scope_mod, "_git_remote_or_path", lambda p: url_clean)
        ns_clean = scope_mod.derive_stdio_namespace(tmp_path)
        assert ns_creds == ns_clean

    def test_password_with_at_sign_fully_stripped(self) -> None:
        # ``user:p@ss@github.com/repo`` — the ``@`` inside the password
        # must NOT survive the credential strip. ``rsplit("@", 1)``
        # on the last ``@`` is the load-bearing detail; a single-pass
        # ``split("@", 1)`` would have left ``ss@github.com/repo``
        # behind.
        assert (
            strip_url_credentials("https://user:p@ss@github.com/repo")
            == "https://github.com/repo"
        )

    def test_scp_syntax_password_with_at_sign_fully_stripped(self) -> None:
        # SCP-syntax password-bearing form (rare but possible).
        assert (
            strip_url_credentials("user:p@ss@github.com:org/repo.git")
            == "github.com:org/repo.git"
        )


class TestResolveScope:
    """Scope resolution from HTTP/worker context."""

    def test_complete_http_ctx_returns_scope(self, _disable_strict: None) -> None:
        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="org/repo",
                branch="main",
                github_token="ghp_test",
            )
        )
        scope = resolve_scope()
        assert scope.user_id == "alice"
        assert scope.repo == "org/repo"
        assert scope.scope_id == "alice:org/repo"
        assert scope.namespace
        # 32 hex chars = 128 bits. Birthday-collision probability is
        # vanishing at any plausible tenant count; the earlier 16-char
        # form was insufficient for hosted-service scale.
        assert len(scope.namespace) == 32
        assert scope.source == "http_ctx"

    def test_canonical_repo_applied(self, _disable_strict: None) -> None:
        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="MostlyHarmless-AI/Watercooler-Cloud-threads",
                branch="main",
                github_token="ghp_test",
            )
        )
        scope = resolve_scope()
        assert scope.repo == "mostlyharmless-ai/watercooler"

    def test_no_context_raises(self, _disable_strict: None) -> None:
        with pytest.raises(ScopeResolutionError, match="no HTTP or worker context"):
            resolve_scope()

    def test_missing_user_id_raises(self, _disable_strict: None) -> None:
        set_http_context(
            HttpRequestContext(
                user_id="",
                repo="org/repo",
                branch="main",
                github_token="ghp_test",
            )
        )
        with pytest.raises(ScopeResolutionError, match="missing user_id"):
            resolve_scope()

    def test_missing_repo_raises(self, _disable_strict: None) -> None:
        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo=None,
                branch="main",
                github_token="ghp_test",
            )
        )
        with pytest.raises(ScopeResolutionError, match="missing repo"):
            resolve_scope()

    def test_resolve_or_none_returns_none_on_failure(
        self, _disable_strict: None
    ) -> None:
        # No context — would raise; but _or_none catches and returns None.
        assert resolve_scope_or_none() is None

    def test_resolve_or_none_returns_scope_on_success(
        self, _disable_strict: None
    ) -> None:
        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="org/repo",
                branch="main",
                github_token="ghp_test",
            )
        )
        scope = resolve_scope_or_none()
        assert scope is not None
        assert scope.user_id == "alice"

    def test_worker_context_source_is_worker_ctx(self, _disable_strict: None) -> None:
        # Daemon/background tasks set the worker context via
        # set_worker_context. Audit must distinguish that path from a
        # real HTTP request — the prior implementation always tagged
        # worker-driven scopes as ``http_ctx``.
        from watercooler_mcp.context import (
            clear_worker_context,
            set_worker_context,
        )

        try:
            set_worker_context(
                HttpRequestContext(
                    user_id="bob",
                    repo="org/repo",
                    branch="main",
                    github_token="ghp_test",
                )
            )
            scope = resolve_scope()
        finally:
            clear_worker_context()
        assert scope.source == "worker_ctx"
        assert scope.user_id == "bob"

    def test_http_context_takes_precedence_over_worker(
        self, _disable_strict: None
    ) -> None:
        from watercooler_mcp.context import (
            clear_worker_context,
            set_worker_context,
        )

        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="org/http_repo",
                branch="main",
                github_token="ghp_test",
            )
        )
        try:
            set_worker_context(
                HttpRequestContext(
                    user_id="bob",
                    repo="org/worker_repo",
                    branch="main",
                    github_token="ghp_test",
                )
            )
            scope = resolve_scope()
        finally:
            clear_worker_context()
        assert scope.source == "http_ctx"
        assert scope.user_id == "alice"

    def test_source_attribution_atomic_no_misattribution_under_clear_race(
        self, _disable_strict: None
    ) -> None:
        # Regression for the source-misattribution race: previously
        # ``resolve_scope`` did two contextvar reads — one to capture
        # ``ctx`` and a separate one to compute ``source`` — so a
        # ``clear_http_context`` between them flipped the label to
        # ``worker_ctx`` while the captured ctx was still the HTTP
        # one. The new pattern reads ``get_http_context`` once and
        # captures ``source`` from the same conditional, so the
        # label always matches the context object actually used.
        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="org/repo",
                branch="main",
                github_token="ghp_test",
            )
        )
        scope = resolve_scope()
        # Whatever the inner timing, source must match the source
        # of the captured context — never falsely "worker_ctx".
        assert scope.source == "http_ctx"


class TestResolveScopeOrOffHosted:
    """Atomic single-lookup helper. ``None`` means off-hosted."""

    def test_no_context_returns_none(self, _disable_strict: None) -> None:
        from watercooler_mcp.auth.scope import resolve_scope_or_off_hosted

        assert resolve_scope_or_off_hosted() is None

    def test_http_context_returns_scope(self, _disable_strict: None) -> None:
        from watercooler_mcp.auth.scope import resolve_scope_or_off_hosted

        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="org/repo",
                branch="main",
                github_token="ghp_test",
            )
        )
        scope = resolve_scope_or_off_hosted()
        assert scope is not None
        assert scope.user_id == "alice"
        assert scope.source == "http_ctx"

    def test_worker_context_returns_scope(self, _disable_strict: None) -> None:
        from watercooler_mcp.auth.scope import resolve_scope_or_off_hosted
        from watercooler_mcp.context import (
            clear_worker_context,
            set_worker_context,
        )

        try:
            set_worker_context(
                HttpRequestContext(
                    user_id="bob",
                    repo="org/repo",
                    branch="main",
                    github_token="ghp_test",
                )
            )
            scope = resolve_scope_or_off_hosted()
        finally:
            clear_worker_context()
        assert scope is not None
        assert scope.source == "worker_ctx"

    def test_incomplete_context_raises_not_returns_none(
        self, _disable_strict: None
    ) -> None:
        # Incomplete context (missing repo) must raise rather than
        # return None — None is reserved for the off-hosted case.
        from watercooler_mcp.auth.scope import resolve_scope_or_off_hosted

        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo=None,
                branch="main",
                github_token="ghp_test",
            )
        )
        with pytest.raises(ScopeResolutionError, match="missing repo"):
            resolve_scope_or_off_hosted()


class TestSemanticToolWrapper:
    """Backwards-compatible wrapper at tools/semantic.py:_scope_group_id_to_http_ctx."""

    def test_off_hosted_returns_caller_value(self, _disable_strict: None) -> None:
        from watercooler_mcp.tools.semantic import _scope_group_id_to_http_ctx

        # No HTTP context → off-hosted; caller value passes through.
        scoped, err = _scope_group_id_to_http_ctx("caller_group")
        assert err is None
        assert scoped == "caller_group"

    def test_hosted_no_repo_returns_error(self, _disable_strict: None) -> None:
        from watercooler_mcp.tools.semantic import _scope_group_id_to_http_ctx

        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo=None,  # hosted but missing X-Repo
                branch="main",
                github_token="ghp_test",
            )
        )
        scoped, err = _scope_group_id_to_http_ctx("any")
        assert scoped == ""
        assert err is not None
        assert "scope_resolution_failed" in err["error"]

    @pytest.mark.parametrize(
        "x_repo",
        [
            "-threads",  # bare slug that strips to ""
            ".git",  # bare slug that strips to ""
            "org/-threads",  # name-only stripped → "org/"
            "org/.git",  # name-only stripped → "org/"
            "-Threads.GIT",  # combined uppercase strip → ""
            "myrepo",  # bare slug, no org prefix at all
            "MyRepo.GIT",  # bare slug after canonicalisation
            "org/repo/extra",  # multi-segment, would crash downstream
            "org/repo/extra/more",  # deeper multi-segment
            "Org/Repo/Extra.GIT",  # multi-segment after canonicalisation
        ],
    )
    def test_hosted_empty_canonical_repo_returns_error_not_crash(
        self, _disable_strict: None, x_repo: str
    ) -> None:
        # Regression: ``canonical_repo`` returns ``""`` for inputs that
        # consist entirely of strippable suffixes. The previous form
        # called ``ResolvedScope(...)`` which raised ``ValueError``
        # from ``__post_init__`` — and the wrapper only caught
        # ``ScopeResolutionError``, so the ``ValueError`` propagated
        # as a 500/crash. ``_build_scope`` now converts the
        # ``ValueError`` to ``ScopeResolutionError`` so the wrapper
        # returns a clean error tuple to any X-Repo header that
        # collapses to empty after canonicalisation.
        from watercooler_mcp.tools.semantic import _scope_group_id_to_http_ctx

        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo=x_repo,
                branch="main",
                github_token="ghp_test",
            )
        )
        scoped, err = _scope_group_id_to_http_ctx("any")
        assert scoped == ""
        assert err is not None
        assert "scope_resolution_failed" in err["error"]

    def test_hosted_caller_mismatch_logs_warning_and_uses_hosted(
        self,
        _disable_strict: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from watercooler_mcp.tools.semantic import _scope_group_id_to_http_ctx

        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="userA/repoA",
                branch="main",
                github_token="ghp_test",
            )
        )
        caplog.set_level(logging.WARNING, logger="watercooler_mcp.auth.scope")
        scoped, err = _scope_group_id_to_http_ctx(
            "userB_repoB"  # caller-supplied, MISMATCH
        )
        assert err is None
        # Auth-derived value wins; the caller's value is discarded.
        assert scoped != "userB_repoB"
        assert "scope.caller_hint_mismatch" in caplog.text

    def test_hosted_caller_mismatch_in_strict_mode_returns_error_tuple(
        self,
        _enable_strict: None,
    ) -> None:
        # Regression: ``warn_caller_hint_mismatch`` raises
        # ``ScopeResolutionError`` under strict mode. The wrapper
        # must catch it and return the established error tuple
        # rather than propagate, so the MCP client sees a
        # consistent ``scope_resolution_failed`` shape in both
        # warn-mode and strict-mode.
        from watercooler_mcp.tools.semantic import _scope_group_id_to_http_ctx

        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="userA/repoA",
                branch="main",
                github_token="ghp_test",
            )
        )
        scoped, err = _scope_group_id_to_http_ctx(
            "userB_repoB"  # caller-supplied, MISMATCH — escalates to raise
        )
        assert scoped == ""
        assert err is not None
        assert "scope_resolution_failed" in err["error"]
        # Strict-mode message includes "strict_mode" tag for forensics.
        assert "strict_mode" in err["error"]

    def test_hosted_caller_match_no_warning(
        self,
        _disable_strict: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from watercooler_mcp.tools.semantic import _scope_group_id_to_http_ctx

        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="userA/repoA",
                branch="main",
                github_token="ghp_test",
            )
        )
        # First call to learn what the auth-derived group_id looks like.
        first_scoped, _ = _scope_group_id_to_http_ctx("")
        caplog.set_level(logging.WARNING, logger="watercooler_mcp.auth.scope")
        # Now feed the same value back as the caller hint.
        scoped, err = _scope_group_id_to_http_ctx(first_scoped)
        assert err is None
        assert scoped == first_scoped
        assert "scope.caller_hint_mismatch" not in caplog.text


class TestEscapeHatch:
    """resolve_unscoped_or_error explicit-allow-list semantics."""

    def test_allow_unscoped_returns_none_on_failure(
        self, _disable_strict: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="watercooler_mcp.auth.scope")
        result = resolve_unscoped_or_error(
            allow_unscoped=True, reason="federation cross-namespace search"
        )
        assert result is None
        assert "resolve_unscoped_or_error" in caplog.text
        assert "federation cross-namespace search" in caplog.text

    def test_disallow_unscoped_raises(self, _disable_strict: None) -> None:
        with pytest.raises(ScopeResolutionError):
            resolve_unscoped_or_error(allow_unscoped=False, reason="")

    def test_allow_unscoped_with_empty_reason_raises(
        self, _disable_strict: None
    ) -> None:
        # No-context path — audit-trail requirement enforced.
        with pytest.raises(ScopeResolutionError, match="audit trail requires"):
            resolve_unscoped_or_error(allow_unscoped=True, reason="")

    def test_allow_unscoped_with_empty_reason_raises_even_with_live_context(
        self, _disable_strict: None
    ) -> None:
        # Regression: the empty-reason guard previously lived inside
        # the ``except ScopeResolutionError`` block, so when
        # ``resolve_scope()`` succeeded the function returned the
        # scope without ever validating ``reason``. The audit-trail
        # requirement now fires up-front, regardless of whether a
        # scope is resolvable.
        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="org/repo",
                branch="main",
                github_token="ghp_test",
            )
        )
        with pytest.raises(ScopeResolutionError, match="audit trail requires"):
            resolve_unscoped_or_error(allow_unscoped=True, reason="")

    def test_allow_unscoped_returns_scope_when_available(
        self, _disable_strict: None
    ) -> None:
        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="org/repo",
                branch="main",
                github_token="ghp_test",
            )
        )
        result = resolve_unscoped_or_error(allow_unscoped=True, reason="diagnostic")
        assert result is not None
        assert result.user_id == "alice"

    def test_allow_unscoped_logs_audit_entry_when_scope_resolves(
        self, _disable_strict: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Regression: previously ``logger.info`` only fired inside the
        # ``except`` branch, so on the most common production path —
        # fully-authenticated hosted requests where the scope resolves
        # cleanly — the escape hatch was invoked with NO audit trace.
        # The fix logs at entry whenever ``allow_unscoped=True`` so
        # every invocation appears in the audit log regardless of
        # whether the scope ends up resolving.
        set_http_context(
            HttpRequestContext(
                user_id="alice",
                repo="org/repo",
                branch="main",
                github_token="ghp_test",
            )
        )
        caplog.set_level(logging.INFO, logger="watercooler_mcp.auth.scope")
        result = resolve_unscoped_or_error(
            allow_unscoped=True, reason="diagnostic-with-live-scope"
        )
        assert result is not None  # scope resolved
        # Audit log MUST contain an entry for this invocation.
        assert "resolve_unscoped_or_error" in caplog.text
        assert "diagnostic-with-live-scope" in caplog.text


class TestStrictMode:
    """Strict mode escalates caller-hint mismatches."""

    def test_strict_mode_predicate(self, _enable_strict: None) -> None:
        assert strict_mode() is True

    def test_strict_mode_off_by_default(self, _disable_strict: None) -> None:
        assert strict_mode() is False

    def test_warn_mismatch_no_op_when_match(self, _disable_strict: None) -> None:
        scope = ResolvedScope(
            user_id="alice",
            repo="org/repo",
            scope_id="alice:org/repo",
            project_group_id="org_repo",
            namespace=compute_namespace("alice:org/repo"),
            source="http_ctx",
        )
        # No exception, no log — values match.
        warn_caller_hint_mismatch(scope=scope, caller_supplied="org_repo")

    def test_strict_mode_escalates_mismatch(self, _enable_strict: None) -> None:
        scope = ResolvedScope(
            user_id="alice",
            repo="org/repo",
            scope_id="alice:org/repo",
            project_group_id="org_repo",
            namespace=compute_namespace("alice:org/repo"),
            source="http_ctx",
        )
        with pytest.raises(ScopeResolutionError, match="strict_mode"):
            warn_caller_hint_mismatch(scope=scope, caller_supplied="other_org_repo")

    def test_non_strict_logs_warning(
        self, _disable_strict: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        scope = ResolvedScope(
            user_id="alice",
            repo="org/repo",
            scope_id="alice:org/repo",
            project_group_id="org_repo",
            namespace=compute_namespace("alice:org/repo"),
            source="http_ctx",
        )
        caplog.set_level(logging.WARNING, logger="watercooler_mcp.auth.scope")
        warn_caller_hint_mismatch(scope=scope, caller_supplied="other_org_repo")
        assert "scope.caller_hint_mismatch" in caplog.text
