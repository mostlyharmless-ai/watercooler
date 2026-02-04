"""Tests for API key configuration: localhost tolerance and error messages.

Validates that:
- Localhost endpoints don't require API keys
- Remote endpoints still require API keys
- Error messages reference credentials.toml (not config.toml)
- _is_localhost_url helper works correctly
"""

from __future__ import annotations

import json

import pytest

from watercooler.memory_config import _is_localhost_url
from watercooler_mcp import memory


@pytest.fixture
def isolated_config(tmp_path, monkeypatch, clean_api_keys):
    """Isolate tests from user AND project config.

    Prevents user's ~/.watercooler/config.toml from affecting tests
    and stops project config discovery walking up to user's home.
    """
    watercooler_dir = tmp_path / ".watercooler"
    watercooler_dir.mkdir()

    from watercooler.config_facade import config
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    config.reset()
    yield tmp_path
    config.reset()


class TestIsLocalhostUrl:
    """Tests for _is_localhost_url helper."""

    def test_localhost_hostname(self):
        assert _is_localhost_url("http://localhost:8080") is True
        assert _is_localhost_url("http://localhost:8080/v1") is True
        assert _is_localhost_url("https://localhost:443") is True
        assert _is_localhost_url("http://localhost") is True

    def test_ipv4_loopback(self):
        assert _is_localhost_url("http://127.0.0.1:8080") is True
        assert _is_localhost_url("http://127.0.0.1:8080/api") is True

    def test_all_interfaces(self):
        assert _is_localhost_url("http://0.0.0.0:8080") is True

    def test_remote_urls(self):
        assert _is_localhost_url("http://example.com:8080") is False
        assert _is_localhost_url("https://api.openai.com/v1") is False
        assert _is_localhost_url("http://192.168.1.100:8080") is False
        assert _is_localhost_url("https://api.deepseek.com/v1") is False

    def test_invalid_input(self):
        assert _is_localhost_url("not a url") is False
        assert _is_localhost_url("") is False

    def test_case_insensitive(self):
        assert _is_localhost_url("http://LOCALHOST:8080") is True
        assert _is_localhost_url("http://LocalHost:8080") is True

    def test_ipv6_bracket_form(self):
        """IPv6 bracket notation should be recognized as localhost."""
        assert _is_localhost_url("http://[::1]:8000") is True
        assert _is_localhost_url("http://[::1]:8080/v1") is True

    def test_credentials_in_url(self):
        """URLs with user:pass@ should still detect localhost."""
        assert _is_localhost_url("http://user:pass@localhost:8080") is True
        assert _is_localhost_url("http://user:pass@127.0.0.1:8000/v1") is True

    def test_scheme_less_url(self):
        """URLs without scheme return False (scheme is required)."""
        assert _is_localhost_url("localhost:8000") is False
        assert _is_localhost_url("127.0.0.1:8080") is False


class TestLocalhostLlmNoKeyRequired:
    """Test that localhost LLM endpoints don't require API keys."""

    def test_localhost_llm_no_key_succeeds(self, monkeypatch, isolated_config):
        """load_graphiti_config() succeeds with localhost LLM + no key."""
        from watercooler.config_facade import config as cfg
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("LLM_API_BASE", "http://localhost:8000/v1")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("EMBEDDING_API_BASE", "http://localhost:8080/v1")
        monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
        cfg.reset()

        result = memory.load_graphiti_config()
        assert result is not None
        assert result.llm_api_key == "LOCAL_NO_KEY"

    def test_localhost_127_llm_no_key_succeeds(self, monkeypatch, isolated_config):
        """load_graphiti_config() succeeds with 127.0.0.1 LLM + no key."""
        from watercooler.config_facade import config as cfg
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("LLM_API_BASE", "http://127.0.0.1:8000/v1")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test-embed")
        cfg.reset()

        result = memory.load_graphiti_config()
        assert result is not None
        assert result.llm_api_key == "LOCAL_NO_KEY"


class TestLocalhostEmbeddingNoKeyRequired:
    """Test that localhost embedding endpoints don't require API keys."""

    def test_localhost_embedding_no_key_succeeds(self, monkeypatch, isolated_config):
        """load_graphiti_config() succeeds with localhost embedding + no key."""
        from watercooler.config_facade import config as cfg
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test-llm")
        monkeypatch.setenv("EMBEDDING_API_BASE", "http://localhost:8080/v1")
        monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
        cfg.reset()

        result = memory.load_graphiti_config()
        assert result is not None
        assert result.embedding_api_key == "LOCAL_NO_KEY"

    def test_both_localhost_no_keys(self, monkeypatch, isolated_config):
        """load_graphiti_config() succeeds with both endpoints on localhost + no keys."""
        from watercooler.config_facade import config as cfg
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("LLM_API_BASE", "http://localhost:8000/v1")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("EMBEDDING_API_BASE", "http://localhost:8080/v1")
        monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
        cfg.reset()

        result = memory.load_graphiti_config()
        assert result is not None
        assert result.llm_api_key == "LOCAL_NO_KEY"
        assert result.embedding_api_key == "LOCAL_NO_KEY"


class TestRemoteEndpointRequiresKey:
    """Test that remote endpoints still require API keys."""

    def test_remote_llm_no_key_returns_none(self, monkeypatch, isolated_config):
        """load_graphiti_config() returns None for remote LLM with no key."""
        from watercooler.config_facade import config as cfg
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("LLM_API_BASE", "https://api.openai.com/v1")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test-embed")
        cfg.reset()

        result = memory.load_graphiti_config()
        assert result is None

    def test_remote_embedding_no_key_returns_none(self, monkeypatch, isolated_config):
        """load_graphiti_config() returns None for remote embedding with no key."""
        from watercooler.config_facade import config as cfg
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test-llm")
        monkeypatch.setenv("EMBEDDING_API_BASE", "https://api.openai.com/v1")
        monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg.reset()

        result = memory.load_graphiti_config()
        assert result is None


class TestDiagnoseMemoryErrorMessage:
    """Test that diagnose_memory error message references credentials.toml."""

    def test_config_issue_references_credentials(self, monkeypatch, isolated_config):
        """config_issue message mentions credentials.toml, not config.toml api_key."""
        from watercooler.config_facade import config as cfg
        from watercooler_mcp.tools.memory import _diagnose_memory_impl
        from unittest.mock import MagicMock

        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "0")
        cfg.reset()

        ctx = MagicMock()
        result = _diagnose_memory_impl(ctx, code_path="")

        # Extract text from ToolResult
        text = result.content[0].text
        diagnostics = json.loads(text)

        assert "config_issue" in diagnostics
        msg = diagnostics["config_issue"]
        # Should reference credentials.toml
        assert "credentials.toml" in msg
        # Should reference env vars
        assert "LLM_API_KEY" in msg or "EMBEDDING_API_KEY" in msg
        # Should NOT reference old config.toml api_key path
        assert "[memory.llm].api_key" not in msg
        assert "[memory.embedding].api_key" not in msg
