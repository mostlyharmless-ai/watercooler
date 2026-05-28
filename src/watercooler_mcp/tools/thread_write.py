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
from pathlib import Path

from fastmcp import Context
from ulid import ULID

from watercooler.role_loader import validate_role

from watercooler import fs
from watercooler.baseline_graph.writer import get_thread_from_graph
from watercooler import commands_graph

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
from ..sync.errors import PushError
from .. import validation  # Import module for runtime access (enables test patching)
from ..validation import is_hosted_context
from ..observability import log_debug, log_error


def _run_with_sync_report_push(context, *args, **kwargs) -> str:
    """Wrapper around run_with_sync that returns a push warning string.

    Returns empty string on success, or a warning message if push failed.
    The entry is still committed locally on push failure.
    """
    try:
        run_with_sync(context, *args, **kwargs)
        return ""
    except PushError as e:
        return f"\n\n⚠️ {e.message}"
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


_TERMINAL_STATUSES = frozenset({"CLOSED", "RESOLVED", "DONE", "MERGED"})


def _next_signal(
    entry_type: str = "Note",
    ball: str = "",
    target_agent: str | None = None,
    status: str | None = None,
    keep_ball: bool = False,
) -> str:
    """Return the Ball:/Next: advisory line appended to every write-tool response.

    Next: meanings:
    - stop         — entry_type is Closure, or status transitioned to terminal
    - handoff      — ball explicitly passed to a named agent
    - keep-working — ball stayed with the caller (ack path); caller should continue
    - continue     — ball flipped to counterpart; counterpart should act next
    """
    if entry_type == "Closure":
        return f"Ball: {ball}. Next: stop. Phase complete."
    if status and status.strip().upper() in _TERMINAL_STATUSES:
        return f"Ball: {ball or 'counterpart'}. Next: stop. Status is terminal ({status.strip().upper()})."
    if target_agent:
        return f"Ball: {target_agent}. Next: handoff. Ball passed to {target_agent}."
    if keep_ball:
        return f"Ball: {ball or 'you'}. Next: keep-working. Ball stayed with you."
    return f"Ball: {ball or 'counterpart'}. Next: continue."


def _touch_annotation(threads_dir, topic, entry_id=None):
    """Update last_touched on annotation state for thread and entry.

    DEPRECATED: This function is a no-op. Write-path activity is now tracked
    via ``last_updated`` in meta.json (committed inside the sync transaction).
    Calling ``update_last_touched()`` *after* sync completes would write
    ``annotation_state.json`` outside the commit/push pipeline, dirtying the
    worktree and causing chronic sync divergence (the "worktree poisoning" bug).

    Kept as a no-op stub so existing call sites don't need to be removed
    in the same commit — they will be cleaned up separately.
    """
    pass


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
        role: Your role — call watercooler_roles for the active catalog (default: implementer)
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

    # Validate role against project role set (early error, user-friendly message)
    try:
        role = validate_role(role, code_path=code_path or None) or role
    except ValueError as exc:
        return str(exc)

    # =====================================================================
    # Hosted Mode Path (GitHub API)
    # =====================================================================
    if is_hosted_context(context):
        log_debug(f"say: using hosted mode for topic={topic}")
        from ..daemons import ensure_hosted_scope_for_current_context
        ensure_hosted_scope_for_current_context(reason="hosted_say")

        entry_id = str(ULID())
        write_error, result = say_hosted(
            topic=topic,
            title=title,
            body=body,
            agent=agent,
            role=role,
            entry_type=entry_type,
            entry_id=entry_id,
            create_if_missing=create_if_missing,
            code_branch=context.code_branch,
        )

        if write_error:
            log_error(f"say hosted mode failed: {write_error}")
            if "not found" in write_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(write_error, operation="say")

        status = result.get("status", "OPEN")
        ball = result.get("ball", "Agent")
        slack_synced = result.get("slack_synced", False)

        lines = [
            f"✅ Entry added to '{topic}'",
            f"Title: {title}",
            f"Role: {role} | Type: {entry_type}",
            f"Ball flipped to: {ball}",
            f"Status: {status}",
            f"Entry-ID: {entry_id}",
        ]
        if slack_synced:
            lines.append("Slack: synced")
        lines.append(_next_signal(entry_type, ball))

        return _format_warnings_for_response("\n".join(lines))

    # =====================================================================
    # Local Mode Path (Filesystem)
    # =====================================================================
    threads_dir = context.threads_dir

    # Generate unique Entry-ID for idempotency
    entry_id = str(ULID())

    # Define the append operation
    def append_operation():
        commands_graph.say(
            topic,
            threads_dir=threads_dir,
            agent=agent,
            role=role,
            title=title,
            entry_type=entry_type,
            body=body,
            entry_id=entry_id,
            code_branch=context.code_branch,
            code_root=Path(code_path) if code_path else None,
        )

    push_warning = _run_with_sync_report_push(
        context,
        f"{agent}: {title} ({topic})",
        append_operation,
        topic=topic,
        entry_id=entry_id,
        agent_spec=agent_spec,
        priority_flush=True,
    )

    # Update last_touched for entry and thread annotations
    _touch_annotation(threads_dir, topic, entry_id)

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
        f"Status: {status}\n"
        f"Entry-ID: {entry_id}"
        f"{push_warning}\n"
        f"{_next_signal(entry_type, ball)}"
    )


def _ack_impl(
    topic: str,
    ctx: Context,
    title: str = "",
    body: str = "",
    code_path: str = "",
    agent_func: str = "",
    role: str | None = None,
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
        from ..daemons import ensure_hosted_scope_for_current_context
        ensure_hosted_scope_for_current_context(reason="hosted_ack")

        write_error, result = ack_hosted(
            topic=topic,
            agent=agent,
            title=title or "Ack",
            body=body or "Acknowledged",
            code_branch=context.code_branch,
            role=role or "pm",
        )

        if write_error:
            log_error(f"ack hosted mode failed: {write_error}")
            if "not found" in write_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(write_error, operation="ack")

        status = result.get("status", "OPEN")
        ball = result.get("ball", "Agent")

        ack_title = title or "Ack"
        # keep_ball is only truthful when the caller actually owns the ball.
        # ack preserves the existing owner — if that owner is someone else,
        # the signal must say so (Next: continue with the real owner) rather
        # than falsely claiming the caller can keep working.
        # Case+whitespace-insensitive comparison — matches the convention in
        # hosted_ops.py:1691-1693 since ball values can drift in case/spacing
        # across human edits, defaulted values ("Agent"), and platform names.
        caller_holds_ball = (
            bool(ball)
            and ball.strip().lower() == (agent or "").strip().lower()
        )
        return (
            f"✅ Acknowledged '{topic}'\n"
            f"Title: {ack_title}\n"
            f"Ball remains with: {ball}\n"
            f"Status: {status}\n"
            f"{_next_signal(ball=ball, keep_ball=caller_holds_ball)}"
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

    # Define ack operation
    def ack_operation():
        commands_graph.ack(
            topic,
            threads_dir=threads_dir,
            agent=agent,
            role=role,
            title=title or None,
            body=body or None,
            entry_id=entry_id,
            code_branch=context.code_branch,
            code_root=Path(code_path) if code_path else None,
        )

    push_warning = _run_with_sync_report_push(
        context,
        f"{agent}: {title or 'Ack'} ({topic})",
        ack_operation,
        topic=topic,
        entry_id=entry_id,
        agent_spec=agent_spec,
    )

    # Update last_touched for entry and thread annotations
    _touch_annotation(threads_dir, topic, entry_id)

    # Get updated thread meta
    _, status, ball, _ = _get_thread_meta(threads_dir, topic)

    ack_title = title or "Ack"
    caller_holds_ball = (
        bool(ball)
        and ball.strip().lower() == (agent or "").strip().lower()
    )
    return (
        f"✅ Acknowledged '{topic}'\n"
        f"Title: {ack_title}\n"
        f"Ball remains with: {ball}\n"
        f"Status: {status}"
        f"{push_warning}\n"
        f"{_next_signal(ball=ball, keep_ball=caller_holds_ball)}"
    )


def _handoff_impl(
    topic: str,
    ctx: Context,
    note: str = "",
    target_agent: str | None = None,
    code_path: str = "",
    agent_func: str = "",
    role: str | None = None,
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

    # Defense-in-depth: scrub CR/LF and surrounding whitespace from target_agent
    # for direct callers that bypass _write_impl's normalization. An embedded
    # newline could otherwise forge commit-message footers downstream.
    if isinstance(target_agent, str):
        normalized = target_agent.replace("\r", " ").replace("\n", " ").strip()
        target_agent = normalized or None

    # =====================================================================
    # Hosted Mode Path (GitHub API)
    # =====================================================================
    if is_hosted_context(context):
        log_debug(f"handoff: using hosted mode for topic={topic}")
        from ..daemons import ensure_hosted_scope_for_current_context
        ensure_hosted_scope_for_current_context(reason="hosted_handoff")

        write_error, result = handoff_hosted(
            topic=topic,
            agent=agent,
            target_agent=target_agent,
            note=note,
            code_branch=context.code_branch,
            role=role or "pm",
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
            + (f"Note: {note}\n" if note else "")
            + _next_signal(ball=new_ball, target_agent=target_agent or new_ball)
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
        # Define operation
        def op():
            commands_graph.set_ball(topic, threads_dir=threads_dir, ball=target_agent)
            if note:
                commands_graph.append_entry(
                    topic,
                    threads_dir=threads_dir,
                    agent=agent,
                    role=role or "pm",
                    title=f"Handoff to {target_agent}",
                    entry_type="Note",
                    body=note,
                    ball=target_agent,
                    entry_id=entry_id,
                    code_branch=context.code_branch,
                )

        push_warning = _run_with_sync_report_push(
            context,
            f"{agent}: Handoff to {target_agent} ({topic})",
            op,
            topic=topic,
            entry_id=entry_id if note else None,
            agent_spec=agent_spec,
            priority_flush=True,
        )

        # Update last_touched for entry and thread annotations
        _touch_annotation(threads_dir, topic, entry_id if note else None)

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
            + (f"Note: {note}\n" if note else "")
            + push_warning
            + f"\n{_next_signal(ball=target_agent, target_agent=target_agent)}"
        )
    else:
        # Define operation
        def op():
            commands_graph.handoff(
                topic,
                threads_dir=threads_dir,
                agent=agent,
                role=role or "pm",
                note=note or None,
                entry_id=entry_id,
                code_branch=context.code_branch,
                code_root=Path(code_path) if code_path else None,
            )

        push_warning = _run_with_sync_report_push(
            context,
            f"{agent}: Handoff ({topic})",
            op,
            topic=topic,
            entry_id=entry_id,
            agent_spec=agent_spec,
            priority_flush=True,
        )

        # Update last_touched for entry and thread annotations
        _touch_annotation(threads_dir, topic, entry_id)

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
            + (f"Note: {note}\n" if note else "")
            + push_warning
            # Implicit handoff (target_agent=None) still changed the ball, so
            # always emit Next: handoff using the resolved ball as the target.
            + f"\n{_next_signal(ball=ball, target_agent=target_agent or ball)}"
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
        from ..daemons import ensure_hosted_scope_for_current_context
        ensure_hosted_scope_for_current_context(reason="hosted_set_status")

        write_error, result = set_status_hosted(
            topic=topic,
            status=status,
        )

        if write_error:
            log_error(f"set_status hosted mode failed: {write_error}")
            if "not found" in write_error.lower():
                raise ThreadNotFoundError(topic=topic, repo=context.code_repo)
            raise HostedModeError(write_error, operation="set_status")

        hosted_ball = result.get("ball", "") if isinstance(result, dict) else ""
        return (
            f"✅ Status updated for '{topic}'\n"
            f"New status: {status}\n"
            f"{_next_signal(ball=hosted_ball, status=status)}"
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

    # Define operation
    def op():
        commands_graph.set_status(topic, threads_dir=threads_dir, status=status)

    priority_flush = status.strip().upper() == "CLOSED"

    push_warning = _run_with_sync_report_push(
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

    # Resolve current ball owner so the response advisory names the real actor.
    # Status updates don't move the ball, but the Ball: <owner> signal must
    # still be truthful for downstream agents following the stop-naturally
    # contract.
    current_ball = ""
    try:
        _, _, current_ball, _ = _get_thread_meta(threads_dir, topic)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "set_status: _get_thread_meta failed for topic=%s: %s", topic, exc
        )

    return (
        f"✅ Status updated for '{topic}'\n"
        f"New status: {status}"
        f"{push_warning}\n"
        f"{_next_signal(ball=current_ball or '', status=status)}"
    )


_VALID_AUTHORITY_MODES = frozenset({"ordinary", "decision", "closure"})


def _write_impl(
    topic: str,
    body: str,
    ctx: Context,
    role: str,
    agent_func: str,
    next_actor: str = "auto",
    authority_mode: str = "ordinary",
    authorization_text: str | None = None,
    downgrade_to_note: bool = False,
    code_path: str = "",
) -> str:
    """Unified write path — the preferred tool for ordinary agent writes.

    Wraps say/ack/handoff internally.  Direct tools remain available for
    explicit coordination control or when title/spec/entry_type override matters.

    Args:
        topic: Thread topic identifier
        body: Entry body (markdown).  Must start with a ``Spec: <spec>`` line;
            if absent one is prepended from ``role``.
        role: Your canonical role: planner | critic | implementer | tester | pm | scribe
        agent_func: Agent identity ``<platform>:<model>:<role>``
        next_actor: ``auto`` (flip ball, same as say) | ``self`` (keep ball, same
            as ack) | ``<agent name>`` (explicit handoff)
        authority_mode: ``ordinary`` (Note) | ``decision`` (Decision entry) |
            ``closure`` (Closure entry).  ``decision`` and ``closure`` require
            ``authorization_text``; without it the call writes nothing and returns
            an error unless ``downgrade_to_note=True``.
        authorization_text: Explicit authorization statement required for
            ``authority_mode="decision"`` or ``"closure"``.
        downgrade_to_note: When True and ``authority_mode`` is decision/closure
            but ``authorization_text`` is absent, write a Note with a loud
            warning instead of returning an error.
        code_path: Path to the repository root (required). The git branch is
            resolved from the code_path context; per-call branch override is
            not supported here — use the direct say/ack/handoff tools if you
            need to bypass context resolution.

    Returns:
        Confirmation string with Ball: / Next: advisory suffix.
    """
    if authority_mode not in _VALID_AUTHORITY_MODES:
        return (
            f"❌ watercooler_write: invalid authority_mode={authority_mode!r}. "
            f"Use 'ordinary', 'decision', or 'closure'."
        )

    # Delegate role validation to the project catalog (consults
    # .watercooler/roles.toml with bundled defaults as fallback).
    try:
        validate_role(role, code_path=code_path or None)
    except ValueError as exc:
        return f"❌ watercooler_write: {exc}"

    # Contract: "auto" | "self" | <explicit agent name>. Whitespace-only or
    # empty values must not route as an implicit handoff (target_agent="" is
    # falsy and would flip the ball to the default counterpart). Embedded CR/LF
    # in an explicit agent name would forge commit-message footers downstream,
    # same threat as authorization_text — scrub before the emptiness check.
    if isinstance(next_actor, str):
        next_actor = next_actor.replace("\r", " ").replace("\n", " ").strip()
    if not next_actor:
        return (
            f"❌ watercooler_write: next_actor must be 'auto', 'self', or an "
            f"explicit agent name. Got empty/whitespace value."
        )

    # Sanitize authorization_text up front: whitespace-only or newline-only is
    # truthy but carries no human-readable authorization. CR/LF scrub also
    # blocks commit-footer forgery via embedded newlines.
    if authorization_text:
        authorization_text = authorization_text.replace("\r", " ").replace("\n", " ").strip()
    needs_auth = authority_mode in ("decision", "closure")
    downgraded_from: str | None = None
    if needs_auth and not authorization_text:
        if not downgrade_to_note:
            return (
                f"❌ watercooler_write: authority_mode={authority_mode!r} requires "
                f"authorization_text. Pass authorization_text=<explicit statement> "
                f"or set downgrade_to_note=True to write a Note instead."
            )
        downgraded_from = authority_mode
        authority_mode = "ordinary"

    # entry_type only flows through _say_impl; ack/handoff drop it. Allowing
    # Decision/Closure on next_actor in ("self", <agent>) would silently write
    # a Note and pretend authority was exercised.
    if authority_mode != "ordinary" and next_actor != "auto":
        return (
            f"❌ watercooler_write: authority_mode={authority_mode!r} requires "
            f"next_actor='auto' (Decision/Closure entries must flip the ball via say). "
            f"Got next_actor={next_actor!r}."
        )

    entry_type_map = {"ordinary": "Note", "decision": "Decision", "closure": "Closure"}
    entry_type = entry_type_map[authority_mode]

    # Per CLAUDE.md, the first body line must be `Spec: <spec>`. The spec value
    # is free-form documentation (canonical role, sub-spec like
    # `planner-architecture`, or cross-cutting label like `security-audit`,
    # `docs`, `ops`, `general-purpose`, `active-disagreement`). The structural
    # role lives in graph metadata via the `role` field; the Spec line is for
    # human readers and does not need to match. Preserve a caller-supplied
    # Spec line verbatim; prepend `Spec: <role>` only when absent.
    stripped = body.lstrip()
    if stripped.startswith("Spec:"):
        body = stripped
    else:
        body = f"Spec: {role}\n\n{stripped}"

    # Banner injected after the Spec line so the first byte of the body
    # remains `Spec:` — protocol invariant.
    if downgraded_from is not None:
        spec_line, _, rest = body.partition("\n")
        # partition() leaves a trailing \r when the body used CRLF endings;
        # strip it so the Spec line doesn't carry stray control bytes.
        spec_line = spec_line.rstrip("\r")
        banner = (
            f"[watercooler_write: downgraded from {downgraded_from} "
            f"— no authorization_text]"
        )
        body = f"{spec_line}\n\n{banner}\n\n{rest.lstrip()}" if rest else f"{spec_line}\n\n{banner}"

    if authorization_text:
        body = body + f"\n\n[watercooler_write: authorized — {authorization_text}]"

    # Title is the first non-empty, non-Spec line; ellipsized past TITLE_MAX.
    TITLE_MAX = 60
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("Spec:")]
    raw = lines[0] if lines else body.strip()
    title = (raw[: TITLE_MAX - 3] + "…") if len(raw) > TITLE_MAX else raw

    if next_actor == "self":
        return _ack_impl(
            topic=topic,
            ctx=ctx,
            title=title,
            body=body,
            code_path=code_path,
            agent_func=agent_func,
            role=role,
        )

    if next_actor not in ("auto", "self"):
        return _handoff_impl(
            topic=topic,
            ctx=ctx,
            note=body,
            target_agent=next_actor,
            code_path=code_path,
            agent_func=agent_func,
            role=role,
        )

    return _say_impl(
        topic=topic,
        title=title,
        body=body,
        ctx=ctx,
        role=role,
        entry_type=entry_type,
        create_if_missing=False,
        code_path=code_path,
        agent_func=agent_func,
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
    mcp.tool(name="watercooler_write")(_write_impl)
