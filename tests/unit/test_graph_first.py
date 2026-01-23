"""Tests for graph-first command implementations.

Tests the writer.py, projector.py, and commands_graph.py modules
that implement the graph-first architecture.
"""

import json
import pytest
from pathlib import Path
from ulid import ULID

from watercooler.baseline_graph.writer import (
    ThreadData,
    EntryData,
    upsert_thread_node,
    upsert_entry_node,
    update_thread_metadata,
    get_thread_from_graph,
    get_entry_node_from_graph,
    get_entries_for_thread,
    get_last_entry_id,
    get_next_entry_index,
    init_thread_in_graph,
)
from watercooler.baseline_graph.projector import (
    project_entry_to_markdown,
    project_thread_to_markdown,
    project_and_write_thread,
    update_header_and_write,
    create_thread_file,
)
from watercooler.commands_graph import (
    say_graph_first,
    ack_graph_first,
    handoff_graph_first,
    set_status_graph_first,
    set_ball_graph_first,
    init_thread_graph_first,
)


class TestWriterModule:
    """Tests for baseline_graph/writer.py."""

    def test_upsert_thread_node(self, tmp_path):
        """Test creating a thread node."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        data = ThreadData(
            topic="test-thread",
            title="Test Thread",
            status="OPEN",
            ball="Claude",
            summary="A test thread",
            entry_count=0,
        )

        result = upsert_thread_node(threads_dir, data)
        assert result is True

        # Verify node exists
        thread = get_thread_from_graph(threads_dir, "test-thread")
        assert thread is not None
        assert thread["topic"] == "test-thread"
        assert thread["title"] == "Test Thread"
        assert thread["status"] == "OPEN"
        assert thread["ball"] == "Claude"

    def test_upsert_entry_node(self, tmp_path):
        """Test creating an entry node."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create thread first
        thread_data = ThreadData(
            topic="test-thread",
            title="Test Thread",
        )
        upsert_thread_node(threads_dir, thread_data)

        # Create entry
        entry_id = str(ULID())
        entry_data = EntryData(
            entry_id=entry_id,
            thread_topic="test-thread",
            index=0,
            agent="Claude",
            role="implementer",
            entry_type="Note",
            title="Test Entry",
            body="Test body content",
        )

        result = upsert_entry_node(threads_dir, entry_data)
        assert result is True

        # Verify entry exists
        entry = get_entry_node_from_graph(threads_dir, entry_id)
        assert entry is not None
        assert entry["entry_id"] == entry_id
        assert entry["agent"] == "Claude"
        assert entry["title"] == "Test Entry"

    def test_update_thread_metadata(self, tmp_path):
        """Test updating thread metadata."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create thread
        data = ThreadData(topic="test-thread", title="Test Thread", status="OPEN")
        upsert_thread_node(threads_dir, data)

        # Update status
        result = update_thread_metadata(
            threads_dir,
            "test-thread",
            status="CLOSED",
            ball="User",
        )
        assert result is True

        # Verify updates
        thread = get_thread_from_graph(threads_dir, "test-thread")
        assert thread["status"] == "CLOSED"
        assert thread["ball"] == "User"

    def test_get_entries_for_thread(self, tmp_path):
        """Test getting all entries for a thread."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create thread
        thread_data = ThreadData(topic="test-thread", title="Test")
        upsert_thread_node(threads_dir, thread_data)

        # Create entries
        for i in range(3):
            entry_data = EntryData(
                entry_id=str(ULID()),
                thread_topic="test-thread",
                index=i,
                agent="Agent",
                role="implementer",
                entry_type="Note",
                title=f"Entry {i}",
                body=f"Body {i}",
            )
            upsert_entry_node(threads_dir, entry_data)

        entries = get_entries_for_thread(threads_dir, "test-thread")
        assert len(entries) == 3
        assert entries[0]["index"] == 0
        assert entries[2]["index"] == 2

    def test_get_next_entry_index(self, tmp_path):
        """Test getting next entry index."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # No entries - should be 0
        assert get_next_entry_index(threads_dir, "test-thread") == 0

        # Create thread and entry
        thread_data = ThreadData(topic="test-thread", title="Test")
        upsert_thread_node(threads_dir, thread_data)

        entry_data = EntryData(
            entry_id=str(ULID()),
            thread_topic="test-thread",
            index=0,
            agent="Agent",
            role="implementer",
            entry_type="Note",
            title="Entry 0",
            body="Body",
        )
        upsert_entry_node(threads_dir, entry_data)

        # Next should be 1
        assert get_next_entry_index(threads_dir, "test-thread") == 1


class TestProjectorModule:
    """Tests for baseline_graph/projector.py."""

    def test_project_entry_to_markdown(self):
        """Test projecting entry node to markdown."""
        entry = {
            "agent": "Claude",
            "timestamp": "2024-01-01T00:00:00Z",
            "role": "implementer",
            "entry_type": "Note",
            "title": "Test Entry",
            "body": "Test body content",
            "entry_id": "test-entry-id",
        }

        md = project_entry_to_markdown(entry)

        assert "Entry: Claude 2024-01-01T00:00:00Z" in md
        assert "Role: implementer" in md
        assert "Type: Note" in md
        assert "Title: Test Entry" in md
        assert "Test body content" in md
        assert "<!-- Entry-ID: test-entry-id -->" in md

    def test_project_thread_to_markdown(self):
        """Test projecting thread with entries to markdown."""
        thread = {
            "topic": "test-thread",
            "status": "OPEN",
            "ball": "Claude",
            "last_updated": "2024-01-01T00:00:00Z",
        }
        entries = [
            {
                "agent": "Claude",
                "timestamp": "2024-01-01T00:00:00Z",
                "role": "implementer",
                "entry_type": "Note",
                "title": "Entry 1",
                "body": "Body 1",
                "entry_id": "entry-1",
            },
        ]

        md = project_thread_to_markdown(thread, entries)

        assert "# test-thread — Thread" in md
        assert "Status: OPEN" in md
        assert "Ball: Claude" in md
        assert "Entry: Claude" in md
        assert "Body 1" in md

    def test_create_thread_file(self, tmp_path):
        """Test creating a new thread file."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        path = create_thread_file(
            threads_dir,
            "test-thread",
            title="Test Thread",
            status="OPEN",
            ball="User",
        )

        assert path.exists()
        content = path.read_text()
        assert "# test-thread — Thread" in content
        assert "Status: OPEN" in content
        assert "Ball: User" in content

    def test_update_header_and_write(self, tmp_path):
        """Test updating header fields in existing file."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create initial file
        create_thread_file(threads_dir, "test-thread", status="OPEN", ball="User")

        # Update status
        update_header_and_write(threads_dir, "test-thread", status="CLOSED")

        content = (threads_dir / "test-thread.md").read_text()
        assert "Status: CLOSED" in content
        assert "Ball: User" in content  # Ball unchanged


class TestCommandsGraphModule:
    """Tests for commands_graph.py graph-first commands."""

    def test_init_thread_graph_first(self, tmp_path):
        """Test initializing thread graph-first."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        path = init_thread_graph_first(
            "test-thread",
            threads_dir=threads_dir,
            title="Test Thread",
            status="OPEN",
            ball="Claude",
        )

        # Verify MD file exists
        assert path.exists()
        content = path.read_text()
        assert "# test-thread — Thread" in content

        # Verify graph node exists
        thread = get_thread_from_graph(threads_dir, "test-thread")
        assert thread is not None
        assert thread["status"] == "OPEN"

    def test_say_graph_first(self, tmp_path):
        """Test say command graph-first."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        entry_id = str(ULID())
        path = say_graph_first(
            "test-thread",
            threads_dir=threads_dir,
            agent="Claude",
            role="implementer",
            title="Test Entry",
            body="Test body content",
            entry_id=entry_id,
        )

        # Verify MD file has entry
        assert path.exists()
        content = path.read_text()
        assert "Test body content" in content
        assert entry_id in content

        # Verify graph has entry
        entry = get_entry_node_from_graph(threads_dir, entry_id)
        assert entry is not None
        assert entry["title"] == "Test Entry"

    def test_ack_graph_first(self, tmp_path):
        """Test ack command graph-first."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Initialize thread
        init_thread_graph_first("test-thread", threads_dir=threads_dir, ball="User")

        # Ack
        entry_id = str(ULID())
        path = ack_graph_first(
            "test-thread",
            threads_dir=threads_dir,
            agent="Claude",
            title="Got it",
            body="Acknowledged",
            entry_id=entry_id,
        )

        assert path.exists()

        # Ball should remain unchanged for ack
        thread = get_thread_from_graph(threads_dir, "test-thread")
        assert thread["ball"] == "User"

    def test_set_status_graph_first(self, tmp_path):
        """Test set_status command graph-first."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Initialize thread
        init_thread_graph_first("test-thread", threads_dir=threads_dir, status="OPEN")

        # Change status
        set_status_graph_first("test-thread", threads_dir=threads_dir, status="CLOSED")

        # Verify graph updated
        thread = get_thread_from_graph(threads_dir, "test-thread")
        assert thread["status"] == "CLOSED"

        # Verify MD updated
        content = (threads_dir / "test-thread.md").read_text()
        assert "Status: CLOSED" in content

    def test_set_ball_graph_first(self, tmp_path):
        """Test set_ball command graph-first."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Initialize thread
        init_thread_graph_first("test-thread", threads_dir=threads_dir, ball="User")

        # Change ball
        set_ball_graph_first("test-thread", threads_dir=threads_dir, ball="Claude")

        # Verify graph updated
        thread = get_thread_from_graph(threads_dir, "test-thread")
        assert thread["ball"] == "Claude"

        # Verify MD updated
        content = (threads_dir / "test-thread.md").read_text()
        assert "Ball: Claude" in content

    def test_handoff_graph_first(self, tmp_path):
        """Test handoff command graph-first."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Initialize thread with ball=Claude
        init_thread_graph_first("test-thread", threads_dir=threads_dir, ball="Claude")

        # Handoff (should flip to counterpart)
        entry_id = str(ULID())
        handoff_graph_first(
            "test-thread",
            threads_dir=threads_dir,
            agent="Claude",
            note="Your turn",
            entry_id=entry_id,
        )

        # Ball should flip to counterpart (which depends on agent registry)
        # The key assertion is that the ball changed from "Claude"
        thread = get_thread_from_graph(threads_dir, "test-thread")
        assert thread["ball"] != "Claude"  # Ball flipped to some counterpart

        # Verify handoff entry exists
        entries = get_entries_for_thread(threads_dir, "test-thread")
        assert len(entries) == 1
        assert "Handoff to" in entries[0]["title"]


class TestEnrichGraphEntry:
    """Tests for enrich_graph_entry() - graph-first enrichment."""

    def test_enrich_graph_entry_reads_from_graph(self, tmp_path):
        """Test that enrich_graph_entry reads entry from graph, not markdown."""
        from watercooler.baseline_graph.sync import enrich_graph_entry

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create thread and entry via graph-first
        entry_id = str(ULID())
        say_graph_first(
            "test-thread",
            threads_dir=threads_dir,
            agent="Claude",
            role="implementer",
            title="Test Entry",
            body="This is test content for enrichment.",
            entry_id=entry_id,
        )

        # Verify entry exists in graph
        entry = get_entry_node_from_graph(threads_dir, entry_id)
        assert entry is not None
        assert entry["body"] == "This is test content for enrichment."

        # Call enrich (without services, should still succeed)
        result = enrich_graph_entry(
            threads_dir=threads_dir,
            topic="test-thread",
            entry_id=entry_id,
            generate_summaries=False,  # Skip to avoid service deps
            generate_embeddings=False,
        )
        assert result is True

    def test_enrich_graph_entry_missing_entry_returns_false(self, tmp_path):
        """Test that enrich_graph_entry returns False for missing entry."""
        from watercooler.baseline_graph.sync import enrich_graph_entry

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        result = enrich_graph_entry(
            threads_dir=threads_dir,
            topic="nonexistent",
            entry_id="nonexistent-id",
            generate_summaries=False,
            generate_embeddings=False,
        )
        assert result is False


# NOTE: TestFeatureFlag class removed - graph-first mode is now always enabled
# and the WATERCOOLER_GRAPH_FIRST env var has been deprecated.
