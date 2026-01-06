"""Tests for the memory configuration validation script.

Tests the scripts/check-memory-config.py module functions.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest

# Add scripts directory to path for imports
scripts_dir = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

# Import after path setup - use importlib to handle hyphenated filename
import importlib.util

spec = importlib.util.spec_from_file_location(
    "check_memory_config",
    scripts_dir / "check-memory-config.py"
)
check_memory_config = importlib.util.module_from_spec(spec)
# Register module in sys.modules before exec (required for Python 3.12+ dataclasses)
sys.modules["check_memory_config"] = check_memory_config
spec.loader.exec_module(check_memory_config)


class TestGetEnv:
    """Tests for get_env function."""

    def test_returns_value_when_set(self, monkeypatch):
        """Test that get_env returns the value when env var is set."""
        monkeypatch.setenv("TEST_VAR", "test_value")
        assert check_memory_config.get_env("TEST_VAR") == "test_value"

    def test_returns_default_when_not_set(self, monkeypatch):
        """Test that get_env returns default when env var is not set."""
        monkeypatch.delenv("TEST_VAR_MISSING", raising=False)
        assert check_memory_config.get_env("TEST_VAR_MISSING", "default") == "default"

    def test_returns_none_for_empty_string(self, monkeypatch):
        """Test that get_env returns None for empty string values."""
        monkeypatch.setenv("TEST_VAR_EMPTY", "")
        assert check_memory_config.get_env("TEST_VAR_EMPTY") is None

    def test_returns_none_when_not_set_no_default(self, monkeypatch):
        """Test that get_env returns None when not set and no default."""
        monkeypatch.delenv("TEST_VAR_MISSING", raising=False)
        assert check_memory_config.get_env("TEST_VAR_MISSING") is None


class TestNormalizeUrl:
    """Tests for normalize_url function."""

    def test_returns_none_for_none(self):
        """Test that normalize_url returns None for None input."""
        assert check_memory_config.normalize_url(None) is None

    def test_removes_trailing_slash(self):
        """Test that normalize_url removes trailing slashes."""
        assert check_memory_config.normalize_url("http://localhost:8080/") == "http://localhost:8080"

    def test_removes_v1_suffix(self):
        """Test that normalize_url removes /v1 suffix."""
        assert check_memory_config.normalize_url("http://localhost:8080/v1") == "http://localhost:8080"

    def test_removes_both_slash_and_v1(self):
        """Test that normalize_url removes both trailing slash and /v1."""
        assert check_memory_config.normalize_url("http://localhost:8080/v1/") == "http://localhost:8080"

    def test_preserves_url_without_v1(self):
        """Test that normalize_url preserves URLs without /v1."""
        assert check_memory_config.normalize_url("http://localhost:8080") == "http://localhost:8080"


class TestCheckConsistency:
    """Tests for check_consistency function."""

    def test_no_warnings_when_consistent(self):
        """Test no warnings when all backends use same servers."""
        backends = {
            "graphiti": check_memory_config.BackendConfig(
                name="Graphiti",
                embedding=check_memory_config.ServerConfig(
                    name="EMBEDDING_API_BASE",
                    url="http://localhost:8080/v1",
                    key=None,
                    model="bge-m3",
                ),
                llm=check_memory_config.ServerConfig(
                    name="LLM_API_BASE",
                    url="http://localhost:8000/v1",
                    key=None,
                    model="local",
                ),
            ),
            "leanrag": check_memory_config.BackendConfig(
                name="LeanRAG",
                embedding=check_memory_config.ServerConfig(
                    name="GLM_BASE_URL",
                    url="http://localhost:8080/v1",
                    key=None,
                    model="bge-m3",
                ),
                llm=check_memory_config.ServerConfig(
                    name="DEEPSEEK_BASE_URL",
                    url="http://localhost:8000/v1",
                    key=None,
                    model="local",
                ),
            ),
        }

        warnings, errors = check_memory_config.check_consistency(backends)
        assert warnings == []
        assert errors == []

    def test_warns_when_embedding_servers_differ(self):
        """Test warning when embedding servers differ."""
        backends = {
            "graphiti": check_memory_config.BackendConfig(
                name="Graphiti",
                embedding=check_memory_config.ServerConfig(
                    name="EMBEDDING_API_BASE",
                    url="http://localhost:8080/v1",
                    key=None,
                    model="bge-m3",
                ),
                llm=check_memory_config.ServerConfig(
                    name="LLM_API_BASE",
                    url=None,
                    key=None,
                    model=None,
                ),
            ),
            "leanrag": check_memory_config.BackendConfig(
                name="LeanRAG",
                embedding=check_memory_config.ServerConfig(
                    name="GLM_BASE_URL",
                    url="http://different-server:9090/v1",
                    key=None,
                    model="bge-m3",
                ),
                llm=check_memory_config.ServerConfig(
                    name="DEEPSEEK_BASE_URL",
                    url=None,
                    key=None,
                    model=None,
                ),
            ),
        }

        warnings, errors = check_memory_config.check_consistency(backends)
        assert len(warnings) == 1
        assert "Embedding servers differ" in warnings[0]


class TestGenerateEnvTemplate:
    """Tests for generate_env_template function."""

    def test_contains_disable_switch(self):
        """Test that template contains disable switch."""
        template = check_memory_config.generate_env_template()
        assert "WATERCOOLER_MEMORY_DISABLED" in template

    def test_contains_graphiti_enabled(self):
        """Test that template contains Graphiti enabled switch."""
        template = check_memory_config.generate_env_template()
        assert "WATERCOOLER_GRAPHITI_ENABLED" in template

    def test_contains_embedding_vars(self):
        """Test that template contains embedding variables."""
        template = check_memory_config.generate_env_template()
        assert "EMBEDDING_API_BASE" in template
        assert "EMBEDDING_MODEL" in template

    def test_contains_llm_vars(self):
        """Test that template contains LLM variables."""
        template = check_memory_config.generate_env_template()
        assert "LLM_API_BASE" in template
        assert "LLM_MODEL" in template

    def test_contains_leanrag_vars(self):
        """Test that template contains LeanRAG variables."""
        template = check_memory_config.generate_env_template()
        assert "GLM_BASE_URL" in template
        assert "DEEPSEEK_BASE_URL" in template

    def test_contains_falkordb_vars(self):
        """Test that template contains FalkorDB variables."""
        template = check_memory_config.generate_env_template()
        assert "FALKORDB_HOST" in template
        assert "FALKORDB_PORT" in template


class TestMainFunction:
    """Tests for main() function behavior."""

    def test_generate_env_returns_zero(self, monkeypatch, capsys):
        """Test that --generate-env returns exit code 0."""
        monkeypatch.setattr(sys, "argv", ["check-memory-config.py", "--generate-env"])
        result = check_memory_config.main()
        assert result == 0

        captured = capsys.readouterr()
        assert "WATERCOOLER_MEMORY_DISABLED" in captured.out

    def test_disabled_returns_zero(self, monkeypatch, capsys):
        """Test that disabled memory returns exit code 0."""
        monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "1")
        monkeypatch.setattr(sys, "argv", ["check-memory-config.py"])
        result = check_memory_config.main()
        assert result == 0

        captured = capsys.readouterr()
        assert "Memory backends disabled" in captured.out

    def test_disabled_accepts_true(self, monkeypatch, capsys):
        """Test that disabled accepts 'true' value."""
        monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "true")
        monkeypatch.setattr(sys, "argv", ["check-memory-config.py"])
        result = check_memory_config.main()
        assert result == 0

        captured = capsys.readouterr()
        assert "Memory backends disabled" in captured.out

    def test_disabled_accepts_yes(self, monkeypatch, capsys):
        """Test that disabled accepts 'yes' value."""
        monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "YES")
        monkeypatch.setattr(sys, "argv", ["check-memory-config.py"])
        result = check_memory_config.main()
        assert result == 0

    def test_invalid_port_causes_error(self, monkeypatch, capsys):
        """Test that invalid FALKORDB_PORT causes error."""
        monkeypatch.delenv("WATERCOOLER_MEMORY_DISABLED", raising=False)
        monkeypatch.setenv("FALKORDB_PORT", "not-a-number")
        monkeypatch.setattr(sys, "argv", ["check-memory-config.py", "--skip-health-check"])
        result = check_memory_config.main()

        captured = capsys.readouterr()
        assert "FALKORDB_PORT must be a number" in captured.out

    def test_strict_mode_returns_nonzero_on_warnings(self, monkeypatch, capsys):
        """Test that --strict returns non-zero on warnings."""
        monkeypatch.delenv("WATERCOOLER_MEMORY_DISABLED", raising=False)
        # Clear all memory config to trigger warnings
        for var in ["EMBEDDING_API_BASE", "LLM_API_BASE", "GLM_BASE_URL", "DEEPSEEK_BASE_URL"]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(sys, "argv", ["check-memory-config.py", "--strict", "--skip-health-check"])
        result = check_memory_config.main()
        assert result == 1

    def test_skip_health_check_skips_server_pings(self, monkeypatch, capsys):
        """Test that --skip-health-check skips server health checks."""
        monkeypatch.delenv("WATERCOOLER_MEMORY_DISABLED", raising=False)
        monkeypatch.setenv("EMBEDDING_API_BASE", "http://localhost:8080/v1")
        monkeypatch.setattr(sys, "argv", ["check-memory-config.py", "--skip-health-check"])

        # Should not attempt to connect
        result = check_memory_config.main()

        captured = capsys.readouterr()
        assert "health check skipped" in captured.out


class TestLoadBackendConfigs:
    """Tests for load_backend_configs function."""

    def test_loads_graphiti_config(self, monkeypatch):
        """Test that Graphiti config is loaded from env vars."""
        monkeypatch.setenv("EMBEDDING_API_BASE", "http://embed:8080/v1")
        monkeypatch.setenv("LLM_API_BASE", "http://llm:8000/v1")

        backends = check_memory_config.load_backend_configs()

        assert "graphiti" in backends
        assert backends["graphiti"].embedding.url == "http://embed:8080/v1"
        assert backends["graphiti"].llm.url == "http://llm:8000/v1"

    def test_loads_leanrag_config(self, monkeypatch):
        """Test that LeanRAG config is loaded from env vars."""
        monkeypatch.setenv("GLM_BASE_URL", "http://glm:8080/v1")
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://deepseek:8000/v1")

        backends = check_memory_config.load_backend_configs()

        assert "leanrag" in backends
        assert backends["leanrag"].embedding.url == "http://glm:8080/v1"
        assert backends["leanrag"].llm.url == "http://deepseek:8000/v1"

    def test_memorygraph_uses_graphiti_vars(self, monkeypatch):
        """Test that MemoryGraph uses same vars as Graphiti."""
        monkeypatch.setenv("EMBEDDING_API_BASE", "http://embed:8080/v1")
        monkeypatch.setenv("LLM_API_BASE", "http://llm:8000/v1")

        backends = check_memory_config.load_backend_configs()

        assert "memorygraph" in backends
        assert backends["memorygraph"].embedding.url == "http://embed:8080/v1"
        assert backends["memorygraph"].llm.url == "http://llm:8000/v1"
