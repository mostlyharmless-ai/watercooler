"""Unit tests for format_candidate_note_body (Phase 1b)."""

from watercooler.decision_extraction import (
    ExtractionResult,
    LLMExtraction,
    format_candidate_note_body,
)


def _make_extraction(
    confidence: int = 3,
    decision_statement: str = "Use FalkorDB for graph storage",
    gate_overrides: dict | None = None,
) -> LLMExtraction:
    gates = {
        "g1_commitment": {"passed": True, "reason": "ok"},
        "g2_not_superseded": {"passed": True, "reason": "ok"},
        "g3_quotable": {"passed": True, "reason": "ok"},
        "g4_rationale": {"passed": True, "reason": "ok"},
        "g5_scope": {"passed": True, "reason": "ok"},
        "g6_temporal": {"passed": True, "reason": "ok"},
        "g7_authority": {"passed": True, "reason": "ok"},
        "g8_self_contained": {"passed": True, "reason": "ok"},
    }
    if gate_overrides:
        gates.update(gate_overrides)
    return LLMExtraction(
        gates=gates,
        confidence=confidence,
        decision_statement=decision_statement,
        rationale="Rationale here",
        scope="watercooler-cloud",
        alternatives_considered=None,
        verbatim_quotes=["we decided to use FalkorDB"],
        warning=None,
    )


def _make_result(
    extraction: LLMExtraction, rejection_reason: str = "soft_gate_failure"
) -> ExtractionResult:
    return ExtractionResult(
        entry_id="01TESTENTRY0000001",
        topic="test-thread",
        passed=False,
        confidence=extraction.confidence,
        gate_results=extraction.gates,
        decision_body=None,
        rejection_reason=rejection_reason,
        extraction=extraction,
    )


def _make_entry(entry_id: str = "01SRCENTRY0000001") -> dict:
    return {
        "entry_id": entry_id,
        "title": "Decide on graph backend",
        "body": "we decided to use FalkorDB for graph storage",
        "agent": "claude-sonnet-4-6",
        "role": "planner",
        "timestamp": "2026-05-19T00:00:00Z",
        "thread_topic": "test-thread",
        "index": 3,
    }


class TestFormatCandidateNoteBody:
    def test_required_metadata_lines_present(self):
        ext = _make_extraction(
            gate_overrides={"g4_rationale": {"passed": False, "reason": "weak"}}
        )
        result = _make_result(ext)
        body = format_candidate_note_body(result, _make_entry())

        assert "Spec: decision-extractor" in body
        assert "Candidate-Type: Decision" in body
        assert "Candidate-Status: needs_human_confirmation" in body
        assert "Surface-Kind: decision" in body
        assert "Promotable: true" in body
        assert "Authority: none" in body
        assert "Confidence: 3/5" in body
        assert "Failed-Gates:" in body

    def test_g8_failure_includes_caveat(self):
        ext = _make_extraction(
            gate_overrides={
                "g8_self_contained": {"passed": False, "reason": "missing context"}
            }
        )
        result = _make_result(ext)
        body = format_candidate_note_body(result, _make_entry())

        assert (
            "Not self-contained. Requires human-supplied context before promotion."
            in body
        )

    def test_g8_not_failed_no_caveat(self):
        ext = _make_extraction(
            gate_overrides={"g4_rationale": {"passed": False, "reason": "weak"}}
        )
        result = _make_result(ext)
        body = format_candidate_note_body(result, _make_entry())

        assert "Not self-contained" not in body

    def test_multiple_failed_gates_in_body(self):
        ext = _make_extraction(
            gate_overrides={
                "g4_rationale": {"passed": False, "reason": "weak"},
                "g6_temporal": {"passed": False, "reason": "unclear"},
            }
        )
        result = _make_result(ext)
        body = format_candidate_note_body(result, _make_entry())

        assert "g4_rationale" in body
        assert "g6_temporal" in body

    def test_source_entry_reference_present(self):
        body = format_candidate_note_body(
            _make_result(
                _make_extraction(
                    gate_overrides={
                        "g8_self_contained": {"passed": False, "reason": "x"}
                    }
                )
            ),
            _make_entry(),
        )
        assert "01SRCENTRY0000001" in body
        assert "test-thread" in body

    def test_verbatim_quote_in_evidence(self):
        body = format_candidate_note_body(
            _make_result(
                _make_extraction(
                    gate_overrides={"g5_scope": {"passed": False, "reason": "vague"}}
                )
            ),
            _make_entry(),
        )
        assert "> we decided to use FalkorDB" in body

    def test_low_confidence_no_failed_gates_explains_reason(self):
        """All gates pass but confidence=3 routes to candidate — body must explain why."""
        ext = _make_extraction(confidence=3, gate_overrides={})  # all gates pass
        result = _make_result(ext, rejection_reason="low_confidence")
        body = format_candidate_note_body(result, _make_entry())

        assert "low_confidence_3" in body
        assert "Failed-Gates: none" in body

    def test_low_confidence_with_warning_includes_warning(self):
        """Extractor warning is surfaced when no gates failed."""
        import dataclasses

        ext = _make_extraction(confidence=3)
        ext = dataclasses.replace(ext, warning="rationale inferred from distant context")
        result = _make_result(ext, rejection_reason="low_confidence")
        body = format_candidate_note_body(result, _make_entry())

        assert "low_confidence_3" in body
        assert "rationale inferred from distant context" in body

    def test_no_extraction_returns_empty(self):
        result = ExtractionResult(
            entry_id="x",
            topic="t",
            passed=False,
            confidence=3,
            gate_results={},
            decision_body=None,
            rejection_reason="soft_gate_failure",
            extraction=None,
        )
        assert format_candidate_note_body(result, {}) == ""

    def test_hallucinated_quote_marks_evidence_unverified(self):
        """conf=5 + hallucinated_quote → body marks quote evidence as unverified
        and adds a Quote-Evidence-Status marker so reviewers see the caveat."""
        ext = _make_extraction(confidence=5)  # all gates pass
        result = _make_result(ext, rejection_reason="hallucinated_quote")
        body = format_candidate_note_body(result, _make_entry())

        assert "Quote-Evidence-Status: weak_unverified" in body
        assert "Evidence (unverified)" in body
        assert "did not validate against the source body" in body
        assert "quote_validation" in body
        # The candidate Decision and source pointer should still be present.
        assert "Use FalkorDB" in body or "FalkorDB" in body
        assert "01SRCENTRY0000001" in body

    def test_summary_only_quote_evidence_marks_evidence_unverified(self):
        """conf=5 + summary_only_quote_evidence → body explains the quote came
        from the summary paraphrase, not the body."""
        ext = _make_extraction(confidence=5)
        result = _make_result(ext, rejection_reason="summary_only_quote_evidence")
        body = format_candidate_note_body(result, _make_entry())

        assert "Quote-Evidence-Status: weak_unverified" in body
        assert "Evidence (unverified)" in body
        assert "matched the entry summary but not the source body" in body

    def test_soft_gate_path_marks_evidence_verified(self):
        """The verified-evidence marker stays on the soft-gate path."""
        ext = _make_extraction(
            confidence=3,
            gate_overrides={"g4_rationale": {"passed": False, "reason": "weak"}},
        )
        result = _make_result(ext, rejection_reason="soft_gate_failure")
        body = format_candidate_note_body(result, _make_entry())

        assert "Quote-Evidence-Status: verified" in body
        # Confirm we didn't pick up the weak-quote section header.
        assert "Evidence (unverified)" not in body

    def test_weak_quote_each_quote_carries_inline_unverified_marker(self):
        """Each unverified quote carries an inline ``[unverified]`` tag so
        the warning survives RAG chunking. Without this, a downstream
        chunk that picks up only blockquote lines surfaces LLM-fabricated
        text as if it were source evidence — the exact failure the
        verbatim-quote gate exists to prevent."""
        import dataclasses

        base = _make_extraction(confidence=5)
        ext = dataclasses.replace(
            base,
            verbatim_quotes=["quote one", "quote two", "quote three"],
        )
        result = _make_result(ext, rejection_reason="hallucinated_quote")
        body = format_candidate_note_body(result, _make_entry())

        # All three quotes carry the inline tag.
        assert body.count("> [unverified]") == 3
        assert "> [unverified] quote one" in body
        assert "> [unverified] quote two" in body
        assert "> [unverified] quote three" in body

    def test_verified_path_does_NOT_add_inline_unverified_marker(self):
        """Regression: the soft-gate (verified) path keeps the plain
        ``> quote`` shape — inline tag is weak-quote only."""
        ext = _make_extraction(
            confidence=3,
            gate_overrides={"g4_rationale": {"passed": False, "reason": "weak"}},
        )
        result = _make_result(ext, rejection_reason="soft_gate_failure")
        body = format_candidate_note_body(result, _make_entry())

        assert "[unverified]" not in body
        assert "> we decided to use FalkorDB" in body


class TestCandidateWarrantReadModel:
    """The candidate body surfaces the tether/warrant read model (#881)."""

    def test_warrant_markers_and_section_present(self):
        ext = _make_extraction(confidence=3)
        result = _make_result(ext, rejection_reason="soft_gate_failure")
        body = format_candidate_note_body(result, _make_entry())

        assert "Tether-Support-Counts:" in body
        assert "Dominant-Tether:" in body
        assert "Thin-Support:" in body
        assert "## Support tethers" in body

    def test_verified_source_quote_is_not_thin(self):
        # Soft-gate path keeps quotes verified → source-supported, not thin.
        ext = _make_extraction(
            confidence=3,
            gate_overrides={"g4_rationale": {"passed": False, "reason": "weak"}},
        )
        result = _make_result(ext, rejection_reason="soft_gate_failure")
        body = format_candidate_note_body(result, _make_entry())

        assert "Thin-Support: false" in body
        assert "source=" in body

    def test_weak_quote_candidate_is_thin(self):
        # Unverified quotes are interpretive, not source → thin support.
        ext = _make_extraction(confidence=5)
        result = _make_result(ext, rejection_reason="hallucinated_quote")
        body = format_candidate_note_body(result, _make_entry())

        assert "Thin-Support: true" in body
        assert "Thin-Support-Reason:" in body

    def test_record_state_source_type_carried_forward_to_promotion(self):
        # A candidate extracted from a record-state entry (Decision/Closure/
        # Supersession) earns record_state support; that must survive promotion
        # via the Source-Entry-Type marker (regression for the carry-forward gap).
        from watercooler.promotion import (
            parse_candidate_body,
            format_promotion_decision_body,
        )

        ext = _make_extraction(confidence=3)
        result = _make_result(ext, rejection_reason="soft_gate_failure")
        entry = _make_entry()
        entry["entry_type"] = "Decision"
        body = format_candidate_note_body(result, entry)

        assert "Source-Entry-Type: Decision" in body
        assert "record_state=" in body  # candidate earned record_state support

        meta = parse_candidate_body(body, "01CANDIDATE000000001", "test-thread")
        assert meta.source_entry_type == "Decision"
        # #887: record_state at promotion is re-derived from the LIVE source —
        # quotes re-validated (quote_verified=True) AND the live source type
        # (source_entry_type) being a record-state type. The candidate's
        # self-asserted Source-Entry-Type marker no longer grants record_state.
        promoted = format_promotion_decision_body(
            meta,
            human_authorized_by="github:caleb",
            quote_verified=True,
            source_entry_type="Decision",
        )
        assert "record_state:" in promoted
        # Without live re-derivation (pure defaults), record_state is withheld.
        promoted_unverified = format_promotion_decision_body(
            meta, human_authorized_by="github:caleb"
        )
        assert "record_state:" not in promoted_unverified

    def test_weak_quote_from_decision_source_stays_thin(self):
        # Codex P2 regression at the body surface: a hallucinated-quote candidate
        # extracted from a Decision source must not render as record-backed.
        ext = _make_extraction(confidence=5)
        result = _make_result(ext, rejection_reason="hallucinated_quote")
        entry = _make_entry()
        entry["entry_type"] = "Decision"
        body = format_candidate_note_body(result, entry)

        assert "Thin-Support: true" in body
        assert "record_state=" not in body
        assert "record_state:" not in body

    def test_note_source_type_does_not_emit_marker(self):
        # The common Note case carries no Source-Entry-Type marker.
        ext = _make_extraction(confidence=3)
        result = _make_result(ext, rejection_reason="soft_gate_failure")
        entry = _make_entry()
        entry["entry_type"] = "Note"
        body = format_candidate_note_body(result, entry)
        assert "Source-Entry-Type:" not in body

    def test_warrant_section_does_not_pollute_evidence_parsing(self):
        # The promotion parser reads `## Evidence` blockquotes; the warrant
        # section (which follows) must not leak into parsed evidence quotes.
        from watercooler.promotion import parse_candidate_body

        ext = _make_extraction(
            confidence=3,
            gate_overrides={"g4_rationale": {"passed": False, "reason": "weak"}},
        )
        result = _make_result(ext, rejection_reason="soft_gate_failure")
        body = format_candidate_note_body(result, _make_entry())

        meta = parse_candidate_body(body, "01CANDIDATE000000001", "test-thread")
        assert meta.evidence_quotes == ["we decided to use FalkorDB"]
