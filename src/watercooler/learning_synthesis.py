"""LLM synthesis of a learning record from a resolved thread's evidence.

The deterministic layer (``learning_extraction``) decides *whether* a closed
thread captured a learning or left a capture-gap. This module drafts the *content*
of the missing learning for a capture-gap thread — a structured "problem -> fix ->
root cause -> lesson" record with verbatim evidence — by prompting an LLM, then
validating the result (grounded quotes, stated root cause + lesson, confidence
floor). Mirrors ``watercooler.decision_extraction`` in shape and rigor.

The daemon records a passing draft as a **shadow finding** under watch-only mode —
never a thread-visible Note (that is the later, gated, graduated behavior). So this
module is where candidate *quality* lives; the precision eval measures its output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from watercooler.decision_extraction import normalize_quote_text

_MAX_FIELD_CHARS = 2000
_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

# Reserved prompt-structure tokens stripped from untrusted thread content.
_SRC_OPEN = "<<<THREAD_SOURCE>>>"
_SRC_CLOSE = "<<<END_THREAD_SOURCE>>>"
_PROMPT_DELIMITERS = (_SRC_OPEN, _SRC_CLOSE)

SYSTEM_PROMPT = """\
You distil a reusable engineering *learning* from a resolved work thread.

A good learning links a concrete problem to its fix and states the transferable
root cause and lesson — something a future engineer would want to know before
touching this area. Do NOT invent content: every quote must be copied verbatim
from the thread source provided.

Respond with ONLY this JSON:
```json
{
  "root_cause": "The underlying cause, stated transferably (or null)",
  "lesson": "The one-sentence reusable lesson (or null)",
  "problem_summary": "What went wrong",
  "fix_summary": "How it was resolved",
  "confidence": 4,
  "verbatim_quotes": ["Exact quotes copied from the thread source only"],
  "warning": null
}
```
Set confidence 0-5. Use null for root_cause/lesson when the thread does not
actually contain a transferable learning (e.g. routine/mechanical work)."""


@dataclass
class LearningDraft:
    """Raw structured output from the synthesis call."""

    root_cause: Optional[str]
    lesson: Optional[str]
    problem_summary: Optional[str]
    fix_summary: Optional[str]
    confidence: int
    verbatim_quotes: list[str] = field(default_factory=list)
    warning: Optional[str] = None


@dataclass
class SynthesisResult:
    """Validated synthesis result for one thread."""

    topic: str
    passed: bool
    confidence: int
    draft_body: Optional[str] = None
    rejection_reason: Optional[str] = None
    draft: Optional[LearningDraft] = None


def _strip_prompt_delimiters(text: str) -> str:
    for d in _PROMPT_DELIMITERS:
        text = text.replace(d, "")
    return text


def _build_source_text(entries: list[dict[str, Any]], max_chars: int) -> str:
    """Concatenate the thread's entries (title + body) as grounding source."""
    parts: list[str] = []
    for e in entries:
        title = str(e.get("title", "") or "")
        body = str(e.get("body", "") or e.get("summary", "") or "")
        parts.append(f"[{e.get('entry_type', 'Note')}] {title}\n{body}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[source truncated from {len(text)} chars]"
    return _strip_prompt_delimiters(text)


def _build_user_prompt(topic: str, source_text: str) -> str:
    topic = _strip_prompt_delimiters(str(topic))
    return f"""\
Distil the reusable learning from this resolved thread, if any.

Thread: {topic}

{_SRC_OPEN}
{source_text}
{_SRC_CLOSE}

Quote only from the thread source above. Respond with ONLY the JSON schema from
the system prompt."""


def _try_json_parse(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _cap(val: Any) -> Optional[str]:
    if val is None:
        return None
    return str(val)[:_MAX_FIELD_CHARS]


def _parse_llm_response(raw: str) -> Optional[LearningDraft]:
    """Parse the synthesis JSON; markdown-fence fallback only."""
    text = raw.strip()
    data = _try_json_parse(text)
    if data is None:
        m = _FENCE_PATTERN.match(text)
        if m:
            data = _try_json_parse(m.group(1).strip())
    if not isinstance(data, dict):
        return None

    try:
        confidence = max(0, min(5, int(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0

    raw_quotes = data.get("verbatim_quotes", [])
    if not isinstance(raw_quotes, list):
        raw_quotes = []
    quotes = [str(q)[:_MAX_FIELD_CHARS] for q in raw_quotes if q and str(q).strip()]

    return LearningDraft(
        root_cause=_cap(data.get("root_cause")),
        lesson=_cap(data.get("lesson")),
        problem_summary=_cap(data.get("problem_summary")),
        fix_summary=_cap(data.get("fix_summary")),
        confidence=confidence,
        verbatim_quotes=quotes,
        warning=_cap(data.get("warning")),
    )


# Markdown the LLM tends to drop when quoting prose. Stripped from both quote and
# source before grounding so a faithful quote that omits emphasis (``**bold**``),
# code ticks, or a blockquote/heading marker still grounds. Stripping is symmetric,
# so the anti-hallucination guarantee holds: the words must still match on both
# sides — only the formatting noise is removed.
#
# Note on ``_``: the inline class strips ALL underscores, not only emphasis
# ``_italic_`` — so identifiers are affected (``entry_count`` -> ``entrycount``).
# This is intentional and safe: the strip is symmetric, so identifiers still match
# themselves on both sides; it just won't bridge ``write_path`` to ``write path``
# (underscore removed, space kept — those stay distinct, as before).
# Out of scope (extend here if they ever cause false-negatives): link/image syntax
# ``[text](url)`` and ``-``/``1.`` list-item markers (``*`` bullets are already
# caught by the inline class; ``>``/``#`` block markers by the prefix class).
_MD_INLINE = re.compile(r"[*_`~]+")
_MD_BLOCK_PREFIX = re.compile(r"(?m)^[ \t]*[>#]+[ \t]*")


def _ground_norm(text: str) -> str:
    """Markdown-strip then apply the shared quote normalizer."""
    stripped = _MD_INLINE.sub("", _MD_BLOCK_PREFIX.sub("", text))
    return normalize_quote_text(stripped)


def _grounded_quotes(quotes: list[str], source_text: str) -> list[str]:
    """Return the subset of quotes whose normalized form appears in the source.

    Both quote and source are markdown-stripped before normalization, so a
    faithful quote that drops the source's markdown formatting still grounds
    (the words must still match — only formatting noise is normalized away).
    Returns the original quote strings (not the normalized form).
    """
    norm_source = _ground_norm(source_text)
    return [q for q in quotes if (nq := _ground_norm(q)) and nq in norm_source]


def format_learning_note_body(result: SynthesisResult, draft: LearningDraft) -> str:
    """Render the advisory-stamped learning record (shadow / future Note body)."""
    quotes = "\n".join(f"> {q}" for q in draft.verbatim_quotes) or "(none)"
    return (
        "Spec: learning\n"
        "Surface-Kind: learning\n"
        "Authority: none\n"
        "Status: advisory_until_phase_1a\n"
        f"Confidence: {draft.confidence}/5\n\n"
        f"## Lesson\n{draft.lesson or '(none)'}\n\n"
        f"## Root cause\n{draft.root_cause or '(none)'}\n\n"
        f"## Problem\n{draft.problem_summary or '(none)'}\n\n"
        f"## Fix\n{draft.fix_summary or '(none)'}\n\n"
        f"## Evidence (verbatim)\n{quotes}\n"
    )


def format_learning_candidate_body(
    result: SynthesisResult, *, topic: str, pr_numbers: list[int]
) -> str:
    """Render a thread-visible *learning candidate* Note body (Phase 2 emission).

    Mirrors the decision extractor's candidate-Note shape: a ``needs_human_
    confirmation`` / ``Authority: none`` surface carrying the synthesized draft,
    for human review and promotion. Never an authoritative entry.
    """
    draft = result.draft
    quotes = "\n".join(f"> {q}" for q in draft.verbatim_quotes) or "(none)"
    prs = ", ".join(f"#{n}" for n in pr_numbers) or "(none)"
    return (
        "Spec: learnings\n"
        "[automated: learnings]\n"
        "Candidate-Type: Learning\n"
        "Candidate-Status: needs_human_confirmation\n"
        "Surface-Kind: learning\n"
        "Authority: none\n"
        "Status: advisory_until_phase_1a\n"
        f"Confidence: {draft.confidence}/5\n"
        f"Source-Thread: {topic}\n"
        f"PRs: {prs}\n\n"
        f"## Candidate learning\n{draft.lesson or '(none)'}\n\n"
        "## Why this is a candidate, not a durable learning\n"
        "Synthesized by the Learnings daemon from a capture-gap thread (merged "
        "work with no write-up). Needs human confirmation before promotion "
        "(update-agent-context / watercooler_promote_candidate).\n\n"
        f"## Root cause\n{draft.root_cause or '(none)'}\n\n"
        f"## Fix\n{draft.fix_summary or '(none)'}\n\n"
        f"## Evidence (verbatim)\n{quotes}\n"
    )


def synthesize_learning(
    topic: str,
    entries: list[dict[str, Any]],
    *,
    llm_complete: Callable[[str, str], Optional[str]],
    min_confidence: int = 3,
    max_source_chars: int = 4000,
) -> SynthesisResult:
    """Draft and validate a learning record for a resolved thread.

    Args:
        topic: Thread topic.
        entries: The thread's entry dicts (the grounding evidence).
        llm_complete: ``Callable(system_prompt, user_prompt) -> response_text``.
        min_confidence: Minimum confidence (0-5) to pass.
        max_source_chars: Cap on grounding source sent to the LLM.

    Returns:
        A ``SynthesisResult``; ``passed`` is True only when the draft states a
        root cause + lesson, clears the confidence floor, and carries at least one
        quote grounded verbatim in the thread source.
    """
    source_text = _build_source_text(entries, max_source_chars)
    user_prompt = _build_user_prompt(topic, source_text)

    raw = llm_complete(SYSTEM_PROMPT, user_prompt)
    if not raw:
        return SynthesisResult(topic, False, 0, rejection_reason="no_llm_response")

    draft = _parse_llm_response(raw)
    if draft is None:
        return SynthesisResult(topic, False, 0, rejection_reason="unparseable_response")

    if draft.confidence < min_confidence:
        return SynthesisResult(
            topic, False, draft.confidence, rejection_reason="below_confidence", draft=draft
        )
    if not (draft.root_cause and draft.lesson):
        return SynthesisResult(
            topic, False, draft.confidence, rejection_reason="no_root_cause_or_lesson", draft=draft
        )

    grounded = _grounded_quotes(draft.verbatim_quotes, source_text)
    if not grounded:
        return SynthesisResult(
            topic, False, draft.confidence, rejection_reason="ungrounded_quotes", draft=draft
        )
    draft.verbatim_quotes = grounded

    result = SynthesisResult(topic, True, draft.confidence, draft=draft)
    result.draft_body = format_learning_note_body(result, draft)
    return result
