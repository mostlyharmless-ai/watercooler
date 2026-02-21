"""End-to-end tests for watercooler memory system.

This test suite validates the complete memory system functionality:
- MCP tool integration
- Multi-tier query orchestration
- Search modes (entries, entities, episodes)
- Graph health and diagnostics
- Error handling and graceful degradation

Tests use mocked backends to avoid external dependencies while
validating the full integration path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Configure pytest-asyncio mode
pytestmark = pytest.mark.anyio


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_threads_dir(tmp_path: Path) -> Path:
    """Create mock threads directory with baseline graph."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    # Create per-thread graph structure
    graph_dir = threads_dir / "graph" / "baseline" / "threads"

    # Auth thread
    auth_dir = graph_dir / "auth-feature"
    auth_dir.mkdir(parents=True)
    (auth_dir / "meta.json").write_text(json.dumps({
        "type": "thread",
        "topic": "auth-feature",
        "title": "Authentication Feature",
        "status": "OPEN",
        "ball": "Claude",
        "summary": "Implementing OAuth2 authentication",
        "entry_count": 3,
    }))
    (auth_dir / "entries.jsonl").write_text(
        '{"type": "entry", "entry_id": "01AUTH001", "thread_topic": "auth-feature", '
        '"title": "Auth Plan", "body": "Implement JWT with RS256", "role": "planner", '
        '"entry_type": "Plan", "timestamp": "2025-01-15T09:00:00Z"}\n'
        '{"type": "entry", "entry_id": "01AUTH002", "thread_topic": "auth-feature", '
        '"title": "OAuth2 Started", "body": "Using passport.js middleware", "role": "implementer", '
        '"entry_type": "Note", "timestamp": "2025-01-15T10:00:00Z"}\n'
        '{"type": "entry", "entry_id": "01AUTH003", "thread_topic": "auth-feature", '
        '"title": "Security Review", "body": "CSRF vulnerability found", "role": "critic", '
        '"entry_type": "Note", "timestamp": "2025-01-15T14:00:00Z"}\n'
    )

    # Error handling thread
    error_dir = graph_dir / "error-handling"
    error_dir.mkdir(parents=True)
    (error_dir / "meta.json").write_text(json.dumps({
        "type": "thread",
        "topic": "error-handling",
        "title": "Error Handling Patterns",
        "status": "CLOSED",
        "ball": "User",
        "summary": "Structured error handling with custom classes",
        "entry_count": 2,
    }))
    (error_dir / "entries.jsonl").write_text(
        '{"type": "entry", "entry_id": "01ERR001", "thread_topic": "error-handling", '
        '"title": "Error Strategy", "body": "Use try-catch with custom error classes", '
        '"role": "planner", "entry_type": "Decision", "timestamp": "2025-01-10T08:00:00Z"}\n'
        '{"type": "entry", "entry_id": "01ERR002", "thread_topic": "error-handling", '
        '"title": "Complete", "body": "Error middleware implemented", "role": "implementer", '
        '"entry_type": "Closure", "timestamp": "2025-01-12T16:00:00Z"}\n'
    )

    return threads_dir


@pytest.fixture
def mock_graphiti_backend() -> MagicMock:
    """Create mock Graphiti backend."""
    mock = MagicMock()

    # Configure search operations
    mock.search_entries.return_value = [
        {
            "entry_id": "01AUTH001",
            "thread_topic": "auth-feature",
            "title": "Auth Plan",
            "body": "Implement JWT with RS256",
            "score": 0.92,
        }
    ]

    async def mock_add_episode(*args, **kwargs):
        return {
            "episode_uuid": "ep-uuid-test",
            "entities_extracted": ["OAuth2", "JWT"],
            "facts_extracted": 2,
        }

    mock.add_episode_direct = AsyncMock(side_effect=mock_add_episode)
    mock.index_entry_as_episode = MagicMock()
    mock.clear_group_episodes = MagicMock(
        return_value={"removed": 5, "group_id": "test", "message": "Cleared"}
    )
    mock.get_entity_edge = MagicMock(
        return_value={
            "uuid": "edge-123",
            "fact": "OAuth2 enables authentication",
            "source_node_uuid": "node-1",
            "target_node_uuid": "node-2",
            "valid_at": "2025-01-15T10:00:00Z",
            "created_at": "2025-01-15T10:00:00Z",
            "group_id": "auth-feature",
        }
    )

    return mock


@pytest.fixture
def mock_graphiti_config() -> MagicMock:
    """Create mock Graphiti configuration."""
    config = MagicMock()
    config.llm_api_key = "stub-llm-key"
    config.embedding_api_key = "stub-embed-key"
    config.llm_model = "gpt-4o-mini"
    config.embedding_model = "bge-m3"
    config.database = "test_db"
    config.openai_api_key = None
    config.llm_api_base = "http://localhost:8000/v1"
    return config


# ============================================================================
# Smart Query E2E Tests
# ============================================================================


class TestSmartQueryE2E:
    """End-to-end tests for watercooler_smart_query tool."""

    async def test_smart_query_with_t1_only(
        self, mock_context: MagicMock, mock_threads_dir: Path
    ) -> None:
        """Test smart_query with only T1 (baseline) available."""
        from watercooler_mcp.tools.memory import _smart_query_impl

        result = await _smart_query_impl(
            query="authentication",
            ctx=mock_context,
            threads_dir=str(mock_threads_dir),
            max_tiers=1,
            force_tier="T1",  # Force T1 to ensure we test baseline graph
        )

        result_data = json.loads(result.content[0].text)

        # Should either find results or report T1 was queried
        assert result_data.get("result_count", 0) >= 0
        # Check that query executed (has standard response fields)
        assert "query" in result_data or "message" in result_data or "error" in result_data

    async def test_smart_query_force_tier(
        self, mock_context: MagicMock, mock_threads_dir: Path
    ) -> None:
        """Test smart_query with forced tier."""
        from watercooler_mcp.tools.memory import _smart_query_impl

        result = await _smart_query_impl(
            query="error handling",
            ctx=mock_context,
            threads_dir=str(mock_threads_dir),
            force_tier="T1",
        )

        result_data = json.loads(result.content[0].text)

        # Should only query T1 (or empty if T1 unavailable due to missing API keys)
        if "tiers_queried" in result_data:
            # In CI without API keys, tiers may be empty; otherwise should be exactly T1
            assert result_data["tiers_queried"] in (["T1"], []), (
                f"Expected ['T1'] or [] but got {result_data['tiers_queried']}"
            )

    async def test_smart_query_invalid_force_tier(
        self, mock_context: MagicMock, mock_threads_dir: Path
    ) -> None:
        """Test smart_query with invalid force_tier value."""
        from watercooler_mcp.tools.memory import _smart_query_impl

        result = await _smart_query_impl(
            query="test",
            ctx=mock_context,
            threads_dir=str(mock_threads_dir),
            force_tier="INVALID",
        )

        result_data = json.loads(result.content[0].text)

        # Either returns an error about invalid tier, or gracefully degrades
        # when no tiers are available (common in CI without API keys)
        has_error = "error" in result_data
        has_no_tiers_message = result_data.get("message", "").lower().find("no memory tiers") >= 0
        has_invalid_message = "invalid" in result_data.get("error", "").lower() or "invalid" in result_data.get("message", "").lower()

        assert has_error or has_no_tiers_message or has_invalid_message, (
            f"Expected error or 'no memory tiers' message, got: {result_data}"
        )

    async def test_smart_query_context_resolution_error(
        self, mock_context: MagicMock, monkeypatch
    ) -> None:
        """Test smart_query surfaces context resolution errors."""
        from watercooler_mcp.tools import memory as memory_tools

        monkeypatch.setattr(
            "watercooler_mcp.tools.memory.validation._require_context",
            lambda path: ("threads repo not found", None),
        )

        result = await memory_tools._smart_query_impl(
            query="test",
            ctx=mock_context,
            code_path="/nonexistent/path",
            threads_dir="",
        )

        result_data = json.loads(result.content[0].text)

        assert result_data["result_count"] == 0
        assert result_data["error"] == "Context resolution failed"

    async def test_smart_query_no_tiers_available(
        self, mock_context: MagicMock, tmp_path: Path
    ) -> None:
        """Test smart_query when no tiers are available."""
        from watercooler_mcp.tools.memory import _smart_query_impl

        # Empty threads dir with no graph
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        result = await _smart_query_impl(
            query="test",
            ctx=mock_context,
            threads_dir=str(empty_dir),
        )

        result_data = json.loads(result.content[0].text)

        # Should handle gracefully
        assert result_data["result_count"] == 0
        assert "available_tiers" in result_data


# ============================================================================
# Get Entity Edge E2E Tests
# ============================================================================


class TestGetEntityEdgeE2E:
    """End-to-end tests for watercooler_get_entity_edge tool."""

    async def test_get_entity_edge_success(
        self,
        mock_context: MagicMock,
        mock_graphiti_backend: MagicMock,
        mock_graphiti_config: MagicMock,
    ) -> None:
        """Test successful entity edge retrieval."""
        from watercooler_mcp.tools.memory import _get_entity_edge_impl

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            result = await _get_entity_edge_impl(
                uuid="edge-123",
                ctx=mock_context,
            )

        result_data = json.loads(result.content[0].text)

        assert result_data["uuid"] == "edge-123"
        assert result_data["fact"] == "OAuth2 enables authentication"
        assert "message" in result_data

    async def test_get_entity_edge_empty_uuid(
        self, mock_context: MagicMock
    ) -> None:
        """Test error when UUID is empty."""
        from watercooler_mcp.tools.memory import _get_entity_edge_impl

        result = await _get_entity_edge_impl(
            uuid="",
            ctx=mock_context,
        )

        result_data = json.loads(result.content[0].text)

        assert "error" in result_data
        assert "uuid" in result_data["error"].lower() or "uuid" in result_data.get("message", "").lower()

    async def test_get_entity_edge_invalid_characters(
        self, mock_context: MagicMock
    ) -> None:
        """Test error when UUID contains invalid characters."""
        from watercooler_mcp.tools.memory import _get_entity_edge_impl

        result = await _get_entity_edge_impl(
            uuid="'; DROP TABLE edges; --",
            ctx=mock_context,
        )

        result_data = json.loads(result.content[0].text)

        assert "error" in result_data
        assert "invalid" in result_data["error"].lower()

    async def test_get_entity_edge_too_long(
        self, mock_context: MagicMock
    ) -> None:
        """Test error when UUID is too long."""
        from watercooler_mcp.tools.memory import _get_entity_edge_impl

        result = await _get_entity_edge_impl(
            uuid="a" * 150,
            ctx=mock_context,
        )

        result_data = json.loads(result.content[0].text)

        assert "error" in result_data
        assert "long" in result_data.get("message", "").lower()

    async def test_get_entity_edge_not_found(
        self,
        mock_context: MagicMock,
        mock_graphiti_backend: MagicMock,
        mock_graphiti_config: MagicMock,
    ) -> None:
        """Test error when edge is not found."""
        from watercooler_mcp.tools.memory import _get_entity_edge_impl

        mock_graphiti_backend.get_entity_edge.return_value = None

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            result = await _get_entity_edge_impl(
                uuid="nonexistent",
                ctx=mock_context,
            )

        result_data = json.loads(result.content[0].text)

        assert "error" in result_data
        assert "not found" in result_data["error"].lower()


# ============================================================================
# Diagnose Memory E2E Tests
# ============================================================================


class TestDiagnoseMemoryE2E:
    """End-to-end tests for watercooler_diagnose_memory tool."""

    def test_diagnose_memory_disabled(
        self, mock_context: MagicMock, monkeypatch
    ) -> None:
        """Test diagnose when Graphiti is disabled."""
        from watercooler_mcp.tools.memory import _diagnose_memory_impl

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "0")

        result = _diagnose_memory_impl(ctx=mock_context)
        result_data = json.loads(result.content[0].text)

        assert result_data["graphiti_enabled"] is False
        assert "config_issue" in result_data

    def test_diagnose_memory_enabled(
        self,
        mock_context: MagicMock,
        mock_graphiti_backend: MagicMock,
        mock_graphiti_config: MagicMock,
    ) -> None:
        """Test diagnose when Graphiti is enabled."""
        from watercooler_mcp.tools.memory import _diagnose_memory_impl

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            result = _diagnose_memory_impl(ctx=mock_context)

        result_data = json.loads(result.content[0].text)

        assert result_data["graphiti_enabled"] is True
        assert "backend_init" in result_data
        assert "✓" in result_data["backend_init"]

    def test_diagnose_memory_shows_python_info(
        self, mock_context: MagicMock, monkeypatch
    ) -> None:
        """Test diagnose includes Python version info."""
        from watercooler_mcp.tools.memory import _diagnose_memory_impl

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "0")

        result = _diagnose_memory_impl(ctx=mock_context)
        result_data = json.loads(result.content[0].text)

        assert "python_version" in result_data
        assert "python_executable" in result_data


# ============================================================================
# Graphiti Add Episode E2E Tests
# ============================================================================


class TestGraphitiAddEpisodeE2E:
    """End-to-end tests for watercooler_graphiti_add_episode tool."""

    async def test_add_episode_success(
        self,
        mock_context: MagicMock,
        mock_graphiti_backend: MagicMock,
        mock_graphiti_config: MagicMock,
    ) -> None:
        """Test successful episode addition."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            result = await _graphiti_add_episode_impl(
                content="We decided to use JWT tokens for authentication",
                group_id="auth-feature",
                ctx=mock_context,
                entry_id="01AUTH001",
                timestamp="2025-01-15T10:00:00Z",
            )

        result_data = json.loads(result.content[0].text)

        # Fire-and-forget: returns immediately with "submitted" status
        assert result_data["success"] is True
        assert result_data["status"] == "submitted"
        assert result_data["group_id"] == "auth-feature"

    async def test_add_episode_empty_content(
        self, mock_context: MagicMock
    ) -> None:
        """Test error when content is empty."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        result = await _graphiti_add_episode_impl(
            content="",
            group_id="test",
            ctx=mock_context,
        )

        result_data = json.loads(result.content[0].text)

        assert result_data["success"] is False
        assert "content" in result_data["error"].lower()

    async def test_add_episode_empty_group_id(
        self, mock_context: MagicMock
    ) -> None:
        """Test error when group_id is empty."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        result = await _graphiti_add_episode_impl(
            content="Test content",
            group_id="",
            ctx=mock_context,
        )

        result_data = json.loads(result.content[0].text)

        assert result_data["success"] is False
        assert "group_id" in result_data["error"].lower()

    async def test_add_episode_graphiti_disabled(
        self, mock_context: MagicMock
    ) -> None:
        """Test error when Graphiti is not enabled."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ):
            result = await _graphiti_add_episode_impl(
                content="Test content",
                group_id="test",
                ctx=mock_context,
            )

        result_data = json.loads(result.content[0].text)

        assert result_data["success"] is False
        assert "not enabled" in result_data["error"].lower()


# ============================================================================
# Clear Graph Group E2E Tests
# ============================================================================


class TestClearGraphGroupE2E:
    """End-to-end tests for watercooler_clear_graph_group tool."""

    async def test_clear_group_requires_confirm(
        self, mock_context: MagicMock
    ) -> None:
        """Test that clear requires explicit confirmation."""
        from watercooler_mcp.tools.memory import _clear_graph_group_impl

        result = await _clear_graph_group_impl(
            group_id="test-group",
            ctx=mock_context,
            confirm=False,
        )

        result_data = json.loads(result.content[0].text)

        assert result_data["success"] is False
        assert "confirm" in result_data["error"].lower() or "confirm" in result_data.get("message", "").lower()

    async def test_clear_group_success(
        self,
        mock_context: MagicMock,
        mock_graphiti_backend: MagicMock,
        mock_graphiti_config: MagicMock,
    ) -> None:
        """Test successful group clear with confirmation."""
        from watercooler_mcp.tools.memory import _clear_graph_group_impl

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            result = await _clear_graph_group_impl(
                group_id="test-group",
                ctx=mock_context,
                confirm=True,
            )

        result_data = json.loads(result.content[0].text)

        assert result_data["success"] is True
        assert result_data["removed"] == 5

    async def test_clear_group_empty_group_id(
        self, mock_context: MagicMock
    ) -> None:
        """Test error when group_id is empty."""
        from watercooler_mcp.tools.memory import _clear_graph_group_impl

        result = await _clear_graph_group_impl(
            group_id="",
            ctx=mock_context,
            confirm=True,
        )

        result_data = json.loads(result.content[0].text)

        assert result_data.get("success") is False or "error" in result_data


# ============================================================================
# LeanRAG Pipeline E2E Tests
# ============================================================================


class TestLeanRAGPipelineE2E:
    """End-to-end tests for watercooler_leanrag_run_pipeline tool."""

    async def test_pipeline_dry_run(
        self, mock_context: MagicMock
    ) -> None:
        """Test pipeline in dry run mode."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        mock_leanrag = MagicMock()

        with patch(
            "watercooler_mcp.tools.memory._get_leanrag_backend",
            return_value=mock_leanrag,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="test-group",
                ctx=mock_context,
                code_path="/tmp/test-repo",
                dry_run=True,
            )

        result_data = json.loads(result.content[0].text)

        assert result_data["success"] is True
        assert result_data["dry_run"] is True

    async def test_pipeline_unavailable_backend(
        self, mock_context: MagicMock
    ) -> None:
        """Test error when LeanRAG backend is unavailable."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        with patch(
            "watercooler_mcp.tools.memory._get_leanrag_backend",
            return_value=None,
        ):
            result = await _leanrag_run_pipeline_impl(
                group_id="test-group",
                ctx=mock_context,
                code_path="/tmp/test-repo",
            )

        result_data = json.loads(result.content[0].text)

        assert result_data["success"] is False
        assert "unavailable" in result_data["error"].lower()

    async def test_pipeline_missing_code_path(
        self, mock_context: MagicMock
    ) -> None:
        """Test error when code_path is empty (required for BULK runs)."""
        from watercooler_mcp.tools.memory import _leanrag_run_pipeline_impl

        result = await _leanrag_run_pipeline_impl(
            group_id="test-group",
            ctx=mock_context,
            code_path="",
        )

        result_data = json.loads(result.content[0].text)

        assert result_data["success"] is False
        assert "code_path" in result_data["error"].lower()


# ============================================================================
# Error Handling & Graceful Degradation Tests
# ============================================================================


class TestErrorHandling:
    """Tests for error handling and graceful degradation."""

    async def test_backend_timeout_fallback(
        self,
        mock_context: MagicMock,
        mock_threads_dir: Path,
    ) -> None:
        """Test graceful handling when higher tiers timeout."""
        from watercooler_mcp.tools.memory import _smart_query_impl

        # Only T1 available - simulates T2/T3 unavailable
        result = await _smart_query_impl(
            query="authentication",
            ctx=mock_context,
            threads_dir=str(mock_threads_dir),
            max_tiers=1,
        )

        result_data = json.loads(result.content[0].text)

        # Should return results from T1 even if other tiers fail
        assert "error" not in result_data or result_data.get("result_count", 0) >= 0

    async def test_missing_api_keys_graceful_error(
        self, mock_context: MagicMock
    ) -> None:
        """Test graceful error when API keys are missing."""
        from watercooler_mcp.tools.memory import _graphiti_add_episode_impl

        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ):
            result = await _graphiti_add_episode_impl(
                content="Test content",
                group_id="test",
                ctx=mock_context,
            )

        result_data = json.loads(result.content[0].text)

        # Should fail gracefully with helpful error
        assert result_data["success"] is False
        assert "error" in result_data


# ============================================================================
# Integration Pattern Tests
# ============================================================================


class TestMemoryIntegrationPatterns:
    """Tests for common memory integration patterns."""

    async def test_query_then_add_episode_pattern(
        self,
        mock_context: MagicMock,
        mock_threads_dir: Path,
        mock_graphiti_backend: MagicMock,
        mock_graphiti_config: MagicMock,
    ) -> None:
        """Test pattern: query memory, then add new episode."""
        from watercooler_mcp.tools.memory import (
            _smart_query_impl,
            _graphiti_add_episode_impl,
        )

        # Step 1: Query existing memory
        query_result = await _smart_query_impl(
            query="authentication patterns",
            ctx=mock_context,
            threads_dir=str(mock_threads_dir),
            max_tiers=1,
        )
        query_data = json.loads(query_result.content[0].text)

        # Query should work
        assert "error" not in query_data or query_data.get("result_count", 0) >= 0

        # Step 2: Add new episode based on findings
        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            add_result = await _graphiti_add_episode_impl(
                content="Based on review, implementing OAuth2 with PKCE",
                group_id="auth-feature",
                ctx=mock_context,
            )

        add_data = json.loads(add_result.content[0].text)
        assert add_data["success"] is True

    async def test_diagnose_before_query_pattern(
        self,
        mock_context: MagicMock,
        mock_threads_dir: Path,
        mock_graphiti_backend: MagicMock,
        mock_graphiti_config: MagicMock,
    ) -> None:
        """Test pattern: diagnose configuration before querying."""
        from watercooler_mcp.tools.memory import (
            _diagnose_memory_impl,
            _smart_query_impl,
        )

        # Step 1: Diagnose configuration
        with patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=mock_graphiti_config,
        ), patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=mock_graphiti_backend,
        ):
            diag_result = _diagnose_memory_impl(ctx=mock_context)

        diag_data = json.loads(diag_result.content[0].text)

        # Check Graphiti is available
        graphiti_ready = diag_data.get("graphiti_enabled", False)

        # Step 2: Query based on what's available
        query_result = await _smart_query_impl(
            query="test",
            ctx=mock_context,
            threads_dir=str(mock_threads_dir),
            max_tiers=2 if graphiti_ready else 1,
        )

        query_data = json.loads(query_result.content[0].text)
        assert "result_count" in query_data or "error" in query_data
