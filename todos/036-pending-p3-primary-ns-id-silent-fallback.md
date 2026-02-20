---
status: completed
priority: p3
issue_id: "036"
tags: [code-review, defensive, federation]
dependencies: []
---

# primary_ns_id silently falls back to "primary"

## Problem Statement

`resolver.py:130` — if `code_root` is `None`, the primary namespace ID becomes `"primary"`, silently breaking allowlist matching. By the time `resolve_all_namespaces` is called, a valid primary context should always have a `code_root`.

## Findings

- **Location**: `src/watercooler_mcp/federation/resolver.py`, line 130
- **Current**: `primary_ns_id = primary_context.code_root.name if primary_context.code_root else "primary"`
- **Context**: Already flagged in Round 5 and documented as a defensive guard

## Proposed Solution

Verify the existing defensive guard comment is sufficient. Add a brief note that allowlist consumers should be aware of this fallback.

**Effort:** Minimal | **Risk:** None

## Acceptance Criteria

- [ ] Existing comment verified or updated

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From PR #190 review round 8 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
