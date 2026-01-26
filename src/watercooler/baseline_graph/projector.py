"""Projector module for generating markdown from graph data.

This module provides functions to project graph nodes into markdown files.
The graph is the source of truth; markdown files are derived projections.

Key functions:
- project_thread_to_markdown(): Generate full thread MD from graph
- project_entry_to_markdown(): Generate single entry block
- write_thread_markdown(): Write projected MD to file

This is the output side of graph-first architecture.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Markdown Templates
# ============================================================================

# Thread header template
THREAD_HEADER_TEMPLATE = """# {topic} — Thread
Status: {status}
Ball: {ball}
Topic: {topic}
Created: {created}
"""

# Entry block template
ENTRY_BLOCK_TEMPLATE = """---
Entry: {agent} {timestamp}
Role: {role}
Type: {entry_type}
Title: {title}

{body}
<!-- Entry-ID: {entry_id} -->
"""


# ============================================================================
# Entry Projection
# ============================================================================


def project_entry_to_markdown(
    entry: Dict[str, Any],
) -> str:
    """Project an entry node to markdown format.

    Args:
        entry: Entry node dict from graph

    Returns:
        Markdown formatted entry block
    """
    agent = entry.get("agent", "Unknown")
    timestamp = entry.get("timestamp", "")
    role = entry.get("role", "implementer")
    entry_type = entry.get("entry_type", "Note")
    title = entry.get("title", "")
    body = entry.get("body", "")
    entry_id = entry.get("entry_id", "")

    # Ensure body ends with newline
    if body and not body.endswith("\n"):
        body = body + "\n"

    return ENTRY_BLOCK_TEMPLATE.format(
        agent=agent,
        timestamp=timestamp,
        role=role,
        entry_type=entry_type,
        title=title,
        body=body,
        entry_id=entry_id,
    )


# ============================================================================
# Thread Projection
# ============================================================================


def project_thread_to_markdown(
    thread: Dict[str, Any],
    entries: List[Dict[str, Any]],
) -> str:
    """Project thread node and entries to full markdown.

    Args:
        thread: Thread node dict from graph
        entries: List of entry node dicts (should be sorted by index)

    Returns:
        Complete thread markdown content
    """
    # Build header
    topic = thread.get("topic", "")
    status = thread.get("status", "OPEN")
    ball = thread.get("ball", "codex")

    # Get created timestamp from first entry or thread
    if entries:
        created = entries[0].get("timestamp", "")
    else:
        created = thread.get("last_updated", "")

    header = THREAD_HEADER_TEMPLATE.format(
        topic=topic,
        status=status,
        ball=ball,
        created=created,
    )

    # Build entry blocks
    entry_blocks = []
    for entry in entries:
        block = project_entry_to_markdown(entry)
        entry_blocks.append(block)

    # Combine header and entries
    if entry_blocks:
        return header + "\n" + "\n".join(entry_blocks)
    else:
        return header


def project_thread_header_only(
    thread: Dict[str, Any],
    first_entry_timestamp: Optional[str] = None,
) -> str:
    """Project just the thread header (for new threads without entries).

    Args:
        thread: Thread node dict
        first_entry_timestamp: Created timestamp

    Returns:
        Thread header markdown
    """
    topic = thread.get("topic", "")
    status = thread.get("status", "OPEN")
    ball = thread.get("ball", "codex")
    created = first_entry_timestamp or thread.get("last_updated", "")

    return THREAD_HEADER_TEMPLATE.format(
        topic=topic,
        status=status,
        ball=ball,
        created=created,
    )


# ============================================================================
# File Operations
# ============================================================================


def write_thread_markdown(
    threads_dir: Path,
    topic: str,
    content: str,
) -> Path:
    """Write projected markdown to thread file atomically.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        content: Markdown content to write

    Returns:
        Path to written file
    """
    thread_path = threads_dir / f"{topic}.md"
    _atomic_write_text(thread_path, content)
    logger.debug(f"Wrote thread markdown: {thread_path}")
    return thread_path


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text file atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".tmp_",
        suffix=".md",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # Set readable permissions before rename (mkstemp creates with 0600)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================================
# High-Level Operations
# ============================================================================


def project_and_write_thread(
    threads_dir: Path,
    topic: str,
) -> Optional[Path]:
    """Project thread from graph and write to markdown file.

    Reads thread and entries from graph, projects to markdown,
    and writes to {topic}.md file.

    Args:
        threads_dir: Threads directory
        topic: Thread topic

    Returns:
        Path to written file, or None if thread not found
    """
    from .writer import get_thread_from_graph, get_entries_for_thread

    thread = get_thread_from_graph(threads_dir, topic)
    if not thread:
        logger.warning(f"Thread not found in graph for projection: {topic}")
        return None

    entries = get_entries_for_thread(threads_dir, topic)

    content = project_thread_to_markdown(thread, entries)
    return write_thread_markdown(threads_dir, topic, content)


def append_entry_and_project(
    threads_dir: Path,
    topic: str,
    entry: Dict[str, Any],
) -> Optional[Path]:
    """Append entry block to existing markdown file.

    This is an optimization for the common case of adding a single entry.
    Instead of regenerating the entire file, we just append the new entry.

    Note: This reads the existing file and appends, rather than regenerating
    from graph. Use project_and_write_thread for full regeneration.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        entry: Entry node dict to append

    Returns:
        Path to updated file, or None on error
    """
    thread_path = threads_dir / f"{topic}.md"

    if not thread_path.exists():
        # Need to create from scratch
        return project_and_write_thread(threads_dir, topic)

    try:
        existing = thread_path.read_text(encoding="utf-8")
        entry_block = project_entry_to_markdown(entry)

        # Append entry (ensure single newline separation)
        new_content = existing.rstrip() + "\n\n" + entry_block

        write_thread_markdown(threads_dir, topic, new_content)
        return thread_path

    except Exception as e:
        logger.error(f"Failed to append entry to {topic}: {e}")
        return None


def update_header_and_write(
    threads_dir: Path,
    topic: str,
    status: Optional[str] = None,
    ball: Optional[str] = None,
) -> Optional[Path]:
    """Update header fields in existing markdown file.

    This modifies the Status/Ball lines in the header without
    regenerating entries. More efficient for metadata-only updates.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        status: New status (or None to keep existing)
        ball: New ball (or None to keep existing)

    Returns:
        Path to updated file, or None on error
    """
    thread_path = threads_dir / f"{topic}.md"

    if not thread_path.exists():
        logger.warning(f"Thread file not found for header update: {topic}")
        return None

    try:
        content = thread_path.read_text(encoding="utf-8")

        if status is not None:
            # Replace Status: line
            import re
            content = re.sub(
                r"^Status:\s*.+$",
                f"Status: {status.upper()}",
                content,
                count=1,
                flags=re.MULTILINE,
            )

        if ball is not None:
            # Replace Ball: line
            import re
            content = re.sub(
                r"^Ball:\s*.+$",
                f"Ball: {ball}",
                content,
                count=1,
                flags=re.MULTILINE,
            )

        write_thread_markdown(threads_dir, topic, content)
        return thread_path

    except Exception as e:
        logger.error(f"Failed to update header for {topic}: {e}")
        return None


# ============================================================================
# Initialization
# ============================================================================


def create_thread_file(
    threads_dir: Path,
    topic: str,
    title: Optional[str] = None,
    status: str = "OPEN",
    ball: str = "codex",
    created: Optional[str] = None,
) -> Path:
    """Create a new thread markdown file from parameters.

    This creates just the header for a new thread (no entries yet).

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        title: Thread title (defaults to topic)
        status: Initial status
        ball: Initial ball owner
        created: Created timestamp

    Returns:
        Path to created file
    """
    from datetime import datetime, timezone

    threads_dir.mkdir(parents=True, exist_ok=True)
    thread_path = threads_dir / f"{topic}.md"

    if created is None:
        created = datetime.now(timezone.utc).isoformat()

    content = THREAD_HEADER_TEMPLATE.format(
        topic=topic,
        status=status.upper(),
        ball=ball,
        created=created,
    )

    write_thread_markdown(threads_dir, topic, content)
    return thread_path


# ============================================================================
# Bulk Projection Operations (New Tool Suite)
# ============================================================================


@dataclass
class ProjectResult:
    """Result of bulk graph projection operation."""

    files_created: int = 0
    files_updated: int = 0
    files_skipped: int = 0
    errors: List[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "files_created": self.files_created,
            "files_updated": self.files_updated,
            "files_skipped": self.files_skipped,
            "errors": self.errors[:20],
            "error_count": len(self.errors),
            "dry_run": self.dry_run,
        }


def project_graph(
    threads_dir: Path,
    mode: str = "missing",  # "missing" | "selective" | "all"
    topics: Optional[List[str]] = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> ProjectResult:
    """Generate markdown files from graph (source of truth).

    Modes:
    - "missing": Only create markdown for topics without .md files
    - "selective": Project specific topics
    - "all": Regenerate all markdown (requires overwrite=True)

    Use cases:
    - Initial markdown generation after graph import
    - Regenerating corrupted markdown
    - Syncing after direct graph edits

    Args:
        threads_dir: Threads directory
        mode: Processing mode - "missing", "selective", or "all"
        topics: Topics to project (required for "selective" mode)
        overwrite: Allow overwriting existing files (required for "all" mode)
        dry_run: If True, return what would be created/updated without changes

    Returns:
        ProjectResult with file operation counts
    """
    from .writer import get_thread_from_graph, get_entries_for_thread
    from . import storage

    result = ProjectResult(dry_run=dry_run)

    # Validate mode
    if mode not in ("missing", "selective", "all"):
        result.errors.append(f"Invalid mode: {mode}. Use 'missing', 'selective', or 'all'")
        return result

    if mode == "selective" and not topics:
        result.errors.append("Mode 'selective' requires topics list")
        return result

    if mode == "all" and not overwrite:
        result.errors.append("Mode 'all' requires overwrite=True to regenerate existing files")
        return result

    graph_dir = storage.get_graph_dir(threads_dir)

    if not storage.is_per_thread_format(graph_dir):
        result.errors.append(f"No per-thread graph format found at {graph_dir}")
        return result

    # Get available topics from graph
    graph_topics = storage.list_thread_topics(graph_dir)

    if not graph_topics:
        result.errors.append("No topics found in graph")
        return result

    # Determine target topics
    if mode == "selective":
        target_topics = [t for t in topics if t in graph_topics]
        if not target_topics:
            result.errors.append(f"No matching topics in graph. Available: {graph_topics[:10]}")
            return result
    else:
        target_topics = graph_topics

    # Process each topic
    for topic in target_topics:
        try:
            thread_path = threads_dir / f"{topic}.md"
            file_exists = thread_path.exists()

            # Check if we should process this topic
            if mode == "missing" and file_exists:
                result.files_skipped += 1
                continue

            if file_exists and not overwrite and mode != "missing":
                result.files_skipped += 1
                continue

            if dry_run:
                if file_exists:
                    result.files_updated += 1
                else:
                    result.files_created += 1
                continue

            # Get thread and entries from graph
            thread = get_thread_from_graph(threads_dir, topic)
            if not thread:
                result.errors.append(f"Thread not found in graph: {topic}")
                continue

            entries = get_entries_for_thread(threads_dir, topic)

            # Project to markdown
            content = project_thread_to_markdown(thread, entries)
            write_thread_markdown(threads_dir, topic, content)

            if file_exists:
                result.files_updated += 1
            else:
                result.files_created += 1

            logger.debug(f"Projected thread to markdown: {topic}")

        except Exception as e:
            result.errors.append(f"Topic {topic}: {e}")

    if not dry_run:
        logger.info(
            f"Projection complete: {result.files_created} created, "
            f"{result.files_updated} updated, {result.files_skipped} skipped"
        )

    return result
