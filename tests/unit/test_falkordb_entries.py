"""Tests for FalkorDB entry embedding storage.

Tests cover:
- FalkorDBEntryStore initialization and configuration
- EntrySearchResult dataclass
- Error handling patterns

Unit tests use mocks and avoid async complexity.
Integration tests with real FalkorDB are in tests/integration/.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from watercooler.baseline_graph.falkordb_entries import (
    FalkorDBEntryStore,
    EntrySearchResult,
    DEFAULT_EMBEDDING_DIM,
)


# =============================================================================
# Unit Tests (synchronous)
# =============================================================================


class TestFalkorDBEntryStoreInit:
    """Tests for FalkorDBEntryStore initialization."""

    def test_init_defaults(self):
        """Test default initialization values."""
        store = FalkorDBEntryStore(group_id="test_project")

        assert store.group_id == "test_project"
        assert store.host == "localhost"
        assert store.port == 6379
        assert store.username is None
        assert store.password is None
        assert store.database == "test_project"  # Defaults to group_id
        assert store.embedding_dim == DEFAULT_EMBEDDING_DIM
        assert store._client is None
        assert store._graph is None
        assert store._index_created is False

    def test_init_custom_values(self):
        """Test initialization with custom values."""
        store = FalkorDBEntryStore(
            group_id="my_project",
            host="falkor.example.com",
            port=6380,
            username="user",
            password="secret",
            database="custom_db",
            embedding_dim=512,
        )

        assert store.group_id == "my_project"
        assert store.host == "falkor.example.com"
        assert store.port == 6380
        assert store.username == "user"
        assert store.password == "secret"
        assert store.database == "custom_db"
        assert store.embedding_dim == 512

    def test_database_defaults_to_group_id(self):
        """Test that database defaults to group_id when not specified."""
        store = FalkorDBEntryStore(group_id="watercooler_cloud")
        assert store.database == "watercooler_cloud"

    def test_database_can_be_overridden(self):
        """Test that database can be explicitly set."""
        store = FalkorDBEntryStore(group_id="watercooler_cloud", database="custom_db")
        assert store.database == "custom_db"
        assert store.group_id == "watercooler_cloud"

    def test_from_config(self, monkeypatch, tmp_path):
        """Test from_config factory method."""
        # Set up isolated config
        from watercooler.config_facade import config

        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("""
[memory.database]
host = "config-host"
port = 6380

[memory.embedding]
dim = 768
        """)
        monkeypatch.setenv("HOME", str(tmp_path))
        # Clear env vars
        monkeypatch.delenv("FALKORDB_HOST", raising=False)
        monkeypatch.delenv("FALKORDB_PORT", raising=False)
        monkeypatch.delenv("EMBEDDING_DIM", raising=False)
        config.reset()

        try:
            store = FalkorDBEntryStore.from_config("my_group")
            assert store.group_id == "my_group"
            assert store.host == "config-host"
            assert store.port == 6380
            assert store.embedding_dim == 768
        finally:
            config.reset()

    def test_from_config_env_vars_override(self, monkeypatch, tmp_path):
        """Test that environment variables override config file."""
        from watercooler.config_facade import config

        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("""
[memory.database]
host = "config-host"
port = 6380
        """)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("FALKORDB_HOST", "env-host")
        monkeypatch.setenv("FALKORDB_PORT", "7379")
        config.reset()

        try:
            store = FalkorDBEntryStore.from_config("test")
            assert store.host == "env-host"
            assert store.port == 7379
        finally:
            config.reset()


class TestFalkorDBEntryStoreConnectionState:
    """Tests for connection state management."""

    def test_initial_state_disconnected(self):
        """Test that store starts disconnected."""
        store = FalkorDBEntryStore(group_id="test")
        assert store._client is None
        assert store._graph is None
        assert store._index_created is False

    def test_close_idempotent(self):
        """Test that close() is safe to call when not connected."""
        store = FalkorDBEntryStore(group_id="test")
        # Should not raise
        import asyncio
        asyncio.run(store.close())
        assert store._client is None


class TestFalkorDBEntryStoreValidation:
    """Tests for input validation."""

    def test_embedding_dimension_stored(self):
        """Test that embedding dimension is stored correctly."""
        store = FalkorDBEntryStore(group_id="test", embedding_dim=512)
        assert store.embedding_dim == 512

    def test_default_embedding_dim(self):
        """Test default embedding dimension."""
        assert DEFAULT_EMBEDDING_DIM == 1024


class TestEntrySearchResult:
    """Tests for EntrySearchResult dataclass."""

    def test_creation(self):
        """Test EntrySearchResult creation."""
        result = EntrySearchResult(
            entry_id="entry123",
            thread_topic="auth-thread",
            score=0.95,
        )

        assert result.entry_id == "entry123"
        assert result.thread_topic == "auth-thread"
        assert result.score == 0.95

    def test_equality(self):
        """Test EntrySearchResult equality."""
        result1 = EntrySearchResult("entry1", "thread1", 0.9)
        result2 = EntrySearchResult("entry1", "thread1", 0.9)
        result3 = EntrySearchResult("entry2", "thread1", 0.9)

        assert result1 == result2
        assert result1 != result3

    def test_as_dict_like(self):
        """Test EntrySearchResult can be unpacked."""
        result = EntrySearchResult("entry1", "thread1", 0.9)
        # Dataclass has field access
        assert result.entry_id == "entry1"
        assert result.thread_topic == "thread1"
        assert result.score == 0.9


class TestCypherQueryPatterns:
    """Tests to verify the Cypher query patterns are correct.

    These tests verify the SQL/Cypher strings generated match expected patterns
    without actually running against FalkorDB.
    """

    def test_store_embedding_query_pattern(self):
        """Verify the MERGE query pattern for storing embeddings."""
        # The expected query pattern
        expected_keywords = ["MERGE", "Entry", "entry_id", "SET", "vecf32", "embedding"]

        # Create a store and simulate the query
        store = FalkorDBEntryStore(group_id="test", embedding_dim=4)
        store._graph = MagicMock()
        store._graph.query = AsyncMock()

        # Intercept the query call
        import asyncio

        async def capture_query():
            await store.store_embedding("entry123", "thread", [0.1, 0.2, 0.3, 0.4])
            return store._graph.query.call_args[0][0]

        query = asyncio.run(capture_query())

        for keyword in expected_keywords:
            assert keyword in query, f"Query should contain '{keyword}'"

    def test_search_similar_query_pattern(self):
        """Verify the vector search query pattern."""
        expected_keywords = [
            "db.idx.vector.queryNodes",
            "Entry",
            "embedding",
            "vecf32",
            "group_id",
        ]

        store = FalkorDBEntryStore(group_id="test", embedding_dim=4)
        store._graph = MagicMock()

        # Mock return value
        mock_result = MagicMock()
        mock_result.result_set = []
        store._graph.query = AsyncMock(return_value=mock_result)

        import asyncio

        async def capture_query():
            await store.search_similar([0.1, 0.2, 0.3, 0.4], limit=5)
            return store._graph.query.call_args[0][0]

        query = asyncio.run(capture_query())

        for keyword in expected_keywords:
            assert keyword in query, f"Query should contain '{keyword}'"

    def test_delete_embedding_query_pattern(self):
        """Verify the DELETE query pattern."""
        expected_keywords = ["MATCH", "Entry", "entry_id", "group_id", "DELETE"]

        store = FalkorDBEntryStore(group_id="test", embedding_dim=4)
        store._graph = MagicMock()

        mock_result = MagicMock()
        mock_result.result_set = [[0]]
        store._graph.query = AsyncMock(return_value=mock_result)

        import asyncio

        async def capture_query():
            await store.delete_embedding("entry123")
            return store._graph.query.call_args[0][0]

        query = asyncio.run(capture_query())

        for keyword in expected_keywords:
            assert keyword in query, f"Query should contain '{keyword}'"


class TestSimilarityScoreConversion:
    """Tests for converting FalkorDB distance to similarity score."""

    def test_distance_to_similarity_conversion(self):
        """Test the distance to similarity formula.

        FalkorDB returns cosine distance (0 = identical, 2 = opposite)
        We convert: similarity = 1 - (distance / 2)
        """
        # Distance 0 -> Similarity 1.0 (identical)
        assert 1.0 - (0.0 / 2.0) == 1.0

        # Distance 1 -> Similarity 0.5 (orthogonal)
        assert 1.0 - (1.0 / 2.0) == 0.5

        # Distance 2 -> Similarity 0.0 (opposite)
        assert 1.0 - (2.0 / 2.0) == 0.0

        # Distance 0.2 -> Similarity 0.9
        assert 1.0 - (0.2 / 2.0) == pytest.approx(0.9)

    def test_similarity_in_search_results(self):
        """Test that search results use correct similarity scores."""
        store = FalkorDBEntryStore(group_id="test", embedding_dim=4)
        store._graph = MagicMock()

        # Mock query result with distance scores
        mock_result = MagicMock()
        mock_result.result_set = [
            ["entry1", "thread1", 0.0],   # distance 0 -> similarity 1.0
            ["entry2", "thread2", 0.2],   # distance 0.2 -> similarity 0.9
            ["entry3", "thread3", 1.0],   # distance 1.0 -> similarity 0.5
        ]
        store._graph.query = AsyncMock(return_value=mock_result)

        import asyncio

        async def run_search():
            return await store.search_similar([0.1, 0.2, 0.3, 0.4], limit=5)

        results = asyncio.run(run_search())

        assert len(results) == 3
        assert results[0].score == pytest.approx(1.0)
        assert results[1].score == pytest.approx(0.9)
        assert results[2].score == pytest.approx(0.5)


class TestErrorHandling:
    """Tests for error handling patterns."""

    def test_store_validates_embedding_dimension(self):
        """Test that store_embedding validates embedding dimension."""
        store = FalkorDBEntryStore(group_id="test", embedding_dim=4)
        store._graph = MagicMock()

        import asyncio

        async def try_wrong_dim():
            # Wrong dimension (2 instead of 4)
            await store.store_embedding("entry", "thread", [0.1, 0.2])

        with pytest.raises(ValueError, match="dimension mismatch"):
            asyncio.run(try_wrong_dim())

    def test_operations_require_connection(self):
        """Test that operations raise when not connected."""
        store = FalkorDBEntryStore(group_id="test", embedding_dim=4)
        # Not connected: _graph is None

        import asyncio

        async def try_store():
            await store.store_embedding("entry", "thread", [0.1, 0.2, 0.3, 0.4])

        with pytest.raises(RuntimeError, match="Not connected"):
            asyncio.run(try_store())

    def test_ensure_index_handles_already_exists(self):
        """Test that ensure_index handles 'already indexed' errors."""
        store = FalkorDBEntryStore(group_id="test", embedding_dim=4)
        store._graph = MagicMock()
        store._graph.query = AsyncMock(side_effect=Exception("already indexed"))

        import asyncio

        async def try_ensure_index():
            await store.ensure_index()

        # Should not raise
        asyncio.run(try_ensure_index())
        assert store._index_created is True


class TestThreadTopicFiltering:
    """Tests for thread topic filtering in queries."""

    def test_search_with_thread_filter_includes_param(self):
        """Test that thread_topic filter is included in query."""
        store = FalkorDBEntryStore(group_id="test", embedding_dim=4)
        store._graph = MagicMock()

        mock_result = MagicMock()
        mock_result.result_set = []
        store._graph.query = AsyncMock(return_value=mock_result)

        import asyncio

        async def run_search():
            await store.search_similar([0.1, 0.2, 0.3, 0.4], thread_topic="auth-thread")
            return store._graph.query.call_args

        call_args = asyncio.run(run_search())
        query = call_args[0][0]
        params = call_args[0][1]

        assert "thread_topic" in query
        assert params["thread_topic"] == "auth-thread"

    def test_search_without_thread_filter(self):
        """Test that search without thread_topic doesn't filter by thread."""
        store = FalkorDBEntryStore(group_id="test", embedding_dim=4)
        store._graph = MagicMock()

        mock_result = MagicMock()
        mock_result.result_set = []
        store._graph.query = AsyncMock(return_value=mock_result)

        import asyncio

        async def run_search():
            await store.search_similar([0.1, 0.2, 0.3, 0.4])
            return store._graph.query.call_args

        call_args = asyncio.run(run_search())
        params = call_args[0][1]

        # Should not have thread_topic param
        assert "thread_topic" not in params
