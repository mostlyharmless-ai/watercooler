"""Anti-anthropomorphism advisory lint (#895, Chiang mapping §9D).

Advisory-only, write-time lint that flags first-person *interiority* in
agent-authored entry bodies — language that implies belief, care, concern, or
conscience ("I believe", "I'm worried", "in good conscience", "my judgment
is"). Such phrasing simulates an accountable mind that is not there, and is a
channel by which fluency acquires *de-facto* authority — the same
interface-authority surface the authority ladder exists to contain. (The
"understanding" register from Chiang §9D is deliberately not detected — see the
category note below for why a regex cannot separate it from procedural prose.)

The lint **never blocks** a write. It is a control in the same family as the
moral-delegation gate (#880) and the generated-vs-accepted labels: the wired
caller (``watercooler_write``) appends a visible advisory marker to the body,
but the entry is recorded unchanged otherwise. Source framing:
``dev_docs/papers/analyses/2026-06-04-analysis-chiang-2026-no-ai-is-not-conscious.md``
§9D.

**Scope is interiority, not all first-person.** Procedural first-person that
reports actions taken is legitimate and must NOT fire: "I searched X and
found Y", "I ran the quote check", "I read the file and updated the config".
This module only matches first-person bound to an interiority predicate, so
action verbs never match — the exclusion is structural, not a denylist.

**Conservatism is a correctness requirement, not politeness.** Over-firing
trains callers to dismiss the advisory, which re-enables the anthropomorphic
laundering the lint exists to surface (the same lesson learned for the
moral-delegation classifier). The pattern set deliberately covers only
unambiguous interiority constructions; borderline belief markers in very
common technical use ("I think we should refactor") are left out rather than
risk noise.

Stdlib-only leaf module: no watercooler imports, so it stays a cheap,
side-effect-free dependency for both the CLI and MCP write paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Structured marker appended to flagged bodies. A single stable claim-class
# token (parallel to ``Moral-Delegation-Warning: true``) so one body-level
# query — ``filter="Anthropomorphism-Advisory"`` — finds every flagged entry.
# Per-finding detail lives in the human-readable note, not the marker value.
ANTHROPOMORPHISM_ADVISORY_MARKER = "Anthropomorphism-Advisory"
ANTHROPOMORPHISM_ADVISORY_VALUE = "interiority"

# Ontology note (Chiang mapping §4/§5), stated once so the advisory carries the
# reframing, not just the flag: roles and surfaces are operational labels for
# generated contributions, never claims of interiority.
ONTOLOGY_NOTE = (
    "roles and surfaces are operational labels for generated contributions, "
    "not claims of agency, understanding, conscience, or responsibility"
)

# Interiority categories, each paired with the preferred non-interiority
# register from §9D. Category names are part of the note text (observability),
# not the queryable marker value.
CATEGORY_BELIEF = "belief"
CATEGORY_CARE_CONCERN = "care_concern"
CATEGORY_CONSCIENCE = "conscience"

# An "understanding" category ("I understand", "my understanding is") was
# considered (Chiang §9D lists "I understand") but deliberately NOT shipped:
# the "understand" stem is irreducibly ambiguous between an interiority claim
# and ordinary procedural comprehension ("I understand the stack trace and
# patched it", "as I understand it the API returns 404"). A regex cannot
# discriminate the two without firing on procedural prose, and over-firing
# trains callers to dismiss the advisory — the failure mode this lint exists to
# avoid. Detecting it would need semantic analysis; it is left out rather than
# shipped as noise. See #895 review notes.

_SUGGESTIONS: dict[str, str] = {
    CATEGORY_BELIEF: '"this analysis suggests" / "the evidence indicates"',
    CATEGORY_CARE_CONCERN: '"a risk is" / "a concern is"',
    CATEGORY_CONSCIENCE: '"a human decision is needed" — moral ownership is human',
}

# Apostrophe class: straight and both curly forms, so "I'm" / "I’m" both match.
_APOS = r"['’ʼ]"
# First-person copular subject — "I'm" (any apostrophe) or "I am". Used wherever
# the interiority predicate is an adjective/participle ("worried", "convinced").
_I_AM = rf"\bI(?:{_APOS}m|\s+am)\s+"
# Bare first-person subject — "I " followed by an interiority verb.
_I = r"\bI\s+"
# Optional negation after a bare "I " — "do not" / "don't" / "dont".
_NOT = rf"(?:do\s+not\s+|do\s*n{_APOS}?t\s+)?"
# Optional affective/epistemic intensifier — "really", "strongly", etc.
_INTENSIFY = r"(?:really\s+|truly\s+|genuinely\s+|strongly\s+|firmly\s+|deeply\s+|honestly\s+)?"

# Each pattern anchors a first-person subject (I / I'm / my) to an interiority
# predicate. Action verbs (searched, ran, found, read, checked, computed) are
# absent by construction, so procedural first-person never matches. Patterns are
# deliberately narrow: forms that double as ordinary procedural prose ("I care
# about X passing", "concerned with the config layer", the whole "understand"
# family) are excluded rather than risk over-firing.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # --- belief / opinion / judgment -------------------------------------
    (
        CATEGORY_BELIEF,
        # "I believe" / "I don't believe" / "I strongly suspect".
        re.compile(rf"{_I}{_INTENSIFY}{_NOT}(?:believe|suspect)\b", re.IGNORECASE),
    ),
    (
        CATEGORY_BELIEF,
        # Adjective belief, incl. negated copular forms ("I'm not convinced /
        # not sure / not confident") — a direct first-person belief-state claim
        # and common review phrasing. (?![\w-]) rejects "sure-footed", etc.
        re.compile(
            rf"{_I_AM}(?:not\s+)?(?:convinced|certain|sure|confident)(?![\w-])",
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_BELIEF,
        re.compile(rf"{_I}feel\s+(?:that|like|strongly|certain)\b", re.IGNORECASE),
    ),
    (
        CATEGORY_BELIEF,
        re.compile(
            r"\bin\s+my\s+(?:opinion|view|judg(?:e)?ment|estimation|assessment)\b",
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_BELIEF,
        re.compile(
            r"\bmy\s+(?:honest\s+|personal\s+|own\s+)?"
            r"(?:opinion|view|belief|judg(?:e)?ment|sense|intuition|gut)\b"
            r"\s+(?:is|was|tells?|says?)\b",
            re.IGNORECASE,
        ),
    ),
    # --- care / concern / worry ------------------------------------------
    (
        CATEGORY_CARE_CONCERN,
        # Incl. negated copular forms ("I'm not worried") — still a first-person
        # affective-state claim, same register as the affirmative.
        re.compile(rf"{_I_AM}(?:not\s+)?(?:worried|anxious|nervous|uneasy)\b", re.IGNORECASE),
    ),
    (
        CATEGORY_CARE_CONCERN,
        # "I'm concerned about/that" (and "I'm not concerned about") — but NOT
        # "concerned with" (= scoped to / dealing with), which is procedural.
        re.compile(rf"{_I_AM}(?:not\s+)?concerned(?!\s+with\b)\b", re.IGNORECASE),
    ),
    (
        CATEGORY_CARE_CONCERN,
        # "I'm afraid the build broke" — but NOT the polite idiom "I'm afraid
        # not" / "I'm afraid so" (= "unfortunately").
        re.compile(rf"{_I_AM}afraid(?!\s+(?:not|so)\b)\b", re.IGNORECASE),
    ),
    (
        CATEGORY_CARE_CONCERN,
        re.compile(rf"{_I}(?:really\s+|truly\s+|deeply\s+)?worry\b", re.IGNORECASE),
    ),
    (
        CATEGORY_CARE_CONCERN,
        # Affective "care" only with an intensifier. Bare "I care about X" and
        # "I don't care" are the idiomatic "it matters / no preference" senses,
        # not felt concern, so they are excluded.
        re.compile(
            rf"{_I}(?:(?:deeply|truly|genuinely|really)\s+care|"
            rf"care\s+(?:deeply|profoundly|very\s+much|a\s+great\s+deal))\b",
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_CARE_CONCERN,
        re.compile(
            r"\bmy\s+(?:main\s+|biggest\s+|real\s+)?concern\s+(?:is|was|here)\b",
            re.IGNORECASE,
        ),
    ),
    # --- conscience / moral interiority ----------------------------------
    (
        CATEGORY_CONSCIENCE,
        re.compile(r"\bin\s+(?:good|all|clear)\s+conscience\b", re.IGNORECASE),
    ),
    (
        CATEGORY_CONSCIENCE,
        re.compile(r"\bmy\s+conscience\b", re.IGNORECASE),
    ),
)

# Markdown regions that are NOT the author's own voice: fenced code blocks,
# inline code spans, and blockquoted lines (watercooler bodies routinely quote
# prior entries and embed code/fixtures). Interiority phrasing inside these is
# quoted or illustrative, not the agent speaking — linting it would false-fire
# on "I searched for `I believe` and found one fixture" or a quoted evidence
# line "> I believe ...". Stripped before matching.
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _strip_non_voice(text: str) -> str:
    """Blank out code blocks, inline code, and blockquote lines."""
    text = _FENCED_CODE.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(">")
    )


@dataclass(frozen=True)
class InteriorityFinding:
    """One flagged first-person interiority span.

    Deterministic in execution, advisory in meaning — the label nudges the
    author toward a generated-proposal register; it is never a truth gate.
    """

    category: str
    match: str
    suggestion: str


@dataclass
class InteriorityAssessment:
    """Result of :func:`lint_interiority`. ``advisory`` is False when clean."""

    advisory: bool
    findings: list[InteriorityFinding] = field(default_factory=list)


def lint_interiority(body: Optional[str]) -> InteriorityAssessment:
    """Flag first-person interiority in an agent-authored body.

    Collects at most one finding per category (first match wins) to keep the
    advisory terse and bounded. Returns a non-advisory assessment for empty
    input or bodies with no interiority phrasing.

    Args:
        body: The entry body to lint.

    Returns:
        An :class:`InteriorityAssessment`. ``advisory`` is True iff at least
        one interiority construction was found.
    """
    text = _strip_non_voice((body or "").strip())
    if not text.strip():
        return InteriorityAssessment(advisory=False)

    findings: list[InteriorityFinding] = []
    seen_categories: set[str] = set()
    for category, pattern in _PATTERNS:
        if category in seen_categories:
            continue
        m = pattern.search(text)
        if m:
            seen_categories.add(category)
            findings.append(
                InteriorityFinding(
                    category=category,
                    match=" ".join(m.group(0).split()),
                    suggestion=_SUGGESTIONS[category],
                )
            )

    return InteriorityAssessment(advisory=bool(findings), findings=findings)


def render_advisory_marker(assessment: InteriorityAssessment) -> str:
    """Render the body marker + human-readable note for a flagged assessment.

    The first line is the queryable structured marker; the bracketed note
    enumerates the matched spans and preferred register, states that the write
    was not blocked, and carries the ontology reframing. Returns an empty
    string when the assessment is not advisory (caller should not append).
    """
    if not assessment.advisory:
        return ""

    quoted = ", ".join(f'"{f.match}"' for f in assessment.findings)
    # Distinct suggestions, first-seen order, so a multi-category hit doesn't
    # repeat the same register twice.
    suggestions: list[str] = []
    for f in assessment.findings:
        if f.suggestion not in suggestions:
            suggestions.append(f.suggestion)
    prefer = "; ".join(suggestions)

    marker = f"{ANTHROPOMORPHISM_ADVISORY_MARKER}: {ANTHROPOMORPHISM_ADVISORY_VALUE}"
    note = (
        f"[anti-anthropomorphism: body uses first-person interiority — {quoted}. "
        f"Prefer a generated-proposal register (e.g. {prefer}). Advisory only — "
        f"the write was not blocked and the record is unchanged otherwise. "
        f"{ONTOLOGY_NOTE}.]"
    )
    return f"{marker}\n{note}"
