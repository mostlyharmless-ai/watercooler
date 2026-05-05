"""Secrets gateway — opaque ``Secret`` wrapper + redaction primitives.

Move 4 of the security consolidation plan v5.1. The motivation:
``credentials.py`` already loads tokens correctly with 0600 perms and
atomic writes. The bug is *consumer sprawl* — once a token escapes
the loader as ``str``, it ends up in JSON dumps, log lines, trace
snapshots, and exception messages. Every consumer must individually
remember to redact.

The fix is to give the value a type that **fails loudly** on naive
handling rather than leaking. A :class:`Secret` raises ``TypeError``
when handed to ``json.dumps`` directly; ``str()``, ``repr()``, and
``format()`` return a constant placeholder; pickle returns a
non-revealing reduction. :meth:`Secret.reveal` is the *primary*
greppable accessor for normal code review purposes — the path
audits look at first.

Audit scope, honestly stated. Python cannot fully seal a class:
``object.__getattribute__(secret, "_Secret__value")`` reads the
slot directly, bypassing :meth:`Secret.__getattribute__`. Subclasses
or descriptor protocol manipulations can also reach the value
without going through ``.reveal()``. The wrapper defends against
*accidental* leakage (json.dumps, log lines, repr in a stack trace,
attribute typos, shallow copies) — not against an attacker with
arbitrary code execution who can call low-level reflection. An
audit grep should look for ``.reveal()`` AND
``object.__getattribute__`` against Secret instances; the latter is
not a normal idiom in production code, so its presence is itself
an audit signal worth investigating.

Why non-``str``: ``class Secret(str)`` is a JSON footgun —
``json.dumps(Secret("token"))`` happily serializes the raw string
because ``json.JSONEncoder`` treats subclasses of ``str`` as
strings. ``Secret`` deliberately inherits from ``object`` so that
default JSON serialization fails fast at the egress boundary,
forcing the caller to either reach for :meth:`reveal` (explicit)
or :class:`SecretJSONEncoder` (structured redaction).

For unknown-but-secret-shaped strings (logs that capture user
input, third-party error messages, etc.) :func:`redact_value` does
pattern-based scrubbing of GitHub tokens (``ghp_``, ``gho_``,
``ghu_``, ``ghs_``, ``ghr_``), OpenAI keys (both classic ``sk-``
and project-scoped ``sk-proj-`` — the longer ``sk-proj-`` prefix
is matched first so the redaction label reflects the key class),
and watercooler-issued bearer tokens (``wc_``). :func:`redact_object`
walks dicts and lists at egress boundaries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Optional


# Pattern-based redaction for unknown-but-secret-shaped strings.
# Matches the prefixes of well-known token types followed by 20+
# safe characters. The trailing character class includes ``-`` so
# multi-segment keys (notably OpenAI's ``sk-proj-<alnum><dashes>...``
# project-scoped keys, and Slack's ``xox[bsap]-<digits>-...``
# segmented tokens) are redacted as a single unit rather than
# leaking any post-prefix portion.
#
# Alternation order matters: ``sk-proj-`` is listed BEFORE ``sk-``
# so the regex engine consumes the longer prefix first and the
# redacted-prefix label reflects which key class it came from.
#
# Slack token prefixes (Move 4 expansion — paired with
# :func:`load_slack_workspace_token_secret`): ``xoxb-`` bot tokens
# (per-workspace, the most common leak shape), ``xoxs-`` (workspace
# tokens), ``xoxa-`` user/admin tokens, ``xoxp-`` user-scoped
# tokens. Aggregated as ``xox[bsap]-`` for compactness. Without
# this, a raw Slack token escaping the legacy
# ``slack.token_service.get_workspace_token`` path (e.g. via an
# error message logged before reaching ``Secret(...)``) would
# emit unredacted, defeating the wrapper's intent.
SECRET_PATTERN = re.compile(
    r"(ghp_|gho_|ghu_|ghs_|ghr_|wc_|sk-proj-|sk-|xox[bsap]-)[A-Za-z0-9_-]{20,}"
)


def _redacted_placeholder(label: str) -> str:
    """Pickle reduction target — never exposes the underlying value."""
    return f"[REDACTED:{label}]"


# Slot name for the underlying value. The literal string
# ``"_Secret__value"`` is used (NOT Python class-body name
# mangling — that mechanism only applies to identifiers like
# ``self.__value`` referenced from within the class body, not to
# ``__slots__`` entries declared as runtime strings). Choosing
# this literal name nevertheless serves the same goal: naive
# attribute access (``secret._value``) hits the
# :meth:`Secret.__getattribute__` block, and
# ``secret._Secret__value`` from outside the class is clearly an
# intentional reach-through. The override blocks both, so the
# normal Python attribute protocol cannot read the value.
# ``object.__getattribute__(secret, "_Secret__value")`` reads
# directly via the C-level slot descriptor, bypassing the
# override — a separate audit concern that
# ``grep -rn 'object.__getattribute__'`` catches.
_VALUE_SLOT = "_Secret__value"
_LABEL_SLOT = "_label"

# Names blocked at attribute-read time. Reads of these go through
# :meth:`Secret.__getattribute__` and AttributeError before reaching
# the slot. ``reveal()`` itself uses :func:`object.__getattribute__`
# to bypass.
_BLOCKED_VALUE_NAMES = frozenset({"_value", "value", _VALUE_SLOT})


class Secret:
    """Opaque wrapper for a secret string.

    Cannot be silently coerced to its underlying value through any
    standard serializer. :meth:`reveal` is the primary greppable
    accessor; ``__getattribute__`` blocks direct slot access via
    the normal Python attribute protocol (``secret._value`` /
    ``secret._Secret__value``). Reflection-level access via
    ``object.__getattribute__`` is NOT blocked — Python cannot
    fully seal a class — so the audit guarantee is "the wrapper
    defends against accidental leakage; reflection bypasses are a
    separate audit concern."

    Equality is constant-time via :func:`hmac.compare_digest` and
    compares ``(label, value)`` — two Secrets with the same value
    but different labels are NOT equal, so a cache keyed on
    Secret cannot conflate ``github_pat`` and ``slack_token``
    objects that happen to share a value. Hash is derived from
    SHA-256 of ``(label, value)`` so the dict invariant
    ``a == b ⇒ hash(a) == hash(b)`` holds.

    Copy and deepcopy raise :class:`TypeError` to prevent
    standard-library reflection from cloning the value; callers who
    need a fresh wrapper construct one explicitly via
    ``Secret(other.reveal(), label=other.label)`` so the
    ``.reveal()`` audit captures the access.
    """

    __slots__ = (_VALUE_SLOT, _LABEL_SLOT)

    def __init__(self, value: str, *, label: str = "secret") -> None:
        if not isinstance(value, str):
            raise TypeError(f"Secret expects str, got {type(value).__name__}")
        if not isinstance(label, str) or not label:
            raise ValueError("Secret label must be a non-empty str")
        # Bypass __setattr__ for the immutability guard.
        object.__setattr__(self, _VALUE_SLOT, value)
        object.__setattr__(self, _LABEL_SLOT, label)

    # ------------------------------------------------------------------ #
    # Attribute access — block direct reads of the value slot
    # ------------------------------------------------------------------ #

    def __getattribute__(self, name: str) -> Any:
        # External callers attempting ``secret._value``,
        # ``secret.value``, or ``secret._Secret__value`` get a clear
        # AttributeError pointing at the legitimate accessor. Internal
        # code (``reveal``, ``__eq__``, etc.) uses
        # ``object.__getattribute__`` to bypass this guard.
        if name in _BLOCKED_VALUE_NAMES:
            raise AttributeError(
                f"{name!r}: Secret value is opaque; call .reveal() to access "
                "(grep -rn '.reveal(' for the egress audit)"
            )
        return object.__getattribute__(self, name)

    # ------------------------------------------------------------------ #
    # Immutability
    # ------------------------------------------------------------------ #

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Secret is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Secret is immutable")

    # ------------------------------------------------------------------ #
    # Stringification — never reveal
    # ------------------------------------------------------------------ #

    def __str__(self) -> str:
        return _redacted_placeholder(object.__getattribute__(self, _LABEL_SLOT))

    def __repr__(self) -> str:
        label = object.__getattribute__(self, _LABEL_SLOT)
        return f"Secret({label!r}, ...)"

    def __format__(self, format_spec: str) -> str:
        # f-strings call __format__. Empty format_spec is the
        # ``f"{secret}"`` default — return the redacted
        # placeholder. A NON-empty format_spec (e.g. ``f"{secret:>20}"``,
        # ``f"{secret:.10s}"``) implies the caller is treating the
        # Secret as a plain string and would silently drop the
        # spec under the previous ``return str(self)`` shortcut —
        # better to fail loudly so the caller notices and
        # explicitly handles the Secret (PR #704 round 4 LOW).
        if format_spec:
            raise TypeError(
                "Secret cannot be formatted with a format_spec "
                f"({format_spec!r}); call .reveal() explicitly if "
                "you need the underlying value."
            )
        return str(self)

    def __bytes__(self) -> bytes:
        return str(self).encode("utf-8")

    # ------------------------------------------------------------------ #
    # Pickle / copy / deepcopy — never reveal
    # ------------------------------------------------------------------ #

    def __reduce__(self) -> tuple[Any, ...]:
        # ``pickle.dumps(secret)`` followed by ``pickle.loads`` would
        # otherwise round-trip the underlying value. Reduce to a
        # placeholder string instead.
        label = object.__getattribute__(self, _LABEL_SLOT)
        return (_redacted_placeholder, (label,))

    def __copy__(self) -> Any:
        # ``copy.copy`` falls back to a slot-copy protocol that
        # produces a live ``Secret`` with the value intact when
        # ``__copy__`` is undefined. Refuse instead — callers who
        # really want a fresh wrapper construct one explicitly via
        # ``Secret(other.reveal(), label=other.label)`` so the
        # ``.reveal()`` audit captures the access.
        raise TypeError(
            "Secret instances cannot be copied. Construct a new Secret "
            "explicitly via Secret(other.reveal(), label=other.label) so "
            "the .reveal() audit captures the access."
        )

    def __deepcopy__(self, memo: Any) -> Any:
        raise TypeError(
            "Secret instances cannot be deep-copied. See __copy__ for the "
            "explicit-construction pattern."
        )

    # ------------------------------------------------------------------ #
    # Equality + hashing
    # ------------------------------------------------------------------ #

    def __eq__(self, other: Any) -> bool:
        # Constant-time compare via hmac.compare_digest. Equality
        # checks BOTH label AND value — two Secrets with the same
        # underlying string but different labels are NOT equal.
        # Otherwise a cache keyed on Secret could silently
        # conflate ``github_pat`` and ``slack_token`` instances
        # that happen to share a value (PR #704 round 4 MED).
        if not isinstance(other, Secret):
            return NotImplemented
        a_label = object.__getattribute__(self, _LABEL_SLOT)
        b_label = object.__getattribute__(other, _LABEL_SLOT)
        if a_label != b_label:
            return False
        a_value = object.__getattribute__(self, _VALUE_SLOT)
        b_value = object.__getattribute__(other, _VALUE_SLOT)
        return hmac.compare_digest(a_value.encode("utf-8"), b_value.encode("utf-8"))

    def __hash__(self) -> int:
        # Hash MUST agree with __eq__ (Python invariant: a == b ⇒
        # hash(a) == hash(b)). Equality compares ``(label, value)``,
        # so the hash digests both fields. SHA-256 of the (label,
        # value) tuple is one-way; an attacker observing Python's
        # hash output cannot invert it any more easily than
        # brute-forcing the digest itself, and Python's hash() of
        # bytes is process-randomised on top of that.
        label = object.__getattribute__(self, _LABEL_SLOT)
        value = object.__getattribute__(self, _VALUE_SLOT)
        material = f"{label}\x00{value}".encode("utf-8")
        digest = hashlib.sha256(material).digest()
        return hash(("Secret", digest))

    def __bool__(self) -> bool:
        # ``bool(secret)`` is the supported "did I load a token at
        # all?" check. ``__len__`` is intentionally NOT defined on
        # Secret because token lengths are class-discriminating
        # (GitHub classic PATs are 40 chars; fine-grained tokens
        # 93; OpenAI sk- keys 51) — exposing length via ``len()``
        # would let an auditor infer the token type without
        # going through .reveal(), bypassing the egress audit.
        return bool(object.__getattribute__(self, _VALUE_SLOT))

    # ------------------------------------------------------------------ #
    # Reveal — the single greppable accessor
    # ------------------------------------------------------------------ #

    def reveal(self) -> str:
        """Return the underlying value.

        This is the *primary* greppable accessor for the secret.
        Code that legitimately needs the value (HTTP bearer
        header, signing, comparison against an external system)
        should call this method. ``grep -rn '.reveal('`` produces
        an audit-able list of every escape point in normal code.

        Caveat: reflection-level access (``object.__getattribute__``,
        descriptor protocol, subclassing) can read the slot
        without going through this method. A complete audit for
        leakage looks for both ``.reveal(`` AND
        ``object.__getattribute__`` calls against Secret
        instances. The wrapper's design goal is to defend against
        *accidental* leakage — the grep filter is the audit
        signal, not a hard seal.
        """
        return object.__getattribute__(self, _VALUE_SLOT)

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def label(self) -> str:
        return object.__getattribute__(self, _LABEL_SLOT)


# ------------------------------------------------------------------ #
# JSON encoder — explicit redaction at structured-output boundaries
# ------------------------------------------------------------------ #


class SecretJSONEncoder(json.JSONEncoder):
    """JSON encoder that emits ``[REDACTED:<label>]`` for Secrets.

    Use this at structured-output boundaries that legitimately
    surface diagnostic shapes containing Secrets — telemetry,
    health-check JSON, audit log records. The default
    ``json.JSONEncoder`` raises ``TypeError`` for Secret (because
    Secret is non-str), which is the correct fail-fast behavior
    for *unintended* serialization. This encoder is for
    *intended* redaction.
    """

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Secret):
            return _redacted_placeholder(object.__getattribute__(obj, _LABEL_SLOT))
        return super().default(obj)


# ------------------------------------------------------------------ #
# Pattern-based redaction
# ------------------------------------------------------------------ #


def redact_value(value: Any) -> Any:
    """Redact secret-shaped substrings in a free-form string.

    Useful when capturing third-party output (LLM responses, error
    messages, log lines) where a Secret wrapper isn't applicable
    because the secret entered as bytes from outside the system.

    The signature is intentionally ``Any -> Any`` rather than
    ``str -> str``: callers commonly pass values whose type they
    haven't inspected (log fields, dict values), and raising on
    non-str input would force every call site to add a guard. The
    passthrough is the conservative choice — non-string values
    cannot contain secret-shaped *strings* by definition, so they
    are returned unchanged.
    """
    if not isinstance(value, str) or not value:
        return value

    def _replace(match: "re.Match[str]") -> str:
        # PR #704 round 7+2 LOW: ``rstrip("_-")`` strips ALL trailing
        # underscores AND dashes from the matched prefix to produce
        # the redaction label. For the current prefix set this is
        # correct: ``ghp_`` → ``ghp``, ``sk-proj-`` → ``sk-proj``,
        # ``wc_`` → ``wc``. If a future prefix ends in ``--`` or
        # ``__`` the multi-character strip would silently collapse
        # both — by then the label would be ambiguous regardless,
        # so the contract is "strip exactly the trailing separator
        # run." Documented here so a future prefix added with a
        # different separator convention (e.g. ``foo.``) gets
        # explicit attention rather than a silent label change.
        prefix = match.group(1).rstrip("_-")
        return f"[REDACTED:{prefix}_*]"

    return SECRET_PATTERN.sub(_replace, value)


def redact_object(obj: Any) -> Any:
    """Recursively redact dicts and lists for egress-boundary logging.

    Returns a new structure (does not mutate the input) where every
    string leaf has had :func:`redact_value` applied and every
    Secret has been replaced with its placeholder. Non-string,
    non-Secret leaves pass through unchanged.

    Use this at egress boundaries that emit nested diagnostic
    structures — request-trace snapshots, daemon-finding
    serializers, failure reports.
    """
    if isinstance(obj, Secret):
        return _redacted_placeholder(obj.label)
    if isinstance(obj, str):
        return redact_value(obj)
    if isinstance(obj, dict):
        # Both keys and values are redacted (PR #704 round 4 MED).
        # A dict can carry secret-shaped strings as keys (e.g.
        # ``{"ghp_aaaa...": "metadata"}``); skipping keys would
        # leave them unscrubbed in the output.
        #
        # PR #704 round 5 MED: when two distinct keys redact to the
        # same placeholder (e.g. two ``ghp_*`` tokens both produce
        # ``[REDACTED:ghp_*]``), a naive dict comprehension
        # silently drops entries — the second value overwrites the
        # first, audit records lose entry count. Fix: detect
        # collisions and append an index suffix
        # (``[REDACTED:ghp_*]#2``) so all entries survive in the
        # output and the audit dict has the same len() as the
        # input.
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            redacted_k = redact_object(k)
            redacted_v = redact_object(v)
            if redacted_k not in out:
                out[redacted_k] = redacted_v
                continue
            # Collision: append #N to the redacted key. Start from
            # #2 so the first occurrence is the bare placeholder.
            if isinstance(redacted_k, str):
                idx = 2
                while f"{redacted_k}#{idx}" in out:
                    idx += 1
                out[f"{redacted_k}#{idx}"] = redacted_v
            else:
                # Non-string redacted key (rare — e.g. a Secret
                # object placeholder is a str so this branch is
                # essentially defensive). Wrap in a tuple to
                # disambiguate.
                idx = 2
                while (redacted_k, idx) in out:
                    idx += 1
                out[(redacted_k, idx)] = redacted_v
        return out
    if isinstance(obj, (list, tuple)):
        # PR #704 round 6 MED: rebinding ``out`` (declared as
        # ``dict[Any, Any]`` in the dict branch) to a list trips
        # mypy. The dict branch always returns before this point,
        # so there is no runtime issue, but the type annotation
        # binds for the function scope. Use a distinct name.
        seq_out: list[Any] = [redact_object(v) for v in obj]
        return seq_out if isinstance(obj, list) else tuple(seq_out)
    if isinstance(obj, (set, frozenset)):
        # Redact set / frozenset leaves with the same
        # collision-handling the dict branch uses. Two distinct
        # Secrets with the same label but different values are
        # legally unequal under :class:`Secret.__eq__`
        # (constant-time compare on (label, value)), so the
        # input set can hold both. Naive set comprehension
        # collapses them to a single placeholder string and
        # silently undercounts the audit. Index suffixes
        # (``[REDACTED:pat]#2``) preserve len() instead.
        # PR #704 round 6 MED.
        out_set: set[Any] = set()
        for v in obj:
            redacted = redact_object(v)
            if redacted not in out_set:
                out_set.add(redacted)
                continue
            if isinstance(redacted, str):
                idx = 2
                while f"{redacted}#{idx}" in out_set:
                    idx += 1
                out_set.add(f"{redacted}#{idx}")
            else:
                idx = 2
                while (redacted, idx) in out_set:
                    idx += 1
                out_set.add((redacted, idx))
        return out_set if isinstance(obj, set) else frozenset(out_set)
    return obj


# ------------------------------------------------------------------ #
# Credentials-pilot loaders
# ------------------------------------------------------------------ #


def load_github_token_secret() -> Optional[Secret]:
    """Load the GitHub PAT and return it wrapped in a :class:`Secret`.

    The Move 4 pilot loader. Lives here in ``watercooler_mcp`` rather
    than in ``watercooler.credentials`` so the open-core import
    direction is preserved: ``watercooler_mcp`` depends on
    ``watercooler``, never the reverse. An open-core deployment with
    only the ``watercooler`` core package can continue to call
    ``watercooler.credentials.get_github_token()`` and receive a
    raw ``str``; the Secret-wrapped pilot path is opt-in for
    callers running inside ``watercooler_mcp``.

    Returns:
        ``Secret(value, label="github_pat")`` if a token is
        configured (env or credentials.toml), else ``None``.
    """
    # Late import keeps the module top-level light and lets
    # consumers stub ``watercooler.credentials.get_github_token``
    # in tests without dragging the loader's deps into other
    # ``secrets.gateway`` consumers.
    from watercooler.credentials import get_github_token

    raw = get_github_token()
    if not raw:
        return None
    return Secret(raw, label="github_pat")


def load_slack_workspace_token_secret(
    workspace_id: str, *, use_cache: bool = True
) -> Optional[Secret]:
    """Load a Slack workspace bot token wrapped in a :class:`Secret`.

    Move 4 expansion (security consolidation plan v5.1) — extends the
    GitHub PAT pilot's wrapper pattern to Slack workspace bot tokens.
    Slack tokens look like ``xoxb-<long>``; an accidental json.dumps,
    log line, exception message, or trace snapshot leaking one is the
    same class of A2 (same-UID local disclosure) finding the wrapper
    is meant to neutralise.

    The legacy raw-string loader
    (:func:`watercooler_mcp.slack.token_service.get_workspace_token`)
    is unchanged — back-compat for any caller that already handles
    redaction at its egress boundary. New callers should prefer this
    Secret-wrapped loader so the token can only leave the wrapper via
    an explicit ``.reveal()`` call (greppable as the audit anchor).

    Args:
        workspace_id: Slack team / workspace ID (``T12345ABC``).
        use_cache: Forwarded to the underlying token-service cache.

    Returns:
        ``Secret(value, label="slack_bot_token")`` if a token is
        configured for the workspace, else ``None``.
    """
    # Late import for the same reason as ``load_github_token_secret``:
    # keeps the module top-level light and lets tests stub the
    # underlying loader without pulling it into every consumer.
    from watercooler_mcp.slack.token_service import get_workspace_token

    raw = get_workspace_token(workspace_id, use_cache=use_cache)
    if not raw:
        return None
    return Secret(raw, label="slack_bot_token")


__all__ = [
    "Secret",
    "SecretJSONEncoder",
    "load_github_token_secret",
    "load_slack_workspace_token_secret",
    "redact_object",
    "redact_value",
    "SECRET_PATTERN",
]
