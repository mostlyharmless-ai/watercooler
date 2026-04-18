"""Tests for sync/primitives.py - pure git operations."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from watercooler_mcp.sync import (
    # Errors
    SyncError,
    PullError,
    PushError,
    ConflictError,
    LockError,
    NetworkError,
    AuthenticationError,
    # Constants
    MAX_PUSH_RETRIES,
    MAX_BRANCH_LENGTH,
    INVALID_BRANCH_PATTERNS,
    # Primitives
    validate_branch_name,
    get_branch_name,
    is_detached_head,
    is_dirty,
    is_rebase_in_progress,
    has_conflicts,
    branch_exists_on_origin,
    get_ahead_behind,
    get_parity_state,
    ensure_readable,
    fetch_with_timeout,
    pull_ff_only,
    pull_rebase,
    push_with_retry,
    checkout_branch,
    detect_stash,
    stash_changes,
    restore_stash,
)


# =============================================================================
# Error Hierarchy Tests
# =============================================================================


class TestSyncErrors:
    """Tests for the error hierarchy."""

    def test_sync_error_basic(self):
        """Test basic SyncError creation."""
        err = SyncError(message="Test error")
        assert str(err) == "Test error"
        assert err.is_retryable is False
        assert err.context == {}

    def test_sync_error_with_context(self):
        """Test SyncError with context."""
        err = SyncError(
            message="Failed to push",
            context={"branch": "main", "remote": "origin"},
            recovery_hint="Try pulling first",
        )
        assert "branch='main'" in str(err)
        assert "Hint: Try pulling first" in str(err)

    def test_sync_error_with_context_method(self):
        """Test adding context via with_context."""
        err = SyncError(message="Error")
        err2 = err.with_context(branch="feature", repo="/path")
        assert err2.context == {"branch": "feature", "repo": "/path"}
        assert err.context == {}  # Original unchanged

    def test_pull_error_is_retryable(self):
        """Pull errors are retryable by default."""
        err = PullError(message="Network timeout")
        assert err.is_retryable is True

    def test_push_error_is_retryable(self):
        """Push errors are retryable by default."""
        err = PushError(message="Remote rejected")
        assert err.is_retryable is True

    def test_conflict_error_has_files(self):
        """ConflictError tracks conflicting files."""
        err = ConflictError(
            message="Merge conflict",
            conflicting_files=["a.py", "b.py", "c.py"],
        )
        assert "a.py" in str(err)
        assert err.is_retryable is False

    def test_conflict_error_truncates_long_file_list(self):
        """ConflictError truncates long file lists."""
        files = [f"file{i}.py" for i in range(10)]
        err = ConflictError(message="Conflict", conflicting_files=files)
        assert "+5 more" in str(err)

    def test_lock_error_has_holder(self):
        """LockError tracks lock holder."""
        err = LockError(
            message="Lock held",
            lock_path="/path/to/.locks/topic.lock",
            lock_holder="12345",
        )
        assert err.lock_holder == "12345"
        assert err.is_retryable is True

    def test_network_error_is_retryable(self):
        """NetworkError is retryable."""
        err = NetworkError(message="Connection refused", remote="origin")
        assert err.is_retryable is True

    def test_authentication_error_not_retryable(self):
        """AuthenticationError is not retryable and has hint."""
        err = AuthenticationError(message="Permission denied")
        assert err.is_retryable is False
        assert "SSH" in err.recovery_hint


# =============================================================================
# Validation Tests
# =============================================================================


class TestValidateBranchName:
    """Tests for branch name validation."""

    def test_valid_branch_names(self):
        """Valid branch names should pass."""
        valid_names = [
            "main",
            "feature/auth",
            "fix-123",
            "user/jane/feature",
            "release-1.0.0",
            "hotfix_urgent",
        ]
        for name in valid_names:
            validate_branch_name(name)  # Should not raise

    def test_empty_branch_name(self):
        """Empty branch names should fail."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_branch_name("")

    def test_branch_too_long(self):
        """Branch names over 255 chars should fail."""
        long_name = "a" * 256
        with pytest.raises(ValueError, match="too long"):
            validate_branch_name(long_name)

    def test_flag_injection(self):
        """Branch names starting with - should fail."""
        with pytest.raises(ValueError, match="flag injection"):
            validate_branch_name("-branch")
        with pytest.raises(ValueError, match="flag injection"):
            validate_branch_name("--delete")

    def test_consecutive_dots(self):
        """Branch names with .. should fail."""
        with pytest.raises(ValueError, match="consecutive dots"):
            validate_branch_name("branch..name")

    def test_consecutive_slashes(self):
        """Branch names with // should fail."""
        with pytest.raises(ValueError, match="consecutive slashes"):
            validate_branch_name("feature//branch")

    def test_trailing_slash(self):
        """Branch names ending with / should fail."""
        with pytest.raises(ValueError, match="end with slash"):
            validate_branch_name("feature/")

    def test_invalid_characters(self):
        """Branch names with invalid git chars should fail."""
        invalid_chars = ["~", "^", ":", "?", "*", "[", "]", "\\"]
        for char in invalid_chars:
            with pytest.raises(ValueError, match="invalid git characters"):
                validate_branch_name(f"branch{char}name")

    def test_lock_suffix(self):
        """Branch names ending in .lock should fail."""
        with pytest.raises(ValueError, match=".lock"):
            validate_branch_name("branch.lock")

    def test_reflog_syntax(self):
        """Branch names with @{ should fail."""
        with pytest.raises(ValueError, match="reflog syntax"):
            validate_branch_name("branch@{1}")

    def test_control_characters(self):
        """Branch names with control chars should fail."""
        with pytest.raises(ValueError, match="control characters"):
            validate_branch_name("branch\x00name")


# =============================================================================
# Branch Operation Tests (with mocked repos)
# =============================================================================


class TestBranchOperations:
    """Tests for branch operations with mocked repos."""

    def test_get_branch_name_normal(self):
        """Get branch name from normal repo."""
        repo = MagicMock()
        repo.head.is_detached = False
        repo.active_branch.name = "feature"
        assert get_branch_name(repo) == "feature"

    def test_get_branch_name_detached(self):
        """Get branch name from detached HEAD returns None."""
        repo = MagicMock()
        repo.head.is_detached = True
        assert get_branch_name(repo) is None

    def test_is_detached_head_true(self):
        """is_detached_head returns True for detached."""
        repo = MagicMock()
        repo.head.is_detached = True
        assert is_detached_head(repo) is True

    def test_is_detached_head_false(self):
        """is_detached_head returns False for attached."""
        repo = MagicMock()
        repo.head.is_detached = False
        assert is_detached_head(repo) is False

    def test_is_dirty_true(self):
        """is_dirty returns True for dirty repo."""
        repo = MagicMock()
        repo.is_dirty.return_value = True
        assert is_dirty(repo) is True

    def test_is_dirty_false(self):
        """is_dirty returns False for clean repo."""
        repo = MagicMock()
        repo.is_dirty.return_value = False
        assert is_dirty(repo) is False

    def test_is_rebase_in_progress(self, tmp_path):
        """is_rebase_in_progress detects rebase state."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        repo.git_dir = str(git_dir)

        # No rebase
        assert is_rebase_in_progress(repo) is False

        # Rebase-merge dir
        (git_dir / "rebase-merge").mkdir()
        assert is_rebase_in_progress(repo) is True

    def test_has_conflicts_no_conflicts(self):
        """has_conflicts returns False for clean status."""
        repo = MagicMock()
        repo.git.status.return_value = "M  file.py\n"
        assert has_conflicts(repo) is False

    def test_has_conflicts_with_conflicts(self):
        """has_conflicts returns True for conflict markers."""
        repo = MagicMock()
        repo.git.status.return_value = "UU file.py\n"
        assert has_conflicts(repo) is True


# =============================================================================
# Stash Operation Tests
# =============================================================================


class TestStashOperations:
    """Tests for stash operations."""

    def test_detect_stash_empty(self):
        """detect_stash returns False for no stashes."""
        repo = MagicMock()
        repo.git.stash.return_value = ""
        assert detect_stash(repo) is False

    def test_detect_stash_has_stash(self):
        """detect_stash returns True when stashes exist."""
        repo = MagicMock()
        repo.git.stash.return_value = "stash@{0}: On main: test"
        assert detect_stash(repo) is True

    def test_stash_changes_clean_repo(self):
        """stash_changes returns None for clean repo."""
        repo = MagicMock()
        repo.is_dirty.return_value = False
        assert stash_changes(repo) is None

    def test_stash_changes_dirty_repo(self):
        """stash_changes stashes and returns ref."""
        repo = MagicMock()
        repo.is_dirty.return_value = True
        repo.git.stash.return_value = "Saved working directory"

        result = stash_changes(repo)
        assert result is not None
        assert "watercooler-auto" in result
        repo.git.stash.assert_called_once()

    def test_restore_stash_none_ref(self):
        """restore_stash with None ref returns True (no-op)."""
        repo = MagicMock()
        assert restore_stash(repo, None) is True
        repo.git.stash.assert_not_called()

    def test_restore_stash_success(self):
        """restore_stash applies then drops stash on success."""
        repo = MagicMock()
        assert restore_stash(repo, "stash-ref") is True
        # Stash safety: apply first, then drop (not pop)
        assert repo.git.stash.call_args_list == [
            (("apply",), {}),
            (("drop",), {}),
        ]


# =============================================================================
# Constants Tests
# =============================================================================


class TestConstants:
    """Tests for module constants."""

    def test_max_push_retries(self):
        """MAX_PUSH_RETRIES is a reasonable value."""
        assert MAX_PUSH_RETRIES >= 1
        assert MAX_PUSH_RETRIES <= 10

    def test_max_branch_length(self):
        """MAX_BRANCH_LENGTH matches git's limit."""
        assert MAX_BRANCH_LENGTH == 255

    def test_invalid_branch_patterns_exist(self):
        """INVALID_BRANCH_PATTERNS has patterns."""
        assert len(INVALID_BRANCH_PATTERNS) > 0
        assert all(isinstance(p, str) for p in INVALID_BRANCH_PATTERNS)


# =============================================================================
# Parity State Classification Tests
# =============================================================================


class TestGetParityStateClassification:
    """Tests for get_parity_state classification priority."""

    def _make_repo(self, dirty_porcelain="", ahead=0, behind=0, has_remote=True):
        """Create a mock repo with configurable state."""
        repo = MagicMock()
        repo.head.is_detached = False
        repo.active_branch.name = "watercooler/threads"
        repo.git_dir = "/tmp/fake/.git"
        repo.is_dirty.return_value = bool(dirty_porcelain.strip())
        repo.git.status.return_value = dirty_porcelain

        if has_remote:
            repo.commit.return_value = MagicMock()
            ahead_commits = [MagicMock() for _ in range(ahead)]
            behind_commits = [MagicMock() for _ in range(behind)]

            def iter_commits_side_effect(rev_range):
                if ".." in rev_range:
                    left, right = rev_range.split("..")
                    if left.startswith("origin/"):
                        return behind_commits
                    return ahead_commits
                return []

            repo.iter_commits.side_effect = iter_commits_side_effect
        else:
            repo.commit.side_effect = Exception("bad revision")

        return repo

    @patch("watercooler_mcp.sync.primitives.is_rebase_in_progress", return_value=False)
    @patch("watercooler_mcp.sync.primitives.is_dirty")
    def test_diverged_trumps_dirty_derived(self, mock_dirty, _mock_rebase):
        """diverged must be returned even when dirty files are all derived."""
        mock_dirty.return_value = True
        repo = self._make_repo(
            dirty_porcelain=" M threads/t1/annotation_state.json\n M threads/t2/annotation_state.json",
            ahead=2, behind=20,
        )
        assert get_parity_state(repo) == "diverged"

    @patch("watercooler_mcp.sync.primitives.is_rebase_in_progress", return_value=False)
    @patch("watercooler_mcp.sync.primitives.is_dirty")
    def test_diverged_trumps_dirty_mixed(self, mock_dirty, _mock_rebase):
        """diverged must be returned even with non-derived dirty files."""
        mock_dirty.return_value = True
        repo = self._make_repo(
            dirty_porcelain=" M threads/t1/annotation_state.json\n M threads/t1/thread.json",
            ahead=2, behind=20,
        )
        assert get_parity_state(repo) == "diverged"

    @patch("watercooler_mcp.sync.primitives.is_rebase_in_progress", return_value=False)
    @patch("watercooler_mcp.sync.primitives.is_dirty")
    def test_dirty_derived_only_when_not_diverged(self, mock_dirty, _mock_rebase):
        """dirty_derived_only returned when dirty-derived but NOT diverged."""
        mock_dirty.return_value = True
        repo = self._make_repo(
            dirty_porcelain=" M threads/t1/annotation_state.json",
            ahead=0, behind=0,
        )
        assert get_parity_state(repo) == "dirty_derived_only"

    @patch("watercooler_mcp.sync.primitives.is_rebase_in_progress", return_value=False)
    @patch("watercooler_mcp.sync.primitives.is_dirty")
    def test_dirty_mixed_when_behind(self, mock_dirty, _mock_rebase):
        """dirty_mixed returned when dirty-mixed but NOT diverged."""
        mock_dirty.return_value = True
        repo = self._make_repo(
            dirty_porcelain=" M threads/t1/annotation_state.json\n M threads/t1/thread.json",
            ahead=0, behind=3,
        )
        assert get_parity_state(repo) == "dirty_mixed"

    @patch("watercooler_mcp.sync.primitives.is_rebase_in_progress", return_value=False)
    @patch("watercooler_mcp.sync.primitives.is_dirty")
    def test_clean_when_clean(self, mock_dirty, _mock_rebase):
        """clean returned when no dirty, no ahead/behind."""
        mock_dirty.return_value = False
        repo = self._make_repo(ahead=0, behind=0)
        assert get_parity_state(repo) == "clean"

    @patch("watercooler_mcp.sync.primitives.is_rebase_in_progress", return_value=False)
    @patch("watercooler_mcp.sync.primitives.is_dirty")
    def test_conflict_markers_without_rebase_head_return_stuck(self, mock_dirty, _mock_rebase):
        """stuck_rebase_or_merge returned when index has conflict markers but no REBASE_HEAD.

        Regression for #556: a stash-pop conflict on a derived file (e.g. annotation_state.json)
        left DU markers in the index after the rebase completed and removed REBASE_HEAD.
        The dirty-file classifier only checked filenames, so it classified the conflicted
        derived file as dirty_derived_only (safe) instead of stuck_rebase_or_merge.
        """
        mock_dirty.return_value = True
        # DU = deleted by us, unmerged — classic stash-pop conflict on a derived file
        repo = self._make_repo(
            dirty_porcelain="DU graph/baseline/threads/foo/annotation_state.json",
            ahead=0, behind=0,
        )
        assert get_parity_state(repo) == "stuck_rebase_or_merge"

    @patch("watercooler_mcp.sync.primitives.is_rebase_in_progress", return_value=False)
    @patch("watercooler_mcp.sync.primitives.is_dirty")
    def test_conflict_markers_on_non_derived_file_return_stuck(self, mock_dirty, _mock_rebase):
        """stuck_rebase_or_merge returned for UU conflict on a non-derived file."""
        mock_dirty.return_value = True
        repo = self._make_repo(
            dirty_porcelain="UU graph/baseline/threads/foo/thread.json",
            ahead=0, behind=0,
        )
        assert get_parity_state(repo) == "stuck_rebase_or_merge"


# =============================================================================
# ensure_readable Diverged Reconciliation Tests
# =============================================================================


class TestEnsureReadableDiverged:
    """Tests for ensure_readable diverged reconciliation."""

    @patch("watercooler_mcp.sync.push_with_retry", return_value=True)
    @patch("watercooler_mcp.sync.pull_rebase", return_value=True)
    @patch("watercooler_mcp.sync.stash_changes", return_value=None)
    @patch("watercooler_mcp.sync.is_dirty", return_value=False)
    @patch("watercooler_mcp.sync.get_parity_state")
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_diverged_triggers_rebase_and_push(
        self, _mock_fetch, mock_parity, _mock_dirty,
        _mock_stash, mock_rebase, mock_push, tmp_path,
    ):
        """ensure_readable rebases and pushes when diverged."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        mock_parity.side_effect = ["diverged", "clean"]

        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = MagicMock()
            ok, actions, parity, _auto_heal_failed = ensure_readable(tmp_path)

        assert ok is True
        assert "rebased onto remote" in actions
        assert "pushed" in actions
        assert parity == "clean"
        mock_rebase.assert_called_once()
        mock_push.assert_called_once()

    @patch("watercooler_mcp.sync.restore_stash", return_value=True)
    @patch("watercooler_mcp.sync.pull_rebase", return_value=False)
    @patch("watercooler_mcp.sync.stash_changes", return_value="watercooler-auto-2026")
    @patch("watercooler_mcp.sync.is_dirty", return_value=False)
    @patch("watercooler_mcp.sync.get_parity_state")
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_diverged_rebase_failure_restores_stash(
        self, _mock_fetch, mock_parity, _mock_dirty,
        _mock_stash, _mock_rebase, mock_restore, tmp_path,
    ):
        """When rebase fails, stash is restored and read still succeeds."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        # Two parity calls: initial check + re-sample after stash restore
        mock_parity.side_effect = ["diverged", "dirty_mixed"]

        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = MagicMock()
            ok, actions, parity, _auto_heal_failed = ensure_readable(tmp_path)

        assert ok is True
        assert any("rebase failed" in a for a in actions)
        assert "restored stash" in actions
        mock_restore.assert_called_once()

    @patch("watercooler_mcp.sync.get_parity_state", return_value="diverged")
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=False)
    def test_diverged_skips_reconciliation_on_fetch_failure(
        self, _mock_fetch, _mock_parity, tmp_path,
    ):
        """When fetch fails, diverged reconciliation is skipped (stale refs)."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = MagicMock()
            with patch("watercooler_mcp.sync.pull_rebase") as mock_rebase:
                ok, actions, parity, _auto_heal_failed = ensure_readable(tmp_path)

        assert ok is True
        assert parity == "diverged"
        assert any("fetch failed" in a or "skipping reconciliation" in a for a in actions)
        mock_rebase.assert_not_called()

    @patch("watercooler_mcp.sync.push_with_retry", return_value=False)
    @patch("watercooler_mcp.sync.pull_rebase", return_value=True)
    @patch("watercooler_mcp.sync.stash_changes", return_value=None)
    @patch("watercooler_mcp.sync.is_dirty", return_value=False)
    @patch("watercooler_mcp.sync.get_parity_state")
    @patch("watercooler_mcp.sync.fetch_with_timeout", return_value=True)
    def test_diverged_push_failure_non_fatal(
        self, _mock_fetch, mock_parity, _mock_dirty,
        _mock_stash, _mock_rebase, _mock_push, tmp_path,
    ):
        """Push failure after rebase is non-fatal — read still succeeds."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        mock_parity.side_effect = ["diverged", "ahead_only"]

        with patch("git.Repo") as MockRepo:
            MockRepo.return_value = MagicMock()
            ok, actions, parity, _auto_heal_failed = ensure_readable(tmp_path)

        assert ok is True
        assert "rebased onto remote" in actions
        assert any("push failed" in a for a in actions)
