"""Unit tests for gate classification helper (Phase 1b)."""

import pytest

from watercooler.decision_extraction import (
    CANDIDATE_FALLBACK_GATES,
    HARD_FAIL_GATES,
    classify_gate_outcome,
)


def _gate(passed: bool) -> dict:
    return {"passed": passed, "reason": "test"}


def _all_pass() -> dict:
    return {g: _gate(True) for g in HARD_FAIL_GATES | CANDIDATE_FALLBACK_GATES}


class TestClassifyGateOutcome:
    def test_all_pass(self):
        assert classify_gate_outcome(_all_pass()) == "pass"

    def test_empty_gate_results(self):
        assert classify_gate_outcome({}) == "pass"

    def test_g1_failure_is_hard_fail(self):
        gates = _all_pass()
        gates["g1_commitment"] = _gate(False)
        assert classify_gate_outcome(gates) == "hard_fail"

    def test_g2_failure_is_hard_fail(self):
        gates = _all_pass()
        gates["g2_not_superseded"] = _gate(False)
        assert classify_gate_outcome(gates) == "hard_fail"

    def test_g3_failure_is_hard_fail(self):
        gates = _all_pass()
        gates["g3_quotable"] = _gate(False)
        assert classify_gate_outcome(gates) == "hard_fail"

    def test_g7_failure_is_hard_fail(self):
        gates = _all_pass()
        gates["g7_authority"] = _gate(False)
        assert classify_gate_outcome(gates) == "hard_fail"

    def test_g4_failure_is_candidate_fallback(self):
        gates = _all_pass()
        gates["g4_rationale"] = _gate(False)
        assert classify_gate_outcome(gates) == "candidate_fallback"

    def test_g5_failure_is_candidate_fallback(self):
        gates = _all_pass()
        gates["g5_scope"] = _gate(False)
        assert classify_gate_outcome(gates) == "candidate_fallback"

    def test_g6_failure_is_candidate_fallback(self):
        gates = _all_pass()
        gates["g6_temporal"] = _gate(False)
        assert classify_gate_outcome(gates) == "candidate_fallback"

    def test_g8_failure_is_candidate_fallback(self):
        gates = _all_pass()
        gates["g8_self_contained"] = _gate(False)
        assert classify_gate_outcome(gates) == "candidate_fallback"

    def test_hard_overrides_soft(self):
        """g7 failure alongside g8 failure → hard_fail (hard wins)."""
        gates = _all_pass()
        gates["g7_authority"] = _gate(False)
        gates["g8_self_contained"] = _gate(False)
        assert classify_gate_outcome(gates) == "hard_fail"

    def test_multiple_soft_failures(self):
        gates = _all_pass()
        gates["g4_rationale"] = _gate(False)
        gates["g6_temporal"] = _gate(False)
        assert classify_gate_outcome(gates) == "candidate_fallback"

    def test_g4_g8_failure_together(self):
        gates = _all_pass()
        gates["g4_rationale"] = _gate(False)
        gates["g8_self_contained"] = _gate(False)
        assert classify_gate_outcome(gates) == "candidate_fallback"
