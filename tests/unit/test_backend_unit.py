"""Unit tests for memory backend internal logic."""

import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from watercooler_memory.backends import BackendError, TransientError
from watercooler_memory.backends.graphiti import (
    GraphitiBackend, GraphitiConfig,
    _normalize_json_response, _get_list_item_model,
)
from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig


class TestGraphitiSanitization:
    """Unit tests for Graphiti thread ID sanitization logic."""

    @pytest.fixture
    def backend(self) -> GraphitiBackend:
        """Create Graphiti backend for testing (no test_mode)."""
        config = GraphitiConfig(
            work_dir=Path("/tmp/test"),
            llm_api_key="test-llm-key",
            embedding_api_key="test-embed-key",
            test_mode=False,
        )
        # Mock validation to avoid requiring Graphiti submodule in CI
        with patch.object(GraphitiBackend, '_validate_config'):
            return GraphitiBackend(config)

    @pytest.fixture
    def backend_test_mode(self) -> GraphitiBackend:
        """Create Graphiti backend with test_mode enabled."""
        config = GraphitiConfig(
            work_dir=Path("/tmp/test"),
            llm_api_key="test-llm-key",
            embedding_api_key="test-embed-key",
            test_mode=True,
        )
        # Mock validation to avoid requiring Graphiti submodule in CI
        with patch.object(GraphitiBackend, '_validate_config'):
            return GraphitiBackend(config)

    def test_sanitize_basic_alphanumeric(self, backend: GraphitiBackend):
        """Test sanitization of simple alphanumeric thread IDs."""
        result = backend._sanitize_thread_id("simple-thread-name")
        assert result == "simple-thread-name"  # Hyphens preserved

    def test_sanitize_special_chars(self, backend: GraphitiBackend):
        """Test sanitization preserves printable special characters."""
        result = backend._sanitize_thread_id("thread@with#special$chars!")
        assert result == "thread@with#special$chars!"  # Printable chars preserved

    def test_sanitize_consecutive_special_chars(self, backend: GraphitiBackend):
        """Test sanitization preserves consecutive special chars."""
        result = backend._sanitize_thread_id("thread@@##name")
        assert result == "thread@@##name"  # Consecutive chars preserved

    def test_sanitize_empty_string(self, backend: GraphitiBackend):
        """Test sanitization handles empty strings."""
        result = backend._sanitize_thread_id("")
        assert result == "unknown"

    def test_sanitize_starts_with_number(self, backend: GraphitiBackend):
        """Test sanitization prepends 't_' when thread ID starts with number."""
        result = backend._sanitize_thread_id("123-thread")
        assert result == "t_123-thread"  # Hyphens preserved

    def test_sanitize_length_limit_production(self, backend: GraphitiBackend):
        """Test sanitization enforces 64-char limit in production mode."""
        long_name = "a" * 100
        result = backend._sanitize_thread_id(long_name)
        assert len(result) == 64

    def test_sanitize_length_limit_test_mode(self, backend_test_mode: GraphitiBackend):
        """Test sanitization reserves space for pytest__ prefix in test mode."""
        # In test mode, max length should be 64 - 8 = 56 before prefix is added
        long_name = "a" * 100
        result = backend_test_mode._sanitize_thread_id(long_name)
        # Result should be 56 chars + "pytest__" = 64 chars total
        assert len(result) == 64
        assert result.startswith("pytest__")
        assert len(result.replace("pytest__", "")) == 56

    def test_sanitize_test_mode_adds_prefix(self, backend_test_mode: GraphitiBackend):
        """Test that test_mode=True adds pytest__ prefix."""
        result = backend_test_mode._sanitize_thread_id("my-thread")
        assert result.startswith("pytest__")
        assert result == "pytest__my-thread"  # Hyphens preserved

    def test_sanitize_test_mode_no_double_prefix(self, backend_test_mode: GraphitiBackend):
        """Test that pytest__ prefix is not duplicated."""
        result = backend_test_mode._sanitize_thread_id("pytest__my-thread")
        assert result.startswith("pytest__")
        # Should not have double prefix
        assert result.count("pytest__") == 1

    def test_sanitize_production_no_test_prefix(self, backend: GraphitiBackend):
        """Test that production mode (test_mode=False) does NOT add pytest__ prefix."""
        result = backend._sanitize_thread_id("thread-name")

        # Production mode should NOT have pytest__ prefix
        assert not result.startswith("pytest__")
        assert result == "thread-name"  # Hyphens preserved, no prefix


class TestLeanRAGTestMode:
    """Unit tests for LeanRAG test_mode prefix application."""

    def test_apply_test_prefix_disabled(self):
        """Test that test_mode=False does not modify work_dir."""
        config = LeanRAGConfig(test_mode=False)
        # Mock validation to avoid requiring LeanRAG submodule in CI
        with patch.object(LeanRAGBackend, '_validate_config'):
            backend = LeanRAGBackend(config)

        original = Path("/tmp/leanrag-work")
        result = backend._apply_test_prefix(original)

        assert result == original
        assert result.name == "leanrag-work"

    def test_apply_test_prefix_enabled(self):
        """Test that test_mode=True adds pytest__ prefix to work_dir basename."""
        config = LeanRAGConfig(test_mode=True)
        # Mock validation to avoid requiring LeanRAG submodule in CI
        with patch.object(LeanRAGBackend, '_validate_config'):
            backend = LeanRAGBackend(config)

        original = Path("/tmp/leanrag-work")
        result = backend._apply_test_prefix(original)

        assert result != original
        assert result.parent == original.parent  # Parent unchanged
        assert result.name == "pytest__leanrag-work"

    def test_apply_test_prefix_no_duplicate(self):
        """Test that pytest__ prefix is not duplicated."""
        config = LeanRAGConfig(test_mode=True)
        # Mock validation to avoid requiring LeanRAG submodule in CI
        with patch.object(LeanRAGBackend, '_validate_config'):
            backend = LeanRAGBackend(config)

        original = Path("/tmp/pytest__leanrag-work")
        result = backend._apply_test_prefix(original)

        # Should not add second prefix
        assert result == original
        assert result.name == "pytest__leanrag-work"
        assert result.name.count("pytest__") == 1


class TestGraphitiAddEpisodeDirect:
    """Unit tests for GraphitiBackend.add_episode_direct method."""

    @pytest.fixture
    def backend(self) -> GraphitiBackend:
        """Create Graphiti backend for testing."""
        config = GraphitiConfig(
            work_dir=Path("/tmp/test"),
            llm_api_key="test-llm-key",
            embedding_api_key="test-embed-key",
            test_mode=True,
        )
        with patch.object(GraphitiBackend, '_validate_config'):
            return GraphitiBackend(config)

    @pytest.mark.anyio
    async def test_add_episode_direct_success(self, backend: GraphitiBackend):
        """Test successful episode addition returns expected result."""
        # Create mock result with expected attributes
        # AddEpisodeResults has episode.uuid, not direct uuid
        mock_episode = Mock()
        mock_episode.uuid = "ep-uuid-123"
        mock_result = Mock()
        mock_result.episode = mock_episode
        mock_result.nodes = [Mock(name="Entity1"), Mock(name="Entity2")]
        mock_result.edges = [Mock(), Mock(), Mock()]

        mock_graphiti = AsyncMock()
        mock_graphiti.add_episode = AsyncMock(return_value=mock_result)

        with patch.object(backend, '_create_graphiti_client', return_value=mock_graphiti):
            result = await backend.add_episode_direct(
                name="Test Episode",
                episode_body="Test content",
                source_description="Test source",
                reference_time=datetime.now(timezone.utc),
                group_id="test-thread",
            )

        assert result["episode_uuid"] == "ep-uuid-123"
        assert len(result["entities_extracted"]) == 2
        assert result["facts_extracted"] == 3

    @pytest.mark.anyio
    async def test_add_episode_direct_missing_uuid_raises_error(self, backend: GraphitiBackend):
        """Test that missing UUID in result raises BackendError."""
        # AddEpisodeResults has episode.uuid, not direct uuid
        mock_episode = Mock()
        mock_episode.uuid = None  # Missing UUID
        mock_result = Mock()
        mock_result.episode = mock_episode
        mock_result.nodes = []
        mock_result.edges = []

        mock_graphiti = AsyncMock()
        mock_graphiti.add_episode = AsyncMock(return_value=mock_result)

        with patch.object(backend, '_create_graphiti_client', return_value=mock_graphiti):
            with pytest.raises(BackendError) as exc_info:
                await backend.add_episode_direct(
                    name="Test Episode",
                    episode_body="Test content",
                    source_description="Test source",
                    reference_time=datetime.now(timezone.utc),
                    group_id="test-thread",
                )

        assert "no episode UUID" in str(exc_info.value)

    @pytest.mark.anyio
    async def test_add_episode_direct_connection_error_raises_transient(
        self, backend: GraphitiBackend
    ):
        """Test that connection errors raise TransientError."""
        with patch.object(
            backend,
            '_create_graphiti_client',
            side_effect=ConnectionError("Connection refused"),
        ):
            with pytest.raises(TransientError) as exc_info:
                await backend.add_episode_direct(
                    name="Test Episode",
                    episode_body="Test content",
                    source_description="Test source",
                    reference_time=datetime.now(timezone.utc),
                    group_id="test-thread",
                )

        assert "connection failed" in str(exc_info.value).lower()

    @pytest.mark.anyio
    async def test_add_episode_direct_operation_error_raises_backend_error(
        self, backend: GraphitiBackend
    ):
        """Test that operation errors raise BackendError."""
        mock_graphiti = AsyncMock()
        mock_graphiti.add_episode = AsyncMock(side_effect=RuntimeError("Graph operation failed"))

        with patch.object(backend, '_create_graphiti_client', return_value=mock_graphiti):
            with pytest.raises(BackendError) as exc_info:
                await backend.add_episode_direct(
                    name="Test Episode",
                    episode_body="Test content",
                    source_description="Test source",
                    reference_time=datetime.now(timezone.utc),
                    group_id="test-thread",
                )

        assert "Failed to add episode" in str(exc_info.value)


class TestGraphitiConfigValidation:
    """Unit tests for GraphitiConfig validation with new LLM/embedding fields."""

    def test_config_with_new_fields(self):
        """Test that new LLM/embedding config fields work correctly."""
        config = GraphitiConfig(
            llm_api_key="test-llm-key",
            llm_api_base="http://localhost:8000/v1",
            llm_model="gpt-4o",
            embedding_api_key="test-embed-key",
            embedding_api_base="http://localhost:8080/v1",
            embedding_model="bge-m3",
        )

        assert config.llm_api_key == "test-llm-key"
        assert config.llm_api_base == "http://localhost:8000/v1"
        assert config.llm_model == "gpt-4o"
        assert config.embedding_api_key == "test-embed-key"
        assert config.embedding_api_base == "http://localhost:8080/v1"
        assert config.embedding_model == "bge-m3"

    def test_config_defaults(self):
        """Test that default values are set correctly."""
        config = GraphitiConfig(
            llm_api_key="test-key",
            embedding_api_key="test-key",
        )

        assert config.llm_api_base is None  # OpenAI default
        assert config.llm_model == "gpt-4o-mini"
        assert config.embedding_api_base is None  # OpenAI default
        assert config.embedding_model == "text-embedding-3-small"

    def test_legacy_openai_fields_still_exist(self):
        """Test that legacy openai_api_key fields still exist for backwards compat."""
        config = GraphitiConfig(
            llm_api_key="new-key",
            embedding_api_key="embed-key",
            openai_api_key="legacy-key",  # Legacy field
            openai_api_base="http://legacy.api/v1",  # Legacy field
            openai_model="legacy-model",  # Legacy field
        )

        assert config.openai_api_key == "legacy-key"
        assert config.openai_api_base == "http://legacy.api/v1"
        assert config.openai_model == "legacy-model"


class TestGraphitiConfigMissingKeys:
    """Unit tests for config validation with missing required keys."""

    def test_missing_llm_api_key_raises_error(self):
        """Test that missing LLM_API_KEY raises ConfigError."""
        from watercooler_memory.backends import ConfigError
        from watercooler_memory.backends.graphiti import _ensure_graphiti_available

        config = GraphitiConfig(
            embedding_api_key="embed-key",
            # No llm_api_key
        )

        # Mock _ensure_graphiti_available to bypass neo4j import check
        with patch('watercooler_memory.backends.graphiti._ensure_graphiti_available'):
            with pytest.raises(ConfigError) as exc_info:
                GraphitiBackend(config)

        assert "LLM_API_KEY" in str(exc_info.value)

    def test_missing_embedding_api_key_raises_error(self):
        """Test that missing EMBEDDING_API_KEY raises ConfigError."""
        from watercooler_memory.backends import ConfigError

        config = GraphitiConfig(
            llm_api_key="llm-key",
            # No embedding_api_key
        )

        # Mock _ensure_graphiti_available to bypass neo4j import check
        with patch('watercooler_memory.backends.graphiti._ensure_graphiti_available'):
            with pytest.raises(ConfigError) as exc_info:
                GraphitiBackend(config)

        assert "EMBEDDING_API_KEY" in str(exc_info.value)

    def test_legacy_openai_key_fallback(self):
        """Test that legacy openai_api_key is used as fallback for llm_api_key."""
        config = GraphitiConfig(
            embedding_api_key="embed-key",
            openai_api_key="legacy-openai-key",  # Legacy fallback
            # No llm_api_key
        )

        # Mock _ensure_graphiti_available to bypass neo4j import check
        # and skip entry episode index init
        with patch('watercooler_memory.backends.graphiti._ensure_graphiti_available'):
            with patch.object(GraphitiBackend, '_init_entry_episode_index'):
                backend = GraphitiBackend(config)

        # Legacy key should be copied to llm_api_key
        assert backend.config.llm_api_key == "legacy-openai-key"


class TestNormalizeJsonResponse:
    """Unit tests for _normalize_json_response field remapping."""

    def test_top_level_remap(self):
        """Remap extra top-level fields to missing required fields."""
        from pydantic import BaseModel

        class MyModel(BaseModel):
            name: str
            value: int

        data = {"entity_name": "foo", "value": 42}
        result = _normalize_json_response(data, MyModel)
        assert result == {"name": "foo", "value": 42}

    def test_no_remap_when_all_present(self):
        """No remapping when all required fields are present."""
        from pydantic import BaseModel

        class MyModel(BaseModel):
            name: str
            value: int

        data = {"name": "foo", "value": 42}
        result = _normalize_json_response(data, MyModel)
        assert result == data

    def test_nested_list_remap(self):
        """Remap fields inside list items containing nested Pydantic models."""
        from pydantic import BaseModel

        class Entity(BaseModel):
            name: str
            entity_type_id: int

        class Entities(BaseModel):
            extracted_entities: list[Entity]

        data = {
            "extracted_entities": [
                {"entity_name": "DeepSeek", "entity_type_id": 5},
                {"entity_name": "branch", "entity_type_id": 3},
            ]
        }
        result = _normalize_json_response(data, Entities)
        assert result["extracted_entities"][0]["name"] == "DeepSeek"
        assert result["extracted_entities"][1]["name"] == "branch"
        # Original extra key should be gone
        assert "entity_name" not in result["extracted_entities"][0]

    def test_nested_list_no_remap_when_correct(self):
        """No remapping for nested items when field names match."""
        from pydantic import BaseModel

        class Entity(BaseModel):
            name: str

        class Entities(BaseModel):
            items: list[Entity]

        data = {"items": [{"name": "correct"}]}
        result = _normalize_json_response(data, Entities)
        assert result == data

    def test_non_pydantic_model_passthrough(self):
        """Non-Pydantic models are returned as-is."""
        data = {"foo": "bar"}
        result = _normalize_json_response(data, dict)
        assert result == data

    def test_combined_top_and_nested_remap(self):
        """Remap both top-level and nested fields in one pass."""
        from pydantic import BaseModel

        class Inner(BaseModel):
            name: str

        class Outer(BaseModel):
            entities: list[Inner]

        # Top-level: entity_nodes -> entities; nested: entity_name -> name
        data = {
            "entity_nodes": [
                {"entity_name": "foo"},
            ]
        }
        result = _normalize_json_response(data, Outer)
        assert "entities" in result
        assert result["entities"][0]["name"] == "foo"


class TestGetListItemModel:
    """Unit tests for _get_list_item_model helper."""

    def test_list_of_pydantic(self):
        from pydantic import BaseModel

        class Foo(BaseModel):
            x: int

        assert _get_list_item_model(list[Foo]) is Foo

    def test_list_of_str(self):
        assert _get_list_item_model(list[str]) is None

    def test_plain_type(self):
        assert _get_list_item_model(str) is None

    def test_none_annotation(self):
        assert _get_list_item_model(None) is None
