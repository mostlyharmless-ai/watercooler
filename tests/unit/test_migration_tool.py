"""Tests for memory backend migration tool.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 7:
- watercooler_migrate_to_memory_backend tool
- Dry-run by default
- Resume from checkpoint
- Preflight checks
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Configure pytest-asyncio mode
pytestmark = pytest.mark.anyio


class TestMigrationPreflight:
    """Tests for migration preflight checks."""

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        """Create mock threads directory with sample threads."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create sample thread files using correct Entry: format
        thread1 = threads_dir / "auth-feature.md"
        thread1.write_text("""# auth-feature — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: Test entry
<!-- Entry-ID: 01ABC123 -->

Test content here.
""")

        thread2 = threads_dir / "api-design.md"
        thread2.write_text("""# api-design — Thread

Status: CLOSED
Ball: -

---

Entry: Human (dev) 2025-01-14T09:00:00Z
Role: planner
Type: Plan
Title: API design notes
<!-- Entry-ID: 01DEF456 -->

API design notes.
""")

        return threads_dir

    async def test_preflight_checks_threads_exist(self, mock_context, mock_threads_dir):
        """Preflight should verify threads directory exists."""
        from watercooler_mcp.tools.migration import _migration_preflight_impl

        result = await _migration_preflight_impl(
            threads_dir=mock_threads_dir,
            backend="graphiti",
            ctx=mock_context,
        )

        result_data = json.loads(result)
        assert result_data["threads_dir_exists"] is True
        assert result_data["thread_count"] >= 1

    async def test_preflight_checks_backend_availability(self, mock_context, mock_threads_dir):
        """Preflight should check if target backend is available."""
        from watercooler_mcp.tools.migration import _migration_preflight_impl

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True, "version": "1.0.0"},
        ):
            result = await _migration_preflight_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                ctx=mock_context,
            )

            result_data = json.loads(result)
            assert result_data["backend_available"] is True

    async def test_preflight_fails_for_missing_threads(self, mock_context, tmp_path):
        """Preflight should fail if threads directory doesn't exist."""
        from watercooler_mcp.tools.migration import _migration_preflight_impl

        missing_dir = tmp_path / "nonexistent"

        result = await _migration_preflight_impl(
            threads_dir=missing_dir,
            backend="graphiti",
            ctx=mock_context,
        )

        result_data = json.loads(result)
        assert result_data["threads_dir_exists"] is False
        assert result_data["ready"] is False

    async def test_preflight_estimates_migration_scope(self, mock_context, mock_threads_dir):
        """Preflight should estimate entries to migrate."""
        from watercooler_mcp.tools.migration import _migration_preflight_impl

        result = await _migration_preflight_impl(
            threads_dir=mock_threads_dir,
            backend="graphiti",
            ctx=mock_context,
        )

        result_data = json.loads(result)
        assert "estimated_entries" in result_data
        assert result_data["estimated_entries"] >= 0


class TestMigrationDryRun:
    """Tests for migration dry-run mode."""

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        """Create mock threads directory."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        thread = threads_dir / "test-thread.md"
        thread.write_text("""# test-thread — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: Test entry
<!-- Entry-ID: 01TEST01 -->

Test entry content.
""")

        return threads_dir

    async def test_dry_run_default_enabled(self, mock_context, mock_threads_dir):
        """Dry-run should be enabled by default."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                ctx=mock_context,
            )

            result_data = json.loads(result)
            assert result_data["dry_run"] is True

    async def test_dry_run_shows_what_would_be_migrated(self, mock_context, mock_threads_dir):
        """Dry-run should show what would be migrated without executing."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                ctx=mock_context,
                dry_run=True,
            )

            result_data = json.loads(result)
            assert "would_migrate" in result_data
            assert result_data["entries_migrated"] == 0

    async def test_dry_run_does_not_call_backend(self, mock_context, mock_threads_dir):
        """Dry-run should not call the actual backend."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock()

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ), patch(
            "watercooler_mcp.tools.migration._get_migration_backend",
            return_value=mock_graphiti,
        ):
            await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                ctx=mock_context,
                dry_run=True,
            )

            mock_graphiti.add_episode_direct.assert_not_called()


class TestMigrationExecution:
    """Tests for actual migration execution."""

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        """Create mock threads directory with entries."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        thread = threads_dir / "test-thread.md"
        thread.write_text("""# test-thread — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: First entry
<!-- Entry-ID: 01ABC123 -->

First entry content.

---

Entry: Human (dev) 2025-01-15T11:00:00Z
Role: reviewer
Type: Note
Title: Second entry
<!-- Entry-ID: 01ABC456 -->

Second entry content.
""")

        return threads_dir

    async def test_actual_migration_calls_backend(self, mock_context, mock_threads_dir):
        """Actual migration should call backend for each entry."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(
            return_value={"episode_uuid": "ep-123"}
        )

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ), patch(
            "watercooler_mcp.tools.migration._get_migration_backend",
            return_value=mock_graphiti,
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                ctx=mock_context,
                dry_run=False,
            )

            result_data = json.loads(result)
            assert result_data["dry_run"] is False
            assert result_data["entries_migrated"] > 0

    async def test_migration_tracks_progress(self, mock_context, mock_threads_dir):
        """Migration should track progress and return stats."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(
            return_value={"episode_uuid": "ep-123"}
        )

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ), patch(
            "watercooler_mcp.tools.migration._get_migration_backend",
            return_value=mock_graphiti,
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                ctx=mock_context,
                dry_run=False,
            )

            result_data = json.loads(result)
            assert "entries_migrated" in result_data
            assert "entries_failed" in result_data
            assert "threads_processed" in result_data


class TestMigrationCheckpoint:
    """Tests for migration checkpoint/resume functionality."""

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        """Create mock threads directory."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        return threads_dir

    async def test_checkpoint_created_during_migration(self, mock_context, mock_threads_dir):
        """Migration should create checkpoint file for resume."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        # Create a thread
        thread = mock_threads_dir / "test-thread.md"
        thread.write_text("""# test-thread — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: Test entry
<!-- Entry-ID: 01ABC123 -->

Content.
""")

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(
            return_value={"episode_uuid": "ep-123"}
        )

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ), patch(
            "watercooler_mcp.tools.migration._get_migration_backend",
            return_value=mock_graphiti,
        ):
            await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                ctx=mock_context,
                dry_run=False,
            )

            # Check checkpoint file was created
            checkpoint_file = mock_threads_dir / ".migration_checkpoint.json"
            assert checkpoint_file.exists()

    async def test_resume_from_checkpoint(self, mock_context, mock_threads_dir):
        """Migration should resume from checkpoint if available."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        # Create checkpoint indicating some entries already migrated
        checkpoint = mock_threads_dir / ".migration_checkpoint.json"
        checkpoint.write_text(json.dumps({
            "migrated_entries": ["01ABC123"],
            "backend": "graphiti",
            "last_updated": "2025-01-15T10:00:00Z",
        }))

        # Create thread with the already-migrated entry
        thread = mock_threads_dir / "test-thread.md"
        thread.write_text("""# test-thread — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: Already migrated
<!-- Entry-ID: 01ABC123 -->

Already migrated content.
""")

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(
            return_value={"episode_uuid": "ep-123"}
        )

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ), patch(
            "watercooler_mcp.tools.migration._get_migration_backend",
            return_value=mock_graphiti,
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                ctx=mock_context,
                dry_run=False,
            )

            result_data = json.loads(result)
            # Should skip the already-migrated entry
            assert result_data["entries_skipped"] >= 1


class TestMigrationFilters:
    """Tests for migration filtering options."""

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        """Create mock threads directory with multiple threads."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Open thread
        (threads_dir / "open-thread.md").write_text("""# open-thread — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: Open entry
<!-- Entry-ID: 01OPEN123 -->

Open thread content.
""")

        # Closed thread
        (threads_dir / "closed-thread.md").write_text("""# closed-thread — Thread

Status: CLOSED
Ball: -

---

Entry: Claude (dev) 2025-01-14T10:00:00Z
Role: implementer
Type: Note
Title: Closed entry
<!-- Entry-ID: 01CLOSED123 -->

Closed thread content.
""")

        return threads_dir

    async def test_filter_by_topic(self, mock_context, mock_threads_dir):
        """Migration should support filtering by topic."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                topics="open-thread",
                ctx=mock_context,
                dry_run=True,
            )

            result_data = json.loads(result)
            # Should only include the open-thread
            assert len(result_data.get("would_migrate", [])) <= 1

    async def test_filter_skip_closed(self, mock_context, mock_threads_dir):
        """Migration should support skipping closed threads."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir,
                backend="graphiti",
                skip_closed=True,
                ctx=mock_context,
                dry_run=True,
            )

            result_data = json.loads(result)
            # Should only include open threads
            topics = [e.get("topic") for e in result_data.get("would_migrate", [])]
            assert "closed-thread" not in topics


class TestToolRegistration:
    """Test tool registration."""

    def test_migrate_tool_registered(self):
        """Test migration tool is registered."""
        assert hasattr(
            __import__("watercooler_mcp.tools.migration", fromlist=["migrate_to_memory_backend"]),
            "migrate_to_memory_backend",
        )

    def test_preflight_tool_registered(self):
        """Test preflight tool is registered."""
        assert hasattr(
            __import__("watercooler_mcp.tools.migration", fromlist=["migration_preflight"]),
            "migration_preflight",
        )
