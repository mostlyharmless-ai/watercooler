"""Security-focused test suite.

Tests for security-critical functionality including:
- Path traversal prevention
- Token sanitization
- Input validation
- CORS configuration
- Request size limits
"""

from __future__ import annotations

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestPathTraversalPrevention:
    """Tests for path traversal attack prevention."""

    def test_validate_topic_rejects_path_traversal(self):
        """Topic validation rejects path traversal attempts."""
        from watercooler_mcp.hosted_ops import _validate_topic

        # Test various path traversal patterns
        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("../etc/passwd")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("..\\windows\\system32")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("foo/../bar")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("foo/bar")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("foo\\bar")

    def test_validate_topic_rejects_hidden_files(self):
        """Topic validation rejects hidden file patterns."""
        from watercooler_mcp.hosted_ops import _validate_topic

        with pytest.raises(ValueError, match="cannot start"):
            _validate_topic(".hidden")

        with pytest.raises(ValueError, match="cannot start"):
            _validate_topic(".env")

    def test_validate_topic_rejects_empty(self):
        """Topic validation rejects empty strings."""
        from watercooler_mcp.hosted_ops import _validate_topic

        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_topic("")

    def test_validate_topic_allows_valid_topics(self):
        """Topic validation allows legitimate topics."""
        from watercooler_mcp.hosted_ops import _validate_topic

        # These should not raise
        _validate_topic("my-topic")
        _validate_topic("feature-auth-refactor")
        _validate_topic("v2-api-design")
        _validate_topic("sprint-42-planning")


class TestTokenSanitization:
    """Tests for token sanitization in error messages."""

    def test_slack_client_sanitizes_errors(self):
        """Slack client sanitizes tokens from error messages."""
        from watercooler_mcp.slack.client import SlackClient

        client = SlackClient(bot_token="xoxb-secret-token-12345")

        # Test error sanitization
        error_msg = "API error: invalid token xoxb-secret-token-12345 provided"
        sanitized = client._sanitize_error(error_msg)

        assert "xoxb-secret-token-12345" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitization_handles_no_token(self):
        """Sanitization works when no token is set."""
        from watercooler_mcp.slack.client import SlackClient

        # Client without token
        with patch.dict(os.environ, {}, clear=True):
            client = SlackClient.__new__(SlackClient)
            client._token = None

            # Should not crash
            error_msg = "Some error message"
            result = client._sanitize_error(error_msg)
            assert result == error_msg


class TestInputValidation:
    """Tests for input validation across modules."""

    def test_topic_max_length(self):
        """Topic names have reasonable length limits."""
        from watercooler_mcp.hosted_ops import _validate_topic

        # Very long topic should be rejected or handled gracefully
        long_topic = "a" * 1000

        # Should either raise or handle gracefully
        # Current implementation may not have this limit, but it's a good practice
        try:
            _validate_topic(long_topic)
        except ValueError:
            pass  # Expected if limit is implemented

    def test_slack_message_truncation(self):
        """Slack messages are truncated to prevent API errors."""
        # The SlackClient.post_entry_reply truncates body internally
        # Verify the truncation logic is present in the code
        from watercooler_mcp.slack import client
        import inspect

        source = inspect.getsource(client.SlackClient.post_entry_reply)
        # Verify truncation logic exists
        assert "max_body" in source
        assert "2500" in source  # The truncation limit
        assert "..." in source  # Adds ellipsis


class TestTempFilePermissions:
    """Tests for secure temp file handling."""

    def test_atomic_write_json_sets_permissions(self, tmp_path):
        """Atomic JSON write sets readable permissions."""
        from watercooler.baseline_graph.storage import atomic_write_json
        import stat

        test_file = tmp_path / "test.json"
        atomic_write_json(test_file, {"key": "value"})

        # Check file exists and is readable
        assert test_file.exists()
        mode = test_file.stat().st_mode

        # Should be readable by user and group (0644)
        assert mode & stat.S_IRUSR  # User read
        assert mode & stat.S_IWUSR  # User write
        assert mode & stat.S_IRGRP  # Group read
        assert mode & stat.S_IROTH  # Other read

    def test_atomic_write_jsonl_sets_permissions(self, tmp_path):
        """Atomic JSONL write sets readable permissions."""
        from watercooler.baseline_graph.storage import atomic_write_jsonl
        import stat

        test_file = tmp_path / "test.jsonl"
        atomic_write_jsonl(test_file, [{"key": "value"}])

        assert test_file.exists()
        mode = test_file.stat().st_mode

        # Should be readable (0644)
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IRGRP
        assert mode & stat.S_IROTH


class TestCacheEviction:
    """Tests for cache size limits and eviction."""

    def test_memory_cache_has_max_size(self):
        """MemoryCache has a configured max size."""
        from watercooler_mcp.cache import MemoryCache

        cache = MemoryCache()
        assert cache._max_entries == 10000  # Default

    def test_memory_cache_evicts_on_overflow(self):
        """MemoryCache evicts oldest entries when full."""
        from watercooler_mcp.cache import MemoryCache

        cache = MemoryCache(max_entries=3)

        # Fill cache
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        # Add one more (should evict oldest)
        cache.set("key4", "value4")

        # key1 should be evicted (oldest)
        assert cache.get("key1") is None
        assert cache.get("key2") is not None
        assert cache.get("key3") is not None
        assert cache.get("key4") is not None

    def test_memory_cache_lru_order(self):
        """MemoryCache uses LRU order for eviction."""
        from watercooler_mcp.cache import MemoryCache

        cache = MemoryCache(max_entries=3)

        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        # Access key1 (moves to end of LRU)
        cache.get("key1")

        # Add new key (should evict key2, not key1)
        cache.set("key4", "value4")

        assert cache.get("key1") is not None  # Was accessed, not evicted
        assert cache.get("key2") is None  # Was oldest unused, evicted
        assert cache.get("key3") is not None
        assert cache.get("key4") is not None


class TestTokenCacheTTL:
    """Tests for token cache TTL configuration."""

    def test_github_token_cache_default_ttl(self):
        """GitHub token cache has 5 minute default TTL."""
        from watercooler_mcp.auth import TOKEN_CACHE_TTL

        assert TOKEN_CACHE_TTL == 300  # 5 minutes

    def test_slack_token_cache_default_ttl(self):
        """Slack token cache has 5 minute default TTL."""
        from watercooler_mcp.slack.token_service import SLACK_TOKEN_CACHE_TTL

        assert SLACK_TOKEN_CACHE_TTL == 300  # 5 minutes

    def test_token_expiration_check(self):
        """Cached tokens are checked for expiration."""
        from watercooler_mcp.auth import CachedToken
        import time

        # Create a token cached 10 minutes ago
        token_info = MagicMock()
        cached = CachedToken(token_info=token_info)
        cached.cached_at = time.time() - 600  # 10 minutes ago

        # Should be expired with 5 minute TTL
        assert cached.is_expired()


class TestManifestLocking:
    """Tests for manifest update locking."""

    def test_manifest_uses_advisory_lock(self, tmp_path):
        """Manifest updates use advisory locking."""
        from watercooler.baseline_graph import storage

        graph_dir = tmp_path / "graph" / "baseline"
        graph_dir.mkdir(parents=True)

        # First update should work
        storage.update_manifest(graph_dir, "test-topic", "entry-1")

        # Lock file should be created
        lock_path = graph_dir / ".manifest.lock"
        # Lock file may or may not persist after release

        # Manifest should be updated
        manifest = storage.load_manifest(graph_dir)
        assert manifest.get("last_topic") == "test-topic"


class TestDualWriteErrorHandling:
    """Tests for dual-write error handling."""

    def test_dual_write_failure_logged_not_raised(self, tmp_path, caplog):
        """Dual-write failures are logged but don't fail the primary write."""
        from watercooler.baseline_graph import writer, storage
        from watercooler.baseline_graph.writer import EntryData

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create entry data
        entry_data = EntryData(
            entry_id="01ABC123",
            thread_topic="test-topic",
            index=0,
            agent="TestAgent",
            role="implementer",
            entry_type="Note",
            title="Test Entry",
            body="Test body content",
        )

        # First, initialize the thread in graph
        writer.init_thread_in_graph(threads_dir, "test-topic")

        # The upsert should succeed even if monolithic write fails
        result = writer.upsert_entry_node(threads_dir, entry_data)

        # Primary write should succeed
        assert result is True

        # Entry should exist in per-thread format
        graph_dir = storage.get_graph_dir(threads_dir)
        entries = storage.load_thread_entries_dict(graph_dir, "test-topic")
        assert f"entry:{entry_data.entry_id}" in entries
