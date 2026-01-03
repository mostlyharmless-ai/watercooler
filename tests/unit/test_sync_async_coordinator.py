"""Tests for sync/async_coordinator.py module."""

import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.sync.async_coordinator import (
    # Constants
    QUEUE_FILE_NAME,
    DEFAULT_BATCH_WINDOW,
    DEFAULT_MAX_DELAY,
    DEFAULT_MAX_BATCH_SIZE,
    DEFAULT_SYNC_INTERVAL,
    # Data classes
    PendingCommit,
    AsyncConfig,
    AsyncStatus,
    # Classes
    AsyncSyncCoordinator,
    # Convenience functions
    get_queue_file_path,
)


# =============================================================================
# Test Constants
# =============================================================================


class TestConstants:
    """Tests for module constants."""

    def test_queue_file_name(self):
        """Test QUEUE_FILE_NAME constant."""
        assert QUEUE_FILE_NAME == "queue.jsonl"

    def test_default_batch_window(self):
        """Test DEFAULT_BATCH_WINDOW constant."""
        assert DEFAULT_BATCH_WINDOW == 5.0

    def test_default_max_delay(self):
        """Test DEFAULT_MAX_DELAY constant."""
        assert DEFAULT_MAX_DELAY == 30.0

    def test_default_max_batch_size(self):
        """Test DEFAULT_MAX_BATCH_SIZE constant."""
        assert DEFAULT_MAX_BATCH_SIZE == 50

    def test_default_sync_interval(self):
        """Test DEFAULT_SYNC_INTERVAL constant."""
        assert DEFAULT_SYNC_INTERVAL == 30.0


# =============================================================================
# Test PendingCommit
# =============================================================================


class TestPendingCommit:
    """Tests for PendingCommit dataclass."""

    def test_basic_creation(self):
        """Test creating a basic pending commit."""
        commit = PendingCommit(
            sequence=1,
            entry_id="01ABC123",
            topic="feature-auth",
            commit_message="Add auth feature",
            timestamp="2024-01-01T00:00:00Z",
        )
        assert commit.sequence == 1
        assert commit.entry_id == "01ABC123"
        assert commit.topic == "feature-auth"
        assert commit.commit_message == "Add auth feature"
        assert commit.timestamp == "2024-01-01T00:00:00Z"

    def test_created_ts_default(self):
        """Test that created_ts defaults to current time."""
        before = time.time()
        commit = PendingCommit(
            sequence=1,
            entry_id=None,
            topic=None,
            commit_message="Test",
            timestamp="2024-01-01T00:00:00Z",
        )
        after = time.time()
        assert before <= commit.created_ts <= after

    def test_optional_fields(self):
        """Test that entry_id and topic are optional."""
        commit = PendingCommit(
            sequence=1,
            entry_id=None,
            topic=None,
            commit_message="Test",
            timestamp="2024-01-01T00:00:00Z",
        )
        assert commit.entry_id is None
        assert commit.topic is None

    def test_to_dict(self):
        """Test conversion to dictionary."""
        commit = PendingCommit(
            sequence=1,
            entry_id="01ABC123",
            topic="feature-auth",
            commit_message="Add auth feature",
            timestamp="2024-01-01T00:00:00Z",
            created_ts=1704067200.0,
        )
        data = commit.to_dict()
        assert data["sequence"] == 1
        assert data["entry_id"] == "01ABC123"
        assert data["topic"] == "feature-auth"
        assert data["commit_message"] == "Add auth feature"
        assert data["timestamp"] == "2024-01-01T00:00:00Z"
        assert data["created_ts"] == 1704067200.0

    def test_from_dict(self):
        """Test creation from dictionary."""
        data = {
            "sequence": 1,
            "entry_id": "01ABC123",
            "topic": "feature-auth",
            "commit_message": "Add auth feature",
            "timestamp": "2024-01-01T00:00:00Z",
            "created_ts": 1704067200.0,
        }
        commit = PendingCommit.from_dict(data)
        assert commit.sequence == 1
        assert commit.entry_id == "01ABC123"
        assert commit.topic == "feature-auth"
        assert commit.commit_message == "Add auth feature"
        assert commit.timestamp == "2024-01-01T00:00:00Z"
        assert commit.created_ts == 1704067200.0

    def test_from_dict_missing_optional(self):
        """Test from_dict with missing optional fields."""
        data = {
            "sequence": 1,
            "commit_message": "Test",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        commit = PendingCommit.from_dict(data)
        assert commit.sequence == 1
        assert commit.entry_id is None
        assert commit.topic is None

    def test_roundtrip(self):
        """Test to_dict and from_dict roundtrip."""
        original = PendingCommit(
            sequence=42,
            entry_id="01XYZ789",
            topic="refactor-core",
            commit_message="Refactor core module",
            timestamp="2024-06-15T12:30:00Z",
            created_ts=1718451000.0,
        )
        data = original.to_dict()
        restored = PendingCommit.from_dict(data)
        assert restored.sequence == original.sequence
        assert restored.entry_id == original.entry_id
        assert restored.topic == original.topic
        assert restored.commit_message == original.commit_message
        assert restored.timestamp == original.timestamp
        assert restored.created_ts == original.created_ts


# =============================================================================
# Test AsyncConfig
# =============================================================================


class TestAsyncConfig:
    """Tests for AsyncConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        config = AsyncConfig()
        assert config.batch_window == DEFAULT_BATCH_WINDOW
        assert config.max_delay == DEFAULT_MAX_DELAY
        assert config.max_batch_size == DEFAULT_MAX_BATCH_SIZE
        assert config.sync_interval == DEFAULT_SYNC_INTERVAL
        assert config.enabled is True

    def test_custom_values(self):
        """Test custom configuration values."""
        config = AsyncConfig(
            batch_window=10.0,
            max_delay=60.0,
            max_batch_size=100,
            sync_interval=15.0,
            enabled=False,
        )
        assert config.batch_window == 10.0
        assert config.max_delay == 60.0
        assert config.max_batch_size == 100
        assert config.sync_interval == 15.0
        assert config.enabled is False


# =============================================================================
# Test AsyncStatus
# =============================================================================


class TestAsyncStatus:
    """Tests for AsyncStatus dataclass."""

    def test_default_values(self):
        """Test default status values."""
        status = AsyncStatus()
        assert status.running is False
        assert status.queue_depth == 0
        assert status.oldest_commit_age is None
        assert status.last_push_at is None
        assert status.last_error is None
        assert status.total_pushed == 0
        assert status.total_failed == 0

    def test_custom_values(self):
        """Test custom status values."""
        status = AsyncStatus(
            running=True,
            queue_depth=5,
            oldest_commit_age=10.5,
            last_push_at="2024-01-01T00:00:00Z",
            last_error="Network error",
            total_pushed=100,
            total_failed=2,
        )
        assert status.running is True
        assert status.queue_depth == 5
        assert status.oldest_commit_age == 10.5
        assert status.last_push_at == "2024-01-01T00:00:00Z"
        assert status.last_error == "Network error"
        assert status.total_pushed == 100
        assert status.total_failed == 2


# =============================================================================
# Test AsyncSyncCoordinator
# =============================================================================


class TestAsyncSyncCoordinator:
    """Tests for AsyncSyncCoordinator class."""

    @pytest.fixture
    def temp_repo(self, tmp_path):
        """Create a temporary directory for testing."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        return repo_path

    @pytest.fixture
    def coordinator(self, temp_repo):
        """Create a coordinator for testing."""
        config = AsyncConfig(
            batch_window=0.1,  # Short for testing
            max_delay=1.0,
            sync_interval=0.1,
        )
        return AsyncSyncCoordinator(repo_path=temp_repo, config=config)

    def test_init_basic(self, temp_repo):
        """Test basic initialization."""
        coord = AsyncSyncCoordinator(repo_path=temp_repo)
        assert coord.repo_path == temp_repo
        assert coord.config is not None
        assert coord.queue_dir == temp_repo
        assert coord._queue == []
        assert coord._sequence == 0

    def test_init_with_config(self, temp_repo):
        """Test initialization with custom config."""
        config = AsyncConfig(batch_window=10.0)
        coord = AsyncSyncCoordinator(repo_path=temp_repo, config=config)
        assert coord.config.batch_window == 10.0

    def test_init_with_queue_dir(self, temp_repo, tmp_path):
        """Test initialization with separate queue directory."""
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        coord = AsyncSyncCoordinator(repo_path=temp_repo, queue_dir=queue_dir)
        assert coord.queue_dir == queue_dir

    def test_enqueue_commit(self, coordinator):
        """Test enqueueing a commit."""
        seq = coordinator.enqueue_commit(
            commit_message="Test commit",
            topic="test-topic",
            entry_id="01ABC123",
        )
        assert seq == 1
        assert len(coordinator._queue) == 1
        commit = coordinator._queue[0]
        assert commit.sequence == 1
        assert commit.commit_message == "Test commit"
        assert commit.topic == "test-topic"
        assert commit.entry_id == "01ABC123"

    def test_enqueue_multiple_commits(self, coordinator):
        """Test enqueueing multiple commits."""
        seq1 = coordinator.enqueue_commit(commit_message="Commit 1")
        seq2 = coordinator.enqueue_commit(commit_message="Commit 2")
        seq3 = coordinator.enqueue_commit(commit_message="Commit 3")
        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3
        assert len(coordinator._queue) == 3

    def test_queue_persistence(self, temp_repo):
        """Test that queue is persisted to disk."""
        config = AsyncConfig(batch_window=0.1)
        coord1 = AsyncSyncCoordinator(repo_path=temp_repo, config=config)
        coord1.enqueue_commit(commit_message="Test commit")

        # Check queue file exists
        queue_file = temp_repo / QUEUE_FILE_NAME
        assert queue_file.exists()

        # Create new coordinator and verify queue is loaded
        coord2 = AsyncSyncCoordinator(repo_path=temp_repo, config=config)
        assert len(coord2._queue) == 1
        assert coord2._queue[0].commit_message == "Test commit"

    def test_get_queue(self, coordinator):
        """Test getting a copy of the queue."""
        coordinator.enqueue_commit(commit_message="Test 1")
        coordinator.enqueue_commit(commit_message="Test 2")

        queue = coordinator.get_queue()
        assert len(queue) == 2
        assert queue[0].commit_message == "Test 1"
        assert queue[1].commit_message == "Test 2"

        # Verify it's a copy
        queue.clear()
        assert len(coordinator._queue) == 2

    def test_status(self, coordinator):
        """Test getting coordinator status."""
        status = coordinator.status()
        assert status.running is False
        assert status.queue_depth == 0
        assert status.oldest_commit_age is None

        coordinator.enqueue_commit(commit_message="Test")
        status = coordinator.status()
        assert status.queue_depth == 1
        assert status.oldest_commit_age is not None
        assert status.oldest_commit_age >= 0

    def test_start_stop(self, coordinator):
        """Test starting and stopping the coordinator."""
        coordinator.start()
        assert coordinator._running is True
        assert coordinator._worker is not None
        assert coordinator._worker.is_alive()

        # Use shutdown with timeout for graceful stop
        coordinator.shutdown(timeout=5.0)
        assert coordinator._running is False

    def test_shutdown(self, coordinator):
        """Test graceful shutdown."""
        coordinator.start()
        assert coordinator._running is True

        result = coordinator.shutdown(timeout=5.0)
        assert result is True
        assert coordinator._running is False

    def test_shutdown_already_stopped(self, coordinator):
        """Test shutdown when already stopped."""
        result = coordinator.shutdown(timeout=1.0)
        assert result is True

    def test_start_twice(self, coordinator):
        """Test that starting twice is a no-op."""
        coordinator.start()
        worker1 = coordinator._worker

        coordinator.start()
        worker2 = coordinator._worker

        assert worker1 is worker2  # Same thread
        coordinator.stop()

    def test_callbacks(self, temp_repo):
        """Test success and failure callbacks."""
        success_calls = []
        failure_calls = []

        def on_success(commits):
            success_calls.append(commits)

        def on_failure(commits, error):
            failure_calls.append((commits, error))

        coord = AsyncSyncCoordinator(
            repo_path=temp_repo,
            on_push_success=on_success,
            on_push_failure=on_failure,
        )
        assert coord.on_push_success is on_success
        assert coord.on_push_failure is on_failure


# =============================================================================
# Test Convenience Functions
# =============================================================================


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_get_queue_file_path(self, tmp_path):
        """Test get_queue_file_path function."""
        path = get_queue_file_path(tmp_path)
        assert path == tmp_path / QUEUE_FILE_NAME


# =============================================================================
# Test Queue Persistence
# =============================================================================


class TestQueuePersistence:
    """Tests for queue persistence functionality."""

    def test_empty_queue_file(self, tmp_path):
        """Test loading from empty queue file."""
        queue_file = tmp_path / QUEUE_FILE_NAME
        queue_file.touch()

        coord = AsyncSyncCoordinator(repo_path=tmp_path)
        assert coord._queue == []

    def test_corrupted_queue_file(self, tmp_path):
        """Test loading from corrupted queue file."""
        queue_file = tmp_path / QUEUE_FILE_NAME
        queue_file.write_text("not valid json\n")

        coord = AsyncSyncCoordinator(repo_path=tmp_path)
        # Should gracefully handle corruption
        assert coord._queue == []

    def test_partial_queue_file(self, tmp_path):
        """Test loading from partially corrupted queue file."""
        queue_file = tmp_path / QUEUE_FILE_NAME
        valid_line = json.dumps({
            "sequence": 1,
            "entry_id": "01ABC",
            "topic": "test",
            "commit_message": "Valid",
            "timestamp": "2024-01-01T00:00:00Z",
        })
        queue_file.write_text(f"{valid_line}\nnot valid json\n")

        coord = AsyncSyncCoordinator(repo_path=tmp_path)
        # First valid line should load, rest skipped
        assert len(coord._queue) == 1
        assert coord._queue[0].commit_message == "Valid"

    def test_sequence_preserved_on_load(self, tmp_path):
        """Test that sequence numbers are preserved when loading."""
        queue_file = tmp_path / QUEUE_FILE_NAME
        lines = []
        for i in [5, 10, 15]:
            lines.append(json.dumps({
                "sequence": i,
                "entry_id": None,
                "topic": None,
                "commit_message": f"Commit {i}",
                "timestamp": "2024-01-01T00:00:00Z",
            }))
        queue_file.write_text("\n".join(lines) + "\n")

        coord = AsyncSyncCoordinator(repo_path=tmp_path)
        assert coord._sequence == 15  # Should be max sequence
        assert len(coord._queue) == 3


# =============================================================================
# Test Thread Safety
# =============================================================================


class TestThreadSafety:
    """Tests for thread safety."""

    def test_concurrent_enqueue(self, tmp_path):
        """Test concurrent enqueue operations."""
        config = AsyncConfig(batch_window=10.0, max_delay=60.0)
        coord = AsyncSyncCoordinator(repo_path=tmp_path, config=config)

        results = []
        errors = []

        def enqueue_many(count, offset):
            try:
                for i in range(count):
                    seq = coord.enqueue_commit(
                        commit_message=f"Commit {offset + i}"
                    )
                    results.append(seq)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=enqueue_many, args=(10, i * 10))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 50
        assert len(set(results)) == 50  # All unique sequences
        assert len(coord._queue) == 50
