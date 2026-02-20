"""Parser for baseline graph - reads threads from graph and generates summaries.

Primary functions read from graph (source of truth):
- iter_threads(): Iterate all threads from graph data
- parse_all_threads(): List all threads from graph
- get_thread_stats(): Statistics from graph

Legacy functions (retained for recovery scripts only):
- parse_thread_file(): Reads from .md (for sync_thread_to_graph migration only)
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Iterator

from .summarizer import (
    summarize_entry,
    summarize_thread,
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


# ============================================================================
# Graph-based thread reading (primary API)
# ============================================================================


def _thread_from_graph(
    threads_dir: Path,
    topic: str,
    config: Optional[SummarizerConfig] = None,
    generate_summaries: bool = True,
) -> Optional[ParsedThread]:
    """Read a thread from graph data and return as ParsedThread.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        config: Summarizer configuration
        generate_summaries: Whether to generate summaries

    Returns:
        ParsedThread or None if not found in graph
    """
    from .writer import get_thread_from_graph, get_entries_for_thread

    config = config or create_summarizer_config()

    thread_meta = get_thread_from_graph(threads_dir, topic)
    if not thread_meta:
        return None

    raw_entries = get_entries_for_thread(threads_dir, topic)

    parsed_entries = []
    entry_dicts = []

    for i, entry in enumerate(raw_entries):
        entry_id = entry.get("entry_id", f"{topic}:{i}")

        # Use existing summary from graph or generate
        summary = entry.get("summary", "")
        if generate_summaries and not summary:
            summary = summarize_entry(
                entry.get("body", ""),
                entry_title=entry.get("title", ""),
                entry_type=entry.get("entry_type", "Note"),
                config=config,
            )

        parsed = ParsedEntry(
            entry_id=entry_id,
            index=entry.get("index", i),
            agent=entry.get("agent"),
            role=entry.get("role"),
            entry_type=entry.get("entry_type"),
            title=entry.get("title"),
            timestamp=entry.get("timestamp"),
            body=entry.get("body", ""),
            summary=summary,
        )
        parsed_entries.append(parsed)

        entry_dicts.append({
            "body": entry.get("body", ""),
            "title": entry.get("title", ""),
            "type": entry.get("entry_type", "Note"),
        })

    # Thread summary
    thread_summary = thread_meta.get("summary", "")
    if generate_summaries and not thread_summary and entry_dicts:
        thread_summary = summarize_thread(
            entry_dicts,
            thread_title=thread_meta.get("title", topic),
            config=config,
        )

    return ParsedThread(
        topic=topic,
        title=thread_meta.get("title", topic),
        status=thread_meta.get("status", "OPEN"),
        ball=thread_meta.get("ball", ""),
        last_updated=thread_meta.get("last_updated", ""),
        summary=thread_summary,
        entries=parsed_entries,
    )


def iter_threads(
    threads_dir: Path,
    config: Optional[SummarizerConfig] = None,
    generate_summaries: bool = True,
    skip_closed: bool = False,
) -> Iterator[ParsedThread]:
    """Iterate over all threads from graph data.

    Args:
        threads_dir: Path to threads directory
        config: Summarizer configuration
        generate_summaries: Whether to generate summaries
        skip_closed: Skip closed threads

    Yields:
        ParsedThread for each thread in graph
    """
    from . import storage

    config = config or create_summarizer_config()

    if not threads_dir.exists():
        logger.warning(f"Threads directory not found: {threads_dir}")
        return

    graph_dir = storage.get_graph_dir(threads_dir)
    topics = storage.list_thread_topics(graph_dir)

    if not topics:
        logger.debug("No topics found in graph")
        return

    for topic in topics:
        thread = _thread_from_graph(threads_dir, topic, config, generate_summaries)
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
    """Parse all threads from graph data.

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
    """Get basic statistics about threads from graph.

    Args:
        threads_dir: Path to threads directory

    Returns:
        Dict with thread counts and status breakdown
    """
    if not threads_dir.exists():
        return {"error": f"Directory not found: {threads_dir}"}

    threads = list(iter_threads(threads_dir, generate_summaries=False))

    status_counts: Dict[str, int] = {}
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


# ============================================================================
# Legacy .md parsing (retained for recovery/migration scripts only)
# ============================================================================


def _generate_entry_id(topic: str, index: int, entry: Any) -> str:
    """Generate entry ID from existing ID or topic:index pattern."""
    if hasattr(entry, "entry_id") and entry.entry_id:
        return entry.entry_id
    return f"{topic}:{index}"


def parse_thread_file(
    thread_path: Path,
    config: Optional[SummarizerConfig] = None,
    generate_summaries: bool = True,
) -> Optional[ParsedThread]:
    """Parse a single thread .md file (legacy — for recovery scripts only).

    In graph-first architecture, use _thread_from_graph() instead.
    This function is retained only for sync_thread_to_graph() and
    recover_graph() paths where .md is the recovery source.

    Args:
        thread_path: Path to thread markdown file
        config: Summarizer configuration
        generate_summaries: Whether to generate summaries

    Returns:
        ParsedThread or None if parsing fails
    """
    from watercooler.fs import discover_thread_files
    from watercooler.thread_entries import parse_thread_entries, parse_thread_header

    if not thread_path.exists():
        logger.warning(f"Thread file not found: {thread_path}")
        return None

    config = config or create_summarizer_config()
    topic = thread_path.stem

    title, status, ball, last_updated = parse_thread_header(thread_path)

    content = thread_path.read_text(encoding="utf-8")
    raw_entries = parse_thread_entries(content)

    parsed_entries = []
    entry_dicts = []

    for entry in raw_entries:
        entry_id = _generate_entry_id(topic, entry.index, entry)

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

        entry_dicts.append({
            "body": entry.body,
            "title": entry.title,
            "type": entry.entry_type,
        })

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
