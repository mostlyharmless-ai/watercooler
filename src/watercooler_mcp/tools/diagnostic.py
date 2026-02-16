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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from ..observability import log_debug


# Module-level references to registered tools (populated by register_diagnostic_tools)
health = None
whoami = None
reconcile_parity = None

# Rate limit warning threshold (10% remaining triggers warning)
RATE_LIMIT_WARNING_THRESHOLD = 0.1


def _get_service_gap_instructions(service_status: dict) -> list[str]:
    """Generate actionable instructions for missing or failed services.

    Args:
        service_status: Dictionary of service statuses from get_service_status()

    Returns:
        List of instruction lines for resolving service gaps
    """
    instructions = []
    has_gaps = False

    for name, status in service_status.items():
        state = status["state"]
        msg = status.get("message", "")

        if state == "failed":
            has_gaps = True
            instructions.append("")
            instructions.append(f"  ⚠️  {name.upper()} - SETUP REQUIRED:")

            if name == "llm":
                instructions.extend([
                    "    llama-server not found or failed to start.",
                    "",
                    "    Option 1: Enable auto-download (config.toml):",
                    "      [mcp.service_provision]",
                    "      llama_server = true",
                    "",
                    "    Option 2: Set environment variable:",
                    "      WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER=true",
                    "",
                    "    Option 3: Manual install:",
                    "      Download from: https://github.com/ggml-org/llama.cpp/releases",
                    "      Extract llama-server to ~/.watercooler/bin/ or add to PATH",
                ])

            elif name == "embedding":
                instructions.extend([
                    "    Embedding service (llama-server) not available.",
                    "",
                    "    Same setup as LLM above - llama-server handles both.",
                    "    Embedding model will auto-download when service starts.",
                ])

            elif name == "falkordb":
                instructions.extend([
                    "    FalkorDB (graph database) not running.",
                    "",
                    "    Requires Docker. Install Docker first:",
                    "      Linux: curl -fsSL https://get.docker.com | sh",
                    "      macOS: Install Docker Desktop from docker.com",
                    "",
                    "    Then start FalkorDB:",
                    "      docker run -d -p 6379:6379 -p 3000:3000 \\",
                    "        --name falkordb \\",
                    "        -v falkordb_data:/var/lib/falkordb/data \\",
                    "        falkordb/falkordb:latest",
                ])

            if msg and "not found" not in msg.lower():
                instructions.append(f"    Details: {msg}")

    if has_gaps:
        instructions.insert(0, "")
        instructions.insert(1, "Service Setup Instructions:")
        instructions.append("")
        instructions.append("  For full setup guide: https://github.com/MostlyHarmless-AI/watercooler-cloud/docs/SETUP.md")

    return instructions


def _check_git_auth_health(threads_dir: Path) -> dict[str, Any]:
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


def _check_github_rate_limit() -> dict[str, Any]:
    """Check GitHub API rate limit status.

    Returns a dict with:
        remaining: calls remaining in current window
        limit: total calls allowed per hour
        percent: percentage remaining (0-100)
        reset_minutes: minutes until rate limit resets
        status: 'ok', 'warning' (<10%), 'limited' (0 remaining), or 'error'
        warnings: list of warning messages
        recommendations: list of recommended actions
    """
    result = {
        "remaining": None,
        "limit": None,
        "percent": None,
        "reset_minutes": None,
        "status": "unknown",
        "warnings": [],
        "recommendations": [],
    }

    try:
        api_result = subprocess.run(
            ["gh", "api", "rate_limit"],
            capture_output=True, text=True, timeout=10
        )
        if api_result.returncode == 0:
            data = json.loads(api_result.stdout)
            core = data.get("resources", {}).get("core", {})

            remaining = core.get("remaining", 0)
            limit = core.get("limit", 5000)
            reset_ts = core.get("reset", 0)

            result["remaining"] = remaining
            result["limit"] = limit
            result["percent"] = round((remaining / limit) * 100) if limit > 0 else 0

            # Calculate minutes until reset (GitHub returns UTC timestamps)
            if reset_ts > 0:
                reset_time = datetime.fromtimestamp(reset_ts, tz=timezone.utc)
                now = datetime.now(tz=timezone.utc)
                if reset_time > now:
                    delta = reset_time - now
                    # Use total_seconds() to handle deltas > 24 hours correctly
                    result["reset_minutes"] = max(0, int(delta.total_seconds()) // 60)
                else:
                    result["reset_minutes"] = 0

            # Determine status and warnings
            if remaining == 0:
                result["status"] = "limited"
                result["warnings"].append(f"RATE LIMITED - 0/{limit} calls remaining")
                result["recommendations"].append(
                    f"Wait {result['reset_minutes']} minutes for reset, or reduce API calls"
                )
            elif remaining < (limit * RATE_LIMIT_WARNING_THRESHOLD):
                result["status"] = "warning"
                result["warnings"].append(
                    f"Approaching rate limit: {remaining}/{limit} ({result['percent']}%) remaining"
                )
                result["recommendations"].append("Consider pausing automated operations")
            else:
                result["status"] = "ok"
        else:
            # gh api call failed - might be auth issue
            stderr = api_result.stderr.strip()
            if "rate limit" in stderr.lower():
                result["status"] = "limited"
                result["warnings"].append("Rate limit exceeded (from error response)")
            else:
                result["status"] = "error"
                result["warnings"].append(f"Could not check rate limit: {stderr[:80]}")

    except FileNotFoundError:
        result["status"] = "gh_not_installed"
        result["warnings"].append("gh CLI not installed")
        result["recommendations"].append("Install: https://cli.github.com/")
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["warnings"].append("Rate limit check timed out")
    except json.JSONDecodeError as e:
        result["status"] = "error"
        result["warnings"].append(f"Invalid JSON from gh api: {e}")
    except Exception as e:
        result["status"] = "error"
        result["warnings"].append(f"Rate limit check failed: {str(e)[:50]}")

    return result


def _check_gh_version() -> dict[str, Any]:
    """Check gh CLI version.

    Returns a dict with:
        version: version string (e.g., "2.83.2")
        major: major version number
        minor: minor version number
        is_outdated: True if version < 2.20
        status: 'ok', 'outdated', 'not_installed', or 'error'
        warnings: list of warning messages
        recommendations: list of recommended actions
    """
    result = {
        "version": None,
        "major": None,
        "minor": None,
        "is_outdated": False,
        "status": "unknown",
        "warnings": [],
        "recommendations": [],
    }

    # Minimum recommended version (2.20 has important fixes)
    MIN_MAJOR = 2
    MIN_MINOR = 20

    try:
        version_result = subprocess.run(
            ["gh", "--version"],
            capture_output=True, text=True, timeout=5
        )
        if version_result.returncode == 0:
            # Parse version from output like "gh version 2.83.2 (2025-12-10)"
            output = version_result.stdout.strip()
            match = re.search(r"gh version (\d+)\.(\d+)\.(\d+)", output)
            if match:
                major = int(match.group(1))
                minor = int(match.group(2))
                patch = int(match.group(3))
                result["version"] = f"{major}.{minor}.{patch}"
                result["major"] = major
                result["minor"] = minor

                # Check if outdated
                if major < MIN_MAJOR or (major == MIN_MAJOR and minor < MIN_MINOR):
                    result["is_outdated"] = True
                    result["status"] = "outdated"
                    result["warnings"].append(
                        f"gh version {result['version']} is outdated (< {MIN_MAJOR}.{MIN_MINOR})"
                    )
                    result["recommendations"].append(
                        "Update gh: sudo apt update && sudo apt install gh"
                    )
                    result["recommendations"].append(
                        "Or see: https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
                    )
                else:
                    result["status"] = "ok"
            else:
                result["status"] = "error"
                result["warnings"].append(f"Could not parse gh version from: {output[:50]}")
        else:
            result["status"] = "error"
            result["warnings"].append(f"gh --version failed: {version_result.stderr[:50]}")

    except FileNotFoundError:
        result["status"] = "not_installed"
        result["warnings"].append("gh CLI not installed")
        result["recommendations"].append("Install: https://cli.github.com/")
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["warnings"].append("gh version check timed out")
    except Exception as e:
        result["status"] = "error"
        result["warnings"].append(f"gh version check failed: {str(e)[:50]}")

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

        # Add backend service auto-start status
        try:
            from watercooler_mcp.startup import get_live_service_status, ServiceState

            service_status = get_live_service_status()
            status_lines.extend([
                "",
                "Backend Services (Auto-Start):",
            ])

            state_icons = {
                "running": "✓",
                "starting": "⏳",
                "failed": "✗",
                "disabled": "○",
                "not_configured": "○",
                "unknown": "?",
            }

            for name, status in service_status.items():
                state = status["state"]
                icon = state_icons.get(state, "?")
                msg = status.get("message", "")
                endpoint = status.get("endpoint", "")

                if state == "running":
                    startup_ms = status.get("startup_time_ms")
                    if startup_ms:
                        status_lines.append(f"  {icon} {name}: {state} ({startup_ms}ms) {endpoint}")
                    else:
                        status_lines.append(f"  {icon} {name}: {state} {endpoint}")
                elif state in ("disabled", "not_configured"):
                    status_lines.append(f"  {icon} {name}: {state} - {msg}")
                elif state == "starting":
                    status_lines.append(f"  {icon} {name}: {state}... {endpoint}")
                elif state == "failed":
                    status_lines.append(f"  {icon} {name}: {state} - {msg}")
                else:
                    status_lines.append(f"  {icon} {name}: {state}")

            # Add actionable instructions for service gaps
            gap_instructions = _get_service_gap_instructions(service_status)
            if gap_instructions:
                status_lines.extend(gap_instructions)

        except Exception as e:
            status_lines.append(f"\nBackend Services: Error - {e}")

        # Add thread storage info
        if context.threads_dir:
            try:
                orphan_label = "orphan worktree" if getattr(context, "orphan_mode", False) else "directory"
                status_lines.extend([
                    "",
                    "Thread Storage:",
                    f"  Mode: {orphan_label}",
                    f"  Path: {context.threads_dir}",
                    f"  Code Branch: {context.code_branch or 'n/a'}",
                ])
            except Exception as e:
                status_lines.append(f"\nThread Storage: Error - {e}")

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

        # Add GitHub rate limit and version check
        try:
            gh_version = _check_gh_version()
            rate_limit = _check_github_rate_limit()

            status_lines.extend([
                "",
                "GitHub:",
            ])

            # gh version
            if gh_version["version"]:
                version_status = "✓" if gh_version["status"] == "ok" else "⚠️"
                status_lines.append(f"  gh Version: {gh_version['version']} {version_status}")
            elif gh_version["status"] == "not_installed":
                status_lines.append("  gh Version: not installed ⚠️")
            else:
                status_lines.append(f"  gh Version: {gh_version['status']}")

            # Rate limit
            if rate_limit["remaining"] is not None:
                percent = rate_limit["percent"]
                remaining = rate_limit["remaining"]
                limit = rate_limit["limit"]
                reset_min = rate_limit["reset_minutes"]

                if rate_limit["status"] == "limited":
                    status_lines.append(f"  Rate Limit: {remaining}/{limit} (0%) ⚠️ RATE LIMITED")
                    status_lines.append(f"    → Resets in {reset_min} minutes")
                elif rate_limit["status"] == "warning":
                    status_lines.append(f"  Rate Limit: {remaining}/{limit} ({percent}%) ⚠️")
                    status_lines.append(f"    → Resets in {reset_min} minutes")
                else:
                    reset_str = f" - resets in {reset_min}min" if reset_min else ""
                    status_lines.append(f"  Rate Limit: {remaining}/{limit} ({percent}%){reset_str}")
            elif rate_limit["status"] == "gh_not_installed":
                status_lines.append("  Rate Limit: n/a (gh not installed)")
            else:
                status_lines.append(f"  Rate Limit: {rate_limit['status']}")

            # Collect all warnings and recommendations
            all_warnings = gh_version["warnings"] + rate_limit["warnings"]
            all_recs = gh_version["recommendations"] + rate_limit["recommendations"]

            if all_warnings:
                status_lines.append("")
                status_lines.append("  ⚠️  GitHub WARNINGS:")
                for warn in all_warnings:
                    status_lines.append(f"    - {warn}")

            if all_recs:
                status_lines.append("")
                status_lines.append("  Recommendations:")
                for rec in all_recs:
                    status_lines.append(f"    → {rec}")

        except Exception as e:
            status_lines.append(f"\nGitHub: Error - {e}")

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
    """Reconcile thread storage with remote.

    Pulls latest threads from origin and pushes any local commits.

    Args:
        code_path: Path to code repository directory (default: current directory)

    Returns:
        JSON with sync status and actions taken.
    """
    try:
        error, context = _require_context(code_path)
        if error:
            return ToolResult(content=[TextContent(type="text", text=error)])
        if context is None or not context.threads_dir:
            return ToolResult(content=[TextContent(
                type="text",
                text="Error: Unable to resolve threads directory."
            )])

        from ..sync import ensure_readable, push_with_retry

        actions_taken = []

        # Pull latest
        ok, msgs = ensure_readable(context.threads_dir)
        if ok:
            actions_taken.append("Pulled latest from origin")
        else:
            actions_taken.append(f"Pull issues: {'; '.join(msgs)}")

        # Push any local commits
        try:
            threads_repo = Repo(context.threads_dir, search_parent_directories=True)
            branch = threads_repo.active_branch.name
            ahead = len(list(threads_repo.iter_commits(f"origin/{branch}..{branch}")))
            if ahead > 0:
                push_ok = push_with_retry(context.threads_dir, branch)
                if push_ok:
                    actions_taken.append(f"Pushed {ahead} commits to origin/{branch}")
                else:
                    actions_taken.append(f"Push failed for {ahead} commits")
        except Exception as push_err:
            actions_taken.append(f"Push check error: {push_err}")

        output = {
            "status": "ok",
            "threads_dir": str(context.threads_dir),
            "code_branch": context.code_branch or "unknown",
            "actions_taken": actions_taken,
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
            text=f"Error reconciling: {str(e)}"
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
