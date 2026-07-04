"""Unit tests for the HMAC v3 primitives in ``auth.hmac_keys``.

Covers H1, H10, H13, H14 from plan v5.1's Move 2.5 verification matrix.
End-to-end auth-pipeline tests live in ``test_hmac_v3.py``.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from watercooler_mcp.auth.hmac_keys import (
    GLOBAL_LEGACY_KEY_ID,
    KeyInfo,
    KeyRegistry,
    RepoAuthError,
    build_v3_canonical_string,
    check_repo_authorisation,
    check_subject_binding,
    hmac_v3_startup_fail_fast_check,
    load_default_registry,
    parse_v3_authorization_header,
    verify_v3_signature,
)


# ------------------------------------------------------------------ #
# Canonical string + signature
# ------------------------------------------------------------------ #


class TestCanonicalString:
    """H1: canonical string covers all eight fields."""

    def test_canonical_includes_all_fields(self) -> None:
        canonical = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="2026-04-29T20:30:00Z",
            key_id="kid-1",
            user_id="u1",
            body=b"hello",
            x_repo="org/repo",
            x_branch="main",
        )
        decoded = canonical.decode("utf-8")
        assert "POST" in decoded
        assert "/mcp/" in decoded
        assert "2026-04-29T20:30:00Z" in decoded
        assert "kid-1" in decoded
        assert "u1" in decoded
        assert hashlib.sha256(b"hello").hexdigest() in decoded
        assert "org/repo" in decoded
        assert "main" in decoded

    def test_canonical_field_order(self) -> None:
        canonical = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="t",
            key_id="kid",
            user_id="u",
            body=b"",
            x_repo="r",
            x_branch="b",
        )
        # newline-delimited, in plan v5.1 order
        parts = canonical.decode("utf-8").split("\n")
        assert parts == [
            "POST",
            "/mcp/",
            "t",
            "kid",
            "u",
            hashlib.sha256(b"").hexdigest(),
            "r",
            "b",
        ]

    def test_canonical_changes_when_x_repo_changes(self) -> None:
        a = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="t",
            key_id="k",
            user_id="u",
            body=b"",
            x_repo="org/A",
            x_branch="main",
        )
        b = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="t",
            key_id="k",
            user_id="u",
            body=b"",
            x_repo="org/B",
            x_branch="main",
        )
        assert a != b


class TestSignatureVerification:
    """H1: signature integrity — tampering invalidates."""

    def _sign(self, secret: bytes, canonical: bytes) -> str:
        return hmac.new(secret, canonical, hashlib.sha256).hexdigest()

    def test_valid_signature_verifies(self) -> None:
        secret = b"shared"
        canonical = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="t",
            key_id="k",
            user_id="u",
            body=b"hello",
            x_repo="org/repo",
            x_branch="main",
        )
        sig = self._sign(secret, canonical)
        assert verify_v3_signature(
            canonical=canonical, signature_hex=sig, secret=secret
        )

    def test_tampered_x_repo_invalidates(self) -> None:
        secret = b"shared"
        signed = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="t",
            key_id="k",
            user_id="u",
            body=b"hello",
            x_repo="org/A",
            x_branch="main",
        )
        sig = self._sign(secret, signed)
        replayed = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="t",
            key_id="k",
            user_id="u",
            body=b"hello",
            x_repo="org/B",
            x_branch="main",
        )
        assert not verify_v3_signature(
            canonical=replayed, signature_hex=sig, secret=secret
        )

    def test_garbage_signature_returns_false(self) -> None:
        canonical = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="t",
            key_id="k",
            user_id="u",
            body=b"",
            x_repo="r",
            x_branch="b",
        )
        assert not verify_v3_signature(
            canonical=canonical, signature_hex="not-hex", secret=b"shared"
        )

    def test_canonical_string_rejects_newline_in_user_id(self) -> None:
        # PR #703 round 7+3 LOW: defense-in-depth. A newline in
        # user_id would shift every downstream field and let a
        # crafted request sign a different canonical string than
        # the server reconstructs. HTTP/1.1 parsers reject CR/LF
        # in header values so this is unreachable through standard
        # transports, but ``build_v3_canonical_string`` rejects it
        # at the call site for the same reason ``_is_valid_key_id``
        # validates ``key_id``.
        with pytest.raises(ValueError, match="user_id"):
            build_v3_canonical_string(
                method="POST",
                path="/mcp/",
                timestamp="t",
                key_id="k",
                user_id="alice\norg/repo",  # injects field boundary
                body=b"",
                x_repo="org/repo",
                x_branch="main",
            )

    def test_canonical_string_rejects_newline_in_x_repo(self) -> None:
        with pytest.raises(ValueError, match="x_repo"):
            build_v3_canonical_string(
                method="POST",
                path="/mcp/",
                timestamp="t",
                key_id="k",
                user_id="alice",
                body=b"",
                x_repo="org/repo\nmain",
                x_branch="main",
            )

    def test_canonical_string_rejects_carriage_return_in_x_branch(self) -> None:
        with pytest.raises(ValueError, match="x_branch"):
            build_v3_canonical_string(
                method="POST",
                path="/mcp/",
                timestamp="t",
                key_id="k",
                user_id="alice",
                body=b"",
                x_repo="org/repo",
                x_branch="main\rinjected",
            )

    def test_canonical_string_rejects_newline_in_path(self) -> None:
        # PR #703 round 7+5 LOW: extend the CR/LF guard to ``path``.
        # A reverse proxy or middleware that percent-decodes
        # ``%0a`` / ``%0d`` in the URL path before Starlette sees
        # it would shift canonical fields the same way as a CR/LF
        # in user_id/x_repo/x_branch.
        with pytest.raises(ValueError, match="path"):
            build_v3_canonical_string(
                method="POST",
                path="/mcp/\nsomething",
                timestamp="t",
                key_id="k",
                user_id="alice",
                body=b"",
                x_repo="org/repo",
                x_branch="main",
            )

    def test_canonical_string_rejects_carriage_return_in_path(self) -> None:
        with pytest.raises(ValueError, match="path"):
            build_v3_canonical_string(
                method="POST",
                path="/mcp/\rinjected",
                timestamp="t",
                key_id="k",
                user_id="alice",
                body=b"",
                x_repo="org/repo",
                x_branch="main",
            )

    def test_uppercase_hex_signature_verifies(self) -> None:
        # PR #703 round 7+2 MED: ``parse_v3_authorization_header``
        # accepts mixed-case hex (``[0-9a-fA-F]{64}``); a unit test
        # explicitly pins that contract. ``verify_v3_signature``
        # was case-sensitive on the candidate, so uppercase hex
        # always failed despite a cryptographically correct sig.
        # Verify both upper- and mixed-case hex now verify the
        # same correct secret.
        secret = b"shared"
        canonical = build_v3_canonical_string(
            method="POST",
            path="/mcp/",
            timestamp="t",
            key_id="k",
            user_id="u",
            body=b"hello",
            x_repo="org/repo",
            x_branch="main",
        )
        sig_lower = self._sign(secret, canonical)
        assert verify_v3_signature(
            canonical=canonical, signature_hex=sig_lower.upper(), secret=secret
        )
        # Mixed case (alternating) — should also verify.
        mixed = "".join(
            c.upper() if i % 2 else c for i, c in enumerate(sig_lower)
        )
        assert verify_v3_signature(
            canonical=canonical, signature_hex=mixed, secret=secret
        )


# ------------------------------------------------------------------ #
# Authorization header parsing
# ------------------------------------------------------------------ #


_VALID_SIG = "ab" * 32  # 64 hex chars — SHA-256 HMAC output length


class TestAuthorizationHeaderParsing:
    def test_valid_v3_header_parses(self) -> None:
        result = parse_v3_authorization_header(
            f"HMAC-SHA256 v=3 kid=svc-1 sig={_VALID_SIG}"
        )
        assert result == ("svc-1", _VALID_SIG)

    def test_v2_form_returns_none(self) -> None:
        assert parse_v3_authorization_header("Bearer token") is None

    def test_missing_version_returns_none(self) -> None:
        assert parse_v3_authorization_header("HMAC-SHA256 kid=k sig=s") is None

    def test_missing_kid_returns_none(self) -> None:
        assert parse_v3_authorization_header("HMAC-SHA256 v=3 sig=s") is None

    def test_missing_sig_returns_none(self) -> None:
        assert parse_v3_authorization_header("HMAC-SHA256 v=3 kid=k") is None

    def test_wrong_version_returns_none(self) -> None:
        # v=2 in v3-shaped header — not v3
        assert parse_v3_authorization_header("HMAC-SHA256 v=2 kid=k sig=s") is None

    def test_empty_returns_none(self) -> None:
        assert parse_v3_authorization_header("") is None

    def test_kid_with_newline_rejected_at_parse(self) -> None:
        # PR #703 round 3 MED finding: defence-in-depth at the
        # parse layer. The registry-load path already rejects
        # malformed kids, so this is harmless in practice — but
        # rejecting at parse removes the implicit dependency on
        # registry miss and keeps the invariant local.
        assert (
            parse_v3_authorization_header(
                f"HMAC-SHA256 v=3 kid=foo\nbar sig={_VALID_SIG}"
            )
            is None
        )

    def test_kid_with_special_chars_rejected_at_parse(self) -> None:
        for bad in ("foo.bar", "foo bar"):
            assert (
                parse_v3_authorization_header(
                    f"HMAC-SHA256 v=3 kid={bad} sig={_VALID_SIG}"
                )
                is None
            ), f"kid {bad!r} should be rejected at parse"

    def test_valid_kid_with_dashes_and_underscores_accepted(self) -> None:
        result = parse_v3_authorization_header(
            f"HMAC-SHA256 v=3 kid=svc-1_dashboard sig={_VALID_SIG}"
        )
        assert result == ("svc-1_dashboard", _VALID_SIG)

    def test_kid_ending_in_reserved_metadata_suffix_rejected(self) -> None:
        # PR #703 round 4 LOW: kids ending in ``_TYPE`` /
        # ``_SERVICE_IDENTITY`` / ``_DELEGATION`` / ``_REPOS`` are
        # reserved for env-var metadata and would create
        # namespace ambiguity. Reject at parse layer (and at
        # registry-load layer).
        for bad in (
            "foo_TYPE",
            "foo_SERVICE_IDENTITY",
            "foo_DELEGATION",
            "foo_REPOS",
        ):
            assert (
                parse_v3_authorization_header(
                    f"HMAC-SHA256 v=3 kid={bad} sig={_VALID_SIG}"
                )
                is None
            ), f"kid {bad!r} should be rejected (reserved suffix)"

    def test_non_hex_sig_rejected_at_parse(self) -> None:
        # PR #703 round 4 LOW: hex-only sig at parse to surface
        # malformed headers before unnecessary HMAC compute.
        assert (
            parse_v3_authorization_header(
                "HMAC-SHA256 v=3 kid=svc sig=not-a-hex-string"
            )
            is None
        )
        assert (
            parse_v3_authorization_header(
                "HMAC-SHA256 v=3 kid=svc sig=zzzz"  # not-hex chars
            )
            is None
        )

    def test_hex_sig_accepted_mixed_case(self) -> None:
        # Hex-only does NOT mean lowercase-only — both 0xab and
        # 0xAB are valid hex bytes.
        mixed_64 = "DeadBeef" * 8  # 64 hex chars, mixed case
        result = parse_v3_authorization_header(
            f"HMAC-SHA256 v=3 kid=svc sig={mixed_64}"
        )
        assert result == ("svc", mixed_64)

    def test_short_sig_rejected_at_parse(self) -> None:
        # PR #703 round 6 LOW: SHA-256 HMAC output is exactly 64
        # hex chars. Anything shorter (or longer) is rejected at
        # parse, before any HMAC work.
        assert (
            parse_v3_authorization_header("HMAC-SHA256 v=3 kid=svc sig=deadbeef")
            is None
        )

    def test_long_sig_rejected_at_parse(self) -> None:
        # 65 chars: one too many.
        long_sig = "a" * 65
        assert (
            parse_v3_authorization_header(f"HMAC-SHA256 v=3 kid=svc sig={long_sig}")
            is None
        )


# ------------------------------------------------------------------ #
# Subject-binding (H10 — cross-subject assertion blocked)
# ------------------------------------------------------------------ #


class TestSubjectBinding:
    """H10: a key issued for userA cannot assert ``X-User-ID: userB``."""

    def test_per_user_key_accepts_bound_subject(self) -> None:
        key = KeyInfo(
            key_id="k", secret=b"s", key_type="per_user", bound_user_id="alice"
        )
        assert check_subject_binding(key=key, signed_user_id="alice") is None

    def test_per_user_key_rejects_other_subject(self) -> None:
        key = KeyInfo(
            key_id="k", secret=b"s", key_type="per_user", bound_user_id="alice"
        )
        err = check_subject_binding(key=key, signed_user_id="bob")
        assert err is not None
        assert "subject mismatch" in err.lower()

    def test_legacy_global_key_accepts_any_subject_single_tenant(self) -> None:
        # bound_user_id=None on the legacy global is the back-compat path.
        # Single-tenant deployments (is_multi_tenant=False, the default)
        # retain wildcard semantics for service-account-style local use.
        key = KeyInfo(
            key_id="legacy", secret=b"s", key_type="per_user", bound_user_id=None
        )
        assert check_subject_binding(key=key, signed_user_id="alice") is None
        assert check_subject_binding(key=key, signed_user_id="bob") is None

    def test_wildcard_per_user_key_rejected_in_multi_tenant(self) -> None:
        """PR #741 review: wildcard per_user key (bound_user_id=None) MUST be
        refused at request time in multi-tenant mode, regardless of how the
        key was issued (env var, dashboard, or HTTP resolver).

        The startup ``hmac_v3_startup_fail_fast_check`` only sees keys
        statically loaded into the registry; HTTP-resolver responses arrive
        after startup. This runtime check is what closes the H13 gap for
        resolver-issued wildcards.
        """
        key = KeyInfo(
            key_id="resolver_wildcard",
            secret=b"s",
            key_type="per_user",
            bound_user_id=None,
        )
        err = check_subject_binding(
            key=key, signed_user_id="alice", is_multi_tenant=True
        )
        assert err is not None
        assert "wildcard per_user key" in err
        assert "multi-tenant" in err
        # Also rejects for any other signed user — the rejection is
        # categorical, not subject-specific.
        err_bob = check_subject_binding(
            key=key, signed_user_id="bob", is_multi_tenant=True
        )
        assert err_bob is not None

    def test_service_no_delegation_requires_service_identity(self) -> None:
        # H11
        key = KeyInfo(
            key_id="svc",
            secret=b"s",
            key_type="service",
            service_identity="dashboard",
            delegation_allow_list=None,  # no_user_delegation
        )
        assert check_subject_binding(key=key, signed_user_id="dashboard") is None
        err = check_subject_binding(key=key, signed_user_id="alice")
        assert err is not None

    def test_service_delegation_allow_list(self) -> None:
        # H12
        key = KeyInfo(
            key_id="svc",
            secret=b"s",
            key_type="service",
            service_identity="dashboard",
            delegation_allow_list=frozenset({"alice", "bob"}),
        )
        assert check_subject_binding(key=key, signed_user_id="alice") is None
        assert check_subject_binding(key=key, signed_user_id="bob") is None
        err = check_subject_binding(key=key, signed_user_id="charlie")
        assert err is not None


# ------------------------------------------------------------------ #
# Repo-authorization (H2-H5)
# ------------------------------------------------------------------ #


class TestRepoAuthorisation:
    def test_per_user_with_matching_claim_allows(self) -> None:
        # H2
        key = KeyInfo(
            key_id="k", secret=b"s", key_type="per_user", bound_user_id="alice"
        )
        claim = frozenset({"org/repo"})
        assert (
            check_repo_authorisation(
                key=key, x_repo="org/repo", per_user_repo_claim=claim
            )
            is None
        )

    def test_per_user_with_mismatched_claim_denies_non_fatal(self) -> None:
        # H3 — request mismatch is acceptable in warn-mode
        key = KeyInfo(
            key_id="k", secret=b"s", key_type="per_user", bound_user_id="alice"
        )
        claim = frozenset({"org/A"})
        err = check_repo_authorisation(
            key=key, x_repo="org/B", per_user_repo_claim=claim
        )
        assert isinstance(err, RepoAuthError)
        assert err.fatal is False
        assert "not in token claim" in err.message

    def test_per_user_with_no_claim_denies_fatally(self) -> None:
        # Operator-misconfiguration on the issuer side — reject
        # unconditionally regardless of warn/enforce mode.
        key = KeyInfo(
            key_id="k", secret=b"s", key_type="per_user", bound_user_id="alice"
        )
        err = check_repo_authorisation(
            key=key, x_repo="org/repo", per_user_repo_claim=None
        )
        assert isinstance(err, RepoAuthError)
        assert err.fatal is True
        assert "no repos claim" in err.message

    def test_per_user_with_empty_claim_denies_fatally(self) -> None:
        key = KeyInfo(
            key_id="k", secret=b"s", key_type="per_user", bound_user_id="alice"
        )
        err = check_repo_authorisation(
            key=key, x_repo="org/repo", per_user_repo_claim=frozenset()
        )
        assert isinstance(err, RepoAuthError)
        assert err.fatal is True

    def test_per_user_claim_with_mixed_case_canonicalises_at_check(
        self,
    ) -> None:
        # PR #703 round 7+5+1 MED: ``check_repo_authorisation``
        # canonicalises ``x_repo`` via ``canonical_repo``
        # but previously accepted ``per_user_repo_claim`` verbatim.
        # The bearer parse path in ``auth/__init__.py`` already
        # canonicalises, but a future caller (custom token resolver,
        # WebSocket transport) might pass non-canonical entries.
        # Verify the membership test now normalises both sides.
        key = KeyInfo(
            key_id="k", secret=b"s", key_type="per_user", bound_user_id="alice"
        )
        # Mixed-case + .git suffix in the claim — emulates a token
        # issuer that didn't pre-canonicalise.
        non_canonical_claim = frozenset({"Org/Repo.git"})
        # x_repo as a normal lower-cased canonical form.
        result = check_repo_authorisation(
            key=key, x_repo="org/repo", per_user_repo_claim=non_canonical_claim
        )
        assert result is None, (
            "non-canonical token claim should be normalised at check time; "
            f"got: {result}"
        )

    def test_service_with_matching_allow_list_allows(self) -> None:
        # H4
        key = KeyInfo(
            key_id="svc",
            secret=b"s",
            key_type="service",
            service_identity="svc",
            repo_allow_list=frozenset({"org/repo"}),
        )
        assert (
            check_repo_authorisation(
                key=key, x_repo="org/repo", per_user_repo_claim=None
            )
            is None
        )

    def test_service_with_mismatched_allow_list_denies_non_fatal(self) -> None:
        # H5 — request mismatch is acceptable in warn-mode
        key = KeyInfo(
            key_id="svc",
            secret=b"s",
            key_type="service",
            service_identity="svc",
            repo_allow_list=frozenset({"org/A"}),
        )
        err = check_repo_authorisation(
            key=key, x_repo="org/B", per_user_repo_claim=None
        )
        assert isinstance(err, RepoAuthError)
        assert err.fatal is False
        assert "service allow-list" in err.message

    def test_service_with_empty_allow_list_denies_fatally(self) -> None:
        # Empty allow-list = operator forgot to set the
        # ``WATERCOOLER_HMAC_KEY_<id>_REPOS`` env var. Honouring
        # warn-mode here would let the key authenticate against any
        # X-Repo — opposite of fail-closed.
        key = KeyInfo(
            key_id="svc",
            secret=b"s",
            key_type="service",
            service_identity="svc",
            repo_allow_list=frozenset(),
        )
        err = check_repo_authorisation(
            key=key, x_repo="org/repo", per_user_repo_claim=None
        )
        assert isinstance(err, RepoAuthError)
        assert err.fatal is True

    def test_service_with_none_allow_list_denies_fatally(self) -> None:
        key = KeyInfo(
            key_id="svc",
            secret=b"s",
            key_type="service",
            service_identity="svc",
            repo_allow_list=None,
        )
        err = check_repo_authorisation(
            key=key, x_repo="org/repo", per_user_repo_claim=None
        )
        assert isinstance(err, RepoAuthError)
        assert err.fatal is True

    def test_empty_x_repo_denies_fatally(self) -> None:
        key = KeyInfo(
            key_id="k", secret=b"s", key_type="per_user", bound_user_id="alice"
        )
        err = check_repo_authorisation(
            key=key, x_repo="", per_user_repo_claim=frozenset({"org/repo"})
        )
        assert isinstance(err, RepoAuthError)
        assert err.fatal is True
        assert "X-Repo header required" in err.message


# ------------------------------------------------------------------ #
# Registry + revocation (H14)
# ------------------------------------------------------------------ #


class TestKeyRegistry:
    def test_lookup_returns_added_key(self) -> None:
        registry = KeyRegistry()
        key = KeyInfo(key_id="kid", secret=b"s", key_type="per_user", bound_user_id="u")
        registry.add(key)
        assert registry.lookup("kid") == key

    def test_lookup_unknown_returns_none(self) -> None:
        # H14 (unknown)
        registry = KeyRegistry()
        assert registry.lookup("missing") is None

    def test_revoked_key_lookup_returns_none(self) -> None:
        # H14 (revoked)
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="kid",
                secret=b"s",
                key_type="per_user",
                bound_user_id="u",
            )
        )
        assert registry.revoke("kid") is True
        assert registry.lookup("kid") is None

    def test_revoke_unknown_returns_false(self) -> None:
        assert KeyRegistry().revoke("nope") is False

    def test_revoke_already_revoked_returns_false(self) -> None:
        # PR #703 round 7+4 LOW: previously the ``info is None``
        # guard alone allowed a re-revoke to return True (and
        # rebuild a redundant KeyInfo). Audit-logging callers
        # using the return value to detect "this call changed
        # state" got a misleading signal. ``True`` iff this call
        # actually transitioned active → revoked.
        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="kid",
                secret=b"s",
                key_type="per_user",
                bound_user_id="u",
            )
        )
        assert registry.revoke("kid") is True
        assert registry.revoke("kid") is False, (
            "double-revoke must report no-op (False), not True"
        )

    def test_len_excludes_revoked(self) -> None:
        registry = KeyRegistry()
        registry.add(
            KeyInfo(key_id="a", secret=b"s", key_type="per_user", bound_user_id="u")
        )
        registry.add(
            KeyInfo(key_id="b", secret=b"s", key_type="per_user", bound_user_id="u")
        )
        assert len(registry) == 2
        registry.revoke("a")
        assert len(registry) == 1


class TestLoadDefaultRegistry:
    def test_legacy_global_secret_no_longer_loaded(self) -> None:
        """Issue #733: the legacy v1/v2 single-secret loader was deleted.

        Setting the legacy env var must NOT register a wildcard
        back-compat key in the v3 registry.
        """
        env = {"WATERCOOLER_INTERNAL_SECRET": "global-secret"}
        registry = load_default_registry(env)
        assert registry.lookup(GLOBAL_LEGACY_KEY_ID) is None
        assert len(registry) == 0

    def test_loads_service_keys_from_env(self) -> None:
        env = {
            "WATERCOOLER_HMAC_KEY_dashboard_SECRET": "svc-secret",
            "WATERCOOLER_HMAC_KEY_dashboard_TYPE": "service",
            "WATERCOOLER_HMAC_KEY_dashboard_SERVICE_IDENTITY": "dashboard",
            "WATERCOOLER_HMAC_KEY_dashboard_DELEGATION": "self",
            "WATERCOOLER_HMAC_KEY_dashboard_REPOS": "org/repo,org/other",
        }
        registry = load_default_registry(env)
        info = registry.lookup("dashboard")
        assert info is not None
        assert info.key_type == "service"
        assert info.service_identity == "dashboard"
        assert info.delegation_allow_list is None  # "self" → None
        assert info.repo_allow_list == frozenset({"org/repo", "org/other"})

    def test_skips_service_key_missing_identity(self) -> None:
        env = {
            "WATERCOOLER_HMAC_KEY_bad_SECRET": "s",
            "WATERCOOLER_HMAC_KEY_bad_TYPE": "service",
            # No SERVICE_IDENTITY
        }
        registry = load_default_registry(env)
        assert registry.lookup("bad") is None

    def test_rejects_key_id_with_newline(self) -> None:
        # PR #703 round 2 LOW finding: a key_id containing ``\n``
        # would corrupt newline-delimited canonical-string field
        # boundaries for any request using it. The loader validates
        # against ``[A-Za-z0-9_-]+`` and skips mismatches.
        env = {
            "WATERCOOLER_HMAC_KEY_bad\nkid_SECRET": "s",
            "WATERCOOLER_HMAC_KEY_bad\nkid_TYPE": "service",
            "WATERCOOLER_HMAC_KEY_bad\nkid_SERVICE_IDENTITY": "svc",
            "WATERCOOLER_HMAC_KEY_bad\nkid_REPOS": "org/repo",
        }
        registry = load_default_registry(env)
        assert len(registry) == 0

    def test_rejects_key_id_with_special_chars(self) -> None:
        # ``=`` / space / ``.`` / ``/`` would tokenize awkwardly in
        # the Authorization-header parser. All rejected at load.
        for bad in ("svc.id", "svc id", "svc=id", "svc/id"):
            env = {
                f"WATERCOOLER_HMAC_KEY_{bad}_SECRET": "s",
                f"WATERCOOLER_HMAC_KEY_{bad}_TYPE": "service",
                f"WATERCOOLER_HMAC_KEY_{bad}_SERVICE_IDENTITY": "svc",
                f"WATERCOOLER_HMAC_KEY_{bad}_REPOS": "org/repo",
            }
            registry = load_default_registry(env)
            assert registry.lookup(bad) is None, f"{bad!r} should be rejected"

    def test_accepts_valid_key_id(self) -> None:
        # Sanity: alphanumeric + ``_`` + ``-`` is allowed.
        env = {
            "WATERCOOLER_HMAC_KEY_dashboard-1_SECRET": "s",
            "WATERCOOLER_HMAC_KEY_dashboard-1_TYPE": "service",
            "WATERCOOLER_HMAC_KEY_dashboard-1_SERVICE_IDENTITY": "svc",
            "WATERCOOLER_HMAC_KEY_dashboard-1_REPOS": "org/repo",
        }
        registry = load_default_registry(env)
        assert registry.lookup("dashboard-1") is not None

    def test_warns_on_empty_repos_list(self) -> None:
        # PR #703 round 4 MED: a service key registered with an
        # empty allow-list will reject every request at call time.
        # Loud-log at load time so operators know.
        #
        # ``caplog`` does NOT work for the ``watercooler_mcp.*``
        # namespace because the package's ``observability`` init
        # flips ``propagate=False`` on the parent logger,
        # blocking caplog's root handler. Attach a direct
        # StringIO handler to the leaf logger instead — the same
        # pattern ``test_secret_wrapper.py`` uses for its logging
        # checks.
        import io
        import logging as _logging

        from watercooler_mcp.auth.hmac_keys import logger as hmac_logger

        buf = io.StringIO()
        handler = _logging.StreamHandler(buf)
        handler.setFormatter(_logging.Formatter("%(levelname)s %(message)s"))
        hmac_logger.addHandler(handler)
        prior_level = hmac_logger.level
        hmac_logger.setLevel(_logging.WARNING)
        try:
            env = {
                "WATERCOOLER_HMAC_KEY_dashboard_SECRET": "s",
                "WATERCOOLER_HMAC_KEY_dashboard_TYPE": "service",
                "WATERCOOLER_HMAC_KEY_dashboard_SERVICE_IDENTITY": "svc",
                "WATERCOOLER_HMAC_KEY_dashboard_REPOS": "",  # explicit empty
            }
            registry = load_default_registry(env)
        finally:
            hmac_logger.removeHandler(handler)
            hmac_logger.setLevel(prior_level)

        # Key registers (so traffic doesn't silently fall through
        # to v2 / legacy) but with an empty allow-list (every
        # request fails closed via RepoAuthError.fatal=True).
        info = registry.lookup("dashboard")
        assert info is not None
        assert info.repo_allow_list == frozenset()
        # Warning surfaces the misconfiguration.
        captured = buf.getvalue()
        assert (
            "empty repo allow-list" in captured
        ), f"expected empty-allow-list warning, got: {captured!r}"

    def test_warns_on_empty_delegation_list(self) -> None:
        # PR #703 round 6 MED: an explicit empty ``DELEGATION=`` (not
        # ``self``, not omitted) parses to ``frozenset()``. Because
        # delegation is non-None, ``check_subject_binding`` falls
        # through to the ``signed_user_id not in <empty>`` check and
        # rejects every request silently. Mirror the empty-REPOS
        # warning so operators detect the misconfiguration.
        import io
        import logging as _logging

        from watercooler_mcp.auth.hmac_keys import logger as hmac_logger

        buf = io.StringIO()
        handler = _logging.StreamHandler(buf)
        handler.setFormatter(_logging.Formatter("%(levelname)s %(message)s"))
        hmac_logger.addHandler(handler)
        prior_level = hmac_logger.level
        hmac_logger.setLevel(_logging.WARNING)
        try:
            env = {
                "WATERCOOLER_HMAC_KEY_svc_SECRET": "s",
                "WATERCOOLER_HMAC_KEY_svc_TYPE": "service",
                "WATERCOOLER_HMAC_KEY_svc_SERVICE_IDENTITY": "svc",
                "WATERCOOLER_HMAC_KEY_svc_DELEGATION": "",  # explicit empty
                "WATERCOOLER_HMAC_KEY_svc_REPOS": "org/repo",
            }
            registry = load_default_registry(env)
        finally:
            hmac_logger.removeHandler(handler)
            hmac_logger.setLevel(prior_level)

        info = registry.lookup("svc")
        assert info is not None
        # Empty delegation is preserved in registry — keeps the failure
        # mode loud (rejects all calls) rather than silently falling
        # back to ``no_user_delegation``.
        assert info.delegation_allow_list == frozenset()
        captured = buf.getvalue()
        assert (
            "empty delegation allow-list" in captured
        ), f"expected empty-delegation warning, got: {captured!r}"

    def test_repos_canonicalised_at_load_time(self) -> None:
        # PR #703 round 7+1 MED: the request path normalises X-Repo
        # via ``canonical_repo`` (lower-case, .git-strip)
        # before the membership test against ``key.repo_allow_list``.
        # If the env var stores the raw mixed-case string, the entry
        # silently never matches. Verify entries are canonicalised
        # on registration so request-side normalisation will hit.
        env = {
            "WATERCOOLER_HMAC_KEY_svc_SECRET": "s",
            "WATERCOOLER_HMAC_KEY_svc_TYPE": "service",
            "WATERCOOLER_HMAC_KEY_svc_SERVICE_IDENTITY": "svc",
            "WATERCOOLER_HMAC_KEY_svc_REPOS": "Org/Repo, Other/THING.git",
        }
        registry = load_default_registry(env)
        info = registry.lookup("svc")
        assert info is not None
        # Lowercased + .git stripped at load — matches the form the
        # request path will produce.
        assert info.repo_allow_list == frozenset(
            {"org/repo", "other/thing"}
        ), f"got: {info.repo_allow_list}"

    def test_legacy_global_secret_in_env_does_not_register_or_log(
        self,
    ) -> None:
        # Issue #733: the loader no longer reads the legacy global
        # secret env var. With only that env var set, the registry
        # is empty and the startup log reports zero v3-reachable
        # keys. Pin the new behaviour so a future re-introduction
        # of the legacy loader is caught.
        import io
        import logging as _logging

        from watercooler_mcp.auth.hmac_keys import logger as hmac_logger

        buf = io.StringIO()
        handler = _logging.StreamHandler(buf)
        handler.setFormatter(_logging.Formatter("%(levelname)s %(message)s"))
        hmac_logger.addHandler(handler)
        prior_level = hmac_logger.level
        hmac_logger.setLevel(_logging.INFO)
        try:
            env = {"WATERCOOLER_INTERNAL_SECRET": "legacy"}
            registry = load_default_registry(env)
        finally:
            hmac_logger.removeHandler(handler)
            hmac_logger.setLevel(prior_level)
        captured = buf.getvalue()
        assert "0 v3-reachable" in captured
        # No legacy-shim mention in the post-#733 log line.
        assert "legacy global back-compat shim" not in captured
        # No legacy sentinel registered.
        assert registry.lookup("legacy-global-v2") is None
        assert len(registry) == 0

    def test_service_key_count_unaffected_by_legacy_env_var(self) -> None:
        # When both a service key and the (now-ignored) legacy env
        # var are configured, the v3-reachable count is just the
        # service key — and the legacy var has no effect.
        import io
        import logging as _logging

        from watercooler_mcp.auth.hmac_keys import logger as hmac_logger

        buf = io.StringIO()
        handler = _logging.StreamHandler(buf)
        handler.setFormatter(_logging.Formatter("%(levelname)s %(message)s"))
        hmac_logger.addHandler(handler)
        prior_level = hmac_logger.level
        hmac_logger.setLevel(_logging.INFO)
        try:
            env = {
                "WATERCOOLER_INTERNAL_SECRET": "legacy",
                "WATERCOOLER_HMAC_KEY_svc_SECRET": "s",
                "WATERCOOLER_HMAC_KEY_svc_TYPE": "service",
                "WATERCOOLER_HMAC_KEY_svc_SERVICE_IDENTITY": "svc",
                "WATERCOOLER_HMAC_KEY_svc_REPOS": "org/repo",
            }
            registry = load_default_registry(env)
        finally:
            hmac_logger.removeHandler(handler)
            hmac_logger.setLevel(prior_level)
        captured = buf.getvalue()
        assert "1 v3-reachable" in captured
        assert "legacy global back-compat shim" not in captured
        assert registry.lookup("legacy-global-v2") is None
        assert registry.lookup("svc") is not None

    def test_warns_on_misspelled_type(self) -> None:
        # PR #703 round 7+4 MED: a SECRET set without a matching
        # ``_TYPE=service`` (e.g. omitted, capitalised "Service",
        # blank) was silently dropped at registration. Operators
        # got no diagnostic — only a registry count lower than
        # expected. Verify the warning fires.
        import io
        import logging as _logging

        from watercooler_mcp.auth.hmac_keys import logger as hmac_logger

        buf = io.StringIO()
        handler = _logging.StreamHandler(buf)
        handler.setFormatter(_logging.Formatter("%(levelname)s %(message)s"))
        hmac_logger.addHandler(handler)
        prior_level = hmac_logger.level
        hmac_logger.setLevel(_logging.WARNING)
        try:
            env = {
                # SECRET set; TYPE has the wrong case.
                "WATERCOOLER_HMAC_KEY_misspelled_SECRET": "s",
                "WATERCOOLER_HMAC_KEY_misspelled_TYPE": "Service",
                "WATERCOOLER_HMAC_KEY_misspelled_SERVICE_IDENTITY": "svc",
                "WATERCOOLER_HMAC_KEY_misspelled_REPOS": "org/repo",
            }
            registry = load_default_registry(env)
        finally:
            hmac_logger.removeHandler(handler)
            hmac_logger.setLevel(prior_level)
        # Key was NOT registered (TYPE != "service" exactly).
        assert registry.lookup("misspelled") is None
        captured = buf.getvalue()
        assert "skipping key misspelled" in captured
        assert "'Service'" in captured

    def test_repos_per_entry_dropped_warning(self) -> None:
        # PR #703 round 7+2 LOW: when one CSV entry fails
        # canonicalisation (here: bare ``.git`` strips to "") but
        # others succeed, the entry is silently dropped from the
        # allow-list. Operators relying on it would see runtime
        # 403s with no startup indication. Emit a per-entry warning
        # naming the offender.
        import io
        import logging as _logging

        from watercooler_mcp.auth.hmac_keys import logger as hmac_logger

        buf = io.StringIO()
        handler = _logging.StreamHandler(buf)
        handler.setFormatter(_logging.Formatter("%(levelname)s %(message)s"))
        hmac_logger.addHandler(handler)
        prior_level = hmac_logger.level
        hmac_logger.setLevel(_logging.WARNING)
        try:
            env = {
                "WATERCOOLER_HMAC_KEY_svc_SECRET": "s",
                "WATERCOOLER_HMAC_KEY_svc_TYPE": "service",
                "WATERCOOLER_HMAC_KEY_svc_SERVICE_IDENTITY": "svc",
                "WATERCOOLER_HMAC_KEY_svc_REPOS": "org/repo, .git",
            }
            registry = load_default_registry(env)
        finally:
            hmac_logger.removeHandler(handler)
            hmac_logger.setLevel(prior_level)
        info = registry.lookup("svc")
        assert info is not None
        assert info.repo_allow_list == frozenset({"org/repo"})
        captured = buf.getvalue()
        assert "failed canonicalisation" in captured
        assert ".git" in captured

    def test_repos_dotgit_stripped_at_load_time(self) -> None:
        # The canonicaliser strips ``.git`` so registry entries
        # match ``X-Repo: org/repo`` regardless of how the operator
        # spelled it in the env var (e.g. copy-paste from a clone
        # URL). Pin that behaviour so a future change to the
        # canonicaliser (e.g. preserving ``.git``) is caught at the
        # registry layer too.
        env = {
            "WATERCOOLER_HMAC_KEY_svc_SECRET": "s",
            "WATERCOOLER_HMAC_KEY_svc_TYPE": "service",
            "WATERCOOLER_HMAC_KEY_svc_SERVICE_IDENTITY": "svc",
            "WATERCOOLER_HMAC_KEY_svc_REPOS": "Org/Repo.git",
        }
        registry = load_default_registry(env)
        info = registry.lookup("svc")
        assert info is not None
        assert info.repo_allow_list == frozenset({"org/repo"})

    def test_rejects_key_id_ending_in_reserved_suffix(self) -> None:
        # ``WATERCOOLER_HMAC_KEY_foo_TYPE_SECRET`` would yield
        # ``key_id="foo_TYPE"`` after stripping the SECRET suffix
        # — ambiguous with the metadata-suffix scheme. Reject.
        env = {
            "WATERCOOLER_HMAC_KEY_foo_TYPE_SECRET": "s",
            "WATERCOOLER_HMAC_KEY_foo_TYPE_TYPE": "service",
            "WATERCOOLER_HMAC_KEY_foo_TYPE_SERVICE_IDENTITY": "svc",
            "WATERCOOLER_HMAC_KEY_foo_TYPE_REPOS": "org/repo",
        }
        registry = load_default_registry(env)
        assert registry.lookup("foo_TYPE") is None

    def test_service_key_with_delegation_csv(self) -> None:
        env = {
            "WATERCOOLER_HMAC_KEY_svc_SECRET": "s",
            "WATERCOOLER_HMAC_KEY_svc_TYPE": "service",
            "WATERCOOLER_HMAC_KEY_svc_SERVICE_IDENTITY": "svc",
            "WATERCOOLER_HMAC_KEY_svc_DELEGATION": "alice, bob ,carol",
            "WATERCOOLER_HMAC_KEY_svc_REPOS": "org/repo",
        }
        registry = load_default_registry(env)
        info = registry.lookup("svc")
        assert info is not None
        assert info.delegation_allow_list == frozenset({"alice", "bob", "carol"})


# ------------------------------------------------------------------ #
# Startup fail-fast (H13)
# ------------------------------------------------------------------ #


class TestStartupFailFast:
    """H13: in multi-tenant enforce mode, the global secret must be absent."""

    def test_warn_mode_allows_global_secret(self) -> None:
        from watercooler_mcp.auth.hmac_keys import KeyRegistry

        assert (
            hmac_v3_startup_fail_fast_check(
                require_v3="warn",
                is_multi_tenant=True,
                has_global_secret=True,
                registry=KeyRegistry(),
            )
            is None
        )

    def test_unset_mode_allows_global_secret(self) -> None:
        from watercooler_mcp.auth.hmac_keys import KeyRegistry

        assert (
            hmac_v3_startup_fail_fast_check(
                require_v3="",
                is_multi_tenant=True,
                has_global_secret=True,
                registry=KeyRegistry(),
            )
            is None
        )

    def test_enforce_mode_single_tenant_allows_global(self) -> None:
        # Local single-tenant treats the global secret as a per-user
        # key — the multi-tenant invariant doesn't apply.
        from watercooler_mcp.auth.hmac_keys import KeyRegistry

        assert (
            hmac_v3_startup_fail_fast_check(
                require_v3="enforce",
                is_multi_tenant=False,
                has_global_secret=True,
                registry=KeyRegistry(),
            )
            is None
        )

    def test_enforce_mode_multi_tenant_with_global_fails(self) -> None:
        # H13: refuse to boot
        from watercooler_mcp.auth.hmac_keys import KeyRegistry

        msg = hmac_v3_startup_fail_fast_check(
            require_v3="enforce",
            is_multi_tenant=True,
            has_global_secret=True,
            registry=KeyRegistry(),
        )
        assert msg is not None
        assert "Refusing to start" in msg
        # Issue #733 generalised the message to drop the specific
        # env-var name; the invariant text describes the legacy
        # global-secret class instead.
        assert "legacy global HMAC secret" in msg

    def test_enforce_mode_multi_tenant_without_global_passes(self) -> None:
        from watercooler_mcp.auth.hmac_keys import KeyRegistry

        assert (
            hmac_v3_startup_fail_fast_check(
                require_v3="enforce",
                is_multi_tenant=True,
                has_global_secret=False,
                registry=KeyRegistry(),
            )
            is None
        )

    def test_registry_is_required_no_default(self) -> None:
        """PR #741 round 2 (MED #2): ``registry`` has no default — a
        future caller cannot omit it and silently bypass the H13
        wildcard-key scan."""
        with pytest.raises(TypeError, match="registry"):
            hmac_v3_startup_fail_fast_check(  # type: ignore[call-arg]
                require_v3="enforce",
                is_multi_tenant=True,
                has_global_secret=False,
            )

    def test_enforce_multi_tenant_with_static_wildcard_key_fails(self) -> None:
        """PR #741 review: refuse to boot if a statically-loaded per_user key
        has ``bound_user_id is None`` in multi-tenant enforce mode.

        Sister check to the runtime ``check_subject_binding`` rejection;
        this branch catches the issue at startup so an operator with a
        misconfigured registry never starts serving in the first place.
        """
        from watercooler_mcp.auth.hmac_keys import KeyRegistry

        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="bad_wildcard",
                secret=b"s",
                key_type="per_user",
                bound_user_id=None,
            )
        )
        msg = hmac_v3_startup_fail_fast_check(
            require_v3="enforce",
            is_multi_tenant=True,
            has_global_secret=False,
            registry=registry,
        )
        assert msg is not None
        assert "Refusing to start" in msg
        assert "bound_user_id=None" in msg
        assert "bad_wildcard" in msg

    def test_enforce_multi_tenant_static_wildcard_offender_count_truncated(
        self,
    ) -> None:
        """When >3 wildcard keys are loaded, the message lists three and
        notes the remainder count rather than dumping every key_id."""
        from watercooler_mcp.auth.hmac_keys import KeyRegistry

        registry = KeyRegistry()
        for i in range(5):
            registry.add(
                KeyInfo(
                    key_id=f"wild_{i:02d}",
                    secret=b"s",
                    key_type="per_user",
                    bound_user_id=None,
                )
            )
        msg = hmac_v3_startup_fail_fast_check(
            require_v3="enforce",
            is_multi_tenant=True,
            has_global_secret=False,
            registry=registry,
        )
        assert msg is not None
        # First three (sorted alphabetically) should appear.
        assert "wild_00" in msg
        assert "wild_01" in msg
        assert "wild_02" in msg
        # The remainder count summarises the rest.
        assert "(+ 2 more)" in msg

    def test_enforce_multi_tenant_with_clean_registry_passes(self) -> None:
        """A registry with only properly-bound per_user and service keys
        boots fine in multi-tenant enforce mode."""
        from watercooler_mcp.auth.hmac_keys import KeyRegistry

        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="alice_key",
                secret=b"s",
                key_type="per_user",
                bound_user_id="alice",
            )
        )
        registry.add(
            KeyInfo(
                key_id="dashboard",
                secret=b"s",
                key_type="service",
                service_identity="dashboard",
                delegation_allow_list=frozenset({"alice", "bob"}),
                repo_allow_list=frozenset({"org/repo"}),
            )
        )
        assert (
            hmac_v3_startup_fail_fast_check(
                require_v3="enforce",
                is_multi_tenant=True,
                has_global_secret=False,
                registry=registry,
            )
            is None
        )

    def test_enforce_single_tenant_with_static_wildcard_passes(self) -> None:
        """Single-tenant deployments retain wildcard semantics — startup
        registry scan is multi-tenant-only."""
        from watercooler_mcp.auth.hmac_keys import KeyRegistry

        registry = KeyRegistry()
        registry.add(
            KeyInfo(
                key_id="local_legacy",
                secret=b"s",
                key_type="per_user",
                bound_user_id=None,
            )
        )
        assert (
            hmac_v3_startup_fail_fast_check(
                require_v3="enforce",
                is_multi_tenant=False,
                has_global_secret=False,
                registry=registry,
            )
            is None
        )


# ------------------------------------------------------------------ #
# KeyInfo invariants
# ------------------------------------------------------------------ #


class TestKeyInfoInvariants:
    def test_empty_key_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="key_id required"):
            KeyInfo(key_id="", secret=b"s", key_type="per_user")

    def test_str_secret_rejected(self) -> None:
        with pytest.raises(TypeError, match="bytes"):
            KeyInfo(
                key_id="k",
                secret="not-bytes",  # type: ignore[arg-type]
                key_type="per_user",
            )

    def test_bad_key_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="per_user|service"):
            KeyInfo(
                key_id="k",
                secret=b"s",
                key_type="invalid",  # type: ignore[arg-type]
            )

    def test_service_without_identity_rejected(self) -> None:
        with pytest.raises(ValueError, match="service_identity"):
            KeyInfo(key_id="svc", secret=b"s", key_type="service")

    def test_immutable(self) -> None:
        key = KeyInfo(key_id="k", secret=b"s", key_type="per_user", bound_user_id="u")
        with pytest.raises(AttributeError):
            key.bound_user_id = "evil"  # type: ignore[misc]
