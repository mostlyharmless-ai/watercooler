"""Tests for EntryEpisodeIndex - bidirectional Entry-ID ↔ Episode UUID mapping.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 4.1:
- Thread-safe, persistent JSON index
- Atomic file operations
- Located at ~/.watercooler/{backend}/entry_episode_index.json
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from watercooler_memory.entry_episode_index import (
    EntryEpisodeIndex,
    IndexEntry,
    IndexConfig,
)


class TestIndexEntry:
    """Test IndexEntry dataclass."""

    def test_create_index_entry(self):
        """Test creating an index entry."""
        entry = IndexEntry(
            entry_id="01ABC123",
            episode_uuid="01DEF456",
            thread_id="test-thread",
            indexed_at="2025-01-15T10:00:00Z",
        )
        assert entry.entry_id == "01ABC123"
        assert entry.episode_uuid == "01DEF456"
        assert entry.thread_id == "test-thread"
        assert entry.indexed_at == "2025-01-15T10:00:00Z"

    def test_index_entry_to_dict(self):
        """Test serializing index entry to dict."""
        entry = IndexEntry(
            entry_id="01ABC123",
            episode_uuid="01DEF456",
            thread_id="test-thread",
            indexed_at="2025-01-15T10:00:00Z",
        )
        d = entry.to_dict()
        assert d["entry_id"] == "01ABC123"
        assert d["episode_uuid"] == "01DEF456"
        assert d["thread_id"] == "test-thread"
        assert d["indexed_at"] == "2025-01-15T10:00:00Z"

    def test_index_entry_from_dict(self):
        """Test deserializing index entry from dict."""
        d = {
            "entry_id": "01ABC123",
            "episode_uuid": "01DEF456",
            "thread_id": "test-thread",
            "indexed_at": "2025-01-15T10:00:00Z",
        }
        entry = IndexEntry.from_dict(d)
        assert entry.entry_id == "01ABC123"
        assert entry.episode_uuid == "01DEF456"
        assert entry.thread_id == "test-thread"


class TestIndexConfig:
    """Test IndexConfig configuration."""

    def test_default_config(self):
        """Test default configuration."""
        config = IndexConfig()
        assert config.backend == "graphiti"
        assert "entry_episode_index.json" in str(config.index_path)

    def test_custom_backend(self):
        """Test custom backend name in config."""
        config = IndexConfig(backend="leanrag")
        assert config.backend == "leanrag"
        assert "leanrag" in str(config.index_path)

    def test_custom_index_path(self, tmp_path: Path):
        """Test custom index path."""
        custom_path = tmp_path / "custom_index.json"
        config = IndexConfig(index_path=custom_path)
        assert config.index_path == custom_path


class TestEntryEpisodeIndexBasicOperations:
    """Test basic index operations."""

    @pytest.fixture
    def index_path(self, tmp_path: Path) -> Path:
        """Provide a temporary index path."""
        return tmp_path / "test_index.json"

    @pytest.fixture
    def index(self, index_path: Path) -> EntryEpisodeIndex:
        """Create a fresh index for testing."""
        config = IndexConfig(index_path=index_path)
        return EntryEpisodeIndex(config)

    def test_create_empty_index(self, index: EntryEpisodeIndex):
        """Test creating an empty index."""
        assert len(index) == 0
        assert index.entry_count == 0

    def test_add_mapping(self, index: EntryEpisodeIndex):
        """Test adding an entry-episode mapping."""
        index.add(
            entry_id="01ABC123",
            episode_uuid="01DEF456",
            thread_id="test-thread",
        )
        assert len(index) == 1
        assert index.get_episode("01ABC123") == "01DEF456"
        assert index.get_entry("01DEF456") == "01ABC123"

    def test_add_duplicate_entry(self, index: EntryEpisodeIndex):
        """Test adding duplicate entry updates the mapping."""
        index.add(
            entry_id="01ABC123",
            episode_uuid="01DEF456",
            thread_id="test-thread",
        )
        # Add same entry with different episode
        index.add(
            entry_id="01ABC123",
            episode_uuid="01NEW789",
            thread_id="test-thread",
        )
        # Should update to new episode
        assert index.get_episode("01ABC123") == "01NEW789"

    def test_get_nonexistent_entry(self, index: EntryEpisodeIndex):
        """Test getting a nonexistent entry returns None."""
        assert index.get_episode("nonexistent") is None
        assert index.get_entry("nonexistent") is None

    def test_remove_by_entry_id(self, index: EntryEpisodeIndex):
        """Test removing a mapping by entry ID."""
        index.add(
            entry_id="01ABC123",
            episode_uuid="01DEF456",
            thread_id="test-thread",
        )
        removed = index.remove_by_entry("01ABC123")
        assert removed is True
        assert len(index) == 0
        assert index.get_episode("01ABC123") is None

    def test_remove_by_episode_uuid(self, index: EntryEpisodeIndex):
        """Test removing a mapping by episode UUID."""
        index.add(
            entry_id="01ABC123",
            episode_uuid="01DEF456",
            thread_id="test-thread",
        )
        removed = index.remove_by_episode("01DEF456")
        assert removed is True
        assert len(index) == 0
        assert index.get_entry("01DEF456") is None

    def test_remove_nonexistent(self, index: EntryEpisodeIndex):
        """Test removing nonexistent entry returns False."""
        removed = index.remove_by_entry("nonexistent")
        assert removed is False

    def test_get_entries_for_thread(self, index: EntryEpisodeIndex):
        """Test getting all entries for a thread."""
        index.add("entry1", "ep1", "thread-a")
        index.add("entry2", "ep2", "thread-a")
        index.add("entry3", "ep3", "thread-b")

        thread_a_entries = index.get_entries_for_thread("thread-a")
        assert len(thread_a_entries) == 2
        entry_ids = [e.entry_id for e in thread_a_entries]
        assert "entry1" in entry_ids
        assert "entry2" in entry_ids

    def test_contains_entry(self, index: EntryEpisodeIndex):
        """Test checking if entry exists."""
        index.add("01ABC123", "01DEF456", "test-thread")
        assert index.has_entry("01ABC123") is True
        assert index.has_entry("nonexistent") is False

    def test_contains_episode(self, index: EntryEpisodeIndex):
        """Test checking if episode exists."""
        index.add("01ABC123", "01DEF456", "test-thread")
        assert index.has_episode("01DEF456") is True
        assert index.has_episode("nonexistent") is False


class TestEntryEpisodeIndexPersistence:
    """Test index persistence to disk."""

    @pytest.fixture
    def index_path(self, tmp_path: Path) -> Path:
        """Provide a temporary index path."""
        return tmp_path / "test_index.json"

    def test_save_and_load(self, index_path: Path):
        """Test saving and loading index."""
        # Create and populate index
        config = IndexConfig(index_path=index_path)
        index1 = EntryEpisodeIndex(config)
        index1.add("entry1", "ep1", "thread-a")
        index1.add("entry2", "ep2", "thread-b")
        index1.save()

        # Verify file exists
        assert index_path.exists()

        # Load into new index instance
        index2 = EntryEpisodeIndex(config)
        index2.load()

        assert len(index2) == 2
        assert index2.get_episode("entry1") == "ep1"
        assert index2.get_episode("entry2") == "ep2"

    def test_auto_load_on_init(self, index_path: Path):
        """Test index auto-loads existing file on init."""
        config = IndexConfig(index_path=index_path)

        # Create and save index
        index1 = EntryEpisodeIndex(config)
        index1.add("entry1", "ep1", "thread-a")
        index1.save()

        # Create new instance with auto_load=True (default)
        index2 = EntryEpisodeIndex(config, auto_load=True)
        assert len(index2) == 1
        assert index2.get_episode("entry1") == "ep1"

    def test_load_nonexistent_file(self, tmp_path: Path):
        """Test loading nonexistent file returns empty index."""
        config = IndexConfig(index_path=tmp_path / "nonexistent.json")
        index = EntryEpisodeIndex(config, auto_load=True)
        assert len(index) == 0

    def test_load_corrupted_file(self, index_path: Path):
        """Test loading corrupted file raises error."""
        # Write invalid JSON
        index_path.write_text("not valid json {{{")

        config = IndexConfig(index_path=index_path)
        index = EntryEpisodeIndex(config, auto_load=False)
        with pytest.raises(json.JSONDecodeError):
            index.load()

    def test_atomic_save(self, index_path: Path):
        """Test save is atomic (writes to temp file first)."""
        config = IndexConfig(index_path=index_path)
        index = EntryEpisodeIndex(config)
        index.add("entry1", "ep1", "thread-a")

        # Save should create file
        index.save()
        assert index_path.exists()

        # Content should be valid JSON
        content = json.loads(index_path.read_text())
        assert "entries" in content
        assert len(content["entries"]) == 1

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        """Test save creates parent directories if needed."""
        nested_path = tmp_path / "deep" / "nested" / "index.json"
        config = IndexConfig(index_path=nested_path)
        index = EntryEpisodeIndex(config)
        index.add("entry1", "ep1", "thread-a")
        index.save()

        assert nested_path.exists()


class TestEntryEpisodeIndexThreadSafety:
    """Test thread-safety of index operations."""

    @pytest.fixture
    def index_path(self, tmp_path: Path) -> Path:
        """Provide a temporary index path."""
        return tmp_path / "test_index.json"

    @pytest.fixture
    def index(self, index_path: Path) -> EntryEpisodeIndex:
        """Create a fresh index for testing."""
        config = IndexConfig(index_path=index_path)
        return EntryEpisodeIndex(config)

    def test_concurrent_adds(self, index: EntryEpisodeIndex):
        """Test concurrent add operations are thread-safe."""
        num_threads = 10
        entries_per_thread = 100

        def add_entries(thread_id: int):
            for i in range(entries_per_thread):
                index.add(
                    entry_id=f"entry-{thread_id}-{i}",
                    episode_uuid=f"ep-{thread_id}-{i}",
                    thread_id=f"thread-{thread_id}",
                )

        threads = [
            threading.Thread(target=add_entries, args=(i,))
            for i in range(num_threads)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All entries should be present
        expected_count = num_threads * entries_per_thread
        assert len(index) == expected_count

    def test_concurrent_read_write(self, index: EntryEpisodeIndex):
        """Test concurrent read and write operations."""
        # Pre-populate some entries
        for i in range(100):
            index.add(f"entry-{i}", f"ep-{i}", "thread-0")

        errors = []

        def reader():
            for _ in range(100):
                try:
                    index.get_episode("entry-50")
                    index.get_entry("ep-50")
                except Exception as e:
                    errors.append(e)

        def writer():
            for i in range(100):
                try:
                    index.add(f"new-entry-{i}", f"new-ep-{i}", "thread-1")
                except Exception as e:
                    errors.append(e)

        reader_threads = [threading.Thread(target=reader) for _ in range(5)]
        writer_threads = [threading.Thread(target=writer) for _ in range(2)]

        all_threads = reader_threads + writer_threads
        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join()

        # No errors should have occurred
        assert len(errors) == 0

    def test_concurrent_save_load(self, index_path: Path):
        """Test concurrent save operations don't corrupt file."""
        config = IndexConfig(index_path=index_path)
        index = EntryEpisodeIndex(config)

        # Pre-populate
        for i in range(100):
            index.add(f"entry-{i}", f"ep-{i}", "thread-0")

        errors = []

        def save_repeatedly():
            for _ in range(20):
                try:
                    index.save()
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=save_repeatedly) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No errors and file should be valid
        assert len(errors) == 0
        assert index_path.exists()

        # Should be loadable
        loaded = json.loads(index_path.read_text())
        assert "entries" in loaded


class TestEntryEpisodeIndexMetadata:
    """Test index metadata and statistics."""

    @pytest.fixture
    def index_path(self, tmp_path: Path) -> Path:
        """Provide a temporary index path."""
        return tmp_path / "test_index.json"

    @pytest.fixture
    def index(self, index_path: Path) -> EntryEpisodeIndex:
        """Create a fresh index for testing."""
        config = IndexConfig(index_path=index_path)
        return EntryEpisodeIndex(config)

    def test_entry_count(self, index: EntryEpisodeIndex):
        """Test entry count property."""
        assert index.entry_count == 0
        index.add("entry1", "ep1", "thread-a")
        assert index.entry_count == 1
        index.add("entry2", "ep2", "thread-a")
        assert index.entry_count == 2

    def test_thread_count(self, index: EntryEpisodeIndex):
        """Test counting unique threads."""
        index.add("entry1", "ep1", "thread-a")
        index.add("entry2", "ep2", "thread-a")
        index.add("entry3", "ep3", "thread-b")

        assert index.thread_count == 2

    def test_get_stats(self, index: EntryEpisodeIndex):
        """Test getting index statistics."""
        index.add("entry1", "ep1", "thread-a")
        index.add("entry2", "ep2", "thread-a")
        index.add("entry3", "ep3", "thread-b")

        stats = index.get_stats()
        assert stats["entry_count"] == 3
        assert stats["thread_count"] == 2
        assert "thread-a" in stats["threads"]
        assert "thread-b" in stats["threads"]

    def test_clear(self, index: EntryEpisodeIndex):
        """Test clearing the index."""
        index.add("entry1", "ep1", "thread-a")
        index.add("entry2", "ep2", "thread-b")
        assert len(index) == 2

        index.clear()
        assert len(index) == 0
        assert index.get_episode("entry1") is None
