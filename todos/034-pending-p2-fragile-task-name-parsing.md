---
status: pending
priority: p2
issue_id: "034"
tags: [code-review, bug-risk, federation]
dependencies: []
---

# Task name parsing for namespace ID is fragile

## Problem Statement

`tools/federation.py:347,359` uses `task_obj.get_name().removeprefix("federation-search-")` to recover namespace IDs from asyncio task names. This couples status tracking to asyncio's internal task naming. A namespace named `"federation-search-foo"` would be recovered as `"foo"`.

## Findings

- **Location**: `src/watercooler_mcp/tools/federation.py`, lines 347 and 359
- **Current**: `ns_id = task_obj.get_name().removeprefix("federation-search-")`
- **Risk**: Fragile coupling to asyncio task naming convention

## Proposed Solution

Build a `dict[asyncio.Task, str]` mapping alongside `task_objects` so namespace IDs are recovered from the dict instead of task names.

**Effort:** Small | **Risk:** Low

## Acceptance Criteria

- [ ] `removeprefix` calls replaced with dict lookup
- [ ] Task-to-namespace mapping built at task creation time

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From PR #190 review round 8 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
