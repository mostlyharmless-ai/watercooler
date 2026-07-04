"""Unit tests for ExtractDecisionsDaemon._build_authority_fields (Phase 4e)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from watercooler.config_schema import DecisionExtractorConfig
from watercooler.decision_extraction import ExtractionResult, LLMExtraction
from watercooler_mcp.daemons.decision_extractor import ExtractDecisionsDaemon


SOURCE_ID = "01HZA1T0BC3D4E5F6G7H8J9K0M"


def _daemon(tmp_path: Path, **cfg_overrides: Any) -> ExtractDecisionsDaemon:
    cfg = DecisionExtractorConfig(**cfg_overrides)
    return ExtractDecisionsDaemon(
        config=cfg,
        threads_dir=tmp_path / "threads",
        llm_client=None,
    )


def _extraction_result(confidence: int = 5, with_gates: bool = True) -> ExtractionResult:
    gates: dict[str, Any] = {}
    if with_gates:
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
    return ExtractionResult(
        entry_id=SOURCE_ID,
        topic="t",
        passed=True,
        confidence=confidence,
        gate_results=gates,
        decision_body="Spec: decision-extractor\n\n## Decision\nX",
        rejection_reason=None,
        extraction=LLMExtraction(
            gates=gates,
            confidence=confidence,
            decision_statement="X",
            rationale="r",
            scope="s",
            alternatives_considered=None,
            verbatim_quotes=["q"],
            warning=None,
        ),
    )


class TestBuildAuthorityFields:
    def test_actor_class_is_daemon(self, tmp_path):
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(SOURCE_ID, _extraction_result())
        assert fields["actor_class"] == "daemon"

    def test_decision_origin_is_daemon_extraction(self, tmp_path):
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(SOURCE_ID, _extraction_result())
        assert fields["decision_origin"] == "daemon_extraction"

    def test_source_entry_id_propagated(self, tmp_path):
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(SOURCE_ID, _extraction_result())
        assert fields["source_entry_id"] == SOURCE_ID

    def test_confidence_propagated(self, tmp_path):
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(SOURCE_ID, _extraction_result(confidence=4))
        assert fields["confidence"] == 4

    def test_gate_results_propagated(self, tmp_path):
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(SOURCE_ID, _extraction_result())
        assert "g1_commitment" in fields["gate_results"]
        assert fields["gate_results"]["g1_commitment"]["passed"] is True

    def test_authority_source_never_stamped(self, tmp_path):
        """The server-side authority gate was removed; the daemon no longer
        stamps ``authority_source`` (it only fed the gate's policy
        resolution). The inert provenance fields remain."""
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(SOURCE_ID, _extraction_result())
        assert "authority_source" not in fields

    def test_authority_basis_intentionally_omitted_for_daemon(self, tmp_path):
        """authority_basis applies to human Decisions per v0.10 §5.4.1 lint;
        daemon writes don't carry it."""
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(SOURCE_ID, _extraction_result())
        assert "authority_basis" not in fields

    def test_no_gate_results_when_extraction_has_none(self, tmp_path):
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(
            SOURCE_ID, _extraction_result(with_gates=False)
        )
        assert "gate_results" not in fields or fields.get("gate_results") == {}

    def test_confidence_zero_is_omitted(self, tmp_path):
        """Phase 4a schema requires ``confidence: minimum=1``. The
        extractor rubric uses 0 for "not a decision" rejections and the
        ``llm_unavailable`` / ``llm_parse_failure`` shapes. Stamping a 0
        on the entry node would fail schema validation once enforcement
        lands. Regression for the #865 + #860 reconciliation finding."""
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(
            SOURCE_ID, _extraction_result(confidence=0)
        )
        assert "confidence" not in fields

    def test_confidence_above_5_is_omitted(self, tmp_path):
        """Out-of-range values (e.g. malformed LLM output coerced into
        confidence) are also omitted rather than stamped."""
        d = _daemon(tmp_path)
        fields = d._build_authority_fields(
            SOURCE_ID, _extraction_result(confidence=99)
        )
        assert "confidence" not in fields

    def test_confidence_in_valid_range_is_stamped(self, tmp_path):
        """Sanity check for the rubric boundaries — 1 and 5 inclusive."""
        d = _daemon(tmp_path)
        for c in (1, 2, 3, 4, 5):
            fields = d._build_authority_fields(
                SOURCE_ID, _extraction_result(confidence=c)
            )
            assert fields.get("confidence") == c

    def test_append_entry_drops_out_of_range_confidence_structurally(self, tmp_path):
        """Defence in depth: even if a *future* caller passes
        ``authority_fields={"confidence": 0}`` directly to
        ``commands_graph.append_entry``, the whitelist drops the value
        rather than producing a schema-invalid entry node. The producer
        side (``_build_authority_fields``) already omits 0, but the
        boundary clamp closes the bug class structurally. Regression for
        the #865 re-review N1 finding."""
        from watercooler.baseline_graph import storage
        from watercooler.commands_graph import append_entry, init_thread
        from watercooler.baseline_graph.writer import get_entries_for_thread
        from ulid import ULID

        topic = "test-confidence-boundary"
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        init_thread(topic, threads_dir=threads_dir, title="t")
        entry_id = str(ULID())

        # Inject confidence=0 via the public API.
        append_entry(
            topic,
            threads_dir=threads_dir,
            agent="ExtractDecisionsDaemon",
            role="scribe",
            title="X",
            entry_type="Note",
            body="Spec: scribe\n\nX",
            entry_id=entry_id,
            authority_fields={
                "actor_class": "daemon",
                "decision_origin": "daemon_extraction",
                "confidence": 0,  # out of range
            },
        )

        # The on-disk entry must NOT carry ``confidence: 0`` — the
        # boundary clamp dropped it.
        entries = list(get_entries_for_thread(threads_dir, topic))
        match = next(e for e in entries if str(e.get("entry_id")) == entry_id)
        assert "confidence" not in match
        # Other authority fields still passed through.
        assert match.get("actor_class") == "daemon"
