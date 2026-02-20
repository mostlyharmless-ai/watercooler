---
status: completed
priority: p3
issue_id: "029"
tags: [code-review, performance, federation]
dependencies: []
---

# Pre-compute deny_topics as lowercased frozenset

## Problem Statement

`is_topic_denied()` in `access.py` calls `.lower()` on each deny topic per entry during the linear scan. For repeated calls with the same namespace config, this redundant lowercasing could be pre-computed.

Flagged by performance-oracle.

## Findings

- **Location**: `src/watercooler_mcp/federation/access.py`, `is_topic_denied()`
- **Impact**: Negligible — deny_topics lists are typically 0-5 items, called at most `limit` times
- **Optimization**: Pre-compute `frozenset(t.lower() for t in config.deny_topics)` once per namespace

## Proposed Solutions

### Solution A: Pre-compute in caller

Compute the lowercased frozenset once before the loop in `federation.py` and pass it to the deny check.

**Effort:** Small | **Risk:** Low

### Solution B: Accept current behavior

The overhead is negligible for expected workloads.

**Effort:** None | **Risk:** None

## Acceptance Criteria

- [ ] deny_topics lowercased at most once per namespace per search

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
