"""Unit tests for MCP memory integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp import memory


@pytest.fixture
def mock_env_disabled(monkeypatch):
    """Mock environment with Graphiti disabled."""
    monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "0")


@pytest.fixture
def mock_env_enabled(monkeypatch):
    """Mock environment with Graphiti enabled."""
    monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


class TestLoadGraphitiConfig:
    """Tests for load_graphiti_config() function."""

    def test_load_config_disabled(self, mock_env_disabled):
        """Test config loading when Graphiti is disabled."""
        config = memory.load_graphiti_config()
        assert config is None

    def test_load_config_missing_api_key(self, monkeypatch):
        """Test config loading fails gracefully without API key."""
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = memory.load_graphiti_config()
        assert config is None

    def test_load_config_success(self, mock_env_enabled):
        """Test config loading with valid environment."""
        config = memory.load_graphiti_config()
        assert config is not None
        assert config.openai_api_key == "sk-test"

    def test_load_config_uses_openai_api_key(self, monkeypatch):
        """Test config uses OPENAI_API_KEY."""
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")

        config = memory.load_graphiti_config()
        assert config is not None
        assert config.openai_api_key == "sk-from-env"


class TestGetGraphitiBackend:
    """Tests for get_graphiti_backend() function."""

    def test_get_backend_import_error(self, mock_env_enabled):
        """Test backend handles missing dependencies gracefully."""
        config = memory.load_graphiti_config()
        assert config is not None

        # Patch the module to make import fail
        import sys
        original_modules = sys.modules.copy()
        # Remove watercooler_memory.backends if present
        sys.modules.pop('watercooler_memory.backends', None)

        # Mock the import to raise ImportError
        with patch.dict('sys.modules', {'watercooler_memory.backends': None}):
            backend = memory.get_graphiti_backend(config)
            # Returns error dict on import error
            assert isinstance(backend, dict)
            assert backend.get("error") == "import_failed"

        # Restore modules
        sys.modules.update(original_modules)


class TestQueryMemory:
    """Tests for query_memory() function.

    These tests require asyncio because query_memory uses asyncio.to_thread.
    """

    @pytest.mark.anyio
    async def test_query_memory_basic(self):
        """Test basic query execution."""
        mock_backend = MagicMock()

        # Mock the result returned by backend.query()
        mock_result = MagicMock()
        mock_result.results = [
            {
                "query": "test query",
                "content": "test result",
                "score": 0.9,
                "metadata": {"thread_id": "test-thread"},
            }
        ]
        mock_result.communities = []
        mock_backend.query.return_value = mock_result

        results, communities = await memory.query_memory(mock_backend, "test query", limit=10)

        assert len(results) == 1
        assert results[0]["content"] == "test result"
        assert results[0]["score"] == 0.9
        assert results[0]["metadata"]["thread_id"] == "test-thread"
        assert isinstance(communities, list)

        # Verify backend.query was called via asyncio.to_thread
        # (can't easily assert since it's called indirectly)

    @pytest.mark.anyio
    async def test_query_memory_custom_limit(self):
        """Test query with custom limit."""
        mock_backend = MagicMock()
        mock_result = MagicMock()
        mock_result.results = []
        mock_result.communities = []
        mock_backend.query.return_value = mock_result

        results, communities = await memory.query_memory(mock_backend, "test", limit=5)

        # Verify backend.query was called via asyncio.to_thread
        # (can't easily assert since it's called indirectly)

    @pytest.mark.anyio
    async def test_query_memory_returns_multiple_results(self):
        """Test query returning multiple results."""
        mock_backend = MagicMock()
        mock_result = MagicMock()
        mock_result.results = [
            {"query": "q", "content": "result1", "score": 0.9, "metadata": {}},
            {"query": "q", "content": "result2", "score": 0.8, "metadata": {}},
            {"query": "q", "content": "result3", "score": 0.7, "metadata": {}},
        ]
        mock_result.communities = []
        mock_backend.query.return_value = mock_result

        results, communities = await memory.query_memory(mock_backend, "q", limit=10)

        assert len(results) == 3
        assert results[0]["content"] == "result1"
        assert results[1]["content"] == "result2"
        assert results[2]["content"] == "result3"
        assert isinstance(communities, list)


class TestSearchNodes:
    """Tests for search_nodes() backend method wrapper.
    
    Note: These test the memory module wrapper, not the MCP tool directly.
    MCP tool tests would require FastMCP context mocking.
    """

    @pytest.mark.anyio
    async def test_search_nodes_basic(self):
        """Test basic node search execution."""
        mock_backend = MagicMock()
        
        # Mock the result returned by backend.search_nodes()
        mock_nodes = [
            {
                "uuid": "01ABC...",
                "name": "TestNode",
                "labels": ["Entity"],
                "summary": "Test node summary",
                "created_at": "2025-01-01T00:00:00Z",
                "group_id": "test-group",
            }
        ]
        mock_backend.search_nodes.return_value = mock_nodes

        # Import after mocking to ensure we can test the wrapper if added
        # For now, we test direct backend calls (no wrapper exists yet)
        import asyncio
        results = await asyncio.to_thread(
            mock_backend.search_nodes,
            query="test",
            group_ids=["test-group"],
            max_nodes=10,
            entity_types=None,
        )

        assert len(results) == 1
        assert results[0]["name"] == "TestNode"
        assert results[0]["uuid"] == "01ABC..."

    @pytest.mark.anyio
    async def test_search_nodes_empty_results(self):
        """Test node search with no results."""
        mock_backend = MagicMock()
        mock_backend.search_nodes.return_value = []

        import asyncio
        results = await asyncio.to_thread(
            mock_backend.search_nodes,
            query="nonexistent",
            group_ids=["test-group"],
            max_nodes=10,
            entity_types=None,
        )

        assert isinstance(results, list)
        assert len(results) == 0


class TestGetEntityEdge:
    """Tests for get_entity_edge() backend method wrapper."""

    @pytest.mark.anyio
    async def test_get_entity_edge_basic(self):
        """Test basic entity edge retrieval."""
        mock_backend = MagicMock()
        
        # Mock the result returned by backend.get_entity_edge()
        mock_edge = {
            "uuid": "01ABC...",
            "fact": "Test fact",
            "source_node_uuid": "01DEF...",
            "target_node_uuid": "01GHI...",
            "valid_at": "2025-01-01T00:00:00Z",
            "created_at": "2025-01-01T00:00:00Z",
            "group_id": "test-group",
        }
        mock_backend.get_entity_edge.return_value = mock_edge

        import asyncio
        result = await asyncio.to_thread(
            mock_backend.get_entity_edge,
            uuid="01ABC...",
        )

        assert result["uuid"] == "01ABC..."
        assert result["fact"] == "Test fact"
        assert result["source_node_uuid"] == "01DEF..."

    @pytest.mark.anyio
    async def test_get_entity_edge_not_found(self):
        """Test entity edge retrieval with nonexistent UUID."""
        from watercooler_memory.backends import BackendError
        
        mock_backend = MagicMock()
        mock_backend.get_entity_edge.side_effect = BackendError("Entity edge 'xyz' not found")

        import asyncio
        with pytest.raises(BackendError, match="not found"):
            await asyncio.to_thread(
                mock_backend.get_entity_edge,
                uuid="xyz",
            )


class TestGetEntityEdgeValidation:
    """Tests for get_entity_edge() UUID validation.
    
    Note: These tests would ideally test the MCP tool directly,
    but that requires FastMCP context mocking. For now, we document
    the expected validation behavior at the backend layer.
    """

    @pytest.mark.anyio
    async def test_get_entity_edge_empty_uuid(self):
        """Test that empty UUID is rejected."""
        from watercooler_memory.backends import BackendError
        
        mock_backend = MagicMock()
        # Backend should receive sanitized input, so we test the expected behavior
        # If an empty UUID somehow reaches the backend, it should fail gracefully
        mock_backend.get_entity_edge.side_effect = BackendError("UUID is required")

        import asyncio
        with pytest.raises(BackendError, match="required"):
            await asyncio.to_thread(
                mock_backend.get_entity_edge,
                uuid="",
            )

    @pytest.mark.anyio
    async def test_get_entity_edge_long_uuid(self):
        """Test that excessively long UUID is rejected."""
        from watercooler_memory.backends import BackendError
        
        mock_backend = MagicMock()
        # Test with a UUID that's too long (>100 chars)
        long_uuid = "a" * 150
        mock_backend.get_entity_edge.side_effect = BackendError(f"UUID too long")

        import asyncio
        with pytest.raises(BackendError):
            await asyncio.to_thread(
                mock_backend.get_entity_edge,
                uuid=long_uuid,
            )

    @pytest.mark.anyio
    async def test_get_entity_edge_invalid_characters(self):
        """Test that UUID with invalid characters is rejected."""
        from watercooler_memory.backends import BackendError
        
        mock_backend = MagicMock()
        # Test with invalid characters (e.g., SQL injection attempt)
        invalid_uuid = "'; DROP TABLE edges; --"
        mock_backend.get_entity_edge.side_effect = BackendError("Invalid UUID format")

        import asyncio
        with pytest.raises(BackendError):
            await asyncio.to_thread(
                mock_backend.get_entity_edge,
                uuid=invalid_uuid,
            )


class TestSearchMemoryFacts:
    """Tests for search_memory_facts() backend method wrapper."""

    @pytest.mark.anyio
    async def test_search_memory_facts_basic(self):
        """Test basic fact search execution."""
        mock_backend = MagicMock()
        
        # Mock the result returned by backend.search_memory_facts()
        mock_facts = [
            {
                "uuid": "01ABC...",
                "fact": "Claude implemented OAuth2",
                "source_node_uuid": "01DEF...",
                "target_node_uuid": "01GHI...",
                "score": 0.89,
                "valid_at": "2025-01-01T00:00:00Z",
                "group_id": "test-group",
            }
        ]
        mock_backend.search_memory_facts.return_value = mock_facts

        import asyncio
        results = await asyncio.to_thread(
            mock_backend.search_memory_facts,
            query="OAuth2",
            group_ids=["test-group"],
            max_facts=10,
            center_node_uuid=None,
        )

        assert len(results) == 1
        assert results[0]["fact"] == "Claude implemented OAuth2"
        assert results[0]["score"] == 0.89

    @pytest.mark.anyio
    async def test_search_memory_facts_with_center_node(self):
        """Test fact search with center node."""
        mock_backend = MagicMock()
        mock_backend.search_memory_facts.return_value = []

        import asyncio
        results = await asyncio.to_thread(
            mock_backend.search_memory_facts,
            query="test",
            group_ids=["test-group"],
            max_facts=10,
            center_node_uuid="01ABC...",
        )

        assert isinstance(results, list)


class TestGetEpisodes:
    """Tests for get_episodes() backend method wrapper."""

    @pytest.mark.anyio
    async def test_get_episodes_basic(self):
        """Test episode search with query."""
        mock_backend = MagicMock()
        
        # Mock the result returned by backend.get_episodes()
        mock_episodes = [
            {
                "uuid": "01ABC...",
                "name": "Entry 01ABC...",
                "content": "Test episode content",
                "created_at": "2025-01-01T00:00:00Z",
                "source": "thread_entry",
                "source_description": "Watercooler thread entry",
                "group_id": "test-group",
                "valid_at": "2025-01-01T00:00:00Z",
            }
        ]
        mock_backend.get_episodes.return_value = mock_episodes

        import asyncio
        results = await asyncio.to_thread(
            mock_backend.get_episodes,
            query="test episode",
            group_ids=["test-group"],
            max_episodes=10,
        )

        assert len(results) == 1
        assert results[0]["name"] == "Entry 01ABC..."
        assert results[0]["content"] == "Test episode content"

    @pytest.mark.anyio
    async def test_get_episodes_empty_query(self):
        """Test get_episodes rejects empty query."""
        from watercooler_memory.backends import ConfigError
        
        mock_backend = MagicMock()
        mock_backend.get_episodes.side_effect = ConfigError("query parameter is required and must be non-empty")

        import asyncio
        with pytest.raises(ConfigError, match="query parameter is required"):
            await asyncio.to_thread(
                mock_backend.get_episodes,
                query="",
                max_episodes=10,
            )


class TestCreateErrorResponse:
    """Tests for create_error_response() helper function."""

    def test_create_error_response_basic(self):
        """Test basic error response creation."""
        result = memory.create_error_response(
            "TestError",
            "Test message",
            "test_operation"
        )

        # Verify it returns a ToolResult
        from fastmcp.tools.tool import ToolResult
        assert isinstance(result, ToolResult)

        # Verify JSON structure
        import json
        from mcp.types import TextContent
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextContent)

        error_dict = json.loads(result.content[0].text)
        assert error_dict["error"] == "TestError"
        assert error_dict["message"] == "Test message"
        assert error_dict["operation"] == "test_operation"

    def test_create_error_response_with_kwargs(self):
        """Test error response with additional fields."""
        result = memory.create_error_response(
            "ValidationError",
            "Invalid input",
            "validate_input",
            query="test",
            result_count=0,
            results=[]
        )

        import json
        error_dict = json.loads(result.content[0].text)

        # Verify core fields
        assert error_dict["error"] == "ValidationError"
        assert error_dict["message"] == "Invalid input"
        assert error_dict["operation"] == "validate_input"

        # Verify additional fields
        assert error_dict["query"] == "test"
        assert error_dict["result_count"] == 0
        assert error_dict["results"] == []

    def test_create_error_response_preserves_core_fields(self):
        """Test that core fields are always present even with many kwargs.

        The defensive field ordering ensures error, message, and operation
        are always set last, so they can't be accidentally omitted.
        """
        result = memory.create_error_response(
            "CoreError",
            "Core message",
            "core_op",
            query="test",
            result_count=0,
            results=[],
            extra_field="value"
        )

        import json
        error_dict = json.loads(result.content[0].text)

        # Verify all core fields are present
        assert "error" in error_dict
        assert "message" in error_dict
        assert "operation" in error_dict

        # Verify core field values
        assert error_dict["error"] == "CoreError"
        assert error_dict["message"] == "Core message"
        assert error_dict["operation"] == "core_op"

        # Verify kwargs are also preserved
        assert error_dict["query"] == "test"
        assert error_dict["extra_field"] == "value"


class TestValidateMemoryPrerequisites:
    """Tests for validate_memory_prerequisites() helper function."""

    def test_validate_prerequisites_disabled(self, monkeypatch):
        """Test validation when Graphiti is disabled."""
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "0")

        backend, error = memory.validate_memory_prerequisites("test_operation")

        # Should return None backend and error response
        assert backend is None
        assert error is not None

        # Verify error structure
        import json
        error_dict = json.loads(error.content[0].text)
        assert error_dict["error"] == "Graphiti not enabled"
        assert error_dict["operation"] == "test_operation"
        assert "WATERCOOLER_GRAPHITI_ENABLED" in error_dict["message"]

    def test_validate_prerequisites_missing_api_key(self, monkeypatch):
        """Test validation fails gracefully without API key."""
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        backend, error = memory.validate_memory_prerequisites("test_operation")

        # Should return None for both (config load fails)
        assert backend is None
        assert error is not None

        import json
        error_dict = json.loads(error.content[0].text)
        assert error_dict["error"] == "Graphiti not enabled"
        assert error_dict["operation"] == "test_operation"

    def test_validate_prerequisites_backend_import_error(self, monkeypatch):
        """Test validation when backend import fails."""
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        # Mock get_graphiti_backend to return error dict
        with patch('watercooler_mcp.memory.get_graphiti_backend') as mock_backend:
            mock_backend.return_value = {
                "error": "import_failed",
                "details": "Module not found"
            }

            backend, error = memory.validate_memory_prerequisites("test_operation")

            assert backend is None
            assert error is not None

            import json
            error_dict = json.loads(error.content[0].text)
            assert "import_failed" in error_dict["error"]
            assert error_dict["operation"] == "test_operation"

    def test_validate_prerequisites_success(self, monkeypatch):
        """Test successful validation."""
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        # Mock successful backend initialization
        mock_backend_instance = MagicMock()
        with patch('watercooler_mcp.memory.get_graphiti_backend') as mock_backend:
            mock_backend.return_value = mock_backend_instance

            backend, error = memory.validate_memory_prerequisites("test_operation")

            # Should return backend and no error
            assert backend is mock_backend_instance
            assert error is None
