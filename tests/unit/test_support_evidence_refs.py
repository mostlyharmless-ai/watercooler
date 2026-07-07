"""Resolvable ``support_evidence`` refs (C2,
thread candidate-research-backend-support).

Evidence pointers that reference an entry now carry ``topic`` and ``index``
when the producer knows where that entry lives, so consumers (the dashboard's
evidence drill-down, hover cards) can build a jump link in one step instead of
resolving the bare ULID first.

Contracts under test:
- ``derive_candidate_support``: entry_id-bearing pointers (verified_quote,
  source_is_*) carry topic/index when supplied; pointers WITHOUT an entry_id
  never gain location fields; omitted inputs keep the legacy minimal shape.
- ``candidate_warrant`` (extraction): location read straight off the source
  entry node's ``thread_topic``/``index``.
- ``plan_promotion``: location of the LIVE-resolved source entry flows into
  the promoted Decision's structured ``decision_support_fields`` — coming from
  the resolution, never assumed equal to the candidate's topic (the source may
  live on another thread).
"""

from __future__ import annotations

from watercooler.authority_support import derive_candidate_support
from watercooler.decision_extraction import (
    ExtractionResult,
    LLMExtraction,
    candidate_warrant,
)
from watercooler.promotion import plan_promotion


def _evidence_by_label(model, label: str) -> list[dict]:
    return [ev for ev in model.support_evidence if ev.get("label") == label]


class TestDeriveCandidateSupportRefs:
    def test_verified_quote_and_record_state_carry_topic_and_index(self):
        model = derive_candidate_support(
            source_entry_id="01SRC0000000000000000000AA",
            verbatim_quotes=["a verified quote"],
            quote_verified=True,
            source_entry_type="Decision",
            source_topic="alpha-thread",
            source_index=7,
        )
        for label in ("verified_quote", "source_is_decision"):
            (ev,) = _evidence_by_label(model, label)
            assert ev["entry_id"] == "01SRC0000000000000000000AA"
            assert ev["topic"] == "alpha-thread"
            assert ev["index"] == 7

    def test_pointers_without_entry_id_never_gain_location(self):
        model = derive_candidate_support(
            source_entry_id=None,
            verbatim_quotes=["an unverified quote"],
            quote_verified=False,
            source_topic="alpha-thread",
            source_index=7,
        )
        for ev in model.support_evidence:
            assert "topic" not in ev
            assert "index" not in ev

    def test_omitted_location_keeps_legacy_shape(self):
        model = derive_candidate_support(
            source_entry_id="01SRC0000000000000000000AA",
            verbatim_quotes=["a verified quote"],
            quote_verified=True,
        )
        (ev,) = _evidence_by_label(model, "verified_quote")
        assert "topic" not in ev
        assert "index" not in ev

    def test_index_zero_is_a_real_location(self):
        model = derive_candidate_support(
            source_entry_id="01SRC0000000000000000000AA",
            verbatim_quotes=["a verified quote"],
            quote_verified=True,
            source_topic="alpha-thread",
            source_index=0,
        )
        (ev,) = _evidence_by_label(model, "verified_quote")
        assert ev["index"] == 0


class TestCandidateWarrantRefs:
    def test_location_read_from_source_entry_node(self):
        extraction = LLMExtraction(
            gates={},
            confidence=4,
            decision_statement="We will adopt X.",
            rationale=None,
            scope=None,
            alternatives_considered=None,
            verbatim_quotes=["we decided to adopt X"],
            warning=None,
        )
        result = ExtractionResult(
            entry_id="01SRC0000000000000000000AA",
            topic="beta-thread",
            passed=True,
            confidence=4,
            gate_results={},
            decision_body=None,
            rejection_reason=None,
            extraction=extraction,
        )
        entry = {
            "entry_id": "01SRC0000000000000000000AA",
            "entry_type": "Note",
            "thread_topic": "beta-thread",
            "index": 3,
            "body": "we decided to adopt X",
        }
        model = candidate_warrant(result, entry)
        assert model is not None
        (ev,) = _evidence_by_label(model, "verified_quote")
        assert ev["topic"] == "beta-thread"
        assert ev["index"] == 3


CANDIDATE_BODY = """Spec: decision-extractor
Candidate-Type: Decision
Candidate-Status: needs_human_confirmation
Surface-Kind: decision
Confidence: 4/5
Source-Entry: 01SRC0000000000000000000AA

## Candidate Decision
Adopt X for the sync path.

## Why this is a candidate, not a Decision
Needs a human call.

## Evidence
> we decided to adopt X
"""


class TestPlanPromotionRefs:
    def test_promoted_decision_support_fields_carry_source_location(self):
        plan = plan_promotion(
            candidate_body=CANDIDATE_BODY,
            candidate_entry_id="01CAND000000000000000000AA",
            candidate_topic="candidate-thread",
            target_type="Decision",
            human_authorized_by="github:caleb",
            quote_verified=True,
            source_entry_type="Decision",
            # The live resolution found the source on ANOTHER thread — the
            # stamped location must be the resolved one, not candidate_topic.
            source_topic="other-thread",
            source_index=12,
        )
        evidence = plan.decision_support_fields["support_evidence"]
        located = [ev for ev in evidence if ev.get("entry_id")]
        assert located, "expected entry_id-bearing evidence pointers"
        for ev in located:
            assert ev["topic"] == "other-thread"
            assert ev["index"] == 12

    def test_unreadable_source_keeps_legacy_shape(self):
        plan = plan_promotion(
            candidate_body=CANDIDATE_BODY,
            candidate_entry_id="01CAND000000000000000000AA",
            candidate_topic="candidate-thread",
            target_type="Decision",
            human_authorized_by="github:caleb",
            quote_verified=False,
        )
        evidence = plan.decision_support_fields.get("support_evidence", [])
        for ev in evidence:
            assert "topic" not in ev
            assert "index" not in ev
