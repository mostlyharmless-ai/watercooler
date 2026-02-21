"""Tests for memory MCP write tools.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 5.1:
- graphiti_add_episode tool
- leanrag_run_pipeline trigger tool
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Configure pytest-asyncio mode
pytestmark = pytest.mark.anyio


class TestGraphitiAddEpisodeTool:
    """Tests for watercooler_graphiti_add_episode tool."""

    @pytest.fixture
    def mock_graphiti_backend(self):
        """Create mock Graphiti backend."""
        mock_episode = MagicMock()
        mock_episode.uuid = "ep-uuid-12345"
        mock_episode.name = "Test Episode"

        mock_backend = MagicMock()
        mock_backend.index_entry_as_episode = MagicMock()

        # Mock the async add_episode_direct method
        async def mock_add_direct(*args, **kwargs):
            return {
                "episode_uuid": "ep-uuid-12345",
                "entities_extracted": ["Entity1", "Entity2"],
                "facts_extracted": 3,
            }

        mock_backend.add_episode_direct = AsyncMock(side_effect=mock_add_direct)

        return mock_backend

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    async def test_add_episode_success(self, mock_graphiti_backend, mock_context):
        """Test successful episode submission (fire-and-forget)."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=MagicMock(),
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            result = await _graphiti_add_episode_impl(
                content="Test episode content about authentication",
                group_id="auth-feature",
                ctx=mock_context,
            )

            # Parse result JSON
            result_text = result.content[0].text
            result_data = json.loads(result_text)

            # Fire-and-forget: returns immediately with "submitted" status
            assert result_data["success"] is True
            assert result_data["status"] == "submitted"
            assert result_data["group_id"] == "auth-feature"
            assert "background" in result_data["message"].lower()

            # Let the background task run
            await asyncio.sleep(0.05)

            # Verify the backend was called in the background
            mock_graphiti_backend.add_episode_direct.assert_called_once()

    async def test_add_episode_with_timestamp(self, mock_graphiti_backend, mock_context):
        """Test episode submission with custom timestamp."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        timestamp = "2025-01-15T10:00:00Z"

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=MagicMock(),
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            result = await _graphiti_add_episode_impl(
                content="Test episode content",
                group_id="test-thread",
                timestamp=timestamp,
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is True
            assert result_data["status"] == "submitted"

            # Let the background task run
            await asyncio.sleep(0.05)

            # Verify timestamp was passed to backend in the background task
            call_args = mock_graphiti_backend.add_episode_direct.call_args
            assert call_args is not None

    async def test_add_episode_with_entry_id(self, mock_graphiti_backend, mock_context):
        """Test episode submission with entry_id for provenance tracking."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=MagicMock(),
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            result = await _graphiti_add_episode_impl(
                content="Test episode content",
                group_id="test-thread",
                entry_id="01ABC123",
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is True
            assert result_data["status"] == "submitted"
            assert result_data["entry_id"] == "01ABC123"

            # Let the background task run
            await asyncio.sleep(0.05)

            # Verify entry-episode mapping was created in background
            mock_graphiti_backend.index_entry_as_episode.assert_called_once()

    async def test_add_episode_graphiti_disabled(self, mock_context):
        """Test error when Graphiti is not enabled."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ):
            result = await _graphiti_add_episode_impl(
                content="Test content",
                group_id="test-thread",
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is False
            assert "error" in result_data
            assert "not enabled" in result_data["error"].lower()

    async def test_add_episode_missing_content(self, mock_context):
        """Test error when content is empty."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        # Empty content validation happens before any imports
        result = await _graphiti_add_episode_impl(
            content="",
            group_id="test-thread",
            ctx=mock_context,
        )

        result_data = json.loads(result.content[0].text)
        assert result_data["success"] is False
        assert "content" in result_data["error"].lower()

    async def test_add_episode_missing_group_id(self, mock_context):
        """Test error when group_id is empty."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        # Empty group_id validation happens before any imports
        result = await _graphiti_add_episode_impl(
            content="Test content",
            group_id="",
            ctx=mock_context,
        )

        result_data = json.loads(result.content[0].text)
        assert result_data["success"] is False
        assert "group_id" in result_data["error"].lower()


class TestLeanRAGRunPipelineTool:
    """Tests for watercooler_leanrag_run_pipeline tool."""

    @pytest.fixture
    def mock_leanrag_backend(self):
        """Create mock LeanRAG backend."""
        # Mock result from backend.index()
        mock_index_result = MagicMock()
        mock_index_result.indexed_count = 5
        mock_index_result.message = "Indexed 5 chunks"

        mock_backend = MagicMock()
        mock_backend.index = MagicMock(return_value=mock_index_result)
        mock_backend.has_incremental_state.return_value = False
        return mock_backend

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    async def test_run_pipeline_success(self, mock_leanrag_backend, mock_context):
        """Test successful pipeline execution (direct, no queue)."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        # Episodes returned as objects with .uuid and .content attributes
        ep1 = MagicMock(uuid="ep1", content="Test content")
        mock_graphiti = MagicMock()
        mock_graphiti.get_group_episodes = MagicMock(return_value=[ep1])

        with patch(
            "watercooler_mcp.tools.memory._get_leanrag_backend",
            return_value=mock_leanrag_backend,
        ), patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=MagicMock(),
        ), patch(
            "watercooler_memory.backends.graphiti.GraphitiBackend",
            return_value=mock_graphiti,
        ), patch(
            "watercooler_mcp.memory_queue.get_queue",
            return_value=None,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="auth-feature",
                code_path="/tmp/test-repo",
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is True
            assert "clusters_created" in result_data

    async def test_run_pipeline_with_filters(self, mock_leanrag_backend, mock_context):
        """Test pipeline with filter options (direct, no queue)."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        # Episodes returned as objects with .uuid and .content attributes
        ep1 = MagicMock(uuid="ep1", content="Test content")
        mock_graphiti = MagicMock()
        mock_graphiti.get_group_episodes = MagicMock(return_value=[ep1])

        with patch(
            "watercooler_mcp.tools.memory._get_leanrag_backend",
            return_value=mock_leanrag_backend,
        ), patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=MagicMock(),
        ), patch(
            "watercooler_memory.backends.graphiti.GraphitiBackend",
            return_value=mock_graphiti,
        ), patch(
            "watercooler_mcp.memory_queue.get_queue",
            return_value=None,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="auth-feature",
                code_path="/tmp/test-repo",
                start_date="2025-01-01",
                end_date="2025-01-31",
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is True

    async def test_run_pipeline_leanrag_unavailable(self, mock_context):
        """Test error when LeanRAG is not available."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        with patch(
            "watercooler_mcp.tools.memory._get_leanrag_backend",
            return_value=None,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="test-thread",
                code_path="/tmp/test-repo",
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is False
            assert "error" in result_data
            assert "unavailable" in result_data["error"].lower()

    async def test_run_pipeline_dry_run(self, mock_leanrag_backend, mock_context):
        """Test dry-run mode."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        with patch(
            "watercooler_mcp.tools.memory._get_leanrag_backend",
            return_value=mock_leanrag_backend,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="auth-feature",
                code_path="/tmp/test-repo",
                dry_run=True,
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is True
            assert result_data.get("dry_run") is True


class TestSmartQueryTool:
    """Tests for watercooler_smart_query tool."""

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    async def test_smart_query_surfaces_context_errors(self, monkeypatch, mock_context):
        """Context resolution errors should be surfaced instead of hidden."""
        from watercooler_mcp.tools import memory as memory_tools

        # Force _require_context to fail
        monkeypatch.setattr(
            "watercooler_mcp.tools.memory.validation._require_context",
            lambda path: ("threads repo missing", None),
        )

        result = await memory_tools._smart_query_impl(
            query="auth history",
            ctx=mock_context,
            code_path="/repo",
            threads_dir="",
        )

        result_data = json.loads(result.content[0].text)
        assert result_data["result_count"] == 0
        assert result_data["error"] == "Context resolution failed"
        assert "threads repo missing" in result_data["message"]
        assert result_data.get("available_tiers") == []


class TestToolRegistration:
    """Test tool registration."""

    def test_graphiti_add_episode_registered(self):
        """Test graphiti_add_episode tool is registered."""
        from watercooler_mcp.tools.memory import graphiti_add_episode

        # Tool should exist after module import
        # (will be None until register_memory_tools is called)
        # This just verifies the module-level variable exists
        assert hasattr(
            __import__("watercooler_mcp.tools.memory", fromlist=["graphiti_add_episode"]),
            "graphiti_add_episode",
        )

    def test_leanrag_run_pipeline_registered(self):
        """Test leanrag_run_pipeline tool is registered."""
        from watercooler_mcp.tools.memory import leanrag_run_pipeline

        assert hasattr(
            __import__("watercooler_mcp.tools.memory", fromlist=["leanrag_run_pipeline"]),
            "leanrag_run_pipeline",
        )


class TestBulkIndexImpl:
    """Tests for _bulk_index_impl thread directory resolution."""

    @pytest.fixture
    def mock_context(self):
        return MagicMock()

    @pytest.fixture
    def threads_dir(self, tmp_path):
        """Create a fake threads directory with .md files and graph data."""
        from watercooler.baseline_graph import storage

        td = tmp_path / "repo-threads"
        td.mkdir()
        (td / "topic-a.md").write_text("# topic-a\n\nentry content A")
        (td / "topic-b.md").write_text("# topic-b\n\nentry content B")

        # Write graph data so list_thread_topics() discovers them
        graph_dir = storage.ensure_graph_dir(td)
        for topic in ("topic-a", "topic-b"):
            thread_dir = storage.ensure_thread_graph_dir(graph_dir, topic)
            storage.atomic_write_json(thread_dir / "meta.json", {
                "id": f"thread:{topic}",
                "topic": topic,
                "title": topic,
                "status": "OPEN",
                "ball": "",
                "last_updated": "",
            })
            storage.atomic_write_jsonl(thread_dir / "entries.jsonl", [])
        return td

    @pytest.fixture
    def mock_queue(self):
        queue = MagicMock()
        queue.status_summary.return_value = {
            "queue_depth": 0,
            "by_status": {},
            "oldest_task_age_s": None,
            "stats": {"total_enqueued": 0, "total_completed": 0,
                      "total_dead_lettered": 0, "total_retries": 0},
        }
        return queue

    async def test_resolve_threads_dir_uses_code_root(
        self, mock_context, threads_dir, mock_queue
    ):
        """Verify resolve_threads_dir is called with code_root kwarg, not cli_value."""
        from watercooler_mcp.tools.memory import _bulk_index_impl

        code_path = "/some/repo"

        with patch("watercooler_mcp.memory_queue.get_queue", return_value=mock_queue), \
             patch("watercooler.commands.list_entries", return_value=[]), \
             patch(
                 "watercooler.path_resolver.resolve_threads_dir",
                 return_value=threads_dir,
             ) as mock_resolve:
            result = await _bulk_index_impl(
                ctx=mock_context, code_path=code_path, backend="graphiti",
            )

        # Key assertion: code_root keyword arg, NOT positional cli_value
        mock_resolve.assert_called_once_with(code_root=Path(code_path))

    async def test_bulk_index_discovers_topics(
        self, mock_context, threads_dir, mock_queue
    ):
        """Verify bulk_index discovers .md files as topics."""
        from watercooler_mcp.tools.memory import _bulk_index_impl

        with patch("watercooler_mcp.memory_queue.get_queue", return_value=mock_queue), \
             patch("watercooler.commands.list_entries", return_value=[]), \
             patch(
                 "watercooler.path_resolver.resolve_threads_dir",
                 return_value=threads_dir,
             ), \
             patch("watercooler_mcp.memory_queue.enqueue_memory_task", return_value="t1"):
            result = await _bulk_index_impl(
                ctx=mock_context, code_path="/repo", backend="graphiti",
            )

        data = json.loads(result.content[0].text)
        assert data["topics_scanned"] == 2

    async def test_bulk_index_code_path_none_returns_error(self, mock_context, mock_queue):
        """Verify graceful error when code_path is empty."""
        from watercooler_mcp.tools.memory import _bulk_index_impl

        with patch("watercooler_mcp.memory_queue.get_queue", return_value=mock_queue):
            result = await _bulk_index_impl(
                ctx=mock_context, code_path="", backend="graphiti",
            )

        data = json.loads(result.content[0].text)
        assert "error" in data
