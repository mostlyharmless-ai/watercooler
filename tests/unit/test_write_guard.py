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
    _read_origin_url,
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
            "https://api.github.com/example/repo.git",
            "git@subdomain.github.com:example/repo.git",
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
            # Round-6 L1 regression: attacker-controlled hostname
            # that merely begins with ``github.`` must NOT satisfy
            # the default host check. Prior
            # ``host.startswith("github.")`` was a real security
            # boundary accuracy bug — a user whose git remote was
            # pointed at ``github.attacker.com`` would have their
            # thread pushes silently accepted by the guard.
            "https://github.attacker.com/example/repo.git",
            "git@github.evil.io:example/repo.git",
            # GHE hostnames are no longer accepted by default —
            # they require the ``WATERCOOLER_GITHUB_HOSTS`` opt-in.
            "https://github.enterprise.example/example/repo.git",
            "git@github.acme.com:example/repo.git",
        ],
    )
    def test_non_github_hosts_rejected(self, url):
        assert _looks_github_hosted(url) is False


class TestGitHubHostAllowlistEnvVar:
    """``WATERCOOLER_GITHUB_HOSTS`` opt-in for GitHub Enterprise."""

    def test_exact_hostname_accepted(self, monkeypatch):
        monkeypatch.setenv("WATERCOOLER_GITHUB_HOSTS", "github.acme.com")
        assert _looks_github_hosted("git@github.acme.com:example/repo.git") is True
        assert _looks_github_hosted("https://github.acme.com/x/y.git") is True
        # Non-matching hosts still rejected.
        assert _looks_github_hosted("https://github.attacker.com/x/y.git") is False

    def test_suffix_pattern_accepts_subdomains(self, monkeypatch):
        monkeypatch.setenv("WATERCOOLER_GITHUB_HOSTS", "*.ghe.io")
        assert _looks_github_hosted("https://github.ghe.io/x/y.git") is True
        assert _looks_github_hosted("https://other.ghe.io/x/y.git") is True
        # The suffix form does NOT match the bare domain.
        assert _looks_github_hosted("https://ghe.io/x/y.git") is False

    def test_comma_separated_list_with_whitespace(self, monkeypatch):
        monkeypatch.setenv(
            "WATERCOOLER_GITHUB_HOSTS",
            " github.acme.com , *.ghe.io ,,",  # whitespace + empty entries
        )
        assert _looks_github_hosted("https://github.acme.com/x.git") is True
        assert _looks_github_hosted("https://github.ghe.io/x.git") is True
        assert _looks_github_hosted("https://evil.com/x.git") is False

    def test_empty_env_var_no_effect(self, monkeypatch):
        monkeypatch.setenv("WATERCOOLER_GITHUB_HOSTS", "   ")
        # Default behavior: only real github.com still accepted.
        assert _looks_github_hosted("https://github.com/x.git") is True
        assert _looks_github_hosted("https://github.acme.com/x.git") is False


class TestAssertGitHubBackedThreads:
    """End-to-end guard behavior."""

    def test_valid_github_repo_passes(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert_github_backed_threads(repo)  # should not raise

    def test_no_git_repo_raises(self, tmp_path):
        with pytest.raises(WatercoolerWriteError) as exc:
            assert_github_backed_threads(tmp_path)
        msg = str(exc.value)
        # Reason text reflects the strict "threads_dir itself must be a
        # worktree" check — no ancestor walk (post-PR #613 correction).
        assert "not itself a git worktree" in msg
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

    def test_ancestor_walk_regression_local_subdir_is_refused(self, tmp_path):
        """Regression guard for the post-PR #613 finding: a sub-directory
        of a valid GitHub-backed repo (e.g. ``<repo>/_local``) must NOT
        be accepted as "GitHub-backed." The parent repo's origin gives
        the sub-dir nothing — writes still land in the sub-dir and
        never reach the remote. Guard must require .git AT threads_dir,
        not at any ancestor."""
        repo = _make_repo(tmp_path, remote_url="https://github.com/example/repo.git")
        local_subdir = repo / "_local"
        local_subdir.mkdir()

        with pytest.raises(WatercoolerWriteError) as exc:
            assert_github_backed_threads(local_subdir)
        msg = str(exc.value)
        assert "not itself a git worktree" in msg
        assert "WATERCOOLER_ALLOW_LOCAL_ONLY" in msg

    def test_arbitrary_subdir_of_repo_also_refused(self, tmp_path):
        """Any sub-directory, not just ``_local``. The ancestor walk
        was too permissive across the board — every subdir would have
        been accepted."""
        repo = _make_repo(tmp_path)
        random_subdir = repo / "notes" / "deep"
        random_subdir.mkdir(parents=True)

        with pytest.raises(WatercoolerWriteError):
            assert_github_backed_threads(random_subdir)

    def test_origin_defined_via_include_directive_accepted(self, tmp_path):
        """Regression guard for the post-PR #613 round-3 finding: the
        lightweight INI parser in ``_read_origin_url`` doesn't follow
        ``[include] path = ...`` stanzas, so a repo whose origin is
        defined only in an included config file was falsely refused
        with "no 'origin' remote configured". Git itself honors the
        include, so the guard now falls back to ``git config --get
        remote.origin.url`` and accepts the write.
        """
        import subprocess as _subprocess

        # Real ``git init`` to get HEAD / refs / objects — git only
        # resolves ``[include]`` against a fully-initialized gitdir,
        # not a manually-mkdir'd skeleton.
        _subprocess.run(
            ["git", "init", "--quiet", str(tmp_path)],
            check=True,
            capture_output=True,
        )

        extra_config = tmp_path / "extra-config"
        extra_config.write_text(
            '[remote "origin"]\n'
            "  url = https://github.com/example/repo.git\n"
            "  fetch = +refs/heads/*:refs/remotes/origin/*\n"
        )
        # Overwrite the post-``init`` config so the ONLY place the
        # origin URL appears is the included file — exactly the
        # dotfile-shared case Codex flagged.
        (tmp_path / ".git" / "config").write_text(
            "[core]\n"
            "  repositoryformatversion = 0\n"
            "[include]\n"
            f"  path = {extra_config}\n"
        )

        # Manual parser finds no origin; fallback to ``git config
        # --get`` must resolve the include and find the URL. Guard
        # accepts the write.
        assert_github_backed_threads(tmp_path)


class TestReadOriginUrlStrictKeyMatch:
    """Regression guards for the post-PR #613 round-7 finding: the
    manual INI parser used ``line.lower().startswith("url")`` which
    would silently match hypothetical keys like ``urlpath`` or
    ``url-tracking`` under ``[remote "origin"]``. Exact key comparison
    now required. The subprocess fallback is patched out so these
    tests exercise the manual parser in isolation.
    """

    @pytest.fixture(autouse=True)
    def _no_git_config_fallback(self, monkeypatch):
        # Force the subprocess fallback to report "no origin" so
        # these tests verify exactly what the manual parser returns.
        monkeypatch.setattr(
            "watercooler.write_guard._read_origin_url_from_git",
            lambda gitdir: None,
        )

    def test_urlpath_alone_is_not_treated_as_url(self, tmp_path):
        gitdir = tmp_path / ".git"
        gitdir.mkdir()
        (gitdir / "config").write_text(
            "[core]\n"
            "  repositoryformatversion = 0\n"
            '[remote "origin"]\n'
            "  urlpath = https://example.com/not-really-the-url.git\n"
        )
        assert _read_origin_url(gitdir) is None

    def test_url_dash_tracking_alone_is_not_treated_as_url(self, tmp_path):
        gitdir = tmp_path / ".git"
        gitdir.mkdir()
        (gitdir / "config").write_text(
            '[remote "origin"]\n'
            "  url-tracking = https://bad.example/x.git\n"
        )
        assert _read_origin_url(gitdir) is None

    def test_real_url_still_parsed(self, tmp_path):
        gitdir = tmp_path / ".git"
        gitdir.mkdir()
        (gitdir / "config").write_text(
            '[remote "origin"]\n'
            "  url = https://github.com/example/repo.git\n"
        )
        assert (
            _read_origin_url(gitdir)
            == "https://github.com/example/repo.git"
        )

    def test_urlpath_before_real_url_does_not_shadow(self, tmp_path):
        """The parser must skip ``urlpath`` and keep looking. If
        ``startswith("url")`` matched, the first key wins and the
        correct URL below is never seen."""
        gitdir = tmp_path / ".git"
        gitdir.mkdir()
        (gitdir / "config").write_text(
            '[remote "origin"]\n'
            "  urlpath = https://wrong.example/x.git\n"
            "  url = https://github.com/example/repo.git\n"
        )
        assert (
            _read_origin_url(gitdir)
            == "https://github.com/example/repo.git"
        )


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

    def test_non_github_remote_reported_as_local_only(self, tmp_path):
        """Regression guard for the post-PR #613 round-5 finding: the
        prior implementation only checked for the presence of an
        origin URL, not whether the host was GitHub-family. A threads
        directory with a GitLab / Bitbucket / Gitea origin was
        labeled ``custom (<name>)`` or ``orphan worktree`` (implying
        writes are fine) while ``assert_github_backed_threads``
        refused the write with "origin URL is not a GitHub-hosted
        remote". Health and guard must agree.
        """
        from watercooler_mcp.tools.diagnostic import _describe_storage_mode

        gitlab_repo = tmp_path / "somewhere"
        _make_repo(
            gitlab_repo, remote_url="https://gitlab.com/example/repo.git"
        )
        assert (
            _describe_storage_mode(gitlab_repo)
            == "local-only (no GitHub backing)"
        )

    def test_custom_mode_for_other_paths(self, tmp_path):
        from watercooler_mcp.tools.diagnostic import _describe_storage_mode

        custom = tmp_path / "somewhere-unusual"
        _make_repo(custom)
        result = _describe_storage_mode(custom)
        assert result.startswith("custom (")
        assert "somewhere-unusual" in result

    def test_local_named_dir_with_github_remote_not_labeled_local_only(
        self, tmp_path
    ):
        """Regression guard for the post-PR #613 round-4 finding: the
        prior implementation hard-coded ``name == "_local"`` so any
        directory named ``_local`` was labeled
        ``"local-only (no GitHub backing)"`` even when it had a valid
        ``.git`` pointing at a GitHub remote. The write guard has no
        such special case and would accept writes — health and guard
        contradicted each other. The label must now derive from the
        actual git / origin state, not the directory name.
        """
        from watercooler_mcp.tools.diagnostic import _describe_storage_mode

        local_named = tmp_path / "_local"
        _make_repo(local_named, remote_url="https://github.com/example/repo.git")
        # GitHub-backed repo at ``_local``; must NOT be labeled
        # "local-only". Falls through to "custom (_local)" since
        # ``_local`` isn't under the worktrees root and doesn't end
        # in ``-threads``.
        result = _describe_storage_mode(local_named)
        assert "local-only" not in result, (
            f"GitHub-backed ``_local`` should not be labeled local-only, got: {result!r}"
        )
        assert result == "custom (_local)"

    def test_subdir_of_github_repo_reported_as_local_only(self, tmp_path):
        """Regression guard for the post-PR #613 round-2 finding: when
        ``threads_dir`` is a plain sub-directory of a GitHub-backed
        repo (no ``.git`` AT the sub-dir), ``_describe_storage_mode``
        must agree with ``assert_github_backed_threads`` that the
        sub-dir is NOT GitHub-backed. Previously it walked ancestors
        via ``_find_git_dir`` and reported ``custom (_custom)`` or
        even ``orphan worktree``, while the write guard refused the
        write — the two outputs contradicted each other on whether
        the same path was backed.
        """
        from watercooler_mcp.tools.diagnostic import _describe_storage_mode

        repo = _make_repo(tmp_path, remote_url="https://github.com/example/repo.git")
        subdir = repo / "_custom"
        subdir.mkdir()
        assert _describe_storage_mode(subdir) == "local-only (no GitHub backing)"
