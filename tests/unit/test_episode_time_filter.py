"""Tests for episode/facts time-range post-filtering (issue #148).

Verifies that:
- _filter_by_time_range correctly filters by start_time, end_time, or both
- Missing/unparseable timestamps are excluded when filters are active
- No-op when no filters are provided
- Over-fetch + trim behavior in the MCP layer
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


class TestFilterByTimeRange:
    """Unit tests for the _filter_by_time_range helper."""

    @pytest.fixture
    def sample_results(self):
        """Sample results with created_at timestamps."""
        return [
            {"uuid": "ep-1", "name": "Early", "created_at": "2026-01-15T10:00:00+00:00"},
            {"uuid": "ep-2", "name": "Mid", "created_at": "2026-02-01T12:00:00+00:00"},
            {"uuid": "ep-3", "name": "Late", "created_at": "2026-02-08T18:00:00+00:00"},
            {"uuid": "ep-4", "name": "Latest", "created_at": "2026-02-10T08:00:00+00:00"},
        ]

    def test_no_filters_returns_all(self, sample_results):
        """No time filters should return all results unchanged."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        filtered = _filter_by_time_range(sample_results, "", "")
        assert len(filtered) == 4
        assert filtered is sample_results  # Same list object (no copy)

    def test_start_time_only(self, sample_results):
        """Only start_time should filter out earlier results."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        filtered = _filter_by_time_range(sample_results, "2026-02-01T00:00:00+00:00", "")
        assert len(filtered) == 3
        assert all(r["uuid"] != "ep-1" for r in filtered)

    def test_end_time_only(self, sample_results):
        """Only end_time should filter out later results."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        filtered = _filter_by_time_range(sample_results, "", "2026-02-05T00:00:00+00:00")
        assert len(filtered) == 2
        assert [r["uuid"] for r in filtered] == ["ep-1", "ep-2"]

    def test_both_filters(self, sample_results):
        """Both start and end time should keep only results in range."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        filtered = _filter_by_time_range(
            sample_results,
            "2026-02-01T00:00:00+00:00",
            "2026-02-09T00:00:00+00:00",
        )
        assert len(filtered) == 2
        assert [r["uuid"] for r in filtered] == ["ep-2", "ep-3"]

    def test_inclusive_bounds(self, sample_results):
        """Bounds should be inclusive (results at exact boundary kept)."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        filtered = _filter_by_time_range(
            sample_results,
            "2026-02-01T12:00:00+00:00",
            "2026-02-08T18:00:00+00:00",
        )
        assert len(filtered) == 2
        assert [r["uuid"] for r in filtered] == ["ep-2", "ep-3"]

    def test_missing_created_at_excluded(self):
        """Results with missing created_at should be excluded when filters active."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        results = [
            {"uuid": "ep-1", "name": "Has time", "created_at": "2026-02-05T10:00:00+00:00"},
            {"uuid": "ep-2", "name": "No time", "created_at": None},
            {"uuid": "ep-3", "name": "Missing key"},
        ]
        filtered = _filter_by_time_range(results, "2026-01-01T00:00:00+00:00", "")
        assert len(filtered) == 1
        assert filtered[0]["uuid"] == "ep-1"

    def test_unparseable_timestamp_excluded(self):
        """Results with invalid timestamps should be excluded when filters active."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        results = [
            {"uuid": "ep-1", "created_at": "2026-02-05T10:00:00+00:00"},
            {"uuid": "ep-2", "created_at": "not-a-date"},
            {"uuid": "ep-3", "created_at": ""},
        ]
        filtered = _filter_by_time_range(results, "2026-01-01T00:00:00+00:00", "")
        assert len(filtered) == 1
        assert filtered[0]["uuid"] == "ep-1"

    def test_invalid_filter_strings_skip_filtering(self):
        """Invalid filter date strings should result in no filtering."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        results = [
            {"uuid": "ep-1", "created_at": "2026-02-05T10:00:00+00:00"},
            {"uuid": "ep-2", "created_at": "2026-03-01T10:00:00+00:00"},
        ]
        # Both bounds are unparseable — should skip filtering
        filtered = _filter_by_time_range(results, "garbage", "also-garbage")
        assert len(filtered) == 2

    def test_naive_datetime_treated_as_utc(self):
        """Naive datetimes (no timezone) should be treated as UTC."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        results = [
            {"uuid": "ep-1", "created_at": "2026-02-05T10:00:00"},  # Naive
        ]
        # Filter with timezone-aware bounds
        filtered = _filter_by_time_range(
            results,
            "2026-02-01T00:00:00+00:00",
            "2026-02-10T00:00:00+00:00",
        )
        assert len(filtered) == 1

    def test_custom_time_key(self):
        """Should use custom time_key when specified."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        results = [
            {"uuid": "ep-1", "valid_at": "2026-02-05T10:00:00+00:00"},
            {"uuid": "ep-2", "valid_at": "2026-03-01T10:00:00+00:00"},
        ]
        filtered = _filter_by_time_range(
            results,
            "2026-02-01T00:00:00+00:00",
            "2026-02-10T00:00:00+00:00",
            time_key="valid_at",
        )
        assert len(filtered) == 1
        assert filtered[0]["uuid"] == "ep-1"

    def test_z_suffix_in_filter_bounds(self, sample_results):
        """Z-suffix (common in ISO 8601) should be handled on Python 3.10+."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        filtered = _filter_by_time_range(
            sample_results,
            "2026-02-01T00:00:00Z",
            "2026-02-09T00:00:00Z",
        )
        assert len(filtered) == 2
        assert [r["uuid"] for r in filtered] == ["ep-2", "ep-3"]

    def test_z_suffix_in_result_timestamps(self):
        """Z-suffix in result created_at values should be handled."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        results = [
            {"uuid": "ep-1", "created_at": "2026-02-05T10:00:00Z"},
            {"uuid": "ep-2", "created_at": "2026-03-01T10:00:00Z"},
        ]
        filtered = _filter_by_time_range(
            results,
            "2026-02-01T00:00:00+00:00",
            "2026-02-10T00:00:00+00:00",
        )
        assert len(filtered) == 1
        assert filtered[0]["uuid"] == "ep-1"

    def test_empty_results_list(self):
        """Empty input should return empty output."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        filtered = _filter_by_time_range([], "2026-01-01", "2026-12-31")
        assert filtered == []

    def test_datetime_object_in_result(self):
        """Should handle datetime objects (not just strings) in results."""
        from watercooler_memory.backends.graphiti import _filter_by_time_range

        results = [
            {"uuid": "ep-1", "created_at": datetime(2026, 2, 5, 10, 0, tzinfo=timezone.utc)},
        ]
        filtered = _filter_by_time_range(
            results,
            "2026-02-01T00:00:00+00:00",
            "2026-02-10T00:00:00+00:00",
        )
        assert len(filtered) == 1
