"""Tests for parity warning formatting and sync_repair parity alignment.

Covers:
- format_parity_warning() returns correct banners for each state
- DiagnosticReport.parity_state maps to canonical vocabulary
"""

from __future__ import annotations

import pytest

from unittest.mock import MagicMock

from watercooler_mcp.sync import format_parity_warning
from watercooler_mcp.sync.primitives import should_discard_dirty_entry
from watercooler.sync_repair import (
    DiagnosticReport,
    is_derived_file,
    is_untracked_write_once_projection,
)


class TestFormatParityWarning:
    """format_parity_warning() returns banners for problematic states."""

    def test_clean_returns_empty(self) -> None:
        assert format_parity_warning("clean") == ""

    def test_behind_only_returns_empty(self) -> None:
        assert format_parity_warning("behind_only") == ""

    def test_ahead_only_returns_empty(self) -> None:
        assert format_parity_warning("ahead_only") == ""

    def test_no_upstream_returns_empty(self) -> None:
        assert format_parity_warning("no_upstream") == ""

    def test_diverged_returns_banner(self) -> None:
        result = format_parity_warning("diverged")
        assert result.startswith("⚠")
        assert "diverged" in result
        assert "stale" in result

    def test_dirty_mixed_returns_banner(self) -> None:
        result = format_parity_warning("dirty_mixed")
        assert result.startswith("⚠")
        assert "uncommitted" in result

    def test_stuck_rebase_returns_banner(self) -> None:
        result = format_parity_warning("stuck_rebase_or_merge")
        assert result.startswith("⚠")
        assert "rebase" in result.lower() or "merge" in result.lower()

    def test_auth_error_returns_banner(self) -> None:
        result = format_parity_warning("auth_or_network_error")
        assert result.startswith("⚠")
        assert "cached" in result.lower()

    def test_unknown_state_returns_empty(self) -> None:
        assert format_parity_warning("some_future_state") == ""

    def test_dirty_derived_only_returns_empty(self) -> None:
        """dirty_derived_only is benign (auto-cleanable), no warning."""
        assert format_parity_warning("dirty_derived_only") == ""


class TestDiagnosticReportParityState:
    """DiagnosticReport.parity_state maps to canonical vocabulary."""

    def test_clean(self) -> None:
        r = DiagnosticReport(tracking="origin/watercooler/threads")
        assert r.parity_state == "clean"

    def test_behind_only(self) -> None:
        r = DiagnosticReport(tracking="origin/watercooler/threads", behind=3)
        assert r.parity_state == "behind_only"

    def test_ahead_only(self) -> None:
        r = DiagnosticReport(tracking="origin/watercooler/threads", ahead=2)
        assert r.parity_state == "ahead_only"

    def test_diverged(self) -> None:
        r = DiagnosticReport(tracking="origin/watercooler/threads", ahead=1, behind=2)
        assert r.parity_state == "diverged"

    def test_stuck_rebase(self) -> None:
        r = DiagnosticReport(tracking="origin/watercooler/threads", stuck_rebase=True)
        assert r.parity_state == "stuck_rebase_or_merge"

    def test_stuck_merge(self) -> None:
        r = DiagnosticReport(tracking="origin/watercooler/threads", stuck_merge=True)
        assert r.parity_state == "stuck_rebase_or_merge"

    def test_no_upstream(self) -> None:
        r = DiagnosticReport()  # no tracking
        assert r.parity_state == "no_upstream"

    def test_dirty_derived_only(self) -> None:
        r = DiagnosticReport(
            tracking="origin/watercooler/threads",
            dirty_files=[" M graph/baseline/threads/topic/annotation_state.json"],
        )
        assert r.parity_state == "dirty_derived_only"

    def test_dirty_mixed(self) -> None:
        r = DiagnosticReport(
            tracking="origin/watercooler/threads",
            dirty_files=[" M some_other_file.txt"],
        )
        assert r.parity_state == "dirty_mixed"

    def test_errors_map_to_auth(self) -> None:
        r = DiagnosticReport(
            tracking="origin/watercooler/threads",
            errors=["Connection refused"],
        )
        assert r.parity_state == "auth_or_network_error"

    def test_stuck_takes_priority_over_dirty(self) -> None:
        """stuck_rebase_or_merge is higher priority than dirty state."""
        r = DiagnosticReport(
            tracking="origin/watercooler/threads",
            stuck_rebase=True,
            dirty_files=[" M something.txt"],
        )
        assert r.parity_state == "stuck_rebase_or_merge"

    def test_diverged_takes_priority_over_dirty(self) -> None:
        """diverged is higher priority than dirty state."""
        r = DiagnosticReport(
            tracking="origin/watercooler/threads",
            ahead=1,
            behind=1,
            dirty_files=[" M something.txt"],
        )
        assert r.parity_state == "diverged"

    def test_dirty_slack_mappings_is_derived(self) -> None:
        """A dirty slack-mappings projection classifies as dirty_derived_only."""
        r = DiagnosticReport(
            tracking="origin/watercooler/threads",
            behind=21,
            dirty_files=[" M .watercooler/slack-mappings/some-topic.json"],
        )
        assert r.parity_state == "dirty_derived_only"

    def test_dirty_slack_mappings_plus_real_file_is_mixed(self) -> None:
        """slack-mappings + a non-derived file stays dirty_mixed."""
        r = DiagnosticReport(
            tracking="origin/watercooler/threads",
            dirty_files=[
                " M .watercooler/slack-mappings/some-topic.json",
                " M graph/baseline/threads/topic/entries.jsonl",
            ],
        )
        assert r.parity_state == "dirty_mixed"


class TestIsDerivedFile:
    """is_derived_file() recognizes derived caches by basename and path prefix."""

    def test_annotation_state_basename(self) -> None:
        assert is_derived_file("annotation_state.json") is True

    def test_annotation_state_nested(self) -> None:
        assert is_derived_file("graph/baseline/threads/topic/annotation_state.json") is True

    def test_slack_mappings_prefix(self) -> None:
        assert is_derived_file(".watercooler/slack-mappings/my-topic.json") is True

    def test_slack_mappings_nested(self) -> None:
        assert is_derived_file(".watercooler/slack-mappings/sub/deep.json") is True

    def test_slack_mappings_backslash_normalized(self) -> None:
        assert is_derived_file(".watercooler\\slack-mappings\\my-topic.json") is True

    def test_real_entry_file_not_derived(self) -> None:
        assert is_derived_file("graph/baseline/threads/topic/entries.jsonl") is False

    def test_roles_toml_not_derived(self) -> None:
        assert is_derived_file(".watercooler/roles.toml") is False

    def test_empty_not_derived(self) -> None:
        assert is_derived_file("") is False


class TestIsUntrackedWriteOnceProjection:
    """is_untracked_write_once_projection() flags un-pushed sole-copy projections."""

    _SLACK = ".watercooler/slack-mappings/new-topic.json"
    _ANNO = "graph/baseline/threads/topic/annotation_state.json"

    def test_untracked_slack_mapping_is_projection(self) -> None:
        assert is_untracked_write_once_projection("??", self._SLACK) is True

    def test_untracked_slack_mapping_backslash_normalized(self) -> None:
        assert is_untracked_write_once_projection("??", ".watercooler\\slack-mappings\\t.json") is True

    def test_tracked_modified_slack_mapping_is_not(self) -> None:
        # Tracked churn is restorable from origin — not a sole-copy projection.
        assert is_untracked_write_once_projection(" M", self._SLACK) is False

    def test_staged_slack_mapping_is_not(self) -> None:
        assert is_untracked_write_once_projection("A ", self._SLACK) is False

    def test_untracked_basename_cache_is_not(self) -> None:
        # annotation_state.json is locally regenerable, not a write-once projection.
        assert is_untracked_write_once_projection("??", self._ANNO) is False

    def test_untracked_non_derived_is_not(self) -> None:
        assert is_untracked_write_once_projection("??", "graph/baseline/threads/topic/entries.jsonl") is False


class TestShouldDiscardDirtyEntry:
    """should_discard_dirty_entry() preserves sole-copy projections, discards the rest."""

    _SLACK = ".watercooler/slack-mappings/new-topic.json"
    _BRANCH = "watercooler/threads"

    def _repo(self, *, origin_has_path: bool) -> MagicMock:
        repo = MagicMock()
        if origin_has_path:
            repo.git.cat_file.return_value = ""
        else:
            repo.git.cat_file.side_effect = Exception("missing in origin")
        return repo

    def test_untracked_projection_absent_on_origin_preserved(self) -> None:
        repo = self._repo(origin_has_path=False)
        assert should_discard_dirty_entry(repo, "??", self._SLACK, branch=self._BRANCH) is False
        repo.git.cat_file.assert_called_once_with("-e", f"origin/{self._BRANCH}:{self._SLACK}")

    def test_untracked_projection_present_on_origin_discarded(self) -> None:
        # Origin already tracks the path → not the sole copy → safe to discard.
        repo = self._repo(origin_has_path=True)
        assert should_discard_dirty_entry(repo, "??", self._SLACK, branch=self._BRANCH) is True

    def test_tracked_modified_projection_discarded_without_origin_query(self) -> None:
        repo = self._repo(origin_has_path=False)
        assert should_discard_dirty_entry(repo, " M", self._SLACK, branch=self._BRANCH) is True
        repo.git.cat_file.assert_not_called()  # tracked → no origin lookup needed

    def test_non_derived_never_discarded(self) -> None:
        repo = self._repo(origin_has_path=True)
        assert should_discard_dirty_entry(repo, " M", "graph/baseline/threads/topic/entries.jsonl", branch=self._BRANCH) is False
        repo.git.cat_file.assert_not_called()

    def test_untracked_basename_cache_discarded(self) -> None:
        repo = self._repo(origin_has_path=False)
        assert should_discard_dirty_entry(repo, "??", "graph/baseline/threads/topic/annotation_state.json", branch=self._BRANCH) is True
        repo.git.cat_file.assert_not_called()  # basename cache → not a projection
