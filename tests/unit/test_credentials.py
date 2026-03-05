"""Tests for credentials module."""

from __future__ import annotations

import json
import os
import stat
import warnings
from pathlib import Path

import pytest

from watercooler.credentials import (
    AnthropicCredentials,
    Credentials,
    DashboardCredentials,
    GitHubCredentials,
    GoogleCredentials,
    GroqCredentials,
    OpenAICredentials,
    VoyageCredentials,
    _MAX_JSON_SIZE_BYTES,
    _migrate_json_to_toml,
    _secure_file_permissions,
    get_github_token,
    get_provider_api_key,
    get_ssh_key_path,
    load_credentials,
    save_credentials,
)


class TestCredentialsModels:
    """Tests for credential model classes."""

    def test_github_credentials_defaults(self):
        """GitHubCredentials has empty defaults."""
        creds = GitHubCredentials()
        assert creds.token == ""
        assert creds.ssh_key == ""

    def test_dashboard_credentials_defaults(self):
        """DashboardCredentials has empty defaults."""
        creds = DashboardCredentials()
        assert creds.session_secret == ""

    def test_credentials_defaults(self):
        """Credentials has nested defaults."""
        creds = Credentials()
        assert isinstance(creds.github, GitHubCredentials)
        assert isinstance(creds.dashboard, DashboardCredentials)

    def test_credentials_with_values(self):
        """Credentials can be constructed with values."""
        creds = Credentials(
            github=GitHubCredentials(token="gh_test_token"),
            dashboard=DashboardCredentials(session_secret="secret123"),
        )
        assert creds.github.token == "gh_test_token"
        assert creds.dashboard.session_secret == "secret123"


class TestSecureFilePermissions:
    """Tests for _secure_file_permissions function."""

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only test")
    def test_sets_600_permissions(self, tmp_path):
        """Sets file to owner read/write only."""
        test_file = tmp_path / "secret.txt"
        test_file.write_text("secret")

        # Make world readable first
        os.chmod(test_file, 0o644)

        _secure_file_permissions(test_file)

        mode = test_file.stat().st_mode & 0o777
        assert mode == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only test")
    def test_warns_on_permission_error(self, tmp_path):
        """Warns when permissions cannot be set."""
        test_file = tmp_path / "secret.txt"
        test_file.write_text("secret")

        # Delete the file to cause an error
        test_file.unlink()

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _secure_file_permissions(test_file)

            # Should warn about permission failure
            assert len(w) == 1
            assert "Could not set secure permissions" in str(w[0].message)


class TestMigrateJsonToToml:
    """Tests for _migrate_json_to_toml function."""

    def test_migrates_simple_structure(self, tmp_path):
        """Migrates simple JSON structure."""
        json_path = tmp_path / "credentials.json"
        toml_path = tmp_path / "credentials.toml"

        json_path.write_text(json.dumps({
            "github_token": "gh_test_123",
            "session_secret": "secret456",
        }))

        result = _migrate_json_to_toml(json_path, toml_path)

        assert result is True
        assert toml_path.exists()
        assert not json_path.exists()  # Renamed to .bak
        assert (tmp_path / "credentials.json.bak").exists()

    def test_migrates_nested_structure(self, tmp_path):
        """Migrates nested JSON structure."""
        json_path = tmp_path / "credentials.json"
        toml_path = tmp_path / "credentials.toml"

        json_path.write_text(json.dumps({
            "github": {"token": "gh_test_nested"},
            "dashboard": {"session_secret": "nested_secret"},
        }))

        result = _migrate_json_to_toml(json_path, toml_path)
        assert result is True

    def test_rejects_large_file(self, tmp_path):
        """Rejects files larger than size limit."""
        json_path = tmp_path / "credentials.json"
        toml_path = tmp_path / "credentials.toml"

        # Create file larger than limit
        large_content = {"data": "x" * (_MAX_JSON_SIZE_BYTES + 1000)}
        json_path.write_text(json.dumps(large_content))

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = _migrate_json_to_toml(json_path, toml_path)

            assert result is False
            assert not toml_path.exists()
            assert len(w) == 1
            assert "too large" in str(w[0].message)

    def test_handles_invalid_json(self, tmp_path):
        """Handles invalid JSON gracefully."""
        json_path = tmp_path / "credentials.json"
        toml_path = tmp_path / "credentials.toml"

        json_path.write_text("not valid json {{{")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = _migrate_json_to_toml(json_path, toml_path)

            assert result is False


class TestLoadCredentials:
    """Tests for load_credentials function."""

    def test_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        """Returns empty credentials when no file exists."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        creds = creds_mod.load_credentials()
        assert creds.github.token == ""
        assert creds.dashboard.session_secret == ""

    def test_loads_from_toml(self, tmp_path, monkeypatch):
        """Loads credentials from TOML file."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)

        toml_file = config_dir / "credentials.toml"
        toml_file.write_text("""
[github]
token = "test_token_from_toml"

[dashboard]
session_secret = "toml_secret"
""")

        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        creds = creds_mod.load_credentials()
        assert creds.github.token == "test_token_from_toml"
        assert creds.dashboard.session_secret == "toml_secret"

    def test_auto_migrates_json(self, tmp_path, monkeypatch):
        """Auto-migrates from JSON to TOML."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)

        json_file = config_dir / "credentials.json"
        json_file.write_text(json.dumps({
            "github_token": "migrated_token",
        }))

        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        creds = creds_mod.load_credentials(auto_migrate=True)
        assert creds.github.token == "migrated_token"

        # TOML file should now exist
        assert (config_dir / "credentials.toml").exists()


class TestSaveCredentials:
    """Tests for save_credentials function."""

    def test_saves_credentials(self, tmp_path, monkeypatch):
        """Saves credentials to TOML file."""
        fake_home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        creds = Credentials(
            github=GitHubCredentials(token="save_test_token"),
        )

        path = creds_mod.save_credentials(creds)
        assert path.exists()

        # Verify content
        content = path.read_text()
        assert "save_test_token" in content


class TestGetGithubToken:
    """Tests for get_github_token function."""

    def test_env_takes_precedence(self, tmp_path, monkeypatch):
        """Environment variable takes precedence over file."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)

        (config_dir / "credentials.toml").write_text("""
[github]
token = "file_token"
""")

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("GITHUB_TOKEN", "env_token")

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        token = creds_mod.get_github_token()
        assert token == "env_token"

    def test_gh_token_env_var(self, tmp_path, monkeypatch):
        """GH_TOKEN environment variable is recognized."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "gh_token_var")

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        token = creds_mod.get_github_token()
        assert token == "gh_token_var"

    def test_falls_back_to_file(self, tmp_path, monkeypatch):
        """Falls back to file when no env var."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)

        (config_dir / "credentials.toml").write_text("""
[github]
token = "fallback_token"
""")

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        token = creds_mod.get_github_token()
        assert token == "fallback_token"


class TestGetSshKeyPath:
    """Tests for get_ssh_key_path function."""

    def test_env_takes_precedence(self, tmp_path, monkeypatch):
        """Environment variable takes precedence."""
        monkeypatch.setenv("WATERCOOLER_GIT_SSH_KEY", "/env/ssh/key")

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        path = creds_mod.get_ssh_key_path()
        assert path == Path("/env/ssh/key")

    def test_expands_tilde(self, tmp_path, monkeypatch):
        """Expands ~ in path."""
        monkeypatch.setenv("WATERCOOLER_GIT_SSH_KEY", "~/ssh/key")

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        path = creds_mod.get_ssh_key_path()
        assert "~" not in str(path)
        assert str(path).startswith(str(Path.home()))


class TestProviderCredentialsModels:
    """Tests for provider-specific credential models."""

    def test_openai_credentials_defaults(self):
        """OpenAICredentials has empty defaults."""
        creds = OpenAICredentials()
        assert creds.api_key == ""

    def test_anthropic_credentials_defaults(self):
        """AnthropicCredentials has empty defaults."""
        creds = AnthropicCredentials()
        assert creds.api_key == ""

    def test_groq_credentials_defaults(self):
        """GroqCredentials has empty defaults."""
        creds = GroqCredentials()
        assert creds.api_key == ""

    def test_voyage_credentials_defaults(self):
        """VoyageCredentials has empty defaults."""
        creds = VoyageCredentials()
        assert creds.api_key == ""

    def test_google_credentials_defaults(self):
        """GoogleCredentials has empty defaults."""
        creds = GoogleCredentials()
        assert creds.api_key == ""

    def test_credentials_has_provider_fields(self):
        """Credentials includes all provider credential fields."""
        creds = Credentials()
        assert hasattr(creds, "openai")
        assert hasattr(creds, "anthropic")
        assert hasattr(creds, "groq")
        assert hasattr(creds, "voyage")
        assert hasattr(creds, "google")
        assert isinstance(creds.openai, OpenAICredentials)
        assert isinstance(creds.anthropic, AnthropicCredentials)

    def test_credentials_with_provider_values(self):
        """Credentials can be constructed with provider values."""
        creds = Credentials(
            openai=OpenAICredentials(api_key="sk-test-123"),
            anthropic=AnthropicCredentials(api_key="sk-ant-test"),
            groq=GroqCredentials(api_key="gsk_test"),
        )
        assert creds.openai.api_key == "sk-test-123"
        assert creds.anthropic.api_key == "sk-ant-test"
        assert creds.groq.api_key == "gsk_test"


class TestGetProviderApiKey:
    """Tests for get_provider_api_key function."""

    def test_returns_none_when_no_file(self, tmp_path, monkeypatch):
        """Returns None when credentials file doesn't exist."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        key = creds_mod.get_provider_api_key("openai")
        assert key is None

    def test_returns_key_from_toml(self, tmp_path, monkeypatch):
        """Returns API key from TOML credentials file."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)

        (config_dir / "credentials.toml").write_text("""
[openai]
api_key = "sk-test-from-toml"

[anthropic]
api_key = "sk-ant-test-from-toml"
""")

        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        assert creds_mod.get_provider_api_key("openai") == "sk-test-from-toml"
        assert creds_mod.get_provider_api_key("anthropic") == "sk-ant-test-from-toml"

    def test_returns_none_for_empty_key(self, tmp_path, monkeypatch):
        """Returns None when API key is empty string."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)

        (config_dir / "credentials.toml").write_text("""
[openai]
api_key = ""
""")

        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        key = creds_mod.get_provider_api_key("openai")
        assert key is None

    def test_case_insensitive_provider(self, tmp_path, monkeypatch):
        """Provider name is case-insensitive."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)

        (config_dir / "credentials.toml").write_text("""
[openai]
api_key = "sk-case-test"
""")

        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        assert creds_mod.get_provider_api_key("openai") == "sk-case-test"
        assert creds_mod.get_provider_api_key("OPENAI") == "sk-case-test"
        assert creds_mod.get_provider_api_key("OpenAI") == "sk-case-test"

    def test_returns_none_for_unknown_provider(self, tmp_path, monkeypatch):
        """Returns None for unknown provider."""
        fake_home = tmp_path / "home"
        config_dir = fake_home / ".watercooler"
        config_dir.mkdir(parents=True)

        (config_dir / "credentials.toml").write_text("""
[openai]
api_key = "sk-test"
""")

        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        key = creds_mod.get_provider_api_key("unknown_provider")
        assert key is None


class TestSaveCredentialsWithProviders:
    """Tests for save_credentials with provider API keys."""

    def test_saves_provider_keys(self, tmp_path, monkeypatch):
        """Saves provider API keys to TOML file."""
        fake_home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        creds = Credentials(
            openai=OpenAICredentials(api_key="sk-save-test"),
            anthropic=AnthropicCredentials(api_key="sk-ant-save"),
            groq=GroqCredentials(api_key="gsk_save"),
        )

        path = creds_mod.save_credentials(creds)
        assert path.exists()

        content = path.read_text()
        assert "sk-save-test" in content
        assert "sk-ant-save" in content
        assert "gsk_save" in content
        assert "[openai]" in content
        assert "[anthropic]" in content
        assert "[groq]" in content

    def test_skips_empty_provider_keys(self, tmp_path, monkeypatch):
        """Doesn't write sections for empty provider keys."""
        fake_home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        creds = Credentials(
            openai=OpenAICredentials(api_key="sk-only-openai"),
            # anthropic, groq, etc. left as empty defaults
        )

        path = creds_mod.save_credentials(creds)
        content = path.read_text()

        assert "[openai]" in content
        assert "sk-only-openai" in content
        assert "[anthropic]" not in content
        assert "[groq]" not in content

    def test_roundtrip_provider_keys(self, tmp_path, monkeypatch):
        """Provider keys survive save/load roundtrip."""
        fake_home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(fake_home))

        import importlib
        import watercooler.credentials as creds_mod
        importlib.reload(creds_mod)

        original = Credentials(
            openai=OpenAICredentials(api_key="sk-roundtrip"),
            voyage=VoyageCredentials(api_key="vg-roundtrip"),
            google=GoogleCredentials(api_key="AIza-roundtrip"),
        )

        creds_mod.save_credentials(original)
        loaded = creds_mod.load_credentials()

        assert loaded.openai.api_key == "sk-roundtrip"
        assert loaded.voyage.api_key == "vg-roundtrip"
        assert loaded.google.api_key == "AIza-roundtrip"
