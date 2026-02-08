"""Exception hierarchy for memory task queue operations.

All memory-queue exceptions inherit from MemoryQueueError, which provides:
- context: Dict of contextual information for debugging
- recovery_hint: Human-readable suggestion for fixing the issue
- is_retryable: Whether the operation can be retried

Mirrors the pattern from sync/errors.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class MemoryQueueError(Exception):
    """Base exception for all memory queue operations.

    Attributes:
        message: Human-readable error description
        context: Dict of contextual information (task_id, backend, etc.)
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
class BackendUnavailableError(MemoryQueueError):
    """Memory backend is not reachable (FalkorDB down, API timeout, etc.)."""

    is_retryable: bool = True


@dataclass
class TaskNotFoundError(MemoryQueueError):
    """Requested task ID does not exist in the queue."""

    is_retryable: bool = False


@dataclass
class DuplicateTaskError(MemoryQueueError):
    """An equivalent task is already pending or running."""

    existing_task_id: Optional[str] = None
    is_retryable: bool = False


@dataclass
class QueueFullError(MemoryQueueError):
    """Queue has reached maximum depth; caller should retry after tasks drain."""

    is_retryable: bool = True


@dataclass
class CheckpointError(MemoryQueueError):
    """Error saving or loading checkpoint state."""

    checkpoint_path: Optional[str] = None
    is_retryable: bool = True  # Filesystem issues are often transient
