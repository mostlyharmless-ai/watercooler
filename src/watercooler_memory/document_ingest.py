"""Standalone document ingestion for reference materials.

This module provides utilities for ingesting external markdown documents
(like white papers, reference docs) into the memory graph as standalone
documents rather than thread entries.

Documents are chunked using semantic-aware chunking and linked to
DocumentNode entities for retrieval.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._utils import _generate_chunk_id, _utc_now_iso
from .chunker import ChunkerConfig, chunk_text
from .whitepaper_parser import parse_whitepaper_structure


def _generate_doc_id(file_path: str, content: str) -> str:
    """Generate a stable document ID based on path and content hash."""
    data = f"{file_path}:{content}"
    return hashlib.sha256(data.encode()).hexdigest()[:16]


@dataclass
class DocumentChunk:
    """A chunk from a standalone document.

    Attributes:
        chunk_id: Hash-based identifier.
        doc_id: Parent document ID.
        index: Position within document.
        text: Chunk text content.
        token_count: Number of tokens in chunk.
        section_path: Section breadcrumb if available.
        embedding: Vector embedding (None until computed).
        ingestion_time: When this chunk was created.
    """

    chunk_id: str
    doc_id: str
    index: int
    text: str
    token_count: int
    section_path: Optional[str] = None
    embedding: Optional[list[float]] = None
    ingestion_time: str = field(default_factory=_utc_now_iso)

    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the graph."""
        return f"doc_chunk:{self.chunk_id}"


@dataclass
class DocumentNode:
    """Represents an ingested reference document.

    Attributes:
        doc_id: Hash of file path + content.
        file_path: Source file path.
        title: Extracted title.
        doc_type: Document type ("whitepaper", "reference", "generic").
        metadata: Authors, year, source, etc.
        chunk_ids: Child chunk IDs.
        summary: Generated summary (None until computed).
        embedding: Vector embedding (None until computed).
        ingestion_time: When this document was ingested.
    """

    doc_id: str
    file_path: str
    title: str
    doc_type: str  # "whitepaper", "reference", "generic"
    metadata: dict = field(default_factory=dict)
    chunk_ids: list[str] = field(default_factory=list)
    summary: Optional[str] = None
    embedding: Optional[list[float]] = None
    ingestion_time: str = field(default_factory=_utc_now_iso)

    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the graph."""
        return f"document:{self.doc_id}"


def ingest_document(
    file_path: Path,
    config: Optional[ChunkerConfig] = None,
) -> tuple[DocumentNode, list[DocumentChunk]]:
    """Ingest a standalone document into memory graph format.

    Parses the document structure, chunks it with semantic awareness,
    and creates DocumentNode and DocumentChunk objects.

    Args:
        file_path: Path to the markdown file.
        config: Chunking configuration. Defaults to whitepaper_preset.

    Returns:
        Tuple of (DocumentNode, list of DocumentChunks).

    Raises:
        FileNotFoundError: If file doesn't exist.
        ValueError: If file is not a markdown file.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Document not found: {file_path}")

    if file_path.suffix.lower() not in (".md", ".markdown"):
        raise ValueError(f"Expected markdown file, got: {file_path.suffix}")

    if config is None:
        config = ChunkerConfig.whitepaper_preset()

    # Read document content
    content = file_path.read_text(encoding="utf-8")

    # Generate document ID
    doc_id = _generate_doc_id(str(file_path), content)

    # Parse document structure
    structure = parse_whitepaper_structure(content)

    # Determine document type
    if structure.is_whitepaper:
        doc_type = "whitepaper"
    elif structure.sections:
        doc_type = "reference"
    else:
        doc_type = "generic"

    # Extract metadata
    metadata: dict = {}
    if structure.metadata.authors:
        metadata["authors"] = structure.metadata.authors
    if structure.metadata.year:
        metadata["year"] = structure.metadata.year
    if structure.metadata.source:
        metadata["source"] = structure.metadata.source

    # Chunk the document
    text_chunks = chunk_text(content, config)

    # Create chunk objects
    chunks: list[DocumentChunk] = []
    chunk_ids: list[str] = []

    for i, (chunk_text_content, token_count) in enumerate(text_chunks):
        chunk_id = _generate_chunk_id(chunk_text_content, doc_id, i)
        chunk_ids.append(chunk_id)

        # Extract section path from chunk if it has a prefix
        section_path = None
        if chunk_text_content.startswith("[") and "]\n" in chunk_text_content:
            bracket_end = chunk_text_content.index("]")
            section_path = chunk_text_content[1:bracket_end]

        chunk = DocumentChunk(
            chunk_id=chunk_id,
            doc_id=doc_id,
            index=i,
            text=chunk_text_content,
            token_count=token_count,
            section_path=section_path,
        )
        chunks.append(chunk)

    # Create document node
    doc = DocumentNode(
        doc_id=doc_id,
        file_path=str(file_path),
        title=structure.metadata.title or file_path.stem,
        doc_type=doc_type,
        metadata=metadata,
        chunk_ids=chunk_ids,
    )

    return doc, chunks


def ingest_directory(
    dir_path: Path,
    pattern: str = "*.md",
    config: Optional[ChunkerConfig] = None,
) -> tuple[list[DocumentNode], list[DocumentChunk]]:
    """Ingest all matching documents from a directory.

    Args:
        dir_path: Path to directory containing documents.
        pattern: Glob pattern for matching files (default: "*.md").
        config: Chunking configuration. Defaults to whitepaper_preset.

    Returns:
        Tuple of (list of DocumentNodes, list of all DocumentChunks).

    Raises:
        FileNotFoundError: If directory doesn't exist.
    """
    if not dir_path.exists():
        raise FileNotFoundError(f"Directory not found: {dir_path}")

    if not dir_path.is_dir():
        raise ValueError(f"Expected directory, got file: {dir_path}")

    if config is None:
        config = ChunkerConfig.whitepaper_preset()

    all_docs: list[DocumentNode] = []
    all_chunks: list[DocumentChunk] = []

    # Find matching files
    files = sorted(dir_path.glob(pattern))

    for file_path in files:
        if not file_path.is_file():
            continue

        try:
            doc, chunks = ingest_document(file_path, config)
            all_docs.append(doc)
            all_chunks.extend(chunks)
        except (ValueError, UnicodeDecodeError):
            # Skip non-markdown or unreadable files
            continue

    return all_docs, all_chunks
