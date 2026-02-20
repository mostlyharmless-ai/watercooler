---
status: completed
priority: p2
issue_id: "005"
tags: [code-review, security, federation]
dependencies: []
---

# Return resolved path from `discover_namespace_worktree()`

## Problem Statement

After performing containment validation on the `resolved` path, the function returns the original unresolved `worktree_path`. This widens the TOCTOU window -- the returned path may diverge from what was validated.

## Findings

- **File:** `src/watercooler_mcp/federation/resolver.py`, line 80
- **Agents:** Security Reviewer (x2)
- **Severity:** Should Fix (P2) -- Medium security risk (local attacker, read-only impact)

```python
resolved = worktree_path.resolve()         # Validate THIS
...
if worktree_path.exists() and worktree_path.is_dir():
    return worktree_path                    # Return THAT (not resolved)
```

## Proposed Solutions

### Solution A: Return resolved path (Recommended)

```python
if resolved.is_dir():
    return resolved  # Return what was validated
```

- **Effort:** Small (1-line change)
- **Risk:** Low -- callers receive a resolved path, which is strictly better

## Acceptance Criteria

- [ ] `discover_namespace_worktree` returns `resolved` path
- [ ] Resolver tests updated to expect resolved paths
- [ ] All tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
