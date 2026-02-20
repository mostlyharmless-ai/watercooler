---
status: completed
priority: p2
issue_id: "019"
tags: [code-review, correctness, federation]
dependencies: []
---

# Guard against primary namespace ID collision with secondary IDs

## Problem Statement

The primary namespace ID is derived from `primary_context.code_root.name` (directory basename). If a user configures a secondary namespace with the same ID as the primary's basename, the primary silently overwrites the secondary in the results dict, or vice versa. No validation prevents this collision.

Flagged by kieran-python-reviewer, security-sentinel, and architecture-strategist.

## Findings

- **Location**: `src/watercooler_mcp/federation/resolver.py`, line 109 (`primary_ns_id = primary_context.code_root.name`)
- **Scenario**: Primary repo is `~/watercooler-cloud/` (basename `watercooler-cloud`), user configures `namespaces = {"watercooler-cloud": FederationNamespaceConfig(...)}`
- **Impact**: Silent data loss — one namespace overwrites the other in the dict
- **Existing guard**: `check_no_basename_collisions` validator only checks secondaries against each other, not primary

## Proposed Solutions

### Solution A: Guard in `resolve_all_namespaces` (Recommended)

After deriving `primary_ns_id`, check that no configured secondary has the same key:

```python
if primary_ns_id in federation_config.namespaces:
    logger.warning("Skipping secondary '%s' — collides with primary", primary_ns_id)
```

**Pros:** Simple runtime guard, no config schema changes needed
**Cons:** Only catches at runtime, not at config load time
**Effort:** Small
**Risk:** Low

### Solution B: Add primary_repo to config validation

Pass the primary repo name into `FederationConfig` validation so `check_no_basename_collisions` also checks against primary.

**Pros:** Fails fast at config load time
**Cons:** Config model would need context it doesn't currently have (primary repo path)
**Effort:** Medium
**Risk:** Low

## Acceptance Criteria

- [ ] Primary namespace ID collision with secondary is detected and handled
- [ ] Warning logged when collision detected
- [ ] Test verifies collision behavior

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3, flagged by 3 agents |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
