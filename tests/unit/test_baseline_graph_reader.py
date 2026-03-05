"""Tests for baseline graph reader module.

Tests the graph-first read operations using per-thread format.
"""

import json
import pytest
from pathlib import Path

from watercooler.baseline_graph.reader import (
    GraphThread,
    GraphEntry,
    is_graph_available,
    get_graph_staleness,
    list_threads_from_graph,
    read_thread_from_graph,
    get_entry_from_graph,
    get_entries_range_from_graph,
    format_thread_markdown,
    format_entry_json,
    get_graph_dir,
    get_thread_graph_dir,
)


def _create_per_thread_graph(graph_dir: Path, topic: str, thread_node: dict, entries: list = None):
    """Helper to create per-thread graph files.

    Args:
        graph_dir: Base graph directory (graph/baseline)
        topic: Thread topic
        thread_node: Thread node dict to write to meta.json
        entries: List of entry node dicts to write to entries.jsonl
    """
    thread_graph_dir = graph_dir / "threads" / topic
    thread_graph_dir.mkdir(parents=True, exist_ok=True)

    # Write meta.json
    (thread_graph_dir / "meta.json").write_text(json.dumps(thread_node, indent=2))

    # Write entries.jsonl if provided
    if entries:
        lines = [json.dumps(e) for e in entries]
        (thread_graph_dir / "entries.jsonl").write_text("\n".join(lines) + "\n")


class TestGraphAvailability:
    """Tests for graph availability checking."""

    def test_is_graph_available_no_graph_dir(self, tmp_path):
        """Returns False when graph directory doesn't exist."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        assert is_graph_available(threads_dir) is False

    def test_is_graph_available_empty_threads_dir(self, tmp_path):
        """Returns False when threads/ directory is empty."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"
        threads_base = graph_dir / "threads"
        threads_base.mkdir(parents=True)
        assert is_graph_available(threads_dir) is False

    def test_is_graph_available_valid_graph(self, tmp_path):
        """Returns True when valid per-thread graph data exists."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        # Create per-thread format
        thread_node = {"type": "thread", "topic": "test", "title": "Test Thread"}
        _create_per_thread_graph(graph_dir, "test", thread_node)

        assert is_graph_available(threads_dir) is True

    def test_is_graph_available_invalid_json(self, tmp_path):
        """Returns False when meta.json contains invalid JSON."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"
        thread_graph_dir = graph_dir / "threads" / "test"
        thread_graph_dir.mkdir(parents=True)
        (thread_graph_dir / "meta.json").write_text("not valid json\n")

        assert is_graph_available(threads_dir) is False


class TestListThreadsFromGraph:
    """Tests for listing threads from graph."""

    def test_list_threads_empty_graph(self, tmp_path):
        """Returns empty list when graph has no threads."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"
        threads_base = graph_dir / "threads"
        threads_base.mkdir(parents=True)

        result = list_threads_from_graph(threads_dir)
        assert result == []

    def test_list_threads_single_thread(self, tmp_path):
        """Returns single thread from graph."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        thread_node = {
            "type": "thread",
            "topic": "feature-auth",
            "title": "Auth Feature",
            "status": "OPEN",
            "ball": "claude",
            "last_updated": "2025-01-01T00:00:00Z",
            "summary": "Implementing auth",
            "entry_count": 3,
        }
        _create_per_thread_graph(graph_dir, "feature-auth", thread_node)

        result = list_threads_from_graph(threads_dir)
        assert len(result) == 1
        assert result[0].topic == "feature-auth"
        assert result[0].title == "Auth Feature"
        assert result[0].status == "OPEN"
        assert result[0].ball == "claude"

    def test_list_threads_filters_by_status(self, tmp_path):
        """Filters threads by open_only parameter."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        # Create two threads with different statuses
        _create_per_thread_graph(graph_dir, "open-thread", {
            "type": "thread", "topic": "open-thread", "status": "OPEN",
            "title": "Open", "ball": "", "last_updated": "", "summary": "", "entry_count": 0
        })
        _create_per_thread_graph(graph_dir, "closed-thread", {
            "type": "thread", "topic": "closed-thread", "status": "CLOSED",
            "title": "Closed", "ball": "", "last_updated": "", "summary": "", "entry_count": 0
        })

        # All threads
        all_result = list_threads_from_graph(threads_dir, open_only=None)
        assert len(all_result) == 2

        # Open only
        open_result = list_threads_from_graph(threads_dir, open_only=True)
        assert len(open_result) == 1
        assert open_result[0].topic == "open-thread"

        # Closed only
        closed_result = list_threads_from_graph(threads_dir, open_only=False)
        assert len(closed_result) == 1
        assert closed_result[0].topic == "closed-thread"

    def test_list_threads_ignores_entry_nodes(self, tmp_path):
        """Returns thread from per-thread format (entries are separate)."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        thread_node = {
            "type": "thread", "topic": "test", "title": "Test",
            "status": "OPEN", "ball": "", "last_updated": "", "summary": "", "entry_count": 1
        }
        entries = [
            {"type": "entry", "thread_topic": "test", "entry_id": "123", "index": 0}
        ]
        _create_per_thread_graph(graph_dir, "test", thread_node, entries)

        result = list_threads_from_graph(threads_dir)
        assert len(result) == 1
        assert result[0].topic == "test"


class TestReadThreadFromGraph:
    """Tests for reading full thread from graph."""

    def test_read_thread_not_found(self, tmp_path):
        """Returns None when thread not found."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"
        threads_base = graph_dir / "threads"
        threads_base.mkdir(parents=True)

        result = read_thread_from_graph(threads_dir, "nonexistent")
        assert result is None

    def test_read_thread_with_entries(self, tmp_path):
        """Returns thread with all entries."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        thread_node = {
            "type": "thread", "topic": "test", "title": "Test Thread",
            "status": "OPEN", "ball": "claude", "last_updated": "2025-01-01T00:00:00Z",
            "summary": "Test", "entry_count": 2
        }
        entries = [
            {"type": "entry", "thread_topic": "test", "entry_id": "entry1", "index": 0,
             "agent": "Claude", "role": "implementer", "entry_type": "Note",
             "title": "First Entry", "timestamp": "2025-01-01T00:00:00Z", "summary": "First"},
            {"type": "entry", "thread_topic": "test", "entry_id": "entry2", "index": 1,
             "agent": "User", "role": "pm", "entry_type": "Decision",
             "title": "Second Entry", "timestamp": "2025-01-01T01:00:00Z", "summary": "Second"},
        ]
        _create_per_thread_graph(graph_dir, "test", thread_node, entries)

        result = read_thread_from_graph(threads_dir, "test")
        assert result is not None

        thread, read_entries = result
        assert thread.topic == "test"
        assert thread.title == "Test Thread"
        assert len(read_entries) == 2
        assert read_entries[0].entry_id == "entry1"
        assert read_entries[0].index == 0
        assert read_entries[1].entry_id == "entry2"
        assert read_entries[1].index == 1

    def test_read_thread_entries_sorted_by_index(self, tmp_path):
        """Entries are returned sorted by index."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        thread_node = {
            "type": "thread", "topic": "test", "title": "Test",
            "status": "OPEN", "ball": "", "last_updated": "", "summary": "", "entry_count": 3
        }
        # Write entries out of order
        entries = [
            {"type": "entry", "thread_topic": "test", "entry_id": "e2", "index": 2,
             "agent": "", "role": "", "entry_type": "Note", "title": "Third", "timestamp": "", "summary": ""},
            {"type": "entry", "thread_topic": "test", "entry_id": "e0", "index": 0,
             "agent": "", "role": "", "entry_type": "Note", "title": "First", "timestamp": "", "summary": ""},
            {"type": "entry", "thread_topic": "test", "entry_id": "e1", "index": 1,
             "agent": "", "role": "", "entry_type": "Note", "title": "Second", "timestamp": "", "summary": ""},
        ]
        _create_per_thread_graph(graph_dir, "test", thread_node, entries)

        result = read_thread_from_graph(threads_dir, "test")
        assert result is not None
        _, read_entries = result

        assert [e.index for e in read_entries] == [0, 1, 2]
        assert [e.title for e in read_entries] == ["First", "Second", "Third"]


class TestGetEntryFromGraph:
    """Tests for getting single entry from graph."""

    def test_get_entry_by_id(self, tmp_path):
        """Gets entry by entry_id."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        thread_node = {"type": "thread", "topic": "test", "title": "Test",
                       "status": "OPEN", "ball": "", "last_updated": "", "summary": "", "entry_count": 2}
        entries = [
            {"type": "entry", "thread_topic": "test", "entry_id": "target", "index": 1,
             "agent": "Claude", "role": "implementer", "entry_type": "Note",
             "title": "Target", "timestamp": "2025-01-01T00:00:00Z", "summary": "Found it"},
            {"type": "entry", "thread_topic": "test", "entry_id": "other", "index": 0,
             "agent": "User", "role": "pm", "entry_type": "Note",
             "title": "Other", "timestamp": "", "summary": ""},
        ]
        _create_per_thread_graph(graph_dir, "test", thread_node, entries)

        result = get_entry_from_graph(threads_dir, "test", entry_id="target")
        assert result is not None
        assert result.entry_id == "target"
        assert result.title == "Target"

    def test_get_entry_by_index(self, tmp_path):
        """Gets entry by index."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        thread_node = {"type": "thread", "topic": "test", "title": "Test",
                       "status": "OPEN", "ball": "", "last_updated": "", "summary": "", "entry_count": 2}
        entries = [
            {"type": "entry", "thread_topic": "test", "entry_id": "e0", "index": 0,
             "agent": "", "role": "", "entry_type": "Note", "title": "First", "timestamp": "", "summary": ""},
            {"type": "entry", "thread_topic": "test", "entry_id": "e1", "index": 1,
             "agent": "", "role": "", "entry_type": "Note", "title": "Second", "timestamp": "", "summary": ""},
        ]
        _create_per_thread_graph(graph_dir, "test", thread_node, entries)

        result = get_entry_from_graph(threads_dir, "test", index=1)
        assert result is not None
        assert result.entry_id == "e1"
        assert result.title == "Second"

    def test_get_entry_not_found(self, tmp_path):
        """Returns None when entry not found."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"
        thread_node = {"type": "thread", "topic": "test", "title": "Test",
                       "status": "OPEN", "ball": "", "last_updated": "", "summary": "", "entry_count": 0}
        _create_per_thread_graph(graph_dir, "test", thread_node, [])

        result = get_entry_from_graph(threads_dir, "test", entry_id="nonexistent")
        assert result is None

    def test_get_entry_no_params_returns_none(self, tmp_path):
        """Returns None when neither entry_id nor index provided."""
        threads_dir = tmp_path / "threads"
        result = get_entry_from_graph(threads_dir, "test")
        assert result is None


class TestGetEntriesRangeFromGraph:
    """Tests for getting range of entries from graph."""

    def test_get_entries_range(self, tmp_path):
        """Gets entries in specified range."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        thread_node = {"type": "thread", "topic": "test", "title": "Test",
                       "status": "OPEN", "ball": "", "last_updated": "", "summary": "", "entry_count": 5}
        entries = [
            {"type": "entry", "thread_topic": "test", "entry_id": f"e{i}", "index": i,
             "agent": "", "role": "", "entry_type": "Note", "title": f"Entry {i}", "timestamp": "", "summary": ""}
            for i in range(5)
        ]
        _create_per_thread_graph(graph_dir, "test", thread_node, entries)

        result = get_entries_range_from_graph(threads_dir, "test", start_index=1, end_index=3)
        assert len(result) == 3
        assert [e.index for e in result] == [1, 2, 3]

    def test_get_entries_range_no_end(self, tmp_path):
        """Gets all entries from start when no end specified."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        thread_node = {"type": "thread", "topic": "test", "title": "Test",
                       "status": "OPEN", "ball": "", "last_updated": "", "summary": "", "entry_count": 5}
        entries = [
            {"type": "entry", "thread_topic": "test", "entry_id": f"e{i}", "index": i,
             "agent": "", "role": "", "entry_type": "Note", "title": f"Entry {i}", "timestamp": "", "summary": ""}
            for i in range(5)
        ]
        _create_per_thread_graph(graph_dir, "test", thread_node, entries)

        result = get_entries_range_from_graph(threads_dir, "test", start_index=2)
        assert len(result) == 3
        assert [e.index for e in result] == [2, 3, 4]

    def test_get_entries_range_sorted(self, tmp_path):
        """Entries are returned sorted by index."""
        threads_dir = tmp_path / "threads"
        graph_dir = threads_dir / "graph" / "baseline"

        thread_node = {"type": "thread", "topic": "test", "title": "Test",
                       "status": "OPEN", "ball": "", "last_updated": "", "summary": "", "entry_count": 3}
        # Write entries out of order
        entries = [
            {"type": "entry", "thread_topic": "test", "entry_id": "e2", "index": 2,
             "agent": "", "role": "", "entry_type": "Note", "title": "", "timestamp": "", "summary": ""},
            {"type": "entry", "thread_topic": "test", "entry_id": "e0", "index": 0,
             "agent": "", "role": "", "entry_type": "Note", "title": "", "timestamp": "", "summary": ""},
            {"type": "entry", "thread_topic": "test", "entry_id": "e1", "index": 1,
             "agent": "", "role": "", "entry_type": "Note", "title": "", "timestamp": "", "summary": ""},
        ]
        _create_per_thread_graph(graph_dir, "test", thread_node, entries)

        result = get_entries_range_from_graph(threads_dir, "test")
        assert [e.index for e in result] == [0, 1, 2]


class TestFormatFunctions:
    """Tests for format conversion functions."""

    def test_format_entry_json(self):
        """Converts GraphEntry to JSON-serializable dict."""
        entry = GraphEntry(
            entry_id="test-id",
            thread_topic="test-thread",
            index=0,
            agent="Claude",
            role="implementer",
            entry_type="Note",
            title="Test Entry",
            timestamp="2025-01-01T00:00:00Z",
            summary="A test entry",
            body="Full body content",
            file_refs=["src/main.py"],
            pr_refs=["#123"],
            commit_refs=["abc123"],
        )

        result = format_entry_json(entry)

        assert result["entry_id"] == "test-id"
        assert result["thread_topic"] == "test-thread"
        assert result["index"] == 0
        assert result["agent"] == "Claude"
        assert result["role"] == "implementer"
        assert result["entry_type"] == "Note"
        assert result["title"] == "Test Entry"
        assert result["body"] == "Full body content"
        assert result["file_refs"] == ["src/main.py"]

    def test_format_thread_markdown(self):
        """Formats thread and entries as markdown."""
        thread = GraphThread(
            topic="test-thread",
            title="Test Thread",
            status="OPEN",
            ball="claude",
            last_updated="2025-01-01T00:00:00Z",
            summary="A test thread",
            entry_count=1,
        )

        entries = [
            GraphEntry(
                entry_id="e1",
                thread_topic="test-thread",
                index=0,
                agent="Claude",
                role="implementer",
                entry_type="Note",
                title="First Entry",
                timestamp="2025-01-01T00:00:00Z",
                summary="First",
                body="Entry body content",
            ),
        ]

        result = format_thread_markdown(thread, entries)

        assert "# test-thread — Thread" in result
        assert "Status: OPEN" in result
        assert "Ball: claude" in result
        assert "Entry: Claude 2025-01-01T00:00:00Z" in result
        assert "Title: First Entry" in result
        assert "Entry body content" in result


class TestGraphEntryDefaults:
    """Tests for GraphEntry default values."""

    def test_default_refs_are_empty_lists(self):
        """Default file_refs, pr_refs, commit_refs are empty lists."""
        entry = GraphEntry(
            entry_id="test",
            thread_topic="test",
            index=0,
            agent="Claude",
            role="implementer",
            entry_type="Note",
            title="Test",
            timestamp="",
            summary="",
        )

        assert entry.file_refs == []
        assert entry.pr_refs == []
        assert entry.commit_refs == []

    def test_none_refs_become_empty_lists(self):
        """None values for refs become empty lists via __post_init__."""
        entry = GraphEntry(
            entry_id="test",
            thread_topic="test",
            index=0,
            agent="Claude",
            role="implementer",
            entry_type="Note",
            title="Test",
            timestamp="",
            summary="",
            file_refs=None,
            pr_refs=None,
            commit_refs=None,
        )

        assert entry.file_refs == []
        assert entry.pr_refs == []
        assert entry.commit_refs == []
