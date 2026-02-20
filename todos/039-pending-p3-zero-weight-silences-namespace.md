---
status: completed
priority: p3
issue_id: "039"
tags: [code-review, docs, federation]
dependencies: []
---

# weight=0.0 silences namespace without warning

## Problem Statement

`config_schema.py:957-963` — `local_weight` and `wide_weight` have `ge=0.0`, but a weight of `0.0` effectively silences an entire namespace. The schema permits it without warning.

## Findings

- **Location**: `src/watercooler/config_schema.py`, lines 957-963
- **Current**: `ge=0.0` constraint, no documentation about 0.0 behavior
- **Behavior**: 0.0 weight zeroes out all ranking_scores for the namespace

## Proposed Solution

Add a note in the field description that `0.0` disables the namespace. Keeping `ge=0.0` (not changing to `gt=0.0`) is correct — disabling is a valid use case, but should be documented.

**Effort:** Minimal | **Risk:** None

## Acceptance Criteria

- [ ] Field descriptions note that 0.0 effectively disables the namespace

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From PR #190 review round 8 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
