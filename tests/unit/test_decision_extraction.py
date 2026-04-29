"""Tests for decision_extraction — LLM-powered extraction with 8-gate validation."""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from watercooler.decision_extraction import (
    _build_user_prompt,
    _CANDIDATE_ENTRY_CLOSE,
    _CANDIDATE_ENTRY_OPEN,
    ExtractionResult,
    GateResult,
    LLMExtraction,
    extract_decision,
    format_decision_body,
    _parse_llm_response,
    _strip_prompt_delimiters,
    _THREAD_CONTEXT_CLOSE,
    _THREAD_CONTEXT_OPEN,
    _validate_gate_consistency,
    _validate_quotes,
    SYSTEM_PROMPT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_gates_pass() -> dict[str, Any]:
    """Return gate results where all 8 gates pass."""
    return {
        f"g{i}_{name}": {"passed": True, "reason": f"Gate {i} passes"}
        for i, name in enumerate(
            ["commitment", "not_superseded", "quotable", "rationale",
             "scope", "temporal", "authority", "self_contained"],
            start=1,
        )
    }


def _full_llm_response(
    confidence: int = 4,
    gates: dict | None = None,
    quotes: list[str] | None = None,
    **overrides: Any,
) -> str:
    """Build a valid JSON LLM response."""
    data = {
        "gates": gates or _all_gates_pass(),
        "confidence": confidence,
        "decision_statement": "Use PostgreSQL for session storage",
        "rationale": "Performance benchmarks showed 3x improvement",
        "scope": "Session storage subsystem",
        "alternatives_considered": "Redis (rejected: slower)",
        "verbatim_quotes": quotes or ["We decided to use PostgreSQL"],
        "warning": None,
    }
    data.update(overrides)
    return json.dumps(data)


def _make_entry(
    body: str = "We decided to use PostgreSQL for session storage.",
    **overrides: Any,
) -> dict[str, Any]:
    entry = {
        "entry_id": "01TESTENTRY",
        "thread_topic": "test-topic",
        "index": 7,
        "agent": "Claude (jay)",
        "role": "implementer",
        "entry_type": "Note",
        "timestamp": "2025-01-15T12:00:00Z",
        "title": "Storage decision",
        "body": body,
        "summary": "Decision about storage",
    }
    entry.update(overrides)
    return entry


def _mock_llm(response: str | None) -> Any:
    """Return a callable that returns the given response."""
    def _complete(system: str, user: str) -> Optional[str]:
        return response
    return _complete


# ---------------------------------------------------------------------------
# Tests — extract_decision()
# ---------------------------------------------------------------------------


class TestExtractDecision:
    def test_all_gates_pass(self):
        entry = _make_entry()
        response = _full_llm_response(
            confidence=4,
            quotes=["We decided to use PostgreSQL"],
        )
        result = extract_decision(
            entry, "Thread context here",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is True
        assert result.confidence == 4
        assert result.decision_body is not None
        assert "PostgreSQL" in result.decision_body

    def test_gate1_fails(self):
        gates = _all_gates_pass()
        gates["g1_commitment"] = {"passed": False, "reason": "No commitment language"}
        response = _full_llm_response(confidence=2, gates=gates)
        result = extract_decision(
            _make_entry(), "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False

    def test_gate7_fails(self):
        gates = _all_gates_pass()
        gates["g7_authority"] = {"passed": False, "reason": "Authority laundering detected"}
        response = _full_llm_response(confidence=2, gates=gates)
        result = extract_decision(
            _make_entry(), "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False

    def test_low_confidence(self):
        response = _full_llm_response(
            confidence=2,
            quotes=["We decided to use PostgreSQL"],
        )
        result = extract_decision(
            _make_entry(), "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False
        assert "low_confidence" in result.rejection_reason

    def test_confidence_3_with_warning(self):
        response = _full_llm_response(
            confidence=3,
            quotes=["We decided to use PostgreSQL"],
            warning="Rationale is partially inferred",
        )
        result = extract_decision(
            _make_entry(), "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is True
        assert result.confidence == 3
        assert "Warning:" in result.decision_body

    def test_llm_returns_none(self):
        result = extract_decision(
            _make_entry(), "context",
            llm_complete=_mock_llm(None),
        )
        assert result.passed is False
        assert result.rejection_reason == "llm_unavailable"
        assert result.extraction is None

    def test_llm_returns_invalid_json(self):
        result = extract_decision(
            _make_entry(), "context",
            llm_complete=_mock_llm("this is not json at all"),
        )
        assert result.passed is False
        assert result.rejection_reason == "llm_parse_failure"

    def test_body_truncation(self):
        long_body = "We decided X. " * 1000  # ~14k chars
        entry = _make_entry(body=long_body, summary="Short summary")
        calls = []

        def _capturing_llm(system: str, user: str) -> str:
            calls.append(user)
            return _full_llm_response(
                confidence=4,
                quotes=["Short summary"],
            )

        extract_decision(
            entry, "context",
            llm_complete=_capturing_llm,
            max_body_chars=100,
        )
        # Should use summary when body exceeds threshold
        assert "truncated" in calls[0]

    def test_prefers_summary_for_long_body(self):
        long_body = "X " * 5000
        entry = _make_entry(body=long_body, summary="Concise summary here")
        calls = []

        def _capturing_llm(system: str, user: str) -> str:
            calls.append(user)
            return _full_llm_response(
                confidence=4,
                quotes=["Concise summary here"],
            )

        extract_decision(
            entry, "context",
            llm_complete=_capturing_llm,
            max_body_chars=100,
        )
        assert "Concise summary here" in calls[0]

    def test_hallucinated_quote_rejected(self):
        entry = _make_entry(body="We decided to use PostgreSQL.")
        response = _full_llm_response(
            confidence=4,
            quotes=["We decided to use MySQL"],  # Not in source!
        )
        result = extract_decision(
            entry, "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False
        assert result.rejection_reason == "hallucinated_quote"

    def test_empty_quotes_rejected(self):
        response = _full_llm_response(
            confidence=4,
            quotes=["", "   "],
        )
        result = extract_decision(
            _make_entry(), "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False
        assert result.rejection_reason == "missing_quote_evidence"

    def test_quote_case_sensitive(self):
        """'We decided' does NOT match 'we decided' — case-sensitive."""
        entry = _make_entry(body="We decided to use PostgreSQL.")
        response = _full_llm_response(
            confidence=4,
            quotes=["we decided to use PostgreSQL."],  # Wrong case
        )
        result = extract_decision(
            entry, "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False
        assert result.rejection_reason == "hallucinated_quote"

    def test_quote_negation_preserved(self):
        """'we decided to use X' does NOT match source containing 'we decided NOT to use X'."""
        entry = _make_entry(body="We decided NOT to use Redis.")
        response = _full_llm_response(
            confidence=4,
            quotes=["We decided to use Redis."],  # Missing negation
        )
        result = extract_decision(
            entry, "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False
        assert result.rejection_reason == "hallucinated_quote"

    def test_critical_gate_fail_overrides_confidence(self):
        """Gate 7 fail + confidence 4 → rejected (gate consistency check)."""
        gates = _all_gates_pass()
        gates["g7_authority"] = {"passed": False, "reason": "Laundered"}
        response = _full_llm_response(confidence=4, gates=gates, quotes=["We decided to use PostgreSQL"])
        result = extract_decision(
            _make_entry(), "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False
        assert "g7_authority" in result.rejection_reason

    def test_caps_field_lengths(self):
        """Oversized fields are truncated to _MAX_FIELD_CHARS."""
        long_statement = "X" * 5000
        response = _full_llm_response(
            confidence=4,
            decision_statement=long_statement,
            quotes=["We decided to use PostgreSQL"],
        )
        result = extract_decision(
            _make_entry(), "context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is True
        assert len(result.extraction.decision_statement) <= 2000


# ---------------------------------------------------------------------------
# Tests — format_decision_body()
# ---------------------------------------------------------------------------


class TestFormatDecisionBody:
    def _make_result(self, confidence: int = 4, **overrides) -> ExtractionResult:
        ext_kwargs = {
            "gates": {},
            "confidence": confidence,
            "decision_statement": "Use PostgreSQL",
            "rationale": "Better performance",
            "scope": "Storage subsystem",
            "alternatives_considered": "Redis",
            "verbatim_quotes": ["We decided to use PostgreSQL"],
            "warning": None,
        }
        ext_kwargs.update(overrides)
        return ExtractionResult(
            entry_id="01TEST",
            topic="test-topic",
            passed=True,
            confidence=confidence,
            gate_results={},
            decision_body=None,
            rejection_reason=None,
            extraction=LLMExtraction(**ext_kwargs),
        )

    def test_full_body(self):
        result = self._make_result()
        entry = _make_entry()
        body = format_decision_body(result, entry)
        assert "Spec: decision-extractor" in body
        assert "[automated: decision_extractor]" in body
        assert "Confidence: 4/5" in body
        assert "## Decision" in body
        assert "## Rationale" in body
        assert "## Scope" in body
        assert "## Alternatives Considered" in body
        assert "## Evidence" in body
        assert "> We decided to use PostgreSQL" in body
        assert "01TESTENTRY" in body
        assert "#7" in body
        assert '"Storage decision"' in body

    def test_evidence_without_index(self):
        result = self._make_result()
        entry = _make_entry(index=None)
        body = format_decision_body(result, entry)
        assert "01TESTENTRY" in body
        assert "#" not in body.split("## Evidence")[1].split("\n")[1]
        assert '"Storage decision"' in body

    def test_evidence_without_title(self):
        result = self._make_result()
        entry = _make_entry(title=None)
        body = format_decision_body(result, entry)
        assert "#7" in body
        assert "01TESTENTRY" in body
        assert "Storage decision" not in body

    def test_no_alternatives(self):
        result = self._make_result(alternatives_considered=None)
        body = format_decision_body(result, _make_entry())
        assert "Alternatives" not in body

    def test_low_confidence_warning(self):
        result = self._make_result(
            confidence=3,
            warning="Rationale partially inferred",
        )
        body = format_decision_body(result, _make_entry())
        assert "Warning: Rationale partially inferred" in body

    def test_low_confidence_default_warning(self):
        result = self._make_result(confidence=3, warning=None)
        body = format_decision_body(result, _make_entry())
        assert "Warning: Confidence below 4" in body


# ---------------------------------------------------------------------------
# Tests — _parse_llm_response()
# ---------------------------------------------------------------------------


class TestParseLLMResponse:
    def test_valid_json(self):
        raw = _full_llm_response()
        result = _parse_llm_response(raw)
        assert result is not None
        assert result.confidence == 4

    def test_fenced_json(self):
        raw = "```json\n" + _full_llm_response() + "\n```"
        result = _parse_llm_response(raw)
        assert result is not None

    def test_invalid_json_returns_none(self):
        assert _parse_llm_response("not json") is None

    def test_whitespace_quotes_filtered(self):
        result = _parse_llm_response(
            _full_llm_response(quotes=["   ", "We decided to use PostgreSQL"])
        )
        assert result is not None
        assert result.verbatim_quotes == ["We decided to use PostgreSQL"]

    def test_missing_gates_returns_none(self):
        result = _parse_llm_response('{"confidence": 3}')
        # Should still parse — missing gates get defaults
        assert result is not None
        # But all gates will be "not evaluated"
        for gate in result.gates.values():
            assert gate["passed"] is False

    def test_confidence_clamped(self):
        raw = _full_llm_response(confidence=99)
        result = _parse_llm_response(raw)
        assert result.confidence == 5

        raw2 = _full_llm_response(confidence=-5)
        result2 = _parse_llm_response(raw2)
        assert result2.confidence == 0


# ---------------------------------------------------------------------------
# Tests — validation helpers
# ---------------------------------------------------------------------------


class TestValidateQuotes:
    def test_valid_quote(self):
        assert _validate_quotes(
            ["We decided to use PostgreSQL"],
            "We decided to use PostgreSQL for storage.",
        ) is None

    def test_hallucinated(self):
        assert _validate_quotes(
            ["We decided to use MySQL"],
            "We decided to use PostgreSQL.",
        ) == "hallucinated_quote"

    def test_whitespace_normalized(self):
        """Extra whitespace in both quote and source is collapsed."""
        assert _validate_quotes(
            ["We  decided   to use PostgreSQL"],
            "We decided  to  use PostgreSQL.",
        ) is None

    def test_case_sensitive(self):
        assert _validate_quotes(
            ["we decided"],
            "We decided to do X.",
        ) == "hallucinated_quote"

    def test_empty_quotes_rejected(self):
        assert _validate_quotes([], "any body") == "missing_quote_evidence"

    def test_blank_quote_rejected(self):
        assert _validate_quotes(["", "  "], "any body") == "missing_quote_evidence"

    def test_unicode_punctuation_normalized(self):
        assert _validate_quotes(
            ['We decided “alpha” — it’s faster'],
            'We decided "alpha" - it\'s faster for the hot path.',
        ) is None


class TestValidateGateConsistency:
    def test_all_pass(self):
        gates = {
            "g1_commitment": GateResult(passed=True, reason="ok"),
            "g7_authority": GateResult(passed=True, reason="ok"),
        }
        assert _validate_gate_consistency(gates, 4) is None

    def test_critical_fail_low_confidence(self):
        """Critical gate fail + confidence < 3 is OK (no override needed)."""
        gates = {"g1_commitment": GateResult(passed=False, reason="no")}
        assert _validate_gate_consistency(gates, 2) is None

    def test_critical_fail_high_confidence(self):
        """Critical gate fail + confidence >= 3 → rejection."""
        gates = {"g7_authority": GateResult(passed=False, reason="laundered")}
        result = _validate_gate_consistency(gates, 4)
        assert result is not None
        assert "g7_authority" in result


# ---------------------------------------------------------------------------
# Tests — Fix #481: g3_quotable=false must reject even when quotes validate
# ---------------------------------------------------------------------------


class TestG3QuotableEnforcement:
    def test_g3_false_rejects_even_with_local_quote_match(self):
        """LLM says not quotable — honor that judgment.

        Previously the extractor ignored ``g3_quotable.passed=false`` when the
        returned quotes happened to substring-match the source body. That let
        the LLM's own judgment be overridden by a coincidental local match and
        produced low-quality Decision entries. See issue #481.
        """
        entry = _make_entry(
            body="We decided to use PostgreSQL for session storage.",
        )
        gates = _all_gates_pass()
        gates["g3_quotable"] = {"passed": False, "reason": "not quotable"}
        response = _full_llm_response(
            confidence=4,
            gates=gates,
            quotes=["We decided to use PostgreSQL"],  # matches the body
        )
        result = extract_decision(
            entry,
            "recent context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False
        assert result.rejection_reason == "g3_quotable_failed"

    def test_g3_true_allows_passage(self):
        """Sanity check: g3_quotable=true still passes."""
        entry = _make_entry()
        response = _full_llm_response(confidence=4)  # gates all pass by default
        result = extract_decision(
            entry,
            "recent context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is True

    def test_g3_omitted_rejects_as_not_evaluated(self):
        """A missing g3_quotable gate defaults to passed=False (fail-closed)
        and must be rejected with a distinct ``g3_quotable_not_evaluated``
        reason so telemetry can tell the omitted case apart from an explicit
        ``passed=false`` verdict.
        """
        entry = _make_entry()
        gates = _all_gates_pass()
        del gates["g3_quotable"]  # LLM omits the gate entirely
        response = _full_llm_response(confidence=4, gates=gates)
        result = extract_decision(
            entry,
            "recent context",
            llm_complete=_mock_llm(response),
        )
        assert result.passed is False
        assert result.rejection_reason == "g3_quotable_not_evaluated"

    @pytest.mark.parametrize(
        "g3_value",
        [
            [],           # empty list
            ["passed"],   # non-empty list
            False,        # bool — has no .get()
            "bad",        # string
            42,           # int
        ],
        ids=["empty_list", "list", "bool", "string", "int"],
    )
    def test_g3_non_mapping_value_rejects_as_malformed(
        self, g3_value, monkeypatch
    ):
        """Defense-in-depth: a present-but-non-mapping ``g3_quotable`` value
        (``[]``, ``False``, ``"bad"``, etc.) must reject as malformed rather
        than crash on ``g3.get(...)`` or ``"passed" not in g3``.

        Same parser-drift class the hardening defends against — if the
        parser ever hands us a non-dict gate value, the guard has to
        classify and reject, not blow up inside the very branch meant to
        enforce fail-closed semantics.
        """
        from watercooler.decision_extraction import LLMExtraction
        import watercooler.decision_extraction as dex

        def _parser_non_mapping_g3(raw: str):
            gates = {
                f"g{i}_{name}": {"passed": True, "reason": "ok"}
                for i, name in enumerate(
                    ["commitment", "not_superseded", "rationale",
                     "scope", "temporal", "authority", "self_contained"],
                    start=1,
                )
                if f"g{i}_{name}" != "g3_quotable"
            }
            gates["g3_quotable"] = g3_value  # type: ignore[assignment]
            return LLMExtraction(
                gates=gates,
                confidence=4,
                decision_statement="x",
                rationale="x",
                scope="x",
                alternatives_considered=None,
                verbatim_quotes=["matches body"],
                warning=None,
            )

        monkeypatch.setattr(dex, "_parse_llm_response", _parser_non_mapping_g3)

        result = extract_decision(
            _make_entry(body="matches body"),
            "context",
            llm_complete=_mock_llm(_full_llm_response()),
        )
        assert result.passed is False
        assert result.rejection_reason == "g3_quotable_malformed"

    def test_g3_present_but_missing_passed_key_rejects_as_malformed(self, monkeypatch):
        """Defense-in-depth: if the parser hands us a ``g3_quotable`` entry
        that lacks the ``passed`` key entirely, the guard must still
        fail-closed (reject) rather than raise ``KeyError``.

        This is the same drift scenario as the missing-gate case, one
        level deeper — current parser shape keeps this unreachable, but
        any future change that returns a malformed gate dict must not
        let the extraction through.
        """
        from watercooler.decision_extraction import LLMExtraction
        import watercooler.decision_extraction as dex

        def _parser_malformed_g3(raw: str):
            gates = {
                f"g{i}_{name}": {"passed": True, "reason": "ok"}
                for i, name in enumerate(
                    ["commitment", "not_superseded", "rationale",
                     "scope", "temporal", "authority", "self_contained"],
                    start=1,
                )
                if f"g{i}_{name}" != "g3_quotable"
            }
            # g3 present but lacks ``passed`` — simulated parser drift.
            gates["g3_quotable"] = {"reason": "missing passed"}
            return LLMExtraction(
                gates=gates,
                confidence=4,
                decision_statement="x",
                rationale="x",
                scope="x",
                alternatives_considered=None,
                verbatim_quotes=["matches body"],
                warning=None,
            )

        monkeypatch.setattr(dex, "_parse_llm_response", _parser_malformed_g3)

        result = extract_decision(
            _make_entry(body="matches body"),
            "context",
            llm_complete=_mock_llm(_full_llm_response()),
        )
        assert result.passed is False
        assert result.rejection_reason == "g3_quotable_malformed"

    def test_g3_missing_from_gates_rejects_as_missing(self, monkeypatch):
        """Defense-in-depth: if the parser's default-injection ever stops
        running and ``extraction.gates`` literally lacks ``g3_quotable``,
        the rejection branch must still fire (fail-closed) rather than
        fall through to quote validation.

        Patches ``_parse_llm_response`` to return an extraction whose
        ``gates`` dict has no ``g3_quotable`` key at all — the state the
        current parser can't produce but a future change could.
        """
        from watercooler.decision_extraction import LLMExtraction
        import watercooler.decision_extraction as dex

        def _parser_without_g3(raw: str):
            return LLMExtraction(
                gates={
                    f"g{i}_{name}": {"passed": True, "reason": "ok"}
                    for i, name in enumerate(
                        ["commitment", "not_superseded", "rationale",
                         "scope", "temporal", "authority", "self_contained"],
                        start=1,
                    )
                    if f"g{i}_{name}" != "g3_quotable"
                },
                confidence=4,
                decision_statement="x",
                rationale="x",
                scope="x",
                alternatives_considered=None,
                verbatim_quotes=["matches body"],
                warning=None,
            )

        monkeypatch.setattr(dex, "_parse_llm_response", _parser_without_g3)

        result = extract_decision(
            _make_entry(body="matches body"),
            "context",
            llm_complete=_mock_llm(_full_llm_response()),
        )
        assert result.passed is False
        assert result.rejection_reason == "g3_quotable_missing"


# ---------------------------------------------------------------------------
# Tests — Prompt hardening: CANDIDATE_ENTRY quote provenance
# ---------------------------------------------------------------------------


class TestPromptProvenance:
    def test_system_prompt_forbids_thread_context_quotes(self):
        """The SYSTEM_PROMPT must instruct the LLM to quote only from
        CANDIDATE_ENTRY — not THREAD_CONTEXT. This is the mechanical defense
        against ``hallucinated_quote`` rejections where the model quoted from
        surrounding context instead of the candidate itself.
        """
        assert "CANDIDATE_ENTRY" in SYSTEM_PROMPT
        # The prompt must explicitly forbid THREAD_CONTEXT as a quote source.
        assert "never from THREAD_CONTEXT" in SYSTEM_PROMPT
        # And explicitly instruct the LLM on what to do when no quote exists.
        assert "verbatim_quotes: []" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Tests — Fix #2: Long-body summary/quote validation mismatch
# ---------------------------------------------------------------------------


class TestLongBodySummaryQuotes:
    def test_long_body_summary_quotes_rejected(self):
        """Summary-derived quotes are not acceptable evidence."""
        long_body = "x" * 5000
        summary_text = "Team decided on PostgreSQL for storage."
        entry = _make_entry(
            body=long_body,
            summary=summary_text,
        )
        response = _full_llm_response(
            confidence=4,
            quotes=["Team decided on PostgreSQL for storage."],
        )
        result = extract_decision(
            entry, "Thread context",
            llm_complete=_mock_llm(response),
            max_body_chars=4000,
        )
        assert result.passed is False
        assert result.rejection_reason == "summary_only_quote_evidence"

    def test_long_body_source_quotes_preserved(self):
        """When body is truncated but quotes exist in original body, they're kept."""
        long_body = "We decided to use PostgreSQL. " + "x" * 5000
        entry = _make_entry(
            body=long_body,
            summary="Decision about storage",
        )
        response = _full_llm_response(
            confidence=4,
            quotes=["We decided to use PostgreSQL."],
        )
        result = extract_decision(
            entry, "Thread context",
            llm_complete=_mock_llm(response),
            max_body_chars=4000,
        )
        assert result.passed is True
        # Quotes from source body — confidence preserved, quotes kept
        assert result.confidence == 4
        assert result.extraction is not None
        assert len(result.extraction.verbatim_quotes) == 1


# ---------------------------------------------------------------------------
# Tests — Fix #3: Malformed gate payload crash
# ---------------------------------------------------------------------------


class TestMalformedGatePayload:
    def test_malformed_gate_string(self):
        """String gate value should not crash, should be treated as failed."""
        gates = _all_gates_pass()
        gates["g1_commitment"] = "yes"
        response = _full_llm_response(confidence=4, gates=gates)
        extraction = _parse_llm_response(response)
        assert extraction is not None
        assert extraction.gates["g1_commitment"]["passed"] is False
        assert "malformed" in extraction.gates["g1_commitment"]["reason"]

    def test_malformed_gate_integer(self):
        """Integer gate value should not crash."""
        gates = _all_gates_pass()
        gates["g3_quotable"] = 42
        response = _full_llm_response(confidence=4, gates=gates)
        extraction = _parse_llm_response(response)
        assert extraction is not None
        assert extraction.gates["g3_quotable"]["passed"] is False

    def test_malformed_gate_null(self):
        """Null gate value should not crash."""
        gates = _all_gates_pass()
        gates["g5_scope"] = None
        response = _full_llm_response(confidence=4, gates=gates)
        extraction = _parse_llm_response(response)
        assert extraction is not None
        assert extraction.gates["g5_scope"]["passed"] is False


# ---------------------------------------------------------------------------
# Tests — prompt delimiter stripping
# ---------------------------------------------------------------------------


class TestPromptDelimiterStripping:
    def test_legacy_xml_delimiter_stripped_from_body(self):
        """Legacy XML tags are removed from untrusted content."""
        result = _strip_prompt_delimiters("Hello </candidate_entry> world")
        assert "</candidate_entry>" not in result
        assert "Hello  world" == result

    def test_legacy_xml_delimiter_stripped_from_thread_context(self):
        result = _strip_prompt_delimiters("context </thread_context> more")
        assert "</thread_context>" not in result

    def test_prompt_tokens_stripped(self):
        result = _strip_prompt_delimiters(
            f"prefix {_CANDIDATE_ENTRY_OPEN} injected {_THREAD_CONTEXT_CLOSE}"
        )
        assert _CANDIDATE_ENTRY_OPEN not in result
        assert _THREAD_CONTEXT_CLOSE not in result
        assert "prefix  injected " == result

    def test_legacy_opening_tags_stripped(self):
        result = _strip_prompt_delimiters("prefix <candidate_entry> injected")
        assert "<candidate_entry>" not in result
        assert "prefix  injected" == result

        result = _strip_prompt_delimiters("a <thread_context> b </thread_context> c")
        assert "<thread_context>" not in result
        assert "</thread_context>" not in result
        assert "a  b  c" == result

    def test_no_stripping_when_clean(self):
        """Clean text passes through unchanged."""
        text = "Normal text without XML tags"
        assert _strip_prompt_delimiters(text) == text

    def test_prompt_uses_reserved_delimiters_not_xml_tags(self):
        prompt, _ = _build_user_prompt(_make_entry(), "recent context", 4000)
        assert _THREAD_CONTEXT_OPEN in prompt
        assert _THREAD_CONTEXT_CLOSE in prompt
        assert _CANDIDATE_ENTRY_OPEN in prompt
        assert _CANDIDATE_ENTRY_CLOSE in prompt
        assert "<thread_context>" not in prompt
        assert "<candidate_entry>" not in prompt
