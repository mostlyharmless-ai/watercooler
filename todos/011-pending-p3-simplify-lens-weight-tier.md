---
status: completed
priority: p3
issue_id: "011"
tags: [code-review, yagni, federation]
dependencies: []
---

# Simplify lens weight tier (unreachable in Phase 1)

## Problem Statement

`resolve_namespace_weight()` accepts a `lens_namespaces: frozenset[str]` parameter, but the sole call site always passes `frozenset()`. The lens tier (0.7 weight) is unreachable code in Phase 1.

## Findings

- **File:** `src/watercooler_mcp/federation/scoring.py`, lines 47-60
- **File:** `src/watercooler_mcp/tools/federation.py`, line 196
- **Agents:** Code Simplicity Reviewer
- **Severity:** Nice-to-Have (P3) -- YAGNI

## Proposed Solutions

### Solution A: Remove lens tier, simplify to 2-tier
Remove `lens_namespaces` param, `lens_weight` config field, simplify to primary/non-primary weight resolution. Add back in Phase 2.

### Solution B: Add comment at call site (Lower effort)
Document that lens is a Phase 2 hook:
```python
# Phase 2: lens namespace support. Currently empty.
nw = resolve_namespace_weight(ns_id, primary_ns_id, frozenset(), ...)
```

## Acceptance Criteria

- [ ] Either lens tier removed or clearly documented as Phase 2 stub
- [ ] All scoring tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
