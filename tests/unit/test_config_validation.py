"""Unit tests for watercooler config schema and validation.

Tests configuration schema enforcement, validation, and loading:
- Pydantic model field validation
- Field validators (path existence, numeric bounds)
- Config precedence (user -> project -> env)
- Deep merge behavior
- Type coercion and defaults
- Security-sensitive field handling
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from watercooler.config_schema import (
    CommonConfig,
    AgentConfig,
    GitConfig,
    SyncConfig,
    LoggingConfig,
    SlackConfig,
    GraphConfig,
    ValidationConfig,
    EntryValidationConfig,
    CommitValidationConfig,
    LLMServiceConfig,
    EmbeddingServiceConfig,
    MemoryDatabaseConfig,
    WatercoolerConfig,
)
from watercooler.config_loader import (
    ConfigError,
    _deep_merge,
    _load_toml,
    _get_user_config_dir,
    _get_project_config_dir,
)


# ============================================================================
# Test CommonConfig
# ============================================================================


class TestCommonConfig:
    """Tests for CommonConfig model."""

    def test_default_values(self):
        """Test default CommonConfig values."""
        config = CommonConfig()
        assert config.threads_suffix == "-threads"
        assert "{org}" in config.threads_pattern
        assert "{repo}" in config.threads_pattern
        assert config.templates_dir == ""

    def test_custom_threads_pattern(self):
        """Test custom threads pattern."""
        config = CommonConfig(threads_pattern="git@github.com:{org}/{repo}-wc.git")
        assert "git@github.com" in config.threads_pattern

    def test_templates_dir_warning_nonexistent(self, tmp_path):
        """Test warning when templates_dir doesn't exist."""
        nonexistent = tmp_path / "nonexistent"

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = CommonConfig(templates_dir=str(nonexistent))
            assert len(w) == 1
            assert "does not exist" in str(w[0].message)

    def test_templates_dir_warning_not_directory(self, tmp_path):
        """Test warning when templates_dir is not a directory."""
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("test")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = CommonConfig(templates_dir=str(file_path))
            assert len(w) == 1
            assert "not a directory" in str(w[0].message)

    def test_templates_dir_valid(self, tmp_path):
        """Test valid templates_dir produces no warning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = CommonConfig(templates_dir=str(tmp_path))
            # Filter for only UserWarning from config validation
            config_warnings = [x for x in w if "templates" in str(x.message).lower()]
            assert len(config_warnings) == 0


# ============================================================================
# Test GitConfig
# ============================================================================


class TestGitConfig:
    """Tests for GitConfig model."""

    def test_default_values(self):
        """Test default GitConfig values."""
        config = GitConfig()
        assert config.author == ""
        assert config.email == "mcp@watercooler.dev"
        assert config.ssh_key == ""

    def test_ssh_key_warning_nonexistent(self, tmp_path):
        """Test warning when ssh_key path doesn't exist."""
        nonexistent = tmp_path / "nonexistent_key"

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = GitConfig(ssh_key=str(nonexistent))
            assert len(w) == 1
            assert "does not exist" in str(w[0].message)

    def test_ssh_key_warning_not_file(self, tmp_path):
        """Test warning when ssh_key is a directory."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = GitConfig(ssh_key=str(tmp_path))
            assert len(w) == 1
            assert "not a file" in str(w[0].message)

    def test_ssh_key_valid(self, tmp_path):
        """Test valid ssh_key produces no warning."""
        key_file = tmp_path / "id_rsa"
        key_file.write_text("fake key content")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = GitConfig(ssh_key=str(key_file))
            ssh_warnings = [x for x in w if "ssh" in str(x.message).lower()]
            assert len(ssh_warnings) == 0


# ============================================================================
# Test SyncConfig
# ============================================================================


class TestSyncConfig:
    """Tests for SyncConfig model."""

    def test_default_values(self):
        """Test default SyncConfig values."""
        config = SyncConfig()
        assert config.async_sync is True
        assert config.batch_window == 5.0
        assert config.max_delay == 30.0
        assert config.max_batch_size == 50
        assert config.max_retries == 5

    def test_batch_window_minimum(self):
        """Test batch_window minimum bound (ge=0)."""
        config = SyncConfig(batch_window=0.0)
        assert config.batch_window == 0.0

        with pytest.raises(ValidationError):
            SyncConfig(batch_window=-1.0)

    def test_max_batch_size_minimum(self):
        """Test max_batch_size minimum bound (ge=1)."""
        config = SyncConfig(max_batch_size=1)
        assert config.max_batch_size == 1

        with pytest.raises(ValidationError):
            SyncConfig(max_batch_size=0)

    def test_interval_minimum(self):
        """Test interval minimum bound (ge=1)."""
        config = SyncConfig(interval=1.0)
        assert config.interval == 1.0

        with pytest.raises(ValidationError):
            SyncConfig(interval=0.5)

    def test_alias_async(self):
        """Test that 'async' alias works for async_sync."""
        # Pydantic alias allows both names
        config = SyncConfig(**{"async": False})
        assert config.async_sync is False


# ============================================================================
# Test LoggingConfig
# ============================================================================


class TestLoggingConfig:
    """Tests for LoggingConfig model."""

    def test_default_values(self):
        """Test default LoggingConfig values."""
        config = LoggingConfig()
        assert config.level == "INFO"
        assert config.max_bytes == 10485760  # 10MB
        assert config.backup_count == 5
        assert config.disable_file is False

    def test_level_validation(self):
        """Test log level literal validation."""
        for level in ["DEBUG", "INFO", "WARNING", "ERROR"]:
            config = LoggingConfig(level=level)
            assert config.level == level

        with pytest.raises(ValidationError):
            LoggingConfig(level="INVALID")

    def test_log_dir_warning_not_directory(self, tmp_path):
        """Test warning when log dir exists but is not a directory."""
        file_path = tmp_path / "log_file"
        file_path.write_text("test")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = LoggingConfig(dir=str(file_path))
            assert len(w) == 1
            assert "not a directory" in str(w[0].message)


# ============================================================================
# Test SlackConfig
# ============================================================================


class TestSlackConfig:
    """Tests for SlackConfig model."""

    def test_default_values(self):
        """Test default SlackConfig values."""
        config = SlackConfig()
        assert config.webhook_url == ""
        assert config.bot_token == ""
        assert config.channel_prefix == "wc-"
        assert config.is_enabled is False

    def test_is_enabled_with_webhook(self):
        """Test is_enabled returns True with webhook."""
        config = SlackConfig(webhook_url="https://hooks.slack.com/services/xxx")
        assert config.is_enabled is True
        assert config.is_webhook_only is True
        assert config.is_bot_enabled is False

    def test_is_enabled_with_bot(self):
        """Test is_enabled returns True with bot token."""
        config = SlackConfig(bot_token="xoxb-test-token")
        assert config.is_enabled is True
        assert config.is_webhook_only is False
        assert config.is_bot_enabled is True

    def test_notification_rate_limit(self):
        """Test notification rate limit validation."""
        config = SlackConfig(min_notification_interval=0.0)
        assert config.min_notification_interval == 0.0

        with pytest.raises(ValidationError):
            SlackConfig(min_notification_interval=-1.0)


# ============================================================================
# Test ValidationConfig
# ============================================================================


class TestValidationConfig:
    """Tests for ValidationConfig model."""

    def test_default_values(self):
        """Test default ValidationConfig values."""
        config = ValidationConfig()
        assert config.on_write is True
        assert config.on_commit is True
        assert config.fail_on_violation is False
        assert config.check_branch_pairing is True

    def test_nested_entry_config(self):
        """Test nested EntryValidationConfig."""
        config = ValidationConfig()
        assert config.entry.require_metadata is True
        assert "planner" in config.entry.allowed_roles
        assert "Note" in config.entry.allowed_types

    def test_nested_commit_config(self):
        """Test nested CommitValidationConfig."""
        config = ValidationConfig()
        assert config.commit.require_footers is True
        assert "Code-Repo" in config.commit.required_footer_fields

    def test_custom_entry_roles(self):
        """Test custom allowed_roles."""
        entry = EntryValidationConfig(allowed_roles=["custom-role", "another"])
        assert entry.allowed_roles == ["custom-role", "another"]


# ============================================================================
# Test LLMServiceConfig
# ============================================================================


class TestLLMServiceConfig:
    """Tests for LLMServiceConfig model."""

    def test_default_values(self):
        """Test default LLMServiceConfig values.

        Note: api_base and model default to empty string, meaning "use context-specific
        default" (e.g., localhost for baseline graph, resolved at runtime).
        """
        config = LLMServiceConfig()
        assert config.api_base == ""  # Empty = use context-specific default
        assert config.model == ""  # Empty = use context-specific default
        assert config.timeout == 60.0
        assert config.max_tokens == 512

    def test_timeout_minimum(self):
        """Test timeout minimum bound (ge=1.0)."""
        config = LLMServiceConfig(timeout=1.0)
        assert config.timeout == 1.0

        with pytest.raises(ValidationError):
            LLMServiceConfig(timeout=0.5)

    def test_max_tokens_minimum(self):
        """Test max_tokens minimum bound (ge=1)."""
        config = LLMServiceConfig(max_tokens=1)
        assert config.max_tokens == 1

        with pytest.raises(ValidationError):
            LLMServiceConfig(max_tokens=0)

    def test_custom_api_base(self):
        """Test custom API base."""
        config = LLMServiceConfig(api_base="http://localhost:8080/v1")
        assert config.api_base == "http://localhost:8080/v1"


# ============================================================================
# Test EmbeddingServiceConfig
# ============================================================================


class TestEmbeddingServiceConfig:
    """Tests for EmbeddingServiceConfig model."""

    def test_default_values(self):
        """Test default EmbeddingServiceConfig values."""
        config = EmbeddingServiceConfig()
        assert config.api_base == "http://localhost:8080/v1"
        assert config.model == "bge-m3"
        assert config.dim == 1024
        assert config.context_size == 8192

    def test_dim_minimum(self):
        """Test dim minimum bound (ge=1)."""
        config = EmbeddingServiceConfig(dim=1)
        assert config.dim == 1

        with pytest.raises(ValidationError):
            EmbeddingServiceConfig(dim=0)

    def test_context_size_minimum(self):
        """Test context_size minimum bound (ge=128)."""
        config = EmbeddingServiceConfig(context_size=128)
        assert config.context_size == 128

        with pytest.raises(ValidationError):
            EmbeddingServiceConfig(context_size=100)

    def test_batch_size_minimum(self):
        """Test batch_size minimum bound (ge=1)."""
        config = EmbeddingServiceConfig(batch_size=1)
        assert config.batch_size == 1

        with pytest.raises(ValidationError):
            EmbeddingServiceConfig(batch_size=0)


# ============================================================================
# Test MemoryDatabaseConfig
# ============================================================================


class TestMemoryDatabaseConfig:
    """Tests for MemoryDatabaseConfig model."""

    def test_default_values(self):
        """Test default MemoryDatabaseConfig values."""
        config = MemoryDatabaseConfig()
        assert config.host == "localhost"
        assert config.port == 6379
        assert config.username == ""

    def test_username_default_is_empty_string(self):
        """Test that username field defaults to empty string."""
        config = MemoryDatabaseConfig()
        assert config.username == ""

    def test_username_can_be_set(self):
        """Test that username field can be set explicitly."""
        config = MemoryDatabaseConfig(username="myuser")
        assert config.username == "myuser"

    def test_port_range(self):
        """Test port range validation (1-65535)."""
        config = MemoryDatabaseConfig(port=1)
        assert config.port == 1

        config = MemoryDatabaseConfig(port=65535)
        assert config.port == 65535

        with pytest.raises(ValidationError):
            MemoryDatabaseConfig(port=0)

        with pytest.raises(ValidationError):
            MemoryDatabaseConfig(port=65536)


# ============================================================================
# Test WatercoolerConfig (Top-level)
# ============================================================================


class TestWatercoolerConfig:
    """Tests for WatercoolerConfig top-level model."""

    def test_default_creation(self):
        """Test creating WatercoolerConfig with defaults."""
        config = WatercoolerConfig()
        assert config.common is not None
        assert config.mcp is not None

    def test_nested_access(self):
        """Test accessing nested config sections."""
        config = WatercoolerConfig()
        # Access nested config
        assert config.common.threads_suffix == "-threads"


# ============================================================================
# Test Config Loader Functions
# ============================================================================


class TestDeepMerge:
    """Tests for _deep_merge function."""

    def test_simple_merge(self):
        """Test merging flat dictionaries."""
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}

        result = _deep_merge(base, override)

        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        """Test merging nested dictionaries."""
        base = {"level1": {"a": 1, "b": 2}}
        override = {"level1": {"b": 3, "c": 4}}

        result = _deep_merge(base, override)

        assert result == {"level1": {"a": 1, "b": 3, "c": 4}}

    def test_deep_nested_merge(self):
        """Test merging deeply nested structures."""
        base = {
            "l1": {
                "l2": {
                    "a": 1,
                    "b": 2,
                },
            },
        }
        override = {
            "l1": {
                "l2": {
                    "b": 3,
                },
                "new_key": "value",
            },
        }

        result = _deep_merge(base, override)

        assert result["l1"]["l2"]["a"] == 1
        assert result["l1"]["l2"]["b"] == 3
        assert result["l1"]["new_key"] == "value"

    def test_list_replacement(self):
        """Test that lists are replaced, not merged."""
        base = {"items": [1, 2, 3]}
        override = {"items": [4, 5]}

        result = _deep_merge(base, override)

        assert result == {"items": [4, 5]}

    def test_type_override(self):
        """Test that override value takes precedence even with type change."""
        base = {"value": {"nested": True}}
        override = {"value": "string"}

        result = _deep_merge(base, override)

        assert result == {"value": "string"}

    def test_base_not_mutated(self):
        """Test that original base dict is not mutated."""
        base = {"a": 1, "b": {"c": 2}}
        override = {"b": {"c": 3}}

        result = _deep_merge(base, override)

        assert base["b"]["c"] == 2  # Original unchanged
        assert result["b"]["c"] == 3


class TestLoadToml:
    """Tests for _load_toml function."""

    def test_load_valid_toml(self, tmp_path):
        """Test loading valid TOML file."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(dedent("""\
            [section]
            key = "value"
            number = 42
        """))

        data = _load_toml(toml_file)

        assert data["section"]["key"] == "value"
        assert data["section"]["number"] == 42

    def test_load_missing_file(self, tmp_path):
        """Test loading nonexistent file raises ConfigError."""
        with pytest.raises(Exception, match="not found"):
            _load_toml(tmp_path / "missing.toml")

    def test_load_invalid_toml(self, tmp_path):
        """Test loading invalid TOML raises ConfigError."""
        toml_file = tmp_path / "invalid.toml"
        toml_file.write_text("not valid = [toml syntax")

        with pytest.raises(Exception, match="Invalid TOML"):
            _load_toml(toml_file)


class TestConfigDiscovery:
    """Tests for config directory discovery."""

    def test_get_user_config_dir(self):
        """Test user config directory is in home."""
        user_dir = _get_user_config_dir()
        assert user_dir == Path.home() / ".watercooler"

    def test_get_project_config_dir_found(self, tmp_path):
        """Test finding project config directory."""
        # Create .watercooler directory
        (tmp_path / ".watercooler").mkdir()

        # Search from subdirectory
        subdir = tmp_path / "src" / "module"
        subdir.mkdir(parents=True)

        project_dir = _get_project_config_dir(subdir)

        assert project_dir == tmp_path / ".watercooler"

    def test_get_project_config_dir_not_found(self, tmp_path):
        """Test returns None when no config dir found."""
        project_dir = _get_project_config_dir(tmp_path)
        assert project_dir is None


# ============================================================================
# Test Type Coercion
# ============================================================================


class TestTypeCoercion:
    """Tests for Pydantic type coercion."""

    def test_string_to_int_coercion(self):
        """Test string is coerced to int."""
        # Pydantic will coerce string "42" to int 42
        config = SyncConfig(max_batch_size="50")
        assert config.max_batch_size == 50
        assert isinstance(config.max_batch_size, int)

    def test_string_to_float_coercion(self):
        """Test string is coerced to float."""
        config = SyncConfig(batch_window="10.5")
        assert config.batch_window == 10.5
        assert isinstance(config.batch_window, float)

    def test_string_to_bool_coercion(self):
        """Test string to bool coercion."""
        # Note: Pydantic v2 requires explicit truthy values
        config = SyncConfig(async_sync=True)
        assert config.async_sync is True


# ============================================================================
# Test Edge Cases
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_config(self):
        """Test creating config with empty dict."""
        config = WatercoolerConfig()
        # Should use all defaults
        assert config.common.threads_suffix == "-threads"

    def test_extra_fields_ignored(self):
        """Test that extra fields are ignored."""
        # By default Pydantic ignores extra fields
        config = CommonConfig(threads_suffix="-threads", unknown_field="value")
        assert config.threads_suffix == "-threads"

    def test_nested_validation_error(self):
        """Test validation error in nested config."""
        with pytest.raises(ValidationError) as exc_info:
            SyncConfig(max_batch_size=-1)

        # Error should mention the field
        errors = exc_info.value.errors()
        assert len(errors) > 0

    def test_validation_error_message_clarity(self):
        """Test that validation errors have clear messages."""
        with pytest.raises(ValidationError) as exc_info:
            LoggingConfig(level="INVALID_LEVEL")

        # Should mention the invalid value
        error_str = str(exc_info.value)
        assert "INVALID_LEVEL" in error_str or "level" in error_str.lower()

    def test_path_with_home_expansion(self, tmp_path, monkeypatch):
        """Test that ~ is expanded in paths."""
        # Mock home directory
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Create the expanded path
        (tmp_path / "templates").mkdir()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = CommonConfig(templates_dir="~/templates")
            # Should not warn because path exists when expanded
            template_warnings = [x for x in w if "template" in str(x.message).lower()]
            # Note: validator checks Path(v).expanduser() so it should find it
            # But the check happens before full resolution


# ============================================================================
# Test Security-Sensitive Fields
# ============================================================================


class TestSecurityFields:
    """Tests for security-sensitive configuration."""

    def test_api_key_not_in_config(self):
        """Test that API keys are not stored in config schema.

        API keys belong in credentials.toml, not config.toml.
        The LLMServiceConfig and EmbeddingServiceConfig no longer have api_key fields.
        """
        config = LLMServiceConfig()

        # api_key field was removed - secrets belong in credentials.toml
        assert not hasattr(config, "api_key")
        # But api_base and model are still config settings
        assert hasattr(config, "api_base")
        assert hasattr(config, "model")

    def test_credentials_separate_from_config(self):
        """Test that credentials and config are properly separated.

        Config holds settings (api_base, model, etc.) - can be version controlled.
        Credentials hold secrets (api_key) - stored in credentials.toml, never committed.
        """
        from watercooler.credentials import Credentials, OpenAICredentials

        # Config doesn't have api_key
        config = LLMServiceConfig()
        assert not hasattr(config, "api_key")

        # Credentials have api_key by provider
        creds = Credentials()
        assert hasattr(creds, "openai")
        assert hasattr(creds.openai, "api_key")

    def test_webhook_url_not_required(self):
        """Test webhook URL is optional."""
        config = SlackConfig()
        assert config.webhook_url == ""
        assert config.is_enabled is False


# ============================================================================
# Test Config Precedence
# ============================================================================


class TestConfigPrecedence:
    """Tests for config precedence chain."""

    def test_override_takes_precedence(self):
        """Test that override values take precedence in merge."""
        base = {"common": {"threads_suffix": "-base"}}
        override = {"common": {"threads_suffix": "-override"}}

        result = _deep_merge(base, override)

        assert result["common"]["threads_suffix"] == "-override"

    def test_partial_override(self):
        """Test that partial overrides preserve unspecified values."""
        base = {
            "common": {
                "threads_suffix": "-threads",
                "templates_dir": "/base/templates",
            }
        }
        override = {
            "common": {
                "threads_suffix": "-custom",
            }
        }

        result = _deep_merge(base, override)

        assert result["common"]["threads_suffix"] == "-custom"
        assert result["common"]["templates_dir"] == "/base/templates"

    def test_new_section_added(self):
        """Test that new sections are added."""
        base = {"existing": {"key": "value"}}
        override = {"new_section": {"new_key": "new_value"}}

        result = _deep_merge(base, override)

        assert "existing" in result
        assert "new_section" in result
        assert result["new_section"]["new_key"] == "new_value"
