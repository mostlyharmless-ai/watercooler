"""Tests for orphan-branch bootstrap — issue #787.

Covers _select_push_remote (deterministic remote selection) and
_create_orphan_branch (stale-scaffold recovery, dual-remote bootstrap).
"""

import subprocess

import pytest

from watercooler_mcp import config


def _git(cwd, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _init_repo(path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, capture_output=True
    )
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")


@pytest.fixture
def code_repo(tmp_path):
    """A code repo with a bare 'origin' remote and one commit pushed."""
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    repo = tmp_path / "code"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "-u", "origin", "main")
    return repo, remote


class TestSelectPushRemote:
    def test_prefers_origin_over_other_remotes(self, code_repo):
        repo, _ = code_repo
        _git(repo, "remote", "add", "upstream", "https://example.com/u.git")
        assert config._select_push_remote(repo) == "origin"

    def test_lone_non_origin_remote_is_used(self, tmp_path):
        repo = tmp_path / "r"
        _init_repo(repo)
        _git(repo, "remote", "add", "bitbucket", "https://example.com/b.git")
        assert config._select_push_remote(repo) == "bitbucket"

    def test_multiple_non_origin_remotes_are_ambiguous(self, tmp_path):
        repo = tmp_path / "r"
        _init_repo(repo)
        _git(repo, "remote", "add", "bitbucket", "https://example.com/b.git")
        _git(repo, "remote", "add", "gitlab", "https://example.com/g.git")
        assert config._select_push_remote(repo) is None

    def test_no_remotes_returns_none(self, tmp_path):
        repo = tmp_path / "r"
        _init_repo(repo)
        assert config._select_push_remote(repo) is None


class TestCreateOrphanBranch:
    def test_clears_stale_scaffold_then_succeeds(self, code_repo, tmp_path, monkeypatch):
        """The #787 trigger: a stale non-empty wt_path with no .git."""
        repo, remote = code_repo
        monkeypatch.setattr(config, "WORKTREE_BASE", tmp_path / "wt")
        wt = config._worktree_path_for(repo)
        # Pre-create the half-scaffold a prior failed bootstrap would leave.
        (wt / "threads").mkdir(parents=True)
        (wt / "threads" / ".gitkeep").touch()
        assert not (wt / ".git").exists()

        ok = config._create_orphan_branch(repo)

        assert ok is True
        assert (wt / ".git").exists(), "worktree must be bound to git"
        branches = subprocess.run(
            ["git", "-C", str(remote), "branch", "--list", "watercooler/threads"],
            capture_output=True, text=True,
        ).stdout
        assert "watercooler/threads" in branches

    def test_dual_remote_bootstrap_targets_origin(self, code_repo, tmp_path, monkeypatch):
        """A second remote (the issue's reported condition) must not break it."""
        repo, remote = code_repo
        upstream = tmp_path / "upstream.git"
        subprocess.run(
            ["git", "init", "--bare", str(upstream)], check=True, capture_output=True
        )
        _git(repo, "remote", "add", "upstream", str(upstream))
        monkeypatch.setattr(config, "WORKTREE_BASE", tmp_path / "wt")

        ok = config._create_orphan_branch(repo)

        assert ok is True
        assert (config._worktree_path_for(repo) / ".git").exists()
        # Pushed to origin, not upstream.
        origin_branches = subprocess.run(
            ["git", "-C", str(remote), "branch", "--list", "watercooler/threads"],
            capture_output=True, text=True,
        ).stdout
        assert "watercooler/threads" in origin_branches
        upstream_branches = subprocess.run(
            ["git", "-C", str(upstream), "branch", "--list", "watercooler/threads"],
            capture_output=True, text=True,
        ).stdout
        assert "watercooler/threads" not in upstream_branches
