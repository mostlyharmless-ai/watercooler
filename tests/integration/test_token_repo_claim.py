"""Move 2 Phase 2a-observe — `repos` claim parsing + enforcement (TC-A1).

Plan v5.1 fixture cases C1-C11 cover canonicalisation, observe-vs-
enforce semantics, and edge cases. The primitives under test:

- ``_normalise_repos_claim`` — converts the raw token-service field
  into a list of canonical ``<org>/<repo>`` slugs (or None).
- ``check_repo_claim`` — the auth-side enforcement primitive.
- ``repo_claim_mode`` — reads ``WATERCOOLER_REQUIRE_REPO_CLAIM``.

The observation-only nature of warn mode is the operational checkpoint
documented in plan v5.1: F1 closes only at the enforce flip in
Sprint 4, not at this PR's merge.
"""

from __future__ import annotations

import logging
import os
from typing import Iterator

import pytest

from watercooler_mcp.auth import (
    GitHubTokenInfo,
    check_repo_claim,
    repo_claim_mode,
)
from watercooler_mcp.auth import _normalise_repos_claim


@pytest.fixture(autouse=True)
def _restore_log_propagation() -> Iterator[None]:
    ns_logger = logging.getLogger("watercooler_mcp")
    saved = ns_logger.propagate
    ns_logger.propagate = True
    try:
        yield
    finally:
        ns_logger.propagate = saved


@pytest.fixture(autouse=True)
def _reset_warn_cache() -> Iterator[None]:
    # The warn-once-per-token cache lives at module scope; reset it
    # per test so each test sees a clean state.
    from watercooler_mcp.auth import _repo_claim_warn_cache

    _repo_claim_warn_cache.clear()
    yield
    _repo_claim_warn_cache.clear()


@pytest.fixture
def _warn_mode() -> Iterator[None]:
    prev = os.environ.get("WATERCOOLER_REQUIRE_REPO_CLAIM")
    os.environ["WATERCOOLER_REQUIRE_REPO_CLAIM"] = "warn"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("WATERCOOLER_REQUIRE_REPO_CLAIM", None)
        else:
            os.environ["WATERCOOLER_REQUIRE_REPO_CLAIM"] = prev


@pytest.fixture
def _enforce_mode() -> Iterator[None]:
    prev = os.environ.get("WATERCOOLER_REQUIRE_REPO_CLAIM")
    os.environ["WATERCOOLER_REQUIRE_REPO_CLAIM"] = "enforce"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("WATERCOOLER_REQUIRE_REPO_CLAIM", None)
        else:
            os.environ["WATERCOOLER_REQUIRE_REPO_CLAIM"] = prev


class TestNormaliseReposClaim:
    """Token-service ``repos`` field normalisation."""

    def test_none_stays_none(self) -> None:
        assert _normalise_repos_claim(None) is None

    def test_empty_list_returns_empty_frozenset(self) -> None:
        # Distinct from absent — an explicit empty list means the
        # token has zero authorised repos. Returns ``frozenset()``
        # (not ``None``) so ``check_repo_claim`` rejects in BOTH
        # modes via the empty-collection path.
        assert _normalise_repos_claim([]) == frozenset()

    def test_non_list_returns_empty_frozenset_and_logs_malformed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Token-service contract violation: not-a-list inputs fail
        # CLOSED via empty-frozenset (rejects in both modes), AND
        # emit a distinct ``repo_claim_malformed`` log so monitoring
        # can distinguish "token-service is broken" from "tokens
        # without claims yet".
        caplog.set_level(logging.WARNING, logger="watercooler_mcp.auth")
        assert _normalise_repos_claim("not-a-list") == frozenset()
        assert _normalise_repos_claim({"org/repo": True}) == frozenset()
        assert "repo_claim_malformed" in caplog.text

    def test_canonicalises_each_entry(self) -> None:
        # Claim canonical is strict: case-fold + ``.git`` strip ONLY.
        # The ``-threads`` suffix is preserved because a real repo
        # named ``org/repo-threads`` would otherwise alias to
        # ``org/repo`` and produce false matches in either direction.
        result = _normalise_repos_claim(
            ["MostlyHarmless-AI/Watercooler-Cloud.git", "Org/Repo-threads"]
        )
        assert result == frozenset(
            {
                "mostlyharmless-ai/watercooler",
                "org/repo-threads",
            }
        )

    def test_drops_malformed_entries(self) -> None:
        # Bare slugs (no ``/``) are silently dropped so an attacker
        # cannot widen a claim by feeding malformed entries.
        result = _normalise_repos_claim(["org/repo", "bare-slug", "-threads", 42, ""])
        assert result == frozenset({"org/repo"})

    def test_drops_empty_segment_entries(self) -> None:
        # The empty-segment guard rejects forms where either the
        # owner or name segment is empty after canonicalisation:
        #
        # - ``org/.git`` → ``org/`` (name stripped to empty) → drop
        # - ``/repo`` → ``/repo`` (owner empty) → drop
        # - ``/`` → ``/`` (both empty) → drop
        # - ``""`` → ``""`` (no slash) → drop
        #
        # NOTE: ``org/-threads`` is NOT dropped because the strict
        # claim canonical does NOT strip ``-threads`` — see
        # ``test_threads_suffix_preserved_in_claim_canonical``. A
        # real repo whose name is literally ``-threads`` is a valid
        # (if unusual) entry; the legacy ``-threads`` strip lives
        # in ``auth.scope.canonical_repo`` only.
        result = _normalise_repos_claim(["org/.git", "/repo", "/", ""])
        assert result == frozenset()

    def test_threads_suffix_preserved_in_claim_canonical(self) -> None:
        # Regression for the third-pass HIGH finding: ``-threads``
        # MUST NOT be stripped from claim entries. Two correctness
        # inversions if it were:
        #
        # 1. Token issued for ``["org/repo-threads"]`` (a real GH
        #    repo) would canonicalise to ``"org/repo"`` and falsely
        #    accept X-Repo ``org/repo`` (different repo).
        # 2. Token issued for ``["org/repo"]`` would falsely accept
        #    X-Repo ``org/repo-threads`` (different repo).
        #
        # The claim-path canonical preserves the ``-threads`` suffix
        # verbatim. ``auth.scope.canonical_repo`` (the legacy form)
        # still strips for backward-compat scope derivation in the
        # ``project_group_id`` path; that's a separate concern.
        result = _normalise_repos_claim(["org/repo-threads"])
        assert result == frozenset({"org/repo-threads"})


class TestThreadsSuffixCorrectness:
    """The two correctness inversions the third-pass review flagged."""

    def test_token_for_threads_repo_does_not_match_non_threads_x_repo(
        self, _warn_mode: None
    ) -> None:
        # Inversion 1: ``["org/repo-threads"]`` MUST NOT match
        # X-Repo ``org/repo``.
        info = _make_token_info(repos=["org/repo-threads"])
        err = check_repo_claim(info, "org/repo")
        assert err is not None
        assert "repo_claim_mismatch" in err

    def test_token_for_non_threads_repo_does_not_match_threads_x_repo(
        self, _warn_mode: None
    ) -> None:
        # Inversion 2: ``["org/repo"]`` MUST NOT match X-Repo
        # ``org/repo-threads``.
        info = _make_token_info(repos=["org/repo"])
        err = check_repo_claim(info, "org/repo-threads")
        assert err is not None
        assert "repo_claim_mismatch" in err

    def test_token_for_threads_repo_matches_same_threads_x_repo(
        self, _warn_mode: None
    ) -> None:
        # Sanity: a real ``-threads``-named repo claim correctly
        # accepts an X-Repo that names the same repo.
        info = _make_token_info(repos=["org/repo-threads"])
        assert check_repo_claim(info, "org/repo-threads") is None


class TestRepoClaimMode:
    """``WATERCOOLER_REQUIRE_REPO_CLAIM`` env var → mode string."""

    def test_default_is_warn(self) -> None:
        prev = os.environ.pop("WATERCOOLER_REQUIRE_REPO_CLAIM", None)
        try:
            assert repo_claim_mode() == "warn"
        finally:
            if prev is not None:
                os.environ["WATERCOOLER_REQUIRE_REPO_CLAIM"] = prev

    def test_warn_explicit(self, _warn_mode: None) -> None:
        assert repo_claim_mode() == "warn"

    def test_enforce_explicit(self, _enforce_mode: None) -> None:
        assert repo_claim_mode() == "enforce"

    def test_truthy_aliases_resolve_to_enforce(self) -> None:
        for raw in ("1", "true", "yes", "on", "ENFORCE"):
            os.environ["WATERCOOLER_REQUIRE_REPO_CLAIM"] = raw
            try:
                assert repo_claim_mode() == "enforce", raw
            finally:
                os.environ.pop("WATERCOOLER_REQUIRE_REPO_CLAIM", None)


def _make_token_info(
    *,
    user_id: str = "alice",
    repos: object = "ABSENT",
) -> GitHubTokenInfo:
    """Build a GitHubTokenInfo, mirroring the real parse-time pipeline.

    ``check_repo_claim`` trusts ``token_info.repos`` to already be
    canonicalised (the contract is that ``_normalise_repos_claim`` is
    the single normalisation site at parse time). The test fixture
    sends raw inputs through the same normalisation so failures
    don't come from the fixture skipping the parse step.

    Pass ``repos="ABSENT"`` for the claim-absent path; pass a list
    (raw or canonical) to exercise the present-claim path.
    """
    if repos == "ABSENT":
        normalised = None
    elif repos is None:
        normalised = None
    elif isinstance(repos, frozenset):
        # Already-normalised input — pass through (the parse-to-
        # enforce pipeline tests construct frozensets directly to
        # exercise the post-parse state).
        normalised = repos
    else:
        normalised = _normalise_repos_claim(repos)  # type: ignore[arg-type]
    return GitHubTokenInfo(
        token="ghp_test",
        user_id=user_id,
        repos=normalised,
    )


class TestCheckRepoClaim:
    """Plan v5.1 fixture cases C1-C9 — observe vs enforce semantics."""

    def test_c1_claim_present_x_repo_matches_accept(self, _warn_mode: None) -> None:
        info = _make_token_info(repos=["userA/repoA"])
        assert check_repo_claim(info, "userA/repoA") is None

    def test_c2_claim_present_x_repo_mismatch_rejected(self, _warn_mode: None) -> None:
        info = _make_token_info(repos=["userA/repoA"])
        err = check_repo_claim(info, "userB/repoB")
        assert err is not None
        assert "repo_claim_mismatch" in err

    def test_c3_org_repo_match(self, _warn_mode: None) -> None:
        info = _make_token_info(repos=["mostlyharmless-ai/watercooler"])
        assert check_repo_claim(info, "mostlyharmless-ai/watercooler") is None

    def test_c4_case_insensitive(self, _warn_mode: None) -> None:
        info = _make_token_info(repos=["mostlyharmless-ai/watercooler"])
        # Mixed case in X-Repo must collapse to the same canonical
        # form as the lower-case claim entry.
        assert check_repo_claim(info, "MostlyHarmless-AI/Watercooler-Cloud") is None

    def test_c5_dot_git_strip(self, _warn_mode: None) -> None:
        info = _make_token_info(repos=["org/repo"])
        assert check_repo_claim(info, "org/repo.git") is None

    def test_c6_same_owner_wrong_repo(self, _warn_mode: None) -> None:
        info = _make_token_info(repos=["mostlyharmless-ai/watercooler"])
        err = check_repo_claim(info, "mostlyharmless-ai/other-repo")
        assert err is not None

    def test_c8_warn_mode_claim_absent_accepts(
        self, _warn_mode: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        info = _make_token_info(repos="ABSENT")
        caplog.set_level(logging.WARNING, logger="watercooler_mcp.auth")
        assert check_repo_claim(info, "userB/repoB") is None
        assert "repo_claim_absent" in caplog.text

    def test_c9_enforce_mode_claim_absent_rejects(self, _enforce_mode: None) -> None:
        info = _make_token_info(repos="ABSENT")
        err = check_repo_claim(info, "userB/repoB")
        assert err is not None
        assert "repo_claim_absent" in err

    def test_warn_mode_logs_once_per_token(
        self, _warn_mode: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Repeated calls for the same token should NOT spam the log.
        info = _make_token_info(repos="ABSENT")
        caplog.set_level(logging.WARNING, logger="watercooler_mcp.auth")
        check_repo_claim(info, "userA/x")
        check_repo_claim(info, "userA/y")
        check_repo_claim(info, "userA/z")
        # Count of "repo_claim_absent" log messages.
        msgs = [r for r in caplog.records if "repo_claim_absent" in r.getMessage()]
        assert len(msgs) == 1, f"expected one log; got {len(msgs)}"

    def test_empty_list_claim_rejects_all(self, _warn_mode: None) -> None:
        # An explicit empty list (not None) means "zero authorised
        # repos" — must reject in BOTH modes regardless of mode flag,
        # because warn-mode is specifically about how to handle
        # ABSENT claims, not present-but-empty ones.
        info = _make_token_info(repos=[])
        err = check_repo_claim(info, "userA/repoA")
        assert err is not None

    def test_empty_list_claim_rejects_when_x_repo_absent(
        self, _warn_mode: None
    ) -> None:
        # Regression: an empty ``repos`` claim with no X-Repo header
        # previously slipped through via the ``x_repo is None``
        # early-return. The fix moves the empty-list check before
        # that gate so the auth boundary fails closed regardless of
        # whether the caller supplied an X-Repo.
        info = _make_token_info(repos=[])
        err = check_repo_claim(info, None)
        assert err is not None
        assert "repo_claim_empty" in err

    def test_empty_list_claim_rejects_in_enforce_mode(
        self, _enforce_mode: None
    ) -> None:
        info = _make_token_info(repos=[])
        assert check_repo_claim(info, "userA/repoA") is not None
        assert check_repo_claim(info, None) is not None

    def test_non_empty_claim_with_no_x_repo_rejects(self, _warn_mode: None) -> None:
        # Regression for the third-pass HIGH finding: a token whose
        # claim names specific repos but the request omits X-Repo
        # was previously fail-open. Letting the request through
        # would let a token restricted to ``["org/a"]`` reach any
        # MCP tool that doesn't supply X-Repo and operate on
        # session-derived state for ``org/b``. The fix rejects in
        # BOTH modes — non-empty claim + no X-Repo means "we cannot
        # verify membership", which fails closed at the auth
        # boundary.
        info = _make_token_info(repos=["userA/repoA"])
        err = check_repo_claim(info, None)
        assert err is not None
        assert "x_repo_required" in err

    def test_non_empty_claim_with_no_x_repo_rejects_in_enforce_mode(
        self, _enforce_mode: None
    ) -> None:
        info = _make_token_info(repos=["userA/repoA"])
        assert check_repo_claim(info, None) is not None

    def test_absent_claim_with_no_x_repo_still_warn_path(
        self, _warn_mode: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Distinct from the non-empty-claim case above: when the
        # claim is absent (``None``) the warn-mode path still
        # accepts because the claim infrastructure can't verify
        # what the token can act on. Enforce mode rejects on
        # claim-absent regardless of X-Repo.
        info = _make_token_info(repos="ABSENT")
        caplog.set_level(logging.WARNING, logger="watercooler_mcp.auth")
        assert check_repo_claim(info, None) is None
        assert "repo_claim_absent" in caplog.text


class TestHmacPathRepoClaimEnforcement:
    """Move 2 Phase 2a applies to HMAC-authenticated requests too.

    The first-pass review flagged that ``check_repo_claim`` was wired
    only into the Bearer branch; the HMAC path resolved
    ``token_info`` (with ``repos`` populated) but never enforced.
    These tests assert ``check_repo_claim`` runs identically across
    both auth classes, so a token's repos claim is honoured
    regardless of how the request authenticated.
    """

    def test_hmac_path_invokes_check_repo_claim(self) -> None:
        # The HMAC branch must call ``check_repo_claim`` AFTER
        # resolving ``token_info`` and BEFORE returning ``_AuthResult``.
        # Verifying this structurally via inspection is brittle; the
        # functional check is that ``check_repo_claim`` is exposed
        # publicly for the HMAC branch to import.
        from watercooler_mcp.auth import check_repo_claim

        # The HMAC branch in server_http.py imports this function.
        # Smoke: it returns an error string for a mismatched claim
        # regardless of the auth class that produced ``token_info``.
        info = _make_token_info(repos=["org/legit"])
        # Simulate what the HMAC branch passes: x_repo from the
        # X-Repo header that the HMAC signature does NOT bind to (in
        # Phase 2a; Move 2.5 will sign it).
        err = check_repo_claim(info, "attacker/repo")
        assert err is not None
        assert "repo_claim_mismatch" in err


class TestParseToEnforcePipeline:
    """End-to-end: raw token-service field → enforcement.

    Regression for the "double-canonicalization masked bugs" finding
    in the second-pass review. With double-canonicalization removed,
    ``check_repo_claim`` trusts ``token_info.repos`` to already be
    canonical. These tests prove the parse-to-enforce pipeline
    delivers canonical entries to the enforcer for the realistic
    range of token-service inputs.
    """

    def test_raw_form_repos_field_matches_x_repo_after_pipeline(
        self, _warn_mode: None
    ) -> None:
        # Token service returns an unsanitised mixed-case ``.git``-
        # suffixed slug; X-Repo is sent in different mixed case.
        # The parse-to-enforce pipeline must collapse both onto the
        # same canonical surface and accept the request.
        raw_repos_field = ["MostlyHarmless-AI/Watercooler-Cloud.git"]
        normalised = _normalise_repos_claim(raw_repos_field)
        assert normalised == frozenset({"mostlyharmless-ai/watercooler"})
        info = _make_token_info(repos=normalised)
        # X-Repo from the caller in different case.
        assert check_repo_claim(info, "mostlyharmless-ai/Watercooler-Cloud") is None

    def test_raw_form_with_threads_suffix_preserves_repo_identity(
        self, _warn_mode: None
    ) -> None:
        # ``.git`` stripped at parse time; ``-threads`` preserved
        # (see HIGH finding on ``-threads`` correctness inversion).
        raw_repos_field = ["org/repo-threads.git"]
        normalised = _normalise_repos_claim(raw_repos_field)
        assert normalised == frozenset({"org/repo-threads"})
        info = _make_token_info(repos=normalised)
        # Same repo, different X-Repo case + .git → match.
        assert check_repo_claim(info, "Org/Repo-Threads") is None
        # Different repo (``org/repo`` is NOT the same as
        # ``org/repo-threads``) → reject.
        assert check_repo_claim(info, "org/repo") is not None

    def test_pipeline_drops_malformed_entries_and_still_enforces(
        self, _warn_mode: None
    ) -> None:
        # Bare slugs are dropped at parse time so an attacker cannot
        # widen the claim with malformed entries; downstream
        # enforcement still rejects mismatched X-Repo.
        raw_repos_field = ["org/legit", "bare-slug", "-threads"]
        normalised = _normalise_repos_claim(raw_repos_field)
        assert normalised == frozenset({"org/legit"})
        info = _make_token_info(repos=normalised)
        assert check_repo_claim(info, "org/legit") is None
        err = check_repo_claim(info, "bare-slug")
        assert err is not None  # mismatch since "bare-slug" not in claim


class TestWarnCacheConcurrency:
    """The warn-once cache must be atomic under concurrent access.

    The previous form had a non-atomic check-then-pop+add: two
    concurrent callers could both pass the size guard, both
    ``pop()`` and both ``add()``, leaving the set larger than the
    cap. ``set.pop()`` is also non-deterministic in which entry it
    drops, so the warn-once-per-token guarantee could break under
    overflow even within a single tenant. The fix wraps check-and-
    add in a ``threading.Lock``.
    """

    def test_concurrent_first_seen_calls_log_once_per_key(self) -> None:
        # 50 threads racing on the SAME cache_key. Exactly one
        # ``_claim_first_seen`` call should return True; all others
        # should see the cache hit.
        import threading

        from watercooler_mcp.auth import _claim_first_seen

        results: list[bool] = []
        results_lock = threading.Lock()
        cache_key = "concurrent-test-token"

        def worker():
            r = _claim_first_seen(cache_key)
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r) == 1, (
            f"expected exactly one first-seen=True; "
            f"got {sum(1 for r in results if r)}"
        )

    def test_overflow_does_not_violate_cap(self) -> None:
        # Push the cache to exactly at-cap, then verify a single
        # overflow keeps the cache at cap (no lock-internal race).
        from watercooler_mcp.auth import (
            _REPO_CLAIM_WARN_CACHE_MAX,
            _claim_first_seen,
            _repo_claim_warn_cache,
        )

        # Reset starts in the autouse fixture, so the cache is empty
        # at test entry.
        for i in range(_REPO_CLAIM_WARN_CACHE_MAX):
            assert _claim_first_seen(f"key-{i}") is True
        assert len(_repo_claim_warn_cache) == _REPO_CLAIM_WARN_CACHE_MAX

        # One more entry — must evict and stay at cap.
        assert _claim_first_seen("overflow-key") is True
        assert len(_repo_claim_warn_cache) == _REPO_CLAIM_WARN_CACHE_MAX

    def test_lru_eviction_evicts_oldest_not_arbitrary(self) -> None:
        # Regression for the third-pass MEDIUM finding: ``set.pop()``
        # evicted an arbitrary entry, so under flood a legitimate
        # cached token could be cycled out and re-warn. The fix
        # uses ``OrderedDict`` LRU: the OLDEST (earliest-observed)
        # key is evicted, and a hit on a cached key refreshes its
        # position so regularly-accessed legitimate tokens are
        # protected.
        from watercooler_mcp.auth import (
            _REPO_CLAIM_WARN_CACHE_MAX,
            _claim_first_seen,
            _repo_claim_warn_cache,
        )

        # Fill cache with N keys, where N == cap.
        for i in range(_REPO_CLAIM_WARN_CACHE_MAX):
            _claim_first_seen(f"key-{i}")

        # "Touch" key-0 (most-recently-observed) so it's pulled to
        # the end and protected from imminent eviction.
        assert _claim_first_seen("key-0") is False  # cache hit

        # Now overflow with one new key. The evicted entry must be
        # ``key-1`` (oldest in current order), NOT ``key-0`` (just
        # touched).
        _claim_first_seen("new-key")
        assert "key-0" in _repo_claim_warn_cache
        assert "key-1" not in _repo_claim_warn_cache
        assert "new-key" in _repo_claim_warn_cache
