---
date: 2026-02-19
topic: federation-phase1
thread: federation-phase1
---

# Federation Phase 1: Implementation Readiness

## What We're Building

A single new MCP tool (`watercooler_federated_search`) that performs passive, read-only
cross-namespace keyword search across configured watercooler repositories. Results are
normalized, weighted by namespace proximity, and returned with full provenance metadata.

This is the smallest useful slice of the [Federated Watercooler Architecture](../watercooler-planning/FEDERATED_WATERCOOLER_ARCHITECTURE.md)
(v4.5a) — it validates the federation concept before adding reference semantics (Phase 2)
or decision projections (Phase 3).

## Why This Approach

The intra-repo memory stack is stable (T1 baseline, T2 Graphiti, T3 LeanRAG). Cross-repo
references already exist informally in threads. Federation formalizes this pattern. One tool
is the right Phase 1 scope — it validates scoring, access control, and namespace resolution
without the complexity of thread listing, thread fetching, or reference parsing.

## Key Decisions

Sourced from the `federation-phase1` thread (entries 0–3) and brainstorming session:

### Scoring & Normalization

- **Fixed-anchor normalization**: `clamp((score - 1.0) / 1.2, 0.0, 1.0)` — calibrated to
  actual keyword score range [1.0, 2.2] from `baseline_graph/search.py:620-629`
- **Multiplicative composition**: `RankingScore = normalized x NW x RecencyDecay`
- **3 active NW tiers in Phase 1**: local=1.0, lens=0.7, wide=0.55. The referenced tier
  (0.85) is defined in config but dormant until Phase 2 adds `Ref:` parsing
- **RecencyDecay**: exponential with floor=0.7, half_life=60 days (configurable)
- **Candidate allocation cap**: `max(limit // 2, 1)` results per non-primary namespace

### Config & Isolation

- **Config models in `config_schema.py`**: follows existing pattern (MemoryConfig, McpConfig, etc.)
- **Frozen Pydantic models**: `ConfigDict(frozen=True)` on all federation config classes —
  safe to share cached singleton, no defensive copies needed
- **Feature-gated**: `federation.enabled = true` required in TOML. Tool registered but
  returns clear error if disabled or no namespaces configured

### Namespace Resolution

- **Read-only discovery**: new `_discover_existing_worktree()` — pure filesystem check,
  no git operations. Uses same path logic as `_worktree_path_for()` (`code_root.name`)
- **Missing worktree = error**: "Namespace X not initialized" — user must run a watercooler
  tool against the secondary repo first to bootstrap its worktree
- **Primary namespace**: uses existing `_require_context(code_path)` — unchanged

### Async & Timeout

- **Async tool handler**: consistent with `tools/memory.py` pattern
- **Per-namespace parallelism**: `asyncio.gather()` with `asyncio.wait_for()` per namespace
- **Each search offloaded via `asyncio.to_thread(search_graph, ...)`**
- **Default timeout**: 0.4s per namespace (configurable via `scoring.namespace_timeout`)
- **Fail-open**: primary failure = hard error, secondary failure = partial results with
  `namespace_status: "timeout"` or `"error"` in response envelope

### Error Handling & Dedup

- **Dedup-by-entry-id**: safety net only — ULIDs are globally unique, so this is a no-op
  in practice. Semantic dedup via CombMNZ deferred to Phase 2
- **Access control**: per-primary allowlists + topic-level deny lists from TOML config
- **Response envelope**: includes `origin_namespace`, `namespace_status`, `federation_search_mode`,
  `ranking_score` breakdown per result

### Branch Filtering

- **`code_branch` parameter**: surfaced on the tool, passed through to `SearchQuery`.
  Branch filtering already implemented downstream in `_load_entries()` / `read_thread_from_graph()`

## File Structure

### New files (6 implementation + 6 tests)

```
src/watercooler_mcp/federation/
  __init__.py                          # Package init
  resolver.py                          # Read-only worktree discovery
  access.py                            # Allowlist + deny_topics enforcement
  scoring.py                           # Fixed-anchor norm, NW tiers, RecencyDecay
  merger.py                            # Response envelope, dedup, allocation cap

src/watercooler_mcp/tools/
  federation.py                        # MCP tool registration + async impl

tests/unit/
  test_federation_scoring.py           # normalize, NW resolve, recency decay
  test_federation_access.py            # allowlist filtering, deny_topics
  test_federation_resolver.py          # worktree discovery (mocked filesystem)
  test_federation_merger.py            # envelope build, dedup, allocation cap

tests/integration/
  test_federation_tool.py              # Full MCP tool flow with mocked search_graph

tests/e2e/
  test_federation_e2e.py               # Real JSONL fixture repos

tests/fixtures/federation/
  site-namespace/
    .graph/
      nodes.jsonl                      # ~20 entries across 3 threads
      edges.jsonl
    thread-a.md
    thread-b.md
```

### Modified files (2)

- `src/watercooler/config_schema.py` — add frozen `FederationConfig` models + field
  on `WatercoolerConfig`
- `src/watercooler_mcp/tools/__init__.py` — import + register `register_federation_tools(mcp)`

## Test Strategy

- **Unit tests** (4 files): mock `search_graph()`, filesystem, and config. Test scoring math,
  access control logic, resolver path derivation, and merger behavior in isolation
- **Integration test** (1 file): wire up the MCP tool handler with mocked `search_graph()`.
  Verify end-to-end parameter passing, timeout behavior, and response envelope structure
- **E2E test** (1 file): pre-built JSONL fixture data for a fake secondary namespace. Tests
  real `search_graph()` against fixture data. Portable, no git operations needed in CI
- **Specific test**: `test_partial_failure_ranking_stability()` — verify removing namespace C
  does not reorder A/B results (multiplicative scoring guarantees this)

## Validation Plan

- 20+ real queries against watercooler-cloud + watercooler-site threads
- Latency target: <800ms for 2 namespaces
- Empirical NW tuning if precision@k suggests adjustment
- Log raw score distributions per namespace for anchor drift detection

## Open Questions

None — all design decisions are locked. Ready for `/workflows:plan`.

## Next Steps

-> `/workflows:plan` for implementation details and task ordering
