"""Learning-candidate promotion path (`target_type="Learning"`).

A learning candidate promotes to a durable in-thread ``## Lesson`` Note — a
human-confirmed lesson, not a Decision and not a §6-warranted entry. The Note's
``## Lesson`` heading is matched by the Learnings daemon's in-thread-lesson signal,
so promoting retires the source thread's capture-gap. These tests pin that path and
guard that the Decision path is unaffected.
"""

from __future__ import annotations

import pytest

from watercooler.learning_extraction import _find_in_thread_lesson
from watercooler.learning_synthesis import (
    LearningDraft,
    SynthesisResult,
    format_learning_candidate_body,
)
from watercooler.promotion import (
    VALID_TARGET_TYPES,
    PromotionError,
    build_promotion_authority_fields,
    parse_candidate_body,
    plan_promotion,
)

CAND = "01CAND00000000000000000000"
AUTH = "github:calebjacksonhoward"


def _learning_candidate_body(
    *, lesson="Guard sync writes against silent push failure",
    root_cause="push errors were swallowed and reported as success",
    fix="check return codes; surface push failures",
    quotes=("push failed but returned 0", "success printed on error"),
    confidence=5, topic="bug-sync-push-silent-success", prs=(123,),
) -> str:
    """Build a realistic learning candidate body via the production formatter."""
    draft = LearningDraft(
        root_cause=root_cause, lesson=lesson, problem_summary="silent push failure",
        fix_summary=fix, confidence=confidence, verbatim_quotes=list(quotes), warning=None,
    )
    res = SynthesisResult(
        topic=topic, passed=True, confidence=confidence, draft_body="",
        rejection_reason=None, draft=draft,
    )
    return format_learning_candidate_body(res, topic=topic, pr_numbers=list(prs))


class TestLearningTargetEnabled:
    def test_learning_in_valid_targets(self):
        assert "Learning" in VALID_TARGET_TYPES


class TestParseLearningSections:
    def test_parses_lesson_root_cause_fix_and_verbatim_evidence(self):
        meta = parse_candidate_body(_learning_candidate_body(), CAND, "topic")
        assert meta.candidate_type == "Learning"
        assert meta.lesson_statement == "Guard sync writes against silent push failure"
        assert meta.root_cause.startswith("push errors were swallowed")
        assert meta.fix.startswith("check return codes")
        # `## Evidence (verbatim)` blockquotes are captured (not just (unverified)).
        assert meta.evidence_quotes == [
            "push failed but returned 0",
            "success printed on error",
        ]


class TestPlanLearningPromotion:
    def test_promotes_to_lesson_note_that_retires_the_gap(self):
        plan = plan_promotion(
            candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
            candidate_topic="bug-sync-push-silent-success", target_type="Learning",
            human_authorized_by=AUTH,
        )
        # Promoted entry is a Note, not a Decision; no §6 warrant.
        assert plan.decision_entry_type == "Note"
        assert plan.decision_support_fields is None
        body = plan.decision_body
        assert "Spec: learnings-promoted" in body
        assert "Authority-Basis: human_promoted" in body
        assert f"Promoted-From: {CAND}" in body
        assert "## Lesson" in body
        assert "## Root cause" in body and "## Fix" in body
        # The lesson body satisfies the daemon's in-thread-lesson signal → the
        # source thread becomes has_learning and the capture-gap retires.
        entry = {"entry_id": "01LESSON0000000000000000AA", "title": plan.decision_title, "body": body}
        assert _find_in_thread_lesson([entry]) == "01LESSON0000000000000000AA"

    def test_disposition_names_learning_not_decision(self):
        plan = plan_promotion(
            candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
            candidate_topic="t", target_type="Learning", human_authorized_by=AUTH,
        )
        assert "CandidateDisposition: promoted" in plan.disposition_body
        assert "to Learning" in plan.disposition_body
        assert "to Decision" not in plan.disposition_body

    def test_edits_override_lesson(self):
        plan = plan_promotion(
            candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
            candidate_topic="t", target_type="Learning", human_authorized_by=AUTH,
            edits={"lesson": "Always verify push parity after a write"},
        )
        assert "Always verify push parity after a write" in plan.decision_body


class TestRefineProvenance:
    """`Promotion-Edits:` records which fields the human changed on a refine."""

    def test_no_edits_stamps_no_marker(self):
        plan = plan_promotion(
            candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
            candidate_topic="t", target_type="Learning", human_authorized_by=AUTH,
        )
        assert "Promotion-Edits:" not in plan.decision_body

    def test_edited_lesson_is_recorded(self):
        plan = plan_promotion(
            candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
            candidate_topic="t", target_type="Learning", human_authorized_by=AUTH,
            edits={"lesson": "Generalize: surface silent failures, never swallow them"},
        )
        assert "Promotion-Edits: lesson" in plan.decision_body

    def test_multiple_edited_fields_listed(self):
        plan = plan_promotion(
            candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
            candidate_topic="t", target_type="Learning", human_authorized_by=AUTH,
            edits={"lesson": "reworded lesson", "fix": "reworded fix"},
        )
        assert "Promotion-Edits: lesson, fix" in plan.decision_body

    def test_edit_equal_to_original_is_not_counted(self):
        # Passing the candidate's own value back is not a real edit → no marker.
        body = _learning_candidate_body(lesson="Guard sync writes against silent push failure")
        plan = plan_promotion(
            candidate_body=body, candidate_entry_id=CAND,
            candidate_topic="t", target_type="Learning", human_authorized_by=AUTH,
            edits={"lesson": "Guard sync writes against silent push failure"},
        )
        assert "Promotion-Edits:" not in plan.decision_body

    def test_no_candidate_learning_section_rejected(self):
        body = "Candidate-Type: Learning\nCandidate-Status: needs_human_confirmation\n\n## Fix\nx\n"
        with pytest.raises(PromotionError, match="Candidate learning"):
            plan_promotion(
                candidate_body=body, candidate_entry_id=CAND, candidate_topic="t",
                target_type="Learning", human_authorized_by=AUTH,
            )

    def test_missing_authorizer_rejected(self):
        with pytest.raises(PromotionError, match="human_authorized_by"):
            plan_promotion(
                candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
                candidate_topic="t", target_type="Learning", human_authorized_by="<>",
            )


class TestLearningAuthorityFields:
    def test_learning_omits_decision_origin(self):
        # A promoted lesson is a Note, not a Decision — it must not claim
        # decision_origin (review #1). authority_basis + human_authorized_by stay.
        fields = build_promotion_authority_fields(
            human_authorized_by=AUTH, source_entry_id=CAND, target_type="Learning"
        )
        assert "decision_origin" not in fields
        assert fields["authority_basis"] == "human_promoted"
        assert fields["human_authorized_by"] == AUTH

    def test_decision_still_stamps_decision_origin(self):
        fields = build_promotion_authority_fields(
            human_authorized_by=AUTH, source_entry_id=CAND  # default target_type=Decision
        )
        assert fields["decision_origin"] == "human_promoted"


class TestDoublePromotionGuardForLearning:
    def test_quoting_note_without_promotion_spec_does_not_block(self):
        # The exact edge _PROMOTED_SPEC_RE defends: a plain Note that quotes the
        # authority markers in prose (no `Spec: *-promoted`) must NOT false-block.
        quoting = {
            "entry_id": "01QUOTE000000000000000000A",
            "entry_type": "Note",
            "body": (
                "Spec: general\nDiscussion of the promotion mechanism.\n"
                f"Promoted-From: {CAND}\nAuthority-Basis: human_promoted\n"
            ),
        }
        plan = plan_promotion(
            candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
            candidate_topic="t", target_type="Learning", human_authorized_by=AUTH,
            existing_thread_entries=[quoting],
        )
        assert plan.decision_entry_type == "Note"

    def test_prior_promoted_lesson_note_blocks_repromotion(self):
        # A promoted lesson Note carries Promoted-From + Authority-Basis:
        # human_promoted on a Note — the guard must catch it (#886 parity).
        plan = plan_promotion(
            candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
            candidate_topic="t", target_type="Learning", human_authorized_by=AUTH,
        )
        prior = {"entry_id": "01PRIOR00000000000000000AA", "entry_type": "Note", "body": plan.decision_body}
        with pytest.raises(PromotionError, match="already has a promoted"):
            plan_promotion(
                candidate_body=_learning_candidate_body(), candidate_entry_id=CAND,
                candidate_topic="t", target_type="Learning", human_authorized_by=AUTH,
                existing_thread_entries=[prior],
            )


class TestDecisionPathUnaffected:
    def test_decision_body_rejected_under_learning_target(self):
        dec = ("Candidate-Type: Decision\nCandidate-Status: needs_human_confirmation\n\n"
               "## Candidate Decision\nUse X\n\n## Evidence\n> q\n")
        with pytest.raises(PromotionError, match="Candidate learning"):
            plan_promotion(
                candidate_body=dec, candidate_entry_id=CAND, candidate_topic="t",
                target_type="Learning", human_authorized_by=AUTH,
            )

    def test_decision_target_still_writes_a_decision(self):
        dec = ("Candidate-Type: Decision\nCandidate-Status: needs_human_confirmation\n"
               "Confidence: 4/5\n\n## Candidate Decision\nAdopt X\n\n## Evidence\n> q\n")
        plan = plan_promotion(
            candidate_body=dec, candidate_entry_id=CAND, candidate_topic="t",
            target_type="Decision", human_authorized_by=AUTH,
        )
        assert plan.decision_entry_type == "Decision"
        assert "## Decision" in plan.decision_body
