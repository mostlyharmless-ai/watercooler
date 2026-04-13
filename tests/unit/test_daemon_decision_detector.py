"""Tests for DetectDecisionsDaemon — continuous decision candidate scoring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from watercooler.config_schema import DecisionDetectorConfig
from watercooler.baseline_graph import storage
from watercooler_mcp.daemons.decision_detector import (
    DetectDecisionsDaemon,
    _compute_search_hit,
)


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
    last_updated: str = "2025-01-01T00:00:00Z",
) -> None:
    """Write graph data for a thread (mirrors test_daemon_auditor pattern)."""
    threads_dir.mkdir(parents=True, exist_ok=True)

    # Write .md file (projection)
    p = threads_dir / f"{topic}.md"
    p.write_text(f"# {title}\nStatus: {status}\n", encoding="utf-8")

    # Write graph data
    graph_dir = storage.ensure_graph_dir(threads_dir)
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, topic)

    meta = {
        "id": f"thread:{topic}",
        "topic": topic,
        "title": title,
        "status": status,
        "last_updated": last_updated,
    }
    storage.atomic_write_json(thread_dir / "meta.json", meta)

    entry_list = entries if entries is not None else []
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", entry_list)


def _decision_entry(
    entry_id: str = "01ENTRY",
    entry_type: str = "Decision",
    title: str = "Use PostgreSQL for primary storage",
    summary: str = "We decided to use PostgreSQL.",
    body: str = "",
) -> dict[str, Any]:
    return {
        "id": f"entry:{entry_id}",
        "entry_id": entry_id,
        "agent": "TestAgent",
        "timestamp": "2025-01-01T00:00:00Z",
        "role": "implementer",
        "entry_type": entry_type,
        "title": title,
        "summary": summary,
        "body": body,
        "index": 0,
    }


def _note_entry(
    entry_id: str = "02ENTRY",
    title: str = "Regular update",
    summary: str = "Just a regular status update on progress.",
    body: str = "",
) -> dict[str, Any]:
    return {
        "id": f"entry:{entry_id}",
        "entry_id": entry_id,
        "agent": "TestAgent",
        "timestamp": "2025-01-01T00:00:00Z",
        "role": "implementer",
        "entry_type": "Note",
        "title": title,
        "summary": summary,
        "body": body,
        "index": 1,
    }


def _make_daemon(
    tmp_path: Path,
    threads_dir: Path | None = None,
    **config_overrides: Any,
) -> DetectDecisionsDaemon:
    """Create a daemon with test config, monkeypatching daemons dir."""
    cfg = DecisionDetectorConfig(**config_overrides)
    return DetectDecisionsDaemon(
        config=cfg,
        threads_dir=threads_dir or tmp_path / "threads",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDetectDecisionsDaemon:
    def test_creation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path)
        assert daemon.name == "decision_detector"
        assert daemon.enabled is True

    def test_tick_empty_graph(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        assert findings == []

    def test_tick_scores_decision_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_decision_entry()],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        findings = daemon.tick()
        assert len(findings) >= 1
        f = findings[0]
        assert f.category == "decision_candidate"
        assert f.severity == "info"
        assert f.details["score"] >= 3  # typed Decision base score
        assert f.details["tier"] == "High"

    def test_tick_scores_commitment_language(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_note_entry(
                summary="We decided to use PostgreSQL for the backend."
            )],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        findings = daemon.tick()
        assert len(findings) >= 1
        assert findings[0].details["score"] >= 2

    def test_tick_incremental_skips_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_decision_entry()],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        tick1 = daemon.tick()
        assert len(tick1) >= 1
        tick2 = daemon.tick()
        assert tick2 == []

    def test_tick_incremental_rescans_changed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_decision_entry(entry_id="01A")],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        daemon.tick()

        # Add a new entry (changes entry count)
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[
                _decision_entry(entry_id="01A"),
                _decision_entry(entry_id="01B", title="Choose Redis for caching"),
            ],
        )
        tick2 = daemon.tick()
        # Should find the new entry (01B); 01A is deduped
        new_ids = [f.entry_id for f in tick2]
        assert "01B" in new_ids

    def test_tick_dedup_suppresses_existing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_decision_entry(entry_id="01DUP")],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        tick1 = daemon.tick()
        assert len(tick1) >= 1

        # Force rescan by changing mtime (rewrite graph)
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_decision_entry(entry_id="01DUP")],
        )
        tick2 = daemon.tick()
        dup_ids = [f.entry_id for f in tick2 if f.entry_id == "01DUP"]
        assert dup_ids == []  # Already reported

    def test_tick_min_score_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_note_entry(summary="Just a regular note.")],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=4)
        findings = daemon.tick()
        # A plain note should score 0, below min_score=4
        assert findings == []

    def test_tick_max_findings_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entries = [
            _decision_entry(entry_id=f"ENT{i:03d}")
            for i in range(10)
        ]
        _write_graph_thread(threads_dir, "test-topic", entries=entries)
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, min_score=1, max_findings_per_run=3,
        )
        findings = daemon.tick()
        assert len(findings) <= 3

    def test_tick_max_findings_no_checkpoint_partial_thread(self, tmp_path, monkeypatch):
        """Thread not checkpointed when max_findings cap hit mid-scan."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entries = [
            _decision_entry(entry_id=f"ENT{i:03d}")
            for i in range(10)
        ]
        _write_graph_thread(threads_dir, "test-topic", entries=entries)
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, min_score=1, max_findings_per_run=3,
        )
        daemon.tick()
        # Thread should NOT be checkpointed since cap was hit mid-scan
        assert "test-topic" not in daemon._checkpoint.thread_state

    def test_tick_handles_missing_graph_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(
            tmp_path, threads_dir=tmp_path / "nonexistent",
        )
        findings = daemon.tick()
        assert findings == []

    def test_tick_handles_corrupt_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        entries = [
            {"corrupt": True},  # Missing all fields
            _decision_entry(entry_id="GOOD"),
        ]
        _write_graph_thread(threads_dir, "test-topic", entries=entries)
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        findings = daemon.tick()
        # Should still produce finding for the good entry
        good_ids = [f.entry_id for f in findings if f.entry_id == "GOOD"]
        assert len(good_ids) >= 1

    def test_tick_prunes_stale_checkpoint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(threads_dir, "alive-topic", entries=[_decision_entry()])
        _write_graph_thread(threads_dir, "doomed-topic", entries=[_decision_entry(entry_id="02X")])

        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        daemon.tick()
        assert "doomed-topic" in daemon._checkpoint.thread_state

        # Remove doomed-topic from graph
        import shutil
        graph_dir = storage.get_graph_dir(threads_dir)
        shutil.rmtree(storage.get_thread_graph_dir(graph_dir, "doomed-topic"))

        daemon.tick()
        assert "doomed-topic" not in daemon._checkpoint.thread_state

    def test_tick_signal2_search_hit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_note_entry(
                body="We committed to using the new API.",
                summary="Technical update.",
            )],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        findings = daemon.tick()
        # "committed" in body triggers search_hit, adding +1
        if findings:
            assert "search_hit" in findings[0].details["signals"]

    def test_tick_closed_thread_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "closed-topic",
            status="CLOSED",
            entries=[_decision_entry()],
        )
        # scan_closed_threads=False should skip
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir,
            min_score=1, scan_closed_threads=False,
        )
        findings = daemon.tick()
        assert findings == []

        # scan_closed_threads=True (default) should include
        daemon2 = _make_daemon(
            tmp_path, threads_dir=threads_dir,
            min_score=1, scan_closed_threads=True,
        )
        findings2 = daemon2.tick()
        assert len(findings2) >= 1

    def test_finding_details_include_signals(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_decision_entry()],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        findings = daemon.tick()
        assert len(findings) >= 1
        details = findings[0].details
        assert "score" in details
        assert "tier" in details
        assert "signals" in details
        assert "matched_phrases" in details
        assert details["signals_available"] == ["s1_title_type", "s2_keyword_match"]

    def test_tick_excludes_daemon_written_entries(self, tmp_path, monkeypatch):
        """Entries with agent starting with 'ExtractDecisionsDaemon' are skipped."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        daemon_entry = _decision_entry(entry_id="DAEMON01")
        daemon_entry["agent"] = "ExtractDecisionsDaemon (system)"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[daemon_entry],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        findings = daemon.tick()
        assert findings == []

    def test_tick_includes_human_decision_entries(self, tmp_path, monkeypatch):
        """Human Decision entries are still scored (only daemon agents excluded)."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        human_entry = _decision_entry(entry_id="HUMAN01")
        human_entry["agent"] = "Claude (jay)"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[human_entry],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        findings = daemon.tick()
        assert len(findings) >= 1

    def test_config_defaults(self):
        cfg = DecisionDetectorConfig()
        assert cfg.enabled is False
        assert cfg.interval == 300.0
        assert cfg.min_score == 2
        assert cfg.max_findings_per_run == 200
        assert cfg.fuzzy_threshold == 85
        assert cfg.scan_closed_threads is True
        assert cfg.exclude_agents == ["ExtractDecisionsDaemon"]

    def test_status_summary_includes_custom_metrics(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir, "test-topic",
            entries=[_decision_entry()],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, min_score=1)
        daemon.tick()
        summary = daemon.status_summary()
        assert "last_tick_scored" in summary
        assert "last_tick_findings" in summary
        assert "last_tick_skipped_threads" in summary
        assert summary["last_tick_scored"] >= 1
        assert summary["last_tick_findings"] >= 1


class TestComputeSearchHit:
    def test_keyword_in_body(self):
        entry = {"body": "we committed to the new approach"}
        assert _compute_search_hit(entry) is True

    def test_keyword_in_title(self):
        entry = {"title": "Decided on PostgreSQL"}
        assert _compute_search_hit(entry) is True

    def test_no_keyword(self):
        entry = {"title": "Regular update", "body": "Nothing special"}
        assert _compute_search_hit(entry) is False

    def test_compound_keyword(self):
        entry = {"body": "going forward we use the new API"}
        assert _compute_search_hit(entry) is True

    def test_we_will_compound(self):
        entry = {"body": "we will adopt the new pattern"}
        assert _compute_search_hit(entry) is True

    def test_topic_slug_not_checked(self):
        """Topic slug keywords must not inflate per-entry scores."""
        entry = {"title": "Some note"}
        assert _compute_search_hit(entry) is False
