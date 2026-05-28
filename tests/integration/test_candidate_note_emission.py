"""Integration tests for Phase 1b candidate-Note emission path.

Covers the full daemon tick loop (not just _process_candidate) for the
candidate-Note path:
- Tick 1: soft-gate failure with score >= min_extraction_score → CAT_CANDIDATE_NOTE
- Tick 2: processed-source cursor blocks re-emission (idempotency)
- Rate cap: >N emissions for the same thread within the week window → CAT_REJECTED_RATE_CAP
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from watercooler.baseline_graph import storage
from watercooler.config_schema import DecisionExtractorConfig
from watercooler.decision_extraction import ExtractionResult, LLMExtraction
from watercooler_mcp.daemons.daemon_write import DaemonWriteResult
from watercooler_mcp.daemons.decision_extractor import (
    CAT_CANDIDATE_NOTE,
    CAT_REJECTED_RATE_CAP,
    ExtractDecisionsDaemon,
)
from watercooler_mcp.daemons.state import Finding, append_findings

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

TOPIC = "integration-test-thread"
ENTRY_ID = "01INTEGENTRY00001"


@pytest.fixture()
def daemons_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "daemons"
    monkeypatch.setattr("watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", d)
    return d


@pytest.fixture()
def threads_dir(tmp_path: Path) -> Path:
    td = tmp_path / "threads"
    td.mkdir(parents=True)
    graph_dir = storage.ensure_graph_dir(td)
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, TOPIC)
    storage.atomic_write_json(
        thread_dir / "meta.json",
        {
            "id": f"thread:{TOPIC}",
            "topic": TOPIC,
            "title": "Integration test thread",
            "status": "OPEN",
        },
    )
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", [])
    return td


def _make_daemon(tmp_path: Path, **overrides: Any) -> ExtractDecisionsDaemon:
    cfg = DecisionExtractorConfig(**overrides)
    d = ExtractDecisionsDaemon(config=cfg, threads_dir=tmp_path / "threads", llm_client=None)
    d._resolved_code_root = tmp_path
    return d


def _detector_finding(entry_id: str = ENTRY_ID, score: int = 5) -> Finding:
    from ulid import ULID

    return Finding(
        finding_id=str(ULID()),
        daemon_name="decision_detector",
        severity="info",
        category="decision_candidate",
        topic=TOPIC,
        entry_id=entry_id,
        message=f"Decision candidate (score={score})",
        details={"score": score, "tier": "High"},
        created_at=time.time(),
    )


def _soft_fail_extraction(entry_id: str = ENTRY_ID) -> ExtractionResult:
    gates = {
        "g1_commitment": {"passed": True, "reason": "ok"},
        "g2_not_superseded": {"passed": True, "reason": "ok"},
        "g3_quotable": {"passed": True, "reason": "ok"},
        "g4_rationale": {"passed": True, "reason": "ok"},
        "g5_scope": {"passed": True, "reason": "ok"},
        "g6_temporal": {"passed": False, "reason": "timing unclear"},
        "g7_authority": {"passed": True, "reason": "ok"},
        "g8_self_contained": {"passed": True, "reason": "ok"},
    }
    ext = LLMExtraction(
        gates=gates,
        confidence=4,
        decision_statement="Use FalkorDB for graph storage",
        rationale="Performance characteristics",
        scope="watercooler-cloud",
        alternatives_considered=None,
        verbatim_quotes=["we decided to use FalkorDB"],
        warning=None,
    )
    return ExtractionResult(
        entry_id=entry_id,
        topic=TOPIC,
        passed=False,
        confidence=4,
        gate_results=gates,
        decision_body=None,
        rejection_reason="soft_gate_failure",
        extraction=ext,
    )


def _entry_dict(entry_id: str = ENTRY_ID) -> dict[str, Any]:
    return {
        "id": f"entry:{entry_id}",
        "entry_id": entry_id,
        "agent": "Claude (jay)",
        "timestamp": "2026-05-19T12:00:00Z",
        "role": "planner",
        "entry_type": "Note",
        "title": "Storage decision",
        "summary": "Decision about graph storage",
        "body": "we decided to use FalkorDB for graph storage",
        "index": 1,
        "thread_topic": TOPIC,
    }


def _write_result() -> DaemonWriteResult:
    from ulid import ULID

    return DaemonWriteResult(written=True, pushed=True, entry_id=str(ULID()), error=None)


def _mock_llm() -> MagicMock:
    llm = MagicMock()
    llm.is_available.return_value = True
    return llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCandidateNoteEmissionIntegration:
    def _run_tick(
        self,
        tmp_path: Path,
        daemon: ExtractDecisionsDaemon,
        extraction: ExtractionResult,
    ) -> list[Finding]:
        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.get_entry_node_from_graph",
                return_value=_entry_dict(),
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=extraction,
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=_write_result(),
            ),
            patch.object(daemon, "_get_llm_client", return_value=_mock_llm()),
            patch.object(
                daemon,
                "_resolve_paths",
                return_value=(tmp_path / "threads", tmp_path),
            ),
        ):
            return daemon.tick()

    def test_tick_emits_candidate_note_on_soft_gate_failure(
        self, tmp_path: Path, daemons_dir: Path, threads_dir: Path
    ) -> None:
        """Soft-gate failure with high detector score → CAT_CANDIDATE_NOTE finding."""
        daemon = _make_daemon(tmp_path)
        finding = _detector_finding(score=5)
        append_findings("decision_detector", [finding], namespace=daemon.state_namespace)

        results = self._run_tick(tmp_path, daemon, _soft_fail_extraction())

        candidate_notes = [f for f in results if f.category == CAT_CANDIDATE_NOTE]
        assert len(candidate_notes) == 1
        cn = candidate_notes[0]
        assert cn.topic == TOPIC
        assert cn.entry_id == ENTRY_ID

    def test_tick_is_idempotent_second_tick_skips(
        self, tmp_path: Path, daemons_dir: Path, threads_dir: Path
    ) -> None:
        """Second tick with same source entry → no re-emission (cursor advanced)."""
        daemon = _make_daemon(tmp_path)
        finding = _detector_finding(score=5)
        append_findings("decision_detector", [finding], namespace=daemon.state_namespace)

        # Tick 1: should emit
        results1 = self._run_tick(tmp_path, daemon, _soft_fail_extraction())
        assert any(f.category == CAT_CANDIDATE_NOTE for f in results1)

        # Re-seed same finding (simulating unacknowledged state persisting)
        append_findings("decision_detector", [finding], namespace=daemon.state_namespace)

        # Tick 2: processed-source cursor blocks re-emission
        results2 = self._run_tick(tmp_path, daemon, _soft_fail_extraction())
        assert not any(f.category == CAT_CANDIDATE_NOTE for f in results2)

    def test_rate_cap_blocks_excess_emissions(
        self, tmp_path: Path, daemons_dir: Path, threads_dir: Path
    ) -> None:
        """After rate cap is exhausted, subsequent entries → CAT_REJECTED_RATE_CAP."""
        daemon = _make_daemon(tmp_path, candidate_note_rate_cap_per_thread_per_week=2)

        # Emit 2 distinct entries (fills the cap)
        for i in range(3):
            eid = f"01INTEGENTRY0000{i}"
            finding = _detector_finding(entry_id=eid, score=5)
            append_findings("decision_detector", [finding], namespace=daemon.state_namespace)
            results = self._run_tick(tmp_path, daemon, _soft_fail_extraction(entry_id=eid))
            if i < 2:
                assert any(f.category == CAT_CANDIDATE_NOTE for f in results), f"tick {i} should emit"
            else:
                assert any(f.category == CAT_REJECTED_RATE_CAP for f in results), "tick 2 should be rate-capped"

    def test_candidate_note_body_has_required_fields(
        self, tmp_path: Path, daemons_dir: Path, threads_dir: Path
    ) -> None:
        """Verify the written Note body carries required metadata fields."""
        daemon = _make_daemon(tmp_path)
        finding = _detector_finding(score=5)
        append_findings("decision_detector", [finding], namespace=daemon.state_namespace)

        written_bodies: list[str] = []

        def capture_write(topic: str, **kwargs: Any) -> DaemonWriteResult:
            written_bodies.append(kwargs.get("body", ""))
            return _write_result()

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.get_entry_node_from_graph",
                return_value=_entry_dict(),
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=_soft_fail_extraction(),
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                side_effect=capture_write,
            ),
            patch.object(daemon, "_get_llm_client", return_value=_mock_llm()),
            patch.object(
                daemon,
                "_resolve_paths",
                return_value=(tmp_path / "threads", tmp_path),
            ),
        ):
            daemon.tick()

        assert written_bodies, "daemon_write_entry should have been called"
        body = written_bodies[0]
        assert "Spec: decision-extractor" in body
        assert "Candidate-Type: Decision" in body
        assert "Candidate-Status: needs_human_confirmation" in body
        assert "Failed-Gates:" in body
        assert "g6_temporal" in body
