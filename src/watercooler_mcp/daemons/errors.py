"""Exception hierarchy for daemon management.

All daemon exceptions inherit from DaemonError, following the pattern
established in memory_queue/errors.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class DaemonError(Exception):
    """Base exception for daemon operations.

    Attributes:
        message: Human-readable error description
        context: Dict of contextual information (daemon_name, etc.)
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


@dataclass
class DaemonNotFoundError(DaemonError):
    """Requested daemon does not exist in the manager."""

    pass


@dataclass
class DaemonAlreadyRegisteredError(DaemonError):
    """A daemon with the same name is already registered."""

    pass


@dataclass
class DaemonLifecycleError(DaemonError):
    """Daemon is in a state that prevents the requested operation.

    Note: is_retryable overridden to True — lifecycle errors are often transient.
    """

    is_retryable: bool = True


@dataclass
class DaemonCheckpointError(DaemonError):
    """Error saving or loading daemon checkpoint.

    Note: is_retryable overridden to True — checkpoint I/O errors are often transient.
    """

    checkpoint_path: Optional[str] = None
    is_retryable: bool = True
