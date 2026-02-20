---
status: completed
priority: p2
issue_id: "026"
tags: [code-review, documentation, configuration, federation]
dependencies: []
---

# Remove phantom scoring fields from example TOML

## Problem Statement

`src/watercooler/templates/config.example.toml` contains commented-out keys `lens_weight = 0.7` and `referenced_weight = 0.85` that were removed from `FederationScoringConfig` during the first review round (todos #010, #011). The example config now references fields that don't exist in the schema.

Flagged by kieran-python-reviewer and pattern-recognition-specialist.

## Findings

- **Location**: `src/watercooler/templates/config.example.toml`, federation scoring section
- **Impact**: Users who uncomment these fields will get validation errors (or silent ignore depending on Pydantic `extra` setting)
- **Root cause**: Example config wasn't updated when #010/#011 removed the fields

## Proposed Solutions

### Solution A: Remove the phantom lines (Recommended)

Delete the commented-out `lens_weight` and `referenced_weight` lines from the example TOML.

**Pros:** Example matches schema
**Cons:** None
**Effort:** Small
**Risk:** Low

## Acceptance Criteria

- [ ] Example TOML only contains fields that exist in `FederationScoringConfig`
- [ ] No phantom fields remain

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
