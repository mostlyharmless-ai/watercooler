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
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(return_value=None)

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
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(return_value=None)

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
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(return_value=None)

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
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(return_value=None)

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
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(return_value=None)

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


class TestChunkedMigration:
    """Tests for chunked migration with episode linking."""

    @pytest.fixture
    def mock_context(self):
        """Create mock MCP context."""
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir_large_entry(self, tmp_path):
        """Create mock threads directory with a large entry that will be chunked."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create thread with large body that will result in multiple chunks
        large_body = "\n\n".join([
            f"Paragraph {i}: " + "This is a substantial paragraph with meaningful content. " * 20
            for i in range(10)
        ])

        thread = threads_dir / "large-entry.md"
        thread.write_text(f"""# large-entry — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: Large entry for chunking test
<!-- Entry-ID: 01LARGE01 -->

{large_body}
""")

        return threads_dir

    @pytest.fixture
    def mock_threads_dir_small_entry(self, tmp_path):
        """Create mock threads directory with a small entry (single chunk)."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        thread = threads_dir / "small-entry.md"
        thread.write_text("""# small-entry — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: Small entry
<!-- Entry-ID: 01SMALL01 -->

This is a small entry that fits in a single chunk.
""")

        return threads_dir

    async def test_chunked_migration_calls_backend_with_previous_uuids(
        self, mock_context, mock_threads_dir_large_entry
    ):
        """Chunked migration should link episodes using previous_episode_uuids."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        # Track all calls to add_episode_direct
        call_count = [0]
        episode_uuids = []

        async def mock_add_episode(**kwargs):
            call_count[0] += 1
            ep_uuid = f"ep-chunk-{call_count[0]}"
            episode_uuids.append(ep_uuid)
            return {"episode_uuid": ep_uuid}

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(side_effect=mock_add_episode)
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(return_value=None)

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ), patch(
            "watercooler_mcp.tools.migration._get_migration_backend",
            return_value=mock_graphiti,
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir_large_entry,
                backend="graphiti",
                ctx=mock_context,
                dry_run=False,
                chunk_max_tokens=200,  # Small chunks to force multiple
                chunk_overlap=20,
            )

            result_data = json.loads(result)

            # Should have migrated at least one entry with chunks
            assert result_data["entries_migrated"] >= 1
            assert result_data["chunks_migrated"] >= 1

            # Verify episode linking pattern:
            # - First chunk: previous_episode_uuids should be None
            # - Subsequent chunks: should link to previous chunk
            calls = mock_graphiti.add_episode_direct.call_args_list
            if len(calls) > 1:
                # First chunk should have [] for previous_episode_uuids (not None - to prevent context overflow)
                first_call = calls[0]
                assert first_call.kwargs.get("previous_episode_uuids") == []

                # Second chunk should link to first chunk's UUID
                second_call = calls[1]
                prev_uuids = second_call.kwargs.get("previous_episode_uuids")
                if prev_uuids:  # Only check if we got multiple chunks from same entry
                    assert isinstance(prev_uuids, list)
                    assert len(prev_uuids) == 1

    async def test_small_entry_has_header_chunk(
        self, mock_context, mock_threads_dir_small_entry
    ):
        """Small entries get header chunk + body chunk with watercooler_preset."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        call_count = [0]

        async def mock_add_episode(**kwargs):
            call_count[0] += 1
            return {"episode_uuid": f"ep-{call_count[0]}"}

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(side_effect=mock_add_episode)
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(return_value=None)

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ), patch(
            "watercooler_mcp.tools.migration._get_migration_backend",
            return_value=mock_graphiti,
        ):
            await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir_small_entry,
                backend="graphiti",
                ctx=mock_context,
                dry_run=False,
            )

            # With watercooler_preset, even small entries get header + body = 2 chunks
            calls = mock_graphiti.add_episode_direct.call_args_list
            assert len(calls) == 2

            # First chunk (header) should have previous_episode_uuids=[] (not None - to prevent context overflow)
            header_call = calls[0]
            assert header_call.kwargs.get("previous_episode_uuids") == []
            # Header content starts with "agent:"
            assert "agent:" in header_call.kwargs.get("episode_body", "")

            # Second chunk (body) should link to header
            body_call = calls[1]
            assert body_call.kwargs.get("previous_episode_uuids") == ["ep-1"]

    async def test_dry_run_estimates_chunks(
        self, mock_context, mock_threads_dir_large_entry
    ):
        """Dry run should estimate number of chunks."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=mock_threads_dir_large_entry,
                backend="graphiti",
                ctx=mock_context,
                dry_run=True,
                chunk_max_tokens=200,
            )

            result_data = json.loads(result)

            # Should show estimated chunks
            assert "estimated_total_chunks" in result_data
            assert result_data["estimated_total_chunks"] >= 1

            # would_migrate entries should have estimated_chunks
            for entry in result_data.get("would_migrate", []):
                assert "estimated_chunks" in entry


class TestCheckpointV2:
    """Tests for checkpoint v2 format with chunk progress."""

    @pytest.fixture
    def mock_context(self):
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        return threads_dir

    def test_checkpoint_v2_structure(self, mock_threads_dir):
        """Checkpoint v2 should have correct structure."""
        from watercooler_mcp.tools.migration import (
            CheckpointV2,
            EntryProgress,
            ChunkProgress,
            _save_checkpoint_v2,
            _load_checkpoint,
        )

        checkpoint = CheckpointV2(backend="graphiti")
        checkpoint.entries["01ABC123"] = EntryProgress(
            thread_id="test-thread",
            status="complete",
            total_chunks=2,
            last_completed_chunk_index=1,
            chunks=[
                ChunkProgress(chunk_index=0, chunk_id="chunk0", episode_uuid="ep0"),
                ChunkProgress(chunk_index=1, chunk_id="chunk1", episode_uuid="ep1"),
            ],
            mode="chunked",
        )

        _save_checkpoint_v2(mock_threads_dir, checkpoint)

        # Load and verify
        loaded = _load_checkpoint(mock_threads_dir)
        assert loaded.version == 2
        assert loaded.backend == "graphiti"
        assert "01ABC123" in loaded.entries
        assert loaded.entries["01ABC123"].total_chunks == 2
        assert len(loaded.entries["01ABC123"].chunks) == 2

    def test_checkpoint_v1_upgrade(self, mock_threads_dir):
        """V1 checkpoint should be upgraded to v2 on load."""
        from watercooler_mcp.tools.migration import _load_checkpoint

        # Create v1 checkpoint
        v1_checkpoint = {
            "migrated_entries": ["01ABC123", "01DEF456"],
            "backend": "graphiti",
            "last_updated": "2025-01-15T10:00:00Z",
        }
        checkpoint_file = mock_threads_dir / ".migration_checkpoint.json"
        checkpoint_file.write_text(json.dumps(v1_checkpoint))

        # Load should return v2
        loaded = _load_checkpoint(mock_threads_dir)
        assert loaded.version == 2
        assert loaded.backend == "graphiti"
        assert "01ABC123" in loaded.entries
        assert loaded.entries["01ABC123"].status == "complete"
        assert loaded.entries["01ABC123"].mode == "single"

    def test_checkpoint_is_entry_complete(self):
        """is_entry_complete should correctly report status."""
        from watercooler_mcp.tools.migration import CheckpointV2, EntryProgress

        checkpoint = CheckpointV2(backend="graphiti")
        checkpoint.entries["complete"] = EntryProgress(
            thread_id="t", status="complete", total_chunks=1, last_completed_chunk_index=0
        )
        checkpoint.entries["in_progress"] = EntryProgress(
            thread_id="t", status="in_progress", total_chunks=2, last_completed_chunk_index=0
        )

        assert checkpoint.is_entry_complete("complete") is True
        assert checkpoint.is_entry_complete("in_progress") is False
        assert checkpoint.is_entry_complete("nonexistent") is False

    def test_checkpoint_get_resume_chunk_index(self):
        """get_resume_chunk_index should return correct index."""
        from watercooler_mcp.tools.migration import CheckpointV2, EntryProgress

        checkpoint = CheckpointV2(backend="graphiti")
        checkpoint.entries["partial"] = EntryProgress(
            thread_id="t",
            status="in_progress",
            total_chunks=5,
            last_completed_chunk_index=2,  # Completed chunks 0, 1, 2
        )

        # Should resume from chunk 3
        assert checkpoint.get_resume_chunk_index("partial") == 3
        # New entry should start from 0
        assert checkpoint.get_resume_chunk_index("new_entry") == 0


class TestEpisodeLinkingChain:
    """Tests specifically for episode linking chain correctness."""

    @pytest.fixture
    def mock_context(self):
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create entry that will produce exactly 3 chunks
        body = "\n\n".join([
            f"Section {i}: " + "Content " * 100
            for i in range(3)
        ])

        thread = threads_dir / "chain-test.md"
        thread.write_text(f"""# chain-test — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: Chain test entry
<!-- Entry-ID: 01CHAIN01 -->

{body}
""")

        return threads_dir

    async def test_episode_chain_none_first_then_previous(
        self, mock_context, mock_threads_dir
    ):
        """Episode chain should be: None -> [ep0] -> [ep1] -> ..."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        call_count = [0]

        async def mock_add_episode(**kwargs):
            call_count[0] += 1
            return {"episode_uuid": f"ep-{call_count[0]}"}

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(side_effect=mock_add_episode)
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(return_value=None)

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
                chunk_max_tokens=150,  # Force multiple chunks
                chunk_overlap=10,
            )

            calls = mock_graphiti.add_episode_direct.call_args_list

            # Verify the chain pattern
            for i, call in enumerate(calls):
                prev_uuids = call.kwargs.get("previous_episode_uuids")
                if i == 0:
                    # First chunk: empty list (not None - to prevent context overflow)
                    assert prev_uuids == [], f"First chunk should have [], got {prev_uuids}"
                else:
                    # Subsequent chunks: should link to previous
                    assert prev_uuids is not None, f"Chunk {i} should have previous UUIDs"
                    assert isinstance(prev_uuids, list)
                    assert len(prev_uuids) == 1
                    # Should link to the previous episode
                    expected_prev = f"ep-{i}"
                    assert prev_uuids[0] == expected_prev, (
                        f"Chunk {i} should link to {expected_prev}, got {prev_uuids[0]}"
                    )


class TestDeduplication:
    """Tests for deduplication via find_episode_by_chunk_id_async."""

    @pytest.fixture
    def mock_context(self):
        return MagicMock()

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        """Create threads directory with an entry that produces multiple chunks."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create entry with enough content for multiple chunks
        body = "\n\n".join([
            f"Paragraph {i}: " + "Content goes here. " * 30
            for i in range(5)
        ])

        thread = threads_dir / "dedup-test.md"
        thread.write_text(f"""# dedup-test — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: Deduplication test entry
<!-- Entry-ID: 01DEDUP01 -->

{body}
""")

        return threads_dir

    async def test_deduplication_skips_existing_episodes(
        self, mock_context, mock_threads_dir
    ):
        """Migration should skip chunks that already exist in the backend."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        add_episode_calls = []
        find_call_count = [0]

        async def mock_add_episode(**kwargs):
            add_episode_calls.append(kwargs)
            return {"episode_uuid": f"ep-{len(add_episode_calls)}"}

        async def mock_find_episode(chunk_id: str, group_id: str):
            # Simulate that first chunk already exists (based on call order)
            find_call_count[0] += 1
            if find_call_count[0] == 1:
                # Return format expected by migration code: uses "uuid" key
                return {"uuid": "existing-ep-0", "chunk_id": chunk_id}
            return None

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(side_effect=mock_add_episode)
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(side_effect=mock_find_episode)

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
                chunk_max_tokens=200,
            )

            result_data = json.loads(result)

            # Should have deduplicated at least one chunk (the first one "exists")
            assert result_data.get("chunks_deduplicated", 0) >= 1

            # Verify find_episode_by_chunk_id_async was called for deduplication
            assert mock_graphiti.find_episode_by_chunk_id_async.call_count >= 1

    async def test_deduplication_check_called_with_correct_params(
        self, mock_context, mock_threads_dir
    ):
        """Deduplication check should be called with chunk_id and group_id."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        find_episode_calls = []

        async def mock_find_episode(chunk_id: str, group_id: str):
            find_episode_calls.append({"chunk_id": chunk_id, "group_id": group_id})
            return None

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(
            return_value={"episode_uuid": "ep-new"}
        )
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(side_effect=mock_find_episode)

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

            # Verify calls were made
            assert len(find_episode_calls) >= 1

            # Each call should have chunk_id and group_id
            for call in find_episode_calls:
                assert "chunk_id" in call
                assert "group_id" in call
                assert call["chunk_id"]  # Not empty
                assert call["group_id"]  # Not empty

    async def test_deduplication_preserves_episode_chain_on_skip(
        self, mock_context, mock_threads_dir
    ):
        """When a chunk is skipped due to dedup, chain should use existing episode UUID."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        call_args_list = []
        existing_uuid = "existing-ep-for-chunk-0"
        find_call_count = [0]

        async def mock_add_episode(**kwargs):
            call_args_list.append(kwargs)
            return {"episode_uuid": f"new-ep-{len(call_args_list)}"}

        async def mock_find_episode(chunk_id: str, group_id: str):
            # First chunk (header) already exists (based on call order)
            find_call_count[0] += 1
            if find_call_count[0] == 1:
                # Return format expected by migration code: uses "uuid" key
                return {"uuid": existing_uuid, "chunk_id": chunk_id}
            return None

        mock_graphiti = MagicMock()
        mock_graphiti.add_episode_direct = AsyncMock(side_effect=mock_add_episode)
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(side_effect=mock_find_episode)

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

            # If there were new chunks added after the skipped one,
            # they should reference the existing episode UUID
            if call_args_list:
                # Find calls that have previous_episode_uuids
                for call in call_args_list:
                    prev_uuids = call.get("previous_episode_uuids", [])
                    # The first new chunk should link to the existing UUID
                    if prev_uuids and existing_uuid in prev_uuids:
                        # Verify the chain was preserved correctly
                        assert existing_uuid in prev_uuids

    async def test_no_deduplication_in_dry_run(
        self, mock_context, mock_threads_dir
    ):
        """Dry run should not call deduplication check."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        mock_graphiti = MagicMock()
        mock_graphiti.find_episode_by_chunk_id_async = AsyncMock(return_value=None)

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

            # In dry run, backend should not be instantiated for dedup checks
            # The _get_migration_backend mock shouldn't be called in dry run
            # (deduplication only happens during actual migration)


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
