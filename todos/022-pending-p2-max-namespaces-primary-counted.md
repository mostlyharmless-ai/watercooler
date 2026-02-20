---
status: completed
priority: p2
issue_id: "022"
tags: [code-review, documentation, federation]
dependencies: []
---

# Clarify `max_namespaces` includes primary in count

## Problem Statement

The `max_namespaces` config field (default 5, max 20) doesn't document whether the primary namespace counts toward the limit. This ambiguity can cause off-by-one behavior.

Flagged by kieran-python-reviewer.

## Findings

- **Location**: `src/watercooler/config_schema.py`, `max_namespaces` field definition
- **Ambiguity**: If a user sets `max_namespaces = 3` and configures 3 secondary namespaces, do they get 3 total (primary + 2 secondary) or 4 (primary + 3 secondary)?
- **Current code**: The enforcement in `federation.py` counts secondaries only — primary is always included

## Proposed Solutions

### Solution A: Update field description (Recommended)

Add clarifying text to the field description: "Maximum number of secondary namespaces to query (primary is always included)."

**Pros:** Simple documentation fix, no behavior change
**Cons:** None
**Effort:** Small
**Risk:** Low

## Acceptance Criteria

- [ ] `max_namespaces` field description clarifies whether primary is counted
- [ ] Behavior matches documentation

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
