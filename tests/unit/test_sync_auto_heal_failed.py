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

    @patch("watercooler_mcp.sync.pull_ff_only", return_value=False)
    @patch(
        "watercooler_mcp.sync.get_parity_state",
        side_effect=["dirty_derived_only", "behind_only"],
    )
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_dirty_derived_then_behind_only_ff_fail_sets_flag(
        self, _fetch, _parity, _pull, tmp_path
    ):
        """Regression guard for post-PR #613 round-6 M2: when
        ``ensure_readable`` enters ``dirty_derived_only``, the
        cleanup loop re-checks parity and lands at ``behind_only``.
        If the subsequent ``pull_ff_only`` returns False, the flag
        must be set — mirroring the primary ``behind_only`` branch
        that already sets it. Prior code silently fell through with
        ``auto_heal_failed = False`` and no banner fired.
        """
        (tmp_path / ".git").mkdir()

        # Fake repo with a no-op cleanup pass. ``repo.git.status(...)``
        # returns empty so the cleanup loop is trivial; ``repo.git
        # .checkout(...)`` is never called because no derived files
        # match.
        fake_repo = MagicMock()
        fake_repo.git.status.return_value = ""

        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = fake_repo
            ok, _actions, parity, auto_heal_failed = ensure_readable(tmp_path)

        assert ok is True
        assert parity == "behind_only"
        assert auto_heal_failed is True, (
            "dirty_derived_only → behind_only → ff-fail must flag unresolved"
        )

    @patch("watercooler_mcp.sync.pull_ff_only", side_effect=RuntimeError("boom"))
    @patch(
        "watercooler_mcp.sync.get_parity_state",
        side_effect=["dirty_derived_only", "behind_only"],
    )
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_dirty_derived_then_behind_only_ff_raises_sets_flag(
        self, _fetch, _parity, _pull, tmp_path
    ):
        """Same path as above, but ``pull_ff_only`` raises instead of
        returning False. The flag must still be set; the outer
        exception handler must not swallow the signal into a generic
        ``derived cache cleanup failed`` message."""
        (tmp_path / ".git").mkdir()
        fake_repo = MagicMock()
        fake_repo.git.status.return_value = ""

        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = fake_repo
            ok, _actions, parity, auto_heal_failed = ensure_readable(tmp_path)

        assert ok is True
        assert parity == "behind_only"
        assert auto_heal_failed is True


_PROJECTION_REL = ".watercooler/slack-mappings/new-topic.json"


def _fake_projection_repo(*, origin_has_path: bool) -> MagicMock:
    """Mock Repo: one untracked slack-mapping dirty; branch + origin lookup wired."""
    repo = MagicMock()
    repo.git.status.return_value = f"?? {_PROJECTION_REL}"
    repo.head.is_detached = False
    repo.active_branch.name = "watercooler/threads"
    if origin_has_path:
        repo.git.cat_file.return_value = ""  # origin tracks the path
    else:
        repo.git.cat_file.side_effect = Exception("missing in origin")
    return repo


class TestEnsureReadableUntrackedProjection:
    """ensure_readable's dirty_derived_only heal exercises the real
    should_discard_dirty_entry: preserve an un-pushed sole-copy slack-mapping
    while still fast-forwarding, but discard one origin already tracks (#924)."""

    @patch("watercooler_mcp.sync.pull_ff_only", return_value=True)
    @patch(
        "watercooler_mcp.sync.get_parity_state",
        side_effect=["dirty_derived_only", "dirty_derived_only"],
    )
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_untracked_projection_absent_on_origin_preserved_and_pulls(
        self, _fetch, _parity, _pull, tmp_path
    ):
        (tmp_path / ".git").mkdir()
        mapping = tmp_path / _PROJECTION_REL
        mapping.parent.mkdir(parents=True)
        mapping.write_text('{"slackThreadTs": "1700000000.000100"}')
        fake_repo = _fake_projection_repo(origin_has_path=False)

        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = fake_repo
            ok, _actions, parity, auto_heal_failed = ensure_readable(tmp_path)

        assert mapping.exists()  # sole copy preserved, never unlinked
        fake_repo.git.cat_file.assert_called_once()  # origin consulted
        fake_repo.git.checkout.assert_not_called()  # preserved → skipped
        _pull.assert_called_once()  # behind state still cleared via ff-pull
        assert ok is True
        assert parity == "clean"
        assert auto_heal_failed is False

    @patch("watercooler_mcp.sync.pull_ff_only", return_value=True)
    @patch(
        "watercooler_mcp.sync.get_parity_state",
        side_effect=["dirty_derived_only", "behind_only"],
    )
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_untracked_projection_present_on_origin_discarded(
        self, _fetch, _parity, _pull, tmp_path
    ):
        (tmp_path / ".git").mkdir()
        mapping = tmp_path / _PROJECTION_REL
        mapping.parent.mkdir(parents=True)
        mapping.write_text('{"slackThreadTs": "local-divergent"}')
        fake_repo = _fake_projection_repo(origin_has_path=True)

        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = fake_repo
            ok, _actions, parity, auto_heal_failed = ensure_readable(tmp_path)

        assert not mapping.exists()  # discarded — origin holds the canonical copy
        fake_repo.git.cat_file.assert_called_once()
        _pull.assert_called_once()
        assert ok is True
        assert parity == "clean"
