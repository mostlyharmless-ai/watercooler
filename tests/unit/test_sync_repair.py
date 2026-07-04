"""Tests for sync_repair.py — derived-only dirt classification and auto-clean."""

import subprocess
import pytest
from pathlib import Path

from watercooler.sync_repair import (
    DERIVED_FILE_PATTERNS,
    DiagnosticReport,
    _parse_porcelain_filename,
    diagnose,
    repair,
)


class TestDerivedFilePatterns:
    """Verify DERIVED_FILE_PATTERNS contains expected entries."""

    def test_annotation_state_is_derived(self):
        assert "annotation_state.json" in DERIVED_FILE_PATTERNS


class TestDirtyDerivedOnly:
    """Test DiagnosticReport.dirty_derived_only property."""

    def test_no_dirty_files(self):
        report = DiagnosticReport(dirty_files=[])
        assert report.dirty_derived_only is False

    def test_all_derived(self):
        report = DiagnosticReport(dirty_files=[
            " M graph/baseline/threads/topic-a/annotation_state.json",
            "?? graph/baseline/threads/topic-b/annotation_state.json",
        ])
        assert report.dirty_derived_only is True

    def test_mixed_content(self):
        report = DiagnosticReport(dirty_files=[
            " M graph/baseline/threads/topic-a/annotation_state.json",
            " M graph/baseline/threads/topic-a/meta.json",
        ])
        assert report.dirty_derived_only is False

    def test_only_content_files(self):
        report = DiagnosticReport(dirty_files=[
            " M graph/baseline/threads/topic-a/entries.jsonl",
        ])
        assert report.dirty_derived_only is False

    def test_derived_with_different_porcelain_prefixes(self):
        """Various git status prefixes should all be recognized as derived."""
        report = DiagnosticReport(dirty_files=[
            "M  graph/baseline/threads/t1/annotation_state.json",
            " M graph/baseline/threads/t2/annotation_state.json",
            "?? graph/baseline/threads/t3/annotation_state.json",
            "A  graph/baseline/threads/t4/annotation_state.json",
        ])
        assert report.dirty_derived_only is True

    def test_stripped_porcelain_lines(self):
        """diagnose() stores line.strip() — verify parsing handles that."""
        report = DiagnosticReport(dirty_files=[
            "M graph/baseline/threads/t1/annotation_state.json",
            "?? graph/baseline/threads/t2/annotation_state.json",
        ])
        assert report.dirty_derived_only is True


class TestParsePorcelainFilename:
    """Test _parse_porcelain_filename helper."""

    def test_raw_porcelain_modified(self):
        assert _parse_porcelain_filename(" M graph/baseline/threads/t/annotation_state.json") == \
            "graph/baseline/threads/t/annotation_state.json"

    def test_raw_porcelain_untracked(self):
        assert _parse_porcelain_filename("?? graph/baseline/threads/t/annotation_state.json") == \
            "graph/baseline/threads/t/annotation_state.json"

    def test_stripped_porcelain(self):
        """After line.strip(), ' M filename' becomes 'M filename'."""
        assert _parse_porcelain_filename("M graph/baseline/threads/t/annotation_state.json") == \
            "graph/baseline/threads/t/annotation_state.json"

    def test_rename(self):
        assert _parse_porcelain_filename("R  old.json -> new.json") == "new.json"

    def test_empty_string(self):
        assert _parse_porcelain_filename("") == ""

    def test_no_space(self):
        assert _parse_porcelain_filename("??") == ""

    def test_filename_starting_with_M(self):
        """Regression: old lstrip approach would eat leading M from filename."""
        assert _parse_porcelain_filename("?? META.json") == "META.json"


# ============================================================================
# repair() local-only commit handling — preserve-first behavior
#
# Regression coverage for bug-watercooler-sync-repair-resets-unpushed / #799:
# the default repair path must NEVER discard committed-but-unpushed work.
# ============================================================================


def _git(cwd: Path, *args: str) -> str:
    """Run a git command in ``cwd``, returning stdout."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


@pytest.fixture
def synced_pair(tmp_path):
    """A threads worktree tracking a local bare 'remote', both in parity.

    Returns (worktree_dir, remote_dir).
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        capture_output=True, check=True,
    )
    wt = tmp_path / "threads"
    subprocess.run(
        ["git", "init", "-b", "main", str(wt)], capture_output=True, check=True,
    )
    _git(wt, "config", "user.email", "test@example.com")
    _git(wt, "config", "user.name", "Test")
    _git(wt, "remote", "add", "origin", str(remote))
    (wt / "seed.txt").write_text("seed\n")
    _git(wt, "add", "seed.txt")
    _git(wt, "commit", "-m", "seed")
    _git(wt, "push", "-u", "origin", "main")
    return wt, remote


def _make_local_only_commit(wt: Path, filename: str, message: str) -> None:
    """Create a committed-but-unpushed commit in the worktree."""
    (wt / filename).write_text("local-only content\n")
    _git(wt, "add", filename)
    _git(wt, "commit", "-m", message)


class TestRepairPreservesLocalCommits:
    """repair() must recover local-only commits, not destroy them."""

    def test_ahead_only_recovered_by_push(self, synced_pair):
        """Default repair pushes local-only commits — no data loss."""
        wt, remote = synced_pair
        _make_local_only_commit(wt, "entry.txt", "local-only entry")
        assert diagnose(wt).ahead == 1

        actions = repair(wt)

        assert diagnose(wt).ahead == 0, "commit should now be on the remote"
        assert any("Recover" in a and "pushed" in a for a in actions), actions
        remote_log = subprocess.run(
            ["git", "-C", str(remote), "log", "--format=%s"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "local-only entry" in remote_log

    def test_default_never_resets_when_push_fails(self, synced_pair, tmp_path):
        """If the push side is broken, commits stay intact — no reset."""
        wt, _ = synced_pair
        _make_local_only_commit(wt, "stranded.txt", "stranded entry")
        # Break the remote so push cannot succeed.
        _git(wt, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

        actions = repair(wt)

        assert diagnose(wt).ahead == 1, "local-only commit must be preserved"
        assert any("FAILED" in a for a in actions), actions
        head_subject = _git(wt, "log", "-1", "--format=%s").strip()
        assert head_subject == "stranded entry"

    def test_discard_opt_in_resets(self, synced_pair):
        """discard_local_commits=True is the explicit destructive path."""
        wt, _ = synced_pair
        _make_local_only_commit(wt, "doomed.txt", "doomed entry")

        actions = repair(wt, discard_local_commits=True)

        assert diagnose(wt).ahead == 0
        assert any("Discard" in a for a in actions), actions
        # Discarded commits are captured in the recovery log first.
        recovery_log = wt / ".watercooler" / "recovery.jsonl"
        assert recovery_log.exists()
        assert "doomed entry" in recovery_log.read_text(encoding="utf-8")

    def test_dry_run_changes_nothing(self, synced_pair):
        """dry_run reports the recover action without touching HEAD."""
        wt, _ = synced_pair
        _make_local_only_commit(wt, "entry.txt", "untouched entry")

        actions = repair(wt, dry_run=True)

        assert diagnose(wt).ahead == 1
        assert any("[DRY RUN]" in a and "Recover" in a for a in actions), actions

    def test_skips_when_worktree_dirty(self, synced_pair):
        """A dirty worktree blocks the local-commit path entirely."""
        wt, _ = synced_pair
        _make_local_only_commit(wt, "entry.txt", "committed entry")
        (wt / "uncommitted.txt").write_text("work in progress\n")

        actions = repair(wt)

        assert diagnose(wt).ahead == 1
        assert any("SKIPPED" in a for a in actions), actions


# ============================================================================
# repair() fast-forwards a behind-only worktree
#
# Regression for bug-sync-worktree-poisoning: a silently-behind worktree must
# be healed by the manual tool, not reported as "No issues found".
# ============================================================================


class TestDiagnosticBehind:
    def test_needs_repair_true_when_behind(self):
        assert DiagnosticReport(behind=3, tracking="origin/main").needs_repair is True

    def test_parity_state_behind_only(self):
        assert (
            DiagnosticReport(behind=3, tracking="origin/main").parity_state
            == "behind_only"
        )

    def test_clean_in_parity_does_not_need_repair(self):
        assert DiagnosticReport(tracking="origin/main").needs_repair is False


def _push_remote_ahead(remote: Path, tmp_path: Path, msg: str = "remote-side commit") -> None:
    """Add a commit to the bare remote via a second clone, so a worktree
    tracking it becomes behind."""
    other = tmp_path / "other_clone"
    subprocess.run(["git", "clone", str(remote), str(other)], capture_output=True, check=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "Other")
    (other / "remote.txt").write_text("from remote\n")
    _git(other, "add", "remote.txt")
    _git(other, "commit", "-m", msg)
    _git(other, "push", "origin", "main")


class TestRepairFastForwardsBehind:
    def test_behind_only_fast_forwarded(self, synced_pair, tmp_path):
        """repair() fetches + fast-forwards a behind-only worktree (no pre-fetch
        needed by the operator)."""
        wt, remote = synced_pair
        _push_remote_ahead(remote, tmp_path)

        actions = repair(wt)

        assert any("Fast-forward pull" in a for a in actions), actions
        assert "No issues found" not in actions
        assert diagnose(wt).behind == 0
        assert "remote-side commit" in _git(wt, "log", "--format=%s")

    def test_behind_only_dry_run_previews_not_no_issues(self, synced_pair, tmp_path):
        wt, remote = synced_pair
        _push_remote_ahead(remote, tmp_path)
        _git(wt, "fetch", "origin")  # dry-run reflects current known state

        actions = repair(wt, dry_run=True)

        assert any("Fast-forward pull" in a and "DRY RUN" in a for a in actions), actions
        assert "No issues found" not in actions
        # dry-run must not mutate
        assert diagnose(wt).behind == 1

    def test_diverged_not_fast_forwarded(self, synced_pair, tmp_path):
        """Ahead+behind (diverged) is handled by the ahead-recovery path, not the
        ff-only block — which must not fire when ahead > 0."""
        wt, remote = synced_pair
        _push_remote_ahead(remote, tmp_path)
        _make_local_only_commit(wt, "local.txt", "local entry")
        _git(wt, "fetch", "origin")
        rep = diagnose(wt)
        assert rep.ahead == 1 and rep.behind == 1

        actions = repair(wt)
        # The ff-only line must not claim a fast-forward on a diverged tree.
        assert not any("Fast-forward pull" in a for a in actions), actions
