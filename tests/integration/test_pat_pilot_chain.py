"""Integration test for the GitHub PAT Secret-wrapper pilot (Move 4).

Validates the load → wrap → emit chain end-to-end:

1. ``credentials.py`` reads a PAT from env (mocked).
2. ``load_github_token_secret`` wraps it as ``Secret(label="github_pat")``.
3. The Secret is passed through every plausible serialization path
   (str, repr, format, JSON-default, JSON-encoder, pickle, log) and
   verified to never leak the raw token.
4. The Secret is "sent as a bearer header" via ``.reveal()`` — the
   single greppable escape — and the token is verified to match the
   loaded value bit-for-bit.

The pilot proves the pattern; Sprint 3 expansion migrates other
secret types (llama-server, Slack, LLM providers).
"""

from __future__ import annotations

import io
import json
import logging
import pickle

import pytest

from watercooler_mcp.secrets.gateway import load_github_token_secret
from watercooler_mcp.secrets.gateway import (
    Secret,
    SecretJSONEncoder,
    redact_object,
)


_PILOT_PAT = "ghp_pilot_test_token_aaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def github_token_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """Inject a known PAT via the GITHUB_TOKEN env var."""
    monkeypatch.setenv("GITHUB_TOKEN", _PILOT_PAT)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return _PILOT_PAT


# ------------------------------------------------------------------ #
# Stage 1: load → wrap
# ------------------------------------------------------------------ #


class TestLoadAndWrap:
    def test_returns_secret_when_env_set(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        assert s is not None
        assert isinstance(s, Secret)
        assert s.label == "github_pat"

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # PR #704 round 6 LOW: previous assertion
        # ``result is None or isinstance(result, Secret)`` was a
        # tautology — passes for every legal return value.
        # Replaced with a stub that forces ``get_github_token`` to
        # return None (no env, no credentials file) so the no-token
        # path produces a deterministic ``None`` we can assert.
        #
        # PR #704 round 7+2 LOW: the patch target
        # ``"watercooler.credentials.get_github_token"`` works ONLY
        # because ``load_github_token_secret`` does a late
        # ``from watercooler.credentials import get_github_token``
        # inside the function body — the local binding is re-fetched
        # from ``sys.modules["watercooler.credentials"]`` on each call,
        # so patching the module attribute is observed. If the
        # function is ever refactored to use a top-of-file
        # ``from … import``, the local name will bind once at import
        # time and the monkeypatch will silently stop working. The
        # test would still pass vacuously because env + credentials
        # are also unset in this fixture. If you make that
        # refactor, switch this patch to
        # ``"watercooler_mcp.secrets.gateway.get_github_token"``
        # (or whatever the new top-level binding is).

        def _stub_no_token() -> None:
            return None

        monkeypatch.setattr("watercooler.credentials.get_github_token", _stub_no_token)
        result = load_github_token_secret()
        assert result is None


# ------------------------------------------------------------------ #
# Stage 2: every serialization path is leak-free
# ------------------------------------------------------------------ #


class TestEverySerializationPathLeaksFree:
    def test_str_redacts(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        assert s is not None
        rendered = str(s)
        assert _PILOT_PAT not in rendered
        assert "github_pat" in rendered

    def test_repr_redacts(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        assert _PILOT_PAT not in repr(s)

    def test_f_string_redacts(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        assert _PILOT_PAT not in f"token: {s}"

    def test_default_json_encoder_fails_loudly(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        # Naive json.dumps must fail rather than leak.
        with pytest.raises(TypeError):
            json.dumps({"token": s})

    def test_json_with_secret_encoder_redacts(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        encoded = json.dumps({"token": s}, cls=SecretJSONEncoder)
        assert _PILOT_PAT not in encoded
        assert "github_pat" in encoded

    def test_redact_object_handles_nested_payload(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        payload = {
            "user": "alice",
            "auth": {
                "scheme": "bearer",
                "token": s,
            },
            "log_line": ("previous request used ghp_aaaaaaaaaaaaaaaaaaaaaaa"),
        }
        redacted = redact_object(payload)
        flattened = json.dumps(redacted)
        assert _PILOT_PAT not in flattened
        # Diagnostic context preserved.
        assert "alice" in flattened
        assert "bearer" in flattened

    def test_pickle_round_trip_redacts(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        rehydrated = pickle.loads(pickle.dumps(s))
        # Round-tripped value is the placeholder string, NOT a Secret
        # carrying the raw value.
        assert rehydrated == "[REDACTED:github_pat]"
        assert rehydrated != _PILOT_PAT

    def test_logger_redacts(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("test_pat_pilot")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        logger.info("loaded github pat: %s", s)
        logger.info("round-tripped: %s", {"k": s, "user": "alice"})

        out = buf.getvalue()
        assert _PILOT_PAT not in out
        assert "github_pat" in out
        assert "alice" in out  # context preserved


# ------------------------------------------------------------------ #
# Stage 3: reveal is the ONLY path to the underlying value
# ------------------------------------------------------------------ #


class TestRevealIsTheOnlyEscape:
    def test_reveal_returns_raw_value(self, github_token_env: str) -> None:
        s = load_github_token_secret()
        assert s is not None
        assert s.reveal() == _PILOT_PAT

    def test_reveal_value_round_trips_for_http_use(self, github_token_env: str) -> None:
        # Simulated bearer-header construction. The test verifies the
        # *recipient* of `Authorization: Bearer <raw>` sees the
        # original token bits — i.e. wrapping does not corrupt the
        # value, only contains it.
        s = load_github_token_secret()
        assert s is not None
        bearer_header = f"Bearer {s.reveal()}"
        # Strip prefix; everything after must equal the loaded value.
        assert bearer_header[len("Bearer ") :] == _PILOT_PAT

    def test_no_other_str_method_returns_value(self, github_token_env: str) -> None:
        # Belt-and-suspenders: enumerate every dunder that returns a
        # string and verify none surface the value.
        s = load_github_token_secret()
        assert s is not None
        for renderer in (str, repr, lambda x: format(x), lambda x: f"{x}"):
            rendered = renderer(s)
            assert _PILOT_PAT not in rendered, f"leaked via {renderer!r}"


# ------------------------------------------------------------------ #
# Stage 4: equality semantics for use as registry/cache keys
# ------------------------------------------------------------------ #


class TestEquality:
    def test_two_secrets_with_same_value_equal(self, github_token_env: str) -> None:
        a = load_github_token_secret()
        b = load_github_token_secret()
        assert a == b

    def test_compare_against_raw_str_returns_false(self, github_token_env: str) -> None:
        # Comparing a Secret to its raw value must NOT silently
        # return True — that would defeat the wrapper. Callers must
        # explicitly call .reveal() to compare against an external
        # value.
        s = load_github_token_secret()
        assert s != _PILOT_PAT
