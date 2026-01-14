"""Thread query tools for watercooler MCP server.

Tools:
- watercooler_list_threads: List all threads
- watercooler_read_thread: Read full thread content
- watercooler_list_thread_entries: List entry headers with pagination
- watercooler_get_thread_entry: Get single entry by index/ID
- watercooler_get_thread_entry_range: Get contiguous range of entries

Modes:
- Local (stdio): Uses filesystem operations and git sync
- Hosted (HTTP): Uses GitHub API via hosted_ops module
"""

import json
import time
from pathlib import Path

from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from watercooler import fs
from watercooler.thread_entries import ThreadEntry

from ..config import (
    get_agent_name,
    get_git_sync_manager_from_context,
    resolve_thread_context,
)
from ..helpers import (
    # Constants
    _MAX_LIMIT,
    _MAX_OFFSET,
    # Startup warnings
    _get_startup_warnings,
    _format_warnings_for_response,
    # Thread parsing
    _extract_thread_metadata,
    _resolve_format,
    # Entry loading
    _entry_header_payload,
    _entry_full_payload,
    # Graph helpers
    _track_access,
    _load_thread_entries_graph_first,
    _list_threads_graph_first,
)
from ..errors import (
    ContextError,
    EntryNotFoundError,
    HostedModeError,
    IndexOutOfRangeError,
    ThreadNotFoundError,
    ValidationError,
)
from ..hosted_ops import (
    list_threads_hosted,
    read_thread_hosted,
    load_thread_entries_hosted,
    thread_exists_hosted,
)
from ..sync import ensure_readable
from ..observability import log_debug, log_error
from .. import validation  # Import module for runtime access (enables test patching)
from ..validation import is_hosted_context


# Module-level references to registered tools (populated by register_thread_query_tools)
list_threads = None
read_thread = None
list_thread_entries = None
get_thread_entry = None
get_thread_entry_range = None


def _list_threads_impl(
    ctx: Context,
    open_only: bool | None = None,
    limit: int = 50,
    cursor: str | None = None,
    format: str = "markdown",
    code_path: str = "",
) -> ToolResult:
    """List all watercooler threads.

    Shows threads where you have the ball (actionable items), threads where
    you're waiting on others, and marks NEW entries since you last contributed.

    Args:
        open_only: Filter by open status (True=open only, False=closed only, None=all)
        limit: Maximum threads to return (Phase 1A: ignored, returns all)
        cursor: Pagination cursor (Phase 1A: ignored, no pagination)
        format: Output format - "markdown" or "json" (Phase 1A: only "markdown" supported)
        code_path: Path to the code repository directory containing the files most immediately
            under discussion. This establishes the code context for branch pairing.
            Should point to the root of your working repository.

    Returns:
        Formatted thread list with:
        - Threads where you have the ball (🎾 marker)
        - Threads with NEW entries for you to read
        - Thread status and last update time

    Phase 1A notes:
        - format must be "markdown" (JSON support in Phase 1B)
        - limit and cursor are ignored (pagination in Phase 1B)
    """
    try:
        start_ts = time.time()
        if format != "markdown":
            raise ValidationError(
                "Phase 1A only supports format='markdown'. JSON support coming in Phase 1B.",
                field="format",
            )

        error, context = validation._require_context(code_path)
        if error:
            raise ContextError(error, code_path=code_path)
        if context is None:
            raise ContextError(
                "Unable to resolve code context for the provided code_path.",
                code_path=code_path,
            )
        log_debug(f"list_threads start code_path={code_path!r} open_only={open_only}")

        # =====================================================================
        # Hosted Mode Path (GitHub API)
        # =====================================================================
        if is_hosted_context(context):
            log_debug("list_threads: using hosted mode (GitHub API)")
            agent = get_agent_name(ctx.client_id)

            list_error, hosted_threads = list_threads_hosted(open_only=open_only)
            if list_error:
                raise HostedModeError(list_error, operation="list_threads")

            if not hosted_threads:
                status_filter = "open " if open_only is True else ("closed " if open_only is False else "")
                return ToolResult(content=[TextContent(type="text", text=f"No {status_filter}threads found in repository: {context.code_repo}")])

            # Format output for hosted mode
            output = []
            output.append(f"# Watercooler Threads ({len(hosted_threads)} total)\n")

            # Classify threads by ball ownership
            agent_lower = agent.lower()
            your_turn = []
            waiting = []

            for ht in hosted_threads:
                ball_lower = (ht.ball or "").lower()
                has_ball = ball_lower == agent_lower
                if has_ball:
                    your_turn.append(ht)
                else:
                    waiting.append(ht)

            # Your turn section
            if your_turn:
                output.append(f"\n## 🎾 Your Turn ({len(your_turn)} threads)\n")
                for ht in your_turn:
                    output.append(f"- **{ht.topic}** - {ht.title}")
                    output.append(f"  Status: {ht.status} | Ball: {ht.ball} | Updated: {ht.last_updated}")

            # Waiting section
            if waiting:
                output.append(f"\n## ⏳ Waiting on Others ({len(waiting)} threads)\n")
                for ht in waiting:
                    output.append(f"- **{ht.topic}** - {ht.title}")
                    output.append(f"  Status: {ht.status} | Ball: {ht.ball} | Updated: {ht.last_updated}")

            output.append(f"\n---\n*You are: {agent}*")
            output.append(f"*Repository: {context.code_repo}*")

            response = "\n".join(output)
            log_debug(f"list_threads hosted mode: returning {len(hosted_threads)} threads")
            return ToolResult(content=[TextContent(type="text", text=_format_warnings_for_response(response))])

        # =====================================================================
        # Local Mode Path (Filesystem)
        # =====================================================================
        if context and validation._dynamic_context_missing(context):
            log_debug("list_threads dynamic context missing")
            raise ContextError(
                "Dynamic threads repo was not resolved from your git context. "
                "Run from inside your code repo or set WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO on the MCP server.",
                code_path=code_path,
            )

        agent = get_agent_name(ctx.client_id)

        # Lightweight read sync: auto-pull if behind origin (never blocks)
        sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
        if sync_actions:
            log_debug(f"list_threads read sync: {sync_actions}")

        log_debug("list_threads refreshing git state")
        git_start = time.time()
        validation._refresh_threads(context)
        git_elapsed = time.time() - git_start
        log_debug(f"list_threads git refreshed in {git_elapsed:.2f}s")
        threads_dir = context.threads_dir

        # Create threads directory if it doesn't exist
        if not threads_dir.exists():
            threads_dir.mkdir(parents=True, exist_ok=True)
            log_debug("list_threads created empty threads directory")
            return ToolResult(content=[TextContent(type="text", text=f"No threads found. Threads directory created at: {threads_dir}\n\nCreate your first thread with watercooler_say.")])

        # Get thread list (canonical graph-first; markdown is legacy/backfill only)
        scan_start = time.time()
        threads = _list_threads_graph_first(threads_dir, open_only=open_only)
        scan_elapsed = time.time() - scan_start
        log_debug(f"list_threads scanned {len(threads)} threads in {scan_elapsed:.2f}s")

        sync = get_git_sync_manager_from_context(context)
        pending_topics: set[str] = set()
        async_summary = ""
        if sync:
            status_info = sync.get_async_status()
            if status_info.get("mode") == "async":
                pending_topics = {topic for topic in (status_info.get("pending_topics") or []) if topic}
                summary_parts: list[str] = []
                if status_info.get("is_syncing"):
                    summary_parts.append("syncing…")
                last_pull_age = status_info.get("last_pull_age_seconds")
                if status_info.get("last_pull"):
                    age_fragment = f"{int(last_pull_age)}s ago" if last_pull_age is not None else "recently"
                    if status_info.get("stale"):
                        age_fragment += " (stale)"
                    summary_parts.append(f"last refresh {age_fragment}")
                else:
                    summary_parts.append("no refresh yet")
                next_eta = status_info.get("next_pull_eta_seconds")
                if next_eta is not None:
                    summary_parts.append(f"next sync in {int(next_eta)}s")
                summary_parts.append(f"pending {status_info.get('pending', 0)}")
                async_summary = "*Async sync: " + ", ".join(summary_parts) + "*\n"

        if not threads:
            status_filter = "open " if open_only is True else ("closed " if open_only is False else "")
            log_debug(f"list_threads no {status_filter or ''}threads found")
            return ToolResult(content=[TextContent(type="text", text=f"No {status_filter}threads found in: {threads_dir}")])

        # Format output
        agent_lower = agent.lower()
        output = []
        output.append(f"# Watercooler Threads ({len(threads)} total)\n")
        if async_summary:
            output.append(async_summary)

        # Separate threads by ball ownership
        classify_start = time.time()
        your_turn = []
        waiting = []
        new_entries = []

        for title, status, ball, updated, path, is_new in threads:
            topic = path.stem
            ball_lower = (ball or "").lower()
            has_ball = ball_lower == agent_lower

            if is_new:
                new_entries.append((title, status, ball, updated, topic, has_ball))
            elif has_ball:
                your_turn.append((title, status, ball, updated, topic, has_ball))
            else:
                waiting.append((title, status, ball, updated, topic, has_ball))
        classify_elapsed = time.time() - classify_start
        log_debug(f"list_threads classified threads in {classify_elapsed:.2f}s (your_turn={len(your_turn)} waiting={len(waiting)} new={len(new_entries)})")

        # Your turn section
        render_start = time.time()
        if your_turn:
            output.append(f"\n## 🎾 Your Turn ({len(your_turn)} threads)\n")
            for title, status, ball, updated, topic, _ in your_turn:
                local_marker = " ⏳" if topic in pending_topics else ""
                updated_label = updated + (" (local)" if topic in pending_topics else "")
                output.append(f"- **{topic}**{local_marker} - {title}")
                output.append(f"  Status: {status} | Ball: {ball} | Updated: {updated_label}")

        # NEW entries section
        if new_entries:
            output.append(f"\n## 🆕 NEW Entries for You ({len(new_entries)} threads)\n")
            for title, status, ball, updated, topic, has_ball in new_entries:
                marker = "🎾 " if has_ball else ""
                local_marker = " ⏳" if topic in pending_topics else ""
                updated_label = updated + (" (local)" if topic in pending_topics else "")
                output.append(f"- {marker}**{topic}**{local_marker} - {title}")
                output.append(f"  Status: {status} | Ball: {ball} | Updated: {updated_label}")

        # Waiting section
        if waiting:
            output.append(f"\n## ⏳ Waiting on Others ({len(waiting)} threads)\n")
            for title, status, ball, updated, topic, _ in waiting:
                local_marker = " ⏳" if topic in pending_topics else ""
                updated_label = updated + (" (local)" if topic in pending_topics else "")
                output.append(f"- **{topic}**{local_marker} - {title}")
                output.append(f"  Status: {status} | Ball: {ball} | Updated: {updated_label}")

        output.append(f"\n---\n*You are: {agent}*")
        output.append(f"*Threads dir: {threads_dir}*")

        response = "\n".join(output)
        render_elapsed = time.time() - render_start
        log_debug(f"list_threads rendered markdown sections in {render_elapsed:.2f}s")
        duration = time.time() - start_ts
        log_debug(
            f"list_threads formatted response in "
            f"{duration:.2f}s (total={len(threads)} new={len(new_entries)} "
            f"your_turn={len(your_turn)} waiting={len(waiting)} "
            f"chars={len(response)})"
        )
        log_debug("list_threads returning response")
        return ToolResult(content=[TextContent(type="text", text=_format_warnings_for_response(response))])

    except (ValidationError, ContextError, HostedModeError, ThreadNotFoundError) as e:
        # Re-raise custom exceptions for proper JSON-RPC error response
        raise
    except Exception as e:
        log_error(f"list_threads unexpected error: {e}")
        raise HostedModeError(f"Unexpected error listing threads: {e}", operation="list_threads")


def _read_thread_impl(
    topic: str,
    from_entry: int = 0,
    limit: int = 100,
    format: str = "markdown",
    code_path: str = "",
) -> str:
    """Read the complete content of a watercooler thread.

    Args:
        topic: Thread topic identifier (e.g., "feature-auth")
        from_entry: Starting entry index for pagination (Phase 1A: ignored)
        limit: Maximum entries to include (Phase 1A: ignored, returns all)
        format: Output format - "markdown" or "json" (Phase 1A: only "markdown" supported)
        code_path: Path to the code repository directory containing the files most immediately
            under discussion. This establishes the code context for branch pairing.
            Should point to the root of your working repository.

    Returns:
        Full thread content including:
        - Thread metadata (status, ball owner, participants)
        - All entries with timestamps, authors, roles, and types
        - Current ball ownership status

    Phase 1A notes:
        - format must be "markdown" (JSON support in Phase 1B)
        - from_entry and limit are ignored (pagination in Phase 1B)
    """
    try:
        fmt_error, resolved_format = _resolve_format(format, default="markdown")
        if fmt_error:
            raise ValidationError(fmt_error, field="format")

        error, context = validation._require_context(code_path)
        if error:
            raise ContextError(error, code_path=code_path)
        if context is None:
            raise ContextError(
                "Unable to resolve code context for the provided code_path.",
                code_path=code_path,
            )

        # =====================================================================
        # Hosted Mode Path (GitHub API)
        # =====================================================================
        if is_hosted_context(context):
            log_debug(f"read_thread: using hosted mode for topic={topic}")

            read_error, content = read_thread_hosted(topic)
            if read_error:
                # Check if it's a "not found" error
                if "not found" in read_error.lower():
                    raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
                raise HostedModeError(read_error, operation="read_thread")

            if resolved_format == "markdown":
                return _format_warnings_for_response(content)

            # For JSON format, parse entries
            load_error, entries = load_thread_entries_hosted(topic)
            if load_error:
                raise HostedModeError(load_error, operation="load_entries")

            # Extract metadata from content
            header_block = content.split("---", 1)[0].strip() if "---" in content else ""
            title, status, ball, last = _extract_thread_metadata(content, topic)

            payload = {
                "topic": topic,
                "format": "json",
                "entry_count": len(entries),
                "meta": {
                    "title": title,
                    "status": status,
                    "ball": ball,
                    "last_entry_at": last,
                    "header": header_block,
                },
                "entries": [_entry_full_payload(entry) for entry in entries],
            }
            warnings = _get_startup_warnings()
            if warnings:
                payload["_warnings"] = warnings
            return json.dumps(payload, indent=2)

        # =====================================================================
        # Local Mode Path (Filesystem)
        # =====================================================================
        if validation._dynamic_context_missing(context):
            raise ContextError(
                "Dynamic threads repo was not resolved from your git context. "
                "Run from inside your code repo or set WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO.",
                code_path=code_path,
            )

        # Lightweight read sync: auto-pull if behind origin (never blocks)
        sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
        if sync_actions:
            log_debug(f"read_thread read sync: {sync_actions}")

        validation._refresh_threads(context)
        threads_dir = context.threads_dir

        # Create threads directory if it doesn't exist
        if not threads_dir.exists():
            threads_dir.mkdir(parents=True, exist_ok=True)

        thread_path = fs.thread_path(topic, threads_dir)

        if not thread_path.exists():
            raise ThreadNotFoundError(topic=topic)

        # Track thread access (non-blocking)
        _track_access(threads_dir, "thread", topic)

        # Read full thread content
        content = fs.read_body(thread_path)
        if resolved_format == "markdown":
            return _format_warnings_for_response(content)

        # For JSON format, use graph-first loading
        load_error, entries = _load_thread_entries_graph_first(topic, context)
        if load_error:
            return load_error

        # Extract metadata from content
        header_block = content.split("---", 1)[0].strip() if "---" in content else ""
        title, status, ball, last = _extract_thread_metadata(content, topic)

        payload = {
            "topic": topic,
            "format": "json",
            "entry_count": len(entries),
            "meta": {
                "title": title,
                "status": status,
                "ball": ball,
                "last_entry_at": last,
                "header": header_block,
            },
            "entries": [_entry_full_payload(entry) for entry in entries],
        }
        # For JSON, add warnings as a separate field if present
        warnings = _get_startup_warnings()
        if warnings:
            payload["_warnings"] = warnings
        return json.dumps(payload, indent=2)

    except (ValidationError, ContextError, HostedModeError, ThreadNotFoundError) as e:
        # Re-raise custom exceptions for proper JSON-RPC error response
        raise
    except Exception as e:
        log_error(f"read_thread unexpected error for '{topic}': {e}")
        raise HostedModeError(f"Unexpected error reading thread '{topic}': {e}", operation="read_thread")


def _list_thread_entries_impl(
    topic: str,
    offset: int = 0,
    limit: int | None = None,
    format: str = "json",
    code_path: str = "",
) -> ToolResult:
    """Return thread entry headers (metadata only) with optional pagination."""

    fmt_error, resolved_format = _resolve_format(format, default="json")
    if fmt_error:
        raise ValidationError(fmt_error, field="format")

    error, context = validation._validate_thread_context(code_path)
    if error or context is None:
        raise ContextError(error or "Unknown context error", code_path=code_path)

    if offset < 0:
        raise ValidationError("offset must be non-negative", field="offset")
    if offset > _MAX_OFFSET:
        raise ValidationError(f"offset must not exceed {_MAX_OFFSET}", field="offset")
    if limit is not None and limit < 0:
        raise ValidationError("limit must be non-negative when provided", field="limit")
    if limit is not None and limit > _MAX_LIMIT:
        raise ValidationError(f"limit must not exceed {_MAX_LIMIT}", field="limit")

    # =========================================================================
    # Hosted Mode Path (GitHub API)
    # =========================================================================
    if is_hosted_context(context):
        log_debug(f"list_thread_entries: using hosted mode for topic={topic}")
        load_error, entries = load_thread_entries_hosted(topic)
        if load_error:
            if "not found" in load_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(load_error, operation="list_entries")

        total = len(entries)
        start = min(offset, total)
        end = total if limit is None else min(start + limit, total)
        slice_entries = entries[start:end]

        payload = {
            "topic": topic,
            "entry_count": total,
            "offset": start,
            "limit": limit,
            "entries": [_entry_header_payload(entry) for entry in slice_entries],
        }

        if resolved_format == "markdown":
            lines = [f"Entries for '{topic}' ({total} total)"]
            if slice_entries:
                for entry in slice_entries:
                    timestamp = entry.timestamp or "unknown"
                    title = entry.title or "(untitled)"
                    entry_id = entry.entry_id or "(no Entry-ID)"
                    lines.append(
                        f"- [{entry.index}] {timestamp} — {title} ({entry.role or 'role?'} / {entry.entry_type or 'type?'}) id={entry_id}"
                    )
            else:
                lines.append("- (no entries in range)")
            text = "\n".join(lines)
            return ToolResult(content=[TextContent(type="text", text=text)])

        return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])

    # =========================================================================
    # Local Mode Path (Filesystem)
    # =========================================================================
    # Lightweight read sync: auto-pull if behind origin (never blocks)
    _sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
    if sync_actions:
        log_debug(f"list_thread_entries read sync: {sync_actions}")

    validation._refresh_threads(context)
    load_error, entries = _load_thread_entries_graph_first(topic, context)
    if load_error:
        if "not found" in load_error.lower():
            raise ThreadNotFoundError(topic=topic)
        raise ContextError(load_error, code_path=code_path)

    total = len(entries)
    start = min(offset, total)
    end = total if limit is None else min(start + limit, total)
    slice_entries = entries[start:end]

    payload = {
        "topic": topic,
        "entry_count": total,
        "offset": start,
        "limit": limit,
        "entries": [_entry_header_payload(entry) for entry in slice_entries],
    }

    if resolved_format == "markdown":
        lines = [f"Entries for '{topic}' ({total} total)"]
        if slice_entries:
            for entry in slice_entries:
                timestamp = entry.timestamp or "unknown"
                title = entry.title or "(untitled)"
                entry_id = entry.entry_id or "(no Entry-ID)"
                lines.append(
                    f"- [{entry.index}] {timestamp} — {title} ({entry.role or 'role?'} / {entry.entry_type or 'type?'}) id={entry_id}"
                )
        else:
            lines.append("- (no entries in range)")
        text = "\n".join(lines)
        return ToolResult(content=[TextContent(type="text", text=text)])

    return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])


def _get_thread_entry_impl(
    topic: str,
    index: int | None = None,
    entry_id: str | None = None,
    format: str = "json",
    code_path: str = "",
) -> ToolResult:
    """Return a single thread entry (header + body)."""

    fmt_error, resolved_format = _resolve_format(format, default="json")
    if fmt_error:
        raise ValidationError(fmt_error, field="format")

    if index is None and entry_id is None:
        raise ValidationError("provide either index or entry_id to select an entry")

    error, context = validation._validate_thread_context(code_path)
    if error or context is None:
        raise ContextError(error or "Unknown context error", code_path=code_path)

    # =========================================================================
    # Hosted Mode Path (GitHub API)
    # =========================================================================
    if is_hosted_context(context):
        log_debug(f"get_thread_entry: using hosted mode for topic={topic}")
        load_error, entries = load_thread_entries_hosted(topic)
        if load_error:
            if "not found" in load_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(load_error, operation="get_entry")

        selected: ThreadEntry | None = None

        if index is not None:
            if index < 0:
                index = len(entries) + index
            if index < 0 or index >= len(entries):
                raise IndexOutOfRangeError(index=index, total=len(entries), topic=topic)
            selected = entries[index]

        if entry_id is not None:
            matching = next((entry for entry in entries if entry.entry_id == entry_id), None)
            if matching is None:
                raise EntryNotFoundError(topic=topic, entry_id=entry_id)
            if selected is not None and matching.index != selected.index:
                raise ValidationError("index and entry_id refer to different entries")
            selected = matching

        if selected is None:
            raise EntryNotFoundError(topic=topic)

        payload = {
            "topic": topic,
            "entry_count": len(entries),
            "index": selected.index,
            "entry": _entry_full_payload(selected),
        }

        if resolved_format == "markdown":
            markdown = payload["entry"]["markdown"]  # type: ignore[index]
            return ToolResult(content=[TextContent(type="text", text=markdown)])

        return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])

    # =========================================================================
    # Local Mode Path (Filesystem)
    # =========================================================================
    # Lightweight read sync: auto-pull if behind origin (never blocks)
    _sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
    if sync_actions:
        log_debug(f"get_thread_entry read sync: {sync_actions}")

    validation._refresh_threads(context)
    load_error, entries = _load_thread_entries_graph_first(topic, context)
    if load_error:
        if "not found" in load_error.lower():
            raise ThreadNotFoundError(topic=topic)
        raise ContextError(load_error, code_path=code_path)

    selected: ThreadEntry | None = None

    if index is not None:
        # Support Python-style negative indexing: -1 = last, -2 = second-to-last, etc.
        if index < 0:
            index = len(entries) + index
        if index < 0 or index >= len(entries):
            raise IndexOutOfRangeError(index=index, total=len(entries), topic=topic)
        selected = entries[index]

    if entry_id is not None:
        matching = next((entry for entry in entries if entry.entry_id == entry_id), None)
        if matching is None:
            raise EntryNotFoundError(topic=topic, entry_id=entry_id)
        if selected is not None and matching.index != selected.index:
            raise ValidationError("index and entry_id refer to different entries")
        selected = matching

    if selected is None:
        raise EntryNotFoundError(topic=topic)

    # Track entry access (non-blocking)
    if selected.entry_id and context.threads_dir:
        _track_access(context.threads_dir, "entry", selected.entry_id)

    payload = {
        "topic": topic,
        "entry_count": len(entries),
        "index": selected.index,
        "entry": _entry_full_payload(selected),
    }

    if resolved_format == "markdown":
        markdown = payload["entry"]["markdown"]  # type: ignore[index]
        return ToolResult(content=[TextContent(type="text", text=markdown)])

    return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])


def _get_thread_entry_range_impl(
    topic: str,
    start_index: int = 0,
    end_index: int | None = None,
    format: str = "json",
    code_path: str = "",
) -> ToolResult:
    """Return a contiguous range of entries (inclusive)."""

    fmt_error, resolved_format = _resolve_format(format, default="json")
    if fmt_error:
        raise ValidationError(fmt_error, field="format")

    if start_index < 0:
        raise ValidationError("start_index must be non-negative", field="start_index")
    if start_index > _MAX_OFFSET:
        raise ValidationError(f"start_index must not exceed {_MAX_OFFSET}", field="start_index")
    if end_index is not None and end_index < start_index:
        raise ValidationError("end_index must be greater than or equal to start_index", field="end_index")
    if end_index is not None and (end_index - start_index) > _MAX_LIMIT:
        raise ValidationError(f"requested range size must not exceed {_MAX_LIMIT} entries", field="range")

    error, context = validation._validate_thread_context(code_path)
    if error or context is None:
        raise ContextError(error or "Unknown context error", code_path=code_path)

    # =========================================================================
    # Hosted Mode Path (GitHub API)
    # =========================================================================
    if is_hosted_context(context):
        log_debug(f"get_thread_entry_range: using hosted mode for topic={topic}")
        load_error, entries = load_thread_entries_hosted(topic)
        if load_error:
            if "not found" in load_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(load_error, operation="get_entry_range")

        total = len(entries)
        if start_index >= total and total > 0:
            raise IndexOutOfRangeError(index=start_index, total=total, topic=topic)

        last_index = total - 1 if total else -1
        effective_end = last_index if end_index is None else min(end_index, last_index)
        if effective_end < start_index and total:
            raise ValidationError("computed end index is before start index", field="end_index")

        selected_entries = entries[start_index : effective_end + 1] if total else []

        payload = {
            "topic": topic,
            "entry_count": total,
            "start_index": start_index,
            "end_index": effective_end if selected_entries else None,
            "entries": [_entry_full_payload(entry) for entry in selected_entries],
        }

        if resolved_format == "markdown":
            if not selected_entries:
                return ToolResult(content=[TextContent(type="text", text="(no entries in range)")])
            markdown_blocks = []
            for entry in selected_entries:
                block = entry.header
                if entry.body:
                    block += "\n\n" + entry.body
                markdown_blocks.append(block)
            text = "\n\n---\n\n".join(markdown_blocks)
            return ToolResult(content=[TextContent(type="text", text=text)])

        return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])

    # =========================================================================
    # Local Mode Path (Filesystem)
    # =========================================================================
    # Lightweight read sync: auto-pull if behind origin (never blocks)
    _sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
    if sync_actions:
        log_debug(f"get_thread_entry_range read sync: {sync_actions}")

    validation._refresh_threads(context)
    load_error, entries = _load_thread_entries_graph_first(topic, context)
    if load_error:
        if "not found" in load_error.lower():
            raise ThreadNotFoundError(topic=topic)
        raise ContextError(load_error, code_path=code_path)

    total = len(entries)
    if start_index >= total and total > 0:
        raise IndexOutOfRangeError(index=start_index, total=total, topic=topic)

    last_index = total - 1 if total else -1
    effective_end = last_index if end_index is None else min(end_index, last_index)
    if effective_end < start_index and total:
        raise ValidationError("computed end index is before start index", field="end_index")

    selected_entries = entries[start_index : effective_end + 1] if total else []

    # Track entry access for all entries in range (non-blocking)
    if context.threads_dir:
        for entry in selected_entries:
            if entry.entry_id:
                _track_access(context.threads_dir, "entry", entry.entry_id)

    payload = {
        "topic": topic,
        "entry_count": total,
        "start_index": start_index,
        "end_index": effective_end if selected_entries else None,
        "entries": [_entry_full_payload(entry) for entry in selected_entries],
    }

    if resolved_format == "markdown":
        if not selected_entries:
            return ToolResult(content=[TextContent(type="text", text="(no entries in range)")])
        markdown_blocks = []
        for entry in selected_entries:
            block = entry.header
            if entry.body:
                block += "\n\n" + entry.body
            markdown_blocks.append(block)
        text = "\n\n---\n\n".join(markdown_blocks)
        return ToolResult(content=[TextContent(type="text", text=text)])

    return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])


def register_thread_query_tools(mcp):
    """Register thread query tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global list_threads, read_thread, list_thread_entries, get_thread_entry, get_thread_entry_range

    # Register tools and store references for testing
    list_threads = mcp.tool(name="watercooler_list_threads")(_list_threads_impl)
    read_thread = mcp.tool(name="watercooler_read_thread")(_read_thread_impl)
    list_thread_entries = mcp.tool(name="watercooler_list_thread_entries")(_list_thread_entries_impl)
    get_thread_entry = mcp.tool(name="watercooler_get_thread_entry")(_get_thread_entry_impl)
    get_thread_entry_range = mcp.tool(name="watercooler_get_thread_entry_range")(_get_thread_entry_range_impl)
