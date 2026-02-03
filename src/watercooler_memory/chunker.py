"""Text chunking for memory graph entries.

Splits entry bodies into chunks suitable for embedding. Uses tiktoken
for token counting when available, falls back to word-based estimation.

Chunking strategy:
- Max tokens: 768 (configurable) - balances comprehensiveness vs "lost in the middle"
- Overlap: 64 tokens (configurable)
- Preserves semantic boundaries where possible (paragraphs, sentences)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from .schema import ChunkNode, EntryNode

# Try to import tiktoken, fall back to estimation
try:
    import tiktoken

    TIKTOKEN_AVAILABLE = True
except ImportError:
    tiktoken = None  # type: ignore
    TIKTOKEN_AVAILABLE = False

# Default chunking parameters (watercooler preset)
# 768/64 balances comprehensiveness vs "lost in the middle" issues
# per GraphRAG literature (see docs/watercooler-planning/MEMORY_INTEGRATION_ROADMAP.md)
DEFAULT_MAX_TOKENS = 768
DEFAULT_OVERLAP = 64
DEFAULT_ENCODING = "cl100k_base"


@dataclass
class ChunkerConfig:
    """Configuration for text chunking."""

    max_tokens: int = DEFAULT_MAX_TOKENS
    overlap: int = DEFAULT_OVERLAP
    encoding_name: str = DEFAULT_ENCODING
    include_header: bool = False
    mode: str = "default"  # "default", "watercooler", "whitepaper", "semantic"
    include_section_context: bool = True
    section_context_format: str = "prefix"  # "[Section] content"
    preserve_abstract: bool = True

    @classmethod
    def watercooler_preset(
        cls,
        max_tokens: int = 768,
        overlap: int = 64,
        encoding_name: str = DEFAULT_ENCODING,
    ) -> "ChunkerConfig":
        """Preset tuned for watercooler threads."""
        return cls(
            max_tokens=max_tokens,
            overlap=overlap,
            encoding_name=encoding_name,
            include_header=True,
            mode="watercooler",
        )

    @classmethod
    def whitepaper_preset(
        cls,
        max_tokens: int = 768,
        overlap: int = 64,
        encoding_name: str = DEFAULT_ENCODING,
    ) -> "ChunkerConfig":
        """Preset tuned for white papers and academic documents.

        Uses semantic-aware chunking that:
        - Respects section boundaries
        - Preserves atomic blocks (math, tables, code)
        - Includes section context as prefixes
        - Keeps abstracts as single chunks when possible
        """
        return cls(
            max_tokens=max_tokens,
            overlap=overlap,
            encoding_name=encoding_name,
            include_header=False,
            mode="whitepaper",
            include_section_context=True,
            section_context_format="prefix",
            preserve_abstract=True,
        )


def _get_encoder(encoding_name: str = DEFAULT_ENCODING):
    """Get tiktoken encoder, or None if unavailable."""
    if not TIKTOKEN_AVAILABLE:
        return None
    try:
        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return None


def _estimate_tokens(text: str) -> int:
    """Estimate token count without tiktoken (rough approximation)."""
    # Rough estimate: ~4 characters per token for English text
    return len(text) // 4


def count_tokens(text: str, encoding_name: str = DEFAULT_ENCODING) -> int:
    """Count tokens in text.

    Uses tiktoken if available, otherwise estimates based on character count.

    Args:
        text: Text to count tokens for.
        encoding_name: Tiktoken encoding name.

    Returns:
        Token count (exact if tiktoken available, estimated otherwise).
    """
    encoder = _get_encoder(encoding_name)
    if encoder:
        return len(encoder.encode(text))
    return _estimate_tokens(text)


def _generate_chunk_id(text: str, entry_id: str, index: int) -> str:
    """Generate a stable chunk ID based on content hash."""
    content = f"{entry_id}:{index}:{text}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _split_into_sentences(text: str) -> list[str]:
    """Split text into sentences (basic implementation)."""
    import re

    # Split on sentence boundaries while keeping the delimiter
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sentences if s.strip()]


def _split_into_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs."""
    paragraphs = text.split("\n\n")
    return [p.strip() for p in paragraphs if p.strip()]


def _chunk_section_content(
    content: str,
    section_prefix: str,
    config: ChunkerConfig,
    atomic_line_ranges: list[tuple[int, int]],
) -> list[tuple[str, int]]:
    """Chunk section content respecting atomic blocks.

    Args:
        content: Section body content.
        section_prefix: Section context prefix to add.
        config: Chunking configuration.
        atomic_line_ranges: List of (start, end) line ranges for atomic blocks.

    Returns:
        List of (chunk_text, token_count) tuples.
    """
    if not content.strip():
        return []

    # Calculate available tokens for content after prefix
    prefix_tokens = count_tokens(section_prefix, config.encoding_name) if section_prefix else 0
    available_tokens = config.max_tokens - prefix_tokens - 2  # -2 for newlines

    chunks: list[tuple[str, int]] = []
    paragraphs = _split_into_paragraphs(content)

    current_chunk: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = count_tokens(para, config.encoding_name)

        # Check if this paragraph contains an atomic block
        # For simplicity, treat long paragraphs with special markers as atomic
        is_atomic = (
            para.startswith("$$")
            or para.startswith("|")
            or para.startswith("```")
        )

        if is_atomic:
            # Flush current chunk first
            if current_chunk:
                chunk_content = "\n\n".join(current_chunk)
                if section_prefix and config.include_section_context:
                    chunk_text_str = f"{section_prefix}\n\n{chunk_content}"
                else:
                    chunk_text_str = chunk_content
                chunks.append((chunk_text_str, count_tokens(chunk_text_str, config.encoding_name)))
                current_chunk = []
                current_tokens = 0

            # Add atomic block as its own chunk (may exceed max if necessary)
            if section_prefix and config.include_section_context:
                chunk_text_str = f"{section_prefix}\n\n{para}"
            else:
                chunk_text_str = para
            chunks.append((chunk_text_str, count_tokens(chunk_text_str, config.encoding_name)))
            continue

        if para_tokens > available_tokens:
            # Flush current chunk
            if current_chunk:
                chunk_content = "\n\n".join(current_chunk)
                if section_prefix and config.include_section_context:
                    chunk_text_str = f"{section_prefix}\n\n{chunk_content}"
                else:
                    chunk_text_str = chunk_content
                chunks.append((chunk_text_str, count_tokens(chunk_text_str, config.encoding_name)))
                current_chunk = []
                current_tokens = 0

            # Split large paragraph by sentences
            sentences = _split_into_sentences(para)
            sentence_chunk: list[str] = []
            sentence_tokens = 0

            for sentence in sentences:
                sent_tokens = count_tokens(sentence, config.encoding_name)

                if sentence_tokens + sent_tokens > available_tokens:
                    if sentence_chunk:
                        chunk_content = " ".join(sentence_chunk)
                        if section_prefix and config.include_section_context:
                            chunk_text_str = f"{section_prefix}\n\n{chunk_content}"
                        else:
                            chunk_text_str = chunk_content
                        chunks.append((chunk_text_str, count_tokens(chunk_text_str, config.encoding_name)))
                        # Overlap
                        overlap_tokens = 0
                        overlap_sentences = []
                        for s in reversed(sentence_chunk):
                            s_tokens = count_tokens(s, config.encoding_name)
                            if overlap_tokens + s_tokens <= config.overlap:
                                overlap_sentences.insert(0, s)
                                overlap_tokens += s_tokens
                            else:
                                break
                        sentence_chunk = overlap_sentences
                        sentence_tokens = overlap_tokens

                sentence_chunk.append(sentence)
                sentence_tokens += sent_tokens

            if sentence_chunk:
                chunk_content = " ".join(sentence_chunk)
                if section_prefix and config.include_section_context:
                    chunk_text_str = f"{section_prefix}\n\n{chunk_content}"
                else:
                    chunk_text_str = chunk_content
                chunks.append((chunk_text_str, count_tokens(chunk_text_str, config.encoding_name)))

        elif current_tokens + para_tokens > available_tokens:
            # Flush and start new chunk
            if current_chunk:
                chunk_content = "\n\n".join(current_chunk)
                if section_prefix and config.include_section_context:
                    chunk_text_str = f"{section_prefix}\n\n{chunk_content}"
                else:
                    chunk_text_str = chunk_content
                chunks.append((chunk_text_str, count_tokens(chunk_text_str, config.encoding_name)))

            current_chunk = [para]
            current_tokens = para_tokens
        else:
            current_chunk.append(para)
            current_tokens += para_tokens

    # Flush remaining
    if current_chunk:
        chunk_content = "\n\n".join(current_chunk)
        if section_prefix and config.include_section_context:
            chunk_text_str = f"{section_prefix}\n\n{chunk_content}"
        else:
            chunk_text_str = chunk_content
        chunks.append((chunk_text_str, count_tokens(chunk_text_str, config.encoding_name)))

    return chunks


def chunk_whitepaper(
    text: str,
    config: Optional[ChunkerConfig] = None,
) -> list[tuple[str, int]]:
    """Chunk whitepaper/academic document with semantic awareness.

    Respects section boundaries, preserves atomic blocks (math, tables, code),
    and includes section context as prefixes for better retrieval.

    Args:
        text: Markdown text to chunk.
        config: Chunking configuration.

    Returns:
        List of (chunk_text, token_count) tuples.
    """
    from .whitepaper_parser import (
        parse_whitepaper_structure,
        get_section_breadcrumb,
        get_all_sections,
    )

    if config is None:
        config = ChunkerConfig.whitepaper_preset()

    if not text.strip():
        return []

    # Parse document structure
    structure = parse_whitepaper_structure(text)

    # If not detected as whitepaper and we're in explicit whitepaper mode,
    # still try to process it as sections
    if not structure.sections:
        # Fall back to default chunking
        return _chunk_text_default(text, config)

    chunks: list[tuple[str, int]] = []

    # Build atomic block line ranges for quick lookup
    atomic_ranges = [
        (block.start_line, block.end_line)
        for block in structure.atomic_blocks
    ]

    # Process each section
    all_sections = get_all_sections(structure)

    for section in all_sections:
        # Skip sections with no content (only have children)
        if not section.content.strip():
            continue

        # Build section prefix for context
        if config.include_section_context:
            breadcrumb = get_section_breadcrumb(section)
            section_prefix = f"[{breadcrumb}]"
        else:
            section_prefix = ""

        # Check if this is an abstract that should be preserved whole
        is_abstract = section.title.lower() == "abstract"
        if is_abstract and config.preserve_abstract:
            content_tokens = count_tokens(section.content, config.encoding_name)
            # Keep abstract as single chunk if under 1.5x max tokens
            if content_tokens <= config.max_tokens * 1.5:
                if section_prefix:
                    chunk_text_str = f"{section_prefix}\n\n{section.content}"
                else:
                    chunk_text_str = section.content
                chunks.append((chunk_text_str, count_tokens(chunk_text_str, config.encoding_name)))
                continue

        # Chunk section content
        section_chunks = _chunk_section_content(
            section.content,
            section_prefix,
            config,
            atomic_ranges,
        )
        chunks.extend(section_chunks)

    # If no chunks were generated (e.g., all content in front matter),
    # fall back to default chunking
    if not chunks:
        return _chunk_text_default(text, config)

    return chunks


def _chunk_text_default(
    text: str,
    config: ChunkerConfig,
) -> list[tuple[str, int]]:
    """Default paragraph-based chunking (internal implementation).

    This is the original chunking logic, extracted for reuse.
    """
    # If text fits in one chunk, return as-is
    total_tokens = count_tokens(text, config.encoding_name)
    if total_tokens <= config.max_tokens:
        return [(text, total_tokens)]

    chunks: list[tuple[str, int]] = []

    # Try paragraph-based chunking first
    paragraphs = _split_into_paragraphs(text)

    current_chunk: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = count_tokens(para, config.encoding_name)

        # If single paragraph exceeds max, split by sentences
        if para_tokens > config.max_tokens:
            # Flush current chunk
            if current_chunk:
                chunk_text_str = "\n\n".join(current_chunk)
                chunks.append((chunk_text_str, current_tokens))
                current_chunk = []
                current_tokens = 0

            # Split paragraph by sentences
            sentences = _split_into_sentences(para)
            sentence_chunk: list[str] = []
            sentence_tokens = 0

            for sentence in sentences:
                sent_tokens = count_tokens(sentence, config.encoding_name)

                if sentence_tokens + sent_tokens > config.max_tokens:
                    if sentence_chunk:
                        chunks.append((" ".join(sentence_chunk), sentence_tokens))
                        # Overlap: keep last few sentences
                        overlap_tokens = 0
                        overlap_sentences = []
                        for s in reversed(sentence_chunk):
                            s_tokens = count_tokens(s, config.encoding_name)
                            if overlap_tokens + s_tokens <= config.overlap:
                                overlap_sentences.insert(0, s)
                                overlap_tokens += s_tokens
                            else:
                                break
                        sentence_chunk = overlap_sentences
                        sentence_tokens = overlap_tokens

                sentence_chunk.append(sentence)
                sentence_tokens += sent_tokens

            if sentence_chunk:
                chunks.append((" ".join(sentence_chunk), sentence_tokens))

        elif current_tokens + para_tokens > config.max_tokens:
            # Flush and start new chunk
            if current_chunk:
                chunk_text_str = "\n\n".join(current_chunk)
                chunks.append((chunk_text_str, current_tokens))

                # Overlap: keep last paragraph if it fits
                if para_tokens <= config.overlap:
                    current_chunk = [current_chunk[-1]] if current_chunk else []
                    current_tokens = (
                        count_tokens(current_chunk[0], config.encoding_name)
                        if current_chunk
                        else 0
                    )
                else:
                    current_chunk = []
                    current_tokens = 0

            current_chunk.append(para)
            current_tokens += para_tokens
        else:
            current_chunk.append(para)
            current_tokens += para_tokens

    # Flush remaining
    if current_chunk:
        chunk_text_str = "\n\n".join(current_chunk)
        chunks.append((chunk_text_str, current_tokens))

    return chunks


def chunk_text(
    text: str,
    config: Optional[ChunkerConfig] = None,
) -> list[tuple[str, int]]:
    """Split text into chunks with token counts.

    Supports multiple modes:
    - "default": Standard paragraph-based chunking
    - "watercooler": Watercooler thread optimized chunking
    - "whitepaper": Semantic-aware chunking for academic papers
    - "semantic": Auto-detect document structure and use appropriate chunking

    Args:
        text: Text to chunk.
        config: Chunking configuration.

    Returns:
        List of (chunk_text, token_count) tuples.
    """
    if config is None:
        config = ChunkerConfig()

    if not text.strip():
        return []

    # Dispatch based on mode
    if config.mode == "whitepaper":
        return chunk_whitepaper(text, config)
    elif config.mode == "semantic":
        # Auto-detect whitepaper structure
        from .whitepaper_parser import detect_whitepaper

        if detect_whitepaper(text):
            return chunk_whitepaper(text, config)
        # Fall through to default chunking

    return _chunk_text_default(text, config)


def chunk_entry(
    entry: EntryNode,
    config: Optional[ChunkerConfig] = None,
) -> list[ChunkNode]:
    """Chunk an entry's body into ChunkNodes.

    Args:
        entry: Entry to chunk.
        config: Chunking configuration.

    Returns:
        List of ChunkNode objects.
    """
    chunks = chunk_text(entry.body, config)

    chunk_nodes: list[ChunkNode] = []
    chunk_index = 0

    # Optional header chunk capturing metadata (agent/role/type/title)
    if config and config.include_header:
        header_fields = [
            f"agent: {entry.agent or ''}",
            f"role: {entry.role or ''}",
            f"type: {entry.entry_type or ''}",
            f"title: {entry.title or ''}",
            f"timestamp: {entry.timestamp or ''}",
        ]
        header_text = "\n".join(header_fields).strip()
        if header_text:
            header_tokens = count_tokens(header_text, config.encoding_name)
            chunk_id = _generate_chunk_id(header_text, entry.entry_id, chunk_index)
            chunk_nodes.append(
                ChunkNode(
                    chunk_id=chunk_id,
                    entry_id=entry.entry_id,
                    thread_id=entry.thread_id,
                    index=chunk_index,
                    text=header_text,
                    token_count=header_tokens,
                    event_time=entry.timestamp,
                )
            )
            chunk_index += 1

    for text, token_count in chunks:
        chunk_id = _generate_chunk_id(text, entry.entry_id, chunk_index)
        chunk_node = ChunkNode(
            chunk_id=chunk_id,
            entry_id=entry.entry_id,
            thread_id=entry.thread_id,
            index=chunk_index,
            text=text,
            token_count=token_count,
            event_time=entry.timestamp,
        )
        chunk_nodes.append(chunk_node)
        chunk_index += 1

    return chunk_nodes


def chunk_entries(
    entries: list[EntryNode],
    config: Optional[ChunkerConfig] = None,
) -> tuple[list[ChunkNode], dict[str, list[str]]]:
    """Chunk multiple entries.

    Args:
        entries: List of entries to chunk.
        config: Chunking configuration.

    Returns:
        Tuple of (all_chunks, entry_to_chunk_ids mapping)
    """
    all_chunks: list[ChunkNode] = []
    entry_to_chunks: dict[str, list[str]] = {}

    for entry in entries:
        chunks = chunk_entry(entry, config)
        all_chunks.extend(chunks)
        entry_to_chunks[entry.entry_id] = [c.chunk_id for c in chunks]

    return all_chunks, entry_to_chunks


def is_tiktoken_available() -> bool:
    """Check if tiktoken is available."""
    return TIKTOKEN_AVAILABLE
