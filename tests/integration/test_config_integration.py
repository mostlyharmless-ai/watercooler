"""Integration tests for config system affecting MCP server behavior.

Tests that configuration settings actually affect runtime behavior,
verifying the connection between config files and MCP server operation.
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch


class TestMcpConfigIntegration:
    """Test that MCP server respects config settings."""

    def test_transport_config_from_file(self, tmp_path, monkeypatch):
        """Test that MCP transport settings come from config file."""
        # Create a config file with custom transport settings
        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("""
[mcp]
transport = "http"
host = "0.0.0.0"
port = 8080
""")

        # Clear any cached config
        from watercooler.config_loader import clear_config_cache
        clear_config_cache()

        # Patch home directory to use our temp config
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Clear MCP config cache
        import watercooler_mcp.config as mcp_config
        mcp_config._loaded_config = None

        # Clear env vars that would override
        monkeypatch.delenv("WATERCOOLER_MCP_TRANSPORT", raising=False)
        monkeypatch.delenv("WATERCOOLER_MCP_HOST", raising=False)
        monkeypatch.delenv("WATERCOOLER_MCP_PORT", raising=False)

        # Get transport config
        result = mcp_config.get_mcp_transport_config()

        assert result["transport"] == "http"
        assert result["host"] == "0.0.0.0"
        assert result["port"] == 8080

    def test_transport_config_env_overrides_file(self, tmp_path, monkeypatch):
        """Test that environment variables override config file."""
        # Create a config file
        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("""
[mcp]
transport = "stdio"
port = 3000
""")

        # Clear caches
        from watercooler.config_loader import clear_config_cache
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        import watercooler_mcp.config as mcp_config
        mcp_config._loaded_config = None

        # Set env var to override
        monkeypatch.setenv("WATERCOOLER_MCP_PORT", "9999")

        result = mcp_config.get_mcp_transport_config()

        # Env var should override
        assert result["port"] == 9999
        # File value should be used where no env var
        assert result["transport"] == "stdio"

    def test_sync_config_from_file(self, tmp_path, monkeypatch):
        """Test that sync settings come from config file."""
        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("""
[mcp.sync]
async = false
batch_window = 10.0
max_retries = 3
interval = 60.0
""")

        from watercooler.config_loader import clear_config_cache
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        import watercooler_mcp.config as mcp_config
        mcp_config._loaded_config = None

        # Clear env vars
        monkeypatch.delenv("WATERCOOLER_ASYNC_SYNC", raising=False)
        monkeypatch.delenv("WATERCOOLER_BATCH_WINDOW", raising=False)
        monkeypatch.delenv("WATERCOOLER_SYNC_MAX_RETRIES", raising=False)
        monkeypatch.delenv("WATERCOOLER_SYNC_INTERVAL", raising=False)

        result = mcp_config.get_sync_config()

        assert result["async_sync"] is False
        assert result["batch_window"] == 10.0
        assert result["max_retries"] == 3
        assert result["interval"] == 60.0

    def test_logging_config_from_file(self, tmp_path, monkeypatch):
        """Test that logging settings come from config file."""
        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("""
[mcp.logging]
level = "DEBUG"
disable_file = true
max_bytes = 5242880
""")

        from watercooler.config_loader import clear_config_cache
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        import watercooler_mcp.config as mcp_config
        mcp_config._loaded_config = None

        # Clear env vars
        monkeypatch.delenv("WATERCOOLER_LOG_LEVEL", raising=False)
        monkeypatch.delenv("WATERCOOLER_LOG_DISABLE_FILE", raising=False)
        monkeypatch.delenv("WATERCOOLER_LOG_MAX_BYTES", raising=False)

        result = mcp_config.get_logging_config()

        assert result["level"] == "DEBUG"
        assert result["disable_file"] is True
        assert result["max_bytes"] == 5242880

    def test_agent_config_from_file(self, tmp_path, monkeypatch):
        """Test that agent settings come from config file."""
        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("""
[mcp.agents.custom-agent]
name = "Custom Agent"
default_spec = "custom-spec"
""")

        from watercooler.config_loader import clear_config_cache
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        import watercooler_mcp.config as mcp_config
        mcp_config._loaded_config = None

        result = mcp_config.get_agent_for_platform("custom-agent")

        assert result["name"] == "Custom Agent"
        assert result["default_spec"] == "custom-spec"

    def test_config_reload(self, tmp_path, monkeypatch):
        """Test that reload_config picks up file changes."""
        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"

        # Initial config
        config_file.write_text("""
[mcp]
default_agent = "Agent1"
""")

        from watercooler.config_loader import clear_config_cache
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        import watercooler_mcp.config as mcp_config
        mcp_config._loaded_config = None

        # Load initial config
        config1 = mcp_config.get_watercooler_config()
        assert config1.mcp.default_agent == "Agent1"

        # Update config file
        config_file.write_text("""
[mcp]
default_agent = "Agent2"
""")
        clear_config_cache()

        # Reload should pick up changes
        config2 = mcp_config.reload_config()
        assert config2.mcp.default_agent == "Agent2"


class TestConfigPrecedence:
    """Test configuration precedence: defaults → user → project → env."""

    def test_project_config_overrides_user(self, tmp_path, monkeypatch):
        """Test that project config overrides user config."""
        # User config
        user_dir = tmp_path / "home" / ".watercooler"
        user_dir.mkdir(parents=True)
        (user_dir / "config.toml").write_text("""
[mcp]
default_agent = "UserAgent"
port = 3000
""")

        # Project config
        project_dir = tmp_path / "project" / ".watercooler"
        project_dir.mkdir(parents=True)
        (project_dir / "config.toml").write_text("""
[mcp]
default_agent = "ProjectAgent"
""")

        from watercooler.config_loader import clear_config_cache, load_config
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

        config = load_config(project_path=tmp_path / "project")

        # Project overrides user
        assert config.mcp.default_agent == "ProjectAgent"
        # User value used when not in project
        assert config.mcp.port == 3000

    def test_env_overrides_all_files(self, tmp_path, monkeypatch):
        """Test that environment variables override all file configs."""
        # User config
        user_dir = tmp_path / ".watercooler"
        user_dir.mkdir()
        (user_dir / "config.toml").write_text("""
[mcp]
default_agent = "FileAgent"
""")

        from watercooler.config_loader import clear_config_cache, load_config
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Set env var
        monkeypatch.setenv("WATERCOOLER_AGENT", "EnvAgent")

        config = load_config()

        # Env should override file
        assert config.mcp.default_agent == "EnvAgent"


class TestCliConfigCommands:
    """Test CLI config commands work end-to-end."""

    def test_config_show_outputs_toml(self, tmp_path, monkeypatch, capsys):
        """Test that 'watercooler config show' outputs valid TOML."""
        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("""
[mcp]
default_agent = "TestAgent"
""")

        from watercooler.config_loader import clear_config_cache
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Run CLI
        from watercooler.cli import main
        import sys

        with pytest.raises(SystemExit) as exc_info:
            main(["config", "show"])

        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        # Should contain TOML-formatted output
        assert "default_agent" in captured.out
        assert "TestAgent" in captured.out

    def test_config_validate_reports_errors(self, tmp_path, monkeypatch, capsys):
        """Test that 'watercooler config validate' detects issues."""
        config_dir = tmp_path / ".watercooler"
        config_dir.mkdir()
        # Invalid TOML
        (config_dir / "config.toml").write_text("invalid [ toml")

        from watercooler.config_loader import clear_config_cache
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        from watercooler.cli import main

        # Should complete (may warn but not crash)
        with pytest.raises(SystemExit):
            main(["config", "validate"])

    def test_config_init_creates_file(self, tmp_path, monkeypatch):
        """Test that 'watercooler config init' creates a config file."""
        from watercooler.config_loader import clear_config_cache
        clear_config_cache()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        from watercooler.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["config", "init"])

        assert exc_info.value.code == 0

        # Config file should exist
        config_path = tmp_path / ".watercooler" / "config.toml"
        assert config_path.exists()
