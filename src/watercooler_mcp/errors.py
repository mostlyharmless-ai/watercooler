"""Custom error types for watercooler MCP tools.

These errors extend FastMCP's ToolError to provide categorized error types
that translate to proper JSON-RPC errors in the MCP protocol.

Usage:
    from .errors import ThreadNotFoundError, ValidationError

    # In a tool implementation:
    raise ThreadNotFoundError(topic="my-thread", repo="org/repo")
    raise ValidationError("code_path required")

Error Codes:
    THREAD_NOT_FOUND - Thread does not exist
    VALIDATION_ERROR - Invalid input parameters
    CONTEXT_ERROR - Failed to resolve thread context
    HOSTED_ERROR - Error in hosted mode operations (GitHub API)
    IDENTITY_ERROR - Missing or invalid agent identity
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError


class WatercoolerError(ToolError):
    """Base class for watercooler MCP errors."""

    code: str = "WATERCOOLER_ERROR"

    def __init__(self, message: str, **details):
        self.details = details
        super().__init__(message)


class ThreadNotFoundError(WatercoolerError):
    """Raised when a thread does not exist."""

    code = "THREAD_NOT_FOUND"

    def __init__(self, topic: str, repo: str | None = None):
        self.topic = topic
        self.repo = repo
        if repo:
            message = f"Thread '{topic}' not found in repository: {repo}"
        else:
            message = f"Thread '{topic}' not found"
        super().__init__(message, topic=topic, repo=repo)


class ValidationError(WatercoolerError):
    """Raised when input validation fails."""

    code = "VALIDATION_ERROR"

    def __init__(self, message: str, field: str | None = None):
        self.field = field
        super().__init__(message, field=field)


class ContextError(WatercoolerError):
    """Raised when thread context cannot be resolved."""

    code = "CONTEXT_ERROR"

    def __init__(self, message: str, code_path: str | None = None):
        self.code_path = code_path
        super().__init__(message, code_path=code_path)


class HostedModeError(WatercoolerError):
    """Raised when hosted mode operations fail."""

    code = "HOSTED_ERROR"

    def __init__(self, message: str, operation: str | None = None):
        self.operation = operation
        super().__init__(message, operation=operation)


class IdentityError(WatercoolerError):
    """Raised when agent identity is missing or invalid."""

    code = "IDENTITY_ERROR"

    def __init__(self, message: str | None = None):
        if message is None:
            message = (
                "identity required: pass agent_func as '<platform>:<model>:<role>' "
                "(e.g., 'Cursor:Composer 1:implementer')"
            )
        super().__init__(message)


class GitHubAPIError(WatercoolerError):
    """Raised when GitHub API operations fail."""

    code = "GITHUB_API_ERROR"

    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message, status_code=status_code)


class EntryNotFoundError(WatercoolerError):
    """Raised when a specific entry is not found."""

    code = "ENTRY_NOT_FOUND"

    def __init__(
        self,
        topic: str,
        index: int | None = None,
        entry_id: str | None = None,
    ):
        self.topic = topic
        self.index = index
        self.entry_id = entry_id
        if entry_id:
            message = f"Entry '{entry_id}' not found in thread '{topic}'"
        elif index is not None:
            message = f"Entry at index {index} not found in thread '{topic}'"
        else:
            message = f"Entry not found in thread '{topic}'"
        super().__init__(message, topic=topic, index=index, entry_id=entry_id)


class IndexOutOfRangeError(WatercoolerError):
    """Raised when an entry index is out of range."""

    code = "INDEX_OUT_OF_RANGE"

    def __init__(self, index: int, total: int, topic: str | None = None):
        self.index = index
        self.total = total
        self.topic = topic
        message = f"Index {index} out of range (entries={total})"
        super().__init__(message, index=index, total=total, topic=topic)
