"""Unit tests for the fact-edge substrate + producer (#897b).

Covers the substrate (tether mapping, normalization, timestamps), the two gates
(composition; disagreement preservation), the serialization round-trip, and the
on-demand producer. No paid LLM: the extractor is injected.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from watercooler import belief_candidate as bc
from watercooler.authority_support import TETHER_SOURCE
from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir
from watercooler.decision_extraction import _normalize_quote_text, normalize_quote_text

_TOPIC = "fact-edge-fixtures"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _edge(
    edge_id="e1",
    anchors=("X", "Y"),
    tether=TETHER_SOURCE,
    supersession=bc.SUPERSESSION_ACTIVE,
    ts="2026-06-08T10:00:00Z",
    claim="c",
    relation="rel",
    source_entry_id="s1",
    source_span="span",
):
    return bc.FactEdge(
        edge_id=edge_id,
        claim=claim,
        relation=relation,
        anchors=anchors,
        source_entry_id=source_entry_id,
        source_span=source_span,
        tether=tether,
        supersession_state=supersession,
        source_timestamp=ts,
    )


def _write_thread(threads_dir: Path, nodes: list[dict]) -> None:
    thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), _TOPIC)
    thread_dir.mkdir(parents=True, exist_ok=True)
    with open(thread_dir / "entries.jsonl", "w", encoding="utf-8") as fh:
        for i, node in enumerate(nodes):
            node.setdefault("index", i)
            node.setdefault("thread_topic", _TOPIC)
            fh.write(json.dumps(node) + "\n")


# --------------------------------------------------------------------------- #
# Phase 1 — substrate
# --------------------------------------------------------------------------- #


class TestNormalization:
    def test_public_wrapper_matches_private_alias(self):
        sample = "  the “team” chose—FalkorDB  "
        assert normalize_quote_text(sample) == _normalize_quote_text(sample)

    def test_normalize_anchor_folds_case_and_spacing(self):
        assert bc.normalize_anchor("FalkorDB") == bc.normalize_anchor("falkordb")
        assert bc.normalize_anchor("Falkor  DB") == bc.normalize_anchor("falkor db")

    def test_normalize_anchor_does_not_fold_distinct_terms(self):
        assert bc.normalize_anchor("FalkorDB") != bc.normalize_anchor("Neo4j")


class TestTimestamp:
    def test_z_form_is_canonical(self):
        assert bc.canonicalize_timestamp("2026-06-08T12:00:00Z") == "2026-06-08T12:00:00Z"

    def test_offset_form_normalizes_to_z(self):
        assert (
            bc.canonicalize_timestamp("2026-06-08T12:00:00+00:00")
            == "2026-06-08T12:00:00Z"
        )

    def test_non_utc_offset_converts_to_utc(self):
        assert (
            bc.canonicalize_timestamp("2026-06-08T13:00:00+01:00")
            == "2026-06-08T12:00:00Z"
        )

    def test_unparseable_returned_verbatim(self):
        # round-trips so the composition gate can fail it closed
        assert bc.canonicalize_timestamp("not-a-date") == "not-a-date"

    def test_preserves_subsecond_precision(self):
        # microseconds must survive canonicalization, else same-second reversed order
        # slips past check_composition's temporal_disorder check
        assert (
            bc.canonicalize_timestamp("2026-06-08T10:00:00.900000+00:00")
            == "2026-06-08T10:00:00.900000Z"
        )

    def test_subsecond_disorder_is_detected(self):
        path = [
            bc.FactEdge("e1", "c", "r", ("X", "Y"), "s1", "span", "source", "active",
                        "2026-06-08T10:00:00.900000Z"),
            bc.FactEdge("e2", "c", "r", ("Y", "Z"), "s2", "span", "source", "active",
                        "2026-06-08T10:00:00.100000Z"),
        ]
        assert "temporal_disorder" in bc.check_composition(path).defects

    def test_empty_returns_empty(self):
        assert bc.canonicalize_timestamp(None) == ""


class TestTetherMapping:
    BODY = "The team chose FalkorDB over Neo4j for the temporal index because of speed."

    def test_verified_ordinary_entry_is_source(self):
        assert (
            bc.tether_for_span("chose FalkorDB over Neo4j for the temporal index", self.BODY, "Note")
            == "source"
        )

    def test_verified_record_entry_is_record_state(self):
        assert (
            bc.tether_for_span("chose FalkorDB over Neo4j for the temporal index", self.BODY, "Decision")
            == "record_state"
        )

    def test_unverified_span_is_interpretive(self):
        assert bc.tether_for_span("a claim not present in the source body", self.BODY, "Note") == "interpretive"

    def test_below_floor_span_is_interpretive(self):
        assert bc.tether_for_span("short", self.BODY, "Note") == "interpretive"

    def test_no_span_is_unknown(self):
        assert bc.tether_for_span("", self.BODY, "Note") == "unknown"

    def test_unreadable_source_is_unknown(self):
        assert bc.tether_for_span("chose FalkorDB over Neo4j for the temporal index", None, "Note") == "unknown"


# --------------------------------------------------------------------------- #
# Phase 2 — composition gate
# --------------------------------------------------------------------------- #


class TestComposition:
    def test_clean_two_hop(self):
        path = [_edge("e1", ("X", "Y")), _edge("e2", ("Y", "Z"), ts="2026-06-08T11:00:00Z")]
        assert bc.check_composition(path).verdict == bc.COMPOSITION_CLEAN

    def test_single_edge_can_be_clean(self):
        assert bc.check_composition([_edge()]).verdict == bc.COMPOSITION_CLEAN

    def test_empty_path_is_interpretive_only(self):
        v = bc.check_composition([])
        assert v.verdict == bc.COMPOSITION_INTERPRETIVE_ONLY
        assert "empty_path" in v.defects

    def test_bridge_mismatch_fails(self):
        path = [_edge("e1", ("X", "Y")), _edge("e2", ("Q", "Z"))]
        assert "bridge_mismatch" in bc.check_composition(path).defects

    def test_bridge_match_is_normalization_insensitive(self):
        # same token, differing only in case + collapsible whitespace
        path = [_edge("e1", ("X", "Falkor  DB")), _edge("e2", ("falkor db", "Z"), ts="2026-06-08T11:00:00Z")]
        assert bc.check_composition(path).verdict == bc.COMPOSITION_CLEAN

    def test_superseded_fails_closed(self):
        path = [_edge("e1", supersession=bc.SUPERSESSION_SUPERSEDED)]
        assert "supersession_not_active" in bc.check_composition(path).defects

    def test_unknown_supersession_fails_closed(self):
        path = [_edge("e1", supersession=bc.SUPERSESSION_UNKNOWN)]
        assert "supersession_not_active" in bc.check_composition(path).defects

    def test_interpretive_tether_in_path_fails(self):
        path = [_edge("e1", tether="interpretive")]
        assert "non_substantive_tether" in bc.check_composition(path).defects

    def test_unparseable_timestamp_fails(self):
        path = [_edge("e1", ts="garbage")]
        assert "unparseable_timestamp" in bc.check_composition(path).defects

    def test_temporal_disorder_fails(self):
        path = [
            _edge("e1", ("X", "Y"), ts="2026-06-08T12:00:00Z"),
            _edge("e2", ("Y", "Z"), ts="2026-06-08T10:00:00Z"),
        ]
        assert "temporal_disorder" in bc.check_composition(path).defects

    def test_empty_anchor_edge_is_rejected_at_construction(self):
        # empty/whitespace anchors normalize to "" and would collide under the bridge
        # check, earning clean-composition for a discontinuous path -> reject at the type
        for anchors in [("alpha", "   "), ("", "beta"), ("\t", "x")]:
            with pytest.raises(ValueError, match="non-empty after normalization"):
                _edge("e1", anchors)

    def test_verdict_is_not_support_metadata(self):
        # the clean-composition verdict must never appear inside the warrant model
        warrant = bc._build_warrant([_edge("e1", ("X", "Y")), _edge("e2", ("Y", "Z"))])
        flat = str(dataclasses.asdict(warrant)).lower()
        assert "clean-composition" not in flat
        assert "composition" not in flat
        # and no field name carries a composition verdict
        assert not any("composition" in f.lower() for f in vars(warrant))


# --------------------------------------------------------------------------- #
# Phase 2 — disagreement preservation gate
# --------------------------------------------------------------------------- #


class TestDisagreementPreservation:
    def _concl(self):
        return [_edge("c1", ("FalkorDB", "Neo4j"))]

    def test_complete_when_every_input_dispositioned_once(self):
        disp = [
            bc.InputDisposition("a", bc.DISPOSITION_INCORPORATED),
            bc.InputDisposition("b", bc.DISPOSITION_INCORPORATED),
        ]
        res = bc.check_disagreement_preservation(["a", "b"], disp, self._concl())
        assert res.complete and res.missing_inputs == ()

    def test_missing_input_fails_completeness(self):
        disp = [bc.InputDisposition("a", bc.DISPOSITION_INCORPORATED)]
        res = bc.check_disagreement_preservation(["a", "b"], disp, self._concl())
        assert not res.complete and "b" in res.missing_inputs

    def test_duplicate_disposition_fails_completeness(self):
        disp = [
            bc.InputDisposition("a", bc.DISPOSITION_INCORPORATED),
            bc.InputDisposition("a", bc.DISPOSITION_DROPPED),
        ]
        res = bc.check_disagreement_preservation(["a"], disp, self._concl())
        assert not res.complete and "a" in res.missing_inputs

    def test_declared_contradicting_surfaces(self):
        disp = [bc.InputDisposition("a", bc.DISPOSITION_CONTRADICTING, edge=_edge("m", ("P", "Q")))]
        res = bc.check_disagreement_preservation(["a"], disp, self._concl())
        assert len(res.active_disagreements) == 1

    def test_dropped_with_anchor_overlap_is_reclassified(self):
        # minority edge touches FalkorDB (a conclusion anchor) -> forced to contradicting
        minority = _edge("m", ("FalkorDB", "Postgres"))
        disp = [bc.InputDisposition("a", bc.DISPOSITION_DROPPED, edge=minority)]
        res = bc.check_disagreement_preservation(["a"], disp, self._concl())
        assert len(res.active_disagreements) == 1
        assert res.active_disagreements[0].disposition == bc.DISPOSITION_CONTRADICTING
        assert res.excluded == ()

    def test_dropped_without_overlap_is_excluded(self):
        offtopic = _edge("m", ("Slack", "Discord"))
        disp = [bc.InputDisposition("a", bc.DISPOSITION_DROPPED, edge=offtopic)]
        res = bc.check_disagreement_preservation(["a"], disp, self._concl())
        assert res.active_disagreements == ()
        assert len(res.excluded) == 1

    def test_dropped_without_edge_fails_closed_to_active(self):
        # a `dropped` item with no edge can't be shown off-topic -> must be preserved,
        # not silently excluded (otherwise a producer hides a minority by omitting the edge)
        disp = [bc.InputDisposition("a", bc.DISPOSITION_DROPPED, edge=None)]
        res = bc.check_disagreement_preservation(["a"], disp, self._concl())
        assert len(res.active_disagreements) == 1
        assert res.active_disagreements[0].disposition == bc.DISPOSITION_CONTRADICTING
        assert res.excluded == ()

    def test_unrecognized_disposition_value_fails_completeness(self):
        # LLM label drift / typo must not pass as validly accounted for
        disp = [bc.InputDisposition("a", "incorprated")]  # typo
        res = bc.check_disagreement_preservation(["a"], disp, self._concl())
        assert not res.complete and "a" in res.missing_inputs


# --------------------------------------------------------------------------- #
# Phase 3 — serialization round-trip
# --------------------------------------------------------------------------- #


def _candidate(conclusion_edges, dispositions, requested):
    edges = list(conclusion_edges)
    for d in dispositions:
        if d.edge is not None and d.edge.edge_id not in {e.edge_id for e in edges}:
            edges.append(d.edge)
    composition = bc.check_composition(conclusion_edges)
    disagreement = bc.check_disagreement_preservation(requested, dispositions, conclusion_edges)
    warrant = bc._build_warrant(edges)
    return bc.BeliefCandidate(
        conclusion="FalkorDB was chosen over Neo4j.",
        edges=tuple(edges),
        conclusion_edges=tuple(conclusion_edges),
        dispositions=tuple(dispositions),
        requested_entry_ids=tuple(requested),
        composition=composition,
        disagreement=disagreement,
        warrant=warrant,
    )


class TestSerializationRoundTrip:
    def _sample(self):
        concl = [_edge("c1", ("FalkorDB", "Neo4j"))]
        disp = [
            bc.InputDisposition("s1", bc.DISPOSITION_INCORPORATED, edge=concl[0]),
            bc.InputDisposition("s2", bc.DISPOSITION_DROPPED, edge=_edge("m", ("FalkorDB", "Postgres"))),
        ]
        return _candidate(concl, disp, ["s1", "s2"])

    def test_round_trip_preserves_gateable_fields(self):
        cand = self._sample()
        parsed = bc.parse_candidate_ledger(bc.render_candidate_body(cand))
        assert parsed.edges == cand.edges
        assert parsed.conclusion_edges == cand.conclusion_edges
        assert parsed.dispositions == cand.dispositions
        assert parsed.composition == cand.composition
        assert parsed.warrant == cand.warrant

    def test_gates_recompute_equal_across_round_trip(self):
        cand = self._sample()
        parsed = bc.parse_candidate_ledger(bc.render_candidate_body(cand))
        # recompute (not echo) the composition verdict from parsed edges
        recomputed = bc.check_composition(parsed.conclusion_edges)
        assert recomputed == cand.composition
        assert parsed.disagreement == cand.disagreement

    def test_ledger_block_has_no_blockquote_line(self):
        # a span containing '>' must not produce a line starting with '> '
        concl = [_edge("c1", ("X", "Y"), source_span="we said a > b in the thread body")]
        cand = _candidate(concl, [bc.InputDisposition("s1", bc.DISPOSITION_INCORPORATED, edge=concl[0])], ["s1"])
        body = bc.render_candidate_body(cand)
        block = body.split(bc._LEDGER_BEGIN)[1].split(bc._LEDGER_END)[0]
        assert not any(line.startswith("> ") for line in block.splitlines())

    def test_missing_gate_field_rejected(self):
        cand = self._sample()
        body = bc.render_candidate_body(cand)
        # strip supersession_state from the serialized ledger
        broken = body.replace(',"supersession_state":"active"', "", 1)
        with pytest.raises(ValueError, match="missing fields"):
            bc.parse_candidate_ledger(broken)

    def test_missing_ledger_block_rejected(self):
        with pytest.raises(ValueError, match="no warrant-ledger block"):
            bc.parse_candidate_ledger("# just prose, no ledger\n")

    def test_unknown_schema_rejected(self):
        cand = self._sample()
        body = bc.render_candidate_body(cand).replace(bc.LEDGER_SCHEMA, "belief-candidate/999")
        with pytest.raises(ValueError, match="unsupported ledger schema"):
            bc.parse_candidate_ledger(body)

    def test_parse_recomputes_composition_not_trusts_stored(self):
        # a clean single-edge candidate whose ledger is tampered: supersession flipped to
        # unknown but the stored verdict left as clean-composition. Parse must recompute.
        cand = _candidate([_edge("c1", ("X", "Y"))], [
            bc.InputDisposition("s1", bc.DISPOSITION_INCORPORATED, edge=_edge("c1", ("X", "Y")))
        ], ["s1"])
        assert cand.composition.verdict == bc.COMPOSITION_CLEAN
        body = bc.render_candidate_body(cand)
        assert '"verdict":"clean-composition"' in body  # stored verdict stays clean
        tampered = body.replace('"supersession_state":"active"', '"supersession_state":"unknown"')
        parsed = bc.parse_candidate_ledger(tampered)
        assert parsed.composition.verdict == bc.COMPOSITION_INTERPRETIVE_ONLY
        assert "supersession_not_active" in parsed.composition.defects

    def test_dangling_conclusion_edge_id_rejected(self):
        cand = self._sample()
        body = bc.render_candidate_body(cand).replace('"conclusion_edges":["c1"]', '"conclusion_edges":["ghost"]')
        with pytest.raises(ValueError, match="unknown edges"):
            bc.parse_candidate_ledger(body)

    def test_multiple_ledger_blocks_rejected(self):
        cand = self._sample()
        body = bc.render_candidate_body(cand)
        # a second (e.g. quoted) ledger block must not be able to shadow the canonical one
        block = body[body.index(bc._LEDGER_BEGIN) : body.index(bc._LEDGER_END) + len(bc._LEDGER_END)]
        with pytest.raises(ValueError, match="multiple warrant-ledger blocks"):
            bc.parse_candidate_ledger(body + "\n" + block + "\n")

    def test_missing_requested_entry_ids_rejected(self):
        cand = self._sample()  # requested = ["s1", "s2"]
        body = bc.render_candidate_body(cand).replace(
            '"requested_entry_ids":["s1","s2"],', "", 1
        )
        with pytest.raises(ValueError, match="missing requested_entry_ids"):
            bc.parse_candidate_ledger(body)

    def test_dangling_disposition_edge_id_rejected(self):
        cand = self._sample()  # has a dropped disposition pointing at edge "m"
        # rename only the disposition's edge ref (not the edge definition's own id)
        body = bc.render_candidate_body(cand).replace(
            '"disposition":"dropped","edge_id":"m"',
            '"disposition":"dropped","edge_id":"ghost"',
        )
        with pytest.raises(ValueError, match="disposition references unknown edge"):
            bc.parse_candidate_ledger(body)

    def _tamper(self, find, replace):
        return bc.render_candidate_body(self._sample()).replace(find, replace, 1)

    def test_non_object_toplevel_rejected(self):
        body = (
            f"{bc._LEDGER_BEGIN}\n```json\n[1,2,3]\n```\n{bc._LEDGER_END}\n"
        )
        with pytest.raises(ValueError, match="top-level value must be an object"):
            bc.parse_candidate_ledger(body)

    def test_requested_entry_ids_as_string_rejected(self):
        # a bare string would iterate into characters and corrupt the gate input set
        body = self._tamper('"requested_entry_ids":["s1","s2"]', '"requested_entry_ids":"abc"')
        with pytest.raises(ValueError, match="requested_entry_ids must be a list"):
            bc.parse_candidate_ledger(body)

    def test_non_string_edge_field_rejected(self):
        body = self._tamper('"tether":"source"', '"tether":5')
        with pytest.raises(ValueError, match="must be a string"):
            bc.parse_candidate_ledger(body)

    def test_duplicate_edge_id_rejected(self):
        # two edges sharing an id would collapse in edges_by_id; a conclusion ref could
        # then resolve to one while the other hides -> reject
        concl = [_edge("dup", ("X", "Y")), _edge("dup", ("Y", "Z"), ts="2026-06-08T11:00:00Z")]
        cand = _candidate(concl[:1], [bc.InputDisposition("s1", bc.DISPOSITION_INCORPORATED, edge=concl[0])], ["s1"])
        # inject a second edge with the same id into the serialized edges array
        body = bc.render_candidate_body(cand)
        dup_edge = '{"edge_id":"dup","claim":"c","relation":"rel","anchors":["Y","Z"],"source_entry_id":"s1","source_span":"span","tether":"source","supersession_state":"active","source_timestamp":"2026-06-08T11:00:00Z"}'
        body = body.replace('"edges":[', f'"edges":[{dup_edge},', 1)
        with pytest.raises(ValueError, match="duplicate edge_id"):
            bc.parse_candidate_ledger(body)

    def test_source_span_containing_brace_round_trips(self):
        # a '}' inside source_span must not truncate the ledger capture
        concl = [_edge("c1", ("X", "Y"), source_span="payload with a } brace and enough length")]
        cand = _candidate(concl, [bc.InputDisposition("s1", bc.DISPOSITION_INCORPORATED, edge=concl[0])], ["s1"])
        parsed = bc.parse_candidate_ledger(bc.render_candidate_body(cand))
        assert parsed.edges[0].source_span == "payload with a } brace and enough length"

    def test_source_span_containing_ledger_terminator_round_trips(self):
        # a span quoting a previous ledger's closing fence + end-sentinel must not
        # terminate the capture early (the JSON is one physical line)
        nasty = "quoted: ```<!-- warrant-ledger:end --> trailing text after the sentinel"
        concl = [_edge("c1", ("X", "Y"), source_span=nasty)]
        cand = _candidate(concl, [bc.InputDisposition("s1", bc.DISPOSITION_INCORPORATED, edge=concl[0])], ["s1"])
        parsed = bc.parse_candidate_ledger(bc.render_candidate_body(cand))
        assert parsed.edges[0].source_span == nasty


class TestBeliefCandidateInvariant:
    def test_duplicate_edge_id_in_candidate_rejected(self):
        dup_a = _edge("dup", ("X", "Y"))
        dup_b = _edge("dup", ("Y", "Z"), ts="2026-06-08T11:00:00Z")
        with pytest.raises(ValueError, match="duplicate edge_id"):
            bc.BeliefCandidate(
                conclusion="c",
                edges=(dup_a, dup_b),
                conclusion_edges=(dup_a,),
                dispositions=(),
                requested_entry_ids=(),
                composition=bc.check_composition([dup_a]),
                disagreement=bc.DisagreementResult(True, (), (), ()),
                warrant=bc._build_warrant([dup_a]),
            )

    def test_conclusion_edge_not_in_edges_rejected(self):
        stray = _edge("stray", ("X", "Y"))
        with pytest.raises(ValueError, match="not in `edges`"):
            bc.BeliefCandidate(
                conclusion="c",
                edges=(),  # stray is not here
                conclusion_edges=(stray,),
                dispositions=(),
                requested_entry_ids=(),
                composition=bc.check_composition([stray]),
                disagreement=bc.DisagreementResult(True, (), (), ()),
                warrant=bc._build_warrant([stray]),
            )


# --------------------------------------------------------------------------- #
# Phase 4 — producer
# --------------------------------------------------------------------------- #


def _extractor(extraction: bc.ProducerExtraction):
    def _extract(entries):
        return extraction

    return _extract


class TestProducer:
    BODY1 = "The team chose FalkorDB over Neo4j for the temporal index, decisively."
    BODY2 = "FalkorDB was preferred over Postgres for graph workloads, the note says."

    def _threads_dir(self, tmp_path: Path) -> Path:
        _write_thread(
            tmp_path,
            [
                {"entry_id": "s1", "entry_type": "Note", "timestamp": "2026-06-08T10:00:00+00:00", "body": self.BODY1},
                {"entry_id": "s2", "entry_type": "Note", "timestamp": "2026-06-08T11:00:00Z", "body": self.BODY2},
            ],
        )
        return tmp_path

    def test_empty_entry_ids_raises(self, tmp_path):
        with pytest.raises(ValueError, match="at least one entry_id"):
            bc.produce_belief_candidate(
                entry_ids=[], threads_dir=tmp_path, topic=_TOPIC, extract=_extractor(None)
            )

    def test_end_to_end_clean_single_edge(self, tmp_path):
        threads = self._threads_dir(tmp_path)
        extraction = bc.ProducerExtraction(
            conclusion="FalkorDB was chosen over Neo4j.",
            edges=(
                bc.ProposedEdge(
                    edge_id="e1",
                    claim="FalkorDB chosen over Neo4j",
                    relation="chosen-over",
                    anchors=("FalkorDB", "Neo4j"),
                    source_entry_id="s1",
                    source_span="chose FalkorDB over Neo4j for the temporal index",
                ),
            ),
            conclusion_edge_ids=("e1",),
            dispositions=(bc.ProposedDisposition("s1", bc.DISPOSITION_INCORPORATED, "e1"),),
        )
        # supersession resolver supplies active currency
        cand = bc.produce_belief_candidate(
            entry_ids=["s1"],
            threads_dir=threads,
            topic=_TOPIC,
            extract=_extractor(extraction),
            resolve_supersession=lambda edge, entry: bc.SUPERSESSION_ACTIVE,
        )
        assert cand.edges[0].tether == "source"
        assert cand.edges[0].source_timestamp == "2026-06-08T10:00:00Z"  # +00:00 normalized
        assert cand.composition.verdict == bc.COMPOSITION_CLEAN
        # round-trips
        assert bc.parse_candidate_ledger(bc.render_candidate_body(cand)).edges == cand.edges

    def test_no_resolver_yields_unknown_currency_and_blocks_clean(self, tmp_path):
        threads = self._threads_dir(tmp_path)
        extraction = bc.ProducerExtraction(
            conclusion="FalkorDB was chosen over Neo4j.",
            edges=(
                bc.ProposedEdge("e1", "c", "chosen-over", ("FalkorDB", "Neo4j"), "s1",
                                "chose FalkorDB over Neo4j for the temporal index"),
            ),
            conclusion_edge_ids=("e1",),
            dispositions=(bc.ProposedDisposition("s1", bc.DISPOSITION_INCORPORATED, "e1"),),
        )
        cand = bc.produce_belief_candidate(
            entry_ids=["s1"], threads_dir=threads, topic=_TOPIC, extract=_extractor(extraction)
        )
        assert cand.edges[0].supersession_state == bc.SUPERSESSION_UNKNOWN
        assert cand.composition.verdict == bc.COMPOSITION_INTERPRETIVE_ONLY

    def _basic_extraction(self):
        return bc.ProducerExtraction(
            conclusion="FalkorDB was chosen over Neo4j.",
            edges=(
                bc.ProposedEdge("e1", "c", "chosen-over", ("FalkorDB", "Neo4j"), "s1",
                                "chose FalkorDB over Neo4j for the temporal index"),
            ),
            conclusion_edge_ids=("e1",),
            dispositions=(bc.ProposedDisposition("s1", bc.DISPOSITION_INCORPORATED, "e1"),),
        )

    def test_resolver_exception_fails_closed_to_unknown(self, tmp_path):
        def _boom(edge, entry):
            raise RuntimeError("resolver backend down")

        cand = bc.produce_belief_candidate(
            entry_ids=["s1"], threads_dir=self._threads_dir(tmp_path), topic=_TOPIC,
            extract=_extractor(self._basic_extraction()), resolve_supersession=_boom,
        )
        assert cand.edges[0].supersession_state == bc.SUPERSESSION_UNKNOWN

    def test_resolver_invalid_state_fails_closed_to_unknown(self, tmp_path):
        cand = bc.produce_belief_candidate(
            entry_ids=["s1"], threads_dir=self._threads_dir(tmp_path), topic=_TOPIC,
            extract=_extractor(self._basic_extraction()),
            resolve_supersession=lambda edge, entry: "bogus-state",
        )
        assert cand.edges[0].supersession_state == bc.SUPERSESSION_UNKNOWN

    def test_disposition_referencing_unknown_edge_raises(self, tmp_path):
        threads = self._threads_dir(tmp_path)
        extraction = bc.ProducerExtraction(
            conclusion="FalkorDB was chosen over Neo4j.",
            edges=(
                bc.ProposedEdge("e1", "c", "chosen-over", ("FalkorDB", "Neo4j"), "s1",
                                "chose FalkorDB over Neo4j for the temporal index"),
            ),
            conclusion_edge_ids=("e1",),
            # disposition points at an edge id the extractor never supplied -> must fail
            # loud (else a mislabeled minority escapes anti-mislabel reclassification)
            dispositions=(
                bc.ProposedDisposition("s1", bc.DISPOSITION_INCORPORATED, "e1"),
                bc.ProposedDisposition("s2", bc.DISPOSITION_DROPPED, "ghost"),
            ),
        )
        with pytest.raises(ValueError, match="dispositions reference unknown edges"):
            bc.produce_belief_candidate(
                entry_ids=["s1", "s2"], threads_dir=threads, topic=_TOPIC, extract=_extractor(extraction)
            )

    def test_duplicate_extractor_edge_id_raises(self, tmp_path):
        threads = self._threads_dir(tmp_path)
        span = "chose FalkorDB over Neo4j for the temporal index"
        extraction = bc.ProducerExtraction(
            conclusion="FalkorDB was chosen over Neo4j.",
            edges=(
                bc.ProposedEdge("e1", "c", "r", ("FalkorDB", "Neo4j"), "s1", span),
                bc.ProposedEdge("e1", "c2", "r2", ("Neo4j", "X"), "s1", span),  # dup id
            ),
            conclusion_edge_ids=("e1",),
            dispositions=(bc.ProposedDisposition("s1", bc.DISPOSITION_INCORPORATED, "e1"),),
        )
        with pytest.raises(ValueError, match="duplicate edge_id"):
            bc.produce_belief_candidate(
                entry_ids=["s1"], threads_dir=threads, topic=_TOPIC, extract=_extractor(extraction)
            )

    def test_missing_conclusion_edge_id_raises(self, tmp_path):
        threads = self._threads_dir(tmp_path)
        extraction = bc.ProducerExtraction(
            conclusion="a two-hop conclusion missing its second edge",
            edges=(
                bc.ProposedEdge("e1", "c", "rel", ("X", "Y"), "s1",
                                "chose FalkorDB over Neo4j for the temporal index"),
            ),
            conclusion_edge_ids=("e1", "e2_missing"),  # e2 never supplied
            dispositions=(bc.ProposedDisposition("s1", bc.DISPOSITION_INCORPORATED, "e1"),),
        )
        with pytest.raises(ValueError, match="unknown edges"):
            bc.produce_belief_candidate(
                entry_ids=["s1"], threads_dir=threads, topic=_TOPIC, extract=_extractor(extraction)
            )

    def test_unreadable_entry_yields_unknown_tether(self, tmp_path):
        threads = self._threads_dir(tmp_path)
        extraction = bc.ProducerExtraction(
            conclusion="claim about a missing entry",
            edges=(
                bc.ProposedEdge("e1", "c", "rel", ("A", "B"), "ghost", "some span text that is long enough"),
            ),
            conclusion_edge_ids=("e1",),
            dispositions=(bc.ProposedDisposition("ghost", bc.DISPOSITION_INCORPORATED, "e1"),),
        )
        cand = bc.produce_belief_candidate(
            entry_ids=["ghost"], threads_dir=threads, topic=_TOPIC, extract=_extractor(extraction)
        )
        assert cand.edges[0].tether == "unknown"
