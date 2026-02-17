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


class TestUseSummaryConfigHelper:
    """Tests for get_graphiti_use_summary() config accessor."""

    def test_default_is_false(self, monkeypatch):
        """Test default use_summary value (False)."""
        from watercooler.memory_config import get_graphiti_use_summary

        monkeypatch.delenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", raising=False)
        assert get_graphiti_use_summary() is False

    def test_env_true(self, monkeypatch):
        """Test use_summary enabled via env var."""
        from watercooler.memory_config import get_graphiti_use_summary

        for val in ("1", "true", "yes"):
            monkeypatch.setenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", val)
            assert get_graphiti_use_summary() is True

    def test_env_false(self, monkeypatch):
        """Test use_summary explicitly disabled via env var."""
        from watercooler.memory_config import get_graphiti_use_summary

        for val in ("0", "false", "no"):
            monkeypatch.setenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", val)
            assert get_graphiti_use_summary() is False

    def test_env_case_insensitive(self, monkeypatch):
        """Test that env var parsing is case-insensitive."""
        from watercooler.memory_config import get_graphiti_use_summary

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", "TRUE")
        assert get_graphiti_use_summary() is True

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", "False")
        assert get_graphiti_use_summary() is False

    def test_unrecognized_env_falls_back_to_config(self, monkeypatch):
        """Test that unrecognized env values fall back to config default."""
        from watercooler.memory_config import get_graphiti_use_summary

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", "maybe")
        # Config default is False
        assert get_graphiti_use_summary() is False


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


class TestGraphitiSyncCallbackSummary:
    """Tests for summary resolution in _graphiti_sync_callback."""

    def test_uses_raw_body_by_default(self, monkeypatch):
        """Test that raw body is used when use_summary is disabled (default)."""
        from watercooler_mcp.memory_sync import _graphiti_sync_callback

        monkeypatch.delenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", raising=False)
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "false")

        log = MagicMock()
        captured_content = []

        with patch(
            "watercooler_mcp.memory_sync._call_graphiti_add_episode"
        ) as mock_simple:
            async def capture_call(*args, **kwargs):
                captured_content.append(kwargs.get("content", args[0] if args else None))
                return {"success": True, "episode_uuid": "test-uuid"}

            mock_simple.side_effect = capture_call

            try:
                _graphiti_sync_callback(
                    threads_dir=Path("/tmp/test-threads"),
                    topic="test",
                    entry_id="e1",
                    entry_body="Raw body text",
                    entry_title="Test",
                    timestamp="2025-01-01T00:00:00Z",
                    agent="claude",
                    role="implementer",
                    entry_type="Note",
                    backend_config={"backend": "graphiti"},
                    log=log,
                    dry_run=False,
                    entry_summary="Enriched summary text",
                )
            except Exception:
                pass

            # Verify _call_graphiti_add_episode was called with raw body
            mock_simple.assert_called_once()
            call_kwargs = mock_simple.call_args
            assert call_kwargs.kwargs.get("content") == "Raw body text"

    def test_uses_summary_when_configured(self, monkeypatch):
        """Test that summary is used when use_summary is enabled."""
        from watercooler_mcp.memory_sync import _graphiti_sync_callback

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", "true")
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "false")

        log = MagicMock()

        with patch(
            "watercooler_mcp.memory_sync._call_graphiti_add_episode"
        ) as mock_simple:
            async def return_success(*args, **kwargs):
                return {"success": True, "episode_uuid": "test-uuid"}

            mock_simple.side_effect = return_success

            try:
                _graphiti_sync_callback(
                    threads_dir=Path("/tmp/test-threads"),
                    topic="test",
                    entry_id="e1",
                    entry_body="Raw body text",
                    entry_title="Test",
                    timestamp="2025-01-01T00:00:00Z",
                    agent="claude",
                    role="implementer",
                    entry_type="Note",
                    backend_config={"backend": "graphiti"},
                    log=log,
                    dry_run=False,
                    entry_summary="Enriched summary text",
                )
            except Exception:
                pass

            mock_simple.assert_called_once()
            call_kwargs = mock_simple.call_args
            assert call_kwargs.kwargs.get("content") == "Enriched summary text"

    def test_falls_back_to_body_when_summary_empty(self, monkeypatch):
        """Test fallback to raw body when summary is empty string."""
        from watercooler_mcp.memory_sync import _graphiti_sync_callback

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", "true")
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "false")

        log = MagicMock()

        with patch(
            "watercooler_mcp.memory_sync._call_graphiti_add_episode"
        ) as mock_simple:
            async def return_success(*args, **kwargs):
                return {"success": True, "episode_uuid": "test-uuid"}

            mock_simple.side_effect = return_success

            try:
                _graphiti_sync_callback(
                    threads_dir=Path("/tmp/test-threads"),
                    topic="test",
                    entry_id="e1",
                    entry_body="Raw body text",
                    entry_title="Test",
                    timestamp="2025-01-01T00:00:00Z",
                    agent="claude",
                    role="implementer",
                    entry_type="Note",
                    backend_config={"backend": "graphiti"},
                    log=log,
                    dry_run=False,
                    entry_summary="",  # Empty summary
                )
            except Exception:
                pass

            mock_simple.assert_called_once()
            call_kwargs = mock_simple.call_args
            assert call_kwargs.kwargs.get("content") == "Raw body text"


class TestSyncEntryToMemoryBackend:
    """Tests for the sync_entry_to_memory_backend helper function."""

    def test_returns_true_when_entry_exists(self, tmp_path, monkeypatch):
        """Test sync_entry_to_memory_backend returns True when entry is found."""
        from watercooler.baseline_graph.sync import sync_entry_to_memory_backend

        # Mock get_entry_node_from_graph to return a valid entry with summary
        monkeypatch.setattr(
            "watercooler.baseline_graph.sync.get_entry_node_from_graph",
            lambda threads_dir, entry_id, topic: {
                "body": "Test body",
                "title": "Test title",
                "timestamp": "2025-01-01T00:00:00Z",
                "agent": "claude",
                "role": "implementer",
                "entry_type": "Note",
                "summary": "Enriched summary of test body",
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
        assert sync_calls[0]["entry_summary"] == "Enriched summary of test body"

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


class TestMiddlewareMemorySync:
    """Tests for memory sync decoupled from enrichment in middleware.

    Verifies that sync_to_memory_backend is called from
    middleware.operation_with_graph_sync() independently of enrichment.
    """

    def _run_middleware(
        self,
        tmp_path,
        topic="test-topic",
        entry_id="entry-123",
        wants_enrichment=True,
        llm_available=False,
        embed_available=False,
        enrich_result=None,
        entry_node=None,
    ):
        """Helper to run middleware with mocked dependencies.

        Returns (result, mock_sync_to_memory, mock_enrich) for assertions.
        """
        from watercooler_mcp.middleware import run_with_sync

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(exist_ok=True)

        context = MagicMock()
        context.threads_dir = threads_dir
        context.code_root = tmp_path / "code"

        # Default entry node
        if entry_node is None:
            entry_node = {
                "body": "test body",
                "title": "Test Title",
                "timestamp": "2025-01-01T00:00:00Z",
                "agent": "claude",
                "role": "implementer",
                "entry_type": "Note",
            }

        # Default enrich result (noop)
        if enrich_result is None:
            from watercooler.baseline_graph.sync import EnrichmentResult
            enrich_result = EnrichmentResult.noop()

        # Mock graph config
        mock_graph_config = MagicMock()
        mock_graph_config.generate_summaries = wants_enrichment
        mock_graph_config.generate_embeddings = False

        mock_wc_config = MagicMock()
        mock_wc_config.mcp.graph = mock_graph_config

        with patch("watercooler_mcp.middleware.get_watercooler_config", return_value=mock_wc_config), \
             patch("watercooler_mcp.middleware._check_enrichment_services_available", return_value=(llm_available, embed_available)), \
             patch("watercooler_mcp.middleware.acquire_topic_lock") as mock_topic_lock, \
             patch("watercooler.baseline_graph.sync.enrich_graph_entry", return_value=enrich_result) as mock_enrich, \
             patch("watercooler.baseline_graph.writer.get_entry_node_from_graph", return_value=entry_node) as mock_get_entry, \
             patch("watercooler.baseline_graph.sync.sync_to_memory_backend") as mock_sync:

            result = run_with_sync(
                context=context,
                commit_title="test commit",
                operation=lambda: "test-result",
                topic=topic,
                entry_id=entry_id,
            )

            return result, mock_sync, mock_enrich

    def test_memory_sync_called_when_enrichment_not_configured(self, tmp_path):
        """Memory sync should fire even when enrichment is disabled."""
        result, mock_sync, _ = self._run_middleware(
            tmp_path,
            wants_enrichment=False,
        )
        assert result == "test-result"
        mock_sync.assert_called_once()

    def test_memory_sync_called_when_enrichment_services_unavailable(self, tmp_path):
        """Memory sync should fire even when LLM/embedding services are down."""
        result, mock_sync, _ = self._run_middleware(
            tmp_path,
            wants_enrichment=True,
            llm_available=False,
            embed_available=False,
        )
        assert result == "test-result"
        mock_sync.assert_called_once()

    def test_memory_sync_called_when_enrichment_noop(self, tmp_path):
        """Memory sync should fire even when enrichment returns noop."""
        from watercooler.baseline_graph.sync import EnrichmentResult

        result, mock_sync, _ = self._run_middleware(
            tmp_path,
            wants_enrichment=True,
            llm_available=True,
            embed_available=False,
            enrich_result=EnrichmentResult.noop(),
        )
        assert result == "test-result"
        mock_sync.assert_called_once()

    def test_memory_sync_failure_does_not_block_write(self, tmp_path):
        """Memory sync failure should not prevent write from completing."""
        from watercooler_mcp.middleware import run_with_sync

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(exist_ok=True)

        context = MagicMock()
        context.threads_dir = threads_dir
        context.code_root = tmp_path / "code"

        mock_graph_config = MagicMock()
        mock_graph_config.generate_summaries = False
        mock_graph_config.generate_embeddings = False
        mock_wc_config = MagicMock()
        mock_wc_config.mcp.graph = mock_graph_config

        entry_node = {"body": "test", "title": "T", "timestamp": None,
                      "agent": "a", "role": "r", "entry_type": "Note"}

        with patch("watercooler_mcp.middleware.get_watercooler_config", return_value=mock_wc_config), \
             patch("watercooler_mcp.middleware._check_enrichment_services_available", return_value=(False, False)), \
             patch("watercooler_mcp.middleware.acquire_topic_lock"), \
             patch("watercooler.baseline_graph.writer.get_entry_node_from_graph", return_value=entry_node), \
             patch("watercooler.baseline_graph.sync.sync_to_memory_backend", side_effect=RuntimeError("sync boom")):

            result = run_with_sync(
                context=context,
                commit_title="test",
                operation=lambda: "success",
                topic="t",
                entry_id="e1",
            )

        assert result == "success"

    def test_memory_sync_not_called_without_entry_id(self, tmp_path):
        """Memory sync should NOT be called when entry_id is None."""
        from watercooler_mcp.middleware import run_with_sync

        context = MagicMock()
        context.threads_dir = tmp_path
        context.code_root = tmp_path

        with patch("watercooler_mcp.middleware.acquire_topic_lock"):
            with patch("watercooler.baseline_graph.sync.sync_to_memory_backend") as mock_sync:
                result = run_with_sync(
                    context=context,
                    commit_title="test",
                    operation=lambda: "ok",
                    topic="t",
                    entry_id=None,
                )

        assert result == "ok"
        mock_sync.assert_not_called()

    def test_memory_sync_with_partial_entry_node(self, tmp_path):
        """Memory sync should handle entry nodes with missing optional fields."""
        partial_node = {
            "body": "minimal body",
            # title, timestamp, agent, role, entry_type, summary all missing
        }
        result, mock_sync, _ = self._run_middleware(
            tmp_path,
            wants_enrichment=False,
            entry_node=partial_node,
        )
        assert result == "test-result"
        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args[1]
        assert call_kwargs["entry_body"] == "minimal body"
        assert call_kwargs["entry_title"] is None
        assert call_kwargs["entry_summary"] == ""
        assert call_kwargs["timestamp"] is None
        assert call_kwargs["agent"] is None
        assert call_kwargs["role"] is None
        assert call_kwargs["entry_type"] is None

    def test_enrich_no_longer_calls_memory_sync(self, tmp_path):
        """enrich_graph_entry should NOT call sync_to_memory_backend."""
        from watercooler.baseline_graph.sync import enrich_graph_entry

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(exist_ok=True)

        # Create a mock entry node
        entry_node = {
            "body": "test body",
            "title": "Test",
            "entry_type": "Note",
            "summary": "",
        }

        # Patch where the name is looked up (sync module imports it at top level)
        with patch("watercooler.baseline_graph.sync.get_entry_node_from_graph", return_value=entry_node), \
             patch("watercooler.baseline_graph.sync.sync_to_memory_backend") as mock_sync:

            result = enrich_graph_entry(
                threads_dir=threads_dir,
                topic="test-topic",
                entry_id="e1",
                generate_summaries=False,
                generate_embeddings=False,
            )

        # Should return noop since no enrichment requested
        assert result.is_noop
        # Memory sync should NOT be called from enrich_graph_entry anymore
        mock_sync.assert_not_called()
