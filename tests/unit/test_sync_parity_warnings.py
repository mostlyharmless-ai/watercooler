"""Tests for parity warning formatting and sync_repair parity alignment.

Covers:
- format_parity_warning() returns correct banners for each state
- DiagnosticReport.parity_state maps to canonical vocabulary
"""

from __future__ import annotations

import pytest

from watercooler_mcp.sync import format_parity_warning
from watercooler.sync_repair import DiagnosticReport


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
