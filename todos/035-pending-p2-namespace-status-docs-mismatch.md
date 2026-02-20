---
status: completed
priority: p2
issue_id: "035"
tags: [code-review, docs, federation]
dependencies: []
---

# namespace_status format diverges from docs

## Problem Statement

`docs/mcp-server.md` documents `namespace_status` as simple string values (`"ok"`, `"timeout"`), but the implementation produces nested dicts with diagnostic fields (`{"status": "ok"}`, `{"status": "not_initialized", "action_hint": "..."}`).

## Findings

- **Location**: `docs/mcp-server.md`, line 302
- **Current docs**: `namespace_status: Per-namespace status (ok, timeout, ...)`
- **Actual format**: Nested dicts with `status`, optional `error_message`, optional `action_hint`

## Proposed Solution

Update `docs/mcp-server.md` to document the actual nested dict format. The richer format is better for agents — docs should match reality.

**Effort:** Small | **Risk:** None

## Acceptance Criteria

- [ ] Docs describe nested dict structure with all optional fields

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From PR #190 review round 8 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
