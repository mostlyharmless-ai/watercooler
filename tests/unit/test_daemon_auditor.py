"""Tests for ThreadAuditorDaemon — the first concrete daemon."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from watercooler.config_schema import ThreadAuditorConfig
from watercooler_mcp.daemons.auditor import ThreadAuditorDaemon


def _write_thread(threads_dir: Path, topic: str, content: str, *, subdir: str = "") -> Path:
    """Helper: write a thread file."""
    if subdir:
        d = threads_dir / subdir
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{topic}.md"
    else:
        p = threads_dir / f"{topic}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# Minimal thread content templates
_THREAD_COMPLETE = """\
# Test Thread
Status: OPEN
Ball: human

---

Entry: TestAgent 2025-01-01T00:00:00Z
<!-- Entry-ID: 01ABCDEFGHIJKLMNOPQRSTUV -->
Role: implementer
Type: Note
Title: Test Entry

Body content here.
"""

_THREAD_NO_STATUS = """\
# Test Thread
Ball: human

---

Entry: TestAgent 2025-01-01T00:00:00Z
<!-- Entry-ID: 01ABCDEFGHIJKLMNOPQRSTUV -->
Role: implementer
Type: Note
Title: Test Entry

Body content here.
"""

_THREAD_NO_BALL = """\
# Test Thread
Status: OPEN

---

Entry: TestAgent 2025-01-01T00:00:00Z
<!-- Entry-ID: 01ABCDEFGHIJKLMNOPQRSTUV -->
Role: implementer
Type: Note
Title: Test Entry

Body content here.
"""

_THREAD_NO_ENTRY_ID = """\
# Test Thread
Status: OPEN
Ball: human

---

Entry: TestAgent 2025-01-01T00:00:00Z
Role: implementer
Type: Note
Title: Test Entry

Body content here.
"""

_THREAD_CLOSED = """\
# Closed Thread
Status: done
Ball: human

---

Entry: TestAgent 2025-01-01T00:00:00Z
<!-- Entry-ID: 01ABCDEFGHIJKLMNOPQRSTUV -->
Role: implementer
Type: Closure
Title: Done

All done.
"""


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
        _write_thread(threads_dir, "good-thread", _THREAD_COMPLETE)

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
        _write_thread(threads_dir, "no-status", _THREAD_NO_STATUS)

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
        _write_thread(threads_dir, "no-ball", _THREAD_NO_BALL)

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
        _write_thread(threads_dir, "no-eid", _THREAD_NO_ENTRY_ID)

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
        p = _write_thread(threads_dir, "stale", _THREAD_COMPLETE)

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
        _write_thread(threads_dir, "done-thread", _THREAD_CLOSED, subdir="threads")

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
        _write_thread(threads_dir, "thread1", _THREAD_COMPLETE)

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
            _write_thread(threads_dir, f"thread-{i}", _THREAD_NO_STATUS)

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
        _write_thread(threads_dir, "thread", _THREAD_NO_STATUS)

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
