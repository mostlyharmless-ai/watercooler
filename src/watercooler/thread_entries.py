"""Parsing utilities for watercooler thread entries.

These helpers expose structured metadata for individual entries inside a
thread markdown file so higher layers (CLI, MCP, etc.) can work with
entry-level operations without reparsing the raw file repeatedly.

Entry-ID Format:
    Entry-ID is a ULID (Universally Unique Lexicographically Sortable Identifier)
    embedded in thread entries as HTML comments (<!-- Entry-ID: ... -->).
    ULIDs are 26-character case-insensitive strings that encode both timestamp
    and randomness, making them ideal for tracking entries chronologically while
    ensuring uniqueness. The format is: [0-9A-Z]{26} (base32 encoded).

    Entry-IDs are automatically generated when entries are created via the
    watercooler commands (say, ack, handoff) and stored in commit footers for
    full traceability in git history.

Parser Strategy:
    This parser uses Entry: header lines as the primary boundary detection.
    Each entry starts with a line matching:
        Entry: <agent> <ISO8601-timestamp>

    This approach is robust because:
    - Entry: lines have a specific format with timestamp that won't be confused
    - No reliance on '---' separators (which conflict with horizontal rules)
    - Works correctly even with code blocks containing '---' or 'Entry:' text
    - Entry-IDs provide unique identification for deduplication
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import List, Optional, Tuple

from .fs import utcnow_iso

# Thread header parsing patterns
_STAT_RE = re.compile(r"^Status:\s*(?P<val>.+)$", re.IGNORECASE | re.MULTILINE)
_BALL_RE = re.compile(r"^Ball:\s*(?P<val>.+)$", re.IGNORECASE | re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s*(?P<val>.+)$", re.MULTILINE)

# Entry: header line - the primary entry boundary marker
# Format: "Entry: Agent Name (user) 2025-01-01T12:00:00Z"
# Supports multiple ISO 8601 formats:
#   - YYYY-MM-DDTHH:MM:SSZ (basic)
#   - YYYY-MM-DDTHH:MM:SS.ffffffZ (with fractional seconds)
#   - YYYY-MM-DDTHH:MM:SS+00:00 (with offset)
#   - YYYY-MM-DDTHH:MM:SS.ffffff+00:00 (with both)
_ENTRY_LINE_RE = re.compile(
    r"^Entry:\s*(?P<agent>.+?)\s+(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s*$"
)

# Entry-ID comment - unique identifier for deduplication
_ENTRY_ID_RE = re.compile(r"<!--\s*Entry-ID:\s*([A-Za-z0-9_-]+)\s*-->", re.IGNORECASE)

# Code fence detection for skipping content inside code blocks
_CODE_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


@dataclass(frozen=True)
class ThreadEntry:
    """Structured representation of a single thread entry.

    Attributes:
        index: Zero-based entry position in the thread
        header: Markdown header block (agent, timestamp, role, type, title)
        body: Entry body content (markdown)
        agent: Agent name (extracted from "Entry: Agent ..." line)
        timestamp: ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SSZ)
        role: Agent role (planner, critic, implementer, tester, pm, scribe)
        entry_type: Entry type (Note, Plan, Decision, PR, Closure)
        title: Entry title
        entry_id: ULID identifier from <!-- Entry-ID: ... --> comment (26 chars, base32)
        start_line: Starting line number (1-indexed)
        end_line: Ending line number (1-indexed)
        start_offset: Starting byte offset in file
        end_offset: Ending byte offset in file
    """

    index: int
    header: str
    body: str
    agent: Optional[str]
    timestamp: Optional[str]
    role: Optional[str]
    entry_type: Optional[str]
    title: Optional[str]
    entry_id: Optional[str]
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int


def _find_entry_line_indexes(lines: List[str]) -> List[Tuple[int, str, str]]:
    """Find all Entry: header lines, skipping those inside code blocks.

    Args:
        lines: List of lines from the markdown file

    Returns:
        List of tuples: (line_index, agent, timestamp) for each Entry: line found
    """
    entry_positions: List[Tuple[int, str, str]] = []
    in_code_block = False
    # Store opening fence char and minimum length for proper matching
    # Per CommonMark spec: closing fence must use same char and be at least as long
    code_fence_char: Optional[str] = None
    code_fence_len: int = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()

        # Track code fence state
        fence_match = _CODE_FENCE_RE.match(stripped)
        if fence_match:
            fence = fence_match.group(1)
            if not in_code_block:
                # Opening fence: record char and length
                in_code_block = True
                code_fence_char = fence[0]
                code_fence_len = len(fence)
            elif fence[0] == code_fence_char and len(fence) >= code_fence_len:
                # Closing fence: same char and at least as long
                in_code_block = False
                code_fence_char = None
                code_fence_len = 0
            # If fence doesn't match (different char or too short), stay in code block
            continue

        # Skip lines inside code blocks
        if in_code_block:
            continue

        # Check for Entry: header line
        match = _ENTRY_LINE_RE.match(stripped)
        if match:
            agent = match.group("agent").strip()
            timestamp = match.group("timestamp").strip()
            entry_positions.append((idx, agent, timestamp))

    return entry_positions


@dataclass(frozen=True)
class _EntryMetadata:
    agent: Optional[str]
    timestamp: Optional[str]
    role: Optional[str]
    entry_type: Optional[str]
    title: Optional[str]


def _parse_header_metadata(lines: List[str], start_idx: int, end_idx: int) -> _EntryMetadata:
    """Parse metadata from header lines (Entry: through first blank line).

    Args:
        lines: All lines from the file
        start_idx: Index of the Entry: line
        end_idx: Index where this entry ends (next Entry: or EOF)

    Returns:
        Parsed metadata
    """
    agent: Optional[str] = None
    timestamp: Optional[str] = None
    role: Optional[str] = None
    entry_type: Optional[str] = None
    title: Optional[str] = None

    # Parse header lines until we hit a blank line
    for idx in range(start_idx, min(end_idx, len(lines))):
        line = lines[idx].strip()

        # Stop at first blank line (end of header)
        if not line:
            break

        # Parse Entry: line
        match = _ENTRY_LINE_RE.match(line)
        if match:
            agent = match.group("agent").strip()
            timestamp = match.group("timestamp").strip()
            continue

        # Parse key: value lines
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "role":
            role = value
        elif key == "type":
            entry_type = value
        elif key == "title":
            title = value

    return _EntryMetadata(
        agent=agent,
        timestamp=timestamp,
        role=role,
        entry_type=entry_type,
        title=title,
    )


def _extract_header_and_body(
    lines: List[str], start_idx: int, end_idx: int
) -> Tuple[str, str]:
    """Extract header block and body from entry lines.

    Args:
        lines: All lines from the file
        start_idx: Index of the Entry: line
        end_idx: Index where this entry ends

    Returns:
        Tuple of (header_text, body_text)
    """
    header_lines: List[str] = []
    body_lines: List[str] = []
    in_body = False

    for idx in range(start_idx, end_idx):
        line = lines[idx]

        if not in_body:
            if line.strip() == "":
                in_body = True
            else:
                header_lines.append(line)
        else:
            body_lines.append(line)

    header_text = "".join(header_lines).rstrip("\n")
    body_text = "".join(body_lines).rstrip("\n")

    return header_text, body_text


def _extract_entry_id(lines: List[str], start_idx: int, end_idx: int) -> Optional[str]:
    """Extract Entry-ID from entry content.

    Args:
        lines: All lines from the file
        start_idx: Index of the Entry: line
        end_idx: Index where this entry ends

    Returns:
        Entry-ID string or None if not found
    """
    for idx in range(start_idx, end_idx):
        match = _ENTRY_ID_RE.search(lines[idx])
        if match:
            return match.group(1).strip() or None
    return None


def _find_last_content_line(lines: List[str], start_idx: int, end_idx: int) -> int:
    """Find the last line with non-whitespace content.

    Args:
        lines: All lines from the file
        start_idx: Start of entry
        end_idx: End of entry (exclusive)

    Returns:
        Line index of last content line
    """
    for idx in range(end_idx - 1, start_idx - 1, -1):
        if lines[idx].strip():
            return idx
    return start_idx


def parse_thread_entries(text: str) -> List[ThreadEntry]:
    """Parse thread entries from a markdown thread file.

    .. deprecated::
        This function parses raw markdown. In graph-first architecture, use
        ``watercooler.baseline_graph.writer.get_entries_for_thread()`` instead.
        This function is retained only for graph reconciliation/rebuild paths
        (sync_thread_to_graph, reconcile_thread_hosted) where markdown is the
        recovery source.

    This parser uses Entry: header lines as the primary boundary detection.
    Each entry starts with a line matching: "Entry: <agent> <timestamp>"

    Args:
        text: Full thread markdown content.

    Returns:
        A list of ``ThreadEntry`` instances sorted by timestamp.
    """
    if not text:
        return []

    lines = text.splitlines(keepends=True)
    if not lines:
        return []

    # Find all Entry: lines (skipping those in code blocks)
    entry_positions = _find_entry_line_indexes(lines)
    if not entry_positions:
        return []

    # Build line offset array for byte-level indexing
    line_starts: List[int] = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line)

    entries: List[ThreadEntry] = []

    # Process each entry
    for i, (start_idx, agent, timestamp) in enumerate(entry_positions):
        # Entry ends at next Entry: line or EOF
        if i + 1 < len(entry_positions):
            end_idx = entry_positions[i + 1][0]
        else:
            end_idx = len(lines)

        # Parse metadata
        metadata = _parse_header_metadata(lines, start_idx, end_idx)

        # Extract header and body
        header_text, body_text = _extract_header_and_body(lines, start_idx, end_idx)

        # Extract Entry-ID
        entry_id = _extract_entry_id(lines, start_idx, end_idx)

        # Find last content line for accurate end_line
        last_content_idx = _find_last_content_line(lines, start_idx, end_idx)

        entry = ThreadEntry(
            index=i,  # Will be re-indexed after sorting
            header=header_text,
            body=body_text,
            agent=metadata.agent or agent,  # Fallback to parsed agent
            timestamp=metadata.timestamp or timestamp,  # Fallback to parsed timestamp
            role=metadata.role,
            entry_type=metadata.entry_type,
            title=metadata.title,
            entry_id=entry_id,
            start_line=start_idx + 1,  # 1-indexed
            end_line=last_content_idx + 1,  # 1-indexed
            start_offset=line_starts[start_idx],
            end_offset=line_starts[last_content_idx] + len(lines[last_content_idx]),
        )
        entries.append(entry)

    # Deduplicate by Entry-ID (keep first occurrence)
    seen_ids: set[str] = set()
    deduplicated: List[ThreadEntry] = []
    for entry in entries:
        if entry.entry_id:
            if entry.entry_id in seen_ids:
                continue
            seen_ids.add(entry.entry_id)
        deduplicated.append(entry)

    # Sort by timestamp (chronological order)
    def sort_key(entry: ThreadEntry) -> Tuple[int, str]:
        has_timestamp = 0 if entry.timestamp else 1
        timestamp = entry.timestamp or ""
        return (has_timestamp, timestamp)

    sorted_entries = sorted(deduplicated, key=sort_key)

    # Re-index after sorting
    result: List[ThreadEntry] = []
    for idx, entry in enumerate(sorted_entries):
        result.append(ThreadEntry(
            index=idx,
            header=entry.header,
            body=entry.body,
            agent=entry.agent,
            timestamp=entry.timestamp,
            role=entry.role,
            entry_type=entry.entry_type,
            title=entry.title,
            entry_id=entry.entry_id,
            start_line=entry.start_line,
            end_line=entry.end_line,
            start_offset=entry.start_offset,
            end_offset=entry.end_offset,
        ))

    return result


def parse_thread_header(thread_path: Path) -> tuple[str, str, str, str]:
    """Parse thread header metadata from markdown file.

    .. deprecated::
        This function reads raw markdown. In graph-first architecture, use
        ``watercooler.baseline_graph.writer.get_thread_from_graph()`` instead.
        Retained only for graph rebuild/reconciliation paths.

    Args:
        thread_path: Path to the thread markdown file

    Returns:
        Tuple of (title, status, ball, last_entry_timestamp)
    """
    content = thread_path.read_text(encoding="utf-8") if thread_path.exists() else ""

    # Extract title from first markdown heading
    title_match = _TITLE_RE.search(content)
    title = title_match.group("val").strip() if title_match else thread_path.stem

    # Extract status (default to "open")
    status_match = _STAT_RE.search(content)
    status = status_match.group("val").strip().lower() if status_match else "open"

    # Extract ball (default to "unknown")
    ball_match = _BALL_RE.search(content)
    ball = ball_match.group("val").strip() if ball_match else "unknown"

    # Get last entry timestamp from parsed entries
    entries = parse_thread_entries(content)
    if entries and entries[-1].timestamp:
        last_updated = entries[-1].timestamp
    else:
        last_updated = utcnow_iso()

    return title, status, ball, last_updated
