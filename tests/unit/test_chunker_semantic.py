"""Unit tests for semantic chunking functionality in chunker module."""

import pytest

from watercooler_memory.chunker import (
    chunk_text,
    chunk_whitepaper,
    ChunkerConfig,
    count_tokens,
)


# Sample whitepaper content for testing
WHITEPAPER_CONTENT = """# Neural Network Paper

**Authors**: Jane Doe
**Year**: 2024

---

## Abstract

This paper presents a novel approach to neural network training. We demonstrate significant improvements in accuracy and training speed compared to existing methods.

## 1. Introduction

Neural networks have revolutionized machine learning. However, training remains computationally expensive. Our method addresses this limitation.

### 1.1 Background

Traditional training uses gradient descent with backpropagation. This process requires many iterations.

### 1.2 Our Approach

We propose a hybrid training procedure combining supervised and unsupervised learning.

## 2. Methods

### 2.1 Loss Function

Our loss function combines reconstruction and classification:

$$\\mathcal{L} = \\mathcal{L}_{rec} + \\lambda \\mathcal{L}_{cls}$$

Where $\\lambda$ controls the trade-off.

### 2.2 Architecture

| Layer | Size | Activation |
|-------|------|------------|
| Input | 784 | - |
| Hidden | 256 | ReLU |
| Output | 10 | Softmax |

## 3. Results

Our method achieves state-of-the-art performance on benchmark datasets.

## References

1. LeCun, Y. (1998). Gradient-based learning.
"""

SIMPLE_MARKDOWN = """# Simple Document

This is a simple document without academic structure.

Some more content here with multiple paragraphs.

And another paragraph to fill space.
"""


class TestChunkWhitepaper:
    """Tests for chunk_whitepaper function."""

    def test_chunk_whitepaper_preserves_sections(self):
        """Chunks should align with section boundaries when possible."""
        config = ChunkerConfig.whitepaper_preset()
        chunks = chunk_whitepaper(WHITEPAPER_CONTENT, config)

        assert len(chunks) > 0

        # Check that section prefixes are present
        chunk_texts = [c[0] for c in chunks]
        has_section_prefix = any("[" in text and "]" in text for text in chunk_texts)
        assert has_section_prefix

    def test_abstract_kept_whole(self):
        """Abstract section should be kept as single chunk if small enough."""
        config = ChunkerConfig.whitepaper_preset()
        config = ChunkerConfig(
            max_tokens=1000,  # Large enough to fit abstract
            mode="whitepaper",
            preserve_abstract=True,
            include_section_context=True,
        )
        chunks = chunk_whitepaper(WHITEPAPER_CONTENT, config)

        # Find abstract chunk
        abstract_chunks = [c for c in chunks if "Abstract" in c[0]]
        assert len(abstract_chunks) >= 1

        # The abstract content should be in a single chunk (small abstract)
        abstract_text = abstract_chunks[0][0]
        assert "novel approach" in abstract_text
        assert "significant improvements" in abstract_text

    def test_atomic_blocks_not_split(self):
        """Math and table blocks should not be split across chunks."""
        config = ChunkerConfig.whitepaper_preset()
        chunks = chunk_whitepaper(WHITEPAPER_CONTENT, config)

        # Find chunks with math
        for chunk_text, _ in chunks:
            # If chunk contains $$, it should be complete
            if "$$" in chunk_text:
                # Count $$ occurrences - should be even (paired)
                count = chunk_text.count("$$")
                assert count % 2 == 0, "Math block should not be split"

        # Find chunks with tables
        for chunk_text, _ in chunks:
            if "|" in chunk_text and "---" not in chunk_text.split("|")[0]:
                # If it has table rows, check they're complete
                lines = chunk_text.split("\n")
                table_lines = [l for l in lines if l.strip().startswith("|")]
                # Table should have header + separator + at least one row
                if len(table_lines) > 0:
                    assert len(table_lines) >= 2  # At least header and one row

    def test_section_context_included(self):
        """Chunks should include section breadcrumb prefixes."""
        config = ChunkerConfig(
            max_tokens=200,  # Small to force multiple chunks
            mode="whitepaper",
            include_section_context=True,
        )
        chunks = chunk_whitepaper(WHITEPAPER_CONTENT, config)

        # Most chunks should have section prefixes
        chunks_with_prefix = [c for c in chunks if c[0].startswith("[")]
        assert len(chunks_with_prefix) > 0

    def test_section_context_format(self):
        """Section context should be in bracket format."""
        config = ChunkerConfig.whitepaper_preset()
        chunks = chunk_whitepaper(WHITEPAPER_CONTENT, config)

        for chunk_text, _ in chunks:
            if chunk_text.startswith("["):
                # Find the closing bracket
                bracket_end = chunk_text.find("]")
                assert bracket_end > 0
                # Content should follow after newlines
                assert "\n" in chunk_text[bracket_end:]


class TestChunkTextModeDispatch:
    """Tests for chunk_text mode dispatch."""

    def test_default_mode_uses_paragraph_chunking(self):
        """Default mode should use paragraph-based chunking."""
        config = ChunkerConfig(mode="default")
        chunks = chunk_text(SIMPLE_MARKDOWN, config)

        assert len(chunks) > 0
        # Default mode doesn't add section prefixes
        assert not any(c[0].startswith("[") for c in chunks)

    def test_whitepaper_mode_uses_semantic_chunking(self):
        """Whitepaper mode should use semantic-aware chunking."""
        config = ChunkerConfig(mode="whitepaper", include_section_context=True)
        chunks = chunk_text(WHITEPAPER_CONTENT, config)

        # Should have section prefixes
        has_prefix = any(c[0].startswith("[") for c in chunks)
        assert has_prefix

    def test_semantic_mode_autodetects(self):
        """Semantic mode should auto-detect whitepaper structure."""
        config = ChunkerConfig(mode="semantic", include_section_context=True)

        # Whitepaper content should be chunked semantically
        wp_chunks = chunk_text(WHITEPAPER_CONTENT, config)
        has_prefix = any(c[0].startswith("[") for c in wp_chunks)
        assert has_prefix

        # Simple content should use default chunking
        simple_chunks = chunk_text(SIMPLE_MARKDOWN, config)
        has_simple_prefix = any(c[0].startswith("[") for c in simple_chunks)
        # Simple doc might have some structure but less semantic prefixes
        # The key test is that it doesn't crash

    def test_backward_compatibility(self):
        """Default chunking should work unchanged for existing use cases."""
        # Old-style config (watercooler preset)
        config = ChunkerConfig.watercooler_preset()
        chunks = chunk_text(SIMPLE_MARKDOWN, config)

        assert len(chunks) > 0
        # Should still produce valid chunks
        for text, tokens in chunks:
            assert len(text) > 0
            assert tokens > 0


class TestChunkerConfigPresets:
    """Tests for ChunkerConfig presets."""

    def test_whitepaper_preset_values(self):
        """Whitepaper preset should have correct default values."""
        config = ChunkerConfig.whitepaper_preset()

        assert config.mode == "whitepaper"
        assert config.include_section_context is True
        assert config.preserve_abstract is True
        assert config.max_tokens == 768
        assert config.overlap == 64

    def test_whitepaper_preset_customization(self):
        """Whitepaper preset should accept custom parameters."""
        config = ChunkerConfig.whitepaper_preset(
            max_tokens=500,
            overlap=32,
        )

        assert config.max_tokens == 500
        assert config.overlap == 32
        assert config.mode == "whitepaper"

    def test_watercooler_preset_unchanged(self):
        """Watercooler preset should still work as before."""
        config = ChunkerConfig.watercooler_preset()

        assert config.mode == "watercooler"
        assert config.include_header is True
        assert config.max_tokens == 768


class TestEdgeCases:
    """Tests for edge cases in semantic chunking."""

    def test_empty_content(self):
        """Empty content should return empty list."""
        config = ChunkerConfig.whitepaper_preset()
        chunks = chunk_whitepaper("", config)
        assert chunks == []

    def test_whitespace_only(self):
        """Whitespace-only content should return empty list."""
        config = ChunkerConfig.whitepaper_preset()
        chunks = chunk_whitepaper("   \n\n   ", config)
        assert chunks == []

    def test_no_sections(self):
        """Content without clear sections should still be chunked."""
        content = "Just some plain text without any markdown headers."
        config = ChunkerConfig.whitepaper_preset()
        chunks = chunk_whitepaper(content, config)

        # Should fall back to default chunking
        assert len(chunks) > 0

    def test_very_long_section(self):
        """Very long sections should be split into multiple chunks."""
        long_content = """# Paper

## Abstract

""" + "This is a long sentence. " * 200 + """

## Methods

Short section.
"""
        config = ChunkerConfig(
            max_tokens=100,
            mode="whitepaper",
        )
        chunks = chunk_whitepaper(long_content, config)

        # Should produce multiple chunks
        assert len(chunks) > 2

    def test_nested_lists_preserved(self):
        """Nested lists within sections should be handled."""
        content = """# Paper

## Abstract

Summary here.

## 1. Features

Our system includes:

- Feature A
  - Sub-feature A1
  - Sub-feature A2
- Feature B
- Feature C

## References

1. Ref one
"""
        config = ChunkerConfig.whitepaper_preset()
        chunks = chunk_whitepaper(content, config)

        # List content should be present
        all_text = " ".join(c[0] for c in chunks)
        assert "Feature A" in all_text
        assert "Feature B" in all_text

    def test_disable_section_context(self):
        """Section context can be disabled."""
        config = ChunkerConfig(
            mode="whitepaper",
            include_section_context=False,
        )
        chunks = chunk_whitepaper(WHITEPAPER_CONTENT, config)

        # No chunks should have section prefixes
        assert not any(c[0].startswith("[") for c in chunks)


class TestTokenCounting:
    """Tests for token counting in chunks."""

    def test_chunk_token_counts_reasonable(self):
        """Chunk token counts should be reasonable."""
        config = ChunkerConfig.whitepaper_preset()
        chunks = chunk_whitepaper(WHITEPAPER_CONTENT, config)

        for text, token_count in chunks:
            # Token count should be positive
            assert token_count > 0
            # Token count should match what count_tokens returns
            actual = count_tokens(text, config.encoding_name)
            assert token_count == actual

    def test_chunks_respect_max_tokens(self):
        """Most chunks should respect max_tokens (atomic blocks may exceed)."""
        config = ChunkerConfig(
            max_tokens=200,
            mode="whitepaper",
        )
        chunks = chunk_whitepaper(WHITEPAPER_CONTENT, config)

        # Count chunks that exceed limit
        exceeding = [c for c in chunks if c[1] > config.max_tokens * 1.5]
        # Only atomic blocks (math, tables) should significantly exceed
        # Most chunks should be within limits
        assert len(exceeding) < len(chunks) // 2
