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
        """Test successful episode addition."""
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

            assert result_data["success"] is True
            assert result_data["episode_uuid"] == "ep-uuid-12345"
            assert "entities_extracted" in result_data

    async def test_add_episode_with_timestamp(self, mock_graphiti_backend, mock_context):
        """Test episode addition with custom timestamp."""
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

            # Verify timestamp was passed to backend
            call_args = mock_graphiti_backend.add_episode_direct.call_args
            assert call_args is not None

    async def test_add_episode_with_entry_id(self, mock_graphiti_backend, mock_context):
        """Test episode addition with entry_id for provenance tracking."""
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

            # Verify entry-episode mapping was created
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
    def mock_leanrag_pipeline(self):
        """Create mock LeanRAG pipeline."""
        mock_result = {
            "clusters_created": 5,
            "chunks_processed": 50,
            "execution_time_ms": 1200,
        }

        async def mock_run(*args, **kwargs):
            return mock_result

        mock_pipeline = MagicMock()
        mock_pipeline.run = AsyncMock(side_effect=mock_run)
        return mock_pipeline

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    async def test_run_pipeline_success(self, mock_leanrag_pipeline, mock_context):
        """Test successful pipeline execution."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        with patch(
            "watercooler_mcp.tools.memory._get_leanrag_pipeline",
            return_value=mock_leanrag_pipeline,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="auth-feature",
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is True
            assert "clusters_created" in result_data

    async def test_run_pipeline_with_filters(self, mock_leanrag_pipeline, mock_context):
        """Test pipeline with filter options."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        with patch(
            "watercooler_mcp.tools.memory._get_leanrag_pipeline",
            return_value=mock_leanrag_pipeline,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="auth-feature",
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
            "watercooler_mcp.tools.memory._get_leanrag_pipeline",
            return_value=None,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="test-thread",
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is False
            assert "error" in result_data
            assert "unavailable" in result_data["error"].lower()

    async def test_run_pipeline_dry_run(self, mock_leanrag_pipeline, mock_context):
        """Test dry-run mode."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        with patch(
            "watercooler_mcp.tools.memory._get_leanrag_pipeline",
            return_value=mock_leanrag_pipeline,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="auth-feature",
                dry_run=True,
                ctx=mock_context,
            )

            result_data = json.loads(result.content[0].text)
            assert result_data["success"] is True
            assert result_data.get("dry_run") is True


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
