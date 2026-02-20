---
status: completed
priority: p2
issue_id: "006"
tags: [code-review, security, quality, federation]
dependencies: ["005"]
---

# Use `Path.relative_to()` for path containment check

## Problem Statement

The path containment check uses string `startswith()` which is fragile on edge cases (case-insensitive filesystems, root resolution). `Path.relative_to()` is the stdlib's built-in for this purpose.

## Findings

- **File:** `src/watercooler_mcp/federation/resolver.py`, lines 70-71
- **Agents:** Python Quality Reviewer, Security Reviewer (x2)
- **Severity:** Should Fix (P2)

```python
if not str(resolved).startswith(str(worktree_base_resolved) + "/"):
```

## Proposed Solutions

### Solution A: Use `Path.relative_to()` (Recommended)

```python
try:
    resolved.relative_to(worktree_base_resolved)
except ValueError:
    logger.warning(...)
    return None
```

- **Pros:** Handles edge cases correctly, more idiomatic
- **Cons:** None
- **Effort:** Small
- **Risk:** Low

## Acceptance Criteria

- [ ] Path containment uses `relative_to()` instead of `startswith()`
- [ ] Path escape test still passes
- [ ] All resolver tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
