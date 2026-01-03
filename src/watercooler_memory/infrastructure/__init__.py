"""Infrastructure modules for memory system."""

from .embedding_validator import (
    EXPECTED_DIM,
    DimensionMismatchError,
    validate_embedding_dimension,
    validate_embeddings_batch,
    enforce_dimension,
)

__all__ = [
    # Embedding validation
    "EXPECTED_DIM",
    "DimensionMismatchError",
    "validate_embedding_dimension",
    "validate_embeddings_batch",
    "enforce_dimension",
]
