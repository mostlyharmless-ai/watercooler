"""Daemon write helper — shared infrastructure for write-capable daemons.

Provides ``daemon_write_entry()`` which wraps ``run_with_sync()`` +
``commands_graph.ack()`` and classifies write outcomes into three buckets:

* **written+pushed** — entry exists locally *and* on the remote.
* **written_local_only** — entry exists locally but push failed.
* **not_written** — nothing durable was written; safe to retry.

**Never-raise contract**: ``daemon_write_entry()`` catches all exceptions
and returns a ``DaemonWriteResult``. Callers distinguish outcomes from the
result, not from exception handling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Allowed entry types for daemon writes — matches the Watercooler taxonomy.
_ALLOWED_ENTRY_TYPES = frozenset({"Note", "Decision", "Plan", "PR", "Closure"})


@dataclass(frozen=True)
class DaemonWriteResult:
    """Outcome of a daemon write attempt.

    Callers should branch on ``written`` and ``pushed`` to decide next steps:

    * ``written=True, pushed=True`` → success.
    * ``written=True, pushed=False`` → local commit exists, push failed.
      Mark finding processed to avoid duplicate writes on retry.
    * ``written=False`` → nothing durable happened; safe to retry next tick.
    """

    entry_id: str
    written: bool
    pushed: bool
    error: Optional[str] = None


def daemon_write_entry(
    topic: str,
    *,
    code_root: Path,
    title: str,
    body: str,
    agent: str = "Daemon",
    role: str = "scribe",
    entry_type: str = "Note",
    entry_id: Optional[str] = None,
    agent_spec: Optional[str] = None,
    code_branch: Optional[str] = None,
    ball: Optional[str] = None,
    status: Optional[str] = None,
    user_tag: str = "system",
    post_write_hooks: list[Callable[[str, Path, str], None]] | None = None,
) -> DaemonWriteResult:
    """Write a thread entry from daemon context.

    Synthesizes a ``ThreadContext`` via
    ``watercooler_mcp.config.resolve_thread_context()`` and calls
    ``run_with_sync()`` + ``commands_graph.ack()``.

    Designed for background daemon threads.  Follows the **never-raise**
    contract — all exceptions are caught, classified, and returned as a
    ``DaemonWriteResult``.

    Args:
        topic: Thread topic (must be non-empty).
        code_root: Repository root for ThreadContext resolution.
        title: Entry title.
        body: Entry body (must be non-empty).
        agent: Agent name (default ``"Daemon"``).
        role: Agent role (default ``"scribe"``).
        entry_type: Must be in {Note, Decision, Plan, PR, Closure}.
        entry_id: Optional ULID; generated if not provided.
        agent_spec: Daemon specialization for commit footers.
        code_branch: Optional override; defaults to resolved branch.
        ball: ``None`` preserves current ball owner.
        status: ``None`` preserves current status.
        user_tag: User tag for the entry (default ``"system"``).
        post_write_hooks: Optional list of callables invoked after
            ``graph_ack()`` but before commit/push.  Each receives
            ``(topic, threads_dir, entry_id)``.  Failures are logged
            but do not affect the write result.

    Returns:
        A :class:`DaemonWriteResult` — never raises.
    """

    # ------------------------------------------------------------------
    # Input validation (at the boundary — callers must not bypass)
    # ------------------------------------------------------------------
    if not topic or not isinstance(topic, str):
        return DaemonWriteResult(
            entry_id=entry_id or "",
            written=False,
            pushed=False,
            error="topic must be a non-empty string",
        )
    if not body or not isinstance(body, str):
        return DaemonWriteResult(
            entry_id=entry_id or "",
            written=False,
            pushed=False,
            error="body must be a non-empty string",
        )
    if not agent or not isinstance(agent, str):
        return DaemonWriteResult(
            entry_id=entry_id or "",
            written=False,
            pushed=False,
            error="agent must be a non-empty string",
        )
    if entry_type not in _ALLOWED_ENTRY_TYPES:
        return DaemonWriteResult(
            entry_id=entry_id or "",
            written=False,
            pushed=False,
            error=f"entry_type must be one of {sorted(_ALLOWED_ENTRY_TYPES)}, got '{entry_type}'",
        )

    # ------------------------------------------------------------------
    # Generate ULID if not provided
    # ------------------------------------------------------------------
    if not entry_id:
        try:
            from ulid import ULID

            entry_id = str(ULID())
        except Exception as e:
            return DaemonWriteResult(
                entry_id="",
                written=False,
                pushed=False,
                error=f"Failed to generate ULID: {e}",
            )

    # ------------------------------------------------------------------
    # Resolve ThreadContext — fail closed if code_root is missing
    # ------------------------------------------------------------------
    try:
        from watercooler_mcp.config import resolve_thread_context

        ctx = resolve_thread_context(code_root)
    except Exception as e:
        logger.warning("[DAEMON_WRITE] Failed to resolve ThreadContext: %s", e)
        return DaemonWriteResult(
            entry_id=entry_id,
            written=False,
            pushed=False,
            error=f"ThreadContext resolution failed: {e}",
        )

    if not ctx.threads_dir or not ctx.threads_dir.exists():
        logger.warning(
            "[DAEMON_WRITE] threads_dir missing or does not exist: %s",
            ctx.threads_dir,
        )
        return DaemonWriteResult(
            entry_id=entry_id,
            written=False,
            pushed=False,
            error=f"threads_dir does not exist: {ctx.threads_dir}",
        )

    if not ctx.code_root:
        logger.warning("[DAEMON_WRITE] code_root not resolved — failing closed")
        return DaemonWriteResult(
            entry_id=entry_id,
            written=False,
            pushed=False,
            error="code_root not resolved — write-capable daemon requires code_root",
        )

    # ------------------------------------------------------------------
    # Write via run_with_sync + commands_graph.ack
    # ------------------------------------------------------------------
    effective_branch = code_branch or ctx.code_branch

    def _classify_post_write_failure(
        error_msg: str,
        *,
        operation_completed: bool = False,
        committed: bool = False,
    ) -> DaemonWriteResult:
        verification_errors: list[str] = []
        try:
            from watercooler.baseline_graph.writer import get_entry_node_from_graph

            entry_node = get_entry_node_from_graph(
                ctx.threads_dir, entry_id, topic
            )
            if entry_node is not None:
                logger.warning(
                    "[DAEMON_WRITE] Entry %s written locally but push/post-write "
                    "failed for '%s': %s",
                    entry_id,
                    topic,
                    error_msg,
                )
                return DaemonWriteResult(
                    entry_id=entry_id,
                    written=True,
                    pushed=False,
                    error=error_msg,
                )
        except Exception as check_err:
            verification_errors.append(
                f"graph verification failed: {check_err}"
            )
            logger.warning(
                "[DAEMON_WRITE] Could not inspect local graph after error: %s",
                check_err,
            )

        try:
            from watercooler.fs import thread_path

            topic_path = thread_path(topic, ctx.threads_dir)
            if topic_path.exists():
                entry_marker = f"<!-- Entry-ID: {entry_id} -->"
                if entry_marker in topic_path.read_text(encoding="utf-8"):
                    logger.warning(
                        "[DAEMON_WRITE] Entry %s verified in markdown but push/post-write "
                        "failed for '%s': %s",
                        entry_id,
                        topic,
                        error_msg,
                    )
                    return DaemonWriteResult(
                        entry_id=entry_id,
                        written=True,
                        pushed=False,
                        error=error_msg,
                    )
        except Exception as check_err:
            verification_errors.append(
                f"markdown verification failed: {check_err}"
            )
            logger.warning(
                "[DAEMON_WRITE] Could not inspect thread markdown after error: %s",
                check_err,
            )

        if operation_completed or committed:
            verify_detail = ""
            if verification_errors:
                verify_detail = f" (could not verify local write: {'; '.join(verification_errors)})"
            logger.warning(
                "[DAEMON_WRITE] Operation reached post-write phase for '%s' but "
                "entry %s could not be verified locally; retrying",
                topic,
                entry_id,
            )
            return DaemonWriteResult(
                entry_id=entry_id,
                written=False,
                pushed=False,
                error=f"{error_msg}{verify_detail}",
            )

        logger.warning(
            "[DAEMON_WRITE] Write failed for '%s': %s",
            topic,
            error_msg,
        )
        return DaemonWriteResult(
            entry_id=entry_id,
            written=False,
            pushed=False,
            error=error_msg,
        )

    sync_status: dict[str, object] = {}

    try:
        from watercooler_mcp.middleware import run_with_sync
        from watercooler.commands_graph import ack as graph_ack

        def _do_write():
            result = graph_ack(
                topic,
                threads_dir=ctx.threads_dir,
                agent=agent,
                role=role,
                title=title,
                entry_type=entry_type,
                body=body,
                status=status,
                ball=ball,
                user_tag=user_tag,
                entry_id=entry_id,
                code_branch=effective_branch,
            )
            if post_write_hooks:
                for hook in post_write_hooks:
                    try:
                        hook(topic, ctx.threads_dir, entry_id)
                    except Exception as hook_err:
                        logger.warning(
                            "[DAEMON_WRITE] Post-write hook failed (non-fatal): %s",
                            hook_err,
                        )
            return result

        run_with_sync(
            ctx,
            f"daemon: {agent_spec or agent} — {title[:60]}",
            _do_write,
            topic=topic,
            entry_id=entry_id,
            agent_spec=agent_spec,
            sync_status=sync_status,
        )

        if sync_status.get("pushed") is True:
            return DaemonWriteResult(
                entry_id=entry_id,
                written=True,
                pushed=True,
            )

        error_msg = str(
            sync_status.get("error")
            or "Entry written locally but commit/push status is incomplete"
        )
        return _classify_post_write_failure(
            error_msg,
            operation_completed=bool(sync_status.get("operation_completed")),
            committed=bool(sync_status.get("committed")),
        )

    except Exception as e:
        return _classify_post_write_failure(
            str(e),
            operation_completed=bool(sync_status.get("operation_completed")),
            committed=bool(sync_status.get("committed")),
        )
