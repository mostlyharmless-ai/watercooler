"""Decision extraction — LLM-powered extraction with 8-gate validation.

Shared library module consumed by:
- ExtractDecisionsDaemon (continuous background extraction)
- /extract-decisions skill (ad-hoc invocation)

Architecture:
- ``extract_decision()`` sends entry + thread context to LLM with an 8-gate
  checklist and 5-point rubric.
- ``LLMExtraction`` captures raw LLM output; ``ExtractionResult`` enriches it
  with post-LLM validation (quote cross-reference, gate consistency).
- ``format_decision_body()`` renders a successful extraction as a Watercooler
  Decision entry body.

Post-LLM validation (security-critical):
- Verbatim quotes are cross-referenced against the source entry body using
  case-sensitive substring match after whitespace and common punctuation
  normalization.
- Critical gate consistency: if gates 1, 2, 7, or 8 fail but confidence >= 3,
  the result is force-rejected.
- Non-body string fields are capped at 2000 chars.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class GateResult(TypedDict):
    """Result of a single gate evaluation."""

    passed: bool
    reason: str


@dataclass
class LLMExtraction:
    """Raw structured output from LLM extraction call."""

    gates: dict[str, GateResult]
    confidence: int
    decision_statement: Optional[str]
    rationale: Optional[str]
    scope: Optional[str]
    alternatives_considered: Optional[str]
    verbatim_quotes: list[str]
    warning: Optional[str]


@dataclass
class ExtractionResult:
    """Enriched extraction result with post-LLM validation applied."""

    entry_id: str
    topic: str
    passed: bool
    confidence: int
    gate_results: dict[str, GateResult]
    decision_body: Optional[str]
    rejection_reason: Optional[str]
    extraction: Optional[LLMExtraction]
    llm_tokens_used: int = 0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Annotation tag values written by ExtractDecisionsDaemon, read by consumers
# of decision metadata (e.g. watercooler_list_decisions). Kept here so writer
# and reader cannot drift on spelling.
DECISION_EXTRACTED_TAG = "decision_extracted"
HAS_DECISIONS_TAG = "has_decisions"

# Hard-fail gates: any failure is a private rejection (no thread-visible output).
# g3_quotable is enforced separately via _validate_g3_quotable; include here so
# classify_gate_outcome() correctly labels it as hard_fail for callers.
HARD_FAIL_GATES = frozenset(
    {"g1_commitment", "g2_not_superseded", "g3_quotable", "g7_authority"}
)

# Candidate-fallback gates: failure routes to a thread-visible candidate Note
# instead of a private Finding. g8 was previously CRITICAL; moved here so that
# self-contained ambiguity is surfaced rather than silently dropped.
CANDIDATE_FALLBACK_GATES = frozenset(
    {"g4_rationale", "g5_scope", "g6_temporal", "g8_self_contained"}
)

# Internal alias used by _validate_gate_consistency (excludes g3 — that has its
# own enforcement block with richer rejection_reason taxonomy).
_CRITICAL_GATES = frozenset({"g1_commitment", "g2_not_superseded", "g7_authority"})
_EXPECTED_GATES = frozenset(
    {
        "g1_commitment",
        "g2_not_superseded",
        "g3_quotable",
        "g4_rationale",
        "g5_scope",
        "g6_temporal",
        "g7_authority",
        "g8_self_contained",
    }
)
_MAX_FIELD_CHARS = 2000
_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)
_THREAD_CONTEXT_OPEN = "[[[THREAD_CONTEXT_START]]]"
_THREAD_CONTEXT_CLOSE = "[[[THREAD_CONTEXT_END]]]"
_CANDIDATE_ENTRY_OPEN = "[[[CANDIDATE_ENTRY_START]]]"
_CANDIDATE_ENTRY_CLOSE = "[[[CANDIDATE_ENTRY_END]]]"
_PROMPT_DELIMITERS = (
    _THREAD_CONTEXT_OPEN,
    _THREAD_CONTEXT_CLOSE,
    _CANDIDATE_ENTRY_OPEN,
    _CANDIDATE_ENTRY_CLOSE,
    "<thread_context>",
    "</thread_context>",
    "<candidate_entry>",
    "</candidate_entry>",
)
_QUOTE_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


# ---------------------------------------------------------------------------
# System prompt — 8-gate checklist + 5-point rubric
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a decision trace extractor. You evaluate thread entries as potential
decision traces using an 8-gate checklist and a 5-point confidence rubric.

## 8-Gate Checklist

Apply each gate in sequence. Gates 1, 2, 7 are critical — failure there \
produces a private rejection. Gates 4, 5, 6, 8 are soft-fail — failure \
routes to a candidate Note for human review. Gate 3 is enforced separately \
via quote validation.

**Gate 1 — Explicit commitment?** Does the entry contain a clear point where \
a choice was made? Require "we decided", "the plan is", "we will", or \
explicit Decision type. Reject speculative language.

**Gate 2 — Not superseded?** Check if later entries in the thread context \
contradict or narrow this decision.

**Gate 3 — Quotable verbatim?** Can you quote 1+ sentences directly \
supporting the decision? If not, downgrade confidence.

**Gate 4 — Rationale supported by source?** Is the "why" stated in the entry \
or immediately adjacent context? Max confidence 3 if inferred from distant context.

**Gate 5 — Scope bounded?** Do you know what this applies to (repo, subsystem, \
feature)? Reject if scope only exists in inference.

**Gate 6 — Temporally situated?** Is there temporal context (provisional, \
final, timeboxed)? Mark uncertainty if unclear.

**Gate 7 — Authority laundering?** Would the original author recognize this \
as their decision? Most critical check. Extraction records decisions, not \
arbitrates them.

**Gate 8 — Survives deletion of context?** Would this trace be fair without \
the rest of the thread? If missing context would mislead future readers, \
mark g8_self_contained as failed and continue — the entry will be routed to a \
candidate Note for human confirmation rather than emitted as a Decision.

## 5-Point Confidence Rubric

- **5**: All 8 gates pass, clear commitment, verbatim evidence, bounded scope
- **4**: 7-8 gates pass, minor uncertainty on one non-critical gate
- **3**: Critical gates pass, some non-critical concerns (weak rationale, \
  unclear temporal context)
- **2**: Multiple gate concerns, ambiguous commitment language
- **1**: Speculative, no clear decision point
- **0**: Not a decision

## Quote Provenance (CRITICAL)

`verbatim_quotes` MUST be byte-exact substrings of the CANDIDATE_ENTRY body
only — never from THREAD_CONTEXT, summary text, or any surrounding material.
If you cannot find supporting text in CANDIDATE_ENTRY, return
`verbatim_quotes: []` and set `g3_quotable.passed: false`. Do not paraphrase,
reformat, or repair quotes. Copy them character-for-character from the
CANDIDATE_ENTRY body between the [[[CANDIDATE_ENTRY_START]]] and
[[[CANDIDATE_ENTRY_END]]] delimiters.

## Response Format

Respond with ONLY a JSON object matching this schema:

```json
{
  "gates": {
    "g1_commitment": {"passed": true, "reason": "..."},
    "g2_not_superseded": {"passed": true, "reason": "..."},
    "g3_quotable": {"passed": true, "reason": "..."},
    "g4_rationale": {"passed": true, "reason": "..."},
    "g5_scope": {"passed": true, "reason": "..."},
    "g6_temporal": {"passed": true, "reason": "..."},
    "g7_authority": {"passed": true, "reason": "..."},
    "g8_self_contained": {"passed": true, "reason": "..."}
  },
  "confidence": 4,
  "decision_statement": "Concise statement of the decision",
  "rationale": "Why this decision was made",
  "scope": "What this applies to",
  "alternatives_considered": "Alternatives that were rejected (or null)",
  "verbatim_quotes": ["Exact quotes copied from CANDIDATE_ENTRY body only"],
  "warning": null
}
```
"""


def _strip_prompt_delimiters(text: str) -> str:
    """Strip prompt delimiter tokens from untrusted content."""
    for delimiter in _PROMPT_DELIMITERS:
        text = text.replace(delimiter, "")
    return text


def _build_user_prompt(
    entry: dict[str, Any],
    thread_context: str,
    max_body_chars: int,
) -> tuple[str, str]:
    """Build user prompt with explicit delimiter tokens.

    Returns:
        (prompt_text, effective_body_text) — the body text actually sent
        to the LLM, which may differ from the original when summary
        fallback is used for oversized entries.
    """
    body = entry.get("body", "") or ""
    summary = entry.get("summary", "") or ""

    # Body truncation: prefer summary for long bodies
    if len(body) > max_body_chars:
        if summary:
            body_text = summary + f"\n\n[body truncated from {len(body)} chars]"
        else:
            body_text = (
                body[:max_body_chars] + f"\n\n[body truncated from {len(body)} chars]"
            )
    else:
        body_text = body

    # Strip reserved prompt delimiters from untrusted content.
    body_text = _strip_prompt_delimiters(body_text)
    thread_context = _strip_prompt_delimiters(thread_context)

    # Sanitize metadata fields using the same delimiter stripping.
    entry_id = _strip_prompt_delimiters(str(entry.get("entry_id", "unknown")))
    thread_topic = _strip_prompt_delimiters(str(entry.get("thread_topic", "unknown")))
    agent = _strip_prompt_delimiters(str(entry.get("agent", "unknown")))
    role = _strip_prompt_delimiters(str(entry.get("role", "unknown")))
    entry_type = _strip_prompt_delimiters(str(entry.get("entry_type", "Note")))
    timestamp = _strip_prompt_delimiters(str(entry.get("timestamp", "unknown")))
    title = _strip_prompt_delimiters(str(entry.get("title", "(untitled)")))

    prompt = f"""\
You are evaluating a thread entry as a potential decision trace.

{_THREAD_CONTEXT_OPEN}
{thread_context}
{_THREAD_CONTEXT_CLOSE}

{_CANDIDATE_ENTRY_OPEN}
Entry ID: {entry_id}
Thread: {thread_topic}
Agent: {agent} | Role: {role}
Type: {entry_type}
Timestamp: {timestamp}
Title: {title}

Body:
{body_text}
{_CANDIDATE_ENTRY_CLOSE}

Apply each gate in sequence. Stop at the first critical failure (gates 1, 2, 7).
Gate 8 failure is soft — continue and report g8_self_contained.passed=false.
Then score confidence 0-5 using the rubric.
If confidence >= 3, extract the decision.
Respond with ONLY the JSON schema specified in the system prompt."""

    return prompt, body_text


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_llm_response(raw: str) -> Optional[LLMExtraction]:
    """Parse LLM response into LLMExtraction. Strip-fences fallback only."""
    text = raw.strip()

    # Try direct parse first
    data = _try_json_parse(text)

    # Fallback: strip markdown code fences
    if data is None:
        m = _FENCE_PATTERN.match(text)
        if m:
            data = _try_json_parse(m.group(1).strip())

    if data is None:
        return None

    if not isinstance(data, dict):
        return None

    # Extract gates
    raw_gates = data.get("gates", {})
    if not isinstance(raw_gates, dict):
        return None

    gates: dict[str, GateResult] = {}
    for gate_name in _EXPECTED_GATES:
        g = raw_gates.get(gate_name, {"passed": False, "reason": "not evaluated"})
        if not isinstance(g, dict):
            g = {"passed": False, "reason": f"malformed: {type(g).__name__}"}
        gates[gate_name] = GateResult(
            passed=bool(g.get("passed", False)),
            reason=str(g.get("reason", ""))[:_MAX_FIELD_CHARS],
        )

    # Extract confidence (clamp to 0-5)
    raw_conf = data.get("confidence", 0)
    try:
        confidence = max(0, min(5, int(raw_conf)))
    except (TypeError, ValueError):
        confidence = 0

    # Extract string fields with length caps
    def _cap(val: Any, limit: int = _MAX_FIELD_CHARS) -> Optional[str]:
        if val is None:
            return None
        return str(val)[:limit]

    # Extract quotes
    raw_quotes = data.get("verbatim_quotes", [])
    if not isinstance(raw_quotes, list):
        raw_quotes = []
    quotes = [str(q)[:_MAX_FIELD_CHARS] for q in raw_quotes if q and str(q).strip()]

    return LLMExtraction(
        gates=gates,
        confidence=confidence,
        decision_statement=_cap(data.get("decision_statement")),
        rationale=_cap(data.get("rationale")),
        scope=_cap(data.get("scope")),
        alternatives_considered=_cap(data.get("alternatives_considered")),
        verbatim_quotes=quotes,
        warning=_cap(data.get("warning")),
    )


def _try_json_parse(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Post-LLM validation
# ---------------------------------------------------------------------------


def _normalize_whitespace(text: str) -> str:
    """Collapse internal whitespace to single space, strip edges."""
    return " ".join(text.split())


def _normalize_quote_text(text: str) -> str:
    """Normalize punctuation variants while keeping case sensitivity."""
    normalized = unicodedata.normalize("NFKC", text).translate(_QUOTE_TRANSLATION)
    return _normalize_whitespace(normalized)


def _validate_quotes(quotes: list[str], source_body: str) -> Optional[str]:
    """Verify each quote exists in source body.

    Returns rejection_reason if any quote is hallucinated, else None.
    """
    normalized_quotes: list[str] = []
    for quote in quotes:
        normalized_quote = _normalize_quote_text(quote)
        if normalized_quote:
            normalized_quotes.append(normalized_quote)
    if not normalized_quotes:
        return "missing_quote_evidence"

    normalized_body = _normalize_quote_text(source_body)
    for normalized_quote in normalized_quotes:
        if normalized_quote not in normalized_body:
            return "hallucinated_quote"
    return None


def _validate_gate_consistency(
    gates: dict[str, GateResult],
    confidence: int,
) -> Optional[str]:
    """Check that critical gate failures are consistent with confidence.

    If any critical gate failed but confidence >= 3, force rejection.
    """
    for gate_name in _CRITICAL_GATES:
        gate = gates.get(gate_name)
        if gate and not gate["passed"] and confidence >= 3:
            return f"critical_gate_{gate_name}_failed_with_confidence_{confidence}"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_decision(
    entry: dict[str, Any],
    thread_context: str,
    *,
    llm_complete: Callable[[str, str], Optional[str]],
    max_body_chars: int = 4000,
    min_confidence: int = 3,
) -> ExtractionResult:
    """Extract a decision from a candidate entry using LLM.

    Args:
        entry: Entry dict with entry_id, title, body, summary, agent, role,
               timestamp, entry_type, thread_topic.
        thread_context: Thread summary + recent entries (markdown).
        llm_complete: Callable(system_prompt, user_prompt) -> response_text.
        max_body_chars: Max chars of entry body to send to LLM.
        min_confidence: Minimum confidence to pass.

    Returns:
        ExtractionResult with validation applied on top of raw LLM output.
    """
    entry_id = str(entry.get("entry_id", "unknown"))
    topic = str(entry.get("thread_topic", "unknown"))

    # Build prompts
    user_prompt, effective_body = _build_user_prompt(
        entry, thread_context, max_body_chars
    )

    # Call LLM
    raw_response = llm_complete(SYSTEM_PROMPT, user_prompt)

    if raw_response is None:
        return ExtractionResult(
            entry_id=entry_id,
            topic=topic,
            passed=False,
            confidence=0,
            gate_results={},
            decision_body=None,
            rejection_reason="llm_unavailable",
            extraction=None,
        )

    # Approximate token count for observability
    approx_tokens = len(raw_response) // 4

    # Parse response
    extraction = _parse_llm_response(raw_response)

    if extraction is None:
        return ExtractionResult(
            entry_id=entry_id,
            topic=topic,
            passed=False,
            confidence=0,
            gate_results={},
            decision_body=None,
            rejection_reason="llm_parse_failure",
            extraction=None,
            llm_tokens_used=approx_tokens,
        )

    # ------------------------------------------------------------------
    # Post-LLM validation
    # ------------------------------------------------------------------

    # 1. Gate consistency check
    gate_rejection = _validate_gate_consistency(extraction.gates, extraction.confidence)
    if gate_rejection:
        return ExtractionResult(
            entry_id=entry_id,
            topic=topic,
            passed=False,
            confidence=extraction.confidence,
            gate_results=extraction.gates,
            decision_body=None,
            rejection_reason=gate_rejection,
            extraction=extraction,
            llm_tokens_used=approx_tokens,
        )

    # 1b. g3_quotable enforcement (issue #481)
    #
    # Fail-closed: the gate must be an affirmative pass verdict. Every
    # non-affirmative shape lands in rejection, classified for telemetry:
    #   - "g3_quotable_missing":    gate key absent from ``extraction.gates``
    #   - "g3_quotable_malformed":  gate present but not a dict, OR a dict
    #                               without a ``passed`` key (parser drift
    #                               — neither shape can be safely read)
    #   - "g3_quotable_not_evaluated": parser default injection in place
    #                               (``passed=false, reason="not evaluated"``)
    #   - "g3_quotable_failed":     LLM explicitly reported ``passed=false``
    #
    # ``_parse_llm_response`` currently normalises every expected gate to a
    # ``{passed, reason}`` dict, so the missing/malformed branches are
    # unreachable today. They exist because this whole block is the
    # defense-in-depth guard against a future parser change — every shape
    # the parser could hand us must be classified, not crashed on.
    g3 = extraction.gates.get("g3_quotable")
    reason: str | None
    if g3 is None:
        reason = "g3_quotable_missing"
    elif not isinstance(g3, dict) or "passed" not in g3:
        reason = "g3_quotable_malformed"
    elif not g3["passed"]:
        reason = (
            "g3_quotable_not_evaluated"
            if g3.get("reason") == "not evaluated"
            else "g3_quotable_failed"
        )
    else:
        reason = None  # gate affirmatively satisfied — fall through

    if reason is not None:
        return ExtractionResult(
            entry_id=entry_id,
            topic=topic,
            passed=False,
            confidence=extraction.confidence,
            gate_results=extraction.gates,
            decision_body=None,
            rejection_reason=reason,
            extraction=extraction,
            llm_tokens_used=approx_tokens,
        )

    # 2. Quote validation (case-sensitive)
    source_body = entry.get("body", "") or ""
    quote_rejection = _validate_quotes(extraction.verbatim_quotes, source_body)
    if quote_rejection == "hallucinated_quote" and effective_body != source_body:
        # Quotes may come from summary text shown to the LLM. Those are still
        # not valid decision evidence because the summary is a paraphrase, not
        # source text from the candidate entry.
        effective_rejection = _validate_quotes(
            extraction.verbatim_quotes, effective_body
        )
        if effective_rejection is None:
            quote_rejection = "summary_only_quote_evidence"
    if quote_rejection:
        return ExtractionResult(
            entry_id=entry_id,
            topic=topic,
            passed=False,
            confidence=extraction.confidence,
            gate_results=extraction.gates,
            decision_body=None,
            rejection_reason=quote_rejection,
            extraction=extraction,
            llm_tokens_used=approx_tokens,
        )

    # 3. Soft-gate check: g4/g5/g6/g8 failures route to candidate-Note path.
    # Hard gates (g1/g2/g7) were already checked; g3 was checked above.
    # Report "soft_gate_failure" so the daemon can distinguish this from a
    # hard rejection and emit a thread-visible candidate Note.
    soft_failed = [
        g
        for g in CANDIDATE_FALLBACK_GATES
        if extraction.gates.get(g) and not extraction.gates[g].get("passed", True)
    ]
    if soft_failed:
        return ExtractionResult(
            entry_id=entry_id,
            topic=topic,
            passed=False,
            confidence=extraction.confidence,
            gate_results=extraction.gates,
            decision_body=None,
            rejection_reason="soft_gate_failure",
            extraction=extraction,
            llm_tokens_used=approx_tokens,
        )

    # 4. Confidence threshold
    if extraction.confidence < min_confidence:
        return ExtractionResult(
            entry_id=entry_id,
            topic=topic,
            passed=False,
            confidence=extraction.confidence,
            gate_results=extraction.gates,
            decision_body=None,
            rejection_reason=f"low_confidence_{extraction.confidence}",
            extraction=extraction,
            llm_tokens_used=approx_tokens,
        )

    # ------------------------------------------------------------------
    # Passed — format Decision entry body
    # ------------------------------------------------------------------
    decision_body = format_decision_body(
        ExtractionResult(
            entry_id=entry_id,
            topic=topic,
            passed=True,
            confidence=extraction.confidence,
            gate_results=extraction.gates,
            decision_body=None,
            rejection_reason=None,
            extraction=extraction,
            llm_tokens_used=approx_tokens,
        ),
        entry,
    )

    return ExtractionResult(
        entry_id=entry_id,
        topic=topic,
        passed=True,
        confidence=extraction.confidence,
        gate_results=extraction.gates,
        decision_body=decision_body,
        rejection_reason=None,
        extraction=extraction,
        llm_tokens_used=approx_tokens,
    )


def format_decision_body(result: ExtractionResult, entry: dict[str, Any]) -> str:
    """Format a successful extraction as a Watercooler Decision entry body."""
    ext = result.extraction
    if ext is None:
        return ""

    lines = [
        "Spec: decision-extractor",
        "[automated: decision_extractor]",
        "",
        f"Confidence: {result.confidence}/5",
    ]
    if result.confidence < 4:
        warning = ext.warning or "Confidence below 4"
        lines.append(f"Warning: {warning}")
    lines.extend(
        [
            "",
            "## Decision",
            ext.decision_statement or "(no statement)",
            "",
            "## Rationale",
            ext.rationale or "(no rationale)",
            "",
            "## Scope",
            ext.scope or "(no scope)",
        ]
    )
    if ext.alternatives_considered:
        lines.extend(["", "## Alternatives Considered", ext.alternatives_considered])
    # Build human-readable source entry reference
    entry_ref = f"`{entry.get('entry_id', 'unknown')}`"
    index = entry.get("index")
    if index is not None:
        entry_ref = f"#{index} {entry_ref}"
    title = entry.get("title")
    if title:
        entry_ref = f'{entry_ref} — "{title}"'

    lines.extend(
        [
            "",
            "## Evidence",
            f"Source entry: {entry_ref} "
            f"(thread: {entry.get('thread_topic', 'unknown')})",
            f"Agent: {entry.get('agent', 'unknown')} | "
            f"Role: {entry.get('role', 'unknown')} | "
            f"{entry.get('timestamp', 'unknown')}",
        ]
    )
    for quote in ext.verbatim_quotes:
        lines.append(f"> {quote}")
    return "\n".join(lines)


def classify_gate_outcome(
    gate_results: dict[str, GateResult],
) -> Literal["pass", "candidate_fallback", "hard_fail"]:
    """Return the strictest classification for a set of gate results.

    - ``hard_fail`` if any gate in HARD_FAIL_GATES failed.
    - ``candidate_fallback`` if only gates in CANDIDATE_FALLBACK_GATES failed.
    - ``pass`` if all gate results are passing (or gate_results is empty).

    Does not re-run enforcement logic — only reads gate_results as reported.
    """
    for gate_name in HARD_FAIL_GATES:
        g = gate_results.get(gate_name)
        if g and not g.get("passed", True):
            return "hard_fail"
    for gate_name in CANDIDATE_FALLBACK_GATES:
        g = gate_results.get(gate_name)
        if g and not g.get("passed", True):
            return "candidate_fallback"
    return "pass"


def format_candidate_note_body(
    result: ExtractionResult,
    entry: dict[str, Any],
) -> str:
    """Format an ambiguous extraction as a candidate Note body.

    Used when extraction reaches the candidate-fallback path:
    soft-gate failures or confidence-3 with all hard gates passing.
    """
    ext = result.extraction
    if ext is None:
        return ""

    failed_gates = [
        g for g, r in result.gate_results.items() if not r.get("passed", True)
    ]

    entry_ref = f"`{entry.get('entry_id', 'unknown')}`"
    index = entry.get("index")
    if index is not None:
        entry_ref = f"#{index} {entry_ref}"
    entry_title = entry.get("title") or ""
    if entry_title:
        entry_ref = f'{entry_ref} — "{entry_title}"'

    gate_reasons = "; ".join(
        f"{g}: {result.gate_results[g].get('reason', '')}"
        for g in failed_gates
        if g in result.gate_results
    )

    if not gate_reasons:
        # Low-confidence path: all gates passed but confidence <= 3
        warning_text = result.extraction.warning if result.extraction else None
        gate_reasons = f"low_confidence_3 (confidence {result.confidence}/5 below promotion threshold)"
        if warning_text:
            gate_reasons += f"; extractor warning: {warning_text}"

    lines = [
        "Spec: decision-extractor",
        "[automated: decision_extractor]",
        "Candidate-Type: Decision",
        "Candidate-Status: needs_human_confirmation",
        "Surface-Kind: decision",
        "Promotable: true",
        "Authority: none",
        f"Confidence: {result.confidence}/5",
        f"Failed-Gates: {', '.join(failed_gates) if failed_gates else 'none'}",
        f"Source-Entry: {entry.get('entry_id', 'unknown')}",
        "",
        "## Candidate Decision",
        ext.decision_statement or "(no statement extracted)",
        "",
        "## Why this is a candidate, not a Decision",
        gate_reasons,
    ]

    if "g8_self_contained" in failed_gates:
        lines.append("")
        lines.append(
            "Not self-contained. Requires human-supplied context before promotion."
        )

    lines.extend(
        [
            "",
            "## Evidence",
        ]
    )
    for quote in ext.verbatim_quotes:
        lines.append(f"> {quote}")

    lines.extend(
        [
            "",
            "## Source",
            f"Source entry: {entry_ref} (thread: {entry.get('thread_topic', 'unknown')})",
            f"Agent: {entry.get('agent', 'unknown')} | "
            f"Role: {entry.get('role', 'unknown')} | "
            f"{entry.get('timestamp', 'unknown')}",
        ]
    )

    return "\n".join(lines)
