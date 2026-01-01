"""Infrastructure modules for memory system."""

from .embedding_validator import (
    EXPECTED_DIM,
    DimensionMismatchError,
    validate_embedding_dimension,
    enforce_dimension,
)

__all__ = [
    "EXPECTED_DIM",
    "DimensionMismatchError",
    "validate_embedding_dimension",
    "enforce_dimension",
]
