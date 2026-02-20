---
status: completed
priority: p2
issue_id: "025"
tags: [code-review, api-consistency, federation]
dependencies: []
---

# Add `schema_version` to all error responses

## Problem Statement

The catch-all exception handler in `_federated_search_impl` includes `schema_version: 1` in its error response, but the early-exit error responses (federation disabled, no namespaces available) do not. This inconsistency makes it harder for consumers to parse error responses uniformly.

Flagged by agent-native-reviewer.

## Findings

- **Location**: `src/watercooler_mcp/tools/federation.py`
  - Catch-all error (lines ~100-108): includes `schema_version: 1`
  - Federation disabled error (lines ~114-118): missing `schema_version`
  - No available namespaces error (lines ~160-168): missing `schema_version`
- **Impact**: Consumers can't rely on `schema_version` being present in all responses

## Proposed Solutions

### Solution A: Add `schema_version` to all error responses (Recommended)

Add `"schema_version": 1` to every `json.dumps` error response.

**Pros:** Uniform response format, easy to parse
**Cons:** None
**Effort:** Small
**Risk:** Low

## Acceptance Criteria

- [ ] All error responses include `schema_version: 1`
- [ ] Tests verify schema_version present in error responses

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
