import os
import shutil
from pathlib import Path

import pytest
from git import Repo

from git.remote import Remote

from watercooler_mcp.git_sync import (
    GitCommandError,
    GitSyncError,
    GitSyncManager,
    _positive_float,
    _positive_int,
)


def init_remote_repo(remote_path: Path) -> Repo:
    remote_path.mkdir(parents=True, exist_ok=True)
    return Repo.init(remote_path, bare=True)


def seed_remote_with_main(remote_path: Path) -> None:
    """Create a bare remote with a seeded main branch.

    This function also sets the remote's HEAD to point to 'main' to ensure
    clones default to 'main' regardless of the system's git default branch setting.
    """
    bare_repo = init_remote_repo(remote_path)
    workdir = remote_path.parent / "seed"
    repo = Repo.init(workdir)
    (workdir / "README.md").write_text("seed\n")
    repo.index.add(["README.md"])
    repo.index.commit("seed")
    repo.git.branch('-M', 'main')
    repo.create_remote('origin', remote_path.as_posix())
    repo.remotes.origin.push('main:main')
    # Set the bare repo's HEAD to point to main (ensures clones default to 'main')
    bare_repo.head.reference = bare_repo.heads['main']
    shutil.rmtree(workdir)


def touch(path: Path, content: str = "data\n") -> None:
    path.write_text(content)


def test_pull_returns_true_when_remote_empty(tmp_path):
    remote = tmp_path / "remote.git"
    init_remote_repo(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
    )

    assert (tmp_path / "threads" / ".git").exists()
    assert mgr.pull() is True


def test_commit_and_push_retries_on_reject(monkeypatch, tmp_path):
    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
    )

    repo = Repo(mgr.local_path)
    touch(mgr.local_path / "file.txt", "update\n")

    call_count = {"push": 0}
    original_push = Remote.push

    def flaky_push(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.repo.working_tree_dir == str(mgr.local_path):
            call_count["push"] += 1
            if call_count["push"] == 1:
                raise GitCommandError("git push", 1, stderr="rejected")
        return original_push(self, *args, **kwargs)

    monkeypatch.setattr(Remote, "push", flaky_push)

    assert mgr.commit_and_push("update") is True
    assert call_count["push"] == 2


def test_commit_and_push_failure_after_retries(monkeypatch, tmp_path):
    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
    )

    repo = Repo(mgr.local_path)
    touch(mgr.local_path / "file.txt", "data\n")

    original_push = Remote.push

    def failing_push(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.repo.working_tree_dir == str(mgr.local_path):
            raise GitCommandError("git push", 1, stderr="reject")
        return original_push(self, *args, **kwargs)

    monkeypatch.setattr(Remote, "push", failing_push)

    assert mgr.commit_and_push("fail") is False
    assert mgr._last_push_error is not None


def test_clone_auto_provisions_missing_repo(monkeypatch, tmp_path):
    remote = tmp_path / "provisioned.git"
    script = tmp_path / "provision.sh"
    script.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        "REMOTE=$1\n"
        "WORK=${REMOTE}.seed\n"
        "mkdir -p \"$WORK\"\n"
        "git init \"$WORK\" >/dev/null 2>&1\n"
        "cd \"$WORK\"\n"
        "git config user.email test@example.com\n"
        "git config user.name Tester\n"
        "touch README.md\n"
        "git add README.md\n"
        "git commit -m seed >/dev/null 2>&1\n"
        "git checkout -b main >/dev/null 2>&1 || git branch -M main >/dev/null 2>&1\n"
        "mkdir -p \"$REMOTE\"\n"
        "git clone --bare \"$WORK\" \"$REMOTE\" >/dev/null 2>&1\n"
        "rm -rf \"$WORK\"\n"
    )
    script.chmod(0o755)

    monkeypatch.setenv("WATERCOOLER_THREADS_AUTO_PROVISION", "1")
    monkeypatch.setenv("WATERCOOLER_THREADS_CREATE_CMD", f"{script} {{repo_url}}")

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
        enable_provision=True,
        threads_slug="org/threads",
        code_repo="org/code",
    )

    assert remote.exists()
    assert (tmp_path / "threads" / ".git").exists()
    assert isinstance(mgr, GitSyncManager)


def test_clone_provision_failure_surfaces_error(monkeypatch, tmp_path):
    remote = tmp_path / "missing.git"
    monkeypatch.setenv("WATERCOOLER_THREADS_AUTO_PROVISION", "1")
    monkeypatch.setenv("WATERCOOLER_THREADS_CREATE_CMD", "exit 1")

    with pytest.raises(GitSyncError):
        GitSyncManager(
            repo_url=remote.as_posix(),
            local_path=tmp_path / "threads",
            ssh_key_path=None,
            enable_provision=True,
            threads_slug="org/threads",
            code_repo="org/code",
        )


def test_commit_and_push_network_failure_aborts(monkeypatch, tmp_path):
    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
    )

    repo = Repo(mgr.local_path)
    touch(mgr.local_path / "file.txt", "change\n")

    original_push = Remote.push

    def network_fail(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.repo.working_tree_dir == str(mgr.local_path):
            raise GitCommandError("git push", 1, stderr="failed to connect to remote")
        return original_push(self, *args, **kwargs)

    monkeypatch.setattr(Remote, "push", network_fail)

    assert mgr.commit_and_push("network") is False
    assert "failed to connect" in (mgr._last_push_error or "")


def test_push_pending_respects_remote_disabled(tmp_path):
    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
        remote_allowed=False,
    )

    touch(mgr.local_path / "file.txt")
    repo = Repo(mgr.local_path)
    repo.git.add('-A')
    repo.git.commit('-m', 'local-change')

    assert mgr.push_pending() is True


# =============================================================================
# SSH BatchMode tests (Codex compatibility)
# =============================================================================


def test_batch_mode_set_for_ssh_url_without_key(tmp_path, monkeypatch):
    """Test BatchMode=yes is set for SSH URLs to prevent MCP server hangs."""
    from watercooler_mcp.git_sync import GitSyncManager

    # Create minimal local path
    local_path = tmp_path / "threads"
    local_path.mkdir()

    # Clear env to avoid interference
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)

    mgr = GitSyncManager(
        repo_url="git@github.com:org/repo-threads.git",
        local_path=local_path,
        ssh_key_path=None,
        remote_allowed=False,  # Skip actual git operations
    )

    # Should have BatchMode=yes in GIT_SSH_COMMAND
    ssh_cmd = mgr._env.get("GIT_SSH_COMMAND", "")
    assert "BatchMode=yes" in ssh_cmd, f"Expected BatchMode=yes in: {ssh_cmd}"


def test_batch_mode_set_for_ssh_url_with_key(tmp_path, monkeypatch):
    """Test BatchMode=yes is set when using explicit SSH key."""
    from watercooler_mcp.git_sync import GitSyncManager

    # Create minimal local path and fake key
    local_path = tmp_path / "threads"
    local_path.mkdir()
    fake_key = tmp_path / "id_rsa"
    fake_key.write_text("fake key")

    # Clear env to avoid interference
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)

    mgr = GitSyncManager(
        repo_url="git@github.com:org/repo-threads.git",
        local_path=local_path,
        ssh_key_path=fake_key,
        remote_allowed=False,  # Skip actual git operations
    )

    # Should have BatchMode=yes in GIT_SSH_COMMAND along with key options
    ssh_cmd = mgr._env.get("GIT_SSH_COMMAND", "")
    assert "BatchMode=yes" in ssh_cmd, f"Expected BatchMode=yes in: {ssh_cmd}"
    assert str(fake_key) in ssh_cmd, f"Expected key path in: {ssh_cmd}"
    assert "IdentitiesOnly=yes" in ssh_cmd, f"Expected IdentitiesOnly=yes in: {ssh_cmd}"


def test_https_url_no_ssh_command(tmp_path, monkeypatch):
    """Test HTTPS URLs don't set GIT_SSH_COMMAND."""
    from watercooler_mcp.git_sync import GitSyncManager

    # Create minimal local path
    local_path = tmp_path / "threads"
    local_path.mkdir()

    # Clear env to avoid interference
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)

    mgr = GitSyncManager(
        repo_url="https://github.com/org/repo-threads.git",
        local_path=local_path,
        ssh_key_path=None,
        remote_allowed=False,  # Skip actual git operations
    )

    # Should NOT have GIT_SSH_COMMAND set for HTTPS
    assert "GIT_SSH_COMMAND" not in mgr._env or "BatchMode" not in mgr._env.get("GIT_SSH_COMMAND", "")


# --- Validation helper tests ---


def test_positive_float_returns_value_when_positive():
    """Test that _positive_float returns the value when it's positive."""
    assert _positive_float(5.0, 1.0) == 5.0
    assert _positive_float(0.1, 1.0) == 0.1
    assert _positive_float(100.5, 1.0) == 100.5


def test_positive_float_returns_default_when_zero_or_negative():
    """Test that _positive_float returns default for zero or negative values."""
    assert _positive_float(0.0, 1.0) == 1.0
    assert _positive_float(-1.0, 1.0) == 1.0
    assert _positive_float(-100.5, 2.5) == 2.5


def test_positive_int_returns_value_when_positive():
    """Test that _positive_int returns the value when it's positive."""
    assert _positive_int(5, 1) == 5
    assert _positive_int(1, 10) == 1
    assert _positive_int(100, 1) == 100


def test_positive_int_returns_default_when_zero_or_negative():
    """Test that _positive_int returns default for zero or negative values."""
    assert _positive_int(0, 1) == 1
    assert _positive_int(-1, 1) == 1
    assert _positive_int(-100, 5) == 5
