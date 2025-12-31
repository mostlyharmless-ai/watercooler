"""Tests for credential helper path resolution in GitSyncManager."""

import pytest
from pathlib import Path
from watercooler_mcp.git_sync import GitSyncManager


def test_credential_helper_path_resolution(tmp_path):
    """Test that credential helper path can be resolved."""
    # Create a mock threads repo
    threads_repo = tmp_path / "test-threads"
    threads_repo.mkdir()

    # Initialize it as a git repo
    import git
    git.Repo.init(threads_repo)

    # Create a GitSyncManager instance
    manager = GitSyncManager(
        local_path=threads_repo,
        repo_url="https://github.com/test/test-threads.git",
        author_name="Test User",
        author_email="test@example.com"
    )

    # Test credential helper path resolution
    helper_path = manager._get_credential_helper_path()

    # Should find the script (either in package data or development location)
    assert helper_path is not None, "Credential helper script should be found"
    assert helper_path.exists(), f"Credential helper should exist at {helper_path}"
    assert helper_path.name == "git-credential-watercooler"

    # Verify it's executable (Unix/Mac only)
    import os
    if os.name != 'nt':
        # Should be readable
        assert os.access(helper_path, os.R_OK), "Credential helper should be readable"


def test_credential_helper_path_in_package_data():
    """Test that credential helper exists in package scripts directory."""
    # Check if it exists in the package structure
    from pathlib import Path
    import watercooler_mcp

    module_dir = Path(watercooler_mcp.__file__).parent
    package_script = module_dir / "scripts" / "git-credential-watercooler"

    assert package_script.exists(), (
        f"Credential helper should exist in package at {package_script}"
    )
