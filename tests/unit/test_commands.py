"""Unit tests for watercooler.commands module.

Tests the active command functions. Legacy .md-only commands (init_thread, say,
ack, handoff, set_status, set_ball, append_entry) were removed — their
graph-canonical replacements live in commands_graph.py with tests in
test_commands_graph.py.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from watercooler.commands import list_entries
from watercooler.baseline_graph import storage


def _write_graph_entries(threads_dir: Path, topic: str, meta: dict, entries: list[dict]) -> None:
    """Write graph data (meta.json + entries.jsonl) for testing."""
    graph_dir = storage.ensure_graph_dir(threads_dir)
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, topic)
    storage.atomic_write_json(thread_dir / "meta.json", meta)
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", entries)


@pytest.fixture
def threads_dir(tmp_path):
    """Create a temporary threads directory."""
    d = tmp_path / "threads"
    d.mkdir()
    return d


# ============================================================================
# Test list_entries
# ============================================================================


class TestListEntries:
    """Tests for list_entries function."""

    def test_list_entries_returns_parsed_entries(self, threads_dir):
        """Test list_entries extracts entry metadata correctly from graph."""
        _write_graph_entries(threads_dir, "le-test", {
            "topic": "le-test",
            "title": "le-test",
            "status": "OPEN",
            "ball": "Claude (user)",
            "last_updated": "2025-01-01T13:00:00Z",
        }, [
            {
                "id": "entry:01AAAA0000000000AAAAAAAA01",
                "entry_id": "01AAAA0000000000AAAAAAAA01",
                "title": "First Entry",
                "body": "Body of the first entry.",
                "timestamp": "2025-01-01T12:00:00Z",
                "agent": "Claude (user)",
                "role": "implementer",
                "entry_type": "Note",
                "index": 0,
            },
            {
                "id": "entry:01AAAA0000000000AAAAAAAA02",
                "entry_id": "01AAAA0000000000AAAAAAAA02",
                "title": "Second Entry",
                "body": "Body of the second entry.",
                "timestamp": "2025-01-01T13:00:00Z",
                "agent": "Codex (user)",
                "role": "planner",
                "entry_type": "Plan",
                "index": 1,
            },
        ])

        entries = list_entries("le-test", threads_dir)

        assert len(entries) == 2
        assert entries[0]["entry_id"] == "01AAAA0000000000AAAAAAAA01"
        assert entries[0]["title"] == "First Entry"
        assert entries[0]["timestamp"] == "2025-01-01T12:00:00Z"
        assert "Body of the first entry." in entries[0]["body"]
        assert entries[1]["entry_id"] == "01AAAA0000000000AAAAAAAA02"
        assert entries[1]["title"] == "Second Entry"
        assert entries[1]["timestamp"] == "2025-01-01T13:00:00Z"

    def test_list_entries_missing_thread_returns_empty(self, threads_dir):
        """Test list_entries returns empty list for missing threads."""
        entries = list_entries("nonexistent-topic", threads_dir)
        assert entries == []

    def test_list_entries_coerces_none_to_empty_string(self, threads_dir):
        """Test list_entries converts missing values to empty strings."""
        _write_graph_entries(threads_dir, "minimal", {
            "topic": "minimal",
            "title": "minimal",
            "status": "OPEN",
            "ball": "Agent",
            "last_updated": "2025-01-01T00:00:00Z",
        }, [
            {
                "id": "entry:minimal-0",
                "body": "Minimal entry with no title or entry-id.",
                "timestamp": "2025-01-01T12:00:00Z",
                "agent": "Agent (user)",
                "index": 0,
            },
        ])

        entries = list_entries("minimal", threads_dir)

        assert len(entries) == 1
        assert entries[0]["entry_id"] == ""
        assert entries[0]["title"] == ""
        assert "Minimal entry" in entries[0]["body"]
