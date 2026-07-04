"""Summarizer grounding guard (#902 / #788).

The root fix for the OAuth2/JWT few-shot bleed: (1) a neutral few-shot example,
(2) an explicit grounding clause in the prompt, and (3) a deterministic detector
that catches a summary asserting an auth/credential mechanism absent from the
source, regenerating once then falling back to grounded extractive prose. The LLM
is mocked, so no service is required.
"""

from unittest.mock import patch

from watercooler.baseline_graph.summarizer import (
    SUMMARY_SCHEMA_VERSION,
    SummarizerConfig,
    summarize_entry,
    summarize_thread,
    summary_is_stale,
    _fabricates_security,
)

_LLM = "watercooler.baseline_graph.summarizer._call_llm"


def _no_auth_body() -> str:
    # > extractive_max_chars (200) so summarize_entry takes the LLM path, and it
    # explicitly NEGATES auth/JWT — asserting them is therefore a fabrication.
    return (
        "spyc is a single-binary terminal file manager that runs locally as the "
        "invoking user. It has no network code of its own, no authentication, and "
        "no JWT. Distributed internally to engineers. The maintainer enumerates "
        "three threats: supply-chain compromise of a Rust dependency, a tampered "
        "local build, and MCP socket misuse gated by filesystem permissions on the "
        "per-PID Unix socket."
    )


# --------------------------------------------------------------------------- #
# _fabricates_security — the detector
# --------------------------------------------------------------------------- #

def test_detects_ungrounded_jwt():
    assert _fabricates_security(
        "Adds JWT authentication for sessions.",
        "A terminal file manager with no network code.",
    )


def test_allows_grounded_oauth():
    assert not _fabricates_security(
        "Implemented OAuth2 login.",
        "We added an OAuth2 login flow via the identity provider.",
    )


def test_negated_source_does_not_ground_the_term():
    # Body says "no JWT" / "no authentication" -> a summary asserting them as
    # present is still a fabrication (the exact #788 acceptance case).
    assert _fabricates_security(
        "Uses JWT tokens and OAuth2 authentication for onboarding security.",
        "It has no authentication and no JWT; it has no network code.",
    )


def test_empty_summary_is_not_fabrication():
    assert not _fabricates_security("", "anything at all")


def test_no_security_vocabulary_is_not_fabrication():
    assert not _fabricates_security(
        "Refactored the date-parsing helper and added tests.",
        "Some unrelated code change.",
    )


def test_substring_word_does_not_ground_term():
    # Word-boundary matching: "sso" must NOT be grounded by "assorted" (#909 review).
    assert _fabricates_security("Adds SSO login.", "An assorted set of changes.")
    # And "authentication" is not grounded by "reauthentication" as a substring...
    assert _fabricates_security(
        "Implements authentication.", "Triggers reauthentication of the cache layer."
    )


def test_oauth_family_grounds_across_the_digit():
    # "OAuth" in the summary is grounded by "OAuth2" in the source and vice versa.
    assert not _fabricates_security("Adds OAuth login.", "We integrated OAuth2 SSO.")
    assert not _fabricates_security("Uses OAuth2.", "An OAuth flow was added.")


# --------------------------------------------------------------------------- #
# summarize_entry — regression + grounded passthrough
# --------------------------------------------------------------------------- #

def test_entry_fabricated_auth_regenerated_clean():
    """#788 regression: a body that has no auth must not yield a JWT/OAuth summary.
    First generation fabricates; the hardened retry is clean and is accepted."""
    body = _no_auth_body()
    with patch(_LLM, side_effect=[
        "Implemented OAuth2 authentication using JWT for onboarding security.",
        "spyc is a local single-binary file manager with no network code; threats "
        "are supply-chain compromise, a tampered build, and MCP socket misuse.",
    ]) as mock:
        result = summarize_entry(body)
    assert mock.call_count == 2  # regenerated once
    assert "oauth" not in result.lower()
    assert "jwt" not in result.lower()


def test_entry_stubborn_fabrication_falls_back_to_grounded_extractive():
    """LLM keeps fabricating -> deterministic extractive prose from the body, which
    contains no positive auth claim (the harmful fabrication is gone)."""
    body = _no_auth_body()
    fab = (
        "OAuth2 authentication implemented with JWT tokens, refresh rotation, and "
        "secure cookie storage."
    )
    with patch(_LLM, return_value=fab) as mock:
        result = summarize_entry(body)
    assert mock.call_count == 2  # tried regenerate, still bad -> extractive
    assert "oauth" not in result.lower()
    assert "secure cookie" not in result.lower()
    assert "single-binary" in result.lower()  # grounded body text


def test_entry_grounded_auth_passes_through():
    """A body that really is about auth keeps its auth summary (no false positive)."""
    body = (
        "Implemented the login flow using OAuth2 and JWT access tokens with refresh "
        "rotation. Added secure cookie storage and unit tests for the token-refresh "
        "edge cases. The middleware validates the bearer token on each request and "
        "rejects expired tokens."
    )
    summary = (
        "Implemented OAuth2 login with JWT access tokens, refresh rotation, and "
        "secure cookie storage."
    )
    with patch(_LLM, return_value=summary) as mock:
        result = summarize_entry(body)
    assert mock.call_count == 1  # grounded -> no regeneration
    assert result == summary


# --------------------------------------------------------------------------- #
# summarize_thread — grounding guard
# --------------------------------------------------------------------------- #

def test_thread_fabricated_security_regenerated():
    entries = [
        {"title": f"N{i}", "body": f"Discussion of the file-manager threat model, point {i}.",
         "type": "Note"}
        for i in range(6)
    ]
    with patch(_LLM, side_effect=[
        "The thread discusses implementing OAuth2 authentication with JWT tokens.",
        "The thread works through the file-manager threat model across several points.",
    ]) as mock:
        result = summarize_thread(entries, thread_title="threat-model")
    assert mock.call_count == 2
    assert "oauth" not in result.lower()
    assert "jwt" not in result.lower()


# --------------------------------------------------------------------------- #
# neutral few-shot example + schema bump
# --------------------------------------------------------------------------- #

def test_default_few_shot_example_is_neutral():
    cfg = SummarizerConfig()
    assert "oauth" not in cfg.summary_example_input.lower()
    assert "jwt" not in cfg.summary_example_input.lower()
    assert "oauth" not in cfg.summary_example_output.lower()
    assert "jwt" not in cfg.summary_example_output.lower()


def test_schema_version_bumped_and_v2_is_stale():
    assert SUMMARY_SCHEMA_VERSION == 3
    assert summary_is_stale({"summary_schema_version": 2})  # poisoned-era summaries
    assert not summary_is_stale({"summary_schema_version": 3})
