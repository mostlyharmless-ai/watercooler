#!/usr/bin/env python3
"""Test Phase 2 LeanRAG backend implementation.

Tests all backend protocol methods:
- search_nodes()
- search_facts()
- get_node()
- get_edge()
- get_capabilities()
- search_episodes() (should raise UnsupportedOperationError)
"""
import pytest
from pathlib import Path

from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig
from watercooler_memory.backends import UnsupportedOperationError


@pytest.fixture
def leanrag_backend(stub_local_memory_servers):
    """Create LeanRAG backend with test-run database.

    Uses stub_local_memory_servers to set required env vars (GLM_MODEL, etc.)
    that LeanRAG's config.yaml substitution requires.
    """
    import os

    project_root = Path(__file__).parent.parent.parent
    leanrag_path = project_root / "external/LeanRAG"

    # Skip if LeanRAG submodule not initialized
    if not leanrag_path.exists():
        pytest.skip(f"LeanRAG submodule not initialized at {leanrag_path}")

    # Skip if process.py not found (incomplete submodule)
    process_script = leanrag_path / "leanrag/pipelines/process.py"
    if not process_script.exists():
        pytest.skip(f"LeanRAG submodule incomplete (missing {process_script})")

    # Check if required dependencies are installed
    try:
        import pymilvus
    except ImportError:
        pytest.skip("LeanRAG dependencies not installed (pymilvus required)")

    config = LeanRAGConfig(
        work_dir=Path.home() / ".watercooler/leanrag/test-run",
        leanrag_path=leanrag_path,
    )
    try:
        return LeanRAGBackend(config)
    except ValueError as e:
        # LeanRAG's config.yaml has many required env vars without defaults
        if "Environment variable" in str(e) and "is not set" in str(e):
            pytest.skip(f"LeanRAG environment not fully configured: {e}")
        raise


@pytest.fixture
def skip_if_no_test_db():
    """Skip test if test-run database doesn't exist."""
    test_db = Path.home() / ".watercooler/leanrag/test-run/threads_chunk.json"
    if not test_db.exists():
        pytest.skip(f"Test database not found: {test_db}")


class TestLeanRAGSearchNodes:
    """Test search_nodes() functionality."""

    def test_search_nodes_returns_valid_results(self, leanrag_backend, skip_if_no_test_db):
        """Test search_nodes returns valid results with correct format."""
        nodes = leanrag_backend.search_nodes(query="Graphiti", max_results=5)

        assert isinstance(nodes, list)
        assert len(nodes) > 0, "Should find Graphiti-related entities"
        assert len(nodes) <= 5, "Should respect max_results limit"

        # Validate response format
        for node in nodes:
            assert "id" in node, "Node must have id"
            assert "name" in node, "Node must have name"
            assert "backend" in node, "Node must have backend"
            assert node["backend"] == "leanrag"
            assert "score" in node, "Node must have score"

    def test_search_nodes_field_types(self, leanrag_backend, skip_if_no_test_db):
        """Test search_nodes response has correct field types."""
        nodes = leanrag_backend.search_nodes(query="memory backend", max_results=5)

        assert isinstance(nodes, list)
        assert len(nodes) > 0, "Should find memory backend entities"

        # Check that nodes have required fields
        for node in nodes:
            assert isinstance(node["id"], str)
            assert isinstance(node["name"], str)
            assert isinstance(node.get("score"), (int, float))


class TestLeanRAGSearchFacts:
    """Test search_facts() functionality with hierarchical traversal."""

    def test_search_facts_returns_valid_results(self, leanrag_backend, skip_if_no_test_db):
        """Test search_facts returns valid results with SOURCE||TARGET format."""
        facts = leanrag_backend.search_facts(query="Graphiti implementation", max_results=5)

        assert isinstance(facts, list)
        # May return 0 if no relationships found - that's ok
        if len(facts) > 0:
            assert len(facts) <= 5, "Should respect max_results limit"

            # Validate response format
            for fact in facts:
                assert "id" in fact, "Fact must have id"
                assert "source_node_id" in fact, "Fact must have source"
                assert "target_node_id" in fact, "Fact must have target"
                assert "backend" in fact, "Fact must have backend"
                assert fact["backend"] == "leanrag"
                assert "score" in fact, "Fact must have score"
                assert "metadata" in fact, "Fact must have metadata"
                assert "level" in fact["metadata"], "Fact metadata must include level"

                # Verify ID format is SOURCE||TARGET
                assert "||" in fact["id"], "Fact ID should be SOURCE||TARGET format"

    def test_search_facts_preserves_directionality(self, leanrag_backend, skip_if_no_test_db):
        """Test search_facts preserves edge directionality in IDs."""
        facts = leanrag_backend.search_facts(query="memory backend", max_results=5)

        assert isinstance(facts, list)
        # Relationships may or may not exist
        if len(facts) > 0:
            # Verify directionality is preserved
            for fact in facts:
                src = fact["source_node_id"]
                tgt = fact["target_node_id"]
                # ID should match the order returned by search_nodes_link
                assert fact["id"] == f"{src}||{tgt}"

    def test_search_facts_sorted_by_score(self, leanrag_backend, skip_if_no_test_db):
        """Test search_facts results are sorted by score descending."""
        facts = leanrag_backend.search_facts(query="MCP server", max_results=5)

        assert isinstance(facts, list)
        # Just verify it doesn't crash
        if len(facts) > 0:
            # Verify sorting by score (descending)
            scores = [f["score"] for f in facts]
            assert scores == sorted(scores, reverse=True), "Facts should be sorted by score"


class TestLeanRAGGetNode:
    """Test get_node() functionality."""

    def test_get_node_by_name(self, leanrag_backend, skip_if_no_test_db):
        """Test retrieving a node by entity name."""
        # First find an entity via search
        nodes = leanrag_backend.search_nodes(query="Graphiti", max_results=1)

        if nodes:
            node_id = nodes[0]["id"]

            # Retrieve the same node by ID
            node = leanrag_backend.get_node(node_id)

            assert node is not None, f"Should retrieve node with ID: {node_id}"
            assert node["id"] == node_id
            assert "name" in node
            assert "backend" in node
            assert node["backend"] == "leanrag"

    def test_get_node_nonexistent(self, leanrag_backend, skip_if_no_test_db):
        """Test retrieving a non-existent node."""
        node = leanrag_backend.get_node("NONEXISTENT_ENTITY_12345")

        assert node is None, "Should return None for non-existent entity"


class TestLeanRAGGetEdge:
    """Test get_edge() functionality."""

    def test_get_edge_by_id(self, leanrag_backend, skip_if_no_test_db):
        """Test retrieving an edge by SOURCE||TARGET ID."""
        # First find a fact via search
        facts = leanrag_backend.search_facts(query="backend", max_results=1)

        if facts:
            edge_id = facts[0]["id"]

            # Retrieve the same edge by ID
            edge = leanrag_backend.get_edge(edge_id)

            assert edge is not None, f"Should retrieve edge with ID: {edge_id}"
            assert edge["id"] == edge_id
            assert "source_node_id" in edge
            assert "target_node_id" in edge
            assert "backend" in edge
            assert edge["backend"] == "leanrag"

    def test_get_edge_invalid_format(self, leanrag_backend, skip_if_no_test_db):
        """Test retrieving an edge with invalid ID format raises error."""
        from watercooler_memory.backends import IdNotSupportedError

        with pytest.raises(IdNotSupportedError) as exc_info:
            leanrag_backend.get_edge("INVALID_ID_NO_SEPARATOR")

        error_msg = str(exc_info.value)
        assert "SOURCE||TARGET" in error_msg, "Error should explain required format"


class TestLeanRAGCapabilities:
    """Test get_capabilities() functionality."""

    def test_get_capabilities(self, leanrag_backend):
        """Test that capabilities are correctly reported."""
        caps = leanrag_backend.get_capabilities()

        # Verify operation support flags (Phase 1 protocol)
        assert caps.supports_nodes is True, "Should support node search"
        assert caps.supports_facts is True, "Should support fact search"
        assert caps.supports_episodes is False, "Should NOT support episodes (no provenance)"
        assert caps.supports_edges is True, "Should support edges via synthetic IDs"

        # Verify ID type flags
        assert caps.node_id_type == "name", "Node IDs are entity names"
        assert caps.edge_id_type == "synthetic", "Edge IDs are SOURCE||TARGET format"

        # Verify core backend features
        assert caps.entity_extraction is True, "Should support entity extraction"
        assert caps.graph_query is True, "Should support graph queries (FalkorDB)"

        # Note: caps.embeddings depends on config.embedding_api_base being set


class TestLeanRAGEpisodes:
    """Test that search_episodes() properly raises UnsupportedOperationError."""

    def test_search_episodes_raises_error(self, leanrag_backend):
        """Test that search_episodes() raises UnsupportedOperationError."""
        with pytest.raises(UnsupportedOperationError) as exc_info:
            leanrag_backend.search_episodes(query="test", max_results=5)

        error_msg = str(exc_info.value)
        assert "LeanRAG backend does not support episode search" in error_msg
        assert "provenance" in error_msg, "Error should explain provenance requirement"
        assert "chunks" in error_msg, "Error should mention chunks"


class TestLeanRAGPerformance:
    """Test that performance caps prevent combinatorial explosion."""

    def test_search_facts_respects_caps(self, leanrag_backend, skip_if_no_test_db):
        """Test that search_facts() doesn't explode on large result sets."""
        # Request many results to test performance caps
        facts = leanrag_backend.search_facts(query="implementation", max_results=100)

        assert isinstance(facts, list)
        assert len(facts) <= 100, "Should respect max_results cap"

        # Verify all facts have required fields (no partial results)
        for fact in facts:
            assert "id" in fact
            assert "source_node_id" in fact
            assert "target_node_id" in fact
            assert "score" in fact
