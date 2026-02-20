---
status: completed
priority: p1
issue_id: "001"
tags: [code-review, quality, federation]
dependencies: []
---

# Replace `_negate_timestamp()` with negated epoch float

## Problem Statement

The `_negate_timestamp()` function in `merger.py` uses a digit-complement approach to invert ISO 8601 timestamps for descending sort. While correct, this is unnecessarily clever and a maintenance trap -- a future contributor will not immediately understand why digits are being complemented.

## Findings

- **File:** `src/watercooler_mcp/federation/merger.py`, lines 102-119
- **Agents:** Python Quality Reviewer, Code Simplicity Reviewer
- **Severity:** Blocking (P1)

The function iterates over each character in a timestamp string, complementing digits (9 - int(c)). This is 18 lines of code for what can be accomplished in 7 lines with a negated epoch float.

## Proposed Solutions

### Solution A: Negated epoch float (Recommended)
Replace `_negate_timestamp()` with a `_ts_epoch()` helper that parses the ISO timestamp to a float and negates it:

```python
def _ts_epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0
```

Sort key becomes: `(-_ts_epoch(r.timestamp),)` instead of `(_negate_timestamp(r.timestamp),)`

- **Pros:** Immediately obvious, standard approach, -11 LOC
- **Cons:** Adds datetime parsing (negligible cost at <100 results)
- **Effort:** Small
- **Risk:** Low

### Solution B: str.translate() table
Use `str.maketrans("0123456789", "9876543210")` for C-level speed.

- **Pros:** Faster than per-char loop
- **Cons:** Still non-obvious digit complement approach
- **Effort:** Small
- **Risk:** Low

## Recommended Action

Solution A -- clarity over micro-optimization.

## Technical Details

- **Affected files:** `src/watercooler_mcp/federation/merger.py`
- **Tests:** Update `test_federation_merger.py` sort-order tests (behavior unchanged)

## Acceptance Criteria

- [ ] `_negate_timestamp()` removed
- [ ] Sort key uses negated epoch float
- [ ] All merger tests pass
- [ ] Tiebreak behavior (newest first) preserved

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-19 | Created | From PR #190 code review |

## Resources

- PR #190: https://github.com/mostlyharmless-ai/watercooler-cloud/pull/190
