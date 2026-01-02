"""Tests for sync/state.py - unified state management."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from watercooler_mcp.sync import (
    # Constants
    STATE_FILE_NAME,
    STATE_FILE_VERSION,
    STATE_DIR,
    # Enums
    ParityStatus,
    # Data classes
    ParityState,
    # Classes
    StateManager,
    # Convenience functions
    read_parity_state,
    write_parity_state,
    get_state_file_path,
)


# =============================================================================
# ParityStatus Enum Tests
# =============================================================================


class TestParityStatus:
    """Tests for ParityStatus enum."""

    def test_all_status_values_exist(self):
        """All expected status values should exist."""
        expected = [
            "CLEAN",
            "PENDING_PUSH",
            "BRANCH_MISMATCH",
            "MAIN_PROTECTION",
            "CODE_BEHIND_ORIGIN",
            "REMOTE_UNREACHABLE",
            "REBASE_IN_PROGRESS",
            "DETACHED_HEAD",
            "DIVERGED",
            "NEEDS_MANUAL_RECOVER",
            "ORPHAN_BRANCH",
            "ERROR",
        ]
        for name in expected:
            assert hasattr(ParityStatus, name)

    def test_status_values_are_strings(self):
        """Status values should be lowercase strings."""
        for status in ParityStatus:
            assert isinstance(status.value, str)
            assert status.value == status.value.lower()

    def test_is_blocking_returns_true_for_blocking_states(self):
        """is_blocking should return True for states requiring human intervention."""
        blocking_states = [
            ParityStatus.CODE_BEHIND_ORIGIN,
            ParityStatus.DETACHED_HEAD,
            ParityStatus.REBASE_IN_PROGRESS,
            ParityStatus.DIVERGED,
            ParityStatus.NEEDS_MANUAL_RECOVER,
            ParityStatus.ORPHAN_BRANCH,
            ParityStatus.ERROR,
        ]
        for status in blocking_states:
            assert ParityStatus.is_blocking(status) is True

    def test_is_blocking_returns_false_for_non_blocking_states(self):
        """is_blocking should return False for auto-fixable and clean states."""
        non_blocking_states = [
            ParityStatus.CLEAN,
            ParityStatus.PENDING_PUSH,
            ParityStatus.BRANCH_MISMATCH,
            ParityStatus.MAIN_PROTECTION,
            ParityStatus.REMOTE_UNREACHABLE,
        ]
        for status in non_blocking_states:
            assert ParityStatus.is_blocking(status) is False

    def test_is_auto_fixable_returns_true_for_auto_fixable_states(self):
        """is_auto_fixable should return True for states that can be auto-fixed."""
        auto_fixable_states = [
            ParityStatus.BRANCH_MISMATCH,
            ParityStatus.PENDING_PUSH,
            ParityStatus.REMOTE_UNREACHABLE,
        ]
        for status in auto_fixable_states:
            assert ParityStatus.is_auto_fixable(status) is True

    def test_is_auto_fixable_returns_false_for_non_auto_fixable_states(self):
        """is_auto_fixable should return False for blocking and clean states."""
        non_auto_fixable_states = [
            ParityStatus.CLEAN,
            ParityStatus.CODE_BEHIND_ORIGIN,
            ParityStatus.DETACHED_HEAD,
            ParityStatus.REBASE_IN_PROGRESS,
            ParityStatus.DIVERGED,
            ParityStatus.NEEDS_MANUAL_RECOVER,
            ParityStatus.ORPHAN_BRANCH,
            ParityStatus.ERROR,
        ]
        for status in non_auto_fixable_states:
            assert ParityStatus.is_auto_fixable(status) is False


# =============================================================================
# ParityState Dataclass Tests
# =============================================================================


class TestParityState:
    """Tests for ParityState dataclass."""

    def test_default_values(self):
        """ParityState should have sensible defaults."""
        state = ParityState()
        assert state.status == ParityStatus.CLEAN.value
        assert state.last_check_at == ""
        assert state.code_branch is None
        assert state.threads_branch is None
        assert state.actions_taken == []
        assert state.pending_push is False
        assert state.last_error is None
        assert state.code_ahead_origin == 0
        assert state.code_behind_origin == 0
        assert state.threads_ahead_origin == 0
        assert state.threads_behind_origin == 0
        assert state.version == STATE_FILE_VERSION

    def test_to_dict(self):
        """to_dict should serialize all fields."""
        state = ParityState(
            status="pending_push",
            code_branch="feature",
            threads_branch="feature",
            pending_push=True,
            threads_ahead_origin=3,
        )
        d = state.to_dict()
        assert d["status"] == "pending_push"
        assert d["code_branch"] == "feature"
        assert d["threads_branch"] == "feature"
        assert d["pending_push"] is True
        assert d["threads_ahead_origin"] == 3
        assert "version" in d

    def test_from_dict_with_full_data(self):
        """from_dict should deserialize all fields."""
        data = {
            "status": "branch_mismatch",
            "last_check_at": "2025-01-01T00:00:00Z",
            "code_branch": "main",
            "threads_branch": "feature",
            "actions_taken": ["checkout"],
            "pending_push": False,
            "last_error": None,
            "code_ahead_origin": 1,
            "code_behind_origin": 2,
            "threads_ahead_origin": 3,
            "threads_behind_origin": 4,
            "version": 1,
        }
        state = ParityState.from_dict(data)
        assert state.status == "branch_mismatch"
        assert state.code_branch == "main"
        assert state.threads_branch == "feature"
        assert state.actions_taken == ["checkout"]
        assert state.code_ahead_origin == 1
        assert state.code_behind_origin == 2

    def test_from_dict_with_missing_fields(self):
        """from_dict should handle missing fields gracefully."""
        data = {"status": "clean"}
        state = ParityState.from_dict(data)
        assert state.status == "clean"
        assert state.code_branch is None
        assert state.actions_taken == []
        assert state.version == 1

    def test_from_dict_with_empty_dict(self):
        """from_dict should return defaults for empty dict."""
        state = ParityState.from_dict({})
        assert state.status == ParityStatus.CLEAN.value
        assert state.version == 1


# =============================================================================
# StateManager Tests
# =============================================================================


class TestStateManager:
    """Tests for StateManager class."""

    def test_init(self, tmp_path):
        """StateManager should initialize with paths."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        code_path = tmp_path / "code"

        manager = StateManager(threads_dir, code_path)
        assert manager.threads_dir == threads_dir
        assert manager.code_repo_path == code_path

    def test_read_returns_default_when_file_missing(self, tmp_path):
        """read() should return default state when file doesn't exist."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        manager = StateManager(threads_dir)
        state = manager.read()
        assert state.status == ParityStatus.CLEAN.value

    def test_read_returns_state_from_file(self, tmp_path):
        """read() should return state from file when it exists."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        state_file = threads_dir / STATE_FILE_NAME
        state_file.write_text(
            json.dumps(
                {
                    "status": "pending_push",
                    "code_branch": "feature",
                    "threads_branch": "feature",
                    "version": 1,
                }
            )
        )

        manager = StateManager(threads_dir)
        state = manager.read()
        assert state.status == "pending_push"
        assert state.code_branch == "feature"

    def test_read_handles_corrupted_json(self, tmp_path):
        """read() should return default state on corrupted JSON."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        state_file = threads_dir / STATE_FILE_NAME
        state_file.write_text("not valid json {{{")

        manager = StateManager(threads_dir)
        state = manager.read()
        assert state.status == ParityStatus.CLEAN.value

    def test_read_handles_invalid_structure(self, tmp_path):
        """read() should return default state on invalid structure."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        state_file = threads_dir / STATE_FILE_NAME
        state_file.write_text(json.dumps([1, 2, 3]))  # Array, not object

        manager = StateManager(threads_dir)
        state = manager.read()
        # Should handle gracefully - may vary based on implementation
        assert state is not None

    def test_read_uses_cache(self, tmp_path):
        """read() should use cache when use_cache=True."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        state_file = threads_dir / STATE_FILE_NAME
        state_file.write_text(json.dumps({"status": "clean", "version": 1}))

        manager = StateManager(threads_dir)
        state1 = manager.read()

        # Modify file
        state_file.write_text(json.dumps({"status": "pending_push", "version": 1}))

        # Should still return cached value
        state2 = manager.read(use_cache=True)
        assert state2.status == "clean"

        # Force refresh
        state3 = manager.read(use_cache=False)
        assert state3.status == "pending_push"

    def test_write_creates_file(self, tmp_path):
        """write() should create state file in preferred location."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        manager = StateManager(threads_dir)
        state = ParityState(status="pending_push", code_branch="feature")
        result = manager.write(state)

        assert result is True
        # State file now lives at .watercooler/state/branch_parity_state.json
        state_file = threads_dir / STATE_DIR / STATE_FILE_NAME
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["status"] == "pending_push"
        assert data["code_branch"] == "feature"

    def test_write_creates_parent_directories(self, tmp_path):
        """write() should create parent directories if needed."""
        threads_dir = tmp_path / "nested" / "threads"
        # Don't create directories

        manager = StateManager(threads_dir)
        state = ParityState(status="clean")
        result = manager.write(state)

        assert result is True
        assert threads_dir.exists()

    def test_write_updates_cache(self, tmp_path):
        """write() should update internal cache."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        manager = StateManager(threads_dir)
        state = ParityState(status="pending_push")
        manager.write(state)

        # Read should return cached value without hitting disk
        cached = manager.read(use_cache=True)
        assert cached.status == "pending_push"

    def test_write_atomic(self, tmp_path):
        """write() should be atomic (no partial writes)."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        manager = StateManager(threads_dir)
        state = ParityState(status="pending_push")
        manager.write(state)

        # Verify no temp files left behind
        temp_files = list(threads_dir.glob(".parity_state_*.tmp"))
        assert len(temp_files) == 0

    def test_invalidate_clears_cache(self, tmp_path):
        """invalidate() should clear cached state."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        state_file = threads_dir / STATE_FILE_NAME
        state_file.write_text(json.dumps({"status": "clean", "version": 1}))

        manager = StateManager(threads_dir)
        manager.read()  # Populate cache

        # Modify file
        state_file.write_text(json.dumps({"status": "pending_push", "version": 1}))

        # Cache still valid
        assert manager.read().status == "clean"

        # Invalidate
        manager.invalidate()

        # Now reads fresh
        assert manager.read().status == "pending_push"

    def test_get_live_status_without_code_repo(self, tmp_path):
        """get_live_status() should work without code repo path."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        manager = StateManager(threads_dir)
        status = manager.get_live_status()

        assert "status" in status
        assert "last_check_at" in status
        assert status["lock_holder"] is None


class TestStateManagerLiveStatus:
    """Tests for StateManager.get_live_status() with mocked repos."""

    def test_get_live_status_clean(self, tmp_path):
        """get_live_status() should return clean when all synced."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        code_path = tmp_path / "code"
        code_path.mkdir()

        # Write initial state file
        state_file = threads_dir / STATE_FILE_NAME
        state_file.write_text(json.dumps({"status": "pending_push", "version": 1}))

        manager = StateManager(threads_dir, code_path)

        with patch("watercooler_mcp.sync.state.Repo") as mock_repo_class:
            # Setup mocked repos
            code_repo = MagicMock()
            threads_repo = MagicMock()
            mock_repo_class.side_effect = [code_repo, threads_repo]

            # Both on same branch
            code_repo.head.is_detached = False
            code_repo.active_branch.name = "main"
            threads_repo.head.is_detached = False
            threads_repo.active_branch.name = "main"

            # No rebase in progress
            code_repo.git_dir = str(tmp_path / "code" / ".git")
            threads_repo.git_dir = str(tmp_path / "threads" / ".git")
            (tmp_path / "code" / ".git").mkdir(parents=True)
            (tmp_path / "threads" / ".git").mkdir(parents=True)

            # No conflicts
            threads_repo.git.status.return_value = ""

            # Mock fetch
            code_repo.remotes.origin.fetch = MagicMock()
            threads_repo.remotes.origin.fetch = MagicMock()

            # Mock commit for ahead/behind (no remote)
            code_repo.commit.side_effect = Exception("No remote")
            threads_repo.commit.side_effect = Exception("No remote")

            status = manager.get_live_status()

            assert status["status"] == ParityStatus.CLEAN.value
            assert status["code_branch"] == "main"
            assert status["threads_branch"] == "main"

    def test_get_live_status_detached_head(self, tmp_path):
        """get_live_status() should detect detached HEAD."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        code_path = tmp_path / "code"
        code_path.mkdir()

        manager = StateManager(threads_dir, code_path)

        with patch("watercooler_mcp.sync.state.Repo") as mock_repo_class:
            code_repo = MagicMock()
            threads_repo = MagicMock()
            mock_repo_class.side_effect = [code_repo, threads_repo]

            # Detached HEAD in code repo
            code_repo.head.is_detached = True
            threads_repo.head.is_detached = False
            threads_repo.active_branch.name = "main"

            status = manager.get_live_status()
            assert status["status"] == ParityStatus.DETACHED_HEAD.value

    def test_get_live_status_branch_mismatch(self, tmp_path):
        """get_live_status() should detect branch mismatch."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        code_path = tmp_path / "code"
        code_path.mkdir()

        manager = StateManager(threads_dir, code_path)

        with patch("watercooler_mcp.sync.state.Repo") as mock_repo_class:
            code_repo = MagicMock()
            threads_repo = MagicMock()
            mock_repo_class.side_effect = [code_repo, threads_repo]

            # Different branches
            code_repo.head.is_detached = False
            code_repo.active_branch.name = "feature"
            threads_repo.head.is_detached = False
            threads_repo.active_branch.name = "main"

            # No rebase
            code_repo.git_dir = str(tmp_path / "code" / ".git")
            threads_repo.git_dir = str(tmp_path / "threads" / ".git")
            (tmp_path / "code" / ".git").mkdir(parents=True)
            (tmp_path / "threads" / ".git").mkdir(parents=True)

            threads_repo.git.status.return_value = ""
            code_repo.remotes.origin.fetch = MagicMock()
            threads_repo.remotes.origin.fetch = MagicMock()
            code_repo.commit.side_effect = Exception("No remote")
            threads_repo.commit.side_effect = Exception("No remote")

            status = manager.get_live_status()
            assert status["status"] == ParityStatus.BRANCH_MISMATCH.value


# =============================================================================
# Convenience Function Tests
# =============================================================================


class TestConvenienceFunctions:
    """Tests for backward-compatibility convenience functions."""

    def test_read_parity_state(self, tmp_path):
        """read_parity_state() should read state from file."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        state_file = threads_dir / STATE_FILE_NAME
        state_file.write_text(json.dumps({"status": "pending_push", "version": 1}))

        state = read_parity_state(threads_dir)
        assert state.status == "pending_push"

    def test_write_parity_state(self, tmp_path):
        """write_parity_state() should write state to preferred location."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        state = ParityState(status="pending_push", code_branch="feature")
        result = write_parity_state(threads_dir, state)

        assert result is True
        # State file now lives at .watercooler/state/branch_parity_state.json
        state_file = threads_dir / STATE_DIR / STATE_FILE_NAME
        assert state_file.exists()

    def test_get_state_file_path(self, tmp_path):
        """get_state_file_path() should return preferred path when neither exists."""
        threads_dir = tmp_path / "threads"
        path = get_state_file_path(threads_dir)
        # When neither file exists, returns preferred location
        assert path == threads_dir / STATE_DIR / STATE_FILE_NAME

    def test_get_state_file_path_legacy_fallback(self, tmp_path):
        """get_state_file_path() should return legacy path when only legacy exists."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        legacy_file = threads_dir / STATE_FILE_NAME
        legacy_file.write_text('{"status": "clean"}')

        path = get_state_file_path(threads_dir)
        # When only legacy exists, returns legacy
        assert path == legacy_file


# =============================================================================
# Constants Tests
# =============================================================================


class TestConstants:
    """Tests for module constants."""

    def test_state_file_name(self):
        """STATE_FILE_NAME should be the expected value."""
        assert STATE_FILE_NAME == "branch_parity_state.json"

    def test_state_file_version(self):
        """STATE_FILE_VERSION should be a positive integer."""
        assert isinstance(STATE_FILE_VERSION, int)
        assert STATE_FILE_VERSION >= 1
