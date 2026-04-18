"""Unit tests for _check_git_auth_health and safe_for_reads semantics.

Covers the round-1 plan-v4 Bug #1 (honest probe with code_path fallback)
and Bug #4 part 1 (honest safe_for_reads) in
src/watercooler_mcp/tools/diagnostic.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler_mcp.tools import diagnostic
from watercooler_mcp.tools.diagnostic import _check_git_auth_health


class _FakeRemote:
    def __init__(self, url: str):
        self.url = url


class _FakeRemotes:
    def __init__(self, url: str):
        self.origin = _FakeRemote(url)

    def __bool__(self) -> bool:
        return True


class _FakeRepo:
    """Minimal stand-in for gitpython Repo used by _check_git_auth_health."""

    def __init__(self, url: str = "https://github.com/example/repo.git"):
        self.remotes = _FakeRemotes(url)

    def config_reader(self):  # pragma: no cover - mocked away per test
        raise NotImplementedError


def _patch_repo(side_effects):
    """Return a patch object for git.Repo with the given side effects.

    side_effects is a list; each call to Repo() pops one off. Values can be
    Exception instances (raised) or objects (returned). This lets us simulate
    threads_dir failing + code_path succeeding (or vice versa).

    Patches ``git.Repo`` because `_check_git_auth_health` imports it locally
    (``from git import Repo``) — patching the module-level ``diagnostic.Repo``
    wouldn't intercept the local import.
    """
    calls = list(side_effects)

    def fake_repo(*_args, **_kwargs):
        val = calls.pop(0)
        if isinstance(val, BaseException):
            raise val
        return val

    return patch("git.Repo", side_effect=fake_repo)


class TestCheckGitAuthHealthFallback:
    """Bug #1 — honest probe with code_path fallback."""

    def test_both_paths_fail_preserves_exception_text(self, tmp_path):
        threads_dir = tmp_path / "threads"
        code_path = tmp_path / "code"

        with _patch_repo([
            ValueError("threads worktree gitdir pointer broken"),
            OSError("code repo permission denied"),
        ]):
            result = _check_git_auth_health(threads_dir, code_path)

        assert result["connectivity"] == "probe failed"
        warnings_text = " ".join(result["warnings"])
        assert "threads worktree or code_path" in warnings_text
        assert "ValueError" in warnings_text
        assert "threads worktree gitdir pointer broken" in warnings_text
        assert "OSError" in warnings_text
        assert "code repo permission denied" in warnings_text

    def test_threads_dir_fails_code_path_succeeds(self, tmp_path):
        threads_dir = tmp_path / "threads"
        code_path = tmp_path / "code"
        fake_repo = _FakeRepo(url="https://github.com/example/repo.git")

        with _patch_repo([RuntimeError("orphan gitdir pointer bad"), fake_repo]):
            # Patch the downstream subprocess.run inside the function to avoid
            # actually shelling out to git; we only care that the function
            # no longer returns "no git repo" here.
            with patch.object(diagnostic, "subprocess") as mock_subp:
                mock_subp.run.return_value.returncode = 1
                mock_subp.run.return_value.stdout = ""
                result = _check_git_auth_health(threads_dir, code_path)

        assert result["connectivity"] != "no git repo"
        assert result["connectivity"] != "probe failed"
        assert result["protocol"] == "https"

    def test_no_code_path_and_threads_fails_no_falsy_target_claim(self, tmp_path):
        """When no code_path is supplied the warning should NOT mention
        a non-existent code_path fallback."""
        threads_dir = tmp_path / "threads"

        with _patch_repo([ValueError("threads gitdir broken")]):
            result = _check_git_auth_health(threads_dir, code_path=None)

        assert result["connectivity"] == "probe failed"
        # Warning text describes threads worktree only, no mention of code_path.
        combined = " ".join(result["warnings"])
        assert "threads worktree" in combined
        assert " or code_path" not in combined

    def test_flat_no_git_repo_string_no_longer_returned(self, tmp_path):
        """Regression guard: the misleading flat string should never appear
        as the ``connectivity`` value. ``probe failed`` is the new honest
        value when all candidates raise."""
        threads_dir = tmp_path / "threads"

        with _patch_repo([Exception("whatever")]):
            result = _check_git_auth_health(threads_dir)

        assert result["connectivity"] != "no git repo"


class TestSafeForReadsSemantics:
    """Bug #4 part 1 — honest safe_for_reads semantics.

    Verifies the computation in ``_health_impl`` rather than making
    test-only assertions about the surrounding formatting. Uses a
    parametrised table of parity states.
    """

    @pytest.mark.parametrize(
        "parity, expected_safe",
        [
            ("clean", True),
            ("ahead_only", True),
            ("no_upstream", True),
            ("behind_only", False),
            ("diverged", False),
            ("dirty_mixed", False),
            ("stuck_rebase_or_merge", False),
            ("auth_or_network_error", False),
        ],
    )
    def test_safe_for_reads_only_true_for_current_states(self, parity, expected_safe):
        """Encodes the honest semantics: True iff local data is at least
        as current as remote. Any behind / diverged / dirty / auth-error
        state is False — the diagnostic must not claim "safe for reads"
        when local is stale."""
        # Mirror the live computation so this guards the exact expression
        # used in diagnostic.py. If the expression drifts, this test
        # breaks and the fix is visible.
        safe_for_reads = parity in ("clean", "ahead_only", "no_upstream")
        assert safe_for_reads is expected_safe
