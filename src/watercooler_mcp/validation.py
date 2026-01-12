"""Context validation functions for watercooler MCP server.

This module contains the patchable validation functions that are used across
tool modules. By centralizing them here, we:
1. Break the circular import pattern (tools no longer import server at runtime)
2. Provide a single source of truth for context validation
3. Make it easy for tests to patch these functions directly

Functions:
- _require_context: Resolve and validate thread context from code path
- _dynamic_context_missing: Check if dynamic context env vars are set but unresolved
- _refresh_threads: Validate branch pairing and pull latest changes
- _validate_thread_context: Combined validation helper for tool implementations
"""

import os
from pathlib import Path

# Local application imports
from watercooler.config_facade import config

from .config import (
    ThreadContext,
    get_git_sync_manager_from_context,
    resolve_thread_context,
)
from .sync import BranchPairingError
from .observability import log_debug


# ============================================================================
# Context Resolution Helpers
# ============================================================================


def _require_context(code_path: str) -> tuple[str | None, ThreadContext | None]:
    """Resolve ThreadContext from a code repository path.

    This function handles WSL path conversion on Windows, validates the
    code_path is provided, and resolves the full ThreadContext including
    the threads directory location.

    Args:
        code_path: Path to the code repository root. Required.

    Returns:
        Tuple of (error_message, context). If error_message is not None,
        context will be None.

    Example:
        error, context = _require_context("/path/to/my/repo")
        if error:
            return error
        # Use context.threads_dir, context.code_root, etc.
    """
    log_debug(f"_require_context: entry with code_path={code_path!r}")
    if not code_path:
        return (
            "code_path required: pass the code repository root (e.g., '.') so the "
            "server can resolve the correct threads repo/branch.",
            None,
        )

    # Handle WSL-style absolute paths on Windows (e.g., /C/Users/...)
    if os.name == "nt" and code_path.startswith("/") and len(code_path) > 2:
        drive = code_path[1]
        if drive.isalpha() and code_path[2] == "/":
            code_path = f"{drive}:{code_path[2:].replace('/', os.sep)}"

    # Detect if a threads repo was passed instead of a code repo
    code_path_obj = Path(code_path).resolve()
    if code_path_obj.name.endswith("-threads"):
        # Check if a matching code repo exists (same path without -threads suffix)
        potential_code_repo = code_path_obj.parent / code_path_obj.name[:-8]  # Remove "-threads"
        if potential_code_repo.exists() and potential_code_repo.is_dir():
            return (
                f"Error: code_path appears to be a threads repo, not a code repo.\n"
                f"You passed: {code_path}\n"
                f"Did you mean: {potential_code_repo}\n\n"
                f"The code_path parameter should point to your code repository,\n"
                f"not the threads repository. The threads repo is managed automatically.",
                None,
            )

    if config.env.get_bool("WATERCOOLER_DEBUG_CODE_PATH", False):
        log_dir = config.env.get("WATERCOOLER_DEBUG_LOG_DIR", "")
        log_path = (
            Path(log_dir).resolve()
            if log_dir
            else Path.home() / ".watercooler-codepath-debug.log"
        )
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"cwd={Path.cwd()} input={code_path!r}\n")
        except Exception:
            pass

    try:
        log_debug(f"_require_context: calling resolve_thread_context({code_path!r})")
        context = resolve_thread_context(Path(code_path))
        log_debug("_require_context: resolve_thread_context returned")
    except Exception as exc:
        log_debug(f"_require_context: exception from resolve_thread_context: {exc}")
        return (f"Error resolving code context: {exc}", None)

    log_debug("_require_context: exit, returning context")
    return (None, context)


def _dynamic_context_missing(context: ThreadContext) -> bool:
    """Check if dynamic context environment variables are set but unresolved.

    This detects the case where the user has set dynamic threads configuration
    (like WATERCOOLER_THREADS_BASE or WATERCOOLER_GIT_REPO) but the resolver
    couldn't find a matching threads directory.

    Args:
        context: The resolved ThreadContext

    Returns:
        True if dynamic env vars are set but context resolution failed,
        False otherwise.
    """
    dynamic_env = any(
        config.env.get(key, "")
        for key in (
            "WATERCOOLER_THREADS_BASE",
            "WATERCOOLER_THREADS_PATTERN",
            "WATERCOOLER_GIT_REPO",
            "WATERCOOLER_CODE_REPO",
        )
    )
    return dynamic_env and not context.explicit_dir and context.threads_slug is None


def _refresh_threads(context: ThreadContext, skip_validation: bool = False) -> None:
    """Refresh threads repo by validating branch pairing and pulling latest changes.

    This function ensures the threads repository is synchronized before any
    read or write operation. It validates that code and threads branches match,
    and pulls the latest changes from the remote.

    Args:
        context: Thread context with repo information
        skip_validation: If True, skip branch validation (used for recovery operations)

    Raises:
        BranchPairingError: If branch validation fails and skip_validation=False
    """
    # Import here to avoid circular import - _validate_and_sync_branches
    # depends on other helpers.py functions
    from .helpers import _validate_and_sync_branches

    # Validate and sync branches (will raise if validation fails)
    _validate_and_sync_branches(context, skip_validation=skip_validation)

    sync = get_git_sync_manager_from_context(context)
    if not sync:
        return

    status = sync.get_async_status()
    if status.get("mode") == "async":
        # Async mode relies on background pulls; avoid blocking operations.
        return
    sync.pull()


def _validate_thread_context(code_path: str) -> tuple[str | None, ThreadContext | None]:
    """Validate and resolve thread context for MCP tools.

    This is a convenience wrapper that combines _require_context and
    _dynamic_context_missing checks into a single call.

    Args:
        code_path: Path to code repository

    Returns:
        Tuple of (error_message, context). If error_message is not None,
        context will be None.

    Example:
        error, context = _validate_thread_context(code_path)
        if error:
            return error
        # Use context safely
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
