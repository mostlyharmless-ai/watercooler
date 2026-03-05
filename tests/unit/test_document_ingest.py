"""Unit tests for document_ingest module."""

import pytest
from pathlib import Path

from watercooler_memory.document_ingest import (
    ingest_document,
    ingest_directory,
    DocumentNode,
    DocumentChunk,
)
from watercooler_memory.chunker import ChunkerConfig


# Sample whitepaper content
WHITEPAPER_CONTENT = """# RAVE: A variational autoencoder

**Authors**: Antoine Caillon, Philippe Esling
**Year**: 2021
**Source**: arXiv:2111.05011

---

## Abstract

Deep generative models have improved audio synthesis. This paper introduces RAVE for fast and high-quality neural audio waveform synthesis.

## 1. Introduction

Deep learning proposes exciting new ways for audio generation. Previous approaches rely on low sampling rates.

### 1.1 Background

Variational autoencoders provide latent control.

### 1.2 Contributions

We introduce a two-stage training procedure.

## 2. Methods

### 2.1 VAE Loss

The loss function is:

$$\\mathcal{L}_{vae} = \\mathbb{E}[S(x, \\hat{x})] + \\beta D_{KL}$$

### 2.2 Architecture

The encoder uses convolutional layers.

## 3. Results

| Model | MOS |
|-------|-----|
| RAVE | 3.01 |

## References

1. Kingma, D. P. (2014). Auto-encoding variational bayes.
"""

GENERIC_MD_CONTENT = """# Simple Doc

This is a simple markdown document without academic structure.

Some content here.
"""


class TestIngestDocument:
    """Tests for ingest_document function."""

    def test_ingest_single_document(self, tmp_path: Path):
        """Single document should be ingested with chunks."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        doc, chunks = ingest_document(doc_path)

        assert isinstance(doc, DocumentNode)
        assert doc.title == "RAVE: A variational autoencoder"
        assert doc.doc_type == "whitepaper"
        assert len(chunks) > 0
        assert len(doc.chunk_ids) == len(chunks)

    def test_document_metadata_extracted(self, tmp_path: Path):
        """Document metadata should be extracted from front matter."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        doc, _ = ingest_document(doc_path)

        assert doc.metadata.get("authors") == "Antoine Caillon, Philippe Esling"
        assert doc.metadata.get("year") == "2021"
        assert "arXiv" in doc.metadata.get("source", "")

    def test_document_node_creation(self, tmp_path: Path):
        """DocumentNode should have all required fields."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        doc, _ = ingest_document(doc_path)

        assert doc.doc_id is not None
        assert doc.file_path == str(doc_path)
        assert doc.title is not None
        assert doc.doc_type in ("whitepaper", "reference", "generic")
        assert isinstance(doc.chunk_ids, list)
        assert doc.ingestion_time is not None

    def test_chunk_ids_linked(self, tmp_path: Path):
        """Document chunk_ids should match created chunks."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        doc, chunks = ingest_document(doc_path)

        chunk_ids_from_doc = set(doc.chunk_ids)
        chunk_ids_from_chunks = {c.chunk_id for c in chunks}

        assert chunk_ids_from_doc == chunk_ids_from_chunks

    def test_chunks_have_doc_id(self, tmp_path: Path):
        """All chunks should reference parent document."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        doc, chunks = ingest_document(doc_path)

        for chunk in chunks:
            assert chunk.doc_id == doc.doc_id

    def test_chunks_have_indices(self, tmp_path: Path):
        """Chunks should have sequential indices."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        _, chunks = ingest_document(doc_path)

        indices = [c.index for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_generic_document_type(self, tmp_path: Path):
        """Non-whitepaper document should be typed as generic or reference."""
        doc_path = tmp_path / "readme.md"
        doc_path.write_text(GENERIC_MD_CONTENT)

        doc, _ = ingest_document(doc_path)

        # Should not be whitepaper
        assert doc.doc_type in ("generic", "reference")

    def test_file_not_found(self, tmp_path: Path):
        """Missing file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            ingest_document(tmp_path / "nonexistent.md")

    def test_non_markdown_rejected(self, tmp_path: Path):
        """Non-markdown files should be rejected."""
        txt_path = tmp_path / "file.txt"
        txt_path.write_text("Some text")

        with pytest.raises(ValueError, match="Expected markdown"):
            ingest_document(txt_path)

    def test_custom_chunker_config(self, tmp_path: Path):
        """Custom chunker config should be used."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        # Use small max_tokens to force more chunks
        config = ChunkerConfig(
            max_tokens=100,
            overlap=10,
            mode="whitepaper",
        )
        _, chunks = ingest_document(doc_path, config)

        # With smaller chunks, should have more of them
        assert len(chunks) > 1


class TestIngestDirectory:
    """Tests for ingest_directory function."""

    def test_ingest_directory(self, tmp_path: Path):
        """Directory with multiple docs should be ingested."""
        (tmp_path / "paper1.md").write_text(WHITEPAPER_CONTENT)
        (tmp_path / "paper2.md").write_text(GENERIC_MD_CONTENT)

        docs, chunks = ingest_directory(tmp_path)

        assert len(docs) == 2
        assert len(chunks) > 0

    def test_ingest_directory_pattern(self, tmp_path: Path):
        """Pattern should filter files."""
        (tmp_path / "ref-paper.md").write_text(WHITEPAPER_CONTENT)
        (tmp_path / "readme.md").write_text(GENERIC_MD_CONTENT)

        docs, _ = ingest_directory(tmp_path, pattern="ref-*.md")

        assert len(docs) == 1
        assert "ref-paper" in docs[0].file_path

    def test_ingest_empty_directory(self, tmp_path: Path):
        """Empty directory should return empty lists."""
        docs, chunks = ingest_directory(tmp_path)

        assert docs == []
        assert chunks == []

    def test_ingest_directory_not_found(self, tmp_path: Path):
        """Missing directory should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            ingest_directory(tmp_path / "nonexistent")

    def test_ingest_directory_is_file(self, tmp_path: Path):
        """File path should raise ValueError."""
        file_path = tmp_path / "file.md"
        file_path.write_text("content")

        with pytest.raises(ValueError, match="Expected directory"):
            ingest_directory(file_path)

    def test_skips_non_markdown(self, tmp_path: Path):
        """Non-markdown files should be skipped."""
        (tmp_path / "paper.md").write_text(WHITEPAPER_CONTENT)
        (tmp_path / "data.json").write_text('{"key": "value"}')
        (tmp_path / "script.py").write_text('print("hello")')

        docs, _ = ingest_directory(tmp_path)

        assert len(docs) == 1
        assert docs[0].file_path.endswith(".md")

    def test_chunks_grouped_by_document(self, tmp_path: Path):
        """Each chunk should reference its parent document."""
        (tmp_path / "paper1.md").write_text(WHITEPAPER_CONTENT)
        (tmp_path / "paper2.md").write_text(GENERIC_MD_CONTENT)

        docs, chunks = ingest_directory(tmp_path)

        doc_ids = {d.doc_id for d in docs}
        chunk_doc_ids = {c.doc_id for c in chunks}

        assert chunk_doc_ids.issubset(doc_ids)


class TestDocumentChunk:
    """Tests for DocumentChunk dataclass."""

    def test_chunk_node_id(self, tmp_path: Path):
        """Chunk should have proper node_id format."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        _, chunks = ingest_document(doc_path)

        for chunk in chunks:
            assert chunk.node_id.startswith("doc_chunk:")
            assert chunk.chunk_id in chunk.node_id

    def test_chunk_has_text(self, tmp_path: Path):
        """Chunks should have non-empty text."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        _, chunks = ingest_document(doc_path)

        for chunk in chunks:
            assert chunk.text
            assert len(chunk.text) > 0

    def test_chunk_token_count(self, tmp_path: Path):
        """Chunks should have positive token counts."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        _, chunks = ingest_document(doc_path)

        for chunk in chunks:
            assert chunk.token_count > 0

    def test_section_path_extracted(self, tmp_path: Path):
        """Chunks with section context should have section_path."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        config = ChunkerConfig.whitepaper_preset()
        _, chunks = ingest_document(doc_path, config)

        # At least some chunks should have section paths
        chunks_with_paths = [c for c in chunks if c.section_path]
        # With whitepaper preset, section context should be included
        assert len(chunks_with_paths) > 0


class TestDocumentNodeId:
    """Tests for DocumentNode node_id property."""

    def test_document_node_id(self, tmp_path: Path):
        """Document should have proper node_id format."""
        doc_path = tmp_path / "paper.md"
        doc_path.write_text(WHITEPAPER_CONTENT)

        doc, _ = ingest_document(doc_path)

        assert doc.node_id.startswith("document:")
        assert doc.doc_id in doc.node_id
