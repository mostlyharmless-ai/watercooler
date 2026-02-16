"""Context validation functions for watercooler MCP server.

This module contains the patchable validation functions that are used across
tool modules. By centralizing them here, we:
1. Break the circular import pattern (tools no longer import server at runtime)
2. Provide a single source of truth for context validation
3. Make it easy for tests to patch these functions directly

Functions:
- _require_context: Resolve and validate thread context from code path
- _require_context_hosted: Resolve context for hosted HTTP mode (GitHub API)
- _dynamic_context_missing: Check if dynamic context env vars are set but unresolved
- _refresh_threads: Validate branch pairing and pull latest changes
- _validate_thread_context: Combined validation helper for tool implementations
- validate_safe_path: Check for path traversal attacks
"""

import os
from pathlib import Path
from typing import Optional, Union


# ============================================================================
# Path Security Helpers
# ============================================================================


def validate_safe_path(
    user_path: Union[str, Path],
    allowed_bases: Optional[list[Path]] = None,
    must_exist: bool = False,
) -> tuple[Optional[str], Optional[Path]]:
    """Validate a user-supplied path for safety.

    Checks for:
    - Path traversal attempts (.. sequences after resolution)
    - Absolute paths escaping allowed directories
    - Null bytes and other injection attempts

    Args:
        user_path: The user-supplied path to validate
        allowed_bases: Optional list of allowed base directories. If provided,
            the resolved path must be under one of these directories.
        must_exist: If True, also verify the path exists.

    Returns:
        Tuple of (error_message, resolved_path). If error_message is not None,
        resolved_path will be None.

    Example:
        error, safe_path = validate_safe_path(user_input, allowed_bases=[threads_dir])
        if error:
            return f"Invalid path: {error}"
        # Use safe_path
    """
    # Convert to string for initial checks
    path_str = str(user_path)

    # Check for null bytes (injection attack)
    if "\x00" in path_str:
        return "Path contains null bytes", None

    # Check for obviously suspicious patterns before resolution
    # These could indicate attempted traversal even if resolve() handles them
    suspicious_patterns = ["...", "...."]
    for pattern in suspicious_patterns:
        if pattern in path_str:
            return f"Path contains suspicious pattern: {pattern}", None

    try:
        # Resolve to absolute path (handles .. and symlinks)
        resolved = Path(user_path).resolve()
    except (OSError, ValueError) as e:
        return f"Invalid path: {e}", None

    # Check existence if required
    if must_exist and not resolved.exists():
        return f"Path does not exist: {resolved}", None

    # Check against allowed bases if provided
    if allowed_bases:
        is_allowed = False
        for base in allowed_bases:
            try:
                resolved_base = base.resolve()
                # Check if resolved path is under the allowed base
                resolved.relative_to(resolved_base)
                is_allowed = True
                break
            except ValueError:
                # Not under this base, try next
                continue

        if not is_allowed:
            bases_str = ", ".join(str(b) for b in allowed_bases)
            return f"Path escapes allowed directories: {bases_str}", None

    return None, resolved

# Local application imports
from watercooler.config_facade import config
from watercooler.path_resolver import (
    get_threads_suffix,
    derive_threads_repo_name,
    derive_code_repo_from_threads,
)

from .auth import is_hosted_mode
from .config import (
    ThreadContext,
    get_git_sync_manager_from_context,
    resolve_thread_context,
)
from .context import get_http_context, HttpRequestContext
from .observability import log_debug


# ============================================================================
# Context Resolution Helpers
# ============================================================================


# Sentinel path to indicate hosted mode (no local filesystem)
HOSTED_MODE_SENTINEL = Path("/hosted")


def _require_context_hosted(
    http_ctx: HttpRequestContext,
) -> tuple[str | None, ThreadContext | None]:
    """Resolve ThreadContext for hosted HTTP mode using GitHub API.

    In hosted mode, we don't have local filesystem access. Instead, we use
    the HTTP request context (user ID, repo, branch, GitHub token) to
    construct a ThreadContext that signals tools should use GitHub API
    operations instead of filesystem operations.

    Args:
        http_ctx: HTTP request context with user ID, repo, branch, and token.

    Returns:
        Tuple of (error_message, context). If error_message is not None,
        context will be None.

    Example:
        http_ctx = get_http_context()
        if is_hosted_mode() and http_ctx:
            error, context = _require_context_hosted(http_ctx)
            if error:
                return error
            # Use context with GitHub API operations
    """
    log_debug(f"_require_context_hosted: entry with repo={http_ctx.repo!r}")

    if not http_ctx.repo:
        return (
            "Hosted mode requires X-Repo header with repository name "
            "(e.g., 'org/repo-threads').",
            None,
        )

    if not http_ctx.github_token:
        return (
            "Hosted mode requires a valid GitHub token. "
            "Ensure the user has connected their GitHub account.",
            None,
        )

    # Parse repo into owner/name for URL construction
    repo = http_ctx.repo
    if "/" not in repo:
        return (
            f"Invalid repository format: {repo}. Expected 'owner/repo-threads'.",
            None,
        )

    owner, repo_name = repo.split("/", 1)
    branch = http_ctx.effective_branch

    # Derive threads repo name from code repo name using config-aware function
    # e.g., "watercooler-site" -> "watercooler-site-threads"
    threads_repo_name = derive_threads_repo_name(repo_name)

    # Construct ThreadContext for hosted mode
    # - threads_dir uses a sentinel path to indicate hosted mode
    # - Tools should check is_hosted_context() before filesystem operations
    context = ThreadContext(
        code_root=None,  # No local filesystem in hosted mode
        threads_dir=HOSTED_MODE_SENTINEL,  # Sentinel indicating hosted mode
        code_repo=repo,  # The CODE repo (from X-Repo header)
        code_branch=branch,
        code_commit=None,  # No local git commit info
        code_remote=f"https://github.com/{owner}/{repo_name}.git",
        explicit_dir=True,  # We have explicit context from HTTP headers
    )

    log_debug(f"_require_context_hosted: exit, returning hosted context for {repo}")
    return (None, context)


def is_hosted_context(context: ThreadContext) -> bool:
    """Check if a ThreadContext represents hosted mode (GitHub API operations).

    Args:
        context: The ThreadContext to check.

    Returns:
        True if the context is for hosted mode (uses GitHub API),
        False if it's for local mode (uses filesystem).
    """
    return context.threads_dir == HOSTED_MODE_SENTINEL


def _require_context(code_path: str) -> tuple[str | None, ThreadContext | None]:
    """Resolve ThreadContext from a code repository path.

    This function handles:
    - Hosted HTTP mode: Uses HTTP context from middleware (GitHub API)
    - Local stdio mode: Uses filesystem paths and git operations

    In hosted mode, the code_path parameter is ignored and context is derived
    from HTTP headers (X-User-ID, X-Repo, X-Branch) set by the middleware.

    Args:
        code_path: Path to the code repository root. Required in local mode,
            ignored in hosted mode.

    Returns:
        Tuple of (error_message, context). If error_message is not None,
        context will be None.

    Example:
        error, context = _require_context("/path/to/my/repo")
        if error:
            return error
        # Use context.threads_dir, context.code_root, etc.
        # Check is_hosted_context(context) to determine operation mode.
    """
    log_debug(f"_require_context: entry with code_path={code_path!r}")

    # Check for hosted HTTP mode first
    # In hosted mode, we use HTTP context instead of filesystem paths
    if is_hosted_mode():
        http_ctx = get_http_context()
        if http_ctx and http_ctx.repo:
            log_debug(f"_require_context: hosted mode detected, using HTTP context")
            return _require_context_hosted(http_ctx)
        else:
            log_debug("_require_context: hosted mode but no HTTP context available")
            return (
                "Hosted mode active but HTTP context not available. "
                "Ensure X-User-ID and X-Repo headers are provided.",
                None,
            )

    # Local stdio mode: require code_path
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

    # Validate path for security (null bytes, suspicious patterns)
    path_error, code_path_obj = validate_safe_path(code_path)
    if path_error:
        return (f"Invalid code_path: {path_error}", None)
    if code_path_obj is None:
        return ("Invalid code_path: could not resolve path", None)

    # Detect if a threads repo was passed instead of a code repo
    threads_suffix = get_threads_suffix()
    if code_path_obj.name.endswith(threads_suffix):
        # Check if a matching code repo exists (same path without threads suffix)
        code_repo_name = derive_code_repo_from_threads(code_path_obj.name, threads_suffix)
        potential_code_repo = code_path_obj.parent / code_repo_name
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
    return dynamic_env and not context.explicit_dir


def _refresh_threads(context: ThreadContext, skip_validation: bool = False) -> None:
    """Refresh threads repo by pulling latest changes.

    This function ensures the threads repository is synchronized before any
    read or write operation by pulling the latest changes from the remote.

    Args:
        context: Thread context with repo information
        skip_validation: If True, skip validation (kept for API compatibility)
    """
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
