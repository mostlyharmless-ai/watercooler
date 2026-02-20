---
status: completed
priority: p3
issue_id: "013"
tags: [code-review, quality, federation]
dependencies: []
---

# Remove unused `namespace` param from `is_topic_denied()`

## Problem Statement

The `namespace` parameter in `is_topic_denied()` is documented as "unused, for future logging" but adds noise to the signature. YAGNI -- add it when logging is implemented.

## Findings

- **File:** `src/watercooler_mcp/federation/access.py`, line 58
- **Agents:** Python Quality Reviewer, Code Simplicity Reviewer
- **Severity:** Nice-to-Have (P3)

## Proposed Solutions

Remove the parameter. Update call site in `tools/federation.py` and test file `test_federation_access.py`.

- **Effort:** Small
- **Risk:** Low

## Acceptance Criteria

- [ ] `namespace` param removed from `is_topic_denied`
- [ ] Call sites updated
- [ ] Tests updated
- [ ] All tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
