---
status: completed
priority: p3
issue_id: "014"
tags: [code-review, simplicity, federation]
dependencies: []
---

# Remove dead `sq.thread_topic = None` assignment

## Problem Statement

`sq.thread_topic = None` at line 180 is a no-op -- `SearchQuery` already defaults `thread_topic` to `None` (constructed 5 lines above). The comment about "branch handled downstream" is misleading since no branch filtering logic exists in Phase 1.

## Findings

- **File:** `src/watercooler_mcp/tools/federation.py`, line 180
- **Agents:** Code Simplicity Reviewer
- **Severity:** Nice-to-Have (P3) -- dead code

## Proposed Solutions

Delete lines 179-180 (the `if` block and assignment).

- **Effort:** Small (-2 LOC)
- **Risk:** None

## Acceptance Criteria

- [ ] Dead assignment removed
- [ ] All tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
