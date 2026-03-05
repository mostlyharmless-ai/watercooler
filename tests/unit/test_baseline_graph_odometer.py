"""Tests for baseline_graph odometer (access tracking) functions."""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from watercooler.baseline_graph.reader import (
    get_graph_dir,
    increment_access_count,
    get_access_count,
    get_most_accessed,
    _get_counters_file,
    _load_counters,
    _save_counters,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def threads_dir(tmp_path: Path) -> Path:
    """Create a temporary threads directory with graph structure."""
    threads = tmp_path / "threads"
    threads.mkdir()
    graph_dir = threads / "graph" / "baseline"
    graph_dir.mkdir(parents=True)
    return threads


@pytest.fixture
def threads_dir_with_counters(threads_dir: Path) -> Path:
    """Create a threads directory with pre-existing counters."""
    counters = {
        "thread:topic-a": 10,
        "thread:topic-b": 5,
        "entry:01HXYZ123": 3,
        "entry:01HXYZ456": 7,
    }
    counters_file = get_graph_dir(threads_dir) / "counters.json"
    counters_file.write_text(json.dumps(counters), encoding="utf-8")
    return threads_dir


# ============================================================================
# Helper Function Tests
# ============================================================================


class TestCountersFileHelpers:
    """Tests for counters file helper functions."""

    def test_get_counters_file_path(self, threads_dir: Path):
        """Test counters file path is in graph directory."""
        counters_file = _get_counters_file(threads_dir)
        assert counters_file == threads_dir / "graph" / "baseline" / "counters.json"

    def test_load_counters_empty(self, threads_dir: Path):
        """Test loading counters when file doesn't exist."""
        counters = _load_counters(threads_dir)
        assert counters == {}

    def test_load_counters_existing(self, threads_dir_with_counters: Path):
        """Test loading existing counters."""
        counters = _load_counters(threads_dir_with_counters)
        assert counters["thread:topic-a"] == 10
        assert counters["thread:topic-b"] == 5
        assert counters["entry:01HXYZ123"] == 3

    def test_load_counters_invalid_json(self, threads_dir: Path):
        """Test loading counters with invalid JSON returns empty dict."""
        counters_file = _get_counters_file(threads_dir)
        counters_file.write_text("not valid json", encoding="utf-8")
        counters = _load_counters(threads_dir)
        assert counters == {}

    def test_save_counters_creates_file(self, threads_dir: Path):
        """Test saving counters creates file."""
        counters = {"thread:test": 5}
        _save_counters(threads_dir, counters)

        counters_file = _get_counters_file(threads_dir)
        assert counters_file.exists()
        loaded = json.loads(counters_file.read_text(encoding="utf-8"))
        assert loaded["thread:test"] == 5

    def test_save_counters_overwrites(self, threads_dir_with_counters: Path):
        """Test saving counters overwrites existing file."""
        new_counters = {"thread:new": 1}
        _save_counters(threads_dir_with_counters, new_counters)

        loaded = _load_counters(threads_dir_with_counters)
        assert loaded == {"thread:new": 1}
        assert "thread:topic-a" not in loaded


# ============================================================================
# Increment Access Count Tests
# ============================================================================


class TestIncrementAccessCount:
    """Tests for increment_access_count function."""

    def test_increment_new_thread(self, threads_dir: Path):
        """Test incrementing count for new thread."""
        count = increment_access_count(threads_dir, "thread", "new-topic")
        assert count == 1

        # Verify persisted
        counters = _load_counters(threads_dir)
        assert counters["thread:new-topic"] == 1

    def test_increment_existing_thread(self, threads_dir_with_counters: Path):
        """Test incrementing count for existing thread."""
        count = increment_access_count(threads_dir_with_counters, "thread", "topic-a")
        assert count == 11

        counters = _load_counters(threads_dir_with_counters)
        assert counters["thread:topic-a"] == 11

    def test_increment_new_entry(self, threads_dir: Path):
        """Test incrementing count for new entry."""
        count = increment_access_count(threads_dir, "entry", "01HABC789")
        assert count == 1

    def test_increment_existing_entry(self, threads_dir_with_counters: Path):
        """Test incrementing count for existing entry."""
        count = increment_access_count(threads_dir_with_counters, "entry", "01HXYZ456")
        assert count == 8

    def test_increment_multiple_times(self, threads_dir: Path):
        """Test incrementing same node multiple times."""
        assert increment_access_count(threads_dir, "thread", "test") == 1
        assert increment_access_count(threads_dir, "thread", "test") == 2
        assert increment_access_count(threads_dir, "thread", "test") == 3

    def test_increment_preserves_other_counters(self, threads_dir_with_counters: Path):
        """Test incrementing one counter doesn't affect others."""
        increment_access_count(threads_dir_with_counters, "thread", "topic-a")

        counters = _load_counters(threads_dir_with_counters)
        assert counters["thread:topic-b"] == 5  # Unchanged
        assert counters["entry:01HXYZ123"] == 3  # Unchanged


# ============================================================================
# Get Access Count Tests
# ============================================================================


class TestGetAccessCount:
    """Tests for get_access_count function."""

    def test_get_nonexistent_thread(self, threads_dir: Path):
        """Test getting count for nonexistent thread returns 0."""
        count = get_access_count(threads_dir, "thread", "nonexistent")
        assert count == 0

    def test_get_existing_thread(self, threads_dir_with_counters: Path):
        """Test getting count for existing thread."""
        count = get_access_count(threads_dir_with_counters, "thread", "topic-a")
        assert count == 10

    def test_get_existing_entry(self, threads_dir_with_counters: Path):
        """Test getting count for existing entry."""
        count = get_access_count(threads_dir_with_counters, "entry", "01HXYZ456")
        assert count == 7

    def test_get_nonexistent_entry(self, threads_dir: Path):
        """Test getting count for nonexistent entry returns 0."""
        count = get_access_count(threads_dir, "entry", "nonexistent")
        assert count == 0


# ============================================================================
# Get Most Accessed Tests
# ============================================================================


class TestGetMostAccessed:
    """Tests for get_most_accessed function."""

    def test_empty_counters(self, threads_dir: Path):
        """Test getting most accessed with no counters."""
        results = get_most_accessed(threads_dir)
        assert results == []

    def test_get_all_most_accessed(self, threads_dir_with_counters: Path):
        """Test getting all most accessed nodes."""
        results = get_most_accessed(threads_dir_with_counters)

        # Should be sorted by count descending
        assert len(results) == 4
        assert results[0] == ("thread", "topic-a", 10)
        assert results[1] == ("entry", "01HXYZ456", 7)
        assert results[2] == ("thread", "topic-b", 5)
        assert results[3] == ("entry", "01HXYZ123", 3)

    def test_filter_by_thread_type(self, threads_dir_with_counters: Path):
        """Test filtering by thread type."""
        results = get_most_accessed(threads_dir_with_counters, node_type="thread")

        assert len(results) == 2
        assert results[0] == ("thread", "topic-a", 10)
        assert results[1] == ("thread", "topic-b", 5)

    def test_filter_by_entry_type(self, threads_dir_with_counters: Path):
        """Test filtering by entry type."""
        results = get_most_accessed(threads_dir_with_counters, node_type="entry")

        assert len(results) == 2
        assert results[0] == ("entry", "01HXYZ456", 7)
        assert results[1] == ("entry", "01HXYZ123", 3)

    def test_limit_results(self, threads_dir_with_counters: Path):
        """Test limiting results."""
        results = get_most_accessed(threads_dir_with_counters, limit=2)

        assert len(results) == 2
        assert results[0] == ("thread", "topic-a", 10)
        assert results[1] == ("entry", "01HXYZ456", 7)

    def test_limit_with_type_filter(self, threads_dir_with_counters: Path):
        """Test limiting results with type filter."""
        results = get_most_accessed(threads_dir_with_counters, node_type="thread", limit=1)

        assert len(results) == 1
        assert results[0] == ("thread", "topic-a", 10)

    def test_handles_malformed_keys(self, threads_dir: Path):
        """Test handling of malformed counter keys."""
        counters = {
            "thread:valid": 5,
            "malformed": 10,  # No colon separator
            "entry:also-valid": 3,
        }
        _save_counters(threads_dir, counters)

        results = get_most_accessed(threads_dir)

        # Should only include valid keys
        assert len(results) == 2
        assert results[0] == ("thread", "valid", 5)
        assert results[1] == ("entry", "also-valid", 3)


# ============================================================================
# Integration Tests
# ============================================================================


class TestOdometerIntegration:
    """Integration tests for odometer functions."""

    def test_increment_then_get(self, threads_dir: Path):
        """Test incrementing then getting count."""
        increment_access_count(threads_dir, "thread", "test-topic")
        increment_access_count(threads_dir, "thread", "test-topic")

        count = get_access_count(threads_dir, "thread", "test-topic")
        assert count == 2

    def test_increment_shows_in_most_accessed(self, threads_dir: Path):
        """Test incremented counts show in most accessed."""
        increment_access_count(threads_dir, "thread", "topic-1")
        increment_access_count(threads_dir, "thread", "topic-1")
        increment_access_count(threads_dir, "thread", "topic-1")
        increment_access_count(threads_dir, "thread", "topic-2")

        results = get_most_accessed(threads_dir, node_type="thread")

        assert results[0] == ("thread", "topic-1", 3)
        assert results[1] == ("thread", "topic-2", 1)

    def test_mixed_node_types(self, threads_dir: Path):
        """Test tracking both thread and entry access."""
        increment_access_count(threads_dir, "thread", "my-thread")
        increment_access_count(threads_dir, "entry", "01HABC123")
        increment_access_count(threads_dir, "thread", "my-thread")

        assert get_access_count(threads_dir, "thread", "my-thread") == 2
        assert get_access_count(threads_dir, "entry", "01HABC123") == 1
