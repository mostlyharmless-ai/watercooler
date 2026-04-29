"""Unit tests for the MigrationSummary result type."""

from __future__ import annotations

import json

from watercooler.migration.summary import MigrationSummary


class TestMigrationSummary:
    def test_default_is_clean(self) -> None:
        s = MigrationSummary(tier="t1", direction="stdio_to_hybrid", dry_run=False)
        assert s.is_clean()
        assert s.errored == 0
        assert s.pushed == 0

    def test_errored_breaks_cleanliness(self) -> None:
        s = MigrationSummary(tier="t1", direction="stdio_to_hybrid", dry_run=False)
        s.errored = 1
        assert not s.is_clean()

    def test_not_implemented_breaks_cleanliness_distinct_from_errored(self) -> None:
        """Round-3 review LOW-2: not_implemented and errored are distinct signals."""
        s = MigrationSummary(tier="t2", direction="hybrid_to_stdio", dry_run=False)
        s.not_implemented = True
        assert not s.is_clean()
        assert s.errored == 0
        assert s.exit_code() == 64  # EX_USAGE

    def test_exit_code_zero_when_clean(self) -> None:
        s = MigrationSummary(tier="t1", direction="stdio_to_hybrid", dry_run=False)
        assert s.exit_code() == 0

    def test_exit_code_two_when_errored(self) -> None:
        s = MigrationSummary(tier="t1", direction="stdio_to_hybrid", dry_run=False)
        s.errored = 1
        assert s.exit_code() == 2

    def test_exit_code_64_takes_precedence_over_errored(self) -> None:
        """If both flags are set, not_implemented wins (it's the structural signal)."""
        s = MigrationSummary(tier="t2", direction="hybrid_to_stdio", dry_run=False)
        s.not_implemented = True
        s.errored = 5  # nonsensical but defensive
        assert s.exit_code() == 64

    def test_to_json_round_trip(self) -> None:
        s = MigrationSummary(
            tier="t2",
            direction="hybrid_to_stdio",
            dry_run=True,
            total_scanned=10,
            pushed=8,
            errored=2,
            elapsed_seconds=1.5,
            notes=["note A", "note B"],
        )
        out = s.to_json()
        d = json.loads(out)
        assert d["tier"] == "t2"
        assert d["direction"] == "hybrid_to_stdio"
        assert d["dry_run"] is True
        assert d["total_scanned"] == 10
        assert d["pushed"] == 8
        assert d["errored"] == 2
        assert d["elapsed_seconds"] == 1.5
        assert d["notes"] == ["note A", "note B"]
