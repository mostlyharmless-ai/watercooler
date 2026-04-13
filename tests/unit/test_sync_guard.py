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


@patch(_HOSTED, return_value=True)
class TestSyncGuardHosted:
    """Tests for hosted mode behavior."""

    def test_tick_hosted_mode_skips(self, _hosted, tmp_path):
        """Hosted mode returns empty findings immediately."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / ".git").mkdir()
        d = SyncGuardDaemon(interval=60.0, threads_dir=threads_dir)
        assert d.tick() == []
