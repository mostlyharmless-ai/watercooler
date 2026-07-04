"""Tests for the sync_guard daemon."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.daemons.sync_guard import SyncGuardDaemon


@pytest.fixture
def daemon(tmp_path: Path) -> SyncGuardDaemon:
    """Create a SyncGuardDaemon with a tmp threads_dir containing .git."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    (threads_dir / ".git").mkdir()
    return SyncGuardDaemon(interval=60.0, threads_dir=threads_dir)


# Common patch targets — the daemon uses lazy imports from these modules.
_HOSTED = "watercooler_mcp.daemons.hosted_data.is_daemon_hosted_mode"
_REPO = "git.Repo"
_FETCH = "watercooler_mcp.sync.primitives.fetch_with_timeout"
_PARITY = "watercooler_mcp.sync.primitives.get_parity_state"
_PULL = "watercooler_mcp.sync.primitives.pull_ff_only"
_DERIVED = "watercooler.sync_repair.DERIVED_FILE_PATTERNS"


@patch(_HOSTED, return_value=False)
class TestSyncGuardDaemon:
    """Tests for SyncGuardDaemon.tick()."""

    @patch(_PARITY, return_value="clean")
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_clean_state(self, _repo, _fetch, _parity, _hosted, daemon):
        """Clean state produces no findings."""
        assert daemon.tick() == []

    @patch(_PARITY, return_value="ahead_only")
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_ahead_only_noop(self, _repo, _fetch, _parity, _hosted, daemon):
        """ahead_only state produces no findings."""
        assert daemon.tick() == []

    @patch(_PARITY, return_value="no_upstream")
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_no_upstream_noop(self, _repo, _fetch, _parity, _hosted, daemon):
        """no_upstream state produces no findings."""
        assert daemon.tick() == []

    @patch(_PULL, return_value=True)
    @patch(_PARITY, return_value="behind_only")
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_behind_only_heals(self, _repo, _fetch, _parity, mock_pull, _hosted, daemon):
        """behind_only state triggers pull and returns healed finding."""
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_healed"
        assert "fast-forward" in findings[0].message
        assert findings[0].details["parity_state"] == "behind_only"
        mock_pull.assert_called_once()

    @patch(_PULL, side_effect=Exception("pull failed"))
    @patch(_PARITY, return_value="behind_only")
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_behind_only_pull_fails(self, _repo, _fetch, _parity, _pull, _hosted, daemon):
        """behind_only with failed pull returns warning."""
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_warning"
        assert findings[0].severity == "warning"

    @patch(_PULL, return_value=True)
    @patch(_PARITY, side_effect=["dirty_derived_only", "behind_only"])
    @patch(_FETCH)
    @patch(_REPO)
    @patch(_DERIVED, frozenset({"annotation_state.json"}))
    def test_tick_dirty_derived_cleans(
        self, _repo, _fetch, _parity, _pull, _hosted, daemon
    ):
        """dirty_derived_only state cleans derived files and pulls."""
        mock_git = MagicMock()
        mock_git.status.return_value = " M annotation_state.json"
        _repo.return_value.git = mock_git

        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_healed"
        assert "derived" in findings[0].message.lower()

    @patch(_PARITY, return_value="diverged")
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_diverged_warns(self, _repo, _fetch, _parity, _hosted, daemon):
        """diverged state returns warning, does not attempt repair."""
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_warning"
        assert "diverged" in findings[0].message
        assert findings[0].details["parity_state"] == "diverged"

    @patch(_PARITY, return_value="dirty_mixed")
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_dirty_mixed_warns(self, _repo, _fetch, _parity, _hosted, daemon):
        """dirty_mixed state returns warning."""
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_warning"
        assert "dirty_mixed" in findings[0].message

    @patch(_PARITY, return_value="stuck_rebase_or_merge")
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_stuck_warns(self, _repo, _fetch, _parity, _hosted, daemon):
        """stuck_rebase_or_merge state returns warning."""
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_warning"
        assert "stuck" in findings[0].message.lower()

    @patch(_PARITY, return_value="auth_or_network_error")
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_auth_error_warns(self, _repo, _fetch, _parity, _hosted, daemon):
        """auth_or_network_error state returns warning."""
        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_warning"
        assert "network" in findings[0].message.lower() or "credentials" in findings[0].message.lower()

    def test_tick_no_threads_dir(self, _hosted):
        """No threads_dir returns empty findings without crashing."""
        d = SyncGuardDaemon(interval=60.0, threads_dir=Path("/nonexistent/path"))
        assert d.tick() == []


    @patch(_PULL, return_value=True)
    @patch(_PARITY, side_effect=["dirty_derived_only", "behind_only"])
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_dirty_slack_mappings_cleans(
        self, _repo, _fetch, _parity, _pull, _hosted, daemon
    ):
        """A dirty slack-mappings projection is treated as derived: cleaned + pulled."""
        mock_git = MagicMock()
        mock_git.status.return_value = " M .watercooler/slack-mappings/some-topic.json"
        _repo.return_value.git = mock_git

        findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_healed"
        assert "derived" in findings[0].message.lower()

    @staticmethod
    def _wire_repo(_repo, status: str, *, origin_has_path: bool) -> MagicMock:
        """Configure the mocked Repo: status output, branch, and origin lookup."""
        mock_git = MagicMock()
        mock_git.status.return_value = status
        if origin_has_path:
            mock_git.cat_file.return_value = ""
        else:
            mock_git.cat_file.side_effect = Exception("missing in origin")
        _repo.return_value.git = mock_git
        _repo.return_value.head.is_detached = False
        _repo.return_value.active_branch.name = "watercooler/threads"
        return mock_git

    @patch(_PULL, return_value=True)
    @patch(_PARITY, side_effect=["dirty_derived_only", "dirty_derived_only"])
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_untracked_slack_mapping_preserved(
        self, _repo, _fetch, _parity, _pull, _hosted, daemon
    ):
        """An untracked slack-mapping absent from origin is preserved (its sole
        un-pushed copy) while the worktree still fast-forwards (#924)."""
        rel = ".watercooler/slack-mappings/new-topic.json"
        mapping = daemon._threads_dir_override / rel
        mapping.parent.mkdir(parents=True, exist_ok=True)
        mapping.write_text('{"slackThreadTs": "1700000000.000100"}')

        mock_git = self._wire_repo(_repo, f"?? {rel}", origin_has_path=False)

        findings = daemon.tick()

        assert mapping.exists()  # preserved, never unlinked
        mock_git.cat_file.assert_called_once()  # origin was consulted
        mock_git.checkout.assert_not_called()  # untracked → skipped before checkout
        _pull.assert_called_once()  # behind state still cleared via ff-pull
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_healed"

    @patch(_PULL, return_value=True)
    @patch(_PARITY, side_effect=["dirty_derived_only", "behind_only"])
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_untracked_mapping_on_origin_is_discarded(
        self, _repo, _fetch, _parity, _pull, _hosted, daemon
    ):
        """An untracked mapping origin already tracks is NOT the sole copy: discard
        it (to take origin's) so the worktree can fast-forward (#924)."""
        rel = ".watercooler/slack-mappings/dup-topic.json"
        mapping = daemon._threads_dir_override / rel
        mapping.parent.mkdir(parents=True, exist_ok=True)
        mapping.write_text('{"slackThreadTs": "local-divergent"}')

        mock_git = self._wire_repo(_repo, f"?? {rel}", origin_has_path=True)

        findings = daemon.tick()

        assert not mapping.exists()  # discarded — origin holds the canonical copy
        mock_git.cat_file.assert_called_once()
        _pull.assert_called_once()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_healed"

    @patch(_PULL, return_value=False)
    @patch(_PARITY, side_effect=["dirty_derived_only", "dirty_derived_only"])
    @patch(_FETCH)
    @patch(_REPO)
    def test_tick_dirty_derived_pull_fails_warns(
        self, _repo, _fetch, _parity, _pull, _hosted, daemon
    ):
        """When the post-clean ff-pull can't resolve the behind state, warn rather
        than falsely report healed (broadened pull gate, #924)."""
        # Tracked-modified projection → discarded without an origin query.
        self._wire_repo(_repo, " M .watercooler/slack-mappings/x.json", origin_has_path=False)

        findings = daemon.tick()

        _pull.assert_called_once()
        assert len(findings) == 1
        assert findings[0].category == "sync_guard_warning"
        assert findings[0].details["parity_state"] == "dirty_derived_only"


@patch(_HOSTED, return_value=True)
class TestSyncGuardHosted:
    """Tests for hosted mode behavior."""

    def test_tick_hosted_mode_skips(self, _hosted, tmp_path):
        """Hosted mode returns empty findings immediately (no override)."""
        d = SyncGuardDaemon(interval=60.0)
        assert d.tick() == []


def _make_worktree(base: Path, name: str, *, git: bool = True) -> Path:
    """Create a fake served worktree dir under base."""
    wt = base / name
    wt.mkdir(parents=True)
    if git:
        (wt / ".git").mkdir()
    return wt


@patch(_HOSTED, return_value=False)
class TestSyncGuardMultiWorktree:
    """sync_guard sweeps every served worktree under WORKTREE_BASE (no override)."""

    @staticmethod
    def _wire(monkeypatch, base: Path) -> None:
        from types import SimpleNamespace

        monkeypatch.setattr("watercooler_mcp.config.WORKTREE_BASE", base)
        # No primary worktree from CWD — exercise pure enumeration.
        monkeypatch.setattr(
            "watercooler_mcp.config.resolve_thread_context",
            lambda *a, **k: SimpleNamespace(threads_dir=None),
        )

    @patch(_PULL, return_value=True)
    @patch(_PARITY, side_effect=["behind_only", "behind_only"])
    @patch(_FETCH)
    @patch(_REPO)
    def test_sweeps_all_worktrees(
        self, _repo, _fetch, _parity, _pull, _hosted, tmp_path, monkeypatch
    ):
        base = tmp_path / "worktrees"
        base.mkdir()
        _make_worktree(base, "repoA")
        _make_worktree(base, "repoB")
        self._wire(monkeypatch, base)

        d = SyncGuardDaemon(interval=60.0)  # no override → sweep
        findings = d.tick()
        assert len(findings) == 2
        assert all(f.category == "sync_guard_healed" for f in findings)
        assert {f.details["worktree"] for f in findings} == {"repoA", "repoB"}

    @patch(_PULL, return_value=True)
    @patch(_PARITY, side_effect=["behind_only", "diverged"])
    @patch(_FETCH)
    @patch(_REPO)
    def test_one_behind_one_diverged(
        self, _repo, _fetch, _parity, _pull, _hosted, tmp_path, monkeypatch
    ):
        base = tmp_path / "worktrees"
        base.mkdir()
        _make_worktree(base, "repoA")  # sorted first → behind_only → healed
        _make_worktree(base, "repoB")  # diverged → warning
        self._wire(monkeypatch, base)

        findings = SyncGuardDaemon(interval=60.0).tick()
        by_wt = {f.details["worktree"]: f for f in findings}
        assert by_wt["repoA"].category == "sync_guard_healed"
        assert by_wt["repoB"].category == "sync_guard_warning"
        assert by_wt["repoB"].details["parity_state"] == "diverged"

    @patch(_PULL, return_value=True)
    @patch(_PARITY, side_effect=["behind_only"])
    @patch(_FETCH)
    @patch(_REPO)
    def test_non_git_dir_skipped(
        self, _repo, _fetch, _parity, _pull, _hosted, tmp_path, monkeypatch
    ):
        base = tmp_path / "worktrees"
        base.mkdir()
        _make_worktree(base, "notgit", git=False)  # sorted first → skipped (no .git)
        _make_worktree(base, "repoA")               # behind_only → healed
        self._wire(monkeypatch, base)

        findings = SyncGuardDaemon(interval=60.0).tick()
        assert len(findings) == 1
        assert findings[0].details["worktree"] == "repoA"

    @patch(_PARITY, side_effect=[RuntimeError("boom"), "behind_only"])
    @patch(_PULL, return_value=True)
    @patch(_FETCH)
    @patch(_REPO)
    def test_per_worktree_error_isolation(
        self, _repo, _fetch, _pull, _parity, _hosted, tmp_path, monkeypatch
    ):
        base = tmp_path / "worktrees"
        base.mkdir()
        _make_worktree(base, "repoA")  # parity raises → isolated, no finding
        _make_worktree(base, "repoB")  # behind_only → healed
        self._wire(monkeypatch, base)

        findings = SyncGuardDaemon(interval=60.0).tick()
        assert len(findings) == 1
        assert findings[0].details["worktree"] == "repoB"
        assert findings[0].category == "sync_guard_healed"
