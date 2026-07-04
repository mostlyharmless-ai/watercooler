"""Tests for the Learnings daemon evidence helpers (Phase 1b plumbing).

Covers the two uncontested, deterministic data-source functions: the
solutions-doc PR index and PR-number extraction from thread entries. The daemon's
firing predicate and emission shape are wired (and tested) in a later commit once
their semantics are settled with the design owner.
"""

from __future__ import annotations

from watercooler.learning_extraction import (
    assess_thread_learning,
    build_solutions_index,
    extract_pr_numbers,
    parse_solution_doc_prs,
)

_DOC_WITH_PR = """\
---
title: Some fix
date: 2026-03-14
category: logic-errors
pr: 486
tags:
  - daemon
---

# Body
"""

_DOC_WITH_HASH_PR = """\
---
title: Hash-prefixed
pr: #742
---
body
"""

_DOC_NO_PR = """\
---
title: No pr field
category: process
---
body
"""

_DOC_NO_FRONTMATTER = "# Just a heading\n\nno frontmatter here\n"

_DOC_QUOTED_PR = """\
---
title: Quoted
pr: "584"
---
body
"""

_DOC_PR_NUMBER = """\
---
title: pr_number field
pr_number: 245
---
body
"""

_DOC_RELATED_PRS_BLOCK = """\
---
title: Related block
related_prs:
  - 531
  - "532"
  - "#533"
---
body
"""

_DOC_RELATED_PRS_INLINE = """\
---
title: Related inline
related_prs: [505, "#506"]
---
body
"""

_DOC_PR_NUMBERS_BLOCK = """\
---
title: pr_numbers block
pr_numbers:
  - 285
  - 289
---
body
"""


def test_parse_solution_doc_prs_variants() -> None:
    assert parse_solution_doc_prs(_DOC_WITH_PR) == [486]
    assert parse_solution_doc_prs(_DOC_WITH_HASH_PR) == [742]
    assert parse_solution_doc_prs(_DOC_NO_PR) == []
    assert parse_solution_doc_prs(_DOC_NO_FRONTMATTER) == []


def test_parse_solution_doc_prs_field_variants() -> None:
    # Repo write-ups use inconsistent PR fields; all must index (CE emits bare pr:).
    assert parse_solution_doc_prs(_DOC_QUOTED_PR) == [584]
    assert parse_solution_doc_prs(_DOC_PR_NUMBER) == [245]
    assert parse_solution_doc_prs(_DOC_RELATED_PRS_BLOCK) == [531, 532, 533]
    assert parse_solution_doc_prs(_DOC_RELATED_PRS_INLINE) == [505, 506]
    assert parse_solution_doc_prs(_DOC_PR_NUMBERS_BLOCK) == [285, 289]


def test_build_solutions_index(tmp_path) -> None:
    (tmp_path / "logic-errors").mkdir()
    (tmp_path / "logic-errors" / "a.md").write_text(_DOC_WITH_PR, encoding="utf-8")
    (tmp_path / "b.md").write_text(_DOC_WITH_HASH_PR, encoding="utf-8")
    (tmp_path / "c.md").write_text(_DOC_NO_PR, encoding="utf-8")

    index = build_solutions_index(tmp_path)

    assert index == {486: "logic-errors/a.md", 742: "b.md"}


def test_build_solutions_index_missing_dir(tmp_path) -> None:
    # Missing solutions dir (e.g. hosted, no code checkout) -> empty, no error.
    assert build_solutions_index(tmp_path / "does-not-exist") == {}


def test_extract_pr_numbers_from_varied_sources() -> None:
    entries = [
        {"entry_type": "Note", "title": "discussion", "body": "see https://github.com/o/r/pull/950 for context"},
        {"entry_type": "Closure", "title": "Closed", "body": "Merged in PR #951; follow-up tracked."},
        {"entry_type": "Note", "title": "scaffold Learnings daemon (#946)", "body": ""},
        {"entry_type": "PR", "title": "feat: thing #939", "body": ""},
        {"entry_type": "Note", "title": "no pr here", "body": "issue triage only"},
    ]
    assert extract_pr_numbers(entries) == {950, 951, 946, 939}


def test_extract_pr_numbers_empty() -> None:
    assert extract_pr_numbers([]) == set()
    assert extract_pr_numbers([{"entry_type": "Note", "title": "x", "body": "y"}]) == set()


# --- assess_thread_learning (the classifier / criteria-as-data seam) -------- #

def test_assess_has_learning_via_solution_doc() -> None:
    entries = [{"entry_type": "Closure", "title": "Done (#486)", "body": ""}]
    a = assess_thread_learning(entries, {486: "logic-errors/a.md"})
    assert a.status == "has_learning"
    assert a.matched_doc == "logic-errors/a.md"
    assert a.triggering_criterion_id == "learning.solution_doc_present"
    assert a.pr_numbers == (486,)
    assert a.severity == "info"


def test_assess_has_learning_via_in_thread_lesson() -> None:
    entries = [
        {"entry_type": "Closure", "entry_id": "01CLOSURE", "title": "Closed PR #999",
         "body": "## Lessons learned\nGuard the write path."},
    ]
    a = assess_thread_learning(entries, {})  # no doc match
    assert a.status == "has_learning"
    assert a.triggering_criterion_id == "learning.in_thread_lesson"
    assert a.lesson_entry_id == "01CLOSURE"
    assert a.matched_doc is None


def test_assess_capture_gap_pr_without_doc() -> None:
    entries = [{"entry_type": "Closure", "title": "Closed", "body": "Merged in PR #999."}]
    a = assess_thread_learning(entries, {486: "a.md"})  # 999 not indexed
    assert a.status == "capture_gap"
    assert a.triggering_criterion_id == "capture_gap.pr_without_solution_doc"
    assert a.pr_numbers == (999,)
    assert a.severity == "warning"


def test_assess_not_applicable_without_pr() -> None:
    entries = [{"entry_type": "Note", "title": "discussion only", "body": "no pr, no lesson"}]
    a = assess_thread_learning(entries, {486: "a.md"})
    assert a.status == "not_applicable"
    assert a.triggering_criterion_id is None
    assert a.pr_numbers == ()


def test_assess_lesson_heading_not_overmatched() -> None:
    # "## Learnings daemon" is a heading ABOUT learnings, not a lesson section —
    # it must NOT trigger has_learning. With a PR ref and no doc -> capture_gap.
    entries = [
        {"entry_type": "Closure", "entry_id": "01X", "title": "Closed PR #999",
         "body": "## Learnings daemon\nWe built the thing."},
    ]
    a = assess_thread_learning(entries, {})
    assert a.status == "capture_gap"
    assert a.triggering_criterion_id == "capture_gap.pr_without_solution_doc"


def test_assess_solution_doc_beats_in_thread_lesson() -> None:
    # Priority: a matched write-up wins over an in-thread lesson section.
    entries = [{"entry_type": "Closure", "entry_id": "01X", "title": "(#486)",
                "body": "## Lesson\nx"}]
    a = assess_thread_learning(entries, {486: "a.md"})
    assert a.triggering_criterion_id == "learning.solution_doc_present"
    assert a.lesson_entry_id is None
