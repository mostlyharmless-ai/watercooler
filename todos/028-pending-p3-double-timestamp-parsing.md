---
status: completed
priority: p3
issue_id: "028"
tags: [code-review, performance, federation]
dependencies: []
---

# Eliminate double timestamp parsing

## Problem Statement

Each entry's timestamp is parsed twice via `datetime.fromisoformat()`: once in `compute_recency_decay()` (scoring.py) and once in `_negate_epoch()` (merger.py, for sort tiebreaking). This is O(2N) datetime parsing where O(N) would suffice.

Flagged by performance-oracle.

## Findings

- **Location**: `scoring.py` (`compute_recency_decay`) and `merger.py` (`_negate_epoch`)
- **Impact**: Minor — datetime parsing is fast, and N is bounded by `limit` (typically 10-20)
- **Optimization**: Parse once in scoring, carry the epoch float through to the merger

## Proposed Solutions

### Solution A: Add `timestamp_epoch` field to `ScoredResult` (Recommended)

Parse timestamp once during scoring, store as `timestamp_epoch: float` in `ScoredResult`. Use pre-computed value in merger sort key.

**Pros:** Eliminates double parsing
**Cons:** Adds one field to the dataclass
**Effort:** Small
**Risk:** Low

### Solution B: Accept current behavior

The double parsing is <1ms for typical result sets. Not worth optimizing.

**Effort:** None
**Risk:** None

## Acceptance Criteria

- [ ] Timestamp parsed only once per entry
- [ ] Sort order unchanged
- [ ] Tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
