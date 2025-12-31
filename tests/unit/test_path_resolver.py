"""Tests for watercooler.path_resolver module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.path_resolver import (
    GitInfo,
    discover_git_info,
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
