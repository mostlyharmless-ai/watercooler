"""Sync tools for watercooler MCP server.

Tools:
- watercooler_sync: Force sync or get sync status
- watercooler_reindex: Generate thread index
"""

from fastmcp import Context

from watercooler import commands

from ..config import get_agent_name, get_threads_dir, get_git_sync_manager_from_context
from ..git_sync import GitPushError
from ..observability import log_debug


# Module-level references to registered tools (populated by register_sync_tools)
force_sync = None
reindex = None


# Runtime accessors for patchable functions (tests patch via server module)
def _require_context(code_path: str):
    """Access _require_context at runtime for test patching."""
    from .. import server
    return server._require_context(code_path)


def _force_sync_impl(
    ctx: Context,
    code_path: str = "",
    action: str = "now",
) -> str:
    """Inspect or flush the async git sync worker.

    Args:
        action: Action to perform - "status"/"inspect" to view sync state, or "now"/"flush" to force immediate sync (default: "now")
        code_path: Path to the code repository directory. This establishes the code context for
            determining which threads repository to sync. Should point to the root of your working repository.

    Returns:
        Status information or confirmation of sync operation
    """
    log_debug(f"TOOL_ENTRY: watercooler_sync(code_path={code_path!r}, action={action!r})")
    try:
        log_debug("TOOL_STEP: calling _require_context")
        error, context = _require_context(code_path)
        log_debug(f"TOOL_STEP: _require_context returned (error={error!r}, context={'present' if context else 'None'})")
        if error:
            return error
        if context is None:
            return "Error: Unable to resolve code context for the provided code_path."

        log_debug("TOOL_STEP: calling get_git_sync_manager_from_context")
        sync = get_git_sync_manager_from_context(context)
        log_debug(f"TOOL_STEP: get_git_sync_manager returned {'present' if sync else 'None'}")
        if not sync:
            return "Async sync unavailable: no git-enabled threads repository for this context."

        action_normalized = (action or "now").strip().lower()
        log_debug(f"TOOL_STEP: action_normalized={action_normalized!r}")

        def _format_status(info: dict) -> str:
            if info.get("mode") != "async":
                return "Async sync disabled; repository uses synchronous git writes."
            lines = ["Async sync status:"]
            lines.append(f"- Pending entries: {info.get('pending', 0)}")
            topics = info.get("pending_topics") or []
            if topics:
                lines.append(f"- Pending topics: {', '.join(topics)}")
            last_pull = info.get("last_pull")
            if last_pull:
                age = info.get("last_pull_age_seconds")
                age_fragment = f"{age:.1f}s ago" if age is not None else "recently"
                stale = " (stale)" if info.get("stale") else ""
                lines.append(f"- Last pull: {last_pull} ({age_fragment}){stale}")
            else:
                lines.append("- Last pull: never")
            next_eta = info.get("next_pull_eta_seconds")
            if next_eta is not None:
                lines.append(f"- Next background pull in: {next_eta:.1f}s")
            if info.get("is_syncing"):
                lines.append("- Sync in progress")
            if info.get("priority"):
                lines.append("- Priority flush requested")
            if info.get("retry_at"):
                retry_in = info.get("retry_in_seconds")
                extra = f" (in {retry_in:.1f}s)" if retry_in is not None else ""
                lines.append(f"- Next retry at: {info['retry_at']}{extra}")
            if info.get("last_error"):
                lines.append(f"- Last error: {info['last_error']}")
            return "\n".join(lines)

        if action_normalized in {"status", "inspect"}:
            log_debug("TOOL_STEP: calling sync.get_async_status()")
            status = sync.get_async_status()
            log_debug(f"TOOL_STEP: get_async_status returned {len(status)} keys")
            result = _format_status(status)
            log_debug(f"TOOL_STEP: formatted status, length={len(result)}")
            log_debug("TOOL_EXIT: returning status result")
            return result

        if action_normalized not in {"now", "flush"}:
            return f"Unknown action '{action}'. Use 'status' or 'now'."

        try:
            sync.flush_async()
        except GitPushError as exc:
            return f"Sync failed: {exc}"

        status_after = sync.get_async_status()
        remaining = status_after.get("pending", 0)
        prefix = "✅ Pending entries synced." if not remaining else f"⚠️ Sync completed with {remaining} entries still pending (retry scheduled)."
        return f"{prefix}\n\n{_format_status(status_after)}"

    except Exception as exc:  # pragma: no cover - defensive guard
        return f"Error running sync: {exc}"


def _reindex_impl(ctx: Context) -> str:
    """Generate and return the index content summarizing all threads.

    Creates a summary view organized by:
    - Actionable threads (where you have the ball)
    - Open threads (waiting on others)
    - In Review threads
    - Closed threads are excluded by default

    Returns:
        Index content (Markdown) with links and status markers
    """
    try:
        threads_dir = get_threads_dir()
        agent = get_agent_name(ctx.client_id)

        # Create threads directory if it doesn't exist
        if not threads_dir.exists():
            threads_dir.mkdir(parents=True, exist_ok=True)
            return f"No threads found. Threads directory created at: {threads_dir}\n\nCreate your first thread with watercooler_say."

        # Get all threads
        all_threads = commands.list_threads(threads_dir=threads_dir, open_only=None)

        if not all_threads:
            return f"No threads found in: {threads_dir}"

        # Categorize threads
        from watercooler.metadata import is_closed

        agent_lower = agent.lower()
        actionable = []
        in_review = []
        open_threads = []
        closed_threads = []

        for title, status, ball, updated, path, is_new in all_threads:
            topic = path.stem
            ball_lower = (ball or "").lower()
            has_ball = ball_lower == agent_lower

            if is_closed(status):
                closed_threads.append((topic, title, status, ball, updated, is_new))
            elif status.upper() == "IN_REVIEW":
                in_review.append((topic, title, status, ball, updated, is_new, has_ball))
            elif has_ball:
                actionable.append((topic, title, status, ball, updated, is_new))
            else:
                open_threads.append((topic, title, status, ball, updated, is_new))

        # Build index
        output = []
        output.append("# Watercooler Index\n")
        output.append(f"*Generated for: {agent}*\n")
        output.append(f"*Total threads: {len(all_threads)}*\n")

        if actionable:
            output.append(f"\n## 🎾 Actionable - Your Turn ({len(actionable)})\n")
            for topic, title, status, ball, updated, is_new in actionable:
                new_marker = " 🆕" if is_new else ""
                output.append(f"- [{topic}]({topic}.md){new_marker} - {title}")
                output.append(f"  *{status} | Updated: {updated}*")

        if open_threads:
            output.append(f"\n## ⏳ Open - Waiting on Others ({len(open_threads)})\n")
            for topic, title, status, ball, updated, is_new in open_threads:
                new_marker = " 🆕" if is_new else ""
                output.append(f"- [{topic}]({topic}.md){new_marker} - {title}")
                output.append(f"  *{status} | Ball: {ball} | Updated: {updated}*")

        if in_review:
            output.append(f"\n## 🔍 In Review ({len(in_review)})\n")
            for topic, title, status, ball, updated, is_new, has_ball in in_review:
                new_marker = " 🆕" if is_new else ""
                your_turn = " 🎾" if has_ball else ""
                output.append(f"- [{topic}]({topic}.md){new_marker}{your_turn} - {title}")
                output.append(f"  *{status} | Ball: {ball} | Updated: {updated}*")

        if closed_threads:
            output.append(f"\n## ✅ Closed ({len(closed_threads)})\n")
            for topic, title, status, ball, updated, is_new in closed_threads[:10]:  # Limit to 10
                output.append(f"- [{topic}]({topic}.md) - {title}")
                output.append(f"  *{status} | Updated: {updated}*")
            if len(closed_threads) > 10:
                output.append(f"\n*... and {len(closed_threads) - 10} more closed threads*")

        output.append(f"\n---\n*Threads directory: {threads_dir}*")

        return "\n".join(output)

    except Exception as e:
        return f"Error generating index: {str(e)}"


def register_sync_tools(mcp):
    """Register sync tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global force_sync, reindex

    # Register tools and store references for testing
    force_sync = mcp.tool(name="watercooler_sync")(_force_sync_impl)
    reindex = mcp.tool(name="watercooler_reindex")(_reindex_impl)
