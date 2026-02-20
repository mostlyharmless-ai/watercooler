---
status: completed
priority: p3
issue_id: "012"
tags: [code-review, simplicity, federation]
dependencies: []
---

# Inline `allocate_candidates()` at call site

## Problem Statement

`allocate_candidates()` is a 13-line function (with docstring) for a single expression: `return limit, max(limit // 2, 1)`. Called exactly once. Over-extracted.

## Findings

- **File:** `src/watercooler_mcp/federation/merger.py`, lines 35-47
- **Agents:** Code Simplicity Reviewer
- **Severity:** Nice-to-Have (P3)

## Proposed Solutions

Replace call site in `tools/federation.py:159` with:
```python
primary_limit = limit
per_secondary_limit = max(limit // 2, 1)
```

Delete function from merger.py. Keep or remove unit tests (they document the allocation math).

- **Effort:** Small (-12 LOC net)
- **Risk:** Low

## Acceptance Criteria

- [ ] Function inlined at call site
- [ ] All tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
