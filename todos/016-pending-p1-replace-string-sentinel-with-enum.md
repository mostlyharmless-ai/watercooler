---
status: completed
priority: p1
issue_id: "016"
tags: [code-review, security, type-safety, federation]
dependencies: []
---

# Replace string sentinel `"security_rejected"` with typed result

## Problem Statement

`discover_namespace_worktree()` returns `Path | str | None` where the string `"security_rejected"` is a sentinel value. This is a fragile union type — callers must remember to check `isinstance(worktree, Path)` vs `worktree == "security_rejected"` vs `None`. A typo in the sentinel string silently breaks security logic with no type-checker help.

Multiple review agents flagged this independently (kieran-python, security-sentinel, architecture-strategist, code-simplicity-reviewer).

## Findings

- **Location**: `src/watercooler_mcp/federation/resolver.py`, lines 42-84
- **Callers**: `resolve_all_namespaces()` at lines 138-167
- **Risk**: If a future contributor writes `worktree == "security-rejected"` (hyphen vs underscore), the security rejection silently falls through to the `not_initialized` branch
- **Type**: The return type `Path | str | None` forces runtime duck-typing where the type system could enforce correctness

## Proposed Solutions

### Solution A: Enum return type (Recommended)

Replace the string sentinel with an `enum.Enum`:

```python
class WorktreeStatus(enum.Enum):
    SECURITY_REJECTED = "security_rejected"

def discover_namespace_worktree(...) -> Path | WorktreeStatus | None:
```

**Pros:** Type-safe, mypy catches typos, self-documenting
**Cons:** Adds a small class (+4 LOC)
**Effort:** Small
**Risk:** Low

### Solution B: Result dataclass

Return a `@dataclass` with `path: Path | None` and `rejected: bool` fields.

**Pros:** Extensible for future metadata (rejection reason, etc.)
**Cons:** More boilerplate than needed for a 2-state discriminator
**Effort:** Small
**Risk:** Low

## Recommended Action

Solution A — enum is the right level of abstraction for a 2-value discriminator.

## Technical Details

- **Affected files**: `resolver.py` (function + callers), test file
- **Components**: Federation resolver module

## Acceptance Criteria

- [ ] `discover_namespace_worktree` return type no longer includes bare `str`
- [ ] All callers use enum comparison, not string comparison
- [ ] mypy passes with no new errors
- [ ] Existing resolver tests updated and passing

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3, flagged by 4 agents |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
