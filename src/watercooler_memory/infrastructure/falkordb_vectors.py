"""FalkorDB vector adapter for memory system.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 2.1:
Reference patterns from Graphiti:
- Storage: SET n.embedding = vecf32($embedding)
- Search: (2 - vec.cosineDistance(n.embedding, vecf32($query_vector)))/2 AS score

This adapter provides a consistent interface for vector operations in FalkorDB,
with dimension enforcement to ensure all vectors are 1024-d (BGE-M3).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .embedding_validator import (
    EXPECTED_DIM,
    validate_embedding_dimension,
    validate_embeddings_batch,
)

# Try to import FalkorDB client
try:
    from falkordb import FalkorDB

    FALKORDB_AVAILABLE = True
except ImportError:
    FalkorDB = None  # type: ignore
    FALKORDB_AVAILABLE = False


# Cypher identifier validation pattern
# Valid identifiers: start with letter or underscore, followed by alphanumeric/underscore
_CYPHER_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(value: str, name: str) -> str:
    """Validate that a value is a safe Cypher identifier.

    Prevents Cypher injection by ensuring identifiers match a strict pattern.
    Valid identifiers start with a letter or underscore and contain only
    alphanumeric characters and underscores.

    Args:
        value: The identifier value to validate.
        name: Human-readable name for error messages (e.g., "node_label").

    Returns:
        The validated value (unchanged if valid).

    Raises:
        ValueError: If the value is not a valid Cypher identifier.
    """
    if not value:
        raise ValueError(f"Invalid {name}: empty value. Must be a non-empty identifier.")
    if not _CYPHER_IDENTIFIER_PATTERN.match(value):
        raise ValueError(
            f"Invalid {name}: {value!r}. Must start with letter/underscore "
            "and contain only alphanumeric characters and underscores."
        )
    return value


@dataclass
class FalkorDBVectorConfig:
    """Configuration for FalkorDB vector adapter.

    Environment variables:
    - FALKORDB_HOST: FalkorDB host (default: localhost)
    - FALKORDB_PORT: FalkorDB port (default: 6379)
    - FALKORDB_DATABASE: FalkorDB database name (default: default)
    """

    host: str = "localhost"
    port: int = 6379
    database: str = "default"
    embedding_dim: int = EXPECTED_DIM  # 1024 for BGE-M3

    @classmethod
    def from_env(cls) -> FalkorDBVectorConfig:
        """Create config from environment variables."""
        return cls(
            host=os.environ.get("FALKORDB_HOST", "localhost"),
            port=int(os.environ.get("FALKORDB_PORT", "6379")),
            database=os.environ.get("FALKORDB_DATABASE", "default"),
        )


@dataclass
class VectorSearchResult:
    """Result from vector similarity search.

    Attributes:
        node_id: The unique identifier of the matched node.
        score: Cosine similarity score (0.0 to 1.0, higher is more similar).
        properties: Additional node properties returned from the query.
    """

    node_id: str
    score: float
    properties: dict[str, Any] = field(default_factory=dict)


def normalize_score(cosine_distance: float) -> float:
    """Normalize FalkorDB cosine distance to similarity score.

    FalkorDB's vec.cosineDistance returns values from 0 (identical) to 2 (opposite).
    This normalizes to 0.0 (opposite) to 1.0 (identical).

    Formula: (2 - cosine_distance) / 2

    Args:
        cosine_distance: FalkorDB cosine distance (0-2 range).

    Returns:
        Normalized similarity score (0-1 range).
    """
    return (2 - cosine_distance) / 2


def build_storage_query(
    node_label: str,
    id_field: str,
    embedding_field: str = "embedding",
    additional_props: Optional[list[str]] = None,
) -> str:
    """Build Cypher query for storing vectors in FalkorDB.

    Uses MERGE to upsert nodes and vecf32() for vector storage.

    Args:
        node_label: The label for the node (e.g., "Chunk", "Entry").
        id_field: The field used for node identification.
        embedding_field: Name of the embedding field.
        additional_props: Additional properties to set on the node.

    Returns:
        Cypher query string with $node_id, $embedding, and property parameters.

    Raises:
        ValueError: If any identifier contains invalid characters (Cypher injection protection).
    """
    # Validate all identifiers to prevent Cypher injection
    node_label = _validate_identifier(node_label, "node_label")
    id_field = _validate_identifier(id_field, "id_field")
    embedding_field = _validate_identifier(embedding_field, "embedding_field")

    props_set = ""
    if additional_props:
        # Validate each property name
        validated_props = [_validate_identifier(prop, "property") for prop in additional_props]
        props_lines = [f"n.{prop} = ${prop}" for prop in validated_props]
        props_set = ", " + ", ".join(props_lines)

    return f"""
        MERGE (n:{node_label} {{{id_field}: $node_id}})
        SET n.{embedding_field} = vecf32($embedding){props_set}
        RETURN n.{id_field} AS node_id
    """


def build_search_query(
    node_label: str,
    embedding_field: str = "embedding",
    return_fields: Optional[list[str]] = None,
    where_clause: Optional[str] = None,
) -> str:
    """Build Cypher query for vector similarity search in FalkorDB.

    Uses vec.cosineDistance with vecf32() for similarity computation.

    Args:
        node_label: The label to search within.
        embedding_field: Name of the embedding field.
        return_fields: Fields to return (default: node_id, score).
        where_clause: Optional WHERE clause for filtering.
            WARNING: This is trusted input only - not validated. Callers must
            ensure it does not contain untrusted user input to prevent Cypher injection.

    Returns:
        Cypher query string with $query_vector and $limit parameters.

    Raises:
        ValueError: If any identifier contains invalid characters (Cypher injection protection).
    """
    # Validate all identifiers to prevent Cypher injection
    node_label = _validate_identifier(node_label, "node_label")
    embedding_field = _validate_identifier(embedding_field, "embedding_field")

    if return_fields is None:
        return_fields = ["node_id", "score"]

    # Note: where_clause is trusted input - not validated (see docstring warning)
    where_str = f"WHERE {where_clause}" if where_clause else ""

    # Build return clause with validated field names
    return_parts = []
    for field_name in return_fields:
        if field_name == "score":
            return_parts.append("score")
        elif field_name == "node_id":
            return_parts.append("n.uuid AS node_id")
        else:
            # Validate each field name
            validated = _validate_identifier(field_name, "return_field")
            return_parts.append(f"n.{validated} AS {validated}")

    return_clause = ", ".join(return_parts)

    return f"""
        MATCH (n:{node_label})
        {where_str}
        WITH n, (2 - vec.cosineDistance(n.{embedding_field}, vecf32($query_vector)))/2 AS score
        WHERE score > 0
        RETURN {return_clause}
        ORDER BY score DESC
        LIMIT $limit
    """


class FalkorDBVectorAdapter:
    """Adapter for vector operations in FalkorDB.

    Provides a consistent interface for storing and searching vectors,
    with dimension enforcement to ensure compatibility across memory tiers.

    Example:
        config = FalkorDBVectorConfig.from_env()
        adapter = FalkorDBVectorAdapter(config)
        adapter.connect()

        # Store vector
        adapter.store_vector(
            node_label="Chunk",
            node_id="chunk:abc123",
            embedding=[0.1] * 1024,
            properties={"text": "Hello world"},
        )

        # Search vectors
        results = adapter.search_vectors(
            node_label="Chunk",
            query_vector=[0.2] * 1024,
            limit=10,
        )
    """

    def __init__(self, config: FalkorDBVectorConfig):
        """Initialize adapter with configuration.

        Args:
            config: FalkorDB connection configuration.
        """
        self.config = config
        self._client: Any = None
        self._graph: Any = None

    def connect(self) -> None:
        """Connect to FalkorDB.

        Raises:
            ImportError: If falkordb package is not installed.
            ConnectionError: If connection fails.
        """
        if not FALKORDB_AVAILABLE:
            raise ImportError(
                "falkordb is required for FalkorDB vector operations. "
                "Install with: pip install 'watercooler-cloud[memory]'"
            )

        self._client = FalkorDB(host=self.config.host, port=self.config.port)
        self._graph = self._client.select_graph(self.config.database)

    def disconnect(self) -> None:
        """Disconnect from FalkorDB."""
        if self._client:
            self._client = None
            self._graph = None

    def healthcheck(self) -> bool:
        """Check if FalkorDB connection is healthy.

        Returns:
            True if connected and responding, False otherwise.
        """
        if not self._graph:
            return False

        try:
            self._graph.query("RETURN 1")
            return True
        except Exception:
            return False

    def store_vector(
        self,
        node_label: str,
        node_id: str,
        embedding: list[float],
        properties: Optional[dict[str, Any]] = None,
        id_field: str = "uuid",
    ) -> str:
        """Store a vector in FalkorDB.

        Args:
            node_label: Label for the node.
            node_id: Unique identifier for the node.
            embedding: Vector to store (must be 1024-d).
            properties: Additional properties to store.
            id_field: Field name for the ID.

        Returns:
            The node ID that was stored.

        Raises:
            DimensionMismatchError: If embedding is not 1024 dimensions.
        """
        # Validate dimension
        validate_embedding_dimension(embedding)

        # Build query
        additional_props = list(properties.keys()) if properties else None
        query = build_storage_query(
            node_label=node_label,
            id_field=id_field,
            additional_props=additional_props,
        )

        # Build parameters
        params = {"node_id": node_id, "embedding": embedding}
        if properties:
            params.update(properties)

        # Execute
        self._graph.query(query, params)
        return node_id

    def batch_store_vectors(
        self,
        node_label: str,
        vectors: list[tuple[str, list[float], Optional[dict[str, Any]]]],
        id_field: str = "uuid",
    ) -> int:
        """Store multiple vectors in batch.

        Args:
            node_label: Label for all nodes.
            vectors: List of (node_id, embedding, properties) tuples.
            id_field: Field name for the ID.

        Returns:
            Number of vectors stored.

        Raises:
            DimensionMismatchError: If any embedding is not 1024 dimensions.
        """
        # Validate all dimensions first
        embeddings = [v[1] for v in vectors]
        validate_embeddings_batch(embeddings)

        # Store each vector
        stored = 0
        for node_id, embedding, properties in vectors:
            self.store_vector(
                node_label=node_label,
                node_id=node_id,
                embedding=embedding,
                properties=properties,
                id_field=id_field,
            )
            stored += 1

        return stored

    def search_vectors(
        self,
        node_label: str,
        query_vector: list[float],
        limit: int = 10,
        return_fields: Optional[list[str]] = None,
        where_clause: Optional[str] = None,
    ) -> list[VectorSearchResult]:
        """Search for similar vectors in FalkorDB.

        Args:
            node_label: Label to search within.
            query_vector: Vector to search for (must be 1024-d).
            limit: Maximum number of results.
            return_fields: Fields to include in results.
            where_clause: Optional filter clause.

        Returns:
            List of search results ordered by similarity.

        Raises:
            DimensionMismatchError: If query_vector is not 1024 dimensions.
        """
        # Validate dimension
        validate_embedding_dimension(query_vector)

        # Build and execute query
        query = build_search_query(
            node_label=node_label,
            return_fields=return_fields,
            where_clause=where_clause,
        )

        result = self._graph.query(query, {"query_vector": query_vector, "limit": limit})

        # Parse results
        results = []
        for row in result.result_set:
            # Row format depends on return_fields
            if return_fields:
                node_id = row[0]
                score = row[1] if len(row) > 1 else 0.0
                props = {}
                for i, field in enumerate(return_fields[2:], start=2):
                    if i < len(row):
                        props[field] = row[i]
            else:
                node_id = row[0]
                score = row[1] if len(row) > 1 else 0.0
                props = {}

            results.append(VectorSearchResult(
                node_id=node_id,
                score=score,
                properties=props,
            ))

        return results

    def delete_vector(
        self,
        node_label: str,
        node_id: str,
        id_field: str = "uuid",
    ) -> bool:
        """Delete a vector node from FalkorDB.

        Args:
            node_label: Label of the node.
            node_id: ID of the node to delete.
            id_field: Field name for the ID.

        Returns:
            True if node was deleted, False if not found.

        Raises:
            ValueError: If node_label or id_field contains invalid characters.
        """
        # Validate identifiers to prevent Cypher injection
        node_label = _validate_identifier(node_label, "node_label")
        id_field = _validate_identifier(id_field, "id_field")

        query = f"""
            MATCH (n:{node_label} {{{id_field}: $node_id}})
            DELETE n
            RETURN count(n) AS deleted
        """

        result = self._graph.query(query, {"node_id": node_id})
        return result.result_set[0][0] > 0 if result.result_set else False


def is_falkordb_available() -> bool:
    """Check if FalkorDB client is available."""
    return FALKORDB_AVAILABLE
