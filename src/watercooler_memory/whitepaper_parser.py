"""White paper detection and structure parsing for semantic chunking.

This module provides utilities for detecting whether a markdown document
has academic paper structure and parsing it into sections for semantic-aware
chunking that respects document hierarchy.

Detection criteria (score >= 3 indicates whitepaper):
- +2: H1 title + `**Authors**:` metadata
- +1: `## Abstract` section
- +1: Numbered sections (`## 1.`)
- +1: Numbered subsections (`### 2.1`)
- +1: `## References` section
- +1: LaTeX math (`$$`)
- +1: Markdown tables
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DocumentMetadata:
    """Metadata extracted from document front matter.

    Attributes:
        title: Document title from H1 header.
        authors: Authors string if present.
        year: Publication year if detected.
        source: Source reference (e.g., arXiv ID).
    """

    title: str
    authors: Optional[str] = None
    year: Optional[str] = None
    source: Optional[str] = None


@dataclass
class AtomicBlock:
    """Content block that should not be split during chunking.

    Attributes:
        block_type: Type of block ("math", "table", "code").
        content: Raw block content including delimiters.
        start_line: Line number where block starts (0-indexed).
        end_line: Line number where block ends (0-indexed).
    """

    block_type: str  # "math", "table", "code"
    content: str
    start_line: int = 0
    end_line: int = 0


@dataclass
class Section:
    """Hierarchical section in document structure.

    Attributes:
        level: Header level (1-4 for H1-H4).
        number: Section number if present (e.g., "1", "2.1").
        title: Section title text.
        content: Section body text (excluding subsections).
        parent: Parent section reference.
        children: Child sections list.
        start_line: Line number where section starts.
        end_line: Line number where section ends.
    """

    level: int  # 1-4 for H1-H4
    number: Optional[str]  # "1", "2.1", etc.
    title: str
    content: str
    parent: Optional["Section"] = None
    children: list["Section"] = field(default_factory=list)
    start_line: int = 0
    end_line: int = 0

    def full_title(self) -> str:
        """Return section number and title combined."""
        if self.number:
            return f"{self.number} {self.title}"
        return self.title


@dataclass
class DocumentStructure:
    """Parsed document structure.

    Attributes:
        metadata: Document metadata (title, authors, etc.).
        sections: Top-level sections list (nested via children).
        atomic_blocks: List of atomic blocks that shouldn't be split.
        is_whitepaper: Whether document matches whitepaper criteria.
        raw_text: Original document text.
    """

    metadata: DocumentMetadata
    sections: list[Section]
    atomic_blocks: list[AtomicBlock]
    is_whitepaper: bool
    raw_text: str = ""


# Detection patterns
_AUTHOR_PATTERN = re.compile(r"^\*\*Authors?\*\*:\s*(.+)$", re.MULTILINE)
_YEAR_PATTERN = re.compile(r"^\*\*Year\*\*:\s*(\d{4})", re.MULTILINE)
_SOURCE_PATTERN = re.compile(r"^\*\*Source\*\*:\s*(.+)$", re.MULTILINE)
_ABSTRACT_PATTERN = re.compile(r"^##\s+Abstract\s*$", re.MULTILINE | re.IGNORECASE)
_REFERENCES_PATTERN = re.compile(r"^##\s+References?\s*$", re.MULTILINE | re.IGNORECASE)
_NUMBERED_SECTION_PATTERN = re.compile(r"^##\s+(\d+)\.\s+")
_NUMBERED_SUBSECTION_PATTERN = re.compile(r"^###\s+(\d+\.\d+)\s+")
_DISPLAY_MATH_PATTERN = re.compile(r"\$\$[^$]+\$\$", re.DOTALL)
_TABLE_PATTERN = re.compile(r"^\|.*\|$", re.MULTILINE)

# Section parsing patterns
_HEADER_PATTERN = re.compile(r"^(#{1,4})\s+(?:(\d+(?:\.\d+)*)\s*[.:]?\s+)?(.+)$")
_FENCED_CODE_PATTERN = re.compile(r"^```", re.MULTILINE)

# =============================================================================
# Whitepaper Detection Scoring Constants
# =============================================================================
# Documents scoring >= WHITEPAPER_DETECTION_THRESHOLD are classified as whitepapers.
# Each feature contributes points toward the total score.

WHITEPAPER_DETECTION_THRESHOLD = 3  # Minimum score to classify as whitepaper

# Scoring weights for each feature
_SCORE_H1_WITH_AUTHORS = 2      # H1 title + **Authors**: metadata together
_SCORE_ABSTRACT_SECTION = 1     # Has ## Abstract section
_SCORE_NUMBERED_SECTIONS = 1    # Has numbered sections (## 1. ...)
_SCORE_NUMBERED_SUBSECTIONS = 1 # Has numbered subsections (### 1.1 ...)
_SCORE_REFERENCES_SECTION = 1   # Has ## References section
_SCORE_LATEX_MATH = 1           # Contains $$ display math
_SCORE_MARKDOWN_TABLES = 1      # Has markdown tables (>= 2 rows)

# Detection scan limits (optimization to avoid scanning entire large documents)
_HEADER_SCAN_LINES = 20         # Lines to scan for H1 header
_METADATA_SCAN_CHARS = 2000     # Characters to scan for author metadata
_MIN_TABLE_ROWS = 2             # Minimum rows for table detection


def detect_whitepaper(text: str) -> bool:
    """Detect whether text has academic paper structure.

    Uses a scoring system where score >= WHITEPAPER_DETECTION_THRESHOLD
    indicates whitepaper structure. See module constants for scoring weights.

    Args:
        text: Markdown text to analyze.

    Returns:
        True if document matches whitepaper criteria.
    """
    score = 0

    # Check for H1 title + authors metadata
    lines = text.split("\n")
    has_h1 = any(line.startswith("# ") for line in lines[:_HEADER_SCAN_LINES])
    has_authors = bool(_AUTHOR_PATTERN.search(text[:_METADATA_SCAN_CHARS]))
    if has_h1 and has_authors:
        score += _SCORE_H1_WITH_AUTHORS

    # Check for Abstract section
    if _ABSTRACT_PATTERN.search(text):
        score += _SCORE_ABSTRACT_SECTION

    # Check for numbered sections
    if _NUMBERED_SECTION_PATTERN.search(text):
        score += _SCORE_NUMBERED_SECTIONS

    # Check for numbered subsections
    if _NUMBERED_SUBSECTION_PATTERN.search(text):
        score += _SCORE_NUMBERED_SUBSECTIONS

    # Check for References section
    if _REFERENCES_PATTERN.search(text):
        score += _SCORE_REFERENCES_SECTION

    # Check for LaTeX display math
    if _DISPLAY_MATH_PATTERN.search(text):
        score += _SCORE_LATEX_MATH

    # Check for markdown tables
    table_rows = _TABLE_PATTERN.findall(text)
    if len(table_rows) >= _MIN_TABLE_ROWS:
        score += _SCORE_MARKDOWN_TABLES

    return score >= WHITEPAPER_DETECTION_THRESHOLD


def _extract_metadata(text: str) -> DocumentMetadata:
    """Extract document metadata from front matter."""
    title = ""
    authors = None
    year = None
    source = None

    # Extract title from first H1
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            break

    # Extract authors
    author_match = _AUTHOR_PATTERN.search(text[:2000])
    if author_match:
        authors = author_match.group(1).strip()

    # Extract year
    year_match = _YEAR_PATTERN.search(text[:2000])
    if year_match:
        year = year_match.group(1)

    # Extract source
    source_match = _SOURCE_PATTERN.search(text[:2000])
    if source_match:
        source = source_match.group(1).strip()

    return DocumentMetadata(
        title=title,
        authors=authors,
        year=year,
        source=source,
    )


def _extract_atomic_blocks(text: str) -> list[AtomicBlock]:
    """Extract atomic blocks (math, tables, code) that shouldn't be split."""
    blocks: list[AtomicBlock] = []
    lines = text.split("\n")

    # Track fenced code blocks
    in_code_block = False
    code_start = 0
    code_lines: list[str] = []

    # Track display math blocks
    in_math_block = False
    math_start = 0
    math_lines: list[str] = []

    # Track table blocks
    table_start = -1
    table_lines: list[str] = []

    for i, line in enumerate(lines):
        # Handle fenced code blocks
        if line.strip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_start = i
                code_lines = [line]
            else:
                code_lines.append(line)
                blocks.append(AtomicBlock(
                    block_type="code",
                    content="\n".join(code_lines),
                    start_line=code_start,
                    end_line=i,
                ))
                in_code_block = False
                code_lines = []
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        # Handle display math blocks
        if line.strip().startswith("$$"):
            if not in_math_block:
                in_math_block = True
                math_start = i
                math_lines = [line]
                # Check if it's a single-line math block
                if line.strip().endswith("$$") and len(line.strip()) > 4:
                    blocks.append(AtomicBlock(
                        block_type="math",
                        content=line,
                        start_line=i,
                        end_line=i,
                    ))
                    in_math_block = False
                    math_lines = []
            else:
                math_lines.append(line)
                blocks.append(AtomicBlock(
                    block_type="math",
                    content="\n".join(math_lines),
                    start_line=math_start,
                    end_line=i,
                ))
                in_math_block = False
                math_lines = []
            continue

        if in_math_block:
            math_lines.append(line)
            # Check for closing $$ within line
            if "$$" in line and not line.strip().startswith("$$"):
                blocks.append(AtomicBlock(
                    block_type="math",
                    content="\n".join(math_lines),
                    start_line=math_start,
                    end_line=i,
                ))
                in_math_block = False
                math_lines = []
            continue

        # Handle table blocks
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            if table_start < 0:
                table_start = i
                table_lines = [line]
            else:
                table_lines.append(line)
        else:
            if table_start >= 0 and len(table_lines) >= 2:
                blocks.append(AtomicBlock(
                    block_type="table",
                    content="\n".join(table_lines),
                    start_line=table_start,
                    end_line=i - 1,
                ))
            table_start = -1
            table_lines = []

    # Handle unclosed blocks at end of file
    if table_start >= 0 and len(table_lines) >= 2:
        blocks.append(AtomicBlock(
            block_type="table",
            content="\n".join(table_lines),
            start_line=table_start,
            end_line=len(lines) - 1,
        ))

    return blocks


def _parse_sections(text: str) -> list[Section]:
    """Parse document into hierarchical section structure."""
    lines = text.split("\n")
    sections: list[Section] = []
    section_stack: list[Section] = []  # Track nesting

    current_content_lines: list[str] = []
    current_section_start = 0

    # Skip past front matter (metadata before first section)
    in_front_matter = True

    for i, line in enumerate(lines):
        header_match = _HEADER_PATTERN.match(line.strip())

        if header_match:
            in_front_matter = False
            level = len(header_match.group(1))
            number = header_match.group(2)  # May be None
            title = header_match.group(3).strip()

            # Finalize previous section content
            if section_stack:
                section_stack[-1].content = "\n".join(current_content_lines).strip()
                section_stack[-1].end_line = i - 1

            # Create new section
            new_section = Section(
                level=level,
                number=number,
                title=title,
                content="",
                start_line=i,
            )

            # Find parent based on level
            while section_stack and section_stack[-1].level >= level:
                section_stack.pop()

            if section_stack:
                new_section.parent = section_stack[-1]
                section_stack[-1].children.append(new_section)
            else:
                sections.append(new_section)

            section_stack.append(new_section)
            current_content_lines = []
            current_section_start = i + 1

        elif not in_front_matter and section_stack:
            current_content_lines.append(line)

    # Finalize last section
    if section_stack:
        section_stack[-1].content = "\n".join(current_content_lines).strip()
        section_stack[-1].end_line = len(lines) - 1

    return sections


def parse_whitepaper_structure(text: str) -> DocumentStructure:
    """Parse a markdown document into structured components.

    Extracts metadata, sections, and atomic blocks for semantic-aware chunking.

    Args:
        text: Markdown text to parse.

    Returns:
        DocumentStructure with parsed components.
    """
    metadata = _extract_metadata(text)
    sections = _parse_sections(text)
    atomic_blocks = _extract_atomic_blocks(text)
    is_whitepaper = detect_whitepaper(text)

    return DocumentStructure(
        metadata=metadata,
        sections=sections,
        atomic_blocks=atomic_blocks,
        is_whitepaper=is_whitepaper,
        raw_text=text,
    )


def get_section_breadcrumb(section: Section) -> str:
    """Build hierarchical breadcrumb path for a section.

    Args:
        section: Section to build breadcrumb for.

    Returns:
        Breadcrumb string like "2. Methods > 2.1 Data Collection".
    """
    parts: list[str] = []
    current: Optional[Section] = section

    while current is not None:
        parts.append(current.full_title())
        current = current.parent

    parts.reverse()
    return " > ".join(parts)


def _flatten_sections(sections: list[Section]) -> list[Section]:
    """Flatten nested section structure into a list preserving order."""
    result: list[Section] = []

    def visit(section: Section):
        result.append(section)
        for child in section.children:
            visit(child)

    for section in sections:
        visit(section)

    return result


def get_all_sections(structure: DocumentStructure) -> list[Section]:
    """Get all sections in document order (flattened from hierarchy).

    Args:
        structure: Parsed document structure.

    Returns:
        List of all sections in document order.
    """
    return _flatten_sections(structure.sections)
