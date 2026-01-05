"""Tests for memory sync module and callback registry.

Tests the memory_sync.py module and the callback registry pattern
introduced in Issue #83.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCallbackRegistry:
    """Tests for the memory sync callback registry in baseline_graph.sync."""

    def test_register_callback(self):
        """Test registering a sync callback."""
        from watercooler.baseline_graph.sync import (
            _memory_sync_callbacks,
            get_registered_backends,
            register_memory_sync_callback,
            unregister_memory_sync_callback,
        )

        # Clean state
        if "test_backend" in _memory_sync_callbacks:
            unregister_memory_sync_callback("test_backend")

        def my_callback(*args, **kwargs):
            return True

        register_memory_sync_callback("test_backend", my_callback)
        assert "test_backend" in get_registered_backends()
        assert _memory_sync_callbacks["test_backend"] is my_callback

        # Cleanup
        unregister_memory_sync_callback("test_backend")
        assert "test_backend" not in get_registered_backends()

    def test_unregister_nonexistent(self):
        """Test unregistering a non-existent callback (should not raise)."""
        from watercooler.baseline_graph.sync import unregister_memory_sync_callback

        # Should not raise
        unregister_memory_sync_callback("nonexistent_backend_xyz")

    def test_get_registered_backends(self):
        """Test getting list of registered backends."""
        from watercooler.baseline_graph.sync import (
            get_registered_backends,
            register_memory_sync_callback,
            unregister_memory_sync_callback,
        )

        # Register test backends
        register_memory_sync_callback("backend_a", lambda: True)
        register_memory_sync_callback("backend_b", lambda: True)

        backends = get_registered_backends()
        assert "backend_a" in backends
        assert "backend_b" in backends

        # Cleanup
        unregister_memory_sync_callback("backend_a")
        unregister_memory_sync_callback("backend_b")


class TestSyncToMemoryBackend:
    """Tests for sync_to_memory_backend function."""

    def test_returns_false_when_disabled(self, monkeypatch):
        """Test that sync returns False when backend is disabled."""
        from watercooler.baseline_graph.sync import sync_to_memory_backend

        monkeypatch.delenv("WATERCOOLER_MEMORY_BACKEND", raising=False)

        result = sync_to_memory_backend(
            threads_dir=Path("/tmp"),
            topic="test-topic",
            entry_id="entry-1",
            entry_body="test body",
        )
        assert result is False

    def test_returns_false_for_unknown_backend(self, monkeypatch):
        """Test that sync returns False for unknown backend."""
        from watercooler.baseline_graph.sync import sync_to_memory_backend

        monkeypatch.setenv("WATERCOOLER_MEMORY_BACKEND", "unknown_backend")

        result = sync_to_memory_backend(
            threads_dir=Path("/tmp"),
            topic="test-topic",
            entry_id="entry-1",
            entry_body="test body",
        )
        assert result is False

    def test_returns_false_when_no_callback_registered(self, monkeypatch):
        """Test that sync returns False when callback not registered."""
        from watercooler.baseline_graph.sync import (
            sync_to_memory_backend,
            unregister_memory_sync_callback,
        )

        monkeypatch.setenv("WATERCOOLER_MEMORY_BACKEND", "graphiti")
        # Ensure callback is not registered
        unregister_memory_sync_callback("graphiti")

        result = sync_to_memory_backend(
            threads_dir=Path("/tmp"),
            topic="test-topic",
            entry_id="entry-1",
            entry_body="test body",
        )
        assert result is False

    def test_submits_to_executor_with_callback(self, monkeypatch):
        """Test that sync submits work to executor when callback is registered."""
        from watercooler.baseline_graph.sync import (
            register_memory_sync_callback,
            sync_to_memory_backend,
            unregister_memory_sync_callback,
        )

        # Use a valid backend name that passes validation
        monkeypatch.setenv("WATERCOOLER_MEMORY_BACKEND", "leanrag")

        callback_called = []

        def test_callback(*args, **kwargs):
            callback_called.append(args)
            return True

        # Override the leanrag callback with our test callback
        register_memory_sync_callback("leanrag", test_callback)

        try:
            result = sync_to_memory_backend(
                threads_dir=Path("/tmp"),
                topic="test-topic",
                entry_id="entry-1",
                entry_body="test body",
            )
            assert result is True

            # Wait for executor to run callback
            import time

            time.sleep(0.2)

            # Callback should have been invoked
            assert len(callback_called) == 1
        finally:
            # Re-register the original callback
            from watercooler_mcp.memory_sync import _leanrag_sync_callback
            register_memory_sync_callback("leanrag", _leanrag_sync_callback)


class TestMemorySyncCallbacks:
    """Tests for the callback implementations in memory_sync.py."""

    def test_graphiti_sync_callback_dry_run(self):
        """Test Graphiti callback in dry run mode."""
        from watercooler_mcp.memory_sync import _graphiti_sync_callback

        log = MagicMock()

        result = _graphiti_sync_callback(
            threads_dir=Path("/tmp"),
            topic="test",
            entry_id="e1",
            entry_body="body",
            entry_title="title",
            timestamp="2025-01-01T00:00:00Z",
            agent="claude",
            role="implementer",
            entry_type="Note",
            backend_config={"backend": "graphiti"},
            log=log,
            dry_run=True,
        )

        assert result is True
        log.debug.assert_called()

    def test_leanrag_sync_callback_dry_run(self):
        """Test LeanRAG callback in dry run mode."""
        from watercooler_mcp.memory_sync import _leanrag_sync_callback

        log = MagicMock()

        result = _leanrag_sync_callback(
            threads_dir=Path("/tmp"),
            topic="test",
            entry_id="e1",
            entry_body="body",
            entry_title="title",
            timestamp="2025-01-01T00:00:00Z",
            agent="claude",
            role="implementer",
            entry_type="Note",
            backend_config={"backend": "leanrag"},
            log=log,
            dry_run=True,
        )

        assert result is True
        log.debug.assert_called()

    def test_leanrag_sync_callback_queues_entry(self):
        """Test LeanRAG callback queues entry (always returns True)."""
        from watercooler_mcp.memory_sync import _leanrag_sync_callback

        log = MagicMock()

        result = _leanrag_sync_callback(
            threads_dir=Path("/tmp"),
            topic="test",
            entry_id="e1",
            entry_body="body",
            entry_title=None,
            timestamp=None,
            agent=None,
            role=None,
            entry_type=None,
            backend_config={"backend": "leanrag"},
            log=log,
            dry_run=False,
        )

        # LeanRAG sync always succeeds (just queues)
        assert result is True


class TestInitMemorySyncCallbacks:
    """Tests for init_memory_sync_callbacks function."""

    def test_idempotent(self):
        """Test that init_memory_sync_callbacks is idempotent."""
        from watercooler_mcp.memory_sync import (
            init_memory_sync_callbacks,
            reset_callbacks,
        )

        # Reset state
        reset_callbacks()

        # First call
        init_memory_sync_callbacks()

        # Second call should not raise or duplicate
        init_memory_sync_callbacks()

        # Check registered
        from watercooler.baseline_graph.sync import get_registered_backends

        backends = get_registered_backends()
        assert "graphiti" in backends
        assert "leanrag" in backends

    def test_registers_both_backends(self):
        """Test that both graphiti and leanrag callbacks are registered."""
        from watercooler_mcp.memory_sync import (
            init_memory_sync_callbacks,
            reset_callbacks,
        )

        reset_callbacks()
        init_memory_sync_callbacks()

        from watercooler.baseline_graph.sync import get_registered_backends

        backends = get_registered_backends()
        assert "graphiti" in backends
        assert "leanrag" in backends


class TestCallGraphitiAddEpisode:
    """Tests for _call_graphiti_add_episode async function."""

    def test_returns_error_when_not_enabled(self):
        """Test error when Graphiti is not enabled."""
        import asyncio
        from watercooler_mcp.memory_sync import _call_graphiti_add_episode

        async def run_test():
            with patch("watercooler_mcp.memory.load_graphiti_config") as mock_config:
                mock_config.return_value = None

                result = await _call_graphiti_add_episode(
                    content="test",
                    group_id="test-group",
                )

                return result

        result = asyncio.run(run_test())
        assert result["success"] is False
        assert "not enabled" in result["error"]

    def test_handles_exception(self):
        """Test handling of exceptions during sync."""
        import asyncio
        from watercooler_mcp.memory_sync import _call_graphiti_add_episode

        async def run_test():
            with patch("watercooler_mcp.memory.load_graphiti_config") as mock_config:
                mock_config.side_effect = Exception("Test error")

                result = await _call_graphiti_add_episode(
                    content="test",
                    group_id="test-group",
                )

                return result

        result = asyncio.run(run_test())
        assert result["success"] is False
        assert "error" in result


class TestLeanRAGQueue:
    """Tests for LeanRAG queue functionality."""

    def test_queue_entry_written(self, tmp_path):
        """Test that LeanRAG callback writes to queue file."""
        from watercooler_mcp.memory_sync import (
            _leanrag_sync_callback,
            get_leanrag_queue_path,
            read_leanrag_queue,
        )

        log = MagicMock()

        result = _leanrag_sync_callback(
            threads_dir=tmp_path,
            topic="test-topic",
            entry_id="entry-123",
            entry_body="Test body content",
            entry_title="Test Title",
            timestamp="2025-01-05T12:00:00Z",
            agent="claude",
            role="implementer",
            entry_type="Note",
            backend_config={"backend": "leanrag"},
            log=log,
            dry_run=False,
        )

        assert result is True

        # Verify queue file exists
        queue_path = get_leanrag_queue_path(tmp_path)
        assert queue_path.exists()

        # Verify entry content
        entries = read_leanrag_queue(tmp_path)
        assert len(entries) == 1
        assert entries[0]["entry_id"] == "entry-123"
        assert entries[0]["topic"] == "test-topic"
        assert entries[0]["entry_body"] == "Test body content"
        assert entries[0]["agent"] == "claude"

    def test_queue_multiple_entries(self, tmp_path):
        """Test that multiple entries are appended to queue."""
        from watercooler_mcp.memory_sync import (
            _leanrag_sync_callback,
            read_leanrag_queue,
        )

        log = MagicMock()

        # Add multiple entries
        for i in range(3):
            _leanrag_sync_callback(
                threads_dir=tmp_path,
                topic=f"topic-{i}",
                entry_id=f"entry-{i}",
                entry_body=f"Body {i}",
                entry_title=None,
                timestamp=None,
                agent=None,
                role=None,
                entry_type=None,
                backend_config={"backend": "leanrag"},
                log=log,
                dry_run=False,
            )

        entries = read_leanrag_queue(tmp_path)
        assert len(entries) == 3
        assert entries[0]["entry_id"] == "entry-0"
        assert entries[2]["entry_id"] == "entry-2"

    def test_clear_queue(self, tmp_path):
        """Test clearing the queue."""
        from watercooler_mcp.memory_sync import (
            _leanrag_sync_callback,
            clear_leanrag_queue,
            get_leanrag_queue_path,
            read_leanrag_queue,
        )

        log = MagicMock()

        # Add entries
        for i in range(2):
            _leanrag_sync_callback(
                threads_dir=tmp_path,
                topic="test",
                entry_id=f"e{i}",
                entry_body="body",
                entry_title=None,
                timestamp=None,
                agent=None,
                role=None,
                entry_type=None,
                backend_config={},
                log=log,
                dry_run=False,
            )

        assert len(read_leanrag_queue(tmp_path)) == 2

        # Clear queue
        cleared = clear_leanrag_queue(tmp_path)
        assert cleared == 2

        # Verify cleared
        assert not get_leanrag_queue_path(tmp_path).exists()
        assert len(read_leanrag_queue(tmp_path)) == 0

    def test_read_empty_queue(self, tmp_path):
        """Test reading non-existent queue returns empty list."""
        from watercooler_mcp.memory_sync import read_leanrag_queue

        entries = read_leanrag_queue(tmp_path)
        assert entries == []
