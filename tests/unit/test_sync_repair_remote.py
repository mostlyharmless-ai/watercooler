"""Tests for sync_repair remote handling — issue #689.

An orphan thread branch with no usable remote upstream: diagnose() should
report the remote context, and repair(publish_remote=...) should publish it.
"""

import subprocess

import pytest

from watercooler.sync_repair import diagnose, repair


def _git(cwd, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def orphan_worktree(tmp_path):
    """A watercooler/threads worktree with NO upstream and two remotes.

    Returns (code_repo, worktree, origin_bare, upstream_bare).
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    upstream = tmp_path / "upstream.git"
    subprocess.run(
        ["git", "init", "--bare", str(upstream)], check=True, capture_output=True
    )
    code = tmp_path / "code"
    subprocess.run(
        ["git", "init", "-b", "main", str(code)], check=True, capture_output=True
    )
    _git(code, "config", "user.email", "test@example.com")
    _git(code, "config", "user.name", "Test")
    _git(code, "remote", "add", "origin", str(origin))
    _git(code, "remote", "add", "upstream", str(upstream))
    (code / "f.txt").write_text("x\n", encoding="utf-8")
    _git(code, "add", "f.txt")
    _git(code, "commit", "-m", "init")
    _git(code, "push", "origin", "main")

    # Orphan-branch worktree — created but deliberately NOT published.
    wt = tmp_path / "wt"
    try:
        _git(code, "worktree", "add", "--orphan", "-b", "watercooler/threads", str(wt))
    except subprocess.CalledProcessError:
        _git(code, "worktree", "add", "--detach", str(wt))
        _git(wt, "checkout", "--orphan", "watercooler/threads")
    _git(wt, "commit", "--allow-empty", "-m", "init threads")
    return code, wt, origin, upstream


class TestDiagnoseRemoteContext:
    def test_no_upstream_with_remote_context(self, orphan_worktree):
        _code, wt, _origin, _upstream = orphan_worktree
        report = diagnose(wt)
        assert report.parity_state == "no_upstream"
        assert report.remotes == ["origin", "upstream"]
        assert report.published_remotes == []

    def test_published_remotes_detected(self, orphan_worktree):
        _code, wt, _origin, _upstream = orphan_worktree
        # Publish the branch (no -u) — updates the remote-tracking ref but
        # does not set upstream, so the branch is still no_upstream.
        _git(wt, "push", "origin", "watercooler/threads")
        report = diagnose(wt)
        assert report.parity_state == "no_upstream"
        assert "origin" in report.published_remotes
        assert "upstream" not in report.published_remotes

    def test_format_report_surfaces_no_upstream(self, orphan_worktree):
        from watercooler.sync_repair import format_report

        _code, wt, _origin, _upstream = orphan_worktree
        text = format_report(diagnose(wt))
        assert "NO REMOTE UPSTREAM" in text
        assert "publish_remote='origin'" in text


class TestPublishRemoteRepair:
    def test_publish_remote_sets_upstream(self, orphan_worktree):
        _code, wt, origin, _upstream = orphan_worktree
        actions = repair(wt, publish_remote="origin")
        assert any("Published" in a and "origin" in a for a in actions), actions
        # The branch now has an upstream — no longer no_upstream.
        assert diagnose(wt).parity_state != "no_upstream"
        on_origin = subprocess.run(
            ["git", "-C", str(origin), "branch", "--list", "watercooler/threads"],
            capture_output=True, text=True,
        ).stdout
        assert "watercooler/threads" in on_origin

    def test_publish_unknown_remote_fails_cleanly(self, orphan_worktree):
        _code, wt, _origin, _upstream = orphan_worktree
        actions = repair(wt, publish_remote="nonexistent")
        assert any("FAILED" in a and "no such remote" in a for a in actions), actions
        assert diagnose(wt).parity_state == "no_upstream"

    def test_publish_remote_dry_run_is_inert(self, orphan_worktree):
        _code, wt, _origin, _upstream = orphan_worktree
        actions = repair(wt, publish_remote="origin", dry_run=True)
        assert any("[DRY RUN]" in a for a in actions), actions
        assert diagnose(wt).parity_state == "no_upstream"


class TestNoUpstreamIsRepairNeeded:
    """Codex review: a no_upstream report must not render as Status: OK."""

    def test_no_upstream_sets_needs_repair(self, orphan_worktree):
        _code, wt, _origin, _upstream = orphan_worktree
        report = diagnose(wt)
        assert report.parity_state == "no_upstream"
        assert report.needs_repair is True

    def test_format_report_footer_not_ok(self, orphan_worktree):
        from watercooler.sync_repair import format_report

        _code, wt, _origin, _upstream = orphan_worktree
        text = format_report(diagnose(wt))
        assert "NEEDS REPAIR" in text
        assert "Status: OK" not in text


class TestPublishTargetSelection:
    """Codex review: prefer the remote the branch is already published to."""

    def test_prefers_sole_published_remote_over_origin(self):
        from watercooler.sync_repair import suggest_publish_remote

        # branch already lives on 'upstream' only — don't fork it onto origin
        assert suggest_publish_remote(["origin", "upstream"], ["upstream"]) == "upstream"

    def test_falls_back_to_origin_when_unpublished(self):
        from watercooler.sync_repair import suggest_publish_remote

        assert suggest_publish_remote(["origin", "upstream"], []) == "origin"

    def test_lone_remote_when_no_origin(self):
        from watercooler.sync_repair import suggest_publish_remote

        assert suggest_publish_remote(["bitbucket"], []) == "bitbucket"

    def test_ambiguous_returns_none(self):
        from watercooler.sync_repair import suggest_publish_remote

        assert suggest_publish_remote(["a", "b"], []) is None
        assert suggest_publish_remote(["a", "b"], ["a", "b"]) is None

    def test_multiple_published_returns_none_even_with_origin(self):
        from watercooler.sync_repair import suggest_publish_remote

        # Branch already on several remotes — must not fall back to origin
        # (that would recommend forking a third copy).
        assert suggest_publish_remote(
            ["origin", "upstream", "fork"], ["upstream", "fork"]
        ) is None

    def test_format_report_recommends_already_published_remote(self, orphan_worktree):
        from watercooler.sync_repair import format_report

        _code, wt, _origin, _upstream = orphan_worktree
        # Publish to 'upstream' only (no -u) — branch stays no_upstream.
        _git(wt, "push", "upstream", "watercooler/threads")
        report = diagnose(wt)
        assert report.published_remotes == ["upstream"]
        text = format_report(report)
        assert "publish_remote='upstream'" in text
        assert "publish_remote='origin'" not in text
