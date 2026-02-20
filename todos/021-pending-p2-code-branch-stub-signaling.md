---
status: completed
priority: p2
issue_id: "021"
tags: [code-review, api-design, federation]
dependencies: []
---

# Signal `code_branch` parameter as Phase 2 stub

## Problem Statement

The `code_branch` parameter on `watercooler_federated_search` is accepted but non-functional — it's assigned to `SearchQuery.thread_topic` which doesn't filter by branch. Users may pass a branch name expecting branch-scoped results and get unfiltered results instead. No warning is surfaced.

Flagged by kieran-python-reviewer, architecture-strategist, and code-simplicity-reviewer.

## Findings

- **Location**: `src/watercooler_mcp/tools/federation.py`, line ~65 (parameter definition), lines ~175-180 (usage)
- **Current behavior**: `code_branch` is silently ignored — assigned to a field that doesn't do branch filtering
- **Risk**: User confusion when branch filtering doesn't work as expected

## Proposed Solutions

### Solution A: Remove parameter, add back in Phase 2 (Recommended)

Remove `code_branch` from the tool signature entirely. Re-add when branch filtering is implemented.

**Pros:** No dead API surface, no user confusion
**Cons:** API change if anyone is already using it (unlikely — not yet released)
**Effort:** Small
**Risk:** Low — feature hasn't shipped

### Solution B: Keep parameter, log warning when used

```python
if code_branch:
    logger.warning("code_branch filtering not yet implemented, parameter ignored")
```

**Pros:** Forward-compatible API
**Cons:** Dead parameter in the API, confusing for users
**Effort:** Small
**Risk:** Low

## Acceptance Criteria

- [ ] `code_branch` parameter either removed or clearly documented as non-functional
- [ ] No silent acceptance of a parameter that does nothing
- [ ] Tests updated

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3, flagged by 3 agents |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
