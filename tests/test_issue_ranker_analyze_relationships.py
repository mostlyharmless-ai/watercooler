"""Tests for analyze_relationships.py — focusing on the analyze_conflicts() fix.

The truncation bug (todo 003) was fixed in fetch_issues.py (_extract_dep_refs) and
analyze_dependencies(), but analyze_conflicts() was still reading the truncated body
directly instead of using the pre-extracted `fixes_refs` structured field.

These tests verify:
  - analyze_conflicts() prefers the `fixes_refs` field when present
  - refs beyond char 2000 are captured via the structured field (regression)
  - the fallback path (no `fixes_refs` field) still works via regex over truncated body
  - two issues both fixing the same issue are detected as a potential conflict
  - a single fixer is not flagged (need ≥2 fixers to flag a conflict)
"""

import sys
from pathlib import Path

import pytest

# Skill scripts are not installable packages; import directly from the source tree.
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude/skills/issue-ranker/scripts"))

from analyze_relationships import analyze_conflicts  # noqa: E402


def _make_issue(number: int, body: str = "", fixes_refs: list[int] | None = None) -> dict:
  """Build a minimal issue dict for testing."""
  issue: dict = {"number": number, "title": f"Issue {number}", "body": body, "labels": []}
  if fixes_refs is not None:
    issue["fixes_refs"] = fixes_refs
  return issue


# ---------------------------------------------------------------------------
# Basic conflict detection
# ---------------------------------------------------------------------------


def test_two_fixers_same_target_flagged() -> None:
  """Two issues both claiming to fix #10 → conflict detected."""
  issues = [
    _make_issue(1, fixes_refs=[10]),
    _make_issue(2, fixes_refs=[10]),
    _make_issue(10),
  ]
  conflicts = analyze_conflicts(issues)
  assert len(conflicts) == 1
  assert sorted(conflicts[0]["issues"]) == [1, 2]
  assert "10" in conflicts[0]["reason"]
  assert conflicts[0]["type"] == "potential_duplicate"
  assert conflicts[0]["confidence"] == "low"


def test_single_fixer_not_flagged() -> None:
  """Only one issue claims to fix #10 — no conflict."""
  issues = [
    _make_issue(1, fixes_refs=[10]),
    _make_issue(10),
  ]
  conflicts = analyze_conflicts(issues)
  assert conflicts == []


def test_no_fixes_refs_no_conflicts() -> None:
  """Issues with empty fixes_refs produce no conflicts."""
  issues = [
    _make_issue(1, fixes_refs=[]),
    _make_issue(2, fixes_refs=[]),
  ]
  assert analyze_conflicts(issues) == []


def test_empty_issue_list() -> None:
  assert analyze_conflicts([]) == []


# ---------------------------------------------------------------------------
# Structured field (`fixes_refs`) takes priority over body regex
# ---------------------------------------------------------------------------


def test_prefers_fixes_refs_field_over_body() -> None:
  """When `fixes_refs` is present, the body text is ignored entirely."""
  # Body says "fixes #10" but fixes_refs says [20].
  # Conflict should be detected on #20, not #10.
  issues = [
    _make_issue(1, body="fixes #10", fixes_refs=[20]),
    _make_issue(2, fixes_refs=[20]),
    _make_issue(10),
    _make_issue(20),
  ]
  conflicts = analyze_conflicts(issues)
  assert len(conflicts) == 1
  assert sorted(conflicts[0]["issues"]) == [1, 2]
  assert "20" in conflicts[0]["reason"]


def test_fixes_refs_beyond_2000_chars_are_captured() -> None:
  """Regression: fixes_refs beyond char 2000 in the original body must not be dropped.

  fetch_issues.py extracts fixes_refs from the full body before truncation.
  analyze_conflicts() must consume that structured field — not re-parse the
  truncated body — so refs at position >2000 are preserved.
  """
  padding = "x" * 2100
  # If analyze_conflicts() re-parsed the truncated body it would miss #50.
  # The structured field ensures it's captured.
  issues = [
    # fixes_refs pre-extracted from full body (beyond char 2000)
    _make_issue(1, body=padding, fixes_refs=[50]),
    _make_issue(2, fixes_refs=[50]),
    _make_issue(50),
  ]
  conflicts = analyze_conflicts(issues)
  assert len(conflicts) == 1
  assert sorted(conflicts[0]["issues"]) == [1, 2]
  assert "50" in conflicts[0]["reason"]


# ---------------------------------------------------------------------------
# Fallback path: no `fixes_refs` field → regex over truncated body
# ---------------------------------------------------------------------------


def test_fallback_to_body_regex_when_no_fixes_refs() -> None:
  """Backward-compat: issues without fixes_refs fall back to body regex."""
  issues = [
    _make_issue(1, body="fixes #30"),   # no fixes_refs key
    _make_issue(2, body="closes #30"),  # no fixes_refs key
    _make_issue(30),
  ]
  conflicts = analyze_conflicts(issues)
  assert len(conflicts) == 1
  assert sorted(conflicts[0]["issues"]) == [1, 2]
  assert "30" in conflicts[0]["reason"]


def test_fallback_deduplicates_repeated_ref_in_body() -> None:
  """'Fixes #10 and closes #10' in one body must not count as two fixers.

  Without dedup, the two regex matches both get appended to fixes_map[10],
  giving len >= 2 and producing a nonsensical conflict with a single participant.
  """
  issues = [
    _make_issue(1, body="Fixes #10 and closes #10"),  # two matches, one issue
    _make_issue(10),
  ]
  # Only 1 unique fixer (issue #1) → no conflict.
  conflicts = analyze_conflicts(issues)
  assert conflicts == []


def test_fallback_ignores_refs_to_closed_issues() -> None:
  """Body references to issue numbers not in the open-issue set are ignored."""
  issues = [
    _make_issue(1, body="fixes #999"),  # #999 not in open issues
    _make_issue(2, body="closes #999"),
  ]
  conflicts = analyze_conflicts(issues)
  assert conflicts == []


def test_fallback_ignores_self_references() -> None:
  """An issue body cannot fix itself."""
  issues = [
    _make_issue(1, body="fixes #1"),  # self-reference — must be excluded
    _make_issue(2, body="fixes #1"),  # real fixer
    _make_issue(3),                   # bystander to ensure #1 is in issue_numbers
  ]
  # Issue #1's self-reference must be excluded; only #2 is a real fixer of #1.
  # Only 1 fixer → no conflict.
  conflicts = analyze_conflicts(issues)
  assert conflicts == []


# ---------------------------------------------------------------------------
# Mixed: some issues have fixes_refs, some do not
# ---------------------------------------------------------------------------


def test_mixed_structured_and_fallback() -> None:
  """Issues 1 (structured) and 2 (fallback) both fixing #40 → conflict detected."""
  issues = [
    _make_issue(1, body="ignored", fixes_refs=[40]),  # structured wins
    _make_issue(2, body="fixes #40"),                 # fallback via regex
    _make_issue(40),
  ]
  conflicts = analyze_conflicts(issues)
  assert len(conflicts) == 1
  assert sorted(conflicts[0]["issues"]) == [1, 2]
  assert "40" in conflicts[0]["reason"]
