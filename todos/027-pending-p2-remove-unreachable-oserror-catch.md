---
status: completed
priority: p2
issue_id: "027"
tags: [code-review, quality, federation]
dependencies: []
---

# Remove unreachable `OSError` catch from `_negate_epoch`

## Problem Statement

`_negate_epoch()` in `merger.py` catches `(ValueError, OSError)` but `datetime.fromisoformat()` and `datetime.timestamp()` only raise `ValueError` for invalid input. `OSError` is unreachable in this code path, making the except clause misleading.

Flagged by kieran-python-reviewer.

## Findings

- **Location**: `src/watercooler_mcp/federation/merger.py`, line ~91
- **Code**: `except (ValueError, OSError):`
- **Analysis**: `fromisoformat` raises `ValueError` for bad formats. `timestamp()` can raise `OSError` for extreme dates (year 1, year 9999) in theory, but ULID timestamps are bounded to recent dates. The `OSError` catch is defensive but unreachable in practice.

## Proposed Solutions

### Solution A: Remove `OSError` (Recommended)

Change to `except ValueError:` only.

**Pros:** Accurate exception handling, no misleading catches
**Cons:** Theoretical edge case for extreme dates (not possible with ULID timestamps)
**Effort:** Small
**Risk:** Low

## Acceptance Criteria

- [ ] Exception handler only catches `ValueError`
- [ ] Tests pass

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
