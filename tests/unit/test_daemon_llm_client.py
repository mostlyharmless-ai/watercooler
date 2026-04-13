"""Tests for daemon LLM client and config resolution."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from watercooler.memory_config import (
    ResolvedDaemonLLMConfig,
    resolve_daemon_llm_config,
)
from watercooler_mcp.daemons.llm_client import DaemonLLMClient


# ------------------------------------------------------------------ #
# resolve_daemon_llm_config tests
# ------------------------------------------------------------------ #


class TestResolveDaemonLLMConfig:
    """Tests for daemon LLM config resolution priority chain."""

    def test_falls_through_to_baseline(self):
        """With no daemon-specific config, falls through to baseline."""
        result = resolve_daemon_llm_config()
        assert isinstance(result, ResolvedDaemonLLMConfig)
        assert result.api_base  # Should have some value from baseline
        assert result.model  # Should have some value from baseline

    def test_env_vars_highest_priority(self, monkeypatch):
        """Environment variables take highest priority."""
        monkeypatch.setenv("DAEMON_LLM_API_BASE", "http://test:9999/v1")
        monkeypatch.setenv("DAEMON_LLM_MODEL", "test-model")
        monkeypatch.setenv("DAEMON_LLM_API_KEY", "test-key-123")
        monkeypatch.setenv("DAEMON_LLM_TIMEOUT", "30")
        monkeypatch.setenv("DAEMON_LLM_MAX_TOKENS", "1024")

        result = resolve_daemon_llm_config("content_refiner")
        assert result.api_base == "http://test:9999/v1"
        assert result.model == "test-model"
        assert result.api_key == "test-key-123"
        assert result.timeout == 30.0
        assert result.max_tokens == 1024

    def test_daemon_name_empty_string(self):
        """Empty daemon name skips per-daemon lookup."""
        result = resolve_daemon_llm_config("")
        assert isinstance(result, ResolvedDaemonLLMConfig)

    def test_repr_redacts_key(self):
        """API key is redacted in repr."""
        cfg = ResolvedDaemonLLMConfig(
            api_key="sk-very-secret-key-12345",
            api_base="http://localhost:8000/v1",
            model="test",
            timeout=30.0,
            max_tokens=256,
        )
        repr_str = repr(cfg)
        assert "sk-very-" not in repr_str
        assert "12345" not in repr_str
        assert "***" in repr_str

    def test_frozen_dataclass(self):
        """Config is immutable."""
        cfg = ResolvedDaemonLLMConfig(
            api_key="key",
            api_base="http://localhost:8000/v1",
            model="test",
            timeout=30.0,
            max_tokens=256,
        )
        with pytest.raises(AttributeError):
            cfg.model = "other"  # type: ignore[misc]


# ------------------------------------------------------------------ #
# DaemonLLMClient tests
# ------------------------------------------------------------------ #


class TestDaemonLLMClient:
    """Tests for the daemon LLM client."""

    @pytest.fixture
    def mock_config(self):
        return ResolvedDaemonLLMConfig(
            api_key="test-key",
            api_base="http://localhost:8000/v1",
            model="test-model",
            timeout=30.0,
            max_tokens=256,
        )

    def test_init_with_config(self, mock_config):
        """Client initializes with explicit config."""
        client = DaemonLLMClient(config=mock_config, daemon_name="test")
        assert client.config == mock_config
        assert client._is_localhost is True

    def test_init_remote_url(self):
        """Client detects non-localhost URLs."""
        config = ResolvedDaemonLLMConfig(
            api_key="key",
            api_base="https://api.openai.com/v1",
            model="gpt-4",
            timeout=30.0,
            max_tokens=256,
        )
        client = DaemonLLMClient(config=config)
        assert client._is_localhost is False

    def test_is_available_empty_api_base(self):
        """Returns False with empty api_base."""
        config = ResolvedDaemonLLMConfig(
            api_key="",
            api_base="",
            model="test",
            timeout=30.0,
            max_tokens=256,
        )
        client = DaemonLLMClient(config=config, daemon_name="test")
        assert client.is_available() is False

    def test_complete_builds_messages(self, mock_config):
        """Complete builds correct message structure."""
        client = DaemonLLMClient(config=mock_config, daemon_name="test")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "test response"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        import httpx
        with patch.object(httpx, "Client", return_value=mock_client):
            result = client.complete(
                prompt="Hello",
                system="Be helpful",
            )

        assert result == "test response"
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"

    def test_complete_no_system_message(self, mock_config):
        """Complete omits system message when not provided."""
        client = DaemonLLMClient(config=mock_config, daemon_name="test")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "response"}}],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        import httpx
        with patch.object(httpx, "Client", return_value=mock_client):
            result = client.complete(prompt="Hello")

        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert len(payload["messages"]) == 1  # No system message

    def test_complete_returns_none_on_error(self, mock_config):
        """Complete returns None on connection error."""
        client = DaemonLLMClient(config=mock_config, daemon_name="test")

        import httpx
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        with patch.object(httpx, "Client", return_value=mock_client):
            result = client.complete(prompt="Hello")

        assert result is None

    def test_complete_returns_none_on_empty(self, mock_config):
        """Complete returns None when LLM returns empty content."""
        client = DaemonLLMClient(config=mock_config, daemon_name="test")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": ""}}],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        import httpx
        with patch.object(httpx, "Client", return_value=mock_client):
            result = client.complete(prompt="Hello")

        assert result is None

    def test_localhost_semaphore_used(self, mock_config):
        """Localhost URLs use per-port concurrency semaphores."""
        client = DaemonLLMClient(config=mock_config, daemon_name="test")
        assert client._is_localhost is True

        # Verify per-port semaphore mechanism
        from watercooler_mcp.daemons.llm_client import _get_localhost_semaphore
        sem = _get_localhost_semaphore(mock_config.api_base)
        assert isinstance(sem, threading.Semaphore)
        # Same port returns same semaphore
        sem2 = _get_localhost_semaphore(mock_config.api_base)
        assert sem is sem2

    def test_auth_header_for_non_local(self):
        """Non-local endpoints get Authorization header."""
        config = ResolvedDaemonLLMConfig(
            api_key="sk-test-key",
            api_base="https://api.openai.com/v1",
            model="gpt-4",
            timeout=30.0,
            max_tokens=256,
        )
        client = DaemonLLMClient(config=config, daemon_name="test")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        import httpx
        with patch.object(httpx, "Client", return_value=mock_client):
            client.complete(prompt="test")

        call_args = mock_client.post.call_args
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer sk-test-key"
