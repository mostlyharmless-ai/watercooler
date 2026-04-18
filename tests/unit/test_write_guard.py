"""Unit tests for the write guard (Bug #3, plan v4).

Covers:
- assert_github_backed_threads in src/watercooler/write_guard.py
- WATERCOOLER_ALLOW_LOCAL_ONLY opt-in bypass
- Detection of no-git-repo / no-origin / non-GitHub-origin
- _describe_storage_mode in diagnostic.py (honest storage-mode display)
- The guard is invoked from the shared wrappers _cli_write_with_sync
  (src/watercooler/cli.py:212) and run_with_sync
  (src/watercooler_mcp/middleware.py:299), covering every current
  write command and the seven graph.py tools that bypass the
  thread_write reporting helper.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.write_guard import (
    ENV_ALLOW_LOCAL_ONLY,
    WatercoolerWriteError,
    assert_github_backed_threads,
    _looks_github_hosted,
)


@pytest.fixture(autouse=True)
def _guard_active(monkeypatch):
    """Override the conftest-wide write-guard bypass so these tests
    actually exercise the guard. The module-level autouse fixture in
    ``tests/conftest.py`` sets ``WATERCOOLER_ALLOW_LOCAL_ONLY=1`` for
    every other test — this file needs the opposite."""
    monkeypatch.delenv(ENV_ALLOW_LOCAL_ONLY, raising=False)
    yield


def _make_repo(tmp_path: Path, remote_url: str | None = "https://github.com/example/repo.git") -> Path:
    """Create a minimal fake git repo at tmp_path with optional origin.

    Auto-creates parent directories so callers can pass a not-yet-
    existing path (e.g., a sibling like ``parent/myrepo-threads``).
    """
    gitdir = tmp_path / ".git"
    gitdir.mkdir(parents=True)
    config_lines = ["[core]", "  repositoryformatversion = 0"]
    if remote_url is not None:
        config_lines += [
            '[remote "origin"]',
            f"  url = {remote_url}",
            "  fetch = +refs/heads/*:refs/remotes/origin/*",
        ]
    (gitdir / "config").write_text("\n".join(config_lines) + "\n")
    return tmp_path


def _make_worktree_pointer(tmp_path: Path, gitdir_target: Path) -> Path:
    """Create a worktree whose .git is a file pointing at gitdir_target."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir_target}\n")
    return wt


class TestGitHubHostRecognition:
    """_looks_github_hosted host-matching."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/example/repo.git",
            "git@github.com:example/repo.git",
            "ssh://git@github.com/example/repo.git",
            "https://github.enterprise.example/example/repo.git",  # github enterprise
            "git@github.acme.com:example/repo.git",
        ],
    )
    def test_github_hosts_accepted(self, url):
        assert _looks_github_hosted(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://gitlab.com/example/repo.git",
            "https://bitbucket.org/example/repo.git",
            "git@gitea.example.com:example/repo.git",
            "file:///local/repo",
            "",
        ],
    )
    def test_non_github_hosts_rejected(self, url):
        assert _looks_github_hosted(url) is False


class TestAssertGitHubBackedThreads:
    """End-to-end guard behavior."""

    def test_valid_github_repo_passes(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert_github_backed_threads(repo)  # should not raise

    def test_no_git_repo_raises(self, tmp_path):
        with pytest.raises(WatercoolerWriteError) as exc:
            assert_github_backed_threads(tmp_path)
        msg = str(exc.value)
        assert "no .git found" in msg
        assert "WATERCOOLER_ALLOW_LOCAL_ONLY" in msg

    def test_no_origin_remote_raises(self, tmp_path):
        repo = _make_repo(tmp_path, remote_url=None)
        with pytest.raises(WatercoolerWriteError) as exc:
            assert_github_backed_threads(repo)
        assert "no 'origin' remote configured" in str(exc.value)

    def test_non_github_origin_raises(self, tmp_path):
        repo = _make_repo(tmp_path, remote_url="https://gitlab.com/example/repo.git")
        with pytest.raises(WatercoolerWriteError) as exc:
            assert_github_backed_threads(repo)
        msg = str(exc.value)
        assert "not a GitHub-hosted remote" in msg
        assert "gitlab.com" in msg

    def test_opt_in_env_var_bypasses_all_checks(self, tmp_path, monkeypatch):
        # Not even a git repo — opt-in should still let it through.
        monkeypatch.setenv(ENV_ALLOW_LOCAL_ONLY, "1")
        assert_github_backed_threads(tmp_path)  # should not raise

    def test_opt_in_falsy_values_do_not_bypass(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_ALLOW_LOCAL_ONLY, "0")
        with pytest.raises(WatercoolerWriteError):
            assert_github_backed_threads(tmp_path)
        monkeypatch.setenv(ENV_ALLOW_LOCAL_ONLY, "")
        with pytest.raises(WatercoolerWriteError):
            assert_github_backed_threads(tmp_path)

    def test_worktree_gitdir_pointer_resolved(self, tmp_path):
        """Orphan-branch worktrees have .git as a file pointing at the
        main repo's .git/worktrees/<name> directory. The guard must
        follow the pointer and then read the main repo's origin URL
        via commondir indirection (the worktree gitdir has no
        config, only a commondir pointer)."""
        # Create main repo .git dir
        main_git = tmp_path / "main" / ".git"
        main_git.mkdir(parents=True)
        (main_git / "config").write_text(
            '[core]\n  repositoryformatversion = 0\n'
            '[remote "origin"]\n  url = https://github.com/example/repo.git\n'
        )
        # Create worktree gitdir with commondir pointing at main
        wt_gitdir = main_git / "worktrees" / "mywt"
        wt_gitdir.mkdir(parents=True)
        # relative commondir back to main .git dir
        rel = os.path.relpath(main_git, wt_gitdir)
        (wt_gitdir / "commondir").write_text(rel + "\n")
        # Worktree directory with .git pointer
        wt = _make_worktree_pointer(tmp_path, wt_gitdir)

        # Should resolve origin via the pointer + commondir indirection
        assert_github_backed_threads(wt)  # does not raise

    def test_remediation_message_includes_all_three_resolutions(self, tmp_path):
        with pytest.raises(WatercoolerWriteError) as exc:
            assert_github_backed_threads(tmp_path)
        msg = str(exc.value)
        assert "cd into a git repository" in msg
        assert "WATERCOOLER_DIR=" in msg
        assert "WATERCOOLER_ALLOW_LOCAL_ONLY=1" in msg
        assert "docs/TROUBLESHOOTING.md#local-only-mode" in msg


class TestDescribeStorageMode:
    """_describe_storage_mode in diagnostic.py."""

    def test_local_only_mode(self, tmp_path):
        from watercooler_mcp.tools.diagnostic import _describe_storage_mode

        local = tmp_path / "_local"
        local.mkdir()
        assert _describe_storage_mode(local) == "local-only (no GitHub backing)"

    def test_local_only_with_opt_in_notes_env_var(self, tmp_path, monkeypatch):
        from watercooler_mcp.tools.diagnostic import _describe_storage_mode

        monkeypatch.setenv(ENV_ALLOW_LOCAL_ONLY, "1")
        local = tmp_path / "_local"
        local.mkdir()
        result = _describe_storage_mode(local)
        assert "local-only" in result
        assert "WATERCOOLER_ALLOW_LOCAL_ONLY" in result

    def test_orphan_worktree_mode(self, tmp_path, monkeypatch):
        from watercooler_mcp.tools import diagnostic

        # Fake Path.home() to point at tmp_path so worktree detection
        # targets a controlled location.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        worktree = tmp_path / ".watercooler" / "worktrees" / "myrepo"
        worktree.mkdir(parents=True)
        _make_repo(worktree)
        assert diagnostic._describe_storage_mode(worktree) == "orphan worktree"

    def test_sibling_threads_mode(self, tmp_path):
        from watercooler_mcp.tools.diagnostic import _describe_storage_mode

        parent = tmp_path / "projects"
        parent.mkdir()
        sibling = parent / "myrepo-threads"
        _make_repo(sibling)
        # sibling has a valid .git + github origin, but name ends in -threads
        assert _describe_storage_mode(sibling) == "sibling-threads (legacy)"

    def test_custom_mode_for_other_paths(self, tmp_path):
        from watercooler_mcp.tools.diagnostic import _describe_storage_mode

        custom = tmp_path / "somewhere-unusual"
        _make_repo(custom)
        result = _describe_storage_mode(custom)
        assert result.startswith("custom (")
        assert "somewhere-unusual" in result
