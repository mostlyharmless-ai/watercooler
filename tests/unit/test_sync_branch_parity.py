"""Tests for sync/branch_parity.py module."""

from dataclasses import fields
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from watercooler_mcp.sync.branch_parity import (
    # Enums
    StateClass,
    # Data classes
    PreflightResult,
    BranchPairingResult,
    # Classes
    BranchParityManager,
)
from watercooler_mcp.sync.state import ParityStatus, ParityState
from watercooler_mcp.sync.errors import BranchPairingError


# =============================================================================
# Test StateClass Enum
# =============================================================================


class TestStateClass:
    """Tests for StateClass enum."""

    def test_ready_states(self):
        """Test ready state values."""
        assert StateClass.READY.value == "ready"
        assert StateClass.READY_DIRTY.value == "ready_dirty"

    def test_behind_states(self):
        """Test behind state values."""
        assert StateClass.BEHIND_CLEAN.value == "behind_clean"
        assert StateClass.BEHIND_DIRTY.value == "behind_dirty"

    def test_ahead_states(self):
        """Test ahead state values."""
        assert StateClass.AHEAD.value == "ahead"
        assert StateClass.AHEAD_DIRTY.value == "ahead_dirty"

    def test_diverged_states(self):
        """Test diverged state values."""
        assert StateClass.DIVERGED_CLEAN.value == "diverged_clean"
        assert StateClass.DIVERGED_DIRTY.value == "diverged_dirty"

    def test_branch_mismatch_states(self):
        """Test branch mismatch state values."""
        assert StateClass.BRANCH_MISMATCH.value == "branch_mismatch"
        assert StateClass.BRANCH_MISMATCH_DIRTY.value == "branch_mismatch_dirty"

    def test_blocking_states(self):
        """Test blocking state values."""
        assert StateClass.DETACHED_HEAD.value == "detached_head"
        assert StateClass.REBASE_IN_PROGRESS.value == "rebase_in_progress"
        assert StateClass.CONFLICT.value == "conflict"
        assert StateClass.CODE_BEHIND.value == "code_behind"
        assert StateClass.ORPHANED_BRANCH.value == "orphaned_branch"

    def test_edge_case_states(self):
        """Test edge case state values."""
        assert StateClass.NO_UPSTREAM.value == "no_upstream"
        assert StateClass.MAIN_PROTECTION.value == "main_protection"

    def test_is_blocking_true(self):
        """Test is_blocking returns True for blocking states."""
        blocking = [
            StateClass.DETACHED_HEAD,
            StateClass.REBASE_IN_PROGRESS,
            StateClass.CONFLICT,
            StateClass.CODE_BEHIND,
            StateClass.ORPHANED_BRANCH,
        ]
        for state in blocking:
            assert StateClass.is_blocking(state) is True, f"{state} should be blocking"

    def test_is_blocking_false(self):
        """Test is_blocking returns False for non-blocking states."""
        non_blocking = [
            StateClass.READY,
            StateClass.READY_DIRTY,
            StateClass.BEHIND_CLEAN,
            StateClass.AHEAD,
            StateClass.BRANCH_MISMATCH,
        ]
        for state in non_blocking:
            assert StateClass.is_blocking(state) is False, f"{state} should not be blocking"

    def test_is_auto_fixable_true(self):
        """Test is_auto_fixable returns True for fixable states."""
        auto_fixable = [
            StateClass.BEHIND_CLEAN,
            StateClass.BEHIND_DIRTY,
            StateClass.AHEAD,
            StateClass.AHEAD_DIRTY,
            StateClass.DIVERGED_CLEAN,
            StateClass.DIVERGED_DIRTY,
            StateClass.BRANCH_MISMATCH,
            StateClass.BRANCH_MISMATCH_DIRTY,
            StateClass.NO_UPSTREAM,
            StateClass.MAIN_PROTECTION,
        ]
        for state in auto_fixable:
            assert StateClass.is_auto_fixable(state) is True, f"{state} should be auto-fixable"

    def test_is_auto_fixable_false(self):
        """Test is_auto_fixable returns False for non-fixable states."""
        non_fixable = [
            StateClass.READY,
            StateClass.READY_DIRTY,
            StateClass.DETACHED_HEAD,
            StateClass.REBASE_IN_PROGRESS,
            StateClass.CONFLICT,
            StateClass.CODE_BEHIND,
        ]
        for state in non_fixable:
            assert StateClass.is_auto_fixable(state) is False, f"{state} should not be auto-fixable"

    def test_state_class_is_string(self):
        """Test that StateClass values can be used as strings."""
        assert "ready" in StateClass.READY
        assert StateClass.READY.value == "ready"


# =============================================================================
# Test PreflightResult
# =============================================================================


class TestPreflightResult:
    """Tests for PreflightResult dataclass."""

    def test_basic_creation(self):
        """Test creating a basic preflight result."""
        state = ParityState()
        result = PreflightResult(
            success=True,
            state=state,
            can_proceed=True,
        )
        assert result.success is True
        assert result.state is state
        assert result.can_proceed is True
        assert result.blocking_reason is None
        assert result.auto_fixed is False
        assert result.actions_taken == []

    def test_blocking_result(self):
        """Test creating a blocking preflight result."""
        state = ParityState(status=ParityStatus.DETACHED_HEAD.value)
        result = PreflightResult(
            success=False,
            state=state,
            can_proceed=False,
            blocking_reason="Code repository is in detached HEAD state",
        )
        assert result.success is False
        assert result.can_proceed is False
        assert result.blocking_reason == "Code repository is in detached HEAD state"

    def test_auto_fixed_result(self):
        """Test creating an auto-fixed preflight result."""
        state = ParityState(status=ParityStatus.CLEAN.value)
        result = PreflightResult(
            success=True,
            state=state,
            can_proceed=True,
            auto_fixed=True,
            actions_taken=["Pulled ff-only", "Checked out to main"],
        )
        assert result.success is True
        assert result.auto_fixed is True
        assert len(result.actions_taken) == 2
        assert "Pulled ff-only" in result.actions_taken

    def test_preflight_result_has_expected_fields(self):
        """Test that PreflightResult has all expected fields."""
        field_names = {f.name for f in fields(PreflightResult)}
        expected = {"success", "state", "can_proceed", "blocking_reason", "auto_fixed", "actions_taken"}
        assert field_names == expected


# =============================================================================
# Test BranchPairingResult
# =============================================================================


class TestBranchPairingResult:
    """Tests for BranchPairingResult dataclass."""

    def test_basic_creation(self):
        """Test creating a basic branch pairing result."""
        result = BranchPairingResult(valid=True)
        assert result.valid is True
        assert result.code_branch is None
        assert result.threads_branch is None
        assert result.state_class is None
        assert result.mismatches == []
        assert result.warnings == []

    def test_valid_pairing(self):
        """Test creating a valid pairing result."""
        result = BranchPairingResult(
            valid=True,
            code_branch="main",
            threads_branch="main",
            state_class=StateClass.READY,
        )
        assert result.valid is True
        assert result.code_branch == "main"
        assert result.threads_branch == "main"
        assert result.state_class == StateClass.READY
        assert len(result.mismatches) == 0

    def test_invalid_pairing_with_mismatches(self):
        """Test creating an invalid pairing result with mismatches."""
        result = BranchPairingResult(
            valid=False,
            code_branch="feature-auth",
            threads_branch="main",
            state_class=StateClass.BRANCH_MISMATCH,
            mismatches=["Branch mismatch: code is on 'feature-auth', threads is on 'main'"],
        )
        assert result.valid is False
        assert result.state_class == StateClass.BRANCH_MISMATCH
        assert len(result.mismatches) == 1
        assert "Branch mismatch" in result.mismatches[0]

    def test_pairing_with_warnings(self):
        """Test creating a result with warnings."""
        result = BranchPairingResult(
            valid=True,
            code_branch="main",
            threads_branch="main",
            state_class=StateClass.BEHIND_CLEAN,
            warnings=["Threads 5 commits behind origin"],
        )
        assert result.valid is True
        assert len(result.warnings) == 1
        assert "5 commits behind" in result.warnings[0]

    def test_branch_pairing_result_has_expected_fields(self):
        """Test that BranchPairingResult has all expected fields."""
        field_names = {f.name for f in fields(BranchPairingResult)}
        expected = {"valid", "code_branch", "threads_branch", "state_class", "mismatches", "warnings"}
        assert field_names == expected


# =============================================================================
# Test BranchParityManager Initialization
# =============================================================================


class TestBranchParityManagerInit:
    """Tests for BranchParityManager initialization."""

    @pytest.fixture
    def temp_repos(self, tmp_path):
        """Create temporary repo directories."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()
        return code_path, threads_path

    def test_basic_init(self, temp_repos):
        """Test basic initialization."""
        code_path, threads_path = temp_repos
        manager = BranchParityManager(code_path, threads_path)
        assert manager.code_repo_path == code_path
        assert manager.threads_repo_path == threads_path
        assert manager._main_branch is None

    def test_init_with_main_branch(self, temp_repos):
        """Test initialization with custom main branch."""
        code_path, threads_path = temp_repos
        manager = BranchParityManager(code_path, threads_path, main_branch="develop")
        assert manager._main_branch == "develop"

    def test_init_paths_converted_to_path(self, temp_repos):
        """Test that string paths are converted to Path objects."""
        code_path, threads_path = temp_repos
        manager = BranchParityManager(str(code_path), str(threads_path))
        assert isinstance(manager.code_repo_path, Path)
        assert isinstance(manager.threads_repo_path, Path)


# =============================================================================
# Test BranchParityManager Properties
# =============================================================================


class TestBranchParityManagerProperties:
    """Tests for BranchParityManager properties."""

    @pytest.fixture
    def mock_manager(self, tmp_path):
        """Create a manager with mock repos."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()
        return BranchParityManager(code_path, threads_path)

    def test_code_repo_raises_on_invalid(self, mock_manager):
        """Test that code_repo raises BranchPairingError for invalid repo."""
        with pytest.raises(BranchPairingError) as exc_info:
            _ = mock_manager.code_repo
        assert "not a git repository" in str(exc_info.value)

    def test_threads_repo_raises_on_invalid(self, mock_manager):
        """Test that threads_repo raises BranchPairingError for invalid repo."""
        with pytest.raises(BranchPairingError) as exc_info:
            _ = mock_manager.threads_repo
        assert "not a git repository" in str(exc_info.value)

    @patch.object(BranchParityManager, 'code_repo', new_callable=PropertyMock)
    def test_main_branch_override(self, mock_code_repo, tmp_path):
        """Test that main_branch override is respected."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        manager = BranchParityManager(code_path, threads_path, main_branch="develop")
        assert manager.main_branch == "develop"

    @patch.object(BranchParityManager, 'code_repo', new_callable=PropertyMock)
    def test_main_branch_auto_detects_main(self, mock_code_repo, tmp_path):
        """Test that main_branch auto-detects 'main'."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_repo = MagicMock()
        mock_main_ref = MagicMock()
        mock_main_ref.name = "main"
        mock_repo.heads = [mock_main_ref]
        mock_code_repo.return_value = mock_repo

        manager = BranchParityManager(code_path, threads_path)
        assert manager.main_branch == "main"


# =============================================================================
# Test BranchParityManager.validate()
# =============================================================================


class TestBranchParityManagerValidate:
    """Tests for BranchParityManager.validate() method."""

    @pytest.fixture
    def manager_with_mocks(self, tmp_path):
        """Create a manager with mock repos."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()
        return BranchParityManager(code_path, threads_path)

    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.has_conflicts')
    @patch('watercooler_mcp.sync.branch_parity.is_rebase_in_progress')
    @patch('watercooler_mcp.sync.branch_parity.get_ahead_behind')
    @patch('watercooler_mcp.sync.branch_parity.is_dirty')
    @patch.object(BranchParityManager, 'code_repo', new_callable=PropertyMock)
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_validate_ready_clean(
        self, mock_threads_repo, mock_code_repo, mock_is_dirty,
        mock_get_ahead_behind, mock_is_rebase, mock_has_conflicts, mock_get_branch,
        manager_with_mocks
    ):
        """Test validate returns READY for clean, synced repos."""
        mock_code_repo.return_value = MagicMock()
        mock_threads_repo.return_value = MagicMock()
        mock_get_branch.return_value = "main"
        mock_has_conflicts.return_value = False
        mock_is_rebase.return_value = False
        mock_get_ahead_behind.return_value = (0, 0)
        mock_is_dirty.return_value = False

        result = manager_with_mocks.validate()
        assert result.valid is True
        assert result.state_class == StateClass.READY

    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch.object(BranchParityManager, 'code_repo', new_callable=PropertyMock)
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_validate_detached_head_code(
        self, mock_threads_repo, mock_code_repo, mock_get_branch, manager_with_mocks
    ):
        """Test validate returns DETACHED_HEAD for code repo."""
        mock_code_repo.return_value = MagicMock()
        mock_threads_repo.return_value = MagicMock()
        mock_get_branch.side_effect = [None, "main"]  # code returns None

        result = manager_with_mocks.validate()
        assert result.valid is False
        assert result.state_class == StateClass.DETACHED_HEAD
        assert "detached HEAD" in result.mismatches[0]

    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.has_conflicts')
    @patch.object(BranchParityManager, 'code_repo', new_callable=PropertyMock)
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_validate_conflict_in_code(
        self, mock_threads_repo, mock_code_repo, mock_has_conflicts, mock_get_branch,
        manager_with_mocks
    ):
        """Test validate returns CONFLICT for code repo with conflicts."""
        mock_code_repo.return_value = MagicMock()
        mock_threads_repo.return_value = MagicMock()
        mock_get_branch.return_value = "main"
        mock_has_conflicts.side_effect = [True, False]  # code has conflicts

        result = manager_with_mocks.validate()
        assert result.valid is False
        assert result.state_class == StateClass.CONFLICT
        assert "unresolved merge conflicts" in result.mismatches[0]

    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.has_conflicts')
    @patch('watercooler_mcp.sync.branch_parity.is_rebase_in_progress')
    @patch('watercooler_mcp.sync.branch_parity.is_dirty')
    @patch.object(BranchParityManager, 'code_repo', new_callable=PropertyMock)
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_validate_branch_mismatch(
        self, mock_threads_repo, mock_code_repo, mock_is_dirty,
        mock_is_rebase, mock_has_conflicts, mock_get_branch, manager_with_mocks
    ):
        """Test validate returns BRANCH_MISMATCH for mismatched branches."""
        mock_code_repo.return_value = MagicMock()
        mock_threads_repo.return_value = MagicMock()
        mock_get_branch.side_effect = ["feature-auth", "main"]
        mock_has_conflicts.return_value = False
        mock_is_rebase.return_value = False
        mock_is_dirty.return_value = False

        result = manager_with_mocks.validate()
        assert result.valid is False
        assert result.state_class == StateClass.BRANCH_MISMATCH
        assert "Branch mismatch" in result.mismatches[0]

    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.has_conflicts')
    @patch('watercooler_mcp.sync.branch_parity.is_rebase_in_progress')
    @patch('watercooler_mcp.sync.branch_parity.get_ahead_behind')
    @patch('watercooler_mcp.sync.branch_parity.is_dirty')
    @patch.object(BranchParityManager, 'code_repo', new_callable=PropertyMock)
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_validate_code_behind(
        self, mock_threads_repo, mock_code_repo, mock_is_dirty,
        mock_get_ahead_behind, mock_is_rebase, mock_has_conflicts, mock_get_branch,
        manager_with_mocks
    ):
        """Test validate returns CODE_BEHIND when code is behind origin."""
        mock_code_repo.return_value = MagicMock()
        mock_threads_repo.return_value = MagicMock()
        mock_get_branch.return_value = "main"
        mock_has_conflicts.return_value = False
        mock_is_rebase.return_value = False
        # code is 5 behind, threads is synced
        mock_get_ahead_behind.side_effect = [(0, 5), (0, 0)]
        mock_is_dirty.return_value = False

        result = manager_with_mocks.validate()
        assert result.valid is False
        assert result.state_class == StateClass.CODE_BEHIND

    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.has_conflicts')
    @patch('watercooler_mcp.sync.branch_parity.is_rebase_in_progress')
    @patch('watercooler_mcp.sync.branch_parity.get_ahead_behind')
    @patch('watercooler_mcp.sync.branch_parity.is_dirty')
    @patch.object(BranchParityManager, 'code_repo', new_callable=PropertyMock)
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_validate_threads_behind(
        self, mock_threads_repo, mock_code_repo, mock_is_dirty,
        mock_get_ahead_behind, mock_is_rebase, mock_has_conflicts, mock_get_branch,
        manager_with_mocks
    ):
        """Test validate returns BEHIND_CLEAN when threads is behind."""
        mock_code_repo.return_value = MagicMock()
        mock_threads_repo.return_value = MagicMock()
        mock_get_branch.return_value = "main"
        mock_has_conflicts.return_value = False
        mock_is_rebase.return_value = False
        # code is synced, threads is 3 behind
        mock_get_ahead_behind.side_effect = [(0, 0), (0, 3)]
        mock_is_dirty.return_value = False

        result = manager_with_mocks.validate()
        assert result.valid is True
        assert result.state_class == StateClass.BEHIND_CLEAN
        assert "3 commits behind" in result.warnings[0]


# =============================================================================
# Test BranchParityManager.classify_state()
# =============================================================================


class TestBranchParityManagerClassifyState:
    """Tests for BranchParityManager.classify_state() method."""

    @patch.object(BranchParityManager, 'validate')
    def test_classify_state_returns_state_class(self, mock_validate, tmp_path):
        """Test classify_state returns the state class from validate."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_validate.return_value = BranchPairingResult(
            valid=True, state_class=StateClass.AHEAD
        )

        manager = BranchParityManager(code_path, threads_path)
        state = manager.classify_state()
        assert state == StateClass.AHEAD

    @patch.object(BranchParityManager, 'validate')
    def test_classify_state_defaults_to_ready(self, mock_validate, tmp_path):
        """Test classify_state defaults to READY if state_class is None."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_validate.return_value = BranchPairingResult(valid=True, state_class=None)

        manager = BranchParityManager(code_path, threads_path)
        state = manager.classify_state()
        assert state == StateClass.READY


# =============================================================================
# Test BranchParityManager.ensure_readable()
# =============================================================================


class TestBranchParityManagerEnsureReadable:
    """Tests for BranchParityManager.ensure_readable() method."""

    @patch('watercooler_mcp.sync.branch_parity.has_conflicts')
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_ensure_readable_with_conflicts_skips_sync(
        self, mock_threads_repo, mock_has_conflicts, tmp_path
    ):
        """Test ensure_readable skips sync if conflicts exist."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_threads_repo.return_value = MagicMock()
        mock_has_conflicts.return_value = True

        manager = BranchParityManager(code_path, threads_path)
        ok, actions = manager.ensure_readable()

        assert ok is True
        assert "Skipped sync due to conflicts" in actions[0]

    @patch('watercooler_mcp.sync.branch_parity.has_conflicts')
    @patch('watercooler_mcp.sync.branch_parity.fetch_with_timeout')
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_ensure_readable_fetch_failure(
        self, mock_threads_repo, mock_fetch, mock_has_conflicts, tmp_path
    ):
        """Test ensure_readable succeeds even if fetch fails."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_threads_repo.return_value = MagicMock()
        mock_has_conflicts.return_value = False
        mock_fetch.return_value = False

        manager = BranchParityManager(code_path, threads_path)
        ok, actions = manager.ensure_readable()

        assert ok is True

    @patch('watercooler_mcp.sync.branch_parity.has_conflicts')
    @patch('watercooler_mcp.sync.branch_parity.fetch_with_timeout')
    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.get_ahead_behind')
    @patch('watercooler_mcp.sync.branch_parity.is_dirty')
    @patch('watercooler_mcp.sync.branch_parity.pull_ff_only')
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_ensure_readable_pulls_when_behind(
        self, mock_threads_repo, mock_pull_ff, mock_is_dirty, mock_get_ahead_behind,
        mock_get_branch, mock_fetch, mock_has_conflicts, tmp_path
    ):
        """Test ensure_readable pulls when behind and clean."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_threads_repo.return_value = MagicMock()
        mock_has_conflicts.return_value = False
        mock_fetch.return_value = True
        mock_get_branch.return_value = "main"
        mock_get_ahead_behind.return_value = (0, 5)  # 5 behind
        mock_is_dirty.return_value = False
        mock_pull_ff.return_value = True

        manager = BranchParityManager(code_path, threads_path)
        ok, actions = manager.ensure_readable()

        assert ok is True
        assert "Pulled" in actions[0]


# =============================================================================
# Test BranchParityManager.ensure_writable()
# =============================================================================


class TestBranchParityManagerEnsureWritable:
    """Tests for BranchParityManager.ensure_writable() method."""

    @patch.object(BranchParityManager, 'run_preflight')
    def test_ensure_writable_returns_tuple(self, mock_preflight, tmp_path):
        """Test ensure_writable returns correct tuple format."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_preflight.return_value = PreflightResult(
            success=True,
            state=ParityState(),
            can_proceed=True,
            actions_taken=["Pulled ff-only"],
        )

        manager = BranchParityManager(code_path, threads_path)
        ok, actions = manager.ensure_writable()

        assert ok is True
        assert "Pulled ff-only" in actions

    @patch.object(BranchParityManager, 'run_preflight')
    def test_ensure_writable_returns_blocking_reason(self, mock_preflight, tmp_path):
        """Test ensure_writable returns blocking reason on failure."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_preflight.return_value = PreflightResult(
            success=False,
            state=ParityState(),
            can_proceed=False,
            blocking_reason="Code is behind origin",
        )

        manager = BranchParityManager(code_path, threads_path)
        ok, reasons = manager.ensure_writable()

        assert ok is False
        assert "Code is behind origin" in reasons


# =============================================================================
# Test BranchParityManager.push_after_commit()
# =============================================================================


class TestBranchParityManagerPushAfterCommit:
    """Tests for BranchParityManager.push_after_commit() method."""

    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_push_after_commit_detached_head(
        self, mock_threads_repo, mock_get_branch, tmp_path
    ):
        """Test push_after_commit fails for detached HEAD."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_threads_repo.return_value = MagicMock()
        mock_get_branch.return_value = None

        manager = BranchParityManager(code_path, threads_path)
        ok, error = manager.push_after_commit()

        assert ok is False
        assert "detached HEAD" in error

    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.validate_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.push_with_retry')
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_push_after_commit_success(
        self, mock_threads_repo, mock_push, mock_validate, mock_get_branch, tmp_path
    ):
        """Test push_after_commit succeeds."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_threads_repo.return_value = MagicMock()
        mock_get_branch.return_value = "main"
        mock_validate.return_value = None
        mock_push.return_value = True

        manager = BranchParityManager(code_path, threads_path)
        ok, error = manager.push_after_commit()

        assert ok is True
        assert error is None

    @patch('watercooler_mcp.sync.branch_parity.get_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.validate_branch_name')
    @patch('watercooler_mcp.sync.branch_parity.push_with_retry')
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_push_after_commit_failure(
        self, mock_threads_repo, mock_push, mock_validate, mock_get_branch, tmp_path
    ):
        """Test push_after_commit reports failure."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_threads_repo.return_value = MagicMock()
        mock_get_branch.return_value = "main"
        mock_validate.return_value = None
        mock_push.return_value = False

        manager = BranchParityManager(code_path, threads_path)
        ok, error = manager.push_after_commit(max_retries=3)

        assert ok is False
        assert "Push failed after 3 attempts" in error


# =============================================================================
# Test BranchParityManager.get_health()
# =============================================================================


class TestBranchParityManagerGetHealth:
    """Tests for BranchParityManager.get_health() method."""

    def test_get_health_delegates_to_state_manager(self, tmp_path):
        """Test get_health delegates to StateManager.get_live_status()."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        expected_health = {
            "status": "clean",
            "code_branch": "main",
            "threads_branch": "main",
        }

        manager = BranchParityManager(code_path, threads_path)
        mock_state_manager = MagicMock()
        mock_state_manager.get_live_status.return_value = expected_health
        manager._state_manager = mock_state_manager

        health = manager.get_health()
        assert health == expected_health
        mock_state_manager.get_live_status.assert_called_once()


# =============================================================================
# Test Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @patch.object(BranchParityManager, 'code_repo', new_callable=PropertyMock)
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_validate_handles_exception(
        self, mock_threads_repo, mock_code_repo, tmp_path
    ):
        """Test validate handles unexpected exceptions gracefully."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_code_repo.return_value = MagicMock()
        mock_threads_repo.side_effect = Exception("Unexpected error")

        manager = BranchParityManager(code_path, threads_path)
        result = manager.validate()

        assert result.valid is False
        assert "Validation error" in result.mismatches[0]

    @patch('watercooler_mcp.sync.branch_parity.has_conflicts')
    @patch.object(BranchParityManager, 'threads_repo', new_callable=PropertyMock)
    def test_ensure_readable_handles_exception(
        self, mock_threads_repo, mock_has_conflicts, tmp_path
    ):
        """Test ensure_readable handles exceptions without blocking."""
        code_path = tmp_path / "code"
        threads_path = tmp_path / "threads"
        code_path.mkdir()
        threads_path.mkdir()

        mock_threads_repo.return_value = MagicMock()
        mock_has_conflicts.side_effect = Exception("Git error")

        manager = BranchParityManager(code_path, threads_path)
        ok, actions = manager.ensure_readable()

        # ensure_readable should never block
        assert ok is True
