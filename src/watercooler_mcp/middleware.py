"""Middleware for watercooler MCP server.

Contains:
- Instrumentation: FunctionTool monkey-patching for observability
- Sync wrappers: run_with_sync, run_with_graph_sync
"""

import sys
import json
import time
from typing import Callable, TypeVar

from .config import (
    ThreadContext,
    get_git_sync_manager_from_context,
    get_watercooler_config,
)
# Import from new sync package where possible
from .sync import BranchPairingError, ParityStatus, read_parity_state, write_parity_state
# Legacy imports (still needed until full migration)
from .branch_parity import (
    run_preflight,
    acquire_topic_lock,
    _now_iso,
)
from .observability import log_debug, log_action, log_warning
from .helpers import _should_auto_branch, _build_commit_footers


# Store original FunctionTool.run for instrumentation
_orig_run = None

T = TypeVar("T")


def setup_instrumentation() -> None:
    """Set up FunctionTool instrumentation for observability.

    Call this once at server startup to monkey-patch FastMCP's FunctionTool.run
    method with timing and logging.
    """
    global _orig_run

    try:
        from fastmcp.tools.tool import FunctionTool  # type: ignore

        _orig_run = FunctionTool.run

        async def _instrumented_run(self, arguments):  # type: ignore
            tool_name = getattr(self, 'name', '<unknown>')
            input_chars = len(json.dumps(arguments)) if arguments else 0
            start_time = time.perf_counter()
            outcome = "ok"
            try:
                result = await _orig_run(self, arguments)
                return result
            except Exception:
                outcome = "error"
                raise
            finally:
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                try:
                    log_action(
                        "mcp.tool",
                        tool_name=tool_name,
                        input_chars=input_chars,
                        duration_ms=duration_ms,
                        outcome=outcome,
                    )
                    # Workaround: Force stdout flush on Windows after tool execution
                    if sys.platform == "win32":
                        sys.stdout.flush()
                        sys.stderr.flush()
                except Exception:
                    pass

        FunctionTool.run = _instrumented_run  # type: ignore
    except Exception:
        pass


def run_with_sync(
    context: ThreadContext,
    commit_title: str,
    operation: Callable[[], T],
    *,
    topic: str | None = None,
    entry_id: str | None = None,
    agent_spec: str | None = None,
    priority_flush: bool = False,
    skip_validation: bool = False,
) -> T:
    """Execute operation with git sync and branch parity enforcement.

    Flow: acquire lock → run preflight → operation → commit → push → release lock

    The new preflight state machine replaces the old _validate_and_sync_branches()
    with comprehensive auto-remediation:
    - Branch mismatch: auto-checkout threads to match code
    - Missing remote branch: auto-push threads branch
    - Threads behind origin: auto-pull with ff-only or rebase
    - Main protection: block writes when threads=main but code=feature
    """
    sync = get_git_sync_manager_from_context(context)
    if not sync:
        return operation()

    # Per-topic locking to serialize concurrent writes
    lock = None
    try:
        if topic and context.threads_dir:
            try:
                lock = acquire_topic_lock(context.threads_dir, topic, timeout=30)
                log_debug(f"[PARITY] Acquired lock for topic '{topic}'")
            except TimeoutError as e:
                raise BranchPairingError(f"Failed to acquire lock for topic '{topic}': {e}")

        # Run preflight with auto-remediation instead of old validation
        if not skip_validation and context.code_root and context.threads_dir:
            preflight_result = run_preflight(
                code_repo_path=context.code_root,
                threads_repo_path=context.threads_dir,
                auto_fix=_should_auto_branch(),
                fetch_first=True,
            )
            if not preflight_result.can_proceed:
                raise BranchPairingError(
                    preflight_result.blocking_reason or "Branch parity preflight failed"
                )
            if preflight_result.auto_fixed:
                log_debug(f"[PARITY] Auto-fixed: {preflight_result.state.actions_taken}")

        # Build commit footers
        footers = _build_commit_footers(
            context,
            topic=topic,
            entry_id=entry_id,
            agent_spec=agent_spec,
        )
        commit_message = commit_title if not footers else f"{commit_title}\n\n" + "\n".join(footers)

        # Execute operation with git sync (pull → operation → commit → push)
        result = sync.with_sync(
            operation,
            commit_message,
            topic=topic,
            entry_id=entry_id,
            priority_flush=priority_flush,
        )

        # Sync to baseline graph (non-blocking - failures don't stop the write)
        if topic and context.threads_dir:
            log_warning(f"[GRAPH] Attempting graph sync for {topic}/{entry_id}")
            try:
                from watercooler.baseline_graph.sync import sync_entry_to_graph

                # Get graph config for summary/embedding generation
                wc_config = get_watercooler_config()
                graph_config = wc_config.mcp.graph
                log_warning(f"[GRAPH] Config: summaries={graph_config.generate_summaries}, embeddings={graph_config.generate_embeddings}")

                sync_result = sync_entry_to_graph(
                    threads_dir=context.threads_dir,
                    topic=topic,
                    entry_id=entry_id,
                    generate_summaries=graph_config.generate_summaries,
                    generate_embeddings=graph_config.generate_embeddings,
                )
                log_warning(f"[GRAPH] Sync result for {topic}/{entry_id}: {sync_result}")

                # Phase 2: Commit graph files to keep working tree clean
                # This prevents uncommitted graph files from blocking future preflight pulls
                if sync_result:
                    graph_committed = sync.commit_graph_changes(topic, entry_id)
                    if graph_committed:
                        log_warning(f"[GRAPH] Graph files committed for {topic}/{entry_id}")
                    else:
                        log_warning(f"[GRAPH] Graph commit skipped or failed for {topic}/{entry_id}")
            except Exception as graph_err:
                # Graph sync failure should not block the write operation
                log_warning(f"[GRAPH] Sync failed (non-blocking): {graph_err}")
                try:
                    from watercooler.baseline_graph.sync import record_graph_sync_error

                    record_graph_sync_error(context.threads_dir, topic, entry_id, graph_err)
                except Exception:
                    pass  # Best effort error recording

        # Update parity state file after successful write
        if context.code_root and context.threads_dir:
            try:
                state = read_parity_state(context.threads_dir)
                # Mark as clean after successful sync
                state.status = ParityStatus.CLEAN.value
                state.pending_push = False
                state.last_check_at = _now_iso()
                state.last_error = None
                write_parity_state(context.threads_dir, state)
            except Exception as state_err:
                log_debug(f"[PARITY] Failed to update state after write: {state_err}")

        return result
    finally:
        if lock:
            lock.release()
            log_debug(f"[PARITY] Released lock for topic '{topic}'")


def run_with_graph_sync(
    context: ThreadContext,
    operation: Callable[[], T],
    commit_msg: str,
) -> T:
    """Execute graph operation with full parity protocol.

    Flow: preflight → operation → commit graph files → push with retry

    Unlike run_with_sync, this is simpler:
    - No topic lock (graph operations are idempotent)
    - No entry footers (graph files, not thread entries)
    - Commits only graph/baseline/* files
    """
    sync = get_git_sync_manager_from_context(context)

    # 1. Preflight (Factor 1 + Factor 2 pre-check)
    if context.code_root and context.threads_dir:
        preflight_result = run_preflight(
            code_repo_path=context.code_root,
            threads_repo_path=context.threads_dir,
            auto_fix=True,
            fetch_first=True,
        )
        if not preflight_result.can_proceed:
            raise BranchPairingError(
                preflight_result.blocking_reason or "Branch parity preflight failed"
            )
        if preflight_result.auto_fixed:
            log_debug(f"[GRAPH-SYNC] Preflight auto-fixed: {preflight_result.state.actions_taken}")

    # 2. Execute operation
    result = operation()

    # 3. Commit and push graph files (blocking)
    if sync and context.threads_dir:
        sync.commit_graph_changes_sync(commit_msg)

    return result
