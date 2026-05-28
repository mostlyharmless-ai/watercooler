"""Sync tools for watercooler MCP server.

This module registers no MCP tools after the PR4b consolidation —
watercooler_reindex (a pre-graph-first markdown thread index) was retired in
favour of the graph-first watercooler_list_threads, which already offers
format="json"/"markdown". _reindex_impl / _reindex_hosted_impl are retained
but no longer MCP-exposed. The CLI `wc reindex` (graph initialisation) is a
separate command and is unaffected.
"""

from fastmcp import Context

from watercooler import commands

from ..config import get_agent_name, get_threads_dir


def _reindex_hosted_impl(ctx: Context) -> str:
    """Generate thread index from hosted GitHub API data."""
    from ..hosted_ops import list_threads_hosted
    from watercooler.fs import is_closed

    agent = get_agent_name(ctx.client_id)
    err, threads = list_threads_hosted()
    if err:
        return f"Error listing threads in hosted mode: {err}"
    if not threads:
        return "No threads found in hosted mode."

    agent_lower = agent.lower()
    actionable = []
    in_review = []
    open_threads = []
    closed_threads = []

    for t in threads:
        ball_lower = (t.ball or "").lower()
        has_ball = ball_lower == agent_lower

        if is_closed(t.status):
            closed_threads.append(t)
        elif t.status.upper() == "IN_REVIEW":
            in_review.append((t, has_ball))
        elif has_ball:
            actionable.append(t)
        else:
            open_threads.append(t)

    output = []
    output.append("# Watercooler Index (Hosted Mode)\n")
    output.append(f"*Generated for: {agent}*\n")
    output.append(f"*Total threads: {len(threads)}*\n")

    if actionable:
        output.append(f"\n## Actionable - Your Turn ({len(actionable)})\n")
        for t in actionable:
            output.append(f"- {t.topic} - {t.title}")
            output.append(f"  *{t.status} | Updated: {t.last_updated}*")

    if open_threads:
        output.append(f"\n## Open - Waiting on Others ({len(open_threads)})\n")
        for t in open_threads:
            output.append(f"- {t.topic} - {t.title}")
            output.append(f"  *{t.status} | Ball: {t.ball} | Updated: {t.last_updated}*")

    if in_review:
        output.append(f"\n## In Review ({len(in_review)})\n")
        for t, has_ball in in_review:
            your_turn = " (your turn)" if has_ball else ""
            output.append(f"- {t.topic}{your_turn} - {t.title}")
            output.append(f"  *{t.status} | Ball: {t.ball} | Updated: {t.last_updated}*")

    if closed_threads:
        output.append(f"\n## Closed ({len(closed_threads)})\n")
        for t in closed_threads[:10]:
            output.append(f"- {t.topic} - {t.title}")
            output.append(f"  *{t.status} | Updated: {t.last_updated}*")

    return "\n".join(output)


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
    # Hosted mode guard — additive early return
    from ..auth import is_hosted_mode
    if is_hosted_mode():
        return _reindex_hosted_impl(ctx)

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
        from watercooler.fs import is_closed

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

    No tools remain in this module after the PR4b consolidation; retained as a
    stable no-op so server_factory's registration sequence is unchanged.

    Args:
        mcp: The FastMCP server instance
    """
    return None
