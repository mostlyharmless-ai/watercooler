"""Decision candidate scoring — shared library module.

Scores Watercooler entry metadata for decision-candidate likelihood using
deterministic NLP (regex lexicons, negation guards, optional fuzzy matching).
Zero LLM cost.

This module is the single source of truth for scoring logic, consumed by:
- DetectDecisionsDaemon (continuous background scanning)
- score_entries.py CLI wrapper (ad-hoc invocation via /detect-decisions skill)

Signal architecture:
- Signal 1: title/type heuristics + phrase lexicons (STRONG, WEAK, INTENT)
- Signal 2: keyword matching (caller provides ``search_hit`` flag)
- Signal 3: T2 supersession proxy (caller provides ``t2_signal`` flag)

Public API:
- ``score_entry(entry, *, fuzzy_threshold=85) -> dict``
- ``score_entries(entries, *, min_score=1, skip_ids=None, fuzzy_threshold=85) -> list[dict]``
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Optional rapidfuzz — fuzzy phrase matching fallback
# ---------------------------------------------------------------------------
try:
    from rapidfuzz.fuzz import ratio as _fuzz_ratio  # type: ignore[import-untyped]
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False

# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------

# Firm commitments — clear-past-tense or first-person declarations
STRONG_PHRASES: list[str] = [
    "we decided",
    "the decision is",
    "decision was made",
    "committed to",
    "resolved to",
    "will use",
    "we've agreed",
    "we have agreed",
    "it was decided",
    "it is decided",
    "finalized",
    "the approach will be",
    "we're committing",
    "we commit",
    "the team decided",
    "team decision",
]

# Selection language — softer than strong but still directional
WEAK_PHRASES: list[str] = [
    "we chose",
    "we have chosen",
    "opted for",
    "going with",
    "agreed on",
    "we prefer",
    "the approach is",
    "we settled on",
    "we're going with",
    "we are going with",
    "we picked",
    "switching to",
    "moved to",
    "replacing with",
    "decided to use",
    "we landed on",
]

# Intention / planning language — future-tense; directional but not yet decided
INTENT_PHRASES: list[str] = [
    "we will",
    "we are going to",
    "the plan is",
    "going forward we",
    "going forward,",
]

# Negation guards — pre-match window (5 words before)
PRE_NEGATIONS: list[str] = [
    "not",
    "never",
    "won't",
    "don't",
    "didn't",
    "shouldn't",
    "haven't",
    "no longer",
    "rejected",
]

# Negation guards — post-match window (4 words after)
POST_NEGATIONS: list[str] = [
    "not",
    "not to",
    "against",
    "instead of",
    "rather than",
    "rejected",
]

# Speculative language — penalise when present (only if no strong commitment)
SPECULATIVE: list[str] = [
    "considering",
    "thinking about",
    "might",
    "could",
    "should we",
    "proposal",
    "brainstorm",
    "option",
    "exploring",
]


def _compile(phrases: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [
        (p, re.compile(rf"(?<!\w){re.escape(p)}(?!\w)", re.IGNORECASE))
        for p in phrases
    ]


_STRONG_PATS = _compile(STRONG_PHRASES)
_WEAK_PATS = _compile(WEAK_PHRASES)
_INTENT_PATS = _compile(INTENT_PHRASES)
_SPEC_PATS = _compile(SPECULATIVE)

_PRE_NEG_PATS = [
    re.compile(rf"(?<!\w){re.escape(p)}(?!\w)", re.IGNORECASE)
    for p in PRE_NEGATIONS
]
_POST_NEG_PATS = [
    re.compile(rf"(?<!\w){re.escape(p)}(?!\w)", re.IGNORECASE)
    for p in POST_NEGATIONS
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _context_window(text: str, start: int, end: int) -> tuple[str, str]:
    pre = text[max(0, start - 60):start]
    post = text[end:min(len(text), end + 40)]
    return " ".join(pre.split()[-5:]), " ".join(post.split()[:4])


def _is_negated(text: str, start: int, end: int) -> bool:
    pre_words, post_words = _context_window(text, start, end)
    return (
        any(p.search(pre_words) for p in _PRE_NEG_PATS)
        or any(p.search(post_words) for p in _POST_NEG_PATS)
    )


def _has_speculation(text: str) -> bool:
    return any(p.search(text) for _, p in _SPEC_PATS)


def _fuzzy_find(
    text: str, phrase: str, *, fuzzy_threshold: int = 85
) -> tuple[int, int] | None:
    """Sliding word-window fuzzy search using rapidfuzz.fuzz.ratio.

    Returns the span of the highest-scoring window that meets threshold,
    or None if no window qualifies, rapidfuzz is unavailable, or threshold is 0.
    """
    if not _RAPIDFUZZ_AVAILABLE or fuzzy_threshold == 0:
        return None

    phrase_n = len(phrase.split())
    tokens: list[tuple[str, int]] = [
        (m.group(), m.start()) for m in re.finditer(r"\S+", text)
    ]

    # Safety guard: skip fuzzy matching on very long texts where
    # exact regex matching is sufficient and sliding-window cost is high.
    if len(tokens) > 500:
        return None
    if len(tokens) < phrase_n:
        return None

    best_score = 0.0
    best_span: tuple[int, int] | None = None

    for i in range(len(tokens) - phrase_n + 1):
        window = " ".join(tokens[i + j][0] for j in range(phrase_n))
        score = _fuzz_ratio(window.lower(), phrase.lower())
        if score > best_score:
            best_score = score
            char_start = tokens[i][1]
            char_end = tokens[i + phrase_n - 1][1] + len(tokens[i + phrase_n - 1][0])
            best_span = (char_start, char_end)

    return best_span if best_score >= fuzzy_threshold else None


def _scan_phrase_list(
    text: str,
    pats: list[tuple[str, re.Pattern[str]]],
    hit_label: str,
    fuzzy_label: str,
    delta: int,
    *,
    fuzzy_threshold: int = 85,
) -> tuple[int, str | None, str | None]:
    """Scan one phrase list for exact + fuzzy matches with negation guard."""
    negated: set[str] = set()
    for phrase, pat in pats:
        m = pat.search(text)
        if m:
            if not _is_negated(text, m.start(), m.end()):
                return delta, hit_label, phrase
            else:
                negated.add(phrase)
    # Fuzzy fallback
    for phrase, _ in pats:
        if phrase in negated:
            continue
        span = _fuzzy_find(text, phrase, fuzzy_threshold=fuzzy_threshold)
        if span and not _is_negated(text, span[0], span[1]):
            return delta, fuzzy_label, f"~{phrase}"
    return 0, None, None


def _scan_field(
    text: str, *, fuzzy_threshold: int = 85
) -> tuple[int, list[str], list[str]]:
    """Scan one text field (title or summary).

    Returns (score_delta, signal_labels, matched_phrases).
    Caps: one strong hit, one weak hit, one intent hit per field.
    """
    if not text:
        return 0, [], []

    score = 0
    signals: list[str] = []
    matched: list[str] = []

    s_delta, s_label, s_phrase = _scan_phrase_list(
        text, _STRONG_PATS, "explicit", "explicit_fuzzy", 2,
        fuzzy_threshold=fuzzy_threshold,
    )
    if s_label:
        score += s_delta
        signals.append(s_label)
        matched.append(s_phrase)  # type: ignore[arg-type]

    w_delta, w_label, w_phrase = _scan_phrase_list(
        text, _WEAK_PATS, "implied", "implied_fuzzy", 1,
        fuzzy_threshold=fuzzy_threshold,
    )
    if w_label:
        score += w_delta
        signals.append(w_label)
        matched.append(w_phrase)  # type: ignore[arg-type]

    for phrase, pat in _INTENT_PATS:
        m = pat.search(text)
        if m and not _is_negated(text, m.start(), m.end()):
            score += 1
            signals.append("intent")
            matched.append(phrase)
            break

    # Penalise speculative language only if no firm commitment was found
    has_commitment = any(s in signals for s in ("explicit", "explicit_fuzzy"))
    if not has_commitment and _has_speculation(text):
        score -= 1
        signals.append("speculative")

    return score, signals, matched


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_entry(
    entry: dict[str, Any], *, fuzzy_threshold: int = 85
) -> dict[str, Any]:
    """Score a single entry for decision-candidate likelihood.

    Args:
        entry: Entry dict with keys: entry_id, entry_type, title, summary.
            Optional: search_hit (bool), t2_signal (bool).
        fuzzy_threshold: rapidfuzz threshold (0=disabled). Requires rapidfuzz.

    Returns:
        Scored dict with: entry_id, thread_topic, entry_type, title, summary,
        score, tier, signals, matched_phrases.
    """
    entry_id = str(entry.get("entry_id", "") or "")
    entry_type = entry.get("entry_type", "Note")
    title = entry.get("title", "") or ""
    summary = entry.get("summary", "") or ""
    t2_signal = bool(entry.get("t2_signal"))
    search_hit = bool(entry.get("search_hit"))

    score = 0
    signals: list[str] = []
    matched_phrases: list[str] = []

    # Typed Decision (+3)
    if entry_type == "Decision":
        score += 3
        signals.append("typed")

    # Title scan — positive contribution capped at +1; speculative penalty applied as-is
    title_score, title_signals, title_matches = _scan_field(
        title, fuzzy_threshold=fuzzy_threshold
    )
    if title_score > 0:
        score += min(title_score, 1)
    elif title_score < 0:
        score += title_score
    signals.extend(f"title_{s}" for s in title_signals)
    matched_phrases.extend(title_matches)

    # Summary scan — full field score
    body_score, body_signals, body_matches = _scan_field(
        summary, fuzzy_threshold=fuzzy_threshold
    )
    score += body_score
    signals.extend(body_signals)
    matched_phrases.extend(body_matches)

    # Signal 2 keyword search hit (+1)
    if search_hit:
        score += 1
        signals.append("search_hit")

    # Signal 3 T2 supersession — boost only when text-based commitment present
    if t2_signal:
        has_commitment = any(
            s in signals
            for s in (
                "explicit", "explicit_fuzzy",
                "title_explicit", "title_explicit_fuzzy",
            )
        )
        if has_commitment:
            score += 2
            signals.append("t2_state_change_boosted")
        else:
            score += 1
            signals.append("t2_state_change")

    # Question-form title penalty
    if title.strip().endswith("?"):
        score -= 1
        signals.append("question_penalty")

    score = max(score, 0)

    # Deduplicate signals while preserving insertion order
    deduped = list(dict.fromkeys(signals))

    tier = "High" if score >= 4 else "Medium" if score >= 2 else "Low"

    return {
        "entry_id": entry_id,
        "thread_topic": entry.get("thread_topic", ""),
        "entry_type": entry_type,
        "title": title,
        "summary": summary[:120] + "..." if len(summary) > 120 else summary,
        "score": score,
        "tier": tier,
        "signals": deduped,
        "matched_phrases": matched_phrases,
    }


def score_entries(
    entries: list[dict[str, Any]],
    *,
    min_score: int = 1,
    skip_ids: set[str] | None = None,
    fuzzy_threshold: int = 85,
) -> list[dict[str, Any]]:
    """Score a batch of entries, filtering and sorting results.

    Args:
        entries: List of entry dicts.
        min_score: Minimum score to include in output.
        skip_ids: Entry IDs to exclude (already-confirmed set).
        fuzzy_threshold: rapidfuzz threshold (0=disabled).

    Returns:
        Scored entries sorted by score descending, filtered by min_score.
    """
    _skip = skip_ids or set()
    results = []
    for entry in entries:
        eid = str(entry.get("entry_id", "") or "")
        if eid in _skip:
            continue
        scored = score_entry(entry, fuzzy_threshold=fuzzy_threshold)
        if scored["score"] >= min_score:
            results.append(scored)
    results.sort(key=lambda x: (-x["score"], x["entry_type"] != "Decision"))
    return results
