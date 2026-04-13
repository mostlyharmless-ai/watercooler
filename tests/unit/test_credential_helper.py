"""Tests for git-credential-watercooler script."""

import json
import os
import subprocess
from pathlib import Path
import pytest


@pytest.fixture
def temp_home(tmp_path):
    """Create a temporary home directory for testing."""
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def script_path():
    """Get the path to the credential helper script."""
    return Path(__file__).parent.parent.parent / "scripts" / "git-credential-watercooler"


def run_credential_helper(script_path, action, input_data="", env=None):
    """Run the credential helper script with given input."""
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    result = subprocess.run(
        [str(script_path), action],
        input=input_data,
        capture_output=True,
        text=True,
        env=proc_env
    )
    return result


def test_credential_helper_get_with_valid_credentials(script_path, temp_home):
    """Test credential helper 'get' action with valid local credentials."""
    # Create credentials file
    creds_dir = temp_home / ".watercooler"
    creds_dir.mkdir()
    creds_file = creds_dir / "credentials.json"

    creds_data = {"github_token": "ghp_test123"}
    creds_file.write_text(json.dumps(creds_data))
    creds_file.chmod(0o600)

    # Run credential helper
    input_data = "protocol=https\nhost=github.com\n\n"
    result = run_credential_helper(
        script_path,
        "get",
        input_data,
        env={"HOME": str(temp_home)}
    )

    assert result.returncode == 0
    assert "username=token" in result.stdout
    assert "password=ghp_test123" in result.stdout


def test_credential_helper_get_no_credentials(script_path, temp_home):
    """Test credential helper 'get' action with no credentials available."""
    input_data = "protocol=https\nhost=github.com\n\n"
    result = run_credential_helper(
        script_path,
        "get",
        input_data,
        env={"HOME": str(temp_home)}  # Empty home, no credentials
    )

    assert result.returncode == 1


def test_credential_helper_get_non_github_host(script_path):
    """Test credential helper skips non-GitHub hosts."""
    input_data = "protocol=https\nhost=gitlab.com\n\n"
    result = run_credential_helper(script_path, "get", input_data)

    assert result.returncode == 0
    assert result.stdout == ""


def test_credential_helper_priority_local_file(script_path, temp_home):
    """Test that local credentials file takes priority over env vars."""
    # Create credentials file
    creds_dir = temp_home / ".watercooler"
    creds_dir.mkdir()
    creds_file = creds_dir / "credentials.json"

    creds_data = {"github_token": "ghp_from_file"}
    creds_file.write_text(json.dumps(creds_data))
    creds_file.chmod(0o600)

    # Run with env vars set (should prefer file)
    input_data = "protocol=https\nhost=github.com\n\n"
    result = run_credential_helper(
        script_path,
        "get",
        input_data,
        env={
            "HOME": str(temp_home),
            "WATERCOOLER_GITHUB_TOKEN": "ghp_from_env",
            "GITHUB_TOKEN": "ghp_from_github",
        }
    )

    assert result.returncode == 0
    assert "password=ghp_from_file" in result.stdout


def test_credential_helper_priority_watercooler_env(script_path, temp_home):
    """Test WATERCOOLER_GITHUB_TOKEN takes priority over GITHUB_TOKEN."""
    input_data = "protocol=https\nhost=github.com\n\n"
    result = run_credential_helper(
        script_path,
        "get",
        input_data,
        env={
            "HOME": str(temp_home),
            "WATERCOOLER_GITHUB_TOKEN": "ghp_from_watercooler",
            "GITHUB_TOKEN": "ghp_from_github",
        }
    )

    assert result.returncode == 0
    assert "password=ghp_from_watercooler" in result.stdout


def test_credential_helper_github_token_env(script_path, temp_home):
    """Test GITHUB_TOKEN fallback."""
    input_data = "protocol=https\nhost=github.com\n\n"
    result = run_credential_helper(
        script_path,
        "get",
        input_data,
        env={
            "HOME": str(temp_home),
            "GITHUB_TOKEN": "ghp_from_github",
        }
    )

    assert result.returncode == 0
    assert "password=ghp_from_github" in result.stdout


def test_credential_helper_gh_token_env(script_path, temp_home):
    """Test GH_TOKEN fallback."""
    input_data = "protocol=https\nhost=github.com\n\n"
    result = run_credential_helper(
        script_path,
        "get",
        input_data,
        env={
            "HOME": str(temp_home),
            "GH_TOKEN": "ghp_from_gh",
        }
    )

    assert result.returncode == 0
    assert "password=ghp_from_gh" in result.stdout


@pytest.mark.skipif(os.name == 'nt', reason="Permission checks not applicable on Windows")
def test_credential_helper_insecure_permissions_warning(script_path, temp_home):
    """Test warning for insecure file permissions."""
    # Create credentials file with insecure permissions
    creds_dir = temp_home / ".watercooler"
    creds_dir.mkdir()
    creds_file = creds_dir / "credentials.json"

    creds_data = {"github_token": "ghp_test123"}
    creds_file.write_text(json.dumps(creds_data))
    creds_file.chmod(0o644)  # World-readable!

    input_data = "protocol=https\nhost=github.com\n\n"
    result = run_credential_helper(
        script_path,
        "get",
        input_data,
        env={"HOME": str(temp_home)}
    )

    # Should still work but warn
    assert result.returncode == 0
    assert "password=ghp_test123" in result.stdout
    assert "insecure permissions" in result.stderr
    assert "chmod 600" in result.stderr


def test_credential_helper_invalid_json(script_path, temp_home):
    """Test handling of invalid JSON in credentials file."""
    creds_dir = temp_home / ".watercooler"
    creds_dir.mkdir()
    creds_file = creds_dir / "credentials.json"

    creds_file.write_text("invalid json{")
    creds_file.chmod(0o600)

    input_data = "protocol=https\nhost=github.com\n\n"
    result = run_credential_helper(
        script_path,
        "get",
        input_data,
        env={"HOME": str(temp_home)}
    )

    assert result.returncode == 1
    assert "Invalid JSON" in result.stderr


def test_credential_helper_store_action(script_path):
    """Test credential helper 'store' action (no-op)."""
    result = run_credential_helper(script_path, "store", "")
    assert result.returncode == 0


def test_credential_helper_erase_action(script_path):
    """Test credential helper 'erase' action (no-op)."""
    result = run_credential_helper(script_path, "erase", "")
    assert result.returncode == 0


def test_credential_helper_unknown_action(script_path):
    """Test credential helper with unknown action."""
    result = run_credential_helper(script_path, "unknown", "")
    assert result.returncode == 1
    assert "Unknown action" in result.stderr


def test_credential_helper_no_action(script_path):
    """Test credential helper with no action."""
    result = subprocess.run([str(script_path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Usage" in result.stderr
