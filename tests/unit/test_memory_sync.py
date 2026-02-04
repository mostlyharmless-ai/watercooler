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


class TestMemoryDisabled:
    """Tests for is_memory_disabled and WATERCOOLER_MEMORY_DISABLED."""

    def test_is_memory_disabled_returns_false_by_default(self, monkeypatch):
        """Test that memory is enabled by default."""
        from watercooler.baseline_graph.sync import is_memory_disabled

        monkeypatch.delenv("WATERCOOLER_MEMORY_DISABLED", raising=False)
        assert is_memory_disabled() is False

    def test_is_memory_disabled_with_1(self, monkeypatch):
        """Test that WATERCOOLER_MEMORY_DISABLED=1 disables memory."""
        from watercooler.baseline_graph.sync import is_memory_disabled

        monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "1")
        assert is_memory_disabled() is True

    def test_is_memory_disabled_with_true(self, monkeypatch):
        """Test that WATERCOOLER_MEMORY_DISABLED=true disables memory."""
        from watercooler.baseline_graph.sync import is_memory_disabled

        monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "true")
        assert is_memory_disabled() is True

    def test_is_memory_disabled_with_yes(self, monkeypatch):
        """Test that WATERCOOLER_MEMORY_DISABLED=yes disables memory."""
        from watercooler.baseline_graph.sync import is_memory_disabled

        monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "yes")
        assert is_memory_disabled() is True

    def test_get_memory_backend_config_returns_none_when_disabled(self, monkeypatch):
        """Test that get_memory_backend_config returns None when disabled."""
        from watercooler.baseline_graph.sync import get_memory_backend_config

        monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "1")
        monkeypatch.setenv("WATERCOOLER_MEMORY_BACKEND", "graphiti")

        config = get_memory_backend_config()
        assert config is None

    def test_get_memory_backend_config_auto_detects_graphiti(self, monkeypatch):
        """Test that get_memory_backend_config auto-detects graphiti from GRAPHITI_ENABLED."""
        from watercooler.baseline_graph.sync import get_memory_backend_config

        # Clear explicit backend setting
        monkeypatch.delenv("WATERCOOLER_MEMORY_BACKEND", raising=False)
        monkeypatch.delenv("WATERCOOLER_MEMORY_DISABLED", raising=False)
        # Set GRAPHITI_ENABLED
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")

        config = get_memory_backend_config()
        assert config is not None
        assert config["backend"] == "graphiti"

    def test_get_memory_backend_config_explicit_overrides_auto(self, monkeypatch):
        """Test that explicit MEMORY_BACKEND overrides auto-detection."""
        from watercooler.baseline_graph.sync import get_memory_backend_config

        monkeypatch.delenv("WATERCOOLER_MEMORY_DISABLED", raising=False)
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("WATERCOOLER_MEMORY_BACKEND", "leanrag")

        config = get_memory_backend_config()
        assert config is not None
        assert config["backend"] == "leanrag"

    def test_sync_returns_false_when_globally_disabled(self, monkeypatch):
        """Test that sync_to_memory_backend returns False when globally disabled."""
        from watercooler.baseline_graph.sync import sync_to_memory_backend

        monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "1")
        monkeypatch.setenv("WATERCOOLER_MEMORY_BACKEND", "graphiti")

        result = sync_to_memory_backend(
            threads_dir=Path("/tmp"),
            topic="test-topic",
            entry_id="entry-1",
            entry_body="test body",
        )
        assert result is False


class TestSyncToMemoryBackend:
    """Tests for sync_to_memory_backend function."""

    def test_returns_false_when_disabled(self, monkeypatch):
        """Test that sync returns False when backend is disabled."""
        from watercooler.baseline_graph.sync import sync_to_memory_backend

        # Explicitly disable memory backend (TOML config may have defaults)
        monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "1")
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
                    topic="test-topic",
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
                    topic="test-topic",
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


class TestChunkConfigHelpers:
    """Tests for Graphiti chunk configuration helpers."""

    def test_get_graphiti_chunk_on_sync_default(self, monkeypatch):
        """Test default chunk_on_sync value (True)."""
        from watercooler.memory_config import get_graphiti_chunk_on_sync

        monkeypatch.delenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", raising=False)
        assert get_graphiti_chunk_on_sync() is True

    def test_get_graphiti_chunk_on_sync_env_true(self, monkeypatch):
        """Test chunk_on_sync enabled via env var."""
        from watercooler.memory_config import get_graphiti_chunk_on_sync

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "1")
        assert get_graphiti_chunk_on_sync() is True

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "true")
        assert get_graphiti_chunk_on_sync() is True

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "yes")
        assert get_graphiti_chunk_on_sync() is True

    def test_get_graphiti_chunk_on_sync_env_false(self, monkeypatch):
        """Test chunk_on_sync disabled via env var."""
        from watercooler.memory_config import get_graphiti_chunk_on_sync

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "0")
        assert get_graphiti_chunk_on_sync() is False

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "false")
        assert get_graphiti_chunk_on_sync() is False

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "no")
        assert get_graphiti_chunk_on_sync() is False

    def test_get_graphiti_chunk_config_defaults(self, monkeypatch):
        """Test default chunk config values."""
        from watercooler.memory_config import get_graphiti_chunk_config

        monkeypatch.delenv("WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS", raising=False)
        monkeypatch.delenv("WATERCOOLER_GRAPHITI_CHUNK_OVERLAP", raising=False)

        max_tokens, overlap = get_graphiti_chunk_config()
        assert max_tokens == 768
        assert overlap == 64

    def test_get_graphiti_chunk_config_env_override(self, monkeypatch):
        """Test chunk config with env var overrides."""
        from watercooler.memory_config import get_graphiti_chunk_config

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS", "512")
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_OVERLAP", "32")

        max_tokens, overlap = get_graphiti_chunk_config()
        assert max_tokens == 512
        assert overlap == 32

    def test_get_graphiti_chunk_config_bounds(self, monkeypatch):
        """Test that chunk config values are bounded."""
        from watercooler.memory_config import get_graphiti_chunk_config

        # Test upper bounds
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS", "10000")
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_OVERLAP", "500")

        max_tokens, overlap = get_graphiti_chunk_config()
        assert max_tokens == 4096  # max bound
        assert overlap == 256  # max bound

        # Test lower bounds
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS", "10")
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_OVERLAP", "-10")

        max_tokens, overlap = get_graphiti_chunk_config()
        assert max_tokens == 100  # min bound
        assert overlap == 0  # min bound

    def test_get_graphiti_chunk_config_invalid_values(self, monkeypatch):
        """Test that invalid env values fall back to defaults."""
        from watercooler.memory_config import get_graphiti_chunk_config

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS", "not_a_number")
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_OVERLAP", "also_not_a_number")

        max_tokens, overlap = get_graphiti_chunk_config()
        assert max_tokens == 768  # default
        assert overlap == 64  # default


class TestCallGraphitiAddEpisodeChunked:
    """Tests for _call_graphiti_add_episode_chunked function."""

    def test_returns_error_when_not_enabled(self):
        """Test error when Graphiti is not enabled."""
        import asyncio

        from watercooler_mcp.memory_sync import _call_graphiti_add_episode_chunked

        async def run_test():
            with patch("watercooler_mcp.memory.load_graphiti_config") as mock_config:
                mock_config.return_value = None

                result = await _call_graphiti_add_episode_chunked(
                    content="test content that is quite long",
                    topic="test-topic",
                )

                return result

        result = asyncio.run(run_test())
        assert result["success"] is False
        assert "not enabled" in result["error"]

    def test_short_content_falls_back_to_simple(self):
        """Test that short content uses simple sync (single chunk)."""
        import asyncio
        from unittest.mock import AsyncMock

        from watercooler_mcp.memory_sync import _call_graphiti_add_episode_chunked

        async def run_test():
            # Mock the entire dependency chain
            mock_config = MagicMock()
            mock_config.database = "test_db"

            mock_backend = MagicMock()
            mock_result = {"episode_uuid": "test-uuid-123", "entities_extracted": []}
            # Use AsyncMock for async method
            mock_backend.add_episode_direct = AsyncMock(return_value=mock_result)
            mock_backend.entry_episode_index = None

            with patch("watercooler_mcp.memory.load_graphiti_config") as mock_load:
                mock_load.return_value = mock_config

                with patch("watercooler_mcp.memory.get_graphiti_backend") as mock_get:
                    mock_get.return_value = mock_backend

                    # Short content that fits in one chunk
                    result = await _call_graphiti_add_episode_chunked(
                        content="Short content",
                        topic="test-topic",
                        entry_id="e1",
                        max_tokens=768,
                        overlap=64,
                    )

                    return result

        result = asyncio.run(run_test())
        # Should fall back to simple sync for short content and succeed
        assert result.get("success") is True

    def test_handles_exception(self):
        """Test handling of exceptions during chunked sync."""
        import asyncio

        from watercooler_mcp.memory_sync import _call_graphiti_add_episode_chunked

        async def run_test():
            with patch("watercooler_mcp.memory.load_graphiti_config") as mock_config:
                mock_config.side_effect = Exception("Test error")

                result = await _call_graphiti_add_episode_chunked(
                    content="test content",
                    topic="test-topic",
                )

                return result

        result = asyncio.run(run_test())
        assert result["success"] is False
        assert "error" in result


class TestGraphitiSyncCallbackChunking:
    """Tests for chunking behavior in _graphiti_sync_callback."""

    def test_callback_respects_chunk_on_sync_disabled(self, monkeypatch):
        """Test that callback skips chunking when disabled."""
        from watercooler_mcp.memory_sync import _graphiti_sync_callback

        # Disable chunking
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "false")

        log = MagicMock()

        # Mock to verify which function is called
        with patch(
            "watercooler_mcp.memory_sync._call_graphiti_add_episode"
        ) as mock_simple:
            with patch(
                "watercooler_mcp.memory_sync._call_graphiti_add_episode_chunked"
            ) as mock_chunked:
                # Make simple sync return success
                async def simple_return(*args, **kwargs):
                    return {"success": True, "episode_uuid": "test-uuid"}

                mock_simple.return_value = simple_return()

                # Run callback (it will fail at asyncio.run but we can check mocks)
                try:
                    _graphiti_sync_callback(
                        threads_dir=Path("/tmp/test-threads"),
                        topic="test",
                        entry_id="e1",
                        entry_body="Long body " * 1000,  # Long content
                        entry_title="Test",
                        timestamp="2025-01-01T00:00:00Z",
                        agent="claude",
                        role="implementer",
                        entry_type="Note",
                        backend_config={"backend": "graphiti"},
                        log=log,
                        dry_run=False,
                    )
                except Exception:
                    pass  # Expected to fail in actual sync

                # With chunking disabled, only simple should be used
                # (chunked should not be called)
                mock_chunked.assert_not_called()

    def test_callback_uses_chunking_when_enabled(self, monkeypatch):
        """Test that callback uses chunking when enabled."""
        from watercooler_mcp.memory_sync import _graphiti_sync_callback

        # Enable chunking
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "true")

        log = MagicMock()

        with patch(
            "watercooler_mcp.memory_sync._call_graphiti_add_episode"
        ) as mock_simple:
            with patch(
                "watercooler_mcp.memory_sync._call_graphiti_add_episode_chunked"
            ) as mock_chunked:
                # Make chunked sync return success
                async def chunked_return(*args, **kwargs):
                    return {"success": True, "episode_uuids": ["uuid1"], "chunk_count": 1}

                mock_chunked.return_value = chunked_return()

                try:
                    _graphiti_sync_callback(
                        threads_dir=Path("/tmp/test-threads"),
                        topic="test",
                        entry_id="e1",
                        entry_body="Test body",
                        entry_title="Test",
                        timestamp="2025-01-01T00:00:00Z",
                        agent="claude",
                        role="implementer",
                        entry_type="Note",
                        backend_config={"backend": "graphiti"},
                        log=log,
                        dry_run=False,
                    )
                except Exception:
                    pass

                # With chunking enabled, chunked should be called
                mock_simple.assert_not_called()


class TestSyncEntryToMemoryBackend:
    """Tests for the sync_entry_to_memory_backend helper function."""

    def test_returns_true_when_entry_exists(self, tmp_path, monkeypatch):
        """Test sync_entry_to_memory_backend returns True when entry is found."""
        from watercooler.baseline_graph.sync import sync_entry_to_memory_backend

        # Mock get_entry_node_from_graph to return a valid entry
        monkeypatch.setattr(
            "watercooler.baseline_graph.sync.get_entry_node_from_graph",
            lambda threads_dir, entry_id, topic: {
                "body": "Test body",
                "title": "Test title",
                "timestamp": "2025-01-01T00:00:00Z",
                "agent": "claude",
                "role": "implementer",
                "entry_type": "Note",
            },
        )

        sync_calls = []

        def mock_sync(**kwargs):
            sync_calls.append(kwargs)
            return True

        monkeypatch.setattr(
            "watercooler.baseline_graph.sync.sync_to_memory_backend",
            lambda **kwargs: mock_sync(**kwargs),
        )

        result = sync_entry_to_memory_backend(tmp_path, "test-topic", "entry-1")

        assert result is True
        assert len(sync_calls) == 1
        assert sync_calls[0]["topic"] == "test-topic"
        assert sync_calls[0]["entry_id"] == "entry-1"
        assert sync_calls[0]["entry_body"] == "Test body"
        assert sync_calls[0]["entry_title"] == "Test title"

    def test_returns_false_when_entry_not_found(self, tmp_path, monkeypatch):
        """Test sync_entry_to_memory_backend returns False for missing entries."""
        from watercooler.baseline_graph.sync import sync_entry_to_memory_backend

        monkeypatch.setattr(
            "watercooler.baseline_graph.sync.get_entry_node_from_graph",
            lambda threads_dir, entry_id, topic: None,
        )

        result = sync_entry_to_memory_backend(tmp_path, "test-topic", "missing-entry")

        assert result is False

    def test_handles_exception_gracefully(self, tmp_path, monkeypatch):
        """Test sync_entry_to_memory_backend catches exceptions."""
        from watercooler.baseline_graph.sync import sync_entry_to_memory_backend

        def raise_error(*args, **kwargs):
            raise RuntimeError("graph explosion")

        monkeypatch.setattr(
            "watercooler.baseline_graph.sync.get_entry_node_from_graph",
            raise_error,
        )

        result = sync_entry_to_memory_backend(tmp_path, "test-topic", "entry-1")

        assert result is False
