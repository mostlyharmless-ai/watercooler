"""EntryEpisodeIndex - Bidirectional Entry-ID ↔ Episode UUID mapping.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 4.1:
- Thread-safe, persistent JSON index
- Atomic file operations
- Located at ~/.watercooler/{backend}/entry_episode_index.json

This index enables cross-tier retrieval by mapping watercooler entry IDs
to Graphiti episode UUIDs, allowing provenance tracking from graph
entities back to original thread entries.

Usage:
    from watercooler_memory.entry_episode_index import EntryEpisodeIndex, IndexConfig

    config = IndexConfig(backend="graphiti")
    index = EntryEpisodeIndex(config)

    # Add mapping when indexing entry as episode
    index.add(entry_id="01ABC123", episode_uuid="01DEF456", thread_id="auth-feature")

    # Lookup in either direction
    episode = index.get_episode("01ABC123")  # -> "01DEF456"
    entry = index.get_entry("01DEF456")      # -> "01ABC123"

    # Persist to disk
    index.save()
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class IndexEntry:
    """A single entry-episode mapping in the index.

    Attributes:
        entry_id: The watercooler entry ID (ULID format)
        episode_uuid: The Graphiti episode UUID
        thread_id: The thread this entry belongs to
        indexed_at: ISO 8601 timestamp when indexed
    """

    entry_id: str
    episode_uuid: str
    thread_id: str
    indexed_at: str = ""

    def __post_init__(self):
        """Set default indexed_at if not provided."""
        if not self.indexed_at:
            self.indexed_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, str]:
        """Serialize to dictionary."""
        return {
            "entry_id": self.entry_id,
            "episode_uuid": self.episode_uuid,
            "thread_id": self.thread_id,
            "indexed_at": self.indexed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> IndexEntry:
        """Deserialize from dictionary."""
        return cls(
            entry_id=d["entry_id"],
            episode_uuid=d["episode_uuid"],
            thread_id=d["thread_id"],
            indexed_at=d.get("indexed_at", ""),
        )


@dataclass
class ChunkEpisodeMapping:
    """Mapping from a chunk to its Graphiti episode.

    Used for tracking chunked entries where a single watercooler entry
    is split into multiple episodes for better LLM processing.

    Attributes:
        chunk_id: SHA256 hash from ChunkNode (unique identifier)
        episode_uuid: The Graphiti episode UUID for this chunk
        entry_id: Parent watercooler entry ID (ULID)
        thread_id: Thread this entry belongs to
        chunk_index: 0-based position within the entry's chunks
        total_chunks: Total number of chunks for the parent entry
        indexed_at: ISO 8601 timestamp when indexed
    """

    chunk_id: str
    episode_uuid: str
    entry_id: str
    thread_id: str
    chunk_index: int
    total_chunks: int
    indexed_at: str = ""

    def __post_init__(self):
        """Set default indexed_at if not provided."""
        if not self.indexed_at:
            self.indexed_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "chunk_id": self.chunk_id,
            "episode_uuid": self.episode_uuid,
            "entry_id": self.entry_id,
            "thread_id": self.thread_id,
            "chunk_index": self.chunk_index,
            "total_chunks": self.total_chunks,
            "indexed_at": self.indexed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChunkEpisodeMapping:
        """Deserialize from dictionary."""
        return cls(
            chunk_id=d["chunk_id"],
            episode_uuid=d["episode_uuid"],
            entry_id=d["entry_id"],
            thread_id=d["thread_id"],
            chunk_index=d["chunk_index"],
            total_chunks=d["total_chunks"],
            indexed_at=d.get("indexed_at", ""),
        )


@dataclass
class IndexConfig:
    """Configuration for EntryEpisodeIndex.

    Attributes:
        backend: The memory backend name (e.g., "graphiti", "leanrag")
        index_path: Custom path for index file. If None, uses default location.
    """

    backend: str = "graphiti"
    index_path: Optional[Path] = None

    def __post_init__(self):
        """Set default index path if not provided."""
        if self.index_path is None:
            base_dir = Path.home() / ".watercooler" / self.backend
            self.index_path = base_dir / "entry_episode_index.json"


class EntryEpisodeIndex:
    """Thread-safe, persistent bidirectional index for entry-episode mappings.

    Provides O(1) lookups in both directions:
    - entry_id -> episode_uuid
    - episode_uuid -> entry_id

    Thread-safety is ensured via a threading.RLock for all operations.
    Persistence uses atomic file operations (write to temp, then rename).

    Example:
        >>> config = IndexConfig(backend="graphiti")
        >>> index = EntryEpisodeIndex(config)
        >>> index.add("entry1", "ep1", "thread-a")
        >>> index.get_episode("entry1")
        'ep1'
        >>> index.save()
    """

    def __init__(self, config: IndexConfig, auto_load: bool = True):
        """Initialize the index.

        Args:
            config: Index configuration
            auto_load: Whether to load existing index on init (default True)
        """
        self._config = config
        self._lock = threading.RLock()

        # Primary storage: entry_id -> IndexEntry
        self._by_entry: dict[str, IndexEntry] = {}

        # Reverse index: episode_uuid -> entry_id
        self._by_episode: dict[str, str] = {}

        # Thread index: thread_id -> set of entry_ids
        self._by_thread: dict[str, set[str]] = {}

        # Chunk storage (for chunked entries)
        # chunk_id -> ChunkEpisodeMapping
        self._by_chunk: dict[str, ChunkEpisodeMapping] = {}
        # entry_id -> list of chunk_ids (ordered by chunk_index)
        self._chunks_by_entry: dict[str, list[str]] = {}

        if auto_load and self._config.index_path and self._config.index_path.exists():
            self.load()

    def __len__(self) -> int:
        """Return number of entries in index."""
        with self._lock:
            return len(self._by_entry)

    @property
    def entry_count(self) -> int:
        """Number of entries in the index."""
        return len(self)

    @property
    def thread_count(self) -> int:
        """Number of unique threads in the index."""
        with self._lock:
            return len(self._by_thread)

    def add(
        self,
        entry_id: str,
        episode_uuid: str,
        thread_id: str,
        indexed_at: Optional[str] = None,
    ) -> IndexEntry:
        """Add or update an entry-episode mapping.

        Args:
            entry_id: The watercooler entry ID
            episode_uuid: The Graphiti episode UUID
            thread_id: The thread this entry belongs to
            indexed_at: Optional timestamp (defaults to now)

        Returns:
            The created or updated IndexEntry
        """
        with self._lock:
            # Remove old mapping if entry existed
            if entry_id in self._by_entry:
                old_entry = self._by_entry[entry_id]
                del self._by_episode[old_entry.episode_uuid]
                self._by_thread[old_entry.thread_id].discard(entry_id)

            # Create new entry
            entry = IndexEntry(
                entry_id=entry_id,
                episode_uuid=episode_uuid,
                thread_id=thread_id,
                indexed_at=indexed_at or "",
            )

            # Update all indices
            self._by_entry[entry_id] = entry
            self._by_episode[episode_uuid] = entry_id

            if thread_id not in self._by_thread:
                self._by_thread[thread_id] = set()
            self._by_thread[thread_id].add(entry_id)

            return entry

    def get_episode(self, entry_id: str) -> Optional[str]:
        """Get episode UUID for an entry ID.

        Args:
            entry_id: The watercooler entry ID

        Returns:
            The episode UUID, or None if not found
        """
        with self._lock:
            entry = self._by_entry.get(entry_id)
            return entry.episode_uuid if entry else None

    def get_entry(self, episode_uuid: str) -> Optional[str]:
        """Get entry ID for an episode UUID.

        Args:
            episode_uuid: The Graphiti episode UUID

        Returns:
            The entry ID, or None if not found
        """
        with self._lock:
            return self._by_episode.get(episode_uuid)

    def get_index_entry(self, entry_id: str) -> Optional[IndexEntry]:
        """Get the full IndexEntry for an entry ID.

        Args:
            entry_id: The watercooler entry ID

        Returns:
            The IndexEntry, or None if not found
        """
        with self._lock:
            return self._by_entry.get(entry_id)

    def get_entries_for_thread(self, thread_id: str) -> list[IndexEntry]:
        """Get all entries for a thread.

        Args:
            thread_id: The thread ID

        Returns:
            List of IndexEntry objects for the thread
        """
        with self._lock:
            entry_ids = self._by_thread.get(thread_id, set())
            return [self._by_entry[eid] for eid in entry_ids if eid in self._by_entry]

    def has_entry(self, entry_id: str) -> bool:
        """Check if an entry ID exists in the index."""
        with self._lock:
            return entry_id in self._by_entry

    def has_any_mapping(self, entry_id: str) -> bool:
        """Check if an entry has any index mapping (non-chunked or chunked).

        Unlike has_entry(), this also checks the chunked-entries index
        (_chunks_by_entry), so it correctly returns True for entries indexed
        via add_chunk_mapping() rather than add(). Use this for dedup guards
        — has_entry() alone is insufficient for chunked entries.

        Args:
            entry_id: The watercooler entry ID to check.

        Returns:
            True if the entry exists in either _by_entry or _chunks_by_entry.
        """
        with self._lock:
            return entry_id in self._by_entry or entry_id in self._chunks_by_entry

    def has_episode(self, episode_uuid: str) -> bool:
        """Check if an episode UUID exists in the index."""
        with self._lock:
            return episode_uuid in self._by_episode

    # --- Chunk mapping methods ---

    @property
    def chunk_count(self) -> int:
        """Number of chunks in the index."""
        with self._lock:
            return len(self._by_chunk)

    def add_chunk_mapping(
        self,
        chunk_id: str,
        episode_uuid: str,
        entry_id: str,
        thread_id: str,
        chunk_index: int,
        total_chunks: int,
        indexed_at: Optional[str] = None,
    ) -> ChunkEpisodeMapping:
        """Add a chunk-to-episode mapping.

        Used when an entry is chunked into multiple episodes.

        Args:
            chunk_id: SHA256 hash identifying the chunk
            episode_uuid: Graphiti episode UUID for this chunk
            entry_id: Parent watercooler entry ID
            thread_id: Thread the entry belongs to
            chunk_index: 0-based position in the entry's chunks
            total_chunks: Total number of chunks for the entry
            indexed_at: Optional timestamp (defaults to now)

        Returns:
            The created ChunkEpisodeMapping
        """
        with self._lock:
            mapping = ChunkEpisodeMapping(
                chunk_id=chunk_id,
                episode_uuid=episode_uuid,
                entry_id=entry_id,
                thread_id=thread_id,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                indexed_at=indexed_at or "",
            )

            self._by_chunk[chunk_id] = mapping
            # Allow reverse provenance lookup for chunked episodes.
            # `wc-provenance` resolves episode_uuid -> entry_id.
            self._by_episode[episode_uuid] = entry_id

            # Maintain ordered list of chunks per entry
            if entry_id not in self._chunks_by_entry:
                self._chunks_by_entry[entry_id] = []

            chunks = self._chunks_by_entry[entry_id]
            # Insert in order by chunk_index
            if chunk_id not in chunks:
                chunks.append(chunk_id)
                # Sort by chunk_index
                chunks.sort(key=lambda cid: self._by_chunk[cid].chunk_index)

            return mapping

    def get_chunks_for_entry(self, entry_id: str) -> list[ChunkEpisodeMapping]:
        """Get all chunk mappings for an entry, ordered by chunk_index.

        Args:
            entry_id: The watercooler entry ID

        Returns:
            List of ChunkEpisodeMapping ordered by chunk_index
        """
        with self._lock:
            chunk_ids = self._chunks_by_entry.get(entry_id, [])
            return [self._by_chunk[cid] for cid in chunk_ids if cid in self._by_chunk]

    def get_episode_for_chunk(self, chunk_id: str) -> Optional[str]:
        """Get the episode UUID for a chunk.

        Args:
            chunk_id: The chunk ID (SHA256 hash)

        Returns:
            The episode UUID, or None if not found
        """
        with self._lock:
            mapping = self._by_chunk.get(chunk_id)
            return mapping.episode_uuid if mapping else None

    def get_chunk_mapping(self, chunk_id: str) -> Optional[ChunkEpisodeMapping]:
        """Get the full ChunkEpisodeMapping for a chunk ID.

        Args:
            chunk_id: The chunk ID (SHA256 hash)

        Returns:
            The ChunkEpisodeMapping, or None if not found
        """
        with self._lock:
            return self._by_chunk.get(chunk_id)

    def has_chunk(self, chunk_id: str) -> bool:
        """Check if a chunk ID exists in the index."""
        with self._lock:
            return chunk_id in self._by_chunk

    def get_episode_uuids_for_entry(self, entry_id: str) -> list[str]:
        """Get all episode UUIDs for an entry's chunks, ordered by chunk_index.

        Useful for building the previous_episode_uuids chain.

        Args:
            entry_id: The watercooler entry ID

        Returns:
            List of episode UUIDs in chunk order
        """
        with self._lock:
            chunks = self.get_chunks_for_entry(entry_id)
            return [c.episode_uuid for c in chunks]

    def remove_chunks_for_entry(self, entry_id: str) -> int:
        """Remove all chunk mappings for an entry.

        Args:
            entry_id: The entry ID whose chunks to remove

        Returns:
            Number of chunks removed
        """
        with self._lock:
            chunk_ids = self._chunks_by_entry.pop(entry_id, [])
            for chunk_id in chunk_ids:
                self._by_chunk.pop(chunk_id, None)
            return len(chunk_ids)

    def remove_by_entry(self, entry_id: str) -> bool:
        """Remove a mapping by entry ID.

        Args:
            entry_id: The entry ID to remove

        Returns:
            True if removed, False if not found
        """
        with self._lock:
            if entry_id not in self._by_entry:
                return False

            entry = self._by_entry.pop(entry_id)
            del self._by_episode[entry.episode_uuid]

            if entry.thread_id in self._by_thread:
                self._by_thread[entry.thread_id].discard(entry_id)
                if not self._by_thread[entry.thread_id]:
                    del self._by_thread[entry.thread_id]

            return True

    def remove_by_episode(self, episode_uuid: str) -> bool:
        """Remove a mapping by episode UUID.

        Args:
            episode_uuid: The episode UUID to remove

        Returns:
            True if removed, False if not found
        """
        with self._lock:
            if episode_uuid not in self._by_episode:
                return False

            entry_id = self._by_episode.pop(episode_uuid)
            entry = self._by_entry.pop(entry_id)

            if entry.thread_id in self._by_thread:
                self._by_thread[entry.thread_id].discard(entry_id)
                if not self._by_thread[entry.thread_id]:
                    del self._by_thread[entry.thread_id]

            return True

    def clear(self) -> None:
        """Clear all entries from the index."""
        with self._lock:
            self._by_entry.clear()
            self._by_episode.clear()
            self._by_thread.clear()
            self._by_chunk.clear()
            self._chunks_by_entry.clear()

    def get_stats(self) -> dict[str, Any]:
        """Get index statistics.

        Returns:
            Dictionary with entry_count, thread_count, chunk_count,
            chunked_entries_count, and threads list
        """
        with self._lock:
            return {
                "entry_count": len(self._by_entry),
                "thread_count": len(self._by_thread),
                "chunk_count": len(self._by_chunk),
                "chunked_entries_count": len(self._chunks_by_entry),
                "threads": list(self._by_thread.keys()),
            }

    def save(self) -> None:
        """Save index to disk atomically.

        Creates parent directories if needed. Uses atomic write
        (write to temp file, then rename) to prevent corruption.

        Version 2 format includes chunk mappings for chunked entries.
        """
        with self._lock:
            # Prepare data - v2 includes chunks
            data = {
                "version": 2,
                "backend": self._config.backend,
                "entries": [e.to_dict() for e in self._by_entry.values()],
                "chunks": [c.to_dict() for c in self._by_chunk.values()],
            }

            # Ensure parent directory exists
            index_path = self._config.index_path
            if index_path is None:
                raise ValueError("index_path not configured")

            index_path.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write: write to temp, then rename
            fd, temp_path = tempfile.mkstemp(
                dir=index_path.parent,
                prefix=".entry_episode_index_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(temp_path, index_path)
            except Exception:
                # Clean up temp file on failure
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise

    def load(self) -> None:
        """Load index from disk.

        Handles both v1 (entries only) and v2 (entries + chunks) formats.

        Raises:
            json.JSONDecodeError: If file contains invalid JSON
            FileNotFoundError: If file doesn't exist
        """
        with self._lock:
            index_path = self._config.index_path
            if index_path is None or not index_path.exists():
                return

            with open(index_path) as f:
                data = json.load(f)

            # Clear current state (all indices including chunks)
            self._by_entry.clear()
            self._by_episode.clear()
            self._by_thread.clear()
            self._by_chunk.clear()
            self._chunks_by_entry.clear()

            # Load entries
            for entry_dict in data.get("entries", []):
                entry = IndexEntry.from_dict(entry_dict)
                self._by_entry[entry.entry_id] = entry
                self._by_episode[entry.episode_uuid] = entry.entry_id

                if entry.thread_id not in self._by_thread:
                    self._by_thread[entry.thread_id] = set()
                self._by_thread[entry.thread_id].add(entry.entry_id)

            # Load chunks (v2 format - backward compatible with v1)
            for chunk_dict in data.get("chunks", []):
                mapping = ChunkEpisodeMapping.from_dict(chunk_dict)
                self._by_chunk[mapping.chunk_id] = mapping

                # Maintain ordered list by entry
                if mapping.entry_id not in self._chunks_by_entry:
                    self._chunks_by_entry[mapping.entry_id] = []
                if mapping.chunk_id not in self._chunks_by_entry[mapping.entry_id]:
                    self._chunks_by_entry[mapping.entry_id].append(mapping.chunk_id)

            # Sort chunks by index after loading
            for entry_id, chunk_ids in self._chunks_by_entry.items():
                chunk_ids.sort(key=lambda cid: self._by_chunk[cid].chunk_index)
