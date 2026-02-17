from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from importlib import metadata as importlib_metadata  # type: ignore
except ImportError:  # pragma: no cover - Python <3.8 fallback
    import importlib_metadata  # type: ignore

from watercooler.agents import _canonical_agent, _load_agents_registry

from .observability import log_debug

# Import shared git discovery and path helpers from path_resolver (consolidates logic)
from watercooler.path_resolver import (
    discover_git_info as _discover_git_shared,
    _expand_path,
    _resolve_path,
    _extract_repo_path,
)


__all__ = [
    "ThreadContext",
    "resolve_thread_context",
    "get_threads_dir",
    "get_threads_dir_for",
    "get_code_context",
    "get_agent_name",
    "get_version",
    "get_slack_config",
    "is_slack_enabled",
]


ORPHAN_BRANCH_NAME = "watercooler/threads"
WORKTREE_BASE = Path("~/.watercooler/worktrees").expanduser()


@dataclass(frozen=True)
class ThreadContext:
    """Resolved configuration for operating on watercooler threads."""

    code_root: Optional[Path]
    threads_dir: Path
    code_repo: Optional[str]
    code_branch: Optional[str]
    code_commit: Optional[str]
    code_remote: Optional[str]
    explicit_dir: bool


@dataclass(frozen=True)
class _GitDetails:
    root: Optional[Path]
    branch: Optional[str]
    commit: Optional[str]
    remote: Optional[str]


def _normalize_code_root(code_root: Optional[Path]) -> Optional[Path]:
    if code_root is None:
        return None
    if not isinstance(code_root, Path):
        code_root = Path(code_root)
    try:
        code_root = code_root.expanduser()
    except Exception:
        pass
    return _resolve_path(code_root)


def _run_git(args: list[str], cwd: Path) -> Optional[str]:
    cmd = " ".join(args)
    log_debug(f"CONFIG_GIT_START: git {cmd} (cwd={cwd})")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
        log_debug(f"CONFIG_GIT_END: git {cmd} (returned {len(result.stdout)} chars)")
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log_debug(f"CONFIG_GIT_FAIL: git {cmd} (error: {type(e).__name__})")
        return None


def _discover_git(code_root: Optional[Path]) -> _GitDetails:
    """Discover git repository info using shared path_resolver.

    Delegates to watercooler.path_resolver.discover_git_info to consolidate
    git discovery logic and eliminate duplication.
    """
    log_debug(f"CONFIG: Discovering git info for {code_root}")

    # Use shared git discovery from path_resolver
    git_info = _discover_git_shared(code_root)

    log_debug(f"CONFIG: Git discovery complete (root={git_info.root}, branch={git_info.branch})")

    return _GitDetails(
        root=git_info.root,
        branch=git_info.branch,
        commit=git_info.commit,
        remote=git_info.remote
    )




def _worktree_path_for(code_root: Path) -> Path:
    """Compute the worktree path for a code repo."""
    return WORKTREE_BASE / code_root.name


def _orphan_branch_exists(code_root: Path) -> bool:
    """Check if the orphan branch exists (local or remote)."""
    result = _run_git(["branch", "-a", "--list", f"*{ORPHAN_BRANCH_NAME}*"], code_root)
    return bool(result and ORPHAN_BRANCH_NAME in result)


def _create_orphan_branch(code_root: Path) -> bool:
    """Create the orphan branch with an empty initial commit.

    Creates the branch without switching the code working tree.
    """
    log_debug(f"CONFIG: Creating orphan branch '{ORPHAN_BRANCH_NAME}' in {code_root}")

    # Use git worktree to create the orphan branch without touching the main tree.
    # Step 1: Create a temporary orphan branch via low-level commands
    wt_path = _worktree_path_for(code_root)
    wt_path.mkdir(parents=True, exist_ok=True)

    # Create orphan branch and worktree in one step
    result = _run_git(
        ["worktree", "add", "--orphan", "-b", ORPHAN_BRANCH_NAME, str(wt_path)],
        code_root,
    )
    if result is None:
        # Fallback for older git: create orphan branch manually
        log_debug("CONFIG: git worktree add --orphan failed, trying manual approach")

        # Create a detached worktree first
        _run_git(["worktree", "add", "--detach", str(wt_path)], code_root)

        # Create orphan branch in the worktree
        _run_git(["checkout", "--orphan", ORPHAN_BRANCH_NAME], wt_path)
        _run_git(["rm", "-rf", "."], wt_path)

    # Create initial empty commit in the worktree
    _run_git(["commit", "--allow-empty", "-m", "Initialize watercooler threads"], wt_path)

    # Push to origin if remote exists
    _run_git(["push", "-u", "origin", ORPHAN_BRANCH_NAME], wt_path)

    log_debug(f"CONFIG: Orphan branch '{ORPHAN_BRANCH_NAME}' created")
    return True


def _ensure_worktree(code_root: Path) -> Optional[Path]:
    """Ensure the orphan branch worktree exists, creating it if needed.

    Returns:
        Path to the worktree directory, or None if creation failed.
    """
    wt_path = _worktree_path_for(code_root)

    # Check if worktree already exists and is valid
    if wt_path.exists() and (wt_path / ".git").exists():
        # Verify it's on the right branch
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], wt_path)
        if branch == ORPHAN_BRANCH_NAME:
            return wt_path
        # Worktree exists but wrong branch — remove and recreate
        log_debug(f"CONFIG: Worktree at {wt_path} on wrong branch '{branch}', recreating")
        _run_git(["worktree", "remove", "--force", str(wt_path)], code_root)

    # Check if orphan branch exists
    if not _orphan_branch_exists(code_root):
        try:
            _create_orphan_branch(code_root)
        except Exception as e:
            log_debug(f"CONFIG: Failed to create orphan branch: {e}")
            return None
    else:
        # Branch exists but no worktree — create worktree
        wt_path.mkdir(parents=True, exist_ok=True)
        result = _run_git(
            ["worktree", "add", str(wt_path), ORPHAN_BRANCH_NAME],
            code_root,
        )
        if result is None:
            log_debug(f"CONFIG: Failed to create worktree at {wt_path}")
            return None

    if wt_path.exists() and (wt_path / ".git").exists():
        return wt_path

    return None


def resolve_thread_context(code_root: Optional[Path] = None) -> ThreadContext:
    normalized_root = _normalize_code_root(code_root)
    git_details = _discover_git(normalized_root)

    explicit_dir_env = os.getenv("WATERCOOLER_DIR")
    if explicit_dir_env:
        threads_dir = _resolve_path(_expand_path(explicit_dir_env))
    else:
        threads_dir = None

    code_repo_env = os.getenv("WATERCOOLER_CODE_REPO")

    code_remote = git_details.remote
    code_repo = code_repo_env or None

    if code_repo is None and code_remote:
        repo_path = _extract_repo_path(code_remote)
        if repo_path:
            parts = [p for p in repo_path.split("/") if p]
            if parts:
                code_repo = "/".join(parts)

    effective_root = git_details.root or normalized_root

    # =========================================================================
    # Explicit directory override (WATERCOOLER_DIR)
    # =========================================================================
    if explicit_dir_env and threads_dir is not None:
        return ThreadContext(
            code_root=effective_root,
            threads_dir=threads_dir,
            code_repo=code_repo,
            code_branch=git_details.branch,
            code_commit=git_details.commit,
            code_remote=code_remote,
            explicit_dir=True,
        )

    # =========================================================================
    # Orphan Branch Worktree (the architecture)
    # =========================================================================
    if effective_root is not None:
        wt_dir = _ensure_worktree(effective_root)
        if wt_dir is not None:
            log_debug(f"CONFIG: Orphan worktree active, threads_dir={wt_dir}")
            return ThreadContext(
                code_root=effective_root,
                threads_dir=wt_dir,
                code_repo=code_repo,
                code_branch=git_details.branch,
                code_commit=git_details.commit,
                code_remote=code_remote,
                explicit_dir=False,
            )
        else:
            log_debug("CONFIG: Worktree creation failed, falling back to _local")
            from .helpers import _add_startup_warning
            _add_startup_warning(
                "Worktree creation failed — threads will be stored in a local "
                "_local/ directory instead of the orphan branch. New writes will "
                "NOT be synced to the remote. Check git permissions and retry."
            )

    # =========================================================================
    # Fallback: no git context (or worktree failed)
    # =========================================================================
    if threads_dir is None:
        base = Path.cwd()
        threads_dir = _resolve_path(base / "_local")

    return ThreadContext(
        code_root=effective_root,
        threads_dir=threads_dir,
        code_repo=code_repo,
        code_branch=git_details.branch,
        code_commit=git_details.commit,
        code_remote=code_remote,
        explicit_dir=False,
    )


def get_threads_dir() -> Path:
    return resolve_thread_context().threads_dir


def get_threads_dir_for(code_root: Optional[Path]) -> Path:
    return resolve_thread_context(code_root).threads_dir



def get_code_context(code_root: Optional[Path]) -> Dict[str, str]:
    ctx = resolve_thread_context(code_root)
    return {
        "code_root": str(ctx.code_root) if ctx.code_root else "",
        "code_repo": ctx.code_repo or "",
        "code_branch": ctx.code_branch or "",
        "code_commit": ctx.code_commit or "",
        "threads_dir": str(ctx.threads_dir),
    }


def get_agent_name(client_id: Optional[str] = None) -> str:
    agent_env = os.getenv("WATERCOOLER_AGENT")
    if agent_env:
        base_agent = agent_env
    else:
        base_agent = _infer_agent_from_client(client_id)
    registry_path = os.getenv("WATERCOOLER_AGENT_REGISTRY")
    registry = _load_agents_registry(registry_path)
    explicit_tag = os.getenv("WATERCOOLER_AGENT_TAG")
    return _canonical_agent(base_agent, registry, user_tag=explicit_tag)


def _infer_agent_from_client(client_id: Optional[str]) -> str:
    if not client_id:
        return "Agent"
    lowered = client_id.strip().lower()
    if not lowered:
        return "Agent"
    if lowered.startswith("claude"):
        return "Claude"
    if lowered.startswith("codex"):
        return "Codex"
    if lowered.startswith("gpt"):
        return "GPT"
    return client_id.split()[0]


def get_version() -> str:
    for dist_name in ("watercooler-cloud", "watercooler-mcp"):
        try:
            return importlib_metadata.version(dist_name)
        except importlib_metadata.PackageNotFoundError:
            continue
        except Exception:
            break
    return os.getenv("WATERCOOLER_MCP_VERSION", "0.0.0")


# =============================================================================
# Config System Integration (TOML-based configuration)
# =============================================================================

# Lazy-loaded config to avoid import-time file I/O
_loaded_config: Optional["WatercoolerConfig"] = None


def get_watercooler_config(project_path: Optional[Path] = None) -> "WatercoolerConfig":
    """Get the loaded Watercooler configuration.

    Lazy-loads config from TOML files on first access.
    Uses cached config for subsequent calls.

    Args:
        project_path: Project directory for config discovery

    Returns:
        WatercoolerConfig instance
    """
    global _loaded_config

    if _loaded_config is None:
        try:
            from watercooler.config_loader import load_config
            _loaded_config = load_config(project_path)
        except ImportError:
            # Config system not available, use defaults
            from watercooler.config_schema import WatercoolerConfig
            _loaded_config = WatercoolerConfig()
        except Exception as e:
            # Config loading failed, use defaults
            log_debug(f"Config loading failed, using defaults: {e}")
            from watercooler.config_schema import WatercoolerConfig
            _loaded_config = WatercoolerConfig()

    return _loaded_config


def reload_config(project_path: Optional[Path] = None) -> "WatercoolerConfig":
    """Force reload of configuration from disk.

    Args:
        project_path: Project directory for config discovery

    Returns:
        Freshly loaded WatercoolerConfig instance
    """
    global _loaded_config
    _loaded_config = None
    return get_watercooler_config(project_path)


def get_mcp_transport_config() -> Dict[str, Any]:
    """Get MCP transport configuration.

    Returns dict with keys: transport, host, port
    Environment variables override config file values.
    """
    config = get_watercooler_config()

    return {
        "transport": os.getenv("WATERCOOLER_MCP_TRANSPORT", config.mcp.transport),
        "host": os.getenv("WATERCOOLER_MCP_HOST", config.mcp.host),
        "port": int(os.getenv("WATERCOOLER_MCP_PORT", str(config.mcp.port))),
    }


def get_sync_config() -> Dict[str, Any]:
    """Get git sync configuration.

    Returns dict with sync settings.
    Environment variables override config file values.
    """
    config = get_watercooler_config()
    sync = config.mcp.sync

    def _get_float(env_key: str, default: float) -> float:
        val = os.getenv(env_key)
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return default

    def _get_int(env_key: str, default: int) -> int:
        val = os.getenv(env_key)
        if val:
            try:
                return int(val)
            except ValueError:
                pass
        return default

    def _get_bool(env_key: str, default: bool) -> bool:
        val = os.getenv(env_key)
        if val:
            return val.lower() in ("1", "true", "yes", "on")
        return default

    return {
        "async_sync": _get_bool("WATERCOOLER_ASYNC_SYNC", sync.async_sync),
        "batch_window": _get_float("WATERCOOLER_BATCH_WINDOW", sync.batch_window),
        "max_delay": sync.max_delay,
        "max_batch_size": sync.max_batch_size,
        "max_retries": _get_int("WATERCOOLER_SYNC_MAX_RETRIES", sync.max_retries),
        "max_backoff": _get_float("WATERCOOLER_SYNC_MAX_BACKOFF", sync.max_backoff),
        "interval": _get_float("WATERCOOLER_SYNC_INTERVAL", sync.interval),
        "stale_threshold": sync.stale_threshold,
    }


def get_logging_config() -> Dict[str, Any]:
    """Get logging configuration.

    Returns dict with logging settings.
    Environment variables override config file values.
    """
    config = get_watercooler_config()
    logging = config.mcp.logging

    return {
        "level": os.getenv("WATERCOOLER_LOG_LEVEL", logging.level),
        "dir": os.getenv("WATERCOOLER_LOG_DIR", logging.dir) or None,
        "max_bytes": int(os.getenv("WATERCOOLER_LOG_MAX_BYTES", str(logging.max_bytes))),
        "backup_count": int(os.getenv("WATERCOOLER_LOG_BACKUP_COUNT", str(logging.backup_count))),
        "disable_file": os.getenv("WATERCOOLER_LOG_DISABLE_FILE", "").lower() in ("1", "true", "yes") or logging.disable_file,
    }


def get_agent_for_platform(platform_slug: Optional[str] = None) -> Dict[str, str]:
    """Get agent configuration for a platform.

    Args:
        platform_slug: Platform identifier (e.g., "claude-code", "cursor")

    Returns:
        Dict with name and default_spec for the agent
    """
    config = get_watercooler_config()

    if platform_slug:
        agent_config = config.get_agent_config(platform_slug)
        if agent_config:
            return {
                "name": agent_config.name,
                "default_spec": agent_config.default_spec,
            }

    return {
        "name": config.mcp.default_agent,
        "default_spec": "general-purpose",
    }


def get_slack_config() -> Dict[str, Any]:
    """Get Slack integration configuration.

    Returns dict with Slack settings.
    Environment variables override config file values.
    """
    config = get_watercooler_config()
    slack = config.mcp.slack

    def _get_bool(env_key: str, default: bool) -> bool:
        val = os.getenv(env_key)
        if val:
            return val.lower() in ("1", "true", "yes", "on")
        return default

    def _get_float(env_key: str, default: float) -> float:
        val = os.getenv(env_key)
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        return default

    return {
        "webhook_url": os.getenv("WATERCOOLER_SLACK_WEBHOOK", slack.webhook_url),
        "bot_token": os.getenv("WATERCOOLER_SLACK_BOT_TOKEN", slack.bot_token),
        "app_token": os.getenv("WATERCOOLER_SLACK_APP_TOKEN", slack.app_token),
        "default_channel": os.getenv("WATERCOOLER_SLACK_CHANNEL", slack.default_channel),
        # Phase 2+ config
        "channel_prefix": os.getenv("WATERCOOLER_SLACK_CHANNEL_PREFIX", slack.channel_prefix),
        "auto_create_channels": _get_bool("WATERCOOLER_SLACK_AUTO_CREATE_CHANNELS", slack.auto_create_channels),
        # Notification toggles
        "notify_on_say": _get_bool("WATERCOOLER_SLACK_NOTIFY_SAY", slack.notify_on_say),
        "notify_on_ball_flip": _get_bool("WATERCOOLER_SLACK_NOTIFY_BALL_FLIP", slack.notify_on_ball_flip),
        "notify_on_status_change": _get_bool("WATERCOOLER_SLACK_NOTIFY_STATUS", slack.notify_on_status_change),
        "notify_on_handoff": _get_bool("WATERCOOLER_SLACK_NOTIFY_HANDOFF", slack.notify_on_handoff),
        "min_notification_interval": _get_float("WATERCOOLER_SLACK_MIN_INTERVAL", slack.min_notification_interval),
    }


def is_slack_enabled() -> bool:
    """Check if Slack notifications are enabled (webhook or bot token)."""
    slack_config = get_slack_config()
    return bool(slack_config.get("webhook_url")) or bool(slack_config.get("bot_token"))


def is_slack_bot_enabled() -> bool:
    """Check if Slack bot API is enabled (Phase 2+)."""
    slack_config = get_slack_config()
    return bool(slack_config.get("bot_token"))


# Type hint import (deferred to avoid circular imports)
if False:  # TYPE_CHECKING equivalent
    from watercooler.config_schema import WatercoolerConfig
