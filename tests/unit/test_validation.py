"""Tests for watercooler_memory validation module."""

import pytest

from watercooler_memory.validation import (
    ValidationError,
    validate_chunk,
    validate_document,
    validate_manifest,
    validate_export,
    validate_pipeline_chunks,
)


class TestValidateChunk:
    """Test chunk validation."""

    def test_valid_chunk(self):
        """Test that a valid chunk passes validation."""
        chunk = {"hash_code": "abc123", "text": "Hello world"}
        errors = validate_chunk(chunk)
        assert errors == []

    def test_chunk_with_embedding(self):
        """Test that a chunk with embedding passes validation."""
        chunk = {
            "hash_code": "abc123",
            "text": "Hello world",
            "embedding": [0.1, 0.2, 0.3],
        }
        errors = validate_chunk(chunk)
        assert errors == []

    def test_chunk_with_null_embedding(self):
        """Test that a chunk with null embedding passes validation."""
        chunk = {"hash_code": "abc123", "text": "Hello world", "embedding": None}
        errors = validate_chunk(chunk)
        assert errors == []

    def test_missing_hash_code(self):
        """Test that missing hash_code is detected."""
        chunk = {"text": "Hello world"}
        errors = validate_chunk(chunk)
        assert len(errors) == 1
        assert "hash_code" in errors[0]

    def test_missing_text(self):
        """Test that missing text is detected."""
        chunk = {"hash_code": "abc123"}
        errors = validate_chunk(chunk)
        assert len(errors) == 1
        assert "text" in errors[0]

    def test_empty_hash_code(self):
        """Test that empty hash_code is detected."""
        chunk = {"hash_code": "", "text": "Hello world"}
        errors = validate_chunk(chunk)
        assert len(errors) == 1
        assert "non-empty" in errors[0]

    def test_invalid_embedding_type(self):
        """Test that invalid embedding type is detected."""
        chunk = {"hash_code": "abc123", "text": "Hello", "embedding": "not a list"}
        errors = validate_chunk(chunk)
        assert len(errors) == 1
        assert "list" in errors[0]

    def test_invalid_embedding_contents(self):
        """Test that non-numeric embedding values are detected."""
        chunk = {
            "hash_code": "abc123",
            "text": "Hello",
            "embedding": [0.1, "not a number", 0.3],
        }
        errors = validate_chunk(chunk)
        assert len(errors) == 1
        assert "numbers" in errors[0]


class TestValidateDocument:
    """Test document validation."""

    def test_valid_document(self):
        """Test that a valid document passes validation."""
        doc = {
            "doc_id": "entry-001",
            "content": "Document content",
            "chunks": [{"hash_code": "abc123", "text": "Chunk text"}],
        }
        errors = validate_document(doc)
        assert errors == []

    def test_missing_doc_id(self):
        """Test that missing doc_id is detected."""
        doc = {
            "content": "Document content",
            "chunks": [{"hash_code": "abc123", "text": "Chunk text"}],
        }
        errors = validate_document(doc)
        assert any("doc_id" in e for e in errors)

    def test_missing_content(self):
        """Test that missing content is detected."""
        doc = {
            "doc_id": "entry-001",
            "chunks": [{"hash_code": "abc123", "text": "Chunk text"}],
        }
        errors = validate_document(doc)
        assert any("content" in e for e in errors)

    def test_missing_chunks(self):
        """Test that missing chunks is detected."""
        doc = {"doc_id": "entry-001", "content": "Document content"}
        errors = validate_document(doc)
        assert any("chunks" in e for e in errors)

    def test_invalid_chunk_in_document(self):
        """Test that invalid chunk in document is detected."""
        doc = {
            "doc_id": "entry-001",
            "content": "Document content",
            "chunks": [{"hash_code": "abc123"}],  # Missing text
        }
        errors = validate_document(doc)
        assert any("chunks[0]" in e and "text" in e for e in errors)


class TestValidateManifest:
    """Test manifest validation."""

    def test_valid_manifest(self):
        """Test that a valid manifest passes validation."""
        manifest = {
            "format": "leanrag",
            "version": "1.0",
            "statistics": {"threads": 1, "documents": 5, "chunks": 10},
            "files": {"documents": "documents.json", "threads": "threads.json"},
        }
        errors = validate_manifest(manifest)
        assert errors == []

    def test_wrong_format(self):
        """Test that wrong format is detected."""
        manifest = {
            "format": "wrong",
            "version": "1.0",
            "statistics": {"threads": 1, "documents": 5, "chunks": 10},
            "files": {"documents": "documents.json", "threads": "threads.json"},
        }
        errors = validate_manifest(manifest)
        assert any("leanrag" in e for e in errors)

    def test_missing_statistics(self):
        """Test that missing statistics is detected."""
        manifest = {
            "format": "leanrag",
            "version": "1.0",
            "files": {"documents": "documents.json", "threads": "threads.json"},
        }
        errors = validate_manifest(manifest)
        assert any("statistics" in e for e in errors)

    def test_negative_counts(self):
        """Test that negative counts are detected."""
        manifest = {
            "format": "leanrag",
            "version": "1.0",
            "statistics": {"threads": -1, "documents": 5, "chunks": 10},
            "files": {"documents": "documents.json", "threads": "threads.json"},
        }
        errors = validate_manifest(manifest)
        assert any("non-negative" in e for e in errors)


class TestValidateExport:
    """Test full export validation."""

    def test_valid_export(self):
        """Test that a valid export passes validation."""
        documents = [
            {
                "doc_id": "entry-001",
                "content": "Content",
                "chunks": [{"hash_code": "abc", "text": "Chunk"}],
            }
        ]
        threads = [{"thread_id": "test-thread"}]
        manifest = {
            "format": "leanrag",
            "version": "1.0",
            "statistics": {"threads": 1, "documents": 1, "chunks": 1},
            "files": {"documents": "documents.json", "threads": "threads.json"},
        }

        # Should not raise
        validate_export(documents, threads, manifest)

    def test_mismatched_document_count(self):
        """Test that mismatched document count is detected."""
        documents = [
            {
                "doc_id": "entry-001",
                "content": "Content",
                "chunks": [{"hash_code": "abc", "text": "Chunk"}],
            }
        ]
        threads = [{"thread_id": "test-thread"}]
        manifest = {
            "format": "leanrag",
            "version": "1.0",
            "statistics": {"threads": 1, "documents": 5, "chunks": 1},  # Wrong count
            "files": {"documents": "documents.json", "threads": "threads.json"},
        }

        with pytest.raises(ValidationError) as exc_info:
            validate_export(documents, threads, manifest)

        assert "consistency" in str(exc_info.value.errors)

    def test_invalid_document_in_export(self):
        """Test that invalid document in export raises error."""
        documents = [
            {
                "doc_id": "entry-001",
                # Missing content
                "chunks": [{"hash_code": "abc", "text": "Chunk"}],
            }
        ]
        threads = [{"thread_id": "test-thread"}]
        manifest = {
            "format": "leanrag",
            "version": "1.0",
            "statistics": {"threads": 1, "documents": 1, "chunks": 1},
            "files": {"documents": "documents.json", "threads": "threads.json"},
        }

        with pytest.raises(ValidationError) as exc_info:
            validate_export(documents, threads, manifest)

        assert any(e["type"] == "document" for e in exc_info.value.errors)


class TestValidatePipelineChunks:
    """Test pipeline chunk validation."""

    def test_valid_pipeline_chunks(self):
        """Test that valid pipeline chunks pass validation."""
        chunks = [
            {"hash_code": "abc123", "text": "First chunk"},
            {"hash_code": "def456", "text": "Second chunk"},
        ]

        # Should not raise
        validate_pipeline_chunks(chunks)

    def test_invalid_pipeline_chunk(self):
        """Test that invalid pipeline chunk raises error."""
        chunks = [
            {"hash_code": "abc123", "text": "Valid chunk"},
            {"hash_code": "", "text": "Invalid - empty hash_code"},
        ]

        with pytest.raises(ValidationError) as exc_info:
            validate_pipeline_chunks(chunks)

        assert len(exc_info.value.errors) == 1
        assert exc_info.value.errors[0]["index"] == 1


class TestValidationError:
    """Test ValidationError class."""

    def test_error_message(self):
        """Test that ValidationError has correct message."""
        error = ValidationError("Test error")
        assert str(error) == "Test error"

    def test_error_with_details(self):
        """Test that ValidationError stores error details."""
        details = [{"type": "test", "errors": ["Error 1", "Error 2"]}]
        error = ValidationError("Test error", errors=details)
        assert error.errors == details
