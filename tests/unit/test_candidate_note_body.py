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
