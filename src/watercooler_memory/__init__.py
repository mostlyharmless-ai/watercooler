"""Memory graph module for watercooler threads.

This module provides tools for building a memory graph from watercooler threads,
enabling structural analysis and export to LeanRAG for semantic search and
entity extraction.

The graph uses a hierarchical structure:
  Thread -> Entry -> Chunk

With temporal FOLLOWS edges for sequencing.

Usage:
    from watercooler_memory import MemoryGraph, GraphConfig

    graph = MemoryGraph()
    graph.build(threads_dir)
    graph.save(output_path)

    # Export to LeanRAG for entity extraction and embeddings
    from watercooler_memory import export_to_leanrag
    export_to_leanrag(graph, output_dir)

CLI:
    watercooler memory build --threads-dir .watercooler
    watercooler memory export --format leanrag -o ./export

Requirements:
    This module requires additional dependencies. Install with:
        pip install 'watercooler-cloud[memory]'
    Or:
        uvx watercooler-cloud[memory]

LLM Features:
    For embeddings, entity extraction, and summarization, export to
    LeanRAG format and run the LeanRAG pipeline. See docs/MEMORY.md.
"""

# Check for required dependencies
_MISSING_DEPS: list[str] = []

try:
    import tiktoken  # noqa: F401
except ImportError:
    _MISSING_DEPS.append("tiktoken")

# Flag for feature availability
MEMORY_AVAILABLE = len(_MISSING_DEPS) == 0


def _raise_missing_deps() -> None:
    """Raise ImportError with helpful message about missing dependencies."""
    deps = ", ".join(_MISSING_DEPS)
    raise ImportError(
        f"Memory features require additional dependencies: {deps}\n\n"
        "Install with:\n"
        "  pip install 'watercooler-cloud[memory]'\n"
        "Or:\n"
        "  uvx watercooler-cloud[memory]"
    )


# Validation module has no external dependencies - always available
from watercooler_memory.validation import (
    ValidationError,
    validate_chunk,
    validate_document,
    validate_manifest,
    validate_export,
    validate_pipeline_chunks,
    LEANRAG_CHUNK_SCHEMA,
    LEANRAG_DOCUMENT_SCHEMA,
    LEANRAG_MANIFEST_SCHEMA,
    LEANRAG_PIPELINE_CHUNK_SCHEMA,
)

if MEMORY_AVAILABLE:
    # Full imports when dependencies are available
    from watercooler_memory.schema import (
        ThreadNode,
        EntryNode,
        ChunkNode,
        DocumentNode,
        DocumentChunkNode,
        Edge,
        EdgeType,
    )
    from watercooler_memory.graph import MemoryGraph, GraphConfig
    from watercooler_memory.parser import parse_thread_to_nodes, parse_threads_directory
    from watercooler_memory.chunker import (
        chunk_text,
        chunk_entry,
        chunk_whitepaper,
        ChunkerConfig,
    )
    from watercooler_memory.leanrag_export import export_to_leanrag
    from watercooler_memory.whitepaper_parser import (
        detect_whitepaper,
        parse_whitepaper_structure,
        get_section_breadcrumb,
        DocumentMetadata,
        Section,
        AtomicBlock,
        DocumentStructure,
    )
    from watercooler_memory.document_ingest import (
        ingest_document,
        ingest_directory,
        DocumentChunk,
    )
else:
    # Stub classes that raise helpful errors when instantiated
    class _StubClass:
        """Stub class that raises ImportError on instantiation."""

        def __init__(self, *args, **kwargs):
            _raise_missing_deps()

    # Schema stubs
    ThreadNode = _StubClass  # type: ignore
    EntryNode = _StubClass  # type: ignore
    ChunkNode = _StubClass  # type: ignore
    DocumentNode = _StubClass  # type: ignore
    DocumentChunkNode = _StubClass  # type: ignore
    Edge = _StubClass  # type: ignore
    EdgeType = _StubClass  # type: ignore

    # Graph stubs
    MemoryGraph = _StubClass  # type: ignore
    GraphConfig = _StubClass  # type: ignore

    # Parser stubs
    def parse_thread_to_nodes(*args, **kwargs):
        _raise_missing_deps()

    def parse_threads_directory(*args, **kwargs):
        _raise_missing_deps()

    # Chunker stubs
    def chunk_text(*args, **kwargs):
        _raise_missing_deps()

    def chunk_entry(*args, **kwargs):
        _raise_missing_deps()

    def chunk_whitepaper(*args, **kwargs):
        _raise_missing_deps()

    ChunkerConfig = _StubClass  # type: ignore

    # Export stubs
    def export_to_leanrag(*args, **kwargs):
        _raise_missing_deps()

    # Whitepaper parser stubs
    def detect_whitepaper(*args, **kwargs):
        _raise_missing_deps()

    def parse_whitepaper_structure(*args, **kwargs):
        _raise_missing_deps()

    def get_section_breadcrumb(*args, **kwargs):
        _raise_missing_deps()

    DocumentMetadata = _StubClass  # type: ignore
    Section = _StubClass  # type: ignore
    AtomicBlock = _StubClass  # type: ignore
    DocumentStructure = _StubClass  # type: ignore

    # Document ingest stubs
    def ingest_document(*args, **kwargs):
        _raise_missing_deps()

    def ingest_directory(*args, **kwargs):
        _raise_missing_deps()

    DocumentChunk = _StubClass  # type: ignore


__all__ = [
    # Availability flag
    "MEMORY_AVAILABLE",
    # Schema
    "ThreadNode",
    "EntryNode",
    "ChunkNode",
    "DocumentNode",
    "DocumentChunkNode",
    "Edge",
    "EdgeType",
    # Graph
    "MemoryGraph",
    "GraphConfig",
    # Parser
    "parse_thread_to_nodes",
    "parse_threads_directory",
    # Chunker
    "chunk_text",
    "chunk_entry",
    "chunk_whitepaper",
    "ChunkerConfig",
    # Export
    "export_to_leanrag",
    # Whitepaper parser
    "detect_whitepaper",
    "parse_whitepaper_structure",
    "get_section_breadcrumb",
    "DocumentMetadata",
    "Section",
    "AtomicBlock",
    "DocumentStructure",
    # Document ingest
    "ingest_document",
    "ingest_directory",
    "DocumentChunk",
    # Validation (always available - no external deps)
    "ValidationError",
    "validate_chunk",
    "validate_document",
    "validate_manifest",
    "validate_export",
    "validate_pipeline_chunks",
    "LEANRAG_CHUNK_SCHEMA",
    "LEANRAG_DOCUMENT_SCHEMA",
    "LEANRAG_MANIFEST_SCHEMA",
    "LEANRAG_PIPELINE_CHUNK_SCHEMA",
]
