"""Tests for _extract_dep_refs in the issue-ranker fetch_issues script.

The truncation bug (todo 003) caused dependency refs beyond char 2000 to be
silently dropped. _extract_dep_refs must operate on the full body; these tests
verify all alternation groups and that refs beyond 2000 chars are captured.
"""

import sys
from pathlib import Path

import pytest

# Skill scripts are not installable packages; import directly from the source tree.
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude/skills/issue-ranker/scripts"))

from fetch_issues import _extract_dep_refs, _BLOCKED_BY_RE, _BLOCKS_RE, _FIXES_RE  # noqa: E402
from analyze_relationships import BLOCKED_BY_PATTERNS, BLOCKS_PATTERNS, FIXES_CLOSES_PATTERNS  # noqa: E402


def test_blocked_by_pattern() -> None:
  result = _extract_dep_refs("blocked by #42")
  assert result["blocked_by_refs"] == [42]
  assert result["blocks_refs"] == []
  assert result["fixes_refs"] == []


def test_depends_on_pattern() -> None:
  result = _extract_dep_refs("depends on #7")
  assert result["blocked_by_refs"] == [7]


def test_requires_pattern() -> None:
  result = _extract_dep_refs("requires #100 to proceed")
  assert result["blocked_by_refs"] == [100]


def test_after_merged_pattern() -> None:
  result = _extract_dep_refs("after #55 is merged")
  assert result["blocked_by_refs"] == [55]


def test_needs_closed_pattern() -> None:
  result = _extract_dep_refs("needs #33 to be closed")
  assert result["blocked_by_refs"] == [33]


def test_prerequisite_pattern() -> None:
  result = _extract_dep_refs("prerequisite: #21")
  assert result["blocked_by_refs"] == [21]


def test_follow_up_to_pattern() -> None:
  result = _extract_dep_refs("follow-up to #88")
  assert result["blocked_by_refs"] == [88]


def test_blocks_pattern() -> None:
  result = _extract_dep_refs("blocks #99")
  assert result["blocks_refs"] == [99]
  assert result["blocked_by_refs"] == []


def test_fixes_pattern() -> None:
  result = _extract_dep_refs("Fixes #15")
  assert result["fixes_refs"] == [15]


def test_closes_pattern() -> None:
  result = _extract_dep_refs("closes #77")
  assert result["fixes_refs"] == [77]


def test_resolves_pattern() -> None:
  result = _extract_dep_refs("Resolves #3")
  assert result["fixes_refs"] == [3]


def test_multiple_refs_deduplicated() -> None:
  result = _extract_dep_refs("blocked by #5\nblocked by #5\ndepends on #10")
  assert result["blocked_by_refs"] == [5, 10]


def test_refs_beyond_2000_chars_are_captured() -> None:
  """Regression: refs in the tail of the body (beyond char 2000) must not be dropped."""
  padding = "x" * 2100
  text = f"{padding}\nblocked by #123\nblocks #456\nfixes #789"
  result = _extract_dep_refs(text)
  assert 123 in result["blocked_by_refs"]
  assert 456 in result["blocks_refs"]
  assert 789 in result["fixes_refs"]


def test_empty_body_returns_empty_lists() -> None:
  result = _extract_dep_refs("")
  assert result == {"blocked_by_refs": [], "blocks_refs": [], "fixes_refs": []}


def test_no_refs_in_body() -> None:
  result = _extract_dep_refs("This issue has no references to other issues.")
  assert result == {"blocked_by_refs": [], "blocks_refs": [], "fixes_refs": []}


def test_refs_are_sorted() -> None:
  result = _extract_dep_refs("blocked by #30\ndepends on #5\nrequires #20")
  assert result["blocked_by_refs"] == [5, 20, 30]


# ---------------------------------------------------------------------------
# Pattern sync: fetch_issues.py compiled regexes must stay in sync with
# analyze_relationships.py pattern lists.  If a new phrasing is added to
# BLOCKED_BY_PATTERNS but not to _BLOCKED_BY_RE (or vice versa), the
# pre-extracted structured field and the fallback regex path will silently
# diverge — producing different results for long vs short issue bodies.
# ---------------------------------------------------------------------------


def test_blocked_by_patterns_in_sync() -> None:
  """Every pattern string in BLOCKED_BY_PATTERNS must appear in _BLOCKED_BY_RE."""
  combined = _BLOCKED_BY_RE.pattern
  for p in BLOCKED_BY_PATTERNS:
    assert p in combined, (
      f"Pattern {p!r} is in analyze_relationships.BLOCKED_BY_PATTERNS "
      f"but missing from fetch_issues._BLOCKED_BY_RE — add it to both."
    )


def test_blocks_patterns_in_sync() -> None:
  """Every pattern string in BLOCKS_PATTERNS must appear in _BLOCKS_RE."""
  combined = _BLOCKS_RE.pattern
  for p in BLOCKS_PATTERNS:
    assert p in combined, (
      f"Pattern {p!r} is in analyze_relationships.BLOCKS_PATTERNS "
      f"but missing from fetch_issues._BLOCKS_RE — add it to both."
    )


def test_fixes_closes_patterns_in_sync() -> None:
  """Every pattern string in FIXES_CLOSES_PATTERNS must appear in _FIXES_RE."""
  combined = _FIXES_RE.pattern
  for p in FIXES_CLOSES_PATTERNS:
    assert p in combined, (
      f"Pattern {p!r} is in analyze_relationships.FIXES_CLOSES_PATTERNS "
      f"but missing from fetch_issues._FIXES_RE — add it to both."
    )
