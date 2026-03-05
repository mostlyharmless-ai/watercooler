"""Integration tests for Memory Integration Phase 1.

Tests for Milestones 5-7:
- Search routing (Milestone 6)
- Migration tool (Milestone 7)
- Memory sync hook (Milestone 5.3)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watercooler.baseline_graph import storage

# Configure pytest-asyncio mode
pytestmark = pytest.mark.anyio


class TestSearchRoutingIntegration:
    """Integration tests for tier-aware search routing."""

    @pytest.fixture
    def mock_threads_dir(self, tmp_path):
        """Create mock threads directory with graph."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create baseline graph structure
        graph_dir = threads_dir / "graph" / "baseline"
        graph_dir.mkdir(parents=True)

        # Create minimal nodes.jsonl
        nodes = [
            {"type": "thread", "id": "test-thread", "topic": "test-thread", "title": "Test Thread", "status": "OPEN"},
            {"type": "entry", "id": "entry-1", "thread_topic": "test-thread", "title": "Entry 1", "body": "Test content"},
        ]
        with open(graph_dir / "nodes.jsonl", "w") as f:
            for node in nodes:
                f.write(json.dumps(node) + "\n")

        # Create empty edges.jsonl
        (graph_dir / "edges.jsonl").write_text("")

        return threads_dir

    def test_search_routing_auto_uses_baseline_by_default(self, mock_threads_dir):
        """Search with auto backend uses baseline when no memory backend configured."""
        from watercooler_mcp.tools.graph import get_search_backend

        # Clear any existing env var
        os.environ.pop("WATERCOOLER_MEMORY_BACKEND", None)

        # Mock TOML config to return "null" (not graphiti/leanrag)
        with patch("watercooler.memory_config.get_memory_backend", return_value="null"):
            backend = get_search_backend("auto")
            assert backend == "baseline"

    def test_search_routing_respects_env_var(self, mock_threads_dir):
        """Search with auto backend respects WATERCOOLER_MEMORY_BACKEND."""
        from watercooler_mcp.tools.graph import get_search_backend

        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "graphiti"}):
            backend = get_search_backend("auto")
            assert backend == "graphiti"

    def test_mode_inference_defaults_to_entries(self):
        """Mode inference defaults to entries for most queries."""
        from watercooler_mcp.tools.graph import infer_search_mode

        mode = infer_search_mode("auto", "find authentication code", False)
        assert mode == "entries"

    def test_explicit_backend_override(self):
        """Explicit backend override is respected regardless of env var."""
        from watercooler_mcp.tools.graph import get_search_backend

        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "graphiti"}):
            # Explicit baseline should override env
            backend = get_search_backend("baseline")
            assert backend == "baseline"


class TestMigrationIntegration:
    """Integration tests for migration tool."""

    @pytest.fixture
    def sample_threads_dir(self, tmp_path):
        """Create threads directory with sample threads."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create sample thread with multiple entries
        (threads_dir / "integration-test.md").write_text("""# integration-test — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: First entry
<!-- Entry-ID: 01INT001 -->

First integration test entry.

---

Entry: Human (dev) 2025-01-15T11:00:00Z
Role: reviewer
Type: Note
Title: Second entry
<!-- Entry-ID: 01INT002 -->

Second integration test entry.
""")

        # Bootstrap graph data
        graph_dir = storage.ensure_graph_dir(threads_dir)
        thread_dir = storage.ensure_thread_graph_dir(graph_dir, "integration-test")
        storage.atomic_write_json(thread_dir / "meta.json", {
            "id": "thread:integration-test",
            "type": "thread",
            "topic": "integration-test",
            "title": "integration-test",
            "status": "OPEN",
            "ball": "Claude (dev)",
            "entry_count": 2,
            "last_updated": "2025-01-15T11:00:00Z",
        })
        storage.atomic_write_jsonl(thread_dir / "entries.jsonl", [
            {"id": "entry:01INT001", "entry_id": "01INT001", "index": 0,
             "agent": "Claude (dev)", "role": "implementer", "entry_type": "Note",
             "title": "First entry", "timestamp": "2025-01-15T10:00:00Z",
             "body": "First integration test entry."},
            {"id": "entry:01INT002", "entry_id": "01INT002", "index": 1,
             "agent": "Human (dev)", "role": "reviewer", "entry_type": "Note",
             "title": "Second entry", "timestamp": "2025-01-15T11:00:00Z",
             "body": "Second integration test entry."},
        ])

        return threads_dir

    async def test_preflight_parses_threads_correctly(self, sample_threads_dir):
        """Preflight correctly parses thread structure."""
        from watercooler_mcp.tools.migration import _migration_preflight_impl

        mock_ctx = MagicMock()

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ):
            result = await _migration_preflight_impl(
                threads_dir=sample_threads_dir,
                backend="graphiti",
                ctx=mock_ctx,
            )

        result_data = json.loads(result)
        assert result_data["thread_count"] == 1
        assert result_data["estimated_entries"] == 2
        assert result_data["ready"] is True

    async def test_dry_run_lists_all_entries(self, sample_threads_dir):
        """Dry run lists all entries to be migrated."""
        from watercooler_mcp.tools.migration import _migrate_to_memory_backend_impl

        mock_ctx = MagicMock()

        with patch(
            "watercooler_mcp.tools.migration._check_backend_availability",
            return_value={"available": True},
        ):
            result = await _migrate_to_memory_backend_impl(
                threads_dir=sample_threads_dir,
                backend="graphiti",
                ctx=mock_ctx,
                dry_run=True,
            )

        result_data = json.loads(result)
        assert result_data["dry_run"] is True
        assert len(result_data["would_migrate"]) == 2

        # Verify entry IDs
        entry_ids = [e["entry_id"] for e in result_data["would_migrate"]]
        assert "01INT001" in entry_ids
        assert "01INT002" in entry_ids

    async def test_checkpoint_persistence(self, sample_threads_dir):
        """Checkpoint is persisted and used for resume."""
        from watercooler_mcp.tools.migration import (
            _load_checkpoint,
            _save_checkpoint,
        )

        # Save checkpoint
        _save_checkpoint(sample_threads_dir, ["01INT001"], "graphiti")

        # Load checkpoint (returns CheckpointV2 object)
        checkpoint = _load_checkpoint(sample_threads_dir)
        assert checkpoint.backend == "graphiti"
        assert "01INT001" in checkpoint.entries

        # Verify checkpoint file exists
        checkpoint_file = sample_threads_dir / ".migration_checkpoint.json"
        assert checkpoint_file.exists()


class TestMemorySyncHookIntegration:
    """Integration tests for memory sync hook."""

    def test_get_memory_backend_config_integration(self):
        """Memory backend config respects environment variable."""
        from watercooler.baseline_graph.sync import get_memory_backend_config

        # Explicitly disabled (TOML config may have defaults)
        os.environ["WATERCOOLER_MEMORY_DISABLED"] = "1"
        os.environ.pop("WATERCOOLER_MEMORY_BACKEND", None)
        try:
            config = get_memory_backend_config()
            assert config is None
        finally:
            os.environ.pop("WATERCOOLER_MEMORY_DISABLED", None)

        # With graphiti
        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "graphiti"}):
            config = get_memory_backend_config()
            assert config is not None
            assert config["backend"] == "graphiti"

        # With leanrag
        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "leanrag"}):
            config = get_memory_backend_config()
            assert config is not None
            assert config["backend"] == "leanrag"

    def test_sync_hook_non_blocking(self, tmp_path):
        """Memory sync hook is non-blocking - uses fire-and-forget pattern.

        With ThreadPoolExecutor, sync_to_memory_backend returns True immediately
        after submitting work to the executor. Errors are logged asynchronously
        in the worker thread without affecting the caller.
        """
        from watercooler.baseline_graph.sync import (
            register_memory_sync_callback,
            sync_to_memory_backend,
            unregister_memory_sync_callback,
        )

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Register a mock callback that raises an error
        def error_callback(*args, **kwargs):
            raise RuntimeError("Connection failed")

        # Register test callback (will override default if exists)
        register_memory_sync_callback("graphiti", error_callback)

        try:
            with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "graphiti"}):
                # Should not raise - fire-and-forget returns True after submission
                result = sync_to_memory_backend(
                    threads_dir=threads_dir,
                    topic="test-thread",
                    entry_id="01TEST123",
                    entry_body="Test content",
                )

                # Returns True because work was submitted to executor (fire-and-forget)
                # Actual errors are logged asynchronously in worker thread
                assert result is True
        finally:
            # Restore the original callback
            from watercooler_mcp.memory_sync import _graphiti_sync_callback
            register_memory_sync_callback("graphiti", _graphiti_sync_callback)


class TestEndToEndWorkflow:
    """End-to-end workflow tests."""

    @pytest.fixture
    def complete_setup(self, tmp_path):
        """Create complete test setup."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create thread
        (threads_dir / "e2e-test.md").write_text("""# e2e-test — Thread

Status: OPEN
Ball: Claude (dev)

---

Entry: Claude (dev) 2025-01-15T10:00:00Z
Role: implementer
Type: Note
Title: E2E test entry
<!-- Entry-ID: 01E2E001 -->

End-to-end test content about authentication implementation.
""")

        # Create baseline graph
        graph_dir = threads_dir / "graph" / "baseline"
        graph_dir.mkdir(parents=True)
        (graph_dir / "nodes.jsonl").write_text("")
        (graph_dir / "edges.jsonl").write_text("")

        return threads_dir

    def test_workflow_baseline_to_graphiti(self, complete_setup):
        """Test workflow: baseline graph → check migration → (would) migrate."""
        from watercooler_mcp.tools.graph import get_search_backend, infer_search_mode
        from watercooler_mcp.tools.migration import _load_checkpoint

        threads_dir = complete_setup

        # Step 1: Default search uses baseline (mock TOML to return null)
        os.environ.pop("WATERCOOLER_MEMORY_BACKEND", None)
        with patch("watercooler.memory_config.get_memory_backend", return_value="null"):
            backend = get_search_backend("auto")
            assert backend == "baseline"

        # Step 2: Check no checkpoint exists (empty CheckpointV2)
        checkpoint = _load_checkpoint(threads_dir)
        assert checkpoint.entries == {}
        assert checkpoint.backend == ""

        # Step 3: Simulate enabling graphiti
        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "graphiti"}):
            backend = get_search_backend("auto")
            assert backend == "graphiti"

            # Mode inference still works
            mode = infer_search_mode("auto", "authentication", False)
            assert mode == "entries"


class TestConfigurationIntegration:
    """Test configuration integration across components."""

    def test_memory_backend_configured_via_env(self):
        """Memory backend is configured via environment variables."""
        from watercooler.baseline_graph.sync import get_memory_backend_config

        # Explicitly disabled (TOML config may have defaults)
        os.environ["WATERCOOLER_MEMORY_DISABLED"] = "1"
        os.environ.pop("WATERCOOLER_MEMORY_BACKEND", None)
        try:
            config = get_memory_backend_config()
            assert config is None
        finally:
            os.environ.pop("WATERCOOLER_MEMORY_DISABLED", None)

        # Enabled via env var
        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "graphiti"}):
            config = get_memory_backend_config()
            assert config is not None
            assert config["backend"] == "graphiti"

    def test_memory_backend_env_vars_documented(self):
        """Key environment variables work as documented."""
        # WATERCOOLER_MEMORY_BACKEND
        with patch.dict(os.environ, {"WATERCOOLER_MEMORY_BACKEND": "graphiti"}):
            from watercooler.baseline_graph.sync import get_memory_backend_config

            config = get_memory_backend_config()
            assert config["backend"] == "graphiti"

        # WATERCOOLER_GRAPHITI_ENABLED (handled by memory module)
        # This is tested in test_mcp_memory_integration.py
