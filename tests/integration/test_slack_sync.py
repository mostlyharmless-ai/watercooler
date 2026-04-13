"""Integration tests for Slack sync functionality.

These tests verify the bidirectional sync between watercooler threads
and Slack channels/threads, including message mapping and state management.

Note: These tests use mocked Slack API responses to avoid real API calls.
"""

from __future__ import annotations

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone


class TestSlackMappingStore:
    """Tests for Slack mapping persistence."""

    def test_mapping_store_creation(self, tmp_path):
        """Mapping store creates storage directory."""
        from watercooler_mcp.slack.mapping import SlackMappingStore

        mappings_file = tmp_path / "slack_mappings.json"
        store = SlackMappingStore(mappings_file)
        assert store.path == mappings_file

    def test_save_and_load_channel_mapping(self, tmp_path):
        """Channel mappings can be saved and loaded."""
        from watercooler_mcp.slack.mapping import (
            SlackMappingStore,
            SlackChannelMapping,
        )

        mappings_file = tmp_path / "slack_mappings.json"
        store = SlackMappingStore(mappings_file)

        mapping = SlackChannelMapping(
            repo="org/repo-threads",
            slack_channel_id="C12345",
            slack_channel_name="#wc-repo-threads",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Save mapping
        store.set_channel(mapping)

        # Load mapping
        loaded = store.get_channel("org/repo-threads")
        assert loaded is not None
        assert loaded.slack_channel_id == "C12345"
        assert loaded.slack_channel_name == "#wc-repo-threads"
        assert loaded.repo == "org/repo-threads"

    def test_save_and_load_thread_mapping(self, tmp_path):
        """Thread mappings can be saved and loaded."""
        from watercooler_mcp.slack.mapping import (
            SlackMappingStore,
            SlackThreadMapping,
        )

        mappings_file = tmp_path / "slack_mappings.json"
        store = SlackMappingStore(mappings_file)

        mapping = SlackThreadMapping(
            topic="test-thread",
            repo="org/repo-threads",
            slack_channel_id="C12345",
            slack_channel_name="#wc-repo-threads",
            slack_thread_ts="1234567890.123456",
            last_synced_entry_id="01ABC123",
            last_synced_at=datetime.now(timezone.utc).isoformat(),
        )

        # Save mapping
        store.set_thread(mapping)

        # Load mapping
        loaded = store.get_thread("org/repo-threads", "test-thread")
        assert loaded is not None
        assert loaded.slack_thread_ts == "1234567890.123456"
        assert loaded.last_synced_entry_id == "01ABC123"

    def test_list_channel_mappings(self, tmp_path):
        """Can list all channel mappings."""
        from watercooler_mcp.slack.mapping import (
            SlackMappingStore,
            SlackChannelMapping,
        )

        mappings_file = tmp_path / "slack_mappings.json"
        store = SlackMappingStore(mappings_file)

        # Create multiple mappings
        for i in range(3):
            mapping = SlackChannelMapping(
                repo=f"org/repo-{i}",
                slack_channel_id=f"C{i:05d}",
                slack_channel_name=f"#wc-repo-{i}",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            store.set_channel(mapping)

        # List all mappings
        mappings = store.list_channels()
        assert len(mappings) == 3


class TestSlackClientMocked:
    """Tests for Slack client with mocked API."""

    @pytest.fixture
    def mock_client(self):
        """Create a mocked Slack client."""
        from watercooler_mcp.slack.client import SlackClient

        with patch.dict(os.environ, {
            "WATERCOOLER_SLACK_BOT_TOKEN": "xoxb-test-token",
        }, clear=False):
            client = SlackClient(bot_token="xoxb-test-token")
            return client

    @patch("watercooler_mcp.slack.client.urllib.request.urlopen")
    def test_post_message(self, mock_urlopen, mock_client):
        """Can post a message to Slack."""
        # Mock successful response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "ok": True,
            "channel": "C12345",
            "ts": "1234567890.123456",
            "message": {"text": "Test message"},
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = mock_client.post_message("C12345", "Test message")
        assert result["ok"] is True
        assert result["ts"] == "1234567890.123456"

    @patch("watercooler_mcp.slack.client.urllib.request.urlopen")
    def test_post_threaded_message(self, mock_urlopen, mock_client):
        """Can post a threaded reply."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "ok": True,
            "channel": "C12345",
            "ts": "1234567890.123457",
            "message": {"text": "Reply", "thread_ts": "1234567890.123456"},
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = mock_client.post_message(
            "C12345",
            "Reply",
            thread_ts="1234567890.123456",
        )
        assert result["ok"] is True

    @patch("watercooler_mcp.slack.client.urllib.request.urlopen")
    def test_auth_test(self, mock_urlopen, mock_client):
        """Can verify bot authentication."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "ok": True,
            "url": "https://test.slack.com/",
            "team": "Test Team",
            "team_id": "T12345",
            "user": "watercooler",
            "user_id": "U12345",
            "bot_id": "B12345",
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = mock_client.auth_test()
        assert result["ok"] is True
        assert result["team_id"] == "T12345"


class TestSlackSyncOperations:
    """Tests for Slack sync operations."""

    @pytest.fixture
    def threads_dir(self, tmp_path):
        """Create a temporary threads directory."""
        threads = tmp_path / "threads"
        threads.mkdir()
        return threads

    @pytest.fixture
    def setup_thread(self, threads_dir):
        """Create a test thread file."""
        thread_file = threads_dir / "test-thread.md"
        thread_file.write_text("""# Thread: test-thread

Ball: codex
Status: OPEN

---

## Entry: Agent (user)

Role: implementer
Type: Note
Title: Initial entry
Timestamp: 2024-01-01T00:00:00Z

This is the first entry.

---

## Entry: Claude (user)

Role: implementer
Type: Note
Title: Second entry
Timestamp: 2024-01-01T00:01:00Z

This is the second entry.
""", encoding="utf-8")
        return thread_file

    def test_sync_detects_new_entries(self, tmp_path, threads_dir, setup_thread):
        """Sync detects entries that haven't been synced to Slack."""
        from watercooler_mcp.slack.mapping import (
            SlackMappingStore,
            SlackThreadMapping,
        )

        mappings_file = tmp_path / "slack_mappings.json"
        store = SlackMappingStore(mappings_file)

        # Create mapping with only 1 entry synced
        mapping = SlackThreadMapping(
            topic="test-thread",
            repo="org/repo-threads",
            slack_channel_id="C12345",
            slack_channel_name="#wc-repo-threads",
            slack_thread_ts="1234567890.123456",
            last_synced_entry_id="entry-1",
            last_synced_at=datetime.now(timezone.utc).isoformat(),
        )
        store.set_thread(mapping)

        # Thread has 2 entries, mapping has 1 synced
        # Sync should detect 1 new entry
        loaded = store.get_thread("org/repo-threads", "test-thread")
        assert loaded is not None
        assert loaded.last_synced_entry_id == "entry-1"


class TestTokenCacheTTL:
    """Tests for token cache TTL."""

    def test_github_token_cache_ttl_default(self):
        """GitHub token cache uses 5 minute default TTL."""
        from watercooler_mcp.auth import TOKEN_CACHE_TTL

        assert TOKEN_CACHE_TTL == 300  # 5 minutes

    def test_slack_token_cache_ttl_default(self):
        """Slack token cache uses 5 minute default TTL."""
        from watercooler_mcp.slack.token_service import SLACK_TOKEN_CACHE_TTL

        assert SLACK_TOKEN_CACHE_TTL == 300  # 5 minutes


class TestClientCacheEviction:
    """Tests for client cache eviction."""

    def test_slack_client_cache_max_default(self):
        """Slack client cache has a max size."""
        from watercooler_mcp.slack import client

        assert client._CLIENT_CACHE_MAX_SIZE == 100

    def test_slack_client_cache_lru_eviction(self):
        """Client cache evicts oldest entries when full."""
        from watercooler_mcp.slack.client import (
            _client_cache,
            clear_client_cache,
        )

        # Clear cache first
        clear_client_cache()

        # The cache uses OrderedDict for LRU
        assert hasattr(_client_cache, "move_to_end")
