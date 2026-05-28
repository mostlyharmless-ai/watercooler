"""Tests for ``reset_decision_extractor_checkpoint``.

The decision_extractor checkpoint reset moved off the MCP surface in the
tool-surface consolidation — it now lives in
``watercooler_mcp.daemons.decision_extractor_reset`` (driven by the CLI
script ``scripts/reset_decision_extractor.py``).

Covers the active-daemon guard for the checkpoint-reset race: the extractor
daemon holds its checkpoint in memory and writes it after every tick, so a
reset issued while the daemon is active is silently overwritten.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from watercooler_mcp.daemons.decision_extractor_reset import (
    reset_decision_extractor_checkpoint,
)


def _prepare_checkpoint(tmp_path: Path, mtime_age_seconds: float) -> Path:
    """Create a checkpoint.json with ``mtime`` set to ``now - age``."""
    cp_dir = tmp_path / "decision_extractor"
    cp_dir.mkdir()
    cp_path = cp_dir / "checkpoint.json"
    cp_path.write_text(
        json.dumps(
            {
                "daemon_name": "decision_extractor",
                "last_processed_index": 0,
                "last_processed_timestamp": None,
                "last_tick_at": None,
                "last_success_at": None,
                "last_error": None,
                "error_count": 0,
                "extras": {
                    "processed_finding_ids": ["f1", "f2"],
                    "processed_source_keys": ["s1"],
                    "daily_count": {"date": "2026-04-22", "count": 5},
                    "llm_extraction_attempts": {},
                    "write_failure_attempts": {},
                },
            }
        )
    )
    mtime = time.time() - mtime_age_seconds
    os.utime(cp_path, (mtime, mtime))
    return cp_dir


class TestDecisionExtractorResetActiveDaemonGuard:
    def test_refuses_when_checkpoint_recently_modified(self, tmp_path):
        """A fresh checkpoint means the daemon is likely live — refuse."""
        cp_dir = _prepare_checkpoint(tmp_path, mtime_age_seconds=5.0)

        with patch(
            "watercooler_mcp.daemons.state._daemon_dir", return_value=cp_dir
        ):
            payload = reset_decision_extractor_checkpoint()

        assert payload["status"] == "active_daemon"
        assert payload["checkpoint_age_seconds"] < 60.0
        assert "force=True" in payload["message"]

    def test_proceeds_when_checkpoint_is_stale(self, tmp_path):
        """A checkpoint older than the active-daemon window resets normally."""
        cp_dir = _prepare_checkpoint(tmp_path, mtime_age_seconds=120.0)

        with patch(
            "watercooler_mcp.daemons.state._daemon_dir", return_value=cp_dir
        ):
            payload = reset_decision_extractor_checkpoint()

        assert payload["status"] == "ok"
        assert payload["before"]["processed_finding_ids"] == 2

    def test_force_overrides_active_daemon_guard(self, tmp_path):
        """``force=True`` bypasses the freshness check even on hot checkpoints."""
        cp_dir = _prepare_checkpoint(tmp_path, mtime_age_seconds=5.0)

        with patch(
            "watercooler_mcp.daemons.state._daemon_dir", return_value=cp_dir
        ):
            payload = reset_decision_extractor_checkpoint(force=True)

        assert payload["status"] == "ok"
        assert "stop the daemon" in payload["note"].lower()

    def test_two_rapid_resets_produce_distinct_backups(self, tmp_path):
        """Backup filenames must use microsecond precision so two calls
        within the same wall-clock second cannot collide and clobber the
        first backup with the already-reset checkpoint.
        """
        cp_dir = _prepare_checkpoint(tmp_path, mtime_age_seconds=120.0)
        cp_path = cp_dir / "checkpoint.json"

        with patch(
            "watercooler_mcp.daemons.state._daemon_dir", return_value=cp_dir
        ):
            p1 = reset_decision_extractor_checkpoint()
            # Age the (now-reset) checkpoint past the active-daemon window
            # so the second call proceeds — simulating an operator running
            # reset twice in quick succession.
            stale = time.time() - 120.0
            os.utime(cp_path, (stale, stale))
            p2 = reset_decision_extractor_checkpoint()

        assert p1["status"] == "ok"
        assert p2["status"] == "ok"
        assert p1["backup_path"] != p2["backup_path"], (
            "two rapid resets must not collide on backup filename"
        )
        assert Path(p1["backup_path"]).exists()
        assert Path(p2["backup_path"]).exists()


_RESET_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "reset_decision_extractor.py"
)


class TestResetModuleRunnableStandalone:
    """The reset module backs the operator CLI script
    ``scripts/reset_decision_extractor.py``. It must be importable without
    the FastMCP server stack or the daemon-fleet deps (PR3a review): the
    point of moving the reset off the MCP surface is a lightweight operator
    tool. These run in fresh subprocess interpreters because the pytest
    process has already imported fastmcp/ulid via other tests."""

    def test_import_does_not_pull_heavy_deps(self):
        """Importing the reset module must not pull ``fastmcp`` (guards the
        lazy ``watercooler_mcp.__init__``) or ``ulid`` (guards the lazy
        daemon-class imports in ``watercooler_mcp.daemons.__init__``)."""
        code = (
            "import sys, watercooler_mcp.daemons.decision_extractor_reset; "
            "heavy = [d for d in ('fastmcp', 'ulid') if d in sys.modules]; "
            "assert not heavy, f'reset import pulled heavy deps: {heavy}'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_cli_help_runs(self):
        """``python scripts/reset_decision_extractor.py --help`` — the
        supported operator invocation — must succeed. The script's src/
        path shim makes it runnable directly from a repo checkout with the
        project interpreter."""
        result = subprocess.run(
            [sys.executable, str(_RESET_SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "decision_extractor" in result.stdout
