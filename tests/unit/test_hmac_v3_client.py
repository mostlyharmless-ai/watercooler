"""Unit tests for ``watercooler_mcp.auth.hmac_v3_client``.

The client helper is the Python-side reference implementation for
v3 signing. These tests pin the round-trip contract against the
server-side ``build_v3_canonical_string`` + ``verify_v3_signature``
pair so any drift between the two surfaces is caught locally.
"""

from __future__ import annotations

import datetime
import re

import pytest

from watercooler_mcp.auth.hmac_keys import (
    build_v3_canonical_string,
    parse_v3_authorization_header,
    verify_v3_signature,
)
from watercooler_mcp.auth.hmac_v3_client import (
    sign_v3_request,
    v3_authorization_header,
)


_SECRET = bytes.fromhex("ab" * 32)
_KEY_ID = "ci_smoke"
_USER_ID = "smoke-test-user"
_REPO = "mostlyharmless-ai/watercooler"
_BRANCH = "main"


def _sign(**overrides):
    """Sign a baseline request, allowing per-test field overrides."""
    base = dict(
        method="POST",
        path="/mcp/",
        body=b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}',
        key_id=_KEY_ID,
        secret=_SECRET,
        user_id=_USER_ID,
        x_repo=_REPO,
        x_branch=_BRANCH,
    )
    base.update(overrides)
    return sign_v3_request(**base)


# -- Round-trip / verification --------------------------------------------- #


def test_sign_then_verify_roundtrip_succeeds():
    body = b'{"hello": "world"}'
    headers = sign_v3_request(
        method="POST",
        path="/mcp/",
        body=body,
        key_id=_KEY_ID,
        secret=_SECRET,
        user_id=_USER_ID,
        x_repo=_REPO,
        x_branch=_BRANCH,
    )

    parsed = parse_v3_authorization_header(headers["Authorization"])
    assert parsed is not None
    parsed_kid, parsed_sig = parsed
    assert parsed_kid == _KEY_ID

    canonical = build_v3_canonical_string(
        method="POST",
        path="/mcp/",
        timestamp=headers["X-Request-Timestamp"],
        key_id=_KEY_ID,
        user_id=_USER_ID,
        body=body,
        x_repo=_REPO,
        x_branch=_BRANCH,
    )
    assert verify_v3_signature(
        canonical=canonical, signature_hex=parsed_sig, secret=_SECRET
    )


def test_sign_uppercases_method():
    """Upper- and lower-cased ``method`` must produce identical signatures.

    The server upper-cases the method before reconstructing the
    canonical string, so the client must match that behaviour for
    verification to succeed regardless of caller convention.
    """
    pinned_ts = "2026-04-30T18:00:00+00:00"
    headers_lower = _sign(method="post", body=b"", timestamp=pinned_ts)
    headers_upper = _sign(method="POST", body=b"", timestamp=pinned_ts)
    assert headers_lower["Authorization"] == headers_upper["Authorization"]


def test_body_inclusion_changes_signature():
    pinned_ts = "2026-04-30T18:00:00+00:00"
    headers_a = _sign(body=b"abc", timestamp=pinned_ts)
    headers_b = _sign(body=b"xyz", timestamp=pinned_ts)
    assert headers_a["Authorization"] != headers_b["Authorization"]


def test_repo_inclusion_changes_signature():
    pinned_ts = "2026-04-30T18:00:00+00:00"
    headers_a = _sign(x_repo="org/a", timestamp=pinned_ts)
    headers_b = _sign(x_repo="org/b", timestamp=pinned_ts)
    assert headers_a["Authorization"] != headers_b["Authorization"]


def test_branch_inclusion_changes_signature():
    pinned_ts = "2026-04-30T18:00:00+00:00"
    headers_a = _sign(x_branch="main", timestamp=pinned_ts)
    headers_b = _sign(x_branch="staging", timestamp=pinned_ts)
    assert headers_a["Authorization"] != headers_b["Authorization"]


def test_user_id_inclusion_changes_signature():
    pinned_ts = "2026-04-30T18:00:00+00:00"
    headers_a = _sign(user_id="alice", timestamp=pinned_ts)
    headers_b = _sign(user_id="bob", timestamp=pinned_ts)
    assert headers_a["Authorization"] != headers_b["Authorization"]


# -- Header shape ----------------------------------------------------------- #


def test_authorization_header_format():
    headers = _sign()
    auth = headers["Authorization"]
    pattern = re.compile(r"^HMAC-SHA256 v=3 kid=[A-Za-z0-9_-]+ sig=[0-9a-f]{64}$")
    assert pattern.fullmatch(auth) is not None


def test_returns_all_required_headers():
    headers = _sign()
    expected = {
        "Authorization",
        "X-User-ID",
        "X-Request-Timestamp",
        "X-Repo",
        "X-Branch",
    }
    assert set(headers.keys()) == expected


def test_authorization_header_helper_format():
    auth = v3_authorization_header(key_id="test-key", signature_hex="ab" * 32)
    assert auth == "HMAC-SHA256 v=3 kid=test-key sig=" + ("ab" * 32)


# -- Timestamp behaviour --------------------------------------------------- #


def test_default_timestamp_is_iso8601_utc():
    headers = _sign(body=b"")
    ts = headers["X-Request-Timestamp"]
    parsed = datetime.datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_explicit_timestamp_is_preserved():
    pinned = "2026-04-30T18:27:18.978049+00:00"
    headers = _sign(timestamp=pinned)
    assert headers["X-Request-Timestamp"] == pinned


# -- Field-boundary guards ------------------------------------------------- #


@pytest.mark.parametrize(
    "field, value",
    [
        ("user_id", "alice\nbob"),
        ("user_id", "alice\rbob"),
        ("x_repo", "org/a\norg/b"),
        ("x_branch", "main\rstaging"),
        ("path", "/mcp\n/admin"),
    ],
)
def test_crlf_in_canonical_field_raises(field, value):
    with pytest.raises(ValueError):
        _sign(**{field: value})


# -- Cross-call determinism ------------------------------------------------ #


def test_same_inputs_same_signature():
    """Determinism: same inputs (with pinned timestamp) → same signature."""
    pinned_ts = "2026-04-30T18:00:00+00:00"
    headers_1 = _sign(timestamp=pinned_ts)
    headers_2 = _sign(timestamp=pinned_ts)
    assert headers_1 == headers_2


def test_default_timestamp_differs_between_calls():
    """Sanity: with no pinned timestamp, two calls produce distinct sigs."""
    # NB: the timestamp resolution is microseconds; calls within the same
    # microsecond would tie, but pytest invocation overhead makes a
    # collision overwhelmingly unlikely. Asserting timestamp inequality
    # rather than full-header inequality keeps this stable even on
    # absurdly fast machines.
    headers_1 = _sign()
    headers_2 = _sign()
    assert headers_1["X-Request-Timestamp"] != headers_2["X-Request-Timestamp"]
