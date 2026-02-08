"""MemoryTask dataclass — primary unit of work for the memory queue.

Modelled after PendingCommit in sync/async_coordinator.py with additional
fields for retry tracking, backend targeting, and bulk job support.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(str, Enum):
    """Lifecycle states for a memory task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class TaskType(str, Enum):
    """Discriminator for single vs bulk tasks."""

    SINGLE = "single"
    BULK = "bulk"


def _generate_task_id() -> str:
    """Generate a ULID-like task identifier.

    Uses a compact timestamp+random hex string.  Not a true ULID but
    sufficient for ordering and uniqueness within a single-process queue.
    """
    import secrets

    ts = int(time.time() * 1000)
    rand = secrets.token_hex(5)
    return f"{ts:013x}{rand}"


@dataclass
class MemoryTask:
    """Primary unit of work for the memory task queue.

    Attributes:
        task_id: Unique identifier (ULID-like).
        task_type: ``single`` for one entry, ``bulk`` for a manifest.
        backend: Target backend name (``graphiti``, ``leanrag``).
        status: Current lifecycle state.

        entry_id: Watercooler entry ULID (single tasks).
        topic: Thread topic slug.
        group_id: Backend partition key (derived from code_path).
        content: Entry body / episode content.
        title: Entry title (used as episode name).
        timestamp: ISO-8601 reference time.
        source_description: Provenance label for the episode.

        attempt: Current retry attempt (1-based).
        max_attempts: Maximum retries before dead-lettering.
        next_retry_at: Unix timestamp for next eligible retry.
        last_error: Most recent error message.

        episode_uuid: Result UUID after successful ingestion.
        entities_extracted: Entity names returned by backend.
        facts_extracted: Fact/edge count returned by backend.

        bulk_manifest: List of ``{entry_id, topic, ...}`` for bulk tasks.
        bulk_progress: Number of manifest items completed.

        created_at: Unix timestamp when task was created.
        updated_at: Unix timestamp of last state change.
    """

    task_id: str = field(default_factory=_generate_task_id)
    task_type: str = TaskType.SINGLE
    backend: str = "graphiti"
    status: str = TaskStatus.PENDING

    # Entry data (single tasks)
    entry_id: str = ""
    topic: str = ""
    group_id: str = ""
    content: str = ""
    title: str = ""
    timestamp: str = ""
    source_description: str = ""

    # Retry tracking
    attempt: int = 0
    max_attempts: int = 3
    next_retry_at: float = 0.0
    last_error: str = ""

    # Result data (populated on success)
    episode_uuid: str = ""
    entities_extracted: List[str] = field(default_factory=list)
    facts_extracted: int = 0

    # Bulk job support
    bulk_manifest: List[Dict[str, Any]] = field(default_factory=list)
    bulk_progress: int = 0

    # Timestamps
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serialisable dictionary."""
        return asdict(self)

    def to_json_line(self) -> str:
        """Serialise as a single JSONL line (no trailing newline)."""
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryTask":
        """Reconstruct from a dictionary (lenient: ignores unknown keys)."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    @classmethod
    def from_json_line(cls, line: str) -> "MemoryTask":
        """Parse a single JSONL line."""
        return cls.from_dict(json.loads(line))

    # ------------------------------------------------------------------ #
    # State helpers
    # ------------------------------------------------------------------ #

    @property
    def is_terminal(self) -> bool:
        """True if the task will not be processed further."""
        return self.status in (TaskStatus.COMPLETED, TaskStatus.DEAD_LETTER)

    @property
    def is_ready(self) -> bool:
        """True if the task can be picked up by the worker now."""
        if self.status != TaskStatus.PENDING:
            return False
        if self.next_retry_at and time.time() < self.next_retry_at:
            return False
        return True

    def mark_running(self) -> None:
        """Transition to RUNNING."""
        self.status = TaskStatus.RUNNING
        self.attempt += 1
        self.updated_at = time.time()

    def mark_completed(
        self,
        episode_uuid: str = "",
        entities: Optional[List[str]] = None,
        facts: int = 0,
    ) -> None:
        """Transition to COMPLETED with optional result data."""
        self.status = TaskStatus.COMPLETED
        self.episode_uuid = episode_uuid
        if entities is not None:
            self.entities_extracted = entities
        self.facts_extracted = facts
        self.updated_at = time.time()

    def mark_failed(self, error: str, *, backoff_base: float = 30.0) -> None:
        """Transition to FAILED or DEAD_LETTER depending on retry budget.

        Backoff: ``backoff_base * 2^(attempt-1)`` seconds, capped at 10 min.
        A small jitter (±10 %) is added to avoid thundering-herd.
        """
        import random

        self.last_error = error
        self.updated_at = time.time()

        if self.attempt >= self.max_attempts:
            self.status = TaskStatus.DEAD_LETTER
        else:
            self.status = TaskStatus.PENDING  # Will be retried
            delay = min(backoff_base * (2 ** (self.attempt - 1)), 600.0)
            jitter = delay * 0.1 * (2 * random.random() - 1)
            self.next_retry_at = time.time() + delay + jitter

    def dedup_key(self) -> str:
        """Key used for deduplication: ``(entry_id, backend)``."""
        return f"{self.entry_id}:{self.backend}"
