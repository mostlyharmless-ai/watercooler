"""Tests for the decisions-first precedence helpers.

Covers the post-rank boost applied by ``watercooler_search`` and
``watercooler_smart_query`` when ``prioritize_decisions=True``.
"""

from __future__ import annotations

import json

import pytest

from watercooler_mcp.tools._boost import sanitize_boost
from watercooler_mcp.tools.decisions import _parse_confidence, _parse_iso
from watercooler_mcp.tools.graph import _apply_decision_boost
from watercooler_mcp.tools.memory import _apply_decision_boost_evidence


class TestParseConfidence:
    def test_valid_range(self):
        for n in range(0, 6):
            assert _parse_confidence(f"Confidence: {n}/5\n\nBody") == n

    def test_out_of_range_rejected(self):
        """Confidence is a 0..5 rubric; a malformed LLM body must not
        propagate a schema-violating integer downstream where
        ``confidence_min`` filtering compares raw values."""
        assert _parse_confidence("Confidence: 10/5\n\nBody") is None
        assert _parse_confidence("Confidence: 99/5\n\nBody") is None

    def test_missing_marker_returns_none(self):
        assert _parse_confidence("No marker here") is None
        assert _parse_confidence("") is None

    def test_accepts_later_body_lines(self):
        body = "Some preamble\nConfidence: 3/5\nMore body"
        assert _parse_confidence(body) == 3

    @pytest.mark.parametrize(
        "body",
        [None, {"text": "Confidence: 3/5"}, ["Confidence: 3/5"], 42, True],
        ids=["none", "dict", "list", "int", "bool"],
    )
    def test_non_string_body_returns_none(self, body):
        """A corrupted entries.jsonl with a non-string ``body`` field must
        not crash ``_parse_confidence`` — the regex only accepts strings."""
        assert _parse_confidence(body) is None


class TestParseIso:
    def test_valid_iso_parses(self):
        assert _parse_iso("2026-04-22T15:30:00Z") is not None
        assert _parse_iso("2026-04-22T15:30:00+00:00") is not None

    def test_empty_returns_none(self):
        assert _parse_iso("") is None

    def test_malformed_string_returns_none(self):
        assert _parse_iso("not a timestamp") is None
        assert _parse_iso("2025/01/01") is None

    @pytest.mark.parametrize(
        "value",
        [None, 1735948800, 1735948800.5, ["2026-04-22"], {"ts": "2026-04-22"}, True],
        ids=["none", "int_epoch", "float_epoch", "list", "dict", "bool"],
    )
    def test_non_string_input_returns_none(self, value):
        """``timestamp`` from a corrupted entries.jsonl could be any JSON
        scalar. ``_parse_iso`` must never call ``.replace`` on a non-string
        (would raise AttributeError not caught by ValueError handler)."""
        assert _parse_iso(value) is None


class TestSanitizeBoost:
    def test_valid_boost_passes_through(self):
        assert sanitize_boost(1.5) == 1.5
        assert sanitize_boost(2.0) == 2.0
        assert sanitize_boost(0.5) == 0.5

    def test_boost_of_one_is_unchanged(self):
        assert sanitize_boost(1.0) == 1.0

    @pytest.mark.parametrize(
        "bad",
        [float("nan"), float("inf"), float("-inf"), -1.0, 0.0, -0.0],
        ids=["nan", "inf", "neg_inf", "negative", "zero", "neg_zero"],
    )
    def test_pathological_floats_collapse_to_noop(self, bad):
        """NaN corrupts TimSort, inf collapses ranking, negative flips
        order while decisions_prioritized=True claims the opposite.
        All must be treated as 1.0 (no-op)."""
        assert sanitize_boost(bad) == 1.0

    @pytest.mark.parametrize(
        "bad",
        [None, "bad", [1.5], {"boost": 2}, object()],
        ids=["none", "garbage_str", "list", "dict", "object"],
    )
    def test_non_numeric_collapses_to_noop(self, bad):
        assert sanitize_boost(bad) == 1.0

    def test_numeric_string_is_coerced(self):
        """A numeric string is semantically equivalent to the float; coerce
        rather than collapse so a Pydantic-bypass callsite doesn't
        silently lose the caller's intent."""
        assert sanitize_boost("2.0") == 2.0
        assert sanitize_boost("0.5") == 0.5

    def test_absurd_positive_boost_clamped(self):
        """A multiplier so large it erases the backend's ranking is
        clamped to the ceiling."""
        assert sanitize_boost(1e9) == 100.0
        assert sanitize_boost(1_000_000.0) == 100.0


def _result_json(items):
    return json.dumps({"count": len(items), "results": items})


class TestApplyDecisionBoost:
    def test_boosts_and_resorts_decision_entries(self):
        items = [
            {"type": "entry", "score": 1.0, "entry": {"entry_type": "Note"}},
            {"type": "entry", "score": 0.5, "entry": {"entry_type": "Decision"}},
            {"type": "entry", "score": 0.4, "entry": {"entry_type": "Plan"}},
        ]
        out = json.loads(_apply_decision_boost(_result_json(items), 1.5))
        assert out["decisions_prioritized"] is True
        assert out["decision_boost"] == 1.5
        # 0.5 * 1.5 = 0.75 — still below the 1.0 Note
        scores = [r["score"] for r in out["results"]]
        assert scores == sorted(scores, reverse=True)

    def test_boost_moves_decision_above_note(self):
        items = [
            {"type": "entry", "score": 0.9, "entry": {"entry_type": "Note"}},
            {"type": "entry", "score": 0.7, "entry": {"entry_type": "Decision"}},
        ]
        out = json.loads(_apply_decision_boost(_result_json(items), 1.5))
        # 0.7 * 1.5 = 1.05 > 0.9 — Decision floats up
        assert out["results"][0]["entry"]["entry_type"] == "Decision"

    def test_boost_1_is_noop(self):
        items = [
            {"type": "entry", "score": 0.5, "entry": {"entry_type": "Decision"}},
        ]
        raw = _result_json(items)
        assert _apply_decision_boost(raw, 1.0) == raw

    def test_no_decisions_passes_through_unchanged(self):
        items = [
            {"type": "entry", "score": 1.0, "entry": {"entry_type": "Note"}},
        ]
        out = json.loads(_apply_decision_boost(_result_json(items), 1.5))
        assert "decisions_prioritized" not in out
        assert out["results"][0]["score"] == 1.0

    def test_malformed_json_returned_unchanged(self):
        raw = "not json"
        assert _apply_decision_boost(raw, 1.5) == raw

    def test_thread_node_ignored(self):
        """Thread-type results have no ``entry`` payload — must not crash."""
        items = [
            {"type": "thread", "score": 1.0, "thread": {"topic": "foo"}},
        ]
        out = json.loads(_apply_decision_boost(_result_json(items), 1.5))
        assert out["results"][0]["score"] == 1.0


class TestApplyDecisionBoostEvidence:
    def test_boosts_t1_decision_evidence(self):
        evidence = [
            {
                "tier": "T1", "score": 1.0,
                "metadata": {"entry_type": "Note"},
            },
            {
                "tier": "T1", "score": 0.7,
                "metadata": {"entry_type": "Decision"},
            },
        ]
        assert _apply_decision_boost_evidence(evidence, 1.5) is True
        # After boost: 1.0, ~1.05 — Decision floats up
        assert evidence[0]["metadata"]["entry_type"] == "Decision"
        assert evidence[0]["score"] == pytest.approx(1.05)

    def test_t2_without_entry_type_unchanged(self):
        evidence = [
            {"tier": "T2", "score": 1.0, "metadata": {}},
        ]
        assert _apply_decision_boost_evidence(evidence, 1.5) is False
        assert evidence[0]["score"] == 1.0

    def test_boost_1_is_noop(self):
        evidence = [
            {"tier": "T1", "score": 0.5, "metadata": {"entry_type": "Decision"}},
        ]
        assert _apply_decision_boost_evidence(evidence, 1.0) is False
