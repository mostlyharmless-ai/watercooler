"""Tests for ThreadAuditorDaemon — the first concrete daemon."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from watercooler.config_schema import ThreadAuditorConfig
from watercooler_mcp.daemons.auditor import ThreadAuditorDaemon


from watercooler.baseline_graph import storage


def _write_graph_thread(
    threads_dir: Path,
    topic: str,
    *,
    status: str = "OPEN",
    ball: str = "human",
    title: str = "Test Thread",
    entries: list | None = None,
    subdir: str = "",
) -> Path:
    """Helper: write graph data for a thread."""
    threads_dir.mkdir(parents=True, exist_ok=True)

    # Write .md file for mtime/stale checks
    if subdir:
        d = threads_dir / subdir
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{topic}.md"
    else:
        p = threads_dir / f"{topic}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {title}\nStatus: {status}\nBall: {ball}\n", encoding="utf-8")

    # Write graph data
    graph_dir = storage.ensure_graph_dir(threads_dir)
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, topic)

    meta = {
        "id": f"thread:{topic}",
        "topic": topic,
        "title": title,
        "status": status,
        "ball": ball,
        "last_updated": "2025-01-01T00:00:00Z",
    }
    storage.atomic_write_json(thread_dir / "meta.json", meta)

    entry_list = entries if entries is not None else []
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", entry_list)

    return p


# Reusable entry templates
_ENTRY_COMPLETE = {
    "id": "entry:01ABCDEFGHIJKLMNOPQRSTUV",
    "entry_id": "01ABCDEFGHIJKLMNOPQRSTUV",
    "agent": "TestAgent",
    "timestamp": "2025-01-01T00:00:00Z",
    "role": "implementer",
    "entry_type": "Note",
    "title": "Test Entry",
    "body": "Body content here.",
    "index": 0,
}

_ENTRY_NO_ID = {
    "id": "entry:auto-0",
    "agent": "TestAgent",
    "timestamp": "2025-01-01T00:00:00Z",
    "role": "implementer",
    "entry_type": "Note",
    "title": "Test Entry",
    "body": "Body content here.",
    "index": 0,
}


class TestThreadAuditorDaemon:
    def test_creation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = ThreadAuditorDaemon(threads_dir=tmp_path)
        assert daemon.name == "thread_auditor"
        assert daemon.enabled is True

    def test_tick_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        threads_dir.mkdir()
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir)
        findings = daemon.tick()
        assert findings == []

    def test_tick_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = ThreadAuditorDaemon(threads_dir=tmp_path / "nonexistent")
        findings = daemon.tick()
        assert findings == []

    def test_complete_thread_no_findings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        _write_graph_thread(threads_dir, "good-thread", entries=[_ENTRY_COMPLETE])

        config = ThreadAuditorConfig(
            check_missing_summaries=False,  # Skip graph checks
            check_stale_threads=False,      # Skip stale checks
            check_classification=False,     # Skip classification
        )
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir, config=config)
        findings = daemon.tick()
        assert len(findings) == 0

    def test_missing_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        _write_graph_thread(threads_dir, "no-status", status="", entries=[_ENTRY_COMPLETE])

        config = ThreadAuditorConfig(
            check_missing_ball=False,
            check_missing_entry_ids=False,
            check_missing_summaries=False,
            check_stale_threads=False,
            check_classification=False,
        )
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir, config=config)
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "missing_status"
        assert findings[0].severity == "warning"

    def test_missing_ball(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        _write_graph_thread(threads_dir, "no-ball", ball="", entries=[_ENTRY_COMPLETE])

        config = ThreadAuditorConfig(
            check_missing_status=False,
            check_missing_entry_ids=False,
            check_missing_summaries=False,
            check_stale_threads=False,
            check_classification=False,
        )
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir, config=config)
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "missing_ball"
        assert findings[0].severity == "info"

    def test_missing_entry_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        _write_graph_thread(threads_dir, "no-eid", entries=[_ENTRY_NO_ID])

        config = ThreadAuditorConfig(
            check_missing_status=False,
            check_missing_ball=False,
            check_missing_summaries=False,
            check_stale_threads=False,
            check_classification=False,
        )
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir, config=config)
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "missing_entry_id"
        assert findings[0].severity == "warning"

    def test_stale_thread(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        p = _write_graph_thread(threads_dir, "stale", entries=[_ENTRY_COMPLETE])

        # Set mtime to 30 days ago
        import os
        old_time = time.time() - (30 * 86400)
        os.utime(str(p), (old_time, old_time))

        config = ThreadAuditorConfig(
            check_missing_status=False,
            check_missing_ball=False,
            check_missing_entry_ids=False,
            check_missing_summaries=False,
            check_classification=False,
            stale_days=14,
        )
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir, config=config)
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "stale_thread"
        assert findings[0].details["days_idle"] >= 28

    def test_classification_closed_in_wrong_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        # Create structured layout
        (threads_dir / "threads").mkdir(parents=True)
        (threads_dir / "closed").mkdir(parents=True)
        _write_graph_thread(
            threads_dir, "done-thread", status="CLOSED",
            entries=[_ENTRY_COMPLETE], subdir="threads",
        )

        config = ThreadAuditorConfig(
            check_missing_status=False,
            check_missing_ball=False,
            check_missing_entry_ids=False,
            check_missing_summaries=False,
            check_stale_threads=False,
        )
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir, config=config)
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "classification_suggestion"
        assert "closed" in findings[0].message

    def test_incremental_skip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        _write_graph_thread(threads_dir, "thread1", entries=[_ENTRY_COMPLETE])

        config = ThreadAuditorConfig(
            check_missing_summaries=False,
            check_stale_threads=False,
            check_classification=False,
        )
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir, config=config)

        # First tick processes the thread
        f1 = daemon.tick()
        assert daemon._checkpoint.threads_processed == 1
        assert daemon._checkpoint.threads_skipped == 0

        # Second tick skips (unchanged thread)
        f2 = daemon.tick()
        assert daemon._checkpoint.threads_processed == 0
        assert daemon._checkpoint.threads_skipped == 1

    def test_max_findings_per_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        # Create many threads with missing status
        for i in range(10):
            _write_graph_thread(
                threads_dir, f"thread-{i}", status="",
                entries=[_ENTRY_COMPLETE],
            )

        config = ThreadAuditorConfig(
            max_findings_per_run=3,
            check_missing_ball=False,
            check_missing_entry_ids=False,
            check_missing_summaries=False,
            check_stale_threads=False,
            check_classification=False,
        )
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir, config=config)
        findings = daemon.tick()
        assert len(findings) <= 3

    def test_all_checks_disabled_no_findings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads_root"
        _write_graph_thread(threads_dir, "thread", status="", entries=[_ENTRY_COMPLETE])

        config = ThreadAuditorConfig(
            check_missing_status=False,
            check_missing_ball=False,
            check_missing_entry_ids=False,
            check_missing_summaries=False,
            check_stale_threads=False,
            check_classification=False,
        )
        daemon = ThreadAuditorDaemon(threads_dir=threads_dir, config=config)
        findings = daemon.tick()
        assert len(findings) == 0
