"""HTTP request context for hosted MCP mode.

This module provides a mechanism to pass HTTP request context (user ID, repo,
branch, GitHub token) from the HTTP middleware to MCP tools. It uses Python's
contextvars to maintain request-scoped state without threading issues.

The context is set by server_http.py middleware and read by validation.py
to determine whether to use local filesystem operations or GitHub API.

Usage:
    # In HTTP middleware (server_http.py):
    from .context import HttpRequestContext, set_http_context

    set_http_context(HttpRequestContext(
        user_id="user_123",
        repo="org/repo",
        branch="main",
        github_token="ghp_...",
    ))

    # In MCP tools:
    from .context import get_http_context

    ctx = get_http_context()
    if ctx and ctx.github_token:
        # Use GitHub API instead of filesystem
        ...
"""

from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass, field
from typing import Optional


# Context variable for HTTP request context
# This is set by the HTTP middleware and accessed by MCP tools
_http_context: contextvars.ContextVar[Optional["HttpRequestContext"]] = (
    contextvars.ContextVar("http_context", default=None)
)


def _generate_request_id() -> str:
    """Generate a unique request ID."""
    return str(uuid.uuid4())


@dataclass
class HttpRequestContext:
    """HTTP request context for hosted MCP mode.

    This dataclass holds the context extracted from HTTP headers that MCP
    tools need to operate in hosted mode (using GitHub API instead of
    local filesystem).

    Attributes:
        user_id: User identifier from X-User-ID header. Required.
        repo: Repository full name (e.g., "org/repo") from X-Repo header.
        branch: Branch name from X-Branch header. Defaults to "main".
        github_token: GitHub OAuth token for API authentication.
    """

    user_id: str
    repo: Optional[str] = None
    branch: Optional[str] = None
    github_token: Optional[str] = None
    request_id: str = field(default_factory=_generate_request_id)
    capabilities: Optional[frozenset[str]] = None  # Preloaded from credentials response
    session_id: Optional[str] = None
    client_id: Optional[str] = None
    daemon_config_json: Optional[str] = None  # Serialized DaemonsConfig from hybrid client
    # HMAC key classification ("per_user" | "service") from the auth
    # stage; None for bearer/identity paths. Consumed by grant gates
    # that give service identities an explicit bypass (graph_admin).
    auth_key_type: Optional[str] = None

    def is_complete(self) -> bool:
        """Check if context has all required fields for hosted operations.

        Returns:
            True if user_id, repo, and github_token are all set.
        """
        return bool(self.user_id and self.repo and self.github_token)

    @property
    def repo_owner(self) -> Optional[str]:
        """Extract owner from repo full name (e.g., 'org' from 'org/repo')."""
        if self.repo and "/" in self.repo:
            return self.repo.split("/")[0]
        return None

    @property
    def repo_name(self) -> Optional[str]:
        """Extract repo name from repo full name (e.g., 'repo' from 'org/repo')."""
        if self.repo and "/" in self.repo:
            return self.repo.split("/")[1]
        return None

    @property
    def effective_branch(self) -> str:
        """Get branch name, defaulting to 'main' if not set."""
        return self.branch or "main"

    @property
    def scope_id(self) -> Optional[str]:
        """Derived scope identifier: ``user_id:repo`` when both exist."""
        if self.user_id and self.repo:
            return f"{self.user_id}:{self.repo}"
        return None


def set_http_context(ctx: HttpRequestContext) -> contextvars.Token:
    """Set the HTTP request context for the current request.

    This should be called from HTTP middleware before the request is
    dispatched to MCP tools. The context is automatically scoped to
    the current async task/thread.

    Args:
        ctx: The HTTP request context to set.

    Returns:
        A token that can be used to reset the context to its previous value.
    """
    return _http_context.set(ctx)


def get_http_context() -> Optional[HttpRequestContext]:
    """Get the HTTP request context for the current request.

    Returns:
        The HttpRequestContext if set, None otherwise.
        Returns None in stdio mode (no HTTP context available).
    """
    return _http_context.get()


def clear_http_context() -> None:
    """Clear the HTTP request context.

    This resets the context to None. Useful for testing or cleanup.
    """
    _http_context.set(None)


def is_http_context_available() -> bool:
    """Check if HTTP context is available.

    Returns:
        True if we have a complete HTTP context (hosted mode),
        False otherwise (stdio mode or incomplete context).
    """
    ctx = get_http_context()
    return ctx is not None and ctx.is_complete()


# =========================================================================
# Worker context (for daemon/background tasks that run outside HTTP requests)
# =========================================================================

_worker_context: contextvars.ContextVar[Optional["HttpRequestContext"]] = (
    contextvars.ContextVar("worker_context", default=None)
)


def set_worker_context(ctx: HttpRequestContext) -> contextvars.Token:
    """Set the worker context for background tasks (daemon scopes, etc.).

    Worker context is used by ``get_effective_context()`` when no HTTP
    request context is available.
    """
    return _worker_context.set(ctx)


def get_worker_context() -> Optional[HttpRequestContext]:
    """Get the worker context, or None."""
    return _worker_context.get()


def clear_worker_context() -> None:
    """Clear the worker context."""
    _worker_context.set(None)


def get_effective_context() -> Optional[HttpRequestContext]:
    """Get the best available context: HTTP request first, worker second.

    Hosted-aware operational code should use this instead of calling
    ``get_http_context()`` directly, so that daemon/background tasks
    can also resolve context.
    """
    ctx = get_http_context()
    if ctx is not None:
        return ctx
    return get_worker_context()


# =========================================================================
# Convenience accessors for session / client identity
# =========================================================================


def get_effective_client_id(fastmcp_ctx: object = None) -> Optional[str]:
    """Get ``client_id`` from the effective context, with FastMCP fallback.

    Checks the contextvars-based ``HttpRequestContext`` first (set by
    HTTP middleware or worker context).  If that yields nothing, falls
    back to reading ``client_id`` from the FastMCP ``Context`` object
    passed by the tool runtime.

    Args:
        fastmcp_ctx: Optional FastMCP ``Context`` instance.

    Returns:
        The client_id string, or ``None`` if unavailable.
    """
    eff = get_effective_context()
    if eff and eff.client_id:
        return eff.client_id
    if fastmcp_ctx and hasattr(fastmcp_ctx, "client_id"):
        try:
            return fastmcp_ctx.client_id  # type: ignore[union-attr]
        except Exception:
            return None
    return None


def get_effective_session_id(fastmcp_ctx: object = None) -> Optional[str]:
    """Get ``session_id`` from the effective context, with FastMCP fallback.

    Checks the contextvars-based ``HttpRequestContext`` first (set by
    HTTP middleware or worker context).  If that yields nothing, falls
    back to reading ``session_id`` from the FastMCP ``Context`` object
    passed by the tool runtime.

    Args:
        fastmcp_ctx: Optional FastMCP ``Context`` instance.

    Returns:
        The session_id string, or ``None`` if unavailable.
    """
    eff = get_effective_context()
    if eff and eff.session_id:
        return eff.session_id
    if fastmcp_ctx and hasattr(fastmcp_ctx, "session_id"):
        try:
            return fastmcp_ctx.session_id  # type: ignore[union-attr]
        except Exception:
            return None
    return None
