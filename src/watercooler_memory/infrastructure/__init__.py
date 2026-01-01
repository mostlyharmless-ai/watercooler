"""Infrastructure modules for memory system."""

from .embedding_validator import (
    EXPECTED_DIM,
    DimensionMismatchError,
    validate_embedding_dimension,
    validate_embeddings_batch,
    enforce_dimension,
)

from .falkordb_vectors import (
    FalkorDBVectorAdapter,
    FalkorDBVectorConfig,
    VectorSearchResult,
    build_storage_query,
    build_search_query,
    normalize_score,
    is_falkordb_available,
)

__all__ = [
    # Embedding validation
    "EXPECTED_DIM",
    "DimensionMismatchError",
    "validate_embedding_dimension",
    "validate_embeddings_batch",
    "enforce_dimension",
    # FalkorDB vectors
    "FalkorDBVectorAdapter",
    "FalkorDBVectorConfig",
    "VectorSearchResult",
    "build_storage_query",
    "build_search_query",
    "normalize_score",
    "is_falkordb_available",
]
