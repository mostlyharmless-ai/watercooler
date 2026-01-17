"""Thread write tools for watercooler MCP server.

Tools:
- watercooler_say: Add entry, flip ball
- watercooler_ack: Acknowledge without flip
- watercooler_handoff: Explicit handoff
- watercooler_set_status: Update thread status

Modes:
- Local (stdio): Uses filesystem operations and git sync
- Hosted (HTTP): Uses GitHub API via hosted_ops module
"""

import logging
from datetime import datetime, timezone

from fastmcp import Context
from ulid import ULID

from watercooler import fs
from watercooler.baseline_graph.writer import get_thread_from_graph
from watercooler.commands_graph import (
    say_graph_first,
    ack_graph_first,
    handoff_graph_first,
    set_status_graph_first,
    set_ball_graph_first,
    append_entry_graph_first,
)

from ..config import get_agent_name, is_slack_enabled, is_slack_bot_enabled
from ..errors import (
    ContextError,
    HostedModeError,
    IdentityError,
    ThreadNotFoundError,
)
from ..helpers import _format_warnings_for_response
from ..hosted_ops import (
    say_hosted,
    ack_hosted,
    handoff_hosted,
    set_status_hosted,
)
from ..middleware import run_with_sync
from .. import validation  # Import module for runtime access (enables test patching)
from ..validation import is_hosted_context
from ..observability import log_debug, log_error
# Phase 1: Webhook notifications
from ..slack import notify_new_entry, notify_ball_flip, notify_handoff, notify_status_change
# Phase 2: Bidirectional sync
from ..slack import (
    sync_entry_to_slack,
    sync_status_change as slack_sync_status_change,
    sync_handoff as slack_sync_handoff,
    update_thread_parent,
)


# Module-level references to registered tools (populated by register_thread_write_tools)
say = None
ack = None
handoff = None
set_status = None


def _get_thread_meta(threads_dir, topic):
    """Get thread metadata from graph.

    Returns:
        Tuple of (title, status, ball, last_updated) or defaults if not found
    """
    thread = get_thread_from_graph(threads_dir, topic)
    if thread:
        return (
            thread.get("title", topic),
            thread.get("status", "OPEN"),
            thread.get("ball", ""),
            thread.get("last_updated", ""),
        )
    return (topic, "OPEN", "", "")


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
    import sys
    print(f"[DEBUG] _say_impl: topic={topic}, title={title}, agent_func={agent_func}", file=sys.stderr)

    error, context = validation._require_context(code_path)
    print(f"[DEBUG] _say_impl: context error={error}, context={context}", file=sys.stderr)
    if error:
        raise ContextError(error, code_path=code_path)
    if context is None:
        raise ContextError("Unable to resolve code context for the provided code_path.", code_path=code_path)

    print(f"[DEBUG] _say_impl: is_hosted_context={is_hosted_context(context)}", file=sys.stderr)

    if not agent_func or ":" not in agent_func:
        print(f"[DEBUG] _say_impl: IdentityError - agent_func invalid", file=sys.stderr)
        raise IdentityError()
    agent_base, agent_spec = [p.strip() for p in agent_func.split(":", 1)]
    if not agent_base or not agent_spec:
        raise IdentityError("identity invalid: agent_func must be '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')")

    agent = agent_base or get_agent_name(ctx.client_id)

    # =====================================================================
    # Hosted Mode Path (GitHub API)
    # =====================================================================
    if is_hosted_context(context):
        print(f"[DEBUG] _say_impl: ENTERING hosted mode branch", file=sys.stderr)
        log_debug(f"say: using hosted mode for topic={topic}")

        entry_id = str(ULID())
        print(f"[DEBUG] _say_impl: calling say_hosted with entry_id={entry_id}", file=sys.stderr)
        write_error, result = say_hosted(
            topic=topic,
            title=title,
            body=body,
            agent=agent,
            role=role,
            entry_type=entry_type,
            entry_id=entry_id,
            create_if_missing=create_if_missing,
        )

        print(f"[DEBUG] _say_impl: say_hosted returned error={write_error}, result={result}", file=sys.stderr)

        if write_error:
            log_error(f"say hosted mode failed: {write_error}")
            if "not found" in write_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(write_error, operation="say")

        status = result.get("status", "OPEN")
        ball = result.get("ball", "Agent")

        return _format_warnings_for_response(
            f"✅ Entry added to '{topic}'\n"
            f"Title: {title}\n"
            f"Role: {role} | Type: {entry_type}\n"
            f"Ball flipped to: {ball}\n"
            f"Status: {status}"
        )

    # =====================================================================
    # Local Mode Path (Filesystem)
    # =====================================================================
    threads_dir = context.threads_dir

    # Generate unique Entry-ID for idempotency
    entry_id = str(ULID())

    # Define the append operation (graph-first)
    def append_operation():
        say_graph_first(
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
    _, status, ball, _ = _get_thread_meta(threads_dir, topic)

    # Send Slack notification (fire-and-forget, non-blocking)
    if is_slack_enabled():
        notify_new_entry(
            topic=topic,
            agent=agent,
            title=title,
            role=role,
            entry_type=entry_type,
            code_repo=context.code_repo,
            ball=ball,
        )

    # Phase 2: Sync entry to Slack channel/thread (if bot enabled)
    if is_slack_bot_enabled():
        try:
            # Extract repo name from code_repo (e.g., "org/repo" -> "repo")
            repo_name = context.code_repo.split("/")[-1] if context.code_repo else ""
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            sync_entry_to_slack(
                repo=repo_name,
                topic=topic,
                entry_id=entry_id,
                agent=agent,
                role=role,
                entry_type=entry_type,
                title=title,
                body=body,
                timestamp=timestamp,
                status=status,
                ball_owner=ball,
                spec=agent_spec,
                threads_dir=threads_dir,
                branch=context.code_branch,
            )
        except Exception as e:
            # Log but don't fail the operation - Slack sync is best-effort
            logging.getLogger(__name__).warning(f"Slack sync failed for {topic}: {e}")

    return _format_warnings_for_response(
        f"✅ Entry added to '{topic}'\n"
        f"Title: {title}\n"
        f"Role: {role} | Type: {entry_type}\n"
        f"Ball flipped to: {ball}\n"
        f"Status: {status}"
    )


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
    error, context = validation._require_context(code_path)
    if error:
        raise ContextError(error, code_path=code_path)
    if context is None:
        raise ContextError("Unable to resolve code context for the provided code_path.", code_path=code_path)

    if not agent_func or ":" not in agent_func:
        raise IdentityError()
    agent_base, agent_spec = [p.strip() for p in agent_func.split(":", 1)]
    if not agent_base or not agent_spec:
        raise IdentityError("identity invalid: agent_func must be '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')")
    agent = agent_base or get_agent_name(ctx.client_id)

    # =====================================================================
    # Hosted Mode Path (GitHub API)
    # =====================================================================
    if is_hosted_context(context):
        log_debug(f"ack: using hosted mode for topic={topic}")

        write_error, result = ack_hosted(
            topic=topic,
            agent=agent,
            title=title or "Ack",
            body=body or "Acknowledged",
        )

        if write_error:
            log_error(f"ack hosted mode failed: {write_error}")
            if "not found" in write_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(write_error, operation="ack")

        status = result.get("status", "OPEN")
        ball = result.get("ball", "Agent")

        ack_title = title or "Ack"
        return (
            f"✅ Acknowledged '{topic}'\n"
            f"Title: {ack_title}\n"
            f"Ball remains with: {ball}\n"
            f"Status: {status}"
        )

    # =====================================================================
    # Local Mode Path (Filesystem)
    # =====================================================================
    if validation._dynamic_context_missing(context):
        raise ContextError(
            "Dynamic threads repo was not resolved from your git context. "
            "Run from inside your code repo or set WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO.",
            code_path=code_path,
        )

    threads_dir = context.threads_dir

    # Generate Entry-ID
    entry_id = str(ULID())

    # Define ack operation (graph-first)
    def ack_operation():
        ack_graph_first(
            topic,
            threads_dir=threads_dir,
            agent=agent,
            title=title or None,
            body=body or None,
            entry_id=entry_id,
        )

    run_with_sync(
        context,
        f"{agent}: {title or 'Ack'} ({topic})",
        ack_operation,
        topic=topic,
        entry_id=entry_id,
        agent_spec=agent_spec,
    )

    # Get updated thread meta
    _, status, ball, _ = _get_thread_meta(threads_dir, topic)

    ack_title = title or "Ack"
    return (
        f"✅ Acknowledged '{topic}'\n"
        f"Title: {ack_title}\n"
        f"Ball remains with: {ball}\n"
        f"Status: {status}"
    )


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
    error, context = validation._require_context(code_path)
    if error:
        raise ContextError(error, code_path=code_path)
    if context is None:
        raise ContextError("Unable to resolve code context for the provided code_path.", code_path=code_path)

    if not agent_func or ":" not in agent_func:
        raise IdentityError()
    agent_base, agent_spec = [p.strip() for p in agent_func.split(":", 1)]
    if not agent_base or not agent_spec:
        raise IdentityError("identity invalid: agent_func must be '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')")
    agent = agent_base or get_agent_name(ctx.client_id)

    # =====================================================================
    # Hosted Mode Path (GitHub API)
    # =====================================================================
    if is_hosted_context(context):
        log_debug(f"handoff: using hosted mode for topic={topic}")

        write_error, result = handoff_hosted(
            topic=topic,
            agent=agent,
            target_agent=target_agent,
            note=note,
        )

        if write_error:
            log_error(f"handoff hosted mode failed: {write_error}")
            if "not found" in write_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(write_error, operation="handoff")

        new_ball = result.get("ball", target_agent or "Agent")
        status = result.get("status", "OPEN")

        return (
            f"✅ Ball handed off to: {new_ball}\n"
            f"Thread: {topic}\n"
            f"Status: {status}\n"
            + (f"Note: {note}" if note else "")
        )

    # =====================================================================
    # Local Mode Path (Filesystem)
    # =====================================================================
    if validation._dynamic_context_missing(context):
        raise ContextError(
            "Dynamic threads repo was not resolved from your git context. "
            "Run from inside your code repo or set WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO.",
            code_path=code_path,
        )

    threads_dir = context.threads_dir

    # Generate Entry-ID (needed when note is provided)
    entry_id = str(ULID())

    if target_agent:
        # Define operation (graph-first)
        def op():
            set_ball_graph_first(topic, threads_dir=threads_dir, ball=target_agent)
            if note:
                append_entry_graph_first(
                    topic,
                    threads_dir=threads_dir,
                    agent=agent,
                    role="pm",
                    title=f"Handoff to {target_agent}",
                    entry_type="Note",
                    body=note,
                    ball=target_agent,
                    entry_id=entry_id,
                )

        run_with_sync(
            context,
            f"{agent}: Handoff to {target_agent} ({topic})",
            op,
            topic=topic,
            entry_id=entry_id if note else None,
            agent_spec=agent_spec,
            priority_flush=True,
        )

        # Send Slack notification (fire-and-forget, non-blocking)
        if is_slack_enabled():
            notify_handoff(
                topic=topic,
                from_agent=agent,
                to_agent=target_agent,
                note=note or None,
                code_repo=context.code_repo,
            )

        # Phase 2: Sync handoff to Slack thread (if bot enabled)
        if is_slack_bot_enabled():
            try:
                repo_name = context.code_repo.split("/")[-1] if context.code_repo else ""
                slack_sync_handoff(
                    repo=repo_name,
                    topic=topic,
                    from_agent=agent,
                    to_agent=target_agent,
                    note=note or None,
                )
            except Exception as e:
                logging.getLogger(__name__).warning(f"Slack handoff sync failed for {topic}: {e}")

        return (
            f"✅ Ball handed off to: {target_agent}\n"
            f"Thread: {topic}\n"
            + (f"Note: {note}" if note else "")
        )
    else:
        # Define operation (graph-first)
        def op():
            handoff_graph_first(
                topic,
                threads_dir=threads_dir,
                agent=agent,
                note=note or None,
                entry_id=entry_id,
            )

        run_with_sync(
            context,
            f"{agent}: Handoff ({topic})",
            op,
            topic=topic,
            entry_id=entry_id,
            agent_spec=agent_spec,
            priority_flush=True,
        )

        # Get updated thread meta
        _, status, ball, _ = _get_thread_meta(threads_dir, topic)

        # Send Slack notification (fire-and-forget, non-blocking)
        if is_slack_enabled():
            notify_handoff(
                topic=topic,
                from_agent=agent,
                to_agent=ball or "unknown",
                note=note or None,
                code_repo=context.code_repo,
            )

        # Phase 2: Sync handoff to Slack thread (if bot enabled)
        if is_slack_bot_enabled():
            try:
                repo_name = context.code_repo.split("/")[-1] if context.code_repo else ""
                slack_sync_handoff(
                    repo=repo_name,
                    topic=topic,
                    from_agent=agent,
                    to_agent=ball or "unknown",
                    note=note or None,
                )
            except Exception as e:
                logging.getLogger(__name__).warning(f"Slack handoff sync failed for {topic}: {e}")

        return (
            f"✅ Ball handed off to: {ball}\n"
            f"Thread: {topic}\n"
            f"Status: {status}\n"
            + (f"Note: {note}" if note else "")
        )


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
    error, context = validation._require_context(code_path)
    if error:
        raise ContextError(error, code_path=code_path)
    if context is None:
        raise ContextError("Unable to resolve code context for the provided code_path.", code_path=code_path)

    # =====================================================================
    # Hosted Mode Path (GitHub API)
    # Note: Identity not required for hosted mode status updates since
    # no entry is created - only thread metadata is updated.
    # =====================================================================
    if is_hosted_context(context):
        log_debug(f"set_status: using hosted mode for topic={topic}")

        write_error, result = set_status_hosted(
            topic=topic,
            status=status,
        )

        if write_error:
            log_error(f"set_status hosted mode failed: {write_error}")
            if "not found" in write_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(write_error, operation="set_status")

        return (
            f"✅ Status updated for '{topic}'\n"
            f"New status: {status}"
        )

    # =====================================================================
    # Local Mode Path - Identity required for commit messages
    # =====================================================================
    if not agent_func or ":" not in agent_func:
        raise IdentityError()
    agent_base, agent_spec = [p.strip() for p in agent_func.split(":", 1)]
    if not agent_base or not agent_spec:
        raise IdentityError("identity invalid: agent_func must be '<platform>:<model>:<role>' (e.g., 'Cursor:Composer 1:implementer')")

    # =====================================================================
    # Local Mode Path (Filesystem)
    # =====================================================================
    if validation._dynamic_context_missing(context):
        raise ContextError(
            "Dynamic threads repo was not resolved from your git context. "
            "Run from inside your code repo or set WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO.",
            code_path=code_path,
        )

    threads_dir = context.threads_dir

    # Get old status before change (for notification)
    old_status = None
    try:
        _, old_status, _, _ = _get_thread_meta(threads_dir, topic)
    except Exception:
        pass  # Thread may not exist yet

    # Define operation (graph-first)
    def op():
        set_status_graph_first(topic, threads_dir=threads_dir, status=status)

    priority_flush = status.strip().upper() == "CLOSED"

    run_with_sync(
        context,
        f"{agent_base}: Status changed to {status} ({topic})",
        op,
        topic=topic,
        agent_spec=agent_spec,
        priority_flush=priority_flush,
    )

    # Send Slack notification (fire-and-forget, non-blocking)
    if is_slack_enabled():
        notify_status_change(
            topic=topic,
            old_status=old_status,
            new_status=status,
            agent=agent_base,
            code_repo=context.code_repo,
        )

    # Phase 2: Sync status change to Slack thread (if bot enabled)
    if is_slack_bot_enabled():
        try:
            repo_name = context.code_repo.split("/")[-1] if context.code_repo else ""
            slack_sync_status_change(
                repo=repo_name,
                topic=topic,
                old_status=old_status or "UNKNOWN",
                new_status=status,
                changed_by=agent_base,
            )
            # Also update thread parent message with new status
            _, _, ball, _ = _get_thread_meta(threads_dir, topic)
            update_thread_parent(
                repo=repo_name,
                topic=topic,
                status=status,
                ball_owner=ball or "",
                entry_count=0,  # We don't track this currently
            )
        except Exception as e:
            logging.getLogger(__name__).warning(f"Slack status sync failed for {topic}: {e}")

    return (
        f"✅ Status updated for '{topic}'\n"
        f"New status: {status}"
    )


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
