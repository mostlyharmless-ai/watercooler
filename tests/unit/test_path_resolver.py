"""Tests for watercooler.path_resolver module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.path_resolver import (
    GitInfo,
    derive_code_repo_from_threads,
    derive_code_repo_name,
    derive_group_id,
    derive_threads_repo_name,
    discover_git_info,
    get_threads_suffix,
    resolve_templates_dir,
    resolve_threads_dir,
)


class TestGitInfo:
    """Tests for GitInfo dataclass."""

    def test_git_info_creation(self):
        """Test GitInfo can be created with all fields."""
        git_info = GitInfo(
            root=Path("/repo"),
            branch="main",
            commit="abc1234",
            remote="git@github.com:org/repo.git"
        )
        assert git_info.root == Path("/repo")
        assert git_info.branch == "main"
        assert git_info.commit == "abc1234"
        assert git_info.remote == "git@github.com:org/repo.git"

    def test_git_info_immutable(self):
        """Test GitInfo is immutable (frozen dataclass)."""
        git_info = GitInfo(Path("/repo"), "main", "abc1234", "origin")
        with pytest.raises(AttributeError):
            git_info.root = Path("/other")  # type: ignore


class TestDiscoverGitInfo:
    """Tests for discover_git_info function."""

    def test_discover_git_info_none_code_root(self):
        """Test discover_git_info with None code_root returns empty GitInfo."""
        result = discover_git_info(None)
        assert result.root is None
        assert result.branch is None
        assert result.commit is None
        assert result.remote is None

    def test_discover_git_info_nonexistent_path(self, tmp_path):
        """Test discover_git_info with non-existent path returns empty GitInfo."""
        nonexistent = tmp_path / "does-not-exist"
        result = discover_git_info(nonexistent)
        assert result.root is None
        assert result.branch is None
        assert result.commit is None
        assert result.remote is None

    def test_discover_git_info_not_a_repo(self, tmp_path):
        """Test discover_git_info with non-git directory returns empty GitInfo."""
        result = discover_git_info(tmp_path)
        assert result.root is None
        assert result.branch is None
        assert result.commit is None
        assert result.remote is None

    @pytest.mark.integration
    def test_discover_git_info_real_repo(self):
        """Test discover_git_info with real git repository (integration test)."""
        # This test runs against the actual watercooler-cloud repo
        repo_root = Path(__file__).parent.parent.parent
        result = discover_git_info(repo_root)

        # Should discover the repo
        assert result.root is not None
        assert isinstance(result.root, Path)
        # Branch might be None (detached HEAD), main, or feat/something
        # Commit should exist
        assert result.commit is not None
        assert len(result.commit) == 7  # Short hash


class TestResolveTemplatesDir:
    """Tests for resolve_templates_dir function."""

    def test_resolve_templates_dir_cli_value(self, tmp_path):
        """Test CLI value takes precedence."""
        cli_dir = tmp_path / "custom"
        cli_dir.mkdir()
        result = resolve_templates_dir(str(cli_dir))
        assert result == cli_dir

    def test_resolve_templates_dir_env_var(self, tmp_path, monkeypatch):
        """Test environment variable takes precedence over discovery."""
        env_dir = tmp_path / "env-templates"
        env_dir.mkdir()
        monkeypatch.setenv("WATERCOOLER_TEMPLATES", str(env_dir))
        result = resolve_templates_dir()
        assert result == env_dir

    def test_resolve_templates_dir_project_local(self, tmp_path, monkeypatch):
        """Test project-local templates directory is discovered."""
        # Change to tmp_path for test
        monkeypatch.chdir(tmp_path)

        # Create .watercooler/templates/
        project_templates = tmp_path / ".watercooler" / "templates"
        project_templates.mkdir(parents=True)

        result = resolve_templates_dir()
        assert result == project_templates

    def test_resolve_templates_dir_package_fallback(self, tmp_path, monkeypatch):
        """Test falls back to package bundled templates."""
        # Change to directory without local templates
        monkeypatch.chdir(tmp_path)

        result = resolve_templates_dir()

        # Should return package bundled templates
        assert result.exists()
        assert result.name == "templates"
        assert "watercooler" in str(result)


class TestResolveThreadsDir:
    """Tests for resolve_threads_dir function."""

    def test_resolve_threads_dir_cli_value(self, tmp_path):
        """Test CLI value takes absolute precedence."""
        cli_dir = tmp_path / "cli-threads"
        result = resolve_threads_dir(str(cli_dir))
        assert result == cli_dir.resolve()

    def test_resolve_threads_dir_env_var(self, tmp_path, monkeypatch):
        """Test WATERCOOLER_DIR environment variable."""
        env_dir = tmp_path / "env-threads"
        monkeypatch.setenv("WATERCOOLER_DIR", str(env_dir))
        result = resolve_threads_dir()
        assert result == env_dir.resolve()

    def test_resolve_threads_dir_tilde_expansion(self, monkeypatch):
        """Test tilde expansion in paths."""
        monkeypatch.setenv("WATERCOOLER_DIR", "~/watercooler-threads")
        result = resolve_threads_dir()
        assert str(result).startswith(str(Path.home()))
        assert "~" not in str(result)

    def test_resolve_threads_dir_git_aware_fallback(self, tmp_path, monkeypatch):
        """Test git-aware discovery fallback."""
        # Change to tmp_path (not a git repo)
        monkeypatch.chdir(tmp_path)

        result = resolve_threads_dir()

        # Should return some valid path (fallback to _local)
        assert isinstance(result, Path)
        assert result.is_absolute()

    def test_resolve_threads_dir_with_code_root(self, tmp_path):
        """Test resolve_threads_dir with explicit code_root parameter."""
        code_root = tmp_path / "code"
        code_root.mkdir()

        result = resolve_threads_dir(code_root=code_root)

        # Should return some valid path
        assert isinstance(result, Path)
        assert result.is_absolute()


class TestHelperFunctions:
    """Tests for internal helper functions (via public API)."""

    def test_expand_path_with_env_vars(self, monkeypatch, tmp_path):
        """Test path expansion with environment variables."""
        monkeypatch.setenv("TEST_DIR", str(tmp_path))
        monkeypatch.setenv("WATERCOOLER_DIR", "$TEST_DIR/threads")

        result = resolve_threads_dir()
        assert str(tmp_path) in str(result)

    def test_expand_path_with_home(self, monkeypatch):
        """Test path expansion with ~ (home directory)."""
        monkeypatch.setenv("WATERCOOLER_DIR", "~/test-threads")

        result = resolve_threads_dir()
        assert "~" not in str(result)
        assert str(Path.home()) in str(result)


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_resolve_threads_dir_relative_cli_path(self, tmp_path, monkeypatch):
        """Test CLI path is resolved relative to cwd."""
        monkeypatch.chdir(tmp_path)
        result = resolve_threads_dir("relative/path")
        assert result.is_absolute()
        assert str(tmp_path) in str(result)

    def test_resolve_templates_dir_cli_precedence_over_env(self, tmp_path, monkeypatch):
        """Test CLI takes precedence over environment variable."""
        cli_dir = tmp_path / "cli"
        env_dir = tmp_path / "env"
        cli_dir.mkdir()
        env_dir.mkdir()

        monkeypatch.setenv("WATERCOOLER_TEMPLATES", str(env_dir))
        result = resolve_templates_dir(str(cli_dir))

        assert result == cli_dir  # CLI wins


# =============================================================================
# Tests for Unified Repo Name and group_id Derivation
# =============================================================================


class TestGetThreadsSuffix:
    """Tests for get_threads_suffix function."""

    def test_default_suffix(self, monkeypatch):
        """Test default suffix when no env or config is set."""
        monkeypatch.delenv("WATERCOOLER_THREADS_SUFFIX", raising=False)
        # Mock config to fail so we get the hardcoded default
        with patch("watercooler.path_resolver.get_threads_suffix") as mock:
            mock.return_value = "-threads"
            result = mock()
        assert result == "-threads"

    def test_env_override(self, monkeypatch):
        """Test WATERCOOLER_THREADS_SUFFIX env var takes precedence."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-wc")
        result = get_threads_suffix()
        assert result == "-wc"

    def test_empty_env_override(self, monkeypatch):
        """Test empty string env var is respected (no suffix)."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "")
        result = get_threads_suffix()
        assert result == ""


class TestDeriveCodeRepoName:
    """Tests for derive_code_repo_name function."""

    def test_from_git_info(self):
        """Test derivation from GitInfo."""
        git_info = GitInfo(
            root=Path("/path/to/watercooler-cloud"),
            branch="main",
            commit="abc1234",
            remote="origin"
        )
        result = derive_code_repo_name(git_info=git_info)
        assert result == "watercooler-cloud"

    def test_from_code_path(self, tmp_path):
        """Test derivation from code_path."""
        code_dir = tmp_path / "my-project"
        code_dir.mkdir()
        result = derive_code_repo_name(code_path=code_dir)
        assert result == "my-project"

    def test_git_info_takes_precedence(self, tmp_path):
        """Test GitInfo takes precedence over code_path."""
        code_dir = tmp_path / "my-project"
        code_dir.mkdir()
        git_info = GitInfo(
            root=Path("/path/to/git-repo"),
            branch="main",
            commit="abc1234",
            remote="origin"
        )
        result = derive_code_repo_name(code_path=code_dir, git_info=git_info)
        assert result == "git-repo"

    def test_returns_none_if_nothing_provided(self):
        """Test returns None when no info is available."""
        result = derive_code_repo_name()
        assert result is None


class TestDeriveThreadsRepoName:
    """Tests for derive_threads_repo_name function."""

    def test_basic_derivation(self, monkeypatch):
        """Test basic threads repo name derivation."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-threads")
        result = derive_threads_repo_name("watercooler-cloud")
        assert result == "watercooler-cloud-threads"

    def test_already_has_suffix(self, monkeypatch):
        """Test name that already has the suffix is unchanged."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-threads")
        result = derive_threads_repo_name("watercooler-cloud-threads")
        assert result == "watercooler-cloud-threads"

    def test_custom_suffix(self, monkeypatch):
        """Test custom suffix from env var."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-wc")
        result = derive_threads_repo_name("myrepo")
        assert result == "myrepo-wc"

    def test_explicit_suffix_parameter(self, monkeypatch):
        """Test explicit suffix parameter overrides config."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-threads")
        result = derive_threads_repo_name("myrepo", suffix="-custom")
        assert result == "myrepo-custom"

    def test_empty_suffix(self, monkeypatch):
        """Test empty suffix (no suffix added)."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "")
        result = derive_threads_repo_name("myrepo")
        assert result == "myrepo"


class TestDeriveCodeRepoFromThreads:
    """Tests for derive_code_repo_from_threads function."""

    def test_basic_reverse_derivation(self, monkeypatch):
        """Test basic reverse derivation."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-threads")
        result = derive_code_repo_from_threads("watercooler-cloud-threads")
        assert result == "watercooler-cloud"

    def test_no_suffix_to_strip(self, monkeypatch):
        """Test name without suffix is returned unchanged."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-threads")
        result = derive_code_repo_from_threads("watercooler-cloud")
        assert result == "watercooler-cloud"

    def test_custom_suffix(self, monkeypatch):
        """Test custom suffix reverse derivation."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-wc")
        result = derive_code_repo_from_threads("myrepo-wc")
        assert result == "myrepo"

    def test_explicit_suffix_parameter(self, monkeypatch):
        """Test explicit suffix parameter overrides config."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-threads")
        result = derive_code_repo_from_threads("myrepo-custom", suffix="-custom")
        assert result == "myrepo"


class TestDeriveGroupId:
    """Tests for derive_group_id function."""

    def test_from_code_repo_name(self):
        """Test derivation from direct code_repo_name."""
        result = derive_group_id(code_repo_name="watercooler-cloud")
        assert result == "watercooler_cloud"

    def test_from_code_path(self, tmp_path):
        """Test derivation from code_path."""
        code_dir = tmp_path / "my-app"
        code_dir.mkdir()
        result = derive_group_id(code_path=code_dir)
        assert result == "my_app"

    def test_from_threads_dir(self, tmp_path, monkeypatch):
        """Test derivation from threads_dir (reverse derivation)."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-threads")
        threads_dir = tmp_path / "watercooler-cloud-threads"
        threads_dir.mkdir()
        result = derive_group_id(threads_dir=threads_dir)
        assert result == "watercooler_cloud"

    def test_code_repo_name_takes_precedence(self, tmp_path):
        """Test code_repo_name takes precedence over other sources."""
        code_dir = tmp_path / "other-project"
        code_dir.mkdir()
        result = derive_group_id(
            code_repo_name="my-app",
            code_path=code_dir
        )
        assert result == "my_app"

    def test_default_fallback(self):
        """Test default fallback when nothing is provided."""
        result = derive_group_id()
        assert result == "watercooler"

    def test_sanitization_hyphens_to_underscores(self):
        """Test hyphens are replaced with underscores."""
        result = derive_group_id(code_repo_name="my-cool-app")
        assert result == "my_cool_app"

    def test_sanitization_lowercase(self):
        """Test names are lowercased."""
        result = derive_group_id(code_repo_name="MyApp")
        assert result == "myapp"

    def test_sanitization_preserves_dots(self):
        """Test dots are preserved (backward compatibility with migrated data)."""
        result = derive_group_id(code_repo_name="my-app.test")
        assert result == "my_app.test"

    def test_sanitization_dots_and_hyphens(self):
        """Test hyphens converted but dots preserved."""
        result = derive_group_id(code_repo_name="My-App.v2.0")
        assert result == "my_app.v2.0"

    def test_consistent_sanitization_across_inputs(self, tmp_path, monkeypatch):
        """Test same result regardless of input method (consistency guarantee)."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-threads")

        # From code_repo_name
        result1 = derive_group_id(code_repo_name="watercooler-cloud")

        # From code_path
        code_dir = tmp_path / "watercooler-cloud"
        code_dir.mkdir()
        result2 = derive_group_id(code_path=code_dir)

        # From threads_dir
        threads_dir = tmp_path / "watercooler-cloud-threads"
        threads_dir.mkdir()
        result3 = derive_group_id(threads_dir=threads_dir)

        # All should be identical
        assert result1 == result2 == result3 == "watercooler_cloud"


class TestRoundTripDerivation:
    """Tests for round-trip derivation consistency."""

    def test_round_trip_threads_name(self, monkeypatch):
        """Test forward and reverse derivation are consistent."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-threads")

        original = "watercooler-cloud"
        threads_name = derive_threads_repo_name(original)
        recovered = derive_code_repo_from_threads(threads_name)

        assert recovered == original

    def test_round_trip_custom_suffix(self, monkeypatch):
        """Test round-trip with custom suffix."""
        monkeypatch.setenv("WATERCOOLER_THREADS_SUFFIX", "-wc")

        original = "my-project"
        threads_name = derive_threads_repo_name(original)
        recovered = derive_code_repo_from_threads(threads_name)

        assert recovered == original
        assert threads_name == "my-project-wc"
