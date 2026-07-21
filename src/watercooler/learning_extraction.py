"""Pure helpers for the Learnings daemon's evidence layer (Phase 1b plumbing).

Two deterministic, side-effect-free functions the daemon composes at tick time:

- ``build_solutions_index`` — read ``dev_docs/solutions/*.md`` frontmatter ``pr:``
  fields into a ``{pr_number: doc_path}`` map. This is the "consume-not-duplicate"
  index: a merged PR with an entry here already has a captured learning (the CE
  write-up), so it is *not* a capture gap.
- ``extract_pr_numbers`` — pull the PR number(s) a closed thread refers to, from its
  PR-type entries / Closure bodies / merge-style footers, to drive the join.

These are the uncontested half of the evidence model (the data sources Jay named in
``01KV3VBSQGQQ…``). The daemon's firing predicate (which PR-less-doc threads become
``capture_gap`` findings) and any positive ``has_learning`` indexing are wired in a
later commit once their semantics are settled.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

# TOML loading: tomllib (3.11+) with tomli fallback (same pattern as role_loader)
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            tomllib = None  # type: ignore

# PR-number patterns, conservative to avoid matching bare issue refs:
# - GitHub PR URLs: ".../pull/950"
# - explicit "PR #950" / "PR-950" / "PR 950"
# - squash-merge subjects: "subject (#950)"
_PR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"pull/(\d+)"),
    re.compile(r"\bPR[ #\-]*(\d+)\b", re.IGNORECASE),
    re.compile(r"\(#(\d+)\)"),
)

# Bare "#950" is only trusted in a PR-type entry's title (where it is the PR ref).
_BARE_HASH = re.compile(r"#(\d+)")


def _frontmatter_block(text: str) -> str | None:
    """Return the YAML frontmatter block (between the leading ``---`` fences).

    Returns None when the text has no leading frontmatter fence.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def parse_solution_doc_prs(text: str) -> list[int]:
    """Extract PR number(s) from a solution doc's frontmatter.

    Recognizes the PR-reference frontmatter variants used across the repo's
    write-ups, so the solutions index matches documented work regardless of which
    field a doc used:

    - ``pr:`` / ``pr_number:`` scalars — bare or quoted, with or without ``#``
      (``pr: 486``, ``pr: "486"``, ``pr_number: #486``)
    - ``related_prs:`` / ``pr_numbers:`` — inline (``[#505, "506"]``) or block
      lists (``- 505`` / ``- "#506"`` lines following the key)

    Parsed line-wise (no YAML dependency). Returns distinct PR numbers in
    first-seen order; empty when there is no frontmatter or no PR field. The
    superset is safe for Compound Engineering interop, which emits bare ``pr:``.

    Args:
        text: Full markdown text of the solution doc.

    Returns:
        Distinct PR numbers declared in the frontmatter.
    """
    block = _frontmatter_block(text)
    if block is None:
        return []
    prs: list[int] = []
    seen: set[int] = set()

    def _add(raw: str) -> None:
        for m in re.finditer(r"#?(\d+)", raw):
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                prs.append(n)

    in_list_block = False
    for line in block.splitlines():
        scalar = re.match(r"\s*(?:pr_number|pr):\s*['\"]?#?(\d+)['\"]?\s*$", line)
        if scalar:
            _add(scalar.group(1))
            in_list_block = False
            continue
        inline = re.match(r"\s*(?:related_prs|pr_numbers):\s*\[(.+)\]\s*$", line)
        if inline:
            _add(inline.group(1))
            in_list_block = False
            continue
        if re.match(r"\s*(?:related_prs|pr_numbers):\s*$", line):
            in_list_block = True
            continue
        if in_list_block:
            item = re.match(r"\s*-\s*['\"]?#?(\d+)['\"]?\s*$", line)
            if item:
                _add(item.group(1))
                continue
            in_list_block = False
    return prs


def build_solutions_index(solutions_dir: Path) -> dict[int, str]:
    """Map PR number -> solution-doc path for every ``pr:``-tagged write-up.

    Walks ``solutions_dir`` recursively for ``*.md`` files and indexes those whose
    frontmatter declares a ``pr:`` field. Paths are relative to ``solutions_dir``.
    A missing directory yields an empty index (the daemon then degrades to
    graph-only signals — e.g. a hosted variant with no code checkout).

    Args:
        solutions_dir: Path to ``dev_docs/solutions`` in the code repo.

    Returns:
        ``{pr_number: relative_doc_path}``. First doc wins on duplicate PRs.
    """
    index: dict[int, str] = {}
    if not solutions_dir.is_dir():
        return index
    for path in sorted(solutions_dir.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(path.relative_to(solutions_dir))
        for pr in parse_solution_doc_prs(text):
            index.setdefault(pr, rel)
    return index


def extract_pr_numbers(entries: list[dict[str, Any]]) -> set[int]:
    """Extract the PR number(s) a thread's entries refer to.

    Scans entry titles, summaries, and bodies for PR references (GitHub pull URLs,
    ``PR #N`` forms, and ``(#N)`` merge subjects); for PR-type entries it also
    trusts a bare ``#N`` in the title. This drives the join against the solutions
    index, per Jay's impl note in ``01KV3VBSQGQQ…``.

    Args:
        entries: Thread entry dicts (graph nodes), each using ``.get()`` fields.

    Returns:
        The set of distinct PR numbers referenced across the entries.
    """
    prs: set[int] = set()
    for entry in entries:
        text = "\n".join(
            str(entry.get(field, "") or "")
            for field in ("title", "summary", "body")
        )
        for pattern in _PR_PATTERNS:
            prs.update(int(n) for n in pattern.findall(text))
        if str(entry.get("entry_type", "")) == "PR":
            prs.update(int(n) for n in _BARE_HASH.findall(str(entry.get("title", ""))))
    return prs


# --------------------------------------------------------------------------- #
# Classification criteria — the "criteria-as-data" seam
# --------------------------------------------------------------------------- #

# In-thread lesson signal: a heading that *is* a lesson section — anchored to the
# end of the line so "## Learnings daemon" / "## Lesson plan" don't false-match.
_LESSON_HEADING = re.compile(
    r"(?im)^#{1,6}\s*(lessons?(?:\s+learned)?|learnings?)\s*:?\s*$"
)


@dataclass(frozen=True)
class LearningCriterion:
    """A named, metadata-carrying classification rule.

    Lightweight by design: it installs the seam (every emission traces to a
    criterion ``id``; maturity is the ``status`` field, not a fork) without a
    rule-DSL. Matching logic lives in ``assess_thread_learning``; criteria can be
    selected/filtered by ``status`` as they graduate experimental -> stable.
    """

    id: str
    kind: str  # "positive" | "capture_gap"
    severity: str  # Finding severity: "info" | "warning"
    status: str = "experimental"  # experimental | stable | deprecated
    applies_to: tuple[str, ...] = ("thread",)


# Proceed-now defaults (all experimental; edit these to tune the definition of
# "a captured learning" without touching code paths).
DEFAULT_CRITERIA: tuple[LearningCriterion, ...] = (
    LearningCriterion("learning.solution_doc_present", "positive", "info"),
    LearningCriterion("learning.in_thread_lesson", "positive", "info"),
    LearningCriterion("capture_gap.pr_without_solution_doc", "capture_gap", "warning"),
)


@dataclass(frozen=True)
class LearningAssessment:
    """Deterministic learning-capture verdict for one closed thread."""

    status: str  # "has_learning" | "capture_gap" | "not_applicable"
    triggering_criterion_id: str | None
    severity: str | None
    pr_numbers: tuple[int, ...]
    matched_doc: str | None = None
    lesson_entry_id: str | None = None


def _find_in_thread_lesson(entries: list[dict[str, Any]]) -> str | None:
    """Return the entry_id of the first entry carrying an explicit lesson section."""
    for entry in entries:
        text = "\n".join(str(entry.get(f, "") or "") for f in ("title", "body"))
        if _LESSON_HEADING.search(text):
            return str(entry.get("entry_id", "")) or None
    return None


def assess_thread_learning(
    entries: list[dict[str, Any]],
    solutions_index: dict[int, str],
    criteria: tuple[LearningCriterion, ...] = DEFAULT_CRITERIA,
) -> LearningAssessment:
    """Classify whether a closed thread captured a learning (default criteria).

    Priority: a matched solutions write-up (high-fidelity) > an in-thread lesson
    section > capture-gap (references a merged PR but neither signal) > not
    applicable (no PR reference). The triggering criterion ``id`` rides on the
    result for provenance.

    Args:
        entries: The thread's entry dicts.
        solutions_index: ``{pr_number: doc_path}`` from ``build_solutions_index``.
        criteria: Active criteria (default: the experimental defaults).

    Returns:
        A ``LearningAssessment``.
    """
    prs = tuple(sorted(extract_pr_numbers(entries)))
    by_id = {c.id: c for c in criteria}

    matched_doc = next(
        (solutions_index[pr] for pr in prs if pr in solutions_index), None
    )
    if matched_doc is not None and "learning.solution_doc_present" in by_id:
        c = by_id["learning.solution_doc_present"]
        return LearningAssessment(
            "has_learning", c.id, c.severity, prs, matched_doc=matched_doc
        )

    lesson_entry_id = _find_in_thread_lesson(entries)
    if lesson_entry_id is not None and "learning.in_thread_lesson" in by_id:
        c = by_id["learning.in_thread_lesson"]
        return LearningAssessment(
            "has_learning", c.id, c.severity, prs, lesson_entry_id=lesson_entry_id
        )

    if prs and "capture_gap.pr_without_solution_doc" in by_id:
        c = by_id["capture_gap.pr_without_solution_doc"]
        return LearningAssessment("capture_gap", c.id, c.severity, prs)

    return LearningAssessment("not_applicable", None, None, prs)


# ---------------------------------------------------------------------------
# F3 — canonical root-cause taxonomy (Decision 01KXQ32Q7Z41F0P7A1JHN0S527)
# ---------------------------------------------------------------------------

_TAXONOMY_RESOURCE = "root_cause_taxonomy.toml"


@lru_cache(maxsize=1)
def load_root_cause_taxonomy() -> dict[str, Any]:
    """Load the packaged canonical root-cause taxonomy.

    Resolved via ``importlib.resources`` (the repository's packaged-asset
    convention — same as ``role_loader`` / ``schema_validation``), never a
    source-tree-relative path. Returns ``{"version": int, "category": [...]}``.
    """
    if tomllib is None:  # pragma: no cover - Python 3.10 without tomli
        raise RuntimeError(
            "Root-cause taxonomy requires tomllib (Python 3.11+) or the "
            "'tomli' package."
        )
    resource = files("watercooler") / "templates" / _TAXONOMY_RESOURCE
    data = tomllib.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(data.get("version"), int) or not data.get("category"):
        raise RuntimeError(
            f"Malformed root-cause taxonomy resource {_TAXONOMY_RESOURCE!r}: "
            "expected an integer 'version' and a non-empty 'category' list."
        )
    return data


def root_cause_taxonomy_version() -> int:
    """The packaged taxonomy's version (stamped as ``<slug>@<version>``)."""
    return int(load_root_cause_taxonomy()["version"])


def normalize_root_cause(text: str | None) -> str:
    """Map a free-text root cause to its canonical taxonomy slug.

    Deterministic, deliberately dumb matching: lowercase substring hints;
    the FIRST category (in taxonomy file order) with a hit wins. Unmatched
    or empty input falls open to ``other`` — a root cause is never dropped,
    only unclassified. Recurrence counting (Phase 5 precision pass and the
    future recurrence generator) groups by this slug, never by the raw
    LLM string.
    """
    if not text or not text.strip():
        return "other"
    lowered = text.lower()
    for category in load_root_cause_taxonomy()["category"]:
        for hint in category.get("hints", []):
            if hint and hint in lowered:
                return str(category["slug"])
    return "other"


def canonical_root_cause_stamp(text: str | None) -> str:
    """The full ``<slug>@<version>`` value for a durable-surface marker."""
    return f"{normalize_root_cause(text)}@{root_cause_taxonomy_version()}"
