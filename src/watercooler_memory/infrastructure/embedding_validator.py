"""Embedding dimension enforcement for memory system.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 1.3:
- Validate at generation time (fail fast) - wrap embedder calls
- Validate at storage/query boundaries (guardrail) - FalkorDB vecf32 setters
- Raise on non-1024 vectors to prevent mixed dimensions

Enforcement Points:
1. Embedder wrapper decorator (generation time)
2. FalkorDB vector adapter (storage/query time)
3. Milvus adapter (storage/query time, if kept)
"""

from __future__ import annotations

import functools
from typing import Callable, TypeVar, Union, overload

# BGE-M3 produces 1024-dimensional embeddings
# This is the standard dimension for all memory tiers
EXPECTED_DIM = 1024


class DimensionMismatchError(ValueError):
    """Raised when embedding dimension doesn't match expected value.

    Attributes:
        actual: The actual dimension of the embedding.
        expected: The expected dimension (1024 for BGE-M3).
    """

    def __init__(self, actual: int, expected: int = EXPECTED_DIM):
        self.actual = actual
        self.expected = expected
        super().__init__(
            f"Embedding dimension mismatch: got {actual}, expected {expected}. "
            f"Ensure all embeddings use BGE-M3 (1024-d) for consistency across tiers."
        )


def validate_embedding_dimension(
    embedding: list[float],
    expected: int = EXPECTED_DIM,
) -> None:
    """Validate that an embedding has the expected dimension.

    Args:
        embedding: The embedding vector to validate.
        expected: Expected dimension (default: 1024 for BGE-M3).

    Raises:
        DimensionMismatchError: If dimension doesn't match expected.
    """
    actual = len(embedding)
    if actual != expected:
        raise DimensionMismatchError(actual=actual, expected=expected)


def validate_embeddings_batch(
    embeddings: list[list[float]],
    expected: int = EXPECTED_DIM,
) -> None:
    """Validate that all embeddings in a batch have the expected dimension.

    Args:
        embeddings: List of embedding vectors to validate.
        expected: Expected dimension (default: 1024 for BGE-M3).

    Raises:
        DimensionMismatchError: If any embedding's dimension doesn't match.
    """
    for i, embedding in enumerate(embeddings):
        actual = len(embedding)
        if actual != expected:
            raise DimensionMismatchError(actual=actual, expected=expected)


# Type variable for the decorated function
F = TypeVar("F", bound=Callable)


def enforce_dimension(func: F) -> F:
    """Decorator to enforce embedding dimension on embedder functions.

    Validates that the return value (single embedding or batch) has
    the correct dimension (1024 for BGE-M3).

    Works with:
    - Single embedder: (text: str) -> list[float]
    - Batch embedder: (texts: list[str]) -> list[list[float]]

    Example:
        @enforce_dimension
        def embed_text(text: str) -> list[float]:
            return model.encode(text)

        @enforce_dimension
        def embed_texts(texts: list[str]) -> list[list[float]]:
            return model.encode(texts)

    Args:
        func: The embedder function to wrap.

    Returns:
        Wrapped function that validates output dimensions.

    Raises:
        DimensionMismatchError: If any returned embedding has wrong dimension.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)

        # Handle batch result (list of embeddings)
        if result and isinstance(result, list):
            if isinstance(result[0], list):
                # Batch: list[list[float]]
                validate_embeddings_batch(result)
            else:
                # Single: list[float]
                validate_embedding_dimension(result)

        return result

    return wrapper  # type: ignore[return-value]
