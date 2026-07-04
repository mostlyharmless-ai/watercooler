"""Tests for the LLM learning-synthesis layer (LLM mocked).

Validates the parse + gate logic of ``synthesize_learning``: a draft passes only
with a stated root cause + lesson, a confidence floor, and at least one quote
grounded verbatim in the thread source. No real LLM is called.
"""

from __future__ import annotations

import json

from watercooler.learning_synthesis import (
    _parse_llm_response,
    synthesize_learning,
)

# A resolved-thread fixture whose body contains the quote the LLM will "extract".
_ENTRIES = [
    {"entry_type": "Note", "title": "Auth bug", "body": "Login failed intermittently."},
    {"entry_type": "Closure", "title": "Fixed",
     "body": "The root cause was a missing guard on the write path."},
]

_GOOD = {
    "root_cause": "Unguarded write path",
    "lesson": "Guard the write path before enabling emission.",
    "problem_summary": "Intermittent login failure",
    "fix_summary": "Added the guard",
    "confidence": 4,
    "verbatim_quotes": ["missing guard on the write path"],
    "warning": None,
}


def _llm(payload):
    return lambda system, user: json.dumps(payload)


def test_parse_handles_plain_and_fenced_json() -> None:
    assert _parse_llm_response(json.dumps(_GOOD)).root_cause == "Unguarded write path"
    fenced = "```json\n" + json.dumps(_GOOD) + "\n```"
    assert _parse_llm_response(fenced).lesson.startswith("Guard the write path")
    assert _parse_llm_response("not json at all") is None


def test_synthesize_happy_path() -> None:
    r = synthesize_learning("auth-thread", _ENTRIES, llm_complete=_llm(_GOOD))
    assert r.passed is True
    assert r.confidence == 4
    assert r.draft_body and "## Lesson" in r.draft_body
    assert r.draft.verbatim_quotes == ["missing guard on the write path"]


def test_synthesize_rejects_below_confidence() -> None:
    r = synthesize_learning("t", _ENTRIES, llm_complete=_llm({**_GOOD, "confidence": 2}))
    assert r.passed is False
    assert r.rejection_reason == "below_confidence"


def test_synthesize_rejects_missing_root_cause_or_lesson() -> None:
    r = synthesize_learning("t", _ENTRIES, llm_complete=_llm({**_GOOD, "root_cause": None}))
    assert r.passed is False
    assert r.rejection_reason == "no_root_cause_or_lesson"


def test_synthesize_rejects_ungrounded_quotes() -> None:
    # Quote not present in the thread source -> rejected (no hallucinated evidence).
    bad = {**_GOOD, "verbatim_quotes": ["a quote that never appears in the thread"]}
    r = synthesize_learning("t", _ENTRIES, llm_complete=_llm(bad))
    assert r.passed is False
    assert r.rejection_reason == "ungrounded_quotes"


def test_synthesize_grounds_quote_despite_dropped_markdown() -> None:
    # A faithful quote that drops the source's markdown emphasis still grounds —
    # markdown-heavy thread bodies must not nondeterministically reject good drafts.
    entries = [
        {"entry_type": "Closure", "title": "Fixed",
         "body": "The fix was a **missing guard** on the `write path`."},
    ]
    quoted = {**_GOOD, "verbatim_quotes": ["missing guard on the write path"]}
    r = synthesize_learning("t", entries, llm_complete=_llm(quoted))
    assert r.passed is True
    assert r.draft.verbatim_quotes == ["missing guard on the write path"]


def test_markdown_stripping_does_not_admit_hallucinated_quotes() -> None:
    # The symmetric strip must not weaken the guarantee: words still must match.
    bad = {**_GOOD, "verbatim_quotes": ["a guarantee never stated in the thread"]}
    r = synthesize_learning("t", _ENTRIES, llm_complete=_llm(bad))
    assert r.passed is False
    assert r.rejection_reason == "ungrounded_quotes"


def test_markdown_strip_does_not_over_merge_tokens() -> None:
    # Boundary: stripping a marker must not bridge two words into one. The source
    # has `set a `b` flag`; the backtick is removed but the surrounding SPACE
    # survives, so "a b flag" grounds while the over-merged "ab flag" must NOT —
    # proving the strip only removes formatting, never token boundaries.
    entries = [
        {"entry_type": "Closure", "title": "Fixed", "body": "We set a `b` flag."},
    ]
    grounds = synthesize_learning(
        "t", entries, llm_complete=_llm({**_GOOD, "verbatim_quotes": ["set a b flag"]})
    )
    assert grounds.passed is True

    over_merged = synthesize_learning(
        "t", entries, llm_complete=_llm({**_GOOD, "verbatim_quotes": ["set ab flag"]})
    )
    assert over_merged.passed is False
    assert over_merged.rejection_reason == "ungrounded_quotes"


def test_synthesize_handles_empty_llm_response() -> None:
    r = synthesize_learning("t", _ENTRIES, llm_complete=lambda system, user: "")
    assert r.passed is False
    assert r.rejection_reason == "no_llm_response"


def test_format_learning_candidate_body() -> None:
    # Phase 2: the thread-visible candidate Note body carries the candidate
    # header (needs_human_confirmation / Authority: none) + the synthesized draft.
    from watercooler.learning_synthesis import format_learning_candidate_body

    r = synthesize_learning("auth-thread", _ENTRIES, llm_complete=_llm(_GOOD))
    assert r.passed
    body = format_learning_candidate_body(r, topic="auth-thread", pr_numbers=[42])
    assert "Candidate-Type: Learning" in body
    assert "Candidate-Status: needs_human_confirmation" in body
    assert "Authority: none" in body
    assert "Source-Thread: auth-thread" in body
    assert "#42" in body
    assert "Guard the write path" in body  # the lesson text
