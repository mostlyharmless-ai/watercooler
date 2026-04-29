"""Resumable checkpoint helper for migrations.

Append-only JSONL of completed entry_ids. Used by all migrate-* flows so
a Ctrl-C / network blip / OOM kill mid-run can resume without
re-pushing what's already landed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Set


class Checkpoint:
    """Append-only entry_id checkpoint with O(1) membership check."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._seen: Set[str] = self._load()

    def _load(self) -> Set[str]:
        if not self.path.exists():
            return set()
        out: Set[str] = set()
        with open(self.path) as fh:
            for line in fh:
                v = line.strip()
                if v:
                    out.add(v)
        return out

    def __contains__(self, entry_id: str) -> bool:
        return entry_id in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, entry_id: str) -> None:
        if entry_id in self._seen:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(entry_id + "\n")
        self._seen.add(entry_id)

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self._seen = set()
