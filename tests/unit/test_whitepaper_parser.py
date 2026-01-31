"""Unit tests for whitepaper_parser module."""

import pytest

from watercooler_memory.whitepaper_parser import (
    detect_whitepaper,
    parse_whitepaper_structure,
    get_section_breadcrumb,
    get_all_sections,
    DocumentMetadata,
    Section,
    AtomicBlock,
    DocumentStructure,
)


# Sample academic paper content for testing
ACADEMIC_PAPER = """# RAVE: A variational autoencoder for fast and high-quality neural audio synthesis

**Authors**: Antoine Caillon, Philippe Esling
**Year**: 2021
**Source**: arXiv:2111.05011v2 [cs.LG]

---

## Abstract

Deep generative models applied to audio have improved by a large margin the state-of-the-art in many speech and music related tasks. This paper introduces RAVE for fast audio synthesis.

## 1. Introduction

Deep learning applied to audio signals proposes exciting new ways to perform speech generation.

### 1.1 Background

Previous approaches usually rely on very low sampling rates.

### 1.2 Contributions

Our key contributions include a two-stage training procedure.

## 2. Methods

### 2.1 Variational Autoencoders

The VAE loss:

$$\\mathcal{L}_{vae}(\\mathbf{x}) = \\mathbb{E}[S(\\mathbf{x}, \\hat{\\mathbf{x}})] + \\beta \\times D_{KL}$$

### 2.2 Architecture

The encoder uses convolutional layers.

## 3. Results

| Model | MOS | Training time |
|-------|-----|---------------|
| NSynth | 2.68 | ~13 days |
| RAVE | 3.01 | ~7 days |

## References

1. Kingma, D. P., & Welling, M. (2014). Auto-encoding variational bayes.
"""

NON_PAPER_MARKDOWN = """# My Project README

This is a simple README file for my project.

## Installation

Run `pip install my-project` to install.

## Usage

```python
import my_project
my_project.run()
```

## License

MIT License
"""


class TestDetectWhitepaper:
    """Tests for detect_whitepaper function."""

    def test_detect_academic_paper(self):
        """Academic paper with metadata, sections, math should be detected."""
        assert detect_whitepaper(ACADEMIC_PAPER) is True

    def test_detect_non_paper_markdown(self):
        """Regular README should not be detected as whitepaper."""
        assert detect_whitepaper(NON_PAPER_MARKDOWN) is False

    def test_detect_with_authors_only(self):
        """Paper with just title and authors might not meet threshold."""
        minimal = """# My Paper

**Authors**: John Doe

Some content here.
"""
        # Only +2 for title+authors, need 3 to pass
        assert detect_whitepaper(minimal) is False

    def test_detect_with_abstract_and_references(self):
        """Paper with abstract and references increases score."""
        paper = """# My Paper

**Authors**: John Doe

## Abstract

This is the abstract.

## 1. Introduction

Introduction content.

## References

1. Some reference
"""
        # +2 title+authors, +1 abstract, +1 numbered sections, +1 references = 5
        assert detect_whitepaper(paper) is True

    def test_detect_with_math_blocks(self):
        """LaTeX math blocks contribute to score."""
        paper = """# Mathematical Paper

**Authors**: Jane Doe

## Abstract

We prove the following:

$$E = mc^2$$

## 1. Theory

The equation above is fundamental.

## References

1. Einstein, A.
"""
        assert detect_whitepaper(paper) is True

    def test_detect_with_tables(self):
        """Markdown tables contribute to score."""
        paper = """# Results Paper

**Authors**: Jane Doe

## Abstract

We present results.

## 1. Results

| Method | Accuracy |
|--------|----------|
| Ours | 95% |
| Baseline | 90% |

## References

1. Some ref
"""
        assert detect_whitepaper(paper) is True


class TestParseWhitepaperStructure:
    """Tests for parse_whitepaper_structure function."""

    def test_parse_metadata(self):
        """Metadata should be extracted from front matter."""
        structure = parse_whitepaper_structure(ACADEMIC_PAPER)

        assert structure.metadata.title == "RAVE: A variational autoencoder for fast and high-quality neural audio synthesis"
        assert structure.metadata.authors == "Antoine Caillon, Philippe Esling"
        assert structure.metadata.year == "2021"
        assert structure.metadata.source == "arXiv:2111.05011v2 [cs.LG]"

    def test_parse_numbered_sections(self):
        """Numbered sections should be parsed with their numbers."""
        structure = parse_whitepaper_structure(ACADEMIC_PAPER)

        # Find Introduction section
        intro = None
        for section in get_all_sections(structure):
            if section.title == "Introduction":
                intro = section
                break

        assert intro is not None
        assert intro.number == "1"  # Number without trailing period
        assert intro.level == 2

    def test_parse_subsections(self):
        """Subsections should be nested under parent sections."""
        structure = parse_whitepaper_structure(ACADEMIC_PAPER)

        # Find a section with children (Introduction has subsections)
        all_sections = get_all_sections(structure)
        sections_with_children = [s for s in all_sections if s.children]

        # Should have at least one section with children
        assert len(sections_with_children) >= 1

        # Check that children have higher level
        for parent in sections_with_children:
            for child in parent.children:
                assert child.level > parent.level
                assert child.parent == parent

    def test_detect_math_blocks(self):
        """Math blocks should be detected as atomic blocks."""
        structure = parse_whitepaper_structure(ACADEMIC_PAPER)

        math_blocks = [b for b in structure.atomic_blocks if b.block_type == "math"]
        assert len(math_blocks) >= 1

        # Check content includes the equation
        math_content = math_blocks[0].content
        assert "mathcal" in math_content or "$$" in math_content

    def test_detect_tables(self):
        """Tables should be detected as atomic blocks."""
        structure = parse_whitepaper_structure(ACADEMIC_PAPER)

        table_blocks = [b for b in structure.atomic_blocks if b.block_type == "table"]
        assert len(table_blocks) >= 1

        # Check table content
        table_content = table_blocks[0].content
        assert "|" in table_content
        assert "RAVE" in table_content or "Model" in table_content

    def test_is_whitepaper_flag(self):
        """Structure should indicate whether document is a whitepaper."""
        paper_structure = parse_whitepaper_structure(ACADEMIC_PAPER)
        assert paper_structure.is_whitepaper is True

        readme_structure = parse_whitepaper_structure(NON_PAPER_MARKDOWN)
        assert readme_structure.is_whitepaper is False


class TestGetSectionBreadcrumb:
    """Tests for get_section_breadcrumb function."""

    def test_top_level_section(self):
        """Top-level section should have simple breadcrumb."""
        section = Section(
            level=2,
            number="1.",
            title="Introduction",
            content="Some content",
        )
        breadcrumb = get_section_breadcrumb(section)
        assert breadcrumb == "1. Introduction"

    def test_nested_section(self):
        """Nested section should include parent in breadcrumb."""
        parent = Section(
            level=2,
            number="2.",
            title="Methods",
            content="",
        )
        child = Section(
            level=3,
            number="2.1",
            title="Data Collection",
            content="Content here",
            parent=parent,
        )
        parent.children.append(child)

        breadcrumb = get_section_breadcrumb(child)
        assert "Methods" in breadcrumb
        assert "Data Collection" in breadcrumb
        assert " > " in breadcrumb

    def test_deeply_nested_section(self):
        """Deeply nested section should show full path."""
        grandparent = Section(level=2, number="3.", title="Results", content="")
        parent = Section(level=3, number="3.1", title="Quantitative", content="", parent=grandparent)
        child = Section(level=4, number="3.1.1", title="Accuracy", content="Data", parent=parent)

        grandparent.children.append(parent)
        parent.children.append(child)

        breadcrumb = get_section_breadcrumb(child)
        assert "Results" in breadcrumb
        assert "Quantitative" in breadcrumb
        assert "Accuracy" in breadcrumb


class TestGetAllSections:
    """Tests for get_all_sections function."""

    def test_flattens_sections(self):
        """All sections should be returned in document order."""
        structure = parse_whitepaper_structure(ACADEMIC_PAPER)
        all_sections = get_all_sections(structure)

        # Should have multiple sections
        assert len(all_sections) >= 5

        # Check order - Abstract should come before Introduction
        titles = [s.title for s in all_sections]
        if "Abstract" in titles and "Introduction" in titles:
            assert titles.index("Abstract") < titles.index("Introduction")

    def test_includes_children(self):
        """Child sections should be included in flattened list."""
        structure = parse_whitepaper_structure(ACADEMIC_PAPER)
        all_sections = get_all_sections(structure)

        # Should include subsections
        has_subsection = any(s.level >= 3 for s in all_sections)
        assert has_subsection


class TestAtomicBlockDetection:
    """Tests for atomic block detection edge cases."""

    def test_code_blocks_detected(self):
        """Fenced code blocks should be detected."""
        content = """# Paper

## Abstract

Here is code:

```python
def train():
    pass
```

## Methods

More content.
"""
        structure = parse_whitepaper_structure(content)
        code_blocks = [b for b in structure.atomic_blocks if b.block_type == "code"]
        assert len(code_blocks) >= 1
        assert "def train" in code_blocks[0].content

    def test_inline_math_not_detected(self):
        """Inline math ($...$) should not be detected as atomic blocks."""
        content = """# Paper

## Abstract

The equation $E=mc^2$ is famous.

## 1. Methods

We use $\\alpha$ and $\\beta$.
"""
        structure = parse_whitepaper_structure(content)
        math_blocks = [b for b in structure.atomic_blocks if b.block_type == "math"]
        # Only display math ($$...$$) should be atomic, not inline
        assert len(math_blocks) == 0

    def test_multiline_math_detected(self):
        """Multi-line display math should be detected."""
        content = """# Paper

## 1. Methods

$$
\\begin{aligned}
a &= b \\\\
c &= d
\\end{aligned}
$$

More content.
"""
        structure = parse_whitepaper_structure(content)
        math_blocks = [b for b in structure.atomic_blocks if b.block_type == "math"]
        assert len(math_blocks) >= 1
        assert "aligned" in math_blocks[0].content
