"""The `_local` fallback warning must distinguish a real degraded write target
from a benign non-repo path.

When a daemon resolves a default/"primary" threads dir from the server's launch
CWD and that CWD is a parent directory holding several cloned repos (not a git
repo itself), resolution falls to a `<dir>/_local` path — but this is NOT a
write target (every real write carries its own code_path). The old startup
notice claiming "New writes will NOT be synced" was false and alarming there.
It must remain only for a genuine git repo whose worktree creation failed.
"""

from __future__ import annotations

import watercooler_mcp.config as config
import watercooler_mcp.helpers as helpers


def _capture_startup_warnings(monkeypatch) -> list:
    warnings: list = []
    monkeypatch.setattr(helpers, "_add_startup_warning", lambda m: warnings.append(m))
    return warnings


def test_non_repo_path_emits_no_sync_failure_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREE_BASE", tmp_path / "wt")
    monkeypatch.delenv("WATERCOOLER_DIR", raising=False)
    monkeypatch.delenv("WATERCOOLER_CODE_REPO", raising=False)

    not_a_repo = tmp_path / "dir-of-clones"
    not_a_repo.mkdir()

    # Not a git repo → root is None; worktree creation can't succeed.
    monkeypatch.setattr(
        config, "_discover_git",
        lambda r: config._GitDetails(root=None, branch=None, commit=None, remote=None),
    )
    monkeypatch.setattr(config, "_ensure_worktree", lambda r: None)
    warnings = _capture_startup_warnings(monkeypatch)

    ctx = config.resolve_thread_context(not_a_repo)

    # Resolves to a _local fallback (graceful), but NO alarming startup notice.
    assert "_local" in str(ctx.threads_dir)
    assert warnings == []


def test_real_repo_worktree_failure_still_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREE_BASE", tmp_path / "wt")
    monkeypatch.delenv("WATERCOOLER_DIR", raising=False)
    monkeypatch.delenv("WATERCOOLER_CODE_REPO", raising=False)

    repo = tmp_path / "real-repo"
    repo.mkdir()

    # A real git repo (root set) whose orphan worktree creation fails.
    monkeypatch.setattr(
        config, "_discover_git",
        lambda r: config._GitDetails(
            root=repo, branch="main", commit="abc1234", remote="git@github.com:o/r.git"
        ),
    )
    monkeypatch.setattr(config, "_ensure_worktree", lambda r: None)
    warnings = _capture_startup_warnings(monkeypatch)

    ctx = config.resolve_thread_context(repo)

    assert "_local" in str(ctx.threads_dir)
    assert len(warnings) == 1
    assert "NOT be synced" in warnings[0]
