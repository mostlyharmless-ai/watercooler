"""Null backend implementation for contract tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Sequence

from . import (
    Capabilities,
    ChunkPayload,
    CorpusPayload,
    HealthStatus,
    IndexResult,
    MemoryBackend,
    PrepareResult,
    QueryPayload,
    QueryResult,
)


class NullBackend(MemoryBackend):
    """In-memory no-op backend that echoes payloads."""

    def __init__(self, capabilities: Capabilities | None = None) -> None:
        self._capabilities = capabilities or Capabilities(schema_versions=["1.0.0"])
        self._corpus: CorpusPayload | None = None
        self._chunks: ChunkPayload | None = None

    def prepare(self, corpus: CorpusPayload) -> PrepareResult:
        self._corpus = deepcopy(corpus)
        prepared_count = len(corpus.entries)
        return PrepareResult(
            manifest_version=corpus.manifest_version,
            prepared_count=prepared_count,
            message="null backend prepared corpus",
        )

    def index(self, chunks: ChunkPayload) -> IndexResult:
        self._chunks = deepcopy(chunks)
        indexed_count = len(chunks.chunks)
        return IndexResult(
            manifest_version=chunks.manifest_version,
            indexed_count=indexed_count,
            message="null backend indexed chunks",
        )

    def query(self, query: QueryPayload) -> QueryResult:
        results: list[dict[str, Any]] = []
        if self._chunks:
            for chunk in self._chunks.chunks:
                results.append(
                    {
                        "chunk": chunk,
                        "query": query.queries,
                    }
                )
        return QueryResult(
            manifest_version=query.manifest_version,
            results=results,
            message="null backend echo response",
        )

    def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True, details="null backend is healthy")

    def get_capabilities(self) -> Capabilities:
        # Create a copy and update with new operation flags
        caps = deepcopy(self._capabilities)
        # Null backend doesn't support any new operations
        caps.supports_nodes = False
        caps.supports_facts = False
        caps.supports_episodes = False
        caps.supports_chunks = False
        caps.supports_edges = False
        caps.node_id_type = "passthrough"
        caps.edge_id_type = "passthrough"
        return caps


    def search_nodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
        entity_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Null implementation - raises UnsupportedOperationError."""
        from . import UnsupportedOperationError
        raise UnsupportedOperationError(
            "Backend 'null' does not support node search operations"
        )

    def search_facts(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
        center_node_id: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Null implementation - raises UnsupportedOperationError."""
        from . import UnsupportedOperationError
        raise UnsupportedOperationError(
            "Backend 'null' does not support fact search operations"
        )

    def search_episodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Null implementation - raises UnsupportedOperationError."""
        from . import UnsupportedOperationError
        raise UnsupportedOperationError(
            "Backend 'null' does not support episode search operations"
        )

    def get_node(
        self,
        node_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Null implementation - raises UnsupportedOperationError."""
        from . import UnsupportedOperationError
        raise UnsupportedOperationError(
            "Backend 'null' does not support node retrieval operations"
        )

    def get_edge(
        self,
        edge_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Null implementation - raises UnsupportedOperationError."""
        from . import UnsupportedOperationError
        raise UnsupportedOperationError(
            "Backend 'null' does not support edge retrieval operations"
        )

