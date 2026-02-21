"""Unit tests for the LeanRAG pipeline executor routing logic.

Tests the _leanrag_pipeline_executor_fn in memory_sync.py, verifying:
- SINGLE tasks route to incremental_index() when state exists
- SINGLE tasks route to full index() when no state exists
- SINGLE tasks respect the incremental=False override
- BULK tasks always use full index()
- Non-JSON content in SINGLE tasks is handled gracefully
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.memory_queue.task import MemoryTask, TaskType


# ================================================================== #
# SINGLE task routing
# ================================================================== #


class TestSingleTaskRouting:
    """Tests for SINGLE task routing in _leanrag_pipeline_executor_fn."""

    def test_single_task_incremental_when_state_exists(self):
        """SINGLE task uses incremental_index when state exists."""
        from watercooler_mcp.memory_sync import _leanrag_pipeline_executor_fn

        mock_result = MagicMock(
            indexed_count=1,
            message="Incremental: 1 entity assigned",
        )
        mock_backend = MagicMock()
        mock_backend.has_incremental_state.return_value = True
        mock_backend.incremental_index = MagicMock(return_value=mock_result)

        task = MemoryTask(
            task_type=TaskType.SINGLE,
            backend="leanrag_pipeline",
            entry_id="E1",
            group_id="test-group",
            content="OAuth2 enables delegated authorization.",
            code_path="/tmp/test-repo",
        )

        with patch("watercooler_mcp.memory.load_leanrag_config", return_value=MagicMock()), \
             patch("watercooler_memory.backends.leanrag.LeanRAGBackend", return_value=mock_backend):
            result = asyncio.run(_leanrag_pipeline_executor_fn(task))

        assert result["episode_uuid"] == "E1"
        assert result["entities_extracted"] == 1
        # incremental_index was called (not index)
        mock_backend.incremental_index.assert_called_once()
        mock_backend.index.assert_not_called()

    def test_single_task_full_index_when_no_state(self):
        """SINGLE task falls back to full index when no state exists."""
        from watercooler_mcp.memory_sync import _leanrag_pipeline_executor_fn

        mock_result = MagicMock(
            indexed_count=5,
            message="Full build: 5 clusters",
        )
        mock_backend = MagicMock()
        mock_backend.has_incremental_state.return_value = False
        mock_backend.index = MagicMock(return_value=mock_result)

        task = MemoryTask(
            task_type=TaskType.SINGLE,
            backend="leanrag_pipeline",
            entry_id="E2",
            group_id="test-group",
            content="JWT tokens carry claims.",
            code_path="/tmp/test-repo",
        )

        with patch("watercooler_mcp.memory.load_leanrag_config", return_value=MagicMock()), \
             patch("watercooler_memory.backends.leanrag.LeanRAGBackend", return_value=mock_backend):
            result = asyncio.run(_leanrag_pipeline_executor_fn(task))

        assert result["episode_uuid"] == "E2"
        assert result["entities_extracted"] == 5
        # Full index was called (not incremental)
        mock_backend.index.assert_called_once()
        mock_backend.incremental_index.assert_not_called()

    def test_single_task_missing_content_raises(self):
        """SINGLE task with empty content raises RuntimeError."""
        from watercooler_mcp.memory_sync import _leanrag_pipeline_executor_fn

        task = MemoryTask(
            task_type=TaskType.SINGLE,
            backend="leanrag_pipeline",
            entry_id="E3",
            content="",
            code_path="/tmp/test-repo",
        )

        mock_backend = MagicMock()

        with patch("watercooler_mcp.memory.load_leanrag_config", return_value=MagicMock()), \
             patch("watercooler_memory.backends.leanrag.LeanRAGBackend", return_value=mock_backend):
            with pytest.raises(RuntimeError, match="Missing content"):
                asyncio.run(_leanrag_pipeline_executor_fn(task))


# ================================================================== #
# BULK task routing
# ================================================================== #


class TestBulkTaskRouting:
    """Tests for BULK task routing in _leanrag_pipeline_executor_fn."""

    def test_bulk_task_always_full_index(self):
        """BULK task always runs full index regardless of incremental state."""
        from watercooler_mcp.memory_sync import _leanrag_pipeline_executor_fn

        mock_result = MagicMock(
            indexed_count=10,
            message="Full build: 10 clusters",
        )
        mock_backend = MagicMock()
        mock_backend.has_incremental_state.return_value = True  # State exists but ignored
        mock_backend.index = MagicMock(return_value=mock_result)

        # Episodes returned as objects with .uuid and .content attributes
        ep1 = MagicMock(uuid="ep-1", content="OAuth2 authorization")
        ep2 = MagicMock(uuid="ep-2", content="JWT token validation")

        mock_graphiti = MagicMock()
        mock_graphiti.get_group_episodes = MagicMock(return_value=[ep1, ep2])

        task = MemoryTask(
            task_type=TaskType.BULK,
            backend="leanrag_pipeline",
            group_id="auth-feature",
            content=json.dumps({"start_date": "", "end_date": ""}),
            code_path="/tmp/test-repo",
        )

        with patch("watercooler_mcp.memory.load_leanrag_config", return_value=MagicMock()), \
             patch("watercooler_memory.backends.leanrag.LeanRAGBackend", return_value=mock_backend), \
             patch("watercooler_mcp.memory.load_graphiti_config", return_value=MagicMock()), \
             patch("watercooler_memory.backends.graphiti.GraphitiBackend", return_value=mock_graphiti):
            result = asyncio.run(_leanrag_pipeline_executor_fn(task))

        assert result["group_id"] == "auth-feature"
        assert result["chunks_processed"] == 2
        # Full index was called (not incremental)
        mock_backend.index.assert_called_once()
        mock_backend.incremental_index.assert_not_called()

    def test_bulk_task_missing_group_id_raises(self):
        """BULK task with no group_id raises ValueError at construction."""
        # __post_init__ guard rejects BULK tasks with empty group_id
        with pytest.raises(ValueError, match="BULK tasks require non-empty group_id"):
            MemoryTask(
                task_type=TaskType.BULK,
                backend="leanrag_pipeline",
                group_id="",
                content=json.dumps({}),
            )

    def test_bulk_task_no_episodes_returns_zero(self):
        """BULK task with no episodes returns zero counts."""
        from watercooler_mcp.memory_sync import _leanrag_pipeline_executor_fn

        mock_graphiti = MagicMock()
        mock_graphiti.get_group_episodes = MagicMock(return_value=[])
        mock_backend = MagicMock()

        task = MemoryTask(
            task_type=TaskType.BULK,
            backend="leanrag_pipeline",
            group_id="empty-group",
            content=json.dumps({}),
            code_path="/tmp/test-repo",
        )

        with patch("watercooler_mcp.memory.load_leanrag_config", return_value=MagicMock()), \
             patch("watercooler_memory.backends.leanrag.LeanRAGBackend", return_value=mock_backend), \
             patch("watercooler_mcp.memory.load_graphiti_config", return_value=MagicMock()), \
             patch("watercooler_memory.backends.graphiti.GraphitiBackend", return_value=mock_graphiti):
            result = asyncio.run(_leanrag_pipeline_executor_fn(task))

        assert result["clusters_created"] == 0
        assert result["chunks_processed"] == 0


# ================================================================== #
# Content parsing
# ================================================================== #


class TestContentParsing:
    """Tests for content JSON parsing in the executor."""

    def test_non_json_content_treated_as_raw_text(self):
        """Non-JSON content in SINGLE tasks is used as raw text."""
        from watercooler_mcp.memory_sync import _leanrag_pipeline_executor_fn

        mock_result = MagicMock(
            indexed_count=1,
            message="indexed 1",
        )
        mock_backend = MagicMock()
        mock_backend.has_incremental_state.return_value = False
        mock_backend.index = MagicMock(return_value=mock_result)

        task = MemoryTask(
            task_type=TaskType.SINGLE,
            backend="leanrag_pipeline",
            entry_id="E4",
            group_id="test",
            content="This is plain text, not JSON.",
            code_path="/tmp/test-repo",
        )

        with patch("watercooler_mcp.memory.load_leanrag_config", return_value=MagicMock()), \
             patch("watercooler_memory.backends.leanrag.LeanRAGBackend", return_value=mock_backend):
            result = asyncio.run(_leanrag_pipeline_executor_fn(task))

        # Should succeed (not raise JSON parse error)
        assert result["episode_uuid"] == "E4"

    def test_incremental_false_forces_full_rebuild(self):
        """When incremental=False is in JSON params, SINGLE tasks use full index."""
        from watercooler_mcp.memory_sync import _leanrag_pipeline_executor_fn

        mock_result = MagicMock(
            indexed_count=5,
            message="Full build",
        )
        mock_backend = MagicMock()
        mock_backend.has_incremental_state.return_value = True  # State exists
        mock_backend.index = MagicMock(return_value=mock_result)

        # JSON content with incremental=False (from tools/memory.py)
        content_with_flag = json.dumps({
            "start_date": "",
            "end_date": "",
            "incremental": False,
        })

        task = MemoryTask(
            task_type=TaskType.SINGLE,
            backend="leanrag_pipeline",
            entry_id="E5",
            group_id="test",
            content=content_with_flag,
            code_path="/tmp/test-repo",
        )

        with patch("watercooler_mcp.memory.load_leanrag_config", return_value=MagicMock()), \
             patch("watercooler_memory.backends.leanrag.LeanRAGBackend", return_value=mock_backend):
            result = asyncio.run(_leanrag_pipeline_executor_fn(task))

        # Should use full index (not incremental) even though state exists
        mock_backend.index.assert_called_once()
        mock_backend.incremental_index.assert_not_called()


# ================================================================== #
# Tool param pass-through
# ================================================================== #


class TestToolIncrementalParam:
    """Tests for incremental parameter in _leanrag_run_pipeline_impl."""

    def test_incremental_param_passed_in_queue_payload(self):
        """The incremental flag is included in queue task content."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        enqueued_task = None

        class FakeQueue:
            def enqueue(self, task):
                nonlocal enqueued_task
                enqueued_task = task
                return "fake-task-id"

        class FakeWorker:
            def has_executor(self, name):
                return True

            def wake(self):
                pass

        mock_ctx = MagicMock()

        with patch("watercooler_mcp.tools.memory._get_leanrag_backend", return_value=MagicMock()), \
             patch("watercooler_mcp.memory_queue.get_queue", return_value=FakeQueue()), \
             patch("watercooler_mcp.memory_queue.get_worker", return_value=FakeWorker()):
            asyncio.run(_leanrag_run_pipeline_impl(
                group_id="test-group",
                code_path="/tmp/test-repo",
                ctx=mock_ctx,
                incremental=False,
            ))

        assert enqueued_task is not None
        payload = json.loads(enqueued_task.content)
        assert payload["incremental"] is False

    def test_incremental_true_by_default_in_payload(self):
        """The incremental flag defaults to True in queue payload."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        enqueued_task = None

        class FakeQueue:
            def enqueue(self, task):
                nonlocal enqueued_task
                enqueued_task = task
                return "fake-task-id"

        class FakeWorker:
            def has_executor(self, name):
                return True

            def wake(self):
                pass

        mock_ctx = MagicMock()

        with patch("watercooler_mcp.tools.memory._get_leanrag_backend", return_value=MagicMock()), \
             patch("watercooler_mcp.memory_queue.get_queue", return_value=FakeQueue()), \
             patch("watercooler_mcp.memory_queue.get_worker", return_value=FakeWorker()):
            asyncio.run(_leanrag_run_pipeline_impl(
                group_id="test-group",
                code_path="/tmp/test-repo",
                ctx=mock_ctx,
            ))

        assert enqueued_task is not None
        payload = json.loads(enqueued_task.content)
        assert payload["incremental"] is True
