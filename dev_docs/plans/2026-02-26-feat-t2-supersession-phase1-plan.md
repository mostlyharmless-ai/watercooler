---
title: "feat: T2 supersession — Phase 1 land + mode=\"facts\" on watercooler_search"
type: feat
status: shipped
date: 2026-02-26
brainstorm: dev_docs/brainstorms/2026-02-25-t2-supersession-brainstorm.md
---

# feat: T2 Supersession — Phase 1 Land + `mode="facts"` on `watercooler_search`

## Overview

Phase 1 of the T2 bi-temporal supersession feature is almost complete as local changes. This
plan covers the one remaining gap (`mode="facts"` on `watercooler_search`), then commits and
ships everything. Phase 2 (supersession enrichment daemon) is a separate plan.

**Related thread**: `t2-supercession-testing-proposal`
**Brainstorm**: `dev_docs/brainstorms/2026-02-25-t2-supersession-brainstorm.md`
**Also connects**: `watercooler-benchmarking-doc` (entry 9 — `wc-t2-facts` portability gap)

---

## Background

Graphiti's bi-temporal edge invalidation sets `invalid_at` on a fact edge when a new episode
contradicts it. This is automatic — call chain:
`add_episode() → _extract_and_resolve_edges() → resolve_edge_contradictions() → edge.invalid_at = resolved_edge.valid_at`

Phase 1 exposes this mechanism to MCP callers:
- Adds `valid_at` / `invalid_at` to `watercooler_search` fact results
- Adds `active_only` filter to return only currently-valid facts (those with `invalid_at=None`)
- Adds `mode="facts"` as an explicit, non-fallback search mode for Graphiti temporal facts

The `wc-t2-facts` benchmark shim (in `tests/benchmarks/scripts/wc_text_tools.py`) currently
bypasses the MCP layer and calls `GraphitiBackend.search_memory_facts()` directly. Once
`mode="facts"` ships, that shim can be replaced with a standard `watercooler_search` call.

---

## What Is Already Done (Local, Uncommitted)

All of the following exists locally and is tested. It just needs `mode="facts"` added, then
committed.

### `src/watercooler_memory/backends/graphiti.py`

- `_filter_active_only(results)` — module-level helper that returns entries where
  `invalid_at is None`. Located at ~line 587.
- `search_memory_facts(active_only=False)` — new param. When `True`, over-fetches
  (`limit * 3`, capped at `MAX_SEARCH_RESULTS`) then applies `_filter_active_only()`.
- `search_facts(active_only=False)` — thin wrapper around `search_memory_facts`; passes
  `active_only` through.
- `valid_at` / `invalid_at` serialized in both methods' result dicts.

### `src/watercooler_mcp/tools/graph.py`

- `_search_graphiti_impl()` — extracts `active_only` from `**kwargs`, passes to
  `backend.search_facts()`, includes `valid_at`/`invalid_at` in every result item.
- `_search_graph_impl()` — has `active_only: bool = False` param; threads through
  `route_search()` → `_search_graphiti_impl()`.
- **Not yet done**: `mode="facts"` — see "Remaining Gap" below.

### `tests/unit/test_supersession_filter.py` (untracked)

9 unit tests across two groups:
- `TestFilterByTimeRangeOnInvalidAt` (3 tests): `_filter_by_time_range` with
  `time_key="invalid_at"` — excludes before end_time, null-safety, no-op when no bounds.
- `TestFilterActiveOnly` (6 tests): removes superseded, empty list, all valid, all superseded,
  missing key treated as valid, order preserved.

### `tests/integration/test_t2_supersession.py` (untracked)

3 integration tests (`@pytest.mark.integration @pytest.mark.integration_graphiti @pytest.mark.slow`).
Require live FalkorDB + configured LLM (OpenAI API key).

- `test_basic_supersession_one_fact_invalidated` — contradicting language-preference facts;
  asserts ≥1 `invalid_at` set, ≥1 `invalid_at=None`; also verifies `get_entity_edge()` path.
- `test_additive_facts_both_remain_active` — non-contradicting facts; asserts 0 superseded.
- `test_temporal_semantics_older_fact_superseded` — promotion scenario; also verifies
  `active_only=True` filter via `search_memory_facts()`.

---

## Remaining Gap — `mode="facts"` on `watercooler_search`

### Problem

Entry 9 of `watercooler-benchmarking-doc` identifies a "portability gap": the `wc-t2-facts`
benchmark shim calls `GraphitiBackend.search_memory_facts()` directly because there is no MCP
tool mode that:
1. Explicitly targets Graphiti temporal fact edges (vs thread entries)
2. Does NOT silently fall back to the baseline graph if Graphiti is unavailable
3. Is clearly named so callers understand they are querying bi-temporal facts

Currently `mode="entries"` + `backend="graphiti"` routes to `_search_graphiti_impl` and does
return facts — but the mode is semantically confusing (thread entries vs Graphiti fact edges
are different things) and still falls back to baseline on error.

### Solution

Add `mode="facts"` as a first-class explicit mode in `watercooler_search`:

**`infer_search_mode()` in `graph.py`** — add `"facts"` to the accepted set:
```python
if mode in ("entries", "entities", "episodes", "facts"):
    return mode
```

**`route_search()` in `graph.py`** — add a `mode == "facts"` branch before the existing
entries routing. This branch:
- Always routes to `_search_graphiti_impl` (ignores `backend` parameter)
- Returns a structured error (not a silent fallback) if Graphiti is unavailable

```python
# Facts mode — Graphiti temporal fact edges (no baseline fallback)
if mode == "facts":
    try:
        return await _search_graphiti_impl(
            ctx=ctx,
            threads_dir=threads_dir,
            query=query,
            code_path=code_path,
            **kwargs,
        )
    except RuntimeError as e:
        # Graphiti not configured — return structured error, no fallback
        return json.dumps({
            "error": "facts_mode_requires_graphiti",
            "message": str(e),
            "hint": "Set WATERCOOLER_GRAPHITI_ENABLED=1 and configure WATERCOOLER_LLM_API_KEY.",
            "results": [],
            "count": 0,
        })
```

**`_search_graph_impl()` docstring** — update `mode` param docs to document `"facts"`:
```
- facts: Search Graphiti temporal fact edges. Returns uuid, fact text, valid_at, invalid_at,
  score. Supports active_only=True to filter out superseded facts. Requires Graphiti backend;
  returns a structured error (not a baseline fallback) if unavailable.
```

**`watercooler_search` tool docstring** — add `"facts"` to the mode description in the MCP
tool docstring (the text that agents see when calling the tool).

### Result

After this change, the benchmark shim and production agents can both use:
```python
watercooler_search(
    query="Dana's role",
    mode="facts",
    active_only=True,   # optional: only currently-valid facts
    code_path="/path/to/repo",
)
```

---

## Acceptance Criteria

- [ ] `mode="facts"` accepted by `infer_search_mode()` — passes through unchanged
- [ ] `mode="facts"` in `route_search()` routes to `_search_graphiti_impl`, not baseline
- [ ] When Graphiti backend not configured, `mode="facts"` returns structured JSON error (no
  silent fallback)
- [ ] `active_only=True` with `mode="facts"` removes entries where `invalid_at` is set
- [ ] `valid_at` and `invalid_at` present in every fact result item
- [ ] Unit tests pass: `pytest tests/unit/test_supersession_filter.py -v`
- [ ] No regressions in existing search tests

---

## Implementation Steps

### Step 1 — Add `mode="facts"` (the remaining gap)

Edit `src/watercooler_mcp/tools/graph.py`:

1. `infer_search_mode()`: add `"facts"` to accepted set
2. `route_search()`: add `mode == "facts"` branch (before entries routing) with structured
   error on Graphiti unavailability
3. `_search_graph_impl()` docstring: document `"facts"` mode
4. `watercooler_search` registered tool's docstring: mention `"facts"` mode

### Step 2 — Run unit tests

```bash
pytest tests/unit/test_supersession_filter.py -v
```

Expected: 9 tests pass.

### Step 3 — Commit

Stage and commit:
```
src/watercooler_memory/backends/graphiti.py
src/watercooler_mcp/tools/graph.py
tests/unit/test_supersession_filter.py
tests/integration/test_t2_supersession.py
```

Commit message:
```
feat(memory): T2 supersession — active_only filter + mode="facts" on watercooler_search

Exposes Graphiti's bi-temporal edge invalidation (invalid_at) to MCP callers:

- Add _filter_active_only() to graphiti.py — removes superseded fact edges
- Add active_only param to search_memory_facts() and search_facts() with
  over-fetch (limit * 3, capped at MAX_SEARCH_RESULTS) before post-filter
- Expose valid_at / invalid_at in search result dicts
- Thread active_only through _search_graph_impl → route_search → _search_graphiti_impl
- Add mode="facts" to watercooler_search — explicit Graphiti fact edge mode with
  structured error (not silent baseline fallback) when Graphiti is unavailable
- Add 9 unit tests (test_supersession_filter.py) — no live services required
- Add 3 integration tests (test_t2_supersession.py) — require FalkorDB + LLM

Closes the wc-t2-facts portability gap identified in watercooler-benchmarking-doc entry 9.
```

### Step 4 — Push

(Per user instruction, push was deferred. Push when ready.)

---

## Not In This Plan (Phase 2)

Phase 2 — supersession enrichment daemon — is a separate deliverable:
- `SupersessionEnricher(BaseDaemon)` subclass
- Periodic scan (300s) of FalkorDB edges where `invalid_at IS NOT NULL AND superseded_by IS NULL`
- Temporal proximity inference: find successor edge F where `|F.valid_at - E.invalid_at|` is minimal
- Write `superseded_by = F.uuid` directly via Cypher (bypassing Graphiti abstraction)
- Emit a `Finding` per new link for audit trail

Design captured in: `dev_docs/brainstorms/2026-02-25-t2-supersession-brainstorm.md`

---

## References

| Item | Location |
|------|----------|
| Brainstorm doc | `dev_docs/brainstorms/2026-02-25-t2-supersession-brainstorm.md` |
| Backend implementation | `src/watercooler_memory/backends/graphiti.py` (lines ~587, ~2066, ~2286) |
| MCP tool | `src/watercooler_mcp/tools/graph.py` (lines ~130, ~151, ~369, ~658) |
| Unit tests | `tests/unit/test_supersession_filter.py` |
| Integration tests | `tests/integration/test_t2_supersession.py` |
| Benchmark shim (portability gap) | `tests/benchmarks/scripts/wc_text_tools.py` |
| Graphiti edge operations | `external/graphiti/graphiti_core/utils/maintenance/edge_operations.py` |
