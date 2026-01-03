"""Tests for embedding dimension enforcement.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 1.3:
- Validate at generation time (fail fast) - wrap embedder calls
- Validate at storage/query boundaries (guardrail) - FalkorDB vecf32 setters
- Raise on non-1024 vectors to prevent mixed dimensions
"""

import pytest
from typing import Callable

from watercooler_memory.infrastructure.embedding_validator import (
    EXPECTED_DIM,
    DimensionMismatchError,
    validate_embedding_dimension,
    enforce_dimension,
)


class TestEmbeddingDimensionValidator:
    """Test embedding dimension validation."""

    def test_expected_dimension_is_1024(self):
        """Verify expected dimension constant is 1024 per BGE-M3."""
        assert EXPECTED_DIM == 1024

    def test_validate_correct_dimension_passes(self):
        """Validate 1024-dim embedding passes without error."""
        embedding = [0.1] * 1024
        # Should not raise
        validate_embedding_dimension(embedding)

    def test_validate_wrong_dimension_raises(self):
        """Validate non-1024 dimension raises DimensionMismatchError."""
        embedding = [0.1] * 512  # Wrong dimension
        with pytest.raises(DimensionMismatchError) as exc_info:
            validate_embedding_dimension(embedding)
        assert "512" in str(exc_info.value)
        assert "1024" in str(exc_info.value)

    def test_validate_empty_embedding_raises(self):
        """Validate empty embedding raises DimensionMismatchError."""
        with pytest.raises(DimensionMismatchError):
            validate_embedding_dimension([])

    def test_validate_batch_all_correct(self):
        """Validate batch of correct embeddings passes."""
        embeddings = [[0.1] * 1024 for _ in range(5)]
        # Should not raise
        for emb in embeddings:
            validate_embedding_dimension(emb)

    def test_validate_batch_one_wrong_raises(self):
        """Validate batch with one wrong dimension fails."""
        embeddings = [
            [0.1] * 1024,
            [0.1] * 768,  # Wrong
            [0.1] * 1024,
        ]
        with pytest.raises(DimensionMismatchError):
            for emb in embeddings:
                validate_embedding_dimension(emb)


class TestEnforceDimensionDecorator:
    """Test the @enforce_dimension decorator for embedder functions."""

    def test_decorator_passes_correct_dimension(self):
        """Decorated function returns valid 1024-dim embedding."""

        @enforce_dimension
        def mock_embedder(text: str) -> list[float]:
            return [0.1] * 1024

        result = mock_embedder("test text")
        assert len(result) == 1024

    def test_decorator_raises_on_wrong_dimension(self):
        """Decorated function raises if returning wrong dimension."""

        @enforce_dimension
        def bad_embedder(text: str) -> list[float]:
            return [0.1] * 512  # Wrong dimension

        with pytest.raises(DimensionMismatchError):
            bad_embedder("test text")

    def test_decorator_works_with_batch_embedder(self):
        """Decorated batch embedder validates all returned embeddings."""

        @enforce_dimension
        def batch_embedder(texts: list[str]) -> list[list[float]]:
            return [[0.1] * 1024 for _ in texts]

        result = batch_embedder(["text1", "text2", "text3"])
        assert len(result) == 3
        assert all(len(emb) == 1024 for emb in result)

    def test_decorator_catches_batch_with_wrong_dimension(self):
        """Decorated batch embedder fails if any embedding is wrong."""

        @enforce_dimension
        def bad_batch_embedder(texts: list[str]) -> list[list[float]]:
            return [
                [0.1] * 1024,
                [0.1] * 768,  # Wrong
                [0.1] * 1024,
            ]

        with pytest.raises(DimensionMismatchError):
            bad_batch_embedder(["text1", "text2", "text3"])

    def test_decorator_preserves_function_metadata(self):
        """Decorated function preserves original function name and docstring."""

        @enforce_dimension
        def documented_embedder(text: str) -> list[float]:
            """Generate embedding for text."""
            return [0.1] * 1024

        assert documented_embedder.__name__ == "documented_embedder"
        assert "Generate embedding" in (documented_embedder.__doc__ or "")


class TestDimensionMismatchError:
    """Test the DimensionMismatchError exception."""

    def test_error_message_includes_dimensions(self):
        """Error message should include actual and expected dimensions."""
        error = DimensionMismatchError(actual=512, expected=1024)
        message = str(error)
        assert "512" in message
        assert "1024" in message

    def test_error_is_value_error_subclass(self):
        """DimensionMismatchError should be a ValueError subclass."""
        assert issubclass(DimensionMismatchError, ValueError)

    def test_error_has_attributes(self):
        """Error should expose actual and expected as attributes."""
        error = DimensionMismatchError(actual=512, expected=1024)
        assert error.actual == 512
        assert error.expected == 1024
