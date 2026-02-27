"""Backend contract for pluggable memory systems."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol, Sequence, Any, TypedDict, runtime_checkable


class BackendError(Exception):
    """Base exception for backend failures."""


class ConfigError(BackendError):
    """Raised when backend configuration is invalid."""


class TransientError(BackendError):
    """Raised for retryable errors."""


class UnsupportedOperationError(BackendError):
    """Raised when an operation is not supported by the backend."""


class IdNotSupportedError(BackendError):
    """Raised when an ID format is not supported by the backend."""


# Type aliases for backend identification
BackendName = Literal["graphiti", "leanrag", "null", "custom"]


class _CoreResultRequired(TypedDict):
    """Required fields for CoreResult."""
    id: str  # Backend-specific ID format
    backend: BackendName  # Which backend produced this


class CoreResult(_CoreResultRequired, total=False):
    """Normalized response format for all backends.

    Core fields that all backends should provide when returning
    nodes, facts, episodes, or chunks.

    Required fields: id, backend
    Optional fields: score, name, content, summary, source, group_id, metadata, extra
    """

    score: float | None  # 0.0-1.0 or None
    name: str | None
    content: str | None
    summary: str | None
    source: str | None
    group_id: str | None
    metadata: Mapping[str, Any]  # Free-form metadata
    extra: Mapping[str, Any]  # Backend-specific fields


# NOTE: The following TypedDicts (CapabilityGetNode, CapabilityGetEdge, etc.) are defined
# for Phase 2+ of the backend abstraction refactoring. They will be used in the service
# layer to enforce capability checks and provide detailed capability descriptors.


class CapabilityGetNode(TypedDict):
    """Node retrieval capabilities."""

    id: bool  # Supports UUID/ID lookup
    name: bool  # Supports name lookup


class CapabilityGetEdge(TypedDict):
    """Edge retrieval capabilities."""

    id: bool  # Supports edge ID lookup


class CapabilityGetEpisode(TypedDict):
    """Episode retrieval capabilities."""

    id: bool  # Supports episode ID lookup


class Supports(TypedDict):
    """Operation support flags."""

    search_nodes: bool
    search_facts: bool
    search_episodes: bool
    search_chunks: bool
    get_node: CapabilityGetNode
    get_edge: CapabilityGetEdge
    get_episode: CapabilityGetEpisode


class IdShapes(TypedDict):
    """ID format descriptors for different entity types."""

    node: Literal["uuid", "name", "passthrough"]
    edge: Literal["uuid", "synthetic", "passthrough"]
    episode: Literal["uuid", "passthrough"]


class CapabilityDescriptor(TypedDict, total=False):
    """Complete backend capability descriptor.

    Describes what operations a backend supports and what ID formats
    it uses for different entity types.
    """

    backend: BackendName
    supports: Supports
    id_shapes: IdShapes
    notes: Sequence[str]


@dataclass
class Capabilities:
    """Describes supported backend features."""

    # Legacy capabilities (maintain backwards compatibility)
    embeddings: bool = False
    entity_extraction: bool = False
    graph_query: bool = False
    rerank: bool = False
    schema_versions: Sequence[str] = field(default_factory=list)
    supports_falkor: bool = False
    supports_milvus: bool = False
    supports_neo4j: bool = False
    max_tokens: int | None = None

    # New operation support flags (Phase 1)
    supports_nodes: bool = False
    supports_facts: bool = False
    supports_episodes: bool = False
    supports_chunks: bool = False
    supports_edges: bool = False

    # ID modality flags (how backends identify entities/edges)
    node_id_type: Literal["uuid", "name", "hybrid"] = "uuid"
    edge_id_type: Literal["uuid", "synthetic", "none"] = "uuid"


@dataclass
class HealthStatus:
    """Health response from backend."""

    ok: bool
    details: str | None = None


@dataclass
class PrepareResult:
    """Result of prepare step."""

    manifest_version: str
    prepared_count: int = 0
    message: str | None = None


@dataclass
class IndexResult:
    """Result of index step."""

    manifest_version: str
    indexed_count: int = 0
    message: str | None = None


@dataclass
class QueryResult:
    """Result of query step."""

    manifest_version: str
    results: Sequence[Mapping[str, Any]] = field(default_factory=list)
    communities: Sequence[Mapping[str, Any]] = field(default_factory=list)
    message: str | None = None


@dataclass
class EpisodeRecord:
    """Lightweight episode reference for corpus enumeration.

    All non-key string fields default to "" because FalkorDB Cypher results
    may return None for optional node properties (e.g., source_description
    on older episodes). Downstream consumers (LeanRAG pipeline) receive empty
    strings instead of None, avoiding crashes in embedding generation.

    created_at is kept as str (not datetime) because _filter_by_time_range()
    operates on raw dicts BEFORE EpisodeRecord construction (see
    get_group_episodes ordering note).
    """

    uuid: str
    name: str = ""
    content: str = ""
    source_description: str = ""
    group_id: str = ""
    created_at: str = ""


@dataclass
class CorpusPayload:
    """Canonical corpus payload passed to prepare()."""

    manifest_version: str
    threads: Sequence[Mapping[str, Any]]
    entries: Sequence[Mapping[str, Any]]
    edges: Sequence[Mapping[str, Any]] | None = None
    metadata: Mapping[str, Any] | None = None
    chunker_name: str | None = None
    chunker_params: Mapping[str, Any] | None = None


@dataclass
class ChunkPayload:
    """Chunk payload passed to index()."""

    manifest_version: str
    chunks: Sequence[Mapping[str, Any]]
    threads: Sequence[Mapping[str, Any]] | None = None
    entries: Sequence[Mapping[str, Any]] | None = None
    edges: Sequence[Mapping[str, Any]] | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass
class QueryPayload:
    """Query payload passed to query()."""

    manifest_version: str
    queries: Sequence[Mapping[str, Any]]
    metadata: Mapping[str, Any] | None = None


@runtime_checkable
class MemoryBackend(Protocol):
    """Protocol for pluggable memory backends."""

    def prepare(self, corpus: CorpusPayload) -> PrepareResult:
        """Prepare a corpus for indexing.

        This step processes raw watercooler threads and entries for indexing. Different
        backends may perform entity extraction, chunking, or other preprocessing.

        Args:
            corpus: Canonical corpus payload containing threads and entries.

        Returns:
            PrepareResult with count of prepared items and optional message.

        Raises:
            ConfigError: If backend configuration is invalid.
            TransientError: For retryable failures (network timeouts, API limits).
            BackendError: For other backend failures.

        Example:
            >>> from watercooler_memory.backends import get_backend, LeanRAGConfig
            >>> from watercooler_memory.backends import CorpusPayload
            >>> config = LeanRAGConfig(work_dir=Path("./memory"))
            >>> backend = get_backend("leanrag", config)
            >>> corpus = CorpusPayload(
            ...     manifest_version="1.0.0",
            ...     threads=[{"thread_id": "auth", "title": "Auth Feature"}],
            ...     entries=[{
            ...         "id": "entry-001",
            ...         "thread_id": "auth",
            ...         "agent": "Claude",
            ...         "role": "implementer",
            ...         "entry_type": "Note",
            ...         "timestamp": "2025-01-01T12:00:00Z",
            ...         "title": "OAuth Implementation",
            ...         "body": "Implemented OAuth2 with JWT tokens...",
            ...     }],
            ... )
            >>> result = backend.prepare(corpus)
            >>> print(f"Version: {result.manifest_version}")
            >>> print(f"Prepared {result.prepared_count} entries")
            Version: 1.0.0
            Prepared 1 entries
        """

    def index(self, chunks: ChunkPayload) -> IndexResult:
        """Index prepared chunks into the backend's storage.

        This step builds the knowledge graph, vector index, or other backend-specific
        data structures from chunked text. For LeanRAG this runs entity extraction and
        graph building. For Graphiti this is typically a no-op (indexing happens during prepare).

        Args:
            chunks: Chunk payload with text chunks and optional metadata.

        Returns:
            IndexResult with count of indexed chunks and optional message.

        Raises:
            ConfigError: If backend configuration is invalid.
            TransientError: For retryable failures.
            BackendError: For other backend failures.

        Example:
            >>> from watercooler_memory.backends import ChunkPayload
            >>> chunks = ChunkPayload(
            ...     manifest_version="1.0.0",
            ...     chunks=[
            ...         {
            ...             "hash_code": "abc123",
            ...             "text": "OAuth2 implementation complete with JWT support.",
            ...             "metadata": {"thread_id": "auth", "entry_id": "entry-001"},
            ...         }
            ...     ],
            ... )
            >>> result = backend.index(chunks)
            >>> print(f"Indexed {result.indexed_count} chunks")
            Indexed 1 chunks
        """

    def query(self, query: QueryPayload) -> QueryResult:
        """Execute semantic search queries against the backend.

        Returns relevant results based on the backend's search strategy (vector similarity,
        graph traversal, hybrid search, etc.). Results include content, scores, and metadata.

        Args:
            query: Query payload with one or more query strings.

        Returns:
            QueryResult with search results and optional message.

        Raises:
            ConfigError: If backend configuration is invalid.
            TransientError: For retryable failures.
            BackendError: For other backend failures.

        Example:
            >>> from watercooler_memory.backends import QueryPayload
            >>> queries = QueryPayload(
            ...     manifest_version="1.0.0",
            ...     queries=[
            ...         {"query": "What authentication method was implemented?"},
            ...         {"query": "Who implemented OAuth2?"},
            ...     ],
            ... )
            >>> result = backend.query(queries)
            >>> for item in result.results:
            ...     # Each item is a dict with keys: query, content, score, metadata
            ...     print(f"Query: {item['query']}")
            ...     print(f"Content: {item['content'][:100]}...")
            ...     print(f"Score: {item['score']:.2f}")
            ...     print(f"Metadata: {item.get('metadata', {})}")
            Query: What authentication method was implemented?
            Content: Implemented OAuth2 with JWT tokens for secure authentication...
            Score: 0.89
            Metadata: {'thread_id': 'auth', 'entry_id': 'entry-001'}
        """

    def healthcheck(self) -> HealthStatus:
        """Check backend health and dependencies.

        Verifies that the backend can connect to required services (databases, APIs),
        has necessary credentials, and is ready for operations. Always call this before
        running prepare/index/query operations.

        Returns:
            HealthStatus with ok flag and optional details message.

        Example:
            >>> health = backend.healthcheck()
            >>> if not health.ok:
            ...     print(f"Backend not healthy: {health.details}")
            ...     exit(1)
            >>> print("Backend ready")
            Backend ready
        """

    def get_capabilities(self) -> Capabilities:
        """Return supported backend capabilities.

        Query the backend's feature set to determine what operations are supported
        (embeddings, entity extraction, graph queries, etc.).

        Returns:
            Capabilities object describing supported features.

        Example:
            >>> caps = backend.get_capabilities()
            >>> if caps.entity_extraction:
            ...     print("Backend supports entity extraction")
            >>> if caps.supports_falkor:
            ...     print("Using FalkorDB graph database")
            Backend supports entity extraction
            Using FalkorDB graph database
        """


    def search_nodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
        entity_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for entity nodes using semantic search.

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by (backend-specific)
            max_results: Maximum number of results to return
            entity_types: Optional list of entity types to filter by

        Returns:
            Sequence of normalized CoreResult dictionaries

        Raises:
            UnsupportedOperationError: If backend doesn't support node search
            ConfigError: If backend configuration is invalid
            TransientError: For retryable failures
            BackendError: For other backend failures
        """

    def search_facts(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
        center_node_id: str | None = None,
        start_time: str = "",
        end_time: str = "",
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Search for facts/relationships using semantic search.

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by (backend-specific)
            max_results: Maximum number of results to return
            center_node_id: Optional node ID to center search around
            start_time: ISO 8601 timestamp; exclude facts invalidated before this time
            end_time: ISO 8601 timestamp; exclude facts invalidated after this time
            active_only: If True, return only non-superseded facts (invalid_at=None)

        Returns:
            Sequence of normalized CoreResult dictionaries

        Raises:
            UnsupportedOperationError: If backend doesn't support fact search
            ConfigError: If backend configuration is invalid
            TransientError: For retryable failures
            BackendError: For other backend failures
        """

    def search_episodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for episodes (provenance-bearing content) using semantic search.

        Episodes are content with temporal/actor provenance (who/when context).
        Different from chunks which are static document segments.

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by (backend-specific)
            max_results: Maximum number of results to return

        Returns:
            Sequence of normalized CoreResult dictionaries

        Raises:
            UnsupportedOperationError: If backend doesn't support episode search
            ConfigError: If backend configuration is invalid
            TransientError: For retryable failures
            BackendError: For other backend failures
        """

    def get_node(
        self,
        node_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get a node by ID.

        ID format is backend-specific (UUID for Graphiti, entity name for LeanRAG).

        Args:
            node_id: Node identifier (format depends on backend)
            group_id: Optional group ID to filter by (backend-specific)

        Returns:
            Normalized CoreResult dictionary or None if not found

        Raises:
            UnsupportedOperationError: If backend doesn't support node retrieval
            IdNotSupportedError: If ID format is not supported by backend
            ConfigError: If backend configuration is invalid
            TransientError: For retryable failures
            BackendError: For other backend failures
        """

    def get_edge(
        self,
        edge_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get an edge/fact by ID.

        ID format is backend-specific (UUID for Graphiti, synthetic for LeanRAG).

        Args:
            edge_id: Edge identifier (format depends on backend)
            group_id: Optional group ID to filter by (backend-specific)

        Returns:
            Normalized CoreResult dictionary or None if not found

        Raises:
            UnsupportedOperationError: If backend doesn't support edge retrieval
            IdNotSupportedError: If ID format is not supported by backend
            ConfigError: If backend configuration is invalid
            TransientError: For retryable failures
            BackendError: For other backend failures
        """


__all__ = [
    # Exceptions
    "BackendError",
    "ConfigError",
    "TransientError",
    "UnsupportedOperationError",
    "IdNotSupportedError",
    # Type aliases
    "BackendName",
    # TypedDicts
    "CoreResult",
    "CapabilityGetNode",
    "CapabilityGetEdge",
    "CapabilityGetEpisode",
    "Supports",
    "IdShapes",
    "CapabilityDescriptor",
    # Dataclasses
    "Capabilities",
    "ChunkPayload",
    "CorpusPayload",
    "EpisodeRecord",
    "HealthStatus",
    "IndexResult",
    "PrepareResult",
    "QueryPayload",
    "QueryResult",
    # Protocol
    "MemoryBackend",
    # Registry helpers
    "register_backend",
    "get_backend",
    "list_backends",
    "resolve_backend",
    "auto_register_builtin",
    # Backend implementations
    "GraphitiBackend",
    "LeanRAGBackend",
    "LeanRAGConfig",
]

# Registry re-exports
from .registry import (  # noqa: E402
    auto_register_builtin,
    get_backend,
    list_backends,
    register_backend,
    resolve_backend,
)

# Backend re-exports
from .graphiti import GraphitiBackend  # noqa: E402
from .leanrag import LeanRAGBackend, LeanRAGConfig  # noqa: E402
