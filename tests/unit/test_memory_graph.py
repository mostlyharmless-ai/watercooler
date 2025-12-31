"""Tests for watercooler_memory module."""

import json
import pytest
from pathlib import Path

from watercooler_memory import (
    MemoryGraph,
    GraphConfig,
    ThreadNode,
    EntryNode,
    ChunkNode,
    Edge,
    EdgeType,
    parse_thread_to_nodes,
    chunk_text,
    ChunkerConfig,
)
from watercooler_memory.leanrag_export import export_to_leanrag, entry_to_leanrag_document


@pytest.fixture
def sample_thread_content():
    """Sample thread markdown for testing."""
    return """# test-thread — Thread

Status: OPEN
Ball: Claude (user)

---

Entry: Claude (user) 2025-01-01T12:00:00Z
Role: planner
Type: Plan
Title: Initial planning

This is the first entry with a plan for the project.
We need to implement several features.

---

Entry: Cursor (user) 2025-01-01T13:00:00Z
Role: implementer
Type: Note
Title: Implementation started

Started implementing the first feature.
Making good progress.

---
"""


@pytest.fixture
def sample_thread_file(tmp_path, sample_thread_content):
    """Create a sample thread file."""
    threads_dir = tmp_path / ".watercooler"
    threads_dir.mkdir()
    thread_file = threads_dir / "test-thread.md"
    thread_file.write_text(sample_thread_content)
    return thread_file


class TestSchema:
    """Test schema dataclasses."""

    def test_thread_node_creation(self):
        """Test ThreadNode can be created."""
        node = ThreadNode(
            thread_id="test-thread",
            title="Test Thread",
            status="OPEN",
            ball="Claude (user)",
            created_at="2025-01-01T12:00:00Z",
            updated_at="2025-01-01T13:00:00Z",
        )
        assert node.thread_id == "test-thread"
        assert node.node_id == "thread:test-thread"
        assert node.event_time == "2025-01-01T12:00:00Z"

    def test_entry_node_creation(self):
        """Test EntryNode can be created."""
        node = EntryNode(
            entry_id="test-entry-001",
            thread_id="test-thread",
            index=0,
            agent="Claude (user)",
            role="planner",
            entry_type="Note",
            title="Test Entry",
            timestamp="2025-01-01T12:00:00Z",
            body="Test body content",
        )
        assert node.entry_id == "test-entry-001"
        assert node.node_id == "entry:test-entry-001"
        assert node.event_time == "2025-01-01T12:00:00Z"

    def test_chunk_node_creation(self):
        """Test ChunkNode can be created."""
        node = ChunkNode(
            chunk_id="abc123",
            entry_id="test-entry-001",
            thread_id="test-thread",
            index=0,
            text="Chunk text content",
            token_count=4,
        )
        assert node.chunk_id == "abc123"
        assert node.node_id == "chunk:abc123"

    def test_edge_creation(self):
        """Test Edge can be created with factory methods."""
        edge = Edge.contains("thread:test", "entry:001")
        assert edge.edge_type == EdgeType.CONTAINS
        assert edge.source_id == "thread:test"
        assert edge.target_id == "entry:001"

        edge2 = Edge.follows("entry:001", "entry:002")
        assert edge2.edge_type == EdgeType.FOLLOWS


class TestParser:
    """Test thread parsing."""

    def test_parse_thread_to_nodes(self, sample_thread_file):
        """Test parsing a thread file into nodes."""
        thread, entries, edges, hyperedges = parse_thread_to_nodes(sample_thread_file)

        assert thread.thread_id == "test-thread"
        assert thread.status == "OPEN"
        assert thread.ball == "Claude (user)"
        assert len(entries) == 2
        assert len(edges) >= 2  # CONTAINS edges
        assert len(hyperedges) == 1  # Thread membership

    def test_parse_entries_have_correct_metadata(self, sample_thread_file):
        """Test that parsed entries have correct metadata."""
        thread, entries, _, _ = parse_thread_to_nodes(sample_thread_file)

        first_entry = entries[0]
        assert first_entry.agent == "Claude (user)"
        assert first_entry.role == "planner"
        assert first_entry.entry_type == "Plan"
        assert first_entry.title == "Initial planning"

        second_entry = entries[1]
        assert second_entry.agent == "Cursor (user)"
        assert second_entry.role == "implementer"
        assert second_entry.preceding_entry_id is not None


class TestChunker:
    """Test text chunking."""

    def test_chunk_short_text(self):
        """Test that short text returns single chunk."""
        text = "This is a short text."
        chunks = chunk_text(text)
        assert len(chunks) == 1
        assert chunks[0][0] == text

    def test_chunk_empty_text(self):
        """Test that empty text returns empty list."""
        chunks = chunk_text("")
        assert chunks == []

    def test_chunk_long_text(self):
        """Test that long text is split into multiple chunks."""
        # Create text longer than default max_tokens (1024)
        long_text = "This is a test sentence. " * 500
        config = ChunkerConfig(max_tokens=100)
        chunks = chunk_text(long_text, config)
        assert len(chunks) > 1


class TestMemoryGraph:
    """Test MemoryGraph class."""

    def test_empty_graph(self):
        """Test empty graph initialization."""
        graph = MemoryGraph()
        stats = graph.stats()
        assert stats["threads"] == 0
        assert stats["entries"] == 0
        assert stats["chunks"] == 0

    def test_add_thread(self, sample_thread_file):
        """Test adding a thread to the graph."""
        graph = MemoryGraph()
        thread = graph.add_thread(sample_thread_file)

        assert thread.thread_id == "test-thread"
        assert len(graph.threads) == 1
        assert len(graph.entries) == 2

    def test_chunk_entries(self, sample_thread_file):
        """Test chunking all entries in graph."""
        graph = MemoryGraph()
        graph.add_thread(sample_thread_file)
        chunks = graph.chunk_all_entries()

        assert len(chunks) >= 2  # At least one chunk per entry
        assert len(graph.chunks) >= 2

    def test_build_no_api(self, sample_thread_file):
        """Test building graph without API calls."""
        config = GraphConfig(
            generate_summaries=False,
            generate_embeddings=False,
        )
        graph = MemoryGraph(config)
        graph.build(sample_thread_file.parent)

        stats = graph.stats()
        assert stats["threads"] == 1
        assert stats["entries"] == 2
        assert stats["chunks"] >= 2

    def test_to_dict(self, sample_thread_file):
        """Test graph serialization to dict."""
        config = GraphConfig(
            generate_summaries=False,
            generate_embeddings=False,
        )
        graph = MemoryGraph(config)
        graph.build(sample_thread_file.parent)

        data = graph.to_dict()
        assert "threads" in data
        assert "entries" in data
        assert "chunks" in data
        assert "edges" in data
        assert "hyperedges" in data

    def test_save_and_load(self, sample_thread_file, tmp_path):
        """Test saving and loading graph."""
        config = GraphConfig(
            generate_summaries=False,
            generate_embeddings=False,
        )
        graph = MemoryGraph(config)
        graph.build(sample_thread_file.parent)

        output_path = tmp_path / "graph.json"
        graph.save(output_path)

        loaded = MemoryGraph.load(output_path)
        assert len(loaded.threads) == len(graph.threads)
        assert len(loaded.entries) == len(graph.entries)


class TestLeanRAGExport:
    """Test LeanRAG export functionality."""

    def test_entry_to_leanrag_document(self):
        """Test converting entry to LeanRAG document."""
        entry = EntryNode(
            entry_id="test-001",
            thread_id="test-thread",
            index=0,
            agent="Claude",
            role="planner",
            entry_type="Note",
            title="Test",
            timestamp="2025-01-01T12:00:00Z",
            body="Test body",
        )
        chunks = [
            ChunkNode(
                chunk_id="chunk-001",
                entry_id="test-001",
                thread_id="test-thread",
                index=0,
                text="Test body",
                token_count=2,
            )
        ]

        doc = entry_to_leanrag_document(entry, chunks)

        assert doc["doc_id"] == "test-001"
        assert doc["title"] == "Test"
        assert doc["content"] == "Test body"
        assert len(doc["chunks"]) == 1
        assert doc["metadata"]["thread_id"] == "test-thread"
        assert doc["metadata"]["agent"] == "Claude"

    def test_export_to_leanrag(self, sample_thread_file, tmp_path):
        """Test full LeanRAG export."""
        config = GraphConfig(
            generate_summaries=False,
            generate_embeddings=False,
        )
        graph = MemoryGraph(config)
        graph.build(sample_thread_file.parent)

        export_dir = tmp_path / "leanrag_export"
        manifest = export_to_leanrag(graph, export_dir, include_embeddings=False)

        assert (export_dir / "documents.json").exists()
        assert (export_dir / "threads.json").exists()
        assert (export_dir / "manifest.json").exists()

        assert manifest["statistics"]["documents"] == 2
        assert manifest["statistics"]["threads"] == 1
