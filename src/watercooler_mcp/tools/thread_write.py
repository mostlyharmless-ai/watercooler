"""Thread write tools for watercooler MCP server.

Tools:
- watercooler_say: Add entry, flip ball
- watercooler_ack: Acknowledge without flip
- watercooler_handoff: Explicit handoff
- watercooler_set_status: Update thread status
"""

from fastmcp import Context
from ulid import ULID

from watercooler import commands, fs
from watercooler.metadata import thread_meta

from ..config import get_agent_name
from ..helpers import _format_warnings_for_response
from ..middleware import run_with_sync


# Module-level references to registered tools (populated by register_thread_write_tools)
say = None
ack = None
handoff = None
set_status = None


# Runtime accessors for patchable functions (tests patch via server module)
def _require_context(code_path: str):
    """Access _require_context at runtime for test patching."""
    from .. import server
    return server._require_context(code_path)


def _dynamic_context_missing(context):
    """Access _dynamic_context_missing at runtime for test patching."""
    from .. import server
    return server._dynamic_context_missing(context)


def _say_impl(
    topic: str,
    title: str,
    body: str,
    ctx: Context,
    role: str = "implementer",
    entry_type: str = "Note",
    create_if_missing: bool = False,
    code_path: str = "",
    agent_func: str = "",
) -> str:
    """Add your response to a thread and flip the ball to your counterpart.

    Use this when you want to contribute and pass the action to another agent.
    The ball automatically flips to your configured counterpart.

    Args:
        topic: Thread topic identifier (e.g., "feature-auth")
        title: Entry title - brief summary of your contribution
        body: Full entry content (markdown supported). In general, threads follow an arc:
            - Start: Persist the state of the project at the start, describe why the thread exists,
              and lay out the desired state change for the code/project
            - Middle: Reason towards the appropriate solution
            - End: Describe the effective solution reached
            - Often: Recap that arc in a closing message to the thread
            Thread entries should explicitly reference any files changed, using file paths
            (e.g., `src/watercooler_mcp/server.py`, `docs/README.md`) to maintain clear
            traceability of what was modified.
        role: Your role - planner, critic, implementer, tester, pm, or scribe (default: implementer)
        entry_type: Entry type - Note, Plan, Decision, PR, or Closure (default: Note)
        create_if_missing: Whether to create the thread if it doesn't exist (default: False, but threads are auto-created by commands.say)
        code_path: Path to the code repository directory containing the files most immediately
            under discussion in this thread. This establishes the code context for branch pairing
            and commit footers. Should point to the root of your working repository.
        agent_func: Agent identity in format '<platform>:<model>:<role>' where:
            - platform: The actual IDE/platform name (e.g., 'Cursor', 'Claude Code', 'Codex')
            - model: The exact model identifier as it identifies itself (e.g., 'Composer 1', 'sonnet-4', 'gpt-4')
            - role: The agent role (e.g., 'implementer', 'reviewer', 'planner')
            Full examples: 'Cursor:Composer 1:implementer', 'Claude Code:sonnet-4:reviewer', 'Codex:gpt-4:planner'
            This information is recorded in commit footers for full traceability.

    Returns:
        Confirmation message with updated ball status

    Example:
        say("feature-auth", "Implementation complete", "All tests passing. Ready for review.",
            role="implementer", entry_type="Note", code_path="/path/to/repo",
            agent_func="Cursor:Composer 1:implementer")
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return error
        if context is None:
            return "Error: Unable to resolve code context for the provided code_path."

        if not agent_func or ":" not in agent_func:
            return "identity required: pass agent_func as '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')"
        agent_base, agent_spec = [p.strip() for p in agent_func.split(":", 1)]
        if not agent_base or not agent_spec:
            return "identity invalid: agent_func must be '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')"

        threads_dir = context.threads_dir
        agent = agent_base or get_agent_name(ctx.client_id)

        # Generate unique Entry-ID for idempotency
        entry_id = str(ULID())

        # Define the append operation
        def append_operation():
            commands.say(
                topic,
                threads_dir=threads_dir,
                agent=agent,
                role=role,
                title=title,
                entry_type=entry_type,
                body=body,
                entry_id=entry_id,
            )

        run_with_sync(
            context,
            f"{agent}: {title} ({topic})",
            append_operation,
            topic=topic,
            entry_id=entry_id,
            agent_spec=agent_spec,
            priority_flush=True,
        )

        # Get updated thread meta to show new ball owner
        thread_path = fs.thread_path(topic, threads_dir)
        _, status, ball, _ = thread_meta(thread_path)

        return _format_warnings_for_response(
            f"✅ Entry added to '{topic}'\n"
            f"Title: {title}\n"
            f"Role: {role} | Type: {entry_type}\n"
            f"Ball flipped to: {ball}\n"
            f"Status: {status}"
        )

    except Exception as e:
        return f"Error adding entry to '{topic}': {str(e)}"


def _ack_impl(
    topic: str,
    ctx: Context,
    title: str = "",
    body: str = "",
    code_path: str = "",
    agent_func: str = "",
) -> str:
    """Acknowledge a thread without flipping the ball.

    Use this when you've read updates but don't need to pass the action.
    The ball stays with the current owner.

    Args:
        topic: Thread topic identifier
        title: Optional acknowledgment title (default: "Ack")
        body: Optional acknowledgment message (default: "ack")
        code_path: Path to the code repository directory containing the files most immediately
            under discussion in this thread. This establishes the code context for branch pairing
            and commit footers. Should point to the root of your working repository.
        agent_func: Agent identity in format '<platform>:<model>:<role>' where:
            - platform: The actual IDE/platform name (e.g., 'Cursor', 'Claude Code', 'Codex')
            - model: The exact model identifier as it identifies itself (e.g., 'Composer 1', 'sonnet-4', 'gpt-4')
            - role: The agent role (e.g., 'implementer', 'reviewer', 'planner')
            Full examples: 'Cursor:Composer 1:implementer', 'Claude Code:sonnet-4:reviewer', 'Codex:gpt-4:planner'
            This information is recorded in commit footers for full traceability.

    Returns:
        Confirmation message

    Example:
        ack("feature-auth", "Noted", "Thanks for the update, looks good!",
            code_path="/path/to/repo", agent_func="Claude Code:sonnet-4:reviewer")
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return error
        if context is None:
            return "Error: Unable to resolve code context for the provided code_path."
        if _dynamic_context_missing(context):
            return (
                "Dynamic threads repo was not resolved from your git context.\n"
                "Run from inside your code repo or set WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO."
            )

        if not agent_func or ":" not in agent_func:
            return "identity required: pass agent_func as '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')"
        agent_base, agent_spec = [p.strip() for p in agent_func.split(":", 1)]
        if not agent_base or not agent_spec:
            return "identity invalid: agent_func must be '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')"
        threads_dir = context.threads_dir
        agent = agent_base or get_agent_name(ctx.client_id)

        def ack_operation():
            commands.ack(
                topic,
                threads_dir=threads_dir,
                agent=agent,
                title=title or None,
                body=body or None,
            )

        run_with_sync(
            context,
            f"{agent}: {title or 'Ack'} ({topic})",
            ack_operation,
            topic=topic,
            agent_spec=agent_spec,
        )

        # Get updated thread meta
        thread_path = fs.thread_path(topic, threads_dir)
        _, status, ball, _ = thread_meta(thread_path)

        ack_title = title or "Ack"
        return (
            f"✅ Acknowledged '{topic}'\n"
            f"Title: {ack_title}\n"
            f"Ball remains with: {ball}\n"
            f"Status: {status}"
        )

    except Exception as e:
        return f"Error acknowledging '{topic}': {str(e)}"


def _handoff_impl(
    topic: str,
    ctx: Context,
    note: str = "",
    target_agent: str | None = None,
    code_path: str = "",
    agent_func: str = "",
) -> str:
    """Hand off the ball to another agent.

    If target_agent is None, hands off to your default counterpart.
    If target_agent is specified, explicitly hands off to that agent.

    Args:
        topic: Thread topic identifier
        note: Optional handoff message explaining context
        target_agent: Agent name to receive the ball (optional, uses counterpart if None)
        code_path: Path to the code repository directory containing the files most immediately
            under discussion in this thread. This establishes the code context for branch pairing
            and commit footers. Should point to the root of your working repository.
        agent_func: Agent identity in format '<platform>:<model>:<role>' where:
            - platform: The actual IDE/platform name (e.g., 'Cursor', 'Claude Code', 'Codex')
            - model: The exact model identifier as it identifies itself (e.g., 'Composer 1', 'sonnet-4', 'gpt-4')
            - role: The agent role (e.g., 'implementer', 'reviewer', 'planner')
            Full examples: 'Cursor:Composer 1:implementer', 'Claude Code:sonnet-4:reviewer', 'Codex:gpt-4:planner'
            This information is recorded in commit footers for full traceability.

    Returns:
        Confirmation with new ball owner

    Example:
        handoff("feature-auth", "Ready for your review", target_agent="Claude",
                code_path="/path/to/repo", agent_func="Cursor:Composer 1:implementer")
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return error
        if context is None:
            return "Error: Unable to resolve code context for the provided code_path."
        if _dynamic_context_missing(context):
            return (
                "Dynamic threads repo was not resolved from your git context.\n"
                "Run from inside your code repo or set WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO."
            )

        if not agent_func or ":" not in agent_func:
            return "identity required: pass agent_func as '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')"
        agent_base, agent_spec = [p.strip() for p in agent_func.split(":", 1)]
        if not agent_base or not agent_spec:
            return "identity invalid: agent_func must be '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')"
        threads_dir = context.threads_dir
        agent = agent_base or get_agent_name(ctx.client_id)

        if target_agent:
            def op():
                commands.set_ball(topic, threads_dir=threads_dir, ball=target_agent)
                if note:
                    commands.append_entry(
                        topic,
                        threads_dir=threads_dir,
                        agent=agent,
                        role="pm",
                        title=f"Handoff to {target_agent}",
                        entry_type="Note",
                        body=note,
                        ball=target_agent,
                    )

            run_with_sync(
                context,
                f"{agent}: Handoff to {target_agent} ({topic})",
                op,
                topic=topic,
                agent_spec=agent_spec,
                priority_flush=True,
            )

            return (
                f"✅ Ball handed off to: {target_agent}\n"
                f"Thread: {topic}\n"
                + (f"Note: {note}" if note else "")
            )
        else:
            def op():
                commands.handoff(
                    topic,
                    threads_dir=threads_dir,
                    agent=agent,
                    note=note or None,
                )

            run_with_sync(
                context,
                f"{agent}: Handoff ({topic})",
                op,
                topic=topic,
                agent_spec=agent_spec,
                priority_flush=True,
            )

            # Get updated thread meta
            thread_path = fs.thread_path(topic, threads_dir)
            _, status, ball, _ = thread_meta(thread_path)

            return (
                f"✅ Ball handed off to: {ball}\n"
                f"Thread: {topic}\n"
                f"Status: {status}\n"
                + (f"Note: {note}" if note else "")
            )

    except Exception as e:
        return f"Error handing off '{topic}': {str(e)}"


def _set_status_impl(
    topic: str,
    status: str,
    code_path: str = "",
    agent_func: str = "",
) -> str:
    """Update the status of a thread.

    Common statuses: OPEN, IN_REVIEW, CLOSED, BLOCKED

    Args:
        topic: Thread topic identifier
        status: New status value (e.g., "IN_REVIEW", "CLOSED")
        code_path: Path to the code repository directory containing the files most immediately
            under discussion in this thread. This establishes the code context for branch pairing
            and commit footers. Should point to the root of your working repository.
        agent_func: Agent identity in format '<platform>:<model>:<role>' where:
            - platform: The actual IDE/platform name (e.g., 'Cursor', 'Claude Code', 'Codex')
            - model: The exact model identifier as it identifies itself (e.g., 'Composer 1', 'sonnet-4', 'gpt-4')
            - role: The agent role (e.g., 'implementer', 'reviewer', 'planner')
            Full examples: 'Cursor:Composer 1:implementer', 'Claude Code:sonnet-4:reviewer', 'Codex:gpt-4:planner'
            This information is recorded in commit footers for full traceability.

    Returns:
        Confirmation message

    Example:
        set_status("feature-auth", "IN_REVIEW", code_path="/path/to/repo",
                   agent_func="Claude Code:sonnet-4:pm")
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return error
        if context is None:
            return "Error: Unable to resolve code context for the provided code_path."
        if _dynamic_context_missing(context):
            return (
                "Dynamic threads repo was not resolved from your git context.\n"
                "Run from inside your code repo or set WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO."
            )

        if not agent_func or ":" not in agent_func:
            return "identity required: pass agent_func as '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')"
        agent_base, agent_spec = [p.strip() for p in agent_func.split(":", 1)]
        if not agent_base or not agent_spec:
            return "identity invalid: agent_func must be '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')"
        threads_dir = context.threads_dir

        def op():
            commands.set_status(topic, threads_dir=threads_dir, status=status)

        priority_flush = status.strip().upper() == "CLOSED"

        run_with_sync(
            context,
            f"{agent_base}: Status changed to {status} ({topic})",
            op,
            topic=topic,
            agent_spec=agent_spec,
            priority_flush=priority_flush,
        )

        return (
            f"✅ Status updated for '{topic}'\n"
            f"New status: {status}"
        )

    except Exception as e:
        return f"Error setting status for '{topic}': {str(e)}"


def register_thread_write_tools(mcp):
    """Register thread write tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global say, ack, handoff, set_status

    # Register tools and store references for testing
    say = mcp.tool(name="watercooler_say")(_say_impl)
    ack = mcp.tool(name="watercooler_ack")(_ack_impl)
    handoff = mcp.tool(name="watercooler_handoff")(_handoff_impl)
    set_status = mcp.tool(name="watercooler_set_status")(_set_status_impl)
