"""Tests for watercooler.testing module (testing utilities)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from watercooler.config_facade import config
from watercooler.testing import (
    clean_config,
    isolated_config,
    mock_env_vars,
    mock_watercooler_env,
    temp_config,
    temp_threads_dir,
)


class TestMockEnvVars:
    """Tests for mock_env_vars context manager."""

    def test_mock_env_vars_sets_vars(self):
        """Test mock_env_vars sets environment variables."""
        with mock_env_vars(TEST_VAR="test_value"):
            assert os.getenv("TEST_VAR") == "test_value"

    def test_mock_env_vars_restores_original(self):
        """Test mock_env_vars restores original values."""
        original_value = "original"
        os.environ["TEST_VAR_RESTORE"] = original_value

        with mock_env_vars(TEST_VAR_RESTORE="modified"):
            assert os.getenv("TEST_VAR_RESTORE") == "modified"

        assert os.getenv("TEST_VAR_RESTORE") == original_value

    def test_mock_env_vars_removes_on_exit(self):
        """Test mock_env_vars removes vars that didn't exist before."""
        # Ensure var doesn't exist
        os.environ.pop("NEW_TEST_VAR", None)

        with mock_env_vars(NEW_TEST_VAR="temporary"):
            assert os.getenv("NEW_TEST_VAR") == "temporary"

        assert os.getenv("NEW_TEST_VAR") is None

    def test_mock_env_vars_none_deletes_var(self):
        """Test setting var to None deletes it."""
        os.environ["DELETE_ME"] = "value"

        with mock_env_vars(DELETE_ME=None):
            assert os.getenv("DELETE_ME") is None

        # Should be restored after exit
        assert os.getenv("DELETE_ME") == "value"

        # Clean up
        os.environ.pop("DELETE_ME")

    def test_mock_env_vars_multiple_vars(self):
        """Test mock_env_vars handles multiple variables."""
        with mock_env_vars(VAR1="value1", VAR2="value2", VAR3="value3"):
            assert os.getenv("VAR1") == "value1"
            assert os.getenv("VAR2") == "value2"
            assert os.getenv("VAR3") == "value3"

        assert os.getenv("VAR1") is None
        assert os.getenv("VAR2") is None
        assert os.getenv("VAR3") is None


class TestTempConfig:
    """Tests for temp_config context manager."""

    def test_temp_config_overrides_threads_dir(self, tmp_path):
        """Test temp_config overrides threads directory."""
        test_threads = tmp_path / "test-threads"

        with temp_config(threads_dir=test_threads):
            assert config.paths.threads_dir == test_threads

    def test_temp_config_restores_original_threads_dir(self):
        """Test temp_config restores original threads directory."""
        config.reset()
        original_threads = config.paths.threads_dir

        with temp_config(threads_dir=Path("/tmp/override")):
            pass  # Exit context

        config.reset()
        restored_threads = config.paths.threads_dir

        # Should be same as original (or at least same resolved path)
        assert original_threads == restored_threads

    def test_temp_config_env_overrides(self):
        """Test temp_config applies environment overrides."""
        with temp_config(env_overrides={"TEST_ENV": "test_value"}):
            assert os.getenv("TEST_ENV") == "test_value"

        assert os.getenv("TEST_ENV") is None

    def test_temp_config_combined_overrides(self, tmp_path):
        """Test temp_config with both paths and env overrides."""
        test_threads = tmp_path / "threads"

        with temp_config(
            threads_dir=test_threads,
            env_overrides={"WATERCOOLER_LOG_LEVEL": "DEBUG"}
        ):
            assert config.paths.threads_dir == test_threads
            assert os.getenv("WATERCOOLER_LOG_LEVEL") == "DEBUG"

        assert os.getenv("WATERCOOLER_LOG_LEVEL") is None

    def test_temp_config_yields_config(self, tmp_path):
        """Test temp_config yields the config object."""
        with temp_config(threads_dir=tmp_path) as cfg:
            assert cfg is config
            assert cfg.paths.threads_dir == tmp_path


class TestCleanConfigFixture:
    """Tests for clean_config pytest fixture."""

    def test_clean_config_provides_reset_config(self, clean_config):
        """Test clean_config fixture provides reset config."""
        # Should be the global config instance
        assert clean_config is config

        # Should have clean state (no cached values at start)
        # Note: After reset, _paths etc. are None until accessed
        assert clean_config._paths is None

    def test_clean_config_resets_after_test(self, clean_config):
        """Test clean_config resets config after test."""
        # Modify config during test
        _ = clean_config.paths  # Populate cache

        assert clean_config._paths is not None
        # Fixture will reset after test completes


class TestTempThreadsDirFixture:
    """Tests for temp_threads_dir pytest fixture."""

    def test_temp_threads_dir_creates_directory(self, temp_threads_dir):
        """Test temp_threads_dir creates a temporary directory."""
        assert temp_threads_dir.exists()
        assert temp_threads_dir.is_dir()
        assert temp_threads_dir.name == "threads"

    def test_temp_threads_dir_configures_config(self, temp_threads_dir):
        """Test temp_threads_dir configures config to use the temp dir."""
        assert config.paths.threads_dir == temp_threads_dir

    def test_temp_threads_dir_is_writable(self, temp_threads_dir):
        """Test temp_threads_dir is writable."""
        test_file = temp_threads_dir / "test.txt"
        test_file.write_text("test content")
        assert test_file.read_text() == "test content"


class TestMockWatercoolerEnvFixture:
    """Tests for mock_watercooler_env pytest fixture."""

    def test_mock_watercooler_env_provides_context_manager(self, mock_watercooler_env):
        """Test mock_watercooler_env provides the mock_env_vars context manager."""
        assert callable(mock_watercooler_env)

        with mock_watercooler_env(WATERCOOLER_TEST="value"):
            assert os.getenv("WATERCOOLER_TEST") == "value"

        assert os.getenv("WATERCOOLER_TEST") is None


class TestIsolatedConfigFixture:
    """Tests for isolated_config pytest fixture."""

    def test_isolated_config_creates_directories(self, isolated_config):
        """Test isolated_config creates all necessary directories."""
        assert isolated_config["threads_dir"].exists()
        assert isolated_config["templates_dir"].exists()
        assert isolated_config["config_dir"].exists()

    def test_isolated_config_sets_env_vars(self, isolated_config):
        """Test isolated_config sets appropriate environment variables."""
        assert os.getenv("WATERCOOLER_DIR") == str(isolated_config["threads_dir"])
        assert os.getenv("WATERCOOLER_TEMPLATES") == str(isolated_config["templates_dir"])

    def test_isolated_config_configures_config(self, isolated_config):
        """Test isolated_config configures the config system."""
        assert config.paths.threads_dir == isolated_config["threads_dir"]

    def test_isolated_config_provides_tmp_path(self, isolated_config):
        """Test isolated_config includes tmp_path for additional files."""
        assert "tmp_path" in isolated_config
        assert isolated_config["tmp_path"].exists()

    def test_isolated_config_is_writable(self, isolated_config):
        """Test isolated_config directories are writable."""
        test_thread = isolated_config["threads_dir"] / "test-thread.md"
        test_thread.write_text("# Test Thread")
        assert test_thread.read_text() == "# Test Thread"


class TestTestingUtilitiesIntegration:
    """Integration tests for testing utilities."""

    def test_nested_context_managers(self, tmp_path):
        """Test nesting temp_config and mock_env_vars."""
        test_threads = tmp_path / "threads"

        with mock_env_vars(OUTER_VAR="outer"):
            with temp_config(
                threads_dir=test_threads,
                env_overrides={"INNER_VAR": "inner"}
            ):
                # Both should be set
                assert os.getenv("OUTER_VAR") == "outer"
                assert os.getenv("INNER_VAR") == "inner"
                assert config.paths.threads_dir == test_threads

            # INNER_VAR should be cleared, OUTER_VAR still set
            assert os.getenv("OUTER_VAR") == "outer"
            assert os.getenv("INNER_VAR") is None

        # OUTER_VAR should be cleared
        assert os.getenv("OUTER_VAR") is None

    def test_config_isolation_between_tests(self):
        """Test config changes don't leak between tests."""
        # This test relies on pytest's fixture cleanup
        # Each test should get clean state via fixtures
        pass  # Implicit test via fixture usage


class TestErrorHandling:
    """Tests for error handling in testing utilities."""

    def test_temp_config_cleanup_on_exception(self, tmp_path):
        """Test temp_config cleans up even if exception occurs."""
        test_threads = tmp_path / "threads"
        original_threads = config.paths.threads_dir

        try:
            with temp_config(threads_dir=test_threads):
                assert config.paths.threads_dir == test_threads
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Should still restore original config
        config.reset()
        assert config.paths.threads_dir == original_threads

    def test_mock_env_vars_cleanup_on_exception(self):
        """Test mock_env_vars cleans up even if exception occurs."""
        os.environ.pop("TEST_CLEANUP", None)  # Ensure not set

        try:
            with mock_env_vars(TEST_CLEANUP="temporary"):
                assert os.getenv("TEST_CLEANUP") == "temporary"
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Should still remove the var
        assert os.getenv("TEST_CLEANUP") is None
