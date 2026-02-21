"""Tests for daemon state types: Finding, DaemonCheckpoint, ThreadCheckpoint."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from watercooler_mcp.daemons.state import (
    DaemonCheckpoint,
    Finding,
    ThreadCheckpoint,
    append_findings,
    load_checkpoint,
    load_findings,
    save_checkpoint,
)


# ------------------------------------------------------------------ #
# Finding
# ------------------------------------------------------------------ #


class TestFinding:
    def test_creation_with_defaults(self):
        f = Finding(
            finding_id="abc123",
            daemon_name="test_daemon",
            severity="warning",
            category="missing_status",
            topic="my-topic",
        )
        assert f.finding_id == "abc123"
        assert f.severity == "warning"
        assert f.created_at > 0

    def test_auto_timestamp(self):
        before = time.time()
        f = Finding(
            finding_id="x",
            daemon_name="d",
            severity="info",
            category="test",
            topic="t",
        )
        after = time.time()
        assert before <= f.created_at <= after

    def test_explicit_timestamp(self):
        f = Finding(
            finding_id="x",
            daemon_name="d",
            severity="info",
            category="test",
            topic="t",
            created_at=12345.0,
        )
        assert f.created_at == 12345.0

    def test_to_dict_roundtrip(self):
        f = Finding(
            finding_id="abc",
            daemon_name="d",
            severity="error",
            category="test",
            topic="t",
            message="something wrong",
            details={"key": "val"},
            created_at=99.0,
        )
        d = f.to_dict()
        f2 = Finding.from_dict(d)
        assert f2.finding_id == f.finding_id
        assert f2.severity == f.severity
        assert f2.details == {"key": "val"}
        assert f2.created_at == 99.0

    def test_from_dict_ignores_extra_keys(self):
        d = {
            "finding_id": "x",
            "daemon_name": "d",
            "severity": "info",
            "category": "c",
            "topic": "t",
            "extra_field": "ignored",
        }
        f = Finding.from_dict(d)
        assert f.finding_id == "x"


# ------------------------------------------------------------------ #
# ThreadCheckpoint
# ------------------------------------------------------------------ #


class TestThreadCheckpoint:
    def test_creation(self):
        tc = ThreadCheckpoint(topic="my-thread", mtime=1000.0, entry_count=5)
        assert tc.topic == "my-thread"
        assert tc.mtime == 1000.0
        assert tc.entry_count == 5
        assert tc.last_audited == 0.0

    def test_roundtrip(self):
        tc = ThreadCheckpoint(topic="t", mtime=1.0, entry_count=3, last_audited=2.0)
        d = tc.to_dict()
        tc2 = ThreadCheckpoint.from_dict(d)
        assert tc2.topic == tc.topic
        assert tc2.mtime == tc.mtime
        assert tc2.entry_count == tc.entry_count
        assert tc2.last_audited == tc.last_audited


# ------------------------------------------------------------------ #
# DaemonCheckpoint
# ------------------------------------------------------------------ #


class TestDaemonCheckpoint:
    def test_creation(self):
        dc = DaemonCheckpoint(daemon_name="test")
        assert dc.daemon_name == "test"
        assert dc.last_run == 0.0
        assert dc.thread_state == {}

    def test_is_thread_changed_new_thread(self):
        dc = DaemonCheckpoint(daemon_name="test")
        assert dc.is_thread_changed("new-topic", 100.0, 5) is True

    def test_is_thread_changed_same(self):
        dc = DaemonCheckpoint(daemon_name="test")
        dc.update_thread("topic", 100.0, 5)
        assert dc.is_thread_changed("topic", 100.0, 5) is False

    def test_is_thread_changed_mtime_differs(self):
        dc = DaemonCheckpoint(daemon_name="test")
        dc.update_thread("topic", 100.0, 5)
        assert dc.is_thread_changed("topic", 200.0, 5) is True

    def test_is_thread_changed_count_differs(self):
        dc = DaemonCheckpoint(daemon_name="test")
        dc.update_thread("topic", 100.0, 5)
        assert dc.is_thread_changed("topic", 100.0, 6) is True

    def test_update_thread(self):
        dc = DaemonCheckpoint(daemon_name="test")
        before = time.time()
        dc.update_thread("t", 10.0, 3)
        after = time.time()
        tc = dc.thread_state["t"]
        assert tc.topic == "t"
        assert tc.mtime == 10.0
        assert tc.entry_count == 3
        assert before <= tc.last_audited <= after

    def test_roundtrip(self):
        dc = DaemonCheckpoint(daemon_name="test", last_run=50.0, error_count=2)
        dc.update_thread("a", 10.0, 1)
        dc.update_thread("b", 20.0, 2)

        d = dc.to_dict()
        dc2 = DaemonCheckpoint.from_dict(d)

        assert dc2.daemon_name == "test"
        assert dc2.last_run == 50.0
        assert dc2.error_count == 2
        assert "a" in dc2.thread_state
        assert dc2.thread_state["a"].mtime == 10.0
        assert dc2.thread_state["b"].entry_count == 2


# ------------------------------------------------------------------ #
# Persistence
# ------------------------------------------------------------------ #


class TestPersistence:
    def test_save_and_load_checkpoint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        dc = DaemonCheckpoint(daemon_name="test", last_run=42.0)
        dc.update_thread("topic", 10.0, 3)
        save_checkpoint(dc)

        loaded = load_checkpoint("test")
        assert loaded.last_run == 42.0
        assert "topic" in loaded.thread_state
        assert loaded.thread_state["topic"].mtime == 10.0

    def test_load_missing_checkpoint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        dc = load_checkpoint("nonexistent")
        assert dc.daemon_name == "nonexistent"
        assert dc.last_run == 0.0

    def test_append_and_load_findings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        findings = [
            Finding(
                finding_id=f"f{i}",
                daemon_name="test",
                severity="info",
                category="test_cat",
                topic="t",
                created_at=float(i),
            )
            for i in range(5)
        ]
        append_findings("test", findings)

        loaded = load_findings("test")
        assert len(loaded) == 5
        # Newest first
        assert loaded[0].finding_id == "f4"
        assert loaded[4].finding_id == "f0"

    def test_load_findings_with_filters(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        findings = [
            Finding(
                finding_id="f1",
                daemon_name="test",
                severity="warning",
                category="missing_status",
                topic="a",
                created_at=1.0,
            ),
            Finding(
                finding_id="f2",
                daemon_name="test",
                severity="info",
                category="stale_thread",
                topic="b",
                created_at=2.0,
            ),
            Finding(
                finding_id="f3",
                daemon_name="test",
                severity="warning",
                category="missing_status",
                topic="a",
                created_at=3.0,
            ),
        ]
        append_findings("test", findings)

        # Filter by severity
        warnings = load_findings("test", severity="warning")
        assert len(warnings) == 2

        # Filter by category
        stale = load_findings("test", category="stale_thread")
        assert len(stale) == 1
        assert stale[0].finding_id == "f2"

        # Filter by topic
        topic_a = load_findings("test", topic="a")
        assert len(topic_a) == 2

        # Limit
        limited = load_findings("test", limit=2)
        assert len(limited) == 2

    def test_load_findings_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        loaded = load_findings("nonexistent")
        assert loaded == []
