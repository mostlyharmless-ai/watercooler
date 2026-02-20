---
status: completed
priority: p1
issue_id: "017"
tags: [code-review, security, federation]
dependencies: []
---

# Fix log injection via unsanitized query string

## Problem Statement

In `federation.py`, the user-supplied `query` string is passed directly into the `log_action` call and f-string log messages without sanitization. A crafted query containing newlines or ANSI escape sequences could inject fake log entries or corrupt log output.

Flagged by kieran-python-reviewer and security-sentinel.

## Findings

- **Location**: `src/watercooler_mcp/tools/federation.py`, lines ~334-337 (`log_action` call) and other f-string log lines
- **Attack vector**: User supplies `query` parameter via MCP tool call — they control the string content
- **Impact**: Log injection can confuse log analysis, hide malicious activity, or trigger false alerts
- **Existing pattern**: `_sanitize_response_text()` in `_utils.py` already handles similar sanitization for error messages

## Proposed Solutions

### Solution A: Sanitize query at entry point (Recommended)

Strip control characters (newlines, ANSI escapes) from `query` at the start of `_federated_search_inner`:

```python
query = query.replace("\n", " ").replace("\r", " ")[:500]
```

**Pros:** Simple, one-line fix at the boundary, follows defense-in-depth
**Cons:** Truncation at 500 chars may drop legitimate long queries (unlikely)
**Effort:** Small
**Risk:** Low

### Solution B: Use %s-style logging

Replace f-string interpolation with `logger.info("query=%s", query)` style. Logging frameworks handle escaping.

**Pros:** Idiomatic Python logging best practice
**Cons:** Doesn't protect the `log_action` call which builds its own strings
**Effort:** Small
**Risk:** Low

## Recommended Action

Solution A — sanitize once at the boundary. Also convert f-string log calls to %-style as a secondary hardening.

## Technical Details

- **Affected files**: `src/watercooler_mcp/tools/federation.py`

## Acceptance Criteria

- [ ] `query` is sanitized before any logging or string interpolation
- [ ] No newlines or control characters can appear in log output from user input
- [ ] Existing tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3, flagged by 2 agents |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
- Existing sanitization pattern: `src/watercooler_mcp/memory/_utils.py:_sanitize_response_text()`
