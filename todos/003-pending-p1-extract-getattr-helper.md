---
status: completed
priority: p1
issue_id: "003"
tags: [code-review, quality, federation, type-safety]
dependencies: []
---

# Extract scattered `getattr` calls into typed helper

## Problem Statement

Seven `getattr` calls on `sr.entry` are scattered across the tool handler with no clear type contract. This makes the code harder to reason about and test in isolation.

## Findings

- **File:** `src/watercooler_mcp/tools/federation.py`, lines 209-242
- **Agents:** Python Quality Reviewer
- **Severity:** Blocking (P1)

```python
entry_data = {
    "topic": getattr(sr.entry, "thread_topic", ""),
    "title": getattr(sr.entry, "title", ""),
    "entry_id": getattr(sr.entry, "entry_id", ""),
    # ... 4 more
}
```

Additionally, `hasattr` + attribute access at line 216-217 should use consistent `getattr` pattern.

## Proposed Solutions

### Solution A: Extract typed helper function (Recommended)

```python
def _extract_entry_data(entry: object) -> dict[str, str]:
    """Extract federation-relevant fields from a search result entry."""
    return {
        "topic": getattr(entry, "thread_topic", "") or "",
        "title": getattr(entry, "title", "") or "",
        "entry_id": getattr(entry, "entry_id", "") or "",
        "role": getattr(entry, "role", "") or "",
        "agent": getattr(entry, "agent", "") or "",
        "entry_type": getattr(entry, "entry_type", "") or "",
        "summary": getattr(entry, "summary", "") or "",
    }
```

- **Pros:** Single location for field extraction, typed return, testable in isolation
- **Cons:** One more function
- **Effort:** Small
- **Risk:** Low

## Acceptance Criteria

- [ ] `getattr` calls consolidated into helper function
- [ ] `hasattr` pattern at line 216-217 uses consistent `getattr` approach
- [ ] All tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
