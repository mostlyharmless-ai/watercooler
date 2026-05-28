"""Tests for ExtractDecisionsDaemon — LLM-powered decision extraction."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from watercooler.baseline_graph import storage
from watercooler.config_schema import DecisionExtractorConfig
from watercooler.decision_extraction import ExtractionResult
from watercooler_mcp.daemons.daemon_write import DaemonWriteResult
from watercooler_mcp.daemons.decision_extractor import (
    CAT_CAP_REACHED,
    CAT_FAILED,
    CAT_PARSE_FAILURE,
    CAT_PUSH_FAILED,
    CAT_RATE_LIMITED,
    CAT_REJECTED,
    CAT_REJECTED_HARD_GATE,
    CAT_SUCCESS,
    ExtractDecisionsDaemon,
)
from watercooler_mcp.daemons.state import Finding

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_graph_thread(
    threads_dir: Path,
    topic: str,
    *,
    status: str = "OPEN",
    title: str = "Test Thread",
    entries: list[dict[str, Any]] | None = None,
) -> None:
    threads_dir.mkdir(parents=True, exist_ok=True)
    graph_dir = storage.ensure_graph_dir(threads_dir)
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, topic)
    meta = {
        "id": f"thread:{topic}",
        "topic": topic,
        "title": title,
        "status": status,
    }
    storage.atomic_write_json(thread_dir / "meta.json", meta)
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", entries or [])


def _make_entry(
    entry_id: str = "01ENTRY",
    body: str = "We decided to use PostgreSQL for session storage.",
    **overrides: Any,
) -> dict[str, Any]:
    entry = {
        "id": f"entry:{entry_id}",
        "entry_id": entry_id,
        "agent": "Claude (jay)",
        "timestamp": "2025-01-15T12:00:00Z",
        "role": "implementer",
        "entry_type": "Note",
        "title": "Storage decision",
        "summary": "Decision about storage",
        "body": body,
        "index": 0,
    }
    entry.update(overrides)
    return entry


def _make_detector_finding(
    entry_id: str = "01ENTRY",
    topic: str = "test-topic",
    score: int = 5,
    **overrides: Any,
) -> Finding:
    from ulid import ULID

    return Finding(
        finding_id=overrides.pop("finding_id", str(ULID())),
        daemon_name="decision_detector",
        severity="info",
        category="decision_candidate",
        topic=topic,
        entry_id=entry_id,
        message=f"Decision candidate (score={score})",
        details={
            "score": score,
            "tier": "High" if score >= 4 else "Medium",
            "signals": [],
            "matched_phrases": [],
        },
        created_at=overrides.pop("created_at", time.time()),
    )


def _llm_response_pass(quotes: list[str] | None = None) -> str:
    """Valid LLM response that passes all gates."""
    return json.dumps(
        {
            "gates": {
                f"g{i}_{name}": {"passed": True, "reason": "ok"}
                for i, name in enumerate(
                    [
                        "commitment",
                        "not_superseded",
                        "quotable",
                        "rationale",
                        "scope",
                        "temporal",
                        "authority",
                        "self_contained",
                    ],
                    start=1,
                )
            },
            "confidence": 4,
            "decision_statement": "Use PostgreSQL for session storage",
            "rationale": "Better performance",
            "scope": "Storage subsystem",
            "alternatives_considered": None,
            "verbatim_quotes": quotes or ["We decided to use PostgreSQL"],
            "warning": None,
        }
    )


def _llm_response_reject() -> str:
    """LLM response with low confidence."""
    gates = {
        f"g{i}_{name}": {
            "passed": i != 1,
            "reason": "ok" if i != 1 else "No commitment",
        }
        for i, name in enumerate(
            [
                "commitment",
                "not_superseded",
                "quotable",
                "rationale",
                "scope",
                "temporal",
                "authority",
                "self_contained",
            ],
            start=1,
        )
    }
    return json.dumps(
        {
            "gates": gates,
            "confidence": 1,
            "decision_statement": None,
            "rationale": None,
            "scope": None,
            "alternatives_considered": None,
            "verbatim_quotes": [],
            "warning": "Not a decision",
        }
    )


def _make_daemon(
    tmp_path: Path,
    threads_dir: Path | None = None,
    llm_client: DaemonLLMClient | None = None,
    **config_overrides: Any,
) -> ExtractDecisionsDaemon:
    cfg = DecisionExtractorConfig(**config_overrides)
    from watercooler_mcp.daemons.llm_client import DaemonLLMClient

    return ExtractDecisionsDaemon(
        config=cfg,
        threads_dir=threads_dir or tmp_path / "threads",
        llm_client=llm_client,
    )


class MockLLMClient:
    """Test LLM client that returns canned responses."""

    def __init__(self, response: str | None = None, available: bool = True):
        self._response = response
        self._available = available
        self.calls: list[tuple[str, str]] = []

    def is_available(self) -> bool:
        return self._available

    def complete(self, prompt: str, system: str = "", **kwargs) -> Optional[str]:
        self.calls.append((system, prompt))
        return self._response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExtractDecisionsDaemon:
    def test_creation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, llm_client=MockLLMClient())
        assert daemon.name == "decision_extractor"
        assert daemon.enabled is True

    def test_tick_empty_findings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, llm_client=MockLLMClient()
        )
        daemon._resolved_code_root = tmp_path

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[],
        ):
            findings = daemon.tick()
        assert findings == []

    def test_tick_extracts_high_score_candidate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        detector_finding = _make_detector_finding(score=5)

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[detector_finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=DaemonWriteResult(
                    entry_id="01WRITTEN", written=True, pushed=True
                ),
            ),
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_SUCCESS
        assert findings[0].details["confidence"] == 4

    def test_tick_fails_closed_without_code_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, llm_client=MockLLMClient()
        )
        daemon._resolved_code_root = None  # No code_root

        findings = daemon.tick()
        assert findings == []

    def test_tick_rejects_gate_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(response=_llm_response_reject())
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[_make_detector_finding()],
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        # g1_commitment failure is a hard-fail gate → CAT_REJECTED_HARD_GATE
        assert findings[0].category == CAT_REJECTED_HARD_GATE

    def test_tick_rejects_low_confidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        # Low confidence but all gates pass
        response = json.dumps(
            {
                "gates": {
                    f"g{i}_{name}": {"passed": True, "reason": "ok"}
                    for i, name in enumerate(
                        [
                            "commitment",
                            "not_superseded",
                            "quotable",
                            "rationale",
                            "scope",
                            "temporal",
                            "authority",
                            "self_contained",
                        ],
                        start=1,
                    )
                },
                "confidence": 2,
                "decision_statement": "Maybe X",
                "rationale": "Unclear",
                "scope": "Unknown",
                "alternatives_considered": None,
                "verbatim_quotes": ["We decided to use PostgreSQL"],
                "warning": None,
            }
        )
        llm = MockLLMClient(response=response)
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[_make_detector_finding()],
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_REJECTED
        assert "low_confidence" in findings[0].details.get("rejection_reason", "")

    def test_tick_rejects_hallucinated_quote(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry(body="We decided to use PostgreSQL.")
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        # Quote doesn't match source
        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use MySQL"])
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[_make_detector_finding()],
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_REJECTED
        assert "hallucinated_quote" in findings[0].details.get("rejection_reason", "")

    def test_tick_handles_llm_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=MockLLMClient(available=False),
        )
        findings = daemon.tick()
        assert findings == []

    def test_tick_handles_llm_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(response=None)  # LLM returns None
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[_make_detector_finding()],
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_FAILED
        # LLM timeout should NOT be marked processed (transient)

    def test_tick_handles_llm_parse_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(response="not valid json {{{")
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[_make_detector_finding()],
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_PARSE_FAILURE

    def test_tick_handles_prewrite_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[_make_detector_finding()],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=DaemonWriteResult(
                    entry_id="01X", written=False, pushed=False, error="Lock failed"
                ),
            ),
        ):
            findings = daemon.tick()

        # Write failed — finding should be CAT_FAILED (not marked processed)
        assert len(findings) == 1
        assert findings[0].category == CAT_FAILED

    def test_tick_handles_push_failure_without_retry_duplication(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        detector_finding = _make_detector_finding()

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[detector_finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=DaemonWriteResult(
                    entry_id="01X", written=True, pushed=False, error="Push timeout"
                ),
            ),
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_PUSH_FAILED
        # Should be marked processed (in cursor) to avoid duplicate writes
        assert detector_finding.finding_id in daemon._get_processed_ids()

    def test_tick_progressive_cursor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        detector_finding = _make_detector_finding(finding_id="F001")

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[detector_finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=DaemonWriteResult(
                    entry_id="01W", written=True, pushed=True
                ),
            ),
        ):
            findings1 = daemon.tick()

        assert len(findings1) == 1
        assert "test-topic:01ENTRY" in daemon._get_processed_source_keys()

        # Second tick: same finding should be skipped
        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[detector_finding],
        ):
            findings2 = daemon.tick()

        assert findings2 == []

    def test_tick_cursor_gc(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, llm_client=MockLLMClient()
        )
        daemon._resolved_code_root = tmp_path

        # Pre-populate cursor with stale IDs
        daemon._set_processed_ids(["stale_1", "stale_2", "live_1"])
        daemon._set_processed_source_keys(["stale-topic:01STALE", "test-topic:01ENTRY"])
        daemon._ticks_since_gc = 23  # Will trigger GC next tick

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            side_effect=[
                [_make_detector_finding(finding_id="live_1")],
                [_make_detector_finding(finding_id="live_1")],
            ],
        ):
            daemon.tick()

        # Stale IDs should be pruned
        ids = daemon._get_processed_ids()
        assert "stale_1" not in ids
        assert "stale_2" not in ids
        assert "live_1" in ids
        source_keys = daemon._get_processed_source_keys()
        assert "stale-topic:01STALE" not in source_keys
        assert "test-topic:01ENTRY" in source_keys

    def test_threads_dir_override_resolves_code_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, llm_client=MockLLMClient()
        )

        ctx = MagicMock()
        ctx.code_root = tmp_path

        with patch(
            "watercooler_mcp.config.resolve_thread_context",
            return_value=ctx,
        ):
            resolved_threads_dir, resolved_code_root = daemon._resolve_paths()

        assert resolved_threads_dir == threads_dir
        assert resolved_code_root == tmp_path

    def test_cursor_gc_uses_all_findings_not_only_unacknowledged(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, llm_client=MockLLMClient()
        )
        daemon._resolved_code_root = tmp_path
        daemon._set_processed_ids(["F001"])
        daemon._set_processed_source_keys(["test-topic:01ENTRY"])
        daemon._ticks_since_gc = 23

        acked_finding = _make_detector_finding(
            finding_id="F001",
            acknowledged=True,
        )

        def _load_findings(*args, **kwargs):
            if kwargs.get("unacknowledged_only"):
                return []
            return [acked_finding]

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            side_effect=_load_findings,
        ):
            findings = daemon.tick()

        assert findings == []
        assert daemon._get_processed_ids() == ["F001"]
        assert daemon._get_processed_source_keys() == ["test-topic:01ENTRY"]

    def test_no_findings_tick_still_advances_gc_counter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, llm_client=MockLLMClient()
        )
        daemon._resolved_code_root = tmp_path

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[],
        ):
            daemon.tick()

        assert daemon._ticks_since_gc == 1

    def test_tick_max_candidates_per_tick(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(response=_llm_response_reject())
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=llm,
            max_candidates_per_tick=1,
        )
        daemon._resolved_code_root = tmp_path

        findings_list = [
            _make_detector_finding(finding_id=f"F{i:03d}", score=5) for i in range(5)
        ]

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=findings_list,
        ):
            findings = daemon.tick()

        # Only 1 processed due to max_candidates_per_tick
        assert daemon._last_tick_candidates == 1

    def test_tick_daily_rate_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=MockLLMClient(),
            max_extractions_per_day=2,
        )
        daemon._resolved_code_root = tmp_path

        # Pre-fill daily count to cap
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daemon._checkpoint.extras["daily_count"] = {"date": today, "count": 2}

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[_make_detector_finding()],
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_RATE_LIMITED

    def test_tick_daily_rate_limit_date_rollover(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=MockLLMClient(),
            max_extractions_per_day=5,
        )
        daemon._resolved_code_root = tmp_path

        # Yesterday's count should be ignored
        daemon._checkpoint.extras["daily_count"] = {"date": "1999-01-01", "count": 100}

        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert daemon._get_daily_count(today) == 0

    def test_tick_max_tick_duration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        # LLM that's slow enough to trigger timeout
        class SlowLLM:
            def is_available(self):
                return True

            def complete(self, prompt, system="", **kwargs):
                time.sleep(0.1)
                return _llm_response_reject()

        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=SlowLLM(),
            max_candidates_per_tick=10,
        )
        # Override max_tick_duration below validator minimum for testing
        daemon._config = DecisionExtractorConfig.model_construct(
            **{**daemon._config.model_dump(), "max_tick_duration": 0.05}
        )
        daemon._resolved_code_root = tmp_path

        findings_list = [
            _make_detector_finding(finding_id=f"F{i:03d}", score=5) for i in range(10)
        ]

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=findings_list,
        ):
            findings = daemon.tick()

        # Should have processed fewer than 10 due to duration guard
        assert daemon._last_tick_candidates < 10

    def test_tick_handles_stale_reference(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        # Thread exists but entry doesn't
        _write_graph_thread(threads_dir, "test-topic", entries=[])

        llm = MockLLMClient()
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        # Finding references a non-existent entry
        detector_finding = _make_detector_finding(entry_id="DELETED_ENTRY")

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[detector_finding],
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_REJECTED
        assert "stale_reference" in findings[0].details.get("rejection_reason", "")

    def test_tick_sorts_by_score_desc(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry_low = _make_entry(entry_id="LOW", body="We decided to use X.")
        entry_high = _make_entry(entry_id="HIGH", body="We decided to use PostgreSQL.")
        _write_graph_thread(threads_dir, "test-topic", entries=[entry_low, entry_high])

        processed_entries = []

        class TrackingLLM:
            def is_available(self):
                return True

            def complete(self, prompt, system="", **kwargs):
                # Track which entry was processed
                if "HIGH" in prompt:
                    processed_entries.append("HIGH")
                elif "LOW" in prompt:
                    processed_entries.append("LOW")
                return _llm_response_reject()

        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=TrackingLLM(),
            max_candidates_per_tick=2,
            min_extraction_score=4,
        )
        daemon._resolved_code_root = tmp_path

        f_high = _make_detector_finding(entry_id="HIGH", score=8)
        f_low = _make_detector_finding(entry_id="LOW", score=4)

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[f_low, f_high],  # Low first — should be reordered
        ):
            daemon.tick()

        # HIGH score should be processed first
        assert processed_entries[0] == "HIGH"

    def test_tick_per_candidate_isolation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        call_count = [0]

        class FailOnceLLM:
            def is_available(self):
                return True

            def complete(self, prompt, system="", **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("Simulated LLM error")
                return _llm_response_reject()

        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=FailOnceLLM(),
            max_candidates_per_tick=2,
        )
        daemon._resolved_code_root = tmp_path

        f1 = _make_detector_finding(finding_id="F1", entry_id="01ENTRY")
        f2 = _make_detector_finding(finding_id="F2", entry_id="01ENTRY")

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[f1, f2],
        ):
            findings = daemon.tick()

        # Both should produce findings (error in first doesn't block second)
        assert len(findings) == 2

    def test_tick_missing_threads_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(
            tmp_path,
            threads_dir=tmp_path / "nonexistent",
            llm_client=MockLLMClient(),
        )
        findings = daemon.tick()
        assert findings == []

    def test_tick_thread_context_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(response=_llm_response_reject())
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=llm,
            max_candidates_per_tick=2,
        )
        daemon._resolved_code_root = tmp_path

        # Two findings for same topic
        f1 = _make_detector_finding(finding_id="F1")
        f2 = _make_detector_finding(finding_id="F2")

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[f1, f2],
            ),
            patch.object(
                daemon, "_load_thread_context", wraps=daemon._load_thread_context
            ) as mock_load,
        ):
            daemon.tick()

        # _load_thread_context should be called twice but cache should prevent
        # redundant graph reads. The mock wraps the real method, so we verify
        # the cache dict has an entry.
        assert "test-topic" in daemon._thread_context_cache

    def test_config_defaults(self):
        cfg = DecisionExtractorConfig()
        assert cfg.enabled is False
        assert cfg.interval == 1800.0
        assert cfg.min_extraction_score == 4
        assert cfg.max_candidates_per_tick == 3
        assert cfg.max_extractions_per_day == 20
        assert cfg.max_body_chars == 4000
        assert cfg.min_confidence == 4
        assert cfg.max_tick_duration == 300.0
        assert cfg.llm is None

    def test_status_summary_custom_metrics(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, llm_client=MockLLMClient())
        daemon._last_tick_candidates = 5
        daemon._last_tick_extracted = 2
        daemon._last_tick_rejected = 3

        summary = daemon.status_summary()
        assert summary["last_tick_candidates_evaluated"] == 5
        assert summary["last_tick_extracted"] == 2
        assert summary["last_tick_rejected"] == 3
        assert "daily_extractions_count" in summary
        assert "daily_extractions_remaining" in summary
        assert "processed_ids_count" in summary

    def test_status_summary_includes_cursor_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, llm_client=MockLLMClient())
        daemon._set_processed_ids(["a", "b", "c"])
        summary = daemon.status_summary()
        assert summary["processed_ids_count"] == 3

    def test_agent_identity_in_written_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        write_calls = []

        def mock_write(**kwargs):
            write_calls.append(kwargs)
            return DaemonWriteResult(entry_id="01W", written=True, pushed=True)

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[_make_detector_finding()],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                side_effect=lambda topic, **kw: mock_write(topic=topic, **kw),
            ),
        ):
            daemon.tick()

        assert len(write_calls) == 1
        call = write_calls[0]
        assert call["agent"] == "ExtractDecisionsDaemon"
        assert call["role"] == "scribe"
        assert call["entry_type"] == "Decision"
        assert call["user_tag"] == "system"
        assert call["agent_spec"] == "decision-extractor"

    def test_provenance_marker_in_body(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        written_bodies = []

        def mock_write(**kwargs):
            written_bodies.append(kwargs.get("body", ""))
            return DaemonWriteResult(entry_id="01W", written=True, pushed=True)

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[_make_detector_finding()],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                side_effect=lambda topic, **kw: mock_write(topic=topic, **kw),
            ),
        ):
            daemon.tick()

        assert len(written_bodies) == 1
        assert "[automated: decision_extractor]" in written_bodies[0]
        assert "Spec: decision-extractor" in written_bodies[0]

    def test_metric_invariant(self, tmp_path, monkeypatch):
        """evaluated == extracted + rejected + failed + rate_limited."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(response=_llm_response_reject())
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=llm,
            max_candidates_per_tick=5,
        )
        daemon._resolved_code_root = tmp_path

        findings_list = [
            _make_detector_finding(finding_id=f"F{i:03d}", score=5) for i in range(3)
        ]

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=findings_list,
        ):
            daemon.tick()

        total = (
            daemon._last_tick_extracted
            + daemon._last_tick_push_failed
            + daemon._last_tick_rejected
            + daemon._last_tick_failed
            + daemon._last_tick_rate_limited
        )
        assert daemon._last_tick_candidates == total

    # ------------------------------------------------------------------
    # Fix #1: Daily count not incremented on write failure
    # ------------------------------------------------------------------

    def test_daily_count_not_incremented_on_write_failure(self, tmp_path, monkeypatch):
        """If daemon_write_entry fails (written=False), daily count stays at 0."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        detector_finding = _make_detector_finding(score=5)

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[detector_finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=DaemonWriteResult(
                    entry_id=None, written=False, pushed=False
                ),
            ),
        ):
            daemon.tick()

        assert daemon._get_daily_count("2026-04-01") == 0

    def test_daily_count_incremented_on_push_failure(self, tmp_path, monkeypatch):
        """If write succeeds but push fails, daily count IS incremented."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        detector_finding = _make_detector_finding(score=5)

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[detector_finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=DaemonWriteResult(
                    entry_id="01W", written=True, pushed=False
                ),
            ),
        ):
            daemon.tick()

        # Written locally → daily count incremented even though push failed
        daily = daemon._checkpoint.extras.get("daily_count", {})
        assert daily.get("count", 0) == 1

    # ------------------------------------------------------------------
    # Fix #4: load_findings limit matches _DEDUP_LIMIT pattern
    # ------------------------------------------------------------------

    def test_load_findings_uses_high_limit(self, tmp_path, monkeypatch):
        """load_findings should be called with limit=50_000, not 2000."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, llm_client=MockLLMClient()
        )
        daemon._resolved_code_root = tmp_path

        load_findings_mock = MagicMock(return_value=[])

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            load_findings_mock,
        ):
            daemon.tick()

        load_findings_mock.assert_called_once()
        call_kwargs = load_findings_mock.call_args
        assert (
            call_kwargs[1].get(
                "limit", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
            )
            == 50_000
        )

    # ------------------------------------------------------------------
    # Fix #5: Inverted default in failed-gate reporting
    # ------------------------------------------------------------------

    def test_missing_passed_key_in_failed_gates(self, tmp_path, monkeypatch):
        """A gate result missing 'passed' key should default to False (failed)."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        # LLM response with low confidence → rejection finding
        llm = MockLLMClient(response=_llm_response_reject())
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, llm_client=llm)
        daemon._resolved_code_root = tmp_path

        detector_finding = _make_detector_finding(score=5)

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            return_value=[detector_finding],
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        # g1_commitment failure is a hard-fail gate → CAT_REJECTED_HARD_GATE
        assert findings[0].category == CAT_REJECTED_HARD_GATE
        # The failed_gates list should include g1_commitment (which was set to failed)
        failed = findings[0].details.get("failed_gates", [])
        assert "g1_commitment" in failed

    def test_empty_decision_body_emits_failed_finding(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entry = _make_entry()
        _write_graph_thread(threads_dir, "test-topic", entries=[entry])

        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, llm_client=MockLLMClient()
        )
        daemon._resolved_code_root = tmp_path
        detector_finding = _make_detector_finding(score=5)

        empty_body_result = ExtractionResult(
            entry_id="01ENTRY",
            topic="test-topic",
            passed=True,
            confidence=4,
            gate_results={},
            decision_body=None,
            rejection_reason=None,
            extraction=None,
        )

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[detector_finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=empty_body_result,
            ),
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_FAILED
        assert findings[0].details["error_type"] == "empty_decision_body"


# ---------------------------------------------------------------------------
# P1.3: per-entry retry caps
# ---------------------------------------------------------------------------


def _empty_body_result() -> ExtractionResult:
    return ExtractionResult(
        entry_id="01ENTRY",
        topic="test-topic",
        passed=True,
        confidence=4,
        gate_results={},
        decision_body=None,
        rejection_reason=None,
        extraction=None,
    )


class TestRetryCaps:
    """P1.3: cap retries on LLM-caused CAT_FAILED sub-cases while leaving
    infrastructure write failures a longer budget."""

    def _setup(self, tmp_path, monkeypatch, **cfg_kwargs):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(threads_dir, "test-topic", entries=[_make_entry()])
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=MockLLMClient(),
            **cfg_kwargs,
        )
        daemon._resolved_code_root = tmp_path
        return daemon

    def test_llm_attempts_counter_increments_on_empty_body(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Each empty_decision_body outcome bumps llm_extraction_attempts."""
        daemon = self._setup(tmp_path, monkeypatch)
        finding = _make_detector_finding()
        source_key = "test-topic:01ENTRY"

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=_empty_body_result(),
            ),
        ):
            findings1 = daemon.tick()
            findings2 = daemon.tick()

        assert findings1[0].category == CAT_FAILED
        assert findings2[0].category == CAT_FAILED
        # Cursor should NOT have advanced — entry is retryable until cap
        assert finding.finding_id not in daemon._get_processed_ids()
        assert source_key not in daemon._get_processed_source_keys()
        # But the counter should have incremented twice
        assert daemon._get_llm_attempts().get(source_key) == 2

    def test_llm_cap_emits_cap_reached_and_advances_cursor(
        self,
        tmp_path,
        monkeypatch,
    ):
        """After max_extraction_attempts empty_body failures, the next tick
        emits extraction_cap_reached (llm_failure) and advances cursor."""
        daemon = self._setup(
            tmp_path,
            monkeypatch,
            max_extraction_attempts=3,
        )
        finding = _make_detector_finding()
        source_key = "test-topic:01ENTRY"

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=_empty_body_result(),
            ) as extract_mock,
        ):
            # Ticks 1-3 exhaust the budget
            for _ in range(3):
                daemon.tick()
            assert extract_mock.call_count == 3
            # Tick 4 should short-circuit: no LLM call, cap_reached emitted
            findings4 = daemon.tick()
            assert extract_mock.call_count == 3  # unchanged

        assert len(findings4) == 1
        f = findings4[0]
        assert f.category == CAT_CAP_REACHED
        assert f.details["reason"] == "llm_failure"
        assert f.details["attempts"] == 3
        assert f.details["cap"] == 3
        # Cursor advanced
        assert finding.finding_id in daemon._get_processed_ids()
        assert source_key in daemon._get_processed_source_keys()

    def test_llm_unavailable_counts_toward_llm_cap(self, tmp_path, monkeypatch):
        """llm_unavailable failures count toward the same LLM attempts cap."""
        daemon = self._setup(
            tmp_path,
            monkeypatch,
            max_extraction_attempts=2,
        )
        finding = _make_detector_finding()
        source_key = "test-topic:01ENTRY"

        # ExtractionResult with rejection_reason=llm_unavailable
        llm_unavailable = ExtractionResult(
            entry_id="01ENTRY",
            topic="test-topic",
            passed=False,
            confidence=0,
            gate_results={},
            decision_body=None,
            rejection_reason="llm_unavailable",
            extraction=None,
        )
        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=llm_unavailable,
            ),
        ):
            daemon.tick()
            daemon.tick()
            findings3 = daemon.tick()

        assert findings3[0].category == CAT_CAP_REACHED
        assert findings3[0].details["reason"] == "llm_failure"
        assert daemon._get_llm_attempts().get(source_key) == 2

    def test_write_failure_counter_does_not_advance_cursor_before_cap(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Write failures increment write_failure_attempts but do NOT
        advance the cursor until the cap is reached (infra-transient)."""
        daemon = self._setup(
            tmp_path,
            monkeypatch,
            max_write_failure_attempts=5,
        )
        finding = _make_detector_finding()
        source_key = "test-topic:01ENTRY"

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon._llm_client = llm

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=DaemonWriteResult(
                    entry_id="01X",
                    written=False,
                    pushed=False,
                    error="disk full",
                ),
            ),
        ):
            # Two write-failure ticks — still under cap
            daemon.tick()
            daemon.tick()

        assert daemon._get_write_attempts().get(source_key) == 2
        # Cursor must NOT be advanced — infra is transient
        assert finding.finding_id not in daemon._get_processed_ids()
        assert source_key not in daemon._get_processed_source_keys()

    def test_write_failure_cap_emits_cap_reached_and_advances_cursor(
        self,
        tmp_path,
        monkeypatch,
    ):
        """After max_write_failure_attempts failures, the next tick emits
        extraction_cap_reached (write_failure) and advances cursor."""
        daemon = self._setup(
            tmp_path,
            monkeypatch,
            max_write_failure_attempts=5,
        )
        finding = _make_detector_finding()
        source_key = "test-topic:01ENTRY"

        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon._llm_client = llm

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=DaemonWriteResult(
                    entry_id="01X",
                    written=False,
                    pushed=False,
                    error="disk full",
                ),
            ) as write_mock,
        ):
            for _ in range(5):
                daemon.tick()
            assert write_mock.call_count == 5
            findings6 = daemon.tick()
            # Short-circuited — write not attempted a 6th time
            assert write_mock.call_count == 5

        assert len(findings6) == 1
        f = findings6[0]
        assert f.category == CAT_CAP_REACHED
        assert f.details["reason"] == "write_failure"
        assert f.details["attempts"] == 5
        assert f.details["cap"] == 5
        assert finding.finding_id in daemon._get_processed_ids()
        assert source_key in daemon._get_processed_source_keys()

    def test_llm_cap_fires_before_write_cap_when_mixed(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Mixed failures: LLM cap is reached first with its lower budget."""
        daemon = self._setup(
            tmp_path,
            monkeypatch,
            max_extraction_attempts=2,
            max_write_failure_attempts=5,
        )
        finding = _make_detector_finding()

        # First tick: write_failure outcome (success → write fails)
        llm = MockLLMClient(
            response=_llm_response_pass(quotes=["We decided to use PostgreSQL"])
        )
        daemon._llm_client = llm
        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.daemon_write_entry",
                return_value=DaemonWriteResult(
                    entry_id="01X",
                    written=False,
                    pushed=False,
                    error="disk full",
                ),
            ),
        ):
            daemon.tick()  # write_failure_attempts=1

        # Next: two LLM failures → hits LLM cap of 2
        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.extract_decision",
                return_value=_empty_body_result(),
            ),
        ):
            daemon.tick()  # llm=1
            daemon.tick()  # llm=2
            findings4 = daemon.tick()  # cap_reached, llm_failure

        assert findings4[0].category == CAT_CAP_REACHED
        assert findings4[0].details["reason"] == "llm_failure"

    def test_gc_prunes_both_attempt_counters(self, tmp_path, monkeypatch):
        """The cursor GC pass prunes llm_extraction_attempts and
        write_failure_attempts alongside processed_source_keys."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            llm_client=MockLLMClient(),
        )
        daemon._resolved_code_root = tmp_path

        # Pre-populate counters with stale + live keys
        daemon._checkpoint.extras["llm_extraction_attempts"] = {
            "stale-topic:01STALE": 2,
            "test-topic:01ENTRY": 1,
        }
        daemon._checkpoint.extras["write_failure_attempts"] = {
            "stale-topic:01GONE": 3,
            "test-topic:01ENTRY": 1,
        }
        daemon._ticks_since_gc = 23  # trigger GC next tick

        with patch(
            "watercooler_mcp.daemons.decision_extractor.load_findings",
            side_effect=[
                [_make_detector_finding(finding_id="live_1")],
                [_make_detector_finding(finding_id="live_1")],
            ],
        ):
            daemon.tick()

        llm_counts = daemon._get_llm_attempts()
        assert "stale-topic:01STALE" not in llm_counts
        assert "test-topic:01ENTRY" in llm_counts

        write_counts = daemon._get_write_attempts()
        assert "stale-topic:01GONE" not in write_counts
        assert "test-topic:01ENTRY" in write_counts

    def test_unknown_error_type_does_not_increment_counters(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Exception-path CAT_FAILED (unknown error_type) should not bump
        either counter — current behavior is unchanged retry."""
        daemon = self._setup(tmp_path, monkeypatch)
        finding = _make_detector_finding()

        with (
            patch(
                "watercooler_mcp.daemons.decision_extractor.load_findings",
                return_value=[finding],
            ),
            patch(
                "watercooler_mcp.daemons.decision_extractor.ExtractDecisionsDaemon._process_candidate",
                side_effect=RuntimeError("boom"),
            ),
        ):
            findings = daemon.tick()

        assert len(findings) == 1
        assert findings[0].category == CAT_FAILED
        # Neither counter touched
        assert daemon._get_llm_attempts() == {}
        assert daemon._get_write_attempts() == {}

    def test_status_summary_exposes_attempt_counters(
        self,
        tmp_path,
        monkeypatch,
    ):
        daemon = self._setup(tmp_path, monkeypatch)
        daemon._checkpoint.extras["llm_extraction_attempts"] = {"a:1": 2}
        daemon._checkpoint.extras["write_failure_attempts"] = {"b:2": 1, "c:3": 3}
        summary = daemon.status_summary()
        assert summary["llm_extraction_attempts_count"] == 1
        assert summary["write_failure_attempts_count"] == 2
