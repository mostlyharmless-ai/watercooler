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

    # The dashboard sends the plain repo name (e.g., "org/repo").
    repo = http_ctx.repo

    client = GitHubClient(
        token=token,
        repo=repo,
        branch=ORPHAN_BRANCH_NAME,
    )
    return (None, client)


def list_topic_dirs_hosted() -> tuple[str | None, list[str]]:
    """Return raw topic directory names from the threads repository.

    Complements :func:`list_threads_hosted`. Where ``list_threads_hosted``
    silently skips threads whose ``meta.json`` is missing or malformed, this
    helper returns the unfiltered directory listing — callers that want to
    reason about those skipped threads (e.g. surface a ``skipped_topics``
    signal) can diff this list against a ``load_all_entries_hosted`` result.

    Returns:
        Tuple of ``(error_message, topic_names)``. On error ``topic_names``
        is empty.
    """
    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", [])

    with trace_stage("tool.github.list_topic_dirs"):
        try:
            items = client.list_files(GRAPH_THREADS_DIR)
            return (None, sorted(f.name for f in items if f.type == "dir"))
        except GitHubNotFoundError:
            log_debug("list_topic_dirs_hosted: threads directory not found")
            return (None, [])
        except GitHubAPIError as e:
            log_error(f"list_topic_dirs_hosted failed: {e}")
            return (f"GitHub API error: {e}", [])
        except Exception as e:
            log_error(
                f"list_topic_dirs_hosted unexpected error: "
                f"{type(e).__name__}: {e}"
            )
            return (f"Unexpected error: {e}", [])


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
    #
    # Malformed JSONL lines are dropped with a log_warning rather than
    # silently swallowed. The prior ``delete_entry_hosted`` path
    # explicitly preserved unparseable lines on rewrite; after the
    # 2026-05-06 atomic-write migration (PR #781) all hosted
    # writers re-render ``entries.jsonl`` from the parsed list, so
    # malformed lines would silently disappear on the next write.
    # In practice no malformed lines should exist (the writer is
    # the only producer and emits a single dict per line); the
    # warning surfaces upstream-corruption events that previously
    # had no signal.
    try:
        entries_file = client.get_file(entries_path)
        entries_sha = entries_file.sha
        for line_no, raw in enumerate(entries_file.content.split("\n"), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                log_warning(
                    f"_read_per_thread_graph: dropping malformed entries.jsonl "
                    f"line {line_no} for topic {topic!r}: {e} (line preview: "
                    f"{line[:80]!r})"
                )
    except GitHubNotFoundError:
        log_debug(f"Per-thread entries.jsonl not found for {topic}, will create")

    # Read edges.jsonl (same malformed-line policy as entries above).
    try:
        edges_file = client.get_file(edges_path)
        edges_sha = edges_file.sha
        for line_no, raw in enumerate(edges_file.content.split("\n"), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                edges.append(json.loads(line))
            except json.JSONDecodeError as e:
                log_warning(
                    f"_read_per_thread_graph: dropping malformed edges.jsonl "
                    f"line {line_no} for topic {topic!r}: {e} (line preview: "
                    f"{line[:80]!r})"
                )
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

    # Sort entries by index
    sorted_entries = sorted(entries, key=lambda e: e.get("index", 0))
    # Sort edges by source_id, target_id
    sorted_edges = sorted(
        edges, key=lambda e: (e.get("source_id", ""), e.get("target_id", ""))
    )

    # Render file contents
    meta_content = json.dumps(meta, indent=2) + "\n"
    entries_content = (
        "\n".join(json.dumps(e, separators=(",", ":")) for e in sorted_entries)
        + "\n"
        if sorted_entries
        else ""
    )
    edges_content = (
        "\n".join(json.dumps(e, separators=(",", ":")) for e in sorted_edges) + "\n"
        if sorted_edges
        else ""
    )

    # Atomic commit of all 3 graph files. Previously this was 3 sequential
    # ``put_file`` calls — each producing its own commit, push event, and
    # webhook delivery. With the multi-file ``commit_files`` (Git Trees
    # API) the same 3 files land in ONE commit and produce ONE webhook
    # delivery, eliminating the 3× burst that overwhelmed downstream
    # webhook receivers (Vercel's edge dropped most of the burst).
    #
    # Concurrency: the caller-supplied ``meta_sha`` / ``entries_sha`` /
    # ``edges_sha`` are forwarded to ``commit_files`` as
    # ``expected_blob_shas`` so the per-file conflict check spans the
    # caller's full read→write window — same coverage as the prior
    # ``put_file(sha=X)`` 422 → ``GitHubConflictError`` path. A drift
    # on any of these between the caller's read and our write raises
    # ``GitHubConflictError`` and the caller's retry loop refreshes
    # from a clean read. (The narrower ref-tip-only check that
    # ``commit_files`` does on its own would miss writes that landed
    # between the caller's read and ``commit_files``'s internal ref
    # read — a lost-write race we explicitly do not want.)
    expected_blob_shas: dict[str, Optional[str]] = {
        meta_path: meta_sha,
        entries_path: entries_sha,
        edges_path: edges_sha,
    }
    try:
        _commit_sha, blob_shas = client.commit_files(
            files=[
                (meta_path, meta_content),
                (entries_path, entries_content),
                (edges_path, edges_content),
            ],
            message=commit_message,
            expected_blob_shas=expected_blob_shas,
        )
        new_meta_sha = blob_shas.get(meta_path, "")
        new_entries_sha = blob_shas.get(entries_path, "")
        new_edges_sha = blob_shas.get(edges_path, "")
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


def _generate_entry_enrichment(
    body: str,
    title: str,
    entry_type: str,
) -> tuple[str | None, list[float] | None]:
    """Generate summary + embedding for a new entry, in memory.

    Best-effort and pure: makes no GitHub writes. Each generator is
    wrapped in try/except so a failing summarizer doesn't block the
    embedder (and vice versa). Either or both may return ``None``.

    Returns ``(summary, embedding)``. Caller decides what to do with
    them — typically, merge into the matching entry node before the
    GitHub write so enrichment lands in the same commit as the graph
    write rather than producing a follow-up commit (and a follow-up
    webhook delivery).
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

    summary: str | None = None
    embedding: list[float] | None = None

    try:
        cfg = create_summarizer_config()
        if is_llm_service_available(cfg):
            generated = summarize_entry(
                body, entry_title=title, entry_type=entry_type, config=cfg
            )
            if generated:
                summary = generated
    except Exception as e:
        log_warning(f"_generate_entry_enrichment: summary generation failed: {e}")

    try:
        embed_cfg = EmbeddingConfig.from_env()
        if is_embedding_available(embed_cfg):
            embed_text = summary if summary else body[: embed_cfg.max_text_chars]
            generated = generate_embedding(embed_text, config=embed_cfg)
            if generated:
                embedding = generated
    except Exception as e:
        log_warning(f"_generate_entry_enrichment: embedding generation failed: {e}")

    return summary, embedding


def _write_per_thread_atomic(
    client: GitHubClient,
    topic: str,
    meta: dict,
    entries: list[dict],
    edges: list[dict],
    commit_message: str,
    project_md: bool = True,
    enrich_entry_id: str | None = None,
    enrich_body: str | None = None,
    enrich_title: str | None = None,
    enrich_entry_type: str | None = None,
    meta_sha: str | None = None,
    entries_sha: str | None = None,
    edges_sha: str | None = None,
    extra_files: list[tuple[str, str]] | None = None,
    extra_blob_shas: dict[str, "Optional[str]"] | None = None,
) -> tuple[str | None, dict]:
    """Write per-thread graph + .md projection + entry enrichment in
    ONE atomic git commit.

    ``extra_files`` / ``extra_blob_shas`` let a caller fold an additional
    repo-level file (e.g. the decisions index) into the same atomic commit with
    its own conflict-check SHA — so it lands together with the entry and a
    concurrent change to it triggers the caller's existing retry.

    Replaces the prior 3-step sequence (``_write_per_thread_graph`` →
    ``_write_md_projection`` → ``_enrich_entry_hosted``), which produced
    5 separate commits and 5 webhook deliveries per ``say``. By
    bundling all writes into a single ``commit_files`` call, one
    ``say`` produces one commit, one push event, one webhook delivery.

    Enrichment (summary + embedding) is generated in-memory BEFORE
    the commit so the enriched entry lands in the same commit as the
    graph and md projection. The enrichment generators are best-effort
    — failures are logged and the entry simply lacks summary/embedding
    until the next write touches it.

    Args:
        client: GitHub API client.
        topic: Thread topic.
        meta: Thread metadata dict.
        entries: List of entry node dicts. Mutated in place to add
            enrichment fields when ``enrich_entry_id`` is set.
        edges: List of edge dicts.
        commit_message: Commit message.
        project_md: If True (default), include the ``.md`` projection
            in the same commit. Set False for callers that don't want
            the markdown re-rendered (e.g. status-only changes that
            don't affect any entry's content).
        enrich_entry_id: If set, generate summary + embedding for the
            matching entry and merge them into ``entries`` before the
            write. Requires ``enrich_body`` / ``enrich_title`` /
            ``enrich_entry_type`` to be non-None.
        enrich_body / enrich_title / enrich_entry_type: Inputs to the
            enrichment generators. Ignored if ``enrich_entry_id`` is
            None.
        meta_sha / entries_sha / edges_sha: Caller-observed blob SHAs
            for the existing per-thread graph files (or ``None`` if
            the caller expects the file to not exist yet, e.g. new
            thread). Forwarded to ``commit_files`` as
            ``expected_blob_shas`` so the per-file conflict check
            spans the caller's full read→write window. A drift on
            any of these between the caller's read and our write
            raises ``GitHubConflictError`` so the retry loop kicks
            in. Without these, two concurrent ``say_hosted`` calls
            could both succeed with the second silently overwriting
            the first's entry — see PR #775 review.

    Returns:
        ``(commit_sha, info)`` where ``info`` has keys
        ``md_projected`` (bool) and ``enriched`` (bool indicating
        whether at least one of summary or embedding was generated).
        ``commit_sha`` is None on GitHubAPIError; raises
        ``GitHubConflictError`` for branch-level concurrency conflicts
        so callers can retry from a fresh read (matches
        ``_write_per_thread_graph``'s contract).
    """
    info = {"md_projected": False, "enriched": False}

    # 1. Generate enrichment in-memory and merge into entries.
    #    ``_generate_entry_enrichment`` already wraps each individual
    #    generator (summary, embedding) in try/except and returns
    #    ``None`` for failures, so this call rarely raises in practice.
    #    We still wrap defensively: an unexpected failure in the
    #    enrichment layer should not block the graph write itself —
    #    the user-visible "say succeeded" outcome takes priority over
    #    "say succeeded with enrichment". The entry simply lacks
    #    summary/embedding until a later write touches it.
    if (
        enrich_entry_id
        and enrich_body is not None
        and enrich_title is not None
        and enrich_entry_type is not None
    ):
        try:
            summary, embedding = _generate_entry_enrichment(
                body=enrich_body, title=enrich_title, entry_type=enrich_entry_type
            )
        except Exception as e:
            log_warning(
                f"_write_per_thread_atomic: enrichment generation raised "
                f"unexpectedly for {enrich_entry_id}, continuing without "
                f"enrichment: {e}"
            )
            summary, embedding = None, None
        if summary or embedding:
            for entry in entries:
                if entry.get("entry_id") == enrich_entry_id:
                    if summary:
                        entry["summary"] = summary
                    if embedding:
                        entry["embedding"] = embedding
                    info["enriched"] = True
                    break
            else:
                log_warning(
                    f"_write_per_thread_atomic: enrich_entry_id {enrich_entry_id} "
                    f"not found in entries list; skipping enrichment merge"
                )

    # 2. Render file contents from the (possibly enriched) graph.
    meta_path, entries_path, edges_path = _get_per_thread_paths(topic)
    sorted_entries = sorted(entries, key=lambda e: e.get("index", 0))
    sorted_edges = sorted(
        edges, key=lambda e: (e.get("source_id", ""), e.get("target_id", ""))
    )
    meta_content = json.dumps(meta, indent=2) + "\n"
    entries_content = (
        "\n".join(json.dumps(e, separators=(",", ":")) for e in sorted_entries)
        + "\n"
        if sorted_entries
        else ""
    )
    edges_content = (
        "\n".join(json.dumps(e, separators=(",", ":")) for e in sorted_edges) + "\n"
        if sorted_edges
        else ""
    )

    files: list[tuple[str, str]] = [
        (meta_path, meta_content),
        (entries_path, entries_content),
        (edges_path, edges_content),
    ]
    if project_md:
        # The .md is a write-only projection of the graph for human
        # diffs. ``_reconstruct_markdown_from_graph`` is pure and
        # failure-resistant in normal operation, but a malformed entry
        # could in principle crash it. Wrap defensively so a renderer
        # crash doesn't take down the whole commit (graph + edges
        # would be lost). On failure we drop just the .md from the
        # commit; the next write naturally re-projects from the
        # current graph state.
        try:
            md_path = f"threads/{topic}.md"
            md_content = _reconstruct_markdown_from_graph(meta, sorted_entries)
            files.append((md_path, md_content))
            info["md_projected"] = True
        except Exception as e:
            log_warning(
                f"_write_per_thread_atomic: .md rendering failed for "
                f"{topic}, skipping md from commit: {e}"
            )

    # 3. One atomic commit. ``commit_files`` validates the
    #    caller-supplied per-file SHAs (``meta_sha`` / ``entries_sha``
    #    / ``edges_sha``) against the parent commit's tree before
    #    creating any blobs, so the conflict-detection window matches
    #    the caller's full read→write transaction (matching the prior
    #    ``put_file(sha=X)`` 422 contract). The .md projection is a
    #    derived file that's always re-rendered from current state,
    #    so we deliberately don't conflict-check it — last-write-wins
    #    is the right semantics for a derived file.
    if extra_files:
        files.extend(extra_files)

    expected_blob_shas: dict[str, Optional[str]] = {
        meta_path: meta_sha,
        entries_path: entries_sha,
        edges_path: edges_sha,
    }
    if extra_blob_shas:
        expected_blob_shas.update(extra_blob_shas)
    try:
        commit_sha, _blob_shas = client.commit_files(
            files=files,
            message=commit_message,
            expected_blob_shas=expected_blob_shas,
        )
        return commit_sha, info
    except GitHubConflictError:
        raise
    except GitHubAPIError as e:
        log_error(
            f"_write_per_thread_atomic failed for {topic}: {e} "
            f"(repo={client.repo}, branch={client.branch})"
        )
        return None, info


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
    code_repo: str | None = None,
    code_commit: str | None = None,
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
        code_repo: Code repo (owner/name) the entry was written against (C3)
        code_commit: Code-repo commit the entry was written against (C3);
            absent in hosted writes — the hosted server has no code checkout

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
            # Code-state provenance (C3) — omitted when absent so legacy and
            # context-less entries keep the identical minimal node shape.
            if code_repo:
                entry_node["code_repo"] = code_repo
            if code_commit:
                entry_node["code_commit"] = code_commit
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


def _slack_sync_secret() -> str:
    """Resolve the slack-sync shared secret.

    Move 2.5 (security consolidation plan v5.1) split the env-var
    purpose: the slack-sync ``X-Watercooler-Secret`` header reads
    exclusively from ``WATERCOOLER_SLACK_SYNC_SECRET``. The legacy
    fallback to the now-retired global secret was removed during
    that rollout — both Cloud (Railway) and Site (Vercel) have been
    on the new var since PRs #722, #723, #724 plus the 2026-05-01
    rotation cycle, and the global-secret HMAC verifier itself was
    deleted in #733.
    """
    return os.getenv("WATERCOOLER_SLACK_SYNC_SECRET", "")


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
        repo_full_name: GitHub repo (e.g., owner/repo)
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
    # PR #722 round 1 MED + PR #723 round 2 dead-code observation:
    # resolve the secret once per sync. The env-var split is
    # specifically designed so the resolver could later fetch from a
    # remote secrets store — at which point any duplicate per-sync
    # call would be a real performance and consistency hazard. Inline
    # the enable-check against the same resolved value here. (The
    # earlier ``_is_slack_sync_enabled`` helper that wrapped this
    # check was deleted in PR #723 since this is its only caller.)
    #
    # PR #723 round 3 MED: gate the URL check FIRST so an operator
    # without a hosted API URL (slack-sync never configured) doesn't
    # incur unnecessary work resolving the slack-sync secret. The
    # old ``_is_slack_sync_enabled`` had this short-circuit for
    # free via ``and``; the inlined check preserves that ordering.
    site_url = _get_hosted_api_url()
    if not site_url:
        log_debug("Slack sync not enabled (missing hosted API URL)")
        return False
    secret = _slack_sync_secret()
    if not secret:
        log_debug(
            "Slack sync not enabled (missing WATERCOOLER_SLACK_SYNC_SECRET)"
        )
        return False
    site_url = site_url.rstrip("/")

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
    code_repo: Optional[str] = None,
    code_commit: Optional[str] = None,
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
            # C3: repo defaults to the request's tenant repo; commit has no
            # hosted default (no code checkout) — explicit param only.
            effective_code_repo = code_repo
            if not effective_code_repo and http_ctx:
                effective_code_repo = http_ctx.repo

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
                code_repo=effective_code_repo,
                code_commit=code_commit,
            )

            # Write to per-thread format. Bundles graph (meta + entries
            # + edges) + .md projection + entry enrichment (summary +
            # embedding) into ONE atomic git commit via the Trees API,
            # which fans out to ONE webhook delivery downstream rather
            # than the prior 5 (3 graph put_files + 1 md put_file + 1
            # enrichment put_file). The 5-event burst was overwhelming
            # the dashboard's webhook receiver — most events were dropped
            # at the edge, leaving ConnectedRepo.graphNodes stale and
            # the dashboard list out of sync with the orphan branch.
            commit_message = f"[watercooler] {topic}: {title}\n\nEntry-ID: {entry_id}"
            # Fold a Decision write's index upsert into the same atomic commit
            # (best-effort; empty for non-Decisions). Source is resolved
            # same-thread; cross-thread is left to the reconcile backfill.
            _idx_files, _idx_shas = _decision_index_extra_for_say(
                client, topic, new_entries, entry_id, entry_type
            )
            new_commit_sha, write_info = _write_per_thread_atomic(
                client,
                topic=topic,
                meta=new_meta,
                entries=new_entries,
                edges=new_edges,
                commit_message=commit_message,
                project_md=True,
                enrich_entry_id=entry_id,
                enrich_body=body,
                enrich_title=title,
                enrich_entry_type=entry_type,
                # Pass through the caller's read-time SHAs so the
                # commit_files conflict check spans the full caller
                # transaction (closes the lost-write window flagged
                # in PR #775 review).
                meta_sha=meta_sha,
                entries_sha=entries_sha,
                edges_sha=edges_sha,
                extra_files=_idx_files,
                extra_blob_shas=_idx_shas,
            )

            if new_commit_sha:
                log_debug(
                    f"say_hosted: atomic write to per-thread format {topic} "
                    f"(commit={new_commit_sha[:8]}, "
                    f"md={write_info['md_projected']}, "
                    f"enriched={write_info['enriched']})"
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
                        "sha": new_commit_sha,
                        "graph_updated": True,
                        "md_projected": write_info["md_projected"],
                        "enriched": write_info["enriched"],
                        "slack_synced": slack_synced,
                        "format": "per-thread",
                    },
                )
            else:
                log_error(f"say_hosted: _write_per_thread_atomic failed for {topic}")
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

            # Atomic write — graph (meta+entries+edges) + .md projection
            # in ONE git commit, ONE webhook event. Status changes don't
            # produce a new entry so no enrichment is needed.
            commit_message = f"[watercooler] {topic}: status {old_status} → {status}"
            new_commit_sha, _info = _write_per_thread_atomic(
                client,
                topic=topic,
                meta=new_meta,
                entries=existing_entries,
                edges=existing_edges,
                commit_message=commit_message,
                project_md=True,
                meta_sha=meta_sha,
                entries_sha=entries_sha,
                edges_sha=edges_sha,
            )

            if new_commit_sha:
                log_debug(f"set_status_hosted: updated {topic} status to {status}")
                return (
                    None,
                    {
                        "topic": topic,
                        "old_status": old_status,
                        "new_status": status,
                        "ball": ball,
                        "sha": new_commit_sha,
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
    code_repo: Optional[str] = None,
    code_commit: Optional[str] = None,
    role: str = "pm",
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
            # C3: repo defaults to the request's tenant repo; commit has no
            # hosted default (no code checkout) — explicit param only.
            effective_code_repo = code_repo
            if not effective_code_repo and http_ctx:
                effective_code_repo = http_ctx.repo

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
                role=role,
                entry_type="Note",
                entry_title=title,
                body=body,
                timestamp=timestamp,
                code_branch=effective_code_branch,
                code_repo=effective_code_repo,
                code_commit=code_commit,
            )

            # Atomic write — graph + .md projection + entry enrichment
            # in ONE git commit. Same pattern as ``say_hosted``: 5
            # webhook events → 1.
            commit_message = f"[watercooler] {topic}: {title} (ack)\n\nEntry-ID: {entry_id}"
            new_commit_sha, _info = _write_per_thread_atomic(
                client,
                topic=topic,
                meta=new_meta,
                entries=new_entries,
                edges=new_edges,
                commit_message=commit_message,
                project_md=True,
                enrich_entry_id=entry_id,
                enrich_body=body,
                enrich_title=title,
                enrich_entry_type="Note",
                meta_sha=meta_sha,
                entries_sha=entries_sha,
                edges_sha=edges_sha,
            )

            if new_commit_sha:
                log_debug(f"ack_hosted: acknowledged {topic}")
                return (
                    None,
                    {
                        "topic": topic,
                        "entry_id": entry_id,
                        "timestamp": timestamp,
                        "status": status,
                        "ball": ball,  # Ball unchanged
                        "sha": new_commit_sha,
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
    code_repo: Optional[str] = None,
    code_commit: Optional[str] = None,
    role: str = "pm",
    title: Optional[str] = None,
) -> tuple[str | None, dict]:
    """Hand off the ball to another agent (per-thread format only).

    Args:
        topic: Thread topic identifier
        agent: Current agent name
        target_agent: Agent to hand off to (optional)
        note: Handoff note
        title: Optional explicit entry title; defaults to
            ``f"Handoff to {new_ball}"`` when omitted.

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
            effective_code_repo = code_repo
            if not effective_code_repo and http_ctx_local:
                effective_code_repo = http_ctx_local.repo

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
                    role=role,
                    entry_type="Note",
                    entry_title=title or f"Handoff to {new_ball}",
                    body=note,
                    timestamp=timestamp,
                    code_branch=effective_code_branch,
                    code_repo=effective_code_repo,
                    code_commit=code_commit,
                )
            else:
                # Just update ball, no entry
                new_meta = {**meta, "ball": new_ball}
                new_entries = existing_entries
                new_edges = existing_edges

            # Atomic write — graph + .md projection + (optional) entry
            # enrichment in ONE git commit. Handoffs may or may not
            # carry a note: with note → new entry → enrichment runs;
            # without note → meta-only update → no enrichment.
            commit_message = f"[watercooler] {topic}: handoff to {new_ball}"
            if note:
                commit_message += f"\n\nEntry-ID: {entry_id}"
            new_commit_sha, _info = _write_per_thread_atomic(
                client,
                topic=topic,
                meta=new_meta,
                entries=new_entries,
                edges=new_edges,
                commit_message=commit_message,
                project_md=True,
                # Enrichment only fires when a handoff note created an
                # entry; ``_write_per_thread_atomic`` skips enrichment
                # when ``enrich_entry_id`` is None.
                enrich_entry_id=entry_id if note else None,
                enrich_body=note if note else None,
                enrich_title=(title or f"Handoff to {new_ball}") if note else None,
                enrich_entry_type="Note" if note else None,
                meta_sha=meta_sha,
                entries_sha=entries_sha,
                edges_sha=edges_sha,
            )

            if new_commit_sha:
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
                        "sha": new_commit_sha,
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

        # Rebuild the repo-level decisions index from the reconciled corpus so the
        # hosted list_decisions fast path has a complete, fresh index. Best-effort.
        decisions_indexed: int | None = None
        idx_err, idx_count = build_decision_index_hosted()
        if idx_err:
            log_warning(
                f"reconcile_graph_hosted: decisions index build failed: {idx_err}"
            )
        else:
            decisions_indexed = idx_count

        return (
            None,
            {
                "total": len(topics),
                "successes": successes,
                "failures": failures,
                "success_topics": list(results.keys()),
                "failure_topics": list(errors.keys()),
                "errors": errors,
                "decisions_indexed": decisions_indexed,
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

    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            # Read full per-thread state (meta + entries + edges + their
            # SHAs) so we can do the entry-delete + meta entry_count
            # update + .md re-projection in ONE atomic commit. The
            # prior implementation issued 3 separate ``put_file`` calls
            # (entries, meta, md) and a nested meta retry — same
            # 3-event-burst-vs-Vercel-edge mismatch the say path used
            # to suffer.
            (
                existing_meta,
                existing_entries,
                existing_edges,
                meta_sha,
                entries_sha,
                edges_sha,
            ) = _read_per_thread_graph(client, topic)

            if entries_sha is None:
                return (f"Error: Thread '{topic}' has no entries file.", {})

            # Filter out the deleted entry; preserve everything else.
            kept_entries = [e for e in existing_entries if e.get("entry_id") != entry_id]
            removed = len(kept_entries) < len(existing_entries)

            if not removed:
                return (f"Error: Entry '{entry_id}' not found in thread '{topic}'.", {})

            # Prune the decisions index in the same atomic commit if a Decision
            # was deleted (best-effort; non-Decision deletes touch nothing).
            removed_entry_type = next(
                (
                    e.get("entry_type")
                    for e in existing_entries
                    if e.get("entry_id") == entry_id
                ),
                None,
            )
            _idx_files, _idx_shas = _decision_index_extra_for_delete(
                client, removed_entry_type, entry_id
            )

            # Update meta.entry_count if a meta file exists (it's
            # optional — pre-meta threads still get the entries write).
            if existing_meta is not None:
                new_meta = {**existing_meta, "entry_count": len(kept_entries)}
            else:
                # Synthesise a minimal meta so the atomic write has a
                # non-None target. Older threads without a meta file
                # are rare; this gives them one going forward.
                new_meta = {
                    "id": f"thread:{topic}",
                    "type": "thread",
                    "topic": topic,
                    "title": topic,
                    "status": "OPEN",
                    "entry_count": len(kept_entries),
                }

            commit_message = f"delete entry {entry_id[:12]} from {topic}"
            new_commit_sha, _info = _write_per_thread_atomic(
                client,
                topic=topic,
                meta=new_meta,
                entries=kept_entries,
                edges=existing_edges,
                commit_message=commit_message,
                project_md=True,
                meta_sha=meta_sha,
                entries_sha=entries_sha,
                edges_sha=edges_sha,
                extra_files=_idx_files,
                extra_blob_shas=_idx_shas,
            )

            if new_commit_sha is None:
                return (f"Failed to write delete commit for {topic}", {})

            return (None, {"status": "deleted", "topic": topic, "entry_id": entry_id})

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

            # Cascade to the human-readable markdown projection. The graph is
            # authoritative; threads/<topic>.md is a derived, write-only
            # projection. Best-effort: a missing .md must never fail the delete,
            # but a leftover .md can ghost-resurrect the thread via
            # recover_graph(), so we remove it when the graph delete succeeded.
            md_path = f"threads/{topic}.md"
            try:
                md_file = client.get_file(md_path)
                client.delete_file(
                    path=md_path,
                    message=f"delete thread {topic}: markdown projection",
                    sha=md_file.sha,
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

    action = "unarchived" if unarchive else "archived"

    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            # Read full per-thread state so meta-mutate + .md re-project
            # land in ONE atomic commit. The prior implementation did
            # a meta-only put_file followed by a separate
            # _write_md_projection — 2 commits, 2 webhook events.
            (
                existing_meta,
                existing_entries,
                existing_edges,
                meta_sha,
                entries_sha,
                edges_sha,
            ) = _read_per_thread_graph(client, topic)

            if existing_meta is None:
                return (f"Error: Thread '{topic}' not found.", {})

            new_meta = dict(existing_meta)
            if unarchive:
                new_meta.pop("archived", None)
                new_meta.pop("archive_reason", None)
                new_meta["status"] = "OPEN"
            else:
                new_meta["archived"] = True
                new_meta["status"] = "CLOSED"
                if reason:
                    new_meta["archive_reason"] = reason

            commit_message = f"{action} thread {topic}"
            new_commit_sha, _info = _write_per_thread_atomic(
                client,
                topic=topic,
                meta=new_meta,
                entries=existing_entries,
                edges=existing_edges,
                commit_message=commit_message,
                project_md=True,
                meta_sha=meta_sha,
                entries_sha=entries_sha,
                edges_sha=edges_sha,
            )
            if new_commit_sha is None:
                return (f"Failed to write {action} commit for {topic}", {})

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
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Defensive: skip valid-JSON-but-wrong-shape lines (e.g. a
                # bare array, string, or ``null`` from a corrupted writer).
                # Consumers downstream assume dict shape and would crash on
                # ``node.get(...)``. Drop them rather than fail the whole
                # listing for one malformed entry.
                if isinstance(parsed, dict):
                    entries.append(parsed)
                else:
                    log_debug(
                        f"load_entries_hosted: skipping non-dict line in "
                        f"{topic} (type={type(parsed).__name__})"
                    )
        return (None, entries)

    except GitHubNotFoundError:
        return (None, [])  # No entries yet — not an error
    except GitHubAPIError as e:
        return (f"GitHub API error loading entries for {topic}: {e}", [])


def _read_decision_index_raw(client) -> tuple[list[dict], "Optional[str]"]:
    """Read the repo-level decisions index records + blob SHA (``([], None)`` absent)."""
    from watercooler.baseline_graph.storage import DECISION_INDEX_FILENAME

    path = f"graph/baseline/{DECISION_INDEX_FILENAME}"
    try:
        index_file = client.get_file(path)
    except GitHubNotFoundError:
        return [], None
    records: list[dict] = []
    for line in (index_file.content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records, index_file.sha


def _decision_index_extra_for_say(client, topic, new_entries, entry_id, entry_type):
    """``(extra_files, extra_blob_shas)`` folding a Decision write's index upsert
    into the same commit; empty when the entry isn't a Decision.

    Best-effort + never-raise. Source is resolved same-thread (this topic's
    current annotations); cross-thread sources are left to the reconcile backfill,
    and an already-resolved source is never downgraded (see
    ``upsert_record_in_list``).
    """
    if entry_type != "Decision":
        return [], {}
    try:
        from watercooler.baseline_graph.decision_index import (
            bare_entry_id,
            build_decision_index_records,
            index_records_to_jsonl,
            upsert_record_in_list,
        )
        from watercooler.baseline_graph.storage import DECISION_INDEX_FILENAME

        a_err, bundle = get_annotations_hosted(topic, target_id="")
        states = {} if a_err else (bundle.get("annotation_states") or {})
        built = build_decision_index_records({topic: new_entries}, {topic: states})
        bare = bare_entry_id(entry_id)
        record = next(
            (r for r in built if bare_entry_id(r.get("entry_id")) == bare), None
        )
        if record is None:
            return [], {}
        current, sha = _read_decision_index_raw(client)
        updated = upsert_record_in_list(current, record)
        path = f"graph/baseline/{DECISION_INDEX_FILENAME}"
        return [(path, index_records_to_jsonl(updated))], {path: sha}
    except Exception as e:
        log_warning(
            f"say_hosted: decisions-index upsert delta failed (non-fatal): {e}"
        )
        return [], {}


def _decision_index_extra_for_delete(client, removed_entry_type, entry_id):
    """``(extra_files, extra_blob_shas)`` pruning a deleted Decision's row; empty
    when the removed entry wasn't a Decision or nothing changed. Never-raise."""
    if removed_entry_type != "Decision":
        return [], {}
    try:
        from watercooler.baseline_graph.decision_index import (
            index_records_to_jsonl,
            remove_record_from_list,
        )
        from watercooler.baseline_graph.storage import DECISION_INDEX_FILENAME

        current, sha = _read_decision_index_raw(client)
        updated = remove_record_from_list(current, entry_id)
        if len(updated) == len(current):
            return [], {}
        path = f"graph/baseline/{DECISION_INDEX_FILENAME}"
        return [(path, index_records_to_jsonl(updated))], {path: sha}
    except Exception as e:
        log_warning(
            f"delete_entry_hosted: decisions-index prune delta failed (non-fatal): {e}"
        )
        return [], {}


def load_decision_index_hosted() -> tuple[str | None, list[dict] | None]:
    """Load the repo-level decisions index from GitHub (hosted mode).

    One ``get_file`` of ``graph/baseline/decisions-index.jsonl`` — the single
    artifact that replaces the per-thread entries fan-out for list_decisions.

    Returns:
        ``(None, [records])`` on success (possibly an empty list).
        ``(None, None)`` when the index file is ABSENT (404) — older or
        pre-backfill repos; the caller falls back to the full per-thread scan,
        this is NOT an error.
        ``(error, None)`` on a real client/API failure.
    """
    from watercooler.baseline_graph.storage import DECISION_INDEX_FILENAME

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", None)

    path = f"graph/baseline/{DECISION_INDEX_FILENAME}"
    try:
        index_file = client.get_file(path)
    except GitHubNotFoundError:
        return (None, None)
    except GitHubAPIError as e:
        return (f"GitHub API error loading decisions index: {e}", None)

    records: list[dict] = []
    for line in (index_file.content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Skip valid-JSON-but-non-dict lines (e.g. ``[]``, ``"bad"``, ``42``) from
        # a corrupted/hand-edited index — otherwise the index reader would hand a
        # non-dict to ``_list_decisions_from_index`` and ``rec.get(...)`` would
        # crash hosted list_decisions. Mirrors ``load_entries_hosted``.
        if isinstance(parsed, dict):
            records.append(parsed)
    return (None, records)


def build_decision_index_hosted() -> tuple[str | None, int]:
    """Rebuild the repo-level decisions index on GitHub from a full corpus scan.

    Backfill/one-time use: this deliberately does the per-thread fan-out we avoid
    on the read path (acceptable as a rare rebuild, not a per-read cost), then
    writes ``graph/baseline/decisions-index.jsonl`` in one ``put_file``. Returns
    ``(error, decision_count)``.
    """
    from watercooler.baseline_graph.decision_index import build_decision_index_records
    from watercooler.baseline_graph.storage import DECISION_INDEX_FILENAME

    err, entries_by_topic = load_all_entries_hosted()
    if err:
        return (err, 0)

    annotations_by_topic: dict[str, dict] = {}
    for topic in entries_by_topic:
        a_err, bundle = get_annotations_hosted(topic, target_id="")
        if a_err:
            # A real annotation read error (NOT a 404 — that returns None +
            # empty state) would index this topic's Decisions with missing
            # source/extracted/confidence, silently degrading the durable read
            # model while reporting success. Fail the rebuild instead.
            return (f"Failed to load annotations for {topic}: {a_err}", 0)
        annotations_by_topic[topic] = bundle.get("annotation_states") or {}

    records = build_decision_index_records(entries_by_topic, annotations_by_topic)

    error, client = _get_github_client()
    if error or not client:
        return (error or "Failed to create GitHub client", 0)

    path = f"graph/baseline/{DECISION_INDEX_FILENAME}"
    content = "".join(
        json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in records
    )
    try:
        existing_sha: str | None = None
        try:
            existing_sha = client.get_file(path).sha
        except GitHubNotFoundError:
            existing_sha = None
        client.put_file(
            path=path,
            content=content,
            message="chore(decisions): rebuild decisions index",
            sha=existing_sha,
        )
    except GitHubAPIError as e:
        return (f"Failed to write decisions index: {e}", 0)

    return (None, len(records))


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
    import contextvars
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
    errors: list[str] = []
    workers = min(max_workers, len(topics)) if topics else 1

    # Propagate the caller's context into each worker. ContextVars set in this
    # thread are NOT visible to ThreadPoolExecutor worker threads, and the worker
    # (load_entries_hosted) resolves its GitHub client via get_effective_context().
    # Without this, every worker fails with "No HTTP context available for hosted
    # mode" — the real cause behind the masked hosted list_decisions total:0.
    # copy_context() snapshots the caller's context (HTTP request or worker) per
    # task so the worker resolves the same identity.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(contextvars.copy_context().run, load_entries_hosted, topic): topic
            for topic in topics
        }
        for future in as_completed(futures):
            topic = futures[future]
            try:
                err, entries = future.result()
                if not err:
                    result[topic] = entries
                else:
                    errors.append(f"{topic}: {err}")
                    log_debug(f"load_all_entries_hosted: skipping {topic}: {err}")
            except Exception as e:
                errors.append(f"{topic}: {e}")
                log_debug(f"load_all_entries_hosted: exception for {topic}: {e}")

    # Un-mask total failure. A 404 (no entries yet) returns ``(None, [])`` and
    # DOES populate ``result``, so an empty ``result`` with errors means every
    # requested topic hit a real load error (e.g. GitHub auth/rate-limit) — a
    # systemic failure, not an empty repo. Returning success here would mask it
    # as ``0 entries`` (the silent ``total:0`` bug in hosted list_decisions).
    # Partial failures are still tolerated: any successful topic keeps the
    # function in the success path with the topics it could load.
    if topics and not result and errors:
        sample = "; ".join(errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        return (
            f"all {len(topics)} hosted topic load(s) failed: {sample}{more}",
            {},
        )

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
