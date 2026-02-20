---
status: completed
priority: p3
issue_id: "010"
tags: [code-review, yagni, federation]
dependencies: []
---

# Remove dormant `referenced_weight` config field

## Problem Statement

`referenced_weight` in `FederationScoringConfig` is explicitly labeled "Phase 2, dormant -- no Phase 1 code reads this". Shipping config fields that no code reads confuses users who set them expecting behavior changes.

## Findings

- **File:** `src/watercooler/config_schema.py`, lines 960-963
- **Agents:** Python Quality Reviewer, Code Simplicity Reviewer
- **Severity:** Nice-to-Have (P3) -- YAGNI violation

## Proposed Solutions

Remove the field entirely. Add it in the Phase 2 PR when code actually reads it.

- **Effort:** Small (-4 LOC)
- **Risk:** None

## Acceptance Criteria

- [ ] `referenced_weight` removed from `FederationScoringConfig`
- [ ] TOML example updated
- [ ] All tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
