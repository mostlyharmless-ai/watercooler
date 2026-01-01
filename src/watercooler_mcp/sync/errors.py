"""Rich exception hierarchy for sync operations.

All sync-related exceptions inherit from SyncError, which provides:
- context: Dict of contextual information for debugging
- recovery_hint: Human-readable suggestion for fixing the issue
- is_retryable: Whether the operation can be retried

This enables consistent error handling and informative error messages
across the sync package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SyncError(Exception):
    """Base exception for all sync operations.

    Attributes:
        message: Human-readable error description
        context: Dict of contextual information (repo path, branch, etc.)
        recovery_hint: Suggested action to resolve the issue
        is_retryable: Whether the operation can be retried
        cause: Original exception that caused this error
    """

    message: str
    context: Dict[str, Any] = field(default_factory=dict)
    recovery_hint: Optional[str] = None
    is_retryable: bool = False
    cause: Optional[Exception] = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:
        parts = [self.message]
        if self.context:
            ctx_str = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            parts.append(f"[{ctx_str}]")
        if self.recovery_hint:
            parts.append(f"Hint: {self.recovery_hint}")
        return " ".join(parts)

    def with_context(self, **kwargs: Any) -> "SyncError":
        """Return a new error with additional context."""
        new_context = {**self.context, **kwargs}
        return type(self)(
            message=self.message,
            context=new_context,
            recovery_hint=self.recovery_hint,
            is_retryable=self.is_retryable,
            cause=self.cause,
        )


@dataclass
class PullError(SyncError):
    """Error during git pull operation.

    Common causes:
    - Network connectivity issues
    - Authentication failures
    - Merge conflicts
    - Diverged history
    """

    is_retryable: bool = True  # Most pull errors are transient


@dataclass
class PushError(SyncError):
    """Error during git push operation.

    Common causes:
    - Network connectivity issues
    - Authentication failures
    - Remote rejected (non-fast-forward)
    - Branch protection rules
    """

    is_retryable: bool = True  # Most push errors are transient


@dataclass
class ConflictError(SyncError):
    """Error due to merge/rebase conflicts.

    Attributes:
        conflicting_files: List of files with conflicts
        conflict_type: Type of conflict (merge, rebase, cherry-pick)
    """

    conflicting_files: list = field(default_factory=list)
    conflict_type: str = "merge"
    is_retryable: bool = False  # Conflicts require manual resolution

    def __str__(self) -> str:
        base = super().__str__()
        if self.conflicting_files:
            files = ", ".join(self.conflicting_files[:5])
            if len(self.conflicting_files) > 5:
                files += f" (+{len(self.conflicting_files) - 5} more)"
            return f"{base} Conflicting files: {files}"
        return base


@dataclass
class BranchPairingError(SyncError):
    """Error in branch parity between code and threads repos.

    Common causes:
    - Branch name mismatch
    - Orphan branch (threads exists, code deleted)
    - Main protection violation
    - Detached HEAD state
    """

    code_branch: Optional[str] = None
    threads_branch: Optional[str] = None
    is_retryable: bool = False  # Usually requires user action


@dataclass
class LockError(SyncError):
    """Error acquiring or releasing advisory lock.

    Common causes:
    - Another process holds the lock
    - Lock file permissions
    - Stale lock from crashed process
    """

    lock_path: Optional[str] = None
    lock_holder: Optional[str] = None
    is_retryable: bool = True  # Can retry after timeout


@dataclass
class NetworkError(SyncError):
    """Error due to network connectivity issues.

    Common causes:
    - No internet connection
    - DNS resolution failure
    - SSH connection timeout
    - Remote server unreachable
    """

    remote: Optional[str] = None
    is_retryable: bool = True  # Network issues are often transient


@dataclass
class AuthenticationError(SyncError):
    """Error due to authentication failure.

    Common causes:
    - Expired SSH key
    - Invalid credentials
    - Token revoked
    - Insufficient permissions
    """

    remote: Optional[str] = None
    is_retryable: bool = False  # Auth issues require user action

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.recovery_hint:
            self.recovery_hint = (
                "Check your SSH keys or GitHub token. "
                "Run 'ssh -T git@github.com' to test SSH connectivity."
            )
