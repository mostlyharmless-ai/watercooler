"""Unit tests for ``watercooler_mcp.secrets.gateway``.

Round-trip every standard serializer to verify the Secret value
never escapes via:
- str() / repr() / format() / f-string
- json.dumps default encoder (must fail loudly)
- json.dumps with SecretJSONEncoder (must redact, not reveal)
- pickle round-trip
- logging formatter
- bytes() coercion

Plus invariant checks for redact_value / redact_object pattern
coverage.
"""

from __future__ import annotations

import io
import json
import logging
import pickle

import pytest

from watercooler_mcp.secrets.gateway import (
    SECRET_PATTERN,
    Secret,
    SecretJSONEncoder,
    redact_object,
    redact_value,
)


# ------------------------------------------------------------------ #
# Construction invariants
# ------------------------------------------------------------------ #


class TestSecretConstruction:
    def test_basic_construction(self) -> None:
        s = Secret("ghp_abc", label="github_pat")
        assert s.label == "github_pat"
        assert s.reveal() == "ghp_abc"

    def test_default_label(self) -> None:
        s = Secret("value")
        assert s.label == "secret"

    def test_non_str_value_rejected(self) -> None:
        with pytest.raises(TypeError, match="Secret expects str"):
            Secret(123)  # type: ignore[arg-type]

    def test_empty_label_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty str"):
            Secret("v", label="")

    def test_immutable(self) -> None:
        s = Secret("v", label="l")
        with pytest.raises(AttributeError, match="immutable"):
            s._value = "evil"  # type: ignore[misc]
        with pytest.raises(AttributeError, match="immutable"):
            del s._value  # type: ignore[misc]


class TestDirectAttributeAccessBlocked:
    """The HIGH-1 finding from PR #704 review.

    Previously ``secret._value`` returned the raw value because
    ``__slots__`` declared ``_value`` and there was no
    ``__getattribute__`` override. The grep audit for ``.reveal()``
    was therefore incomplete — anyone who knew the slot name could
    bypass it. After the fix, direct slot access raises
    ``AttributeError``.
    """

    def test_underscore_value_attr_blocked(self) -> None:
        s = Secret("ghp_real_value", label="pat")
        with pytest.raises(AttributeError, match="opaque"):
            _ = s._value  # type: ignore[attr-defined]

    def test_value_attr_blocked(self) -> None:
        s = Secret("ghp_real_value", label="pat")
        with pytest.raises(AttributeError, match="opaque"):
            _ = s.value  # type: ignore[attr-defined]

    def test_mangled_slot_name_blocked(self) -> None:
        # The slot is named ``_Secret__value`` to discourage naive
        # access via the unmangled form. The override blocks the
        # mangled name too so the audit is complete.
        s = Secret("ghp_real_value", label="pat")
        with pytest.raises(AttributeError, match="opaque"):
            _ = s._Secret__value  # type: ignore[attr-defined]

    def test_label_remains_accessible(self) -> None:
        # The label is intentionally non-secret — it's part of the
        # redacted placeholder. Accessing it via the property
        # remains allowed.
        s = Secret("ghp_real_value", label="pat")
        assert s.label == "pat"

    def test_reveal_still_works(self) -> None:
        # The internal accessor must continue to work — it uses
        # object.__getattribute__ to bypass the guard.
        s = Secret("ghp_real_value", label="pat")
        assert s.reveal() == "ghp_real_value"

    def test_object_getattribute_bypass_is_documented(self) -> None:
        """PR #704 round 3 MED finding: the wrapper does NOT seal
        the value against reflection-level access. Honest audit
        scope (per the module docstring): ``.reveal()`` is the
        *primary* greppable escape; ``object.__getattribute__`` is
        a separate audit concern that grep -rn
        'object.__getattribute__' catches.

        This test pins the bypass as KNOWN and intentionally
        UNBLOCKED. If a future refactor accidentally seals this
        path, the test fails — forcing the audit-scope claim in
        the docs to be revisited rather than silently changed.
        """
        s = Secret("ghp_real_value", label="pat")
        # This is the documented bypass — succeeds by design.
        raw = object.__getattribute__(s, "_Secret__value")
        assert raw == "ghp_real_value"


class TestPackageReexports:
    """PR #704 round 3 LOW finding: ``SECRET_PATTERN`` was in
    ``gateway.__all__`` but missing from ``secrets/__init__.py``,
    so ``from watercooler_mcp.secrets import SECRET_PATTERN``
    raised ImportError. The fix re-exports it via the package
    init.
    """

    def test_secret_pattern_importable_from_package_root(self) -> None:
        from watercooler_mcp.secrets import SECRET_PATTERN as pattern_at_pkg
        from watercooler_mcp.secrets.gateway import (
            SECRET_PATTERN as pattern_at_module,
        )

        assert pattern_at_pkg is pattern_at_module

    def test_load_github_token_secret_importable_from_package_root(
        self,
    ) -> None:
        # Same shape: the pilot loader is exported at both levels.
        from watercooler_mcp.secrets import load_github_token_secret as a
        from watercooler_mcp.secrets.gateway import load_github_token_secret as b

        assert a is b

    def test_load_slack_workspace_token_secret_importable_from_package_root(
        self,
    ) -> None:
        # Same shape: the Move 4 expansion loader is exported at both levels.
        from watercooler_mcp.secrets import (
            load_slack_workspace_token_secret as a,
        )
        from watercooler_mcp.secrets.gateway import (
            load_slack_workspace_token_secret as b,
        )

        assert a is b


class TestLoadSlackWorkspaceTokenSecret:
    """Move 4 expansion loader for Slack workspace bot tokens.

    Wraps ``slack.token_service.get_workspace_token`` so the returned
    value is a ``Secret`` rather than a raw ``str``. The legacy raw-
    string path is unchanged; this loader is opt-in for callers that
    want the wrapper's leak-resistance guarantees.
    """

    def test_returns_secret_when_token_service_returns_value(
        self, monkeypatch
    ) -> None:
        from watercooler_mcp.secrets.gateway import (
            Secret,
            load_slack_workspace_token_secret,
        )

        captured: dict = {}

        def _fake_get_workspace_token(workspace_id: str, *, use_cache: bool = True):
            captured["workspace_id"] = workspace_id
            captured["use_cache"] = use_cache
            return "xoxb-fake-bot-token-1234567890"

        monkeypatch.setattr(
            "watercooler_mcp.slack.token_service.get_workspace_token",
            _fake_get_workspace_token,
        )

        result = load_slack_workspace_token_secret("T12345ABC")
        assert isinstance(result, Secret)
        assert result.reveal() == "xoxb-fake-bot-token-1234567890"
        # Label distinguishes from github_pat in mixed-context logs.
        assert str(result) == "[REDACTED:slack_bot_token]"
        # Forwarded args verbatim.
        assert captured == {"workspace_id": "T12345ABC", "use_cache": True}

    def test_returns_none_when_token_service_returns_none(
        self, monkeypatch
    ) -> None:
        from watercooler_mcp.secrets.gateway import load_slack_workspace_token_secret

        monkeypatch.setattr(
            "watercooler_mcp.slack.token_service.get_workspace_token",
            lambda workspace_id, *, use_cache=True: None,
        )

        assert load_slack_workspace_token_secret("T-not-found") is None

    def test_returns_none_when_token_service_returns_empty_string(
        self, monkeypatch
    ) -> None:
        # Defensive: empty string from upstream should not produce a
        # Secret wrapping an empty value (which would be a useless
        # signing/auth key and a potential trap).
        from watercooler_mcp.secrets.gateway import load_slack_workspace_token_secret

        monkeypatch.setattr(
            "watercooler_mcp.slack.token_service.get_workspace_token",
            lambda workspace_id, *, use_cache=True: "",
        )

        assert load_slack_workspace_token_secret("T-empty") is None

    def test_use_cache_false_is_forwarded(self, monkeypatch) -> None:
        from watercooler_mcp.secrets.gateway import load_slack_workspace_token_secret

        captured: dict = {}

        def _fake(workspace_id: str, *, use_cache: bool = True):
            captured["use_cache"] = use_cache
            return "xoxb-some-token"

        monkeypatch.setattr(
            "watercooler_mcp.slack.token_service.get_workspace_token",
            _fake,
        )

        load_slack_workspace_token_secret("T1", use_cache=False)
        assert captured["use_cache"] is False


class TestCopyAndDeepcopyBlocked:
    """The HIGH-2 finding from PR #704 review.

    ``copy.copy`` and ``copy.deepcopy`` previously round-tripped
    the underlying value via the slot-copy protocol. The fix raises
    ``TypeError`` from ``__copy__`` / ``__deepcopy__`` to force
    callers who really need a fresh wrapper to construct one
    explicitly via ``Secret(other.reveal(), label=other.label)``,
    so the ``.reveal()`` audit captures the access.
    """

    def test_copy_copy_raises(self) -> None:
        import copy

        s = Secret("ghp_real_value", label="pat")
        with pytest.raises(TypeError, match="cannot be copied"):
            copy.copy(s)

    def test_copy_deepcopy_raises(self) -> None:
        import copy

        s = Secret("ghp_real_value", label="pat")
        with pytest.raises(TypeError, match="cannot be deep-copied"):
            copy.deepcopy(s)

    def test_deepcopy_through_dict_raises(self) -> None:
        # A nested deepcopy traversal hits __deepcopy__ for each
        # Secret leaf.
        import copy

        payload = {"creds": {"token": Secret("ghp_real_value", label="pat")}}
        with pytest.raises(TypeError, match="cannot be deep-copied"):
            copy.deepcopy(payload)

    def test_explicit_reconstruct_works(self) -> None:
        # The supported pattern: explicitly construct a new Secret
        # via reveal(). This is greppable.
        original = Secret("ghp_real_value", label="pat")
        clone = Secret(original.reveal(), label=original.label)
        assert clone.reveal() == original.reveal()
        assert clone.label == original.label
        # Both wrap the same value — equality holds.
        assert clone == original


# ------------------------------------------------------------------ #
# Stringification — never leak
# ------------------------------------------------------------------ #


class TestStringificationNeverLeaks:
    SECRET_VALUE = "ghp_super_secret_token_value"

    def _secret(self) -> Secret:
        return Secret(self.SECRET_VALUE, label="github_pat")

    def test_str_returns_placeholder(self) -> None:
        assert str(self._secret()) == "[REDACTED:github_pat]"

    def test_repr_returns_label_only(self) -> None:
        r = repr(self._secret())
        assert "github_pat" in r
        assert self.SECRET_VALUE not in r

    def test_format_empty_spec_returns_placeholder(self) -> None:
        s = self._secret()
        assert format(s) == "[REDACTED:github_pat]"
        assert format(s, "") == "[REDACTED:github_pat]"

    def test_format_non_empty_spec_raises(self) -> None:
        # PR #704 round 4 LOW: a non-empty format_spec implies
        # the caller is treating the Secret as a plain string and
        # would silently drop the spec. Better to fail loudly so
        # the caller notices and explicitly handles the Secret.
        s = self._secret()
        with pytest.raises(TypeError, match="format_spec"):
            format(s, ">20")
        with pytest.raises(TypeError, match="format_spec"):
            format(s, ".10s")
        # f-strings with format_spec also raise.
        with pytest.raises(TypeError, match="format_spec"):
            _ = f"token={s:>20}"

    def test_f_string_uses_format(self) -> None:
        s = self._secret()
        formatted = f"token={s}"
        assert "[REDACTED:github_pat]" in formatted
        assert self.SECRET_VALUE not in formatted

    def test_str_format_method(self) -> None:
        s = self._secret()
        formatted = "token={}".format(s)
        assert "[REDACTED:github_pat]" in formatted
        assert self.SECRET_VALUE not in formatted

    def test_percent_format(self) -> None:
        s = self._secret()
        formatted = "token=%s" % (s,)
        assert "[REDACTED:github_pat]" in formatted
        assert self.SECRET_VALUE not in formatted

    def test_bytes_coercion_redacts(self) -> None:
        s = self._secret()
        b = bytes(s)
        assert b"[REDACTED:github_pat]" in b
        assert self.SECRET_VALUE.encode("utf-8") not in b


# ------------------------------------------------------------------ #
# JSON serialization
# ------------------------------------------------------------------ #


class TestJSONSerialization:
    SECRET_VALUE = "ghp_super_secret_token_value"

    def _secret(self) -> Secret:
        return Secret(self.SECRET_VALUE, label="github_pat")

    def test_default_json_encoder_fails_fast(self) -> None:
        # A naive ``json.dumps(secret)`` MUST raise. This is the
        # whole point of the non-str design — fail loudly so we
        # notice unintended serialization.
        with pytest.raises(TypeError, match="not JSON serializable"):
            json.dumps(self._secret())

    def test_default_json_encoder_fails_fast_in_dict(self) -> None:
        # Even nested in a dict, the default encoder must fail.
        payload = {"token": self._secret()}
        with pytest.raises(TypeError):
            json.dumps(payload)

    def test_secret_json_encoder_redacts(self) -> None:
        encoded = json.dumps({"token": self._secret()}, cls=SecretJSONEncoder)
        assert "[REDACTED:github_pat]" in encoded
        assert self.SECRET_VALUE not in encoded

    def test_secret_json_encoder_redacts_nested(self) -> None:
        payload = {
            "user": "alice",
            "auth": {"token": self._secret(), "kind": "bearer"},
        }
        encoded = json.dumps(payload, cls=SecretJSONEncoder)
        assert self.SECRET_VALUE not in encoded
        loaded = json.loads(encoded)
        assert loaded["auth"]["token"] == "[REDACTED:github_pat]"


# ------------------------------------------------------------------ #
# Pickle round-trip
# ------------------------------------------------------------------ #


class TestPickleRoundTrip:
    def test_pickle_does_not_round_trip_value(self) -> None:
        # ``pickle.dumps(secret)`` followed by ``pickle.loads`` MUST
        # NOT yield a Secret carrying the original value. The
        # __reduce__ override ensures we get a redacted placeholder
        # string instead.
        original = Secret("ghp_real_value", label="pat")
        rehydrated = pickle.loads(pickle.dumps(original))
        assert rehydrated == "[REDACTED:pat]"
        assert rehydrated != "ghp_real_value"

    def test_pickle_blob_does_not_contain_value(self) -> None:
        original = Secret("ghp_super_secret", label="pat")
        blob = pickle.dumps(original)
        assert b"ghp_super_secret" not in blob


# ------------------------------------------------------------------ #
# Logging formatter
# ------------------------------------------------------------------ #


class TestLoggingFormatter:
    def _make_logger(self) -> tuple[logging.Logger, io.StringIO]:
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        # Unique name per call so the handler-list reset is local.
        unique = f"test_secret_logging.{id(buf)}"
        logger = logging.getLogger(unique)
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        return logger, buf

    def test_logger_does_not_leak_direct_secret(self) -> None:
        s = Secret("ghp_secret_token", label="pat")
        logger, buf = self._make_logger()
        logger.info("loaded token: %s", s)
        output = buf.getvalue()
        assert "ghp_secret_token" not in output
        # Direct ``%s`` formatting of a Secret goes through
        # ``__str__`` and produces the placeholder.
        assert "[REDACTED:pat]" in output

    def test_logger_does_not_leak_secret_in_dict(self) -> None:
        # PR #704 round 6 LOW: the previous combined test asserted
        # ``[REDACTED:pat] in output`` once across both log lines.
        # The placeholder substring was present from the
        # direct-secret line, so the dict-case assertion was
        # vacuously satisfied. A regression in dict-formatting
        # could not have failed the assertion. Splitting the
        # cases into independent tests means each path validates
        # itself.
        #
        # Note: ``%s`` of a dict uses ``repr()`` on the dict, which
        # uses ``repr()`` on each value. Secret.__repr__ returns
        # ``Secret('pat', ...)`` — NOT the placeholder. So the
        # security-relevant assertion is "the raw value does not
        # appear", and the diagnostic is "the label appears via
        # repr". Both can be checked.
        s = Secret("ghp_secret_token", label="pat")
        logger, buf = self._make_logger()
        logger.info("token in dict: %s", {"k": s})
        output = buf.getvalue()
        # Security invariant: the underlying value never appears.
        assert "ghp_secret_token" not in output
        # Diagnostic invariant: the label is visible (via repr).
        assert "pat" in output


# ------------------------------------------------------------------ #
# Equality / hashing
# ------------------------------------------------------------------ #


class TestEqualityAndHashing:
    def test_eq_constant_time(self) -> None:
        a = Secret("alpha", label="x")
        b = Secret("alpha", label="x")
        c = Secret("beta", label="x")
        assert a == b
        assert a != c

    def test_eq_with_string_returns_notimplemented(self) -> None:
        # NotImplemented surfaces as False under == because Python
        # consults the other side and str does not return NotImplemented.
        s = Secret("alpha", label="x")
        assert (s == "alpha") is False
        assert (s == "anything") is False

    def test_hash_agrees_with_eq(self) -> None:
        # PR #704 round 4 MED: hash and eq agree. Equality
        # checks BOTH label and value, hash digests both.
        a = Secret("alpha", label="x")
        b = Secret("alpha", label="x")
        c = Secret("beta", label="x")
        assert hash(a) == hash(b)
        assert hash(a) != hash(c)

    def test_eq_distinguishes_by_label(self) -> None:
        # PR #704 round 4 MED: same value, DIFFERENT labels →
        # NOT equal. A cache keyed on Secret cannot conflate
        # ``github_pat`` and ``slack_token`` instances that
        # happen to share an underlying string.
        a = Secret("alpha", label="github_pat")
        b = Secret("alpha", label="slack_token")
        assert a != b
        # And hash distinguishes too.
        assert hash(a) != hash(b)

    def test_dict_invariant_holds(self) -> None:
        # Python's dict invariant: a == b ⇒ hash(a) == hash(b).
        # Different value → not equal → free to differ in hash.
        a = Secret("alpha", label="x")
        b = Secret("beta", label="x")
        assert a != b
        # Different label same value → not equal (round 4 fix).
        c = Secret("alpha", label="y")
        assert a != c
        # Same label same value → equal AND same hash.
        d = Secret("alpha", label="x")
        assert a == d
        assert hash(a) == hash(d)

    def test_hash_in_set_dedupes_by_value(self) -> None:
        a = Secret("alpha", label="x")
        b = Secret("alpha", label="x")
        s = {a, b}
        # Same value → equal → set has one.
        assert len(s) == 1
        # Different values → set has two even with same label.
        s2 = {Secret("a", label="x"), Secret("b", label="x")}
        assert len(s2) == 2


# ------------------------------------------------------------------ #
# bool — __len__ removed (token-length side-channel)
# ------------------------------------------------------------------ #


class TestBool:
    def test_truthy_non_empty(self) -> None:
        assert bool(Secret("value")) is True

    def test_falsy_empty_value(self) -> None:
        assert bool(Secret("")) is False

    def test_len_not_supported(self) -> None:
        # PR #704 round 2 LOW finding: token lengths are
        # class-discriminating (GitHub PATs are 40 chars, OpenAI
        # sk- keys are 51, fine-grained PATs are 93). Exposing
        # length lets callers infer the token type without going
        # through .reveal(), bypassing the egress audit. Use
        # bool(secret) for the "did I load a token at all?"
        # check.
        s = Secret("ghp_abc", label="pat")
        with pytest.raises(TypeError):
            len(s)


# ------------------------------------------------------------------ #
# redact_value pattern coverage
# ------------------------------------------------------------------ #


class TestRedactValue:
    @pytest.mark.parametrize(
        "raw,prefix",
        [
            ("ghp_abcdefghijklmnopqrstu", "ghp"),
            ("gho_abcdefghijklmnopqrstu", "gho"),
            ("ghu_abcdefghijklmnopqrstu", "ghu"),
            ("ghs_abcdefghijklmnopqrstu", "ghs"),
            ("ghr_abcdefghijklmnopqrstu", "ghr"),
            ("sk-abcdefghijklmnopqrstu1", "sk"),
            ("wc_abcdefghijklmnopqrstu", "wc"),
        ],
    )
    def test_known_prefixes_redacted(self, raw: str, prefix: str) -> None:
        out = redact_value(raw)
        assert raw not in out
        assert f"[REDACTED:{prefix}_*]" in out

    def test_sk_proj_redacted(self) -> None:
        # The MED finding from PR #704 review: ``sk-proj-`` was
        # previously excluded with an incoherent rationale, leaving
        # real OpenAI project-scoped keys unmasked. Now ``sk-proj-``
        # is redacted with its own label.
        raw = "sk-proj-abcdefghijklmnopqrstuvwxyz"
        out = redact_value(raw)
        assert raw not in out
        assert "[REDACTED:sk-proj_*]" in out

    def test_sk_proj_with_dashes_in_tail_redacted(self) -> None:
        # OpenAI sk-proj- keys can contain ``-`` in the tail. The
        # trailing character class includes ``-`` so the full key
        # is redacted as one unit.
        raw = "sk-proj-AbCd-1234-EfGh-5678-IjKl-9012"
        out = redact_value(raw)
        assert raw not in out
        assert "[REDACTED:sk-proj_*]" in out

    @pytest.mark.parametrize(
        "raw,prefix",
        [
            # Slack bot tokens — the most common leak shape, paired
            # with the new ``load_slack_workspace_token_secret`` loader.
            ("xoxb-1234567890123-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "xoxb"),
            # Slack workspace, admin, and user-scoped variants.
            ("xoxs-1234567890123-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "xoxs"),
            ("xoxa-1234567890123-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "xoxa"),
            ("xoxp-1234567890123-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx", "xoxp"),
        ],
    )
    def test_slack_prefixes_redacted(self, raw: str, prefix: str) -> None:
        # Move 4 expansion (PR #724 round 1 MED): the SECRET_PATTERN
        # gained ``xox[bsap]-`` so a raw Slack token escaping the
        # legacy code path (e.g. via an error message logged before
        # reaching the Secret wrapper) is redacted by the
        # pattern-based fallback in ``redact_value`` /
        # ``redact_object``. Without this, the wrapper and the
        # pattern would have inconsistent coverage.
        out = redact_value(raw)
        assert raw not in out
        assert f"[REDACTED:{prefix}_*]" in out

    def test_slack_token_in_log_line_redacted(self) -> None:
        # End-to-end shape: a log line that captured an upstream
        # error containing a raw Slack bot token must come back with
        # the token redacted. Verifies the pattern fires inside a
        # surrounding string.
        line = (
            "ERROR: Slack API call failed with token "
            "xoxb-1234567890123-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx "
            "for workspace T12345ABC"
        )
        out = redact_value(line)
        assert "xoxb-" not in out.replace("[REDACTED:xoxb_*]", "")
        assert "T12345ABC" in out  # context preserved

    def test_short_match_not_redacted(self) -> None:
        # 20+ chars after the prefix is required.
        raw = "ghp_short"
        assert redact_value(raw) == raw

    def test_redaction_in_log_line(self) -> None:
        line = (
            "ERROR: GitHub API call failed with token "
            "ghp_aaaaaaaaaaaaaaaaaaaaaaa for user alice"
        )
        out = redact_value(line)
        assert "ghp_" not in out.replace("[REDACTED:ghp_*]", "")
        assert "alice" in out  # context preserved

    def test_empty_input_unchanged(self) -> None:
        assert redact_value("") == ""

    def test_non_string_passthrough(self) -> None:
        # The MED finding from PR #704 review: signature is
        # documented as ``Any -> Any``. Non-string inputs pass
        # through unchanged because they cannot contain
        # secret-shaped *strings* by definition.
        assert redact_value(None) is None
        assert redact_value(42) == 42
        assert redact_value(["a", "b"]) == ["a", "b"]


# ------------------------------------------------------------------ #
# redact_object recursive
# ------------------------------------------------------------------ #


class TestRedactObject:
    def test_redacts_secret_in_dict(self) -> None:
        secret = Secret("ghp_value", label="pat")
        out = redact_object({"token": secret})
        assert out == {"token": "[REDACTED:pat]"}

    def test_redacts_pattern_in_string_leaf(self) -> None:
        out = redact_object({"log": "tried token ghp_aaaaaaaaaaaaaaaaaaaaaaa here"})
        assert "[REDACTED:ghp_*]" in out["log"]

    def test_recurses_into_nested(self) -> None:
        secret = Secret("v", label="x")
        out = redact_object(
            {"user": "alice", "auth": {"token": secret, "kind": "bearer"}}
        )
        assert out["auth"]["token"] == "[REDACTED:x]"

    def test_preserves_list_type(self) -> None:
        out = redact_object(["alice", Secret("v")])
        assert isinstance(out, list)
        assert out == ["alice", "[REDACTED:secret]"]

    def test_preserves_tuple_type(self) -> None:
        out = redact_object(("alice", Secret("v")))
        assert isinstance(out, tuple)
        assert out == ("alice", "[REDACTED:secret]")

    def test_passes_through_non_string_leaves(self) -> None:
        out = redact_object({"count": 42, "ratio": 0.5, "active": True})
        assert out == {"count": 42, "ratio": 0.5, "active": True}

    def test_does_not_mutate_input(self) -> None:
        secret = Secret("v", label="x")
        original = {"token": secret}
        redact_object(original)
        # Original still has the Secret reference (not a string).
        assert original["token"] is secret

    def test_redacts_set_branch(self) -> None:
        # PR #704 round 2 MED finding: the previous implementation
        # had no ``set`` branch, so a set containing Secret
        # instances passed through unredacted.
        secret = Secret("ghp_value", label="pat")
        out = redact_object({secret, "alice"})
        assert isinstance(out, set)
        # Both leaves redacted.
        assert "[REDACTED:pat]" in out
        assert "alice" in out

    def test_redacts_frozenset_branch(self) -> None:
        secret = Secret("ghp_value", label="pat")
        out = redact_object(frozenset({secret, "alice"}))
        assert isinstance(out, frozenset)
        assert "[REDACTED:pat]" in out

    def test_redacts_set_with_pattern_leaves(self) -> None:
        out = redact_object({"clean", "ghp_aaaaaaaaaaaaaaaaaaaaaaa"})
        assert isinstance(out, set)
        # Pattern-redacted leaf appears once; the clean leaf is
        # untouched.
        assert "clean" in out
        # The redacted leaf has the [REDACTED:ghp_*] form.
        assert any("[REDACTED:ghp_*]" in v for v in out)

    def test_redacts_secret_shaped_dict_keys(self) -> None:
        # PR #704 round 4 MED: dict keys can carry secret-shaped
        # strings. The previous implementation only redacted
        # values, leaving the key in clear in the output.
        out = redact_object({"ghp_aaaaaaaaaaaaaaaaaaaaaaa": "metadata"})
        # Original key gone; redacted form present.
        assert "ghp_aaaaaaaaaaaaaaaaaaaaaaa" not in out
        assert any("[REDACTED:ghp_*]" in k for k in out.keys())

    def test_redacts_secret_object_dict_keys(self) -> None:
        # A Secret instance used as a dict key (uncommon but
        # possible since Secret is hashable) is also redacted.
        secret_key = Secret("ghp_value", label="pat")
        out = redact_object({secret_key: "metadata"})
        assert "[REDACTED:pat]" in out
        assert out["[REDACTED:pat]"] == "metadata"

    def test_dict_key_collision_preserves_entry_count(self) -> None:
        # PR #704 round 5 MED: two distinct keys that redact to
        # the same placeholder used to silently overwrite under
        # the naive dict comprehension. The fix appends an index
        # suffix (``[REDACTED:ghp_*]#2``) so the output dict has
        # the same len() as the input.
        original = {
            "ghp_aaaaaaaaaaaaaaaaaaaaaaa": "alice-token",
            "ghp_bbbbbbbbbbbbbbbbbbbbbbb": "bob-token",
            "ghp_ccccccccccccccccccccccc": "carol-token",
        }
        out = redact_object(original)
        # Same entry count: no silent drop.
        assert len(out) == len(original) == 3
        # No raw secret leaks.
        for k in original:
            assert k not in out
        # First collision gets the bare placeholder; later ones
        # get indexed suffixes.
        assert "[REDACTED:ghp_*]" in out
        suffix_keys = [k for k in out if isinstance(k, str) and "#" in k]
        assert any(k.endswith("#2") for k in suffix_keys)
        assert any(k.endswith("#3") for k in suffix_keys)
        # Every original value still appears somewhere.
        assert set(out.values()) == set(original.values())

    def test_dict_collision_preserves_all_secret_object_entries(self) -> None:
        # Same invariant for Secret-keyed dicts.
        a = Secret("ghp_a", label="pat")
        b = Secret("ghp_b", label="pat")
        out = redact_object({a: "meta-a", b: "meta-b"})
        assert len(out) == 2
        assert set(out.values()) == {"meta-a", "meta-b"}

    def test_set_collision_preserves_entry_count(self) -> None:
        # PR #704 round 6 MED: two distinct Secrets with the same
        # label but different values are legally unequal under
        # ``Secret.__eq__`` (which compares (label, value)), so
        # they can co-exist in an input set. Both reduce to the
        # same placeholder string after redact_object — the naive
        # set comprehension dedups them and the audit silently
        # under-counts. The fix indexes collisions the same way
        # the dict branch does.
        a = Secret("ghp_alice", label="pat")
        b = Secret("ghp_bob", label="pat")
        c = Secret("ghp_carol", label="pat")
        out = redact_object({a, b, c})
        # Same len() as input — no silent drop.
        assert len(out) == 3
        # First gets the bare placeholder; later ones get
        # indexed.
        assert "[REDACTED:pat]" in out
        assert any(isinstance(v, str) and v.endswith("#2") for v in out)
        assert any(isinstance(v, str) and v.endswith("#3") for v in out)

    def test_frozenset_collision_preserves_entry_count(self) -> None:
        a = Secret("ghp_alice", label="pat")
        b = Secret("ghp_bob", label="pat")
        out = redact_object(frozenset({a, b}))
        assert isinstance(out, frozenset)
        assert len(out) == 2
        assert "[REDACTED:pat]" in out


# ------------------------------------------------------------------ #
# Module-level pattern compile
# ------------------------------------------------------------------ #


class TestSecretPatternCompile:
    def test_pattern_is_compiled(self) -> None:
        # Sanity: SECRET_PATTERN is exposed for callers that want to
        # build their own redaction layer on top of the same regex.
        assert SECRET_PATTERN.pattern.startswith("(ghp_")
