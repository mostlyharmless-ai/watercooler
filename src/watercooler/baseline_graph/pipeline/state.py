"""State management for incremental pipeline builds.

Tracks thread modification times and cached summaries/embeddings
to enable efficient incremental updates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ThreadState:
    """State for a single thread."""

    topic: str
    mtime: float  # File modification time
    entry_count: int
    summary: str = ""
    entry_summaries: Dict[str, str] = field(default_factory=dict)  # entry_id -> summary
    entry_embeddings: Dict[str, List[float]] = field(default_factory=dict)  # entry_id -> embedding


@dataclass
class PipelineState:
    """State for the entire pipeline."""

    version: str = "1.0"
    last_run: str = ""
    threads: Dict[str, ThreadState] = field(default_factory=dict)  # topic -> ThreadState

    @classmethod
    def load(cls, state_path: Path) -> "PipelineState":
        """Load state from file.

        Args:
            state_path: Path to state.json file

        Returns:
            Loaded state or empty state if file doesn't exist
        """
        if not state_path.exists():
            return cls()

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            threads = {}
            for topic, thread_data in data.get("threads", {}).items():
                threads[topic] = ThreadState(
                    topic=thread_data.get("topic", topic),
                    mtime=thread_data.get("mtime", 0),
                    entry_count=thread_data.get("entry_count", 0),
                    summary=thread_data.get("summary", ""),
                    entry_summaries=thread_data.get("entry_summaries", {}),
                    entry_embeddings=thread_data.get("entry_embeddings", {}),
                )

            return cls(
                version=data.get("version", "1.0"),
                last_run=data.get("last_run", ""),
                threads=threads,
            )
        except (json.JSONDecodeError, KeyError):
            return cls()

    def save(self, state_path: Path) -> None:
        """Save state to file.

        Args:
            state_path: Path to state.json file
        """
        state_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self.version,
            "last_run": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "threads": {
                topic: asdict(thread_state)
                for topic, thread_state in self.threads.items()
            },
        }

        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def is_thread_changed(self, topic: str, current_mtime: float, current_entry_count: int) -> bool:
        """Check if a thread has changed since last run.

        Args:
            topic: Thread topic
            current_mtime: Current file modification time
            current_entry_count: Current number of entries

        Returns:
            True if thread is new or has changed
        """
        if topic not in self.threads:
            return True

        cached = self.threads[topic]
        return (
            cached.mtime != current_mtime or
            cached.entry_count != current_entry_count
        )

    def get_cached_summary(self, topic: str) -> Optional[str]:
        """Get cached thread summary if available."""
        if topic in self.threads:
            return self.threads[topic].summary or None
        return None

    def get_cached_entry_summary(self, topic: str, entry_id: str) -> Optional[str]:
        """Get cached entry summary if available."""
        if topic in self.threads:
            return self.threads[topic].entry_summaries.get(entry_id)
        return None

    def get_cached_entry_embedding(self, topic: str, entry_id: str) -> Optional[List[float]]:
        """Get cached entry embedding if available."""
        if topic in self.threads:
            return self.threads[topic].entry_embeddings.get(entry_id)
        return None

    def update_thread(
        self,
        topic: str,
        mtime: float,
        entry_count: int,
        summary: str = "",
        entry_summaries: Optional[Dict[str, str]] = None,
        entry_embeddings: Optional[Dict[str, List[float]]] = None,
    ) -> None:
        """Update state for a thread.

        Args:
            topic: Thread topic
            mtime: File modification time
            entry_count: Number of entries
            summary: Thread summary
            entry_summaries: Entry summaries (entry_id -> summary)
            entry_embeddings: Entry embeddings (entry_id -> embedding)
        """
        self.threads[topic] = ThreadState(
            topic=topic,
            mtime=mtime,
            entry_count=entry_count,
            summary=summary,
            entry_summaries=entry_summaries or {},
            entry_embeddings=entry_embeddings or {},
        )

    def remove_deleted_threads(self, current_topics: set) -> List[str]:
        """Remove threads that no longer exist.

        Args:
            current_topics: Set of currently existing thread topics

        Returns:
            List of removed topics
        """
        removed = []
        for topic in list(self.threads.keys()):
            if topic not in current_topics:
                del self.threads[topic]
                removed.append(topic)
        return removed
