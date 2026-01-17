"""Shared helper functions and constants for watercooler MCP server.

This module contains:
- Startup warnings system
- Context validation helpers
- Branch validation and sync helpers
- Thread parsing and metadata extraction
- Entry loading and formatting
- Graph-first read optimization helpers
- Commit footer building

These are extracted from server.py for modularity and testability.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from git import Repo

# Local application imports
from watercooler import commands, fs
from watercooler.config_facade import config
from watercooler.thread_entries import ThreadEntry, parse_thread_entries
from watercooler.baseline_graph.reader import (
    is_graph_available,
    list_threads_from_graph,
    read_thread_from_graph,
    increment_access_count,
    GraphEntry,
)
from .config import (
    ThreadContext,
    get_git_sync_manager_from_context,
    resolve_thread_context,
)
from .sync import (
    BranchPairingError,
    BranchMismatch,
    BranchPairingResult,
    validate_branch_pairing,
    sync_branch_history,
    auto_merge_to_main,
    _find_main_branch,
)
from .observability import log_debug


# ============================================================================
# Constants
# ============================================================================

_ALLOWED_FORMATS = {"markdown", "json"}

# Resource limits to prevent exhaustion
_MAX_LIMIT = 1000  # Maximum entries that can be requested in a single call
_MAX_OFFSET = 100000  # Maximum offset to prevent excessive memory usage

# Regex patterns for extracting thread metadata from content
_TITLE_RE = re.compile(r"^#\s*(?P<val>.+)$", re.MULTILINE)
_STAT_RE = re.compile(r"^Status:\s*(?P<val>.+)$", re.IGNORECASE | re.MULTILINE)
_BALL_RE = re.compile(r"^Ball:\s*(?P<val>.+)$", re.IGNORECASE | re.MULTILINE)
_ENTRY_RE = re.compile(
    r"^Entry:\s*(?P<who>.+?)\s+(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*$",
    re.MULTILINE,
)
_CLOSED_STATES = {"done", "closed", "merged", "resolved", "abandoned", "obsolete"}


# ============================================================================
# Startup Warnings System
# ============================================================================
# Store warnings at startup (missing config, unavailable services, etc.)
# These are surfaced in tool responses on first invocation, not stderr.

_startup_warnings: List[str] = []
_warnings_shown: bool = False


def _add_startup_warning(msg: str) -> None:
    """Add a warning message to be shown on first tool invocation."""
    global _startup_warnings
    if msg and msg not in _startup_warnings:
        _startup_warnings.append(msg)


def _get_startup_warnings() -> List[str]:
    """Get pending startup warnings and mark them as shown."""
    global _warnings_shown, _startup_warnings
    if _warnings_shown:
        return []
    _warnings_shown = True
    return list(_startup_warnings)


def _format_warnings_for_response(response: str) -> str:
    """Append any pending startup warnings to a tool response."""
    warnings = _get_startup_warnings()
    if not warnings:
        return response

    warning_block = "\n\n" + "─" * 60 + "\n"
    warning_block += "⚠️  Setup Notices:\n"
    for warning in warnings:
        # Indent each line of the warning
        indented = "\n".join("   " + line for line in warning.strip().split("\n"))
        warning_block += f"\n{indented}\n"
    warning_block += "─" * 60

    return response + warning_block


# ============================================================================
# Configuration Helpers
# ============================================================================


def _should_auto_branch() -> bool:
    return config.env.get_bool("WATERCOOLER_AUTO_BRANCH", True)


# ============================================================================
# Context Resolution Helpers (re-exported from validation.py)
# ============================================================================
# These functions are now defined in validation.py to break circular imports.
# They are re-exported here for backward compatibility.

from .validation import (
    _require_context,
    _dynamic_context_missing,
    _validate_thread_context,
)


# ============================================================================
# Branch Validation and Sync Helpers
# ============================================================================


def _attempt_auto_fix_divergence(
    context: ThreadContext,
    validation_result: BranchPairingResult,
) -> Optional[BranchPairingResult]:
    """Attempt to auto-fix branch history divergence via rebase or merge.

    Args:
        context: Thread context with code and threads repo info
        validation_result: The failed validation result containing divergence info

    Returns:
        New BranchPairingResult on successful fix, None on failure.
        On failure, raises BranchPairingError with details.

    Raises:
        BranchPairingError: If auto-fix fails and requires manual intervention
    """
    log_debug("Detected branch history divergence, attempting auto-fix")

    # Check if this is a "merge to main" case (code PR merged, threads needs to follow)
    merge_to_main_mismatch = None
    for mismatch in validation_result.mismatches:
        if mismatch.type == "branch_history_diverged" and mismatch.needs_merge_to_main:
            merge_to_main_mismatch = mismatch
            break

    # Handle merge-to-main case: code branch merged to main, threads should follow
    # Apply conservative evidence gating: only attempt auto-merge when the validation
    # result explicitly requests merge-to-main (needs_merge_to_main=True) AND a main
    # branch exists. Otherwise, surface the mismatch for explicit resolution.
    if merge_to_main_mismatch:
        try:
            threads_repo = Repo(context.threads_dir, search_parent_directories=True)
            main_branch = _find_main_branch(threads_repo)
            if not main_branch:
                raise BranchPairingError(
                    "Cannot auto-merge: main branch not found in threads repo"
                )

            feature_branch = validation_result.threads_branch or context.code_branch

            success, message = auto_merge_to_main(
                threads_repo,
                feature_branch,
                main_branch,
            )

            if success:
                log_debug(f"Auto-merged threads to main: {message}")
                # Re-validate to confirm fix worked
                revalidation = validate_branch_pairing(
                    code_repo=context.code_root,
                    threads_repo=context.threads_dir,
                    strict=True,
                    check_history=True,
                )
                if revalidation.valid:
                    log_debug("Branch pairing now valid after auto-merge to main")
                    return revalidation
                else:
                    log_debug(
                        f"Auto-merge completed but validation still failing: "
                        f"{revalidation.warnings}"
                    )
                    return revalidation
            else:
                # Surface for explicit resolution
                raise BranchPairingError(
                    f"Auto-merge to main failed: {message}\n"
                    f"Manual recovery: cd <threads-repo> && git checkout main && "
                    f"git merge {feature_branch} && git push origin main"
                )
        except BranchPairingError:
            raise
        except Exception as e:
            raise BranchPairingError(f"Auto-merge to main failed: {e}")

    # Check if this is a "behind-main" divergence (threads behind main but code not)
    # vs a local-vs-origin divergence. They require different fix strategies.
    behind_main_mismatch = None
    for mismatch in validation_result.mismatches:
        if mismatch.type == "branch_history_diverged" and "behind main" in mismatch.recovery.lower():
            behind_main_mismatch = mismatch
            break

    # Determine the target for rebase
    onto_branch: Optional[str] = None
    if behind_main_mismatch:
        # Need to rebase onto main, not origin/branch
        try:
            threads_repo = Repo(context.threads_dir, search_parent_directories=True)
            onto_branch = _find_main_branch(threads_repo)
            if onto_branch:
                log_debug(
                    f"Behind-main divergence detected, will rebase onto {onto_branch}"
                )
            else:
                log_debug(
                    "Behind-main divergence detected but couldn't find main branch"
                )
        except Exception as e:
            log_debug(f"Error finding main branch: {e}")

    try:
        sync_result = sync_branch_history(
            threads_repo_path=context.threads_dir,
            branch=validation_result.threads_branch or context.code_branch,
            strategy="rebase",
            force=True,  # Uses --force-with-lease for safety
            onto=onto_branch,  # None for origin/branch, "main" for behind-main fix
        )

        if not sync_result.success:
            log_debug(f"Auto-fix failed: {sync_result.details}")
            error_parts = [
                "Branch history divergence detected and auto-fix failed:",
                f"  Code branch: {validation_result.code_branch or '(detached/unknown)'}",
                f"  Threads branch: {validation_result.threads_branch or '(detached/unknown)'}",
                f"  Fix attempt: {sync_result.details}",
            ]
            if sync_result.needs_manual_resolution:
                error_parts.append("  Manual resolution required.")
            error_parts.append(
                "\nManual recovery: watercooler_sync_branch_state with operation='recover'"
            )
            raise BranchPairingError("\n".join(error_parts))

        log_debug(f"Auto-fixed branch divergence: {sync_result.details}")

        # Re-validate to confirm fix worked
        revalidation = validate_branch_pairing(
            code_repo=context.code_root,
            threads_repo=context.threads_dir,
            strict=True,
            check_history=True,
        )

        if revalidation.valid:
            log_debug("Branch pairing now valid after auto-fix")
            return revalidation
        else:
            log_debug(
                f"Auto-fix completed but validation still failing: "
                f"{revalidation.warnings}"
            )
            # Return the updated result so caller can report remaining issues
            return revalidation

    except BranchPairingError:
        raise
    except Exception as fix_error:
        log_debug(f"Auto-fix exception: {fix_error}")
        error_parts = [
            "Branch history divergence detected, auto-fix failed:",
            f"  Code branch: {validation_result.code_branch or '(detached/unknown)'}",
            f"  Threads branch: {validation_result.threads_branch or '(detached/unknown)'}",
            f"  Error: {fix_error}",
            "\nManual recovery: watercooler_sync_branch_state with operation='recover'",
        ]
        raise BranchPairingError("\n".join(error_parts))


def _validate_and_sync_branches(
    context: ThreadContext,
    skip_validation: bool = False,
) -> None:
    """Validate branch pairing and sync branches if needed.

    This helper is used by both read and write operations to ensure
    the threads repo is on the correct branch before any operation.

    Includes automatic detection and repair of:
    1. Branch name mismatch: Checks out threads repo to match code repo branch
    2. Branch history divergence: Rebases threads branch after code repo rebase/force-push

    When auto-fix is enabled (WATERCOOLER_AUTO_BRANCH=1, default), these issues
    are resolved automatically. If auto-fix fails, raises BranchPairingError.

    Side effects:
        - May checkout threads repo to different branch
        - May rebase threads branch to match code branch history
        - May push to remote with --force-with-lease if divergence detected
        - Blocks operation if conflicts occur during auto-fix

    Args:
        context: Thread context with code and threads repo info
        skip_validation: If True, skip strict validation (used for recovery operations)

    Raises:
        BranchPairingError: If branch validation fails and auto-fix is not possible,
                           or if auto-fix encounters conflicts requiring manual resolution
    """
    sync = get_git_sync_manager_from_context(context)
    if not sync:
        return

    # Validate branch pairing before any operation
    if not skip_validation and context.code_root and context.threads_dir:
        try:
            validation_result = validate_branch_pairing(
                code_repo=context.code_root,
                threads_repo=context.threads_dir,
                strict=True,
                check_history=True,  # Enable divergence detection
            )
            if not validation_result.valid:
                # Check if this is a branch name mismatch we can auto-fix via checkout
                branch_mismatch: Optional[BranchMismatch] = next(
                    (
                        m
                        for m in validation_result.mismatches
                        if m.type == "branch_name_mismatch"
                    ),
                    None,
                )

                if branch_mismatch and context.code_branch and _should_auto_branch():
                    log_debug(
                        f"Branch name mismatch detected, auto-fixing via checkout "
                        f"to {context.code_branch}"
                    )
                    try:
                        sync.ensure_branch(context.code_branch)
                        # Re-validate after branch checkout
                        validation_result = validate_branch_pairing(
                            code_repo=context.code_root,
                            threads_repo=context.threads_dir,
                            strict=True,
                            check_history=True,
                        )
                        if validation_result.valid:
                            log_debug(
                                f"Branch name mismatch auto-fixed: checked out to "
                                f"{context.code_branch}"
                            )
                        else:
                            log_debug(
                                f"Branch checkout completed but validation still "
                                f"failing: {validation_result.warnings}"
                            )
                    except Exception as e:
                        log_debug(f"Auto-fix branch checkout failed: {e}")

                # Check if this is a history divergence we can auto-fix
                history_mismatch: Optional[BranchMismatch] = next(
                    (
                        m
                        for m in validation_result.mismatches
                        if m.type == "branch_history_diverged"
                    ),
                    None,
                )

                if history_mismatch:
                    # Attempt auto-fix - may raise BranchPairingError on failure
                    validation_result = _attempt_auto_fix_divergence(
                        context, validation_result
                    )

                # Unified error reporting for any remaining validation failures
                # (non-history issues, or edge case where auto-fix succeeded but
                # other mismatches remain)
                if not validation_result.valid:
                    error_parts = [
                        "Branch pairing validation failed:",
                        f"  Code branch: {validation_result.code_branch or '(detached/unknown)'}",
                        f"  Threads branch: {validation_result.threads_branch or '(detached/unknown)'}",
                    ]
                    if validation_result.mismatches:
                        error_parts.append("\nMismatches:")
                        for mismatch in validation_result.mismatches:
                            error_parts.append(
                                f"  - {mismatch.type}: {mismatch.recovery}"
                            )
                    if validation_result.warnings:
                        error_parts.append("\nWarnings:")
                        for warning in validation_result.warnings:
                            error_parts.append(f"  - {warning}")
                    error_parts.append(
                        "\nRun: watercooler_sync_branch_state with "
                        "operation='checkout' to sync branches"
                    )
                    raise BranchPairingError("\n".join(error_parts))
        except BranchPairingError:
            raise
        except Exception as e:
            # Log but don't block on validation errors (e.g., repo not initialized)
            log_debug(f"Branch validation warning: {e}")


# _refresh_threads is now in validation.py - re-export for backward compatibility
from .validation import _refresh_threads  # noqa: F401 (re-export)


# ============================================================================
# Thread Parsing Helpers
# ============================================================================


def _normalize_status(s: str) -> str:
    """Normalize status string to lowercase."""
    return s.strip().lower()


def _extract_thread_metadata(content: str, topic: str) -> tuple[str, str, str, str]:
    """Extract thread metadata from content string without re-reading the file.

    DEPRECATED: For local mode, prefer _get_thread_metadata_graph_first() which
    reads from the canonical graph. This MD-parsing version is still needed for
    hosted mode where we only have GitHub API content.

    Args:
        content: Full thread markdown content
        topic: Thread topic (used as fallback for title)

    Returns:
        Tuple of (title, status, ball, last_entry_timestamp)
    """
    title_match = _TITLE_RE.search(content)
    title = title_match.group("val").strip() if title_match else topic

    status_match = _STAT_RE.search(content)
    status = _normalize_status(status_match.group("val") if status_match else "open")

    ball_match = _BALL_RE.search(content)
    ball = ball_match.group("val").strip() if ball_match else "unknown"

    # Extract last entry timestamp
    hits = list(_ENTRY_RE.finditer(content))
    last = hits[-1].group("ts").strip() if hits else fs.utcnow_iso()

    return title, status, ball, last


def _get_thread_metadata_graph_first(
    threads_dir: Path, topic: str, content: str | None = None
) -> tuple[str, str, str, str]:
    """Get thread metadata from graph with MD fallback.

    Graph-first: reads from canonical graph JSONL. Falls back to MD parsing
    if graph data is not available.

    Args:
        threads_dir: Threads directory
        topic: Thread topic
        content: Optional MD content (avoids re-reading file if already loaded)

    Returns:
        Tuple of (title, status, ball, last_entry_timestamp)
    """
    # Try graph first
    if _use_graph_for_reads(threads_dir):
        try:
            result = read_thread_from_graph(threads_dir, topic)
            if result:
                graph_thread, graph_entries = result
                last_ts = (
                    graph_entries[-1].timestamp
                    if graph_entries
                    else graph_thread.last_updated
                )
                return (
                    graph_thread.title,
                    graph_thread.status,
                    graph_thread.ball,
                    last_ts,
                )
        except Exception as e:
            log_debug(f"[GRAPH] Failed to get metadata from graph: {e}")

    # Fallback to MD parsing
    if content is None:
        thread_path = threads_dir / f"{topic}.md"
        if thread_path.exists():
            content = fs.read_body(thread_path)
        else:
            return topic, "open", "unknown", fs.utcnow_iso()

    return _extract_thread_metadata(content, topic)


def _resolve_format(
    value: str | None, *, default: str = "markdown"
) -> tuple[str | None, str]:
    fmt = (value or "").strip().lower()
    if not fmt:
        return (None, default)
    if fmt not in _ALLOWED_FORMATS:
        allowed = ", ".join(sorted(_ALLOWED_FORMATS))
        return (
            f"Error: unsupported format '{value}'. Allowed formats: {allowed}.",
            default,
        )
    return (None, fmt)


# ============================================================================
# Entry Loading Helpers
# ============================================================================


def _load_thread_entries(
    topic: str, context: ThreadContext
) -> tuple[str | None, list[ThreadEntry]]:
    """Load and parse thread entries from disk.

    Thread Safety Note:
        This function performs unlocked reads. This is safe because:
        - Write operations (say, ack, handoff) use AdvisoryLock for serialization
        - Reads may see partially written entries, but won't corrupt existing ones
        - Thread entry boundaries (---) ensure partial writes don't break parsing
        - File system guarantees atomic writes at the block level
        - MCP tool calls are typically infrequent enough that read/write races are rare

    For high-concurrency scenarios, consider adding shared/exclusive locking
    or caching with mtime-based invalidation.
    """
    threads_dir = context.threads_dir
    thread_path = fs.thread_path(topic, threads_dir)

    if not thread_path.exists():
        if threads_dir.exists():
            available_list = sorted(p.stem for p in threads_dir.glob("*.md"))
            if len(available_list) > 10:
                available = (
                    ", ".join(available_list[:10])
                    + f" (and {len(available_list) - 10} more)"
                )
            else:
                available = ", ".join(available_list) if available_list else "none"
        else:
            available = "none"
        return (
            f"Error: Thread '{topic}' not found in {threads_dir}\n\n"
            f"Available threads: {available}",
            [],
        )

    content = fs.read_body(thread_path)
    entries = parse_thread_entries(content)
    return (None, entries)


def _entry_header_payload(entry: ThreadEntry) -> Dict[str, object]:
    return {
        "index": entry.index,
        "entry_id": entry.entry_id,
        "agent": entry.agent,
        "timestamp": entry.timestamp,
        "role": entry.role,
        "type": entry.entry_type,
        "title": entry.title,
        "header": entry.header,
        "start_line": entry.start_line,
        "end_line": entry.end_line,
        "start_offset": entry.start_offset,
        "end_offset": entry.end_offset,
    }


def _entry_full_payload(entry: ThreadEntry) -> Dict[str, object]:
    """Convert ThreadEntry to full JSON payload including body content.

    Note on whitespace handling:
        - 'body' field preserves original whitespace from the thread file
        - 'markdown' field uses stripped body to avoid trailing whitespace in output
        This ensures markdown rendering is clean while preserving original content.

    Args:
        entry: ThreadEntry to convert

    Returns:
        Dictionary with entry metadata, body, and markdown representation
    """
    data = _entry_header_payload(entry)
    # Handle whitespace-only bodies as empty
    body_content = entry.body.strip() if entry.body else ""
    data.update(
        {
            "body": entry.body,  # Preserve original whitespace
            "markdown": entry.header
            + ("\n\n" + body_content if body_content else ""),  # Clean output
        }
    )
    return data


# ============================================================================
# Graph-First Read Helpers
# ============================================================================


def _use_graph_for_reads(threads_dir: Path) -> bool:
    """Check if graph should be used for read operations.

    The graph is used when:
    1. WATERCOOLER_USE_GRAPH env var is set to "1" (explicit opt-in)
    2. OR graph data exists and is available

    This allows graceful fallback - if graph doesn't exist or is broken,
    we fall back to markdown parsing.
    """
    explicit_opt_in = config.env.get("WATERCOOLER_USE_GRAPH", "0") == "1"
    if explicit_opt_in:
        return is_graph_available(threads_dir)
    # Auto-use graph if available and not explicitly disabled
    auto_use = config.env.get("WATERCOOLER_USE_GRAPH", "auto") == "auto"
    if auto_use:
        return is_graph_available(threads_dir)
    return False


def _track_access(threads_dir: Path, node_type: str, node_id: str) -> None:
    """Safely track access to a node (thread or entry).

    This is a non-blocking operation - errors are logged but don't fail the read.
    Only tracks if graph features are enabled.

    Args:
        threads_dir: Threads directory
        node_type: "thread" or "entry"
        node_id: Topic (for threads) or entry_id (for entries)
    """
    # TODO: Counter writes disabled - they dirty the tree and block auto-sync.
    # See thread: graph-access-counters-sync-strategy for design discussion.
    # Re-enable once per-system counter files or deferred writes are implemented.
    return
    if not _use_graph_for_reads(threads_dir):
        return
    try:
        increment_access_count(threads_dir, node_type, node_id)
    except Exception as e:
        log_debug(f"[ODOMETER] Failed to track {node_type}:{node_id} access: {e}")


def _graph_entry_to_thread_entry(
    graph_entry: GraphEntry, full_body: str | None = None
) -> ThreadEntry:
    """Convert GraphEntry to ThreadEntry for compatibility with existing code.

    Args:
        graph_entry: Entry from graph
        full_body: Optional full body if retrieved from markdown
    """
    # Build header line in expected format
    header = f"Entry: {graph_entry.agent} {graph_entry.timestamp}\n"
    header += f"Role: {graph_entry.role}\n"
    header += f"Type: {graph_entry.entry_type}\n"
    header += f"Title: {graph_entry.title}"

    body = full_body if full_body else graph_entry.body or graph_entry.summary or ""

    return ThreadEntry(
        index=graph_entry.index,
        header=header,
        body=body,
        agent=graph_entry.agent,
        timestamp=graph_entry.timestamp,
        role=graph_entry.role,
        entry_type=graph_entry.entry_type,
        title=graph_entry.title,
        entry_id=graph_entry.entry_id,
    )


def _load_thread_entries_graph_first(
    topic: str,
    context: ThreadContext,
) -> tuple[str | None, list[ThreadEntry]]:
    """Load thread entries from canonical graph JSONL, with legacy markdown backfill.

    Canonical source of truth is the baseline graph JSONL (`graph/baseline/*`) in the
    threads repo. Markdown is a derived, human-friendly projection.

    This function prefers graph reads. If graph data is missing/stale (e.g., older
    repos or incomplete backfills), it may temporarily fall back to markdown parsing
    to keep UX usable, and optionally auto-repair the graph from markdown. That
    fallback path is compatibility-only and should shrink over time.

    Args:
        topic: Thread topic
        context: Thread context

    Returns:
        Tuple of (error_message, entries). Error is None on success.
    """
    threads_dir = context.threads_dir

    # Try graph first if available
    if _use_graph_for_reads(threads_dir):
        try:
            result = read_thread_from_graph(threads_dir, topic)
            if not result:
                log_debug(f"[GRAPH] Topic '{topic}' not in graph, falling back to markdown")
            if result:
                graph_thread, graph_entries = result
                # Graph entries may not have full body - need to get from markdown
                # For now, use summaries from graph (bodies are optional in graph)
                thread_path = fs.thread_path(topic, threads_dir)
                if thread_path.exists():
                    # Parse markdown to check graph completeness
                    content = fs.read_body(thread_path)
                    md_entries = parse_thread_entries(content)

                    # Check if graph is stale (fewer entries than markdown)
                    if len(graph_entries) < len(md_entries):
                        log_debug(
                            f"[GRAPH] Graph stale for {topic}: "
                            f"{len(graph_entries)} graph vs {len(md_entries)} markdown. "
                            "Auto-repairing from markdown."
                        )
                        # Auto-repair: sync full thread to graph
                        try:
                            from watercooler.baseline_graph.sync import (
                                sync_thread_to_graph,
                            )
                            from watercooler_mcp.config import get_watercooler_config

                            wc_config = get_watercooler_config()
                            graph_config = wc_config.mcp.graph

                            sync_result = sync_thread_to_graph(
                                threads_dir=threads_dir,
                                topic=topic,
                                generate_summaries=graph_config.generate_summaries,
                                generate_embeddings=graph_config.generate_embeddings,
                            )
                            if sync_result:
                                log_debug(f"[GRAPH] Auto-repair succeeded for {topic}")
                                # Re-read from graph after repair
                                repaired = read_thread_from_graph(threads_dir, topic)
                                if repaired:
                                    _, graph_entries = repaired
                            else:
                                log_debug(
                                    "[GRAPH] Auto-repair failed, using markdown entries"
                                )
                                return (None, md_entries)
                        except Exception as repair_err:
                            log_debug(
                                f"[GRAPH] Auto-repair error: {repair_err}, "
                                "using markdown"
                            )
                            return (None, md_entries)

                    # Merge: use graph metadata with markdown bodies
                    entries = []
                    for ge in graph_entries:
                        # Find matching markdown entry by index
                        md_entry = next(
                            (e for e in md_entries if e.index == ge.index),
                            None,
                        )
                        if md_entry:
                            entries.append(md_entry)
                        else:
                            # Use graph entry with summary as body
                            entries.append(_graph_entry_to_thread_entry(ge))
                    log_debug(
                        f"[GRAPH] Loaded {len(entries)} entries from graph for {topic}"
                    )
                    return (None, entries)
                else:
                    # No markdown, use graph entries directly
                    entries = [_graph_entry_to_thread_entry(ge) for ge in graph_entries]
                    log_debug(
                        f"[GRAPH] Loaded {len(entries)} entries from graph only "
                        f"for {topic}"
                    )
                    return (None, entries)
        except Exception as e:
            log_debug(
                f"[GRAPH] Failed to load from graph, falling back to markdown: {e}"
            )

    # Fallback to markdown parsing
    log_debug(f"[GRAPH] Using markdown fallback for '{topic}' entries")
    return _load_thread_entries(topic, context)


def _list_threads_graph_first(
    threads_dir: Path,
    open_only: bool | None = None,
) -> list[tuple[str, str, str, str, Path, bool]]:
    """List threads from canonical graph JSONL, with legacy markdown backfill.

    Args:
        threads_dir: Threads directory
        open_only: Filter by status

    Returns:
        List of thread tuples (title, status, ball, updated, path, is_new)
    """
    # Try graph first if available
    if _use_graph_for_reads(threads_dir):
        try:
            graph_threads = list_threads_from_graph(threads_dir, open_only)
            if not graph_threads:
                log_debug("[GRAPH] No threads in graph, falling back to markdown")
            if graph_threads:
                # Convert to expected tuple format
                result = []
                for gt in graph_threads:
                    thread_path = threads_dir / f"{gt.topic}.md"
                    # is_new would require checking against agent's last contribution
                    # For now, set to False - the markdown fallback handles this
                    is_new = False
                    result.append(
                        (
                            gt.title,
                            gt.status,
                            gt.ball,
                            gt.last_updated,
                            thread_path,
                            is_new,
                        )
                    )
                log_debug(f"[GRAPH] Listed {len(result)} threads from graph")
                return result
        except Exception as e:
            log_debug(
                f"[GRAPH] Failed to list from graph, falling back to markdown: {e}"
            )

    # Fallback to markdown
    log_debug("[GRAPH] Using markdown fallback for list_threads")
    return commands.list_threads(threads_dir=threads_dir, open_only=open_only)


# ============================================================================
# Commit Footer Helpers
# ============================================================================


def _build_commit_footers(
    context: ThreadContext,
    *,
    topic: str | None = None,
    entry_id: str | None = None,
    agent_spec: str | None = None,
) -> list[str]:
    footers: list[str] = []
    if entry_id:
        footers.append(f"Watercooler-Entry-ID: {entry_id}")
    if topic:
        footers.append(f"Watercooler-Topic: {topic}")
    if context.code_repo:
        footers.append(f"Code-Repo: {context.code_repo}")
    if context.code_branch:
        footers.append(f"Code-Branch: {context.code_branch}")
    if context.code_commit:
        footers.append(f"Code-Commit: {context.code_commit}")
    if agent_spec:
        footers.append(f"Spec: {agent_spec}")
    return footers
