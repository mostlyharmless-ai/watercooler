---
status: completed
priority: p2
issue_id: "007"
tags: [code-review, agent-native, federation]
dependencies: []
---

# Add top-level exception handler to `_federated_search_impl`

## Problem Statement

Unlike other tool handlers (e.g., `watercooler_search` in `graph.py`), the federation tool handler has no catch-all `try/except`. If `config.full()` or `filter_allowed_namespaces` throws an unexpected error, a raw Python traceback propagates through MCP, which is not actionable for agents.

## Findings

- **File:** `src/watercooler_mcp/tools/federation.py` -- entire `_federated_search_impl`
- **Agents:** Agent-Native Reviewer
- **Severity:** Should Fix (P2)

## Proposed Solutions

### Solution A: Wrap with structured error return (Recommended)

```python
except Exception as e:
    logger.exception("Federated search unexpected error")
    return json.dumps({
        "error": "INTERNAL_ERROR",
        "message": f"Unexpected error: {type(e).__name__}: {str(e)[:200]}",
    })
```

- **Effort:** Small (~5 lines)
- **Risk:** Low

## Acceptance Criteria

- [ ] Top-level try/except wraps the handler body
- [ ] Returns structured JSON error consistent with other federation errors
- [ ] Test added for unexpected exception path

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
