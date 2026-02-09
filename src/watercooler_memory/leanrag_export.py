"""Export memory graph to LeanRAG format.

Projects the superset schema to LeanRAG's expected document format,
preserving temporal metadata for future analysis.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .graph import MemoryGraph
from .schema import EntryNode, ChunkNode
from .validation import validate_export, validate_pipeline_chunks


def entry_to_leanrag_document(
    entry: EntryNode,
    chunks: list[ChunkNode],
) -> dict[str, Any]:
    """Convert an entry to LeanRAG document format.

    Args:
        entry: The entry node.
        chunks: List of chunks for this entry.

    Returns:
        LeanRAG-compatible document dict.
    """
    return {
        "doc_id": entry.entry_id,
        "title": entry.title or f"Entry {entry.index}",
        "content": entry.body,
        "summary": entry.summary,
        "chunks": [
            {
                "hash_code": chunk.chunk_id,
                "text": chunk.text,
                "token_count": chunk.token_count,
                "embedding": chunk.embedding,
            }
            for chunk in chunks
        ],
        "metadata": {
            "thread_id": entry.thread_id,
            "entry_index": entry.index,
            "agent": entry.agent,
            "role": entry.role,
            "entry_type": entry.entry_type,
            "timestamp": entry.timestamp,
            "event_time": entry.event_time,
            "ingestion_time": entry.ingestion_time,
            "sequence_index": entry.sequence_index,
            "preceding_entry_id": entry.preceding_entry_id,
            "following_entry_id": entry.following_entry_id,
        },
        "embedding": entry.embedding,
    }


def export_to_leanrag(
    graph: MemoryGraph,
    output_dir: Path,
    include_embeddings: bool = True,
    validate: bool = True,
) -> dict[str, Any]:
    """Export graph to LeanRAG format.

    Creates:
    - documents.json: All entries as documents with chunks
    - threads.json: Thread metadata
    - manifest.json: Export metadata

    Args:
        graph: The memory graph to export.
        output_dir: Directory to write export files.
        include_embeddings: Whether to include embedding vectors.
        validate: Whether to validate export against schema (default True).

    Returns:
        Export manifest with statistics.

    Raises:
        ValidationError: If validate=True and export fails validation.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Export documents (entries with chunks)
    documents: list[dict] = []

    for entry_id, entry in graph.entries.items():
        # Get chunks for this entry
        entry_chunks = [
            graph.chunks[cid] for cid in entry.chunk_ids if cid in graph.chunks
        ]

        doc = entry_to_leanrag_document(entry, entry_chunks)

        # Optionally strip embeddings
        if not include_embeddings:
            doc["embedding"] = None
            for chunk in doc["chunks"]:
                chunk["embedding"] = None

        documents.append(doc)

    # Sort documents by thread and sequence
    documents.sort(
        key=lambda d: (
            d["metadata"]["thread_id"],
            d["metadata"]["sequence_index"],
        )
    )

    # Export threads
    threads: list[dict] = []

    for thread_id, thread in graph.threads.items():
        thread_doc = {
            "thread_id": thread.thread_id,
            "title": thread.title,
            "status": thread.status,
            "ball": thread.ball,
            "created_at": thread.created_at,
            "updated_at": thread.updated_at,
            "summary": thread.summary,
            "entry_count": len(thread.entry_ids),
            "entry_ids": thread.entry_ids,
            "branch_context": thread.branch_context,
            "event_time": thread.event_time,
            "ingestion_time": thread.ingestion_time,
        }

        if include_embeddings:
            thread_doc["embedding"] = thread.embedding

        threads.append(thread_doc)

    # Sort threads by creation time
    threads.sort(key=lambda t: t.get("created_at") or "")

    # Create manifest
    manifest = {
        "format": "leanrag",
        "version": "1.0",
        "source": "watercooler-cloud",
        "statistics": {
            "threads": len(threads),
            "documents": len(documents),
            "chunks": sum(len(d["chunks"]) for d in documents),
            "embeddings_included": include_embeddings,
        },
        "files": {
            "documents": "documents.json",
            "threads": "threads.json",
        },
    }

    # Validate export before writing
    if validate:
        validate_export(documents, threads, manifest)

    # Write files
    (output_dir / "documents.json").write_text(
        json.dumps(documents, indent=2, default=str)
    )
    (output_dir / "threads.json").write_text(
        json.dumps(threads, indent=2, default=str)
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )

    # Also export chunks.json for LeanRAG pipeline compatibility
    # LeanRAG's get_chunk() expects list of {hash_code, text} objects
    chunks_for_leanrag = []
    for doc in documents:
        for chunk in doc["chunks"]:
            chunks_for_leanrag.append({
                "hash_code": chunk["hash_code"],
                "text": chunk["text"],
            })
    (output_dir / "chunks.json").write_text(
        json.dumps(chunks_for_leanrag, indent=2, default=str)
    )
    manifest["files"]["chunks"] = "chunks.json"

    return manifest


def export_for_leanrag_pipeline(
    graph: MemoryGraph,
    output_path: Path,
    validate: bool = True,
) -> None:
    """Export in format directly consumable by LeanRAG build_graph pipeline.

    Creates a single JSON file with all chunks ready for entity extraction
    and graph building.

    Args:
        graph: The memory graph to export.
        output_path: Path to output JSON file.
        validate: Whether to validate chunks against schema (default True).

    Raises:
        ValidationError: If validate=True and chunks fail validation.
    """
    chunks_for_pipeline: list[dict] = []

    for chunk_id, chunk in graph.chunks.items():
        # Get parent entry for metadata
        entry = graph.entries.get(chunk.entry_id)

        chunk_doc = {
            "hash_code": chunk.chunk_id,
            "text": chunk.text,
            "embedding": chunk.embedding,
            "metadata": {
                "source": "watercooler",
                "thread_id": chunk.thread_id,
                "entry_id": chunk.entry_id,
                "chunk_index": chunk.index,
                "event_time": chunk.event_time,
            },
        }

        if entry:
            chunk_doc["metadata"].update(
                {
                    "agent": entry.agent,
                    "role": entry.role,
                    "entry_type": entry.entry_type,
                    "entry_title": entry.title,
                }
            )

        chunks_for_pipeline.append(chunk_doc)

    # Sort by thread and position
    chunks_for_pipeline.sort(
        key=lambda c: (
            c["metadata"]["thread_id"],
            c["metadata"]["entry_id"],
            c["metadata"]["chunk_index"],
        )
    )

    # Validate chunks before writing
    if validate:
        validate_pipeline_chunks(chunks_for_pipeline)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(chunks_for_pipeline, indent=2, default=str))
