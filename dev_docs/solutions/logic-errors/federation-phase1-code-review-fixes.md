---
title: "Federation Phase 1: Cross-namespace keyword search — 14-round review hardening"
category: logic-errors
tags:
  - federation
  - cross-namespace-search
  - mypy-strict
  - pydantic-v2
  - dedup
  - validation
  - code-review
  - asyncio
module:
  - src/watercooler_mcp/tools/federation.py
  - src/watercooler_mcp/federation/merger.py
  - src/watercooler_mcp/federation/scoring.py
  - src/watercooler_mcp/federation/access.py
  - src/watercooler_mcp/federation/resolver.py
  - src/watercooler/config_schema.py
symptom:
  - "mypy strict mode failures: T | None passed to callsites expecting T"
  - "Dedup race: secondary namespace version wins over primary due to dict iteration order"
  - "Empty entry_id values collapse all entries into a single dedup bucket"
  - "namespace_timeout can exceed max_total_timeout without validation error"
  - "PRIMARY_SEARCH_FAILED error response omits diagnostic namespace_status dict"
root_cause:
  - "Type-narrowing not propagated after None checks in federation tool handler"
  - "Dedup kept first occurrence from unsorted dict iteration — secondary could win"
  - "Dedup key derived from entry_id without guard for empty/missing values"
  - "Cross-field validation between namespace_timeout and max_total_timeout not implemented"
  - "Error-path return dict constructed without namespace_status field"
  - "Namespace override not validated against max_namespaces limit after resolution"
date_solved: 2026-02-20
pr_number: 190
follow_up_issues:
  - 193
  - 194
  - 195
  - 196
  - 197
  - 198
  - 199
  - 200
  - 201
  - 202
  - 203
  - 204
review_rounds: 14
test_count: 111
---

# Federation Phase 1: Code Review Fixes

PR #190 introduced federated cross-namespace keyword search to watercooler-cloud. Over 14 review rounds, six merge-blocking defects were identified and resolved. This document captures the bugs, fixes, and prevention strategies.

## Problem Symptoms

1. `mypy --strict` failures on two `T | None` variables used without narrowing
2. Federated search returned secondary-namespace entries instead of primary when both had same entry
3. Empty `entry_id` values collapsed all unidentified entries into a single result
4. Config accepted `namespace_timeout > max_total_timeout` silently
5. `PRIMARY_SEARCH_FAILED` error response lacked diagnostic metadata
6. Comma-separated namespace override allowed unbounded fan-out, bypassing `max_namespaces` safeguard

## Root Cause Analysis

All six bugs share a common theme: **implicit assumptions not enforced by code**.

- mypy couldn't see through `_require_context`'s contract that non-error means non-None
- Dedup assumed dict iteration would place primary first — no explicit sort
- Dedup assumed `entry_id` would always be non-empty — no guard
- Config assumed users wouldn't set contradictory timeouts — no cross-field validation
- Error path assumed callers only needed the error message — no diagnostics
- Namespace override not validated against `max_namespaces` limit after resolution

## Fixes Applied

Fixes are grouped by type, not chronological order. See the Key Commits table for the actual timeline.

### Fix 1: Sort before dedup in merger (Round 10)

**File:** `src/watercooler_mcp/federation/merger.py`

The merge function iterated `namespace_results.values()` to build a flat list, then deduped by keeping the first occurrence of each `entry_id`. If a secondary namespace appeared first in dict order, it won the dedup race.

**Fix:** Sort the combined list before dedup. The sort key uses `ranking_score` descending with a primary-first tiebreaker, ensuring primary always wins at equal score.

```python
# After min_score filter, sort BEFORE dedup:
all_results.sort(key=sort_key)

seen: set[str] = set()
deduped: list[ScoredResult] = []
for r in all_results:
    if r.entry_id not in seen:
        seen.add(r.entry_id)
        deduped.append(r)

return deduped[:limit]
```

### Fix 2: Include namespace_status in error response (Round 10)

**File:** `src/watercooler_mcp/tools/federation.py`

The `PRIMARY_SEARCH_FAILED` error response only returned `schema_version`, `error`, `message`, `results` — discarding the already-computed `namespace_status` dict with per-namespace diagnostics.

**Fix:** Add `namespace_status`, `queried_namespaces`, and `primary_namespace` to the error response.

```python
return json.dumps({
    "schema_version": 1,
    "error": "PRIMARY_SEARCH_FAILED",
    "message": f"Primary namespace '{primary_ns_id}' search failed: {primary_status_val}",
    "results": [],
    "primary_namespace": primary_ns_id,
    "queried_namespaces": list(resolutions.keys()),
    "namespace_status": namespace_status,
})
```

### Fix 3: mypy type-narrowing asserts (Round 12)

**File:** `src/watercooler_mcp/tools/federation.py`

Two variables typed as `T | None` were used after logical guards that guaranteed non-None, but mypy couldn't narrow through the guard pattern.

**Fix:** Explicit assert guards with contract documentation.

```python
# After "if error: return ..."
assert primary_ctx is not None  # guaranteed by _require_context contract

# Inside search_namespace, after filtering to status="ok"
assert res.threads_dir is not None  # guaranteed: only status="ok" namespaces are searchable
```

### Fix 4: Cross-field timeout validation (Round 13)

**File:** `src/watercooler/config_schema.py`

`FederationConfig` had independent fields `namespace_timeout` and `max_total_timeout` with no cross-field constraint. A config with `namespace_timeout=30, max_total_timeout=2` was silently accepted — guaranteeing every search would timeout.

**Fix:** Pydantic v2 `model_validator(mode="after")`.

```python
@model_validator(mode="after")
def check_timeout_ordering(self) -> "FederationConfig":
    if self.namespace_timeout > self.max_total_timeout:
        raise ValueError(
            f"namespace_timeout ({self.namespace_timeout}s) must be <= "
            f"max_total_timeout ({self.max_total_timeout}s)"
        )
    return self
```

**Cascading test fix:** Existing tests used `namespace_timeout=30.0` without setting `max_total_timeout`. Fixed by adding `max_total_timeout=30.0` to those fixtures.

### Fix 5: Skip entries with empty entry_id (Round 13)

**File:** `src/watercooler_mcp/tools/federation.py`

Entries with empty `node_id` (the field that maps to `entry_id` in `ScoredResult`) all mapped to the same dedup key (`""`), collapsing distinct results into one.

**Fix:** Skip guard before scoring.

```python
# Skip entries without a usable ID (prevents dedup collapse)
if not sr.node_id:
    continue
```

### Fix 6: Namespace override max_namespaces validation (Round 9)

**File:** `src/watercooler_mcp/tools/federation.py`

The comma-separated namespace override wasn't validated against `max_namespaces`, allowing unbounded fan-out. A misconfigured or malicious caller could request arbitrarily many namespaces, bypassing the resource safeguard.

**Fix:** Count secondary namespaces after resolution and reject if the cap is exceeded.

```python
# 7. Check max_namespaces cap (primary doesn't count toward limit)
secondary_count = len(resolutions) - 1  # Exclude primary
if secondary_count > fed_config.max_namespaces:
    return json.dumps({
        "schema_version": 1,
        "error": "TOO_MANY_NAMESPACES",
        "message": (
            f"Query spans {secondary_count} secondary namespaces, "
            f"exceeding max_namespaces={fed_config.max_namespaces}"
        ),
        "results": [],
    })
```

**Note:** Per Prevention Strategy 5, this error response could include `queried_namespaces` so callers see which namespaces were resolved before hitting the cap. Tracked for Phase 2.

## Key Commits

| Commit | Round | Fixes |
|--------|-------|-------|
| `866b2da` | 9 | Namespace override max_namespaces validation |
| `7e54ade` | 10 | Dedup sort order, PRIMARY_SEARCH_FAILED diagnostics |
| `b8afb0a` | 12 | mypy type-narrowing asserts |
| `75a407e` | 13 | Timeout cross-field validator, empty entry_id guard |

## Prevention Strategies

### 1. Always assert after guard clauses

When a function returns `tuple[T | None, Error | None]` and you guard with `if error: return`, add `assert result is not None` immediately after. mypy can't narrow through function-call contracts.

### 2. Sort before dedup when order matters

Never rely on dict iteration order for dedup correctness. If tiebreaking semantics exist (e.g., primary beats secondary), define an explicit sort key and apply it before the dedup loop.

### 3. Use model_validator for cross-field config constraints

Pydantic `field_validator` operates on individual fields. For ordering invariants between fields (like `timeout_a <= timeout_b`), use `model_validator(mode="after")`.

### 4. Validate dedup keys are non-empty

Empty-string keys are valid dict/set keys. Any dedup logic must skip or reject entries with empty dedup keys, or use a compound key that provides discrimination.

### 5. Error responses need full diagnostics

Error-handling code paths should include all diagnostic data computed up to the failure point. Define response models where diagnostic fields are structurally required, not optional.

### 6. Grep test fixtures after adding validators

After adding a cross-field validator, grep all test files for the involved field names and update fixtures. Use factory functions in `conftest.py` for config construction so defaults satisfy all invariants.

### 7. Triage review findings against existing issues

Automated reviewers produce duplicates and false positives. For each finding, check: (1) Is it already tracked? (2) Does the referenced code exist? (3) Is the finding based on correct understanding? Respond to every finding with an explicit verdict.

## Related Documentation

- **Architecture spec:** `docs/watercooler-planning/FEDERATED_WATERCOOLER_ARCHITECTURE.md` (906 lines, v4.5a; lives in `external/` submodule)
- **Implementation plan:** `docs/plans/2026-02-19-feat-federated-search-phase1-plan.md` (completed; gitignored, local only)
- **Brainstorm:** `docs/brainstorms/2026-02-19-federation-phase1-brainstorm.md` (gitignored, local only)
- **Config reference:** `docs/CONFIGURATION.md` (federation section, lines 231-260)
- **MCP tool reference:** `docs/mcp-server.md` (watercooler_federated_search, lines 289-316)
- **asyncio.to_thread precedent:** `docs/watercooler-planning/MEMORY_CONSOLIDATION_PHASE3.md`, PR #172
- **Follow-up issues:** #193-#204 (12 issues covering Phase 2, cleanup, and validation gaps)

## Verification

All 111 federation tests pass across 7 test files. PR merged to `main` on 2026-02-20.
