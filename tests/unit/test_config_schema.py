"""Tests for config_schema module."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from watercooler.config_schema import (
    AgentConfig,
    CommonConfig,
    DashboardConfig,
    EntryValidationConfig,
    GitConfig,
    LoggingConfig,
    McpConfig,
    SyncConfig,
    ValidationConfig,
    WatercoolerConfig,
)


class TestAgentConfig:
    """Tests for AgentConfig model."""

    def test_with_name(self):
        """AgentConfig requires name, has default_spec."""
        config = AgentConfig(name="TestAgent")
        assert config.name == "TestAgent"
        assert config.default_spec == "general-purpose"

    def test_custom_values(self):
        """AgentConfig accepts custom values."""
        config = AgentConfig(name="Claude", default_spec="implementer-code")
        assert config.name == "Claude"
        assert config.default_spec == "implementer-code"

    def test_name_required(self):
        """AgentConfig requires name field."""
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AgentConfig()  # name is required


class TestGitConfig:
    """Tests for GitConfig model."""

    def test_defaults(self):
        """GitConfig has sensible defaults."""
        config = GitConfig()
        assert config.author == ""
        assert config.email == "mcp@watercooler.dev"
        assert config.ssh_key == ""

    def test_custom_values(self):
        """GitConfig accepts custom values."""
        config = GitConfig(
            author="Test Author",
            email="test@example.com",
            ssh_key="/path/to/key",
        )
        assert config.author == "Test Author"
        assert config.email == "test@example.com"


class TestSyncConfig:
    """Tests for SyncConfig model."""

    def test_defaults(self):
        """SyncConfig has sensible defaults."""
        config = SyncConfig()
        assert config.async_sync is True
        assert config.batch_window == 5.0
        assert config.max_retries == 5
        assert config.interval == 30.0

    def test_batch_window_bounds(self):
        """batch_window must be non-negative."""
        config = SyncConfig(batch_window=0.0)
        assert config.batch_window == 0.0

        config = SyncConfig(batch_window=100.0)
        assert config.batch_window == 100.0


class TestLoggingConfig:
    """Tests for LoggingConfig model."""

    def test_defaults(self):
        """LoggingConfig has sensible defaults."""
        config = LoggingConfig()
        assert config.level == "INFO"
        assert config.max_bytes == 10485760  # 10MB
        assert config.backup_count == 5
        assert config.disable_file is False


class TestMcpConfig:
    """Tests for McpConfig model."""

    def test_defaults(self):
        """McpConfig has sensible defaults."""
        config = McpConfig()
        assert config.transport == "stdio"
        assert config.host == "127.0.0.1"
        assert config.port == 3000
        assert config.default_agent == "Agent"
        assert isinstance(config.git, GitConfig)
        assert isinstance(config.sync, SyncConfig)
        assert isinstance(config.logging, LoggingConfig)

    def test_nested_config(self):
        """McpConfig nests other configs correctly."""
        config = McpConfig(
            default_agent="Claude",
            git=GitConfig(author="Claude"),
            sync=SyncConfig(batch_window=10.0),
        )
        assert config.default_agent == "Claude"
        assert config.git.author == "Claude"
        assert config.sync.batch_window == 10.0

    def test_agents_dict(self):
        """McpConfig handles agents dictionary."""
        config = McpConfig(
            agents={
                "claude-code": AgentConfig(name="Claude", default_spec="implementer-code"),
                "cursor": AgentConfig(name="Cursor"),
            }
        )
        assert "claude-code" in config.agents
        assert config.agents["claude-code"].name == "Claude"


class TestCommonConfig:
    """Tests for CommonConfig model."""

    def test_defaults(self):
        """CommonConfig has sensible defaults."""
        config = CommonConfig()
        # Default pattern uses HTTPS (works without SSH agent - Codex compatibility)
        assert config.threads_pattern == "https://github.com/{org}/{repo}-threads.git"
        assert config.threads_suffix == "-threads"
        assert config.templates_dir == ""

    def test_pattern_overrides_suffix(self):
        """threads_pattern can override default suffix behavior."""
        config = CommonConfig(
            threads_pattern="git@github.com:{org}/{repo}-watercooler.git"
        )
        assert config.threads_pattern is not None
        assert "-watercooler" in config.threads_pattern


class TestValidationConfig:
    """Tests for ValidationConfig model."""

    def test_defaults(self):
        """ValidationConfig has sensible defaults."""
        config = ValidationConfig()
        assert config.on_write is True
        assert config.on_commit is True
        assert config.fail_on_violation is False

    def test_entry_validation(self):
        """Entry validation config works correctly."""
        config = ValidationConfig(
            entry=EntryValidationConfig(
                require_metadata=True,
                allowed_roles=["planner", "implementer"],
            )
        )
        assert config.entry.require_metadata is True
        assert "planner" in config.entry.allowed_roles


class TestDashboardConfig:
    """Tests for DashboardConfig model."""

    def test_defaults(self):
        """DashboardConfig has sensible defaults."""
        config = DashboardConfig()
        assert config.default_repo == ""
        assert config.default_branch == "main"
        assert config.poll_interval_active == 15


class TestWatercoolerConfig:
    """Tests for WatercoolerConfig model."""

    def test_defaults(self):
        """WatercoolerConfig has all default sections."""
        config = WatercoolerConfig()
        assert config.version == 1
        assert isinstance(config.common, CommonConfig)
        assert isinstance(config.mcp, McpConfig)
        assert isinstance(config.dashboard, DashboardConfig)
        assert isinstance(config.validation, ValidationConfig)

    def test_from_dict(self):
        """WatercoolerConfig can be created from dict."""
        config = WatercoolerConfig.model_validate({
            "version": 1,
            "mcp": {
                "default_agent": "TestAgent",
                "git": {"author": "Test"},
            },
        })
        assert config.mcp.default_agent == "TestAgent"
        assert config.mcp.git.author == "Test"

    def test_get_agent_config_found(self):
        """get_agent_config returns config for known platform."""
        config = WatercoolerConfig(
            mcp=McpConfig(
                agents={"claude-code": AgentConfig(name="Claude")}
            )
        )
        agent = config.get_agent_config("claude-code")
        assert agent is not None
        assert agent.name == "Claude"

    def test_get_agent_config_not_found(self):
        """get_agent_config returns None for unknown platform."""
        config = WatercoolerConfig()
        agent = config.get_agent_config("unknown-platform")
        assert agent is None

    def test_to_dict(self):
        """WatercoolerConfig can be serialized to dict."""
        config = WatercoolerConfig(
            mcp=McpConfig(default_agent="TestAgent")
        )
        d = config.model_dump()
        assert isinstance(d, dict)
        assert d["mcp"]["default_agent"] == "TestAgent"

    def test_version_validation(self):
        """Version must be a positive integer."""
        config = WatercoolerConfig(version=1)
        assert config.version == 1

        # Version 2 should be accepted for forward compatibility
        config = WatercoolerConfig(version=2)
        assert config.version == 2
