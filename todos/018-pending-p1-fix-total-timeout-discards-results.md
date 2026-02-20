---
status: completed
priority: p1
issue_id: "018"
tags: [code-review, performance, correctness, federation]
dependencies: []
---

# Fix total timeout discarding ALL completed namespace results

## Problem Statement

In `_federated_search_inner`, `asyncio.gather(*tasks, return_exceptions=True)` is wrapped in `asyncio.wait_for(gather_coro, timeout=max_total_timeout)`. When the total timeout fires, `wait_for` raises `asyncio.TimeoutError` and **discards ALL results**, including namespaces that already completed successfully. This is a correctness bug — a single slow namespace causes zero results instead of partial results.

Flagged by performance-oracle as a critical correctness issue.

## Findings

- **Location**: `src/watercooler_mcp/tools/federation.py`, lines ~287-294
- **Scenario**: 3 namespaces queried, 2 complete in 0.1s, 1 takes 3s. With `max_total_timeout=2.0`, the timeout fires and ALL results (including the 2 fast ones) are lost
- **Impact**: Users get empty results when they should get partial results from responsive namespaces
- **Current behavior**: Returns empty `namespace_results` dict on total timeout

## Proposed Solutions

### Solution A: Use `asyncio.wait` with FIRST_EXCEPTION + timeout (Recommended)

Replace `gather` + `wait_for` with `asyncio.wait(tasks, timeout=max_total_timeout)`:

```python
done, pending = await asyncio.wait(tasks, timeout=max_total_timeout)
for task in pending:
    task.cancel()
# Collect results from done tasks
```

**Pros:** Preserves completed results, cancels only timed-out tasks, standard asyncio pattern
**Cons:** Slightly more code than gather
**Effort:** Medium
**Risk:** Low — semantics are well-defined

### Solution B: Individual `wait_for` per task

Wrap each namespace task individually in `asyncio.wait_for(task, timeout=per_ns_timeout)`, remove the outer total timeout.

**Pros:** Simple per-namespace timeout, no result loss
**Cons:** Total wall-clock time could be `N * per_ns_timeout` if tasks run sequentially (they don't since they're gathered, but conceptually)
**Effort:** Small
**Risk:** Low

## Recommended Action

Solution A — `asyncio.wait` is the correct primitive for "collect what finishes within a deadline."

## Technical Details

- **Affected files**: `src/watercooler_mcp/tools/federation.py`
- **Test coverage**: Add integration test with simulated slow namespace

## Acceptance Criteria

- [ ] Completed namespace results are preserved when total timeout fires
- [ ] Timed-out namespaces are cancelled and reported as `timeout` in namespace_status
- [ ] Test verifies partial results returned on total timeout
- [ ] Existing tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3, flagged by performance-oracle |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
- Python docs: `asyncio.wait` vs `asyncio.gather`
