"""Checkpoint system for bulk memory indexing jobs.

Adapted from scripts/index_graphiti.py checkpoint infrastructure.
Provides atomic per-chunk progress tracking that survives interruptions.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EntryProgress:
    """Progress tracking for a single entry within a bulk job.

    Attributes:
        entry_id: Watercooler entry ULID.
        topic: Thread topic slug.
        status: ``pending`` | ``in_progress`` | ``complete``.
        episode_uuid: Result UUID (populated on success).
        error: Last error message (if failed).
        updated_at: ISO timestamp of last state change.
    """

    entry_id: str = ""
    topic: str = ""
    status: str = "pending"
    episode_uuid: str = ""
    error: str = ""
    updated_at: str = ""


@dataclass
class BulkCheckpoint:
    """Persistent checkpoint for a bulk indexing job.

    Saved atomically to disk after each entry so the job can resume
    from the last successful point after interruption.

    Attributes:
        task_id: Parent MemoryTask ID.
        backend: Target backend (``graphiti``, ``leanrag``).
        total_entries: Total entries in the manifest.
        completed_entries: Number of entries completed so far.
        entries: Per-entry progress keyed by entry_id.
        created_at: ISO timestamp.
        updated_at: ISO timestamp.
    """

    task_id: str = ""
    backend: str = "graphiti"
    total_entries: int = 0
    completed_entries: int = 0
    entries: Dict[str, EntryProgress] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    # ------------------------------------------------------------------ #
    # Query helpers
    # ------------------------------------------------------------------ #

    def is_entry_complete(self, entry_id: str) -> bool:
        ep = self.entries.get(entry_id)
        return ep is not None and ep.status == "complete"

    def next_pending(self) -> Optional[str]:
        """Return the entry_id of the first non-complete entry, or None."""
        for eid, ep in self.entries.items():
            if ep.status != "complete":
                return eid
        return None

    def mark_entry_started(self, entry_id: str, topic: str = "") -> None:
        if entry_id not in self.entries:
            self.entries[entry_id] = EntryProgress(entry_id=entry_id, topic=topic)
        self.entries[entry_id].status = "in_progress"
        self.entries[entry_id].updated_at = _now_iso()

    def mark_entry_complete(self, entry_id: str, episode_uuid: str = "") -> None:
        if entry_id not in self.entries:
            self.entries[entry_id] = EntryProgress(entry_id=entry_id)
        ep = self.entries[entry_id]
        ep.status = "complete"
        ep.episode_uuid = episode_uuid
        ep.updated_at = _now_iso()
        self.completed_entries = sum(
            1 for e in self.entries.values() if e.status == "complete"
        )
        self.updated_at = _now_iso()

    def mark_entry_failed(self, entry_id: str, error: str) -> None:
        if entry_id not in self.entries:
            self.entries[entry_id] = EntryProgress(entry_id=entry_id)
        ep = self.entries[entry_id]
        ep.status = "failed"
        ep.error = error
        ep.updated_at = _now_iso()
        self.updated_at = _now_iso()

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # entries is a dict of dataclass → convert inner values too
        d["entries"] = {k: asdict(v) for k, v in self.entries.items()}
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BulkCheckpoint":
        entries = {}
        for k, v in data.get("entries", {}).items():
            entries[k] = EntryProgress(**{
                fld: v.get(fld, "")
                for fld in EntryProgress.__dataclass_fields__
            })
        return cls(
            task_id=data.get("task_id", ""),
            backend=data.get("backend", "graphiti"),
            total_entries=data.get("total_entries", 0),
            completed_entries=data.get("completed_entries", 0),
            entries=entries,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


# ------------------------------------------------------------------ #
# File I/O (atomic save/load following index_graphiti.py pattern)
# ------------------------------------------------------------------ #


def save_checkpoint(checkpoint: BulkCheckpoint, path: Path) -> None:
    """Atomically persist checkpoint to *path* (temp + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".ckpt.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(checkpoint.to_dict(), f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_checkpoint(path: Path) -> Optional[BulkCheckpoint]:
    """Load checkpoint from *path*, or return None if absent / corrupt."""
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return BulkCheckpoint.from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError) as e:
        logger.warning("MEMORY_QUEUE: corrupt checkpoint at %s: %s", path, e)
        return None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
