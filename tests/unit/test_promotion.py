"""Unit tests for watercooler.promotion (Phase 1b promotion helper)."""

from __future__ import annotations

import pytest

from watercooler.promotion import (
    CandidateMetadata,
    PromotionError,
    PromotionPlan,
    build_promotion_authority_fields,
    format_candidate_disposition_body,
    format_promotion_decision_body,
    parse_candidate_body,
    plan_promotion,
    validate_candidate_for_promotion,
)


# Canonical candidate body emitted by format_candidate_note_body for a
# soft-gate failure (g6_temporal) at confidence 4.
_SOFT_GATE_CANDIDATE_BODY = """\
Spec: decision-extractor
[automated: decision_extractor]
Candidate-Type: Decision
Candidate-Status: needs_human_confirmation
Surface-Kind: decision
Promotable: true
Authority: none
Confidence: 4/5
Failed-Gates: g6_temporal
Quote-Evidence-Status: verified
Source-Entry: 01SRCENTRY00000000000000001

## Candidate Decision
We will adopt PostgreSQL for session storage.

## Why this is a candidate, not a Decision
g6_temporal: unclear timing — entry does not state when this takes effect.

## Evidence
> We decided to use PostgreSQL for session storage.

## Source
Source entry: #3 `01SRCENTRY00000000000000001` — "Storage decision" (thread: feature-storage)
Agent: claude-sonnet-4-6 | Role: planner | 2026-05-19T00:00:00Z
"""


# Weak-quote candidate body (the new emission path from PR #858).
_WEAK_QUOTE_CANDIDATE_BODY = """\
Spec: decision-extractor
[automated: decision_extractor]
Candidate-Type: Decision
Candidate-Status: needs_human_confirmation
Surface-Kind: decision
Promotable: true
Authority: none
Confidence: 5/5
Failed-Gates: none
Quote-Evidence-Status: weak_unverified
Source-Entry: 01SRCENTRY00000000000000002

## Candidate Decision
Adopt FalkorDB for graph storage.

## Why this is a candidate, not a Decision
quote_validation: LLM-supplied quotes did not match source body verbatim.

## Evidence (unverified)
_The following quotes were produced by the LLM but did not validate against the source body. Treat as suggestions for where to look, not as direct evidence._
> we adopted FalkorDB for graph
"""


class TestParseCandidateBody:
    def test_parses_canonical_markers(self):
        meta = parse_candidate_body(
            _SOFT_GATE_CANDIDATE_BODY, "01CAND0001", "feature-storage"
        )
        assert meta.candidate_type == "Decision"
        assert meta.candidate_status == "needs_human_confirmation"
        assert meta.surface_kind == "decision"
        assert meta.confidence == 4
        assert meta.failed_gates == ["g6_temporal"]
        assert meta.quote_evidence_status == "verified"
        assert meta.source_entry_id == "01SRCENTRY00000000000000001"

    def test_extracts_decision_statement(self):
        meta = parse_candidate_body(
            _SOFT_GATE_CANDIDATE_BODY, "01CAND0001", "feature-storage"
        )
        assert meta.decision_statement == "We will adopt PostgreSQL for session storage."

    def test_extracts_why_section(self):
        meta = parse_candidate_body(
            _SOFT_GATE_CANDIDATE_BODY, "01CAND0001", "feature-storage"
        )
        assert meta.why_section is not None
        assert "g6_temporal: unclear timing" in meta.why_section

    def test_extracts_evidence_quotes(self):
        meta = parse_candidate_body(
            _SOFT_GATE_CANDIDATE_BODY, "01CAND0001", "feature-storage"
        )
        assert meta.evidence_quotes == [
            "We decided to use PostgreSQL for session storage."
        ]

    def test_weak_quote_candidate_marks_evidence_unverified(self):
        meta = parse_candidate_body(
            _WEAK_QUOTE_CANDIDATE_BODY, "01CAND0002", "feature-graph"
        )
        assert meta.quote_evidence_status == "weak_unverified"
        assert meta.failed_gates == []
        assert meta.evidence_quotes == ["we adopted FalkorDB for graph"]

    def test_failed_gates_none_string_yields_empty_list(self):
        meta = parse_candidate_body(
            "Failed-Gates: none\n\n## Candidate Decision\nX\n",
            "01CAND0003",
            "topic",
        )
        assert meta.failed_gates == []

    def test_multiple_failed_gates_parsed(self):
        body = (
            "Candidate-Status: needs_human_confirmation\n"
            "Failed-Gates: g4_rationale, g6_temporal, g8_self_contained\n\n"
            "## Candidate Decision\nDecide stuff\n"
        )
        meta = parse_candidate_body(body, "01C", "t")
        assert meta.failed_gates == ["g4_rationale", "g6_temporal", "g8_self_contained"]

    def test_missing_markers_tolerated(self):
        meta = parse_candidate_body(
            "## Candidate Decision\nThe thing\n", "01CAND", "topic"
        )
        assert meta.candidate_type is None
        assert meta.candidate_status is None
        assert meta.confidence is None
        assert meta.decision_statement == "The thing"


class TestValidateCandidateForPromotion:
    def _meta(self, **overrides) -> CandidateMetadata:
        base = CandidateMetadata(
            candidate_entry_id="01CAND",
            candidate_topic="t",
            candidate_status="needs_human_confirmation",
            decision_statement="Do the thing",
        )
        for k, v in overrides.items():
            setattr(base, k, v)
        return base

    def test_decision_target_with_authorizer_passes(self):
        validate_candidate_for_promotion(self._meta(), "Decision", "caleb")

    def test_unsupported_target_type_rejected(self):
        with pytest.raises(PromotionError, match="not supported"):
            validate_candidate_for_promotion(self._meta(), "Closure", "caleb")

    def test_empty_authorizer_rejected(self):
        with pytest.raises(PromotionError, match="human_authorized_by is required"):
            validate_candidate_for_promotion(self._meta(), "Decision", "")

    def test_whitespace_authorizer_rejected(self):
        with pytest.raises(PromotionError, match="human_authorized_by is required"):
            validate_candidate_for_promotion(self._meta(), "Decision", "   ")

    def test_markup_only_authorizer_rejected(self):
        # A raw-non-empty string that scrubs to "" (angle brackets / zero-width)
        # must be rejected — otherwise ownership would be recorded as empty.
        for raw in ("<>", "<<>>", "​​", "‮‍"):
            with pytest.raises(
                PromotionError, match="human_authorized_by is required"
            ):
                validate_candidate_for_promotion(self._meta(), "Decision", raw)

    def test_promoted_candidate_cannot_be_re_promoted(self):
        with pytest.raises(PromotionError, match="not 'needs_human_confirmation'"):
            validate_candidate_for_promotion(
                self._meta(candidate_status="promoted"), "Decision", "caleb"
            )

    def test_missing_decision_statement_rejected(self):
        with pytest.raises(PromotionError, match="no '## Candidate Decision' section"):
            validate_candidate_for_promotion(
                self._meta(decision_statement=None), "Decision", "caleb"
            )

    def test_candidate_status_with_spaces_accepted(self):
        validate_candidate_for_promotion(
            self._meta(candidate_status="needs human confirmation"),
            "Decision",
            "caleb",
        )


class TestFormatPromotionDecisionBody:
    def _meta(self) -> CandidateMetadata:
        return parse_candidate_body(
            _SOFT_GATE_CANDIDATE_BODY, "01CAND0001", "feature-storage"
        )

    def test_scrub_to_empty_authorizer_raises(self):
        # Defense in depth: a direct call (bypassing validate_candidate_for_
        # promotion) with a control/markup-only authorizer must not render
        # "Ownership is satisfied: ``" — it must raise.
        with pytest.raises(PromotionError, match="scrubbed to empty"):
            format_promotion_decision_body(self._meta(), human_authorized_by="<>")

    def test_required_provenance_markers_present(self):
        body = format_promotion_decision_body(
            self._meta(), human_authorized_by="caleb"
        )
        assert "Spec: decision-extractor-promoted" in body
        assert "Promoted-From: 01CAND0001" in body
        assert "Source-Entry: 01SRCENTRY00000000000000001" in body
        assert "Authority-Source: human" in body
        assert "Authority-Basis: human_promoted" in body
        assert "Human-Authorized-By: caleb" in body
        assert "Confidence: 4/5 (from candidate)" in body

    def test_failed_gates_carried_forward(self):
        body = format_promotion_decision_body(
            self._meta(), human_authorized_by="caleb"
        )
        assert "Failed-Gates-At-Extraction: g6_temporal" in body

    def test_evidence_quotes_carried_forward(self):
        body = format_promotion_decision_body(
            self._meta(), human_authorized_by="caleb"
        )
        assert "> We decided to use PostgreSQL for session storage." in body
        assert "## Evidence (carried forward)" in body

    def test_weak_quote_evidence_labeled_in_promoted_body(self):
        meta = parse_candidate_body(
            _WEAK_QUOTE_CANDIDATE_BODY, "01CAND0002", "feature-graph"
        )
        body = format_promotion_decision_body(meta, human_authorized_by="caleb")
        assert "Quote-Evidence-Status-At-Extraction: weak_unverified" in body
        assert "unverified at extraction time" in body
        # The promotion records that a human reviewed the unverified quotes.
        assert "promoting human has reviewed them" in body

    def test_edits_override_decision_statement(self):
        body = format_promotion_decision_body(
            self._meta(),
            human_authorized_by="caleb",
            edits={
                "decision_statement": "Use PostgreSQL 16 for session storage",
                "rationale": "vector ops + JSON support",
                "scope": "watercooler-site/api",
            },
        )
        assert "Use PostgreSQL 16 for session storage" in body
        assert "## Rationale" in body
        assert "vector ops + JSON support" in body
        assert "## Scope" in body
        assert "watercooler-site/api" in body

    def test_warrant_section_shows_user_support_after_promotion(self):
        # The promoted Decision re-derives the read model with the human
        # authorizer present, so it carries user support and is not thin (#881).
        body = format_promotion_decision_body(
            self._meta(), human_authorized_by="github:caleb"
        )
        assert "## Support tethers" in body
        assert "user:" in body
        assert "Thin support: no" in body

    def test_warrant_section_renders_for_weak_quote_promotion(self):
        # Even when the candidate's quotes were unverified, human ownership keeps
        # the promoted Decision from being thin.
        meta = parse_candidate_body(
            _WEAK_QUOTE_CANDIDATE_BODY, "01CAND0002", "feature-graph"
        )
        body = format_promotion_decision_body(meta, human_authorized_by="github:caleb")
        assert "## Support tethers" in body
        assert "Thin support: no" in body  # user support present

    # ---- #887: source/record_state granted only on live re-validation ----

    def test_quote_verified_true_grants_source_support(self):
        # The candidate cites a source and carries a quote; when the composition
        # layer re-validated that quote against the live source (quote_verified=
        # True), the promoted Decision shows source support and says so.
        body = format_promotion_decision_body(
            self._meta(), human_authorized_by="github:caleb", quote_verified=True
        )
        assert "- source:" in body
        assert "Quote-Reverified-At-Promotion: reverified" in body
        assert "re-validated against the live source entry at promotion and matched" in body

    def test_quote_verified_false_withholds_source_support(self):
        # Re-validation ran and the quotes did NOT confirm (or the source was
        # unreadable): no source/record_state, but the human authorizer keeps the
        # Decision non-thin.
        body = format_promotion_decision_body(
            self._meta(), human_authorized_by="github:caleb", quote_verified=False
        )
        assert "- source:" not in body
        assert "- record_state:" not in body
        assert "Quote-Reverified-At-Promotion: not_reverified" in body
        assert "did NOT confirm against the live source" in body
        assert "Thin support: no" in body  # user tether present

    def test_short_matching_quote_note_does_not_claim_unmatched_source(self):
        body = format_promotion_decision_body(
            self._meta(),
            human_authorized_by="github:caleb",
            quote_verified=False,
            quote_reverification_reason="quote_below_minimum_length",
        )
        assert "- source:" not in body
        assert "Quote-Reverified-At-Promotion: not_reverified" in body
        assert "Quote-Reverification-Reason: quote_below_minimum_length" in body
        assert "matched the live source entry at promotion" in body
        assert "too short to count as durable source support" in body
        assert "did NOT confirm against the live source" not in body

    def test_quote_verified_none_withholds_source_support(self):
        # The pure default (no re-validation performed) must NOT grant source
        # support from the candidate's self-asserted Quote-Evidence-Status —
        # that is the laundering path #887 closes.
        body = format_promotion_decision_body(
            self._meta(), human_authorized_by="github:caleb"
        )
        assert "- source:" not in body
        assert "Quote-Reverified-At-Promotion: not_checked" in body
        assert "were NOT re-validated against the live source" in body
        assert "Thin support: no" in body  # user tether keeps it non-thin

    def test_record_state_follows_live_source_type_not_self_assertion(self):
        # #887: record_state is granted from the LIVE source entry type supplied
        # by the composition, not the candidate's self-asserted Source-Entry-Type.
        # A record-state live type + re-validated quotes grants it...
        decision_src = format_promotion_decision_body(
            self._meta(),
            human_authorized_by="github:caleb",
            quote_verified=True,
            source_entry_type="Decision",
        )
        assert "- record_state:" in decision_src
        # ...but when the live source is a plain Note, record_state is withheld
        # even though quotes re-validated (source support still stands).
        note_src = format_promotion_decision_body(
            self._meta(),
            human_authorized_by="github:caleb",
            quote_verified=True,
            source_entry_type="Note",
        )
        assert "- record_state:" not in note_src
        assert "- source:" in note_src


class TestFormatCandidateDispositionBody:
    def test_contains_required_markers(self):
        meta = parse_candidate_body(
            _SOFT_GATE_CANDIDATE_BODY, "01CAND0001", "feature-storage"
        )
        body = format_candidate_disposition_body(
            meta, promoted_entry_id="01PROMOTED0001", human_authorized_by="caleb"
        )
        assert "Spec: candidate-disposition" in body
        assert "CandidateDisposition: promoted" in body
        assert "Disposition-Target: 01CAND0001" in body
        assert "Promoted-To: 01PROMOTED0001" in body
        assert "Disposition-Authorized-By: caleb" in body

    def test_explains_append_only_discipline(self):
        meta = parse_candidate_body(
            _SOFT_GATE_CANDIDATE_BODY, "01CAND0001", "feature-storage"
        )
        body = format_candidate_disposition_body(
            meta, promoted_entry_id="01P", human_authorized_by="caleb"
        )
        assert "append-only" in body
        # Queries that need the candidate's current disposition follow this.
        assert "latest `CandidateDisposition` Note" in body


class TestPlanPromotion:
    def test_returns_plan_with_both_entries(self):
        plan = plan_promotion(
            candidate_body=_SOFT_GATE_CANDIDATE_BODY,
            candidate_entry_id="01CAND0001",
            candidate_topic="feature-storage",
            target_type="Decision",
            human_authorized_by="caleb",
        )
        assert isinstance(plan, PromotionPlan)
        assert plan.decision_entry_type == "Decision"
        assert plan.disposition_entry_type == "Note"
        assert plan.topic == "feature-storage"
        assert plan.candidate_entry_id == "01CAND0001"
        assert "Promoted-From: 01CAND0001" in plan.decision_body
        assert "Spec: candidate-disposition" in plan.disposition_body

    def test_unsupported_target_type_raises(self):
        with pytest.raises(PromotionError, match="not supported"):
            plan_promotion(
                candidate_body=_SOFT_GATE_CANDIDATE_BODY,
                candidate_entry_id="01CAND0001",
                candidate_topic="feature-storage",
                target_type="Closure",
                human_authorized_by="caleb",
            )

    def test_already_promoted_candidate_raises(self):
        promoted_body = _SOFT_GATE_CANDIDATE_BODY.replace(
            "Candidate-Status: needs_human_confirmation",
            "Candidate-Status: promoted",
        )
        with pytest.raises(PromotionError, match="not 'needs_human_confirmation'"):
            plan_promotion(
                candidate_body=promoted_body,
                candidate_entry_id="01CAND0001",
                candidate_topic="feature-storage",
                target_type="Decision",
                human_authorized_by="caleb",
            )

    def test_decision_title_derived_from_statement(self):
        plan = plan_promotion(
            candidate_body=_SOFT_GATE_CANDIDATE_BODY,
            candidate_entry_id="01CAND0001",
            candidate_topic="feature-storage",
            target_type="Decision",
            human_authorized_by="caleb",
        )
        assert plan.decision_title.startswith("We will adopt PostgreSQL")

    def test_long_statement_title_truncated(self):
        body = _SOFT_GATE_CANDIDATE_BODY.replace(
            "We will adopt PostgreSQL for session storage.",
            "We will adopt PostgreSQL for session storage and also for "
            "analytics workloads and reporting and audit logs.",
        )
        plan = plan_promotion(
            candidate_body=body,
            candidate_entry_id="01CAND0001",
            candidate_topic="feature-storage",
            target_type="Decision",
            human_authorized_by="caleb",
        )
        assert len(plan.decision_title) <= 81  # 80 + ellipsis

    def test_weak_quote_candidate_round_trip(self):
        plan = plan_promotion(
            candidate_body=_WEAK_QUOTE_CANDIDATE_BODY,
            candidate_entry_id="01CAND0002",
            candidate_topic="feature-graph",
            target_type="Decision",
            human_authorized_by="caleb",
        )
        assert "Quote-Evidence-Status-At-Extraction: weak_unverified" in plan.decision_body
        assert "Adopt FalkorDB for graph storage" in plan.decision_body

    def test_existing_disposition_blocks_re_promotion(self):
        """An existing CandidateDisposition Note for this candidate blocks
        re-promotion (the spec-compliant double-promotion guard — the
        candidate body's Candidate-Status never transitions because
        promotion is append-only)."""
        # Use a real 26-char Crockford ULID for Disposition-Target so the
        # marker regex picks it up.
        candidate_id = "01HZAAT0BC3D4E5F6G7H8J9K0M"
        prior_disposition = {
            "entry_id": "01HZADJ5P0SJT00N00000000KM",
            "entry_type": "Note",
            "body": (
                "Spec: candidate-disposition\n"
                "CandidateDisposition: promoted\n"
                f"Disposition-Target: {candidate_id}\n"
                "Promoted-To: 01HZAEJ5P0SJT00N00000000KM\n"
            ),
        }
        with pytest.raises(PromotionError, match="already has a CandidateDisposition"):
            plan_promotion(
                candidate_body=_SOFT_GATE_CANDIDATE_BODY,
                candidate_entry_id=candidate_id,
                candidate_topic="feature-storage",
                target_type="Decision",
                human_authorized_by="caleb",
                existing_thread_entries=[prior_disposition],
            )

    def test_disposition_for_different_candidate_does_not_block(self):
        """A CandidateDisposition Note referencing a different candidate
        must not block promotion of this one."""
        # Different real ULID — does not match this candidate.
        other_candidate_id = "01HZAFT0BC3D4E5F6G7H8J9K0M"
        unrelated_disposition = {
            "entry_id": "01HZAEJ5P0SJT00N00000000KM",
            "entry_type": "Note",
            "body": (
                "Spec: candidate-disposition\n"
                "CandidateDisposition: promoted\n"
                f"Disposition-Target: {other_candidate_id}\n"
                "Promoted-To: 01HZAGT0BC3D4E5F6G7H8J9K0M\n"
            ),
        }
        plan = plan_promotion(
            candidate_body=_SOFT_GATE_CANDIDATE_BODY,
            candidate_entry_id="01CAND0001",
            candidate_topic="feature-storage",
            target_type="Decision",
            human_authorized_by="caleb",
            existing_thread_entries=[unrelated_disposition],
        )
        assert plan.decision_entry_type == "Decision"

    def test_existing_promoted_entry_blocks_re_promotion(self):
        """#886: a promoted entry carrying ``Promoted-From: <candidate>``
        blocks re-promotion even when NO matching CandidateDisposition Note
        exists — the disposition write failed after the promoted entry committed,
        and a re-run must not append a duplicate promoted entry."""
        candidate_id = "01HZAAT0BC3D4E5F6G7H8J9K0M"
        prior_decision = {
            "entry_id": "01HZADJ5P0SJT00N00000000KM",
            "entry_type": "Decision",
            "body": (
                "Spec: decision-extractor-promoted\n"
                f"Promoted-From: {candidate_id}\n"
                "Authority-Source: human\n"
                "Authority-Basis: human_promoted\n"
                "## Decision\nAdopt FalkorDB for graph storage.\n"
            ),
        }
        with pytest.raises(
            PromotionError, match="already has a promoted entry"
        ):
            plan_promotion(
                candidate_body=_SOFT_GATE_CANDIDATE_BODY,
                candidate_entry_id=candidate_id,
                candidate_topic="feature-storage",
                target_type="Decision",
                human_authorized_by="caleb",
                existing_thread_entries=[prior_decision],
            )

    def test_promoted_from_without_authority_basis_does_not_block(self):
        """A Decision carrying a lone ``Promoted-From`` line but NOT the
        ``Authority-Basis: human_promoted`` marker that every genuine promotion
        emits must not block — this is the griefing-mitigation: a writer cannot
        permanently freeze a candidate's promotion by planting one marker line."""
        candidate_id = "01HZAAT0BC3D4E5F6G7H8J9K0M"
        planted = {
            "entry_id": "01HZADJ5P0SJT00N00000000KM",
            "entry_type": "Decision",
            "body": (
                "Spec: general\n"
                "A hand-written Decision that mentions a candidate.\n"
                f"Promoted-From: {candidate_id}\n"
            ),
        }
        plan = plan_promotion(
            candidate_body=_SOFT_GATE_CANDIDATE_BODY,
            candidate_entry_id=candidate_id,
            candidate_topic="feature-storage",
            target_type="Decision",
            human_authorized_by="caleb",
            existing_thread_entries=[planted],
        )
        assert plan.decision_entry_type == "Decision"

    def test_promoted_from_marker_on_non_decision_does_not_block(self):
        """The Promoted-From idempotency gate is Decision-scoped: a Note that
        merely quotes ``Promoted-From:`` in prose must not block a legitimate
        promotion (false-positive avoidance)."""
        candidate_id = "01HZAAT0BC3D4E5F6G7H8J9K0M"
        quoting_note = {
            "entry_id": "01HZAEJ5P0SJT00N00000000KM",
            "entry_type": "Note",
            "body": (
                "Spec: general\n"
                "Discussion of the promotion mechanism.\n"
                f"Promoted-From: {candidate_id}\n"
                "Authority-Basis: human_promoted\n"
            ),
        }
        plan = plan_promotion(
            candidate_body=_SOFT_GATE_CANDIDATE_BODY,
            candidate_entry_id=candidate_id,
            candidate_topic="feature-storage",
            target_type="Decision",
            human_authorized_by="caleb",
            existing_thread_entries=[quoting_note],
        )
        assert plan.decision_entry_type == "Decision"

    def test_promoted_decision_for_different_candidate_does_not_block(self):
        """A promoted Decision for a *different* candidate must not block
        promotion of this one."""
        other_candidate_id = "01HZAFT0BC3D4E5F6G7H8J9K0M"
        prior_decision = {
            "entry_id": "01HZAGT0BC3D4E5F6G7H8J9K0M",
            "entry_type": "Decision",
            "body": (
                "Spec: decision-extractor-promoted\n"
                f"Promoted-From: {other_candidate_id}\n"
                "Authority-Basis: human_promoted\n"
                "## Decision\nSomething unrelated.\n"
            ),
        }
        plan = plan_promotion(
            candidate_body=_SOFT_GATE_CANDIDATE_BODY,
            candidate_entry_id="01CAND0001",
            candidate_topic="feature-storage",
            target_type="Decision",
            human_authorized_by="caleb",
            existing_thread_entries=[prior_decision],
        )
        assert plan.decision_entry_type == "Decision"

    def test_promoted_entry_with_missing_entry_type_still_blocks(self):
        """When an entry omits ``entry_type``, the Promoted-From marker is
        trusted on its own — blocking is the fail-safe direction for a
        double-write guard."""
        candidate_id = "01HZAAT0BC3D4E5F6G7H8J9K0M"
        prior_decision = {
            "entry_id": "01HZADJ5P0SJT00N00000000KM",
            "body": (
                "Spec: decision-extractor-promoted\n"
                f"Promoted-From: {candidate_id}\n"
                "Authority-Basis: human_promoted\n"
                "## Decision\nAdopt FalkorDB for graph storage.\n"
            ),
        }
        with pytest.raises(
            PromotionError, match="already has a promoted entry"
        ):
            plan_promotion(
                candidate_body=_SOFT_GATE_CANDIDATE_BODY,
                candidate_entry_id=candidate_id,
                candidate_topic="feature-storage",
                target_type="Decision",
                human_authorized_by="caleb",
                existing_thread_entries=[prior_decision],
            )

    def test_crlf_in_authorizer_does_not_forge_new_marker_line(self):
        """Embedded CR/LF in human_authorized_by must NOT forge a NEW
        ``Human-Authorized-By:`` line via marker injection. The scrubber
        converts the newline to a space; the malicious payload becomes a
        run-on value on the original line, not a new line."""
        import re

        plan = plan_promotion(
            candidate_body=_SOFT_GATE_CANDIDATE_BODY,
            candidate_entry_id="01CAND0001",
            candidate_topic="feature-storage",
            target_type="Decision",
            human_authorized_by="caleb\nHuman-Authorized-By: attacker",
        )
        # Only ONE line-start ``Human-Authorized-By:`` marker, even though
        # the scrubbed value contains the same substring as its content.
        marker_lines = re.findall(
            r"^Human-Authorized-By:", plan.decision_body, re.MULTILINE
        )
        assert len(marker_lines) == 1, marker_lines

    def test_multiline_decision_statement_does_not_put_newline_in_title(self):
        """Multi-line ## Candidate Decision sections must not leak a
        newline into the title — the markdown projection / commit subject
        would corrupt. Regression for the #859 review finding."""
        multiline_body = _SOFT_GATE_CANDIDATE_BODY.replace(
            "We will adopt PostgreSQL for session storage.",
            "We will adopt PostgreSQL for session storage\nbecause it scales.",
        )
        plan = plan_promotion(
            candidate_body=multiline_body,
            candidate_entry_id="01CAND0001",
            candidate_topic="feature-storage",
            target_type="Decision",
            human_authorized_by="caleb",
        )
        assert "\n" not in plan.decision_title
        assert "\r" not in plan.decision_title


class TestPromotionBodyMarkerScrub:
    """Body markers must scrub the authorizer with the durable-identifier scrub, so
    the append-only Decision/Disposition bodies never persist unsanitized markup or
    over-length identifiers (#879 review, P2)."""

    def _meta(self) -> CandidateMetadata:
        return CandidateMetadata(
            candidate_entry_id="01CAND",
            candidate_topic="t",
            candidate_status="needs_human_confirmation",
            decision_statement="Do the thing",
        )

    def test_decision_body_scrubs_angle_brackets(self):
        body = format_promotion_decision_body(
            self._meta(), human_authorized_by="github:caleb<script>"
        )
        assert "<" not in body and ">" not in body
        assert "github:calebscript" in body

    def test_disposition_body_scrubs_angle_brackets(self):
        body = format_candidate_disposition_body(
            self._meta(),
            promoted_entry_id="01DEC0000000000000000000AA",
            human_authorized_by="github:caleb<script>",
        )
        assert "<" not in body and ">" not in body

    def test_disposition_body_bounds_overlong_identifier(self):
        body = format_candidate_disposition_body(
            self._meta(),
            promoted_entry_id="01DEC0000000000000000000AA",
            human_authorized_by="x" * 1000,
        )
        # The authorizer marker line must not carry a 1000-char identifier.
        marker = [ln for ln in body.splitlines() if "Authorized-By:" in ln][0]
        assert len(marker) < 320  # 256 cap + short label


class TestBuildPromotionAuthorityFields:
    """Shared authority-fields builder used by the MCP and CLI promote paths (#879)."""

    def test_core_fields_always_present(self):
        from watercooler.promotion import build_promotion_authority_fields

        f = build_promotion_authority_fields(human_authorized_by="github:caleb")
        assert f["decision_origin"] == "human_promoted"
        assert f["authority_basis"] == "human_promoted"
        assert f["human_authorized_by"] == "github:caleb"

    def test_actor_class_optional(self):
        from watercooler.promotion import build_promotion_authority_fields

        assert "actor_class" not in build_promotion_authority_fields(
            human_authorized_by="x"
        )
        assert build_promotion_authority_fields(
            human_authorized_by="x", actor_class="agent"
        )["actor_class"] == "agent"

    def test_source_entry_id_only_when_ulid(self):
        from watercooler.promotion import build_promotion_authority_fields

        ulid = "01CAND0000000000000000000A"
        assert build_promotion_authority_fields(
            human_authorized_by="x", source_entry_id=ulid
        )["source_entry_id"] == ulid
        # Non-ULID source is dropped (the schema field is ULID-typed).
        assert "source_entry_id" not in build_promotion_authority_fields(
            human_authorized_by="x", source_entry_id="not-a-ulid"
        )

    def test_authorizer_is_scrubbed(self):
        from watercooler.promotion import build_promotion_authority_fields

        f = build_promotion_authority_fields(human_authorized_by="github:caleb<x>")
        assert "<" not in f["human_authorized_by"]
        # Empty after scrub -> field omitted entirely.
        assert "human_authorized_by" not in build_promotion_authority_fields(
            human_authorized_by="   "
        )


_MORAL_CANDIDATE_BODY = """\
Spec: decision-extractor
[automated: decision_extractor]
Candidate-Type: Decision
Candidate-Status: needs_human_confirmation
Surface-Kind: decision
Promotable: true
Authority: none
Confidence: 4/5
Failed-Gates: none
Quote-Evidence-Status: verified
Source-Entry: 01SRCENTRY00000000000000001
Moral-Delegation-Warning: true
Human-Moral-Ownership-Required: true
Moral-Delegation-Reason: Decision statement uses ethical language; a human must own it.

## Candidate Decision
We should preserve user consent before logging.

## Why this is a candidate, not a Decision
moral_delegation: the decision statement makes a value/ethical judgment.

## Moral-delegation warning
Decision statement makes a value judgment affecting people.

## Evidence
> we should preserve user consent before logging
"""


class TestMoralDelegationPromotion:
    """Unit 3 (#880) — warned candidates carry forward and require ownership."""

    def test_parse_moral_delegation_markers(self):
        meta = parse_candidate_body(_MORAL_CANDIDATE_BODY, "01CAND9", "topic")
        assert meta.moral_delegation_warning is True
        assert meta.moral_delegation_reason
        assert "ethical language" in meta.moral_delegation_reason

    def test_non_moral_candidate_defaults_warning_false(self):
        meta = parse_candidate_body(
            "## Candidate Decision\nUse FalkorDB\n", "01C", "t"
        )
        assert meta.moral_delegation_warning is False
        assert meta.moral_delegation_reason is None

    def test_warned_candidate_without_authorizer_fails(self):
        # Promotion of a warned candidate without human_authorized_by must fail.
        with pytest.raises(PromotionError, match="human_authorized_by is required"):
            plan_promotion(
                candidate_body=_MORAL_CANDIDATE_BODY,
                candidate_entry_id="01CAND9",
                candidate_topic="topic",
                target_type="Decision",
                human_authorized_by="",
            )

    def test_warned_candidate_with_scrub_to_empty_authorizer_fails(self):
        # A warned candidate must not be promotable with an authorizer that
        # scrubs to empty — otherwise the "Ownership is satisfied" carry-forward
        # would record an empty authorizer, bypassing the ownership requirement.
        with pytest.raises(PromotionError, match="human_authorized_by is required"):
            plan_promotion(
                candidate_body=_MORAL_CANDIDATE_BODY,
                candidate_entry_id="01CAND9",
                candidate_topic="topic",
                target_type="Decision",
                human_authorized_by="<>",
            )

    def test_warned_candidate_with_authorizer_succeeds_and_carries_ownership(self):
        plan = plan_promotion(
            candidate_body=_MORAL_CANDIDATE_BODY,
            candidate_entry_id="01CAND9",
            candidate_topic="topic",
            target_type="Decision",
            human_authorized_by="github:octocat",
        )
        body = plan.decision_body
        assert "## Moral ownership" in body
        assert "Ownership is satisfied" in body
        assert "github:octocat" in body
        # Procedural framing — not a moral-correctness adjudication.
        assert "not a determination that the" in body

    def test_promotion_authority_fields_carry_human_authorized_by(self):
        fields = build_promotion_authority_fields(
            human_authorized_by="github:octocat",
            source_entry_id="01SRCENTRY00000000000000001",
            actor_class="agent",
        )
        assert fields["human_authorized_by"] == "github:octocat"
        assert fields["decision_origin"] == "human_promoted"
        assert fields["authority_basis"] == "human_promoted"
