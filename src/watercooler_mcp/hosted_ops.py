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
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from watercooler.config_facade import config
from watercooler.thread_entries import parse_thread_entries, ThreadEntry

from .context import get_http_context, HttpRequestContext
from .github_api import (
    GitHubClient,
    GitHubNotFoundError,
    GitHubAPIError,
    GitHubConflictError,
)
from .observability import log_debug, log_error, log_warning

# Graph file paths (monolithic format - deprecated, kept for backward compatibility)
GRAPH_NODES_PATH = "graph/baseline/nodes.jsonl"
GRAPH_EDGES_PATH = "graph/baseline/edges.jsonl"

# Per-thread graph directory (canonical format)
GRAPH_THREADS_DIR = "graph/baseline/threads"

# Default retry count for conflict handling (configurable via env)
DEFAULT_MAX_RETRIES = int(os.getenv("WATERCOOLER_GRAPH_MAX_RETRIES", "3"))


def _validate_topic(topic: str) -> None:
    """Validate topic parameter to prevent path traversal attacks.

    Args:
        topic: Thread topic identifier

    Raises:
        ValueError: If topic contains invalid characters
    """
    if not topic:
        raise ValueError("Topic cannot be empty")
    if "/" in topic or "\\" in topic or ".." in topic:
        raise ValueError(
            f"Invalid topic: {topic!r} - contains path traversal characters"
        )
    if topic.startswith("."):
        raise ValueError(f"Invalid topic: {topic!r} - cannot start with '.'")


def _get_per_thread_paths(topic: str) -> tuple[str, str, str]:
    """Get per-thread graph file paths.

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (meta_path, entries_path, edges_path)
    """
    base = f"{GRAPH_THREADS_DIR}/{topic}"
    return (f"{base}/meta.json", f"{base}/entries.jsonl", f"{base}/edges.jsonl")


def _reconstruct_markdown_from_graph(meta: dict, entries: list[dict]) -> str:
    """Reconstruct markdown thread content from per-thread graph data.

    Args:
        meta: Thread metadata from meta.json
        entries: List of entry objects from entries.jsonl

    Returns:
        Reconstructed markdown content matching the legacy format.
    """
    lines: list[str] = []

    # Thread header
    title = meta.get("title", meta.get("topic", "Untitled"))
    lines.append(f"# {title}")
    lines.append("")

    # Metadata block
    topic = meta.get("topic", "")
    if topic:
        lines.append(f"Topic: {topic}")
    status = meta.get("status", "OPEN")
    lines.append(f"Status: {status}")
    ball = meta.get("ball", "")
    if ball:
        lines.append(f"Ball: {ball}")
    priority = meta.get("priority", "")
    if priority:
        lines.append(f"Priority: {priority}")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Entries (sorted by index with timestamp as tie-breaker for stable ordering)
    sorted_entries = sorted(
        entries, key=lambda e: (e.get("index", 0), e.get("timestamp", ""))
    )
    for entry in sorted_entries:
        # Entry header line
        agent = entry.get("agent", "Agent")
        role = entry.get("role", "")
        entry_type = entry.get(
            "entry_type", "Note"
        )  # entry_type, not type (type is "entry")
        entry_title = entry.get("title", "")
        timestamp = entry.get("timestamp", "")

        header_parts = [f"Entry: {agent}"]
        if role:
            header_parts.append(f"({role})")
        if entry_type:
            header_parts.append(f"[{entry_type}]")
        if entry_title:
            header_parts.append(f"- {entry_title}")
        if timestamp:
            header_parts.append(f"@ {timestamp}")

        lines.append(" ".join(header_parts))
        lines.append("")

        # Entry body
        body = entry.get("body", "")
        if body:
            lines.append(body)
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _validate_meta_fields(meta: dict, topic: str) -> None:
    """Validate meta.json fields and log warnings for missing/corrupt data.

    Args:
        meta: Thread metadata dict
        topic: Topic for error messages
    """
    required_fields = ["topic", "status"]
    recommended_fields = ["title", "ball", "entry_count"]

    for field in required_fields:
        if field not in meta:
            log_warning(f"meta.json for {topic} missing required field: {field}")

    for field in recommended_fields:
        if field not in meta:
            log_debug(f"meta.json for {topic} missing recommended field: {field}")

    # Validate status value
    status = meta.get("status", "")
    if status and status.upper() not in ("OPEN", "CLOSED", "IN_REVIEW", "BLOCKED"):
        log_warning(f"meta.json for {topic} has unexpected status: {status}")

    # Validate topic matches
    meta_topic = meta.get("topic", "")
    if meta_topic and meta_topic != topic:
        log_warning(f"meta.json topic mismatch: expected {topic}, got {meta_topic}")


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
    import sys

    http_ctx = get_http_context()

    if not http_ctx:
        return ("No HTTP context available for hosted mode", None)

    if not http_ctx.github_token:
        return ("No GitHub token available for hosted mode", None)

    if not http_ctx.repo:
        return ("No repository specified in HTTP context", None)

    # Use repo directly - dashboard sends the threads repo name
    # (e.g., "org/repo-threads", not "org/repo")
    threads_repo = http_ctx.repo

    client = GitHubClient(
        token=http_ctx.github_token,
        repo=threads_repo,
        branch=http_ctx.effective_branch,
    )
    return (None, client)


def list_threads_hosted(
    open_only: bool | None = None,
) -> tuple[str | None, list[HostedThread]]:
    """List threads from GitHub repository (per-thread format).

    Args:
        open_only: Filter by status (True=open only, False=closed only, None=all)

    Returns:
        Tuple of (error_message, threads). If error_message is not None,
        threads will be empty.
    """
    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", [])

    try:
        # List directories in graph/baseline/threads/
        try:
            items = client.list_files(GRAPH_THREADS_DIR)
        except GitHubNotFoundError:
            # No threads directory yet
            log_debug("list_threads_hosted: threads directory not found")
            return (None, [])

        thread_dirs = [f for f in items if f.type == "dir"]

        threads: list[HostedThread] = []
        for dir_info in thread_dirs:
            topic = dir_info.name

            try:
                # Read meta.json for this thread
                meta_path = f"{GRAPH_THREADS_DIR}/{topic}/meta.json"
                meta_content = client.get_file(meta_path)
                meta = json.loads(meta_content.content)

                title = meta.get("title", topic)
                status = meta.get("status", "OPEN")
                ball = meta.get("ball", "")
                last_updated = meta.get("last_updated", "")
                entry_count = meta.get("entry_count", 0)

                # Apply status filter
                if open_only is True and status.upper() != "OPEN":
                    continue
                if open_only is False and status.upper() == "OPEN":
                    continue

                threads.append(
                    HostedThread(
                        topic=topic,
                        title=title,
                        status=status,
                        ball=ball,
                        last_updated=last_updated,
                        entry_count=entry_count,
                    )
                )

            except GitHubNotFoundError:
                log_debug(f"No meta.json for thread {topic}, skipping")
                continue
            except GitHubAPIError as e:
                log_debug(f"Error reading thread {topic}: {e}")
                continue
            except json.JSONDecodeError as e:
                log_debug(f"Invalid meta.json for thread {topic}: {e}")
                continue

        log_debug(f"list_threads_hosted: found {len(threads)} threads")
        return (None, threads)

    except GitHubAPIError as e:
        log_error(f"list_threads_hosted failed: {e}")
        return (f"GitHub API error: {e}", [])
    except Exception as e:
        log_error(f"list_threads_hosted unexpected error: {type(e).__name__}: {e}")
        raise


def read_thread_hosted(topic: str) -> tuple[str | None, str]:
    """Read thread content from GitHub repository (per-thread format).

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (error_message, content). If error_message is not None,
        content will be empty. Content is reconstructed markdown from
        per-thread graph files.
    """
    try:
        _validate_topic(topic)
    except ValueError as e:
        return (str(e), "")

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", "")

    meta_path, entries_path, _ = _get_per_thread_paths(topic)

    try:
        # Read meta.json
        meta_content = client.get_file(meta_path)
        meta = json.loads(meta_content.content)

        # Validate meta fields (logs warnings for issues)
        _validate_meta_fields(meta, topic)

        # Read entries.jsonl
        entries: list[dict] = []
        try:
            entries_content = client.get_file(entries_path)
            for line in entries_content.content.strip().split("\n"):
                if line.strip():
                    entries.append(json.loads(line))
        except GitHubNotFoundError:
            # No entries yet - that's OK for new threads
            log_debug(f"read_thread_hosted: no entries.jsonl for {topic} (new thread)")

        # Reconstruct markdown from per-thread data
        content = _reconstruct_markdown_from_graph(meta, entries)
        log_debug(
            f"read_thread_hosted: read {topic} ({len(content)} chars from per-thread format)"
        )
        return (None, content)

    except GitHubNotFoundError:
        return (f"Thread '{topic}' not found", "")

    except GitHubAPIError as e:
        log_error(f"read_thread_hosted failed: {e}")
        return (f"GitHub API error: {e}", "")


def load_thread_entries_hosted(topic: str) -> tuple[str | None, list[ThreadEntry]]:
    """Load thread entries from GitHub repository (reads entries.jsonl directly).

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (error_message, entries). If error_message is not None,
        entries will be empty.
    """
    try:
        _validate_topic(topic)
    except ValueError as e:
        return (str(e), [])

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", [])

    _, entries_path, _ = _get_per_thread_paths(topic)

    try:
        entries_content = client.get_file(entries_path)
        raw_entries: list[dict] = []
        for line in entries_content.content.strip().split("\n"):
            if line.strip():
                raw_entries.append(json.loads(line))

        # Convert graph entry dicts to ThreadEntry objects
        entries: list[ThreadEntry] = []
        for i, e in enumerate(raw_entries):
            entries.append(ThreadEntry(
                index=e.get("index", i),
                header="",  # Not available from graph format
                body=e.get("body", ""),
                agent=e.get("agent"),
                timestamp=e.get("timestamp"),
                role=e.get("role"),
                entry_type=e.get("entry_type"),
                title=e.get("title"),
                entry_id=e.get("entry_id"),
                start_line=0,
                end_line=0,
                start_offset=0,
                end_offset=0,
            ))

        log_debug(
            f"load_thread_entries_hosted: parsed {len(entries)} entries from {topic}"
        )
        return (None, entries)

    except GitHubNotFoundError:
        return (f"Thread '{topic}' not found", [])

    except Exception as e:
        log_error(f"load_thread_entries_hosted failed: {e}")
        return (f"Error loading thread entries: {e}", [])


def load_thread_metadata_hosted(topic: str) -> tuple[str | None, dict]:
    """Load thread metadata from GitHub repository (reads meta.json directly).

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (error_message, metadata_dict). If error_message is not None,
        metadata will be empty. metadata_dict has keys:
        title, status, ball, last_updated, entry_count.
    """
    try:
        _validate_topic(topic)
    except ValueError as e:
        return (str(e), {})

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    meta_path, _, _ = _get_per_thread_paths(topic)

    try:
        meta_content = client.get_file(meta_path)
        meta = json.loads(meta_content.content)

        return (None, {
            "title": meta.get("title", topic),
            "status": meta.get("status", "OPEN"),
            "ball": meta.get("ball", ""),
            "last_updated": meta.get("last_updated", meta.get("created", "")),
            "entry_count": meta.get("entry_count", 0),
            "summary": meta.get("summary", ""),
        })

    except GitHubNotFoundError:
        return (f"Thread '{topic}' metadata not found", {})
    except json.JSONDecodeError as e:
        return (f"Invalid meta.json for thread '{topic}': {e}", {})
    except Exception as e:
        log_error(f"load_thread_metadata_hosted failed: {e}")
        return (f"Error loading thread metadata: {e}", {})


def thread_exists_hosted(topic: str) -> bool:
    """Check if a thread exists in GitHub repository (per-thread format).

    Args:
        topic: Thread topic identifier

    Returns:
        True if thread exists, False otherwise.
    """
    try:
        _validate_topic(topic)
    except ValueError:
        return False

    error, client = _get_github_client()
    if error or not client:
        return False

    # Check for per-thread format (meta.json)
    meta_path, _, _ = _get_per_thread_paths(topic)
    return client.file_exists(meta_path)


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
    try:
        _validate_topic(topic)
    except ValueError as e:
        return (str(e), "")

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
# Graph Operations (for graph-first hosted mode)
# ============================================================================


def _read_graph_files(
    client: GitHubClient,
) -> tuple[list[dict], list[dict], str | None, str | None]:
    """Read graph nodes and edges from GitHub.

    Returns:
        Tuple of (nodes, edges, nodes_sha, edges_sha).
        If files don't exist, returns empty lists and None SHAs.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    nodes_sha: str | None = None
    edges_sha: str | None = None

    try:
        nodes_file = client.get_file(GRAPH_NODES_PATH)
        nodes_sha = nodes_file.sha
        for line in nodes_file.content.split("\n"):
            line = line.strip()
            if line:
                try:
                    nodes.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except GitHubNotFoundError:
        log_debug("Graph nodes.jsonl not found, will create")

    try:
        edges_file = client.get_file(GRAPH_EDGES_PATH)
        edges_sha = edges_file.sha
        for line in edges_file.content.split("\n"):
            line = line.strip()
            if line:
                try:
                    edges.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except GitHubNotFoundError:
        log_debug("Graph edges.jsonl not found, will create")

    return nodes, edges, nodes_sha, edges_sha


def _write_graph_files(
    client: GitHubClient,
    nodes: list[dict],
    edges: list[dict],
    nodes_sha: str | None,
    edges_sha: str | None,
    commit_message: str,
) -> tuple[str | None, str | None]:
    """Write graph files to GitHub.

    Returns:
        Tuple of (new_nodes_sha, new_edges_sha) or (None, None) on error.

    Raises:
        GitHubConflictError: If there's a SHA mismatch (caller should retry)
    """
    try:
        # Sort nodes: threads first (by topic), then entries (by thread_topic, index)
        def node_sort_key(n: dict) -> tuple:
            if n.get("type") == "thread":
                return (0, n.get("topic", ""), 0)
            else:
                return (1, n.get("thread_topic", ""), n.get("index", 0))

        sorted_nodes = sorted(nodes, key=node_sort_key)

        # Sort edges by source_id
        sorted_edges = sorted(
            edges, key=lambda e: (e.get("source_id", ""), e.get("target_id", ""))
        )

        # Write nodes
        nodes_content = (
            "\n".join(json.dumps(n, separators=(",", ":")) for n in sorted_nodes) + "\n"
        )
        new_nodes_sha = client.put_file(
            path=GRAPH_NODES_PATH,
            content=nodes_content,
            message=commit_message,
            sha=nodes_sha,
        )

        # Write edges
        edges_content = (
            "\n".join(json.dumps(e, separators=(",", ":")) for e in sorted_edges) + "\n"
        )
        new_edges_sha = client.put_file(
            path=GRAPH_EDGES_PATH,
            content=edges_content,
            message=commit_message,
            sha=edges_sha,
        )

        return new_nodes_sha, new_edges_sha

    except GitHubConflictError:
        # Let conflict errors propagate for retry handling
        raise

    except GitHubAPIError as e:
        log_error(f"Failed to write graph files: {e}")
        return None, None


# ============================================================================
# Per-Thread Graph Operations (canonical format)
# ============================================================================


def _read_per_thread_graph(
    client: GitHubClient,
    topic: str,
) -> tuple[dict | None, list[dict], list[dict], str | None, str | None, str | None]:
    """Read per-thread graph files from GitHub.

    Args:
        client: GitHub API client
        topic: Thread topic identifier

    Returns:
        Tuple of (meta, entries, edges, meta_sha, entries_sha, edges_sha).
        If files don't exist, returns None/empty lists and None SHAs.
    """
    meta_path, entries_path, edges_path = _get_per_thread_paths(topic)

    meta: dict | None = None
    entries: list[dict] = []
    edges: list[dict] = []
    meta_sha: str | None = None
    entries_sha: str | None = None
    edges_sha: str | None = None

    # Read meta.json
    try:
        meta_file = client.get_file(meta_path)
        meta_sha = meta_file.sha
        meta = json.loads(meta_file.content)
    except GitHubNotFoundError:
        log_debug(f"Per-thread meta.json not found for {topic}, will create")
    except json.JSONDecodeError as e:
        log_error(f"Failed to parse meta.json for {topic}: {e}")

    # Read entries.jsonl
    try:
        entries_file = client.get_file(entries_path)
        entries_sha = entries_file.sha
        for line in entries_file.content.split("\n"):
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except GitHubNotFoundError:
        log_debug(f"Per-thread entries.jsonl not found for {topic}, will create")

    # Read edges.jsonl
    try:
        edges_file = client.get_file(edges_path)
        edges_sha = edges_file.sha
        for line in edges_file.content.split("\n"):
            line = line.strip()
            if line:
                try:
                    edges.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except GitHubNotFoundError:
        log_debug(f"Per-thread edges.jsonl not found for {topic}, will create")

    return meta, entries, edges, meta_sha, entries_sha, edges_sha


def _write_per_thread_graph(
    client: GitHubClient,
    topic: str,
    meta: dict,
    entries: list[dict],
    edges: list[dict],
    meta_sha: str | None,
    entries_sha: str | None,
    edges_sha: str | None,
    commit_message: str,
) -> tuple[str | None, str | None, str | None]:
    """Write per-thread graph files to GitHub.

    Args:
        client: GitHub API client
        topic: Thread topic identifier
        meta: Thread metadata dict
        entries: List of entry node dicts
        edges: List of edge dicts
        meta_sha: Current meta.json SHA (or None to create)
        entries_sha: Current entries.jsonl SHA (or None to create)
        edges_sha: Current edges.jsonl SHA (or None to create)
        commit_message: Commit message

    Returns:
        Tuple of (new_meta_sha, new_entries_sha, new_edges_sha) or (None, None, None) on error.

    Raises:
        GitHubConflictError: If there's a SHA mismatch (caller should retry)
    """
    meta_path, entries_path, edges_path = _get_per_thread_paths(topic)

    try:
        # Write meta.json (single JSON object, pretty-printed for readability)
        meta_content = json.dumps(meta, indent=2) + "\n"
        new_meta_sha = client.put_file(
            path=meta_path,
            content=meta_content,
            message=commit_message,
            sha=meta_sha,
        )

        # Sort entries by index
        sorted_entries = sorted(entries, key=lambda e: e.get("index", 0))

        # Write entries.jsonl
        entries_content = (
            "\n".join(json.dumps(e, separators=(",", ":")) for e in sorted_entries)
            + "\n"
            if sorted_entries
            else ""
        )
        new_entries_sha = client.put_file(
            path=entries_path,
            content=entries_content,
            message=commit_message,
            sha=entries_sha,
        )

        # Sort edges by source_id, target_id
        sorted_edges = sorted(
            edges, key=lambda e: (e.get("source_id", ""), e.get("target_id", ""))
        )

        # Write edges.jsonl
        edges_content = (
            "\n".join(json.dumps(e, separators=(",", ":")) for e in sorted_edges) + "\n"
            if sorted_edges
            else ""
        )
        new_edges_sha = client.put_file(
            path=edges_path,
            content=edges_content,
            message=commit_message,
            sha=edges_sha,
        )

        return new_meta_sha, new_entries_sha, new_edges_sha

    except GitHubConflictError:
        # Let conflict errors propagate for retry handling
        raise

    except GitHubAPIError as e:
        log_error(f"Failed to write per-thread graph files for {topic}: {e}")
        log_error(
            f"  -> repo={client.repo}, branch={client.branch}, meta_path={meta_path}"
        )
        log_error(
            f"  -> meta_sha={meta_sha}, entries_sha={entries_sha}, edges_sha={edges_sha}"
        )
        return None, None, None


def _build_per_thread_graph_data(
    topic: str,
    status: str,
    ball: str,
    title: str,
    existing_meta: dict | None,
    existing_entries: list[dict],
    existing_edges: list[dict],
    entry_id: str | None = None,
    agent: str | None = None,
    role: str | None = None,
    entry_type: str | None = None,
    entry_title: str | None = None,
    body: str | None = None,
    timestamp: str | None = None,
) -> tuple[dict, list[dict], list[dict]]:
    """Build per-thread graph data structures.

    This is a pure function that builds meta/entries/edges for per-thread format.

    Args:
        topic: Thread topic
        status: Thread status
        ball: Ball owner
        title: Thread title
        existing_meta: Existing meta dict (or None)
        existing_entries: Existing entry nodes
        existing_edges: Existing edges
        entry_id: New entry ID (optional)
        agent: Entry agent (required if entry_id provided)
        role: Entry role
        entry_type: Entry type
        entry_title: Entry title (required if entry_id provided)
        body: Entry body (required if entry_id provided)
        timestamp: Entry timestamp (required if entry_id provided)

    Returns:
        Tuple of (meta, entries, edges)
    """
    thread_id = f"thread:{topic}"
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build/update meta
    entry_count = len(existing_entries)

    # Add new entry if provided
    entries = list(existing_entries)
    edges = list(existing_edges)

    if entry_id and agent and entry_title and body and timestamp:
        entry_node_id = f"entry:{entry_id}"

        # Check if entry already exists (idempotency)
        if not any(e.get("id") == entry_node_id for e in entries):
            # Determine next index
            entry_indices = [e.get("index", 0) for e in entries]
            next_index = max(entry_indices, default=-1) + 1

            # Create entry node
            entry_node = {
                "id": entry_node_id,
                "type": "entry",
                "entry_id": entry_id,
                "thread_topic": topic,
                "index": next_index,
                "agent": agent,
                "role": role or "implementer",
                "entry_type": entry_type or "Note",
                "title": entry_title,
                "body": body,
                "timestamp": timestamp,
            }
            entries.append(entry_node)

            # Add CONTAINS edge (thread -> entry)
            edges.append(
                {
                    "id": f"contains:{thread_id}:{entry_node_id}",
                    "type": "CONTAINS",
                    "source_id": thread_id,
                    "target_id": entry_node_id,
                    "created": timestamp,
                }
            )

            # Add FOLLOWS edge if not first entry
            if next_index > 0:
                prev_entries = [e for e in entries if e.get("index") == next_index - 1]
                if prev_entries:
                    prev_entry = prev_entries[0]
                    edges.append(
                        {
                            "id": f"follows:{prev_entry['id']}:{entry_node_id}",
                            "type": "FOLLOWS",
                            "source_id": prev_entry["id"],
                            "target_id": entry_node_id,
                            "created": timestamp,
                        }
                    )

            entry_count += 1

    # Build meta
    meta = {
        "id": thread_id,
        "type": "thread",
        "topic": topic,
        "title": title,
        "status": status.upper(),
        "ball": ball,
        "created": existing_meta.get("created", now) if existing_meta else now,
        "last_updated": now,
        "entry_count": entry_count,
    }

    return meta, entries, edges


def _upsert_thread_node(
    nodes: list[dict],
    topic: str,
    status: str,
    ball: str,
    title: str | None = None,
    created: str | None = None,
    entry_count: int | None = None,
) -> list[dict]:
    """Create or update a thread node in the nodes list.

    Returns:
        Updated nodes list.
    """
    thread_id = f"thread:{topic}"
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Find existing thread node
    existing_idx = None
    for i, node in enumerate(nodes):
        if node.get("id") == thread_id:
            existing_idx = i
            break

    if existing_idx is not None:
        # Update existing
        node = nodes[existing_idx]
        node["status"] = status.upper()
        node["ball"] = ball
        node["last_updated"] = now
        if title:
            node["title"] = title
        if entry_count is not None:
            node["entry_count"] = entry_count
    else:
        # Create new
        new_node = {
            "id": thread_id,
            "type": "thread",
            "topic": topic,
            "title": title or topic,
            "status": status.upper(),
            "ball": ball,
            "created": created or now,
            "last_updated": now,
            "entry_count": entry_count or 0,
        }
        nodes.append(new_node)

    return nodes


def _add_entry_node(
    nodes: list[dict],
    edges: list[dict],
    topic: str,
    entry_id: str,
    agent: str,
    role: str,
    entry_type: str,
    title: str,
    body: str,
    timestamp: str,
) -> tuple[list[dict], list[dict], int]:
    """Add an entry node and update edges.

    Returns:
        Tuple of (updated_nodes, updated_edges, entry_index).
    """
    thread_id = f"thread:{topic}"
    entry_node_id = f"entry:{entry_id}"

    # Find existing entry count for this thread
    entry_indices = [
        n.get("index", 0)
        for n in nodes
        if n.get("type") == "entry" and n.get("thread_topic") == topic
    ]
    next_index = max(entry_indices, default=-1) + 1

    # Create entry node
    entry_node = {
        "id": entry_node_id,
        "type": "entry",
        "thread_topic": topic,
        "entry_id": entry_id,
        "index": next_index,
        "agent": agent,
        "role": role,
        "entry_type": entry_type,
        "title": title,
        "body": body,
        "timestamp": timestamp,
    }
    nodes.append(entry_node)

    # Add CONTAINS edge (thread -> entry)
    edges.append(
        {
            "id": f"contains:{thread_id}:{entry_node_id}",
            "type": "CONTAINS",
            "source_id": thread_id,
            "target_id": entry_node_id,
            "created": timestamp,
        }
    )

    # Add FOLLOWS edge if not first entry
    if next_index > 0:
        # Find previous entry
        prev_entries = [
            n
            for n in nodes
            if n.get("type") == "entry"
            and n.get("thread_topic") == topic
            and n.get("index") == next_index - 1
        ]
        if prev_entries:
            prev_entry = prev_entries[0]
            edges.append(
                {
                    "id": f"follows:{prev_entry['id']}:{entry_node_id}",
                    "type": "FOLLOWS",
                    "source_id": prev_entry["id"],
                    "target_id": entry_node_id,
                    "created": timestamp,
                }
            )

    return nodes, edges, next_index


def _apply_graph_changes(
    nodes: list[dict],
    edges: list[dict],
    topic: str,
    status: str | None,
    ball: str | None,
    title: str | None,
    entry_id: str | None,
    agent: str | None,
    role: str | None,
    entry_type: str | None,
    entry_title: str | None,
    body: str | None,
    timestamp: str | None,
) -> tuple[list[dict], list[dict]]:
    """Apply graph changes (entry + thread node updates) to nodes/edges.

    This is a pure function that doesn't do I/O - it just modifies the data.
    Separated from I/O to enable retry logic.

    Returns:
        Tuple of (modified_nodes, modified_edges)
    """
    # Get current values from existing thread node
    thread_id = f"thread:{topic}"
    existing_thread = next((n for n in nodes if n.get("id") == thread_id), None)

    current_status = (
        existing_thread.get("status", "OPEN") if existing_thread else "OPEN"
    )
    current_ball = existing_thread.get("ball", "") if existing_thread else ""
    current_title = existing_thread.get("title", topic) if existing_thread else topic

    # Use provided values or fall back to current
    final_status = status if status is not None else current_status
    final_ball = ball if ball is not None else current_ball
    final_title = title if title is not None else current_title

    # Count entries for this thread
    entry_count = len(
        [
            n
            for n in nodes
            if n.get("type") == "entry" and n.get("thread_topic") == topic
        ]
    )

    # Add entry if provided
    if entry_id and agent and entry_title and body and timestamp:
        # Check if entry already exists (for idempotency)
        entry_node_id = f"entry:{entry_id}"
        if not any(n.get("id") == entry_node_id for n in nodes):
            nodes, edges, _ = _add_entry_node(
                nodes,
                edges,
                topic,
                entry_id,
                agent,
                role or "implementer",
                entry_type or "Note",
                entry_title,
                body,
                timestamp,
            )
            entry_count += 1

    # Upsert thread node
    nodes = _upsert_thread_node(
        nodes,
        topic,
        final_status,
        final_ball,
        title=final_title,
        entry_count=entry_count,
    )

    return nodes, edges


def _update_thread_in_graph(
    client: GitHubClient,
    topic: str,
    status: str | None = None,
    ball: str | None = None,
    title: str | None = None,
    entry_id: str | None = None,
    agent: str | None = None,
    role: str | None = None,
    entry_type: str | None = None,
    entry_title: str | None = None,
    body: str | None = None,
    timestamp: str | None = None,
    commit_suffix: str = "",
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> bool:
    """Update thread and optionally add entry in graph files.

    Dual-write strategy:
    1. PRIMARY: Per-thread format (graph/baseline/threads/<topic>/)
    2. SECONDARY: Monolithic format (graph/baseline/nodes.jsonl, edges.jsonl)

    Per-thread format is canonical. Monolithic is maintained for backward
    compatibility during migration transition.

    Includes retry logic for handling concurrent write conflicts (SHA mismatch).

    Args:
        client: GitHub API client
        topic: Thread topic
        status: New status (optional)
        ball: New ball owner (optional)
        title: New title (optional)
        entry_id: Entry ID to add (optional)
        agent: Entry agent (required if entry_id provided)
        role: Entry role (optional, defaults to "implementer")
        entry_type: Entry type (optional, defaults to "Note")
        entry_title: Entry title (required if entry_id provided)
        body: Entry body (required if entry_id provided)
        timestamp: Entry timestamp (required if entry_id provided)
        commit_suffix: Suffix for commit message
        max_retries: Maximum retry attempts for conflicts (default from WATERCOOLER_GRAPH_MAX_RETRIES env)

    Returns:
        True if graph was updated successfully, False otherwise.
    """
    import time

    # Validate topic to prevent path traversal
    _validate_topic(topic)

    per_thread_success = False
    monolithic_success = False

    # ==========================================================================
    # PRIMARY: Per-thread format (canonical)
    # ==========================================================================
    for attempt in range(max_retries):
        try:
            # Read current per-thread state
            (
                existing_meta,
                existing_entries,
                existing_edges,
                meta_sha,
                entries_sha,
                edges_sha,
            ) = _read_per_thread_graph(client, topic)

            # Determine final values (use existing or defaults if not provided)
            final_status = (
                status
                if status is not None
                else (existing_meta.get("status", "OPEN") if existing_meta else "OPEN")
            )
            final_ball = (
                ball
                if ball is not None
                else (existing_meta.get("ball", "") if existing_meta else "")
            )
            final_title = (
                title
                if title is not None
                else (existing_meta.get("title", topic) if existing_meta else topic)
            )

            # Build updated per-thread data
            meta, entries, edges = _build_per_thread_graph_data(
                topic=topic,
                status=final_status,
                ball=final_ball,
                title=final_title,
                existing_meta=existing_meta,
                existing_entries=existing_entries,
                existing_edges=existing_edges,
                entry_id=entry_id,
                agent=agent,
                role=role,
                entry_type=entry_type,
                entry_title=entry_title,
                body=body,
                timestamp=timestamp,
            )

            # Write per-thread graph files
            commit_msg = f"[watercooler] {topic}: graph update{commit_suffix}"
            new_meta_sha, new_entries_sha, new_edges_sha = _write_per_thread_graph(
                client,
                topic,
                meta,
                entries,
                edges,
                meta_sha,
                entries_sha,
                edges_sha,
                commit_msg,
            )

            if new_meta_sha is not None:
                per_thread_success = True
                log_debug(f"Per-thread graph update succeeded for {topic}")
                break

            # Write failed but not due to conflict - don't retry
            log_error(
                f"Per-thread graph update failed for {topic} (attempt {attempt + 1})"
            )
            break

        except GitHubConflictError:
            if attempt < max_retries - 1:
                wait_time = (2**attempt) * 0.1
                log_debug(
                    f"Per-thread graph conflict for {topic}, retrying in {wait_time}s"
                )
                time.sleep(wait_time)
            else:
                log_error(
                    f"Per-thread graph update failed for {topic} after {max_retries} retries"
                )

        except GitHubAPIError as e:
            log_error(f"Per-thread graph update failed for {topic}: {e}")
            break

    # ==========================================================================
    # SECONDARY: Monolithic format (backward compatibility)
    # ==========================================================================
    for attempt in range(max_retries):
        try:
            # Read current monolithic state
            nodes, edges, nodes_sha, edges_sha = _read_graph_files(client)

            # Apply changes to monolithic format
            nodes, edges = _apply_graph_changes(
                nodes,
                edges,
                topic,
                status,
                ball,
                title,
                entry_id,
                agent,
                role,
                entry_type,
                entry_title,
                body,
                timestamp,
            )

            # Write monolithic graph files
            commit_msg = (
                f"[watercooler] {topic}: graph update{commit_suffix} (monolithic)"
            )
            new_nodes_sha, new_edges_sha = _write_graph_files(
                client, nodes, edges, nodes_sha, edges_sha, commit_msg
            )

            if new_nodes_sha is not None:
                monolithic_success = True
                log_debug(f"Monolithic graph update succeeded for {topic}")
                break

            # Write failed but not due to conflict - don't retry
            log_error(
                f"Monolithic graph update failed for {topic} (attempt {attempt + 1})"
            )
            break

        except GitHubConflictError:
            if attempt < max_retries - 1:
                wait_time = (2**attempt) * 0.1
                log_debug(
                    f"Monolithic graph conflict for {topic}, retrying in {wait_time}s"
                )
                time.sleep(wait_time)
            else:
                log_error(
                    f"Monolithic graph update failed for {topic} after {max_retries} retries"
                )

        except GitHubAPIError as e:
            log_error(f"Monolithic graph update failed for {topic}: {e}")
            break

    # Per-thread is canonical - return success if it succeeded
    # Log warning if monolithic failed but per-thread succeeded
    if per_thread_success and not monolithic_success:
        log_debug(
            f"Warning: Per-thread succeeded but monolithic failed for {topic} (non-fatal)"
        )

    return per_thread_success


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
    entry_timestamps = re.findall(
        r"^Entry:\s*[^\s]+\s+(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)", content, re.MULTILINE
    )
    if entry_timestamps:
        last_updated = entry_timestamps[-1]
    else:
        # Try Created: field
        created_match = re.search(r"^Created:\s*(.+)$", header, re.MULTILINE)
        if created_match:
            last_updated = created_match.group(1).strip()

    return (title, status, ball, last_updated)


# ============================================================================
# Slack Sync (via watercooler-site API)
# ============================================================================


def _get_hosted_api_url() -> str:
    """Get hosted API URL from unified config.

    Resolution priority:
    1. WATERCOOLER_TOKEN_API_URL env var
    2. TOML config: [mcp.hosted].api_url
    3. Empty string (disabled)
    """
    url = os.getenv("WATERCOOLER_TOKEN_API_URL", "")
    if url:
        return url

    try:
        from watercooler.config_facade import config
        return config.full().mcp.hosted.api_url or ""
    except ImportError:
        return ""


def _is_slack_sync_enabled() -> bool:
    """Check if Slack sync via watercooler-site is configured.

    Requires both hosted API URL and WATERCOOLER_INTERNAL_SECRET.
    """
    site_url = _get_hosted_api_url()
    # Secret must be env-only for security
    secret = os.getenv("WATERCOOLER_INTERNAL_SECRET", "")
    return bool(site_url) and bool(secret)


def _sync_entry_to_slack_site(
    repo_full_name: str,
    topic: str,
    branch: str,
    entry_id: str,
    agent: str,
    role: str,
    entry_type: str,
    title: str,
    body: str,
    timestamp: str,
) -> bool:
    """Sync entry to Slack via watercooler-site sync-entry API.

    This enables immediate Slack sync after hosted mode writes,
    rather than waiting for the next dashboard polling cycle.

    Args:
        repo_full_name: GitHub repo (e.g., owner/repo-threads)
        topic: Thread topic
        branch: Git branch
        entry_id: Entry ULID
        agent: Agent name (e.g., "Claude (user)")
        role: Agent role (e.g., "implementer")
        entry_type: Entry type (e.g., "Note")
        title: Entry title
        body: Entry body
        timestamp: Entry timestamp (ISO 8601)

    Returns:
        True if synced successfully, False otherwise.
    """
    if not _is_slack_sync_enabled():
        log_debug(
            "Slack sync not enabled (missing hosted API URL or WATERCOOLER_INTERNAL_SECRET)"
        )
        return False

    site_url = _get_hosted_api_url().rstrip("/")
    secret = os.getenv("WATERCOOLER_INTERNAL_SECRET", "")

    url = f"{site_url}/api/slack/sync-entry"

    payload = {
        "repoFullName": repo_full_name,
        "topic": topic,
        "branch": branch,
        "entry": {
            "entryId": entry_id,
            "agent": agent,
            "role": role,
            "entryType": entry_type,
            "title": title,
            "body": body,
            "timestamp": timestamp,
        },
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Watercooler-Secret": secret,
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=10.0) as response:
            result = json.loads(response.read().decode("utf-8"))

        synced = result.get("synced", 0)
        if synced > 0:
            log_debug(f"Slack sync: entry {entry_id[:8]} synced to Slack")
            return True
        else:
            # No Slack mapping for this thread - this is expected for threads not connected to Slack
            log_debug(
                f"Slack sync: no mapping found for {topic} (expected if no Slack thread)"
            )
            return False

    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8") if e.fp else ""
        log_warning(f"Slack sync API error {e.code}: {body_text}")
        return False

    except urllib.error.URLError as e:
        log_warning(f"Slack sync connection error: {e.reason}")
        return False

    except Exception as e:
        log_warning(f"Slack sync unexpected error: {e}")
        return False


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

    entry_id = entry_id or str(ULID())
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # Read per-thread format (canonical, only format supported)
        meta, existing_entries, existing_edges, meta_sha, entries_sha, edges_sha = (
            _read_per_thread_graph(client, topic)
        )

        # If thread doesn't exist, check create_if_missing
        if meta is None:
            if not create_if_missing:
                return (f"Thread '{topic}' not found and create_if_missing=False", {})
            log_debug(f"say_hosted: creating new thread in per-thread format: {topic}")

        # Get or initialize status and ball
        if meta is not None:
            log_debug(f"say_hosted: found thread in per-thread format: {topic}")
            status = meta.get("status", "OPEN")
            old_ball = meta.get("ball", "Agent")
        else:
            # New thread defaults
            status = "OPEN"
            old_ball = ""

        # Determine new ball owner (flip to "other" agent)
        agent_lower = agent.lower()
        old_ball_lower = (old_ball or "").lower()
        if old_ball_lower == agent_lower or not old_ball:
            new_ball = "Agent"  # Default counterpart
        else:
            new_ball = agent  # Give ball to current agent

        # Build updated graph data with new entry
        # Thread title: use existing title if present, otherwise derive from topic
        # (NOT the entry title - that's a separate field)
        thread_title = meta.get("title", topic) if meta else topic
        new_meta, new_entries, new_edges = _build_per_thread_graph_data(
            topic=topic,
            status=status,
            ball=new_ball,
            title=thread_title,
            existing_meta=meta,
            existing_entries=existing_entries,
            existing_edges=existing_edges,
            entry_id=entry_id,
            agent=agent,
            role=role,
            entry_type=entry_type,
            entry_title=title,
            body=body,
            timestamp=timestamp,
        )

        # Write to per-thread format
        commit_message = f"[watercooler] {topic}: {title}\n\nEntry-ID: {entry_id}"
        new_meta_sha, new_entries_sha, new_edges_sha = _write_per_thread_graph(
            client,
            topic=topic,
            meta=new_meta,
            entries=new_entries,
            edges=new_edges,
            meta_sha=meta_sha,
            entries_sha=entries_sha,
            edges_sha=edges_sha,
            commit_message=commit_message,
        )

        if new_meta_sha:
            log_debug(
                f"say_hosted: wrote entry to per-thread format {topic} (meta_sha={new_meta_sha[:8]})"
            )

            # Sync entry to Slack (non-blocking, non-fatal)
            slack_synced = False
            if http_ctx.repo:
                slack_synced = _sync_entry_to_slack_site(
                    repo_full_name=http_ctx.repo,
                    topic=topic,
                    branch=http_ctx.branch or "main",
                    entry_id=entry_id,
                    agent=agent,
                    role=role,
                    entry_type=entry_type,
                    title=title,
                    body=body,
                    timestamp=timestamp,
                )
                if slack_synced:
                    log_debug(f"say_hosted: synced entry to Slack for {topic}")

            log_debug(f"say_hosted: SUCCESS topic={topic}, entry_id={entry_id}")
            return (
                None,
                {
                    "topic": topic,
                    "entry_id": entry_id,
                    "timestamp": timestamp,
                    "status": status,
                    "ball": new_ball,
                    "sha": new_meta_sha,
                    "graph_updated": True,
                    "slack_synced": slack_synced,
                    "format": "per-thread",
                },
            )
        else:
            log_error(f"say_hosted: _write_per_thread_graph failed for {topic}")
            return (f"Failed to write entry to per-thread format for {topic}", {})

    except GitHubAPIError as e:
        log_error(f"say_hosted failed: {e}")
        return (f"GitHub API error: {e}", {})
    except Exception as e:
        log_error(f"say_hosted failed with unexpected error: {e}")
        return (f"Unexpected error: {e}", {})


def set_status_hosted(
    topic: str,
    status: str,
) -> tuple[str | None, dict]:
    """Update thread status using GitHub API (per-thread format only).

    Args:
        topic: Thread topic identifier
        status: New status value

    Returns:
        Tuple of (error_message, result_dict).
    """
    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    try:
        # Read per-thread format (canonical)
        meta, existing_entries, existing_edges, meta_sha, entries_sha, edges_sha = (
            _read_per_thread_graph(client, topic)
        )

        if meta is None:
            return (f"Thread '{topic}' not found", {})

        old_status = meta.get("status", "OPEN")
        ball = meta.get("ball", "Agent")

        # Update status in meta
        new_meta = {**meta, "status": status}

        # Write to per-thread format
        commit_message = f"[watercooler] {topic}: status {old_status} → {status}"
        new_meta_sha, _, _ = _write_per_thread_graph(
            client,
            topic=topic,
            meta=new_meta,
            entries=existing_entries,
            edges=existing_edges,
            meta_sha=meta_sha,
            entries_sha=entries_sha,
            edges_sha=edges_sha,
            commit_message=commit_message,
        )

        if new_meta_sha:
            log_debug(f"set_status_hosted: updated {topic} status to {status}")
            return (
                None,
                {
                    "topic": topic,
                    "old_status": old_status,
                    "new_status": status,
                    "ball": ball,
                    "sha": new_meta_sha,
                    "format": "per-thread",
                },
            )
        else:
            return (f"Failed to write status update for {topic}", {})

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
    """Acknowledge a thread without flipping the ball (per-thread format only).

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

    entry_id = entry_id or str(ULID())
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # Read per-thread format (canonical)
        meta, existing_entries, existing_edges, meta_sha, entries_sha, edges_sha = (
            _read_per_thread_graph(client, topic)
        )

        if meta is None:
            return (f"Thread '{topic}' not found", {})

        status = meta.get("status", "OPEN")
        ball = meta.get("ball", "Agent")  # Ball stays the same for ack

        # Build updated graph data with ack entry (ball unchanged)
        new_meta, new_entries, new_edges = _build_per_thread_graph_data(
            topic=topic,
            status=status,
            ball=ball,  # Keep ball unchanged
            title=meta.get("title", topic),
            existing_meta=meta,
            existing_entries=existing_entries,
            existing_edges=existing_edges,
            entry_id=entry_id,
            agent=agent,
            role="pm",  # Ack entries are typically from PM role
            entry_type="Note",
            entry_title=title,
            body=body,
            timestamp=timestamp,
        )

        # Write to per-thread format
        commit_message = f"[watercooler] {topic}: {title} (ack)\n\nEntry-ID: {entry_id}"
        new_meta_sha, _, _ = _write_per_thread_graph(
            client,
            topic=topic,
            meta=new_meta,
            entries=new_entries,
            edges=new_edges,
            meta_sha=meta_sha,
            entries_sha=entries_sha,
            edges_sha=edges_sha,
            commit_message=commit_message,
        )

        if new_meta_sha:
            log_debug(f"ack_hosted: acknowledged {topic}")
            return (
                None,
                {
                    "topic": topic,
                    "entry_id": entry_id,
                    "timestamp": timestamp,
                    "status": status,
                    "ball": ball,  # Ball unchanged
                    "sha": new_meta_sha,
                    "format": "per-thread",
                },
            )
        else:
            return (f"Failed to write ack entry for {topic}", {})

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
    """Hand off the ball to another agent (per-thread format only).

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

    entry_id = entry_id or str(ULID())
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # Read per-thread format (canonical)
        meta, existing_entries, existing_edges, meta_sha, entries_sha, edges_sha = (
            _read_per_thread_graph(client, topic)
        )

        if meta is None:
            return (f"Thread '{topic}' not found", {})

        status = meta.get("status", "OPEN")
        new_ball = target_agent or "Agent"  # Default to "Agent" if not specified

        # Build updated graph data
        if note:
            # Add handoff entry
            new_meta, new_entries, new_edges = _build_per_thread_graph_data(
                topic=topic,
                status=status,
                ball=new_ball,
                title=meta.get("title", topic),
                existing_meta=meta,
                existing_entries=existing_entries,
                existing_edges=existing_edges,
                entry_id=entry_id,
                agent=agent,
                role="pm",
                entry_type="Note",
                entry_title=f"Handoff to {new_ball}",
                body=note,
                timestamp=timestamp,
            )
        else:
            # Just update ball, no entry
            new_meta = {**meta, "ball": new_ball}
            new_entries = existing_entries
            new_edges = existing_edges

        # Write to per-thread format
        commit_message = f"[watercooler] {topic}: handoff to {new_ball}"
        if note:
            commit_message += f"\n\nEntry-ID: {entry_id}"
        new_meta_sha, _, _ = _write_per_thread_graph(
            client,
            topic=topic,
            meta=new_meta,
            entries=new_entries,
            edges=new_edges,
            meta_sha=meta_sha,
            entries_sha=entries_sha,
            edges_sha=edges_sha,
            commit_message=commit_message,
        )

        if new_meta_sha:
            log_debug(f"handoff_hosted: handed off {topic} to {new_ball}")
            return (
                None,
                {
                    "topic": topic,
                    "from_agent": agent,
                    "to_agent": new_ball,
                    "entry_id": entry_id if note else None,
                    "timestamp": timestamp,
                    "status": status,
                    "ball": new_ball,
                    "sha": new_meta_sha,
                    "format": "per-thread",
                },
            )
        else:
            return (f"Failed to write handoff for {topic}", {})

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


# ============================================================================
# Hosted Reconciliation
# ============================================================================


def reconcile_thread_hosted(topic: str) -> tuple[str | None, dict]:
    """Reconcile a single thread's graph data from its markdown file via GitHub API.

    Dual-write strategy:
    1. PRIMARY: Per-thread format (graph/baseline/threads/<topic>/)
    2. SECONDARY: Monolithic format (graph/baseline/nodes.jsonl, edges.jsonl)

    This is the hosted equivalent of reconcile_graph for a single topic. It:
    1. Reads the markdown file from GitHub
    2. Parses entries and metadata
    3. Rebuilds graph nodes/edges
    4. Writes graph files to both formats

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (error_message, result_dict). If error_message is not None,
        result_dict will be empty.
    """
    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    file_path = f"{topic}.md"
    thread_id = f"thread:{topic}"

    try:
        # 1. Read markdown file
        try:
            file_content = client.get_file(file_path)
            content = file_content.content
        except GitHubNotFoundError:
            return (f"Thread '{topic}' not found", {})

        # 2. Parse metadata and entries
        title, status, ball, last_updated = _extract_thread_metadata(content, topic)
        parsed_entries = parse_thread_entries(content)
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        # ======================================================================
        # Build per-thread format data
        # ======================================================================
        per_thread_meta = {
            "id": thread_id,
            "type": "thread",
            "topic": topic,
            "title": title,
            "status": status.upper(),
            "ball": ball,
            "created": last_updated or now,  # Use first entry timestamp ideally
            "last_updated": last_updated or now,
            "entry_count": len(parsed_entries),
        }

        per_thread_entries: list[dict] = []
        per_thread_edges: list[dict] = []
        prev_entry_node_id: str | None = None

        for entry in parsed_entries:
            entry_id = entry.entry_id or f"{topic}:{entry.index}"
            entry_node_id = f"entry:{entry_id}"

            entry_node = {
                "id": entry_node_id,
                "type": "entry",
                "entry_id": entry_id,
                "thread_topic": topic,
                "index": entry.index,
                "agent": entry.agent,
                "role": entry.role,
                "entry_type": entry.entry_type,
                "title": entry.title,
                "body": entry.body,
                "timestamp": entry.timestamp or "",
            }
            per_thread_entries.append(entry_node)

            # CONTAINS edge
            per_thread_edges.append(
                {
                    "id": f"contains:{thread_id}:{entry_node_id}",
                    "type": "CONTAINS",
                    "source_id": thread_id,
                    "target_id": entry_node_id,
                    "created": entry.timestamp or "",
                }
            )

            # FOLLOWS edge
            if prev_entry_node_id:
                per_thread_edges.append(
                    {
                        "id": f"follows:{prev_entry_node_id}:{entry_node_id}",
                        "type": "FOLLOWS",
                        "source_id": prev_entry_node_id,
                        "target_id": entry_node_id,
                        "created": entry.timestamp or "",
                    }
                )

            prev_entry_node_id = entry_node_id

        # ======================================================================
        # PRIMARY: Write per-thread format
        # ======================================================================
        per_thread_success = False
        _, _, _, meta_sha, entries_sha, edges_sha = _read_per_thread_graph(
            client, topic
        )

        commit_msg = f"[watercooler] reconcile: {topic}"
        try:
            new_meta_sha, new_entries_sha, new_edges_sha = _write_per_thread_graph(
                client,
                topic,
                per_thread_meta,
                per_thread_entries,
                per_thread_edges,
                meta_sha,
                entries_sha,
                edges_sha,
                commit_msg,
            )
            if new_meta_sha is not None:
                per_thread_success = True
                log_debug(
                    f"reconcile_thread_hosted: per-thread format written for {topic}"
                )
        except GitHubAPIError as e:
            log_error(
                f"reconcile_thread_hosted: per-thread write failed for {topic}: {e}"
            )

        # ======================================================================
        # SECONDARY: Write monolithic format (backward compatibility)
        # ======================================================================
        monolithic_success = False

        # Read current monolithic graph files
        nodes, edges, nodes_sha, edges_sha = _read_graph_files(client)

        # Remove existing nodes/edges for this thread from monolithic
        nodes = [
            n
            for n in nodes
            if n.get("id") != thread_id and n.get("thread_topic") != topic
        ]
        # Filter edges more carefully - remove edges related to this thread
        old_entry_ids = {
            f"entry:{e.entry_id or f'{topic}:{e.index}'}" for e in parsed_entries
        }
        edges = [
            e
            for e in edges
            if not (
                e.get("source_id") == thread_id
                or e.get("target_id") == thread_id
                or e.get("source_id") in old_entry_ids
                or e.get("target_id") in old_entry_ids
            )
        ]

        # Build monolithic thread node (with summary field for compatibility)
        thread_node = {
            "id": thread_id,
            "type": "thread",
            "topic": topic,
            "title": title,
            "status": status.upper(),
            "ball": ball,
            "last_updated": last_updated or now,
            "entry_count": len(parsed_entries),
            "summary": "",
        }
        nodes.append(thread_node)

        # Add entry nodes to monolithic (with summary field)
        for entry_node in per_thread_entries:
            monolithic_entry = dict(entry_node)
            monolithic_entry["summary"] = ""
            nodes.append(monolithic_entry)

        # Add all edges to monolithic
        edges.extend(per_thread_edges)

        try:
            new_nodes_sha, new_edges_sha = _write_graph_files(
                client, nodes, edges, nodes_sha, edges_sha, f"{commit_msg} (monolithic)"
            )
            if new_nodes_sha is not None:
                monolithic_success = True
                log_debug(
                    f"reconcile_thread_hosted: monolithic format written for {topic}"
                )
        except GitHubAPIError as e:
            log_error(
                f"reconcile_thread_hosted: monolithic write failed for {topic}: {e}"
            )

        # Per-thread is canonical
        if not per_thread_success:
            return ("Failed to write per-thread graph files", {})

        if not monolithic_success:
            log_debug(
                f"Warning: Per-thread succeeded but monolithic failed for {topic} (non-fatal)"
            )

        log_debug(
            f"reconcile_thread_hosted: reconciled {topic} ({len(parsed_entries)} entries)"
        )

        return (
            None,
            {
                "topic": topic,
                "entry_count": len(parsed_entries),
                "status": status,
                "ball": ball,
                "last_updated": last_updated,
                "per_thread_success": per_thread_success,
                "monolithic_success": monolithic_success,
            },
        )

    except GitHubAPIError as e:
        log_error(f"reconcile_thread_hosted failed: {e}")
        return (f"GitHub API error: {e}", {})


def reconcile_graph_hosted(
    topics: list[str] | None = None,
) -> tuple[str | None, dict]:
    """Reconcile graph data from markdown files via GitHub API.

    This is the hosted equivalent of reconcile_graph. It:
    1. Lists all markdown thread files (or uses provided topics)
    2. For each thread, rebuilds graph data from markdown
    3. Writes updated graph files to GitHub

    Args:
        topics: Optional list of topics to reconcile. If None, reconciles all threads.

    Returns:
        Tuple of (error_message, result_dict). If error_message is not None,
        result_dict will be empty.
    """
    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    try:
        # Get topics to reconcile
        if topics is None:
            # List all .md files in root
            files = client.list_files("")
            md_files = [f for f in files if f.name.endswith(".md") and f.type == "file"]
            topics = []
            for file_info in md_files:
                topic = file_info.name[:-3]  # Remove .md extension
                # Skip non-thread files
                if topic.lower() not in (
                    "readme",
                    "contributing",
                    "license",
                    "changelog",
                ):
                    topics.append(topic)

        # Reconcile each topic
        results: dict[str, dict] = {}
        errors: dict[str, str] = {}

        for topic in topics:
            err, result = reconcile_thread_hosted(topic)
            if err:
                errors[topic] = err
            else:
                results[topic] = result

        successes = len(results)
        failures = len(errors)

        log_debug(f"reconcile_graph_hosted: {successes} succeeded, {failures} failed")

        return (
            None,
            {
                "total": len(topics),
                "successes": successes,
                "failures": failures,
                "success_topics": list(results.keys()),
                "failure_topics": list(errors.keys()),
                "errors": errors,
            },
        )

    except GitHubAPIError as e:
        log_error(f"reconcile_graph_hosted failed: {e}")
        return (f"GitHub API error: {e}", {})
