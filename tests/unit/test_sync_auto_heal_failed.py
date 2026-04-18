"""Unit tests for the ``auto_heal_failed`` extension to the sync module
(Bug #4 part 2, plan v4).

Covers:
- ``ensure_readable`` returns a 4-tuple with ``auto_heal_failed``.
- The flag is True only when parity was ``behind_only`` at entry AND
  ``pull_ff_only()`` returned False (or raised).
- ``format_parity_warning`` emits a targeted banner for
  ``(parity="behind_only", auto_heal_failed=True)`` without adding
  ``behind_only`` to ``_WARN_PARITY_STATES`` (which would also fire
  in the common successful ff-only path).
- The canonical parity vocabulary is preserved — no synthetic states.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.sync import (
    _WARN_PARITY_STATES,
    ensure_readable,
    format_parity_warning,
)


class TestFormatParityWarningAutoHealFailed:
    """Parity vocabulary unchanged. New behavior lives in the flag."""

    def test_behind_only_with_auto_heal_failed_emits_banner(self):
        msg = format_parity_warning("behind_only", auto_heal_failed=True)
        assert msg
        assert "behind origin" in msg
        assert "auto-heal could not fast-forward" in msg
        assert "watercooler_sync_repair" in msg

    def test_behind_only_without_flag_stays_silent(self):
        """The common successful ff-only path — ``behind_only`` is
        intentionally NOT in ``_WARN_PARITY_STATES`` because the
        auto-heal usually succeeds and flips parity to ``clean``
        before this is called. Without the flag, no banner."""
        assert format_parity_warning("behind_only") == ""
        assert format_parity_warning("behind_only", auto_heal_failed=False) == ""

    def test_other_states_ignore_the_flag(self):
        """The flag is a narrow signal only for behind_only. Passing
        it for unrelated states must not spuriously change their
        message. Each state has exactly one banner string."""
        for state in ("diverged", "dirty_mixed", "stuck_rebase_or_merge", "auth_or_network_error"):
            with_flag = format_parity_warning(state, auto_heal_failed=True)
            without_flag = format_parity_warning(state)
            assert with_flag == without_flag, f"state={state}"

    def test_canonical_vocabulary_preserved(self):
        """_WARN_PARITY_STATES must continue to hold the same four
        states as before this PR. Regression guard — the v4 plan
        explicitly rejects synthetic states like ``behind_only_unresolved``."""
        assert _WARN_PARITY_STATES == frozenset(
            {"diverged", "dirty_mixed", "stuck_rebase_or_merge", "auth_or_network_error"}
        )


class TestEnsureReadableAutoHealFailed:
    """``ensure_readable`` returns ``auto_heal_failed=True`` when
    parity is ``behind_only`` at entry AND ``pull_ff_only`` fails."""

    @patch("watercooler_mcp.sync.pull_ff_only", return_value=False)
    @patch("watercooler_mcp.sync.get_parity_state", return_value="behind_only")
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_behind_only_ff_returns_false_sets_flag(
        self, _fetch, _parity, _pull, tmp_path
    ):
        (tmp_path / ".git").mkdir()
        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = MagicMock()
            ok, _actions, parity, auto_heal_failed = ensure_readable(tmp_path)
        assert ok is True
        assert parity == "behind_only"  # canonical, unchanged
        assert auto_heal_failed is True

    @patch("watercooler_mcp.sync.pull_ff_only", side_effect=RuntimeError("boom"))
    @patch("watercooler_mcp.sync.get_parity_state", return_value="behind_only")
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_behind_only_ff_raises_sets_flag(
        self, _fetch, _parity, _pull, tmp_path
    ):
        (tmp_path / ".git").mkdir()
        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = MagicMock()
            ok, _actions, parity, auto_heal_failed = ensure_readable(tmp_path)
        assert ok is True
        assert parity == "behind_only"
        assert auto_heal_failed is True

    @patch("watercooler_mcp.sync.pull_ff_only", return_value=True)
    @patch("watercooler_mcp.sync.get_parity_state", return_value="behind_only")
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_behind_only_ff_succeeds_clears_flag(
        self, _fetch, _parity, _pull, tmp_path
    ):
        """Common case: ff-only succeeds, parity flips to ``clean``,
        flag stays False. No banner should fire downstream."""
        (tmp_path / ".git").mkdir()
        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = MagicMock()
            ok, _actions, parity, auto_heal_failed = ensure_readable(tmp_path)
        assert ok is True
        assert parity == "clean"
        assert auto_heal_failed is False

    @patch("watercooler_mcp.sync.get_parity_state", return_value="clean")
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_non_behind_state_leaves_flag_false(
        self, _fetch, _parity, tmp_path
    ):
        (tmp_path / ".git").mkdir()
        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = MagicMock()
            _ok, _actions, _parity, auto_heal_failed = ensure_readable(tmp_path)
        assert auto_heal_failed is False

    def test_missing_threads_dir_returns_four_tuple(self, tmp_path):
        """Fast path for non-existent threads_dir must still return
        the new 4-tuple shape so every caller can unpack safely."""
        result = ensure_readable(tmp_path / "does-not-exist")
        assert len(result) == 4
        ok, _actions, parity, auto_heal_failed = result
        assert ok is True
        assert parity == "clean"
        assert auto_heal_failed is False
