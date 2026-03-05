"""Testing utilities for configuration.

Provides clean interfaces for injecting test configuration
without environment variable pollution.

Includes both context managers (for flexible use) and
pytest fixtures (for common test scenarios).

Usage:
    # Context managers
    from watercooler.testing import temp_config, mock_env_vars

    with temp_config(threads_dir="/tmp/test"):
        # Test code with overridden config
        pass

    with mock_env_vars(WATERCOOLER_LOG_LEVEL="DEBUG"):
        # Test code with env vars set
        pass

    # Pytest fixtures
    def test_something(clean_config, temp_threads_dir):
        # clean_config provides reset config
        # temp_threads_dir provides temporary directory
        pass
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False
    # Define dummy decorator for when pytest isn't available
    class pytest:  # type: ignore
        @staticmethod
        def fixture(func):
            return func


@contextmanager
def temp_config(
    threads_dir: Optional[Path] = None,
    env_overrides: Optional[Dict[str, str]] = None,
    config_dict: Optional[Dict[str, Any]] = None
):
    """Temporarily override configuration for testing.

    Saves current config state, applies overrides, then restores
    original state on exit. Useful for test isolation.

    Args:
        threads_dir: Override threads directory
        env_overrides: Dictionary of env vars to set
        config_dict: Dictionary to construct WatercoolerConfig from

    Yields:
        config: The facade config instance with overrides applied

    Example:
        with temp_config(
            threads_dir=Path("/tmp/test-threads"),
            env_overrides={"WATERCOOLER_LOG_LEVEL": "DEBUG"}
        ):
            assert config.paths.threads_dir == Path("/tmp/test-threads")
            assert config.env.get("WATERCOOLER_LOG_LEVEL") == "DEBUG"
    """
    from .config_facade import config, PathConfig

    # Save state
    old_paths = config._paths
    old_full = config._full_config
    old_creds = config._credentials
    old_env: Dict[str, Optional[str]] = {}

    try:
        # Apply environment overrides
        if env_overrides:
            for key, value in env_overrides.items():
                old_env[key] = os.environ.get(key)
                os.environ[key] = value

        # Apply path overrides
        if threads_dir:
            # Get current templates_dir or resolve it
            if config._paths:
                templates_dir = config._paths.templates_dir
            else:
                from .path_resolver import resolve_templates_dir
                templates_dir = resolve_templates_dir()

            config._paths = PathConfig(
                threads_dir=threads_dir,
                templates_dir=templates_dir
            )

        # Apply config dict override
        if config_dict:
            from .config_schema import WatercoolerConfig
            config._full_config = WatercoolerConfig.model_validate(config_dict)

        yield config

    finally:
        # Restore state
        config._paths = old_paths
        config._full_config = old_full
        config._credentials = old_creds

        # Restore environment variables
        for key, old_value in old_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


@contextmanager
def mock_env_vars(**env_vars):
    """Temporarily set environment variables for testing.

    Saves current env vars, sets new values, then restores
    originals on exit. Setting value to None deletes the var.

    Args:
        **env_vars: Environment variables to set (var=value pairs)

    Yields:
        None

    Example:
        with mock_env_vars(
            WATERCOOLER_LOG_LEVEL="DEBUG",
            WATERCOOLER_AUTO_PROVISION="0"
        ):
            assert os.getenv("WATERCOOLER_LOG_LEVEL") == "DEBUG"
            assert config.env.get_bool("WATERCOOLER_AUTO_PROVISION") is False
    """
    old_env: Dict[str, Optional[str]] = {}

    try:
        for key, value in env_vars.items():
            old_env[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)

        yield

    finally:
        for key, old_value in old_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


# Pytest fixtures (only defined if pytest is available)

@pytest.fixture
def clean_config():
    """Reset config to clean state.

    Ensures each test starts with fresh, uncached configuration.
    Automatically resets after test completes.

    Yields:
        config: The facade config instance (reset)

    Example:
        def test_something(clean_config):
            assert clean_config.paths.threads_dir is not None
            # Config is automatically reset after test
    """
    from .config_facade import config
    config.reset()
    yield config
    config.reset()


@pytest.fixture
def temp_threads_dir(tmp_path):
    """Provide temporary threads directory.

    Creates a temporary threads directory and configures
    the config system to use it. Automatically cleans up.

    Args:
        tmp_path: pytest's tmp_path fixture

    Yields:
        Path: Temporary threads directory

    Example:
        def test_something(temp_threads_dir):
            # temp_threads_dir is Path to tmp/threads
            assert temp_threads_dir.exists()
            assert config.paths.threads_dir == temp_threads_dir
    """
    threads = tmp_path / "threads"
    threads.mkdir()
    with temp_config(threads_dir=threads):
        yield threads


@pytest.fixture
def mock_watercooler_env():
    """Context manager for mocking WATERCOOLER_* env vars.

    Provides the mock_env_vars context manager as a fixture
    for convenient use in tests.

    Yields:
        Callable: mock_env_vars context manager

    Example:
        def test_something(mock_watercooler_env):
            with mock_watercooler_env(WATERCOOLER_LOG_LEVEL="DEBUG"):
                assert config.env.get("WATERCOOLER_LOG_LEVEL") == "DEBUG"
    """
    return mock_env_vars


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Provide completely isolated config for testing.

    Creates temporary directories for threads, templates,
    and config files. Redirects HOME to prevent user config
    from affecting tests. Useful for integration tests.

    This fixture provides comprehensive isolation:
    - Creates .watercooler dir to stop upward config search
    - Redirects HOME to temp dir (no user config loaded)
    - Clears XDG_CONFIG_HOME
    - Sets WATERCOOLER_DIR and WATERCOOLER_TEMPLATES
    - Resets config cache before and after

    Args:
        tmp_path: pytest's tmp_path fixture
        monkeypatch: pytest's monkeypatch fixture

    Yields:
        dict: Paths for threads_dir, templates_dir, config_dir, tmp_path

    Example:
        def test_integration(isolated_config):
            threads_dir = isolated_config["threads_dir"]
            # Work with completely isolated environment
    """
    from .config_facade import config

    # Create isolated directories
    threads_dir = tmp_path / "threads"
    templates_dir = tmp_path / "templates"
    config_dir = tmp_path / ".watercooler"

    threads_dir.mkdir()
    templates_dir.mkdir()
    config_dir.mkdir()

    # Redirect HOME to temp dir so no user config is loaded
    monkeypatch.setenv("HOME", str(tmp_path))
    # Clear XDG dirs that might be checked
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    # Set up environment
    with mock_env_vars(
        WATERCOOLER_DIR=str(threads_dir),
        WATERCOOLER_TEMPLATES=str(templates_dir)
    ):
        with temp_config(threads_dir=threads_dir):
            config.reset()
            yield {
                "threads_dir": threads_dir,
                "templates_dir": templates_dir,
                "config_dir": config_dir,
                "tmp_path": tmp_path
            }
            config.reset()
