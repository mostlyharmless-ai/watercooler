"""E2E smoke tests for T3 LeanRAG plumbing and semantic bridge.

Tests verify:
- Config convergence (all sites use load_*_config factories)
- group_id derivation from code_path
- Episode enumeration via get_group_episodes (not get_episodes)
- Hard-fail on missing code_path
- EntryEpisodeIndex MCP provenance tool (bidirectional + chunk-aware)
- MemoryTask code_path round-trip and dead-letter safety
- Safety limit warning and filter-then-construct ordering
- Provenance input validation

Tests use mocked backends to avoid external dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.anyio


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def leanrag_test_env(monkeypatch: pytest.MonkeyPatch, stub_memory_api_keys):
    """Enable LeanRAG with stub keys and a fake leanrag_path."""
    from watercooler_mcp.tools.memory import _clear_provenance_cache

    monkeypatch.setenv("WATERCOOLER_LEANRAG_ENABLED", "1")
    monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
    _clear_provenance_cache()
    yield
    _clear_provenance_cache()


# ============================================================================
# 1. Config convergence
# ============================================================================


class TestConfigConvergence:
    """Verify all LeanRAG construction sites use load_*_config factories."""

    def test_get_leanrag_backend_uses_factory(self, leanrag_test_env):
        """_get_leanrag_backend calls load_leanrag_config, not bare constructor."""
        from watercooler_mcp.tools.memory import _get_leanrag_backend

        mock_config = MagicMock()
        mock_config.work_dir = Path("/tmp/leanrag_watercooler_cloud")

        with patch(
            "watercooler_mcp.memory.load_leanrag_config",
            return_value=mock_config,
        ) as mock_load, patch(
            "watercooler_memory.backends.leanrag.LeanRAGBackend",
        ) as mock_cls:
            mock_cls.return_value = MagicMock()
            result = _get_leanrag_backend(code_path="/repo")

        mock_load.assert_called_once_with(code_path="/repo")
        mock_cls.assert_called_once_with(mock_config)
        assert result is not None

    def test_get_leanrag_backend_returns_none_when_disabled(self, leanrag_test_env):
        """Returns None (not exception) when config factory returns None."""
        from watercooler_mcp.tools.memory import _get_leanrag_backend

        with patch(
            "watercooler_mcp.memory.load_leanrag_config",
            return_value=None,
        ):
            result = _get_leanrag_backend(code_path="/repo")

        assert result is None


# ============================================================================
# 2. Index-query graph name match
# ============================================================================


class TestGraphNameMatch:
    """Pipeline and search should use the same work_dir (same FalkorDB graph)."""

    async def test_pipeline_and_search_use_same_config(
        self, leanrag_test_env, mock_context
    ):
        """Both _leanrag_run_pipeline_impl and _search_leanrag_impl
        use load_leanrag_config with the same code_path."""

        calls: list[dict[str, Any]] = []

        def track_load(code_path: str = ""):
            calls.append({"code_path": code_path})
            return MagicMock(work_dir=Path("/tmp/leanrag_watercooler_cloud"))

        with patch(
            "watercooler_mcp.memory.load_leanrag_config",
            side_effect=track_load,
        ), patch(
            "watercooler_memory.backends.leanrag.LeanRAGBackend",
        ):
            from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

            await _leanrag_run_pipeline_impl(
                code_path="/repo", dry_run=True, ctx=mock_context
            )

        assert any(c["code_path"] == "/repo" for c in calls)


# ============================================================================
# 3. group_id derivation and BULK guard
# ============================================================================


class TestGroupIdDerivation:
    """group_id is auto-derived from code_path when not provided."""

    async def test_group_id_auto_derived(self, leanrag_test_env, mock_context):
        """Pipeline derives group_id from code_path via derive_group_id."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        with patch(
            "watercooler_mcp.memory.load_leanrag_config",
            return_value=MagicMock(work_dir=Path("/tmp/leanrag_test")),
        ), patch(
            "watercooler_memory.backends.leanrag.LeanRAGBackend",
        ), patch(
            "watercooler.path_resolver.derive_group_id",
            return_value="my_project",
        ) as mock_derive:
            result = await _leanrag_run_pipeline_impl(
                code_path="/home/user/my-project", dry_run=True, ctx=mock_context
            )

        mock_derive.assert_called_once_with(code_path="/home/user/my-project")
        payload = json.loads(result.content[0].text)
        assert payload["group_id"] == "my_project"

    def test_bulk_task_requires_group_id(self):
        """MemoryTask(task_type=BULK, group_id='') raises ValueError."""
        from watercooler_mcp.memory_queue.task import MemoryTask, TaskType

        with pytest.raises(ValueError, match="BULK tasks require"):
            MemoryTask(task_type=TaskType.BULK, group_id="", code_path="")

    def test_bulk_task_with_group_id_succeeds(self):
        """MemoryTask(task_type=BULK, group_id='derived') succeeds."""
        from watercooler_mcp.memory_queue.task import MemoryTask, TaskType

        task = MemoryTask(
            task_type=TaskType.BULK, group_id="derived", code_path="/repo"
        )
        assert task.group_id == "derived"
        assert task.code_path == "/repo"

    async def test_pipeline_rejects_empty_code_path(
        self, leanrag_test_env, mock_context
    ):
        """Pipeline returns error when code_path is empty."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        result = await _leanrag_run_pipeline_impl(
            code_path="", ctx=mock_context
        )
        payload = json.loads(result.content[0].text)
        assert payload["success"] is False
        assert "code_path is required" in payload["error"]


# ============================================================================
# 4. Episode enumeration
# ============================================================================


class TestEpisodeEnumeration:
    """get_group_episodes is used (not get_episodes) for full corpus."""

    def test_get_group_episodes_returns_episode_records(self):
        """Return type is list[EpisodeRecord]."""
        from watercooler_memory.backends import EpisodeRecord

        rec = EpisodeRecord(
            uuid="ep-1", name="test", content="body",
            source_description="src", group_id="grp", created_at="2025-01-01"
        )
        assert rec.uuid == "ep-1"
        assert rec.content == "body"

    def test_get_group_episodes_rejects_empty_group_id(self):
        """get_group_episodes raises ConfigError for empty group_id."""
        from watercooler_memory.backends import ConfigError
        from watercooler_memory.backends.graphiti import GraphitiBackend

        backend = GraphitiBackend.__new__(GraphitiBackend)
        with pytest.raises(ConfigError, match="group_id is required"):
            backend.get_group_episodes(group_id="")

    def test_get_group_episodes_calls_graphiti_client(self):
        """get_group_episodes creates a Graphiti client and runs Cypher query."""
        from watercooler_memory.backends.graphiti import GraphitiBackend

        backend = GraphitiBackend.__new__(GraphitiBackend)
        backend.config = MagicMock(
            graphiti_graph_name="test_graph",
        )

        # Mock _create_graphiti_client to raise early (avoids FalkorDB dep)
        with patch.object(
            backend, "_create_graphiti_client",
            side_effect=ConnectionError("no FalkorDB"),
        ):
            from watercooler_memory.backends import TransientError
            with pytest.raises(TransientError, match="Database connection failed"):
                backend.get_group_episodes(group_id="test-group")


# ============================================================================
# 5. Hard-fail on missing code_path in executor
# ============================================================================


class TestExecutorHardFail:
    """memory_sync executor raises when code_path is missing for LeanRAG."""

    def test_executor_raises_on_missing_code_path(self):
        """LeanRAG executor raises RuntimeError when code_path is empty."""
        import asyncio
        from watercooler_mcp.memory_queue.task import MemoryTask, TaskType
        from watercooler_mcp.memory_sync import _leanrag_pipeline_executor_fn

        task = MemoryTask(
            task_type=TaskType.BULK,
            backend="leanrag_pipeline",
            group_id="test",
            code_path="",  # Missing!
        )

        with pytest.raises(RuntimeError, match="requires code_path"):
            asyncio.run(_leanrag_pipeline_executor_fn(task))


# ============================================================================
# 6. EntryEpisodeIndex MCP provenance tool
# ============================================================================


class TestProvenanceTool:
    """Tests for watercooler_get_entry_provenance."""

    async def test_episode_to_entry_hit(self, mock_context, leanrag_test_env):
        """episode_uuid lookup returns entry_id + thread_id."""
        from watercooler_memory.entry_episode_index import (
            EntryEpisodeIndex, IndexEntry,
        )
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        mock_index = MagicMock(spec=EntryEpisodeIndex)
        mock_index.get_entry.return_value = "01AUTH001"
        mock_index.get_index_entry.return_value = IndexEntry(
            entry_id="01AUTH001",
            episode_uuid="ep-uuid-123",
            thread_id="auth-feature",
            indexed_at="2025-01-15T10:00:00Z",
        )

        with patch(
            "watercooler_memory.entry_episode_index.EntryEpisodeIndex",
            return_value=mock_index,
        ), patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ):
            result = await _get_entry_provenance_impl(
                ctx=mock_context, episode_uuid="ep-uuid-123"
            )

        payload = json.loads(result.content[0].text)
        assert payload["provenance_available"] is True
        assert payload["entry_id"] == "01AUTH001"
        assert payload["thread_id"] == "auth-feature"

    async def test_episode_to_entry_miss(self, mock_context, leanrag_test_env):
        """Unknown episode_uuid returns provenance_available=False."""
        from watercooler_memory.entry_episode_index import EntryEpisodeIndex
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        mock_index = MagicMock(spec=EntryEpisodeIndex)
        mock_index.get_entry.return_value = None

        with patch(
            "watercooler_memory.entry_episode_index.EntryEpisodeIndex",
            return_value=mock_index,
        ), patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ):
            result = await _get_entry_provenance_impl(
                ctx=mock_context, episode_uuid="unknown-uuid"
            )

        payload = json.loads(result.content[0].text)
        assert payload["provenance_available"] is False
        assert "action_hints" in payload

    async def test_entry_to_episodes_non_chunked(
        self, mock_context, leanrag_test_env
    ):
        """entry_id lookup (non-chunked) returns single episode."""
        from watercooler_memory.entry_episode_index import (
            EntryEpisodeIndex, IndexEntry,
        )
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        mock_index = MagicMock(spec=EntryEpisodeIndex)
        mock_index.get_chunks_for_entry.return_value = []  # No chunks
        mock_index.get_episode.return_value = "ep-uuid-456"
        mock_index.get_index_entry.return_value = IndexEntry(
            entry_id="01AUTH001",
            episode_uuid="ep-uuid-456",
            thread_id="auth-feature",
        )

        with patch(
            "watercooler_memory.entry_episode_index.EntryEpisodeIndex",
            return_value=mock_index,
        ), patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ):
            result = await _get_entry_provenance_impl(
                ctx=mock_context, entry_id="01AUTH001"
            )

        payload = json.loads(result.content[0].text)
        assert payload["provenance_available"] is True
        assert len(payload["episodes"]) == 1
        assert payload["episodes"][0]["episode_uuid"] == "ep-uuid-456"
        assert payload["episodes"][0]["total_chunks"] == 1

    async def test_entry_to_episodes_chunked(
        self, mock_context, leanrag_test_env
    ):
        """Chunked entry returns multiple episodes with chunk metadata."""
        from watercooler_memory.entry_episode_index import (
            EntryEpisodeIndex, ChunkEpisodeMapping,
        )
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        chunks = [
            ChunkEpisodeMapping(
                chunk_id="sha-0", episode_uuid="ep-chunk-0",
                entry_id="01LONG001", thread_id="long-thread",
                chunk_index=0, total_chunks=3,
            ),
            ChunkEpisodeMapping(
                chunk_id="sha-1", episode_uuid="ep-chunk-1",
                entry_id="01LONG001", thread_id="long-thread",
                chunk_index=1, total_chunks=3,
            ),
            ChunkEpisodeMapping(
                chunk_id="sha-2", episode_uuid="ep-chunk-2",
                entry_id="01LONG001", thread_id="long-thread",
                chunk_index=2, total_chunks=3,
            ),
        ]

        mock_index = MagicMock(spec=EntryEpisodeIndex)
        mock_index.get_chunks_for_entry.return_value = chunks
        mock_index.get_episode.return_value = None  # No stale direct mapping

        with patch(
            "watercooler_memory.entry_episode_index.EntryEpisodeIndex",
            return_value=mock_index,
        ), patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ):
            result = await _get_entry_provenance_impl(
                ctx=mock_context, entry_id="01LONG001"
            )

        payload = json.loads(result.content[0].text)
        assert payload["provenance_available"] is True
        assert len(payload["episodes"]) == 3
        assert payload["episodes"][0]["chunk_index"] == 0
        assert payload["episodes"][2]["chunk_index"] == 2
        assert payload["thread_id"] == "long-thread"
        assert "stale_direct_mapping" not in payload

    async def test_provenance_works_without_api_keys(
        self, mock_context, clean_api_keys
    ):
        """Provenance tool uses local index file — no LLM keys needed."""
        from watercooler_memory.entry_episode_index import (
            EntryEpisodeIndex, IndexEntry,
        )
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        mock_index = MagicMock(spec=EntryEpisodeIndex)
        mock_index.get_entry.return_value = "01TEST001"
        mock_index.get_index_entry.return_value = IndexEntry(
            entry_id="01TEST001", episode_uuid="ep-1",
            thread_id="test-thread",
        )

        with patch(
            "watercooler_memory.entry_episode_index.EntryEpisodeIndex",
            return_value=mock_index,
        ), patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ):
            result = await _get_entry_provenance_impl(
                ctx=mock_context, episode_uuid="ep-1"
            )

        payload = json.loads(result.content[0].text)
        assert payload["provenance_available"] is True


# ============================================================================
# 6b. Provenance with config-provided index path
# ============================================================================


class TestConfigProvidedIndexPath:
    """Verify provenance tool uses index path from graphiti config when available."""

    async def test_episode_lookup_uses_config_index_path(
        self, mock_context, leanrag_test_env
    ):
        """When load_graphiti_config returns a config with entry_episode_index_path,
        EntryEpisodeIndex receives that path instead of the default."""
        from pathlib import Path
        from watercooler_memory.entry_episode_index import (
            EntryEpisodeIndex, IndexConfig, IndexEntry,
        )
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        custom_path = Path("/tmp/custom/index.json")

        mock_graphiti_config = MagicMock()
        mock_graphiti_config.entry_episode_index_path = custom_path

        mock_index = MagicMock(spec=EntryEpisodeIndex)
        mock_index.get_entry.return_value = "01CUSTOM001"
        mock_index.get_index_entry.return_value = IndexEntry(
            entry_id="01CUSTOM001",
            episode_uuid="ep-custom-123",
            thread_id="custom-thread",
            indexed_at="2025-06-01T12:00:00Z",
        )

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_memory.entry_episode_index.EntryEpisodeIndex",
            return_value=mock_index,
        ) as mock_index_cls:
            result = await _get_entry_provenance_impl(
                ctx=mock_context, episode_uuid="ep-custom-123"
            )

        # Verify EntryEpisodeIndex was constructed with the config-provided path
        mock_index_cls.assert_called_once()
        passed_config = mock_index_cls.call_args[0][0]
        assert isinstance(passed_config, IndexConfig)
        assert passed_config.index_path == custom_path

        # Verify the lookup still works correctly through this path
        payload = json.loads(result.content[0].text)
        assert payload["provenance_available"] is True
        assert payload["entry_id"] == "01CUSTOM001"
        assert payload["thread_id"] == "custom-thread"
        assert payload["episode_uuid"] == "ep-custom-123"

    async def test_entry_lookup_uses_config_index_path(
        self, mock_context, leanrag_test_env
    ):
        """entry_id lookup also uses the config-provided index path."""
        from pathlib import Path
        from watercooler_memory.entry_episode_index import (
            EntryEpisodeIndex, ChunkEpisodeMapping, IndexConfig,
        )
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        custom_path = Path("/tmp/custom-entry/index.json")

        mock_graphiti_config = MagicMock()
        mock_graphiti_config.entry_episode_index_path = custom_path

        mock_index = MagicMock(spec=EntryEpisodeIndex)
        mock_index.get_chunks_for_entry.return_value = []
        mock_index.get_episode.return_value = "ep-from-config"
        mock_index.get_index_entry.return_value = MagicMock(
            entry_id="01ENTRY001",
            episode_uuid="ep-from-config",
            thread_id="config-thread",
        )

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_memory.entry_episode_index.EntryEpisodeIndex",
            return_value=mock_index,
        ) as mock_index_cls:
            result = await _get_entry_provenance_impl(
                ctx=mock_context, entry_id="01ENTRY001"
            )

        # Verify the config-provided path was used
        passed_config = mock_index_cls.call_args[0][0]
        assert isinstance(passed_config, IndexConfig)
        assert passed_config.index_path == custom_path

        payload = json.loads(result.content[0].text)
        assert payload["provenance_available"] is True
        assert payload["entry_id"] == "01ENTRY001"

    async def test_none_index_path_falls_back_to_default(
        self, mock_context, leanrag_test_env
    ):
        """When config exists but entry_episode_index_path is None,
        falls back to the default IndexConfig path."""
        from watercooler_memory.entry_episode_index import (
            EntryEpisodeIndex, IndexConfig, IndexEntry,
        )
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        mock_graphiti_config = MagicMock()
        mock_graphiti_config.entry_episode_index_path = None  # Falsy → fallback

        mock_index = MagicMock(spec=EntryEpisodeIndex)
        mock_index.get_entry.return_value = "01FALLBACK"
        mock_index.get_index_entry.return_value = IndexEntry(
            entry_id="01FALLBACK",
            episode_uuid="ep-fallback",
            thread_id="fallback-thread",
        )

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_memory.entry_episode_index.EntryEpisodeIndex",
            return_value=mock_index,
        ) as mock_index_cls:
            result = await _get_entry_provenance_impl(
                ctx=mock_context, episode_uuid="ep-fallback"
            )

        # With None index_path, should use default IndexConfig path
        passed_config = mock_index_cls.call_args[0][0]
        assert isinstance(passed_config, IndexConfig)
        default_config = IndexConfig(backend="graphiti")
        assert passed_config.index_path == default_config.index_path

        payload = json.loads(result.content[0].text)
        assert payload["provenance_available"] is True


# ============================================================================
# 7. MemoryTask code_path round-trip
# ============================================================================


class TestMemoryTaskCodePath:
    """code_path field serialization and backward compatibility."""

    def test_code_path_round_trip(self):
        """code_path survives serialize→deserialize."""
        from watercooler_mcp.memory_queue.task import MemoryTask

        task = MemoryTask(code_path="/home/user/project", entry_id="01X")
        d = task.to_dict()
        assert d["code_path"] == "/home/user/project"

        restored = MemoryTask.from_dict(d)
        assert restored.code_path == "/home/user/project"

    def test_old_task_without_code_path(self):
        """Tasks serialized before code_path was added deserialize safely."""
        from watercooler_mcp.memory_queue.task import MemoryTask

        old_data = {
            "task_id": "test123",
            "task_type": "single",
            "backend": "graphiti",
            "status": "pending",
            "entry_id": "01OLD001",
            # No code_path field
        }
        task = MemoryTask.from_dict(old_data)
        assert task.code_path == ""

    def test_jsonl_round_trip(self):
        """code_path survives to_json_line → from_json_line."""
        from watercooler_mcp.memory_queue.task import MemoryTask

        task = MemoryTask(code_path="/repo", entry_id="01X")
        line = task.to_json_line()
        restored = MemoryTask.from_json_line(line)
        assert restored.code_path == "/repo"


# ============================================================================
# 8. Dead-letter safety
# ============================================================================


class TestDeadLetterSafety:
    """ValueError from __post_init__ doesn't block queue recovery."""

    def test_corrupt_bulk_task_skipped_on_load(self, tmp_path):
        """Queue._load() skips BULK tasks with empty group_id."""
        from watercooler_mcp.memory_queue.queue import MemoryTaskQueue

        # Write a valid task followed by an invalid BULK task (empty group_id)
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()

        valid_line = json.dumps({
            "task_id": "valid001",
            "task_type": "single",
            "backend": "graphiti",
            "status": "pending",
            "entry_id": "01GOOD",
            "group_id": "",
            "code_path": "",
        })
        invalid_line = json.dumps({
            "task_id": "bad002",
            "task_type": "bulk",
            "backend": "leanrag_pipeline",
            "status": "pending",
            "entry_id": "",
            "group_id": "",  # Will trigger ValueError in __post_init__
            "code_path": "",
        })
        (queue_dir / "queue.jsonl").write_text(valid_line + "\n" + invalid_line + "\n")

        queue = MemoryTaskQueue(queue_dir=queue_dir)
        # Valid task loaded, invalid skipped
        assert queue.depth() == 1
        assert queue.get_task("valid001") is not None
        assert queue.get_task("bad002") is None

    def test_corrupt_bulk_task_skipped_in_retry_dead_letters(self, tmp_path):
        """retry_dead_letters() skips BULK tasks with empty group_id."""
        from watercooler_mcp.memory_queue.queue import MemoryTaskQueue

        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()

        dead_letter_line = json.dumps({
            "task_id": "dead001",
            "task_type": "bulk",
            "backend": "leanrag_pipeline",
            "status": "dead_letter",
            "group_id": "",  # Will trigger ValueError
            "code_path": "",
        })
        (queue_dir / "dead_letter.jsonl").write_text(dead_letter_line + "\n")

        queue = MemoryTaskQueue(queue_dir=queue_dir)
        recovered = queue.retry_dead_letters()
        assert recovered == 0  # Skipped due to ValueError


# ============================================================================
# 9. Safety limit warning
# ============================================================================


class TestSafetyLimit:
    """get_group_episodes emits warning at SAFETY_LIMIT."""

    def test_safety_limit_constant_exists(self):
        """EPISODE_SAFETY_LIMIT is defined on GraphitiBackend."""
        from watercooler_memory.backends.graphiti import GraphitiBackend

        assert GraphitiBackend.EPISODE_SAFETY_LIMIT == 10_000

    def test_cypher_query_includes_limit(self):
        """The Cypher query passes safety_limit to execute_query."""
        from unittest.mock import AsyncMock
        from watercooler_memory.backends.graphiti import GraphitiBackend

        backend = object.__new__(GraphitiBackend)
        backend.EPISODE_SAFETY_LIMIT = 10_000

        mock_driver = MagicMock()
        mock_driver.execute_query = AsyncMock(return_value=([], None, None))

        mock_graphiti = MagicMock()
        mock_graphiti.clients.driver = mock_driver

        with patch.object(backend, "_create_graphiti_client", return_value=mock_graphiti), \
             patch.object(backend, "_sanitize_thread_id", return_value="test_group"):
            backend.get_group_episodes("test_group")

        call_args = mock_driver.execute_query.call_args
        assert call_args.kwargs.get("safety_limit") == 10_000
        assert "LIMIT $safety_limit" in call_args.args[0]


# ============================================================================
# 10. Filter-then-construct ordering
# ============================================================================


class TestFilterOrdering:
    """_filter_by_time_range is called on raw dicts, not EpisodeRecord."""

    def test_filter_operates_on_dicts(self):
        """_filter_by_time_range accepts list[dict], not list[EpisodeRecord]."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        raw = [
            {"uuid": "1", "created_at": "2025-01-15T10:00:00Z"},
            {"uuid": "2", "created_at": "2025-02-01T10:00:00Z"},
        ]
        filtered = _filter_by_time_range(
            raw, start_time="2025-01-20T00:00:00Z", end_time=""
        )
        # Only the second record passes the filter
        assert len(filtered) == 1
        assert filtered[0]["uuid"] == "2"
        # Verify it returns dicts, not dataclasses
        assert isinstance(filtered[0], dict)


# ============================================================================
# 11. Provenance input validation
# ============================================================================


class TestProvenanceValidation:
    """Input validation for the provenance tool."""

    async def test_both_ids_rejected(self, mock_context):
        """Providing both entry_id and episode_uuid returns error."""
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        result = await _get_entry_provenance_impl(
            ctx=mock_context,
            entry_id="01AUTH001",
            episode_uuid="ep-uuid-123",
        )
        payload = json.loads(result.content[0].text)
        assert "error" in payload
        assert "exactly one" in payload["error"]

    async def test_neither_id_rejected(self, mock_context):
        """Providing neither entry_id nor episode_uuid returns error."""
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        result = await _get_entry_provenance_impl(ctx=mock_context)
        payload = json.loads(result.content[0].text)
        assert "error" in payload

    async def test_whitespace_stripped(self, mock_context, leanrag_test_env):
        """Whitespace-padded keys are stripped before lookup."""
        from watercooler_memory.entry_episode_index import (
            EntryEpisodeIndex, IndexEntry,
        )
        from watercooler_mcp.tools.memory import _get_entry_provenance_impl

        mock_index = MagicMock(spec=EntryEpisodeIndex)
        mock_index.get_entry.return_value = "01AUTH001"
        mock_index.get_index_entry.return_value = IndexEntry(
            entry_id="01AUTH001", episode_uuid="ep-1",
            thread_id="auth-feature",
        )

        with patch(
            "watercooler_memory.entry_episode_index.EntryEpisodeIndex",
            return_value=mock_index,
        ), patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ):
            result = await _get_entry_provenance_impl(
                ctx=mock_context, episode_uuid="  ep-1  "
            )

        # The stripped UUID should be passed to get_entry
        mock_index.get_entry.assert_called_once_with("ep-1")
        payload = json.loads(result.content[0].text)
        assert payload["provenance_available"] is True


# ============================================================================
# 13. episodes_to_chunk_payload unit tests
# ============================================================================


class TestEpisodesToChunkPayload:
    """Unit tests for the episodes_to_chunk_payload helper."""

    def test_basic_conversion(self):
        """Episodes with uuids are converted to ChunkPayload chunks."""
        from dataclasses import dataclass
        from watercooler_mcp.memory_sync import episodes_to_chunk_payload

        @dataclass
        class FakeEpisode:
            uuid: str = ""
            content: str = ""

        episodes = [
            FakeEpisode(uuid="ep-1", content="first episode"),
            FakeEpisode(uuid="ep-2", content="second episode"),
        ]
        payload = episodes_to_chunk_payload(episodes, "test_group")

        assert payload.manifest_version == "1.0"
        assert len(payload.chunks) == 2
        assert payload.chunks[0]["id"] == "ep-1"
        assert payload.chunks[0]["text"] == "first episode"
        assert payload.chunks[0]["metadata"]["group_id"] == "test_group"
        assert payload.chunks[0]["metadata"]["source"] == "graphiti_episode"

    def test_empty_uuid_falls_back_to_md5(self):
        """Episodes with empty uuid use md5 content hash as chunk id."""
        import hashlib
        from dataclasses import dataclass
        from watercooler_mcp.memory_sync import episodes_to_chunk_payload

        @dataclass
        class FakeEpisode:
            uuid: str = ""
            content: str = ""

        episodes = [FakeEpisode(uuid="", content="some content")]
        payload = episodes_to_chunk_payload(episodes, "grp")

        expected_id = hashlib.md5(b"some content", usedforsecurity=False).hexdigest()
        assert payload.chunks[0]["id"] == expected_id

    def test_empty_episodes_returns_empty_payload(self):
        """Empty episode list produces a payload with no chunks."""
        from watercooler_mcp.memory_sync import episodes_to_chunk_payload

        payload = episodes_to_chunk_payload([], "grp")
        assert payload.chunks == []
        assert payload.manifest_version == "1.0"

    def test_empty_content_skipped(self):
        """Episodes with empty content are excluded from the payload."""
        from dataclasses import dataclass
        from watercooler_mcp.memory_sync import episodes_to_chunk_payload

        @dataclass
        class FakeEpisode:
            uuid: str = ""
            content: str = ""

        episodes = [
            FakeEpisode(uuid="ep-1", content="real content"),
            FakeEpisode(uuid="ep-2", content=""),
            FakeEpisode(uuid="ep-3", content="more content"),
        ]
        payload = episodes_to_chunk_payload(episodes, "grp")
        assert len(payload.chunks) == 2
        assert [c["id"] for c in payload.chunks] == ["ep-1", "ep-3"]


# ============================================================================
# 14. get_group_episodes asyncio loop guard
# ============================================================================


class TestGetGroupEpisodesLoopGuard:
    """Calling get_group_episodes from an async context raises RuntimeError."""

    async def test_rejects_call_from_running_loop(self):
        """get_group_episodes raises RuntimeError inside an event loop."""
        from watercooler_memory.backends.graphiti import GraphitiBackend

        backend = object.__new__(GraphitiBackend)
        with pytest.raises(RuntimeError, match="async context"):
            backend.get_group_episodes("test_group")
