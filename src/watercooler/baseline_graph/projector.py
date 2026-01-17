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
