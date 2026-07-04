"""Fact-edge substrate and StabilizedBeliefCandidate producer (#897b).

This module introduces the **atomic, gateable fact-edge** that the authority
surface lacked, and a **minimal on-demand producer** that composes fact-edges
into a ``StabilizedBeliefCandidate`` — a Note carrying a canonical, round-trip
warrant ledger. It unblocks the two deferred falsification fixtures (invalid
composition over true edges; disagreement-minority preservation) without a
durable graph, a daemon, or any new authority.

Design invariants (from
``dev_docs/brainstorms/2026-06-08-feat-fact-edge-substrate-brainstorm.md`` and the
matching plan):

- **Candidate-scoped, ephemeral.** Fact-edges live only inside a single
  candidate's ledger (the Note body), never a durable fact-edge graph. Nothing
  here is written to the durable read model, so the "T3/derived may never
  auto-promote" constraint is satisfied structurally.
- **Span vs. relation.** A :class:`FactEdge` splits into a byte-verifiable
  ``source_span`` and an interpretive ``claim``/``relation``/``anchors``. A
  ``source``/``record_state`` tether attaches to the *span*, not the relation —
  a verified span never makes the extracted proposition source-backed.
- **Verdict, not support.** :func:`check_composition` is monotone-downward: a
  ``clean-composition`` verdict is a *field on the candidate*, never a tether, a
  support count, a dominant tether, or a ``thin_support`` input. Path shape never
  confers authority.
- **Disagreement preserved, not judged.** :func:`check_disagreement_preservation`
  checks self-declared completeness plus a bounded, set-theoretic anti-mislabel
  guard. It makes no semantic divergence judgment. Its residual evasions (anchor
  perturbation, conclusion-edge omission) are a *named residual risk*, not closed;
  the backstop is human/critic audit over the round-trippable ledger.
- **Not a promotion candidate.** These Notes carry ``Promotable: false`` /
  ``Authority: none`` and must never be fed to ``promotion.parse_candidate_body``.

This module is one layer above ``authority_support`` (stdlib-only) and
``decision_extraction``; it may import both plus ``baseline_graph.reader``.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol, Sequence

from watercooler.authority_support import (
    RECORD_STATE_ENTRY_TYPES,
    TETHER_INTERPRETIVE,
    TETHER_RECORD_STATE,
    TETHER_SOURCE,
    TETHER_UNKNOWN,
    WarrantReadModel,
    build_read_model,
    quote_hash,
    render_support_markers,
)
from watercooler.decision_extraction import (
    normalize_quote_text,
    reverify_quotes_against_source,
)

# ``baseline_graph.reader`` is imported only inside ``produce_belief_candidate`` (the
# one function that does graph I/O). Importing it at module top would pull in the
# package ``__init__`` → git/FalkorDB stack, coupling the *pure* gate core (gates,
# serialization, data model, ``tether_for_span``) to GitPython at import time. Keeping
# the import function-local lets the substrate be imported standalone (e.g. by the
# eventual skill/MCP tool or a gate-only unit test) without the graph stack.
if TYPE_CHECKING:
    from watercooler.baseline_graph.reader import GraphEntry

# ---------------------------------------------------------------------------
# Vocabulary (no truth-certifying terms)
# ---------------------------------------------------------------------------

COMPOSITION_INTERPRETIVE_ONLY = "interpretive-only"  # conservative default
COMPOSITION_CLEAN = "clean-composition"  # earned upgrade; NOT support

SUPERSESSION_ACTIVE = "active"
SUPERSESSION_SUPERSEDED = "superseded"
SUPERSESSION_UNKNOWN = "unknown"  # fail-closed; blocks clean-composition
SUPERSESSION_STATES: frozenset[str] = frozenset(
    {SUPERSESSION_ACTIVE, SUPERSESSION_SUPERSEDED, SUPERSESSION_UNKNOWN}
)

DISPOSITION_INCORPORATED = "incorporated"
DISPOSITION_DROPPED = "dropped"
DISPOSITION_CONTRADICTING = "contradicting"
DISPOSITIONS: frozenset[str] = frozenset(
    {DISPOSITION_INCORPORATED, DISPOSITION_DROPPED, DISPOSITION_CONTRADICTING}
)

LEDGER_SCHEMA = "belief-candidate/1"
_LEDGER_BEGIN = "<!-- warrant-ledger:begin -->"
_LEDGER_END = "<!-- warrant-ledger:end -->"
# The ledger payload is compact single-line JSON (json.dumps escapes real newlines to
# ``\n``), so the JSON occupies exactly one physical line. We capture that single line
# (note: NO re.DOTALL — ``.`` does not match a newline), so the closing ``\n```\n`` fence
# is the only place the block can end. This makes the round trip robust even when a
# ``source_span`` quotes the literal closing fence + end-sentinel of a previous ledger:
# those terminators sit *inside* the one JSON line (no surrounding newlines) and cannot
# truncate the capture. ``parse_candidate_ledger`` also rejects >1 block so a quoted
# block can't shadow the canonical one.
_LEDGER_RE = re.compile(
    re.escape(_LEDGER_BEGIN) + r"\n```json\n(?P<json>.+)\n```\n" + re.escape(_LEDGER_END)
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactEdge:
    """The smallest gateable claim.

    The ``source_span`` is byte-verifiable (it provably appears in the referenced
    entry). The ``claim``/``relation``/``anchors`` are interpretive (LLM-extracted)
    and prove nothing about the world. ``tether``, ``supersession_state`` and
    ``source_timestamp`` are set by the producer from *live* evidence, never
    trusted from extractor output.
    """

    edge_id: str
    claim: str
    relation: str
    anchors: tuple[str, str]
    source_entry_id: str
    source_span: str
    tether: str
    supersession_state: str
    source_timestamp: str

    def __post_init__(self) -> None:
        # Anchors must be non-empty *after normalization*. An empty/whitespace anchor
        # normalizes to "" and would collide with any other empty anchor under the
        # composition bridge check (``"" == ""``), letting a discontinuous path
        # (alpha → "" … "" → beta) earn clean-composition — and would erase a conclusion
        # anchor so a real minority no longer overlaps it. An empty anchor is the
        # *absence* of a bridge token, not a shared one; reject it at construction so
        # every path (producer, parse, direct) fails closed.
        if not isinstance(self.anchors, tuple) or len(self.anchors) != 2:
            raise ValueError(f"FactEdge {self.edge_id!r}: anchors must be a 2-tuple")
        if not normalize_anchor(self.anchors[0]) or not normalize_anchor(self.anchors[1]):
            raise ValueError(
                f"FactEdge {self.edge_id!r}: anchors must be non-empty after normalization"
            )


@dataclass(frozen=True)
class CompositionVerdict:
    """A monotone-downward composition verdict. Never support metadata."""

    verdict: str
    defects: tuple[str, ...]


@dataclass(frozen=True)
class InputDisposition:
    """How the producer accounted for one input entry."""

    entry_id: str
    disposition: str
    edge: Optional[FactEdge] = None


@dataclass(frozen=True)
class DisagreementResult:
    complete: bool
    missing_inputs: tuple[str, ...]
    active_disagreements: tuple[InputDisposition, ...]
    excluded: tuple[InputDisposition, ...]


@dataclass(frozen=True)
class BeliefCandidate:
    conclusion: str
    edges: tuple[FactEdge, ...]
    conclusion_edges: tuple[FactEdge, ...]
    dispositions: tuple[InputDisposition, ...]
    requested_entry_ids: tuple[str, ...]
    composition: CompositionVerdict
    disagreement: DisagreementResult
    warrant: WarrantReadModel

    def __post_init__(self) -> None:
        # Enforce ``conclusion_edges ⊆ edges`` (and every disposition edge) on the type
        # itself, not just inside the producer. ``edges`` is the only edge pool the
        # serializer persists; a conclusion or disposition edge outside it would be lost
        # on render→parse and could shorten a multi-hop path into a clean single-edge
        # one. Direct construction or a future producer cannot bypass this.
        edge_ids = {e.edge_id for e in self.edges}
        # Duplicate edge_ids make the candidate un-round-trippable: render serializes all
        # edges but id-based refs (conclusion_edges, dispositions) and parse's edges_by_id
        # bind only one, so parse_candidate_ledger rejects the rendered body. Reject here
        # too, so a bad extractor (two ProposedEdge sharing an id) fails loud at the type
        # instead of producing a candidate that can't survive its own round trip.
        if len(edge_ids) != len(self.edges):
            raise ValueError("BeliefCandidate: duplicate edge_id in `edges`")
        stray = [e.edge_id for e in self.conclusion_edges if e.edge_id not in edge_ids]
        stray += [
            d.edge.edge_id
            for d in self.dispositions
            if d.edge is not None and d.edge.edge_id not in edge_ids
        ]
        if stray:
            raise ValueError(
                f"BeliefCandidate: conclusion/disposition edges not in `edges`: {stray}"
            )


# ---------------------------------------------------------------------------
# Normalization (one shared function — see brainstorm §3.2 mitigation)
# ---------------------------------------------------------------------------


def normalize_anchor(text: str) -> str:
    """Canonicalize an anchor for set-membership comparison.

    Reuses the *exact* quote normalization that byte-reverification uses
    (``decision_extraction.normalize_quote_text``: NFKC + smart-quote/dash fold +
    whitespace collapse), then casefolds. Documented as **cost-raising, not
    hermetic**: it absorbs trivial casing/spacing perturbation, but a determined
    producer can still perturb an anchor string to dodge the disagreement guard
    (named residual risk).
    """
    return normalize_quote_text(text).casefold()


def canonicalize_timestamp(value: Optional[str]) -> str:
    """Return a canonical UTC ``...Z`` timestamp, or the input unchanged if it
    cannot be parsed.

    Accepts both trailing ``Z`` and ``+00:00`` forms (existing Watercooler entries
    carry both; treating only literal ``Z`` as valid would wrongly downgrade good
    entries). An unparseable value is returned verbatim so it round-trips and the
    composition gate fails it closed rather than silently passing.
    """
    parsed = _parse_timestamp(value)
    if parsed is None:
        return value or ""
    # isoformat() preserves fractional seconds when present (so sub-second ordering
    # survives into check_composition's temporal check — entries within the same second
    # must not normalize to equal timestamps and slip past temporal_disorder) and
    # zero-pads the year (strftime("%Y") drops leading zeros for years < 1000).
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (``Z`` or offset form) into an aware UTC
    datetime, or ``None`` when it cannot be parsed."""
    if not value:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Supersession provenance (fail closed)
# ---------------------------------------------------------------------------


class SupersessionResolver(Protocol):
    """Resolve an edge's currency from live state. Returns ``active`` |
    ``superseded`` | ``unknown``. Never trusted from extractor output."""

    def __call__(self, edge: FactEdge, source_entry: GraphEntry) -> str: ...


# ---------------------------------------------------------------------------
# Tether mapping (per edge; mirrors derive_candidate_support semantics)
# ---------------------------------------------------------------------------


def tether_for_span(
    source_span: str,
    source_body: Optional[str],
    source_entry_type: Optional[str],
) -> str:
    """Classify an edge's tether from *live* byte-reverification of its span.

    - no span -> ``unknown``
    - source unreadable -> ``unknown``
    - span reverifies and the source entry is a Decision/Closure/Supersession ->
      ``record_state``
    - span reverifies (ordinary entry) -> ``source``
    - span present but does not reverify (hallucinated / below length floor) ->
      ``interpretive``

    The tether attaches to the *span*, never to the interpretive relation. The
    verified-span → ``record_state``/``source`` rule mirrors
    ``authority_support.derive_candidate_support`` (the promotion-path classifier); if
    that record-state-granting rule is ever tightened, mirror the change here so the two
    authority paths stay consistent.
    """
    if not source_span or not source_span.strip():
        return TETHER_UNKNOWN
    if source_body is None:
        return TETHER_UNKNOWN
    result = reverify_quotes_against_source([source_span], source_body)
    if not result.verified:
        return TETHER_INTERPRETIVE
    if source_entry_type in RECORD_STATE_ENTRY_TYPES:
        return TETHER_RECORD_STATE
    return TETHER_SOURCE


# ---------------------------------------------------------------------------
# Warrant read model (span tether + mandatory interpretive extraction pointer)
# ---------------------------------------------------------------------------


def _build_warrant(edges: Sequence[FactEdge]) -> WarrantReadModel:
    """Build the #881 read model over the candidate's edges.

    Each edge contributes **two** evidence pointers: one for the span tether
    (source/record_state/interpretive/unknown) and one mandatory ``interpretive``
    pointer for the extracted proposition. So a fully byte-verified path still
    visibly contains generated interpretation — a verified span can never make a
    whole edge read as source-backed.
    """
    evidence: list[dict[str, Any]] = []
    for edge in edges:
        span_label = {
            TETHER_SOURCE: "verified_span",
            TETHER_RECORD_STATE: "verified_record_span",
            TETHER_INTERPRETIVE: "unverified_span",
            TETHER_UNKNOWN: "no_span",
        }.get(edge.tether, "span")
        span_ev: dict[str, Any] = {
            "tether": edge.tether,
            "label": span_label,
            "entry_id": edge.source_entry_id,
        }
        if edge.source_span:
            span_ev["quote_hash"] = quote_hash(edge.source_span)
        evidence.append(span_ev)
        evidence.append(
            {
                "tether": TETHER_INTERPRETIVE,
                "label": "extracted_relation",
                "detail": f"{edge.anchors[0]} -{edge.relation}-> {edge.anchors[1]}",
            }
        )
    return build_read_model(evidence)


# ---------------------------------------------------------------------------
# Gate 1 — composition (deny-by-default, monotone-downward)
# ---------------------------------------------------------------------------


def check_composition(path: Sequence[FactEdge]) -> CompositionVerdict:
    """Gate a composed multi-hop path. Deny-by-default.

    Returns ``clean-composition`` only when *every* hop is source/record_state
    tethered, currency is ``active`` (``superseded`` and ``unknown`` both fail
    closed), bridging anchors are byte-identical (shared normalization), and the
    source timestamps parse and are non-decreasing. Otherwise ``interpretive-only``
    with the failing reasons in ``defects``.

    The verdict is *not support*: it never enters the warrant read model.
    """
    defects: list[str] = []
    if not path:
        return CompositionVerdict(COMPOSITION_INTERPRETIVE_ONLY, ("empty_path",))

    if any(edge.tether not in (TETHER_SOURCE, TETHER_RECORD_STATE) for edge in path):
        defects.append("non_substantive_tether")

    if any(edge.supersession_state != SUPERSESSION_ACTIVE for edge in path):
        defects.append("supersession_not_active")

    for left, right in zip(path, path[1:]):
        if normalize_anchor(left.anchors[1]) != normalize_anchor(right.anchors[0]):
            defects.append("bridge_mismatch")
            break

    parsed = [_parse_timestamp(edge.source_timestamp) for edge in path]
    if any(ts is None for ts in parsed):
        defects.append("unparseable_timestamp")
    elif any(a > b for a, b in zip(parsed, parsed[1:])):  # type: ignore[operator]
        defects.append("temporal_disorder")

    if defects:
        return CompositionVerdict(COMPOSITION_INTERPRETIVE_ONLY, tuple(defects))
    return CompositionVerdict(COMPOSITION_CLEAN, ())


# ---------------------------------------------------------------------------
# Gate 2 — disagreement preservation (completeness + anti-mislabel guard)
# ---------------------------------------------------------------------------


def check_disagreement_preservation(
    requested_entry_ids: Sequence[str],
    dispositions: Sequence[InputDisposition],
    conclusion_edges: Sequence[FactEdge],
) -> DisagreementResult:
    """Preserve minority disagreement by accounting, not semantic judgment.

    1. Completeness: every requested id appears exactly once across dispositions.
    2. Anti-mislabel: a ``dropped`` item whose edge shares *any* normalized anchor
       with the conclusion path is reclassified ``contradicting`` (it touches the
       conclusion's subject matter, so it is a preserved disagreement, not an
       off-topic drop). Set-membership only — no semantic divergence judgment.

    ``active_disagreements`` (declared + reclassified ``contradicting``) MUST be
    rendered verbatim into ``## Active Disagreements``; ``excluded`` are genuinely
    off-topic ``dropped`` items.
    """
    counts: dict[str, int] = {}
    malformed: set[str] = set()
    for disp in dispositions:
        counts[disp.entry_id] = counts.get(disp.entry_id, 0) + 1
        if disp.disposition not in DISPOSITIONS:
            # An unrecognized disposition value (LLM label drift / typo) must not pass
            # as validly accounted for — fail closed rather than silently dropping the
            # evidence (it would be neither incorporated, active, nor excluded).
            malformed.add(disp.entry_id)
    not_once = {rid for rid in set(requested_entry_ids) if counts.get(rid, 0) != 1}
    missing = tuple(sorted(not_once | malformed))
    complete = not missing

    conclusion_anchors: set[str] = set()
    for edge in conclusion_edges:
        conclusion_anchors.add(normalize_anchor(edge.anchors[0]))
        conclusion_anchors.add(normalize_anchor(edge.anchors[1]))

    active: list[InputDisposition] = []
    excluded: list[InputDisposition] = []
    for disp in dispositions:
        if disp.disposition == DISPOSITION_CONTRADICTING:
            active.append(disp)
        elif disp.disposition == DISPOSITION_DROPPED:
            if disp.edge is None:
                # A `dropped` item with no edge cannot be *shown* off-topic — the
                # anchor-overlap check has nothing to run on. Fail closed: preserve it as
                # a disagreement rather than silently excluding it, so a producer can't
                # bury a conflicting minority by omitting the edge reference.
                active.append(
                    InputDisposition(
                        entry_id=disp.entry_id,
                        disposition=DISPOSITION_CONTRADICTING,
                        edge=None,
                    )
                )
                continue
            edge_anchors = {
                normalize_anchor(disp.edge.anchors[0]),
                normalize_anchor(disp.edge.anchors[1]),
            }
            if edge_anchors & conclusion_anchors:
                active.append(
                    InputDisposition(
                        entry_id=disp.entry_id,
                        disposition=DISPOSITION_CONTRADICTING,
                        edge=disp.edge,
                    )
                )
            else:
                excluded.append(disp)

    return DisagreementResult(
        complete=complete,
        missing_inputs=missing,
        active_disagreements=tuple(active),
        excluded=tuple(excluded),
    )


# ---------------------------------------------------------------------------
# Serialization (canonical ledger lives in the Note body; round-trips gate fields)
# ---------------------------------------------------------------------------


def _edge_to_dict(edge: FactEdge) -> dict[str, Any]:
    return {
        "edge_id": edge.edge_id,
        "claim": edge.claim,
        "relation": edge.relation,
        "anchors": [edge.anchors[0], edge.anchors[1]],
        "source_entry_id": edge.source_entry_id,
        "source_span": edge.source_span,
        "tether": edge.tether,
        "supersession_state": edge.supersession_state,
        "source_timestamp": edge.source_timestamp,
    }


# Derived from the dataclass so the parser's required-field check can never silently
# drift from FactEdge's actual shape (a new field added to FactEdge + _edge_to_dict but
# forgotten here would otherwise serialize yet never be validated on parse).
_EDGE_FIELDS = tuple(f.name for f in dataclasses.fields(FactEdge))


_EDGE_STR_FIELDS = tuple(f for f in _EDGE_FIELDS if f != "anchors")


def _edge_from_dict(raw: dict[str, Any]) -> FactEdge:
    if not isinstance(raw, dict):
        raise ValueError("malformed warrant-ledger: each edge must be an object")
    missing = [f for f in _EDGE_FIELDS if f not in raw]
    if missing:
        raise ValueError(f"malformed warrant-ledger: edge missing fields {missing}")
    # Untrusted JSON: validate field *types*, not just presence — a non-string scalar
    # (e.g. tether as a number, source_timestamp as null) would otherwise flow into the
    # gates and either crash or be silently mis-handled.
    for field_name in _EDGE_STR_FIELDS:
        if not isinstance(raw[field_name], str):
            raise ValueError(
                f"malformed warrant-ledger: edge field {field_name!r} must be a string"
            )
    anchors = raw["anchors"]
    if not isinstance(anchors, list) or len(anchors) != 2:
        raise ValueError("malformed warrant-ledger: anchors must be a 2-element list")
    if not all(isinstance(a, str) for a in anchors):
        raise ValueError("malformed warrant-ledger: anchors must be strings")
    return FactEdge(
        edge_id=raw["edge_id"],
        claim=raw["claim"],
        relation=raw["relation"],
        anchors=(anchors[0], anchors[1]),
        source_entry_id=raw["source_entry_id"],
        source_span=raw["source_span"],
        tether=raw["tether"],
        supersession_state=raw["supersession_state"],
        source_timestamp=raw["source_timestamp"],
    )


def _dangling(ids: Sequence[str], pool: dict[str, FactEdge]) -> list[str]:
    """Ids referencing an edge not present in ``pool``.

    The shared dangling-edge predicate — single-sourced so a future tightening of the
    "every referenced edge must exist" guard can't be applied to some call sites and
    forgotten in others. Used by both the producer (extractor output) and the parser
    (ledger bytes); each caller raises its own site-specific ``ValueError``.
    """
    return [i for i in ids if i not in pool]


def render_candidate_body(candidate: BeliefCandidate) -> str:
    """Render the full StabilizedBeliefCandidate Note body.

    The sentinel-delimited compact-JSON ledger is canonical; the human sections
    below it are a rendering of that ledger, never an independent source of truth.
    The JSON is emitted compact (single line) so no value can begin a line with
    ``> `` and collide with ``promotion.parse_candidate_body`` evidence parsing.
    """
    ledger = {
        "schema": LEDGER_SCHEMA,
        "conclusion": candidate.conclusion,
        "requested_entry_ids": list(candidate.requested_entry_ids),
        "edges": [_edge_to_dict(e) for e in candidate.edges],
        "conclusion_edges": [e.edge_id for e in candidate.conclusion_edges],
        "composition": {
            "verdict": candidate.composition.verdict,
            "defects": list(candidate.composition.defects),
        },
        "dispositions": [
            {
                "entry_id": d.entry_id,
                "disposition": d.disposition,
                "edge_id": d.edge.edge_id if d.edge is not None else None,
            }
            for d in candidate.dispositions
        ],
    }
    ledger_json = json.dumps(ledger, separators=(",", ":"), ensure_ascii=False)

    lines: list[str] = [
        "Spec: stabilized-belief-candidate",
        "Surface-Kind: stabilized_belief_candidate",
        "Promotable: false",
        "Authority: none",
        "",
        "# Stabilized belief candidate",
        "",
        candidate.conclusion,
        "",
        f"Composition: {candidate.composition.verdict}",
        "",
        _LEDGER_BEGIN,
        "```json",
        ledger_json,
        "```",
        _LEDGER_END,
        "",
        # Neutral heading: ``authority_support`` deliberately keeps the word "warrant"
        # out of *rendered* headings (a retrieval reader can chunk a heading away from
        # its disclaimer; CLAUDE.md vocabulary-lock). The internal model stays
        # WarrantReadModel; only this user-facing surface uses the flat term.
        "## Support tethers",
        *render_support_markers(candidate.warrant),
    ]

    if candidate.disagreement.active_disagreements:
        lines += ["", "## Active Disagreements"]
        for disp in candidate.disagreement.active_disagreements:
            position = disp.edge.claim if disp.edge is not None else "(no edge supplied)"
            lines += [f"### Position ({disp.entry_id})", f"> {position}"]

    if candidate.disagreement.excluded:
        lines += ["", "## Excluded Evidence"]
        for disp in candidate.disagreement.excluded:
            lines.append(f"- {disp.entry_id} (off-topic; no conclusion-anchor overlap)")

    return "\n".join(lines) + "\n"


def parse_candidate_ledger(body: str) -> BeliefCandidate:
    """Parse a candidate Note body back into a :class:`BeliefCandidate`.

    Reads the canonical ledger only; the human sections are ignored. The composition
    verdict, warrant read model, and disagreement result are *recomputed* from the
    parsed primitives (the same builders the producer uses), so a round trip preserves
    them. Missing gate-read fields are rejected, never silently defaulted.

    Note on scope: recompute re-establishes *structural* consistency (anchors, currency,
    timestamps, path shape) from the stored fields — it does **not** re-byte-verify each
    span against its live source. A hand-forged ledger asserting ``tether: source`` over
    a fabricated span will therefore still recompute ``clean-composition``; the stored
    tether is trusted. The backstop is human/critic audit over this ledger — no consumer
    should treat a *parsed* verdict as authority-bearing unless the producer generated it.
    """
    matches = _LEDGER_RE.findall(body)
    if not matches:
        raise ValueError("no warrant-ledger block found in candidate body")
    if len(matches) > 1:
        # Ambiguous: a second (e.g. quoted) ledger block must not be able to shadow the
        # canonical one. Refuse rather than silently bind to the first.
        raise ValueError(f"multiple warrant-ledger blocks found ({len(matches)})")
    try:
        raw = json.loads(matches[0])
    except (json.JSONDecodeError, RecursionError) as exc:
        # RecursionError: deeply-nested JSON in an untrusted body must fail as a clean
        # ValueError, not leak an uncaught crash to a future daemon/MCP caller.
        raise ValueError(f"malformed warrant-ledger JSON: {exc}") from exc

    # Untrusted JSON: validate container types before indexing, so a hostile body fails
    # closed as a ValueError instead of an uncaught AttributeError/KeyError/TypeError.
    if not isinstance(raw, dict):
        raise ValueError("malformed warrant-ledger: top-level value must be an object")
    if raw.get("schema") != LEDGER_SCHEMA:
        raise ValueError(f"unsupported ledger schema: {raw.get('schema')!r}")
    for key in ("edges", "conclusion_edges", "dispositions", "conclusion", "requested_entry_ids"):
        if key not in raw:
            raise ValueError(f"malformed warrant-ledger: missing {key}")
    for key in ("edges", "conclusion_edges", "dispositions", "requested_entry_ids"):
        if not isinstance(raw[key], list):
            raise ValueError(f"malformed warrant-ledger: {key} must be a list")
    if not isinstance(raw["conclusion"], str):
        raise ValueError("malformed warrant-ledger: conclusion must be a string")

    edges = tuple(_edge_from_dict(e) for e in raw["edges"])
    edges_by_id = {e.edge_id: e for e in edges}
    if len(edges_by_id) != len(edges):
        # A duplicate edge_id collapses in the dict (second wins); a conclusion ref would
        # then resolve to one edge while a second edge with the same id hides in `edges`,
        # an un-covered route to shortening a multi-hop path. Reject.
        raise ValueError("malformed warrant-ledger: duplicate edge_id in edges")

    if not all(isinstance(eid, str) for eid in raw["conclusion_edges"]):
        raise ValueError("malformed warrant-ledger: conclusion_edges must be strings")
    dangling = _dangling(raw["conclusion_edges"], edges_by_id)
    if dangling:
        raise ValueError(
            f"malformed warrant-ledger: conclusion_edges reference unknown edges {dangling}"
        )
    conclusion_edges = tuple(edges_by_id[eid] for eid in raw["conclusion_edges"])

    dispositions: list[InputDisposition] = []
    for d in raw["dispositions"]:
        if (
            not isinstance(d, dict)
            or not isinstance(d.get("entry_id"), str)
            or not isinstance(d.get("disposition"), str)
        ):
            raise ValueError(
                "malformed warrant-ledger: each disposition needs string entry_id/disposition"
            )
        edge_id = d.get("edge_id")
        if edge_id is not None and not isinstance(edge_id, str):
            raise ValueError(
                "malformed warrant-ledger: disposition edge_id must be a string or null"
            )
        if edge_id and edge_id not in edges_by_id:
            # Symmetric with the dangling-conclusion-edge check: a forged ledger must not
            # null a disposition's edge (which would drop it from anchor-overlap
            # reclassification while completeness still counts the entry). Fail closed.
            raise ValueError(
                f"malformed warrant-ledger: disposition references unknown edge {edge_id!r}"
            )
        dispositions.append(
            InputDisposition(
                entry_id=d["entry_id"],
                disposition=d["disposition"],
                edge=edges_by_id.get(edge_id) if edge_id else None,
            )
        )

    # ``requested_entry_ids`` is a gate-read field (the completeness check verifies every
    # requested input is dispositioned). Its presence + list-type were checked above; a
    # bare string would otherwise iterate into characters and corrupt the gate input set.
    if not all(isinstance(rid, str) for rid in raw["requested_entry_ids"]):
        raise ValueError("malformed warrant-ledger: requested_entry_ids must be strings")
    requested = tuple(raw["requested_entry_ids"])
    # Recompute the composition verdict from the parsed gate-read fields rather than
    # trusting the stored block: a manually-edited or forged ledger must not keep a
    # ``clean-composition`` verdict after its edges change (deny-by-default).
    composition = check_composition(conclusion_edges)
    disagreement = check_disagreement_preservation(
        requested, dispositions, conclusion_edges
    )
    warrant = _build_warrant(edges)

    return BeliefCandidate(
        conclusion=raw["conclusion"],
        edges=edges,
        conclusion_edges=conclusion_edges,
        dispositions=tuple(dispositions),
        requested_entry_ids=requested,
        composition=composition,
        disagreement=disagreement,
        warrant=warrant,
    )


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposedEdge:
    """An extractor's proposal for one fact-edge (pre-verification)."""

    edge_id: str
    claim: str
    relation: str
    anchors: tuple[str, str]
    source_entry_id: str
    source_span: str


@dataclass(frozen=True)
class ProposedDisposition:
    entry_id: str
    disposition: str
    edge_id: Optional[str] = None


@dataclass(frozen=True)
class ProducerExtraction:
    """What an :class:`EdgeExtractor` returns over a set of entries."""

    conclusion: str
    edges: tuple[ProposedEdge, ...]
    conclusion_edge_ids: tuple[str, ...]
    dispositions: tuple[ProposedDisposition, ...]


class EdgeExtractor(Protocol):
    """Maps live entries to a proposed extraction. The interpretive front-end —
    the default is an LLM; tests inject a deterministic extractor."""

    def __call__(self, entries: list[GraphEntry]) -> ProducerExtraction: ...


def produce_belief_candidate(
    *,
    entry_ids: Sequence[str],
    threads_dir: Path,
    topic: str,
    extract: EdgeExtractor,
    resolve_supersession: Optional[SupersessionResolver] = None,
) -> BeliefCandidate:
    """Produce a StabilizedBeliefCandidate from a set of source entries.

    Reads live entry bodies, runs the injected interpretive extractor, then does
    the *mechanical* work: byte-reverifies every span to set its tether, resolves
    currency (fail-closed ``unknown`` without a resolver), runs both gates, and
    builds the warrant read model. Returns the candidate; it writes nothing.
    """
    if not entry_ids:
        raise ValueError("produce_belief_candidate requires at least one entry_id")

    # Function-local import: keeps the pure gate core importable without the
    # baseline_graph package __init__ (git/FalkorDB) — see the module-top note.
    from watercooler.baseline_graph.reader import get_entries_by_ids

    # One pass over the thread, then O(1) lookups (not an O(N*M) per-id rescan).
    entries_by_id = get_entries_by_ids(threads_dir, topic, entry_ids)
    entries = [entries_by_id[eid] for eid in entry_ids if eid in entries_by_id]

    extraction = extract(entries)

    edges: list[FactEdge] = []
    edges_by_id: dict[str, FactEdge] = {}
    for proposed in extraction.edges:
        source_entry = entries_by_id.get(proposed.source_entry_id)
        source_body = source_entry.body if source_entry is not None else None
        source_type = source_entry.entry_type if source_entry is not None else None
        tether = tether_for_span(proposed.source_span, source_body, source_type)
        source_timestamp = canonicalize_timestamp(
            source_entry.timestamp if source_entry is not None else None
        )
        edge = FactEdge(
            edge_id=proposed.edge_id,
            claim=proposed.claim,
            relation=proposed.relation,
            anchors=proposed.anchors,
            source_entry_id=proposed.source_entry_id,
            source_span=proposed.source_span,
            tether=tether,
            supersession_state=SUPERSESSION_UNKNOWN,
            source_timestamp=source_timestamp,
        )
        if resolve_supersession is not None and source_entry is not None:
            try:
                state = resolve_supersession(edge, source_entry)
            except Exception:
                # The resolver is caller-supplied/untrusted; any failure fails closed to
                # ``unknown`` (which blocks clean-composition) rather than propagating.
                state = SUPERSESSION_UNKNOWN
            if state not in SUPERSESSION_STATES:
                state = SUPERSESSION_UNKNOWN
            edge = dataclasses.replace(edge, supersession_state=state)
        edges.append(edge)
        edges_by_id[edge.edge_id] = edge

    missing_conclusion = _dangling(extraction.conclusion_edge_ids, edges_by_id)
    if missing_conclusion:
        # A conclusion that references edges the extractor did not supply is malformed:
        # silently dropping them would let a multi-hop conclusion degrade into a shorter
        # path that can earn clean-composition. Fail loud rather than fail open.
        raise ValueError(
            "malformed extraction: conclusion_edge_ids reference unknown edges "
            f"{missing_conclusion}"
        )
    conclusion_edges = tuple(
        edges_by_id[eid] for eid in extraction.conclusion_edge_ids
    )

    # A disposition that names an edge_id the extractor never supplied is malformed:
    # silently nulling it to ``edge=None`` would let a mislabeled minority edge escape
    # the anti-mislabel reclassification (which needs the edge's anchors) — the one
    # mechanical guard of the disagreement gate. Fail loud, symmetric with the
    # conclusion-edge check above.
    bad_disp = _dangling(
        [d.edge_id for d in extraction.dispositions if d.edge_id], edges_by_id
    )
    if bad_disp:
        raise ValueError(
            f"malformed extraction: dispositions reference unknown edges {bad_disp}"
        )
    dispositions = tuple(
        InputDisposition(
            entry_id=d.entry_id,
            disposition=d.disposition,
            edge=edges_by_id.get(d.edge_id) if d.edge_id else None,
        )
        for d in extraction.dispositions
    )

    composition = check_composition(conclusion_edges)
    disagreement = check_disagreement_preservation(
        entry_ids, dispositions, conclusion_edges
    )
    warrant = _build_warrant(tuple(edges))

    return BeliefCandidate(
        conclusion=extraction.conclusion,
        edges=tuple(edges),
        conclusion_edges=conclusion_edges,
        dispositions=dispositions,
        requested_entry_ids=tuple(entry_ids),
        composition=composition,
        disagreement=disagreement,
        warrant=warrant,
    )
