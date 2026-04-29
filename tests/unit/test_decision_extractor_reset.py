"""Tests for ``_decision_extractor_reset_impl`` admin tool.

Covers the active-daemon guard added for the checkpoint-reset race: the
extractor daemon holds its checkpoint in memory and writes it after every
tick, so a reset issued while the daemon is active is silently overwritten.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from watercooler_mcp.tools.daemon import _decision_extractor_reset_impl


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
            result = _decision_extractor_reset_impl(ctx=MagicMock())

        payload = json.loads(result)
        assert payload["status"] == "active_daemon"
        assert payload["checkpoint_age_seconds"] < 60.0
        assert "force=True" in payload["message"]

    def test_proceeds_when_checkpoint_is_stale(self, tmp_path):
        """A checkpoint older than the active-daemon window resets normally."""
        cp_dir = _prepare_checkpoint(tmp_path, mtime_age_seconds=120.0)

        with patch(
            "watercooler_mcp.daemons.state._daemon_dir", return_value=cp_dir
        ):
            result = _decision_extractor_reset_impl(ctx=MagicMock())

        payload = json.loads(result)
        assert payload["status"] == "ok"
        assert payload["before"]["processed_finding_ids"] == 2

    def test_force_overrides_active_daemon_guard(self, tmp_path):
        """``force=True`` bypasses the freshness check even on hot checkpoints."""
        cp_dir = _prepare_checkpoint(tmp_path, mtime_age_seconds=5.0)

        with patch(
            "watercooler_mcp.daemons.state._daemon_dir", return_value=cp_dir
        ):
            result = _decision_extractor_reset_impl(ctx=MagicMock(), force=True)

        payload = json.loads(result)
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
            result1 = _decision_extractor_reset_impl(ctx=MagicMock())
            # Age the (now-reset) checkpoint past the active-daemon window
            # so the second call proceeds — simulating an operator running
            # reset twice in quick succession.
            stale = time.time() - 120.0
            os.utime(cp_path, (stale, stale))
            result2 = _decision_extractor_reset_impl(ctx=MagicMock())

        p1 = json.loads(result1)
        p2 = json.loads(result2)
        assert p1["status"] == "ok"
        assert p2["status"] == "ok"
        assert p1["backup_path"] != p2["backup_path"], (
            "two rapid resets must not collide on backup filename"
        )
        assert Path(p1["backup_path"]).exists()
        assert Path(p2["backup_path"]).exists()
