"""Hosted mode operations using GitHub API.

This module provides thread operations for hosted HTTP mode, using the GitHub
Contents API instead of local filesystem operations. It mirrors the interface
of the local filesystem operations in helpers.py.

Usage:
    from .hosted_ops import (
        list_threads_hosted,
        read_thread_hosted,
        write_thread_hosted,
    )

    # In hosted mode:
    if is_hosted_context(context):
        threads = list_threads_hosted(http_ctx)
        content = read_thread_hosted(http_ctx, topic)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from watercooler.thread_entries import parse_thread_entries, ThreadEntry

from .context import get_http_context, HttpRequestContext
from .github_api import GitHubClient, GitHubNotFoundError, GitHubAPIError
from .observability import log_debug, log_error

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class HostedThread:
    """Thread metadata from hosted mode."""
    topic: str
    title: str
    status: str
    ball: str
    last_updated: str
    entry_count: int


# ============================================================================
# Thread Reading Operations
# ============================================================================


def _get_github_client() -> tuple[str | None, GitHubClient | None]:
    """Get GitHubClient from current HTTP context.

    Returns:
        Tuple of (error_message, client). If error_message is not None,
        client will be None.
    """
    http_ctx = get_http_context()
    if not http_ctx:
        return ("No HTTP context available for hosted mode", None)

    if not http_ctx.github_token:
        return ("No GitHub token available for hosted mode", None)

    if not http_ctx.repo:
        return ("No repository specified in HTTP context", None)

    client = GitHubClient(
        token=http_ctx.github_token,
        repo=http_ctx.repo,
        branch=http_ctx.effective_branch,
    )
    return (None, client)


def list_threads_hosted(
    open_only: bool | None = None,
) -> tuple[str | None, list[HostedThread]]:
    """List threads from GitHub repository.

    Args:
        open_only: Filter by status (True=open only, False=closed only, None=all)

    Returns:
        Tuple of (error_message, threads). If error_message is not None,
        threads will be empty.
    """
    import sys
    print(f"[DEBUG] list_threads_hosted: entry, open_only={open_only}", file=sys.stderr)

    error, client = _get_github_client()
    print(f"[DEBUG] list_threads_hosted: client error={error}, client={client}", file=sys.stderr)
    if error or not client:
        return (error or "Failed to create GitHub client", [])

    try:
        # List all .md files in root
        print(f"[DEBUG] list_threads_hosted: calling list_files", file=sys.stderr)
        files = client.list_files("")
        print(f"[DEBUG] list_threads_hosted: got {len(files)} files", file=sys.stderr)
        md_files = [f for f in files if f.name.endswith(".md") and f.type == "file"]

        threads: list[HostedThread] = []
        for file_info in md_files:
            topic = file_info.name[:-3]  # Remove .md extension

            # Skip non-thread markdown files
            if topic.lower() in ("readme", "contributing", "license", "changelog"):
                continue

            try:
                # Read thread content to extract metadata
                file_content = client.get_file(file_info.path)
                content = file_content.content
                title, status, ball, last_updated = _extract_thread_metadata(content, topic)

                # Apply status filter
                if open_only is True and status.upper() != "OPEN":
                    continue
                if open_only is False and status.upper() == "OPEN":
                    continue

                # Count entries
                entries = parse_thread_entries(content)
                entry_count = len(entries)

                threads.append(HostedThread(
                    topic=topic,
                    title=title,
                    status=status,
                    ball=ball,
                    last_updated=last_updated,
                    entry_count=entry_count,
                ))

            except GitHubAPIError as e:
                log_debug(f"Error reading thread {topic}: {e}")
                # Skip threads we can't read
                continue

        log_debug(f"list_threads_hosted: found {len(threads)} threads")
        return (None, threads)

    except GitHubAPIError as e:
        import sys
        print(f"[DEBUG] list_threads_hosted: GitHubAPIError: {e}", file=sys.stderr)
        log_error(f"list_threads_hosted failed: {e}")
        return (f"GitHub API error: {e}", [])
    except Exception as e:
        import sys
        print(f"[DEBUG] list_threads_hosted: UNEXPECTED ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        raise


def read_thread_hosted(topic: str) -> tuple[str | None, str]:
    """Read thread content from GitHub repository.

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (error_message, content). If error_message is not None,
        content will be empty.
    """
    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", "")

    try:
        file_path = f"{topic}.md"
        file_content = client.get_file(file_path)
        log_debug(f"read_thread_hosted: read {topic} ({len(file_content.content)} chars)")
        return (None, file_content.content)

    except GitHubNotFoundError:
        return (f"Thread '{topic}' not found", "")

    except GitHubAPIError as e:
        log_error(f"read_thread_hosted failed: {e}")
        return (f"GitHub API error: {e}", "")


def load_thread_entries_hosted(topic: str) -> tuple[str | None, list[ThreadEntry]]:
    """Load thread entries from GitHub repository.

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (error_message, entries). If error_message is not None,
        entries will be empty.
    """
    error, content = read_thread_hosted(topic)
    if error:
        return (error, [])

    try:
        entries = parse_thread_entries(content)
        log_debug(f"load_thread_entries_hosted: parsed {len(entries)} entries from {topic}")
        return (None, entries)

    except Exception as e:
        log_error(f"load_thread_entries_hosted failed: {e}")
        return (f"Error parsing thread entries: {e}", [])


def thread_exists_hosted(topic: str) -> bool:
    """Check if a thread exists in GitHub repository.

    Args:
        topic: Thread topic identifier

    Returns:
        True if thread exists, False otherwise.
    """
    error, client = _get_github_client()
    if error or not client:
        return False

    return client.file_exists(f"{topic}.md")


# ============================================================================
# Thread Writing Operations
# ============================================================================


def write_thread_hosted(
    topic: str,
    content: str,
    message: str,
    sha: Optional[str] = None,
) -> tuple[str | None, str]:
    """Write thread content to GitHub repository.

    Args:
        topic: Thread topic identifier
        content: New thread content
        message: Commit message
        sha: Current file SHA (required for updates, omit for creates)

    Returns:
        Tuple of (error_message, new_sha). If error_message is not None,
        new_sha will be empty.
    """
    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", "")

    try:
        file_path = f"{topic}.md"

        # If no SHA provided, try to get current file's SHA
        if sha is None:
            try:
                existing = client.get_file(file_path)
                sha = existing.sha
            except GitHubNotFoundError:
                # File doesn't exist, will be created
                pass

        new_sha = client.put_file(
            path=file_path,
            content=content,
            message=message,
            sha=sha,
        )

        log_debug(f"write_thread_hosted: wrote {topic} (sha={new_sha[:8]})")
        return (None, new_sha)

    except GitHubAPIError as e:
        log_error(f"write_thread_hosted failed: {e}")
        return (f"GitHub API error: {e}", "")


def get_thread_sha_hosted(topic: str) -> tuple[str | None, str]:
    """Get the current SHA of a thread file.

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (error_message, sha). If error_message is not None,
        sha will be empty.
    """
    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", "")

    try:
        file_path = f"{topic}.md"
        file_content = client.get_file(file_path)
        return (None, file_content.sha)

    except GitHubNotFoundError:
        return (None, "")  # File doesn't exist, return empty SHA

    except GitHubAPIError as e:
        return (f"GitHub API error: {e}", "")


# ============================================================================
# Helper Functions
# ============================================================================


def _extract_thread_metadata(
    content: str,
    topic: str,
) -> tuple[str, str, str, str]:
    """Extract metadata from thread markdown content.

    Args:
        content: Thread markdown content
        topic: Thread topic (used as fallback title)

    Returns:
        Tuple of (title, status, ball, last_updated)
    """
    title = topic
    status = "OPEN"
    ball = ""
    last_updated = ""

    # Parse header section (before first ---)
    if "---" in content:
        header = content.split("---")[0]
    else:
        header = content[:500]  # First 500 chars as fallback

    # Extract title from first # heading
    title_match = re.search(r"^#\s+(.+?)(?:\s*—|\s*$)", header, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()

    # Extract Status:
    status_match = re.search(r"^Status:\s*(.+)$", header, re.MULTILINE)
    if status_match:
        status = status_match.group(1).strip()

    # Extract Ball:
    ball_match = re.search(r"^Ball:\s*(.+)$", header, re.MULTILINE)
    if ball_match:
        ball = ball_match.group(1).strip()

    # Find last entry timestamp
    entry_timestamps = re.findall(r"^Entry:\s*[^\s]+\s+(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)", content, re.MULTILINE)
    if entry_timestamps:
        last_updated = entry_timestamps[-1]
    else:
        # Try Created: field
        created_match = re.search(r"^Created:\s*(.+)$", header, re.MULTILINE)
        if created_match:
            last_updated = created_match.group(1).strip()

    return (title, status, ball, last_updated)


# ============================================================================
# Hosted Write Operations
# ============================================================================


def say_hosted(
    topic: str,
    title: str,
    body: str,
    agent: str,
    role: str = "implementer",
    entry_type: str = "Note",
    entry_id: Optional[str] = None,
    create_if_missing: bool = True,
) -> tuple[str | None, dict]:
    """Add an entry to a thread using GitHub API.

    This is the hosted equivalent of watercooler.commands.say. It:
    1. Reads current thread content (or creates new thread if missing)
    2. Appends a new entry with proper formatting
    3. Flips the ball to the other party
    4. Writes back to GitHub

    Args:
        topic: Thread topic identifier
        title: Entry title
        body: Entry body content
        agent: Agent name (e.g., "Claude")
        role: Agent role (planner, critic, implementer, etc.)
        entry_type: Entry type (Note, Plan, Decision, etc.)
        entry_id: Optional entry ID (generated if not provided)
        create_if_missing: Create thread if it doesn't exist

    Returns:
        Tuple of (error_message, result_dict). If error_message is not None,
        result_dict will be empty.
    """
    from ulid import ULID

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    http_ctx = get_http_context()
    if not http_ctx:
        return ("No HTTP context available", {})

    file_path = f"{topic}.md"
    entry_id = entry_id or str(ULID())
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # Try to read existing thread
        file_content = None
        existing_sha = None
        try:
            file_content = client.get_file(file_path)
            existing_sha = file_content.sha
            current_content = file_content.content
        except GitHubNotFoundError:
            if not create_if_missing:
                return (f"Thread '{topic}' not found and create_if_missing=False", {})
            current_content = None

        if current_content:
            # Parse existing thread to get metadata
            _, status, old_ball, _ = _extract_thread_metadata(current_content, topic)

            # Determine new ball owner (flip to "other" agent)
            # Simple flip: if current agent has ball, give to "Agent", otherwise keep
            agent_lower = agent.lower()
            old_ball_lower = (old_ball or "").lower()
            if old_ball_lower == agent_lower or not old_ball:
                new_ball = "Agent"  # Default counterpart
            else:
                new_ball = agent  # Give ball to current agent

            # Append entry to existing content
            new_entry = _format_entry(
                agent=agent,
                timestamp=timestamp,
                role=role,
                entry_type=entry_type,
                title=title,
                body=body,
                entry_id=entry_id,
            )

            # Update ball in header
            updated_content = _update_ball_in_header(current_content, new_ball)
            new_content = updated_content.rstrip() + "\n\n" + new_entry + "\n"
        else:
            # Create new thread
            new_ball = "Agent"  # Default to Agent for new threads
            status = "OPEN"

            header = _create_thread_header(
                topic=topic,
                created=timestamp,
                status=status,
                ball=new_ball,
            )

            new_entry = _format_entry(
                agent=agent,
                timestamp=timestamp,
                role=role,
                entry_type=entry_type,
                title=title,
                body=body,
                entry_id=entry_id,
            )

            new_content = header + "\n\n" + new_entry + "\n"

        # Write to GitHub
        commit_message = f"[watercooler] {topic}: {title}\n\nEntry-ID: {entry_id}"
        new_sha = client.put_file(
            path=file_path,
            content=new_content,
            message=commit_message,
            sha=existing_sha,
        )

        log_debug(f"say_hosted: wrote entry to {topic} (sha={new_sha[:8]})")

        return (None, {
            "topic": topic,
            "entry_id": entry_id,
            "timestamp": timestamp,
            "status": status,
            "ball": new_ball,
            "sha": new_sha,
        })

    except GitHubAPIError as e:
        log_error(f"say_hosted failed: {e}")
        return (f"GitHub API error: {e}", {})


def set_status_hosted(
    topic: str,
    status: str,
) -> tuple[str | None, dict]:
    """Update thread status using GitHub API.

    Args:
        topic: Thread topic identifier
        status: New status value

    Returns:
        Tuple of (error_message, result_dict).
    """
    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    file_path = f"{topic}.md"

    try:
        # Read current content
        file_content = client.get_file(file_path)
        current_content = file_content.content
        existing_sha = file_content.sha

        # Get old status
        _, old_status, ball, _ = _extract_thread_metadata(current_content, topic)

        # Update status in header
        new_content = _update_status_in_header(current_content, status)

        # Write to GitHub
        commit_message = f"[watercooler] {topic}: status {old_status} → {status}"
        new_sha = client.put_file(
            path=file_path,
            content=new_content,
            message=commit_message,
            sha=existing_sha,
        )

        log_debug(f"set_status_hosted: updated {topic} status to {status}")

        return (None, {
            "topic": topic,
            "old_status": old_status,
            "new_status": status,
            "ball": ball,
            "sha": new_sha,
        })

    except GitHubNotFoundError:
        return (f"Thread '{topic}' not found", {})
    except GitHubAPIError as e:
        log_error(f"set_status_hosted failed: {e}")
        return (f"GitHub API error: {e}", {})


def ack_hosted(
    topic: str,
    agent: str,
    title: str = "Ack",
    body: str = "Acknowledged",
    entry_id: Optional[str] = None,
) -> tuple[str | None, dict]:
    """Acknowledge a thread without flipping the ball.

    Args:
        topic: Thread topic identifier
        agent: Agent name
        title: Acknowledgment title
        body: Acknowledgment body

    Returns:
        Tuple of (error_message, result_dict).
    """
    from ulid import ULID

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    file_path = f"{topic}.md"
    entry_id = entry_id or str(ULID())
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # Read current content
        file_content = client.get_file(file_path)
        current_content = file_content.content
        existing_sha = file_content.sha

        # Get current metadata (ball stays the same for ack)
        _, status, ball, _ = _extract_thread_metadata(current_content, topic)

        # Append ack entry
        new_entry = _format_entry(
            agent=agent,
            timestamp=timestamp,
            role="pm",  # Ack entries are typically from PM role
            entry_type="Note",
            title=title,
            body=body,
            entry_id=entry_id,
        )

        new_content = current_content.rstrip() + "\n\n" + new_entry + "\n"

        # Write to GitHub (ball doesn't change)
        commit_message = f"[watercooler] {topic}: {title} (ack)\n\nEntry-ID: {entry_id}"
        new_sha = client.put_file(
            path=file_path,
            content=new_content,
            message=commit_message,
            sha=existing_sha,
        )

        log_debug(f"ack_hosted: acknowledged {topic}")

        return (None, {
            "topic": topic,
            "entry_id": entry_id,
            "timestamp": timestamp,
            "status": status,
            "ball": ball,  # Ball unchanged
            "sha": new_sha,
        })

    except GitHubNotFoundError:
        return (f"Thread '{topic}' not found", {})
    except GitHubAPIError as e:
        log_error(f"ack_hosted failed: {e}")
        return (f"GitHub API error: {e}", {})


def handoff_hosted(
    topic: str,
    agent: str,
    target_agent: Optional[str] = None,
    note: str = "",
    entry_id: Optional[str] = None,
) -> tuple[str | None, dict]:
    """Hand off the ball to another agent.

    Args:
        topic: Thread topic identifier
        agent: Current agent name
        target_agent: Agent to hand off to (optional)
        note: Handoff note

    Returns:
        Tuple of (error_message, result_dict).
    """
    from ulid import ULID

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    file_path = f"{topic}.md"
    entry_id = entry_id or str(ULID())
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # Read current content
        file_content = client.get_file(file_path)
        current_content = file_content.content
        existing_sha = file_content.sha

        # Get current metadata
        _, status, old_ball, _ = _extract_thread_metadata(current_content, topic)

        # Determine new ball owner
        new_ball = target_agent or "Agent"  # Default to "Agent" if not specified

        # Update ball in header
        updated_content = _update_ball_in_header(current_content, new_ball)

        # Add handoff entry if note provided
        if note:
            new_entry = _format_entry(
                agent=agent,
                timestamp=timestamp,
                role="pm",
                entry_type="Note",
                title=f"Handoff to {new_ball}",
                body=note,
                entry_id=entry_id,
            )
            new_content = updated_content.rstrip() + "\n\n" + new_entry + "\n"
        else:
            new_content = updated_content

        # Write to GitHub
        commit_message = f"[watercooler] {topic}: handoff to {new_ball}"
        if entry_id:
            commit_message += f"\n\nEntry-ID: {entry_id}"
        new_sha = client.put_file(
            path=file_path,
            content=new_content,
            message=commit_message,
            sha=existing_sha,
        )

        log_debug(f"handoff_hosted: handed off {topic} to {new_ball}")

        return (None, {
            "topic": topic,
            "from_agent": agent,
            "to_agent": new_ball,
            "entry_id": entry_id if note else None,
            "timestamp": timestamp,
            "status": status,
            "ball": new_ball,
            "sha": new_sha,
        })

    except GitHubNotFoundError:
        return (f"Thread '{topic}' not found", {})
    except GitHubAPIError as e:
        log_error(f"handoff_hosted failed: {e}")
        return (f"GitHub API error: {e}", {})


# ============================================================================
# Entry Formatting Helpers
# ============================================================================


def _format_entry(
    agent: str,
    timestamp: str,
    role: str,
    entry_type: str,
    title: str,
    body: str,
    entry_id: str,
) -> str:
    """Format a thread entry in markdown.

    Returns:
        Formatted entry string.
    """
    lines = [
        f"Entry: {agent} (user) {timestamp}",
        f"Role: {role}",
        f"Type: {entry_type}",
        f"Title: {title}",
        f"<!-- Entry-ID: {entry_id} -->",
        "",
        body,
    ]
    return "\n".join(lines)


def _create_thread_header(
    topic: str,
    created: str,
    status: str = "OPEN",
    ball: str = "Agent",
    priority: str = "P2",
) -> str:
    """Create a thread header in markdown.

    Returns:
        Formatted header string.
    """
    lines = [
        f"# {topic} — Thread",
        f"Status: {status}",
        f"Ball: {ball}",
        f"Topic: {topic}",
        f"Created: {created}",
        f"Priority: {priority}",
        "",
        "---",
    ]
    return "\n".join(lines)


def _update_ball_in_header(content: str, new_ball: str) -> str:
    """Update the Ball: field in thread header.

    Args:
        content: Current thread content
        new_ball: New ball owner

    Returns:
        Updated content with new ball owner.
    """
    # Replace Ball: line in header
    return re.sub(
        r"^Ball:\s*.+$",
        f"Ball: {new_ball}",
        content,
        count=1,
        flags=re.MULTILINE,
    )


def _update_status_in_header(content: str, new_status: str) -> str:
    """Update the Status: field in thread header.

    Args:
        content: Current thread content
        new_status: New status value

    Returns:
        Updated content with new status.
    """
    # Replace Status: line in header
    return re.sub(
        r"^Status:\s*.+$",
        f"Status: {new_status}",
        content,
        count=1,
        flags=re.MULTILINE,
    )
