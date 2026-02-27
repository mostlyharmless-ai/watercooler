"""Unit tests for T2 supersession filter logic.

Tests two groups:
  A - _filter_by_time_range with time_key="invalid_at" (existing utility, new usage)
  B - _filter_active_only (new helper that removes superseded facts)

No live services required.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Group A — _filter_by_time_range with time_key="invalid_at"
# ---------------------------------------------------------------------------


class TestFilterByTimeRangeOnInvalidAt:
  """Verify _filter_by_time_range works correctly when keyed on invalid_at.

  NOTE: All production call sites of _filter_by_time_range currently use the
  default time_key="created_at". The time_key="invalid_at" combination tested
  here is not yet wired — these tests document that the capability exists and
  behaves correctly if it is wired in a future change.

  TODO: wire time_key="invalid_at" for temporal validity queries (see #258).
  """

  @pytest.fixture
  def mixed_results(self):
    """Facts with a mix of invalid_at values."""
    return [
      {"uuid": "f-1", "fact": "alpha", "invalid_at": "2026-01-10T12:00:00+00:00"},
      {"uuid": "f-2", "fact": "beta",  "invalid_at": "2026-02-15T08:00:00+00:00"},
      {"uuid": "f-3", "fact": "gamma", "invalid_at": None},
      {"uuid": "f-4", "fact": "delta", "invalid_at": "2026-03-01T00:00:00+00:00"},
    ]

  def test_filter_by_invalid_at_excludes_after_end_time(self, mixed_results):
    """Facts invalidated after end_time are excluded; facts invalidated before end_time are included."""
    from watercooler_memory.backends.graphiti import _filter_by_time_range

    filtered = _filter_by_time_range(
      mixed_results,
      start_time="",
      end_time="2026-02-01T00:00:00+00:00",
      time_key="invalid_at",
    )
    uuids = [r["uuid"] for r in filtered]
    assert "f-1" in uuids        # invalidated before end_time — included
    assert "f-2" not in uuids    # invalidated after end_time — excluded
    assert "f-4" not in uuids    # invalidated after end_time — excluded

  def test_filter_by_invalid_at_excludes_null_when_filter_active(self, mixed_results):
    """Results with invalid_at=None are excluded when an end_time filter is active."""
    from watercooler_memory.backends.graphiti import _filter_by_time_range

    # When filtering with end_time, results missing the key are excluded —
    # this matches the _filter_by_time_range contract: "exclude when filters active".
    filtered = _filter_by_time_range(
      mixed_results,
      start_time="",
      end_time="2026-03-31T00:00:00+00:00",
      time_key="invalid_at",
    )
    uuids = [r["uuid"] for r in filtered]
    # f-3 has invalid_at=None → excluded (missing value when filter active).
    # NOTE: this means facts that were never superseded (open-ended, "still active")
    # would be excluded from a "state at time T" query — arguably the opposite of
    # what a user expects. This semantic gap must be addressed before wiring
    # time_key="invalid_at" in production (see #258).
    assert "f-3" not in uuids

  def test_no_filters_returns_all_unchanged(self, mixed_results):
    """No time bounds = no-op regardless of time_key."""
    from watercooler_memory.backends.graphiti import _filter_by_time_range

    result = _filter_by_time_range(mixed_results, "", "", time_key="invalid_at")
    assert result is mixed_results  # Same object, no copy


# ---------------------------------------------------------------------------
# Group B — _filter_active_only
# ---------------------------------------------------------------------------


class TestFilterActiveOnly:
  """Verify _filter_active_only removes superseded facts and passes valid ones."""

  def test_active_only_removes_superseded(self):
    """Mixed list: only invalid_at=None entries survive."""
    from watercooler_memory.backends.graphiti import _filter_active_only

    results = [
      {"uuid": "e-1", "fact": "old fact", "invalid_at": "2026-01-01T00:00:00+00:00"},
      {"uuid": "e-2", "fact": "current fact", "invalid_at": None},
      {"uuid": "e-3", "fact": "another old", "invalid_at": "2026-02-01T00:00:00+00:00"},
    ]
    filtered = _filter_active_only(results)
    assert len(filtered) == 1
    assert filtered[0]["uuid"] == "e-2"

  def test_active_only_empty_list(self):
    """Empty input returns empty output."""
    from watercooler_memory.backends.graphiti import _filter_active_only

    assert _filter_active_only([]) == []

  def test_active_only_all_valid(self):
    """All invalid_at=None → all returned unchanged."""
    from watercooler_memory.backends.graphiti import _filter_active_only

    results = [
      {"uuid": "v-1", "fact": "fact A", "invalid_at": None},
      {"uuid": "v-2", "fact": "fact B", "invalid_at": None},
    ]
    filtered = _filter_active_only(results)
    assert len(filtered) == 2
    assert [r["uuid"] for r in filtered] == ["v-1", "v-2"]

  def test_active_only_all_superseded(self):
    """All have invalid_at set → empty list returned."""
    from watercooler_memory.backends.graphiti import _filter_active_only

    results = [
      {"uuid": "s-1", "fact": "stale A", "invalid_at": "2026-01-01T00:00:00+00:00"},
      {"uuid": "s-2", "fact": "stale B", "invalid_at": "2026-02-01T00:00:00+00:00"},
    ]
    filtered = _filter_active_only(results)
    assert filtered == []

  def test_active_only_missing_key_treated_as_valid(self):
    """Entries without invalid_at key pass through (absence ≈ None)."""
    from watercooler_memory.backends.graphiti import _filter_active_only

    results = [
      {"uuid": "n-1", "fact": "no key"},
      {"uuid": "n-2", "fact": "has key", "invalid_at": "2026-01-01T00:00:00+00:00"},
    ]
    filtered = _filter_active_only(results)
    assert len(filtered) == 1
    assert filtered[0]["uuid"] == "n-1"

  def test_active_only_order_preserved(self):
    """Result order is preserved after filtering."""
    from watercooler_memory.backends.graphiti import _filter_active_only

    results = [
      {"uuid": "z-3", "fact": "third valid",  "invalid_at": None},
      {"uuid": "z-1", "fact": "first stale",  "invalid_at": "2026-01-01T00:00:00+00:00"},
      {"uuid": "z-2", "fact": "second valid", "invalid_at": None},
    ]
    filtered = _filter_active_only(results)
    assert [r["uuid"] for r in filtered] == ["z-3", "z-2"]
