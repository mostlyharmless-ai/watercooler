"""Tests for config_loader module."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest

from watercooler.config_loader import (
    ConfigError,
    _deep_merge,
    _env_to_config_key,
    _get_project_config_dir,
    _get_user_config_dir,
    clear_config_cache,
    ensure_config_dir,
    get_config,
    get_config_paths,
    load_config,
)
from watercooler.config_schema import WatercoolerConfig


class TestDeepMerge:
    """Tests for _deep_merge function."""

    def test_simple_merge(self):
        """Simple key-value merge."""
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        """Nested dict merge."""
        base = {"outer": {"a": 1, "b": 2}}
        override = {"outer": {"b": 3, "c": 4}}
        result = _deep_merge(base, override)
        assert result == {"outer": {"a": 1, "b": 3, "c": 4}}

    def test_list_replacement(self):
        """Lists are replaced, not merged."""
        base = {"items": [1, 2, 3]}
        override = {"items": [4, 5]}
        result = _deep_merge(base, override)
        assert result == {"items": [4, 5]}

    def test_base_unchanged(self):
        """Original base dict should not be modified."""
        base = {"a": 1}
        override = {"a": 2}
        _deep_merge(base, override)
        assert base == {"a": 1}


class TestEnvToConfigKey:
    """Tests for _env_to_config_key function."""

    def test_common_pattern(self):
        """Common section env vars."""
        path, key = _env_to_config_key("WATERCOOLER_TEMPLATES")
        assert path == ["common"]
        assert key == "templates_dir"

    def test_mcp_core(self):
        """MCP core env vars."""
        path, key = _env_to_config_key("WATERCOOLER_AGENT")
        assert path == ["mcp"]
        assert key == "default_agent"

    def test_nested_section(self):
        """Nested section env vars."""
        path, key = _env_to_config_key("WATERCOOLER_GIT_SSH_KEY")
        assert path == ["mcp", "git"]
        assert key == "ssh_key"

    def test_unknown_var(self):
        """Unknown env var returns empty path."""
        path, key = _env_to_config_key("UNKNOWN_VAR")
        assert path == []
        assert key == "UNKNOWN_VAR"

    def test_graphiti_path_mapping(self):
        """WATERCOOLER_GRAPHITI_PATH maps to memory.graphiti.path."""
        path, key = _env_to_config_key("WATERCOOLER_GRAPHITI_PATH")
        assert path == ["memory", "graphiti"]
        assert key == "path"

    def test_falkordb_username_mapping(self):
        """FALKORDB_USERNAME maps to memory.database.username."""
        path, key = _env_to_config_key("FALKORDB_USERNAME")
        assert path == ["memory", "database"]
        assert key == "username"

    def test_falkordb_password_mapping(self):
        """FALKORDB_PASSWORD maps to memory.database.password."""
        path, key = _env_to_config_key("FALKORDB_PASSWORD")
        assert path == ["memory", "database"]
        assert key == "password"

    def test_falkordb_socket_timeout_mapping(self):
        """FALKORDB_SOCKET_TIMEOUT maps to memory.database.socket_timeout."""
        path, key = _env_to_config_key("FALKORDB_SOCKET_TIMEOUT")
        assert path == ["memory", "database"]
        assert key == "socket_timeout"


class TestGraphitiPathEnvOverlay:
    """Tests that WATERCOOLER_GRAPHITI_PATH env var flows through to config."""

    def test_graphiti_path_env_var_sets_config(self, tmp_path, monkeypatch):
        """Setting WATERCOOLER_GRAPHITI_PATH makes config.full().memory.graphiti.path match."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_PATH", "/opt/graphiti-dev")

        import importlib
        import watercooler.config_loader as cl
        importlib.reload(cl)

        config = cl.load_config()
        assert config.memory.graphiti.path == "/opt/graphiti-dev"


class TestConfigDirectories:
    """Tests for config directory functions."""

    def test_user_config_dir(self):
        """User config dir is in home."""
        user_dir = _get_user_config_dir()
        assert user_dir == Path.home() / ".watercooler"

    def test_project_config_dir_found(self, tmp_path):
        """Project config dir found when exists."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        config_dir = project_dir / ".watercooler"
        config_dir.mkdir()

        result = _get_project_config_dir(project_dir)
        assert result == config_dir

    def test_project_config_dir_searches_upward(self, tmp_path):
        """Project config dir searches parent directories."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        config_dir = project_dir / ".watercooler"
        config_dir.mkdir()

        subdir = project_dir / "src" / "deep"
        subdir.mkdir(parents=True)

        result = _get_project_config_dir(subdir)
        assert result == config_dir

    def test_project_config_dir_not_found(self, tmp_path):
        """Returns None when no config dir found."""
        result = _get_project_config_dir(tmp_path)
        assert result is None

    def test_ensure_config_dir_user(self, tmp_path, monkeypatch):
        """Ensure user config dir creates directory."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        # Force new home lookup
        import importlib
        import watercooler.config_loader as cl
        importlib.reload(cl)

        result = cl.ensure_config_dir(user=True)
        assert result.exists()
        assert result.is_dir()

    def test_ensure_config_dir_project(self, tmp_path):
        """Ensure project config dir creates directory."""
        result = ensure_config_dir(user=False, project_path=tmp_path)
        assert result == tmp_path / ".watercooler"
        assert result.exists()


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_empty_config(self, tmp_path, monkeypatch):
        """Load config with no files returns defaults."""
        # Point to empty home and project
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        # Clear relevant env vars
        for var in ["WATERCOOLER_AGENT", "WATERCOOLER_LOG_LEVEL"]:
            monkeypatch.delenv(var, raising=False)

        config = load_config(project_path=tmp_path, skip_env=True)
        assert isinstance(config, WatercoolerConfig)
        assert config.version == 1

    def test_load_user_config(self, tmp_path, monkeypatch):
        """Load user config from TOML file."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)

        config_file = config_dir / "config.toml"
        config_file.write_text("""
version = 1

[mcp]
default_agent = "TestAgent"
""")

        monkeypatch.setenv("HOME", str(fake_home))

        # Force reload
        import importlib
        import watercooler.config_loader as cl
        importlib.reload(cl)

        config = cl.load_config(skip_env=True)
        assert config.mcp.default_agent == "TestAgent"

    def test_project_config_overrides_user(self, tmp_path, monkeypatch):
        """Project config overrides user config."""
        # User config
        fake_home = tmp_path / "home"
        user_config_dir = fake_home / ".watercooler"
        user_config_dir.mkdir(parents=True)
        (user_config_dir / "config.toml").write_text("""
[mcp]
default_agent = "UserAgent"
agent_tag = "user"
""")

        # Project config
        project_dir = tmp_path / "project"
        project_config_dir = project_dir / ".watercooler"
        project_config_dir.mkdir(parents=True)
        (project_config_dir / "config.toml").write_text("""
[mcp]
default_agent = "ProjectAgent"
""")

        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.config_loader as cl
        importlib.reload(cl)

        config = cl.load_config(project_path=project_dir, skip_env=True)
        # Project overrides user
        assert config.mcp.default_agent == "ProjectAgent"
        # User config preserved where not overridden
        assert config.mcp.agent_tag == "user"

    def test_env_overlay(self, tmp_path, monkeypatch):
        """Environment variables override config files."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text("""
[mcp]
default_agent = "FileAgent"
""")

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("WATERCOOLER_AGENT", "EnvAgent")

        import importlib
        import watercooler.config_loader as cl
        importlib.reload(cl)

        config = cl.load_config()
        assert config.mcp.default_agent == "EnvAgent"

    def test_invalid_toml_raises(self, tmp_path, monkeypatch):
        """Invalid project TOML raises ConfigError."""
        project_dir = tmp_path / "project"
        config_dir = project_dir / ".watercooler"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text("invalid toml [[[")

        # Use Exception to avoid class identity issues from module reloads in other tests
        with pytest.raises(Exception) as exc_info:
            load_config(project_path=project_dir, skip_env=True)

        assert "ConfigError" in type(exc_info.value).__name__
        assert "Invalid" in str(exc_info.value)


class TestGetConfig:
    """Tests for cached get_config function."""

    def test_caches_config(self, tmp_path, monkeypatch):
        """Config is cached on subsequent calls."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        clear_config_cache()

        config1 = get_config(project_path=tmp_path)
        config2 = get_config(project_path=tmp_path)
        assert config1 is config2

    def test_force_reload(self, tmp_path, monkeypatch):
        """force_reload=True reloads from disk."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        clear_config_cache()

        config1 = get_config(project_path=tmp_path)
        config2 = get_config(project_path=tmp_path, force_reload=True)
        # Different objects but equal values
        assert config1 is not config2
        assert config1.version == config2.version

    def test_different_project_paths(self, tmp_path, monkeypatch):
        """Different project paths get different configs."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        project1 = tmp_path / "project1"
        project1.mkdir()
        project2 = tmp_path / "project2"
        project2.mkdir()

        clear_config_cache()

        config1 = get_config(project_path=project1)
        config2 = get_config(project_path=project2)
        # Cache is invalidated for different paths
        assert config1 is not config2

    def test_empty_path_treated_as_none(self, tmp_path, monkeypatch):
        """Empty string path is treated as None."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        clear_config_cache()

        config1 = get_config(project_path=None)
        # Path("") should be treated same as None
        config2 = get_config(project_path=Path(""))
        # Both should work without errors
        assert isinstance(config1, WatercoolerConfig)
        assert isinstance(config2, WatercoolerConfig)


class TestGetConfigReentrancy:
    """Regression tests for issue #810.

    get_config() guards its load with a non-reentrant threading.Lock. A
    same-thread re-entry while a load is in flight previously self-deadlocked
    forever; it must now fail loud instead. The historical trigger was an
    import-time config access reached via the ``mcp.capability_routes``
    validator importing ``watercooler_mcp.capabilities`` (which, before #818,
    eagerly imported ``watercooler_mcp.server`` and its import-time config load).
    """

    def test_reentrant_load_raises_instead_of_deadlocking(self, tmp_path, monkeypatch):
        """A same-thread re-entry during load raises RuntimeError, never hangs."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        clear_config_cache()

        import watercooler.config_loader as cl

        real_load = cl.load_config
        captured: Dict[str, Any] = {"exc": None}

        def reentrant_load(*args, **kwargs):
            # Simulate an import-time get_config() reached during validation,
            # on the same thread that already holds _config_lock.
            try:
                cl.get_config(project_path=tmp_path)
            except RuntimeError as exc:
                captured["exc"] = exc
            return real_load(*args, **kwargs)

        monkeypatch.setattr(cl, "load_config", reentrant_load)

        # The outer call must complete (no deadlock); the inner re-entry raised.
        config = cl.get_config(project_path=tmp_path, force_reload=True)
        assert isinstance(config, WatercoolerConfig)
        assert isinstance(captured["exc"], RuntimeError)
        assert "#810" in str(captured["exc"])

    def test_loading_flag_reset_after_load(self, tmp_path, monkeypatch):
        """The per-thread loading flag clears so later calls are not mis-flagged."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        clear_config_cache()

        get_config(project_path=tmp_path, force_reload=True)
        # A subsequent (sequential, non-reentrant) call must not raise.
        config = get_config(project_path=tmp_path, force_reload=True)
        assert isinstance(config, WatercoolerConfig)

    def test_capability_routes_validation_does_not_import_server(self, tmp_path):
        """Validating mcp.capability_routes must not import watercooler_mcp.server.

        Run in a clean subprocess: the in-process test runner imports ``server``
        during collection, which is precisely what masked the #810 deadlock in
        the full suite. A hang here surfaces as a subprocess timeout (failure).
        """
        import subprocess
        import sys
        import textwrap

        clean_home = tmp_path / "home"
        clean_home.mkdir()
        wc = tmp_path / ".watercooler"
        wc.mkdir()
        (wc / "config.toml").write_text(
            '[mcp.capability_routes]\nmemory_ingest = "local"\n'
        )

        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path

            assert "watercooler_mcp.server" not in sys.modules, "precondition: server unimported"

            from watercooler.config_loader import get_config

            cfg = get_config(project_path=Path(r"{tmp_path}"), force_reload=True)
            assert cfg.mcp.capability_routes == {{"memory_ingest": "local"}}, cfg.mcp.capability_routes
            # The validator must have run (imported capabilities) ...
            assert "watercooler_mcp.capabilities" in sys.modules, "validator did not run"
            # ... but must NOT have dragged in the server (the #810 deadlock chain).
            assert "watercooler_mcp.server" not in sys.modules, "server imported during validation (#810)"
            print("OK-810")
            """
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            # Inherit the parent env (editable install / PYTHONPATH resolution)
            # but redirect HOME so the real ~/.watercooler/config.toml is ignored.
            env={**os.environ, "HOME": str(clean_home)},
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "OK-810" in result.stdout


class TestGetConfigPaths:
    """Tests for get_config_paths function."""

    def test_returns_all_paths(self, tmp_path, monkeypatch):
        """Returns dict with all expected keys."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.config_loader as cl
        importlib.reload(cl)

        paths = cl.get_config_paths(project_path=tmp_path)

        assert "user_config" in paths
        assert "project_config" in paths
        assert "user_credentials" in paths
        assert "project_credentials" in paths


class TestPhantomEnvVarRegistration:
  """Tests that previously-phantom env vars are registered in ENV_MAPPING.

  These tests validate the source file directly (via source inspection) because
  the worktree may share an editable install with the main project tree. Using
  source-level assertions guarantees we validate the worktree code changes.
  """

  def _get_config_loader_source(self) -> str:
    """Read config_loader.py from the worktree source."""
    from pathlib import Path
    loader_path = Path(__file__).parents[2] / "src" / "watercooler" / "config_loader.py"
    return loader_path.read_text()

  def test_auto_start_services_in_env_mapping(self):
    """WATERCOOLER_AUTO_START_SERVICES is registered in ENV_MAPPING with path mcp.graph."""
    source = self._get_config_loader_source()
    assert '"WATERCOOLER_AUTO_START_SERVICES"' in source, (
      "WATERCOOLER_AUTO_START_SERVICES must be registered in ENV_MAPPING"
    )
    assert '"auto_start_services"' in source, (
      "auto_start_services key must appear in ENV_MAPPING entry"
    )
    # Verify the mcp.graph section path appears near this entry
    assert '["mcp", "graph"], "auto_start_services"' in source, (
      "WATERCOOLER_AUTO_START_SERVICES must map to ['mcp', 'graph'], 'auto_start_services'"
    )

  def test_embedding_divergence_threshold_in_env_mapping(self):
    """WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD is registered in ENV_MAPPING with path mcp.graph."""
    source = self._get_config_loader_source()
    assert '"WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD"' in source, (
      "WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD must be registered in ENV_MAPPING"
    )
    assert '["mcp", "graph"], "embedding_divergence_threshold"' in source, (
      "WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD must map to "
      "['mcp', 'graph'], 'embedding_divergence_threshold'"
    )

  def test_graphiti_enabled_not_in_env_mapping(self):
    """WATERCOOLER_GRAPHITI_ENABLED is intentionally NOT in ENV_MAPPING (runtime-only legacy flag)."""
    from watercooler.config_loader import _env_to_config_key
    section_path, _ = _env_to_config_key("WATERCOOLER_GRAPHITI_ENABLED")
    assert section_path == [], (
      "WATERCOOLER_GRAPHITI_ENABLED must remain a runtime-only flag outside the config system"
    )

  def test_graphiti_enabled_comment_in_sync(self):
    """WATERCOOLER_GRAPHITI_ENABLED occurrences in sync.py are documented as runtime-only."""
    from pathlib import Path

    sync_path = Path(__file__).parents[2] / "src" / "watercooler" / "baseline_graph" / "sync.py"
    source = sync_path.read_text()

    comment = "# Intentional: WATERCOOLER_GRAPHITI_ENABLED is a legacy runtime-only flag (not in config system)"
    occurrences = source.count("WATERCOOLER_GRAPHITI_ENABLED")
    comment_count = source.count(comment)

    # Should have the comment for each os.environ.get usage
    assert comment_count >= 2, (
      f"Expected at least 2 runtime-only comments for WATERCOOLER_GRAPHITI_ENABLED, found {comment_count}"
    )
    assert occurrences > 0
