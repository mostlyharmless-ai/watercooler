"""Tests for the per-repo PID-lock model (L1).

Closes the failure mode Jay observed 2026-05-08: pre-L1, the first MCP
server to boot on a machine claimed the only ``daemon.pid`` lock and
ran daemons against its own CWD; concurrent MCP servers on the same
machine watching different repos got an empty registry. Post-L1, each
repo gets its own lock file (``<repo_key>.pid``) so multiple fleets
can co-exist.

Per cloud Design (local) entry ``01KR5RCWK0F0EM1YVKWRJPD239`` on
``daemon-architecture-audit-2026-05`` and execution-roadmap entry
``01KR5Y2A6FBS7T83K9ASY6B25P`` (L1).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import watercooler_mcp.daemons as _daemons_module
from watercooler_mcp.daemons import (
    _list_sibling_fleets,
    _pidfile_path,
    _try_acquire_daemon_lock,
)


@pytest.fixture
def temp_pidfile_dir(tmp_path: Path):
    """Redirect the module-level ``_PIDFILE_DIR`` to a temp dir for the test."""
    orig_dir = _daemons_module._PIDFILE_DIR
    orig_pidfile = _daemons_module._daemon_pidfile
    _daemons_module._PIDFILE_DIR = tmp_path
    _daemons_module._daemon_pidfile = None
    try:
        yield tmp_path
    finally:
        _daemons_module._PIDFILE_DIR = orig_dir
        _daemons_module._daemon_pidfile = orig_pidfile


def _write_pidfile(path: Path, pid: int) -> None:
    """Write a pidfile in the canonical ``pid=<n>`` format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"pid={pid}\n", encoding="utf-8")


class TestPidfilePath:
    """``_pidfile_path`` derives the per-repo lock filename."""

    def test_pidfile_path_uses_repo_key(self, temp_pidfile_dir: Path) -> None:
        """Non-empty repo_key gives ``<repo_key>.pid``."""
        path = _pidfile_path("abc123")
        assert path == temp_pidfile_dir / "abc123.pid"

    def test_pidfile_path_legacy_fallback(self, temp_pidfile_dir: Path) -> None:
        """Empty repo_key falls back to the pre-L1 ``daemon.pid``."""
        path = _pidfile_path("")
        assert path == temp_pidfile_dir / "daemon.pid"

    def test_distinct_repo_keys_get_distinct_paths(
        self, temp_pidfile_dir: Path
    ) -> None:
        """Different repo_keys → different files (the whole point of L1)."""
        assert _pidfile_path("abc123") != _pidfile_path("xyz789")


class TestPerRepoLockAcquisition:
    """``_try_acquire_daemon_lock(repo_key)`` creates one lock per repo."""

    def test_acquire_creates_per_repo_pidfile(
        self, temp_pidfile_dir: Path
    ) -> None:
        """Successful acquire writes our PID to ``<repo_key>.pid``."""
        ok = _try_acquire_daemon_lock("abc123")
        assert ok is True
        pidfile = temp_pidfile_dir / "abc123.pid"
        assert pidfile.exists()
        content = pidfile.read_text(encoding="utf-8")
        assert content.startswith(f"pid={os.getpid()}")

    def test_two_repo_keys_get_independent_locks(
        self, temp_pidfile_dir: Path
    ) -> None:
        """Same process can acquire locks for two different repos.

        This is the load-bearing claim of L1: multiple repos on one
        machine each get their own lock and don't block each other.
        """
        ok_a = _try_acquire_daemon_lock("repo-a")
        assert ok_a is True
        ok_b = _try_acquire_daemon_lock("repo-b")
        assert ok_b is True
        assert (temp_pidfile_dir / "repo-a.pid").exists()
        assert (temp_pidfile_dir / "repo-b.pid").exists()

    def test_acquire_blocks_when_other_live_holder_for_same_repo(
        self, temp_pidfile_dir: Path
    ) -> None:
        """Live holder for repo X → another process for repo X fails to acquire."""
        # Synthesise a live "other" PID by using the parent process —
        # always alive while pytest runs, and definitely not us.
        other_pid = os.getppid()
        if other_pid == os.getpid() or other_pid <= 1:
            pytest.skip("Cannot synthesise distinct live PID in this environment")
        _write_pidfile(temp_pidfile_dir / "abc123.pid", other_pid)

        ok = _try_acquire_daemon_lock("abc123")
        assert ok is False, (
            "Should not steal lock from live holder for the same repo"
        )

    def test_acquire_takes_over_stale_lock(
        self, temp_pidfile_dir: Path
    ) -> None:
        """Dead holder PID → stale lock, new process takes over."""
        # PID 0 is never a real process; ``_pid_is_alive(0)`` returns False
        # without ambiguity.
        _write_pidfile(temp_pidfile_dir / "abc123.pid", 0)

        ok = _try_acquire_daemon_lock("abc123")
        assert ok is True
        content = (temp_pidfile_dir / "abc123.pid").read_text(encoding="utf-8")
        assert content.startswith(f"pid={os.getpid()}")

    def test_legacy_fallback_when_repo_key_empty(
        self, temp_pidfile_dir: Path
    ) -> None:
        """Empty repo_key → legacy ``daemon.pid`` lock for back-compat."""
        ok = _try_acquire_daemon_lock("")
        assert ok is True
        assert (temp_pidfile_dir / "daemon.pid").exists()


class TestSiblingFleetListing:
    """``_list_sibling_fleets`` surfaces other live fleets on the machine."""

    def test_returns_empty_when_only_self_holds_a_lock(
        self, temp_pidfile_dir: Path
    ) -> None:
        """Only our own lock present → empty sibling list."""
        _write_pidfile(temp_pidfile_dir / "abc123.pid", os.getpid())
        siblings = _list_sibling_fleets()
        assert siblings == []

    def test_returns_other_live_fleets(self, temp_pidfile_dir: Path) -> None:
        """A different live PID's lock surfaces as a sibling fleet."""
        other_pid = os.getppid()
        if other_pid == os.getpid() or other_pid <= 1:
            pytest.skip("Cannot synthesise distinct live PID")
        _write_pidfile(temp_pidfile_dir / "repo-other.pid", other_pid)

        siblings = _list_sibling_fleets()
        assert any(
            s["repo_key"] == "repo-other" and s["pid"] == other_pid
            for s in siblings
        ), f"Expected sibling for repo-other, got: {siblings}"

    def test_excludes_dead_holders(self, temp_pidfile_dir: Path) -> None:
        """A pidfile pointing to a dead PID is NOT surfaced as live."""
        _write_pidfile(temp_pidfile_dir / "ghost-repo.pid", 0)
        siblings = _list_sibling_fleets()
        assert all(s["repo_key"] != "ghost-repo" for s in siblings)

    def test_excludes_corrupt_pidfiles(self, temp_pidfile_dir: Path) -> None:
        """Corrupt pidfile contents are skipped silently — defensive."""
        bad = temp_pidfile_dir / "broken.pid"
        bad.write_text("not-a-pidfile\n")
        # Should not raise; broken file just doesn't appear in the list.
        siblings = _list_sibling_fleets()
        assert all(s["repo_key"] != "broken" for s in siblings)

    def test_sibling_listing_includes_legacy_pidfile(
        self, temp_pidfile_dir: Path
    ) -> None:
        """Legacy ``daemon.pid`` from a pre-L1 process surfaces as repo_key='daemon'."""
        other_pid = os.getppid()
        if other_pid == os.getpid() or other_pid <= 1:
            pytest.skip("Cannot synthesise distinct live PID")
        _write_pidfile(temp_pidfile_dir / "daemon.pid", other_pid)

        siblings = _list_sibling_fleets()
        assert any(s["repo_key"] == "daemon" for s in siblings), (
            "Legacy daemon.pid sibling must be visible during migration"
        )


class TestRepoKeyResolution:
    """``_resolve_repo_key_from_cwd`` derives a repo_key when CWD is a repo."""

    def test_returns_empty_when_resolution_fails(self) -> None:
        """When ``resolve_thread_context`` raises, return empty string."""
        with patch(
            "watercooler_mcp.config.resolve_thread_context",
            side_effect=RuntimeError("unresolvable"),
        ):
            from watercooler_mcp.daemons import _resolve_repo_key_from_cwd

            assert _resolve_repo_key_from_cwd() == ""

    def test_returns_empty_when_code_root_is_none(self) -> None:
        """No code_root in resolved context → empty repo_key."""
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.code_root = None
        with patch(
            "watercooler_mcp.config.resolve_thread_context", return_value=ctx
        ):
            from watercooler_mcp.daemons import _resolve_repo_key_from_cwd

            assert _resolve_repo_key_from_cwd() == ""

    def test_resolves_repo_key_when_cwd_is_a_repo(self, tmp_path: Path) -> None:
        """Valid resolved code_root → 12-char SHA-1-derived key."""
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.code_root = tmp_path
        with patch(
            "watercooler_mcp.config.resolve_thread_context", return_value=ctx
        ):
            from watercooler_mcp.daemons import _resolve_repo_key_from_cwd

            key = _resolve_repo_key_from_cwd()
            assert isinstance(key, str)
            assert len(key) == 12
            # Same path → same key (determinism).
            key2 = _resolve_repo_key_from_cwd()
            assert key == key2


class TestDaemonManagerRepoKey:
    """``DaemonManager`` exposes ``repo_key`` for sibling-fleet attribution."""

    def test_default_repo_key_is_empty(self) -> None:
        """``DaemonManager()`` without arg keeps the legacy single-fleet shape."""
        from watercooler_mcp.daemons.manager import DaemonManager

        mgr = DaemonManager()
        assert mgr.repo_key == ""

    def test_repo_key_arg_is_stored(self) -> None:
        """Constructor arg is stored as a public attribute."""
        from watercooler_mcp.daemons.manager import DaemonManager

        mgr = DaemonManager(repo_key="abc123")
        assert mgr.repo_key == "abc123"
