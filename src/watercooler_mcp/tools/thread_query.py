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
from typing import Any

from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from watercooler import fs
from watercooler.thread_entries import ThreadEntry

from ..config import (
    get_agent_name,
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
    _get_thread_metadata,  # Canonical: graph-first
    _resolve_format,
    # Entry loading
    _entry_header_payload,
    _entry_full_payload,
    # Graph helpers
    _track_access,
    _use_graph_for_reads,
    _load_entries,
    _list_threads,
    _get_thread_summary,
    _scan_thread_entries,
    # Types
    ThreadRecord,
)
from watercooler.baseline_graph.reader import (
    read_thread_from_graph,
    format_thread_markdown,
    get_branches_with_entries,
    format_branch_discovery_hint,
)
from watercooler.baseline_graph.summarizer import (
    entry_type_counts,
    format_entry_mix,
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
from ..sync import ensure_readable, format_parity_warning
from ..observability import log_debug, log_error, log_warning
from .. import validation  # Import module for runtime access (enables test patching)
from ..validation import is_hosted_context


def _entry_to_markdown(entry: ThreadEntry) -> str:
    """Render a ThreadEntry as markdown text (header + body)."""
    body_content = entry.body.strip() if entry.body else ""
    if body_content:
        return entry.header + "\n\n" + body_content
    return entry.header


# Module-level references to registered tools (populated by register_thread_query_tools)
list_threads = None
read_thread = None
list_thread_entries = None
get_thread_entry = None


def _filter_threads_by_annotations(
    threads_dir: Path,
    threads: list,
    tags_list: list[str],
    flag: str,
    pinned: bool | None,
) -> list:
    """Filter thread tuples by annotation state.

    Semantics:
    - tags: thread passes if at least one entry has ALL required tags
    - flag: thread passes if at least one entry has a matching flag
    - pinned=True: thread passes if at least one entry is pinned
    - pinned=False: thread passes if NO entry is pinned (includes threads
      with no annotations, since they are implicitly not pinned)

    Args:
        threads_dir: Threads directory
        threads: List of thread tuples from _list_threads
        tags_list: Required tags (AND semantics, case-insensitive)
        flag: Flag value substring (case-insensitive)
        pinned: Pinned filter (True/False/None)

    Returns:
        Filtered list of thread tuples
    """
    from watercooler.baseline_graph.annotations import load_or_rebuild_state
    from watercooler.baseline_graph.reader import get_graph_dir
    from watercooler.baseline_graph import storage

    graph_dir = get_graph_dir(threads_dir)
    filtered = []

    for thread_tuple in threads:
        topic = thread_tuple.path.stem

        thread_dir = storage.get_thread_graph_dir(graph_dir, topic)
        try:
            ann_states = load_or_rebuild_state(thread_dir, read_only=True)
        except Exception:
            ann_states = {}

        if not ann_states:
            # No annotations — thread has no tags, no flags, is not pinned.
            # Passes pinned=False but fails tags/flag/pinned=True.
            if tags_list or flag or pinned is True:
                continue
            filtered.append(thread_tuple)
            continue

        # pinned=False: thread must have NO pinned entries
        if pinned is False:
            if any(ann.pinned for ann in ann_states.values()):
                continue

        # tags/flag/pinned=True use any-entry-matches semantics
        needs_per_entry = bool(tags_list) or bool(flag) or pinned is True
        if not needs_per_entry:
            # Only pinned=False was requested and passed above
            filtered.append(thread_tuple)
            continue

        topic_matches = False
        for _entry_id, ann in ann_states.items():
            entry_ok = True

            if tags_list:
                entry_tags_lower = [t.lower() for t in ann.tags]
                if not all(rt.lower() in entry_tags_lower for rt in tags_list):
                    entry_ok = False

            if entry_ok and flag:
                reason_lower = flag.lower()
                if not any(reason_lower in (f.get("reason", "")).lower() for f in ann.flags):
                    entry_ok = False

            if entry_ok and pinned is True:
                if not ann.pinned:
                    entry_ok = False

            if entry_ok:
                topic_matches = True
                break

        if topic_matches:
            filtered.append(thread_tuple)

    return filtered


def _list_threads_impl(
    ctx: Context,
    open_only: bool | None = None,
    limit: int = 50,
    cursor: str | None = None,
    format: str = "markdown",
    scan: bool = False,
    tags: str = "",
    flag: str = "",
    pinned: bool | None = None,
    code_path: str = "",
) -> ToolResult:
    """List all watercooler threads.

    Shows threads where you have the ball (actionable items), threads where
    you're waiting on others, and marks NEW entries since you last contributed.

    Args:
        open_only: Filter by open status (True=open only, False=closed only, None=all)
        limit: Maximum threads to return (Phase 1A: ignored, returns all)
        cursor: Pagination cursor (Phase 1A: ignored, no pagination)
        format: Output format - "markdown" or "json"
        scan: When True, includes per-entry summaries for every thread in a single
            call. Replaces N sequential read_thread(summary_only=true) calls with
            one batch operation. Each entry summary links back to its thread (topic)
            and entry (index, entry_id).
        tags: Comma-separated tag names to filter by (e.g. "podcast,newsletter").
            All specified tags must be present on at least one entry in the thread
            (AND semantics, case-insensitive).
        flag: Flag value substring to match (case-insensitive). Returns only
            threads with at least one entry whose flag value contains this string.
        pinned: Filter by pinned status. True=threads with at least one pinned entry,
            False=threads with no pinned entries, None=no filter (default).
        code_path: Path to the code repository directory containing the files most immediately
            under discussion. This establishes the code context for branch pairing.
            Should point to the root of your working repository.

    Returns:
        Formatted thread list with:
        - Threads where you have the ball (🎾 marker)
        - Threads with NEW entries for you to read
        - Thread status, entry count, and last update time
        - When scan=True: per-entry summaries with index and entry_id
    """
    try:
        start_ts = time.time()
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
        log_debug(f"list_threads start code_path={code_path!r} open_only={open_only} scan={scan}")

        # =====================================================================
        # Hosted Mode Path (GitHub API)
        # =====================================================================
        if is_hosted_context(context):
            # Hosted mode (GitHub API) does not support scan or summary features
            # because summaries are generated by the local baseline graph, which
            # is unavailable when threads are served via the GitHub API.
            log_debug("list_threads: using hosted mode (GitHub API)")

            # Hosted mode has no annotation state — warn if annotation filters were requested
            warning_text = ""
            _tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
            _flag_clean = flag.strip() if flag else ""
            if _tags_list or _flag_clean or pinned is not None:
                log_warning("list_threads hosted mode: annotation filters (tags/flag/pinned) not available")
                warning_text = (
                    "⚠️ Annotation filters (tags, flag, pinned) are not supported in hosted mode. "
                    "These filters were ignored. Use local mode for annotation filtering.\n\n"
                )
            else:
                warning_text = ""

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

            response = warning_text + "\n".join(output)
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
        sync_ok, sync_actions, parity, auto_heal_failed = ensure_readable(context.threads_dir, context.code_root)
        parity_banner = format_parity_warning(parity, auto_heal_failed=auto_heal_failed)
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
        threads = _list_threads(threads_dir, open_only=open_only, agent=agent)
        scan_elapsed = time.time() - scan_start
        log_debug(f"list_threads scanned {len(threads)} threads in {scan_elapsed:.2f}s")

        # Post-filter by annotation fields (tags, flag, pinned)
        tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        flag_clean = flag.strip() if flag else ""
        if tags_list or flag_clean or pinned is not None:
            threads = _filter_threads_by_annotations(
                threads_dir, threads, tags_list, flag_clean, pinned,
            )
            log_debug(f"list_threads annotation filter: {len(threads)} threads remaining")

        # If scan mode, load all entry summaries in one batch
        entry_scan: dict = {}
        if scan:
            topics = [t.path.stem for t in threads]
            if len(topics) > 50:
                log_warning(
                    f"Scan mode loading entries for {len(topics)} threads — "
                    "consider using open_only=True or limit to reduce memory usage"
                )
            scan_entries_start = time.time()
            entry_scan = _scan_thread_entries(threads_dir, topics)
            scan_entries_elapsed = time.time() - scan_entries_start
            total_entries = sum(len(v) for v in entry_scan.values())
            log_debug(f"list_threads scan loaded {total_entries} entries across {len(entry_scan)} threads in {scan_entries_elapsed:.2f}s")

        if not threads:
            status_filter = "open " if open_only is True else ("closed " if open_only is False else "")
            log_debug(f"list_threads no {status_filter or ''}threads found")
            return ToolResult(content=[TextContent(type="text", text=f"No {status_filter}threads found in: {threads_dir}")])

        # Classify threads by ball ownership
        agent_lower = agent.lower()
        classify_start = time.time()
        your_turn = []
        waiting = []
        new_entries = []

        for title, status, ball, updated, path, is_new, summary, entry_count in threads:
            topic = path.stem
            ball_lower = (ball or "").lower()
            has_ball = ball_lower == agent_lower

            if is_new:
                new_entries.append((title, status, ball, updated, topic, has_ball, summary, entry_count))
            elif has_ball:
                your_turn.append((title, status, ball, updated, topic, has_ball, summary, entry_count))
            else:
                waiting.append((title, status, ball, updated, topic, has_ball, summary, entry_count))
        classify_elapsed = time.time() - classify_start
        log_debug(f"list_threads classified threads in {classify_elapsed:.2f}s (your_turn={len(your_turn)} waiting={len(waiting)} new={len(new_entries)})")

        # =====================================================================
        # JSON output
        # =====================================================================
        if resolved_format == "json":
            all_classified = [
                ("your_turn", your_turn),
                ("new_entries", new_entries),
                ("waiting", waiting),
            ]
            json_threads = []
            for _section, items in all_classified:
                for title, status, ball, updated, topic, has_ball, summary, entry_count in items:
                    t_obj: dict = {
                        "topic": topic,
                        "title": title,
                        "status": status,
                        "ball": ball,
                        "updated": updated,
                        "entry_count": entry_count,
                        "summary": summary,
                        "has_ball": has_ball,
                    }
                    if scan and topic in entry_scan:
                        t_obj["entry_type_counts"] = entry_type_counts(
                            [{"entry_type": ge.entry_type} for ge in entry_scan[topic]]
                        )
                        t_obj["entries"] = [
                            {
                                "index": ge.index,
                                "entry_id": ge.entry_id,
                                "title": ge.title,
                                "timestamp": ge.timestamp,
                                "role": ge.role,
                                "type": ge.entry_type,
                                "summary": ge.summary,
                            }
                            for ge in entry_scan[topic]
                        ]
                    json_threads.append(t_obj)

            payload: dict = {
                "total": len(threads),
                "scan": scan,
                "threads": json_threads,
            }
            warnings = _get_startup_warnings()
            if warnings:
                payload["_warnings"] = warnings
            if parity_banner:
                payload["_parity_warning"] = parity_banner.strip()
            return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])

        # =====================================================================
        # Markdown output
        # =====================================================================
        output: list[str] = []
        if parity_banner:
            output.append(parity_banner)
        scan_label = " — Scan" if scan else ""
        output.append(f"# Watercooler Threads ({len(threads)} total){scan_label}\n")

        def _render_thread_md(
            title: str, status: str, ball: str, updated: str,
            topic: str, summary: str, entry_count: int,
            marker: str = "",
        ) -> None:
            ec = f" | Entries: {entry_count}" if entry_count else ""
            output.append(f"- {marker}**{topic}** - {title}")
            output.append(f"  Status: {status} | Ball: {ball}{ec} | Updated: {updated}")
            if summary:
                output.append(f"  {summary}")
            # Scan mode: append per-entry summaries
            if scan and topic in entry_scan:
                for ge in entry_scan[topic]:
                    eid_short = ge.entry_id[:12] + "…" if ge.entry_id and len(ge.entry_id) > 12 else ge.entry_id or ""
                    t = ge.title or "(untitled)"
                    output.append(f"    [{ge.index}] {ge.timestamp or '?'} — {t} ({eid_short})")
                    if ge.summary:
                        output.append(f"      {ge.summary}")

        render_start = time.time()
        if your_turn:
            output.append(f"\n## 🎾 Your Turn ({len(your_turn)} threads)\n")
            for title, status, ball, updated, topic, _, summary, entry_count in your_turn:
                _render_thread_md(title, status, ball, updated, topic, summary, entry_count)

        if new_entries:
            output.append(f"\n## 🆕 NEW Entries for You ({len(new_entries)} threads)\n")
            for title, status, ball, updated, topic, has_ball, summary, entry_count in new_entries:
                marker = "🎾 " if has_ball else ""
                _render_thread_md(title, status, ball, updated, topic, summary, entry_count, marker)

        if waiting:
            output.append(f"\n## ⏳ Waiting on Others ({len(waiting)} threads)\n")
            for title, status, ball, updated, topic, _, summary, entry_count in waiting:
                _render_thread_md(title, status, ball, updated, topic, summary, entry_count)

        output.append(f"\n---\n*You are: {agent}*")
        output.append(f"*Threads dir: {threads_dir}*")

        response = "\n".join(output)
        render_elapsed = time.time() - render_start
        log_debug(f"list_threads rendered sections in {render_elapsed:.2f}s")
        duration = time.time() - start_ts
        log_debug(
            f"list_threads formatted response in "
            f"{duration:.2f}s (total={len(threads)} new={len(new_entries)} "
            f"your_turn={len(your_turn)} waiting={len(waiting)} "
            f"scan={scan} chars={len(response)})"
        )
        log_debug("list_threads returning response")
        return ToolResult(content=[TextContent(type="text", text=_format_warnings_for_response(response))])

    except (ValidationError, ContextError, HostedModeError, ThreadNotFoundError) as e:
        # Re-raise custom exceptions for proper JSON-RPC error response
        raise
    except Exception as e:
        log_error(f"list_threads unexpected error: {e}")
        raise HostedModeError(f"Unexpected error listing threads: {e}", operation="list_threads")


def _stance_block_markdown(context: Any) -> str:
    """Best-effort stance block for summary_only markdown reads. Never raises.

    Returns "" on any failure so the core thread summary is always returned
    intact (authority ladder L1: advisory-only, best-effort).
    """
    try:
        from ..stance_read_decoration import render_stance_markdown, resolve_stance_block

        return render_stance_markdown(resolve_stance_block(context))
    except Exception as exc:  # decoration must never break the core read
        log_debug(f"stance decoration (markdown) skipped: {exc}")
        return ""


def _stance_block_json(context: Any) -> dict[str, Any] | None:
    """Best-effort stance block for summary_only JSON reads. Never raises.

    Returns None on any failure so the ``_stance_advisory`` peer key is simply
    omitted rather than breaking the read. Note: unlike the markdown wrapper
    (which returns "" and is skipped when there are no bullets), a successful
    JSON render always returns a populated dict — the peer key is intentionally
    always present on local summary_only reads so a JSON consumer gets the
    machine-readable ``salience_status``/``stance_block_status`` even when the
    stance is empty (plan C10: diagnosable, not silently absent).
    """
    try:
        from ..stance_read_decoration import render_stance_json, resolve_stance_block

        return render_stance_json(resolve_stance_block(context))
    except Exception as exc:  # decoration must never break the core read
        log_debug(f"stance decoration (json) skipped: {exc}")
        return None


def _read_thread_impl(
    topic: str,
    from_entry: int = 0,
    limit: int = 100,
    format: str = "markdown",
    summary_only: bool = False,
    code_path: str = "",
    code_branch: str | None = None,
) -> str:
    """Read the complete content of a watercooler thread.

    Args:
        topic: Thread topic identifier (e.g., "feature-auth")
        from_entry: Starting entry index for pagination (Phase 1A: ignored)
        limit: Maximum entries to include (Phase 1A: ignored, returns all)
        format: Output format - "markdown" or "json"
        summary_only: When True, returns only entry summaries (no bodies).
            Reduces token usage by ~90% — ideal for scanning threads.
        code_path: Path to the code repository directory containing the files most immediately
            under discussion. This establishes the code context for branch pairing.
            Should point to the root of your working repository.
        code_branch: Filter entries by code branch. Auto-populated from current
            branch in orphan mode. Pass "*" to see all branches.

    Returns:
        Full thread content (or summary-only condensed view) including:
        - Thread metadata (status, ball owner, participants)
        - All entries with timestamps, authors, roles, and types. Each entry
          carries a ``ref`` — the canonical citation
          ``<thread_topic>:<index> (<entry_id>)``; the ``topic`` you queried with
          is the write-back handle.
        - Current ball ownership status
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
            # Hosted mode (GitHub API) does not support summary_only — summaries
            # require the local baseline graph. Falls through to full content.
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

            # Read metadata from graph (meta.json) instead of parsing markdown
            from ..hosted_ops import load_thread_metadata_hosted
            meta_error, meta = load_thread_metadata_hosted(topic)
            if meta_error:
                # Fallback to defaults if meta.json read fails
                meta = {"title": topic, "status": "OPEN", "ball": "", "last_updated": "", "summary": ""}

            payload = {
                "topic": topic,
                "format": "json",
                "entry_count": len(entries),
                "meta": {
                    "title": meta.get("title", topic),
                    "status": meta.get("status", "OPEN"),
                    "ball": meta.get("ball", ""),
                    "last_entry_at": meta.get("last_updated", ""),
                    "summary": meta.get("summary", ""),
                },
                "entries": [_entry_full_payload(entry, topic=topic) for entry in entries],
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

        # Auto-populate code_branch from context
        if code_branch is None:
            code_branch = context.code_branch

        # Lightweight read sync: auto-pull if behind origin (never blocks)
        sync_ok, sync_actions, parity, auto_heal_failed = ensure_readable(context.threads_dir, context.code_root)
        parity_banner = format_parity_warning(parity, auto_heal_failed=auto_heal_failed)
        if sync_actions:
            log_debug(f"read_thread read sync: {sync_actions}")

        validation._refresh_threads(context)
        threads_dir = context.threads_dir

        # Create threads directory if it doesn't exist
        if not threads_dir.exists():
            threads_dir.mkdir(parents=True, exist_ok=True)

        # Check thread existence via graph (source of truth)
        graph_result = read_thread_from_graph(threads_dir, topic, code_branch=code_branch)
        if not graph_result:
            raise ThreadNotFoundError(topic=topic)

        # Track thread access (non-blocking)
        _track_access(threads_dir, "thread", topic)

        # Branch-discovery hint when a filtered read produced 0 entries
        # but other code_branch values do have entries for this topic.
        # The filter is a documented feature; this only surfaces the
        # escape hatch when it would actually be useful.
        _graph_entries_for_hint = graph_result[1]
        branch_hint = ""
        branches_with_entries_list: list[str] = []
        if (
            not _graph_entries_for_hint
            and code_branch
            and code_branch != "*"
        ):
            _all_branches = get_branches_with_entries(threads_dir, topic)
            branch_hint = format_branch_discovery_hint(code_branch, _all_branches)
            if branch_hint:
                branches_with_entries_list = sorted(
                    b for b in _all_branches if b != code_branch
                )
        hint_banner = f"{branch_hint}\n\n" if branch_hint else ""

        # Serve markdown format reconstructed from graph
        if resolved_format == "markdown" and not summary_only:
            graph_thread, graph_entries = graph_result
            content = format_thread_markdown(graph_thread, graph_entries)
            # Deterministic, non-LLM entry-type badge (same signal as summary_only/JSON).
            # Inserted just after the title line so the leading "# <topic>" is preserved.
            mix = format_entry_mix(
                entry_type_counts([{"entry_type": e.entry_type} for e in graph_entries])
            )
            mix_line = f"Entry mix: {mix}"
            nl = content.find("\n")
            if nl != -1:
                content = f"{content[:nl + 1]}{mix_line}\n{content[nl + 1:]}"
            else:
                content = f"{content}\n{mix_line}"
            return _format_warnings_for_response(hint_banner + parity_banner + content)

        # Load entries from graph (canonical)
        load_error, entries, summaries = _load_entries(topic, context, code_branch=code_branch)
        if load_error:
            return load_error

        # Extract metadata from graph
        title, status, ball, last = _get_thread_metadata(threads_dir, topic)
        thread_summary = _get_thread_summary(threads_dir, topic)

        # Deterministic, non-LLM entry-type badge computed from the loaded entries.
        # It is never sourced from the (LLM-generated) summary prose, so it cannot be
        # laundered: a Note-only thread always reports 0 Decisions / 0 Closures here.
        entry_mix_counts = entry_type_counts(
            [{"entry_type": e.entry_type} for e in entries]
        )

        if summary_only:
            if resolved_format == "markdown":
                # Condensed "Cole's Notes" view
                lines = [f"# {topic} — {title}"]
                if thread_summary:
                    lines.append(f"\n{thread_summary}")
                lines.append(f"\nStatus: {status} | Ball: {ball} | Entries: {len(entries)}")
                lines.append(f"Entry mix: {format_entry_mix(entry_mix_counts)}\n")
                for entry in entries:
                    ts = entry.timestamp or "unknown"
                    t = entry.title or "(untitled)"
                    eid = entry.entry_id or ""
                    eid_short = eid[:12] + "…" if len(eid) > 12 else eid
                    s = summaries.get(eid, "")
                    lines.append(f"- [{entry.index}] {ts} — {t} ({eid_short})")
                    if s:
                        lines.append(f"  {s}")
                body = "\n".join(lines)
                stance_md = _stance_block_markdown(context)
                if stance_md:
                    body = f"{body}\n\n{stance_md}"
                return _format_warnings_for_response(hint_banner + body)

            # JSON summary_only mode
            payload = {
                "topic": topic,
                "format": "json",
                "summary_only": True,
                "entry_count": len(entries),
                "entry_type_counts": entry_mix_counts,
                "meta": {
                    "title": title,
                    "status": status,
                    "ball": ball,
                    "last_entry_at": last,
                    "summary": thread_summary,
                },
                "entries": [
                    _entry_header_payload(
                        entry,
                        summary=summaries.get(entry.entry_id or "", ""),
                        topic=topic,
                    )
                    for entry in entries
                ],
            }
            warnings = _get_startup_warnings()
            if warnings:
                payload["_warnings"] = warnings
            if parity_banner:
                payload["_parity_warning"] = parity_banner.strip()
            if branch_hint:
                payload["_hint"] = branch_hint
                payload["_branches_with_entries"] = branches_with_entries_list
            stance_json = _stance_block_json(context)
            if stance_json is not None:
                payload["_stance_advisory"] = stance_json
            return json.dumps(payload, indent=2)

        # Full JSON mode (with summaries included)
        payload = {
            "topic": topic,
            "format": "json",
            "entry_count": len(entries),
            "entry_type_counts": entry_mix_counts,
            "meta": {
                "title": title,
                "status": status,
                "ball": ball,
                "last_entry_at": last,
                "summary": thread_summary,
            },
            "entries": [
                _entry_full_payload(
                    entry,
                    summary=summaries.get(entry.entry_id or "", ""),
                    topic=topic,
                )
                for entry in entries
            ],
        }
        # For JSON, add warnings as a separate field if present
        warnings = _get_startup_warnings()
        if warnings:
            payload["_warnings"] = warnings
        if parity_banner:
            payload["_parity_warning"] = parity_banner.strip()
        if branch_hint:
            payload["_hint"] = branch_hint
            payload["_branches_with_entries"] = branches_with_entries_list
        return json.dumps(payload, indent=2)

    except (ValidationError, ContextError, HostedModeError, ThreadNotFoundError) as e:
        # Re-raise custom exceptions for proper JSON-RPC error response
        raise
    except Exception as e:
        log_error(f"read_thread unexpected error for '{topic}': {e}")
        raise HostedModeError(f"Unexpected error reading thread '{topic}': {e}", operation="read_thread")


def _filter_entries(entries: list, keyword: str) -> list:
    """Keep entries whose title or body contains *keyword* (case-insensitive).

    A blank/empty keyword returns *entries* unchanged. Backs the in-thread
    keyword filter on watercooler_list_thread_entries (#325).
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return entries
    return [
        e
        for e in entries
        if kw in (getattr(e, "title", "") or "").lower()
        or kw in (getattr(e, "body", "") or "").lower()
    ]


def _list_thread_entries_impl(
    topic: str,
    offset: int = 0,
    limit: int | None = None,
    format: str = "json",
    code_path: str = "",
    code_branch: str | None = None,
    filter: str = "",
) -> ToolResult:
    """Return thread entry headers (metadata only) with optional pagination.

    Args:
        topic: Thread topic identifier
        offset: Starting entry offset
        limit: Maximum entries to return
        format: Output format ("json" or "markdown")
        code_path: Path to code repository
        code_branch: Filter entries by code branch. Auto-populated from current
            branch in orphan mode. Pass "*" to see all branches.
        filter: Optional keyword — case-insensitive substring matched against
            each entry's title and body. Only matching entries are returned
            (in-thread keyword search); pagination applies to the matches.

    Returns:
        Entry headers (metadata only). Each entry carries a ``ref`` — the
        canonical citation ``<thread_topic>:<index> (<entry_id>)``; the ``topic``
        you queried with is the write-back handle.
    """

    fmt_error, resolved_format = _resolve_format(format, default="json")
    if fmt_error:
        raise ValidationError(fmt_error, field="format")

    error, context = validation._validate_thread_context(code_path)
    if error or context is None:
        raise ContextError(error or "Unknown context error", code_path=code_path)

    # Auto-populate code_branch from context
    if code_branch is None:
        code_branch = context.code_branch

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

        entries = _filter_entries(entries, filter)
        total = len(entries)
        start = min(offset, total)
        end = total if limit is None else min(start + limit, total)
        slice_entries = entries[start:end]

        payload = {
            "topic": topic,
            "entry_count": total,
            "offset": start,
            "limit": limit,
            "entries": [_entry_header_payload(entry, topic=topic) for entry in slice_entries],
        }
        if filter:
            payload["filter"] = filter

        if resolved_format == "markdown":
            lines = [f"Entries for '{topic}' ({total} total)"]
            if filter:
                lines[0] += f" [filter: {filter}]"
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
    _sync_ok, sync_actions, parity, auto_heal_failed = ensure_readable(context.threads_dir, context.code_root)
    parity_banner = format_parity_warning(parity, auto_heal_failed=auto_heal_failed)
    if sync_actions:
        log_debug(f"list_thread_entries read sync: {sync_actions}")

    validation._refresh_threads(context)
    load_error, entries, summaries = _load_entries(topic, context, code_branch=code_branch)
    if load_error:
        if "not found" in load_error.lower():
            raise ThreadNotFoundError(topic=topic)
        raise ContextError(load_error, code_path=code_path)

    # Branch-discovery hint — fires only when the topic exists but the
    # branch-filtered entries list is empty AND an explicit branch filter is
    # active. Computed before the keyword filter, so a no-match keyword does
    # not trigger it. Matches `_read_thread_impl` semantics.
    branch_hint = ""
    branches_with_entries_list: list[str] = []
    if not entries and code_branch and code_branch != "*":
        _all_branches = get_branches_with_entries(context.threads_dir, topic)
        branch_hint = format_branch_discovery_hint(code_branch, _all_branches)
        if branch_hint:
            branches_with_entries_list = sorted(
                b for b in _all_branches if b != code_branch
            )
    hint_banner = f"{branch_hint}\n\n" if branch_hint else ""

    # In-thread keyword filter (#325) — narrows to matching entries.
    entries = _filter_entries(entries, filter)

    total = len(entries)
    start = min(offset, total)
    end = total if limit is None else min(start + limit, total)
    slice_entries = entries[start:end]

    payload = {
        "topic": topic,
        "entry_count": total,
        "offset": start,
        "limit": limit,
        "code_branch": code_branch,
        "entries": [
            _entry_header_payload(
                entry,
                summary=summaries.get(entry.entry_id or "", ""),
                topic=topic,
            )
            for entry in slice_entries
        ],
    }
    if filter:
        payload["filter"] = filter

    if resolved_format == "markdown":
        lines = [f"Entries for '{topic}' ({total} total)"]
        if code_branch and code_branch != "*":
            lines[0] += f" [branch: {code_branch}]"
        if filter:
            lines[0] += f" [filter: {filter}]"
        if slice_entries:
            for entry in slice_entries:
                timestamp = entry.timestamp or "unknown"
                title = entry.title or "(untitled)"
                entry_id = entry.entry_id or "(no Entry-ID)"
                s = summaries.get(entry_id, "")
                lines.append(
                    f"- [{entry.index}] {timestamp} — {title} ({entry.role or 'role?'} / {entry.entry_type or 'type?'}) id={entry_id}"
                )
                if s:
                    lines.append(f"  {s}")
        else:
            lines.append("- (no entries in range)")
        text = "\n".join(lines)
        return ToolResult(content=[TextContent(type="text", text=hint_banner + parity_banner + text)])

    if parity_banner:
        payload["_parity_warning"] = parity_banner.strip()
    if branch_hint:
        payload["_hint"] = branch_hint
        payload["_branches_with_entries"] = branches_with_entries_list
    return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])


def _get_thread_entry_impl(
    topic: str,
    index: int | None = None,
    entry_id: str | None = None,
    to_index: int | None = None,
    format: str = "json",
    summary_only: bool = False,
    code_path: str = "",
    code_branch: str | None = None,
) -> ToolResult:
    """Return a thread entry, or a contiguous range of entries.

    With ``to_index`` unset this returns a single entry selected by ``index``
    or ``entry_id``. With ``to_index`` set it returns the inclusive range
    ``index .. to_index`` (``index`` defaults to 0), honouring ``summary_only``
    and ``code_branch`` filtering. ``summary_only`` and ``code_branch`` apply
    only to the range form.

    Each returned entry carries a ``ref`` — the canonical citation
    ``<thread_topic>:<index> (<entry_id>)``; the ``topic`` you queried with is
    the write-back handle.
    """

    if to_index is not None:
        if entry_id is not None:
            raise ValidationError(
                "entry_id cannot be combined with to_index; use index as the "
                "range start",
                field="entry_id",
            )
        return _get_thread_entry_range_impl(
            topic=topic,
            start_index=index if index is not None else 0,
            end_index=to_index,
            format=format,
            summary_only=summary_only,
            code_path=code_path,
            code_branch=code_branch,
        )

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
            "entry": _entry_full_payload(selected, topic=topic),
        }

        if resolved_format == "markdown":
            text = _entry_to_markdown(selected)
            return ToolResult(content=[TextContent(type="text", text=text)])

        return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])

    # =========================================================================
    # Local Mode Path (Filesystem)
    # =========================================================================
    # Lightweight read sync: auto-pull if behind origin (never blocks)
    _sync_ok, sync_actions, parity, auto_heal_failed = ensure_readable(context.threads_dir, context.code_root)
    parity_banner = format_parity_warning(parity, auto_heal_failed=auto_heal_failed)
    if sync_actions:
        log_debug(f"get_thread_entry read sync: {sync_actions}")

    validation._refresh_threads(context)
    load_error, entries, summaries = _load_entries(topic, context)
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

    entry_summary = summaries.get(selected.entry_id or "", "")
    payload = {
        "topic": topic,
        "entry_count": len(entries),
        "index": selected.index,
        "entry": _entry_full_payload(selected, summary=entry_summary, topic=topic),
    }

    if resolved_format == "markdown":
        text = _entry_to_markdown(selected)
        return ToolResult(content=[TextContent(type="text", text=parity_banner + text)])

    if parity_banner:
        payload["_parity_warning"] = parity_banner.strip()
    return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])


def _get_thread_entry_range_impl(
    topic: str,
    start_index: int = 0,
    end_index: int | None = None,
    format: str = "json",
    summary_only: bool = False,
    code_path: str = "",
    code_branch: str | None = None,
) -> ToolResult:
    """Return a contiguous range of entries (inclusive).

    Args:
        topic: Thread topic identifier
        start_index: First entry index (inclusive)
        end_index: Last entry index (inclusive), or None for all remaining
        format: Output format - "markdown" or "json"
        summary_only: When True, returns only entry summaries (no bodies).
            Reduces token usage by ~90% — ideal for scanning a range.
        code_path: Code repository path for branch pairing context
        code_branch: Filter entries by code branch. Auto-populated from current
            branch in orphan mode. Pass "*" to see all branches.
    """

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
            "entries": [_entry_full_payload(entry, topic=topic) for entry in selected_entries],
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
    # Auto-populate code_branch from context
    if code_branch is None:
        code_branch = context.code_branch

    # Lightweight read sync: auto-pull if behind origin (never blocks)
    _sync_ok, sync_actions, parity, auto_heal_failed = ensure_readable(context.threads_dir, context.code_root)
    parity_banner = format_parity_warning(parity, auto_heal_failed=auto_heal_failed)
    if sync_actions:
        log_debug(f"get_thread_entry_range read sync: {sync_actions}")

    validation._refresh_threads(context)
    load_error, entries, summaries = _load_entries(topic, context, code_branch=code_branch)
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

    # Branch-discovery hint — same semantics as the other two branch-
    # filtered read tools. Only fires when filtered `entries` is empty
    # AND an explicit branch filter is active.
    branch_hint = ""
    branches_with_entries_list: list[str] = []
    if not entries and code_branch and code_branch != "*":
        _all_branches = get_branches_with_entries(context.threads_dir, topic)
        branch_hint = format_branch_discovery_hint(code_branch, _all_branches)
        if branch_hint:
            branches_with_entries_list = sorted(
                b for b in _all_branches if b != code_branch
            )
    hint_banner = f"{branch_hint}\n\n" if branch_hint else ""

    # Track entry access for all entries in range (non-blocking)
    if context.threads_dir:
        for entry in selected_entries:
            if entry.entry_id:
                _track_access(context.threads_dir, "entry", entry.entry_id)

    if summary_only:
        payload = {
            "topic": topic,
            "entry_count": total,
            "summary_only": True,
            "start_index": start_index,
            "end_index": effective_end if selected_entries else None,
            "entries": [
                _entry_header_payload(
                    entry,
                    summary=summaries.get(entry.entry_id or "", ""),
                    topic=topic,
                )
                for entry in selected_entries
            ],
        }
    else:
        payload = {
            "topic": topic,
            "entry_count": total,
            "start_index": start_index,
            "end_index": effective_end if selected_entries else None,
            "entries": [
                _entry_full_payload(
                    entry,
                    summary=summaries.get(entry.entry_id or "", ""),
                    topic=topic,
                )
                for entry in selected_entries
            ],
        }

    if resolved_format == "markdown":
        if not selected_entries:
            empty_text = "(no entries in range)"
            return ToolResult(content=[TextContent(type="text", text=hint_banner + empty_text)])
        if summary_only:
            lines = [f"Range for '{topic}':"]
            for entry in selected_entries:
                ts = entry.timestamp or "unknown"
                t = entry.title or "(untitled)"
                eid = entry.entry_id or ""
                eid_short = eid[:12] + "…" if len(eid) > 12 else eid
                s = summaries.get(eid, "")
                lines.append(f"- [{entry.index}] {ts} — {t} ({eid_short})")
                if s:
                    lines.append(f"  {s}")
            text = "\n".join(lines)
        else:
            markdown_blocks = []
            for entry in selected_entries:
                block = entry.header
                if entry.body:
                    block += "\n\n" + entry.body
                markdown_blocks.append(block)
            text = "\n\n---\n\n".join(markdown_blocks)
        return ToolResult(content=[TextContent(type="text", text=hint_banner + parity_banner + text)])

    if parity_banner:
        payload["_parity_warning"] = parity_banner.strip()
    if branch_hint:
        payload["_hint"] = branch_hint
        payload["_branches_with_entries"] = branches_with_entries_list
    return ToolResult(content=[TextContent(type="text", text=json.dumps(payload, indent=2))])


def register_thread_query_tools(mcp):
    """Register thread query tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global list_threads, read_thread, list_thread_entries, get_thread_entry

    # Register tools and store references for testing
    list_threads = mcp.tool(name="watercooler_list_threads")(_list_threads_impl)
    read_thread = mcp.tool(name="watercooler_read_thread")(_read_thread_impl)
    list_thread_entries = mcp.tool(name="watercooler_list_thread_entries")(_list_thread_entries_impl)
    get_thread_entry = mcp.tool(name="watercooler_get_thread_entry")(_get_thread_entry_impl)
