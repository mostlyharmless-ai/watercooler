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
    _find_main_branch,
    _detect_behind_main_divergence,
    _rebase_branch_onto,
    _positive_float,
    _positive_int,
    BranchSyncResult,
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


def test_ensure_branch_creates_branch_when_missing(tmp_path):
    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
    )

    assert mgr.ensure_branch("feature/test") is True
    repo = Repo(mgr.local_path)
    assert repo.active_branch.name == "feature/test"


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


# Note on ambiguous upstream testing:
# The ambiguous upstream scenario (git_sync.py:761-795) is difficult to test in isolation
# because it requires:
# 1. Multiple remotes with the same branch name (e.g., origin/main and fork/main)
# 2. Git's rebase logic to fail with "cannot rebase onto multiple branches"
# 3. Complex git repository state that's hard to reproduce in unit tests
#
# The error handling has been reviewed and includes:
# - Catching both AttributeError and TypeError when calling tracking_branch()
# - Logging warnings when tracking branch cannot be determined
# - Proper error propagation when both initial pull and fallback fail
# - Fallback to explicit remote/branch specification
#
# For verification of this logic, see:
# - Code review comments on PR #21
# - Manual testing in development environments with multiple remotes
# - Integration testing in real-world scenarios


# ============================================================================
# Branch Divergence Detection and Recovery Tests
# ============================================================================

from watercooler_mcp.git_sync import (
    BranchDivergenceInfo,
    BranchSyncResult,
    _detect_branch_divergence,
    sync_branch_history,
    validate_branch_pairing,
)


def create_diverged_repos(tmp_path):
    """Create a scenario with diverged branches."""
    # Create remote
    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    # Create threads repo
    threads_path = tmp_path / "threads"
    threads_repo = Repo.clone_from(remote.as_posix(), threads_path, branch="main")
    threads_repo.config_writer().set_value("user", "email", "test@example.com").release()
    threads_repo.config_writer().set_value("user", "name", "Tester").release()
    # Ensure we're on main branch
    if threads_repo.active_branch.name != "main":
        threads_repo.git.checkout('-B', 'main', 'origin/main')
    # Set up tracking
    threads_repo.git.branch('--set-upstream-to=origin/main', 'main')

    # Create code repo (doesn't need to be real, just for testing)
    code_path = tmp_path / "code"
    code_repo = Repo.init(code_path)
    code_repo.config_writer().set_value("user", "email", "test@example.com").release()
    code_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(code_path / "code.py", "# code")
    code_repo.index.add(["code.py"])
    code_repo.index.commit("initial code")
    code_repo.git.branch('-M', 'main')

    return remote, threads_path, code_path


def test_detect_divergence_in_sync(tmp_path):
    """Test divergence detection when branches are in sync."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)

    threads_repo = Repo(threads_path)
    code_repo = Repo(code_path)

    info = _detect_branch_divergence(code_repo, threads_repo, "main", "main")

    # Should not be diverged since we just cloned
    assert not info.diverged
    assert info.commits_ahead == 0
    assert info.commits_behind == 0


def test_detect_divergence_ahead_of_origin(tmp_path):
    """Test divergence detection when local is ahead."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)

    threads_repo = Repo(threads_path)
    code_repo = Repo(code_path)

    # Add a local commit
    touch(threads_path / "local.md", "local change")
    threads_repo.index.add(["local.md"])
    threads_repo.index.commit("local commit")

    info = _detect_branch_divergence(code_repo, threads_repo, "main", "main")

    # Should be ahead but not diverged
    assert not info.diverged
    assert info.commits_ahead == 1
    assert info.commits_behind == 0


def test_detect_divergence_behind_origin(tmp_path):
    """Test divergence detection when local is behind."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)

    threads_repo = Repo(threads_path)
    code_repo = Repo(code_path)

    # Push a change to remote from another clone
    other_clone = tmp_path / "other"
    other_repo = Repo.clone_from(remote.as_posix(), other_clone, branch="main")
    other_repo.config_writer().set_value("user", "email", "test@example.com").release()
    other_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(other_clone / "other.md", "other change")
    other_repo.index.add(["other.md"])
    other_repo.index.commit("other commit")
    other_repo.remotes.origin.push("main")

    # Fetch in our threads repo
    threads_repo.remotes.origin.fetch()

    info = _detect_branch_divergence(code_repo, threads_repo, "main", "main")

    # Should be behind
    assert not info.diverged
    assert info.commits_ahead == 0
    assert info.commits_behind == 1


def test_detect_divergence_actually_diverged(tmp_path):
    """Test divergence detection when branches have truly diverged."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)

    threads_repo = Repo(threads_path)
    code_repo = Repo(code_path)

    # Add a local commit first
    touch(threads_path / "local.md", "local change")
    threads_repo.index.add(["local.md"])
    threads_repo.index.commit("local commit")

    # Push a different change to remote from another clone
    other_clone = tmp_path / "other"
    other_repo = Repo.clone_from(remote.as_posix(), other_clone, branch="main")
    other_repo.config_writer().set_value("user", "email", "test@example.com").release()
    other_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(other_clone / "other.md", "other change")
    other_repo.index.add(["other.md"])
    other_repo.index.commit("other commit")
    other_repo.remotes.origin.push("main")

    # Fetch in our threads repo
    threads_repo.remotes.origin.fetch()

    info = _detect_branch_divergence(code_repo, threads_repo, "main", "main")

    # Should be diverged - both ahead and behind
    assert info.diverged
    assert info.commits_ahead == 1
    assert info.commits_behind == 1
    assert info.needs_rebase


def test_sync_branch_history_in_sync(tmp_path):
    """Test sync_branch_history when already in sync."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)

    result = sync_branch_history(threads_path, "main")

    assert result.success
    assert result.action_taken == "no_action"
    assert not result.needs_manual_resolution


def test_sync_branch_history_fast_forward(tmp_path):
    """Test sync_branch_history with fast-forward case."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)

    # Push a change to remote from another clone
    other_clone = tmp_path / "other"
    other_repo = Repo.clone_from(remote.as_posix(), other_clone, branch="main")
    other_repo.config_writer().set_value("user", "email", "test@example.com").release()
    other_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(other_clone / "other.md", "other change")
    other_repo.index.add(["other.md"])
    other_repo.index.commit("other commit")
    other_repo.remotes.origin.push("main")

    result = sync_branch_history(threads_path, "main")

    assert result.success
    assert result.action_taken == "fast_forward"
    assert not result.needs_manual_resolution


def test_sync_branch_history_push_local(tmp_path):
    """Test sync_branch_history when local has unpushed commits."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)
    threads_repo = Repo(threads_path)

    # Add a local commit
    touch(threads_path / "local.md", "local change")
    threads_repo.index.add(["local.md"])
    threads_repo.index.commit("local commit")

    result = sync_branch_history(threads_path, "main")

    assert result.success
    assert result.action_taken == "push"
    assert result.commits_preserved == 1


def test_sync_branch_history_rebase_diverged(tmp_path):
    """Test sync_branch_history with rebase strategy on diverged branches."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)
    threads_repo = Repo(threads_path)

    # Add a local commit first
    touch(threads_path / "local.md", "local change")
    threads_repo.index.add(["local.md"])
    threads_repo.index.commit("local commit")

    # Push a different change to remote from another clone
    other_clone = tmp_path / "other"
    other_repo = Repo.clone_from(remote.as_posix(), other_clone, branch="main")
    other_repo.config_writer().set_value("user", "email", "test@example.com").release()
    other_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(other_clone / "other.md", "other change")
    other_repo.index.add(["other.md"])
    other_repo.index.commit("other commit")
    other_repo.remotes.origin.push("main")

    # Now sync with rebase (no force, so won't push)
    result = sync_branch_history(threads_path, "main", strategy="rebase", force=False)

    assert result.success
    assert result.action_taken == "rebased"
    assert result.commits_preserved == 1
    assert result.needs_manual_resolution  # needs force=True to push


def test_sync_branch_history_rebase_with_force_push(tmp_path):
    """Test sync_branch_history with rebase and force push."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)
    threads_repo = Repo(threads_path)

    # Add a local commit first
    touch(threads_path / "local.md", "local change")
    threads_repo.index.add(["local.md"])
    threads_repo.index.commit("local commit")

    # Push a different change to remote from another clone
    other_clone = tmp_path / "other"
    other_repo = Repo.clone_from(remote.as_posix(), other_clone, branch="main")
    other_repo.config_writer().set_value("user", "email", "test@example.com").release()
    other_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(other_clone / "other.md", "other change")
    other_repo.index.add(["other.md"])
    other_repo.index.commit("other commit")
    other_repo.remotes.origin.push("main")

    # Now sync with rebase and force
    result = sync_branch_history(threads_path, "main", strategy="rebase", force=True)

    assert result.success
    assert result.action_taken == "rebased"
    assert result.commits_preserved == 1
    assert not result.needs_manual_resolution


def test_validate_branch_pairing_with_history_check(tmp_path):
    """Test validate_branch_pairing with check_history enabled."""
    remote, threads_path, code_path = create_diverged_repos(tmp_path)
    threads_repo = Repo(threads_path)

    # Add a local commit first
    touch(threads_path / "local.md", "local change")
    threads_repo.index.add(["local.md"])
    threads_repo.index.commit("local commit")

    # Push a different change to remote from another clone
    other_clone = tmp_path / "other"
    other_repo = Repo.clone_from(remote.as_posix(), other_clone, branch="main")
    other_repo.config_writer().set_value("user", "email", "test@example.com").release()
    other_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(other_clone / "other.md", "other change")
    other_repo.index.add(["other.md"])
    other_repo.index.commit("other commit")
    other_repo.remotes.origin.push("main")

    # Fetch in our threads repo
    threads_repo.remotes.origin.fetch()

    # Validate with history check
    result = validate_branch_pairing(
        code_repo=code_path,
        threads_repo=threads_path,
        strict=True,
        check_history=True,
    )

    # Should detect the divergence
    assert not result.valid
    assert any(m.type == "branch_history_diverged" for m in result.mismatches)
    assert len(result.warnings) > 0  # Should have divergence details in warnings


# === Tests for _find_main_branch ===

def test_find_main_branch_with_main(tmp_path):
    """Test _find_main_branch finds 'main' branch."""
    repo = Repo.init(tmp_path / "repo")
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    repo.config_writer().set_value("user", "name", "Tester").release()
    touch(tmp_path / "repo" / "README.md", "test")
    repo.index.add(["README.md"])
    repo.index.commit("initial")
    repo.git.branch("-M", "main")

    result = _find_main_branch(repo)
    assert result == "main"


def test_find_main_branch_with_master(tmp_path):
    """Test _find_main_branch finds 'master' branch when main doesn't exist."""
    repo = Repo.init(tmp_path / "repo")
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    repo.config_writer().set_value("user", "name", "Tester").release()
    touch(tmp_path / "repo" / "README.md", "test")
    repo.index.add(["README.md"])
    repo.index.commit("initial")
    # Default branch is typically 'master' after first commit if not renamed
    # But modern git may default to 'main', so let's explicitly set it
    repo.git.branch("-M", "master")

    result = _find_main_branch(repo)
    assert result == "master"


def test_find_main_branch_returns_none_when_missing(tmp_path):
    """Test _find_main_branch returns None when neither main nor master exists."""
    repo = Repo.init(tmp_path / "repo")
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    repo.config_writer().set_value("user", "name", "Tester").release()
    touch(tmp_path / "repo" / "README.md", "test")
    repo.index.add(["README.md"])
    repo.index.commit("initial")
    # Rename to something else
    repo.git.branch("-M", "develop")

    result = _find_main_branch(repo)
    assert result is None


# === Tests for _detect_behind_main_divergence ===

def test_detect_behind_main_divergence_when_threads_behind(tmp_path):
    """Test detection when threads/staging is behind threads/main but code IS synced with main.

    Real scenario: Code branch was merged to main (so code/staging matches code/main),
    but threads/staging was NOT rebased onto threads/main, so it's missing the closure entry.
    """
    # Setup code repo with main branch
    code_path = tmp_path / "code"
    code_repo = Repo.init(code_path)
    code_repo.config_writer().set_value("user", "email", "test@example.com").release()
    code_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(code_path / "code.py", "initial")
    code_repo.index.add(["code.py"])
    code_repo.index.commit("initial code")
    code_repo.git.branch("-M", "main")

    # Create staging branch AT SAME COMMIT as main (simulates merged state)
    # In real workflow: staging work was merged to main, staging now equals main
    code_repo.git.checkout("-b", "staging")
    # code/staging is now 0 ahead, 0 behind main - this is "synced"

    # Setup threads repo where staging IS behind main
    threads_path = tmp_path / "threads"
    threads_repo = Repo.init(threads_path)
    threads_repo.config_writer().set_value("user", "email", "test@example.com").release()
    threads_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(threads_path / "thread1.md", "initial thread")
    threads_repo.index.add(["thread1.md"])
    threads_repo.index.commit("initial thread")
    threads_repo.git.branch("-M", "main")

    # Add closure entry to threads/main (this happens when PR is merged)
    touch(threads_path / "closure.md", "PR closure entry")
    threads_repo.index.add(["closure.md"])
    threads_repo.index.commit("PR closure on main")

    # Create staging from BEFORE the closure (simulates unrebased state)
    threads_repo.git.checkout("-b", "staging", "HEAD~1")

    # State now:
    # - code/staging: 0 ahead, 0 behind main (synced)
    # - threads/staging: 0 ahead, 1 behind main (needs rebase)
    # This is a real divergence that should be detected

    result = _detect_behind_main_divergence(
        code_repo, threads_repo, "staging", "staging"
    )

    assert result is not None, "Should detect divergence when code synced but threads behind"
    assert result.diverged is True
    assert result.commits_behind == 1
    assert result.needs_rebase is True


def test_detect_behind_main_divergence_returns_none_when_synced(tmp_path):
    """Test that detection returns None when both repos are synced."""
    # Setup code repo
    code_path = tmp_path / "code"
    code_repo = Repo.init(code_path)
    code_repo.config_writer().set_value("user", "email", "test@example.com").release()
    code_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(code_path / "code.py", "initial")
    code_repo.index.add(["code.py"])
    code_repo.index.commit("initial code")
    code_repo.git.branch("-M", "main")
    code_repo.git.checkout("-b", "staging")

    # Setup threads repo - both branches at same commit
    threads_path = tmp_path / "threads"
    threads_repo = Repo.init(threads_path)
    threads_repo.config_writer().set_value("user", "email", "test@example.com").release()
    threads_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(threads_path / "thread1.md", "initial thread")
    threads_repo.index.add(["thread1.md"])
    threads_repo.index.commit("initial thread")
    threads_repo.git.branch("-M", "main")
    threads_repo.git.checkout("-b", "staging")

    result = _detect_behind_main_divergence(
        code_repo, threads_repo, "staging", "staging"
    )

    assert result is None


def test_detect_behind_main_divergence_returns_none_on_main_branch(tmp_path):
    """Test that detection returns None when already on main branch."""
    code_path = tmp_path / "code"
    code_repo = Repo.init(code_path)
    code_repo.config_writer().set_value("user", "email", "test@example.com").release()
    code_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(code_path / "code.py", "initial")
    code_repo.index.add(["code.py"])
    code_repo.index.commit("initial code")
    code_repo.git.branch("-M", "main")

    threads_path = tmp_path / "threads"
    threads_repo = Repo.init(threads_path)
    threads_repo.config_writer().set_value("user", "email", "test@example.com").release()
    threads_repo.config_writer().set_value("user", "name", "Tester").release()
    touch(threads_path / "thread1.md", "initial thread")
    threads_repo.index.add(["thread1.md"])
    threads_repo.index.commit("initial thread")
    threads_repo.git.branch("-M", "main")

    # On main branch - should return None
    result = _detect_behind_main_divergence(
        code_repo, threads_repo, "main", "main"
    )

    assert result is None


def test_detect_behind_main_divergence_with_squash_merge(tmp_path):
    """Test detection handles squash merges (different commits, same tree content).

    This tests the scenario where:
    - code/staging has commits that were squash-merged to code/main
    - code/staging and code/main have identical tree content but different commit history
    - threads/staging is behind threads/main
    - Detection should still trigger because code is content-synced with main
    """
    # Setup code repo
    code_path = tmp_path / "code"
    code_repo = Repo.init(code_path)
    code_repo.config_writer().set_value("user", "email", "test@example.com").release()
    code_repo.config_writer().set_value("user", "name", "Tester").release()

    # Initial commit on main
    touch(code_path / "file.txt", "initial")
    code_repo.index.add(["file.txt"])
    code_repo.index.commit("initial")
    code_repo.git.branch("-M", "main")

    # Create staging branch and add commits
    code_repo.git.checkout("-b", "staging")
    touch(code_path / "feature.txt", "feature content")
    code_repo.index.add(["feature.txt"])
    code_repo.index.commit("Add feature")

    # Squash merge staging into main
    code_repo.git.checkout("main")
    code_repo.git.merge("staging", "--squash")
    code_repo.index.commit("Squash merge staging")

    # Go back to staging - now staging and main have same content but different commits
    code_repo.git.checkout("staging")

    # Verify: staging has commits main doesn't have (it's "ahead" by commits)
    code_staging_ahead = list(code_repo.iter_commits("main..staging"))
    assert len(code_staging_ahead) > 0, "staging should have commits main doesn't"

    # But tree content should be identical
    main_tree = code_repo.commit("main").tree.hexsha
    staging_tree = code_repo.commit("staging").tree.hexsha
    assert main_tree == staging_tree, "Trees should match after squash merge"

    # Setup threads repo where staging IS behind main
    threads_path = tmp_path / "threads"
    threads_repo = Repo.init(threads_path)
    threads_repo.config_writer().set_value("user", "email", "test@example.com").release()
    threads_repo.config_writer().set_value("user", "name", "Tester").release()

    touch(threads_path / "thread.md", "thread content")
    threads_repo.index.add(["thread.md"])
    threads_repo.index.commit("initial thread")
    threads_repo.git.branch("-M", "main")

    # Create staging at this point
    threads_repo.git.checkout("-b", "staging")

    # Add commit to threads/main (simulating prior PR closure entry)
    threads_repo.git.checkout("main")
    touch(threads_path / "closure.md", "closure entry")
    threads_repo.index.add(["closure.md"])
    threads_repo.index.commit("PR closure entry")

    # Go back to staging - now threads/staging is behind threads/main
    threads_repo.git.checkout("staging")

    # Test: detection should find disparity because code is content-synced
    result = _detect_behind_main_divergence(
        code_repo, threads_repo, "staging", "staging"
    )

    # Should detect divergence even though code/staging has commits main doesn't
    # because the tree content is identical (squash merge)
    assert result is not None, "Should detect disparity with squash merge scenario"
    assert result.diverged is True
    assert result.commits_behind > 0
    assert result.needs_rebase is True
    assert "content-equivalent" in result.details, "Should indicate content-equivalent sync"


def test_detect_behind_main_divergence_feature_branch_ahead_not_synced(tmp_path):
    """Test that a feature branch ahead of main is NOT considered 'synced'.

    This is a regression test for a bug where code_synced was computed as:
        code_commit_synced = len(code_behind_main) == 0

    The problem: being 0 behind main just means main is an ancestor, which is
    the normal state for any feature branch. A branch 6 ahead / 0 behind is
    NOT synced with main.

    The fix: code_commit_synced should be True only when 0 behind AND 0 ahead.
    """
    # Setup code repo with main branch
    code_path = tmp_path / "code"
    code_repo = Repo.init(code_path)
    code_repo.config_writer().set_value("user", "email", "test@example.com").release()
    code_repo.config_writer().set_value("user", "name", "Tester").release()

    touch(code_path / "file.txt", "initial")
    code_repo.index.add(["file.txt"])
    code_repo.index.commit("initial")
    code_repo.git.branch("-M", "main")

    # Create feature branch with commits AHEAD of main
    code_repo.git.checkout("-b", "feature-branch")
    touch(code_path / "feature.txt", "feature work")
    code_repo.index.add(["feature.txt"])
    code_repo.index.commit("feature commit 1")
    touch(code_path / "feature2.txt", "more feature work")
    code_repo.index.add(["feature2.txt"])
    code_repo.index.commit("feature commit 2")

    # Now code/feature-branch is 2 ahead, 0 behind main

    # Setup threads repo in the same state
    threads_path = tmp_path / "threads"
    threads_repo = Repo.init(threads_path)
    threads_repo.config_writer().set_value("user", "email", "test@example.com").release()
    threads_repo.config_writer().set_value("user", "name", "Tester").release()

    touch(threads_path / "thread.md", "initial")
    threads_repo.index.add(["thread.md"])
    threads_repo.index.commit("initial")
    threads_repo.git.branch("-M", "main")

    # Create feature branch with entries AHEAD of main
    threads_repo.git.checkout("-b", "feature-branch")
    touch(threads_path / "feature-thread.md", "feature thread")
    threads_repo.index.add(["feature-thread.md"])
    threads_repo.index.commit("feature thread entry 1")
    touch(threads_path / "feature-thread2.md", "more feature thread")
    threads_repo.index.add(["feature-thread2.md"])
    threads_repo.index.commit("feature thread entry 2")

    # Now threads/feature-branch is 2 ahead, 0 behind main
    # Both are in the SAME state - no disparity should be detected

    result = _detect_behind_main_divergence(
        code_repo, threads_repo, "feature-branch", "feature-branch"
    )

    # Should return None - no disparity, both branches are in sync with each other
    # (both are feature branches ahead of main)
    assert result is None, (
        "Feature branches ahead of main should NOT be flagged as 'synced with main'. "
        f"Got: {result}"
    )


# === Tests for _rebase_branch_onto ===

def test_rebase_branch_onto_already_up_to_date(tmp_path):
    """Test rebase when branch is already up-to-date."""
    repo_path = tmp_path / "repo"
    repo = Repo.init(repo_path)
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    repo.config_writer().set_value("user", "name", "Tester").release()
    touch(repo_path / "file.txt", "content")
    repo.index.add(["file.txt"])
    repo.index.commit("initial")
    repo.git.branch("-M", "main")
    repo.git.checkout("-b", "staging")

    # staging is at same point as main, no rebase needed
    result = _rebase_branch_onto(repo, "staging", "main", force=False)

    assert result.success is True
    assert result.action_taken == "no_action"
    assert "already up-to-date" in result.details


def test_rebase_branch_onto_success(tmp_path):
    """Test successful rebase of branch onto main."""
    repo_path = tmp_path / "repo"
    remote_path = tmp_path / "remote.git"

    # Create bare remote
    init_remote_repo(remote_path)

    # Clone and setup
    repo = Repo.clone_from(remote_path.as_posix(), repo_path)
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    repo.config_writer().set_value("user", "name", "Tester").release()

    # Create initial commit on main
    touch(repo_path / "file.txt", "initial")
    repo.index.add(["file.txt"])
    repo.index.commit("initial")
    repo.git.branch("-M", "main")
    repo.remotes.origin.push("main:main")

    # Create staging branch
    repo.git.checkout("-b", "staging")
    touch(repo_path / "staging.txt", "staging work")
    repo.index.add(["staging.txt"])
    repo.index.commit("staging commit")
    repo.remotes.origin.push("staging:staging")

    # Go back to main and add a commit
    repo.git.checkout("main")
    touch(repo_path / "main_update.txt", "main update")
    repo.index.add(["main_update.txt"])
    repo.index.commit("main update")
    repo.remotes.origin.push("main")

    # Now staging is behind main by 1 commit
    # Rebase staging onto main
    repo.git.checkout("staging")  # ensure on staging
    result = _rebase_branch_onto(repo, "staging", "main", force=True)

    assert result.success is True
    assert result.action_taken == "rebased"
    assert result.commits_preserved == 1  # Our staging commit
    assert "Force-pushed" in result.details


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


# ============================================================================
# Sync Write Push Mechanism Tests (Task 1 verification)
# ============================================================================


def test_sync_write_uses_push_after_commit(tmp_path, monkeypatch):
    """Test that sync writes use push_after_commit with rebase-on-reject."""
    from unittest.mock import patch, MagicMock
    from watercooler_mcp.git_sync import GitSyncManager

    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
    )

    # Track calls to push_after_commit
    push_after_commit_called = []

    def mock_push_after_commit(repo_path, branch, max_retries=3):
        push_after_commit_called.append({
            'repo_path': repo_path,
            'branch': branch,
            'max_retries': max_retries
        })
        return (True, None)

    with patch('watercooler_mcp.branch_parity.push_after_commit', mock_push_after_commit):
        # Execute a sync write operation
        def write_op():
            touch(tmp_path / "threads" / "test.md", "test content")
            mgr._repo.index.add(["test.md"])
            return "success"

        result = mgr._with_sync_sync(write_op, "test commit")

    assert result == "success"
    assert len(push_after_commit_called) == 1
    # Branch could be 'main' or 'master' depending on git config
    assert push_after_commit_called[0]['branch'] in ("main", "master")


def test_sync_write_push_failure_marks_pending(tmp_path, monkeypatch):
    """Test that push failure marks pending_push=True in parity state."""
    from unittest.mock import patch
    from watercooler_mcp.git_sync import GitSyncManager, GitPushError
    from watercooler_mcp.branch_parity import read_parity_state, ParityState

    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
    )

    # Initialize parity state
    parity_dir = tmp_path / "threads" / ".wc-parity"
    parity_dir.mkdir(parents=True, exist_ok=True)

    def mock_push_after_commit(repo_path, branch, max_retries=3):
        return (False, "Simulated push failure")

    with patch('watercooler_mcp.branch_parity.push_after_commit', mock_push_after_commit):
        def write_op():
            touch(tmp_path / "threads" / "test.md", "test content")
            mgr._repo.index.add(["test.md"])
            return "success"

        with pytest.raises(GitPushError) as exc_info:
            mgr._with_sync_sync(write_op, "test commit")

        assert "Simulated push failure" in str(exc_info.value)

    # Check parity state was updated
    state = read_parity_state(tmp_path / "threads")
    assert state.pending_push is True
    assert state.status == "pending_push"
    assert "Simulated push failure" in state.last_error


def test_sync_write_push_success_clears_pending(tmp_path, monkeypatch):
    """Test that push success clears pending_push in parity state."""
    from unittest.mock import patch
    from watercooler_mcp.git_sync import GitSyncManager
    from watercooler_mcp.branch_parity import (
        read_parity_state,
        write_parity_state,
        ParityState,
        ParityStatus,
    )

    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
    )

    # Set up initial state with pending_push=True
    parity_dir = tmp_path / "threads" / ".wc-parity"
    parity_dir.mkdir(parents=True, exist_ok=True)
    initial_state = ParityState(
        status=ParityStatus.PENDING_PUSH.value,
        pending_push=True,
        last_error="Previous push failed",
    )
    write_parity_state(tmp_path / "threads", initial_state)

    def mock_push_after_commit(repo_path, branch, max_retries=3):
        return (True, None)

    with patch('watercooler_mcp.branch_parity.push_after_commit', mock_push_after_commit):
        def write_op():
            touch(tmp_path / "threads" / "test.md", "test content")
            mgr._repo.index.add(["test.md"])
            return "success"

        result = mgr._with_sync_sync(write_op, "test commit")

    assert result == "success"

    # Check parity state was updated
    state = read_parity_state(tmp_path / "threads")
    assert state.pending_push is False
    assert state.status == "clean"
    assert state.last_error is None


def test_sync_write_no_commit_skips_push(tmp_path, monkeypatch):
    """Test that sync writes skip push when no changes were committed."""
    from unittest.mock import patch
    from watercooler_mcp.git_sync import GitSyncManager

    remote = tmp_path / "remote.git"
    seed_remote_with_main(remote)

    mgr = GitSyncManager(
        repo_url=remote.as_posix(),
        local_path=tmp_path / "threads",
        ssh_key_path=None,
    )

    push_called = []

    def mock_push_after_commit(repo_path, branch, max_retries=3):
        push_called.append(True)
        return (True, None)

    def mock_commit_local(message):
        # Simulate no changes to commit
        return False

    with patch('watercooler_mcp.branch_parity.push_after_commit', mock_push_after_commit):
        with patch.object(mgr, 'commit_local', mock_commit_local):
            # Operation that doesn't make any changes
            def noop():
                return "no changes"

            result = mgr._with_sync_sync(noop, "empty commit")

    assert result == "no changes"
    # push_after_commit should NOT be called if no commit was made
    assert len(push_called) == 0


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
