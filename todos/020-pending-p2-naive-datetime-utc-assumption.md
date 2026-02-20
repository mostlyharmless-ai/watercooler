---
status: completed
priority: p2
issue_id: "020"
tags: [code-review, correctness, federation]
dependencies: []
---

# Warn or reject naive datetimes in `compute_recency_decay`

## Problem Statement

`compute_recency_decay()` in `scoring.py` silently normalizes naive (timezone-unaware) datetimes by treating them as UTC. This is a hidden assumption — if an entry has a naive datetime in a non-UTC timezone, the recency score will be wrong. No warning is logged.

Flagged by kieran-python-reviewer.

## Findings

- **Location**: `src/watercooler_mcp/federation/scoring.py`, lines ~80-83
- **Code**: `if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)`
- **Risk**: Subtle scoring errors if entries originate from systems that produce naive local-time timestamps
- **Mitigation**: Watercooler entries always use UTC ISO 8601, so this is defensive code — but silent normalization is still surprising

## Proposed Solutions

### Solution A: Add debug-level log warning (Recommended)

```python
if ts.tzinfo is None:
    logger.debug("Naive datetime assumed UTC: %s", ts)
    ts = ts.replace(tzinfo=timezone.utc)
```

**Pros:** Visible in debug logs, zero overhead in production
**Cons:** Only visible with debug logging enabled
**Effort:** Small
**Risk:** Low

### Solution B: Raise ValueError for naive datetimes

**Pros:** Strict, forces callers to provide aware datetimes
**Cons:** Could break if any code path passes naive datetimes
**Effort:** Small
**Risk:** Medium — may surface unexpected callers

## Acceptance Criteria

- [ ] Naive datetimes produce a visible warning (at least debug level)
- [ ] Existing tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
