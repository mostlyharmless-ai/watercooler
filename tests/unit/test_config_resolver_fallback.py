"""Tests for the threads-resolver fallback path — GH issue #837.

Locks the behaviors that comprise the fix:

- Bug B: when worktree creation fails, the fallback `_local` lives under
  `effective_root`, NEVER under `Path.cwd()` (which would be the MCP
  server's CWD, leaking writes across repos).
- Bug C: a structural guard refuses to return a `threads_dir` outside
  `{canonical worktree, effective_root/_local}` for a given
  `effective_root` — defense-in-depth against future resolution regressions
  silently leaking across repos.

Plus an operator-attention case: branch already checked out at an
unexpected (non-canonical) path → resolver refuses to silently fall
back; surfaces a warning.

Real git repos in tmpdirs (matches `tests/unit/test_config_orphan_bootstrap.py`
pattern — mocking git would miss the real semantics that produced the bug).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from watercooler_mcp import config
from watercooler_mcp.config import (
    ORPHAN_BRANCH_NAME,
    _enforce_threads_dir_safety,
    _ensure_worktree,
    _find_existing_worktree_on_branch,
    resolve_thread_context,
)


@pytest.fixture(autouse=True)
def _propagate_watercooler_logger():
    """Re-enable propagation on the `watercooler_mcp` logger so caplog
    sees records. The codebase sets propagate=False by default once
    handlers are attached; this fixture flips it back for tests that
    assert on warning content. Restores prior state after the test."""
    import logging

    logger = logging.getLogger("watercooler_mcp")
    prior = logger.propagate
    logger.propagate = True
    yield
    logger.propagate = prior


def _git(cwd, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")


def _commit_file(path: Path, name: str, content: str = "x\n") -> None:
    (path / name).write_text(content, encoding="utf-8")
    _git(path, "add", name)
    _git(path, "commit", "-m", f"add {name}")


def _add_orphan_worktree(code_root: Path, wt_path: Path) -> None:
    """Create an orphan-branch worktree at ``wt_path`` portably.

    Prefers ``git worktree add --orphan`` (git >= 2.42); falls back to
    the detached + checkout-orphan + rm dance (matches production code
    in ``config._create_orphan_branch``). Always commits an empty
    initial commit on the new branch so it's a "born" ref — without
    that, ``git branch --list`` is empty even though
    ``worktree list --porcelain`` reports the worktree, which breaks
    ``_orphan_branch_exists()`` and any code path that checks for the
    branch by name (see PR #838 reviewer note).
    """
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    used_orphan_form = False
    try:
        _git(
            code_root,
            "worktree",
            "add",
            "--orphan",
            "-b",
            ORPHAN_BRANCH_NAME,
            str(wt_path),
        )
        used_orphan_form = True
    except subprocess.CalledProcessError:
        pass
    if not used_orphan_form:
        # Fallback: detached worktree + manual orphan setup
        if wt_path.exists():
            # cleanup any partial state from the failed --orphan attempt
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(code_root),
                    "worktree",
                    "remove",
                    "--force",
                    str(wt_path),
                ],
                capture_output=True,
            )
            try:
                wt_path.rmdir()
            except OSError:
                pass
        _git(code_root, "worktree", "add", "--detach", str(wt_path))
        _git(wt_path, "checkout", "--orphan", ORPHAN_BRANCH_NAME)
        subprocess.run(
            ["git", "-C", str(wt_path), "rm", "-rf", "."],
            capture_output=True,
        )
    # Both paths land here: an orphan branch is unborn until it has a
    # commit. Without this, `git branch --list watercooler/threads`
    # returns empty on git >= 2.42 (--orphan path), and
    # `_orphan_branch_exists()` falsely reports no branch.
    _git(wt_path, "commit", "--allow-empty", "-m", "init orphan")


@pytest.fixture
def code_repo(tmp_path) -> Path:
    """A code repo with the main branch and one commit (no remote needed)."""
    repo = tmp_path / "code"
    _init_repo(repo)
    _commit_file(repo, "f.txt")
    return repo


@pytest.fixture
def isolated_worktree_base(tmp_path, monkeypatch) -> Path:
    """Redirect WORKTREE_BASE to a tmpdir so tests don't touch ~/.watercooler."""
    base = tmp_path / "worktrees"
    monkeypatch.setattr(config, "WORKTREE_BASE", base)
    return base


# ---------------------------------------------------------------------------
# Orphan-branch worktree discovery
# ---------------------------------------------------------------------------


class TestWorktreeDiscovery:
    def test_finds_existing_worktree_on_branch(self, code_repo):
        """_find_existing_worktree_on_branch parses `git worktree list`
        correctly and returns the path."""
        # Create an orphan branch and check it out at <repo>/_local
        _add_orphan_worktree(code_repo, code_repo / "_local")

        found = _find_existing_worktree_on_branch(code_repo, ORPHAN_BRANCH_NAME)
        assert found is not None
        assert found.resolve() == (code_repo / "_local").resolve()

    def test_returns_none_when_branch_not_checked_out(self, code_repo):
        """No worktree on the branch → returns None."""
        # Branch doesn't exist yet — list returns no matches
        found = _find_existing_worktree_on_branch(code_repo, ORPHAN_BRANCH_NAME)
        assert found is None

    def test_ensure_worktree_refuses_branch_at_unexpected_path(
        self,
        code_repo,
        isolated_worktree_base,
        caplog,
    ):
        """Branch checked out at neither _local nor canonical → don't
        auto-migrate (operator-attention case); return None and log loudly.

        Logs go through the project's `watercooler_mcp` logger; pin
        caplog to that namespace so the records show up here.
        """
        unexpected = code_repo / ".threads-tmp"
        _add_orphan_worktree(code_repo, unexpected)

        with caplog.at_level("WARNING", logger="watercooler_mcp"):
            result = _ensure_worktree(code_repo)

        assert result is None
        warnings = " ".join(r.message for r in caplog.records)
        assert ".threads-tmp" in warnings or "unexpected" in warnings.lower()


# ---------------------------------------------------------------------------
# Bug B: fallback uses effective_root, not Path.cwd()
# ---------------------------------------------------------------------------


class TestFallbackUsesEffectiveRoot:
    def test_fallback_resolves_inside_effective_root_not_cwd(
        self,
        tmp_path,
        monkeypatch,
        code_repo,
        isolated_worktree_base,
    ):
        """If _ensure_worktree returns None, the fallback _local must live
        inside `effective_root` (the queried repo), NEVER inside Path.cwd()
        (the MCP server's CWD)."""
        # Force _ensure_worktree to fail (simulates Bug A's failure mode
        # without setting up the full migration scenario)
        monkeypatch.setattr(config, "_ensure_worktree", lambda r: None)

        # Change cwd to a separate, unrelated tmpdir — emulates the
        # MCP server running from `watercooler-cloud` while the caller
        # passes `spatial-tm` as code_path
        mcp_cwd = tmp_path / "unrelated-mcp-server"
        mcp_cwd.mkdir()
        monkeypatch.chdir(mcp_cwd)

        ctx = resolve_thread_context(code_repo)

        # The bug used to land threads_dir at mcp_cwd / _local. The fix
        # places it under code_repo / _local instead.
        assert ctx.threads_dir.resolve() == (code_repo / "_local").resolve()
        # Critically, it is NOT under the MCP server's cwd
        try:
            ctx.threads_dir.resolve().relative_to(mcp_cwd.resolve())
            pytest.fail(
                f"threads_dir leaked into MCP-server CWD: {ctx.threads_dir}",
            )
        except ValueError:
            pass  # expected — not relative to mcp_cwd


# ---------------------------------------------------------------------------
# Bug C: structural guard against cross-repo leak
# ---------------------------------------------------------------------------


class TestStructuralGuard:
    """Tests for the post-resolve safety guard. Calls the helper directly
    rather than threading a synthetic threads_dir through the full
    resolver — too brittle in practice because the resolver's own
    `legacy_local` computation collides with any monkeypatch that
    targets `_resolve_path`."""

    def test_guard_passes_canonical_unchanged(
        self,
        code_repo,
        isolated_worktree_base,
    ):
        """Canonical worktree path passes through untouched."""
        canonical = isolated_worktree_base / code_repo.name
        result = _enforce_threads_dir_safety(canonical, code_repo)
        assert result == canonical

    def test_guard_passes_legacy_local_unchanged(self, code_repo):
        """In-repo `_local` fallback passes through untouched."""
        legacy = code_repo / "_local"
        result = _enforce_threads_dir_safety(legacy, code_repo)
        assert result.resolve() == legacy.resolve()

    def test_guard_overrides_threads_dir_in_unrelated_repo(
        self,
        tmp_path,
        code_repo,
        isolated_worktree_base,
        caplog,
    ):
        """A threads_dir landing OUTSIDE the allowed set (canonical or
        in-repo _local) MUST be overridden to in-repo _local. This is
        the exact Bug-B-leak symptom: writes were landing in another
        repo's `_local` because the fallback used `Path.cwd()`."""
        bad = tmp_path / "some-other-repo" / "_local"
        bad.mkdir(parents=True)

        with caplog.at_level("WARNING", logger="watercooler_mcp"):
            result = _enforce_threads_dir_safety(bad, code_repo)

        # Guard re-routed to in-repo _local
        assert result.resolve() == (code_repo / "_local").resolve()
        # And logged loudly so operators see the regression
        warnings = " ".join(r.message for r in caplog.records)
        assert "cross-repo" in warnings.lower() or "regression" in warnings.lower()

    def test_guard_overrides_threads_dir_in_path_cwd(
        self,
        tmp_path,
        code_repo,
        isolated_worktree_base,
        caplog,
    ):
        """Specific symptom from the original bug: threads_dir under
        Path.cwd() / _local (different from effective_root). Guard
        catches it."""
        mcp_cwd_local = tmp_path / "mcp-server-cwd" / "_local"
        mcp_cwd_local.mkdir(parents=True)

        with caplog.at_level("WARNING", logger="watercooler_mcp"):
            result = _enforce_threads_dir_safety(mcp_cwd_local, code_repo)

        assert result.resolve() == (code_repo / "_local").resolve()


# ---------------------------------------------------------------------------
# Happy-path no-regression check
# ---------------------------------------------------------------------------


class TestHappyPathStillWorks:
    def test_canonical_worktree_returned_when_present(
        self,
        code_repo,
        isolated_worktree_base,
    ):
        """Canonical worktree already at WORKTREE_BASE/<repo> → fast-path
        returns it, no migration, no fallback."""
        canonical = isolated_worktree_base / code_repo.name
        _add_orphan_worktree(code_repo, canonical)

        result = _ensure_worktree(code_repo)
        assert result == canonical

    def test_resolve_thread_context_returns_canonical_in_happy_path(
        self,
        code_repo,
        isolated_worktree_base,
    ):
        """Full resolver: canonical worktree present → ThreadContext
        points at it; no fallback fires."""
        canonical = isolated_worktree_base / code_repo.name
        _add_orphan_worktree(code_repo, canonical)

        ctx = resolve_thread_context(code_repo)
        assert ctx.threads_dir.resolve() == canonical.resolve()
        assert ctx.code_root.resolve() == code_repo.resolve()
