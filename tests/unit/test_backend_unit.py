"""Unit tests for memory backend internal logic."""

import pytest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, Mock, patch

from watercooler_memory.backends import BackendError, TransientError
from watercooler_memory.backends.graphiti import (
    GraphitiBackend, GraphitiConfig,
    _normalize_json_response, _get_list_item_model, _best_extra_match,
    MAX_NORMALIZE_DEPTH,
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


class TestGraphitiClientConstruction:
    """Unit tests for Graphiti client construction details."""

    @staticmethod
    def _fake_graphiti_modules(
        captured: dict[str, object], include_openai_reranker: bool = False
    ) -> dict[str, ModuleType]:
        """Create a fake graphiti_core module tree for import-time patching."""
        graphiti_core = ModuleType("graphiti_core")
        driver_pkg = ModuleType("graphiti_core.driver")
        driver_mod = ModuleType("graphiti_core.driver.falkordb_driver")
        llm_pkg = ModuleType("graphiti_core.llm_client")
        llm_openai_mod = ModuleType("graphiti_core.llm_client.openai_generic_client")
        llm_config_mod = ModuleType("graphiti_core.llm_client.config")
        embedder_mod = ModuleType("graphiti_core.embedder")
        embedder_openai_mod = ModuleType("graphiti_core.embedder.openai")
        cross_encoder_pkg = ModuleType("graphiti_core.cross_encoder")
        cross_encoder_client_mod = ModuleType("graphiti_core.cross_encoder.client")

        class FakeGraphiti:
            def __init__(self, **kwargs):
                captured["graphiti_kwargs"] = kwargs
                self.driver = kwargs.get("graph_driver")

        class FakeFalkorDriver:
            def __init__(self, **kwargs):
                captured["driver_kwargs"] = kwargs

            def close(self) -> None:
                return None

        class FakeLLMConfig:
            def __init__(self, api_key=None, model=None, base_url=None):
                self.api_key = api_key
                self.model = model
                self.base_url = base_url

        class FakeOpenAIGenericClient:
            def __init__(self, config=None, **kwargs):
                self.config = config
                self.client = object()

        class FakeOpenAIEmbedderConfig:
            def __init__(
                self, embedding_model=None, api_key=None, base_url=None, embedding_dim=None
            ):
                self.embedding_model = embedding_model
                self.api_key = api_key
                self.base_url = base_url
                self.embedding_dim = embedding_dim

        class FakeOpenAIEmbedder:
            def __init__(self, config=None):
                self.config = config

        class FakeCrossEncoderClient:
            async def rank(self, query: str, passages: list[str]):
                del query
                return [(p, 0.0) for p in passages]

        graphiti_core.Graphiti = FakeGraphiti
        driver_mod.FalkorDriver = FakeFalkorDriver
        llm_openai_mod.OpenAIGenericClient = FakeOpenAIGenericClient
        llm_config_mod.LLMConfig = FakeLLMConfig
        embedder_mod.OpenAIEmbedder = FakeOpenAIEmbedder
        embedder_openai_mod.OpenAIEmbedderConfig = FakeOpenAIEmbedderConfig
        cross_encoder_pkg.CrossEncoderClient = FakeCrossEncoderClient
        cross_encoder_client_mod.CrossEncoderClient = FakeCrossEncoderClient

        modules = {
            "graphiti_core": graphiti_core,
            "graphiti_core.driver": driver_pkg,
            "graphiti_core.driver.falkordb_driver": driver_mod,
            "graphiti_core.llm_client": llm_pkg,
            "graphiti_core.llm_client.openai_generic_client": llm_openai_mod,
            "graphiti_core.llm_client.config": llm_config_mod,
            "graphiti_core.embedder": embedder_mod,
            "graphiti_core.embedder.openai": embedder_openai_mod,
            "graphiti_core.cross_encoder": cross_encoder_pkg,
            "graphiti_core.cross_encoder.client": cross_encoder_client_mod,
        }

        if include_openai_reranker:
            cross_encoder_openai_mod = ModuleType(
                "graphiti_core.cross_encoder.openai_reranker_client"
            )

            class FakeOpenAIRerankerClient(FakeCrossEncoderClient):
                def __init__(self, config=None, client=None):
                    self.config = config
                    self.client = client

            cross_encoder_openai_mod.OpenAIRerankerClient = FakeOpenAIRerankerClient
            modules[
                "graphiti_core.cross_encoder.openai_reranker_client"
            ] = cross_encoder_openai_mod

        return modules

    @pytest.fixture(autouse=True)
    def clear_noop_cache(self):
        """Reset the lru_cache on _build_noop_cross_encoder between tests.

        The cache holds a CrossEncoderClient instance whose base class is
        whichever fake was active at first call. Clearing between tests keeps
        each test fully isolated regardless of run order.
        """
        from watercooler_memory.backends.graphiti import _build_noop_cross_encoder

        _build_noop_cross_encoder.cache_clear()
        yield
        _build_noop_cross_encoder.cache_clear()

    @pytest.mark.parametrize("reranker", ["rrf", "mmr"])
    def test_create_graphiti_client_sets_noop_cross_encoder_for_non_cross_encoder_modes(
        self, reranker: str
    ):
        """rrf and mmr reranker modes both use the noop cross encoder (same code path).

        Neither should trigger Graphiti's implicit OpenAI reranker initialization.
        """
        config = GraphitiConfig(
            llm_api_key="test-llm-key",
            embedding_api_key="test-embed-key",
            llm_api_base="http://localhost:11434/v1",
            embedding_api_base="http://localhost:8080/v1",
            reranker=reranker,
        )
        with patch.object(GraphitiBackend, "_validate_config"):
            backend = GraphitiBackend(config)

        captured: dict[str, object] = {}
        modules = self._fake_graphiti_modules(captured)
        with patch.object(backend, "_ensure_embedding_service_available"), patch.dict(
            "sys.modules", modules, clear=False
        ):
            backend._create_graphiti_client()

        kwargs = captured["graphiti_kwargs"]
        assert kwargs["cross_encoder"].__class__.__name__ == "_NoopCrossEncoderClient"

    def test_create_graphiti_client_cross_encoder_uses_llm_config(self):
        """cross_encoder mode should use configured LLM settings, not env defaults."""
        config = GraphitiConfig(
            llm_api_key="llm-key-123",
            embedding_api_key="embed-key-123",
            llm_api_base="https://example-openai-compatible/v1",
            llm_model="my-llm-model",
            reranker="cross_encoder",
        )
        with patch.object(GraphitiBackend, "_validate_config"):
            backend = GraphitiBackend(config)

        captured: dict[str, object] = {}
        modules = self._fake_graphiti_modules(captured, include_openai_reranker=True)
        with patch.object(backend, "_ensure_embedding_service_available"), patch.dict(
            "sys.modules", modules, clear=False
        ):
            backend._create_graphiti_client()

        kwargs = captured["graphiti_kwargs"]
        cross_encoder = kwargs["cross_encoder"]
        assert cross_encoder.__class__.__name__ == "FakeOpenAIRerankerClient"
        assert cross_encoder.config.api_key == "llm-key-123"
        assert cross_encoder.config.model == "my-llm-model"
        assert cross_encoder.config.base_url == "https://example-openai-compatible/v1"

    def test_create_graphiti_client_cross_encoder_with_anthropic_raises(self):
        """cross_encoder reranker with an Anthropic URL must raise ConfigError."""
        config = GraphitiConfig(
            llm_api_key="ant-key",
            embedding_api_key="embed-key",
            llm_api_base="https://api.anthropic.com/v1",
            reranker="cross_encoder",
        )
        with patch.object(GraphitiBackend, "_validate_config"):
            backend = GraphitiBackend(config)

        captured: dict[str, object] = {}
        modules = self._fake_graphiti_modules(captured)
        with patch.object(backend, "_ensure_embedding_service_available"), patch.dict(
            "sys.modules", modules, clear=False
        ):
            # Anthropic provides its own client path; we need to also fake that module.
            anthropic_mod = ModuleType("graphiti_core.llm_client.anthropic_client")

            class FakeAnthropicClient:
                def __init__(self, config=None, **kwargs):
                    pass

            anthropic_mod.AnthropicClient = FakeAnthropicClient
            with patch.dict(
                "sys.modules",
                {"graphiti_core.llm_client.anthropic_client": anthropic_mod},
                clear=False,
            ):
                from watercooler_memory.backends import ConfigError

                with pytest.raises(ConfigError, match="cross_encoder.*Anthropic"):
                    backend._create_graphiti_client()


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

    def test_single_dict_coerced_to_list(self):
        """Wrap a single dict in a list when list[Model] is expected."""
        from pydantic import BaseModel

        class NodeDuplicate(BaseModel):
            id: int
            name: str
            duplicates: list[int]

        class NodeResolutions(BaseModel):
            entity_resolutions: list[NodeDuplicate]

        # DeepSeek returns a single dict instead of a list
        data = {
            "entity_resolutions": {
                "id": 0,
                "name": "test entity",
                "duplicates": [1, 2],
            },
        }
        result = _normalize_json_response(data, NodeResolutions)
        assert isinstance(result["entity_resolutions"], list)
        assert len(result["entity_resolutions"]) == 1
        assert result["entity_resolutions"][0]["name"] == "test entity"
        # Validate it actually parses
        parsed = NodeResolutions(**result)
        assert parsed.entity_resolutions[0].id == 0

    def test_single_dict_coerced_with_nested_remap(self):
        """Wrap single dict in list AND remap nested field names."""
        from pydantic import BaseModel

        class Inner(BaseModel):
            name: str

        class Outer(BaseModel):
            items: list[Inner]

        # Single dict with variant field name
        data = {"items": {"entity_name": "foo"}}
        result = _normalize_json_response(data, Outer)
        assert isinstance(result["items"], list)
        assert len(result["items"]) == 1
        assert result["items"][0]["name"] == "foo"


class TestBestExtraMatch:
    """Unit tests for _best_extra_match field similarity scoring."""

    def test_suffix_match_preferred(self):
        """entity_name should match 'name' over entity_id (suffix wins)."""
        extras = {"entity_id", "entity_name", "entity_type_name"}
        assert _best_extra_match("name", extras) == "entity_name"

    def test_exact_match(self):
        """Exact match should be picked."""
        extras = {"name", "other_field"}
        assert _best_extra_match("name", extras) == "name"

    def test_substring_match_fallback(self):
        """Substring containment is used when no suffix match."""
        extras = {"xnamex", "other"}
        assert _best_extra_match("name", extras) == "xnamex"

    def test_alphabetical_fallback(self):
        """Fall back to alphabetical when no similarity."""
        extras = {"aaa", "bbb", "ccc"}
        assert _best_extra_match("name", extras) == "aaa"

    def test_empty_extras(self):
        """Return None for empty extras set."""
        assert _best_extra_match("name", set()) is None

    def test_underscore_suffix(self):
        """entity_name ends with _name → matches 'name'."""
        extras = {"entity_name"}
        assert _best_extra_match("name", extras) == "entity_name"

    def test_entity_id_not_picked_for_name(self):
        """Regression: entity_id must NOT be picked for 'name' field."""
        extras = {"entity_id", "entity_name", "entity_type_description", "entity_type_name"}
        result = _best_extra_match("name", extras)
        assert result == "entity_name"
        assert result != "entity_id"


class TestNormalizeEntityIdRegression:
    """Regression tests for DeepSeek returning entity_id in entities."""

    def test_entity_with_entity_id_maps_correctly(self):
        """entity_name→name, entity_id left as extra (not mapped to name)."""
        from pydantic import BaseModel, Field

        class ExtractedEntity(BaseModel):
            name: str = Field(..., description="Entity name")
            entity_type_id: int = Field(description="Entity type ID")

        class ExtractedEntities(BaseModel):
            extracted_entities: list[ExtractedEntity]

        # Exact DeepSeek response format that caused the bug
        data = {
            "entity_nodes": [
                {
                    "entity_id": 0,
                    "entity_name": "DeepSeek",
                    "entity_type_id": 0,
                    "entity_type_name": "Entity",
                    "entity_type_description": "Default type",
                },
                {
                    "entity_id": 1,
                    "entity_name": "json_object",
                    "entity_type_id": 0,
                    "entity_type_name": "Entity",
                    "entity_type_description": "Default type",
                },
            ]
        }
        result = _normalize_json_response(data, ExtractedEntities)
        entities = result["extracted_entities"]

        # name must be the string entity_name, NOT the int entity_id
        assert entities[0]["name"] == "DeepSeek"
        assert entities[1]["name"] == "json_object"
        assert isinstance(entities[0]["name"], str)
        assert isinstance(entities[1]["name"], str)

        # Validate with Pydantic
        parsed = ExtractedEntities(**result)
        assert parsed.extracted_entities[0].name == "DeepSeek"
        assert parsed.extracted_entities[0].entity_type_id == 0

    def test_entity_without_entity_id_still_works(self):
        """Normal case without entity_id should still remap correctly."""
        from pydantic import BaseModel, Field

        class ExtractedEntity(BaseModel):
            name: str
            entity_type_id: int

        class ExtractedEntities(BaseModel):
            extracted_entities: list[ExtractedEntity]

        data = {
            "entities": [
                {"entity_name": "Foo", "entity_type_id": 0},
            ]
        }
        result = _normalize_json_response(data, ExtractedEntities)
        assert result["extracted_entities"][0]["name"] == "Foo"


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


class TestNormalizeDepthLimit:
    """Tests for MAX_NORMALIZE_DEPTH guard in _normalize_json_response."""

    def test_depth_limit_returns_data_as_is(self):
        """At max depth, data is returned without modification."""
        from pydantic import BaseModel

        class Inner(BaseModel):
            name: str

        data = {"entity_name": "foo"}
        # Call at exactly the depth limit — should bail out
        result = _normalize_json_response(data, Inner, _depth=MAX_NORMALIZE_DEPTH)
        assert result == data  # No remapping applied
        assert "entity_name" in result  # Extra key NOT remapped

    def test_below_depth_limit_still_remaps(self):
        """Just below the limit, normal remapping occurs."""
        from pydantic import BaseModel

        class Inner(BaseModel):
            name: str

        data = {"entity_name": "foo"}
        result = _normalize_json_response(data, Inner, _depth=MAX_NORMALIZE_DEPTH - 1)
        assert result == {"name": "foo"}  # Remapping applied

    def test_max_normalize_depth_is_reasonable(self):
        """Sanity: the constant is high enough for real Graphiti responses (2-3 levels)."""
        assert MAX_NORMALIZE_DEPTH >= 5


class TestDictToListCoercionValidation:
    """Tests for validation before wrapping a single dict in a list."""

    def test_coercion_to_empty_list_when_no_matching_keys(self):
        """Dict with no keys matching target model fields is coerced to empty list.

        When a scalar/dict value appears where list[Model] is expected and the
        keys don't match the inner model, the normalizer coerces to [] to
        avoid Pydantic validation failures from local LLM responses.
        """
        from pydantic import BaseModel

        class Inner(BaseModel):
            name: str
            value: int

        class Outer(BaseModel):
            items: list[Inner]

        # Dict has keys that don't match Inner's fields at all
        data = {"items": {"totally_unrelated": "garbage", "xyz": 99}}
        result = _normalize_json_response(data, Outer)
        # Coerced to empty list (safe default for unrecognizable data)
        assert isinstance(result["items"], list)
        assert result["items"] == []

    def test_coercion_applied_when_keys_overlap(self):
        """Dict with at least one matching key IS coerced to list."""
        from pydantic import BaseModel

        class Inner(BaseModel):
            name: str
            value: int

        class Outer(BaseModel):
            items: list[Inner]

        # Dict has "name" which matches Inner.name
        data = {"items": {"name": "foo", "value": 42}}
        result = _normalize_json_response(data, Outer)
        assert isinstance(result["items"], list)
        assert len(result["items"]) == 1
        assert result["items"][0]["name"] == "foo"
