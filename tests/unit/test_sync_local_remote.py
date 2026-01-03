"""Tests for sync/local_remote.py module."""

from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from watercooler_mcp.sync.local_remote import (
    # Data classes
    PullResult,
    CommitResult,
    PushResult,
    SyncResult,
    SyncStatus,
    # Classes
    LocalRemoteSyncManager,
)
from watercooler_mcp.sync.errors import SyncError


# =============================================================================
# Test PullResult
# =============================================================================


class TestPullResult:
    """Tests for PullResult dataclass."""

    def test_success_result(self):
        """Test successful pull result."""
        result = PullResult(success=True, strategy="rebase", commits_pulled=5)
        assert result.success is True
        assert result.strategy == "rebase"
        assert result.commits_pulled == 5
        assert result.stash_used is False
        assert result.error is None

    def test_failure_result(self):
        """Test failed pull result."""
        result = PullResult(success=False, error="Merge conflict")
        assert result.success is False
        assert result.error == "Merge conflict"

    def test_default_values(self):
        """Test default values."""
        result = PullResult(success=True)
        assert result.strategy == "ff-only"
        assert result.commits_pulled == 0
        assert result.stash_used is False

    def test_stash_used(self):
        """Test result with stash used."""
        result = PullResult(success=True, stash_used=True)
        assert result.stash_used is True


# =============================================================================
# Test CommitResult
# =============================================================================


class TestCommitResult:
    """Tests for CommitResult dataclass."""

    def test_success_result(self):
        """Test successful commit result."""
        result = CommitResult(
            success=True,
            commit_sha="abc123def456",
            files_committed=["file1.py", "file2.py"],
        )
        assert result.success is True
        assert result.commit_sha == "abc123def456"
        assert result.files_committed == ["file1.py", "file2.py"]
        assert result.error is None

    def test_failure_result(self):
        """Test failed commit result."""
        result = CommitResult(success=False, error="Nothing to commit")
        assert result.success is False
        assert result.error == "Nothing to commit"

    def test_default_values(self):
        """Test default values."""
        result = CommitResult(success=True)
        assert result.commit_sha is None
        assert result.files_committed == []


# =============================================================================
# Test PushResult
# =============================================================================


class TestPushResult:
    """Tests for PushResult dataclass."""

    def test_success_result(self):
        """Test successful push result."""
        result = PushResult(success=True, commits_pushed=3)
        assert result.success is True
        assert result.commits_pushed == 3
        assert result.retries == 0
        assert result.error is None

    def test_failure_result(self):
        """Test failed push result."""
        result = PushResult(
            success=False,
            retries=5,
            error="Push rejected",
        )
        assert result.success is False
        assert result.retries == 5
        assert result.error == "Push rejected"

    def test_default_values(self):
        """Test default values."""
        result = PushResult(success=True)
        assert result.commits_pushed == 0
        assert result.retries == 0


# =============================================================================
# Test SyncResult
# =============================================================================


class TestSyncResult:
    """Tests for SyncResult dataclass."""

    def test_success_result(self):
        """Test successful sync result."""
        commit_result = CommitResult(success=True, commit_sha="abc123")
        push_result = PushResult(success=True, commits_pushed=1)
        result = SyncResult(
            success=True,
            commit_result=commit_result,
            push_result=push_result,
        )
        assert result.success is True
        assert result.commit_result is commit_result
        assert result.push_result is push_result
        assert result.timestamp is not None

    def test_failure_result(self):
        """Test failed sync result."""
        commit_result = CommitResult(success=False, error="Failed")
        result = SyncResult(success=False, commit_result=commit_result)
        assert result.success is False
        assert result.push_result is None

    def test_timestamp_auto_generated(self):
        """Test that timestamp is auto-generated."""
        result = SyncResult(success=True)
        assert result.timestamp is not None
        assert "T" in result.timestamp  # ISO format


# =============================================================================
# Test SyncStatus
# =============================================================================


class TestSyncStatus:
    """Tests for SyncStatus dataclass."""

    def test_default_values(self):
        """Test default status values."""
        status = SyncStatus()
        assert status.branch is None
        assert status.ahead == 0
        assert status.behind == 0
        assert status.is_clean is True
        assert status.has_conflicts is False
        assert status.is_detached is False
        assert status.is_rebasing is False
        assert status.can_push is False
        assert status.can_pull is False

    def test_custom_values(self):
        """Test custom status values."""
        status = SyncStatus(
            branch="main",
            ahead=5,
            behind=3,
            is_clean=False,
            has_conflicts=True,
            is_detached=False,
            is_rebasing=False,
            can_push=False,
            can_pull=False,
        )
        assert status.branch == "main"
        assert status.ahead == 5
        assert status.behind == 3
        assert status.is_clean is False
        assert status.has_conflicts is True

    def test_can_push_logic(self):
        """Test can_push field."""
        # Can push when ahead and no blockers
        status = SyncStatus(
            branch="main",
            ahead=1,
            is_detached=False,
            has_conflicts=False,
            is_rebasing=False,
            can_push=True,
        )
        assert status.can_push is True

    def test_can_pull_logic(self):
        """Test can_pull field."""
        # Can pull when behind and no blockers
        status = SyncStatus(
            branch="main",
            behind=1,
            is_detached=False,
            has_conflicts=False,
            is_rebasing=False,
            can_pull=True,
        )
        assert status.can_pull is True


# =============================================================================
# Test LocalRemoteSyncManager
# =============================================================================


class TestLocalRemoteSyncManager:
    """Tests for LocalRemoteSyncManager class."""

    @pytest.fixture
    def temp_repo(self, tmp_path):
        """Create a temporary directory for testing."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        return repo_path

    @pytest.fixture
    def mock_repo(self):
        """Create a mock git repository."""
        repo = MagicMock()
        repo.head.is_detached = False
        repo.active_branch.name = "main"
        repo.remotes.origin = MagicMock()
        return repo

    def test_init_basic(self, temp_repo):
        """Test basic initialization."""
        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        assert manager.repo_path == temp_repo
        assert manager.remote == "origin"
        assert manager.auto_stash is True
        assert manager.max_push_retries == 5

    def test_init_custom(self, temp_repo):
        """Test initialization with custom settings."""
        manager = LocalRemoteSyncManager(
            repo_path=temp_repo,
            remote="upstream",
            auto_stash=False,
            max_push_retries=10,
        )
        assert manager.remote == "upstream"
        assert manager.auto_stash is False
        assert manager.max_push_retries == 10

    @patch("watercooler_mcp.sync.local_remote.Repo")
    def test_repo_property_cached(self, mock_repo_class, temp_repo):
        """Test that repo property is cached."""
        mock_instance = MagicMock()
        mock_repo_class.return_value = mock_instance

        manager = LocalRemoteSyncManager(repo_path=temp_repo)

        # Access repo twice
        repo1 = manager.repo
        repo2 = manager.repo

        # Should only create once
        assert mock_repo_class.call_count == 1
        assert repo1 is repo2

    @patch("watercooler_mcp.sync.local_remote.Repo")
    def test_repo_property_invalid_repo(self, mock_repo_class, temp_repo):
        """Test repo property with invalid repository."""
        from git.exc import InvalidGitRepositoryError
        mock_repo_class.side_effect = InvalidGitRepositoryError("Invalid")

        manager = LocalRemoteSyncManager(repo_path=temp_repo)

        with pytest.raises(SyncError) as exc_info:
            _ = manager.repo

        assert "Invalid git repository" in str(exc_info.value)

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.is_detached_head")
    @patch("watercooler_mcp.sync.local_remote.is_dirty")
    @patch("watercooler_mcp.sync.local_remote.is_rebase_in_progress")
    @patch("watercooler_mcp.sync.local_remote.has_conflicts")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    @patch("watercooler_mcp.sync.local_remote.fetch_with_timeout")
    def test_get_sync_status(
        self,
        mock_fetch,
        mock_ahead_behind,
        mock_conflicts,
        mock_rebasing,
        mock_dirty,
        mock_detached,
        mock_branch,
        mock_repo_class,
        temp_repo,
    ):
        """Test get_sync_status method."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = "main"
        mock_detached.return_value = False
        mock_dirty.return_value = False
        mock_rebasing.return_value = False
        mock_conflicts.return_value = False
        mock_ahead_behind.return_value = (2, 1)

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        status = manager.get_sync_status()

        assert status.branch == "main"
        assert status.ahead == 2
        assert status.behind == 1
        assert status.is_clean is True
        assert status.has_conflicts is False
        assert status.is_detached is False
        assert status.is_rebasing is False
        assert status.can_push is True
        assert status.can_pull is True

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    @patch("watercooler_mcp.sync.local_remote.is_dirty")
    @patch("watercooler_mcp.sync.local_remote.pull_ff_only")
    def test_pull_ff_only(
        self,
        mock_pull,
        mock_dirty,
        mock_ahead_behind,
        mock_branch,
        mock_repo_class,
        temp_repo,
    ):
        """Test pull with ff-only strategy."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = "main"
        mock_ahead_behind.return_value = (0, 3)
        mock_dirty.return_value = False
        mock_pull.return_value = True

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.pull(strategy="ff-only")

        assert result.success is True
        assert result.strategy == "ff-only"
        assert result.commits_pulled == 3
        mock_pull.assert_called_once()

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    @patch("watercooler_mcp.sync.local_remote.is_dirty")
    @patch("watercooler_mcp.sync.local_remote.pull_rebase")
    def test_pull_rebase(
        self,
        mock_pull,
        mock_dirty,
        mock_ahead_behind,
        mock_branch,
        mock_repo_class,
        temp_repo,
    ):
        """Test pull with rebase strategy."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = "main"
        mock_ahead_behind.return_value = (0, 2)
        mock_dirty.return_value = False
        mock_pull.return_value = True

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.pull(strategy="rebase")

        assert result.success is True
        assert result.strategy == "rebase"
        mock_pull.assert_called_once()

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    def test_pull_detached_head(self, mock_branch, mock_repo_class, temp_repo):
        """Test pull in detached HEAD state."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = None

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.pull()

        assert result.success is False
        assert "detached HEAD" in result.error

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    def test_pull_already_synced(
        self, mock_ahead_behind, mock_branch, mock_repo_class, temp_repo
    ):
        """Test pull when already synced."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = "main"
        mock_ahead_behind.return_value = (0, 0)

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.pull()

        assert result.success is True
        assert result.commits_pulled == 0

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    @patch("watercooler_mcp.sync.local_remote.push_with_retry")
    def test_push_success(
        self, mock_push, mock_ahead_behind, mock_branch, mock_repo_class, temp_repo
    ):
        """Test successful push."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = "main"
        mock_ahead_behind.return_value = (3, 0)
        mock_push.return_value = True

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.push()

        assert result.success is True
        assert result.commits_pushed == 3

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    def test_push_detached_head(self, mock_branch, mock_repo_class, temp_repo):
        """Test push in detached HEAD state."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = None

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.push()

        assert result.success is False
        assert "detached HEAD" in result.error

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    def test_push_nothing_to_push(
        self, mock_ahead_behind, mock_branch, mock_repo_class, temp_repo
    ):
        """Test push when nothing to push."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = "main"
        mock_ahead_behind.return_value = (0, 0)

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.push()

        assert result.success is True
        assert result.commits_pushed == 0

    @patch("watercooler_mcp.sync.local_remote.Repo")
    def test_commit_success(self, mock_repo_class, temp_repo):
        """Test successful commit."""
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = [MagicMock(a_path="file1.py")]
        mock_repo.untracked_files = []
        mock_commit = MagicMock()
        mock_commit.hexsha = "abcdef123456789"
        mock_repo.index.commit.return_value = mock_commit
        mock_repo_class.return_value = mock_repo

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.commit("Test commit", files=["file1.py"])

        assert result.success is True
        assert result.commit_sha == "abcdef123456"
        mock_repo.index.add.assert_called_once_with(["file1.py"])

    @patch("watercooler_mcp.sync.local_remote.Repo")
    def test_commit_nothing_to_commit(self, mock_repo_class, temp_repo):
        """Test commit with nothing to commit."""
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = []
        mock_repo.untracked_files = []
        mock_repo_class.return_value = mock_repo

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.commit("Test commit")

        assert result.success is False
        assert "Nothing to commit" in result.error

    @patch("watercooler_mcp.sync.local_remote.Repo")
    def test_commit_all_changes(self, mock_repo_class, temp_repo):
        """Test commit with all_changes flag."""
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = [MagicMock(a_path="file1.py")]
        mock_repo.untracked_files = []
        mock_commit = MagicMock()
        mock_commit.hexsha = "abcdef123456789"
        mock_repo.index.commit.return_value = mock_commit
        mock_repo_class.return_value = mock_repo

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.commit("Test commit", all_changes=True)

        assert result.success is True
        mock_repo.git.add.assert_called_once_with("-A")

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    @patch("watercooler_mcp.sync.local_remote.push_with_retry")
    def test_commit_and_push_success(
        self, mock_push, mock_ahead_behind, mock_branch, mock_repo_class, temp_repo
    ):
        """Test successful commit and push."""
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = [MagicMock(a_path="file1.py")]
        mock_repo.untracked_files = []
        mock_commit = MagicMock()
        mock_commit.hexsha = "abcdef123456789"
        mock_repo.index.commit.return_value = mock_commit
        mock_repo_class.return_value = mock_repo
        mock_branch.return_value = "main"
        mock_ahead_behind.return_value = (1, 0)
        mock_push.return_value = True

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.commit_and_push("Test commit", files=["file1.py"])

        assert result.success is True
        assert result.commit_result.success is True
        assert result.push_result.success is True

    @patch("watercooler_mcp.sync.local_remote.Repo")
    def test_commit_and_push_commit_fails(self, mock_repo_class, temp_repo):
        """Test commit_and_push when commit fails."""
        mock_repo = MagicMock()
        mock_repo.index.diff.return_value = []
        mock_repo.untracked_files = []
        mock_repo_class.return_value = mock_repo

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        result = manager.commit_and_push("Test commit")

        assert result.success is False
        assert result.commit_result.success is False
        assert result.push_result is None


# =============================================================================
# Test ensure_synced
# =============================================================================


class TestEnsureSynced:
    """Tests for ensure_synced method."""

    @pytest.fixture
    def temp_repo(self, tmp_path):
        """Create a temporary directory for testing."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        return repo_path

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.is_detached_head")
    @patch("watercooler_mcp.sync.local_remote.is_dirty")
    @patch("watercooler_mcp.sync.local_remote.is_rebase_in_progress")
    @patch("watercooler_mcp.sync.local_remote.has_conflicts")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    @patch("watercooler_mcp.sync.local_remote.fetch_with_timeout")
    def test_already_synced(
        self,
        mock_fetch,
        mock_ahead_behind,
        mock_conflicts,
        mock_rebasing,
        mock_dirty,
        mock_detached,
        mock_branch,
        mock_repo_class,
        temp_repo,
    ):
        """Test ensure_synced when already synced."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = "main"
        mock_detached.return_value = False
        mock_dirty.return_value = False
        mock_rebasing.return_value = False
        mock_conflicts.return_value = False
        mock_ahead_behind.return_value = (0, 0)

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        success, actions = manager.ensure_synced()

        assert success is True
        assert "Already synced" in actions

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.is_detached_head")
    @patch("watercooler_mcp.sync.local_remote.is_dirty")
    @patch("watercooler_mcp.sync.local_remote.is_rebase_in_progress")
    @patch("watercooler_mcp.sync.local_remote.has_conflicts")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    @patch("watercooler_mcp.sync.local_remote.fetch_with_timeout")
    def test_detached_head_error(
        self,
        mock_fetch,
        mock_ahead_behind,
        mock_conflicts,
        mock_rebasing,
        mock_dirty,
        mock_detached,
        mock_branch,
        mock_repo_class,
        temp_repo,
    ):
        """Test ensure_synced in detached HEAD state."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = "main"
        mock_detached.return_value = True
        mock_dirty.return_value = False
        mock_rebasing.return_value = False
        mock_conflicts.return_value = False
        mock_ahead_behind.return_value = (0, 0)

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        success, actions = manager.ensure_synced()

        assert success is False
        assert any("detached HEAD" in a for a in actions)

    @patch("watercooler_mcp.sync.local_remote.Repo")
    @patch("watercooler_mcp.sync.local_remote.get_branch_name")
    @patch("watercooler_mcp.sync.local_remote.is_detached_head")
    @patch("watercooler_mcp.sync.local_remote.is_dirty")
    @patch("watercooler_mcp.sync.local_remote.is_rebase_in_progress")
    @patch("watercooler_mcp.sync.local_remote.has_conflicts")
    @patch("watercooler_mcp.sync.local_remote.get_ahead_behind")
    @patch("watercooler_mcp.sync.local_remote.fetch_with_timeout")
    def test_conflicts_error(
        self,
        mock_fetch,
        mock_ahead_behind,
        mock_conflicts,
        mock_rebasing,
        mock_dirty,
        mock_detached,
        mock_branch,
        mock_repo_class,
        temp_repo,
    ):
        """Test ensure_synced with conflicts."""
        mock_repo_class.return_value = MagicMock()
        mock_branch.return_value = "main"
        mock_detached.return_value = False
        mock_dirty.return_value = False
        mock_rebasing.return_value = False
        mock_conflicts.return_value = True
        mock_ahead_behind.return_value = (0, 0)

        manager = LocalRemoteSyncManager(repo_path=temp_repo)
        success, actions = manager.ensure_synced()

        assert success is False
        assert any("conflicts" in a for a in actions)
