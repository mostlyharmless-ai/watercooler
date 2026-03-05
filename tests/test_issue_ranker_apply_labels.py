"""Tests for the _validate_plan security gate in the issue-ranker apply_labels script."""

import sys
from pathlib import Path

import pytest

# Skill scripts are not installable packages; import directly from the source tree.
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude/skills/issue-ranker/scripts"))

from apply_labels import PRIORITY_LABELS, SEV_LABELS, _validate_plan  # noqa: E402


def test_validate_plan_accepts_valid_plan() -> None:
  plan = [
    {"number": 1, "priority": "priority:now", "sev": "sev:critical"},
    {"number": 2, "priority": "priority:next", "sev": "sev:high"},
    {"number": 3, "priority": "priority:soon", "sev": "sev:medium"},
    {"number": 4, "priority": "priority:backlog", "sev": "sev:low"},
  ]
  _validate_plan(plan)  # must not raise


def test_validate_plan_accepts_empty_plan() -> None:
  _validate_plan([])  # empty plan is valid — no issues to update


def test_validate_plan_rejects_invalid_priority() -> None:
  plan = [{"number": 1, "priority": "priority:invalid", "sev": "sev:high"}]
  with pytest.raises(ValueError, match="invalid priority"):
    _validate_plan(plan)


def test_validate_plan_rejects_invalid_sev() -> None:
  plan = [{"number": 1, "priority": "priority:now", "sev": "sev:unknown"}]
  with pytest.raises(ValueError, match="invalid sev"):
    _validate_plan(plan)


def test_validate_plan_rejects_duplicate_number() -> None:
  plan = [
    {"number": 42, "priority": "priority:now", "sev": "sev:critical"},
    {"number": 42, "priority": "priority:next", "sev": "sev:high"},
  ]
  with pytest.raises(ValueError, match="duplicate issue number"):
    _validate_plan(plan)


def test_validate_plan_rejects_string_number() -> None:
  plan = [{"number": "123", "priority": "priority:now", "sev": "sev:critical"}]
  with pytest.raises(ValueError, match="invalid number"):
    _validate_plan(plan)


def test_validate_plan_rejects_float_number() -> None:
  plan = [{"number": 1.5, "priority": "priority:now", "sev": "sev:critical"}]
  with pytest.raises(ValueError, match="invalid number"):
    _validate_plan(plan)


def test_validate_plan_rejects_zero_number() -> None:
  plan = [{"number": 0, "priority": "priority:now", "sev": "sev:critical"}]
  with pytest.raises(ValueError, match="invalid number"):
    _validate_plan(plan)


def test_validate_plan_rejects_negative_number() -> None:
  plan = [{"number": -5, "priority": "priority:now", "sev": "sev:critical"}]
  with pytest.raises(ValueError, match="invalid number"):
    _validate_plan(plan)


def test_validate_plan_rejects_missing_number() -> None:
  plan = [{"priority": "priority:now", "sev": "sev:critical"}]
  with pytest.raises(ValueError, match="invalid number"):
    _validate_plan(plan)


def test_validate_plan_rejects_bool_true() -> None:
  # bool is a subclass of int in Python, so isinstance(True, int) is True.
  # A JSON plan with {"number": true} must be rejected, not silently treated as #1.
  plan = [{"number": True, "priority": "priority:now", "sev": "sev:critical"}]
  with pytest.raises(ValueError, match="invalid number"):
    _validate_plan(plan)


def test_validate_plan_rejects_bool_false() -> None:
  plan = [{"number": False, "priority": "priority:now", "sev": "sev:critical"}]
  with pytest.raises(ValueError, match="invalid number"):
    _validate_plan(plan)


def test_validate_plan_covers_all_priority_labels() -> None:
  """Every valid priority label must be accepted."""
  for priority in PRIORITY_LABELS:
    _validate_plan([{"number": 1, "priority": priority, "sev": "sev:low"}])


def test_validate_plan_covers_all_sev_labels() -> None:
  """Every valid sev label must be accepted."""
  for sev in SEV_LABELS:
    _validate_plan([{"number": 1, "priority": "priority:backlog", "sev": sev}])


def test_validate_plan_reports_first_bad_entry_index() -> None:
  """Error message includes the zero-based plan entry index."""
  plan = [
    {"number": 10, "priority": "priority:now", "sev": "sev:critical"},
    {"number": 20, "priority": "priority:now", "sev": "sev:BOGUS"},
  ]
  with pytest.raises(ValueError, match="entry 1"):
    _validate_plan(plan)
