---
status: completed
priority: p3
issue_id: "033"
tags: [code-review, api-design, agent-native, federation]
dependencies: []
---

# Consider list type for `namespaces` parameter instead of comma-separated string

## Problem Statement

The `namespaces` parameter on `watercooler_federated_search` accepts a comma-separated string (e.g., `"cloud,site,docs"`). MCP tools support array parameters natively, which would be more type-safe and avoid parsing ambiguity.

Flagged by agent-native-reviewer.

## Findings

- **Location**: `src/watercooler_mcp/tools/federation.py`, `namespaces` parameter
- **Current**: `namespaces: str = ""` → split on comma
- **Alternative**: `namespaces: list[str] = []` → native list
- **Consideration**: Some MCP clients may not support array parameters well. The comma-separated string is more universally compatible.

## Proposed Solutions

### Solution A: Keep comma-separated string (Recommended for Phase 1)

The current approach works across all MCP clients. Revisit in Phase 2 when MCP client support for arrays is more consistent.

**Effort:** None | **Risk:** None

### Solution B: Change to list type

**Effort:** Small | **Risk:** Medium — may break clients that don't support array params

## Acceptance Criteria

- [ ] Decision documented (keep or change)

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
