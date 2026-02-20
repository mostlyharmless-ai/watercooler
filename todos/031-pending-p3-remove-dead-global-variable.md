---
status: completed
priority: p3
issue_id: "031"
tags: [code-review, dead-code, federation]
dependencies: []
---

# Remove dead `federated_search_tool = None` global

## Problem Statement

`src/watercooler_mcp/tools/federation.py` has a module-level `federated_search_tool = None` global that is never read. It appears to be scaffolding that was never used.

Flagged by code-simplicity-reviewer.

## Findings

- **Location**: `src/watercooler_mcp/tools/federation.py`, line ~41
- **Impact**: Dead code, no functional effect

## Proposed Solutions

### Solution A: Delete the line (Recommended)

**Effort:** Small | **Risk:** Low

## Acceptance Criteria

- [ ] `federated_search_tool` global removed
- [ ] No references to it elsewhere

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
