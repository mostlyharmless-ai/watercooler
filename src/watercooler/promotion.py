"""Candidate promotion — append-only human-authorized lift from candidate Notes.

Phase 1b helper from the authority-ladder proposal (v0.10 §Phase 1b "Minimal
Phase 1 promotion helper"). Without this helper, humans and agents copy
candidate body text into a durable entry by hand, losing provenance.

Supported targets:
- ``target_type="Decision"`` promotes a candidate decision into a Decision.
- ``target_type="Learning"`` promotes a learning candidate into a durable
  ``## Lesson`` Note. Closure / Supersession / Plan / StatusChange need
  target-specific validators before they can be added.

Promotion is append-only:
1. A promoted Decision or lesson Note is appended to the candidate's thread,
   carrying forward the target-specific provenance and human authorizer.
2. A ``CandidateDisposition`` ``Note`` is appended to the same thread,
   referencing the candidate and the promoted entry. The candidate Note itself
   is never edited (per ``feedback_thread_recapitulation`` discipline and v0.10
   §10.5 Note-convention requirements).

This module is pure — it parses candidate body markers, constructs the new
entry bodies, and returns a ``PromotionPlan`` describing the two entries to
write. The MCP tool and CLI compose this with the canonical write path.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from . import authority_support


VALID_TARGET_TYPES: frozenset[str] = frozenset({"Decision", "Learning"})

# Target types that carry a promotion VALIDATOR (the L3 guards in
# ``validate_candidate_for_promotion``) even when they do not plan a Decision/Learning
# entry. "Supersession" ratifies into an append-only ``xref_supersedes`` annotation but
# must pass the same authorizer-scrub / needs_human_confirmation / double-promotion guards
# (review #1041). It is intentionally NOT in ``VALID_TARGET_TYPES`` — ``plan_promotion``
# must not try to build an entry for it.
_VALIDATABLE_TARGET_TYPES: frozenset[str] = VALID_TARGET_TYPES | {"Supersession"}

# Entry type each promotion target writes. A Decision candidate promotes to a
# Decision entry; a Learning candidate promotes to a Note carrying a ``## Lesson``
# heading (so the Learnings daemon's in-thread-lesson signal retires the
# capture-gap) — a human-confirmed lesson, never a governance act.
_TARGET_ENTRY_TYPE: dict[str, str] = {"Decision": "Decision", "Learning": "Note"}


class PromotionError(ValueError):
    """Raised when a candidate cannot be promoted."""


@dataclass
class CandidateMetadata:
    """Parsed body markers from a candidate Note.

    Only fields needed for promotion carry-forward are surfaced. Unknown
    markers are tolerated and ignored — the candidate body remains the source
    of truth.
    """

    candidate_entry_id: str
    candidate_topic: str
    candidate_type: Optional[str] = None
    candidate_status: Optional[str] = None
    surface_kind: Optional[str] = None
    confidence: Optional[int] = None
    failed_gates: list[str] = field(default_factory=list)
    quote_evidence_status: Optional[str] = None
    source_entry_id: Optional[str] = None
    source_entry_type: Optional[str] = None
    moral_delegation_warning: bool = False
    moral_delegation_reason: Optional[str] = None
    decision_statement: Optional[str] = None
    why_section: Optional[str] = None
    evidence_quotes: list[str] = field(default_factory=list)
    # Learning-candidate sections (Candidate-Type: Learning). Parsed alongside the
    # decision fields; only the target-relevant ones are validated/rendered.
    lesson_statement: Optional[str] = None
    root_cause: Optional[str] = None
    fix: Optional[str] = None
    raw_body: str = ""


@dataclass
class PromotionPlan:
    """Two-entry write plan produced by ``plan_promotion``."""

    decision_title: str
    decision_body: str
    decision_entry_type: str  # "Decision" or "Note"
    disposition_title: str
    disposition_body: str
    disposition_entry_type: str  # "Note"
    topic: str
    candidate_entry_id: str
    # §6 tether read-model for promoted Decisions (#896 Leg 2). None for
    # Learning promotions or when the Decision promotion derived no warrant.
    # Passed to the write as ``support_fields``.
    decision_support_fields: Optional[dict] = None


# ---------------------------------------------------------------------------
# Body parsing
# ---------------------------------------------------------------------------


_MARKER_PATTERNS: dict[str, re.Pattern[str]] = {
    "Candidate-Type": re.compile(r"^Candidate-Type:\s*(.+?)\s*$", re.MULTILINE),
    "Candidate-Status": re.compile(r"^Candidate-Status:\s*(.+?)\s*$", re.MULTILINE),
    "Surface-Kind": re.compile(r"^Surface-Kind:\s*(.+?)\s*$", re.MULTILINE),
    "Confidence": re.compile(r"^Confidence:\s*(\d+)\s*/\s*\d+\s*$", re.MULTILINE),
    "Failed-Gates": re.compile(r"^Failed-Gates:\s*(.+?)\s*$", re.MULTILINE),
    "Quote-Evidence-Status": re.compile(
        r"^Quote-Evidence-Status:\s*(.+?)\s*$", re.MULTILINE
    ),
    "Source-Entry": re.compile(r"^Source-Entry:\s*(.+?)\s*$", re.MULTILINE),
    "Source-Entry-Type": re.compile(
        r"^Source-Entry-Type:\s*(.+?)\s*$", re.MULTILINE
    ),
    "Moral-Delegation-Warning": re.compile(
        r"^Moral-Delegation-Warning:\s*(.+?)\s*$", re.MULTILINE
    ),
    "Moral-Delegation-Reason": re.compile(
        r"^Moral-Delegation-Reason:\s*(.+?)\s*$", re.MULTILINE
    ),
}


# Markers on `CandidateDisposition` Notes (per
# format_candidate_disposition_body).
_DISPOSITION_TARGET_RE = re.compile(
    r"^Disposition-Target:\s*([0-9A-HJKMNP-TV-Z]{26})\s*$", re.MULTILINE
)
_DISPOSITION_KIND_RE = re.compile(
    r"^CandidateDisposition:\s*(\S+)\s*$", re.MULTILINE
)

# A promoted entry stamps `Promoted-From: <candidate ULID>` in its body. The
# marker is unique to promotions — a
# candidate Note carries `Source-Entry`, a disposition Note carries `Promoted-To`
# — so it identifies an existing promotion of a given candidate even when the
# paired `CandidateDisposition` Note never got written (#886).
_PROMOTED_FROM_RE = re.compile(
    r"^Promoted-From:\s*([0-9A-HJKMNP-TV-Z]{26})\s*$", re.MULTILINE
)
# Every genuine promoted entry also carries this authority marker. Requiring
# it alongside `Promoted-From` makes the idempotency match precise and raises the
# bar against a planted entry that carries only a lone `Promoted-From:` line to
# grief a candidate's promotion (the markers match anywhere in the body, and on
# an append-only thread a planted block can never be removed). This is parity
# with the disposition guard, not full anti-spoofing — server-side write
# enforcement is the deferred Phase 1a boundary.
#
# Dedup correctness depends on this regex staying byte-identical to the
# `Authority-Basis: human_promoted` line that ``format_promotion_decision_body``
# emits; the two are pinned together by the tests in test_promotion.py. If that
# emitted value ever changes, update both.
_PROMOTION_BASIS_RE = re.compile(
    r"^Authority-Basis:\s*human_promoted\s*$", re.MULTILINE
)
# A genuine promotion's first body line is its promotion Spec
# (`decision-extractor-promoted` for a Decision, `learnings-promoted` for a
# learning's `## Lesson` Note). Used to identify a promoted *Note* (learning
# target) precisely — the entry-type filter alone can't, since Notes are generic,
# so a Note merely quoting the authority markers in prose must not false-block.
_PROMOTED_SPEC_RE = re.compile(r"^Spec:\s*\S*-promoted\s*$", re.MULTILINE)


def _extract_disposition_target(disp_entry: dict) -> Optional[str]:
    body = disp_entry.get("body", "") or ""
    m = _DISPOSITION_TARGET_RE.search(body)
    return m.group(1) if m else None


def _extract_disposition_kind(disp_entry: dict) -> Optional[str]:
    body = disp_entry.get("body", "") or ""
    m = _DISPOSITION_KIND_RE.search(body)
    return m.group(1).lower() if m else None


def _extract_promoted_from(entry: dict) -> Optional[str]:
    """Return the candidate ULID a promoted entry was lifted from, if any.

    A match requires both the ``Promoted-From: <ULID>`` marker and the
    ``Authority-Basis: human_promoted`` marker that every genuine promotion
    carries — a lone ``Promoted-From`` line (e.g. quoted in prose, or planted to
    grief a promotion) does not match. A ``Decision`` is identified by its entry
    type. A learning promotion is a ``Note`` (the ``## Lesson`` entry), which the
    type filter can't distinguish from any other Note — so for a non-Decision
    entry the promotion ``Spec`` marker (``*-promoted``) is additionally required,
    so a Note merely quoting the authority markers in prose does not false-block.
    When ``entry_type`` is absent the markers are trusted on their own (blocking is
    the fail-safe direction for a double-write guard).
    """
    entry_type = (entry.get("entry_type") or "").strip()
    body = entry.get("body", "") or ""
    if not _PROMOTION_BASIS_RE.search(body):
        return None
    if entry_type and entry_type != "Decision" and not _PROMOTED_SPEC_RE.search(body):
        return None
    m = _PROMOTED_FROM_RE.search(body)
    return m.group(1) if m else None


def _extract_first_paragraph(body: str, heading: str) -> Optional[str]:
    """Return the text under a ``## <heading>`` section up to the next heading.

    Tolerant of body variation (mirrors the decision-statement extractor). Used
    for the learning-candidate sections. Returns None if the section is absent or
    its only content is the synthesizer's ``(none)`` placeholder.
    """
    m = re.search(
        rf"^##\s+{re.escape(heading)}\s*\n+([^\n#][^\n]*(?:\n[^#\n][^\n]*)*)",
        body,
        re.MULTILINE,
    )
    if not m:
        return None
    text = m.group(1).strip()
    if not text or text == "(none)":
        return None
    return text


def parse_candidate_body(
    body: str, candidate_entry_id: str, candidate_topic: str
) -> CandidateMetadata:
    """Parse the body of a candidate Note into structured metadata.

    Recognized markers (per ``format_candidate_note_body`` in
    ``decision_extraction.py``):
        - ``Candidate-Type``, ``Candidate-Status``, ``Surface-Kind``
        - ``Confidence: N/5``, ``Failed-Gates: g4_rationale, ...``
        - ``Quote-Evidence-Status: weak_unverified | verified``
        - ``Source-Entry: <ULID>``
        - ``Source-Entry-Type: Decision | Closure | Supersession`` (#881; carries
          record-state provenance forward so promotion can re-derive it)
        - ``Moral-Delegation-Warning: true`` (#880), ``Moral-Delegation-Reason: ...``

    Section parsing (best-effort, tolerant of body variations):
        - ``## Candidate Decision`` — first paragraph becomes ``decision_statement``
        - ``## Why this is a candidate, not a Decision`` — first paragraph becomes
          ``why_section``
        - ``## Evidence`` / ``## Evidence (unverified)`` — blockquote lines become
          ``evidence_quotes``
    """
    meta = CandidateMetadata(
        candidate_entry_id=candidate_entry_id,
        candidate_topic=candidate_topic,
        raw_body=body,
    )

    for key, pattern in _MARKER_PATTERNS.items():
        m = pattern.search(body)
        if not m:
            continue
        value = m.group(1).strip()
        if key == "Candidate-Type":
            meta.candidate_type = value
        elif key == "Candidate-Status":
            meta.candidate_status = value
        elif key == "Surface-Kind":
            meta.surface_kind = value
        elif key == "Confidence":
            try:
                meta.confidence = int(value)
            except ValueError:
                meta.confidence = None
        elif key == "Failed-Gates":
            if value.lower() in ("none", ""):
                meta.failed_gates = []
            else:
                meta.failed_gates = [g.strip() for g in value.split(",") if g.strip()]
        elif key == "Quote-Evidence-Status":
            meta.quote_evidence_status = value
        elif key == "Source-Entry":
            meta.source_entry_id = value
        elif key == "Source-Entry-Type":
            meta.source_entry_type = value
        elif key == "Moral-Delegation-Warning":
            meta.moral_delegation_warning = value.strip().lower() == "true"
        elif key == "Moral-Delegation-Reason":
            meta.moral_delegation_reason = value

    # Decision statement — first non-empty line after ## Candidate Decision
    cd_match = re.search(
        r"^##\s+Candidate Decision\s*\n+([^\n#][^\n]*(?:\n[^#\n][^\n]*)*)",
        body,
        re.MULTILINE,
    )
    if cd_match:
        meta.decision_statement = cd_match.group(1).strip()

    # Why this is a candidate — first paragraph after the header
    why_match = re.search(
        r"^##\s+Why this is a candidate,?\s*not a Decision\s*\n+"
        r"([^\n#][^\n]*(?:\n[^#\n][^\n]*)*)",
        body,
        re.MULTILINE,
    )
    if why_match:
        meta.why_section = why_match.group(1).strip()

    # Evidence quotes — blockquote lines after ## Evidence, ## Evidence
    # (unverified) [decision candidates], or ## Evidence (verbatim) [learning
    # candidates, per format_learning_candidate_body].
    ev_match = re.search(
        r"^##\s+Evidence(?:\s+\((?:unverified|verbatim)\))?\s*\n([\s\S]*?)(?:^##\s|\Z)",
        body,
        re.MULTILINE,
    )
    if ev_match:
        section = ev_match.group(1)
        for line in section.splitlines():
            stripped = line.strip()
            if stripped.startswith("> "):
                meta.evidence_quotes.append(stripped[2:].strip())
            elif stripped.startswith(">"):
                meta.evidence_quotes.append(stripped[1:].strip())

    # Learning-candidate sections (Candidate-Type: Learning, per
    # format_learning_candidate_body): lesson statement + root cause + fix.
    meta.lesson_statement = _extract_first_paragraph(body, "Candidate learning")
    meta.root_cause = _extract_first_paragraph(body, "Root cause")
    meta.fix = _extract_first_paragraph(body, "Fix")

    return meta


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_candidate_for_promotion(
    meta: CandidateMetadata,
    target_type: str,
    human_authorized_by: str,
    *,
    existing_thread_entries: Optional[list[dict[str, Any]]] = None,
) -> None:
    """Raise PromotionError if the candidate cannot be promoted as requested.

    Args:
        existing_thread_entries: Optional list of entries already on the
            candidate's thread (the caller passes all of them; this function
            filters by marker). When supplied, two append-only double-promotion
            guards run over the list:

            1. **Disposition guard** — any ``CandidateDisposition`` Note whose
               ``Disposition-Target`` references ``meta.candidate_entry_id`` and
               whose ``CandidateDisposition`` marker is ``promoted``/``rejected``
               blocks re-promotion. The ``Candidate-Status`` check on the
               candidate body alone is insufficient because promotion is
               append-only (the candidate body is never mutated, so its status
               stays ``needs_human_confirmation`` forever).
            2. **Promoted-entry guard (#886)** — any promoted entry carrying
               ``Promoted-From: <candidate>`` blocks re-promotion even when *no*
               matching disposition exists. Promotion writes the promoted entry
               then a disposition non-atomically; if the disposition write fails
               after the promoted entry commits, the disposition guard alone
               would pass and a re-run would write a duplicate promotion from
               one candidate.

    Both guards are read-then-write checks against a snapshot of the thread, so
    they are TOCTOU-safe only for *serialized* retries (the case #886 targets:
    re-running after a partial failure). Two promotions of the same candidate
    racing before either commits can still both pass; eliminating that needs the
    append-only store to gain a multi-entry transaction primitive, which it lacks.
    """
    if target_type not in _VALIDATABLE_TARGET_TYPES:
        raise PromotionError(
            f"target_type={target_type!r} is not supported. "
            f"Only {sorted(_VALIDATABLE_TARGET_TYPES)} carry a promotion validator. "
            f"Closure / Plan / StatusChange still need target-specific validators."
        )

    # Validate the *scrubbed* authorizer, not the raw string: a value made up
    # only of characters the scrub removes (markup like ``<>``, zero-width /
    # control chars) is non-empty raw but scrubs to "". Accepting it would let a
    # promotion (and the "ownership satisfied" carry-forward on a moral-delegation
    # warned candidate) record an EMPTY authorizer and omit human_authorized_by
    # from the authority fields — bypassing the ownership requirement this is
    # meant to enforce. Validate what will actually be persisted.
    if not scrub_authority_identifier(human_authorized_by):
        raise PromotionError(
            "human_authorized_by is required for promotion — promotion is a "
            "Level 3 act and must carry the authorizing human's identifier "
            "(a value that survives scrubbing: namespace-qualified, not a "
            "control/markup-only string)."
        )

    # Block double-promotion via append-only thread lookup. The candidate's own
    # body never transitions to "promoted" by design (it's immutable), so the
    # Candidate-Status check below is preserved only as a guard for the unusual
    # case where some other process did edit the body. The real guards scan the
    # thread for (1) a prior disposition and (2) a prior promoted entry.
    # `is not None` (not truthiness): an empty list means "thread state supplied,
    # nothing to match"; None means "caller opted out of the check" (unit tests
    # of pure body construction). Production callers always supply the list.
    if existing_thread_entries is not None:
        for entry in existing_thread_entries:
            target = _extract_disposition_target(entry)
            kind = _extract_disposition_kind(entry)
            if (
                target == meta.candidate_entry_id
                and kind in ("promoted", "rejected")
            ):
                raise PromotionError(
                    f"candidate {meta.candidate_entry_id} already has a "
                    f"CandidateDisposition Note with kind={kind!r} on the "
                    f"thread (disposition entry id "
                    f"{entry.get('entry_id', '?')!r}); re-promotion would "
                    f"silently duplicate the Level-3 act. Append-only: the "
                    f"candidate body itself stays "
                    f"`Candidate-Status: needs_human_confirmation` forever, "
                    f"so the body check alone is insufficient."
                )

            # #886: a promoted entry already exists for this candidate even
            # though no disposition was found — the disposition write failed
            # after the promoted entry committed. Re-promotion would append a
            # duplicate promoted entry. Reconcile by appending the missing
            # disposition Note, not by re-running promotion.
            if _extract_promoted_from(entry) == meta.candidate_entry_id:
                raise PromotionError(
                    f"candidate {meta.candidate_entry_id} already has a "
                    f"promoted entry on the thread (entry id "
                    f"{entry.get('entry_id', '?')!r}) carrying "
                    f"`Promoted-From: {meta.candidate_entry_id}`, but no "
                    f"matching CandidateDisposition Note was found — the "
                    f"disposition write likely failed after the promoted entry "
                    f"committed (#886). Re-promotion would write a duplicate "
                    f"promoted entry; reconcile by appending the "
                    f"missing CandidateDisposition Note instead."
                )

    if meta.candidate_status and meta.candidate_status.lower() not in (
        "needs_human_confirmation",
        "needs human confirmation",
    ):
        raise PromotionError(
            f"candidate Candidate-Status={meta.candidate_status!r} is not "
            f"'needs_human_confirmation'. Already-promoted or rejected "
            f"candidates cannot be re-promoted."
        )

    if target_type == "Learning":
        if not meta.lesson_statement:
            raise PromotionError(
                "candidate body has no '## Candidate learning' section with "
                "extractable text. Cannot construct a durable lesson from an "
                "empty candidate learning."
            )
    elif target_type == "Decision" and not meta.decision_statement:
        # Supersession carries no statement body — it ratifies an edge into an
        # xref_supersedes annotation, so this Decision-only extract check is skipped.
        raise PromotionError(
            "candidate body has no '## Candidate Decision' section with "
            "extractable text. Cannot construct a Decision from an empty "
            "candidate statement."
        )


# ---------------------------------------------------------------------------
# Body construction
# ---------------------------------------------------------------------------


def _format_failed_gates(failed_gates: list[str]) -> str:
    if not failed_gates:
        return "none"
    return ", ".join(failed_gates)


def _format_evidence_block(
    quotes: list[str], evidence_status: Optional[str]
) -> str:
    if not quotes:
        return "(no evidence quotes carried forward from candidate)"
    header = (
        "## Evidence (carried forward, unverified at extraction time)"
        if evidence_status == "weak_unverified"
        else "## Evidence (carried forward)"
    )
    lines = [header]
    if evidence_status == "weak_unverified":
        lines.append(
            "_The candidate marked these quotes as unverified at extraction "
            "time. The promoting human has reviewed them; treat as confirmed "
            "by the authorizing identifier below._"
        )
    for q in quotes:
        lines.append(f"> {q}")
    return "\n".join(lines)


_NEWLINE_SCRUB = str.maketrans({"\r": " ", "\n": " "})


def _scrub_marker_value(value: str) -> str:
    """Scrub control bytes that would corrupt body markers or commit footers.

    CR/LF in a marker value forges multi-line bodies (an attacker writes
    ``caleb\\nHuman-Authorized-By: someone-else`` and the parser sees two
    separate authorization lines). Replace newlines with spaces and strip.
    """
    return value.translate(_NEWLINE_SCRUB).strip()


# Max length for the durable, federation-visible human_authorized_by identifier.
# Matches the entry_schema.json maxLength so the scrubbed value always validates.
_HUMAN_AUTHORIZED_BY_MAXLEN = 256


def scrub_authority_identifier(value: Optional[str]) -> str:
    """Sanitize a ``human_authorized_by`` identifier for durable graph metadata.

    The value lands in an append-only, git-committed, federation-visible record and
    cannot be redacted later, so it is scrubbed conservatively at the write boundary:

    - Unicode **format** chars (``Cf`` — zero-width joiners, and crucially the bidi
      overrides ``U+202A–202E`` / ``U+2066–2069`` that enable visual identity spoofing
      of the displayed authorizer) are dropped.
    - Unicode **control** and **separator** chars (``Cc``/``Cs``/``Co``/``Z*`` — CR/LF,
      tab, NUL, ``U+2028``/``U+2029``, NBSP, …) collapse to a single space, closing the
      marker/footer line-forgery vector beyond just CR/LF.
    - Angle-bracket markup is stripped; internal whitespace runs collapse to one space;
      length is bounded to the schema ``maxLength``.

    Callers should pass a namespace-qualified identifier (e.g. ``github:<login>``), never
    a bare email. Returns ``""`` for empty or whitespace-only input.

    Out of scope: Unicode homoglyph confusability (e.g. Cyrillic ``а`` for Latin ``a``) is
    not normalized away — identifiers are namespace-qualified for *exact-match* queries,
    not visual trust, so a homoglyph variant is simply a different (auditable) identifier,
    not an identity spoof of an existing one.

    Args:
        value: Raw identifier (may be ``None``).

    Returns:
        Scrubbed identifier, or ``""`` when nothing usable remains.
    """
    if not value:
        return ""
    out: list[str] = []
    for ch in value:
        if ch in "<>":
            continue
        category = unicodedata.category(ch)
        if category == "Cf":
            # Format chars (zero-width, bidi overrides) — drop entirely.
            continue
        if category[0] in ("C", "Z"):
            # Other control/separator chars — collapse to a space.
            out.append(" ")
            continue
        out.append(ch)
    # Collapse internal whitespace runs and strip ends so the durable, federation-
    # visible value is stable for exact-match queries. Re-strip after truncation so
    # the function is idempotent even when the maxLength boundary lands on a space
    # (otherwise a trailing space would survive one pass and be stripped on the
    # next, diverging the .md projection from the re-scrubbed graph field).
    cleaned = " ".join("".join(out).split())
    return cleaned[:_HUMAN_AUTHORIZED_BY_MAXLEN].strip()


ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def build_promotion_authority_fields(
    *,
    human_authorized_by: str,
    source_entry_id: Optional[str] = None,
    actor_class: Optional[str] = None,
    target_type: str = "Decision",
) -> dict[str, str]:
    """Graph authority metadata for a human-promoted entry.

    Shared by the MCP (`watercooler_promote_candidate`) and CLI (`promote-candidate`)
    promote paths so both persist the *same* queryable shape — otherwise a query
    filtering on ``decision_origin``/``human_authorized_by`` would miss CLI-promoted
    Decisions.

    ``authority_basis`` is always ``human_promoted``. ``decision_origin`` is
    *decision-specific* provenance, so it is stamped only when ``target_type ==
    "Decision"`` — a promoted **learning** lesson is a ``Note``, not a Decision, and
    must not claim a ``decision_origin`` (``list_decisions`` scopes to
    ``entry_type == "Decision"`` regardless, so this is correctness-of-meaning, not
    a query fix). ``human_authorized_by`` is scrubbed for the durable,
    federation-visible record. ``source_entry_id`` is included only when it is a
    valid ULID (that schema field is ULID-typed). ``actor_class`` is set only when
    the caller can name the writer honestly: the MCP path passes ``"agent"`` (an
    agent executes the write); the CLI path leaves it unset rather than guess.

    Args:
        human_authorized_by: Accountable-human identifier (scrubbed here).
        source_entry_id: Candidate entry ULID (dropped if not ULID-shaped).
        actor_class: Optional writer class to stamp.
        target_type: ``"Decision"`` (stamps ``decision_origin``) or ``"Learning"``
            (omits it). Defaults to Decision for back-compat.

    Returns:
        Authority-fields dict suitable for ``append_entry(authority_fields=...)``.
    """
    fields: dict[str, str] = {"authority_basis": "human_promoted"}
    if target_type == "Decision":
        fields["decision_origin"] = "human_promoted"
    if actor_class:
        fields["actor_class"] = actor_class
    authorizer = scrub_authority_identifier(human_authorized_by)
    if authorizer:
        fields["human_authorized_by"] = authorizer
    if source_entry_id and ULID_PATTERN.match(source_entry_id):
        fields["source_entry_id"] = source_entry_id
    return fields


def _reverification_label(quote_verified: Optional[bool]) -> str:
    """Marker value for ``Quote-Reverified-At-Promotion`` (#887)."""
    if quote_verified is True:
        return "reverified"
    if quote_verified is False:
        return "not_reverified"
    return "not_checked"


def _quote_reverification_reason_marker(reason: Optional[str]) -> Optional[str]:
    """Safe marker value for ``Quote-Reverification-Reason``."""
    if not reason:
        return None
    marker = re.sub(r"[^a-z0-9_:-]+", "_", reason.strip().lower()).strip("_")
    return marker[:80] or None


def _warrant_revalidation_note(
    quote_verified: Optional[bool],
    quote_reverification_reason: Optional[str] = None,
) -> str:
    """Honest rendered note describing the promotion-time quote re-validation (#887).

    The note must state truthfully whether quotes were re-checked against the live
    source, so a reader never mistakes withheld support for a verification failure
    (or vice versa).
    """
    if quote_verified is True:
        return (
            "_Support note: the candidate's evidence quotes were re-validated "
            "against the live source entry at promotion and matched; the "
            "source/record-state support above reflects that re-validation. "
            "Tethers describe record/source support, never world-truth._"
        )
    if quote_verified is False:
        if quote_reverification_reason == "quote_below_minimum_length":
            return (
                "_Support note: the candidate's evidence quotes matched the live "
                "source entry at promotion, but at least one matched quote was "
                "too short to count as durable source support. Source/record-state "
                "support is withheld. The promotion rests on the authorizing "
                "human (user tether)._"
            )
        return (
            "_Support note: the candidate's evidence quotes did NOT confirm "
            "against the live source entry at promotion (unmatched quote, or the "
            "source was unreadable), so source/record-state support is withheld. "
            "The promotion rests on the authorizing human (user tether)._"
        )
    return (
        "_Support note: the candidate's evidence quotes were NOT re-validated "
        "against the live source entry at promotion, so source/record-state "
        "support is withheld here regardless of the candidate's self-asserted "
        "status. The promotion rests on the authorizing human (user tether)._"
    )


def promotion_warrant(
    meta: CandidateMetadata,
    *,
    human_authorized_by: str,
    quote_verified: Optional[bool] = None,
    source_entry_type: Optional[str] = None,
) -> authority_support.WarrantReadModel:
    """The §6 warrant read-model for a promoted Decision (#896 Leg 2).

    Single source for both the promotion body's support section and the structured
    tether entry-fields. Re-derived against the live source (#887): source /
    record_state support is granted only from re-validated values, and the
    promoting human is conferred as ``user`` support.
    """
    return authority_support.derive_candidate_support(
        source_entry_id=meta.source_entry_id,
        verbatim_quotes=meta.evidence_quotes,
        quote_verified=quote_verified is True,
        human_authorized_by=scrub_authority_identifier(human_authorized_by),
        source_entry_type=source_entry_type,
        moral_delegation_warning=meta.moral_delegation_warning,
    )


def format_promotion_decision_body(
    meta: CandidateMetadata,
    *,
    human_authorized_by: str,
    quote_verified: Optional[bool] = None,
    quote_reverification_reason: Optional[str] = None,
    source_entry_type: Optional[str] = None,
    edits: Optional[dict] = None,
) -> str:
    """Construct the Decision body for a promoted candidate.

    Carry-forward fields per v0.10 §Phase 1b spec:
        - ``promoted_from``: the candidate entry ID
        - ``source_entry_id``: the original source entry (from the candidate)
        - ``authority_source``: ``human``
        - ``human_authorized_by``: the authorizer
        - evidence quotes, confidence, failed_gates from candidate
        - ``authority_basis``: ``human_promoted`` (v0.10 §5.4.1 lint)

    Args:
        quote_verified: Result of re-validating the candidate's evidence quotes
            against the *live* source entry at promotion (#887). ``source`` /
            ``record_state`` warrant support is granted **only** when this is
            ``True``. ``False`` (re-validation ran and the quotes did not
            confirm, or the source was unreadable) and ``None`` (the promotion
            path did not re-validate — e.g. a direct pure caller) both withhold
            substantive source support, because the candidate's self-asserted
            ``Quote-Evidence-Status`` marker is not trustworthy (a hand-forged
            candidate could claim verified provenance). The composition layers
            (MCP tool + CLI) compute this by fetching the source body and calling
            ``decision_extraction.reverify_quotes_against_source``.
        quote_reverification_reason: Machine-readable reason when
            ``quote_verified`` is ``False``. Used only for audit prose/markers;
            support is still withheld for every false result.
        source_entry_type: The ``entry_type`` of the *live* cited source entry
            (#887), supplied by the composition layer from the fetched source
            node — **not** the candidate's self-asserted ``Source-Entry-Type``
            marker, which a forged candidate could set to ``Decision`` to fake
            ``record_state``. ``record_state`` support requires this to be a
            record-state type (Decision/Closure/Supersession) *and*
            ``quote_verified`` True. ``None`` (source unreadable, or a pure
            caller) withholds ``record_state``.

    ``edits`` may override:
        - ``decision_statement`` (replaces the candidate's decision statement)
        - ``rationale`` (added as a ## Rationale section)
        - ``scope`` (added as a ## Scope section)
    Unknown keys in ``edits`` are ignored.
    """
    edits = edits or {}

    decision_statement = edits.get("decision_statement") or meta.decision_statement
    if not decision_statement:
        decision_statement = "(no decision statement)"

    confidence_line = (
        f"Confidence: {meta.confidence}/5 (from candidate)"
        if meta.confidence is not None
        else "Confidence: (not recorded on candidate)"
    )

    # Scrub the authorizer with the SAME helper used for the graph metadata field, so
    # the body marker and the queryable graph value never diverge for one input (a
    # weaker body-only scrub would let e.g. angle-bracket markup survive in the .md).
    auth = scrub_authority_identifier(human_authorized_by)

    # Precondition guard (defense in depth). plan_promotion always validates the
    # scrubbed authorizer before reaching here, but this is a public function: a
    # direct caller passing a control/markup-only value (e.g. "<>") that scrubs to
    # "" must not be able to render "Ownership is satisfied: ``" or "by ``". The
    # whole body asserts human ownership, so an empty authorizer is meaningless.
    if not auth:
        raise PromotionError(
            "human_authorized_by scrubbed to empty — cannot construct a promoted "
            "Decision body that asserts accountable human ownership. Pass a value "
            "that survives scrubbing (namespace-qualified, not control/markup-only)."
        )

    reason_marker = _quote_reverification_reason_marker(quote_reverification_reason)
    lines = [
        "Spec: decision-extractor-promoted",
        f"Promoted-From: {meta.candidate_entry_id}",
        f"Source-Entry: {meta.source_entry_id or '(not recorded)'}",
        "Authority-Source: human",
        "Authority-Basis: human_promoted",
        f"Human-Authorized-By: {auth}",
        confidence_line,
        f"Failed-Gates-At-Extraction: {_format_failed_gates(meta.failed_gates)}",
        f"Quote-Evidence-Status-At-Extraction: "
        f"{meta.quote_evidence_status or 'verified'}",
        # #887: queryable record of whether the candidate's quotes were
        # re-validated against the LIVE source entry at promotion time (distinct
        # from the extraction-time status above, which the candidate self-asserts).
        f"Quote-Reverified-At-Promotion: "
        f"{_reverification_label(quote_verified)}",
    ]
    if reason_marker and quote_verified is False:
        lines.append(f"Quote-Reverification-Reason: {reason_marker}")
    lines.extend(["", "## Decision", decision_statement])

    rationale = edits.get("rationale")
    if rationale:
        lines.extend(["", "## Rationale", rationale.strip()])

    scope = edits.get("scope")
    if scope:
        lines.extend(["", "## Scope", scope.strip()])

    if meta.why_section:
        lines.extend(
            [
                "",
                "## Original candidate caveat (carried forward)",
                meta.why_section,
            ]
        )

    # Carry forward a moral-delegation warning as *resolved ownership*: the
    # candidate flagged a value judgment, and promotion records that an
    # accountable human (`auth`, required by validate_candidate_for_promotion)
    # now owns it. Procedural — it records ownership, not moral correctness.
    if meta.moral_delegation_warning:
        reason = meta.moral_delegation_reason or (
            "The candidate statement carried a value/ethical judgment."
        )
        lines.extend(
            [
                "",
                "## Moral ownership",
                f"This Decision carried a moral-delegation warning at extraction: "
                f"{reason}",
                "",
                f"Ownership is satisfied: `{auth}` authorized the promotion and "
                f"takes accountability for the value judgment. The warning was a "
                f"procedural ownership check, not a determination that the "
                f"decision is morally correct.",
            ]
        )

    lines.extend(["", _format_evidence_block(meta.evidence_quotes, meta.quote_evidence_status)])

    # Warrant read model, re-derived at promotion. The promoted Decision carries
    # `auth` as user support, so a candidate thin on inference alone becomes
    # user-tethered here. Tethers describe record/source support, never world-truth.
    #
    # #887: source/record_state support is granted ONLY from values re-derived
    # against the live source entry — never the candidate's self-asserted markers.
    # `source` requires quote_verified is True (quotes re-validated against the
    # live source body); `record_state` additionally requires `source_entry_type`
    # (the LIVE source node's type, from the composition layer) to be a
    # record-state type. The candidate's self-asserted Quote-Evidence-Status and
    # Source-Entry-Type cannot launder unverified content into substantive support
    # on the promoted Decision. False/None for either input withholds; the
    # rendered note below states which quote case applies.
    # Single source for both this body's support section and the structured tether
    # entry-fields persisted at the write site (#896 Leg 2). See promotion_warrant().
    warrant = promotion_warrant(
        meta,
        human_authorized_by=human_authorized_by,
        quote_verified=quote_verified,
        source_entry_type=source_entry_type,
    )
    lines.append("")
    lines.extend(authority_support.render_support_section(warrant))
    lines.append("")
    lines.append(
        _warrant_revalidation_note(quote_verified, quote_reverification_reason)
    )

    lines.extend(
        [
            "",
            "## Promotion provenance",
            f"This Decision was promoted from candidate entry "
            f"`{meta.candidate_entry_id}` on thread `{meta.candidate_topic}` by "
            f"`{auth}`. The candidate Note is not edited "
            f"(append-only); a `CandidateDisposition` Note has been appended to "
            f"the same thread to mark the candidate as promoted.",
        ]
    )

    return "\n".join(lines)


def format_promotion_lesson_body(
    meta: CandidateMetadata,
    *,
    human_authorized_by: str,
    edits: Optional[dict] = None,
) -> str:
    """Construct the durable-lesson Note body for a promoted *learning* candidate.

    The promoted entry is a ``Note`` carrying a bare ``## Lesson`` heading — which
    the Learnings daemon's ``_find_in_thread_lesson`` signal matches, so the source
    thread flips to ``has_learning`` and its capture-gap retires. It is a
    human-confirmed lesson (``Authority-Basis: human_promoted``), **not** a
    governance Decision: no §6 warrant/tether, no quote re-validation chain — the
    evidence is carried forward verbatim from the closed thread.

    ``edits`` may override ``lesson`` / ``root_cause`` / ``fix``. Unknown keys are
    ignored. Mirrors ``format_promotion_decision_body``'s authorizer scrubbing.
    """
    edits = edits or {}
    lesson = edits.get("lesson") or meta.lesson_statement
    if not lesson:
        raise PromotionError(
            "cannot construct a durable lesson body from an empty learning "
            "statement (no '## Candidate learning' section and no edits['lesson'])."
        )
    root_cause = edits.get("root_cause") or meta.root_cause
    fix = edits.get("fix") or meta.fix

    # Refine provenance: record which fields the human actually changed (a
    # field counts as edited only when an edit was supplied AND differs from the
    # candidate's original). Evidence is never in `edits` — it is carried
    # verbatim as grounding — so it can never appear here. The marker lets a
    # reader (and the disposition-rate metric) tell a refined lesson from a
    # verbatim promotion, and is the honest record of human authorship over the
    # wording.
    def _edited(key: str, original: Optional[str]) -> bool:
        v = edits.get(key)
        return bool(v) and v.strip() != (original or "").strip()

    edited_fields = [
        name
        for name, original in (
            ("lesson", meta.lesson_statement),
            ("root_cause", meta.root_cause),
            ("fix", meta.fix),
        )
        if _edited(name, original)
    ]

    auth = scrub_authority_identifier(human_authorized_by)
    if not auth:
        raise PromotionError(
            "human_authorized_by scrubbed to empty — cannot construct a promoted "
            "lesson that asserts accountable human ownership. Pass a value that "
            "survives scrubbing (namespace-qualified, not control/markup-only)."
        )

    confidence_line = (
        f"Confidence: {meta.confidence}/5 (from candidate)"
        if meta.confidence is not None
        else "Confidence: (not recorded on candidate)"
    )
    lines = [
        "Spec: learnings-promoted",
        f"Promoted-From: {meta.candidate_entry_id}",
        f"Source-Thread: {meta.candidate_topic}",
        "Authority-Source: human",
        "Authority-Basis: human_promoted",
        f"Human-Authorized-By: {auth}",
    ]
    if edited_fields:
        lines.append(f"Promotion-Edits: {', '.join(edited_fields)}")
    lines += [
        confidence_line,
        "",
        # Bare `## Lesson` heading — matched by the daemon's _find_in_thread_lesson
        # (retires the capture-gap). Keep it bare: extra text on the line breaks
        # the heading regex.
        "## Lesson",
        lesson.strip(),
    ]
    if root_cause:
        lines.extend(["", "## Root cause", root_cause.strip()])
    if fix:
        lines.extend(["", "## Fix", fix.strip()])
    lines.extend(
        ["", _format_evidence_block(meta.evidence_quotes, meta.quote_evidence_status)]
    )
    lines.extend(
        [
            "",
            "## Promotion provenance",
            f"This durable lesson was promoted from learning candidate "
            f"`{meta.candidate_entry_id}` on thread `{meta.candidate_topic}` by "
            f"`{auth}`. The candidate Note is not edited (append-only); a "
            f"`CandidateDisposition` Note marks the candidate as promoted. This is "
            f"a human-confirmed lesson, not a governance Decision.",
        ]
    )
    return "\n".join(lines)


def format_candidate_disposition_body(
    meta: CandidateMetadata,
    *,
    promoted_entry_id: str,
    human_authorized_by: str,
    disposition: str = "promoted",
    promoted_kind: str = "Decision",
) -> str:
    """Construct the body for the CandidateDisposition Note.

    Per v0.10 §10.5 Note-convention discipline. The Note records that the
    candidate was promoted (or rejected, in future use) and points to the new
    entry. ``promoted_kind`` names what it was promoted to (``Decision`` for a
    decision candidate, ``Learning`` for a learning candidate's durable lesson
    Note). The candidate Note itself is not edited.
    """
    # Use the same durable-identifier scrub as the graph field and the Decision body
    # marker, so the authorizer is sanitized identically across every append-only
    # surface (CR/LF, bidi/zero-width, angle-bracket markup, length).
    auth = scrub_authority_identifier(human_authorized_by)
    lines = [
        "Spec: candidate-disposition",
        f"CandidateDisposition: {disposition}",
        f"Disposition-Target: {meta.candidate_entry_id}",
        f"Promoted-To: {promoted_entry_id}",
        f"Disposition-Authorized-By: {auth}",
        "",
        "## Disposition",
        f"Candidate `{meta.candidate_entry_id}` on thread "
        f"`{meta.candidate_topic}` has been **{disposition}** to {promoted_kind} "
        f"`{promoted_entry_id}` by `{auth}`.",
        "",
        "## Why this Note exists",
        "Watercooler threads are append-only. A candidate's status change is "
        "recorded as a separate `CandidateDisposition` Note rather than by "
        "editing the candidate body. Queries that need the candidate's current "
        "disposition should look for the latest `CandidateDisposition` Note "
        "whose `Disposition-Target` matches the candidate's entry ID.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _plan_learning_promotion(
    meta: CandidateMetadata,
    *,
    human_authorized_by: str,
    edits: Optional[dict] = None,
) -> PromotionPlan:
    """Build the two-entry plan for a *learning* promotion (durable ``## Lesson``
    Note + ``CandidateDisposition``). Pure; no warrant/tether (learning ≠ Decision).
    """
    lesson_body = format_promotion_lesson_body(
        meta, human_authorized_by=human_authorized_by, edits=edits
    )
    disposition_body_template = format_candidate_disposition_body(
        meta,
        promoted_entry_id="(promoted_entry_id pending)",
        human_authorized_by=human_authorized_by,
        promoted_kind="Learning",
    )

    raw_title = (edits or {}).get("lesson") or meta.lesson_statement or "Promoted lesson"
    raw_title_oneline = _scrub_marker_value(raw_title)
    lesson_title = raw_title_oneline[:80]
    if len(raw_title_oneline) > 80:
        lesson_title = lesson_title.rstrip() + "…"

    return PromotionPlan(
        decision_title=lesson_title,
        decision_body=lesson_body,
        decision_entry_type=_TARGET_ENTRY_TYPE["Learning"],  # "Note"
        disposition_title=(
            f"Candidate {meta.candidate_entry_id[:6]}… promoted to Learning"
        ),
        disposition_body=disposition_body_template,
        disposition_entry_type="Note",
        topic=meta.candidate_topic,
        candidate_entry_id=meta.candidate_entry_id,
        decision_support_fields=None,  # no §6 warrant for a learning
    )


def plan_promotion(
    *,
    candidate_body: str,
    candidate_entry_id: str,
    candidate_topic: str,
    target_type: str,
    human_authorized_by: str,
    edits: Optional[dict] = None,
    existing_thread_entries: Optional[list[dict[str, Any]]] = None,
    quote_verified: Optional[bool] = None,
    quote_reverification_reason: Optional[str] = None,
    source_entry_type: Optional[str] = None,
) -> PromotionPlan:
    """Plan a promotion of a candidate Note to a supported durable entry.

    Returns a ``PromotionPlan`` describing the promoted entry and the
    ``CandidateDisposition`` Note to write. Pure function — does not perform I/O.

    Args:
        quote_verified: Result of re-validating the candidate's evidence quotes
            against the live source entry (#887); forwarded to
            ``format_promotion_decision_body``. Production callers (MCP tool +
            CLI) compute this from the live source body. ``source``/``record_state``
            warrant support is granted only when it is ``True``.
        quote_reverification_reason: Reason for a false quote revalidation,
            forwarded to ``format_promotion_decision_body`` for audit prose.
        source_entry_type: The live cited source entry's ``entry_type`` (#887),
            forwarded to ``format_promotion_decision_body``. ``record_state``
            support requires it to be a record-state type — the candidate's
            self-asserted marker is not trusted.
        existing_thread_entries: All entries already on the candidate's thread
            (the caller supplies them; this function filters by marker). When
            supplied, blocks double-promotion if a prior ``CandidateDisposition``
            Note (kind ``promoted``/``rejected``) or a prior promoted entry
            carrying ``Promoted-From: <candidate>`` already references
            ``candidate_entry_id`` (raises ``PromotionError``). Pass
            ``None`` (default) to skip the check — useful in unit tests that
            exercise the pure body construction. Production callers (MCP tool +
            CLI) must supply this.

    The MCP tool and CLI compose this with the canonical write path:
        plan = plan_promotion(...)
        promoted_id = say(plan.topic, plan.decision_title, plan.decision_body,
                          entry_type=plan.decision_entry_type, ...)
        disposition_body = format_candidate_disposition_body(
            meta, promoted_entry_id=promoted_id, ...)
        say(plan.topic, plan.disposition_title, disposition_body,
            entry_type="Note", ...)
    """
    meta = parse_candidate_body(candidate_body, candidate_entry_id, candidate_topic)
    validate_candidate_for_promotion(
        meta,
        target_type,
        human_authorized_by,
        existing_thread_entries=existing_thread_entries,
    )

    if target_type == "Learning":
        return _plan_learning_promotion(
            meta, human_authorized_by=human_authorized_by, edits=edits
        )

    decision_body = format_promotion_decision_body(
        meta,
        human_authorized_by=human_authorized_by,
        quote_verified=quote_verified,
        quote_reverification_reason=quote_reverification_reason,
        source_entry_type=source_entry_type,
        edits=edits,
    )

    # Disposition body is built with a placeholder ID; the caller substitutes
    # the real promoted entry ID after the Decision write completes.
    disposition_body_template = format_candidate_disposition_body(
        meta,
        promoted_entry_id="(promoted_entry_id pending)",
        human_authorized_by=human_authorized_by,
    )

    # Derive title from the decision statement; scrub CR/LF so multi-line
    # statements don't put a newline into the markdown projection / commit
    # subject (the same failure mode that PR #846 fixed for the canonical
    # write wrapper — fixing it here keeps the CLI path safe without
    # depending on each caller's title-derivation hygiene).
    raw_title = meta.decision_statement or "Promoted Decision"
    raw_title_oneline = _scrub_marker_value(raw_title)
    decision_title = raw_title_oneline[:80]
    if len(raw_title_oneline) > 80:
        decision_title = decision_title.rstrip() + "…"

    disposition_title = f"Candidate {candidate_entry_id[:6]}… promoted to Decision"

    # Structured tether read-model for the promoted Decision — same warrant the
    # body's support section renders (#896 Leg 2).
    decision_support_fields = promotion_warrant(
        meta,
        human_authorized_by=human_authorized_by,
        quote_verified=quote_verified,
        source_entry_type=source_entry_type,
    ).to_entry_fields()

    return PromotionPlan(
        decision_title=decision_title,
        decision_body=decision_body,
        decision_entry_type="Decision",
        disposition_title=disposition_title,
        disposition_body=disposition_body_template,
        disposition_entry_type="Note",
        topic=candidate_topic,
        candidate_entry_id=candidate_entry_id,
        decision_support_fields=decision_support_fields,
    )
