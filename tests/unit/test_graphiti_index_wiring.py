"""Tests for EntryEpisodeIndex wiring into Graphiti backend.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 4.2:
- Initialize index in __init__
- index_entry_as_episode() method
- Mapping updates on index operations
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watercooler_memory.backends.graphiti import GraphitiBackend, GraphitiConfig
from watercooler_memory.entry_episode_index import (
    EntryEpisodeIndex,
    IndexConfig,
)


class TestGraphitiBackendIndexInitialization:
    """Test index initialization in GraphitiBackend."""

    @pytest.fixture
    def mock_validation(self):
        """Mock config validation to skip graphiti/openai checks."""
        with patch.object(GraphitiBackend, "_validate_config"):
            yield

    def test_backend_creates_index_by_default(self, tmp_path: Path, mock_validation):
        """Test backend creates EntryEpisodeIndex on init."""
        index_path = tmp_path / "index.json"
        config = GraphitiConfig(
            entry_episode_index_path=index_path,
        )
        backend = GraphitiBackend(config)

        assert backend.entry_episode_index is not None
        assert isinstance(backend.entry_episode_index, EntryEpisodeIndex)

    def test_backend_with_custom_index_path(self, tmp_path: Path, mock_validation):
        """Test backend uses custom index path."""
        custom_path = tmp_path / "custom" / "index.json"
        config = GraphitiConfig(
            entry_episode_index_path=custom_path,
        )
        backend = GraphitiBackend(config)

        assert backend.entry_episode_index._config.index_path == custom_path

    def test_backend_index_disabled(self, tmp_path: Path, mock_validation):
        """Test backend can disable index tracking."""
        config = GraphitiConfig(
            track_entry_episodes=False,
        )
        backend = GraphitiBackend(config)

        assert backend.entry_episode_index is None

    def test_backend_loads_existing_index(self, tmp_path: Path, mock_validation):
        """Test backend loads existing index on init."""
        index_path = tmp_path / "index.json"

        # Create and save an index
        index_config = IndexConfig(index_path=index_path)
        index = EntryEpisodeIndex(index_config)
        index.add("entry1", "ep1", "thread-a")
        index.save()

        # Create backend - should load existing index
        config = GraphitiConfig(
            entry_episode_index_path=index_path,
        )
        backend = GraphitiBackend(config)

        assert len(backend.entry_episode_index) == 1
        assert backend.entry_episode_index.get_episode("entry1") == "ep1"


class TestGraphitiBackendIndexOperations:
    """Test index operations in GraphitiBackend."""

    @pytest.fixture
    def backend_with_index(self, tmp_path: Path) -> GraphitiBackend:
        """Create backend with index for testing."""
        index_path = tmp_path / "index.json"
        config = GraphitiConfig(
            entry_episode_index_path=index_path,
        )
        with patch.object(GraphitiBackend, "_validate_config"):
            return GraphitiBackend(config)

    def test_index_entry_as_episode(self, backend_with_index: GraphitiBackend):
        """Test adding entry-episode mapping."""
        backend_with_index.index_entry_as_episode(
            entry_id="01ABC123",
            episode_uuid="01DEF456",
            thread_id="auth-feature",
        )

        index = backend_with_index.entry_episode_index
        assert index.get_episode("01ABC123") == "01DEF456"
        assert index.get_entry("01DEF456") == "01ABC123"

    def test_index_entry_auto_saves(self, tmp_path: Path):
        """Test index auto-saves after adding entry."""
        index_path = tmp_path / "index.json"
        config = GraphitiConfig(
            entry_episode_index_path=index_path,
            auto_save_index=True,
        )
        with patch.object(GraphitiBackend, "_validate_config"):
            backend = GraphitiBackend(config)

        backend.index_entry_as_episode(
            entry_id="01ABC123",
            episode_uuid="01DEF456",
            thread_id="test-thread",
        )

        # File should exist and contain the mapping
        assert index_path.exists()
        data = json.loads(index_path.read_text())
        assert len(data["entries"]) == 1
        assert data["entries"][0]["entry_id"] == "01ABC123"

    def test_get_episode_for_entry(self, backend_with_index: GraphitiBackend):
        """Test getting episode UUID for entry."""
        backend_with_index.index_entry_as_episode(
            entry_id="entry1",
            episode_uuid="ep1",
            thread_id="thread-a",
        )

        result = backend_with_index.get_episode_for_entry("entry1")
        assert result == "ep1"

    def test_get_entry_for_episode(self, backend_with_index: GraphitiBackend):
        """Test getting entry ID for episode."""
        backend_with_index.index_entry_as_episode(
            entry_id="entry1",
            episode_uuid="ep1",
            thread_id="thread-a",
        )

        result = backend_with_index.get_entry_for_episode("ep1")
        assert result == "entry1"

    def test_get_nonexistent_mapping(self, backend_with_index: GraphitiBackend):
        """Test getting nonexistent mapping returns None."""
        assert backend_with_index.get_episode_for_entry("nonexistent") is None
        assert backend_with_index.get_entry_for_episode("nonexistent") is None


class TestGraphitiBackendIndexDuringIndex:
    """Test index updates during actual indexing operations."""

    @pytest.fixture
    def mock_graphiti_client(self):
        """Create mock Graphiti client."""
        mock_episode = MagicMock()
        mock_episode.uuid = "mock-episode-uuid-1234"

        mock_client = MagicMock()
        mock_client.add_episode = AsyncMock(return_value=mock_episode)

        return mock_client

    @pytest.fixture
    def backend_with_mocked_graphiti(
        self, tmp_path: Path, mock_graphiti_client
    ) -> tuple[GraphitiBackend, MagicMock]:
        """Create backend with mocked Graphiti for testing."""
        index_path = tmp_path / "index.json"
        work_dir = tmp_path / "work"
        work_dir.mkdir()

        config = GraphitiConfig(
            entry_episode_index_path=index_path,
            work_dir=work_dir,
        )
        with patch.object(GraphitiBackend, "_validate_config"):
            backend = GraphitiBackend(config)

        return backend, mock_graphiti_client

    def test_index_method_tracks_episodes(
        self, backend_with_mocked_graphiti, tmp_path: Path
    ):
        """Test that index() method tracks entry-episode mappings."""
        backend, mock_client = backend_with_mocked_graphiti

        # Create mock episode response with incrementing UUIDs
        # AddEpisodeResults has an 'episode' field containing the EpisodicNode
        episode_uuids = ["ep-uuid-1", "ep-uuid-2", "ep-uuid-3"]
        call_count = [0]

        async def mock_add_episode(*args, **kwargs):
            mock_episode = MagicMock()
            mock_episode.uuid = episode_uuids[call_count[0] % len(episode_uuids)]
            mock_result = MagicMock()
            mock_result.episode = mock_episode
            call_count[0] += 1
            return mock_result

        mock_client.add_episode = AsyncMock(side_effect=mock_add_episode)

        # Prepare episodes file
        work_dir = backend.config.work_dir
        episodes = [
            {
                "name": "entry-1: Test entry 1",
                "episode_body": "Body 1",
                "source_description": "Test",
                "reference_time": "2025-01-15T10:00:00Z",
                "group_id": "test-thread",
                "metadata": {
                    "entry_id": "entry-1",
                    "thread_id": "test-thread",
                },
            },
            {
                "name": "entry-2: Test entry 2",
                "episode_body": "Body 2",
                "source_description": "Test",
                "reference_time": "2025-01-15T11:00:00Z",
                "group_id": "test-thread",
                "metadata": {
                    "entry_id": "entry-2",
                    "thread_id": "test-thread",
                },
            },
        ]
        (work_dir / "episodes.json").write_text(json.dumps(episodes))

        # Mock _create_graphiti_client to return our mock
        with patch.object(
            backend, "_create_graphiti_client", return_value=mock_client
        ):
            from watercooler_memory.backends import ChunkPayload

            result = backend.index(
                ChunkPayload(manifest_version=1, chunks=[])
            )

        # Verify mappings were tracked
        assert result.indexed_count == 2
        assert backend.entry_episode_index.get_episode("entry-1") == "ep-uuid-1"
        assert backend.entry_episode_index.get_episode("entry-2") == "ep-uuid-2"


class TestGraphitiConfigWithIndex:
    """Test GraphitiConfig with index settings."""

    def test_default_config_enables_index(self):
        """Test default config enables entry episode tracking."""
        config = GraphitiConfig()
        assert config.track_entry_episodes is True

    def test_default_index_path(self):
        """Test default index path is in ~/.watercooler/graphiti."""
        config = GraphitiConfig()
        expected_suffix = ".watercooler/graphiti/entry_episode_index.json"
        assert str(config.entry_episode_index_path).endswith(expected_suffix)

    def test_custom_index_path(self, tmp_path: Path):
        """Test custom index path."""
        custom_path = tmp_path / "my_index.json"
        config = GraphitiConfig(entry_episode_index_path=custom_path)
        assert config.entry_episode_index_path == custom_path

    def test_disabled_index_tracking(self):
        """Test disabling index tracking."""
        config = GraphitiConfig(track_entry_episodes=False)
        assert config.track_entry_episodes is False
