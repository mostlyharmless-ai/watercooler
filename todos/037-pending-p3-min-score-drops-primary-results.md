---
status: completed
priority: p3
issue_id: "037"
tags: [code-review, docs, federation]
dependencies: []
---

# min_score drops zero-score primary results

## Problem Statement

`merger.py:66` — a keyword score of exactly `KEYWORD_SCORE_MIN` (1.0) normalizes to 0.0, producing `ranking_score = 0.0`, which is filtered by `min_score=0.01`. Primary namespace results that matched only by the baseline scoring floor get silently dropped.

## Findings

- **Location**: `src/watercooler_mcp/federation/merger.py`, line 66
- **Current**: `all_results = [r for r in all_results if r.ranking_score >= min_score]`
- **Behavior**: Primary results are NOT exempt from min_score filtering — intentional

## Proposed Solution

Add a docstring note to `merge_results()` clarifying that primary results are not exempt from `min_score` filtering and that this is intentional (baseline-floor-only matches are noise).

**Effort:** Minimal | **Risk:** None

## Acceptance Criteria

- [ ] Docstring documents min_score applies equally to all namespaces

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From PR #190 review round 8 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
