---
status: completed
priority: p3
issue_id: "030"
tags: [code-review, style, federation]
dependencies: []
---

# Fix import ordering in federation.py

## Problem Statement

`src/watercooler_mcp/tools/federation.py` has imports that don't follow PEP 8 ordering (stdlib, third-party, local). Specifically, `json` and `asyncio` may be interleaved with local imports.

Flagged by kieran-python-reviewer and pattern-recognition-specialist.

## Findings

- **Location**: `src/watercooler_mcp/tools/federation.py`, top of file
- **Fix**: Run `ruff check --fix` or `isort` to auto-sort

## Proposed Solutions

### Solution A: Auto-fix with ruff (Recommended)

```bash
ruff check --fix src/watercooler_mcp/tools/federation.py
```

**Effort:** Small | **Risk:** Low

## Acceptance Criteria

- [ ] Imports follow PEP 8 ordering
- [ ] `ruff check` passes

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
