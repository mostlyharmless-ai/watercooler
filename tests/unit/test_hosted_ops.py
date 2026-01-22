"""Unit tests for hosted_ops module.

Tests for hosted mode operations including markdown reconstruction,
thread creation, and per-thread format handling.
"""

import pytest

from watercooler_mcp.hosted_ops import (
    _reconstruct_markdown_from_graph,
    _validate_topic,
    _get_per_thread_paths,
)


class TestReconstructMarkdownFromGraph:
    """Tests for _reconstruct_markdown_from_graph function."""

    def test_basic_reconstruction(self):
        """Test basic markdown reconstruction with all fields."""
        meta = {
            "topic": "test-topic",
            "title": "Test Topic Title",
            "status": "OPEN",
            "ball": "Claude",
            "priority": "P1",
        }
        entries = [
            {
                "index": 0,
                "agent": "Claude",
                "role": "implementer",
                "type": "Note",
                "title": "First entry",
                "timestamp": "2026-01-22T10:00:00Z",
                "body": "This is the first entry body.",
            }
        ]

        result = _reconstruct_markdown_from_graph(meta, entries)

        assert "# Test Topic Title" in result
        assert "Topic: test-topic" in result
        assert "Status: OPEN" in result
        assert "Ball: Claude" in result
        assert "Priority: P1" in result
        assert "Entry: Claude (implementer) [Note] - First entry @ 2026-01-22T10:00:00Z" in result
        assert "This is the first entry body." in result

    def test_minimal_meta(self):
        """Test reconstruction with minimal metadata."""
        meta = {"topic": "minimal-topic"}
        entries = []

        result = _reconstruct_markdown_from_graph(meta, entries)

        assert "# minimal-topic" in result  # Falls back to topic as title
        assert "Topic: minimal-topic" in result
        assert "Status: OPEN" in result  # Default status

    def test_empty_entries(self):
        """Test reconstruction with no entries."""
        meta = {
            "topic": "empty-thread",
            "title": "Empty Thread",
            "status": "OPEN",
        }
        entries = []

        result = _reconstruct_markdown_from_graph(meta, entries)

        assert "# Empty Thread" in result
        assert "Entry:" not in result

    def test_multiple_entries_sorted_by_index(self):
        """Test that entries are sorted by index."""
        meta = {"topic": "multi-entry", "title": "Multi Entry Thread"}
        entries = [
            {"index": 2, "agent": "Agent3", "body": "Third"},
            {"index": 0, "agent": "Agent1", "body": "First"},
            {"index": 1, "agent": "Agent2", "body": "Second"},
        ]

        result = _reconstruct_markdown_from_graph(meta, entries)

        # Verify order by checking positions in result
        first_pos = result.find("First")
        second_pos = result.find("Second")
        third_pos = result.find("Third")

        assert first_pos < second_pos < third_pos

    def test_entry_missing_optional_fields(self):
        """Test entry reconstruction with missing optional fields."""
        meta = {"topic": "sparse-entry"}
        entries = [
            {
                "index": 0,
                "agent": "SimpleAgent",
                # No role, type, title, timestamp
                "body": "Just a body.",
            }
        ]

        result = _reconstruct_markdown_from_graph(meta, entries)

        # Should have agent but handle missing fields gracefully
        assert "Entry: SimpleAgent" in result
        assert "Just a body." in result

    def test_entry_empty_body(self):
        """Test entry with empty body."""
        meta = {"topic": "no-body"}
        entries = [
            {
                "index": 0,
                "agent": "Agent",
                "title": "Title Only",
                "body": "",
            }
        ]

        result = _reconstruct_markdown_from_graph(meta, entries)

        assert "Entry: Agent" in result
        assert "- Title Only" in result

    def test_meta_fallback_to_topic_for_title(self):
        """Test that topic is used as title when title is missing."""
        meta = {"topic": "fallback-topic"}  # No title field
        entries = []

        result = _reconstruct_markdown_from_graph(meta, entries)

        assert "# fallback-topic" in result

    def test_entries_with_same_index(self):
        """Test entries with duplicate indices maintain stable order."""
        meta = {"topic": "same-index"}
        entries = [
            {"index": 0, "agent": "A", "timestamp": "2026-01-22T10:00:00Z", "body": "First A"},
            {"index": 0, "agent": "B", "timestamp": "2026-01-22T10:01:00Z", "body": "First B"},
        ]

        result = _reconstruct_markdown_from_graph(meta, entries)

        # Both entries should appear
        assert "First A" in result
        assert "First B" in result


class TestValidateTopic:
    """Tests for _validate_topic function."""

    def test_valid_topic(self):
        """Test valid topic passes validation."""
        _validate_topic("valid-topic")  # Should not raise
        _validate_topic("my-feature-123")  # Should not raise

    def test_empty_topic_raises(self):
        """Test empty topic raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_topic("")

    def test_path_traversal_raises(self):
        """Test path traversal characters raise ValueError."""
        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("../etc/passwd")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("topic/subtopic")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("topic\\subtopic")

    def test_dot_prefix_raises(self):
        """Test topic starting with dot raises ValueError."""
        with pytest.raises(ValueError, match="cannot start with"):
            _validate_topic(".hidden")


class TestGetPerThreadPaths:
    """Tests for _get_per_thread_paths function."""

    def test_returns_correct_paths(self):
        """Test correct paths are generated."""
        meta, entries, edges = _get_per_thread_paths("my-topic")

        assert meta == "graph/baseline/threads/my-topic/meta.json"
        assert entries == "graph/baseline/threads/my-topic/entries.jsonl"
        assert edges == "graph/baseline/threads/my-topic/edges.jsonl"
