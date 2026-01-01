"""Tests for FalkorDB vector adapter.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 2.1:
Reference patterns from Graphiti:
- Storage: SET n.embedding = vecf32($embedding)
- Search: (2 - vec.cosineDistance(n.embedding, vecf32($query_vector)))/2 AS score
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from dataclasses import dataclass

from watercooler_memory.infrastructure.embedding_validator import EXPECTED_DIM
from watercooler_memory.infrastructure.falkordb_vectors import (
    FalkorDBVectorAdapter,
    FalkorDBVectorConfig,
    VectorSearchResult,
    build_storage_query,
    build_search_query,
    normalize_score,
)


class TestFalkorDBVectorConfig:
    """Test FalkorDB vector adapter configuration."""

    def test_default_config(self):
        """Test default configuration values."""
        config = FalkorDBVectorConfig()
        assert config.host == "localhost"
        assert config.port == 6379
        assert config.database == "default"
        assert config.embedding_dim == EXPECTED_DIM  # 1024

    def test_config_from_env(self, monkeypatch):
        """Test config loads from environment variables."""
        monkeypatch.setenv("FALKORDB_HOST", "redis.example.com")
        monkeypatch.setenv("FALKORDB_PORT", "7379")
        monkeypatch.setenv("FALKORDB_DATABASE", "test_db")

        config = FalkorDBVectorConfig.from_env()
        assert config.host == "redis.example.com"
        assert config.port == 7379
        assert config.database == "test_db"


class TestQueryBuilders:
    """Test query builder functions."""

    def test_build_storage_query_basic(self):
        """Test basic storage query generation."""
        query = build_storage_query(
            node_label="Chunk",
            id_field="chunk_id",
            embedding_field="embedding",
        )
        assert "MERGE" in query
        assert "vecf32($embedding)" in query
        assert "Chunk" in query
        assert "chunk_id" in query

    def test_build_storage_query_with_properties(self):
        """Test storage query with additional properties."""
        query = build_storage_query(
            node_label="Entry",
            id_field="entry_id",
            embedding_field="embedding",
            additional_props=["text", "metadata"],
        )
        assert "text" in query
        assert "metadata" in query

    def test_build_search_query_basic(self):
        """Test basic vector search query generation."""
        query = build_search_query(
            node_label="Chunk",
            embedding_field="embedding",
            return_fields=["chunk_id", "text"],
        )
        assert "vec.cosineDistance" in query
        assert "Chunk" in query
        assert "ORDER BY score DESC" in query
        assert "LIMIT $limit" in query

    def test_build_search_query_with_filter(self):
        """Test search query with WHERE clause filter."""
        query = build_search_query(
            node_label="Chunk",
            embedding_field="embedding",
            return_fields=["chunk_id"],
            where_clause="n.thread_id = $thread_id",
        )
        assert "WHERE" in query
        assert "n.thread_id = $thread_id" in query


class TestNormalizeScore:
    """Test score normalization."""

    def test_normalize_identical_vectors(self):
        """Identical vectors should have score ~1.0."""
        # cosineDistance of 0 → score of 1.0
        score = normalize_score(0.0)
        assert score == pytest.approx(1.0)

    def test_normalize_orthogonal_vectors(self):
        """Orthogonal vectors should have score ~0.5."""
        # cosineDistance of 1 → score of 0.5
        score = normalize_score(1.0)
        assert score == pytest.approx(0.5)

    def test_normalize_opposite_vectors(self):
        """Opposite vectors should have score ~0.0."""
        # cosineDistance of 2 → score of 0.0
        score = normalize_score(2.0)
        assert score == pytest.approx(0.0)


class TestVectorSearchResult:
    """Test VectorSearchResult dataclass."""

    def test_result_creation(self):
        """Test creating search result."""
        result = VectorSearchResult(
            node_id="chunk:abc123",
            score=0.95,
            properties={"text": "Hello world"},
        )
        assert result.node_id == "chunk:abc123"
        assert result.score == 0.95
        assert result.properties["text"] == "Hello world"


class TestFalkorDBVectorAdapter:
    """Test FalkorDB vector adapter operations."""

    @pytest.fixture
    def mock_client(self):
        """Create mock FalkorDB client."""
        client = Mock()
        graph = Mock()
        client.select_graph.return_value = graph
        return client, graph

    @pytest.fixture
    def adapter(self, mock_client):
        """Create adapter with mock client."""
        client, graph = mock_client
        config = FalkorDBVectorConfig()
        adapter = FalkorDBVectorAdapter(config)
        adapter._client = client
        adapter._graph = graph
        return adapter

    def test_store_vector_validates_dimension(self, adapter):
        """Storing wrong dimension vector should raise error."""
        from watercooler_memory.infrastructure.embedding_validator import DimensionMismatchError

        wrong_dim_vector = [0.1] * 512  # Wrong dimension
        with pytest.raises(DimensionMismatchError):
            adapter.store_vector(
                node_label="Chunk",
                node_id="chunk:abc",
                embedding=wrong_dim_vector,
            )

    def test_store_vector_success(self, adapter, mock_client):
        """Test successful vector storage."""
        _, graph = mock_client
        graph.query.return_value = Mock(result_set=[[]])

        correct_vector = [0.1] * 1024
        adapter.store_vector(
            node_label="Chunk",
            node_id="chunk:abc",
            embedding=correct_vector,
        )

        graph.query.assert_called_once()
        call_args = graph.query.call_args
        assert "vecf32" in call_args[0][0]

    def test_search_vectors_validates_dimension(self, adapter):
        """Search with wrong dimension query vector should raise error."""
        from watercooler_memory.infrastructure.embedding_validator import DimensionMismatchError

        wrong_dim_vector = [0.1] * 512
        with pytest.raises(DimensionMismatchError):
            adapter.search_vectors(
                node_label="Chunk",
                query_vector=wrong_dim_vector,
                limit=10,
            )

    def test_search_vectors_returns_results(self, adapter, mock_client):
        """Test vector search returns properly formatted results."""
        _, graph = mock_client

        # Mock FalkorDB response
        mock_result = Mock()
        mock_result.result_set = [
            ["chunk:abc", 0.95, "Hello world"],
            ["chunk:def", 0.87, "Goodbye world"],
        ]
        graph.query.return_value = mock_result

        query_vector = [0.1] * 1024
        results = adapter.search_vectors(
            node_label="Chunk",
            query_vector=query_vector,
            limit=10,
            return_fields=["node_id", "score", "text"],
        )

        assert len(results) == 2
        assert results[0].node_id == "chunk:abc"
        assert results[0].score == 0.95

    def test_batch_store_vectors(self, adapter, mock_client):
        """Test batch vector storage."""
        _, graph = mock_client
        graph.query.return_value = Mock(result_set=[[]])

        vectors = [
            ("chunk:a", [0.1] * 1024, {"text": "A"}),
            ("chunk:b", [0.2] * 1024, {"text": "B"}),
            ("chunk:c", [0.3] * 1024, {"text": "C"}),
        ]

        adapter.batch_store_vectors(
            node_label="Chunk",
            vectors=vectors,
        )

        # Should have called query 3 times (or once with batch)
        assert graph.query.call_count >= 1

    def test_delete_vector(self, adapter, mock_client):
        """Test vector deletion."""
        _, graph = mock_client
        graph.query.return_value = Mock(result_set=[[1]])

        result = adapter.delete_vector(
            node_label="Chunk",
            node_id="chunk:abc",
        )

        assert result is True
        graph.query.assert_called_once()


class TestAdapterConnection:
    """Test adapter connection handling."""

    @pytest.fixture
    def mock_client(self):
        """Create mock FalkorDB client."""
        client = Mock()
        graph = Mock()
        client.select_graph.return_value = graph
        return client, graph

    @pytest.fixture
    def adapter(self, mock_client):
        """Create adapter with mock client."""
        client, graph = mock_client
        config = FalkorDBVectorConfig()
        adapter = FalkorDBVectorAdapter(config)
        adapter._client = client
        adapter._graph = graph
        return adapter

    def test_connect_creates_client(self):
        """Test that connect creates FalkorDB client."""
        config = FalkorDBVectorConfig(host="localhost", port=6379)
        adapter = FalkorDBVectorAdapter(config)

        with patch("watercooler_memory.infrastructure.falkordb_vectors.FalkorDB") as mock_falkor:
            mock_client = Mock()
            mock_falkor.return_value = mock_client

            adapter.connect()

            mock_falkor.assert_called_once_with(host="localhost", port=6379)
            assert adapter._client is mock_client

    def test_healthcheck_success(self, adapter, mock_client):
        """Test healthcheck returns True when connected."""
        _, graph = mock_client
        graph.query.return_value = Mock(result_set=[[1]])

        result = adapter.healthcheck()
        assert result is True

    def test_healthcheck_failure(self, adapter, mock_client):
        """Test healthcheck returns False on error."""
        _, graph = mock_client
        graph.query.side_effect = Exception("Connection failed")

        result = adapter.healthcheck()
        assert result is False
