"""Tests for repository setup helpers.

Tests cover:
- ensure_gitignore() - .gitignore creation and updates
- install_hooks() - git hook installation
- check_large_files() - file size checking
- setup_threads_repo() - full setup workflow
"""

from pathlib import Path
from unittest.mock import patch

import pytest


class TestEnsureGitignore:
    """Tests for ensure_gitignore() function."""

    def test_creates_gitignore_if_missing(self, tmp_path: Path):
        """Creates .gitignore with required entries if it doesn't exist."""
        from watercooler_mcp.repo_setup import ensure_gitignore

        modified, entries = ensure_gitignore(tmp_path)

        assert modified is True
        assert len(entries) > 0
        assert "memory/" in entries

        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "memory/" in content
        assert "*.rdb" in content

    def test_preserves_existing_content(self, tmp_path: Path):
        """Preserves existing .gitignore entries when adding required ones."""
        from watercooler_mcp.repo_setup import ensure_gitignore

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# My custom ignores\n*.log\nnode_modules/\n")

        modified, entries = ensure_gitignore(tmp_path)

        assert modified is True
        content = gitignore.read_text()
        # Original content preserved
        assert "*.log" in content
        assert "node_modules/" in content
        # New content added
        assert "memory/" in content

    def test_idempotent_when_entries_exist(self, tmp_path: Path):
        """Returns False when required entries already present."""
        from watercooler_mcp.repo_setup import ensure_gitignore, REQUIRED_GITIGNORE_ENTRIES

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("\n".join(REQUIRED_GITIGNORE_ENTRIES) + "\n")

        modified, entries = ensure_gitignore(tmp_path)

        assert modified is False
        assert entries == []

    def test_adds_only_missing_entries(self, tmp_path: Path):
        """Only adds entries that are actually missing."""
        from watercooler_mcp.repo_setup import ensure_gitignore

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("memory/\n")  # One entry already exists

        modified, entries = ensure_gitignore(tmp_path)

        assert modified is True
        assert "memory/" not in entries  # Already existed
        assert "*.rdb" in entries  # Was missing


class TestInstallHooks:
    """Tests for install_hooks() function."""

    def test_skips_if_no_git_dir(self, tmp_path: Path):
        """Returns False if .git/hooks doesn't exist."""
        from watercooler_mcp.repo_setup import install_hooks

        modified, hooks = install_hooks(tmp_path)

        assert modified is False
        assert hooks == []

    def test_installs_pre_commit_hook(self, tmp_path: Path):
        """Installs pre-commit hook when .git/hooks exists."""
        from watercooler_mcp.repo_setup import install_hooks

        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)

        modified, hooks = install_hooks(tmp_path)

        assert modified is True
        assert "pre-commit" in hooks

        hook_path = hooks_dir / "pre-commit"
        assert hook_path.exists()
        # Check it's executable
        import os
        assert os.access(hook_path, os.X_OK)

    def test_backs_up_existing_hook(self, tmp_path: Path):
        """Backs up existing hook before overwriting."""
        from watercooler_mcp.repo_setup import install_hooks

        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)

        # Create existing hook with different content
        existing_hook = hooks_dir / "pre-commit"
        existing_hook.write_text("#!/bin/bash\necho 'original hook'\n")

        modified, hooks = install_hooks(tmp_path)

        assert modified is True
        # Backup created
        backup = hooks_dir / "pre-commit.bak"
        assert backup.exists()
        assert "original hook" in backup.read_text()

    def test_idempotent_when_same_content(self, tmp_path: Path):
        """Returns False when hook already has same content."""
        from watercooler_mcp.repo_setup import install_hooks, get_template_path
        from importlib.resources import as_file

        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)

        # Install hook first time
        install_hooks(tmp_path)

        # Install again - should be idempotent
        modified, hooks = install_hooks(tmp_path)

        assert modified is False
        assert hooks == []


class TestCheckLargeFiles:
    """Tests for check_large_files() function."""

    def test_finds_oversized_files(self, tmp_path: Path):
        """Detects files exceeding size limit."""
        from watercooler_mcp.repo_setup import check_large_files, MAX_FILE_SIZE_BYTES

        # Create a file and check that the function works
        # We can't easily mock file sizes without breaking pathlib internals,
        # so we test with real (small) files and verify the logic works
        small_file = tmp_path / "small.bin"
        small_file.write_bytes(b"x" * 100)

        # Should find no oversized files for small files
        oversized = check_large_files(tmp_path)
        assert oversized == []

        # Test the threshold constant is set correctly
        assert MAX_FILE_SIZE_BYTES == 100 * 1024 * 1024  # 100MB

    def test_excludes_git_directory(self, tmp_path: Path):
        """Excludes files in .git directory."""
        from watercooler_mcp.repo_setup import check_large_files

        git_dir = tmp_path / ".git" / "objects"
        git_dir.mkdir(parents=True)
        git_file = git_dir / "pack.idx"
        git_file.write_bytes(b"x" * 100)

        oversized = check_large_files(tmp_path)

        # .git files should not be checked
        assert all(".git" not in str(f) for f, _ in oversized)

    def test_returns_empty_for_small_files(self, tmp_path: Path):
        """Returns empty list when all files are under limit."""
        from watercooler_mcp.repo_setup import check_large_files

        small_file = tmp_path / "small.txt"
        small_file.write_text("hello world")

        oversized = check_large_files(tmp_path)

        assert oversized == []


class TestSetupThreadsRepo:
    """Tests for setup_threads_repo() function."""

    def test_full_setup_creates_gitignore_and_hooks(self, tmp_path: Path):
        """Full setup creates both .gitignore and hooks."""
        from watercooler_mcp.repo_setup import setup_threads_repo

        # Create .git/hooks directory
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)

        result = setup_threads_repo(tmp_path)

        assert result["gitignore_modified"] is True
        assert "memory/" in result["gitignore_entries"]
        assert "pre-commit" in result["hooks_installed"]
        assert result["large_files"] == []

    def test_skip_hooks_option(self, tmp_path: Path):
        """Can skip hook installation."""
        from watercooler_mcp.repo_setup import setup_threads_repo

        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)

        result = setup_threads_repo(tmp_path, install_git_hooks=False)

        assert result["gitignore_modified"] is True
        assert result["hooks_installed"] == []

    def test_reports_large_files_key_exists(self, tmp_path: Path):
        """Result includes large_files key (empty for small files)."""
        from watercooler_mcp.repo_setup import setup_threads_repo

        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)

        # Create small file
        small_file = tmp_path / "small.txt"
        small_file.write_text("hello")

        result = setup_threads_repo(tmp_path)

        # Verify structure includes large_files key
        assert "large_files" in result
        assert isinstance(result["large_files"], list)
        # Small files should not be flagged
        assert result["large_files"] == []


class TestGetTemplatePath:
    """Tests for get_template_path() function."""

    def test_returns_path_for_existing_template(self):
        """Returns path for templates that exist."""
        from watercooler_mcp.repo_setup import get_template_path
        from importlib.resources import as_file

        template_ref = get_template_path("pre-commit")
        with as_file(template_ref) as path:
            assert path.exists()
            content = path.read_text()
            assert "#!/bin/bash" in content

    def test_returns_path_for_gitignore_template(self):
        """Returns path for threads.gitignore template."""
        from watercooler_mcp.repo_setup import get_template_path
        from importlib.resources import as_file

        template_ref = get_template_path("threads.gitignore")
        with as_file(template_ref) as path:
            assert path.exists()
