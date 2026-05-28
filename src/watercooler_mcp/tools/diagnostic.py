"""Diagnostic tools for watercooler MCP server.

Tools:
- watercooler_health: Server health check; detail="identity" returns the
  resolved agent identity and a write-readiness assessment (folded-in
  watercooler_whoami).
"""

import os
import sys
import json
import subprocess
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastmcp import Context

from ..config import (
    get_agent_name,
    get_version,
    resolve_thread_context,
)
from ..helpers import (
    _should_auto_branch,
    _format_warnings_for_response,
)
from ..context import get_effective_client_id, get_effective_session_id
from ..observability import log_debug


# Module-level references to registered tools (populated by register_diagnostic_tools)
health = None

# Rate limit warning threshold (10% remaining triggers warning)
RATE_LIMIT_WARNING_THRESHOLD = 0.1


# =============================================================================
# Runtime context (Plan v20 Phase 1)
# =============================================================================
#
# Mirrors the ``_runtime`` module-level pattern in
# :mod:`watercooler_mcp.tools.memory`. Set from
# :func:`watercooler_mcp.server_factory.build_mcp_server`.
# ``watercooler_health`` consults this to resolve capability targets,
# queue state, and receipt summaries for the ``memory_sync`` block.

_runtime: "ToolRuntime | None" = None  # type: ignore[name-defined]  # noqa: F821


def set_runtime(runtime: "ToolRuntime | None") -> None:  # type: ignore[name-defined]  # noqa: F821
    """Set the module-level runtime context for diagnostic tools."""
    global _runtime
    _runtime = runtime


def _render_graphiti_warmup_line(state: dict) -> str:
    """Format the Graphiti warmup status line for the diagnostic surface.

    Pulled out so the render is unit-testable in isolation. Honours the
    same shape the inline code used: optional duration, topology
    (db @ host:port) only when both database and host are set, error
    suffix when present.

    Non-success states (``"skipped"``, ``"failed"``) include the
    ``reason`` field when set so operators reading ``/health`` see
    *why* the warmup didn't reach ``"ready"`` (e.g.,
    ``"multi-tenant scope-bound; warmup deferred to first per-scope
    request"`` for hosted deployments).

    When both ``error`` and ``reason`` are set (the ``"failed"`` shape),
    ``error`` wins — it carries the raw exception string the operator
    needs to diagnose an outage. ``reason`` is the fallback for states
    like ``"skipped"`` where there is no exception to surface.
    Review #737 round 1 LOW: prior code preferred the curated ``reason``
    and silently dropped the raw exception on ``/health``.
    """
    state_value = state.get("state", "unknown")
    duration = state.get("duration_ms", 0)
    err = state.get("error")
    reason = state.get("reason")
    host = state.get("host")
    port = state.get("port")
    database = state.get("database")
    detail = f"{state_value}"
    if duration:
        detail += f" ({duration}ms)"
    if database and host:
        detail += f" db={database} @ {host}:{port}"
    suffix = err or reason
    if suffix:
        detail += f" — {suffix}"
    return f"  Graphiti Warmup: {detail}"


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
        instructions.append("  For full setup guide: https://github.com/mostlyharmless-ai/watercooler/blob/main/docs/SETUP.md")

    return instructions


def _append_memory_sync_block(status_lines: list[str], context: Any) -> None:
    """Append the ``Memory Sync`` split-surface block to ``status_lines``.

    Plan v20 Phase 1 scaffolding. Reports:

    - resolved capability targets for ``memory_ingest``,
      ``semantic_similarity``, ``daemon_observe``;
    - canonical identity quadruple: ``repo_slug``, ``repo_name``,
      ``project_group_id``, physical ``t1_database`` + ``t2_database``;
    - mismatch warnings (e.g., local FalkorDB reachable while the T2
      route is remote — a muddle-producing configuration);
    - local submission queue depth + receipt counts (Phase 4);
    - remote handoff receipt counts per backend/stage (Phase 5 + Phase 8);
    - pointers to hosted-authority tools for scopes this tool does not
      own.

    ``context`` is the ``ThreadContext`` resolved by
    :func:`resolve_thread_context` in the caller.
    """
    from ..capabilities import HYBRID_DEFAULT_ROUTES
    from ..config import get_watercooler_config
    from watercooler.path_resolver import (
        derive_project_group_id,
        derive_t1_database_name,
        derive_t2_database_name,
    )

    status_lines.extend(["", "Memory Sync:"])

    # --- Resolved capability targets ------------------------------------
    try:
        wc_config = get_watercooler_config()
        transport = wc_config.mcp.transport
    except Exception:
        transport = "unknown"

    status_lines.append(f"  Transport: {transport}")

    # Capability targets: surface-level resolution. In hybrid, defaults
    # come from HYBRID_DEFAULT_ROUTES; explicit user overrides in
    # ``[mcp.capability_routes]`` win when present.
    try:
        capability_routes_override = dict(
            getattr(wc_config.mcp, "capability_routes", {}) or {}
        )
    except Exception:
        capability_routes_override = {}

    def _resolved_route(name: str) -> str:
        if transport == "hybrid":
            return capability_routes_override.get(name) or HYBRID_DEFAULT_ROUTES.get(name, "local")
        if transport == "stdio":
            return capability_routes_override.get(name) or "local"
        if transport == "proxy":
            return "remote"
        return capability_routes_override.get(name) or "local"

    status_lines.extend([
        f"  Routes:",
        f"    memory_ingest       = {_resolved_route('memory_ingest')}",
        f"    memory_observe      = {_resolved_route('memory_observe')}",
        f"    semantic_similarity = {_resolved_route('semantic_similarity')}",
        f"    daemon_observe      = {_resolved_route('daemon_observe')}",
    ])

    # --- Canonical identity quadruple -----------------------------------
    repo_slug = getattr(context, "repo_slug", None) or getattr(context, "code_repo", None)
    repo_name = getattr(context, "code_repo_name", None)
    project_group_id = derive_project_group_id(
        repo_slug=repo_slug,
        code_repo_name=repo_name,
    )
    t1_db = derive_t1_database_name(
        repo_slug=repo_slug,
        code_repo_name=repo_name,
    )
    t2_db = derive_t2_database_name(
        repo_slug=repo_slug,
        code_repo_name=repo_name,
    )
    status_lines.extend([
        f"  Identity:",
        f"    repo_slug         = {repo_slug or 'n/a'}",
        f"    repo_name         = {repo_name or 'n/a'}",
        f"    project_group_id  = {project_group_id}",
        f"    t1_database       = {t1_db}",
        f"    t2_database       = {t2_db}",
    ])

    # --- Local FalkorDB reachability + mismatch warnings ---------------
    local_falkor_reachable = False
    try:
        import socket as _socket
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", 6379))
            local_falkor_reachable = True
        except OSError:
            local_falkor_reachable = False
        finally:
            s.close()
    except Exception:
        local_falkor_reachable = False

    status_lines.append(
        f"  Local FalkorDB (127.0.0.1:6379): "
        f"{'reachable' if local_falkor_reachable else 'not reachable'}"
    )

    memory_ingest_route = _resolved_route("memory_ingest")
    semantic_similarity_route = _resolved_route("semantic_similarity")
    if transport == "hybrid" and local_falkor_reachable and memory_ingest_route == "remote":
        status_lines.append(
            "  ⚠️  Mismatch: hybrid mode with memory_ingest=remote, but a "
            "local FalkorDB is reachable. Leftover state may shadow the "
            "hosted path if a code-path regression re-enables in-process "
            "GraphitiBackend. Track the muddle-fix landing via the "
            "hybrid-falkordb-state-vs-intent thread."
        )

    # --- Local submission queue summary (Phase 4) ----------------------
    queue_line = "  Local submission queue: (not initialised)"
    try:
        from ..memory_queue import get_queue as _get_queue

        q = _get_queue()
        if q is not None:
            summary = q.status_summary()
            stats = summary.get("stats", {})
            queue_line = (
                f"  Local submission queue: depth={summary.get('queue_depth', 0)}"
                f", pending={summary.get('by_status', {}).get('pending', 0)}"
                f", running={summary.get('by_status', {}).get('running', 0)}"
                f", total_completed={stats.get('total_completed', 0)}"
                f", total_dead_lettered={stats.get('total_dead_lettered', 0)}"
            )
    except Exception:
        pass
    status_lines.append(queue_line)

    # --- Remote handoff receipt summary (Phase 5 + Phase 8) ------------
    handoff_line = "  Remote handoff receipts: (none recorded)"
    try:
        from ..handoff_receipts import summary as _handoff_summary

        s = _handoff_summary()
        total = s.get("total", 0)
        if total:
            by_stage = s.get("by_stage", {})
            by_backend = s.get("by_backend", {})
            stages = ", ".join(
                f"{k}={v}" for k, v in sorted(by_stage.items())
            )
            backends = ", ".join(
                f"{k}={v}" for k, v in sorted(by_backend.items())
            )
            handoff_line = (
                f"  Remote handoff receipts: total={total}"
                f" (stages: {stages}; backends: {backends})"
            )
    except Exception:
        pass
    status_lines.append(handoff_line)

    # --- Footer pointers to hosted-authority tools ---------------------
    status_lines.extend([
        f"  Hosted memory/backend authority: watercooler_diagnose_memory",
        f"  Hosted premium-daemon authority: watercooler_daemon_status",
    ])


def _describe_storage_mode(threads_dir: Path) -> str:
    """Return a human label for the thread storage mode at ``threads_dir``.

    The health block previously hard-coded "orphan worktree" regardless
    of actual state. This helper classifies the directory so the
    display matches what's really there (Bug #3, plan v4):

    - No ``.git`` at ``threads_dir`` OR no origin remote OR origin
      is not a GitHub-family host → ``"local-only (no GitHub
      backing)"`` — appended with ``" (WATERCOOLER_ALLOW_LOCAL_ONLY)"``
      when the opt-in is set. These are the same three conditions
      ``assert_github_backed_threads`` refuses on, so the health
      display stays 1:1 with the write guard.
    - Under ``~/.watercooler/worktrees/<repo>/`` with a valid
      ``.git`` entry → ``"orphan worktree"``.
    - Sibling ``<parent>/<repo>-threads`` directory → ``"sibling-threads (legacy)"``.
    - Anything else → ``"custom (<basename>)"``.

    Stdlib-only; reuses the write_guard module's lightweight git
    detection so we don't pay for a GitPython import in the hot
    health path.
    """
    from watercooler.write_guard import (
        ENV_ALLOW_LOCAL_ONLY,
        _looks_github_hosted,
        _read_origin_url,
        _resolve_real_gitdir,
        _is_allow_local_only_enabled,
    )

    name = threads_dir.name
    worktrees_root = Path.home() / ".watercooler" / "worktrees"
    is_under_worktrees = False
    try:
        threads_dir.resolve(strict=False).relative_to(
            worktrees_root.resolve(strict=False)
        )
        is_under_worktrees = True
    except (ValueError, OSError):
        pass

    # Require the .git entry AT threads_dir, not at an ancestor — same
    # semantics as ``assert_github_backed_threads``. Walking ancestors
    # here would label ``<repo>/_custom`` as ``custom (_custom)`` or
    # even ``orphan worktree`` (implying GitHub-backed) while the write
    # guard refuses the write with "not a git worktree". Health output
    # and write behavior must agree on what counts as backed.
    direct_git = threads_dir / ".git"
    git_entry = direct_git if direct_git.exists() else None
    origin_url: Optional[str] = None
    if git_entry is not None:
        gitdir = _resolve_real_gitdir(git_entry)
        if gitdir is not None:
            origin_url = _read_origin_url(gitdir)

    # "local-only" iff writes won't actually reach a GitHub remote —
    # the exact same conditions ``assert_github_backed_threads``
    # refuses on: no .git at threads_dir, no origin URL, or an
    # origin URL that isn't GitHub-hosted. Omitting the host check
    # would label a GitLab / Bitbucket threads dir as ``custom`` or
    # ``orphan worktree`` (implying writes are fine) while the
    # guard refuses the write — health and guard must agree. The
    # directory's NAME (e.g. ``_local``) is a convention, not a
    # guarantee, and is deliberately not a short-circuit here.
    # A worktree-path directory with no .git is a bootstrap that scaffolded
    # the topic-directory tree but never bound the git worktree (issue
    # #787). That is distinct from genuine local-only mode — a real
    # operational failure — so surface it as its own state rather than
    # mislabelling it "local-only".
    if is_under_worktrees and git_entry is None and threads_dir.exists():
        return "scaffold-only (bootstrap incomplete — re-run onboarding)"

    local_only = (
        git_entry is None
        or not origin_url
        or not _looks_github_hosted(origin_url)
    )
    if local_only:
        label = "local-only (no GitHub backing)"
        if _is_allow_local_only_enabled():
            label += f" ({ENV_ALLOW_LOCAL_ONLY})"
        return label

    if is_under_worktrees:
        return "orphan worktree"

    if name.endswith("-threads") and threads_dir.parent.exists():
        return "sibling-threads (legacy)"

    return f"custom ({name})"


def _check_git_auth_health(
    threads_dir: Path,
    code_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Check git authentication configuration and connectivity.

    Tries the threads worktree first (where thread pushes land), then
    falls back to ``code_path`` if the worktree probe fails — orphan
    worktrees occasionally raise from ``Repo()`` for gitdir-pointer
    edge cases even when the worktree otherwise functions. When both
    probes fail, the original exception texts are preserved in
    ``warnings`` instead of reporting a flat "no git repo" while every
    other feature is demonstrably working against the same repo.

    Returns a dict with:
        protocol: 'https' or 'ssh' or 'unknown'
        credential_helper: configured helper or None
        ssh_agent_running: True/False (only for SSH)
        ssh_keys_loaded: True/False (only for SSH)
        connectivity: 'ok', 'failed', 'probe failed', or error message
        warnings: list of warning messages (includes probe-error details
            when all candidates raised)
        recommendations: list of recommended actions
    """
    result: dict[str, Any] = {
        "protocol": "unknown",
        "credential_helper": None,
        "ssh_agent_running": None,
        "ssh_keys_loaded": None,
        "connectivity": "unknown",
        "warnings": [],
        "recommendations": [],
    }

    # Local import (matches the pattern elsewhere in this module; `Repo`
    # isn't imported at module scope, which in the pre-fix code caused
    # every call to raise NameError and fall through to the flat
    # "no git repo" connectivity string — the root reason Adi's Mac
    # reported that despite the worktree being operational).
    from git import Repo

    repo = None
    opened_from: Optional[Path] = None
    probe_errors: list[str] = []
    for candidate in (threads_dir, code_path):
        if candidate is None:
            continue
        try:
            repo = Repo(candidate, search_parent_directories=True)
            opened_from = candidate
            break
        except Exception as exc:
            probe_errors.append(
                f"{candidate}: {type(exc).__name__}: {exc}"
            )

    if repo is None:
        result["connectivity"] = "probe failed"
        targets = "threads worktree" + (" or code_path" if code_path else "")
        result["warnings"].append(
            f"Could not open a git repository at {targets}. "
            f"Probe errors: {'; '.join(probe_errors) if probe_errors else '(none recorded)'}."
        )
        return result

    # Every subprocess probe below uses ``probe_cwd`` — the working
    # tree of the repo that Repo() actually opened. Falling back from
    # threads_dir to code_path but then shelling out to git with
    # cwd=threads_dir would re-trigger the filesystem error we just
    # recovered from (missing/broken worktree), producing a misleading
    # "connectivity: error: [Errno 2] No such file or directory" when
    # the actual auth state of the code repo is healthy.
    #
    # Preference order: working_tree_dir > the candidate path that
    # actually opened the repo > threads_dir as last resort.
    # ``working_tree_dir`` is None for bare repos, in which case
    # falling back to ``threads_dir`` (which may not exist — that's
    # why we fell back) would re-introduce the Errno 2 we're guarding
    # against. Using ``opened_from`` preserves the fallback intent.
    probe_cwd: Path = threads_dir
    try:
        wt_dir = repo.working_tree_dir
    except Exception:
        wt_dir = None
    if wt_dir:
        probe_cwd = Path(wt_dir)
    elif opened_from is not None:
        probe_cwd = opened_from

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
                cwd=str(probe_cwd)
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
                    cwd=str(probe_cwd)
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
                    cwd=str(probe_cwd)
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
            cwd=str(probe_cwd),
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


def _health_hosted_impl(ctx: Context) -> str:
    """Cheap health check for hosted mode — control-plane info only.

    Reports only in-process / cached state.  No network I/O:
    no GitHub API calls, no LLM/embedding probes, no FalkorDB pings.
    Heavy diagnostics live in ``watercooler_diagnose_memory``.
    """
    agent = get_agent_name(ctx.client_id)
    version = get_version()

    status_lines = [
        f"Watercooler MCP Server v{version}",
        f"Status: Healthy",
        f"Mode: hosted",
        f"Agent: {agent}",
        f"Threads Dir: hosted (GitHub API)",
    ]

    # Deployment profile (cheap — reads cached dataclass, no I/O)
    try:
        from ..deployment_profile import resolve_deployment_availability
        da = resolve_deployment_availability()
        status_lines.extend([
            "",
            "Deployment Profile:",
            f"  Requested: {da.requested_profile}",
            f"  Effective: {da.effective_profile}",
        ])
        if da.degraded_reasons:
            status_lines.append(f"  Degraded: {'; '.join(da.degraded_reasons)}")
    except Exception as e:
        status_lines.append(f"\nDeployment Profile: Error - {e}")

    # Graphiti warmup state (module-level dict in server_http, no I/O)
    try:
        from ..server_http import _graphiti_warm_state  # type: ignore[attr-defined]
        status_lines.append(_render_graphiti_warmup_line(_graphiti_warm_state))
    except ImportError:
        # Not running inside server_http (e.g. stdio mode) — skip
        pass
    except Exception:
        status_lines.append("  Graphiti Warmup: unknown")

    # Daemon runtime type (cheap — reads module-level singleton)
    try:
        from ..daemons import get_daemon_runtime
        from ..daemons.hosted_coordinator import HostedDaemonCoordinator
        runtime = get_daemon_runtime()
        if isinstance(runtime, HostedDaemonCoordinator):
            status_lines.append(f"\nDaemons: hosted coordinator")
        elif runtime is not None:
            names = runtime.daemon_names
            status_lines.append(f"\nDaemons: manager ({len(names)} registered)")
        else:
            status_lines.append("\nDaemons: not initialized")
    except Exception:
        status_lines.append("\nDaemons: error checking status")

    # Request timeout budget (module-level constant in server_http, no I/O)
    try:
        from ..server_http import REQUEST_TIMEOUT  # type: ignore[attr-defined]
        status_lines.append(f"Request Timeout: {REQUEST_TIMEOUT}s")
    except ImportError:
        pass
    except Exception:
        pass

    return "\n".join(status_lines)


def _health_identity_impl(ctx: Context, code_path: str = "") -> str:
    """Resolved agent identity plus a write-readiness assessment.

    Folds in the retired watercooler_whoami and addresses #327: a bare
    identity check should tell the agent whether the write tools
    (say/ack/handoff) will accept a call, not just echo a client id.
    """
    try:
        client_id = get_effective_client_id(ctx)
        session_id = get_effective_session_id(ctx)
        agent = get_agent_name(client_id)
    except Exception as e:  # pragma: no cover - defensive
        return f"Error determining identity: {str(e)}"

    lines = [
        f"You are: {agent}",
        f"Client ID: {client_id or 'None'}",
        f"Session ID: {session_id or 'None'}",
        "",
        "Write readiness:",
    ]
    try:
        context = resolve_thread_context(Path(code_path) if code_path else None)
        threads_dir = context.threads_dir
        # Observational only — an identity probe must not mutate the
        # filesystem (the retired watercooler_whoami never did). Report
        # whether the dir exists / is writable; do NOT create it.
        if threads_dir.exists():
            state = "writable" if os.access(threads_dir, os.W_OK) else "NOT writable"
        else:
            parent = threads_dir.parent
            creatable = parent.exists() and os.access(parent, os.W_OK)
            state = "absent, creatable" if creatable else "absent, parent not writable"
        lines.append(f"  Threads dir: {threads_dir} ({state})")
    except Exception as e:
        lines.append(f"  Threads dir: unresolved ({e})")
    lines.append(
        "  agent_func: write tools require an explicit agent_func in "
        "'<platform>:<model>:<role>' form, passed per call — it is not "
        "server state and cannot be pre-verified here."
    )
    return "\n".join(lines)


def _health_impl(ctx: Context, code_path: str = "", detail: str = "") -> str:
    """Check server health and configuration including branch parity status.

    Returns server version, configured agent identity, threads directory,
    and branch parity health status.

    Args:
        code_path: Optional path to code repository for parity checks.
        detail: Pass ``"identity"`` for the resolved agent identity and a
            write-readiness assessment (folded-in watercooler_whoami).

    Example output:
        Watercooler MCP Server v0.1.0
        Status: Healthy
        Agent: Codex
        Threads Dir: /path/to/project/.watercooler
        Threads Dir Exists: True
        Branch Parity: clean
    """
    if str(detail).strip().lower() == "identity":
        return _health_identity_impl(ctx, code_path)

    # Hosted mode guard — additive early return
    from ..auth import is_hosted_mode
    if is_hosted_mode():
        return _health_hosted_impl(ctx)

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
            f"Threads Repo URL: {context.code_remote or 'local-only'}",
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

        # Add daemon status summary
        try:
            from watercooler_mcp.daemons import get_daemon_runtime
            from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

            _d_runtime = get_daemon_runtime()
            if isinstance(_d_runtime, HostedDaemonCoordinator):
                daemon_statuses = _d_runtime.status()
            elif _d_runtime is not None:
                daemon_statuses = _d_runtime.status_all()
            else:
                daemon_statuses = None
            if daemon_statuses is not None and daemon_statuses:
                if daemon_statuses:
                    status_lines.extend(["", "Daemons:"])
                    daemon_icons = {
                        "running": "✓",
                        "stopped": "○",
                        "disabled": "○",
                        "paused": "⏸",
                        "failed": "✗",
                        "starting": "⏳",
                    }
                    # Label reflects which runtime owns these daemons.  Under
                    # the config-driven registration model any daemon can run
                    # locally (``DaemonManager``) or hosted
                    # (``HostedDaemonCoordinator``) depending on deployment —
                    # the daemon itself is no longer classified per-name, so
                    # the label is derived once from the runtime type and
                    # applies to every daemon it owns.
                    location = "hosted" if isinstance(_d_runtime, HostedDaemonCoordinator) else "local"
                    for dname, dinfo in daemon_statuses.items():
                        dstate = dinfo.get("status", "unknown")
                        dicon = daemon_icons.get(dstate, "?")
                        interval = dinfo.get("interval", 0)
                        ticks = dinfo.get("total_ticks", 0)
                        findings = dinfo.get("total_findings", 0)
                        errors = dinfo.get("error_count", 0)

                        if dstate == "running":
                            status_lines.append(
                                f"  {dicon} {dname} [{location}]: {dstate} "
                                f"(interval={interval}s, ticks={ticks}, "
                                f"findings={findings}, errors={errors})"
                            )
                        elif dstate == "disabled":
                            status_lines.append(f"  {dicon} {dname} [{location}]: {dstate}")
                        else:
                            last_err = dinfo.get("last_error", "")
                            suffix = f" - {last_err}" if last_err else ""
                            status_lines.append(f"  {dicon} {dname} [{location}]: {dstate}{suffix}")
                    # Hybrid/proxy mode may route premium daemons to the hosted
                    # coordinator — point operators at the right tool to see
                    # the full hosted picture (see docs/CONFIGURATION_HOSTED.md).
                    # ``project_coordinator`` is governed by ``daemon_observe``,
                    # the capability that decides where
                    # ``watercooler_daemon_status`` itself runs — so its
                    # location tells us whether the hint applies.
                    from watercooler_mcp.daemons import daemon_runtime_location

                    if (
                        not isinstance(_d_runtime, HostedDaemonCoordinator)
                        and daemon_runtime_location("project_coordinator") == "hosted"
                    ):
                        status_lines.append(
                            "  ℹ Premium daemons run on the hosted service — "
                            "use watercooler_daemon_status for full view"
                        )
        except Exception as e:
            status_lines.append(f"\nDaemons: Error - {e}")

        # Add memory-sync split-surface block (Plan v20 Phase 1).
        # Reports resolved capability routes, canonical identity, and
        # mismatch warnings for hybrid mode.  Queue depth / receipt
        # summaries are placeholders here; Phase 5 populates them.
        try:
            _append_memory_sync_block(status_lines, context)
        except Exception as e:
            status_lines.append(f"\nMemory Sync: Error - {e}")

        # Add service telemetry summary
        try:
            from watercooler_mcp.daemons.telemetry import get_telemetry
            telem = get_telemetry()
            if telem:
                status_lines.extend(["", "Service Telemetry:"])
                for svc, stats in sorted(telem.items()):
                    calls = stats.get("calls", 0)
                    errors = stats.get("errors", 0)
                    cache_hits = stats.get("cache_hits", 0)
                    cache_misses = stats.get("cache_misses", 0)
                    parts = [f"calls={calls}"]
                    if errors:
                        parts.append(f"errors={errors}")
                    if cache_hits or cache_misses:
                        total = cache_hits + cache_misses
                        rate = f"{cache_hits/total*100:.0f}%" if total else "n/a"
                        parts.append(f"cache={rate}")
                    status_lines.append(f"  {svc}: {', '.join(parts)}")
        except Exception:
            pass  # Telemetry is optional

        # Add thread storage info with parity state
        if context.threads_dir:
            try:
                # Storage-mode display is computed dynamically (Bug #3).
                # Previously hard-coded as "orphan worktree" regardless
                # of the actual threads_dir state, which directly
                # contradicted the "Threads Repo URL: local-only" line
                # printed above when the resolver fell back to _local.
                orphan_label = _describe_storage_mode(context.threads_dir)
                status_lines.extend([
                    "",
                    "Thread Storage:",
                    f"  Mode: {orphan_label}",
                    f"  Path: {context.threads_dir}",
                    f"  Code Branch: {context.code_branch or 'n/a'}",
                ])

                # Branch parity reporting
                try:
                    from ..sync import get_parity_state, fetch_with_timeout
                    from git import Repo as _Repo
                    _parity_repo = _Repo(context.threads_dir)
                    # Fetch first for accurate remote comparison (short timeout)
                    try:
                        fetch_with_timeout(_parity_repo, timeout=5)
                    except Exception:
                        pass  # Stale parity is better than no parity
                    parity = get_parity_state(_parity_repo)

                    parity_icons = {
                        "clean": "✓",
                        "behind_only": "⬇",
                        "ahead_only": "⬆",
                        "diverged": "⬆⬇",
                        "dirty_derived_only": "⚠️",
                        "dirty_mixed": "✗",
                        "stuck_rebase_or_merge": "✗",
                        "no_upstream": "○",
                        "auth_or_network_error": "✗",
                    }
                    icon = parity_icons.get(parity, "?")
                    status_lines.append(f"  Branch Parity: {parity} {icon}")

                    # Add details for non-clean states
                    if parity in ("dirty_derived_only", "dirty_mixed"):
                        try:
                            dirty_out = _parity_repo.git.status("--porcelain")
                            dirty_count = len([l for l in dirty_out.strip().split("\n") if l.strip()])
                            status_lines.append(f"  Dirty Files: {dirty_count}")
                        except Exception:
                            pass

                    if parity in ("behind_only", "ahead_only", "diverged"):
                        try:
                            from ..sync import get_ahead_behind
                            branch_name = _parity_repo.active_branch.name
                            ahead, behind = get_ahead_behind(_parity_repo, branch_name)
                            status_lines.append(f"  Ahead: {ahead}  Behind: {behind}")
                        except Exception:
                            pass

                    if parity == "stuck_rebase_or_merge":
                        status_lines.append("  → Run: watercooler sync-repair")

                    # no_upstream: the thread branch isn't tracking any
                    # remote — it was bootstrapped against a remote that is
                    # gone/unreachable, or never published (issue #689).
                    # Surface the remotes + a concrete republish command.
                    if parity == "no_upstream":
                        try:
                            remote_names = sorted(
                                r.name for r in _parity_repo.remotes
                            )
                        except Exception:
                            remote_names = []
                        # Which remotes already carry the thread branch —
                        # republishing there repairs tracking instead of
                        # forking history onto a second remote.
                        published: list[str] = []
                        try:
                            suffix = "/watercooler/threads"
                            rbr = _parity_repo.git.branch(
                                "-r", "--list", f"*{suffix}"
                            )
                            for line in rbr.splitlines():
                                ref = line.strip()
                                if ref and " -> " not in ref and ref.endswith(suffix):
                                    published.append(ref[: -len(suffix)])
                        except Exception:
                            pass
                        if remote_names:
                            status_lines.append(
                                f"  Remotes: {', '.join(remote_names)}"
                            )
                            from watercooler.sync_repair import suggest_publish_remote
                            _tgt = suggest_publish_remote(
                                remote_names, sorted(set(published))
                            )
                            _arg = (
                                f"publish_remote='{_tgt}'" if _tgt
                                else "publish_remote='<remote>'"
                            )
                            status_lines.append(
                                "  → Thread branch has no remote upstream — "
                                f"publish it: watercooler_sync_repair({_arg})"
                            )
                        else:
                            status_lines.append(
                                "  → Thread branch has no remote upstream and "
                                "no git remotes are configured."
                            )

                    # Honest semantics: "safe for reads" means local data is
                    # at least as current as the remote — not just "did the
                    # fetch complete." A behind-only worktree serves stale
                    # entries until a pull lands, so report that truthfully
                    # and surface a stale-data line for states that
                    # format_parity_warning doesn't already cover (behind_only
                    # in particular is excluded from _WARN_PARITY_STATES).
                    safe_for_reads = parity in ("clean", "ahead_only", "no_upstream")
                    status_lines.append(f"  Safe for Reads: {safe_for_reads}")

                    if not safe_for_reads:
                        status_lines.append(
                            f"  ⚠️ STALE DATA (parity={parity}): "
                            "local thread data may not reflect recent origin updates."
                        )

                    if parity not in ("clean", "behind_only", "ahead_only"):
                        status_lines.append(f"  Recommended: watercooler sync-repair --diagnose")

                except Exception as parity_err:
                    status_lines.append(f"  Branch Parity: error ({parity_err})")
            except Exception as e:
                status_lines.append(f"\nThread Storage: Error - {e}")

        # Add git authentication health check.
        # Pass code_path so the probe has a fallback when the orphan
        # worktree's Repo() call raises (gitdir-pointer quirks, moved
        # code repo, etc.). Keeps the diagnostic honest instead of
        # reporting "no git repo" while the rest of the system works.
        if context.threads_dir:
            try:
                git_health = _check_git_auth_health(
                    context.threads_dir,
                    Path(code_path) if code_path else None,
                )
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


def register_diagnostic_tools(mcp):
    """Register diagnostic tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global health

    # Register tools and store references for testing
    health = mcp.tool(name="watercooler_health")(_health_impl)
