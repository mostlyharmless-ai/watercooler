"""Tests for watercooler.config_facade module."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from watercooler.config_facade import Config, PathConfig, config


class TestPathConfig:
    """Tests for PathConfig dataclass."""

    def test_path_config_creation(self, tmp_path):
        """Test PathConfig can be created with all fields."""
        threads = tmp_path / "threads"
        templates = tmp_path / "templates"
        code_root = tmp_path / "code"

        path_config = PathConfig(
            threads_dir=threads,
            templates_dir=templates,
            code_root=code_root
        )

        assert path_config.threads_dir == threads
        assert path_config.templates_dir == templates
        assert path_config.code_root == code_root

    def test_path_config_immutable(self, tmp_path):
        """Test PathConfig is immutable (frozen dataclass)."""
        path_config = PathConfig(
            threads_dir=tmp_path / "threads",
            templates_dir=tmp_path / "templates"
        )

        with pytest.raises(AttributeError):
            path_config.threads_dir = tmp_path / "other"  # type: ignore


class TestConfigPaths:
    """Tests for Config.paths property."""

    def test_paths_property_lazy_loading(self):
        """Test paths property is lazy-loaded."""
        test_config = Config()
        assert test_config._paths is None

        # Access paths
        paths = test_config.paths
        assert paths is not None
        assert isinstance(paths, PathConfig)

        # Should be cached
        assert test_config._paths is paths

    def test_paths_returns_path_config(self):
        """Test paths returns PathConfig instance."""
        test_config = Config()
        paths = test_config.paths

        assert isinstance(paths, PathConfig)
        assert isinstance(paths.threads_dir, Path)
        assert isinstance(paths.templates_dir, Path)

    def test_paths_cached_across_calls(self):
        """Test paths is cached across multiple calls."""
        test_config = Config()
        paths1 = test_config.paths
        paths2 = test_config.paths

        assert paths1 is paths2  # Same object

    def test_paths_reset_clears_cache(self):
        """Test reset() clears paths cache."""
        test_config = Config()
        paths1 = test_config.paths
        test_config.reset()
        paths2 = test_config.paths

        assert paths1 is not paths2  # Different objects after reset


class TestConfigEnv:
    """Tests for Config.env environment variable helpers."""

    def test_env_get_basic(self, monkeypatch):
        """Test env.get() basic functionality."""
        monkeypatch.setenv("TEST_VAR", "test_value")
        assert config.env.get("TEST_VAR") == "test_value"

    def test_env_get_default(self):
        """Test env.get() returns default when var not set."""
        result = config.env.get("NONEXISTENT_VAR_12345", "default")
        assert result == "default"

    def test_env_get_bool_true_values(self, monkeypatch):
        """Test env.get_bool() recognizes true values."""
        true_values = ["1", "true", "True", "TRUE", "yes", "YES", "on", "ON"]
        for val in true_values:
            monkeypatch.setenv("TEST_BOOL", val)
            assert config.env.get_bool("TEST_BOOL") is True

    def test_env_get_bool_false_values(self, monkeypatch):
        """Test env.get_bool() treats other values as false."""
        false_values = ["0", "false", "False", "no", "off", "random"]
        for val in false_values:
            monkeypatch.setenv("TEST_BOOL", val)
            assert config.env.get_bool("TEST_BOOL") is False

    def test_env_get_bool_default(self):
        """Test env.get_bool() returns default when var not set."""
        # Not set, default True
        assert config.env.get_bool("NONEXISTENT_BOOL_12345", default=True) is True
        # Not set, default False
        assert config.env.get_bool("NONEXISTENT_BOOL_12345", default=False) is False

    def test_env_get_int_valid(self, monkeypatch):
        """Test env.get_int() parses valid integers."""
        monkeypatch.setenv("TEST_INT", "42")
        assert config.env.get_int("TEST_INT") == 42

        monkeypatch.setenv("TEST_INT", "-100")
        assert config.env.get_int("TEST_INT") == -100

    def test_env_get_int_invalid(self, monkeypatch):
        """Test env.get_int() returns default for invalid values."""
        monkeypatch.setenv("TEST_INT", "not_a_number")
        assert config.env.get_int("TEST_INT", default=99) == 99

    def test_env_get_int_default(self):
        """Test env.get_int() returns default when var not set."""
        assert config.env.get_int("NONEXISTENT_INT_12345", default=50) == 50

    def test_env_get_float_valid(self, monkeypatch):
        """Test env.get_float() parses valid floats."""
        monkeypatch.setenv("TEST_FLOAT", "3.14")
        assert config.env.get_float("TEST_FLOAT") == 3.14

        monkeypatch.setenv("TEST_FLOAT", "-2.5")
        assert config.env.get_float("TEST_FLOAT") == -2.5

    def test_env_get_float_invalid(self, monkeypatch):
        """Test env.get_float() returns default for invalid values."""
        monkeypatch.setenv("TEST_FLOAT", "not_a_float")
        assert config.env.get_float("TEST_FLOAT", default=1.5) == 1.5

    def test_env_get_path_with_expansion(self, monkeypatch, tmp_path):
        """Test env.get_path() expands paths."""
        monkeypatch.setenv("TEST_PATH", "~/test")
        result = config.env.get_path("TEST_PATH")

        assert result is not None
        assert "~" not in str(result)
        assert str(Path.home()) in str(result)

    def test_env_get_path_with_env_vars(self, monkeypatch, tmp_path):
        """Test env.get_path() expands environment variables."""
        monkeypatch.setenv("BASE_DIR", str(tmp_path))
        monkeypatch.setenv("TEST_PATH", "$BASE_DIR/subdir")

        result = config.env.get_path("TEST_PATH")

        assert result is not None
        assert str(tmp_path) in str(result)
        assert "BASE_DIR" not in str(result)

    def test_env_get_path_default(self):
        """Test env.get_path() returns default when var not set."""
        default = Path("/default/path")
        result = config.env.get_path("NONEXISTENT_PATH_12345", default=default)
        assert result == default


class TestConfigHelperMethods:
    """Tests for Config helper methods."""

    def test_get_threads_dir_cli_value(self, tmp_path):
        """Test get_threads_dir() with CLI value."""
        test_config = Config()
        result = test_config.get_threads_dir(cli_value=str(tmp_path))
        assert result == tmp_path.resolve()

    def test_get_threads_dir_no_args(self):
        """Test get_threads_dir() with no arguments."""
        test_config = Config()
        result = test_config.get_threads_dir()
        assert isinstance(result, Path)
        assert result.is_absolute()

    def test_get_github_token_from_env(self, monkeypatch):
        """Test get_github_token() from environment variable."""
        test_config = Config()
        monkeypatch.setenv("GITHUB_TOKEN", "test_token_123")

        token = test_config.get_github_token()
        assert token == "test_token_123"

    def test_get_github_token_gh_token_alias(self, monkeypatch):
        """Test get_github_token() recognizes GH_TOKEN alias."""
        test_config = Config()
        monkeypatch.setenv("GH_TOKEN", "gh_token_456")

        token = test_config.get_github_token()
        assert token == "gh_token_456"

    def test_get_github_token_github_token_precedence(self, monkeypatch):
        """Test GITHUB_TOKEN takes precedence over GH_TOKEN."""
        test_config = Config()
        monkeypatch.setenv("GITHUB_TOKEN", "primary")
        monkeypatch.setenv("GH_TOKEN", "secondary")

        token = test_config.get_github_token()
        assert token == "primary"


class TestConfigReset:
    """Tests for Config.reset() method."""

    def test_reset_clears_all_caches(self):
        """Test reset() clears all cached state."""
        test_config = Config()

        # Access all lazy properties to populate cache
        _ = test_config.paths

        # Verify cached
        assert test_config._paths is not None

        # Reset
        test_config.reset()

        # Verify cleared
        assert test_config._paths is None
        assert test_config._full_config is None
        assert test_config._credentials is None

    def test_reset_allows_reloading(self):
        """Test reset() allows fresh loading."""
        test_config = Config()

        paths1 = test_config.paths
        test_config.reset()
        paths2 = test_config.paths

        # Should get new instances after reset
        assert paths1 is not paths2


class TestGlobalConfigSingleton:
    """Tests for global config singleton."""

    def test_global_config_exists(self):
        """Test global config instance is available."""
        from watercooler.config_facade import config

        assert isinstance(config, Config)

    def test_global_config_is_singleton(self):
        """Test global config is the same instance."""
        from watercooler.config_facade import config as config1
        from watercooler.config_facade import config as config2

        assert config1 is config2


class TestConfigIntegration:
    """Integration tests for Config facade."""

    def test_full_workflow(self, monkeypatch, tmp_path):
        """Test complete config workflow."""
        test_config = Config()

        # Set up environment
        monkeypatch.setenv("WATERCOOLER_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("WATERCOOLER_AUTO_PROVISION", "1")

        # Access paths
        paths = test_config.paths
        assert isinstance(paths.threads_dir, Path)
        assert isinstance(paths.templates_dir, Path)

        # Access env vars
        assert test_config.env.get("WATERCOOLER_LOG_LEVEL") == "DEBUG"
        assert test_config.env.get_bool("WATERCOOLER_AUTO_PROVISION") is True

        # Reset
        test_config.reset()
        assert test_config._paths is None


class TestConfigErrorHandling:
    """Tests for config error handling."""

    def test_get_github_token_no_credentials_file(self):
        """Test get_github_token() handles missing credentials gracefully."""
        test_config = Config()
        test_config.reset()

        # Should not raise, should return None
        # (assuming no env var set and no credentials file)
        token = test_config.get_github_token()
        # Could be None or a value from env/file
        assert token is None or isinstance(token, str)
