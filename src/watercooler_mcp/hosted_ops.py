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
import random
import re
import time as _time_mod
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from watercooler.config_facade import config
from watercooler.thread_entries import parse_thread_entries, ThreadEntry

from .context import get_effective_context, get_http_context, HttpRequestContext
from .github_api import (
    GitHubClient,
    GitHubNotFoundError,
    GitHubAPIError,
    GitHubConflictError,
)
from .config import ORPHAN_BRANCH_NAME
from .observability import log_debug, log_error, log_warning
from .request_trace import trace_stage

# Per-thread graph directory
GRAPH_THREADS_DIR = "graph/baseline/threads"


def _validate_topic(topic: str) -> str | None:
    """Validate topic for safe use in file paths. Returns error message or None."""
    if not topic or not topic.strip():
        return "Topic is required (cannot be empty or whitespace)"
    if ".." in topic or "/" in topic or "\\" in topic:
        return f"Invalid topic: contains path traversal characters: {topic!r}"
    if topic.startswith("."):
        return f"Invalid topic: starts with dot: {topic!r}"
    # Reject null bytes and control characters (log injection / path tricks).
    if any(ord(ch) < 0x20 for ch in topic) or "\x00" in topic:
        return f"Invalid topic: contains control characters: {topic!r}"
    return None

# Default retry count for conflict handling (configurable via env)
DEFAULT_MAX_RETRIES = max(1, int(os.getenv("WATERCOOLER_GRAPH_MAX_RETRIES", "3")))


def _jittered_backoff(attempt: int, base: float = 0.1, cap: float = 5.0) -> float:
    """Calculate exponential backoff with full jitter (thundering herd prevention).

    Uses the "full jitter" strategy from AWS Architecture Blog:
    sleep = random(0, min(cap, base * 2^attempt))

    Args:
        attempt: Zero-based retry attempt number
        base: Base delay in seconds
        cap: Maximum delay cap in seconds

    Returns:
        Jittered delay in seconds
    """
    exp_delay = min(cap, base * (2 ** attempt))
    return random.uniform(0, exp_delay)


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
    """Get GitHubClient from the best available context.

    Uses get_effective_context() so this works in both HTTP request
    handlers and daemon background threads (which use worker context).

    Returns:
        Tuple of (error_message, client). If error_message is not None,
        client will be None.
    """
    import sys

    http_ctx = get_effective_context()

    if not http_ctx:
        return ("No HTTP context available for hosted mode", None)

    if not http_ctx.repo:
        return ("No repository specified in HTTP context", None)

    # Resolve GitHub token: prefer context token, fall back to token service.
    # Daemon threads may have user_id but no token in their scope context;
    # the token service can resolve a fresh token from the user's stored creds.
    token = http_ctx.github_token
    if not token and http_ctx.user_id:
        try:
            from .auth import get_github_token as _resolve_token
            token_info = _resolve_token(http_ctx.user_id)
            if token_info:
                token = token_info.token
                logger.debug(
                    "Resolved GitHub token via token service for user %s",
                    http_ctx.user_id,
                )
        except Exception as exc:
            logger.debug(
                "Token service fallback failed for user %s: %s",
                http_ctx.user_id, exc,
            )

    if not token:
        return ("No GitHub token available for hosted mode", None)

    # Use repo directly - dashboard sends the threads repo name
    # (e.g., "org/repo-threads", not "org/repo")
    threads_repo = http_ctx.repo

    client = GitHubClient(
        token=token,
        repo=threads_repo,
        branch=ORPHAN_BRANCH_NAME,
    )
    return (None, client)


def list_threads_hosted(
    open_only: bool | None = None,
) -> tuple[str | None, list[HostedThread]]:
    """List threads from GitHub repository (per-thread format).

    Uses concurrent reads to fetch per-thread meta.json files in parallel,
    avoiding the N+1 sequential API call bottleneck that causes timeouts
    on repositories with many threads.

    Args:
        open_only: Filter by status (True=open only, False=closed only, None=all)

    Returns:
        Tuple of (error_message, threads). If error_message is not None,
        threads will be empty.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", [])

    with trace_stage("tool.github.list_threads"):
        try:
            # List directories in graph/baseline/threads/
            try:
                items = client.list_files(GRAPH_THREADS_DIR)
            except GitHubNotFoundError:
                # No threads directory yet
                log_debug("list_threads_hosted: threads directory not found")
                return (None, [])

            thread_dirs = [f for f in items if f.type == "dir"]

            def _read_thread_meta(topic: str) -> HostedThread | None:
                """Read a single thread's meta.json. Returns None on error."""
                try:
                    with trace_stage("tool.github.load_thread_meta"):
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
                        return None
                    if open_only is False and status.upper() == "OPEN":
                        return None

                    return HostedThread(
                        topic=topic,
                        title=title,
                        status=status,
                        ball=ball,
                        last_updated=last_updated,
                        entry_count=entry_count,
                    )
                except GitHubNotFoundError:
                    log_debug(f"No meta.json for thread {topic}, skipping")
                    return None
                except GitHubAPIError as e:
                    log_debug(f"Error reading thread {topic}: {e}")
                    return None
                except json.JSONDecodeError as e:
                    log_debug(f"Invalid meta.json for thread {topic}: {e}")
                    return None
                except Exception as e:
                    log_debug(f"Unexpected error reading thread {topic}: {e}")
                    return None

            # Read all meta.json files concurrently (10 parallel workers)
            threads: list[HostedThread] = []
            max_workers = min(10, len(thread_dirs)) if thread_dirs else 1
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_read_thread_meta, d.name): d.name
                    for d in thread_dirs
                }
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except Exception:
                        # Unexpected errors should not fail the entire listing
                        continue
                    if result is not None:
                        threads.append(result)

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
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, "")

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
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, [])

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
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, {})

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
    topic_err = _validate_topic(topic)
    if topic_err:
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
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, "")

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
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, "")

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


# ============================================================================
# Per-Thread Graph Operations
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


def _write_md_projection(
    client: GitHubClient,
    topic: str,
    meta: dict,
    entries: list[dict],
    commit_message: str,
) -> str | None:
    """Write .md projection file from graph data via GitHub API.

    The .md file is a write-only projection for human review and git diffs.
    The graph (JSON) remains the source of truth.

    Args:
        client: GitHub API client
        topic: Thread topic identifier
        meta: Thread metadata dict
        entries: List of entry node dicts
        commit_message: Commit message for the write

    Returns:
        New file SHA on success, None on failure.
    """
    md_path = f"threads/{topic}.md"
    md_content = _reconstruct_markdown_from_graph(meta, entries)

    # Read existing .md SHA (if file already exists, we need it for update)
    md_sha: str | None = None
    try:
        existing = client.get_file(md_path)
        md_sha = existing.sha
    except GitHubNotFoundError:
        pass  # New file, no SHA needed

    try:
        new_sha = client.put_file(
            path=md_path,
            content=md_content,
            message=commit_message,
            sha=md_sha,
        )
        log_debug(f"Wrote .md projection for {topic} (sha={new_sha[:8] if new_sha else '?'})")
        return new_sha
    except GitHubConflictError:
        # .md is a projection — conflict is non-fatal, will be correct on next write
        log_warning(f"Conflict writing .md projection for {topic}, skipping (non-fatal)")
        return None
    except GitHubAPIError as e:
        log_warning(f"Failed to write .md projection for {topic}: {e}")
        return None


def _enrich_entry_hosted(
    client: GitHubClient,
    topic: str,
    entry_id: str,
    body: str,
    title: str,
    entry_type: str,
    entries: list[dict],
    entries_sha: str | None,
    commit_message: str,
) -> bool:
    """Enrich a new entry with summary and embedding, writing back via GitHub API.

    Best-effort: failures are logged but do not propagate. The graph data
    written by the caller is already committed; this adds optional enrichment.

    Args:
        client: GitHub API client
        topic: Thread topic identifier
        entry_id: Entry ID to enrich
        body: Entry body text
        title: Entry title
        entry_type: Entry type (Note, Plan, etc.)
        entries: Full entries list (already includes the new entry)
        entries_sha: Current entries.jsonl SHA after the graph write
        commit_message: Base commit message

    Returns:
        True if any enrichment was generated, False otherwise.
    """
    from watercooler.baseline_graph.summarizer import (
        create_summarizer_config,
        is_llm_service_available,
        summarize_entry,
    )
    from watercooler.baseline_graph.sync import (
        EmbeddingConfig,
        generate_embedding,
        is_embedding_available,
    )

    summary_generated = False
    embedding_generated = False
    new_summary = ""
    new_embedding = None

    # Generate summary
    try:
        summarizer_config = create_summarizer_config()
        if is_llm_service_available(summarizer_config):
            new_summary = summarize_entry(
                body,
                entry_title=title,
                entry_type=entry_type,
                config=summarizer_config,
            )
            if new_summary:
                summary_generated = True
                log_debug(f"enrich_entry_hosted: generated summary for {entry_id}")
        else:
            log_debug(f"enrich_entry_hosted: LLM service unavailable, skipping summary")
    except Exception as e:
        log_warning(f"enrich_entry_hosted: summary generation failed for {entry_id}: {e}")

    # Generate embedding
    try:
        embed_config = EmbeddingConfig.from_env()
        if is_embedding_available(embed_config):
            embed_text = new_summary if new_summary else body[:embed_config.max_text_chars]
            new_embedding = generate_embedding(embed_text, config=embed_config)
            if new_embedding:
                embedding_generated = True
                log_debug(f"enrich_entry_hosted: generated embedding for {entry_id}")
        else:
            log_debug(f"enrich_entry_hosted: embedding service unavailable, skipping")
    except Exception as e:
        log_warning(f"enrich_entry_hosted: embedding generation failed for {entry_id}: {e}")

    if not summary_generated and not embedding_generated:
        return False

    # Update the entry in the entries list and write back
    updated = False
    for entry in entries:
        if entry.get("entry_id") == entry_id:
            if new_summary:
                entry["summary"] = new_summary
            if new_embedding:
                entry["embedding"] = new_embedding
            updated = True
            break

    if not updated:
        log_warning(f"enrich_entry_hosted: entry {entry_id} not found in entries list")
        return False

    # Write updated entries.jsonl back to GitHub
    _, entries_path, _ = _get_per_thread_paths(topic)
    sorted_entries = sorted(entries, key=lambda e: e.get("index", 0))
    entries_content = (
        "\n".join(json.dumps(e, separators=(",", ":")) for e in sorted_entries)
        + "\n"
        if sorted_entries
        else ""
    )

    try:
        client.put_file(
            path=entries_path,
            content=entries_content,
            message=f"{commit_message}\n\nEnrichment: summary={'yes' if summary_generated else 'no'}, embedding={'yes' if embedding_generated else 'no'}",
            sha=entries_sha,
        )
        log_debug(
            f"enrich_entry_hosted: wrote enrichment for {entry_id} "
            f"(summary={summary_generated}, embedding={embedding_generated})"
        )
        return True
    except (GitHubConflictError, GitHubAPIError) as e:
        log_warning(f"enrich_entry_hosted: failed to write enrichment for {entry_id}: {e}")
        return False


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
    code_branch: str | None = None,
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
        code_branch: Code branch this entry was created in context of

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
            if code_branch:
                entry_node["code_branch"] = code_branch
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

    # Preserve annotation state from existing meta. Annotation state is kept
    # current in meta.json by _sync_annotations_to_meta_hosted() on every
    # annotation write, so existing_meta.annotations reflects the latest state.
    if existing_meta and existing_meta.get("annotations"):
        meta["annotations"] = existing_meta["annotations"]

    return meta, entries, edges


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
    """Update thread and optionally add entry in per-thread graph files.

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
    topic_err = _validate_topic(topic)
    if topic_err:
        logger.warning("Topic validation failed: %s", topic_err)
        return False

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
                log_debug(f"Per-thread graph update succeeded for {topic}")
                return True

            # Write failed but not due to conflict - don't retry
            log_error(
                f"Per-thread graph update failed for {topic} (attempt {attempt + 1})"
            )
            return False

        except GitHubConflictError:
            if attempt < max_retries - 1:
                wait_time = _jittered_backoff(attempt)
                log_debug(
                    f"Per-thread graph conflict for {topic}, retrying in {wait_time:.3f}s"
                )
                _time_mod.sleep(wait_time)
            else:
                log_error(
                    f"Per-thread graph update failed for {topic} after {max_retries} retries"
                )

        except GitHubAPIError as e:
            log_error(f"Per-thread graph update failed for {topic}: {e}")
            return False

    return False


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
    code_branch: Optional[str] = None,
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
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, {})

    from ulid import ULID

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    http_ctx = get_http_context()
    if not http_ctx:
        return ("No HTTP context available", {})

    entry_id = entry_id or str(ULID())
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    import time as _time_say

    for attempt in range(DEFAULT_MAX_RETRIES):
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
            # Resolve code_branch: explicit param > HTTP context > None
            effective_code_branch = code_branch
            if not effective_code_branch and http_ctx:
                effective_code_branch = http_ctx.effective_branch

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
                code_branch=effective_code_branch,
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

                # Project .md from graph data (write-only projection for human review)
                md_projected = False
                try:
                    md_sha = _write_md_projection(
                        client, topic, new_meta, new_entries, commit_message,
                    )
                    md_projected = md_sha is not None
                except Exception as e:
                    log_warning(f"say_hosted: .md projection failed for {topic}: {e}")

                # Enrich new entry (summary + embedding) — best-effort
                enriched = False
                try:
                    enriched = _enrich_entry_hosted(
                        client,
                        topic=topic,
                        entry_id=entry_id,
                        body=body,
                        title=title,
                        entry_type=entry_type,
                        entries=new_entries,
                        entries_sha=new_entries_sha,
                        commit_message=commit_message,
                    )
                except Exception as e:
                    log_warning(f"say_hosted: enrichment failed for {topic}/{entry_id}: {e}")

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
                        "md_projected": md_projected,
                        "enriched": enriched,
                        "slack_synced": slack_synced,
                        "format": "per-thread",
                    },
                )
            else:
                log_error(f"say_hosted: _write_per_thread_graph failed for {topic}")
                return (f"Failed to write entry to per-thread format for {topic}", {})

        except GitHubConflictError:
            if attempt < DEFAULT_MAX_RETRIES - 1:
                wait_time = _jittered_backoff(attempt)
                log_debug(
                    f"say_hosted: conflict for {topic}, retrying in {wait_time:.3f}s "
                    f"(attempt {attempt + 1}/{DEFAULT_MAX_RETRIES})"
                )
                _time_say.sleep(wait_time)
            else:
                log_error(
                    f"say_hosted: conflict persisted for {topic} after "
                    f"{DEFAULT_MAX_RETRIES} retries"
                )
                return (
                    f"Write conflict for thread '{topic}' after "
                    f"{DEFAULT_MAX_RETRIES} retries (concurrent write detected)",
                    {},
                )

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
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, {})

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    import time as _time_ss

    for attempt in range(DEFAULT_MAX_RETRIES):
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

                # Project .md from graph data (non-fatal)
                try:
                    _write_md_projection(
                        client, topic, new_meta, existing_entries, commit_message,
                    )
                except Exception as e:
                    log_warning(f"set_status_hosted: .md projection failed for {topic}: {e}")

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

        except GitHubConflictError:
            if attempt < DEFAULT_MAX_RETRIES - 1:
                wait_time = _jittered_backoff(attempt)
                log_debug(
                    f"set_status_hosted: conflict for {topic}, retrying in {wait_time:.3f}s"
                )
                _time_ss.sleep(wait_time)
            else:
                return (
                    f"Write conflict for thread '{topic}' after "
                    f"{DEFAULT_MAX_RETRIES} retries",
                    {},
                )

        except GitHubAPIError as e:
            log_error(f"set_status_hosted failed: {e}")
            return (f"GitHub API error: {e}", {})
        except Exception as e:
            log_error(f"set_status_hosted failed with unexpected error: {e}")
            return (f"Unexpected error: {e}", {})


def ack_hosted(
    topic: str,
    agent: str,
    title: str = "Ack",
    body: str = "Acknowledged",
    entry_id: Optional[str] = None,
    code_branch: Optional[str] = None,
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
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, {})

    from ulid import ULID

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    entry_id = entry_id or str(ULID())
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    import time as _time_ack

    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            # Read per-thread format (canonical)
            meta, existing_entries, existing_edges, meta_sha, entries_sha, edges_sha = (
                _read_per_thread_graph(client, topic)
            )

            if meta is None:
                return (f"Thread '{topic}' not found", {})

            status = meta.get("status", "OPEN")
            ball = meta.get("ball", "Agent")  # Ball stays the same for ack

            # Resolve code_branch: explicit param > HTTP context > None
            http_ctx = get_http_context()
            effective_code_branch = code_branch
            if not effective_code_branch and http_ctx:
                effective_code_branch = http_ctx.effective_branch

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
                code_branch=effective_code_branch,
            )

            # Write to per-thread format
            commit_message = f"[watercooler] {topic}: {title} (ack)\n\nEntry-ID: {entry_id}"
            new_meta_sha, new_entries_sha, _ = _write_per_thread_graph(
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

                # Project .md from graph data (non-fatal)
                try:
                    _write_md_projection(
                        client, topic, new_meta, new_entries, commit_message,
                    )
                except Exception as e:
                    log_warning(f"ack_hosted: .md projection failed for {topic}: {e}")

                # Enrich new entry (summary + embedding) — best-effort
                try:
                    _enrich_entry_hosted(
                        client,
                        topic=topic,
                        entry_id=entry_id,
                        body=body,
                        title=title,
                        entry_type="Note",
                        entries=new_entries,
                        entries_sha=new_entries_sha,
                        commit_message=commit_message,
                    )
                except Exception as e:
                    log_warning(f"ack_hosted: enrichment failed for {topic}/{entry_id}: {e}")

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

        except GitHubConflictError:
            if attempt < DEFAULT_MAX_RETRIES - 1:
                wait_time = _jittered_backoff(attempt)
                log_debug(
                    f"ack_hosted: conflict for {topic}, retrying in {wait_time:.3f}s"
                )
                _time_ack.sleep(wait_time)
            else:
                return (
                    f"Write conflict for thread '{topic}' after "
                    f"{DEFAULT_MAX_RETRIES} retries",
                    {},
                )

        except GitHubAPIError as e:
            log_error(f"ack_hosted failed: {e}")
            return (f"GitHub API error: {e}", {})
        except Exception as e:
            log_error(f"ack_hosted failed with unexpected error: {e}")
            return (f"Unexpected error: {e}", {})


def handoff_hosted(
    topic: str,
    agent: str,
    target_agent: Optional[str] = None,
    note: str = "",
    entry_id: Optional[str] = None,
    code_branch: Optional[str] = None,
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
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, {})

    from ulid import ULID

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    entry_id = entry_id or str(ULID())
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    import time as _time_ho

    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            # Read per-thread format (canonical)
            meta, existing_entries, existing_edges, meta_sha, entries_sha, edges_sha = (
                _read_per_thread_graph(client, topic)
            )

            if meta is None:
                return (f"Thread '{topic}' not found", {})

            status = meta.get("status", "OPEN")
            new_ball = target_agent or "Agent"  # Default to "Agent" if not specified

            # Resolve code_branch: explicit param > HTTP context > None
            http_ctx_local = get_http_context()
            effective_code_branch = code_branch
            if not effective_code_branch and http_ctx_local:
                effective_code_branch = http_ctx_local.effective_branch

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
                    code_branch=effective_code_branch,
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
            new_meta_sha, new_entries_sha, _ = _write_per_thread_graph(
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

                # Project .md from graph data (non-fatal)
                try:
                    _write_md_projection(
                        client, topic, new_meta, new_entries, commit_message,
                    )
                except Exception as e:
                    log_warning(f"handoff_hosted: .md projection failed for {topic}: {e}")

                # Enrich handoff entry if one was created (best-effort)
                if note:
                    try:
                        _enrich_entry_hosted(
                            client,
                            topic=topic,
                            entry_id=entry_id,
                            body=note,
                            title=f"Handoff to {new_ball}",
                            entry_type="Note",
                            entries=new_entries,
                            entries_sha=new_entries_sha,
                            commit_message=commit_message,
                        )
                    except Exception as e:
                        log_warning(f"handoff_hosted: enrichment failed for {topic}/{entry_id}: {e}")

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

        except GitHubConflictError:
            if attempt < DEFAULT_MAX_RETRIES - 1:
                wait_time = _jittered_backoff(attempt)
                log_debug(
                    f"handoff_hosted: conflict for {topic}, retrying in {wait_time:.3f}s"
                )
                _time_ho.sleep(wait_time)
            else:
                return (
                    f"Write conflict for thread '{topic}' after "
                    f"{DEFAULT_MAX_RETRIES} retries",
                    {},
                )

        except GitHubAPIError as e:
            log_error(f"handoff_hosted failed: {e}")
            return (f"GitHub API error: {e}", {})
        except Exception as e:
            log_error(f"handoff_hosted failed with unexpected error: {e}")
            return (f"Unexpected error: {e}", {})


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

    This is the hosted equivalent of reconcile_graph for a single topic. It:
    1. Reads the markdown file from GitHub
    2. Parses entries and metadata
    3. Rebuilds per-thread graph nodes/edges
    4. Writes graph files

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

        # Write per-thread format
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
            if new_meta_sha is None:
                return ("Failed to write per-thread graph files", {})
            log_debug(
                f"reconcile_thread_hosted: per-thread format written for {topic}"
            )
        except GitHubAPIError as e:
            log_error(
                f"reconcile_thread_hosted: per-thread write failed for {topic}: {e}"
            )
            return ("Failed to write per-thread graph files", {})

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


# ============================================================================
# Hosted Annotation Operations
# ============================================================================


def get_annotations_hosted(
    topic: str,
    target_id: str = "",
) -> tuple[str | None, dict]:
    """Read annotation state from GitHub (hosted mode).

    Reads annotations.jsonl from the orphan branch and materializes state.

    Args:
        topic: Thread topic identifier
        target_id: Specific target ID, or empty for all targets

    Returns:
        Tuple of (error_message, result_dict).
    """
    from watercooler.baseline_graph.annotations import (
        AnnotationEvent,
        materialize_state,
        materialize_all_states,
    )

    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, {})

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    ann_path = f"{GRAPH_THREADS_DIR}/{topic}/annotations.jsonl"

    try:
        file_content = client.get_file(ann_path)
        content = file_content.content
    except GitHubNotFoundError:
        # No annotations file yet — return empty state
        content = ""
    except GitHubAPIError as e:
        return (f"GitHub API error reading annotations: {e}", {})

    # Parse JSONL events
    events: list[AnnotationEvent] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            events.append(AnnotationEvent(**data))
        except (json.JSONDecodeError, TypeError):
            continue

    if target_id:
        state = materialize_state(events, target_id)
        return (None, {
            "target_id": target_id,
            "annotation_state": state.to_dict(),
        })
    else:
        states = materialize_all_states(events)
        return (None, {
            "topic": topic,
            "annotation_states": {
                tid: s.to_dict() for tid, s in states.items()
            },
        })


def _sync_annotations_to_meta_hosted(
    client: "GitHubClient",
    topic: str,
    annotations_content: str,
) -> None:
    """Re-embed materialized annotation state into meta.json on GitHub.

    Called after annotation writes so the graph node includes up-to-date
    annotation state for the dashboard sync pipeline.
    """
    from watercooler.baseline_graph.annotations import (
        AnnotationEvent,
        materialize_all_states,
    )

    # Materialize state from the full annotations content
    events: list[AnnotationEvent] = []
    for line in annotations_content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            events.append(AnnotationEvent(**data))
        except (json.JSONDecodeError, TypeError):
            continue

    states = materialize_all_states(events)
    ann_dict = {tid: s.to_dict() for tid, s in states.items()}

    # Read-modify-write meta.json with conflict retry
    meta_path = f"{GRAPH_THREADS_DIR}/{topic}/meta.json"
    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            file_content = client.get_file(meta_path)
            meta = json.loads(file_content.content)
            meta_sha = file_content.sha
        except (GitHubNotFoundError, json.JSONDecodeError):
            return  # No meta.json to update

        meta["annotations"] = ann_dict

        try:
            client.put_file(
                path=meta_path,
                content=json.dumps(meta, separators=(",", ":")),
                message=f"sync annotation state to meta: {topic}",
                sha=meta_sha,
            )
            return  # Success
        except GitHubConflictError:
            if attempt < DEFAULT_MAX_RETRIES - 1:
                log_debug(f"_sync_annotations_to_meta_hosted: conflict on attempt {attempt + 1}, retrying")
                continue
            log_debug(f"_sync_annotations_to_meta_hosted: conflict after {DEFAULT_MAX_RETRIES} attempts for {topic}")
        except GitHubAPIError as e:
            log_debug(f"_sync_annotations_to_meta_hosted: API error for {topic}: {e}")
            return


def append_annotation_hosted(
    topic: str,
    event_dict: dict,
) -> tuple[str | None, str]:
    """Append an annotation event to annotations.jsonl on GitHub.

    Args:
        topic: Thread topic identifier
        event_dict: Serialized AnnotationEvent dict

    Returns:
        Tuple of (error_message, new_sha).
    """
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, "")

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", "")

    ann_path = f"{GRAPH_THREADS_DIR}/{topic}/annotations.jsonl"
    new_line = json.dumps(event_dict, separators=(",", ":")) + "\n"

    # Read-modify-write with conflict retry
    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            # Read current file
            try:
                file_content = client.get_file(ann_path)
                current_content = file_content.content
                sha = file_content.sha
            except GitHubNotFoundError:
                current_content = ""
                sha = None

            # Append new event
            updated_content = current_content + new_line

            new_sha = client.put_file(
                path=ann_path,
                content=updated_content,
                message=f"annotate: {event_dict.get('kind', '?')} on {topic}",
                sha=sha,
            )

            # Re-embed materialized annotation state into meta.json so the
            # graph node stays current for the dashboard sync pipeline.
            _sync_annotations_to_meta_hosted(client, topic, updated_content)

            return (None, new_sha)

        except GitHubConflictError:
            if attempt < DEFAULT_MAX_RETRIES - 1:
                log_debug(f"append_annotation_hosted: conflict on attempt {attempt + 1}, retrying")
                continue
            return ("Conflict after max retries appending annotation", "")

        except GitHubAPIError as e:
            return (f"GitHub API error appending annotation: {e}", "")


# ============================================================================
# Hosted Entry/Thread Management Operations
# ============================================================================


def delete_entry_hosted(
    topic: str,
    entry_id: str,
) -> tuple[str | None, dict]:
    """Delete an entry from a thread on GitHub (hosted mode).

    Removes the entry from entries.jsonl and updates meta.json entry_count.

    Args:
        topic: Thread topic identifier
        entry_id: Entry ID (ULID) to delete

    Returns:
        Tuple of (error_message, result_dict).
    """
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, {})

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    entries_path = f"{GRAPH_THREADS_DIR}/{topic}/entries.jsonl"
    meta_path = f"{GRAPH_THREADS_DIR}/{topic}/meta.json"

    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            # Read entries
            try:
                file_content = client.get_file(entries_path)
            except GitHubNotFoundError:
                return (f"Error: Thread '{topic}' has no entries file.", {})

            lines = file_content.content.splitlines()
            kept = []
            valid_entry_count = 0
            removed = False
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("entry_id") == entry_id:
                        removed = True
                        continue
                    kept.append(line)
                    valid_entry_count += 1
                except json.JSONDecodeError:
                    kept.append(line)  # preserve malformed lines but don't count

            if not removed:
                return (f"Error: Entry '{entry_id}' not found in thread '{topic}'.", {})

            # Write updated entries
            new_content = "\n".join(kept) + "\n" if kept else ""
            client.put_file(
                path=entries_path,
                content=new_content,
                message=f"delete entry {entry_id[:12]} from {topic}",
                sha=file_content.sha,
            )

            # Update meta.json entry_count — retry on conflict since entries already updated
            meta_updated = False
            for meta_attempt in range(DEFAULT_MAX_RETRIES):
                try:
                    meta_file = client.get_file(meta_path)
                    meta = json.loads(meta_file.content)
                    meta["entry_count"] = valid_entry_count
                    client.put_file(
                        path=meta_path,
                        content=json.dumps(meta, indent=2) + "\n",
                        message=f"update meta after deleting {entry_id[:12]}",
                        sha=meta_file.sha,
                    )
                    meta_updated = True
                    break
                except GitHubConflictError:
                    if meta_attempt < DEFAULT_MAX_RETRIES - 1:
                        continue
                    log_warning(f"Meta update conflict after entry delete {entry_id[:12]}")
                except GitHubNotFoundError:
                    meta_updated = True  # no meta file is fine
                    break

            # Project .md (non-fatal) — re-read graph state for consistent projection
            try:
                meta_r, entries_r, _, _, _, _ = _read_per_thread_graph(client, topic)
                if meta_r is not None:
                    _write_md_projection(
                        client, topic, meta_r, entries_r,
                        f"delete entry {entry_id[:12]} from {topic}",
                    )
            except Exception as e:
                log_warning(f"delete_entry_hosted: .md projection failed for {topic}: {e}")

            status = "deleted" if meta_updated else "deleted_meta_stale"
            return (None, {"status": status, "topic": topic, "entry_id": entry_id})

        except GitHubConflictError:
            if attempt < DEFAULT_MAX_RETRIES - 1:
                continue
            return ("Conflict after max retries deleting entry", {})

        except GitHubAPIError as e:
            return (f"GitHub API error deleting entry: {e}", {})


def delete_thread_hosted(
    topic: str,
) -> tuple[str | None, dict]:
    """Delete an entire thread directory on GitHub (hosted mode).

    Removes all files in the thread's graph directory.

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (error_message, result_dict).
    """
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, {})

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    thread_path = f"{GRAPH_THREADS_DIR}/{topic}"

    try:
        # List all files in the thread directory
        try:
            files = client.list_files(thread_path)
        except GitHubNotFoundError:
            return (f"Error: Thread '{topic}' not found.", {})

        # Delete each file
        file_count = sum(1 for f in files if f.type == "file")
        deleted = 0
        failed = 0
        for f in files:
            if f.type == "file":
                try:
                    file_content = client.get_file(f.path)
                    client.delete_file(
                        path=f.path,
                        message=f"delete thread {topic}: {f.name}",
                        sha=file_content.sha,
                    )
                    deleted += 1
                except GitHubAPIError as e:
                    failed += 1
                    log_warning(f"Failed to delete {f.path}: {e}")

        # Only remove from manifest if ALL files were deleted
        if failed == 0:
            manifest_path = "graph/baseline/manifest.json"
            try:
                manifest_file = client.get_file(manifest_path)
                manifest = json.loads(manifest_file.content)
                topics = manifest.get("topics", {})
                topics.pop(topic, None)
                manifest["topics"] = topics
                client.put_file(
                    path=manifest_path,
                    content=json.dumps(manifest, indent=2) + "\n",
                    message=f"remove {topic} from manifest",
                    sha=manifest_file.sha,
                )
            except (GitHubNotFoundError, GitHubAPIError):
                pass
        elif failed > 0 and deleted == 0:
            return (f"Failed to delete any files for thread '{topic}'", {})
        elif failed > 0:
            return (
                f"Partial deletion: {deleted} files removed, {failed} failed for thread '{topic}'",
                {},
            )

        return (None, {
            "status": "deleted",
            "topic": topic,
            "files_removed": deleted,
        })

    except GitHubAPIError as e:
        return (f"GitHub API error deleting thread: {e}", {})


def archive_thread_hosted(
    topic: str,
    reason: str = "",
    unarchive: bool = False,
) -> tuple[str | None, dict]:
    """Archive or unarchive a thread on GitHub (hosted mode).

    Updates meta.json with archived flag and status.

    Args:
        topic: Thread topic identifier
        reason: Archive reason
        unarchive: If True, unarchive instead

    Returns:
        Tuple of (error_message, result_dict).
    """
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, {})

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", {})

    meta_path = f"{GRAPH_THREADS_DIR}/{topic}/meta.json"

    action = "unarchived" if unarchive else "archived"

    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            try:
                meta_file = client.get_file(meta_path)
            except GitHubNotFoundError:
                return (f"Error: Thread '{topic}' not found.", {})

            meta = json.loads(meta_file.content)

            if unarchive:
                meta.pop("archived", None)
                meta.pop("archive_reason", None)
                meta["status"] = "OPEN"
            else:
                meta["archived"] = True
                meta["status"] = "CLOSED"
                if reason:
                    meta["archive_reason"] = reason

            client.put_file(
                path=meta_path,
                content=json.dumps(meta, indent=2) + "\n",
                message=f"{action} thread {topic}",
                sha=meta_file.sha,
            )

            # Project .md (non-fatal) — re-read entries for consistent projection
            try:
                _, entries_r, _, _, _, _ = _read_per_thread_graph(client, topic)
                _write_md_projection(
                    client, topic, meta, entries_r,
                    f"{action} thread {topic}",
                )
            except Exception as e:
                log_warning(f"archive_thread_hosted: .md projection failed for {topic}: {e}")

            result = {"status": action, "topic": topic}
            if reason and not unarchive:
                result["reason"] = reason
            return (None, result)

        except GitHubConflictError:
            if attempt < DEFAULT_MAX_RETRIES - 1:
                continue
            return (f"Conflict after max retries {action} thread", {})

        except GitHubAPIError as e:
            return (f"GitHub API error: {e}", {})


# ============================================================================
# Hosted Search & Graph Stats Operations
# ============================================================================


def load_entries_hosted(topic: str) -> tuple[str | None, list[dict]]:
    """Fetch entries.jsonl for a thread via GitHub API.

    Returns raw entry dicts (not ThreadEntry objects) for search/daemon use.

    Args:
        topic: Thread topic identifier

    Returns:
        Tuple of (error_message, entries_list). If error_message is not None,
        entries will be empty.
    """
    topic_err = _validate_topic(topic)
    if topic_err:
        return (topic_err, [])

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", [])

    _, entries_path, _ = _get_per_thread_paths(topic)

    try:
        entries_content = client.get_file(entries_path)
        entries: list[dict] = []
        for line in entries_content.content.strip().split("\n"):
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return (None, entries)

    except GitHubNotFoundError:
        return (None, [])  # No entries yet — not an error
    except GitHubAPIError as e:
        return (f"GitHub API error loading entries for {topic}: {e}", [])


def load_all_entries_hosted(
    topics: list[str] | None = None,
    max_workers: int = 10,
) -> tuple[str | None, dict[str, list[dict]]]:
    """Fetch entries for multiple threads concurrently via GitHub API.

    If topics is None, discovers all threads first via list_threads_hosted.

    Args:
        topics: List of topic names. If None, loads all threads.
        max_workers: Concurrent worker count (default: 10).

    Returns:
        Tuple of (error_message, {topic: [entries]}). On partial errors,
        failed topics are omitted from the dict (not treated as fatal).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Discover topics if not provided
    if topics is None:
        err, thread_list = list_threads_hosted()
        if err:
            return (err, {})
        topics = [t.topic for t in thread_list]

    if not topics:
        return (None, {})

    result: dict[str, list[dict]] = {}
    workers = min(max_workers, len(topics)) if topics else 1

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(load_entries_hosted, topic): topic
            for topic in topics
        }
        for future in as_completed(futures):
            topic = futures[future]
            try:
                err, entries = future.result()
                if not err:
                    result[topic] = entries
                else:
                    log_debug(f"load_all_entries_hosted: skipping {topic}: {err}")
            except Exception as e:
                log_debug(f"load_all_entries_hosted: exception for {topic}: {e}")

    return (None, result)


def search_entries_hosted(
    query: str = "",
    thread_topic: str = "",
    role: str = "",
    entry_type: str = "",
    agent: str = "",
    start_time: str = "",
    end_time: str = "",
    limit: int = 10,
    query_operator: str = "AND",
) -> tuple[str | None, dict]:
    """Search entries via GitHub API with keyword/metadata filters.

    Fetches entries from GitHub (scoped to thread_topic if given, otherwise
    all threads) and applies keyword + metadata filtering in memory.

    Args:
        query: Keyword search query (case-insensitive substring match).
        thread_topic: Filter to a specific thread.
        role: Filter by entry role.
        entry_type: Filter by entry type.
        agent: Filter by agent (partial match).
        start_time: ISO timestamp lower bound.
        end_time: ISO timestamp upper bound.
        limit: Max results (default 10).
        query_operator: "AND" or "OR" for multi-word queries.

    Returns:
        Tuple of (error_message, results_dict).
    """
    # Load entries
    if thread_topic:
        err, entries = load_entries_hosted(thread_topic)
        if err:
            return (err, {})
        all_entries = {thread_topic: entries}
    else:
        err, all_entries = load_all_entries_hosted()
        if err:
            return (err, {})

    # Flatten entries with topic context
    flat: list[dict] = []
    for topic, entries in all_entries.items():
        for entry in entries:
            entry_with_topic = dict(entry)
            if "thread_topic" not in entry_with_topic:
                entry_with_topic["thread_topic"] = topic
            flat.append(entry_with_topic)

    # Apply keyword filter
    if query:
        tokens = query.lower().split()
        filtered = []
        for entry in flat:
            searchable = " ".join([
                entry.get("title", ""),
                entry.get("body", ""),
                entry.get("agent", ""),
                entry.get("role", ""),
            ]).lower()
            if query_operator.upper() == "OR":
                if any(t in searchable for t in tokens):
                    filtered.append(entry)
            else:  # AND
                if all(t in searchable for t in tokens):
                    filtered.append(entry)
        flat = filtered

    # Apply metadata filters
    if role:
        role_lower = role.lower()
        flat = [e for e in flat if (e.get("role") or "").lower() == role_lower]
    if entry_type:
        type_lower = entry_type.lower()
        flat = [e for e in flat if (e.get("entry_type") or "").lower() == type_lower]
    if agent:
        agent_lower = agent.lower()
        flat = [e for e in flat if agent_lower in (e.get("agent") or "").lower()]
    if start_time:
        flat = [e for e in flat if (e.get("timestamp") or "") >= start_time]
    if end_time:
        flat = [e for e in flat if (e.get("timestamp") or "") <= end_time]

    # Truncate
    total = len(flat)
    flat = flat[:limit]

    # Format results
    results = []
    for entry in flat:
        results.append({
            "entry_id": entry.get("entry_id", ""),
            "thread_topic": entry.get("thread_topic", ""),
            "title": entry.get("title", ""),
            "agent": entry.get("agent", ""),
            "role": entry.get("role", ""),
            "entry_type": entry.get("entry_type", ""),
            "timestamp": entry.get("timestamp", ""),
            "summary": entry.get("summary", entry.get("body", "")[:200]),
        })

    return (None, {
        "count": len(results),
        "total_matched": total,
        "results": results,
        "source": "hosted_github_api",
    })


def get_baseline_graph_stats_hosted() -> tuple[str | None, dict]:
    """Thread/entry/graph metrics from GitHub API.

    Fetches all thread metadata concurrently to compute aggregate stats.

    Returns:
        Tuple of (error_message, stats_dict).
    """
    err, thread_list = list_threads_hosted()
    if err:
        return (err, {})

    total_entries = sum(t.entry_count for t in thread_list)
    open_threads = sum(1 for t in thread_list if t.status.upper() == "OPEN")
    closed_threads = sum(1 for t in thread_list if t.status.upper() != "OPEN")

    return (None, {
        "total_threads": len(thread_list),
        "open_threads": open_threads,
        "closed_threads": closed_threads,
        "total_entries": total_entries,
        "source": "hosted_github_api",
    })


def get_baseline_sync_status_hosted() -> tuple[str | None, dict]:
    """Graph freshness from GitHub API metadata.

    In hosted mode, the GitHub orphan branch IS the source of truth,
    so sync status is always "synced" by definition. Reports thread
    counts and last-updated times.

    Returns:
        Tuple of (error_message, status_dict).
    """
    err, thread_list = list_threads_hosted()
    if err:
        return (err, {})

    last_updated = ""
    for t in thread_list:
        if t.last_updated and t.last_updated > last_updated:
            last_updated = t.last_updated

    return (None, {
        "graph_available": True,
        "healthy": True,
        "total_threads": len(thread_list),
        "synced_threads": len(thread_list),
        "stale_threads": [],
        "error_threads": 0,
        "pending_threads": 0,
        "error_details": [],
        "last_updated": last_updated,
        "recommendations": [],
        "source": "hosted_github_api",
        "note": "In hosted mode, GitHub is the source of truth. Sync status is always current.",
    })
