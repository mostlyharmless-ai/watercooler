"""Contract tests for MemoryBackend protocol.

These tests validate that backends correctly implement the MemoryBackend contract
by testing against the null backend reference implementation.
"""

from __future__ import annotations

import pytest

from watercooler_memory.backends import (
    BackendError,
    Capabilities,
    ChunkPayload,
    ConfigError,
    CorpusPayload,
    HealthStatus,
    IndexResult,
    MemoryBackend,
    PrepareResult,
    QueryPayload,
    QueryResult,
    TransientError,
)
from watercooler_memory.backends.null import NullBackend
from watercooler_memory.backends.registry import get_backend, list_backends


# Fixtures


@pytest.fixture
def corpus_payload() -> CorpusPayload:
    """Minimal corpus payload for testing."""
    return CorpusPayload(
        manifest_version="1.0.0",
        threads=[
            {
                "id": "thread-1",
                "topic": "test-thread",
                "status": "OPEN",
                "ball": "Claude",
                "entry_count": 2,
            }
        ],
        entries=[
            {
                "id": "entry-1",
                "thread_id": "thread-1",
                "agent": "Claude",
                "role": "planner",
                "type": "Note",
                "title": "Test Entry 1",
                "body": "This is test entry one.",
                "timestamp": "2025-01-01T12:00:00Z",
            },
            {
                "id": "entry-2",
                "thread_id": "thread-1",
                "agent": "Codex",
                "role": "implementer",
                "type": "Note",
                "title": "Test Entry 2",
                "body": "This is test entry two.",
                "timestamp": "2025-01-01T12:05:00Z",
            },
        ],
        edges=[
            {
                "source": "thread-1",
                "target": "entry-1",
                "type": "CONTAINS",
            },
            {
                "source": "thread-1",
                "target": "entry-2",
                "type": "CONTAINS",
            },
            {
                "source": "entry-1",
                "target": "entry-2",
                "type": "FOLLOWS",
            },
        ],
        metadata={"source": "test", "version": "1.0.0"},
    )


@pytest.fixture
def chunk_payload() -> ChunkPayload:
    """Minimal chunk payload for testing."""
    return ChunkPayload(
        manifest_version="1.0.0",
        chunks=[
            {
                "id": "chunk-1",
                "entry_id": "entry-1",
                "text": "This is test entry one.",
                "token_count": 5,
                "hash_code": "abc123",
            },
            {
                "id": "chunk-2",
                "entry_id": "entry-2",
                "text": "This is test entry two.",
                "token_count": 5,
                "hash_code": "def456",
            },
        ],
        threads=[{"id": "thread-1", "topic": "test-thread"}],
        entries=[
            {"id": "entry-1", "title": "Test Entry 1"},
            {"id": "entry-2", "title": "Test Entry 2"},
        ],
    )


@pytest.fixture
def query_payload() -> QueryPayload:
    """Minimal query payload for testing."""
    return QueryPayload(
        manifest_version="1.0.0",
        queries=[
            {
                "query": "What is the test about?",
                "limit": 5,
            }
        ],
    )


@pytest.fixture
def null_backend() -> NullBackend:
    """Null backend instance."""
    return NullBackend()


# Contract Tests


class TestBackendProtocol:
    """Test that backends implement the MemoryBackend protocol correctly."""

    def test_backend_has_required_methods(self, null_backend: NullBackend) -> None:
        """Backend must implement all 5 protocol methods."""
        assert hasattr(null_backend, "prepare")
        assert hasattr(null_backend, "index")
        assert hasattr(null_backend, "query")
        assert hasattr(null_backend, "healthcheck")
        assert hasattr(null_backend, "get_capabilities")

        # Verify they're callable
        assert callable(null_backend.prepare)
        assert callable(null_backend.index)
        assert callable(null_backend.query)
        assert callable(null_backend.healthcheck)
        assert callable(null_backend.get_capabilities)

    def test_backend_is_memory_backend(self, null_backend: NullBackend) -> None:
        """Backend must be recognized as MemoryBackend."""
        assert isinstance(null_backend, MemoryBackend)


class TestPrepareMethod:
    """Test prepare() method contract."""

    def test_prepare_accepts_corpus_payload(
        self, null_backend: NullBackend, corpus_payload: CorpusPayload
    ) -> None:
        """prepare() must accept CorpusPayload."""
        result = null_backend.prepare(corpus_payload)
        assert isinstance(result, PrepareResult)

    def test_prepare_returns_prepare_result(
        self, null_backend: NullBackend, corpus_payload: CorpusPayload
    ) -> None:
        """prepare() must return PrepareResult."""
        result = null_backend.prepare(corpus_payload)

        assert hasattr(result, "manifest_version")
        assert hasattr(result, "prepared_count")
        assert hasattr(result, "message")

        assert result.manifest_version == corpus_payload.manifest_version
        assert isinstance(result.prepared_count, int)
        assert result.prepared_count >= 0

    def test_prepare_counts_entries(
        self, null_backend: NullBackend, corpus_payload: CorpusPayload
    ) -> None:
        """prepare() should count prepared entries."""
        result = null_backend.prepare(corpus_payload)
        assert result.prepared_count == len(corpus_payload.entries)

    def test_prepare_preserves_manifest_version(
        self, null_backend: NullBackend, corpus_payload: CorpusPayload
    ) -> None:
        """prepare() must preserve manifest version."""
        result = null_backend.prepare(corpus_payload)
        assert result.manifest_version == corpus_payload.manifest_version


class TestIndexMethod:
    """Test index() method contract."""

    def test_index_accepts_chunk_payload(
        self, null_backend: NullBackend, chunk_payload: ChunkPayload
    ) -> None:
        """index() must accept ChunkPayload."""
        result = null_backend.index(chunk_payload)
        assert isinstance(result, IndexResult)

    def test_index_returns_index_result(
        self, null_backend: NullBackend, chunk_payload: ChunkPayload
    ) -> None:
        """index() must return IndexResult."""
        result = null_backend.index(chunk_payload)

        assert hasattr(result, "manifest_version")
        assert hasattr(result, "indexed_count")
        assert hasattr(result, "message")

        assert result.manifest_version == chunk_payload.manifest_version
        assert isinstance(result.indexed_count, int)
        assert result.indexed_count >= 0

    def test_index_counts_chunks(
        self, null_backend: NullBackend, chunk_payload: ChunkPayload
    ) -> None:
        """index() should count indexed chunks."""
        result = null_backend.index(chunk_payload)
        assert result.indexed_count == len(chunk_payload.chunks)

    def test_index_preserves_manifest_version(
        self, null_backend: NullBackend, chunk_payload: ChunkPayload
    ) -> None:
        """index() must preserve manifest version."""
        result = null_backend.index(chunk_payload)
        assert result.manifest_version == chunk_payload.manifest_version


class TestQueryMethod:
    """Test query() method contract."""

    def test_query_accepts_query_payload(
        self, null_backend: NullBackend, query_payload: QueryPayload
    ) -> None:
        """query() must accept QueryPayload."""
        result = null_backend.query(query_payload)
        assert isinstance(result, QueryResult)

    def test_query_returns_query_result(
        self, null_backend: NullBackend, query_payload: QueryPayload
    ) -> None:
        """query() must return QueryResult."""
        result = null_backend.query(query_payload)

        assert hasattr(result, "manifest_version")
        assert hasattr(result, "results")
        assert hasattr(result, "message")

        assert result.manifest_version == query_payload.manifest_version
        assert isinstance(result.results, (list, tuple))

    def test_query_returns_list_of_mappings(
        self,
        null_backend: NullBackend,
        chunk_payload: ChunkPayload,
        query_payload: QueryPayload,
    ) -> None:
        """query() results must be list of mappings."""
        # Index some data first
        null_backend.index(chunk_payload)

        result = null_backend.query(query_payload)
        assert isinstance(result.results, (list, tuple))

        for item in result.results:
            assert isinstance(item, dict)

    def test_query_preserves_manifest_version(
        self, null_backend: NullBackend, query_payload: QueryPayload
    ) -> None:
        """query() must preserve manifest version."""
        result = null_backend.query(query_payload)
        assert result.manifest_version == query_payload.manifest_version


class TestHealthcheckMethod:
    """Test healthcheck() method contract."""

    def test_healthcheck_returns_health_status(
        self, null_backend: NullBackend
    ) -> None:
        """healthcheck() must return HealthStatus."""
        result = null_backend.healthcheck()
        assert isinstance(result, HealthStatus)

    def test_health_status_has_ok_field(self, null_backend: NullBackend) -> None:
        """HealthStatus must have ok: bool field."""
        result = null_backend.healthcheck()
        assert hasattr(result, "ok")
        assert isinstance(result.ok, bool)

    def test_health_status_has_details_field(self, null_backend: NullBackend) -> None:
        """HealthStatus must have details: str | None field."""
        result = null_backend.healthcheck()
        assert hasattr(result, "details")
        assert result.details is None or isinstance(result.details, str)

    def test_null_backend_is_healthy(self, null_backend: NullBackend) -> None:
        """Null backend should always report healthy."""
        result = null_backend.healthcheck()
        assert result.ok is True


class TestGetCapabilitiesMethod:
    """Test get_capabilities() method contract."""

    def test_get_capabilities_returns_capabilities(
        self, null_backend: NullBackend
    ) -> None:
        """get_capabilities() must return Capabilities."""
        result = null_backend.get_capabilities()
        assert isinstance(result, Capabilities)

    def test_capabilities_has_required_fields(
        self, null_backend: NullBackend
    ) -> None:
        """Capabilities must have all required fields."""
        caps = null_backend.get_capabilities()

        # Boolean flags
        assert hasattr(caps, "embeddings")
        assert isinstance(caps.embeddings, bool)

        assert hasattr(caps, "entity_extraction")
        assert isinstance(caps.entity_extraction, bool)

        assert hasattr(caps, "graph_query")
        assert isinstance(caps.graph_query, bool)

        assert hasattr(caps, "rerank")
        assert isinstance(caps.rerank, bool)

        # Schema versions
        assert hasattr(caps, "schema_versions")
        assert isinstance(caps.schema_versions, (list, tuple))

        # Optional fields
        assert hasattr(caps, "supports_falkor")
        assert hasattr(caps, "supports_milvus")
        assert hasattr(caps, "supports_neo4j")
        assert hasattr(caps, "max_tokens")

    def test_capabilities_schema_versions_not_empty(
        self, null_backend: NullBackend
    ) -> None:
        """Capabilities must declare at least one schema version."""
        caps = null_backend.get_capabilities()
        assert len(caps.schema_versions) > 0


class TestExceptionHandling:
    """Test exception contract."""

    def test_backend_error_is_exception(self) -> None:
        """BackendError must be an Exception."""
        assert issubclass(BackendError, Exception)

    def test_config_error_is_backend_error(self) -> None:
        """ConfigError must inherit from BackendError."""
        assert issubclass(ConfigError, BackendError)

    def test_transient_error_is_backend_error(self) -> None:
        """TransientError must inherit from BackendError."""
        assert issubclass(TransientError, BackendError)

    def test_exceptions_can_be_raised(self) -> None:
        """All exception types must be raisable."""
        with pytest.raises(BackendError):
            raise BackendError("test")

        with pytest.raises(ConfigError):
            raise ConfigError("test")

        with pytest.raises(TransientError):
            raise TransientError("test")

    def test_config_error_caught_as_backend_error(self) -> None:
        """ConfigError can be caught as BackendError."""
        with pytest.raises(BackendError):
            raise ConfigError("test")

    def test_transient_error_caught_as_backend_error(self) -> None:
        """TransientError can be caught as BackendError."""
        with pytest.raises(BackendError):
            raise TransientError("test")


class TestBackendRegistry:
    """Test backend registry contract."""

    def test_list_backends_returns_list(self) -> None:
        """list_backends() must return list."""
        backends = list_backends()
        assert isinstance(backends, list)

    def test_null_backend_registered(self) -> None:
        """Null backend must be registered."""
        backends = list_backends()
        assert "null" in backends

    def test_get_backend_returns_backend(self) -> None:
        """get_backend() must return MemoryBackend instance."""
        backend = get_backend("null")
        assert isinstance(backend, MemoryBackend)

    def test_get_backend_raises_for_unknown(self) -> None:
        """get_backend() must raise BackendError for unknown backend."""
        with pytest.raises(BackendError, match="not registered"):
            get_backend("nonexistent-backend")


class TestNullBackendBehavior:
    """Test null backend specific behavior."""

    def test_null_backend_stores_corpus(
        self, null_backend: NullBackend, corpus_payload: CorpusPayload
    ) -> None:
        """Null backend should store corpus for inspection."""
        null_backend.prepare(corpus_payload)
        # Verify it was stored (implementation detail, but useful for testing)
        assert null_backend._corpus is not None
        assert null_backend._corpus.manifest_version == corpus_payload.manifest_version

    def test_null_backend_stores_chunks(
        self, null_backend: NullBackend, chunk_payload: ChunkPayload
    ) -> None:
        """Null backend should store chunks for inspection."""
        null_backend.index(chunk_payload)
        assert null_backend._chunks is not None
        assert null_backend._chunks.manifest_version == chunk_payload.manifest_version

    def test_null_backend_echoes_in_query(
        self,
        null_backend: NullBackend,
        chunk_payload: ChunkPayload,
        query_payload: QueryPayload,
    ) -> None:
        """Null backend should echo chunks in query results."""
        null_backend.index(chunk_payload)
        result = null_backend.query(query_payload)

        # Should return all chunks
        assert len(result.results) == len(chunk_payload.chunks)

    def test_null_backend_custom_capabilities(self) -> None:
        """Null backend should accept custom capabilities."""
        custom_caps = Capabilities(
            embeddings=True,
            entity_extraction=True,
            schema_versions=["1.0.0", "1.1.0"],
        )
        backend = NullBackend(capabilities=custom_caps)
        caps = backend.get_capabilities()

        assert caps.embeddings is True
        assert caps.entity_extraction is True
        assert "1.0.0" in caps.schema_versions
        assert "1.1.0" in caps.schema_versions


class TestEndToEndWorkflow:
    """Test complete workflow through backend."""

    def test_prepare_index_query_workflow(
        self,
        null_backend: NullBackend,
        corpus_payload: CorpusPayload,
        chunk_payload: ChunkPayload,
        query_payload: QueryPayload,
    ) -> None:
        """Test complete prepare→index→query workflow."""
        # Step 1: Prepare corpus
        prepare_result = null_backend.prepare(corpus_payload)
        assert prepare_result.prepared_count == 2

        # Step 2: Index chunks
        index_result = null_backend.index(chunk_payload)
        assert index_result.indexed_count == 2

        # Step 3: Query
        query_result = null_backend.query(query_payload)
        assert len(query_result.results) > 0

        # Verify manifest versions carried through
        assert prepare_result.manifest_version == "1.0.0"
        assert index_result.manifest_version == "1.0.0"
        assert query_result.manifest_version == "1.0.0"

    def test_healthcheck_anytime(
        self, null_backend: NullBackend, corpus_payload: CorpusPayload
    ) -> None:
        """Healthcheck should work before/during/after operations."""
        # Before any operations
        health1 = null_backend.healthcheck()
        assert health1.ok is True

        # During operations
        null_backend.prepare(corpus_payload)
        health2 = null_backend.healthcheck()
        assert health2.ok is True

        # After operations
        caps = null_backend.get_capabilities()
        health3 = null_backend.healthcheck()
        assert health3.ok is True

    def test_multiple_operations(
        self, null_backend: NullBackend, corpus_payload: CorpusPayload
    ) -> None:
        """Backend should handle multiple operations gracefully."""
        # Prepare twice (simulate re-preparation)
        result1 = null_backend.prepare(corpus_payload)
        result2 = null_backend.prepare(corpus_payload)

        assert result1.prepared_count == result2.prepared_count
        assert result1.manifest_version == result2.manifest_version
