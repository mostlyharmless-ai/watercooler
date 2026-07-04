"""Unit tests for Phase 1b candidate-Note routing in ExtractDecisionsDaemon."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from watercooler.baseline_graph import storage
from watercooler.config_schema import DecisionExtractorConfig
from watercooler.decision_extraction import ExtractionResult, LLMExtraction
from watercooler_mcp.daemons.daemon_write import DaemonWriteResult
from watercooler_mcp.daemons.decision_extractor import (
    CAT_CANDIDATE_NOTE,
    CAT_REJECTED_HARD_GATE,
    CAT_REJECTED_RATE_CAP,
    CAT_SUCCESS,
    ExtractDecisionsDaemon,
)
from watercooler_mcp.daemons.state import Finding

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_daemon(tmp_path: Path, **config_overrides: Any) -> ExtractDecisionsDaemon:
    cfg = DecisionExtractorConfig(**config_overrides)
    return ExtractDecisionsDaemon(
        config=cfg,
        threads_dir=tmp_path / "threads",
        llm_client=None,  # mocked at extract_decision level
    )


def _make_entry_dict(
    entry_id: str = "01SRCENTRY0000001",
    body: str = "We decided to use PostgreSQL for session storage.",
) -> dict[str, Any]:
    return {
        "id": f"entry:{entry_id}",
        "entry_id": entry_id,
        "agent": "Claude (jay)",
        "timestamp": "2026-01-15T12:00:00Z",
        "role": "planner",
        "entry_type": "Note",
        "title": "Storage decision",
        "summary": "Decision about storage",
        "body": body,
        "index": 1,
        "thread_topic": "test-thread",
    }


def _make_finding(
    entry_id: str = "01SRCENTRY0000001",
    topic: str = "test-thread",
    score: int = 5,
) -> Finding:
    from ulid import ULID

    return Finding(
        finding_id=str(ULID()),
        daemon_name="decision_detector",
        severity="info",
        category="decision_candidate",
        topic=topic,
        entry_id=entry_id,
        message=f"Decision candidate (score={score})",
        details={"score": score, "tier": "High" if score >= 4 else "Medium"},
        created_at=time.time(),
    )


def _extraction_result(
    rejection_reason: str = "soft_gate_failure",
    confidence: int = 3,
    gate_overrides: dict | None = None,
) -> ExtractionResult:
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
    ext = LLMExtraction(
        gates=gates,
        confidence=confidence,
        decision_statement="Use PostgreSQL for session storage",
        rationale="Performance",
        scope="Storage",
        alternatives_considered=None,
        verbatim_quotes=["We decided to use PostgreSQL"],
        warning=None,
    )
    return ExtractionResult(
        entry_id="01SRCENTRY0000001",
        topic="test-thread",
        passed=False,
        confidence=confidence,
        gate_results=gates,
        decision_body=None,
        rejection_reason=rejection_reason,
        extraction=ext,
    )


def _write_result(written: bool = True, pushed: bool = True) -> DaemonWriteResult:
    from ulid import ULID

    return DaemonWriteResult(
        written=written,
        pushed=pushed,
        entry_id=str(ULID()),
        error=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCandidateNoteRouting:
    def _run_process_candidate(
        self,
        tmp_path: Path,
        monkeypatch,
        extraction: ExtractionResult,
        finding: Finding,
        write_result: DaemonWriteResult | None = None,
    ) -> Finding | None:
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(parents=True, exist_ok=True)
        storage.ensure_graph_dir(threads_dir)
        daemon = _make_daemon(tmp_path)
        daemon._resolved_code_root = tmp_path

        entry_dict = _make_entry_dict()

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.get_entry_node_from_graph",
                return_value=entry_dict,
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=extraction,
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=write_result or _write_result(),
            ),
        ):
            return daemon._process_candidate(
                finding, threads_dir, tmp_path, MagicMock(), "2026-05-19"
            )

    def test_hard_fail_produces_private_finding(self, tmp_path, monkeypatch):
        """g1 failure → hard reject, no thread write."""
        extraction = _extraction_result(
            rejection_reason="critical_gate_g1_commitment_failed_with_confidence_4",
            confidence=4,
            gate_overrides={
                "g1_commitment": {"passed": False, "reason": "no commitment"}
            },
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding()
        )
        assert result is not None
        assert result.category == CAT_REJECTED_HARD_GATE

    def test_soft_fail_g6_produces_candidate_note(self, tmp_path, monkeypatch):
        """g6 failure + score 5 → candidate Note."""
        extraction = _extraction_result(
            rejection_reason="soft_gate_failure",
            confidence=4,
            gate_overrides={
                "g6_temporal": {"passed": False, "reason": "unclear timing"}
            },
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding(score=5)
        )
        assert result is not None
        assert result.category == CAT_CANDIDATE_NOTE

    def test_g8_only_failure_produces_candidate_note(self, tmp_path, monkeypatch):
        """g8 failure alone (soft gate) → candidate Note, not a hard reject."""
        extraction = _extraction_result(
            rejection_reason="soft_gate_failure",
            confidence=4,
            gate_overrides={
                "g8_self_contained": {"passed": False, "reason": "context missing"}
            },
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding(score=4)
        )
        assert result is not None
        assert result.category == CAT_CANDIDATE_NOTE

    def test_confidence3_all_gates_pass_produces_candidate_note(
        self, tmp_path, monkeypatch
    ):
        """Confidence-3 with all gates passing → candidate Note (not direct Decision)."""
        extraction = _extraction_result(
            rejection_reason="low_confidence_3",
            confidence=3,
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding(score=5)
        )
        assert result is not None
        assert result.category == CAT_CANDIDATE_NOTE

    def test_confidence4_all_gates_pass_produces_direct_decision(
        self, tmp_path, monkeypatch
    ):
        """Confidence-4, all gates pass → direct Decision (regression check)."""
        passing = ExtractionResult(
            entry_id="01SRCENTRY0000001",
            topic="test-thread",
            passed=True,
            confidence=4,
            gate_results={
                g: {"passed": True, "reason": "ok"}
                for g in [
                    "g1_commitment",
                    "g2_not_superseded",
                    "g3_quotable",
                    "g4_rationale",
                    "g5_scope",
                    "g6_temporal",
                    "g7_authority",
                    "g8_self_contained",
                ]
            },
            decision_body="Spec: decision-extractor\n\n## Decision\nUse PostgreSQL",
            rejection_reason=None,
            extraction=LLMExtraction(
                gates={},
                confidence=4,
                decision_statement="Use PostgreSQL for session storage",
                rationale="Performance",
                scope="Storage",
                alternatives_considered=None,
                verbatim_quotes=["We decided to use PostgreSQL"],
                warning=None,
            ),
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, passing, _make_finding(score=5)
        )
        assert result is not None
        assert result.category == CAT_SUCCESS

    def test_below_detector_threshold_stays_private(self, tmp_path, monkeypatch):
        """Soft-fail with detector score 3 (< min_extraction_score=4) → private Finding."""
        extraction = _extraction_result(
            rejection_reason="soft_gate_failure",
            confidence=4,
            gate_overrides={"g6_temporal": {"passed": False, "reason": "unclear"}},
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding(score=3)
        )
        assert result is not None
        assert result.category != CAT_CANDIDATE_NOTE

    def test_g3_quotable_failure_is_hard_reject(self, tmp_path, monkeypatch):
        """g3_quotable failure (quote enforcement) stays private."""
        extraction = _extraction_result(
            rejection_reason="g3_quotable_failed",
            confidence=4,
            gate_overrides={"g3_quotable": {"passed": False, "reason": "cannot quote"}},
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding(score=5)
        )
        assert result is not None
        assert result.category != CAT_CANDIDATE_NOTE

    def test_candidate_note_rate_cap(self, tmp_path, monkeypatch):
        """After hitting the rate cap, further extractions produce CAT_REJECTED_RATE_CAP."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(parents=True, exist_ok=True)
        storage.ensure_graph_dir(threads_dir)
        daemon = _make_daemon(tmp_path, candidate_note_rate_cap_per_thread_per_week=2)
        daemon._resolved_code_root = tmp_path

        entry_dict = _make_entry_dict()
        extraction = _extraction_result(
            rejection_reason="soft_gate_failure",
            confidence=4,
            gate_overrides={"g8_self_contained": {"passed": False, "reason": "x"}},
        )

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.get_entry_node_from_graph",
                return_value=entry_dict,
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=extraction,
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=_write_result(),
            ),
        ):
            # Emit 2 to fill the cap
            daemon._process_candidate(
                _make_finding(), threads_dir, tmp_path, MagicMock(), "2026-05-19"
            )
            daemon._process_candidate(
                _make_finding(), threads_dir, tmp_path, MagicMock(), "2026-05-19"
            )
            # Third should be rate-capped
            result = daemon._process_candidate(
                _make_finding(), threads_dir, tmp_path, MagicMock(), "2026-05-19"
            )

        assert result is not None
        assert result.category == CAT_REJECTED_RATE_CAP

    def test_hallucinated_quote_conf5_produces_candidate_note(
        self, tmp_path, monkeypatch
    ):
        """conf=5 + hallucinated_quote at score >= 4 → candidate Note (weak-quote path)."""
        extraction = _extraction_result(
            rejection_reason="hallucinated_quote",
            confidence=5,
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding(score=5)
        )
        assert result is not None
        assert result.category == CAT_CANDIDATE_NOTE

    def test_summary_only_quote_evidence_conf5_produces_candidate_note(
        self, tmp_path, monkeypatch
    ):
        """conf=5 + summary_only_quote_evidence at score >= 4 → candidate Note."""
        extraction = _extraction_result(
            rejection_reason="summary_only_quote_evidence",
            confidence=5,
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding(score=5)
        )
        assert result is not None
        assert result.category == CAT_CANDIDATE_NOTE

    def test_hallucinated_quote_conf0_stays_private(self, tmp_path, monkeypatch):
        """conf=0 + hallucinated_quote → private finding (below conf threshold)."""
        extraction = _extraction_result(
            rejection_reason="hallucinated_quote",
            confidence=0,
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding(score=5)
        )
        assert result is not None
        assert result.category != CAT_CANDIDATE_NOTE

    def test_weak_quote_below_detector_threshold_stays_private(
        self, tmp_path, monkeypatch
    ):
        """conf=5 + hallucinated_quote but score=3 → private (below detector gate)."""
        extraction = _extraction_result(
            rejection_reason="hallucinated_quote",
            confidence=5,
        )
        result = self._run_process_candidate(
            tmp_path, monkeypatch, extraction, _make_finding(score=3)
        )
        assert result is not None
        assert result.category != CAT_CANDIDATE_NOTE

    def test_candidate_note_idempotency(self, tmp_path, monkeypatch):
        """After a candidate Note is emitted, source key advances so it won't re-emit."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(parents=True, exist_ok=True)
        storage.ensure_graph_dir(threads_dir)

        graph_dir = storage.ensure_graph_dir(threads_dir)
        thread_dir = storage.ensure_thread_graph_dir(graph_dir, "test-thread")
        storage.atomic_write_json(
            thread_dir / "meta.json",
            {
                "id": "thread:test-thread",
                "topic": "test-thread",
                "title": "T",
                "status": "OPEN",
            },
        )
        storage.atomic_write_jsonl(thread_dir / "entries.jsonl", [])

        daemon = _make_daemon(tmp_path)
        daemon._resolved_code_root = tmp_path

        entry_dict = _make_entry_dict()
        extraction = _extraction_result(
            rejection_reason="soft_gate_failure",
            confidence=4,
            gate_overrides={"g6_temporal": {"passed": False, "reason": "unclear"}},
        )
        finding = _make_finding()
        source_key = f"{finding.topic}:{finding.entry_id}"

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.get_entry_node_from_graph",
                return_value=entry_dict,
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=extraction,
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=_write_result(),
            ),
        ):
            result = daemon._process_candidate(
                finding, threads_dir, tmp_path, MagicMock(), "2026-05-19"
            )

        assert result is not None
        assert result.category == CAT_CANDIDATE_NOTE

        # Simulate the tick loop advancing the cursor
        existing = daemon._get_processed_source_keys()
        daemon._set_processed_source_keys(existing + [source_key])

        # Verify the key is marked processed (tick loop would skip re-processing)
        assert source_key in daemon._get_processed_source_keys()


def _passing_value_laden_result(
    decision_statement: str = "We should preserve user consent before logging",
) -> ExtractionResult:
    """A gate-passing extraction whose statement is value-laden (#880)."""
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
    ext = LLMExtraction(
        gates=gates,
        confidence=5,
        decision_statement=decision_statement,
        rationale="Respect for users",
        scope="watercooler-cloud",
        alternatives_considered=None,
        verbatim_quotes=[decision_statement.lower()],
        warning=None,
    )
    return ExtractionResult(
        entry_id="01SRCENTRY0000001",
        topic="test-thread",
        passed=True,
        confidence=5,
        gate_results=gates,
        decision_body="Spec: decision-extractor\n\n## Decision\n" + decision_statement,
        rejection_reason=None,
        extraction=ext,
    )


class TestMoralDelegationRouting:
    """Unit 3 (#880) — value-laden direct-writes route to candidate Notes."""

    def _run(
        self,
        tmp_path: Path,
        monkeypatch,
        extraction: ExtractionResult,
        entry_dict: dict[str, Any],
    ) -> tuple[Finding | None, MagicMock]:
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(parents=True, exist_ok=True)
        storage.ensure_graph_dir(threads_dir)
        daemon = _make_daemon(tmp_path)
        daemon._resolved_code_root = tmp_path

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.get_entry_node_from_graph",
                return_value=entry_dict,
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=extraction,
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=_write_result(),
            ) as mock_write,
        ):
            finding = daemon._process_candidate(
                _make_finding(score=5), threads_dir, tmp_path, MagicMock(), "2026-06-04"
            )
        return finding, mock_write

    def test_value_laden_without_owner_routes_to_candidate(
        self, tmp_path, monkeypatch
    ):
        """High-confidence value-laden extraction → candidate Note, not Decision."""
        entry = _make_entry_dict(body="we should preserve user consent before logging")
        finding, mock_write = self._run(
            tmp_path, monkeypatch, _passing_value_laden_result(), entry
        )
        assert finding is not None
        assert finding.category == CAT_CANDIDATE_NOTE
        # The write that happened must be a Note (candidate), not a Decision.
        assert mock_write.call_count == 1
        assert mock_write.call_args.kwargs["entry_type"] == "Note"

    def test_value_laden_candidate_body_carries_warning(self, tmp_path, monkeypatch):
        entry = _make_entry_dict(body="we should preserve user consent before logging")
        _finding, mock_write = self._run(
            tmp_path, monkeypatch, _passing_value_laden_result(), entry
        )
        written_body = mock_write.call_args.kwargs["body"]
        assert "Moral-Delegation-Warning: true" in written_body

    def test_value_laden_with_human_owner_direct_writes_decision(
        self, tmp_path, monkeypatch
    ):
        """If the source entry already records human ownership, the value-laden
        extraction may direct-write a Decision (ownership is satisfied)."""
        entry = _make_entry_dict(body="we should preserve user consent before logging")
        entry["human_authorized_by"] = "github:octocat"
        finding, mock_write = self._run(
            tmp_path, monkeypatch, _passing_value_laden_result(), entry
        )
        assert finding is not None
        assert finding.category == CAT_SUCCESS
        kwargs = mock_write.call_args.kwargs
        assert kwargs["entry_type"] == "Decision"
        # The owned, value-laden Decision must remain auditable: ownership is
        # recorded in queryable metadata AND surfaced via the body marker, so it
        # is distinguishable from an ordinary (unowned) daemon extraction.
        assert kwargs["authority_fields"]["human_authorized_by"] == "github:octocat"
        assert kwargs["authority_fields"]["authority_basis"] == "human_endorsed"
        assert "Moral-Delegation-Warning: true" in kwargs["body"]

    def test_scrub_to_empty_source_owner_routes_to_candidate(
        self, tmp_path, monkeypatch
    ):
        """A source human_authorized_by that scrubs to empty (e.g. "<>") is NOT
        real ownership — the value-laden extraction must route to a candidate
        Note, not direct-write with a false "ownership recorded ()" claim."""
        entry = _make_entry_dict(body="we should preserve user consent before logging")
        entry["human_authorized_by"] = "<>"  # raw-truthy, scrubs to ""
        finding, mock_write = self._run(
            tmp_path, monkeypatch, _passing_value_laden_result(), entry
        )
        assert finding is not None
        assert finding.category == CAT_CANDIDATE_NOTE
        assert mock_write.call_args.kwargs["entry_type"] == "Note"

    def test_factual_extraction_direct_writes_decision(self, tmp_path, monkeypatch):
        """A non-value-laden extraction is unaffected — still direct-writes."""
        entry = _make_entry_dict(body="we decided to use FalkorDB")
        finding, mock_write = self._run(
            tmp_path,
            monkeypatch,
            _passing_value_laden_result(
                decision_statement="Use FalkorDB for the T2 graph backend"
            ),
            entry,
        )
        assert finding is not None
        assert finding.category == CAT_SUCCESS
        assert mock_write.call_args.kwargs["entry_type"] == "Decision"
