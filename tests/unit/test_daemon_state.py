"""Tests for daemon state types: Finding, DaemonCheckpoint, ThreadCheckpoint."""

from __future__ import annotations

import time

from watercooler_mcp.daemons.state import (
    DaemonCheckpoint,
    Finding,
    ThreadCheckpoint,
    acknowledge_finding,
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

    def test_load_findings_order_oldest_returns_oldest_slice(
        self, tmp_path, monkeypatch
    ):
        """With order="oldest", the truncation keeps the oldest N (not newest)."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        findings = [
            Finding(
                finding_id=f"f{i}",
                daemon_name="test",
                severity="info",
                category="c",
                topic="t",
                created_at=float(i),
            )
            for i in range(10)
        ]
        append_findings("test", findings)

        # Default newest-first: limit=3 returns f9, f8, f7
        newest = load_findings("test", limit=3)
        assert [f.finding_id for f in newest] == ["f9", "f8", "f7"]

        # order="oldest": limit=3 returns f0, f1, f2 — NOT dropped by the limit.
        oldest = load_findings("test", limit=3, order="oldest")
        assert [f.finding_id for f in oldest] == ["f0", "f1", "f2"]

        # limit=None: all 10 returned (no truncation).
        all_loaded = load_findings("test", limit=None)
        assert len(all_loaded) == 10


class TestAcknowledgeFinding:
    def _seed(self, tmp_path, monkeypatch, finding_ids: list) -> None:
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        findings = [
            Finding(
                finding_id=fid,
                daemon_name="test",
                severity="info",
                category="test_cat",
                topic="t",
            )
            for fid in finding_ids
        ]
        append_findings("test", findings)

    def test_acknowledge_finding_marks_acknowledged(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch, ["f1", "f2"])
        ok = acknowledge_finding("test", "f1")
        assert ok is True
        loaded = load_findings("test", unacknowledged_only=False)
        acked = {f.finding_id: f.acknowledged for f in loaded}
        assert acked["f1"] is True
        assert acked["f2"] is False

    def test_acknowledge_finding_returns_false_for_missing_id(
        self, tmp_path, monkeypatch
    ):
        self._seed(tmp_path, monkeypatch, ["f1"])
        ok = acknowledge_finding("test", "no-such-id")
        assert ok is False

    def test_acknowledge_finding_is_idempotent(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch, ["f1"])
        assert acknowledge_finding("test", "f1") is True
        assert acknowledge_finding("test", "f1") is True
        loaded = load_findings("test", unacknowledged_only=False)
        assert loaded[0].acknowledged is True

    def test_acknowledge_finding_preserves_other_lines(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch, ["f1", "f2", "f3"])
        acknowledge_finding("test", "f2")
        loaded = load_findings("test", unacknowledged_only=False)
        by_id = {f.finding_id: f for f in loaded}
        assert by_id["f1"].acknowledged is False
        assert by_id["f2"].acknowledged is True
        assert by_id["f3"].acknowledged is False

    def test_acknowledge_finding_returns_false_when_no_file(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path
        )
        ok = acknowledge_finding("no_such_daemon", "f1")
        assert ok is False


# ------------------------------------------------------------------ #
# BaseException re-raise contracts (PR #705 round 7+5+2 review)
# ------------------------------------------------------------------ #
#
# The reviewer claimed that ``_maybe_compact``'s ``except BaseException:``
# handler was missing a trailing ``raise`` and would silently swallow
# ``KeyboardInterrupt`` / ``SystemExit``. The claim is factually wrong
# — the ``raise`` is present and KI/SE propagate correctly. These
# tests pin the actual behaviour so the same false-positive review
# claim can't recur.


class TestMaybeCompactBaseExceptionPropagates:
    """``_maybe_compact`` must clean up the tmp file AND re-raise on
    ``KeyboardInterrupt`` / ``SystemExit``. The outer ``except
    Exception`` handler does NOT catch ``BaseException``, so KI/SE
    pass through to the caller (typically ``append_findings`` under
    ``_findings_lock``)."""

    def _force_compaction_setup(self, tmp_path, monkeypatch):
        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)
        monkeypatch.setattr(state_mod, "_MAX_FINDINGS_LINES", 1)
        monkeypatch.setattr(state_mod, "_COMPACT_KEEP_LINES", 1)
        # Pre-create a findings file with enough lines that compaction
        # will run.
        d = daemons_root / "test"
        d.mkdir(parents=True)
        path = d / "findings.jsonl"
        with path.open("w") as f:
            for i in range(5):
                f.write(f'{{"finding_id":"f{i}"}}\n')
        return state_mod, path

    def test_keyboard_interrupt_during_replace_propagates(
        self, tmp_path, monkeypatch
    ):
        # Simulate Ctrl+C arriving during ``os.replace``. The
        # ``except BaseException:`` branch must unlink the tmp file
        # and re-raise; the outer ``except Exception:`` does NOT
        # catch it (KI is not an Exception subclass).
        import os
        import pytest

        state_mod, path = self._force_compaction_setup(tmp_path, monkeypatch)

        def _interrupt(*_a, **_kw):
            raise KeyboardInterrupt("simulated mid-replace")

        monkeypatch.setattr(os, "replace", _interrupt)

        with pytest.raises(KeyboardInterrupt, match="mid-replace"):
            state_mod._maybe_compact(path, "test")

        # Tmp files should not be left behind in the daemon dir
        # (the BaseException branch unlinks before re-raising).
        leftover = list(path.parent.glob("*.tmp"))
        assert leftover == [], (
            f"BaseException branch must unlink tmp file before re-raising; "
            f"found leftover: {leftover}"
        )

    def test_system_exit_during_replace_propagates(self, tmp_path, monkeypatch):
        import os
        import pytest

        state_mod, path = self._force_compaction_setup(tmp_path, monkeypatch)

        def _exit(*_a, **_kw):
            raise SystemExit(7)

        monkeypatch.setattr(os, "replace", _exit)

        with pytest.raises(SystemExit) as excinfo:
            state_mod._maybe_compact(path, "test")
        assert excinfo.value.code == 7

        leftover = list(path.parent.glob("*.tmp"))
        assert leftover == [], (
            f"SystemExit must unlink tmp before re-raising; "
            f"found leftover: {leftover}"
        )

    def test_regular_exception_during_replace_logs_and_swallows(
        self, tmp_path, monkeypatch
    ):
        # Regression guard: regular ``Exception`` subclasses (e.g.
        # ``OSError``) DO get caught by the outer ``except
        # Exception as e: logger.warning(...)`` block. The bool-only
        # contract of the surrounding ``append_findings`` call must
        # be preserved; a disk-full error during compaction must
        # not raise out of ``_maybe_compact``.
        import os

        state_mod, path = self._force_compaction_setup(tmp_path, monkeypatch)

        def _disk_full(*_a, **_kw):
            raise OSError("simulated disk full")

        monkeypatch.setattr(os, "replace", _disk_full)

        # Must not raise.
        state_mod._maybe_compact(path, "test")

        # Tmp file still cleaned up by the inner BaseException branch.
        leftover = list(path.parent.glob("*.tmp"))
        assert leftover == []


class TestSaveCheckpointBaseExceptionPropagates:
    """``save_checkpoint`` must clean up the tmp file AND re-raise
    on ``KeyboardInterrupt`` / ``SystemExit``. Mirrors the
    ``_maybe_compact`` and ``acknowledge_finding`` discipline that
    landed in PR #705 round 7+3 / 7+4. The pre-cleanup PR
    upgraded those two sites but missed ``save_checkpoint`` —
    this test class pins the consistency."""

    def test_keyboard_interrupt_during_replace_propagates(
        self, tmp_path, monkeypatch
    ):
        import os
        import pytest

        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)

        def _interrupt(*_a, **_kw):
            raise KeyboardInterrupt("simulated mid-replace")

        monkeypatch.setattr(os, "replace", _interrupt)

        cp = state_mod.DaemonCheckpoint(daemon_name="test")
        with pytest.raises(KeyboardInterrupt, match="mid-replace"):
            state_mod.save_checkpoint(cp)

        # Tmp file unlinked before re-raise.
        leftover = list((daemons_root / "test").glob("*.tmp"))
        assert leftover == [], (
            f"save_checkpoint BaseException branch must unlink tmp before "
            f"re-raise; found leftover: {leftover}"
        )

    def test_system_exit_during_replace_propagates(self, tmp_path, monkeypatch):
        import os
        import pytest

        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)

        def _exit(*_a, **_kw):
            raise SystemExit(42)

        monkeypatch.setattr(os, "replace", _exit)

        cp = state_mod.DaemonCheckpoint(daemon_name="test")
        with pytest.raises(SystemExit) as excinfo:
            state_mod.save_checkpoint(cp)
        assert excinfo.value.code == 42

        leftover = list((daemons_root / "test").glob("*.tmp"))
        assert leftover == []

    def test_oserror_during_replace_unlinks_and_propagates(
        self, tmp_path, monkeypatch
    ):
        # Unlike ``_maybe_compact``, ``save_checkpoint`` doesn't have
        # an outer log-and-swallow handler — it lets disk failures
        # raise to the caller. Verify the tmp cleanup still fires.
        import os
        import pytest

        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)

        def _disk_full(*_a, **_kw):
            raise OSError("simulated disk full")

        monkeypatch.setattr(os, "replace", _disk_full)

        cp = state_mod.DaemonCheckpoint(daemon_name="test")
        with pytest.raises(OSError, match="disk full"):
            state_mod.save_checkpoint(cp)

        leftover = list((daemons_root / "test").glob("*.tmp"))
        assert leftover == []
