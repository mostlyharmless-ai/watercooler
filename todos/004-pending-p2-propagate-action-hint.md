---
status: completed
priority: p2
issue_id: "004"
tags: [code-review, agent-native, federation]
dependencies: []
---

# Propagate `action_hint` and `error_message` to response envelope

## Problem Statement

The resolver correctly populates `action_hint` (for `not_initialized` namespaces) and `error_message` (for error namespaces) on `NamespaceResolution`, but the tool handler discards both -- only storing `res.status` as a string. Agents receiving `"site": "not_initialized"` cannot self-heal without knowing what action to take.

## Findings

- **File:** `src/watercooler_mcp/tools/federation.py`, line 145 -- only stores `res.status`
- **File:** `src/watercooler_mcp/federation/merger.py`, line 122 -- `namespace_status: dict[str, str]` has no room for richer metadata
- **Agents:** Agent-Native Reviewer (x2)
- **Severity:** Should Fix (P2)

The plan document (line 581) explicitly marked this as "agent-native reviewer, CRITICAL" and the resolver populates:
```python
action_hint=f"Run watercooler_health(code_path='{ns_config.code_path}') to bootstrap..."
```

But this never reaches the JSON envelope.

## Proposed Solutions

### Solution A: Add `namespace_hints` parallel dict (Recommended)
Add `namespace_hints: dict[str, str]` alongside `namespace_status` in the envelope. Backward compatible with `schema_version: 1`.

- **Pros:** No breaking change, agents get actionable hints
- **Cons:** +1 field in envelope
- **Effort:** Small
- **Risk:** Low

### Solution B: Enrich `namespace_status` to `dict[str, dict]`
Change from `{"site": "not_initialized"}` to `{"site": {"status": "not_initialized", "action_hint": "..."}}`.

- **Pros:** All metadata in one place
- **Cons:** Breaking change to status shape, may need `schema_version: 2`
- **Effort:** Medium
- **Risk:** Medium

## Acceptance Criteria

- [ ] `action_hint` from `NamespaceResolution` appears in response envelope
- [ ] `error_message` from `NamespaceResolution` appears in response envelope
- [ ] Integration test verifies hint presence for `not_initialized` namespace
- [ ] All tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
