---
status: pending
priority: p3
issue_id: "038"
tags: [code-review, enhancement, federation]
dependencies: []
---

# entry_data omits timestamp

## Problem Statement

`tools/federation.py:45-55` — `_extract_entry_data()` extracts 7 fields but not `timestamp`. Consumers can't display or sort by time without a follow-up read. Timestamp is already available on the entry object.

## Findings

- **Location**: `src/watercooler_mcp/tools/federation.py`, lines 45-55
- **Current**: Extracts topic, title, entry_id, role, agent, entry_type, summary
- **Missing**: `timestamp` field

## Proposed Solution

Add `"timestamp": getattr(entry, "timestamp", "")` to the extracted fields dict.

**Effort:** Minimal | **Risk:** None

## Acceptance Criteria

- [ ] `timestamp` included in `_extract_entry_data()` output

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From PR #190 review round 8 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
