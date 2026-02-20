---
status: completed
priority: p2
issue_id: "009"
tags: [code-review, performance, federation]
dependencies: []
---

# Consider dedicated thread pool for federation searches

## Problem Statement

`asyncio.to_thread()` dispatches to the default `ThreadPoolExecutor` (typically 32 workers). Each federated search consumes N threads (one per namespace). With 7+ concurrent federated searches, the pool saturates, causing spurious timeouts for namespaces that were queue-starved, not slow.

## Findings

- **File:** `src/watercooler_mcp/tools/federation.py`, lines 183-185
- **Agents:** Performance Reviewer (x2)
- **Severity:** Should Fix (P2) -- not urgent for Phase 1 single-user, important for future

## Proposed Solutions

### Solution A: Dedicated bounded executor (Recommended for Phase 2)

```python
_federation_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=10, thread_name_prefix="federation-search"
)
# Use loop.run_in_executor(_federation_executor, ...) instead of to_thread()
```

- **Effort:** Small (~20 lines)
- **Risk:** Low

### Solution B: Document limitation (Acceptable for Phase 1)
Add comment noting the shared pool constraint and scaling limit.

## Acceptance Criteria

- [ ] Either dedicated executor created or limitation documented
- [ ] No regression in existing tests

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
