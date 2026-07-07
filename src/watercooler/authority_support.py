"""Tether and thin-support read model (#881, authority-surface hardening).

Tethers are **support categories, not truth labels**. This module derives a
compact, deterministic read model describing *what backs* an authority-bearing
candidate surface — its source, the record's own state, a test/contract, or a
human owner — and *whether that support is thin*. It never certifies that a
claim is true in the world.

The taxonomy mirrors the Workflow Packs Authority Map (see
``dev_docs/proposals/watercooler-workflow-packs-and-prepare-work.md`` §4.4)
collapsed into seven tether categories:

- ``source``       — byte-checkable evidence in a referenced entry (verified quote)
- ``record_state`` — Watercooler record state (a backing Decision/Closure/supersession);
  named ``record_state`` rather than ``state`` because "state" is overloaded — this
  means *the record's own state*, never state-of-the-world.
- ``test``         — enforced by an executable test
- ``contract``     — enforced by an API/schema/event contract
- ``user``         — supplied/owned by an accountable human for this work
- ``interpretive`` — agent inference, generated summary/rationale, LLM warning
- ``unknown``      — no derivable support signal; needs review

Shipped producer coverage (do not overstate): on the candidate/promotion path
``derive_candidate_support`` produces only ``source`` / ``record_state`` /
``user`` substantive support plus ``interpretive`` / ``unknown``. ``test`` and
``contract`` stay substantive categories in the taxonomy (a test-/contract-backed
surface genuinely *is* substantively supported, and ``build_read_model`` callers
may populate them), but no shipped producer emits them yet (#893). Mapped to the
brainstorm §4 warrant gates, this surface answers Provenance / Quotability /
Authority (and partial Evidence); Attribution / Scope / Contestability have no
producer here, and Durability is a T2 temporal property — so this is not an
"8 gates implemented" surface.

Design constraints (from the ``epistemic-failure-modes-unification-2026-06-03``
thread and the authority-surface-hardening plan):

- Per-tether counts are the primary surface. A single collapsed confidence
  number is explicitly **out** — do not reduce support to one score.
- The read model is itself heuristic (tether classification is interpretive), so
  it renders as descriptive counts, not a "well-supported" badge.
- ``decision-backed`` / quote-backed support proves *what the record says or who
  authorized it* — the record, not the world. Labels say record/source-supported,
  never true.

This module imports stdlib only; it must not import higher-level Watercooler
modules (``decision_extraction``, ``promotion``) so they can import it freely.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

# ---------------------------------------------------------------------------
# Tether taxonomy
# ---------------------------------------------------------------------------

TETHER_SOURCE = "source"
TETHER_RECORD_STATE = "record_state"
TETHER_TEST = "test"
TETHER_CONTRACT = "contract"
TETHER_USER = "user"
TETHER_INTERPRETIVE = "interpretive"
TETHER_UNKNOWN = "unknown"

# Canonical ordering — used for deterministic tie-breaks and rendering. Listed
# strongest-tether-first; ``dominant_tether`` resolves to the strongest *present*
# tether in this order.
TETHER_CATEGORIES: tuple[str, ...] = (
    TETHER_SOURCE,
    TETHER_RECORD_STATE,
    TETHER_TEST,
    TETHER_CONTRACT,
    TETHER_USER,
    TETHER_INTERPRETIVE,
    TETHER_UNKNOWN,
)

# Precomputed canonical ordinal (strongest = 0) for O(1) strength comparison.
_TETHER_ORDER: dict[str, int] = {
    cat: i for i, cat in enumerate(TETHER_CATEGORIES)
}

# Tethers that count as *substantive* support. ``thin_support`` fires when a
# surface has zero substantive support — i.e. it is interpretive/unknown only.
# Seeded conservatively (fires only when nothing substantive backs the surface);
# tune against the #882 falsification battery before tightening.
SUBSTANTIVE_TETHERS: frozenset[str] = frozenset(
    {
        TETHER_SOURCE,
        TETHER_RECORD_STATE,
        TETHER_TEST,
        TETHER_CONTRACT,
        TETHER_USER,
    }
)

RECORD_STATE_ENTRY_TYPES: frozenset[str] = frozenset(
    {"Decision", "Closure", "Supersession"}
)

# Cap *displayed* evidence pointers per tether so a noisy extraction cannot bloat
# a rendered body. This is a render-only cap — it never limits support_counts,
# which must report the true number of supports.
_MAX_EVIDENCE_PER_TETHER = 3


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


@dataclass
class WarrantReadModel:
    """Compact, deterministic description of how a surface is supported.

    Attributes:
        support_counts: Per-tether counts, non-zero entries only, in canonical
            tether order. Counts are the primary surface; never collapsed into a
            single confidence number.
        dominant_tether: The most authoritative tether *present* — the strongest
            substantive tether (canonical order) when any substantive support
            exists, else the strongest present interpretive/unknown tether. It is
            deliberately not "the most frequent tether": every candidate carries
            a baseline ``interpretive`` pointer (the generated extraction), so a
            raw-count winner would spuriously report interpretive-dominance over
            real source support. ``"unknown"`` when there is no support at all.
        thin_support: ``True`` when no substantive (source/record_state/test/
            contract/user) support backs the surface.
        thin_support_reason: A deterministic one-line explanation when
            ``thin_support`` is ``True``; ``None`` otherwise.
        support_evidence: Compact per-support pointers so a consumer can inspect
            support rather than trust a bare count. Each is a dict with
            ``tether`` and ``label`` plus optional ``entry_id``, ``quote_hash``,
            ``detail``, and — when the producer knows where the referenced
            entry lives — ``topic`` and ``index``, so a consumer can build a
            jump link in one step instead of resolving the ULID first (C2,
            thread candidate-research-backend-support).
    """

    support_counts: dict[str, int]
    dominant_tether: str
    thin_support: bool
    thin_support_reason: Optional[str]
    support_evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_entry_fields(self) -> dict[str, Any]:
        """Serialise the read-model to structured entry-node metadata (#896 Leg 2).

        Mirrors the body markers but structured, so a consumer renders support at
        point of consumption instead of re-parsing the body text (the §7
        anti-pattern). ``thin_support_reason`` is omitted when None and
        ``support_evidence`` when empty, so an unsupported surface and a legacy
        entry produce the same minimal node shape.
        """
        fields: dict[str, Any] = {
            "support_counts": dict(self.support_counts),
            "dominant_tether": self.dominant_tether,
            "thin_support": self.thin_support,
        }
        if self.thin_support_reason is not None:
            fields["thin_support_reason"] = self.thin_support_reason
        if self.support_evidence:
            fields["support_evidence"] = list(self.support_evidence)
        return fields


def quote_hash(text: str) -> str:
    """Return a short, stable hex digest for a quote (inspection pointer, not a
    secret). Whitespace is collapsed so trivially reformatted quotes hash alike.
    """
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _evidence(
    tether: str,
    label: str,
    *,
    entry_id: Optional[str] = None,
    quote_hash_value: Optional[str] = None,
    detail: Optional[str] = None,
    topic: Optional[str] = None,
    index: Optional[int] = None,
) -> dict[str, Any]:
    """Build one compact evidence pointer, omitting empty optional fields.

    ``topic``/``index`` locate the referenced ``entry_id`` (C2): consumers can
    then link straight to the source entry instead of resolving the bare ULID.
    Only meaningful alongside ``entry_id``; omitted when unknown so older
    producers and topic-less pointers keep the same minimal shape.
    """
    ev: dict[str, Any] = {"tether": tether, "label": label}
    if entry_id:
        ev["entry_id"] = entry_id
        if topic:
            ev["topic"] = topic
        if index is not None:
            ev["index"] = index
    if quote_hash_value:
        ev["quote_hash"] = quote_hash_value
    if detail:
        ev["detail"] = detail
    return ev


def build_read_model(evidence: Sequence[dict[str, Any]]) -> WarrantReadModel:
    """Aggregate a list of evidence pointers into a :class:`WarrantReadModel`.

    Pure aggregation: counts tethers, picks the dominant tether (the strongest
    substantive tether present — see :func:`_dominant_tether`, strength not
    frequency), and decides thinness from substantive-support presence. Evidence
    with an unrecognized ``tether`` is counted under ``unknown`` so a malformed
    pointer can never silently inflate substantive support.
    """
    normalized_evidence: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for ev in evidence:
        tether = ev.get("tether")
        if tether not in TETHER_CATEGORIES:
            tether = TETHER_UNKNOWN
            ev = {**ev, "tether": tether}
        counts[tether] += 1
        normalized_evidence.append(ev)

    # Insertion order is canonical (strongest-first) — load-bearing for rendering.
    support_counts = {
        cat: counts[cat] for cat in TETHER_CATEGORIES if counts[cat]
    }

    dominant_tether = _dominant_tether(counts)

    substantive = sum(counts[cat] for cat in SUBSTANTIVE_TETHERS)
    thin_support = substantive == 0
    thin_support_reason = _thin_reason(support_counts) if thin_support else None

    return WarrantReadModel(
        support_counts=support_counts,
        dominant_tether=dominant_tether,
        thin_support=thin_support,
        thin_support_reason=thin_support_reason,
        support_evidence=normalized_evidence,
    )


def _dominant_tether(counts: "Counter[str]") -> str:
    """The most authoritative tether present.

    Prefers the strongest substantive tether (canonical order) when any
    substantive support exists; otherwise the strongest present tether. Returns
    ``unknown`` when nothing is present. Strength, not frequency — see
    :class:`WarrantReadModel`.
    """
    present = [cat for cat in TETHER_CATEGORIES if counts[cat]]
    if not present:
        return TETHER_UNKNOWN
    substantive_present = [c for c in present if c in SUBSTANTIVE_TETHERS]
    pool = substantive_present or present
    return min(pool, key=lambda cat: _TETHER_ORDER[cat])


def _thin_reason(support_counts: dict[str, int]) -> str:
    """Deterministic explanation for why support is thin."""
    if not support_counts:
        return (
            "No support signals derived (no source, record-state, test, "
            "contract, or user tether)."
        )
    mix = ", ".join(f"{cat}={n}" for cat, n in support_counts.items())
    return (
        "No source, record-state, test, contract, or user support — support is "
        f"interpretive/unknown only ({mix}). Treat as generated content pending "
        "human review."
    )


# ---------------------------------------------------------------------------
# Candidate-surface derivation
# ---------------------------------------------------------------------------


def derive_candidate_support(
    *,
    source_entry_id: Optional[str],
    verbatim_quotes: Sequence[str],
    quote_verified: bool,
    human_authorized_by: Optional[str] = None,
    source_entry_type: Optional[str] = None,
    extractor_warning: Optional[str] = None,
    moral_delegation_warning: bool = False,
    source_topic: Optional[str] = None,
    source_index: Optional[int] = None,
) -> WarrantReadModel:
    """Derive the warrant read model for a Decision candidate from existing facts.

    Support is derived from facts that already exist on the candidate — no new
    classification of the *content's* truth:

    - Verified verbatim quotes against a referenced source entry → ``source``.
    - Quotes that did not validate → ``interpretive`` (LLM artifact, not source).
    - No quotes at all → ``unknown`` (missing evidence).
    - A source entry that is itself a Decision/Closure/Supersession **and** the
      candidate has verified source evidence tying it to that entry →
      ``record_state``. Without a validated tie the record-state tether is
      withheld, so unverified quotes cannot launder into record-backed support.
    - An accountable ``human_authorized_by`` → ``user`` (usually absent until
      promotion; passed through for the promotion surface).
    - The generated extraction, extractor warnings, and moral-delegation
      warnings → ``interpretive``.

    The ``test`` and ``contract`` tethers are part of the mandated taxonomy but
    have no producer on this candidate path yet — Decision candidates carry no
    test/contract refs today. They remain valid categories for other surfaces
    (and for ``build_read_model`` callers) to populate.

    Args:
        source_entry_id: Entry the candidate was extracted from, if any.
        verbatim_quotes: Quotes the extractor supplied.
        quote_verified: Whether those quotes validated byte-for-byte against the
            source body (``True`` only when there are validated quotes).
        human_authorized_by: Accountable human, already scrubbed/validated by the
            caller; treated as user support when non-empty.
        source_entry_type: ``entry_type`` of the source entry, if known.
        extractor_warning: Free-text warning the extractor emitted, if any.
        moral_delegation_warning: Whether a procedural moral-delegation warning fired.
        source_topic: Thread topic of the source entry, when the caller knows
            it — stamped onto entry_id-bearing pointers so consumers can build
            jump links in one step (C2). Omitted when unknown.
        source_index: The source entry's index within its thread; same purpose.

    Returns:
        A :class:`WarrantReadModel`.
    """
    evidence: list[dict[str, Any]] = []

    # The extraction itself is a generated, interpretive artifact — always.
    evidence.append(_evidence(TETHER_INTERPRETIVE, "generated_extraction"))

    quotes = [q for q in verbatim_quotes if q and q.strip()]
    has_verified_source = bool(quotes) and quote_verified
    # Emit one pointer per quote — uncapped. Counts are the primary surface and
    # must reflect true support; the per-tether cap is a *render* concern (display
    # bloat), applied in render_support_section, not here.
    if has_verified_source:
        for quote in quotes:
            evidence.append(
                _evidence(
                    TETHER_SOURCE,
                    "verified_quote",
                    entry_id=source_entry_id,
                    quote_hash_value=quote_hash(quote),
                    topic=source_topic,
                    index=source_index,
                )
            )
    elif quotes:
        # Quotes exist but did not validate — they are LLM output, not source.
        for quote in quotes:
            evidence.append(
                _evidence(
                    TETHER_INTERPRETIVE,
                    "unverified_quote",
                    quote_hash_value=quote_hash(quote),
                )
            )
    else:
        evidence.append(_evidence(TETHER_UNKNOWN, "missing_quote_evidence"))

    # record_state support requires a *validated* tie to the source entry. The
    # only link between this candidate's statement and the source Decision/Closure
    # is the quote evidence; if that failed validation (hallucinated_quote /
    # summary_only_quote_evidence) or is absent, marking the candidate
    # record-backed would launder weak evidence into substantive support — the
    # exact false-authority surface this read model exists to prevent. Gate on
    # verified source evidence, the same signal that grants the source tether.
    if has_verified_source and source_entry_type in RECORD_STATE_ENTRY_TYPES:
        evidence.append(
            _evidence(
                TETHER_RECORD_STATE,
                f"source_is_{source_entry_type.lower()}",
                entry_id=source_entry_id,
                topic=source_topic,
                index=source_index,
            )
        )

    if human_authorized_by and human_authorized_by.strip():
        evidence.append(
            _evidence(
                TETHER_USER,
                "human_authorized",
                detail=human_authorized_by.strip(),
            )
        )

    if extractor_warning and extractor_warning.strip():
        evidence.append(_evidence(TETHER_INTERPRETIVE, "extractor_warning"))

    if moral_delegation_warning:
        evidence.append(
            _evidence(TETHER_INTERPRETIVE, "moral_delegation_warning")
        )

    return build_read_model(evidence)


# ---------------------------------------------------------------------------
# Rendering — deterministic body markers + a human-readable section
# ---------------------------------------------------------------------------

# Marker keys. A deterministic, greppable structured surface for downstream
# consumers (hosted UI, federation readers, grep) — aligned with the existing
# candidate marker convention (Candidate-Type, Moral-Delegation-Warning, ...).
# The core promotion path does NOT parse these back; it re-derives the read
# model from the candidate's underlying facts (see
# ``promotion.format_promotion_decision_body``), so the markers are an output
# surface, not a parse-back contract.
MARKER_SUPPORT_COUNTS = "Tether-Support-Counts"
MARKER_DOMINANT = "Dominant-Tether"
MARKER_THIN = "Thin-Support"
MARKER_THIN_REASON = "Thin-Support-Reason"


def render_support_markers(model: WarrantReadModel) -> list[str]:
    """Render the read model as deterministic body marker lines."""
    counts = (
        ", ".join(f"{cat}={n}" for cat, n in model.support_counts.items())
        or "none"
    )
    lines = [
        f"{MARKER_SUPPORT_COUNTS}: {counts}",
        f"{MARKER_DOMINANT}: {model.dominant_tether}",
        f"{MARKER_THIN}: {'true' if model.thin_support else 'false'}",
    ]
    if model.thin_support and model.thin_support_reason:
        lines.append(f"{MARKER_THIN_REASON}: {model.thin_support_reason}")
    return lines


def render_support_section(model: WarrantReadModel) -> list[str]:
    """Render the read model as a human-readable ``## Support tethers`` section.

    The section is descriptive: it states what backs the candidate by tether
    type and warns explicitly that tethers describe record/source support, never
    world-truth.

    The user-facing heading deliberately avoids the word "warrant": in
    epistemology *warrant* names the truth-conducive thing that converts true
    belief into knowledge, so a rendered ``## Warrant`` heading — which can be
    chunked away from its disclaimer by a retrieval reader — carries the same
    overclaim vector the team retired with "JTB". The internal model is still
    ``WarrantReadModel`` (no reader sees it); only the rendered surface uses the
    neutral "support tethers" framing.

    Invariant: this section must never emit blockquote (``> ``) lines. It is
    rendered after ``## Evidence`` on both candidate and promoted bodies, and
    ``promotion.parse_candidate_body`` collects ``> `` lines up to the next
    ``## `` header — a blockquote here would leak into parsed evidence quotes.
    """
    lines = [
        "## Support tethers",
        "Support is categorized by *tether type*, not truth. A tether records "
        "what backs this candidate — its source, the record's own state, a "
        "test/contract, or a human owner. It does **not** certify the claim is "
        "true in the world.",
        "",
    ]
    if model.support_counts:
        for cat, n in model.support_counts.items():
            lines.append(f"- {cat}: {n}")
    else:
        lines.append("- (no support signals derived)")
    lines.extend(
        [
            "",
            f"Dominant tether: {model.dominant_tether}",
            f"Thin support: {'yes' if model.thin_support else 'no'}",
        ]
    )
    if model.thin_support and model.thin_support_reason:
        lines.append(f"Thin-support reason: {model.thin_support_reason}")

    if model.support_evidence:
        lines.extend(["", "Support evidence:"])
        shown: Counter[str] = Counter()
        omitted: Counter[str] = Counter()
        for ev in model.support_evidence:
            tether = ev["tether"]
            # Display cap only — full counts live in support_counts above.
            if shown[tether] >= _MAX_EVIDENCE_PER_TETHER:
                omitted[tether] += 1
                continue
            shown[tether] += 1
            parts = [tether, ev["label"]]
            if ev.get("entry_id"):
                parts.append(f"entry `{ev['entry_id']}`")
            if ev.get("quote_hash"):
                parts.append(f"quote `{ev['quote_hash']}`")
            if ev.get("detail"):
                parts.append(ev["detail"])
            lines.append("- " + " · ".join(parts))
        for tether in TETHER_CATEGORIES:
            if omitted[tether]:
                plural = "s" if omitted[tether] != 1 else ""
                lines.append(
                    f"- … (+{omitted[tether]} more {tether} pointer{plural}; "
                    f"see Tether-Support-Counts for the full count)"
                )
    return lines
