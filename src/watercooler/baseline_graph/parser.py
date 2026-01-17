"""Parser for baseline graph - reads threads and generates summaries.

Uses existing watercooler parsing infrastructure and adds summarization
for baseline graph nodes.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Iterator, Tuple

from watercooler.thread_entries import parse_thread_entries, parse_thread_header, ThreadEntry

from .summarizer import (
    summarize_entry,
    summarize_thread,
    extractive_summary,
    SummarizerConfig,
    create_summarizer_config,
)

logger = logging.getLogger(__name__)


@dataclass
class ParsedEntry:
    """Parsed thread entry with summary."""

    entry_id: str
    index: int
    agent: Optional[str]
    role: Optional[str]
    entry_type: Optional[str]
    title: Optional[str]
    timestamp: Optional[str]
    body: str
    summary: str


@dataclass
class ParsedThread:
    """Parsed thread with metadata and entries."""

    topic: str
    title: str
    status: str
    ball: str
    last_updated: str
    summary: str
    entries: List[ParsedEntry] = field(default_factory=list)

    @property
    def entry_count(self) -> int:
        return len(self.entries)


def _generate_entry_id(topic: str, index: int, entry: ThreadEntry) -> str:
    """Generate entry ID from existing ID or topic:index pattern.

    Args:
        topic: Thread topic
        index: Entry index
        entry: Parsed entry

    Returns:
        Entry ID string
    """
    if entry.entry_id:
        return entry.entry_id
    return f"{topic}:{index}"


def parse_thread_file(
    thread_path: Path,
    config: Optional[SummarizerConfig] = None,
    generate_summaries: bool = True,
) -> Optional[ParsedThread]:
    """Parse a single thread file.

    Args:
        thread_path: Path to thread markdown file
        config: Summarizer configuration
        generate_summaries: Whether to generate summaries

    Returns:
        ParsedThread or None if parsing fails
    """
    if not thread_path.exists():
        logger.warning(f"Thread file not found: {thread_path}")
        return None

    config = config or create_summarizer_config()
    topic = thread_path.stem

    # Get thread metadata from markdown file
    title, status, ball, last_updated = parse_thread_header(thread_path)

    # Parse entries
    content = thread_path.read_text(encoding="utf-8")
    raw_entries = parse_thread_entries(content)

    # Convert to ParsedEntry with summaries
    parsed_entries = []
    entry_dicts = []  # For thread summary

    for entry in raw_entries:
        entry_id = _generate_entry_id(topic, entry.index, entry)

        # Generate entry summary
        if generate_summaries:
            summary = summarize_entry(
                entry.body,
                entry_title=entry.title,
                entry_type=entry.entry_type,
                config=config,
            )
        else:
            summary = ""

        parsed = ParsedEntry(
            entry_id=entry_id,
            index=entry.index,
            agent=entry.agent,
            role=entry.role,
            entry_type=entry.entry_type,
            title=entry.title,
            timestamp=entry.timestamp,
            body=entry.body,
            summary=summary,
        )
        parsed_entries.append(parsed)

        # Collect for thread summary
        entry_dicts.append({
            "body": entry.body,
            "title": entry.title,
            "type": entry.entry_type,
        })

    # Generate thread summary
    if generate_summaries and entry_dicts:
        thread_summary = summarize_thread(entry_dicts, thread_title=title, config=config)
    else:
        thread_summary = ""

    return ParsedThread(
        topic=topic,
        title=title,
        status=status,
        ball=ball,
        last_updated=last_updated,
        summary=thread_summary,
        entries=parsed_entries,
    )


def iter_threads(
    threads_dir: Path,
    config: Optional[SummarizerConfig] = None,
    generate_summaries: bool = True,
    skip_closed: bool = False,
) -> Iterator[ParsedThread]:
    """Iterate over all threads in directory.

    Args:
        threads_dir: Path to threads directory
        config: Summarizer configuration
        generate_summaries: Whether to generate summaries
        skip_closed: Skip closed threads

    Yields:
        ParsedThread for each thread file
    """
    config = config or create_summarizer_config()

    if not threads_dir.exists():
        logger.warning(f"Threads directory not found: {threads_dir}")
        return

    # Find all .md files (exclude index.md)
    for md_file in sorted(threads_dir.glob("*.md")):
        if md_file.name == "index.md":
            continue

        thread = parse_thread_file(md_file, config, generate_summaries)
        if thread is None:
            continue

        if skip_closed and thread.status.upper() == "CLOSED":
            logger.debug(f"Skipping closed thread: {thread.topic}")
            continue

        yield thread


def parse_all_threads(
    threads_dir: Path,
    config: Optional[SummarizerConfig] = None,
    generate_summaries: bool = True,
    skip_closed: bool = False,
) -> List[ParsedThread]:
    """Parse all threads in directory.

    Args:
        threads_dir: Path to threads directory
        config: Summarizer configuration
        generate_summaries: Whether to generate summaries
        skip_closed: Skip closed threads

    Returns:
        List of ParsedThread objects
    """
    return list(iter_threads(threads_dir, config, generate_summaries, skip_closed))


def get_thread_stats(threads_dir: Path) -> Dict[str, Any]:
    """Get basic statistics about threads directory.

    Args:
        threads_dir: Path to threads directory

    Returns:
        Dict with thread counts and status breakdown
    """
    if not threads_dir.exists():
        return {"error": f"Directory not found: {threads_dir}"}

    threads = list(iter_threads(threads_dir, generate_summaries=False))

    status_counts = {}
    total_entries = 0

    for thread in threads:
        status = thread.status.upper()
        status_counts[status] = status_counts.get(status, 0) + 1
        total_entries += thread.entry_count

    return {
        "threads_dir": str(threads_dir),
        "total_threads": len(threads),
        "total_entries": total_entries,
        "status_breakdown": status_counts,
        "avg_entries_per_thread": total_entries / len(threads) if threads else 0,
    }
