"""Watercooler MCP Server - Phase 1A MVP

FastMCP server exposing watercooler-cloud tools to AI agents.
All tools are namespaced as watercooler_* for provider compatibility.

Phase 1A features:
- 7 core tools + 2 diagnostic tools
- Markdown-only output (format param accepted but unused)
- Simple env-based config (WATERCOOLER_AGENT, WATERCOOLER_DIR)
- Basic error handling with helpful messages
"""

import sys
if sys.version_info < (3, 10):
    raise RuntimeError(
        f"Watercooler MCP requires Python 3.10+; found {sys.version.split()[0]}"
    )

# Standard library imports
import json
import os
import time
from pathlib import Path
from typing import Callable, TypeVar, Optional, List

# Third-party imports
from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from ulid import ULID
from git import Repo, InvalidGitRepositoryError, GitCommandError

# Local application imports
from watercooler import commands, fs
from watercooler.config_facade import config
from watercooler.metadata import thread_meta
from watercooler.thread_entries import ThreadEntry
from .config import (
    ThreadContext,
    get_agent_name,
    get_threads_dir,
    get_version,
    get_git_sync_manager_from_context,
    get_watercooler_config,
    resolve_thread_context,
)
from .git_sync import (
    GitPushError,
    BranchPairingError,
    BranchMismatch,
    validate_branch_pairing,
    _find_main_branch,
)
from .branch_parity import (
    run_preflight,
    acquire_topic_lock,
    write_parity_state,
    get_branch_health,
    ensure_readable,
    ParityStatus,
)
from .observability import log_debug, log_action, log_warning, log_error

# Import helpers (extracted for modularity)
from .helpers import (
    # Constants
    _ALLOWED_FORMATS,
    _MAX_LIMIT,
    _MAX_OFFSET,
    _CLOSED_STATES,
    # Startup warnings
    _add_startup_warning,
    _get_startup_warnings,
    _format_warnings_for_response,
    # Context helpers
    _should_auto_branch,
    _require_context,
    _dynamic_context_missing,
    # Branch helpers
    _attempt_auto_fix_divergence,
    _validate_and_sync_branches,
    _refresh_threads,
    # Thread parsing
    _normalize_status,
    _extract_thread_metadata,
    _resolve_format,
    # Entry loading
    _load_thread_entries,
    _entry_header_payload,
    _entry_full_payload,
    # Graph helpers
    _use_graph_for_reads,
    _track_access,
    _graph_entry_to_thread_entry,
    _load_thread_entries_graph_first,
    _list_threads_graph_first,
    # Commit helpers
    _build_commit_footers,
)


# Keep _validate_thread_context in server.py to allow test patching of _require_context
def _validate_thread_context(code_path: str) -> tuple[str | None, ThreadContext | None]:
    """Validate and resolve thread context for MCP tools.

    Note: This function is kept in server.py (not helpers.py) so that tests can
    patch _require_context and _dynamic_context_missing via the server module.

    Args:
        code_path: Path to code repository

    Returns:
        Tuple of (error_message, context). If error_message is not None,
        context will be None.
    """
    error, context = _require_context(code_path)
    if error:
        return (error, None)
    if context is None:
        return (
            "Error: Unable to resolve code context for the provided code_path.",
            None,
        )
    if _dynamic_context_missing(context):
        return (
            "Dynamic threads repo was not resolved from your git context.\n"
            "Run from inside your code repo or set "
            "WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO.",
            None,
        )
    return (None, context)

# Workaround for Windows stdio hang: Force auto-flush on every stdout write
# On Windows, FastMCP's stdio transport gets stuck after subprocess operations
# Auto-flushing after every write prevents response from getting stuck in buffer
if sys.platform == "win32":
    import io

    class AutoFlushWrapper(io.TextIOWrapper):
        def write(self, s):
            result = super().write(s)
            self.flush()
            return result

    # Wrap stdout with auto-flush
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = AutoFlushWrapper(
            sys.stdout.buffer,
            encoding=sys.stdout.encoding,
            errors=sys.stdout.errors,
            newline=None,
            line_buffering=False,
            write_through=True
        )

# Initialize FastMCP server with configurable transport
# WATERCOOLER_MCP_TRANSPORT: "http" or "stdio" (default: "stdio" for backward compatibility)
_TRANSPORT = config.env.get("WATERCOOLER_MCP_TRANSPORT", "stdio").lower()
mcp = FastMCP(name="Watercooler Cloud")


# Instrument FastMCP tool execution for observability
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


T = TypeVar("T")


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
                from watercooler_mcp.config import get_watercooler_config

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
                from watercooler_mcp.branch_parity import (
                    read_parity_state,
                    write_parity_state,
                    _now_iso,
                    ParityStatus,
                )
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


# ============================================================================
# Resources (Instructions & Documentation)
# ============================================================================

@mcp.resource("watercooler://instructions")
def get_instructions() -> str:
    """Get comprehensive instructions for using watercooler effectively.

    This resource provides quick-start guidance, common workflows, and best
    practices for AI agents collaborating via watercooler threads.
    """
    return f"""# Watercooler Cloud Guide for AI Agents

## 🎯 Quick Start

The simplest way to collaborate:

```bash
watercooler say <topic> --title "Your title" --body "Your message"
```

This ONE command:
- Creates the thread if it doesn't exist
- Adds your entry with timestamp and attribution
- Automatically flips the ball to your counterpart
- Returns you to async work

## 📋 Common Workflows

### Starting a New Discussion
```bash
watercooler say feature-auth --title "Authentication design" --body "Proposing OAuth2 with JWT tokens..."
```

### Responding to a Thread
```bash
# First, see what's new
watercooler list

# Read the full thread
watercooler say <topic> --title "Response" --body "Agreed, let's proceed..."
```

### Acknowledging Without Taking Action
```bash
watercooler ack <topic> --title "Noted" --body "Thanks, looks good!"
```

### Handing Off to Someone Specific
```bash
watercooler handoff <topic> "Ready for review" --target Claude
```

### Updating Thread Status
```bash
watercooler set-status <topic> IN_REVIEW
watercooler set-status <topic> CLOSED
```

## 🎾 The Ball Pattern

The **ball** indicates whose turn it is:
- When you `say`, the ball flips to your counterpart automatically
- When you `ack`, the ball stays where it is
- When you `handoff`, you explicitly pass the ball

## 💡 Best Practices

1. **Be Specific** - Use descriptive topic names (feature-auth, bug-login, refactor-api)
2. **Stay Focused** - One thread per topic/decision
3. **Mark NEW threads** - The `list` command shows NEW entries you haven't seen
4. **Close When Done** - Use `set-status <topic> CLOSED` when resolved
5. **Keep it Async** - Don't wait for responses, move on to other work

## 🔧 Available Tools (MCP)

- `watercooler_list_threads` - See all threads, identify where you have the ball
- `watercooler_read_thread` - Read full thread content
- `watercooler_say` - Add entry and flip ball (most common)
- `watercooler_ack` - Acknowledge without flipping ball
- `watercooler_handoff` - Explicitly hand off to another agent
- `watercooler_set_status` - Update thread status
- `watercooler_reindex` - Generate summary of all threads
- `watercooler_health` - Check server status
- `watercooler_whoami` - Check your agent identity

## 🚀 Pro Tips

- Thread topics use kebab-case (feature-auth, not "Feature Auth")
- Titles should be brief summaries (1-5 words)
- Bodies support full markdown formatting
- You can pass `--role` (planner/implementer/tester/pm/critic/scribe)
- You can pass `--type` (Note/Plan/Decision/PR/Closure)

---
*Generated by Watercooler MCP Server v{get_version()}*
"""


# ============================================================================
# Diagnostic Tools (Phase 1A)
# ============================================================================

@mcp.tool(name="watercooler_health")
def health(ctx: Context, code_path: str = "") -> str:
    """Check server health and configuration including branch parity status.

    Returns server version, configured agent identity, threads directory,
    and branch parity health status.

    Args:
        code_path: Optional path to code repository for parity checks.

    Example output:
        Watercooler MCP Server v0.1.0
        Status: Healthy
        Agent: Codex
        Threads Dir: /path/to/project/.watercooler
        Threads Dir Exists: True
        Branch Parity: clean
    """
    try:
        agent = get_agent_name(ctx.client_id)
        context = resolve_thread_context(Path(code_path) if code_path else None)
        threads_dir = context.threads_dir
        version = get_version()

        # Create threads directory if it doesn't exist
        if not threads_dir.exists():
            threads_dir.mkdir(parents=True, exist_ok=True)

        # Lightweight diagnostics to help average users verify env
        py_exec = sys.executable or "unknown"
        try:
            import fastmcp as _fm
            fm_ver = getattr(_fm, "__version__", "unknown")
        except Exception:
            fm_ver = "not-importable"

        status_lines = [
            f"Watercooler MCP Server v{version}",
            f"Status: Healthy",
            f"Agent: {agent}",
            f"Threads Dir: {threads_dir}",
            f"Threads Dir Exists: {threads_dir.exists()}",
            f"Threads Repo URL: {context.threads_repo_url or 'local-only'}",
            f"Code Branch: {context.code_branch or 'n/a'}",
            f"Auto-Branch: {'enabled' if _should_auto_branch() else 'disabled'}",
            f"Python: {py_exec}",
            f"fastmcp: {fm_ver}",
        ]

        # Add graph service status
        try:
            from watercooler_mcp.config import get_watercooler_config
            from watercooler.baseline_graph.summarizer import (
                SummarizerConfig,
                is_llm_service_available,
                create_summarizer_config,
            )
            from watercooler.baseline_graph.sync import (
                EmbeddingConfig,
                is_embedding_available,
            )

            wc_config = get_watercooler_config()
            graph_config = wc_config.mcp.graph

            # Check service availability
            summarizer_cfg = create_summarizer_config()
            llm_available = is_llm_service_available(summarizer_cfg)
            embed_cfg = EmbeddingConfig.from_env()
            embed_available = is_embedding_available(embed_cfg)

            status_lines.extend([
                "",
                "Graph Services:",
                f"  Summaries Enabled: {graph_config.generate_summaries}",
                f"  LLM Service: {'available' if llm_available else 'unavailable'} ({summarizer_cfg.api_base})",
                f"  Embeddings Enabled: {graph_config.generate_embeddings}",
                f"  Embedding Service: {'available' if embed_available else 'unavailable'} ({embed_cfg.api_base})",
                f"  Auto-Detect Services: {graph_config.auto_detect_services}",
            ])
        except Exception as e:
            status_lines.append(f"\nGraph Services: Error - {e}")

        # Add branch parity health if code and threads repos are available
        if context.code_root and context.threads_dir:
            try:
                parity_health = get_branch_health(context.code_root, context.threads_dir)
                status_lines.extend([
                    "",
                    "Branch Parity:",
                    f"  Status: {parity_health.get('status', 'unknown')}",
                    f"  Code Branch: {parity_health.get('code_branch', 'n/a')}",
                    f"  Threads Branch: {parity_health.get('threads_branch', 'n/a')}",
                    f"  Code Ahead/Behind: {parity_health.get('code_ahead_origin', 0)}/{parity_health.get('code_behind_origin', 0)}",
                    f"  Threads Ahead/Behind: {parity_health.get('threads_ahead_origin', 0)}/{parity_health.get('threads_behind_origin', 0)}",
                    f"  Pending Push: {parity_health.get('pending_push', False)}",
                ])
                if parity_health.get('last_error'):
                    status_lines.append(f"  Last Error: {parity_health.get('last_error')}")
                if parity_health.get('actions_taken'):
                    status_lines.append(f"  Actions Taken: {', '.join(parity_health.get('actions_taken', []))}")
                if parity_health.get('lock_holder'):
                    status_lines.append(f"  Lock Holder: PID {parity_health.get('lock_holder')}")
            except Exception as e:
                status_lines.append(f"\nBranch Parity: Error - {e}")

        return _format_warnings_for_response("\n".join(status_lines))
    except Exception as e:
        return _format_warnings_for_response(f"Watercooler MCP Server\nStatus: Error\nError: {str(e)}")


@mcp.tool(name="watercooler_whoami")
def whoami(ctx: Context) -> str:
    """Get your resolved agent identity.

    Returns the agent name that will be used when you create entries.
    Automatically detects your identity from the MCP client.

    Example:
        You are: Claude
    """
    try:
        agent = get_agent_name(ctx.client_id)
        debug_info = f"\nClient ID: {ctx.client_id or 'None'}\nSession ID: {ctx.session_id or 'None'}"
        return f"You are: {agent}{debug_info}"
    except Exception as e:
        return f"Error determining identity: {str(e)}"


@mcp.tool(name="watercooler_reconcile_parity")
def reconcile_parity(
    ctx: Context,
    code_path: str = "",
) -> ToolResult:
    """Rerun branch parity preflight with auto-remediation and retry pending push.

    Use this tool to:
    - Recover from failed pushes (e.g., network issues, conflicts)
    - Sync threads branch when it's behind origin
    - Force a sync after manual thread edits outside MCP
    - Proactively ensure parity before starting work on a branch
    - Debug branch state issues by inspecting the detailed response

    Args:
        code_path: Path to code repository directory (default: current directory)

    Returns:
        JSON with parity status, actions taken, and push result if applicable.
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.code_root or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve code and threads repo paths."
            )])

        from watercooler_mcp.branch_parity import (
            get_branch_health,
            run_preflight,
            push_after_commit,
            read_parity_state,
            write_parity_state,
            _pull_ff_only,
            _pull_rebase,
            ParityStatus,
        )
        from git import Repo

        # First, try to sync threads if behind origin (the reconcile part)
        threads_repo = Repo(context.threads_dir, search_parent_directories=True)
        code_repo = Repo(context.code_root, search_parent_directories=True)
        actions_taken = []

        # Get current health before reconcile
        health_before = get_branch_health(context.code_root, context.threads_dir)

        # Check if CODE is behind origin - this requires user action, we can't auto-fix
        code_behind = health_before.get('code_behind_origin', 0)
        if code_behind > 0:
            code_branch = health_before.get('code_branch', 'unknown')
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "status": "code_behind_origin",
                    "error": f"Code branch '{code_branch}' is {code_behind} commits behind origin. "
                             f"Please pull the code repo first: git pull",
                    "code_behind": code_behind,
                    "code_branch": code_branch,
                    "code_root": str(context.code_root),
                    "suggested_commands": [
                        f"cd {context.code_root}",
                        "git pull --rebase",
                    ],
                    "actions_taken": [],
                }, indent=2)
            )])

        # If threads is behind, pull it (this is the "reconcile" operation)
        threads_behind = health_before.get('threads_behind_origin', 0)
        if threads_behind > 0:
            log_debug(f"[RECONCILE] Threads behind origin by {threads_behind} commits, pulling")
            if _pull_ff_only(threads_repo):
                actions_taken.append(f"Pulled threads (ff-only, {threads_behind} commits)")
            else:
                log_debug("[RECONCILE] FF-only pull failed, trying rebase")
                if _pull_rebase(threads_repo):
                    actions_taken.append(f"Pulled threads (rebase, {threads_behind} commits)")
                else:
                    # Pull failed - let run_preflight handle any conflicts (including graph-only conflicts)
                    log_debug("[RECONCILE] Pull with rebase failed, will check for conflicts in preflight")
                    actions_taken.append(f"Pull with rebase failed (conflicts may exist)")

        # Run preflight with auto-fix enabled
        preflight_result = run_preflight(
            context.code_root,
            context.threads_dir,
            auto_fix=True,
            fetch_first=True,
        )

        # Collect preflight actions
        if preflight_result.state.actions_taken:
            actions_taken.extend(preflight_result.state.actions_taken)

        # Get updated health status
        health = get_branch_health(context.code_root, context.threads_dir)

        # If there are pending commits, try to push them
        push_result = None
        if health.get('pending_push') or health.get('threads_ahead_origin', 0) > 0:
            try:
                # Use threads_branch from health (correct branch)
                branch_name = health.get('threads_branch') or context.code_branch or "main"
                push_success, push_error = push_after_commit(
                    context.threads_dir,
                    branch_name,
                    max_retries=3
                )
                if push_success:
                    push_result = "pushed successfully"
                    actions_taken.append(f"Pushed threads to origin/{branch_name}")
                else:
                    push_result = f"push failed: {push_error}"
                # Refresh health after push
                health = get_branch_health(context.code_root, context.threads_dir)
            except Exception as push_err:
                push_result = f"push error: {push_err}"

        output = {
            "status": health.get('status', 'unknown'),
            "code_branch": health.get('code_branch', 'unknown'),
            "threads_branch": health.get('threads_branch', 'unknown'),
            "code_ahead_origin": health.get('code_ahead_origin', 0),
            "code_behind_origin": health.get('code_behind_origin', 0),
            "threads_ahead_origin": health.get('threads_ahead_origin', 0),
            "threads_behind_origin": health.get('threads_behind_origin', 0),
            "pending_push": health.get('pending_push', False),
            "actions_taken": actions_taken,
            "push_result": push_result,
            "last_error": health.get('last_error'),
            "preflight_success": preflight_result.success,
            "preflight_can_proceed": preflight_result.can_proceed,
        }

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(output, indent=2)
        )])

    except InvalidGitRepositoryError as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error: Not a git repository: {str(e)}"
        )])
    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error reconciling parity: {str(e)}"
        )])


# ============================================================================
# Core Tools (Phase 1A)
# ============================================================================

@mcp.tool(name="watercooler_list_threads")
def list_threads(
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
            return ToolResult(content=[TextContent(type="text", text=f"Error: Phase 1A only supports format='markdown'. JSON support coming in Phase 1B.")])

        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None:
            return ToolResult(content=[TextContent(type="text", text="Error: Unable to resolve code context for the provided code_path.")])
        log_debug(f"list_threads start code_path={code_path!r} open_only={open_only}")
        if context and _dynamic_context_missing(context):
            log_debug("list_threads dynamic context missing")
            return ToolResult(content=[TextContent(type="text", text=(
                "Dynamic threads repo was not resolved from your git context.\n"
                "Run from inside your code repo or set WATERCOOLER_CODE_REPO/WATERCOOLER_GIT_REPO on the MCP server.\n"
                f"Resolved threads dir: {context.threads_dir} (local fallback).\n"
                f"Code root: {context.code_root or Path.cwd()}"
            ))])

        agent = get_agent_name(ctx.client_id)

        # Lightweight read sync: auto-pull if behind origin (never blocks)
        sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
        if sync_actions:
            log_debug(f"list_threads read sync: {sync_actions}")

        log_debug("list_threads refreshing git state")
        git_start = time.time()
        _refresh_threads(context)
        git_elapsed = time.time() - git_start
        log_debug(f"list_threads git refreshed in {git_elapsed:.2f}s")
        threads_dir = context.threads_dir

        # Create threads directory if it doesn't exist
        if not threads_dir.exists():
            threads_dir.mkdir(parents=True, exist_ok=True)
            log_debug("list_threads created empty threads directory")
            return ToolResult(content=[TextContent(type="text", text=f"No threads found. Threads directory created at: {threads_dir}\n\nCreate your first thread with watercooler_say.")])

        # Get thread list (graph-first with markdown fallback)
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

    except Exception as e:
        log_error(f"list_threads error: {e}")
        return ToolResult(content=[TextContent(type="text", text=f"Error listing threads: {str(e)}")])


@mcp.tool(name="watercooler_read_thread")
def read_thread(
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
            return fmt_error

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

        # Lightweight read sync: auto-pull if behind origin (never blocks)
        sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
        if sync_actions:
            log_debug(f"read_thread read sync: {sync_actions}")

        _refresh_threads(context)
        threads_dir = context.threads_dir

        # Create threads directory if it doesn't exist
        if not threads_dir.exists():
            threads_dir.mkdir(parents=True, exist_ok=True)

        thread_path = fs.thread_path(topic, threads_dir)

        if not thread_path.exists():
            return f"Error: Thread '{topic}' not found in {threads_dir}\n\nAvailable threads: {', '.join(p.stem for p in threads_dir.glob('*.md')) if threads_dir.exists() else 'none'}"

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

    except Exception as e:
        return f"Error reading thread '{topic}': {str(e)}"


@mcp.tool(name="watercooler_list_thread_entries")
def list_thread_entries(
    topic: str,
    offset: int = 0,
    limit: int | None = None,
    format: str = "json",
    code_path: str = "",
) -> ToolResult:
    """Return thread entry headers (metadata only) with optional pagination."""

    fmt_error, resolved_format = _resolve_format(format, default="json")
    if fmt_error:
        return ToolResult(content=[TextContent(type="text", text=fmt_error)])

    error, context = _validate_thread_context(code_path)
    if error or context is None:
        return ToolResult(content=[TextContent(type="text", text=error or "Unknown error")])

    if offset < 0:
        return ToolResult(content=[TextContent(type="text", text="Error: offset must be non-negative.")])
    if offset > _MAX_OFFSET:
        return ToolResult(content=[TextContent(type="text", text=f"Error: offset must not exceed {_MAX_OFFSET}.")])
    if limit is not None and limit < 0:
        return ToolResult(content=[TextContent(type="text", text="Error: limit must be non-negative when provided.")])
    if limit is not None and limit > _MAX_LIMIT:
        return ToolResult(content=[TextContent(type="text", text=f"Error: limit must not exceed {_MAX_LIMIT}.")])

    # Lightweight read sync: auto-pull if behind origin (never blocks)
    sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
    if sync_actions:
        log_debug(f"list_thread_entries read sync: {sync_actions}")

    _refresh_threads(context)
    load_error, entries = _load_thread_entries_graph_first(topic, context)
    if load_error:
        return ToolResult(content=[TextContent(type="text", text=load_error)])

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


@mcp.tool(name="watercooler_get_thread_entry")
def get_thread_entry(
    topic: str,
    index: int | None = None,
    entry_id: str | None = None,
    format: str = "json",
    code_path: str = "",
) -> ToolResult:
    """Return a single thread entry (header + body)."""

    fmt_error, resolved_format = _resolve_format(format, default="json")
    if fmt_error:
        return ToolResult(content=[TextContent(type="text", text=fmt_error)])

    if index is None and entry_id is None:
        return ToolResult(content=[TextContent(type="text", text="Error: provide either index or entry_id to select an entry.")])

    error, context = _validate_thread_context(code_path)
    if error or context is None:
        return ToolResult(content=[TextContent(type="text", text=error or "Unknown error")])

    # Lightweight read sync: auto-pull if behind origin (never blocks)
    sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
    if sync_actions:
        log_debug(f"get_thread_entry read sync: {sync_actions}")

    _refresh_threads(context)
    load_error, entries = _load_thread_entries_graph_first(topic, context)
    if load_error:
        return ToolResult(content=[TextContent(type="text", text=load_error)])

    selected: ThreadEntry | None = None

    if index is not None:
        if index < 0 or index >= len(entries):
            return ToolResult(content=[TextContent(type="text", text=f"Error: index {index} out of range (entries={len(entries)}).")])
        selected = entries[index]

    if entry_id is not None:
        matching = next((entry for entry in entries if entry.entry_id == entry_id), None)
        if matching is None:
            return ToolResult(content=[TextContent(type="text", text=f"Error: entry_id '{entry_id}' not found in thread '{topic}'.")])
        if selected is not None and matching.index != selected.index:
            return ToolResult(content=[TextContent(type="text", text="Error: index and entry_id refer to different entries.")])
        selected = matching

    if selected is None:
        return ToolResult(content=[TextContent(type="text", text="Error: failed to resolve the requested entry.")])

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


@mcp.tool(name="watercooler_get_thread_entry_range")
def get_thread_entry_range(
    topic: str,
    start_index: int = 0,
    end_index: int | None = None,
    format: str = "json",
    code_path: str = "",
) -> ToolResult:
    """Return a contiguous range of entries (inclusive)."""

    fmt_error, resolved_format = _resolve_format(format, default="json")
    if fmt_error:
        return ToolResult(content=[TextContent(type="text", text=fmt_error)])

    if start_index < 0:
        return ToolResult(content=[TextContent(type="text", text="Error: start_index must be non-negative.")])
    if start_index > _MAX_OFFSET:
        return ToolResult(content=[TextContent(type="text", text=f"Error: start_index must not exceed {_MAX_OFFSET}.")])
    if end_index is not None and end_index < start_index:
        return ToolResult(content=[TextContent(type="text", text="Error: end_index must be greater than or equal to start_index.")])
    if end_index is not None and (end_index - start_index) > _MAX_LIMIT:
        return ToolResult(content=[TextContent(type="text", text=f"Error: requested range size must not exceed {_MAX_LIMIT} entries.")])

    error, context = _validate_thread_context(code_path)
    if error or context is None:
        return ToolResult(content=[TextContent(type="text", text=error or "Unknown error")])

    # Lightweight read sync: auto-pull if behind origin (never blocks)
    sync_ok, sync_actions = ensure_readable(context.threads_dir, context.code_root)
    if sync_actions:
        log_debug(f"get_thread_entry_range read sync: {sync_actions}")

    _refresh_threads(context)
    load_error, entries = _load_thread_entries_graph_first(topic, context)
    if load_error:
        return ToolResult(content=[TextContent(type="text", text=load_error)])

    total = len(entries)
    if start_index >= total and total > 0:
        return ToolResult(content=[TextContent(type="text", text=f"Error: start_index {start_index} out of range (entries={total}).")])

    last_index = total - 1 if total else -1
    effective_end = last_index if end_index is None else min(end_index, last_index)
    if effective_end < start_index and total:
        return ToolResult(content=[TextContent(type="text", text="Error: computed end index is before start index.")])

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


@mcp.tool(name="watercooler_say")
def say(
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
        from watercooler.metadata import thread_meta
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


@mcp.tool(name="watercooler_ack")
def ack(
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
        from watercooler.metadata import thread_meta
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


@mcp.tool(name="watercooler_handoff")
def handoff(
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
            from watercooler.metadata import thread_meta
            _, status, ball, _ = thread_meta(thread_path)

            return (
                f"✅ Ball handed off to: {ball}\n"
                f"Thread: {topic}\n"
                f"Status: {status}\n"
                + (f"Note: {note}" if note else "")
            )

    except Exception as e:
        return f"Error handing off '{topic}': {str(e)}"


@mcp.tool(name="watercooler_set_status")
def set_status(
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


@mcp.tool(name="watercooler_sync")
def force_sync(
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


@mcp.tool(name="watercooler_reindex")
def reindex(ctx: Context) -> str:
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


# ============================================================================
# Baseline Graph Tools (Free-tier, local LLM)
# ============================================================================

@mcp.tool(name="watercooler_baseline_graph_stats")
def baseline_graph_stats(
    ctx: Context,
    code_path: str = "",
) -> str:
    """Get statistics about threads for baseline graph.

    Returns thread counts, entry counts, and status breakdown.
    Useful for understanding the scope before building a baseline graph.

    Args:
        code_path: Path to code repository (for resolving threads dir).

    Returns:
        JSON with thread statistics.
    """
    try:
        from watercooler.baseline_graph import get_thread_stats

        error, context = _require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        stats = get_thread_stats(threads_dir)
        return json.dumps(stats, indent=2)

    except Exception as e:
        return f"Error getting baseline graph stats: {str(e)}"


@mcp.tool(name="watercooler_baseline_graph_build")
def baseline_graph_build(
    ctx: Context,
    code_path: str = "",
    output_dir: str = "",
    extractive_only: Optional[bool] = None,
    skip_closed: bool = False,
    generate_embeddings: Optional[bool] = None,
) -> str:
    """Build baseline graph from threads.

    Creates a lightweight knowledge graph using extractive summaries
    or local LLM. Output is JSONL format (nodes.jsonl, edges.jsonl).

    Default output is {threads_dir}/graph/baseline.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        output_dir: Output directory for graph files (optional).
        extractive_only: Use extractive summaries only (no LLM).
            Defaults to inverse of config generate_summaries from ~/.watercooler/config.toml.
        skip_closed: Skip closed threads. Default: False.
        generate_embeddings: Generate embedding vectors for entries.
            Defaults to config value from ~/.watercooler/config.toml.

    Returns:
        JSON manifest with export statistics.
    """
    try:
        from pathlib import Path
        from watercooler.baseline_graph import export_all_threads, SummarizerConfig
        from watercooler_mcp.config import get_watercooler_config

        error, context = _require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Get config defaults for summary/embedding generation
        wc_config = get_watercooler_config()
        graph_config = wc_config.mcp.graph

        # Use config values if not explicitly provided
        # extractive_only is inverse of generate_summaries
        do_extractive = extractive_only if extractive_only is not None else not graph_config.generate_summaries
        do_embeddings = generate_embeddings if generate_embeddings is not None else graph_config.generate_embeddings

        # Default output to threads_dir/graph/baseline
        if output_dir:
            out_path = Path(output_dir)
        else:
            out_path = threads_dir / "graph" / "baseline"

        config = SummarizerConfig(prefer_extractive=do_extractive)

        # Define the build operation
        def _do_build() -> dict:
            return export_all_threads(
                threads_dir,
                out_path,
                config,
                skip_closed=skip_closed,
                generate_embeddings=do_embeddings,
            )

        # Run with full parity protocol (preflight + commit + push)
        manifest = run_with_graph_sync(
            context,
            _do_build,
            "graph: build baseline",
        )

        return json.dumps(manifest, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error building baseline graph: {str(e)}"


@mcp.tool(name="watercooler_search")
def search_graph_tool(
    ctx: Context,
    code_path: str = "",
    query: str = "",
    semantic: bool = False,
    semantic_threshold: float = 0.5,
    start_time: str = "",
    end_time: str = "",
    thread_status: str = "",
    thread_topic: str = "",
    role: str = "",
    entry_type: str = "",
    agent: str = "",
    limit: int = 10,
    combine: str = "AND",
    include_threads: bool = True,
    include_entries: bool = True,
) -> str:
    """Unified search across threads and entries in the baseline graph.

    Supports keyword search, semantic search with embeddings, time-based
    filtering, and metadata filters. All filters can be combined with AND or OR logic.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        query: Search query (keyword or semantic depending on mode).
        semantic: If True, use semantic search with embedding cosine similarity.
            Requires embeddings to be generated. Falls back to keyword if unavailable.
        semantic_threshold: Minimum cosine similarity for semantic matches (0.0-1.0).
            Only used when semantic=True. Default: 0.5. Lower values return more results.
        start_time: Filter results after this ISO timestamp.
        end_time: Filter results before this ISO timestamp.
        thread_status: Filter threads by status (OPEN, CLOSED, etc.).
        thread_topic: Filter entries by specific thread topic.
        role: Filter entries by role (planner, implementer, etc.).
        entry_type: Filter entries by type (Note, Plan, Decision, etc.).
        agent: Filter entries by agent name (partial match).
        limit: Maximum results to return (default: 10, max: 100).
        combine: How to combine filters - "AND" or "OR" (default: AND).
        include_threads: Include thread nodes in results (default: True).
        include_entries: Include entry nodes in results (default: True).

    Returns:
        JSON with search results including matched nodes and metadata.
    """
    try:
        from watercooler.baseline_graph.search import SearchQuery, search_graph
        from watercooler.baseline_graph.reader import is_graph_available

        error, context = _require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Check if graph is available
        if not is_graph_available(threads_dir):
            return json.dumps({
                "error": "Graph not available",
                "message": "No baseline graph found. Run watercooler_baseline_graph_build first.",
                "results": [],
                "count": 0,
            })

        # Validate and constrain limit
        limit = max(1, min(limit, 100))

        # Build search query
        search_query = SearchQuery(
            query=query if query else None,
            semantic=semantic,
            semantic_threshold=max(0.0, min(1.0, semantic_threshold)),
            start_time=start_time if start_time else None,
            end_time=end_time if end_time else None,
            thread_status=thread_status if thread_status else None,
            thread_topic=thread_topic if thread_topic else None,
            role=role if role else None,
            entry_type=entry_type if entry_type else None,
            agent=agent if agent else None,
            limit=limit,
            combine=combine.upper() if combine.upper() in ("AND", "OR") else "AND",
            include_threads=include_threads,
            include_entries=include_entries,
        )

        # Execute search
        results = search_graph(threads_dir, search_query)

        # Format results for JSON output
        output = {
            "count": results.count,
            "total_scanned": results.total_scanned,
            "results": [],
        }

        for result in results.results:
            item = {
                "type": result.node_type,
                "id": result.node_id,
                "score": result.score,
                "matched_fields": result.matched_fields,
            }

            if result.thread:
                item["thread"] = {
                    "topic": result.thread.topic,
                    "title": result.thread.title,
                    "status": result.thread.status,
                    "ball": result.thread.ball,
                    "last_updated": result.thread.last_updated,
                    "entry_count": result.thread.entry_count,
                    "summary": result.thread.summary,
                }

            if result.entry:
                item["entry"] = {
                    "entry_id": result.entry.entry_id,
                    "thread_topic": result.entry.thread_topic,
                    "index": result.entry.index,
                    "agent": result.entry.agent,
                    "role": result.entry.role,
                    "entry_type": result.entry.entry_type,
                    "title": result.entry.title,
                    "timestamp": result.entry.timestamp,
                    "summary": result.entry.summary,
                }

            output["results"].append(item)

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error searching graph: {str(e)}"


@mcp.tool(name="watercooler_find_similar")
def find_similar_entries_tool(
    ctx: Context,
    entry_id: str,
    code_path: str = "",
    limit: int = 5,
    similarity_threshold: float = 0.5,
    use_embeddings: bool = True,
) -> str:
    """Find entries similar to a given entry using embedding similarity.

    Uses cosine similarity with embedding vectors when available.
    Falls back to same-thread heuristic if embeddings are not available.

    Args:
        entry_id: The entry ID to find similar entries for.
        code_path: Path to code repository (for resolving threads dir).
        limit: Maximum number of similar entries to return (default: 5).
        similarity_threshold: Minimum cosine similarity (0.0-1.0, default: 0.5).
        use_embeddings: Try to use embedding similarity (default: True).

    Returns:
        JSON with similar entries and their similarity scores.
    """
    try:
        from watercooler.baseline_graph.search import find_similar_entries
        from watercooler.baseline_graph.reader import is_graph_available

        error, context = _require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        if not is_graph_available(threads_dir):
            return json.dumps({
                "error": "Graph not available",
                "message": "No baseline graph found. Run watercooler_baseline_graph_build first.",
                "results": [],
            })

        # Validate parameters
        limit = max(1, min(limit, 50))
        similarity_threshold = max(0.0, min(1.0, similarity_threshold))

        # Find similar entries
        similar = find_similar_entries(
            threads_dir=threads_dir,
            entry_id=entry_id,
            limit=limit,
            use_embeddings=use_embeddings,
            similarity_threshold=similarity_threshold,
        )

        # Format results
        output = {
            "source_entry_id": entry_id,
            "count": len(similar),
            "method": "embedding_similarity" if use_embeddings else "same_thread_heuristic",
            "threshold": similarity_threshold,
            "results": [],
        }

        for entry in similar:
            output["results"].append({
                "entry_id": entry.entry_id,
                "thread_topic": entry.thread_topic,
                "title": entry.title,
                "agent": entry.agent,
                "role": entry.role,
                "timestamp": entry.timestamp,
                "summary": entry.summary,
            })

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error finding similar entries: {str(e)}"


@mcp.tool(name="watercooler_graph_health")
def graph_health_tool(
    ctx: Context,
    code_path: str = "",
) -> str:
    """Check graph synchronization health and report any issues.

    Reports the status of all threads in the graph:
    - Synced threads (graph matches markdown)
    - Stale threads (need sync)
    - Error threads (sync failed)
    - Pending threads (sync in progress)

    Use this to diagnose graph sync issues before running reconcile.

    Args:
        code_path: Path to code repository (for resolving threads dir).

    Returns:
        JSON health report with thread statuses and recommendations.
    """
    try:
        from watercooler.baseline_graph.sync import check_graph_health
        from watercooler.baseline_graph.reader import is_graph_available

        error, context = _require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Check if graph exists at all
        graph_available = is_graph_available(threads_dir)

        # Get health report
        health = check_graph_health(threads_dir)

        output = {
            "graph_available": graph_available,
            "healthy": health.healthy,
            "total_threads": health.total_threads,
            "synced_threads": health.synced_threads,
            "stale_threads": health.stale_threads,
            "error_threads": health.error_threads,
            "pending_threads": health.pending_threads,
            "error_details": health.error_details,
            "recommendations": [],
        }

        # Add recommendations
        if not graph_available:
            output["recommendations"].append(
                "Graph not available. Run watercooler_baseline_graph_build to create it."
            )
        if health.stale_threads:
            output["recommendations"].append(
                f"{len(health.stale_threads)} threads need sync. Run watercooler_reconcile_graph."
            )
        if health.error_threads:
            output["recommendations"].append(
                f"{health.error_threads} threads have sync errors. Check error_details and run reconcile."
            )

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error checking graph health: {str(e)}"


@mcp.tool(name="watercooler_reconcile_graph")
def reconcile_graph_tool(
    ctx: Context,
    code_path: str = "",
    topics: str = "",
    generate_summaries: Optional[bool] = None,
    generate_embeddings: Optional[bool] = None,
) -> str:
    """Reconcile graph with markdown files to fix sync issues.

    Rebuilds graph nodes and edges for threads that are stale, have errors,
    or are explicitly specified. This is the primary tool for ingesting
    legacy markdown-only threads into the graph representation.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        topics: Comma-separated list of topics to reconcile. If empty,
                reconciles all stale/error topics.
        generate_summaries: Whether to generate LLM summaries (slower).
            Defaults to config value from ~/.watercooler/config.toml.
        generate_embeddings: Whether to generate embedding vectors (slower).
            Defaults to config value from ~/.watercooler/config.toml.

    Returns:
        JSON report with reconciliation results per topic.
    """
    try:
        from watercooler.baseline_graph.sync import reconcile_graph
        from watercooler_mcp.config import get_watercooler_config

        error, context = _require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Get config defaults for summary/embedding generation
        wc_config = get_watercooler_config()
        graph_config = wc_config.mcp.graph

        # Use config values if not explicitly provided
        do_summaries = generate_summaries if generate_summaries is not None else graph_config.generate_summaries
        do_embeddings = generate_embeddings if generate_embeddings is not None else graph_config.generate_embeddings

        # Parse topics list
        topic_list = None
        if topics:
            topic_list = [t.strip() for t in topics.split(",") if t.strip()]

        # Define the reconcile operation
        def _do_reconcile() -> dict:
            return reconcile_graph(
                threads_dir=threads_dir,
                topics=topic_list,
                generate_summaries=do_summaries,
                generate_embeddings=do_embeddings,
            )

        # Run with full parity protocol (preflight + commit + push)
        results = run_with_graph_sync(
            context,
            _do_reconcile,
            f"graph: reconcile {topics or 'all'}",
        )

        # Build output
        successes = [t for t, ok in results.items() if ok]
        failures = [t for t, ok in results.items() if not ok]

        output = {
            "total_reconciled": len(results),
            "successes": len(successes),
            "failures": len(failures),
            "success_topics": successes,
            "failure_topics": failures,
        }

        return json.dumps(output, indent=2)

    except BranchPairingError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error reconciling graph: {str(e)}"


@mcp.tool(name="watercooler_access_stats")
def access_stats_tool(
    ctx: Context,
    code_path: str = "",
    node_type: str = "",
    limit: int = 10,
) -> str:
    """Get access statistics from the graph odometer.

    Returns the most frequently accessed threads and entries, useful for
    understanding usage patterns and identifying popular content.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        node_type: Filter by "thread" or "entry". Empty string returns both.
        limit: Maximum number of results to return (default 10).

    Returns:
        JSON with most accessed nodes including type, id, and access count.
    """
    try:
        from watercooler.baseline_graph.reader import get_most_accessed

        error, context = _require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Validate node_type
        filter_type = None
        if node_type:
            if node_type.lower() not in ("thread", "entry"):
                return f"Invalid node_type: {node_type}. Must be 'thread', 'entry', or empty."
            filter_type = node_type.lower()

        # Get most accessed
        results = get_most_accessed(
            threads_dir=threads_dir,
            node_type=filter_type,
            limit=max(1, min(limit, 100)),  # Clamp to 1-100
        )

        # Format output
        output = {
            "total_results": len(results),
            "filter": filter_type or "all",
            "stats": [
                {"type": t, "id": nid, "access_count": count}
                for t, nid, count in results
            ],
        }

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error getting access stats: {str(e)}"


# ============================================================================
# Branch Sync Enforcement Tools
# ============================================================================

@mcp.tool(name="watercooler_validate_branch_pairing")
def validate_branch_pairing_tool(
    ctx: Context,
    code_path: str = "",
    strict: bool = True,
) -> ToolResult:
    """Validate branch pairing between code and threads repos.

    Checks that the code repo and threads repo are on matching branches.
    This validation is automatically performed before all write operations,
    but this tool allows explicit checking.

    Args:
        code_path: Path to code repository directory. Defaults to current directory.
        strict: If True, return valid=False on any mismatch. If False, only return
                valid=False on critical errors.

    Returns:
        JSON result with validation status, branch names, mismatches, and warnings.
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.code_root or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve code and threads repo paths."
            )])

        result = validate_branch_pairing(
            code_repo=context.code_root,
            threads_repo=context.threads_dir,
            strict=strict,
        )

        # Convert to JSON-serializable format
        output = {
            "valid": result.valid,
            "code_branch": result.code_branch,
            "threads_branch": result.threads_branch,
            "mismatches": [
                {
                    "type": m.type,
                    "code": m.code,
                    "threads": m.threads,
                    "severity": m.severity,
                    "recovery": m.recovery,
                }
                for m in result.mismatches
            ],
            "warnings": result.warnings,
        }

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(output, indent=2)
        )])

    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error validating branch pairing: {str(e)}"
        )])


@mcp.tool(name="watercooler_sync_branch_state")
def sync_branch_state(
    ctx: Context,
    code_path: str = "",
    branch: Optional[str] = None,
    operation: str = "checkout",
    force: bool = False,
) -> ToolResult:
    """Synchronize branch state between code and threads repos.

    Performs branch lifecycle operations to keep repos in sync:
    - create: Create threads branch if code branch exists
    - delete: Delete threads branch if code branch deleted (with safeguards)
    - merge: Merge threads branch to main if code branch merged
    - checkout: Ensure both repos on same branch
    - recover: Recover from branch history divergence (e.g., after rebase/force-push)

    Note:
        This operational tool does **not** require ``agent_func`` or other
        provenance parameters. Unlike write operations (``watercooler_say``,
        ``watercooler_ack``, etc.), it only performs git lifecycle
        management, so pass just ``code_path``, ``branch``, ``operation``, and
        ``force``. FastMCP will automatically reject any unexpected parameters.

    Args:
        code_path: Path to code repository directory (default: current directory)
        branch: Specific branch to sync (default: current branch)
        operation: One of "create", "delete", "merge", "checkout", "recover" (default: "checkout")
        force: Skip safety checks (use with caution, default: False). For "recover",
               this controls whether to force-push after rebasing.

    Returns:
        Operation result with success/failure and any warnings.

    Example:
        >>> sync_branch_state(ctx, code_path=".", branch="feature-auth", operation="checkout")
        >>> sync_branch_state(ctx, code_path=".", branch="staging", operation="recover", force=True)
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.code_root or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve code and threads repo paths."
            )])

        code_repo = Repo(context.code_root, search_parent_directories=True)
        threads_repo = Repo(context.threads_dir, search_parent_directories=True)

        target_branch = branch or context.code_branch
        if not target_branch:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: No branch specified and unable to detect current branch."
            )])

        warnings: List[str] = []
        result_msg = ""

        if operation == "checkout":
            # Ensure both repos on same branch
            if target_branch not in [b.name for b in code_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in code repo."
                )])

            # Checkout code branch
            if code_repo.active_branch.name != target_branch:
                code_repo.git.checkout(target_branch)
                result_msg += f"Checked out '{target_branch}' in code repo.\n"

            # Checkout or create threads branch
            if target_branch in [b.name for b in threads_repo.heads]:
                if threads_repo.active_branch.name != target_branch:
                    threads_repo.git.checkout(target_branch)
                    result_msg += f"Checked out '{target_branch}' in threads repo.\n"
            else:
                # Create threads branch
                threads_repo.git.checkout('-b', target_branch)
                result_msg += f"Created and checked out '{target_branch}' in threads repo.\n"

            result_msg += f"✅ Both repos now on branch '{target_branch}'"

        elif operation == "create":
            # Create threads branch if code branch exists
            if target_branch not in [b.name for b in code_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in code repo."
                )])

            if target_branch in [b.name for b in threads_repo.heads]:
                result_msg = f"Branch '{target_branch}' already exists in threads repo."
            else:
                threads_repo.git.checkout('-b', target_branch)
                result_msg = f"✅ Created branch '{target_branch}' in threads repo."

        elif operation == "delete":
            # Delete threads branch (with safeguards)
            if target_branch not in [b.name for b in threads_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in threads repo."
                )])

            # Check for OPEN threads
            if not force:
                threads_repo.git.checkout(target_branch)
                threads_dir = context.threads_dir
                open_threads = []
                for thread_file in threads_dir.glob("*.md"):
                    try:
                        from watercooler.metadata import thread_meta, is_closed
                        title, status, ball, updated = thread_meta(thread_file)
                        if not is_closed(status):
                            open_threads.append(thread_file.stem)
                    except Exception:
                        pass

                if open_threads:
                    return ToolResult(content=[TextContent(
                        type="text",
                        text=(
                            f"Error: Cannot delete branch '{target_branch}' with OPEN threads:\n"
                            f"  {', '.join(open_threads)}\n"
                            f"Close threads first or use force=True to override."
                        )
                    )])

            # Switch to another branch before deleting
            if threads_repo.active_branch.name == target_branch:
                if "main" in [b.name for b in threads_repo.heads]:
                    threads_repo.git.checkout("main")
                else:
                    # Create main if it doesn't exist
                    threads_repo.git.checkout('-b', 'main')

            threads_repo.git.branch('-D', target_branch)
            result_msg = f"✅ Deleted branch '{target_branch}' from threads repo."

        elif operation == "merge":
            # Merge threads branch to main
            if target_branch not in [b.name for b in threads_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in threads repo."
                )])

            if "main" not in [b.name for b in threads_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text="Error: 'main' branch does not exist in threads repo."
                )])

            # Check for OPEN threads before merge
            if not force:
                threads_repo.git.checkout(target_branch)
                from watercooler.metadata import thread_meta, is_closed
                open_threads = []
                for thread_file in context.threads_dir.glob("*.md"):
                    try:
                        title, status, ball, updated = thread_meta(thread_file)
                        if not is_closed(status):
                            open_threads.append(thread_file.stem)
                    except Exception:
                        pass

                if open_threads:
                    warnings.append(f"Warning: {len(open_threads)} OPEN threads found on {target_branch}: {', '.join(open_threads)}")
                    warnings.append("Consider closing threads before merge or use force=True to proceed")

            # Detect squash merge in code repo
            squash_info = None
            if context.code_root:
                try:
                    from watercooler_mcp.git_sync import _detect_squash_merge
                    code_repo_obj = Repo(context.code_root, search_parent_directories=True)
                    is_squash, squash_sha = _detect_squash_merge(code_repo_obj, target_branch)
                    if is_squash:
                        squash_info = f"Detected squash merge in code repo"
                        if squash_sha:
                            squash_info += f" (squash commit: {squash_sha})"
                        warnings.append(squash_info)
                        warnings.append("Note: Original commits preserved in threads branch history")
                except Exception:
                    pass  # Ignore squash detection errors

            threads_repo.git.checkout("main")
            try:
                threads_repo.git.merge(target_branch, '--no-ff', '-m', f"Merge {target_branch} into main")

                # Check if code repo branch exists on remote - if yes, push threads merge too
                code_branch_on_remote = False
                if context.code_root:
                    try:
                        code_repo_obj = Repo(context.code_root, search_parent_directories=True)
                        remote_refs = [ref.name for ref in code_repo_obj.remote().refs]
                        code_branch_on_remote = f"origin/{target_branch}" in remote_refs
                    except Exception:
                        pass  # Ignore errors checking remote

                if code_branch_on_remote:
                    # Code branch is on remote, push threads merge too
                    threads_repo.git.push('origin', 'main')
                    result_msg = f"✅ Merged '{target_branch}' into 'main' in threads repo and pushed to remote."
                else:
                    # Code branch is local only, keep threads merge local
                    result_msg = f"✅ Merged '{target_branch}' into 'main' in threads repo (local only - code branch not on remote)."

                if warnings:
                    result_msg += "\n" + "\n".join(warnings)
            except GitCommandError as e:
                error_str = str(e)
                # Check if this is a merge conflict
                if "CONFLICT" in error_str or threads_repo.is_dirty():
                    # Detect conflicts in thread files
                    conflicted_files = []
                    for item in threads_repo.index.unmerged_blobs():
                        conflicted_files.append(item.path)
                    
                    if conflicted_files:
                        conflict_msg = (
                            f"Merge conflict detected in {len(conflicted_files)} file(s):\n"
                            f"  {', '.join(conflicted_files)}\n\n"
                            f"Append-only conflict resolution:\n"
                            f"  - Both entries will be preserved in chronological order\n"
                            f"  - Status/Ball conflicts: Higher severity status wins, last entry author gets ball\n"
                            f"  - Manual resolution may be required for complex conflicts\n\n"
                            f"To resolve:\n"
                            f"  1. Review conflicted files\n"
                            f"  2. Keep both entries in chronological order\n"
                            f"  3. Resolve header conflicts (status/ball) manually\n"
                            f"  4. Run: git add <files> && git commit"
                        )
                        warnings.append(conflict_msg)
                        return ToolResult(content=[TextContent(
                            type="text",
                            text=f"Merge conflict: {error_str}\n\n{conflict_msg}"
                        )])
                
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error merging branch: {error_str}"
                )])

        elif operation == "recover":
            # Recover from branch history divergence
            # This handles cases where threads branch has diverged from remote
            # (e.g., after force-push or rebase on code repo)

            if target_branch not in [b.name for b in threads_repo.heads]:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=f"Error: Branch '{target_branch}' does not exist in threads repo."
                )])

            # Use sync_branch_history to attempt automatic recovery
            sync_result = sync_branch_history(
                threads_repo_path=context.threads_dir,
                branch=target_branch,
                strategy="rebase",  # Default to rebase to preserve local work
                force=force,  # Only force-push if user explicitly requests
            )

            if sync_result.success:
                result_msg = f"✅ Recovery successful: {sync_result.details}"
                if sync_result.commits_preserved > 0:
                    result_msg += f"\n  - Preserved {sync_result.commits_preserved} local commits"
                if sync_result.needs_manual_resolution:
                    result_msg += "\n  ⚠️ Manual push required: run with force=True to push changes"
                    warnings.append("Rebase complete but push not performed. Use force=True to push.")
            else:
                if sync_result.needs_manual_resolution:
                    result_msg = (
                        f"❌ Recovery requires manual intervention:\n"
                        f"  {sync_result.details}\n\n"
                        f"Manual recovery options:\n"
                        f"  1. Resolve conflicts and commit\n"
                        f"  2. Use operation='recover' with force=True to discard local changes\n"
                        f"  3. Manually rebase: git rebase origin/{target_branch}"
                    )
                else:
                    result_msg = f"❌ Recovery failed: {sync_result.details}"

                if sync_result.commits_lost > 0:
                    warnings.append(f"Warning: {sync_result.commits_lost} local commits may be lost")

                return ToolResult(content=[TextContent(
                    type="text",
                    text=result_msg
                )])

        else:
            return ToolResult(content=[TextContent(
                type="text",
                text=f"Error: Unknown operation '{operation}'. Must be one of: create, delete, merge, checkout, recover"
            )])

        output = {
            "success": True,
            "operation": operation,
            "branch": target_branch,
            "message": result_msg,
            "warnings": warnings,
        }

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(output, indent=2)
        )])

    except InvalidGitRepositoryError as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error: Not a git repository: {str(e)}"
        )])
    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error syncing branch state: {str(e)}"
        )])


@mcp.tool(name="watercooler_audit_branch_pairing")
def audit_branch_pairing(
    ctx: Context,
    code_path: str = "",
    include_merged: bool = False,
) -> ToolResult:
    """Comprehensive audit of branch pairing across entire repo pair.

    Scans all branches in both repos and identifies:
    - Synced branches (exist in both with same name)
    - Code-only branches (exist in code but not threads)
    - Threads-only branches (exist in threads but not code)
    - Mismatched branches

    Args:
        code_path: Path to code repository directory
        include_merged: Include fully merged branches (0 commits ahead) in report

    Returns:
        JSON report with categorized branches and recommendations.
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.code_root or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve code and threads repo paths."
            )])

        code_repo = Repo(context.code_root, search_parent_directories=True)
        threads_repo = Repo(context.threads_dir, search_parent_directories=True)

        # Get all branches
        code_branches = {b.name for b in code_repo.heads}
        threads_branches = {b.name for b in threads_repo.heads}

        # Categorize branches
        synced = []
        code_only = []
        threads_only = []
        recommendations = []

        # Find synced branches
        for branch in code_branches & threads_branches:
            try:
                code_sha = code_repo.heads[branch].commit.hexsha[:7]
                threads_sha = threads_repo.heads[branch].commit.hexsha[:7]
                synced.append({
                    "name": branch,
                    "code_sha": code_sha,
                    "threads_sha": threads_sha,
                })
            except Exception:
                synced.append({"name": branch, "code_sha": "unknown", "threads_sha": "unknown"})

        # Find code-only branches
        for branch in code_branches - threads_branches:
            try:
                branch_obj = code_repo.heads[branch]
                commits_ahead = len(list(code_repo.iter_commits(f"main..{branch}"))) if "main" in code_branches else 0
                code_only.append({
                    "name": branch,
                    "commits_ahead": commits_ahead,
                    "action": "create_threads_branch" if commits_ahead > 0 or include_merged else "delete_if_merged",
                })
                if commits_ahead == 0 and not include_merged:
                    recommendations.append(f"Code branch '{branch}' is fully merged - consider deleting")
                else:
                    recommendations.append(f"Create threads branch '{branch}' to match code branch")
            except Exception:
                code_only.append({"name": branch, "commits_ahead": 0, "action": "unknown"})

        # Find threads-only branches
        for branch in threads_branches - code_branches:
            try:
                branch_obj = threads_repo.heads[branch]
                commits_ahead = len(list(threads_repo.iter_commits(f"main..{branch}"))) if "main" in threads_branches else 0
                threads_only.append({
                    "name": branch,
                    "commits_ahead": commits_ahead,
                    "action": "delete_or_merge" if commits_ahead == 0 or include_merged else "create_code_branch",
                })
                if commits_ahead == 0:
                    recommendations.append(f"Threads branch '{branch}' is fully merged - safe to delete")
                else:
                    recommendations.append(f"Code branch '{branch}' was deleted - merge or delete threads branch")
            except Exception:
                threads_only.append({"name": branch, "commits_ahead": 0, "action": "unknown"})

        output = {
            "synced_branches": synced,
            "code_only_branches": code_only,
            "threads_only_branches": threads_only,
            "mismatched_branches": [],  # Future: detect name mismatches
            "recommendations": recommendations,
        }

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(output, indent=2)
        )])

    except InvalidGitRepositoryError as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error: Not a git repository: {str(e)}"
        )])
    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error auditing branch pairing: {str(e)}"
        )])


@mcp.tool(name="watercooler_recover_branch_state")
def recover_branch_state(
    ctx: Context,
    code_path: str = "",
    auto_fix: bool = False,
    diagnose_only: bool = False,
) -> ToolResult:
    """Recover from branch state inconsistencies.

    Diagnoses and optionally fixes branch pairing issues:
    - Branch name mismatches
    - Orphaned threads branches (code branch deleted)
    - Missing threads branches (code branch exists)
    - Git state issues (rebase conflicts, detached HEAD, etc.)

    Args:
        code_path: Path to code repository directory
        auto_fix: Automatically apply safe fixes
        diagnose_only: Only report issues, don't fix

    Returns:
        Diagnostic report with detected issues and recommended fixes.
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.code_root or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve code and threads repo paths."
            )])

        issues = []
        fixes_applied = []

        # Validate branch pairing
        validation_result = validate_branch_pairing(
            code_repo=context.code_root,
            threads_repo=context.threads_dir,
            strict=False,
        )

        if not validation_result.valid:
            for mismatch in validation_result.mismatches:
                issues.append({
                    "type": mismatch.type,
                    "severity": mismatch.severity,
                    "description": f"Code: {mismatch.code}, Threads: {mismatch.threads}",
                    "recovery": mismatch.recovery,
                })

                # Auto-fix if requested and safe
                if auto_fix and not diagnose_only:
                    if mismatch.type == "branch_name_mismatch" and mismatch.code:
                        try:
                            threads_repo = Repo(context.threads_dir, search_parent_directories=True)
                            if mismatch.code in [b.name for b in threads_repo.heads]:
                                threads_repo.git.checkout(mismatch.code)
                                fixes_applied.append(f"Checked out '{mismatch.code}' in threads repo")
                            else:
                                threads_repo.git.checkout('-b', mismatch.code)
                                fixes_applied.append(f"Created branch '{mismatch.code}' in threads repo")
                        except Exception as e:
                            issues.append({
                                "type": "auto_fix_failed",
                                "severity": "warning",
                                "description": f"Failed to auto-fix {mismatch.type}: {str(e)}",
                                "recovery": "Manual intervention required",
                            })

        # Check for git state issues
        try:
            code_repo = Repo(context.code_root, search_parent_directories=True)
            if code_repo.head.is_detached:
                issues.append({
                    "type": "code_repo_detached_head",
                    "severity": "warning",
                    "description": "Code repo is in detached HEAD state",
                    "recovery": "Checkout a branch: git checkout <branch>",
                })
        except Exception:
            pass

        try:
            threads_repo = Repo(context.threads_dir, search_parent_directories=True)
            if threads_repo.head.is_detached:
                issues.append({
                    "type": "threads_repo_detached_head",
                    "severity": "warning",
                    "description": "Threads repo is in detached HEAD state",
                    "recovery": "Checkout a branch: git checkout <branch>",
                })
        except Exception:
            pass

        # Check for rebase/merge conflicts
        try:
            code_repo = Repo(context.code_root, search_parent_directories=True)
            if (code_repo.git_dir / "rebase-merge").exists() or (code_repo.git_dir / "rebase-apply").exists():
                issues.append({
                    "type": "code_repo_rebase_in_progress",
                    "severity": "error",
                    "description": "Code repo has in-progress rebase",
                    "recovery": "Run: git rebase --abort (or --continue)",
                })
        except Exception:
            pass

        try:
            threads_repo = Repo(context.threads_dir, search_parent_directories=True)
            if (threads_repo.git_dir / "rebase-merge").exists() or (threads_repo.git_dir / "rebase-apply").exists():
                issues.append({
                    "type": "threads_repo_rebase_in_progress",
                    "severity": "error",
                    "description": "Threads repo has in-progress rebase",
                    "recovery": "Run: git rebase --abort (or --continue)",
                })
        except Exception:
            pass

        output = {
            "diagnosis_complete": True,
            "issues_found": len(issues),
            "issues": issues,
            "fixes_applied": fixes_applied,
            "warnings": validation_result.warnings,
        }

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(output, indent=2)
        )])

    except InvalidGitRepositoryError as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error: Not a git repository: {str(e)}"
        )])
    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=f"Error recovering branch state: {str(e)}"
        )])


@mcp.tool(name="watercooler_query_memory")
async def query_memory(
    query: str,
    ctx: Context,
    code_path: str = "",
    limit: int = 10,
    topic: Optional[str] = None,
) -> ToolResult:
    """Query thread history using Graphiti temporal graph memory.

    Searches indexed watercooler threads using semantic search and graph traversal.
    Returns relevant facts, entities, and relationships from thread history.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        query: Search query (e.g., "What authentication method was implemented?")
        code_path: Path to code repository (for resolving threads directory)
        limit: Maximum results to return (default: 10, range: 1-50)
        topic: Optional thread topic to restrict search (default: search all threads)

    Returns:
        JSON response with search results containing:
        - results: List of matching facts/entities with scores
        - query: Original query text
        - result_count: Number of results returned
        - message: Status/error message

    Example:
        query_memory(
            query="Who implemented OAuth2?",
            code_path=".",
            limit=5
        )

    Response Format:
        {
          "query": "Who implemented OAuth2?",
          "result_count": 2,
          "results": [
            {
              "content": "Claude implemented OAuth2 with JWT tokens",
              "score": 0.89,
              "metadata": {
                "thread_id": "auth-feature",
                "entry_id": "01ABC...",
                "valid_at": "2025-10-01T10:00:00Z"
              }
            }
          ],
          "message": "Found 2 results"
        }
    """
    try:
        # Import memory module (lazy-load)
        try:
            from . import memory as mem
        except ImportError as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Memory module unavailable",
                        "message": f"Install with: pip install watercooler-cloud[memory]. Details: {e}",
                        "query": query,
                        "result_count": 0,
                        "results": [],
                    },
                    indent=2,
                )
            )])

        # Load configuration
        config = mem.load_graphiti_config()
        if config is None:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Graphiti not enabled",
                        "message": (
                            "Set WATERCOOLER_GRAPHITI_ENABLED=1 and configure "
                            "OPENAI_API_KEY to enable memory queries."
                        ),
                        "query": query,
                        "result_count": 0,
                        "results": [],
                    },
                    indent=2,
                )
            )])

        # Validate limit parameter
        if limit < 1:
            limit = 10
        if limit > 50:
            limit = 50

        # Get backend instance
        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            if isinstance(backend, dict):
                # Structured error with details
                error_type = backend.get("error", "unknown")
                details = backend.get("details", "No details available")
                package_path = backend.get("package_path", "unknown")
                python_version = backend.get("python_version", "unknown")

                # Determine fix based on error type
                if "uv/archive" in package_path or "cache" in package_path:
                    fix_msg = (
                        f"Python {python_version} is loading from UV cache. "
                        "Fix: Ensure MCP server uses the correct Python environment, "
                        f"or install in Python {python_version} with: "
                        "uv pip install --reinstall --no-cache -e \".[memory,mcp]\""
                    )
                else:
                    fix_msg = "Check MCP server configuration and Python environment"

                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "error": f"Backend {error_type}",
                        "message": details,
                        "python_version": python_version,
                        "package_path": package_path,
                        "fix": fix_msg,
                        "query": query,
                        "result_count": 0,
                        "results": [],
                    }, indent=2)
                )])
            else:
                # Fallback for None (shouldn't happen with new code, but kept for safety)
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": "Backend initialization failed",
                            "message": "Check logs for Graphiti backend errors",
                            "query": query,
                            "result_count": 0,
                            "results": [],
                        },
                        indent=2,
                    )
                )])

        # Resolve threads directory (for context logging, not directly used in query)
        error, context = _require_context(code_path)
        if error:
            log_warning(f"MEMORY: Could not resolve context: {error}")
            # Continue anyway - query may work with existing index

        # Execute query
        log_action("memory.query", query=query, limit=limit, topic=topic)

        try:
            results, communities = await mem.query_memory(backend, query, limit, topic=topic)

            # Format response
            response = {
                "query": query,
                "result_count": len(results),
                "results": [
                    {
                        "content": r.get("content", ""),
                        "score": r.get("score", 0.0),
                        "metadata": r.get("metadata", {}),
                    }
                    for r in results
                ],
                "communities": communities,
                "message": f"Found {len(results)} results and {len(communities)} communities",
            }

            if topic:
                response["filtered_by_topic"] = topic

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: Query failed: {e}")
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Query execution failed",
                        "message": str(e),
                        "query": query,
                        "result_count": 0,
                        "results": [],
                    },
                    indent=2,
                )
            )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in query_memory: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(
                {
                    "error": "Internal error",
                    "message": str(e),
                    "query": query,
                    "result_count": 0,
                    "results": [],
                },
                indent=2,
            )
        )])




@mcp.tool(name="watercooler_search_nodes")
async def search_nodes(
    query: str,
    ctx: Context,
    code_path: str = "",
    group_ids: Optional[List[str]] = None,
    max_nodes: int = 10,
    entity_types: Optional[List[str]] = None,
) -> ToolResult:
    """Search for entity nodes using hybrid semantic search.

    Searches indexed watercooler threads for entity nodes (people, concepts, etc.)
    using Graphiti's hybrid search combining semantic embeddings, keyword search,
    and graph traversal.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        query: Search query (e.g., "authentication implementation")
        ctx: MCP context
        code_path: Path to code repository (for resolving threads directory)
        group_ids: Optional list of thread topics to filter by
        max_nodes: Maximum nodes to return (default: 10, max: 50)
        entity_types: Optional list of entity type names to filter

    Returns:
        JSON response with search results containing:
        - query: Original query text
        - result_count: Number of nodes returned
        - results: List of nodes with uuid, name, labels, summary, etc.
        - message: Status message

    Example:
        search_nodes(
            query="OAuth2 implementation",
            code_path=".",
            max_nodes=5
        )

    Response Format:
        {
          "query": "OAuth2 implementation",
          "result_count": 3,
          "results": [
            {
              "uuid": "01ABC...",
              "name": "OAuth2Provider",
              "labels": ["Class", "Authentication"],
              "summary": "OAuth2 provider implementation...",
              "created_at": "2025-10-01T10:00:00Z",
              "group_id": "auth-feature"
            }
          ],
          "message": "Found 3 nodes"
        }
    """
    try:
        from . import memory as mem
        
        # Validate query parameter
        if not query or not query.strip():
            return mem.create_error_response(
                "Invalid query",
                "Query parameter is required and must be non-empty",
                "search_nodes",
                query=query,
                result_count=0,
                results=[],
            )
        
        # Validate max_nodes parameter
        if max_nodes < 1:
            max_nodes = 10
        if max_nodes > 50:
            max_nodes = 50
        
        # Common validation (replaces ~100 lines of duplicated code)
        backend, error = mem.validate_memory_prerequisites("search_nodes")
        if error:
            # Add query/result fields to error response
            error_dict = json.loads(error.content[0].text)
            error_dict.update({
                "query": query,
                "result_count": 0,
                "results": [],
            })
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(error_dict, indent=2)
            )])
        
        # Execute search
        import asyncio
        from .observability import log_action, log_error
        
        log_action("memory.search_nodes", query=query, max_nodes=max_nodes, group_ids=group_ids)
        
        try:
            results = await asyncio.to_thread(
                backend.search_nodes,
                query=query,
                group_ids=group_ids,
                max_nodes=max_nodes,
                entity_types=entity_types,
            )
            
            # Format response
            response = {
                "query": query,
                "result_count": len(results),
                "results": results,
                "message": f"Found {len(results)} node(s)",
            }
            
            if group_ids:
                response["filtered_by_topics"] = group_ids
            
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])
            
        except Exception as e:
            log_error(f"MEMORY: Node search failed: {e}")
            return mem.create_error_response(
                "Search execution failed",
                str(e),
                "search_nodes",
                query=query,
                result_count=0,
                results=[],
            )
    
    except Exception as e:
        from .observability import log_error
        from . import memory as mem
        
        log_error(f"MEMORY: Unexpected error in search_nodes: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "search_nodes",
            query=query,
            result_count=0,
            results=[],
        )


@mcp.tool(name="watercooler_get_entity_edge")
async def get_entity_edge(
    uuid: str,
    ctx: Context,
    code_path: str = "",
    group_id: Optional[str] = None,
) -> ToolResult:
    """Get a specific entity edge (relationship) by UUID.

    Retrieves detailed information about a specific relationship between entities
    in the Graphiti knowledge graph.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        uuid: Edge UUID to retrieve
        ctx: MCP context
        code_path: Path to code repository (for resolving threads directory)
        group_id: Thread topic (database name) where edge is stored.
                 Required for multi-database setups. Searches default database if not provided.

    Returns:
        JSON response with edge details containing:
        - uuid: Edge UUID
        - fact: Description of the relationship
        - source_node_uuid: UUID of source entity
        - target_node_uuid: UUID of target entity
        - valid_at: When relationship became valid
        - invalid_at: When relationship became invalid (if applicable)
        - created_at: When edge was created
        - group_id: Thread topic this edge belongs to
        - message: Status message

    Example:
        get_entity_edge(
            uuid="01ABC123...",
            code_path="."
        )

    Response Format:
        {
          "uuid": "01ABC123...",
          "fact": "Claude implemented OAuth2 authentication",
          "source_node_uuid": "01DEF456...",
          "target_node_uuid": "01GHI789...",
          "valid_at": "2025-10-01T10:00:00Z",
          "created_at": "2025-10-01T10:00:00Z",
          "group_id": "auth-feature",
          "message": "Retrieved edge 01ABC123..."
        }
    """
    try:
        from . import memory as mem
        
        # Validate UUID parameter (tool-specific validation)
        if not uuid or not uuid.strip():
            return mem.create_error_response(
                "Invalid UUID",
                "UUID parameter is required and must be non-empty",
                "get_entity_edge"
            )
        
        # Sanitize UUID (limit length and characters)
        if len(uuid) > 100:
            return mem.create_error_response(
                "Invalid UUID",
                "UUID too long (max 100 characters)",
                "get_entity_edge",
                uuid=uuid[:50] + "..."
            )
        
        # Check for valid characters (alphanumeric, hyphen, underscore)
        if not all(c.isalnum() or c in '-_' for c in uuid):
            return mem.create_error_response(
                "Invalid UUID",
                "UUID contains invalid characters (only alphanumeric, hyphen, underscore allowed)",
                "get_entity_edge"
            )
        
        # Common validation (replaces ~100 lines of duplicated code)
        backend, error = mem.validate_memory_prerequisites("get_entity_edge")
        if error:
            return error
        
        # Execute query
        import asyncio
        from .observability import log_action, log_error
        
        log_action("memory.get_entity_edge", uuid=uuid, group_id=group_id)

        try:
            edge = await asyncio.to_thread(backend.get_entity_edge, uuid, group_id=group_id)
            
            # Handle None return (edge not found)
            if edge is None:
                return mem.create_error_response(
                    "Edge not found",
                    f"No edge found with UUID {uuid}",
                    "get_entity_edge",
                    uuid=uuid
                )
            
            # Format response
            response = {
                **edge,
                "message": f"Retrieved edge {uuid}",
            }
            
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])
            
        except Exception as e:
            log_error(f"MEMORY: Get entity edge failed: {e}")
            return mem.create_error_response(
                "Edge retrieval failed",
                str(e),
                "get_entity_edge",
                uuid=uuid
            )
    
    except Exception as e:
        from .observability import log_error
        from . import memory as mem
        
        log_error(f"MEMORY: Unexpected error in get_entity_edge: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "get_entity_edge"
        )


@mcp.tool(name="watercooler_search_memory_facts")
async def search_memory_facts(
    query: str,
    ctx: Context,
    code_path: str = "",
    group_ids: Optional[List[str]] = None,
    max_facts: int = 10,
    center_node_uuid: Optional[str] = None,
) -> ToolResult:
    """Search for facts (edges/relationships) with optional center-node traversal.

    Searches indexed watercooler threads for facts (relationships between entities)
    using Graphiti's hybrid search. Optionally centers the search around a specific
    entity node.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        query: Search query (e.g., "authentication decisions")
        ctx: MCP context
        code_path: Path to code repository (for resolving threads directory)
        group_ids: Optional list of thread topics to filter by
        max_facts: Maximum facts to return (default: 10, max: 50)
        center_node_uuid: Optional node UUID to center search around

    Returns:
        JSON response with search results containing:
        - query: Original query text
        - result_count: Number of facts returned
        - results: List of facts with uuid, fact text, source/target nodes, scores
        - message: Status message

    Example:
        search_memory_facts(
            query="OAuth2 implementation decisions",
            code_path=".",
            max_facts=5,
            center_node_uuid="01ABC..."
        )

    Response Format:
        {
          "query": "OAuth2 implementation decisions",
          "result_count": 2,
          "results": [
            {
              "uuid": "01ABC...",
              "fact": "Claude implemented OAuth2 with JWT tokens",
              "source_node_uuid": "01DEF...",
              "target_node_uuid": "01GHI...",
              "score": 0.89,
              "valid_at": "2025-10-01T10:00:00Z",
              "group_id": "auth-feature"
            }
          ],
          "message": "Found 2 fact(s)"
        }
    """
    try:
        from . import memory as mem
        
        # Validate query parameter
        if not query or not query.strip():
            return mem.create_error_response(
                "Invalid query",
                "Query parameter is required and must be non-empty",
                "search_memory_facts",
                query=query,
                result_count=0,
                results=[],
            )
        
        # Validate max_facts parameter
        if max_facts < 1:
            max_facts = 10
        if max_facts > 50:
            max_facts = 50
        
        # Common validation (replaces ~100 lines of duplicated code)
        backend, error = mem.validate_memory_prerequisites("search_memory_facts")
        if error:
            # Add query/result fields to error response
            error_dict = json.loads(error.content[0].text)
            error_dict.update({
                "query": query,
                "result_count": 0,
                "results": [],
            })
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(error_dict, indent=2)
            )])
        
        # Execute search
        import asyncio
        from .observability import log_action, log_error
        
        log_action(
            "memory.search_memory_facts",
            query=query,
            max_facts=max_facts,
            group_ids=group_ids,
            center_node_uuid=center_node_uuid,
        )
        
        try:
            results = await asyncio.to_thread(
                backend.search_memory_facts,
                query=query,
                group_ids=group_ids,
                max_facts=max_facts,
                center_node_uuid=center_node_uuid,
            )
            
            # Format response
            response = {
                "query": query,
                "result_count": len(results),
                "results": results,
                "message": f"Found {len(results)} fact(s)",
            }
            
            if group_ids:
                response["filtered_by_topics"] = group_ids
            if center_node_uuid:
                response["centered_on_node"] = center_node_uuid
            
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])
            
        except Exception as e:
            log_error(f"MEMORY: Fact search failed: {e}")
            return mem.create_error_response(
                "Search execution failed",
                str(e),
                "search_memory_facts",
                query=query,
                result_count=0,
                results=[],
            )
    
    except Exception as e:
        from .observability import log_error
        from . import memory as mem
        
        log_error(f"MEMORY: Unexpected error in search_memory_facts: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "search_memory_facts",
            query=query,
            result_count=0,
            results=[],
        )


@mcp.tool(name="watercooler_get_episodes")
async def get_episodes(
    query: str,
    ctx: Context,
    code_path: str = "",
    group_ids: Optional[List[str]] = None,
    max_episodes: int = 10,
) -> ToolResult:
    """Search for episodes from Graphiti memory using semantic search.

    Performs semantic search on episodic content from indexed watercooler threads.
    Note: Graphiti doesn't support listing all episodes; this tool requires a query
    string to perform semantic search.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        query: Search query string (required, must be non-empty)
        ctx: MCP context
        code_path: Path to code repository (for resolving threads directory)
        group_ids: Optional list of thread topics to filter by
        max_episodes: Maximum episodes to return (default: 10, max: 50)

    Returns:
        JSON response with episodes containing:
        - result_count: Number of episodes returned
        - results: List of episodes with uuid, name, content, timestamps
        - message: Status message

    Example:
        get_episodes(
            query="authentication implementation",
            code_path=".",
            group_ids=["auth-feature", "api-design"],
            max_episodes=5
        )

    Response Format:
        {
          "result_count": 2,
          "results": [
            {
              "uuid": "01ABC...",
              "name": "Entry 01ABC...",
              "content": "Implemented OAuth2 authentication...",
              "created_at": "2025-10-01T10:00:00Z",
              "source": "thread_entry",
              "source_description": "Watercooler thread entry",
              "group_id": "auth-feature",
              "valid_at": "2025-10-01T10:00:00Z"
            }
          ],
          "message": "Found 2 episode(s)",
          "filtered_by_topics": ["auth-feature", "api-design"]
        }
    """
    try:
        from . import memory as mem
        
        # Validate query parameter (tool-specific)
        if not query or not query.strip():
            return mem.create_error_response(
                "Invalid query",
                "Query parameter is required and must be non-empty for semantic search",
                "get_episodes",
                result_count=0,
                results=[],
            )
        
        # Validate max_episodes parameter
        if max_episodes < 1:
            max_episodes = 10
        if max_episodes > 50:
            max_episodes = 50
        
        # Common validation (replaces ~100 lines of duplicated code)
        backend, error = mem.validate_memory_prerequisites("get_episodes")
        if error:
            # Add result fields to error response
            error_dict = json.loads(error.content[0].text)
            error_dict.update({
                "result_count": 0,
                "results": [],
            })
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(error_dict, indent=2)
            )])
        
        # Execute query
        import asyncio
        from .observability import log_action, log_error
        
        log_action("memory.get_episodes", query=query, max_episodes=max_episodes, group_ids=group_ids)
        
        try:
            results = await asyncio.to_thread(
                backend.get_episodes,
                query=query,
                group_ids=group_ids,
                max_episodes=max_episodes,
            )
            
            # Format response
            response = {
                "result_count": len(results),
                "results": results,
                "message": f"Found {len(results)} episode(s)",
            }
            
            if group_ids:
                response["filtered_by_topics"] = group_ids
            
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])
            
        except Exception as e:
            log_error(f"MEMORY: Get episodes failed: {e}")
            return mem.create_error_response(
                "Episodes retrieval failed",
                str(e),
                "get_episodes",
                result_count=0,
                results=[],
            )
    
    except Exception as e:
        from .observability import log_error
        from . import memory as mem
        
        log_error(f"MEMORY: Unexpected error in get_episodes: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "get_episodes",
            result_count=0,
            results=[],
        )


@mcp.tool(name="watercooler_diagnose_memory")
def diagnose_memory(ctx: Context) -> ToolResult:
    """Diagnose Graphiti memory backend installation and configuration.

    Returns diagnostic information about package paths, imports, and configuration.
    Useful for debugging backend initialization issues.

    Returns:
        JSON with diagnostic information including:
        - Python version
        - watercooler_memory package path
        - GraphitiBackend import status
        - Configuration status
        - Backend initialization status

    Example:
        diagnose_memory()
    """
    try:
        # Import memory module (lazy-load)
        try:
            from . import memory as mem
        except ImportError as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Memory module unavailable",
                        "message": f"Install with: pip install watercooler-cloud[memory]. Details: {e}",
                    },
                    indent=2,
                )
            )])

        import sys
        diagnostics = {
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "python_executable": sys.executable,
        }

        # Check watercooler_memory import and path
        try:
            import watercooler_memory
            diagnostics["watercooler_memory_path"] = watercooler_memory.__file__
            diagnostics["watercooler_memory_version"] = getattr(
                watercooler_memory, "__version__", "unknown"
            )
        except ImportError as e:
            diagnostics["watercooler_memory_import"] = f"✗ Failed: {e}"

        # Check GraphitiBackend import
        try:
            from watercooler_memory.backends import GraphitiBackend
            diagnostics["graphiti_backend_import"] = "✓ Success"
            diagnostics["graphiti_backend_in_all"] = "GraphitiBackend" in getattr(
                __import__("watercooler_memory.backends"), "__all__", []
            )
        except ImportError as e:
            diagnostics["graphiti_backend_import"] = f"✗ Failed: {e}"

        # Check config
        config = mem.load_graphiti_config()
        diagnostics["graphiti_enabled"] = config is not None
        if config:
            diagnostics["openai_key_set"] = bool(config.openai_api_key)
        else:
            diagnostics["config_issue"] = "WATERCOOLER_GRAPHITI_ENABLED != '1' or OPENAI_API_KEY not set"

        # Check backend initialization
        if config:
            backend = mem.get_graphiti_backend(config)
            if isinstance(backend, dict):
                diagnostics["backend_init"] = f"✗ Failed: {backend.get('error', 'unknown')}"
                diagnostics["backend_error_details"] = backend
            elif backend is None:
                diagnostics["backend_init"] = "✗ Failed: Returned None"
            else:
                diagnostics["backend_init"] = "✓ Success"

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(diagnostics, indent=2)
        )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in diagnose_memory: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(
                {
                    "error": "Diagnostic failed",
                    "message": str(e),
                },
                indent=2,
            )
        )])


# ============================================================================
# Server Entry Point
# ============================================================================

def _check_first_run() -> None:
    """Check if this is first run and suggest config initialization."""
    try:
        from watercooler.config_loader import get_config_paths

        paths = get_config_paths()
        user_config = paths.get("user_config")
        project_config = paths.get("project_config")

        # Check if any config file exists
        has_config = (
            (user_config and user_config.exists()) or
            (project_config and project_config.exists())
        )

        if not has_config:
            _add_startup_warning(
                "No config file found. Create one to customize settings:\n"
                "  uvx watercooler-cloud config init --user\n"
                "Using built-in defaults for now."
            )
    except Exception:
        # Don't let config check errors break server startup
        pass


def _ensure_ollama_running():
    """Start Ollama if graph features are enabled and it's not running.

    This reduces friction for new users - if they have Ollama installed
    and graph features enabled, we'll start it automatically.
    """
    import subprocess
    import urllib.request
    import urllib.error

    try:
        from .config import get_watercooler_config
        config = get_watercooler_config()
        graph_config = config.mcp.graph

        # Only auto-start if graph features are enabled
        if not (graph_config.generate_summaries or graph_config.generate_embeddings):
            return

        # Check if Ollama is already responding
        try:
            req = urllib.request.Request(
                "http://localhost:11434/v1/models",
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return  # Already running
        except (urllib.error.URLError, TimeoutError, OSError):
            pass  # Not running, try to start

        # Try to start Ollama
        log_debug("Starting Ollama for graph features...")

        # Method 1: Try systemctl (Linux with systemd)
        try:
            result = subprocess.run(
                ["systemctl", "start", "ollama"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                # Wait for it to be ready
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        req = urllib.request.Request("http://localhost:11434/v1/models")
                        with urllib.request.urlopen(req, timeout=2):
                            log_debug("Ollama started successfully via systemctl.")
                            return
                    except (urllib.error.URLError, TimeoutError, OSError):
                        continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Method 2: Try ollama serve directly (macOS, or Linux without systemd)
        try:
            # Check if ollama command exists
            result = subprocess.run(
                ["which", "ollama"],
                capture_output=True,
                timeout=2
            )
            if result.returncode == 0:
                # Start ollama serve in background
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                # Wait for it to be ready
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        req = urllib.request.Request("http://localhost:11434/v1/models")
                        with urllib.request.urlopen(req, timeout=2):
                            log_debug("Ollama started successfully via ollama serve.")
                            return
                    except (urllib.error.URLError, TimeoutError, OSError):
                        continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # If we get here, couldn't start Ollama - give platform-aware guidance
        import platform
        system = platform.system().lower()

        if system == "windows":
            install_cmd = "winget install Ollama.Ollama"
            alt_msg = "Or download from: https://ollama.com/download/windows\n"
        elif system == "darwin":
            install_cmd = "brew install ollama"
            alt_msg = "Or: curl -fsSL https://ollama.com/install.sh | sh\n"
        else:  # Linux
            install_cmd = "curl -fsSL https://ollama.com/install.sh | sh"
            alt_msg = ""

        msg = (
            "Ollama not available - graph features (summaries/embeddings) disabled.\n"
            "To enable AI-powered summaries and semantic search:\n"
            f"  {install_cmd}\n"
        )
        if alt_msg:
            msg += f"  {alt_msg}"
        msg += (
            "Then pull models:\n"
            "  ollama pull llama3.2:3b\n"
            "  ollama pull nomic-embed-text\n"
            "Restart your IDE to reload the MCP server."
        )
        _add_startup_warning(msg)
    except Exception as e:
        # Don't let auto-start errors break server startup
        log_debug(f"Ollama auto-start check failed: {e}")


def main():
    """Entry point for watercooler-mcp command."""
    # Check for first-run and suggest config initialization
    _check_first_run()

    # Auto-start Ollama if graph features are enabled
    _ensure_ollama_running()

    # Get transport configuration from unified config system
    from .config import get_mcp_transport_config

    transport_config = get_mcp_transport_config()
    transport = transport_config["transport"]

    if transport == "http":
        host = transport_config["host"]
        port = transport_config["port"]

        print(f"Starting Watercooler MCP Server on http://{host}:{port}", file=sys.stderr)
        print(f"Health check: http://{host}:{port}/health", file=sys.stderr)

        mcp.run(transport="http", host=host, port=port)
    else:
        # stdio transport (default)
        mcp.run()


if __name__ == "__main__":
    main()
