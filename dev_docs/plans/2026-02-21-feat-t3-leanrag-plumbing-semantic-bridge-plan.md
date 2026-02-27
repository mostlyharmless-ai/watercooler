---
title: "feat: T3 LeanRAG Plumbing + Semantic Bridge"
type: feat
status: completed
date: 2026-02-21
deepened: 2026-02-21
brainstorm: docs/brainstorms/2026-02-21-t3-epistemological-integration-brainstorm.md
thread: leanrag-tier3-integration-audit (entries 0-17)
---

# T3 LeanRAG Plumbing + Semantic Bridge

## Enhancement Summary

**Deepened on:** 2026-02-21
**Agents used:** 16 (architecture-strategist, kieran-python-reviewer,
performance-oracle, security-sentinel, pattern-recognition-specialist,
code-simplicity-reviewer, data-integrity-guardian, graphrag-engineer,
technical-documentation-specialist, git-history-analyzer, agent-native-
architecture, pydantic-hardener, federation-learning-applicability,
FalkorDB-cypher-research, MemoryTask-patterns, EntryEpisodeIndex-patterns)

### Key Improvements

1. **Rename method**: `enumerate_group_episodes()` → `get_group_episodes()`
   (matches codebase naming convention — `get_*` prefix, not `enumerate_*`)
2. **Typed return**: New method returns `list[EpisodeRecord]` dataclass, not
   raw `list[dict[str, Any]]` (aligning with the design preference for typed
   results — though existing search methods still return `list[dict]`)
3. **Python post-filtering for dates**: Confirmed FalkorDB Cypher `datetime()`
   is unreliable — codebase universally uses `_filter_by_time_range()` in Python
4. **Provenance tool handles chunked entries**: `EntryEpisodeIndex` supports
   `ChunkEpisodeMapping` — one entry may map to multiple episodes. Tool must
   expose this, plus support bidirectional lookup (episode→entry AND entry→episodes)
5. **Simplify Step 5**: Auto-derive (`group_id` from `code_path`) lives in
   Step 5 callers (not `__post_init__`). Defer hyphen detection and canonical
   comparison to Phase 2
6. **Migration note**: Config convergence changes `work_dir` from
   `threads_dir/graph/leanrag` to `~/.watercooler/leanrag_*` — existing index
   data under old path becomes unreachable (acceptable: pipeline does full rebuild)
7. **Documentation location**: `docs/SEMANTIC_BRIDGE.md` (operational reference),
   not `docs/watercooler-planning/` (internal planning archive)
8. **MemoryTask validation**: Add `__post_init__` with BULK-requires-group_id
   guard (federation learning: empty dedup keys cause silent bugs)
9. **Conflict risk**: `tools/memory.py` and `tools/graph.py` modified 2 days ago
   in graph-only-reads refactor (PR #191). Verify imports and tool registration
   against current state before implementation.
10. **Dead-letter safety**: `__post_init__` keeps task.py stdlib-only (no
    cross-package import). Auto-derive moves to callers. `ValueError` added to
    `retry_dead_letters()` and `_load()` catch clauses so one malformed task
    cannot block recovery of all subsequent tasks.
11. **code_path forwarding**: `route_search()` consumes `code_path` as a named
    parameter — it does NOT appear in `**kwargs`. Pass `code_path=code_path`
    explicitly to `_search_leanrag_impl()`.
12. **Cypher safety LIMIT**: `get_group_episodes()` adds `LIMIT $safety_limit`
    with `SAFETY_LIMIT = 10_000` constant and warning log when hit.
13. **EpisodeRecord placement**: Define in `backends/__init__.py` alongside
    `PrepareResult`, `IndexResult`, `QueryResult` — not in the god module.
14. **Agent discoverability**: Update `resources.py` instructions resource with
    provenance tool, semantic bridge pattern, and `backend="leanrag"` option.

### New Considerations Discovered

- GraphRAG expert confirms: semantic search as UMAP input is fundamentally wrong
  for **full-build** (distribution bias + missing inter-cluster edges). Full-corpus
  enumeration is the correct approach. The incremental path (parametric UMAP
  `transform()` + cosine similarity assignment) is unaffected — it correctly
  takes only new embeddings, not the full corpus.
- Semantic bridge precision depends heavily on leaf description quality: with
  named entities → 0.65-0.80 precision; abstract summaries → 0.30-0.50. Phase 2
  enrichment is essential, not optional.
- `_leanrag_pipeline_executor_fn` creates `LeanRAGConfig` with `work_dir=None`,
  which falls through to `tempfile.mkdtemp()`. Indexed data goes into randomly-
  named FalkorDB graphs — data exists but is unreachable by queries.
- `get_group_episodes()` is NOT a `MemoryBackend` protocol method (only
  meaningful for Graphiti). Document this explicitly to prevent `AttributeError`
  when called through the generic interface.

## Overview

Fix the three-way config inconsistency that makes T3 (LeanRAG) unreliable, add
project-context propagation so the pipeline indexes and queries the correct
FalkorDB graph, expose EntryEpisodeIndex for reverse provenance, wire dormant
date filters, and document the T3 → T2 → T1 semantic bridge pattern for agents.

This is Phase 1 of the epistemological integration described in the
[brainstorm](../brainstorms/2026-02-21-t3-epistemological-integration-brainstorm.md).
Phases 2 (Graphiti-seeded LeanRAG) and 3 (JTB certification gate) are scoped as
follow-on work.

## Problem Statement

T3 is currently broken at the plumbing level:

1. **Config divergence** — Three sites construct bare `LeanRAGConfig()` with
   only `leanrag_path` set, bypassing `load_leanrag_config()` which resolves
   `work_dir`, LLM/embedding keys, and database name from unified config. The
   correct factory lives in `src/watercooler_mcp/memory.py:185` but is unused
   by the pipeline tool, executor, and search tool.

2. **FalkorDB graph name mismatch** — Indexing (via `load_leanrag_config`)
   writes to graph `leanrag_watercooler_cloud` (`work_dir.name`). Querying (via
   `_search_leanrag_impl`) reads from graph `leanrag` (hardcoded
   `threads_dir/graph/leanrag`). Result: queries silently return zero results.

3. **Bare `GraphitiBackend()` constructors** — Two sites create Graphiti
   backends with no config, relying on env vars rather than unified config.

4. **Wrong Graphiti episode API usage** — Both pipeline call sites use
   `get_episodes(group_ids=[...], limit=N)` — but the actual API is
   `get_episodes(query, group_ids, max_episodes, start_time, end_time)`. It
   requires a `query` string (semantic search), uses `max_episodes` not `limit`,
   is sync (not awaitable), and returns `list[dict]` not a dict with
   `"episodes"` key. Both callers `await` it and call `.get("episodes")`.
   (`graphiti.py:2356-2363`, `tools/memory.py:798-802`, `memory_sync.py:752-756`)

5. **Semantic search can't enumerate full corpus** —
   `GraphitiBackend.get_episodes()` performs semantic search; it cannot enumerate
   all episodes for a group (`graphiti.py:2366`). LeanRAG clustering (UMAP)
   requires the full corpus as input, not a semantic subset. The pipeline needs a
   different data collection primitive.

6. **No project-context propagation** — `_leanrag_run_pipeline_impl` has no
   `code_path` parameter. `MemoryTask` has no `code_path` field. The queued
   executor cannot call `load_leanrag_config(code_path=...)`.

7. **Dormant date filters** — `start_date`/`end_date` are accepted by the MCP
   tool but never forwarded to episode fetching.

8. **No reverse provenance** — `EntryEpisodeIndex.get_entry(episode_uuid)` is
   not exposed via MCP. Agents cannot trace T3 results back to T1 entries.

9. **Broken incremental import** — `backends/leanrag.py:849` imports
   `leanrag.pipelines.incremental` without a try/except guard.

## Proposed Solution

Replace all ad-hoc LeanRAG/Graphiti construction with the canonical factory
pattern (`load_leanrag_config` / `load_graphiti_config`), replace the broken
`get_episodes()` calls with a new Cypher-based `get_group_episodes()` that
provides the full corpus LeanRAG needs (returning typed `EpisodeRecord`
dataclasses), add `code_path` to `MemoryTask` with `__post_init__` validation,
expose EntryEpisodeIndex via MCP with bidirectional + chunk-aware lookup, and
auto-derive `group_id` from `code_path`.

## Technical Approach

### Architecture

All changes follow the existing two-stage pattern already used by T2 (Graphiti):

```
code_path → load_{backend}_config(code_path) → BackendConfig → Backend(config)
```

The fix applies this pattern to every T3 call site. No new abstractions.

### Broken Sites (Exhaustive)

| # | Location | Line | Problem |
|---|----------|------|---------|
| 1 | `tools/memory.py` `_get_leanrag_backend()` | 652 | Bare `LeanRAGConfig(leanrag_path=...)` |
| 2 | `memory_sync.py` `_leanrag_pipeline_executor_fn()` | 690 | Bare `LeanRAGConfig(leanrag_path=...)` |
| 3 | `tools/graph.py` `_search_leanrag_impl()` | 566-568 | Bare config + wrong `work_dir` |
| 4 | `tools/memory.py` `_leanrag_run_pipeline_impl()` | 797 | Bare `GraphitiBackend()` |
| 5 | `memory_sync.py` `_leanrag_pipeline_executor_fn()` | 751 | Bare `GraphitiBackend()` |
| 6 | `tools/memory.py` `_leanrag_run_pipeline_impl()` | 798 | Wrong API: `await get_episodes(group_ids=..., limit=...)` — API is sync, needs `query`, uses `max_episodes`, returns `list` not dict |
| 7 | `memory_sync.py` `_leanrag_pipeline_executor_fn()` | 752 | Same wrong API usage as #6 |
| 8 | `memory_queue/task.py` `MemoryTask` | 50-115 | Missing `code_path` field |
| 9 | `backends/leanrag.py` `incremental_index()` | 849 | Unguarded `import` |
| 10 | `tools/memory.py` `_leanrag_run_pipeline_impl()` | 670-671 | Date filters accepted, never forwarded |
| 11 | `memory_sync.py` `_leanrag_pipeline_executor_fn()` | 752 | Date filters parsed, never forwarded |

### Implementation Steps

#### Step 1: Incremental Import Guard

**Files**: `src/watercooler_memory/backends/leanrag.py`

Wrap the broken import at line 849 in a try/except:

```python
try:
    from leanrag.pipelines.incremental import incremental_update
except ImportError:
    logger.warning(
        "leanrag.pipelines.incremental not available; "
        "falling back to full rebuild"
    )
    incremental_update = None  # type: ignore[assignment]
```

Guard the usage site to fall back to full `index()` when `incremental_update is
None`, with a warning in the result.

- **Effort**: ~10 LOC
- **Risk**: None — purely defensive
- **Dependencies**: None

> **Research insight (pattern-recognition)**: The existing `graphiti.py` already
> uses this pattern via `_is_graphiti_installed()` and
> `_ensure_graphiti_available()` (lines 67-166). The incremental guard should
> follow the same convention. `type: ignore[assignment]` is the correct mypy
> escape for the `None` assignment (confirmed by Python reviewer).

#### Step 2: Config Convergence (3 sites)

**Files**: `src/watercooler_mcp/tools/memory.py`, `src/watercooler_mcp/tools/graph.py`, `src/watercooler_mcp/memory_sync.py`

**2a. Replace `_get_leanrag_backend()` in `tools/memory.py:638-664`**

Current: Constructs `LeanRAGConfig(leanrag_path=Path(leanrag_path))` with only
leanrag_path set, missing work_dir/LLM/embedding/database.

Fix: Accept `code_path: str = ""` parameter. Delegate to
`load_leanrag_config(code_path=code_path)`. Return `None` with logged warning
if config is unavailable (matching `validate_memory_prerequisites` pattern for
Graphiti). Return `LeanRAGBackend(config)`.

**2b. Replace bare config in `_search_leanrag_impl()` at `tools/graph.py:539-605`**

Current: `work_dir=threads_dir / "graph" / "leanrag"` and
`leanrag_path=Path("external/LeanRAG")` — hardcoded, wrong graph name.

Fix: Add `code_path: str = ""` parameter to `_search_leanrag_impl()`.
In `route_search()`, pass `code_path=code_path` explicitly to the LeanRAG
call site at `graph.py:229-232` (currently only `ctx`, `threads_dir`, and
`query` are forwarded; `code_path` is consumed as a named parameter at
`graph.py:157` and does **NOT** appear in `**kwargs`). Call
`load_leanrag_config(code_path=code_path)`. Return error if config
unavailable. After extracting `code_path` explicitly, audit whether
`**kwargs` is still consumed downstream — if not, replace with explicit
parameters for better discoverability and static analysis.

**2c. Replace bare config in `_leanrag_pipeline_executor_fn()` at `memory_sync.py:686-691`**

Current: `LeanRAGConfig(leanrag_path=Path(leanrag_path))`.

Fix: Extract `code_path` from `MemoryTask.code_path` (added in Step 4). Call
`load_leanrag_config(code_path=code_path)`. If `code_path` is absent (empty
string), **hard-fail with a clear error** rather than falling back to
`load_leanrag_config()` with no code_path. Falling back to the generic
`"watercooler"` database silently cross-contaminates projects and makes
debugging extremely difficult. Error message:
`"LeanRAG pipeline requires code_path for project-scoped config. "
"Set code_path on the MemoryTask or provide it to the MCP tool."`

- **Effort**: ~60 LOC (3 sites, ~20 each)
- **Risk**: Low — follows existing Graphiti pattern. Hard-fail on missing
  code_path is safer than silent cross-contamination.
- **Dependencies**: None (Step 4 adds code_path to MemoryTask)

> **Research insights (architecture, data-integrity, git-history)**:
>
> - **work_dir path change is a functional behavior change**: Currently
>   `_search_leanrag_impl` hardcodes `work_dir=threads_dir/"graph"/"leanrag"`,
>   while `load_leanrag_config()` resolves to `~/.watercooler/leanrag_{db_name}`.
>   Any existing LeanRAG index data under `{threads_dir}/graph/leanrag/` will
>   not be found after migration. This is acceptable because: (a) the pipeline
>   does full rebuilds, (b) the old data was written to random temp dirs by the
>   executor anyway (see Enhancement Summary), and (c) the graph name mismatch
>   meant zero queries ever succeeded. No migration needed — just a note in
>   the commit message.
>
> - **Executor `work_dir=None` falls to tempdir**: `_leanrag_pipeline_executor_fn`
>   at `memory_sync.py:688-691` creates `LeanRAGConfig(leanrag_path=...)` with
>   no `work_dir`, which `index()` at `leanrag.py:587` resolves to
>   `tempfile.mkdtemp(prefix="leanrag-index-")`. Indexed data goes into randomly-
>   named FalkorDB graphs. This means no LeanRAG pipeline data has ever been
>   queryable through the executor path.
>
> - **Conflict risk (MEDIUM)**: `tools/memory.py` and `tools/graph.py` were both
>   modified 2 days ago in the graph-only-reads refactor (PR #191, commits
>   `3628af0` and `3ec2be0`). Verify current imports and tool registration state
>   before implementing. `memory_sync.py` is stable (LOW risk).
>
> - **Federation learning — Strategy 1 (assert after guards)**: After calling
>   `_require_context(code_path)` or `validate_memory_prerequisites()`, add
>   `assert context is not None` after the `if error: return` guard. mypy
>   cannot narrow through these function-call contracts.
>
> - **Federation learning — Strategy 5 (error diagnostics)**: The hard-fail
>   error in Step 2c should include diagnostic context beyond just the message.
>   Include `group_id`, `task_id`, and the config resolution chain attempted.
>   Pattern: return a JSON dict with `error`, `message`, and `diagnostics` keys.

#### Step 3: Graphiti Backend Factory + Episode API Fix (2 sites)

**Files**: `src/watercooler_mcp/tools/memory.py`, `src/watercooler_mcp/memory_sync.py`, `src/watercooler_memory/backends/graphiti.py`

**3a. Replace `GraphitiBackend()` at `tools/memory.py:797`**

In `_leanrag_run_pipeline_impl()`, the direct execution path creates a bare
Graphiti backend to fetch episodes. Replace with:

```python
graphiti_config = load_graphiti_config(code_path=code_path)
if graphiti_config is None:
    return mem.create_error_response(
        "Graphiti backend not configured",
        "LeanRAG pipeline requires Graphiti for episode retrieval",
        "leanrag_run_pipeline",
    )
graphiti = GraphitiBackend(graphiti_config)
```

**3b. Replace `GraphitiBackend()` at `memory_sync.py:751`**

In `_leanrag_pipeline_executor_fn()`, the BULK path creates a bare Graphiti
backend. Same fix: use `load_graphiti_config(code_path=task.code_path)`.

**3c. Fix episode API usage at both sites**

Both callers at `tools/memory.py:798` and `memory_sync.py:752` use the wrong
API contract:

```python
# CURRENT (broken):
episodes_result = await graphiti.get_episodes(group_ids=[group_id], limit=1000)
episodes = episodes_result.get("episodes", [])

# PROBLEMS:
# 1. get_episodes() is sync, not async — cannot await it
# 2. Requires `query` (semantic search) — can't enumerate full corpus
# 3. Uses `limit` but API param is `max_episodes`
# 4. Returns list[dict], not dict with "episodes" key
```

These callers need a different primitive entirely (see Step 3.5).

- **Effort**: ~30 LOC
- **Risk**: Low — identical to existing Graphiti tool pattern
- **Dependencies**: Logically paired with Step 2

#### Step 3.5: Full-Corpus Episode Enumeration

**Files**: `src/watercooler_memory/backends/graphiti.py`

**Problem**: `get_episodes()` performs semantic search — it requires a query
string and cannot enumerate all episodes for a group (`graphiti.py:2366`).
LeanRAG clustering (UMAP) requires the **complete** episode corpus for a
project, not a semantic subset. Using semantic search as pipeline input
would produce incomplete, query-biased clusters.

**Solution**: Add `get_group_episodes()` to `GraphitiBackend` using a Cypher
query (the same `MATCH (e:Episodic) WHERE e.group_id = $group_id` pattern
already used in `find_episode_by_chunk_id` at `graphiti.py:2495`):

Define `EpisodeRecord` in `backends/__init__.py` alongside `PrepareResult`,
`IndexResult`, and `QueryResult` (not in the god module `graphiti.py`).

```python
@dataclass
class EpisodeRecord:
    """Lightweight episode reference for corpus enumeration.

    All non-key string fields default to "" because FalkorDB Cypher results
    may return None for optional node properties (e.g., source_description
    on older episodes). Downstream consumers (LeanRAG pipeline) receive empty
    strings instead of None, avoiding crashes in embedding generation.

    created_at is kept as str (not datetime) because _filter_by_time_range()
    operates on raw dicts BEFORE EpisodeRecord construction (see ordering
    note below). Parse-once-at-construction would require changing the filter
    contract.
    """
    uuid: str
    name: str = ""
    content: str = ""
    source_description: str = ""
    group_id: str = ""
    created_at: str = ""

def get_group_episodes(
    self,
    group_id: str,
    start_time: str = "",
    end_time: str = "",
) -> list[EpisodeRecord]:
    """Get ALL episodes for a group via Cypher (not semantic search).

    Unlike get_episodes() which performs semantic search and requires a
    query string, this method enumerates the complete episode set for a
    group_id. Used by LeanRAG pipeline which needs the full corpus for
    UMAP/clustering.

    This is NOT a MemoryBackend protocol method — it is only meaningful
    for Graphiti (FalkorDB-backed storage). LeanRAG does not store episodes
    directly; it consumes them from Graphiti.

    Args:
        group_id: Project group ID (e.g., "watercooler_cloud")
        start_time: ISO 8601 lower bound for created_at (inclusive)
        end_time: ISO 8601 upper bound for created_at (inclusive)
    """
```

This is a **sync** method (like `get_episodes()`, `search_facts()`, and
`search_nodes()`) that internally calls `asyncio.run()` on the FalkorDB
driver. Callers use `asyncio.to_thread()` from the MCP async context.

No pagination in Phase 1 — typical projects have <5K episodes, well within
FalkorDB's single-query capacity. Add cursor pagination in Phase 2 if needed.

Cypher query (no `ORDER BY` — UMAP clustering is order-invariant, and
episodes with null `created_at` would sort unpredictably under FalkorDB's
nulls-first semantics, corrupting any future cursor-based pagination):
```cypher
MATCH (e:Episodic)
WHERE e.group_id = $group_id
RETURN e.uuid as uuid, e.name as name, e.content as content,
       e.source_description as source_description,
       e.group_id as group_id, e.created_at as created_at
LIMIT $safety_limit
```

Safety limit constant:
```python
SAFETY_LIMIT = 10_000

# After executing the query:
if len(raw_results) >= SAFETY_LIMIT:
    logger.warning(
        "get_group_episodes: hit safety limit %d for group %s",
        SAFETY_LIMIT, group_id,
    )
```

**Date filtering**: Use Python post-filtering via the existing
`_filter_by_time_range()` helper, not Cypher-level `datetime()`. The entire
codebase uses this pattern (5 call sites in `graphiti.py`). FalkorDB's Cypher
`datetime()` support is unreliable for ISO 8601 strings with `Z` suffix, and
the existing helper handles timezone-naive datetimes, missing timestamps, and
edge cases gracefully.

**Critical ordering constraint**: `_filter_by_time_range()` at
`graphiti.py:525` is typed `list[dict[str, Any]] -> list[dict[str, Any]]`
and uses `r.get(time_key)`. It **cannot** accept `EpisodeRecord` instances
(no `.get()` method). The three-step pipeline must enforce
filter-before-construct:

```python
# 1. Raw dicts from FalkorDB Cypher result
raw_dicts = [dict(record) for record in cypher_results]
# 2. Filter as dicts (before dataclass construction)
filtered = _filter_by_time_range(raw_dicts, start_time, end_time)
# 3. Construct typed records with null-safe defaults
return [
    EpisodeRecord(**{k: v if v is not None else "" for k, v in r.items()})
    for r in filtered
]
```

**Replace callers**: Both `tools/memory.py:798` and `memory_sync.py:752`
switch from `get_episodes()` to `get_group_episodes()`:

```python
# FIXED (direct path):
episodes = await asyncio.to_thread(
    graphiti.get_group_episodes,
    group_id=group_id,
    start_time=start_date,
    end_time=end_date,
)
# Returns list[EpisodeRecord] directly — no .get("episodes") needed
```

This also absorbs the date filter wiring, since `get_group_episodes()`
accepts `start_time`/`end_time` natively.

- **Effort**: ~60 LOC (dataclass + new method + caller updates)
- **Risk**: Medium — new Cypher query, but pattern is proven in
  `find_episode_by_chunk_id`. No pagination needed for typical project sizes.
- **Dependencies**: Step 3 (uses properly configured GraphitiBackend)

> **Research insights (python-review, pattern-recognition, performance,
> graphrag-expert, FalkorDB-research)**:
>
> - **Naming**: Use `get_group_episodes()` not `enumerate_group_episodes()`.
>   The codebase uses `get_*` prefix exclusively for data retrieval on
>   `GraphitiBackend` (e.g., `get_episodes()`, `get_node()`, `get_edge()`).
>   `enumerate_*` does not appear anywhere in the backend layer.
>
> - **Typed return**: Return `list[EpisodeRecord]` (new dataclass), not
>   `list[dict[str, Any]]`. Pipeline-level methods use typed results
>   (`PrepareResult`, `IndexResult`, `QueryResult`, `HealthStatus`), and new
>   methods should follow that direction. Existing search methods still return
>   `list[dict]`, so this is a design preference, not a strict invariant.
>
> - **Not a protocol method**: `get_group_episodes()` belongs only on
>   `GraphitiBackend`, not on the `MemoryBackend` protocol at
>   `backends/__init__.py:214`. LeanRAG does not store episodes directly.
>   Document this in the docstring to prevent `AttributeError` when called
>   through the generic interface.
>
> - **Sync facade**: Must be a sync method with internal `asyncio.run()`, same
>   as `get_episodes()` (line 2356) and `search_facts()` (line 2051). Callers
>   wrap with `asyncio.to_thread()`. If accidentally made async, the
>   `asyncio.to_thread()` wrapper would silently fail.
>
> - **Date filtering via Python**: Confirmed: all 5 time-filter sites in
>   `graphiti.py` use Python's `_filter_by_time_range()`, not Cypher
>   `datetime()`. FalkorDB Cypher has limited datetime support. The Python
>   helper handles ISO 8601 `Z` suffix, timezone-naive datetimes, and missing
>   timestamps. No Cypher-level date filtering should be attempted.
>
> - **Performance at scale**: For <5K episodes, full corpus fetch is ~10-20MB
>   in memory. FalkorDB is Redis-based (in-memory) — the scan is trivially
>   fast. At 50K+ episodes, add SKIP/LIMIT pagination and consider not
>   returning `content` in the enumeration (fetch content lazily during
>   LeanRAG processing).
>
> - **GraphRAG confirmation (full-build only)**: Semantic search as UMAP input
>   is "almost always wrong" for index construction. UMAP needs the full corpus
>   for global topical structure. A semantic subset introduces distribution bias
>   and missing inter-cluster edges. Full-corpus enumeration is the correct
>   approach — this is how LeanRAG and RAPTOR are designed to operate.
>   **This applies only to BULK tasks** (full `index()` path). The incremental
>   path (`incremental_index()`, routed from SINGLE tasks) does NOT need the
>   full corpus — it takes only new `ChunkPayload` embeddings and assigns them
>   to existing clusters via cosine similarity in raw 1024-d embedding space.
>   Parametric UMAP's `transform()` on new samples is a correct, designed use
>   case (learned projection, not fresh fitting). The `get_group_episodes()`
>   method added here serves only the full-rebuild path.
>
> - **Cypher injection safety**: FalkorDB uses parameterized queries
>   (`$group_id` passed as bind parameter to `execute_query()`). All
>   group_id values also pass through `_sanitize_thread_id()` which strips
>   control characters, null bytes, enforces 64-char max, and ensures the
>   value starts with a letter. No injection risk.

#### Step 4: `code_path` Propagation

**Files**: `src/watercooler_mcp/memory_queue/task.py`, `src/watercooler_mcp/tools/memory.py`, `src/watercooler_mcp/memory_sync.py`

**4a. Add `code_path` to `MemoryTask` dataclass**

Add field: `code_path: str = ""` with docstring clarifying it is optional and
that `group_id` is the authoritative routing key. The persistence rule: for
LeanRAG BULK caller paths, `group_id` is derived from `code_path` when
absent (Step 5); `code_path` is stored only when the caller provides it,
never as the primary key. This
avoids leaking absolute host paths into persistent queue state.

`MemoryTask.from_dict()` already ignores unknown keys, so old serialized tasks
will deserialize safely with `code_path=""`.

**4b. Add `code_path` parameter to `_leanrag_run_pipeline_impl()`**

Current signature at `tools/memory.py:667`:
```python
async def _leanrag_run_pipeline_impl(
    ctx, group_id, start_date, end_date, dry_run, incremental
)
```

Add `code_path: str = ""` parameter. Make `group_id` optional with default
`group_id: str = ""` (Step 5 derives it from `code_path` when empty). When
enqueuing a BULK task, populate `task.code_path = code_path`. When running
directly, pass to `_get_leanrag_backend(code_path=code_path)` and
`load_graphiti_config(code_path=code_path)`.

**4c. Propagate `code_path` from MCP tool to impl**

The MCP tool `watercooler_leanrag_run_pipeline` (registered at
`tools/memory.py:1399`) currently does NOT have a `code_path` parameter —
`_leanrag_run_pipeline_impl` at line 667 accepts only `group_id`, `ctx`,
`start_date`, `end_date`, `dry_run`, `incremental`. **Add `code_path` as a
new parameter** to both the impl function and the MCP tool registration.
This is a tool interface change: the MCP schema will include the new
parameter, but it is optional (`code_path: str = ""`) so existing clients
are unaffected.

- **Effort**: ~50 LOC (includes `__post_init__` validation)
- **Risk**: Low — `from_dict()` is lenient, backward-compatible
- **Dependencies**: Step 2 (config convergence uses code_path)

**4d. Add `__post_init__` validation to MemoryTask**

Add minimal validation to catch dedup-key and routing bugs early. Keep
`__post_init__` as a **pure guard** — no auto-derive, no cross-package
imports. `task.py` is currently stdlib-only; importing
`watercooler.path_resolver.derive_group_id` would break that invariant and
create hidden coupling. Auto-derivation belongs in callers (Step 5).

```python
def __post_init__(self) -> None:
    if self.task_type == TaskType.BULK and not self.group_id:
        raise ValueError(
            "BULK tasks require non-empty group_id (provide group_id or "
            "call derive_group_id(code_path) before construction)"
        )
```

**Dead-letter safety**: `retry_dead_letters()` at `queue.py:250` and
`_load()` at `queue.py:310` catch only `(json.JSONDecodeError, KeyError)`.
The new `ValueError` from `__post_init__` would propagate uncaught during
deserialization via `from_json_line() → cls(**filtered) → __post_init__`,
blocking recovery of ALL subsequent tasks in the dead-letter file. Add
`ValueError` to both catch clauses:

```python
# queue.py:250 (retry_dead_letters)
except (json.JSONDecodeError, KeyError, ValueError):
    remaining_lines.append(line)

# queue.py:310 (_load)
except (json.JSONDecodeError, KeyError, ValueError) as e:
    logger.warning("MEMORY_QUEUE: skipping corrupt line: %s", e)
```

**Derive `group_id` fallback warning**: After auto-derivation in callers
(Step 5), check for the generic fallback:

```python
if group_id == "watercooler" and code_path:
    logger.warning(
        "group_id defaulted to 'watercooler' from code_path=%s; "
        "verify code_path is in a git repo",
        code_path,
    )
```

**Test impact**: The existing `test_bulk_task_missing_group_id_raises` at
`tests/unit/test_memory_queue_incremental.py:161-177` creates
`MemoryTask(task_type=BULK, group_id="")` and expects `RuntimeError` from
the executor. With `__post_init__`, this raises `ValueError` at
construction instead. Before implementation, run
`grep -n 'MemoryTask(' tests/` to enumerate ALL fixtures that create BULK
tasks with empty `group_id`, and update each to either: (a) provide a valid
`group_id`, or (b) change expected exception from `RuntimeError` to
`ValueError`.

**Annotation modernization**: While touching `task.py`, replace legacy
`List[str]`, `Dict[str, Any]`, `Optional[X]` with `list[str]`,
`dict[str, Any]`, `X | None`. The file already has
`from __future__ import annotations` at line 7 but still imports from
`typing` at line 15.

Keep MemoryTask as a dataclass (not Pydantic): it is a mutable work item
mutated many times per lifecycle (`mark_running`, `mark_failed`,
`mark_completed`). Pydantic models are for frozen config objects loaded once.
The `to_json_line()` hot path (called on every `_persist()`) would add
unnecessary overhead with Pydantic serialization.

> **Research insights (pydantic-hardener, federation-learning,
> MemoryTask-research, data-integrity, security)**:
>
> - **Keep as dataclass**: All 27 existing fields have defaults.
>   `from_dict()` ignores unknown keys. `to_dict()` uses
>   `dataclasses.asdict()`. Every other data object in `memory_queue/`
>   (`BulkCheckpoint`, `EntryProgress`, `MigrationCheckpoint`) uses
>   dataclasses. Switching just `MemoryTask` to Pydantic breaks consistency.
>
> - **Federation learning — Strategy 4 (empty dedup keys)**: Empty `group_id`
>   on BULK tasks would collapse all tasks into one dedup bucket — exactly
>   the federation Phase 1 empty-entry_id bug. The `__post_init__` guard
>   catches this at construction, not at execution.
>
> - **Federation learning — Strategy 6 (grep fixtures)**: After adding
>   `__post_init__`, grep test files for `MemoryTask(` and ensure all test
>   fixtures satisfy the new invariant (BULK tasks need non-empty group_id).
>
> - **Schema evolution is safe**: `from_dict()` (lines 130-134) filters to
>   known fields. Old tasks missing `code_path` deserialize with
>   `code_path=""`. Dead-letter queue uses the same `from_json_line()` path.
>
> - **code_path exposure**: Queue persists tasks to
>   `~/.watercooler/memory_queue/queue.jsonl`. `code_path` contains absolute
>   filesystem paths. `source_description` already leaks paths (line 1300 of
>   `tools/memory.py`). This is an existing concern, not new to this plan.
>   Mitigate by keeping `code_path` optional and preferring `group_id` as the
>   routing key in logs and error messages.

#### Step 5: Pipeline group_id Auto-Derivation (Simplified)

**Files**: `src/watercooler_mcp/tools/memory.py`

The original plan had hyphen detection, canonical comparison warnings, and
auto-derivation as separate validation steps. Per simplicity review, only the
auto-derivation is needed now — the other validations are deferred to Phase 2
when there is evidence of misuse.

**Auto-derive lives here, not in `__post_init__`** (Step 4d keeps task.py
stdlib-only). In `_leanrag_run_pipeline_impl()`, derive `group_id` before
creating the task:

```python
# Require code_path for all LeanRAG BULK runs (direct + queued).
# group_id is optional — derived from code_path if absent.
if not code_path:
    return mem.create_error_response(
        "code_path required",
        "LeanRAG BULK pipeline requires code_path to derive project-scoped group_id",
        "leanrag_run_pipeline",
    )
if not group_id:
    group_id = derive_group_id(code_path=code_path)
```

- **Effort**: ~10 LOC (auto-derive + fallback warning)
- **Risk**: None
- **Dependencies**: Step 4 (code_path field on MemoryTask)

> **Research insight (code-simplicity)**: Step 5's hyphen detection (5.1) and
> canonical comparison warning (5.2) were deferred: no evidence of misuse,
> no consumer of the warnings, and the hard-fail on missing code_path (Step 2c)
> already prevents the most dangerous case. Saves ~20 LOC. Add hyphen
> detection in Phase 2 if someone actually passes a thread topic as group_id.

#### Step 6: EntryEpisodeIndex MCP Exposure

**Files**: `src/watercooler_mcp/tools/memory.py`, `src/watercooler_mcp/tools/__init__.py`, `src/watercooler_mcp/server.py`

Register a new MCP tool: `watercooler_get_entry_provenance`

```python
async def _get_entry_provenance_impl(
    ctx: Context,
    entry_id: str = "",
    episode_uuid: str = "",
    code_path: str = "",
) -> ToolResult:
```

**Input validation**: Strip whitespace from both inputs before lookup
(copy-paste often includes trailing whitespace). Enforce mutual exclusion:
if both `entry_id` and `episode_uuid` are provided, return an error
explaining that only one should be supplied.

**Bidirectional lookup**: Accept either `entry_id` or `episode_uuid` (not
both). The `EntryEpisodeIndex` supports O(1) lookups in both directions.
Agents doing graph exploration often have an episode UUID (from T2 search
results) and need to resolve back to the entry. Agents exploring T1 entries
may want to find associated episodes. Supporting both directions makes the
tool fully composable with `watercooler_search(mode="episodes")`.

**Chunk awareness**: `EntryEpisodeIndex` supports `ChunkEpisodeMapping` — one
entry may map to multiple episodes (when long entries are chunked during sync).
The response must reflect this:

Behavior:
- Resolve the index file path using `code_path` (from the tool parameter):
  1. Try `load_graphiti_config(code_path=code_path)` → use
     `config.entry_episode_index_path` if it succeeds.
  2. If `load_graphiti_config()` returns `None` (Graphiti disabled or
     missing LLM keys), fall back to `IndexConfig(backend="graphiti")`
     default path (`~/.watercooler/graphiti/entry_episode_index.json`).
     **Limitation**: this fallback uses the default path only — users with
     a custom `entry_episode_index_path` in TOML config AND disabled
     Graphiti will get false misses. Log a warning in this case:
     `"Graphiti config unavailable; using default index path"`.
  3. Do NOT initialize `GraphitiBackend` — its `__init__` validates LLM
     API keys (`graphiti.py:706`), which would make this read-only index
     lookup fail when Graphiti runtime config is incomplete. The index is
     a standalone JSON file that only needs a file path to load.
- If `episode_uuid` provided: Call `index.get_entry(episode_uuid)` then
  `index.get_index_entry(entry_id)` for full metadata
- If `entry_id` provided: First try `index.get_chunks_for_entry(entry_id)`.
  If chunks exist, return chunk-aware episode list. Else fallback to
  `index.get_episode(entry_id)` + `index.get_index_entry(entry_id)` for
  non-chunked 1:1 mapping.
  **Coexisting mappings**: If an entry has both a direct (1:1) mapping and
  chunk (1:N) mappings (e.g., after re-indexing from non-chunked to chunked),
  prefer chunks and include a `"stale_direct_mapping": true` flag in the
  response so agents are aware of the inconsistency.
- On hit (episode→entry): return:
  ```json
  {"provenance_available": true, "entry_id": "...",
   "thread_id": "...", "episode_uuid": "...", "indexed_at": "..."}
  ```
- On hit (entry→episodes, may be chunked): return:
  ```json
  {"provenance_available": true, "entry_id": "...",
   "thread_id": "...", "episodes": [
     {"episode_uuid": "...", "chunk_index": 0, "total_chunks": 3},
     {"episode_uuid": "...", "chunk_index": 1, "total_chunks": 3}
   ]}
  ```
- On miss: return:
  ```json
  {"provenance_available": false, "lookup_key": "...",
   "message": "No mapping found for this identifier",
   "action_hints": [
     "Run watercooler_bulk_index to populate the index",
     "Check watercooler_diagnose_memory for backend status"
   ]}
  ```

Follow MCP tool registration pattern:
1. Add `_get_entry_provenance_impl` function
2. Add sentinel variable and `register_provenance_tools(mcp)` function
3. Register in `tools/__init__.py` → `register_all_tools()`
4. Re-export in `server.py`

**Response typing**: Define `ProvenanceHit(TypedDict)` and
`ProvenanceMiss(TypedDict)` for the two response shapes. The plan introduces
`EpisodeRecord` for type safety in Step 3.5; the same discipline should
apply to provenance responses. Place in the same module as the tool impl.

- **Effort**: ~80 LOC (increased for bidirectional + chunk support +
  TypedDicts + input validation + action_hints)
- **Risk**: Low — read-only tool, no backend init needed, existing index
  infrastructure. Remains available even when LLM keys are missing.
- **Dependencies**: None (EntryEpisodeIndex already works)

> **Research insights (agent-native-architecture, EntryEpisodeIndex-research,
> code-simplicity)**:
>
> - **Bidirectional lookup is essential**: T2 search returns episode UUIDs.
>   The semantic bridge pattern (T3→T2→T1) needs episode→entry resolution.
>   But agents browsing T1 entries may also want to find linked episodes.
>   Without both directions, agents need separate tools or manual index work.
>
> - **Chunk awareness**: `EntryEpisodeIndex` stores both `IndexEntry` (1:1
>   entry↔episode) and `ChunkEpisodeMapping` (1:N entry→episodes with
>   `chunk_index` and `total_chunks`). The flat response format originally
>   proposed would silently drop chunk information.
>
> - **`get_stats()` method exists** (line 511 of `entry_episode_index.py`):
>   Returns `{entry_count, thread_count, chunk_count}`. Consider including
>   index stats in the miss response to give agents awareness of index
>   completeness (deferred — can be added as enrichment later).
>
> - **Thread safety**: `EntryEpisodeIndex` uses `threading.RLock()` and atomic
>   file writes (write-to-temp-then-rename). Concurrent reads during writes
>   are safe. Loading on every MCP call is acceptable — the JSON file is
>   typically <1MB even with thousands of entries.
>
> - **IndexConfig independence**: `IndexConfig(backend="graphiti")` computes
>   the default path `~/.watercooler/graphiti/entry_episode_index.json`
>   automatically. No GraphitiBackend or runtime config needed. Fresh system
>   with no index file: `load()` returns an empty index (not an error).
>
> - **Naming is correct**: `watercooler_get_entry_provenance` follows the
>   `watercooler_{verb}_{noun}` convention, matching `watercooler_get_entity_edge`.
>
> - **Keep thread_id and indexed_at in response** (code-simplicity): These
>   fields are essentially free (already in the index entry). `thread_id`
>   lets agents immediately navigate to the source thread without additional
>   lookups.

#### Step 7: E2E Smoke Test

**Files**: `tests/test_leanrag_plumbing.py` (new)

Verify the full happy path with mocked backends:

1. **Config convergence test**: `_get_leanrag_backend(code_path="/repo")` calls
   `load_leanrag_config(code_path="/repo")` and returns a backend with correct
   `work_dir` — assert `work_dir.name` starts with `leanrag_`

2. **Index-query graph name match**: Mock `load_leanrag_config()` returning a
   config, verify that both `_leanrag_run_pipeline_impl` and
   `_search_leanrag_impl` use the same `work_dir` (and thus the same FalkorDB
   graph name)

3. **group_id derivation and BULK guard**:
   - `_leanrag_run_pipeline_impl(code_path="/repo")` derives `group_id` before
     constructing `MemoryTask` (Step 5 caller, not `__post_init__`)
   - `MemoryTask(task_type=BULK, group_id="", code_path="")` raises `ValueError`
   - `MemoryTask(task_type=BULK, group_id="derived", code_path="/repo")` succeeds
   - `_leanrag_run_pipeline_impl(code_path="")` returns error requiring code_path

4. **Episode enumeration**: Mock `get_group_episodes()`, call pipeline,
   verify it is called (not `get_episodes()`). Verify `start_time`/`end_time`
   are forwarded when `start_date`/`end_date` are provided. Verify return
   type is `list[EpisodeRecord]`.

5. **Hard-fail on missing code_path**: Enqueue a BULK task with empty
   `code_path`, verify the executor raises with a clear error message
   (not a silent fallback to generic database)

6. **EntryEpisodeIndex MCP tool**: Call `_get_entry_provenance_impl` with known
   UUID → hit. Call with unknown UUID → `provenance_available: false`.
   Test bidirectional lookup (episode→entry and entry→episodes).
   Test chunked entry response (one entry with multiple episodes).
   Verify it works without LLM API keys set.

7. **MemoryTask code_path round-trip**: Create task with code_path, serialize
   to dict, deserialize, verify code_path preserved. Also verify old tasks
   (without code_path) deserialize with `code_path=""`.

8. **Dead-letter safety**: Serialize a BULK task with empty group_id to a
   JSONL line, feed it to `retry_dead_letters()` and `_load()`, verify
   the `ValueError` is caught (not propagated) and subsequent tasks still
   load.

9. **Safety LIMIT**: Mock `get_group_episodes()` to return exactly
   `SAFETY_LIMIT` records, verify warning is emitted. Verify the Cypher
   query includes `LIMIT $safety_limit`.

10. **Filter-then-construct ordering**: Verify `_filter_by_time_range()` is
    called on raw dicts, not on `EpisodeRecord` instances (mock and assert
    call args type).

11. **Provenance input validation**: Verify error when both `entry_id` and
    `episode_uuid` are provided. Verify whitespace-padded keys are stripped.

Test infrastructure:
- Use existing `stub_memory_api_keys` fixture for API key stubs
- Create a `leanrag_test_env` fixture (enable LeanRAG, set stub leanrag_path)
- Mock `LeanRAGBackend.index()` and `GraphitiBackend.get_group_episodes()`
  to avoid real FalkorDB/LLM dependencies
- Follow existing patterns in `tests/test_memory_e2e.py`

- **Effort**: ~130 LOC
- **Risk**: None — tests only
- **Dependencies**: Steps 1-6 (tests verify their correctness)

#### Step 8: Semantic Bridge Documentation

**Files**: `docs/SEMANTIC_BRIDGE.md` (new)

Place the document in `docs/` (operational reference alongside `baseline-graph.md`,
`GRAPH_SYNC.md`, `CONFIGURATION.md`), **not** in `docs/watercooler-planning/`
(which is an internal planning archive for ADRs, phase plans, and roadmaps).

Document the T3 → T2 → T1 reverse provenance pattern:

1. **Prerequisites**: When is reverse provenance available? Requires both T2
   and T3 indexing to have completed for the relevant group. The
   `EntryEpisodeIndex` is only populated after `watercooler_bulk_index` +
   Graphiti sync has run.
2. **Pattern overview**: How an agent traces a T3 concept back to source T1
   entries
3. **Step-by-step flow** with **Mermaid diagram**:
   ```
   flowchart LR
     Q[Agent query] --> T3[T3: LeanRAG search_nodes]
     T3 --> LEAF[leaf descriptions]
     LEAF --> T2[T2: watercooler_search\nmode=episodes]
     T2 --> EPUUID[Graphiti episode UUID]
     EPUUID --> PROV[watercooler_get_entry_provenance\nepisode_uuid → entry_id + thread_id]
     PROV --> T1[T1: watercooler_get_thread_entry\ntopic + entry_id]
   ```
   - Query T3 (LeanRAG) → get cluster + leaf entity descriptions
   - Use leaf descriptions as T2 (Graphiti) query keys → get episodes with UUIDs
   - Call `watercooler_get_entry_provenance(episode_uuid=...)` → get entry_id +
     thread_id
   - Read entry via `watercooler_get_thread_entry(topic=<thread_id>,
     entry_id=<entry_id>)` (topic comes from provenance response `thread_id`)
4. **Why semantic bridging** (not structural links): self-maintaining, no
   cross-tier index, degrades gracefully
5. **Limitations**: Depends on leaf description quality (addressed in Phase 2),
   EntryEpisodeIndex coverage (episodes not indexed before the index was
   introduced return `provenance_available: false`)
6. **When provenance is unavailable**: If `entry_episode_index.json` has no
   mapping, surface the raw entity name and cluster summary rather than failing
7. **T3 field mapping for bridge queries**: Document which fields from the
   `_search_leanrag_impl` response (currently `query`, `answer`, `context`,
   `topk`) agents should extract as T2 query keys. Show a worked example
   with real field values. Note that the `context` field and individual
   `topk` entries are the most useful bridge inputs, but this is not
   guaranteed — Phase 2 should add an explicit `leaf_entities` array.
8. **Examples**: Concrete MCP tool invocation sequences using real tool names
   and parameters (copy-pasteable, not pseudocode)

**Also update**:
- `docs/mcp-server.md` — add `watercooler_get_entry_provenance` tool entry
- `docs/watercooler-planning/MEMORY_INTEGRATION_ROADMAP.md` — mark Phase 1
  plumbing as complete
- `src/watercooler_mcp/resources.py` — update `watercooler://instructions`
  resource (lines 87-131) with:
  1. `watercooler_get_entry_provenance` under Memory & Search Tools
  2. `backend="leanrag"` option for `watercooler_search`
  3. Condensed semantic bridge pattern:
     ```
     ## Reverse Provenance (T3 -> T1)
     When a T3 (LeanRAG) result needs source verification:
     1. watercooler_search(query=<leaf description>, mode="episodes") → episode UUID
     2. watercooler_get_entry_provenance(episode_uuid=<uuid>) → entry_id + thread_id
     3. watercooler_get_thread_entry(topic=<thread_id>, entry_id=<entry_id>) → source
     ```

- **Effort**: ~120 lines of documentation (increased for Mermaid, prerequisites,
  unavailability section)
- **Risk**: None
- **Dependencies**: Step 6 (documents the new MCP tool)

> **Research insights (technical-docs, graphrag-expert)**:
>
> - **Mermaid diagram is essential**: The chain between three identifier types
>   (chunk hash → episode UUID → ULID entry_id) is hard to follow in prose.
>   The diagram makes two external dependencies explicit (EntryEpisodeIndex
>   file and a live Graphiti query).
>
> - **Real tool names in examples**: The existing `docs/mcp-server.md` uses
>   real tool call syntax throughout. Examples should show actual
>   `watercooler_smart_query(query=..., code_path=...)` invocations that
>   agents can copy-paste.
>
> - **Semantic bridge precision**: With named entities in leaf descriptions →
>   precision 0.65-0.80, recall 0.55-0.75. With abstract summaries → 0.30-0.50.
>   Phase 2 enrichment (Graphiti-seeded descriptions) is essential, not
>   optional. Document this expectation clearly so users understand the
>   current limitations.
>
> - **Config awareness**: `~/.watercooler/config.toml` has
>   `[memory.tiers]` with `t3_enabled = true` and `[memory.leanrag]` with
>   `path = "~/projects/watercooler-cloud/external/LeanRAG"`. The semantic
>   bridge only works when T3 is enabled and configured. Include a config
>   check in the prerequisites section.

### Implementation Ordering

```
Step 1 (incremental guard)     ─┐
Step 2 (config convergence)    ─┤─── can run in parallel
Step 3 (Graphiti factory)      ─┘
         │
         ▼
Step 3.5 (get_group_episodes)     ── depends on 3 (date post-filtering)
Step 4 (code_path + MemoryTask)   ── depends on 2, 3 (includes __post_init__)
Step 5 (group_id auto-derive)     ── caller-side derive (4d is guard-only)
Step 6 (EntryEpisodeIndex MCP)    ── independent, can parallel with 3.5-5
         │
         ▼
Step 7 (E2E smoke test)        ── depends on 1-6
Step 8 (semantic bridge docs)  ── depends on 6

Total estimated: ~490 LOC (code) + ~140 LOC (docs) + ~180 LOC (tests)
```

## Alternative Approaches Considered

1. **Structural cross-tier entity mapping** — Build an explicit T3↔T2 entity
   UUID index. Rejected: high maintenance burden, breaks on T3 rebuild,
   couples data models. Semantic bridging is simpler and self-maintaining.

2. **Inline provenance in T3 community summaries** — Embed source references
   directly in LeanRAG community reports. Rejected: community summaries are
   synthetic and rebuilt; provenance lives at the leaf level.

3. **Auto-resolve T3→T1 in a single MCP call** — Build a compound tool that
   chains T3 query → T2 query → T1 lookup. Rejected: agents should decide
   when provenance matters. The semantic bridge is a pattern, not a pipeline.

## Acceptance Criteria

### Functional Requirements

- [x] All LeanRAG operations (index, query, search) use `load_leanrag_config()`
- [x] All Graphiti backends in T3 paths use `load_graphiti_config()`
- [x] Index and query use the same FalkorDB graph name (verified by test)
- [x] Pipeline uses `get_group_episodes()` (Cypher), not `get_episodes()`
      (semantic search), for full-corpus episode collection
- [x] `get_group_episodes()` returns typed `list[EpisodeRecord]`, not raw dicts
- [x] `code_path` propagates from MCP tool → `_leanrag_run_pipeline_impl` →
      `MemoryTask` → queued executor → `load_leanrag_config(code_path=...)`
- [x] Queued executor hard-fails with clear error when `code_path` is missing,
      rather than falling back to generic `"watercooler"` database
- [x] `MemoryTask` persists `group_id` as authoritative key; `code_path` is
      optional and never the primary routing key
- [x] Old `MemoryTask` serializations (without `code_path`) deserialize safely
- [x] Empty `group_id` auto-derives from `code_path` in **callers** (Step 5),
      NOT in `__post_init__` (which is guard-only, no cross-package imports)
- [x] BULK tasks with empty `group_id` AND empty `code_path` fail at construction
- [x] BULK tasks with empty `group_id` but valid `code_path` succeed when caller
      derives `group_id` before construction (e.g., `_leanrag_run_pipeline_impl`)
- [x] `_leanrag_run_pipeline_impl` requires non-empty `code_path` for all BULK
      runs (direct + queued), not just executor
- [x] `start_date`/`end_date` filter episodes via Python post-filtering in both paths
- [x] `watercooler_get_entry_provenance(episode_uuid)` returns entry_id on hit
- [x] `watercooler_get_entry_provenance(entry_id)` returns episode list on hit
      (bidirectional lookup)
- [x] Chunked entries return multiple episodes with chunk_index metadata
- [x] `watercooler_get_entry_provenance` returns
      `{provenance_available: false}` on miss (not an error)
- [x] `watercooler_get_entry_provenance` works without LLM API keys (loads
      index file directly, no GraphitiBackend init)
- [x] `from leanrag.pipelines.incremental import ...` is guarded with try/except
- [x] Incremental import failure falls back to full index with warning
- [x] `get_group_episodes()` is NOT on the `MemoryBackend` protocol (Graphiti-only)
- [x] `get_group_episodes()` includes `LIMIT $safety_limit` with warning log when hit
- [x] `_filter_by_time_range()` operates on raw dicts BEFORE `EpisodeRecord` construction
- [x] `EpisodeRecord` fields have empty-string defaults for null FalkorDB properties
- [x] `retry_dead_letters()` and `_load()` catch `ValueError` from `__post_init__`
- [x] `__post_init__` does NOT import from `watercooler.path_resolver` (task.py stays stdlib-only)
- [x] `resources.py` instructions resource lists `watercooler_get_entry_provenance`
- [x] `resources.py` instructions resource documents `backend="leanrag"` for `watercooler_search`
- [x] `resources.py` instructions resource includes condensed semantic bridge pattern
- [x] Provenance tool validates mutual exclusion of `entry_id` / `episode_uuid`
- [x] Provenance miss response includes `action_hints` array
- [x] `docs/mcp-server.md` updated with `watercooler_get_entry_provenance` tool entry

### Non-Functional Requirements

- [x] No new dependencies added
- [x] No changes to T1 or T2 behavior (note: `ValueError` catch in queue
      `_load()`/`retry_dead_letters()` is a shared robustness improvement,
      not T3-specific — acceptable because it only affects deserialization
      error handling, not task routing or execution)
- [x] Backward-compatible MemoryTask serialization
- [x] All changes follow existing code patterns (no new abstractions)

### Quality Gates

- [x] All existing tests pass (no regressions)
- [x] New tests in `tests/test_leanrag_plumbing.py` cover Steps 1-6
- [x] `ruff check` and `mypy` pass on changed files
- [x] Semantic bridge documentation reviewed for clarity

## Success Metrics

- LeanRAG pipeline produces non-empty results when queried after indexing
  (currently returns 0 due to graph name mismatch)
- Date-filtered pipeline runs produce smaller episode sets than unfiltered
- `watercooler_get_entry_provenance` resolves episode UUIDs to entry_ids for
  all episodes indexed after EntryEpisodeIndex was introduced

## Dependencies & Prerequisites

- LeanRAG submodule at `external/LeanRAG` (already present)
- FalkorDB running (for manual E2E verification; mocked in unit tests)
- No external service changes required

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `get_group_episodes()` Cypher query returns large result set | Low | Low | Typical projects have <5K episodes (~10-20MB in memory); same FalkorDB driver used in `find_episode_by_chunk_id`. Add SKIP/LIMIT pagination in Phase 2 if needed. |
| Config convergence changes `work_dir` path — old index data unreachable | Known | Low | Pipeline does full rebuild; executor path previously wrote to temp dirs (data was already unreachable). No migration needed. Note in commit message. |
| MemoryTask schema change breaks existing queued tasks | Low | Medium | `from_dict()` ignores unknown keys; missing `code_path` defaults to `""` |
| `derive_group_id()` returns unexpected values for edge-case paths | Low | Medium | Existing unit tests for `derive_group_id()`; hyphen-based detection (not strict regex) avoids false positives |
| Incremental LeanRAG module unavailable in some environments | Known | Low | Try/except guard with fallback to full rebuild |
| Hard-fail on missing code_path breaks existing queue consumers | Low | High | Only LeanRAG BULK tasks affected; Graphiti tasks unaffected. Clear error message guides user to fix. Old tasks without code_path can be retried after fix. |

## Future Considerations (Phase 2 & 3)

- **Phase 2: Graphiti-Seeded LeanRAG** — Entity exporter, Episodic Bridge
  template for enriched leaf descriptions, validate semantic bridge effectiveness
- **Phase 3: JTB Certification Gate** — Qualified distillation, confidence
  metadata on T3 leaves, certification rubric adapted from decision trace format
- **Reference data pipeline** — PDF → chunk → T2 ingestion path (Phase 1
  design must not preclude non-thread episode sources in `code_path`/`group_id`
  handling)

## Files Changed (Summary)

| File | Changes |
|------|---------|
| `src/watercooler_memory/backends/leanrag.py` | Incremental import guard |
| `src/watercooler_memory/backends/graphiti.py` | New `get_group_episodes()` method (imports `EpisodeRecord` from `backends/__init__`) |
| `src/watercooler_mcp/tools/memory.py` | Config convergence, code_path, Graphiti factory, episode API fix, provenance tool (bidirectional + chunk-aware) |
| `src/watercooler_mcp/tools/graph.py` | Config convergence for search (pass `code_path` explicitly, not via kwargs) |
| `src/watercooler_mcp/memory_sync.py` | Config convergence, Graphiti factory, episode API fix, code_path in executor (hard-fail) |
| `src/watercooler_mcp/memory_queue/task.py` | Add `code_path` field + `__post_init__` validation to `MemoryTask` |
| `src/watercooler_mcp/tools/__init__.py` | Register provenance tools |
| `src/watercooler_mcp/server.py` | Re-export provenance tool |
| `src/watercooler_mcp/resources.py` | Update instructions resource (provenance tool, semantic bridge, backend="leanrag") |
| `src/watercooler_memory/backends/__init__.py` | New `EpisodeRecord` dataclass (alongside `PrepareResult`, `IndexResult`, `QueryResult`) |
| `tests/test_leanrag_plumbing.py` | New E2E smoke tests |
| `docs/SEMANTIC_BRIDGE.md` | New operational reference documentation |
| `docs/mcp-server.md` | Add `watercooler_get_entry_provenance` tool entry |
| `docs/watercooler-planning/MEMORY_INTEGRATION_ROADMAP.md` | Mark Phase 1 plumbing as complete |

## References & Research

### Internal References

- Brainstorm: `docs/brainstorms/2026-02-21-t3-epistemological-integration-brainstorm.md`
- Config factories: `src/watercooler_mcp/memory.py:185` (`load_leanrag_config`)
- Config factories: `src/watercooler_mcp/memory.py:51` (`load_graphiti_config`)
- Path resolver: `src/watercooler/path_resolver.py:306` (`derive_group_id`)
- EntryEpisodeIndex: `src/watercooler_memory/entry_episode_index.py:276` (`get_entry`)
- MCP tool pattern: `src/watercooler_mcp/tools/federation.py` (recent, clean example)
- ADR 0001: `docs/watercooler-planning/adr-0001-memory-backend-contract.md`
- Phase 4 design: `docs/watercooler-planning/MEMORY_CONSOLIDATION_PHASE4.md`

### Related Work

- Thread: `leanrag-tier3-integration-audit` (entries 0-17)
- Memory consolidation roadmap: `docs/watercooler-planning/MEMORY_INTEGRATION_ROADMAP.md`
