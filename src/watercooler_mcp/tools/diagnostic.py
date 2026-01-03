"""Diagnostic tools for watercooler MCP server.

Tools:
- watercooler_health: Server health check
- watercooler_whoami: Agent identity
- watercooler_reconcile_parity: Branch parity reconciliation
"""

import os
import sys
import json
import subprocess
from pathlib import Path

from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from git import Repo, InvalidGitRepositoryError

from ..config import (
    get_agent_name,
    get_version,
    resolve_thread_context,
)
from ..helpers import (
    _should_auto_branch,
    _require_context,
    _format_warnings_for_response,
)
from ..sync import get_branch_health
from ..observability import log_debug


# Module-level references to registered tools (populated by register_diagnostic_tools)
health = None
whoami = None
reconcile_parity = None


def _check_git_auth_health(threads_dir: Path) -> dict:
    """Check git authentication configuration and connectivity.

    Returns a dict with:
        protocol: 'https' or 'ssh' or 'unknown'
        credential_helper: configured helper or None
        ssh_agent_running: True/False (only for SSH)
        ssh_keys_loaded: True/False (only for SSH)
        connectivity: 'ok', 'failed', or error message
        warnings: list of warning messages
        recommendations: list of recommended actions
    """
    result = {
        "protocol": "unknown",
        "credential_helper": None,
        "ssh_agent_running": None,
        "ssh_keys_loaded": None,
        "connectivity": "unknown",
        "warnings": [],
        "recommendations": [],
    }

    try:
        repo = Repo(threads_dir, search_parent_directories=True)
    except Exception:
        result["connectivity"] = "no git repo"
        return result

    # Detect protocol from remote URL
    try:
        remote_url = repo.remotes.origin.url if repo.remotes else None
        if remote_url:
            if remote_url.startswith("git@") or remote_url.startswith("ssh://"):
                result["protocol"] = "ssh"
            elif remote_url.startswith("https://"):
                result["protocol"] = "https"
            else:
                result["protocol"] = "other"
    except Exception:
        result["protocol"] = "no remote"

    # Check credential helper (for HTTPS)
    try:
        # Try multiple methods to find credential helper
        helper = None
        github_helper = None

        # Method 1: Check GitHub-specific credential helper (takes precedence)
        try:
            result_cmd = subprocess.run(
                ["git", "config", "--global", "--get", "credential.https://github.com.helper"],
                capture_output=True, text=True, timeout=5,
                cwd=str(threads_dir)
            )
            if result_cmd.returncode == 0:
                github_helper = result_cmd.stdout.strip() or None
        except Exception:
            pass

        # Method 2: Check repo-local config
        try:
            helper = repo.config_reader().get_value("credential", "helper", fallback=None)
        except Exception:
            pass

        # Method 3: Check global config via git command (most reliable)
        if not helper:
            try:
                result_cmd = subprocess.run(
                    ["git", "config", "--global", "--get", "credential.helper"],
                    capture_output=True, text=True, timeout=5,
                    cwd=str(threads_dir)
                )
                if result_cmd.returncode == 0:
                    helper = result_cmd.stdout.strip() or None
            except Exception:
                pass

        # Method 4: Check system config
        if not helper:
            try:
                result_cmd = subprocess.run(
                    ["git", "config", "--system", "--get", "credential.helper"],
                    capture_output=True, text=True, timeout=5,
                    cwd=str(threads_dir)
                )
                if result_cmd.returncode == 0:
                    helper = result_cmd.stdout.strip() or None
            except Exception:
                pass

        # Use GitHub-specific helper if available (for GitHub repos)
        result["credential_helper"] = github_helper or helper
        result["github_credential_helper"] = github_helper
    except Exception:
        pass

    # Check SSH agent (for SSH protocol)
    if result["protocol"] == "ssh":
        # Check if SSH_AUTH_SOCK is set
        ssh_sock = os.environ.get("SSH_AUTH_SOCK")
        result["ssh_agent_running"] = bool(ssh_sock)

        if ssh_sock:
            # Check if keys are loaded
            try:
                ssh_add = subprocess.run(
                    ["ssh-add", "-l"],
                    capture_output=True, text=True, timeout=5
                )
                if ssh_add.returncode == 0 and ssh_add.stdout.strip():
                    result["ssh_keys_loaded"] = True
                else:
                    result["ssh_keys_loaded"] = False
            except Exception:
                result["ssh_keys_loaded"] = False
        else:
            result["ssh_keys_loaded"] = False

        # Add warnings for SSH without agent
        if not result["ssh_agent_running"]:
            result["warnings"].append("SSH protocol detected but no SSH agent running")
            result["recommendations"].append("Start SSH agent: eval \"$(ssh-agent -s)\" && ssh-add")
            result["recommendations"].append("Or switch to HTTPS: gh config set git_protocol https && gh auth setup-git")
        elif not result["ssh_keys_loaded"]:
            result["warnings"].append("SSH agent running but no keys loaded")
            result["recommendations"].append("Load SSH key: ssh-add ~/.ssh/id_ed25519")

    # Check HTTPS without credential helper
    if result["protocol"] == "https" and not result["credential_helper"]:
        result["warnings"].append("HTTPS protocol but no credential helper configured")
        result["recommendations"].append("Set up credential helper: gh auth setup-git")

    # Check GitHub CLI auth status if using gh as credential helper
    result["gh_auth_status"] = None
    if result["credential_helper"] and "gh" in result["credential_helper"]:
        try:
            gh_status = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=5
            )
            if gh_status.returncode == 0:
                result["gh_auth_status"] = "valid"
            else:
                stderr = gh_status.stderr.strip()
                if "authentication failed" in stderr.lower() or "no longer valid" in stderr.lower():
                    result["gh_auth_status"] = "expired"
                    result["warnings"].append("GitHub CLI token has expired")
                    result["recommendations"].append("Re-authenticate: gh auth login -h github.com --web")
                elif "not logged" in stderr.lower():
                    result["gh_auth_status"] = "not authenticated"
                    result["warnings"].append("GitHub CLI not authenticated")
                    result["recommendations"].append("Authenticate: gh auth login -h github.com --web")
                else:
                    result["gh_auth_status"] = f"error: {stderr[:50]}"
        except FileNotFoundError:
            result["gh_auth_status"] = "gh not installed"
        except subprocess.TimeoutExpired:
            result["gh_auth_status"] = "timeout"
        except Exception as e:
            result["gh_auth_status"] = f"error: {str(e)[:30]}"

    # Quick connectivity test (non-blocking, with short timeout)
    try:
        # Use git ls-remote with timeout - just checks if we can connect
        ls_remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin"],
            capture_output=True, text=True, timeout=10,
            cwd=str(threads_dir),
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # Prevent password prompts
        )
        if ls_remote.returncode == 0:
            result["connectivity"] = "ok"
        else:
            stderr = ls_remote.stderr.strip()
            if "Permission denied" in stderr or "publickey" in stderr:
                result["connectivity"] = "auth failed"
                if result["protocol"] == "ssh":
                    result["warnings"].append("SSH authentication failed - key not accepted")
            elif "Could not resolve" in stderr or "unable to access" in stderr:
                result["connectivity"] = "network error"
            else:
                result["connectivity"] = f"failed: {stderr[:100]}"
    except subprocess.TimeoutExpired:
        result["connectivity"] = "timeout (possible SSH agent issue)"
        if result["protocol"] == "ssh":
            result["warnings"].append("Git operation timed out - likely waiting for SSH passphrase")
            result["recommendations"].append("SSH agent may not have keys loaded")
    except Exception as e:
        result["connectivity"] = f"error: {str(e)[:50]}"

    return result


def _health_impl(ctx: Context, code_path: str = "") -> str:
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

        # Add git authentication health check
        if context.threads_dir:
            try:
                git_health = _check_git_auth_health(context.threads_dir)
                status_lines.extend([
                    "",
                    "Git Authentication:",
                    f"  Protocol: {git_health['protocol']}",
                    f"  Connectivity: {git_health['connectivity']}",
                ])

                if git_health['protocol'] == 'https':
                    helper = git_health['credential_helper'] or 'none'
                    status_lines.append(f"  Credential Helper: {helper}")
                    if git_health.get('gh_auth_status'):
                        status_lines.append(f"  GitHub CLI Auth: {git_health['gh_auth_status']}")
                elif git_health['protocol'] == 'ssh':
                    agent_status = "running" if git_health['ssh_agent_running'] else "not running"
                    keys_status = "loaded" if git_health['ssh_keys_loaded'] else "not loaded"
                    status_lines.append(f"  SSH Agent: {agent_status}")
                    status_lines.append(f"  SSH Keys: {keys_status}")

                # Add warnings prominently
                if git_health['warnings']:
                    status_lines.append("")
                    status_lines.append("  ⚠️  WARNINGS:")
                    for warn in git_health['warnings']:
                        status_lines.append(f"    - {warn}")

                # Add recommendations
                if git_health['recommendations']:
                    status_lines.append("")
                    status_lines.append("  Recommendations:")
                    for rec in git_health['recommendations']:
                        status_lines.append(f"    → {rec}")
            except Exception as e:
                status_lines.append(f"\nGit Authentication: Error - {e}")

        return _format_warnings_for_response("\n".join(status_lines))
    except Exception as e:
        return _format_warnings_for_response(f"Watercooler MCP Server\nStatus: Error\nError: {str(e)}")


def _whoami_impl(ctx: Context) -> str:
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


def _reconcile_parity_impl(
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

        # Use new sync package for state management
        from watercooler_mcp.sync import (
            read_parity_state,
            write_parity_state,
            ParityStatus,
            pull_ff_only,
            pull_rebase,
            get_branch_health,
            run_preflight,
            push_after_commit,
        )

        # First, try to sync threads if behind origin (the reconcile part)
        threads_repo = Repo(context.threads_dir, search_parent_directories=True)
        code_repo = Repo(context.code_root, search_parent_directories=True)
        actions_taken = []

        # Get current health before reconcile
        health_before = get_branch_health(context.code_root, context.threads_dir)

        # Check if CODE is behind origin - try auto-pull if safe (fast-forward)
        code_behind = health_before.get('code_behind_origin', 0)
        if code_behind > 0:
            code_branch = health_before.get('code_branch', 'unknown')
            code_ahead = health_before.get('code_ahead_origin', 0)

            # Check if auto-pull is safe:
            # 1. Working tree must be clean (no uncommitted changes)
            # 2. Must be fast-forward (no local commits ahead)
            is_clean = not code_repo.is_dirty(untracked_files=False)
            is_fast_forward = code_ahead == 0

            if is_clean and is_fast_forward:
                # Safe to auto-pull
                try:
                    log_debug(f"[RECONCILE] Code behind by {code_behind}, attempting auto-pull (ff-only)")
                    code_repo.git.pull('--ff-only')
                    actions_taken.append(f"Auto-pulled code repo (fast-forward, {code_behind} commits)")
                    log_debug("[RECONCILE] Code auto-pull succeeded")
                    # Refresh health after pull
                    health_before = get_branch_health(context.code_root, context.threads_dir)
                except Exception as pull_err:
                    log_debug(f"[RECONCILE] Code auto-pull failed: {pull_err}")
                    return ToolResult(content=[TextContent(
                        type="text",
                        text=json.dumps({
                            "status": "code_behind_origin",
                            "error": f"Auto-pull failed: {pull_err}. Please pull manually.",
                            "code_behind": code_behind,
                            "code_branch": code_branch,
                            "code_root": str(context.code_root),
                            "suggested_commands": [
                                f"cd {context.code_root}",
                                "git pull --rebase",
                            ],
                            "actions_taken": actions_taken,
                        }, indent=2)
                    )])
            else:
                # Not safe to auto-pull
                reason = []
                if not is_clean:
                    reason.append("working tree has uncommitted changes")
                if not is_fast_forward:
                    reason.append(f"local has {code_ahead} commits ahead (requires rebase)")

                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "code_behind_origin",
                        "error": f"Code branch '{code_branch}' is {code_behind} commits behind origin. "
                                 f"Cannot auto-pull: {'; '.join(reason)}. Please pull manually.",
                        "code_behind": code_behind,
                        "code_ahead": code_ahead,
                        "code_branch": code_branch,
                        "code_root": str(context.code_root),
                        "auto_pull_blocked": reason,
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
            if pull_ff_only(threads_repo):
                actions_taken.append(f"Pulled threads (ff-only, {threads_behind} commits)")
            else:
                log_debug("[RECONCILE] FF-only pull failed, trying rebase")
                if pull_rebase(threads_repo):
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
        health_status = get_branch_health(context.code_root, context.threads_dir)

        # If there are pending commits, try to push them
        push_result = None
        if health_status.get('pending_push') or health_status.get('threads_ahead_origin', 0) > 0:
            try:
                # Use threads_branch from health (correct branch)
                branch_name = health_status.get('threads_branch') or context.code_branch or "main"
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
                health_status = get_branch_health(context.code_root, context.threads_dir)
            except Exception as push_err:
                push_result = f"push error: {push_err}"

        output = {
            "status": health_status.get('status', 'unknown'),
            "code_branch": health_status.get('code_branch', 'unknown'),
            "threads_branch": health_status.get('threads_branch', 'unknown'),
            "code_ahead_origin": health_status.get('code_ahead_origin', 0),
            "code_behind_origin": health_status.get('code_behind_origin', 0),
            "threads_ahead_origin": health_status.get('threads_ahead_origin', 0),
            "threads_behind_origin": health_status.get('threads_behind_origin', 0),
            "pending_push": health_status.get('pending_push', False),
            "actions_taken": actions_taken,
            "push_result": push_result,
            "last_error": health_status.get('last_error'),
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


def register_diagnostic_tools(mcp):
    """Register diagnostic tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global health, whoami, reconcile_parity

    # Register tools and store references for testing
    health = mcp.tool(name="watercooler_health")(_health_impl)
    whoami = mcp.tool(name="watercooler_whoami")(_whoami_impl)
    reconcile_parity = mcp.tool(name="watercooler_reconcile_parity")(_reconcile_parity_impl)
