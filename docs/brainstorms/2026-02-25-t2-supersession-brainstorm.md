# T2 Supersession: Testing and Explicit Link Enrichment

**Date**: 2026-02-25
**Status**: Design complete, ready for planning

---

## What We're Building

Two related capabilities:

1. **Basic T2 plumbing** (already implemented as pre-work): expose `invalid_at`/`valid_at` in MCP search results and add an `active_only` filter so agents can ask for "just the currently-true facts."

2. **Supersession enrichment daemon**: a new `BaseDaemon` subclass that periodically scans FalkorDB for edges with `invalid_at` set and writes a `superseded_by` property back to each one, identifying *which* new fact replaced the old one.

### Why This Matters

Graphiti's bi-temporal edge invalidation (`invalid_at`) tells you *when* a fact became stale, but not *what superseded it*. For decision-trace diffing — "what was believed about X on date D, and what changed it?" — you need the explicit link. Without it, you can see two facts side-by-side but can't connect them causally.

The graph is the team's shared memory. The link must live there so all team members (and agents) see the same enriched view.

---

## Context: Graphiti's Supersession Mechanism

Graphiti automatically sets `invalid_at` on edges when a new episode contradicts them. The call chain is:

```
add_episode()
  → _extract_and_resolve_edges()
  → resolve_extracted_edge()
  → resolve_edge_contradictions()
      → edge.invalid_at = resolved_edge.valid_at   # ← key line
```

What Graphiti does **not** store: which new edge caused the invalidation. Only `invalid_at` is set on the old edge. The `superseded_by` link must be inferred externally.

---

## Design Decisions

### Basic plumbing (Phase 1 — already implemented)

| Decision | Choice |
|----------|--------|
| Expose `invalid_at`/`valid_at` in MCP | Yes — added to `_search_graphiti_impl()` result dict |
| Active-only filter | `active_only: bool = False` on `search_memory_facts()` + `search_facts()`, threaded through to MCP tool |
| Filter implementation | Python post-filter via `_filter_active_only()`, consistent with `_filter_by_time_range()` pattern |
| Over-fetch when filtering | `limit * 3`, capped at `MAX_SEARCH_RESULTS` |

### Supersession enrichment daemon (Phase 2 — to be built)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Link storage | FalkorDB graph (shared, canonical) | Team-shared memory; all agents read the same enriched graph |
| Write mechanism | Direct FalkorDB Cypher (bypass Graphiti abstraction) | Graphiti doesn't expose edge property annotation; direct Cypher is clean and non-invasive |
| What gets written | `superseded_by` property on the invalidated edge | `EntityEdge` nodes already accept arbitrary properties in FalkorDB |
| Inference algorithm | Temporal proximity | For each invalidated edge E, find edge F involving the same entity pair where `F.valid_at` is closest to `E.invalid_at`; F is the inferred successor |
| Trigger model | Periodic scan (`tick_on_interval=True`, interval=300s) | Fits existing `BaseDaemon` pattern; no coupling to ingest path |
| Daemon base class | `BaseDaemon` subclass | Phase 2 in the daemon roadmap — first daemon that writes |

**Inference caveat**: Temporal proximity is the correct first assumption but is probabilistic. If two facts about the same entity pair change simultaneously, the pairing may be ambiguous. Alternatives (LLM-assisted reasoning, Graphiti's own contradiction reasoning) are available if temporal proximity proves insufficient.

---

## Approach: Supersession Enrichment Daemon

### Daemon tick algorithm

```
ON EACH TICK:
  1. Query FalkorDB: edges WHERE invalid_at IS NOT NULL
                             AND superseded_by IS NULL
                             (only unprocessed edges)
  2. FOR each invalidated edge E:
     a. Find candidate successor edges:
        - Same source entity as E
        - Same target entity as E (or same predicate/fact type)
        - NOT E itself
     b. Among candidates, pick F where abs(F.valid_at - E.invalid_at) is minimal
     c. If confidence threshold met:
        SET E.superseded_by = F.uuid
        SET E.superseded_at = <discovered_at>
  3. Emit a Finding per new link (audit trail)
  4. Save checkpoint
```

### FalkorDB write (Cypher sketch)

```cypher
MATCH (e:Relationship {uuid: $invalidated_uuid})
SET e.superseded_by = $successor_uuid,
    e.superseded_at = $discovered_at
```

### Storage

- Daemon dir: `~/.watercooler/daemons/supersession-enricher/`
- `checkpoint.json` — last run, per-edge scan watermark
- `findings.jsonl` — audit trail of new links discovered
- The links themselves live in FalkorDB (shared)

### MCP exposure

`get_entity_edge()` and `search_memory_facts()` read `superseded_by` from FalkorDB automatically — no additional MCP-layer changes needed once the property is written.

---

## MCP Use Cases

**Query mode** (Phase 1 plumbing):
```python
watercooler_search(query="Dana's role", active_only=True)
# Returns only currently-valid facts (invalid_at=None)
```

**Audit/diff mode** (Phase 1 plumbing + Phase 2 daemon):
```python
# Get full temporal chain
watercooler_search(query="Dana's role", active_only=False)
# Each result includes invalid_at, valid_at, superseded_by
# Callers can reconstruct the decision chain: A → superseded by B → superseded by C
```

---

## Testing Strategy

| Layer | What | Cost |
|-------|------|------|
| Unit tests | `_filter_by_time_range` with `time_key="invalid_at"`, `_filter_active_only()` | Cheap (no services) — already written |
| Integration tests | End-to-end Graphiti supersession: ingest contradicting episodes, verify `invalid_at` set, verify `active_only` filter works | Medium (requires FalkorDB + LLM) — already written |
| Daemon unit tests | `SupersessionEnricher.tick()` with mock FalkorDB — verify inference logic, Cypher correctness, finding emission | Cheap (mock) — to be written |
| Daemon integration tests | Live FalkorDB: ingest contradicting episodes, run daemon tick, verify `superseded_by` written | Medium (requires FalkorDB) — to be written |

---

## What's Already Done

The pre-work from the initial implementation pass (before this brainstorm):
- `_filter_active_only()` added to `graphiti.py`
- `active_only` param threaded through `search_memory_facts()`, `search_facts()`, `_search_graph_impl()`, `_search_graphiti_impl()`
- `valid_at` and `invalid_at` exposed in MCP search results
- `tests/unit/test_supersession_filter.py` — 9 unit tests, all passing
- `tests/integration/test_t2_supersession.py` — 3 integration tests (require live backend)

---

## Open Questions

None — all design decisions resolved.

---

## Resolved Questions

- **Where do links live?** FalkorDB (shared graph), not a local side-car file. Team-shared memory requires one canonical store.
- **Bypass Graphiti?** Yes — Graphiti doesn't expose edge property annotation. Direct Cypher is the right approach.
- **Inference algorithm?** Temporal proximity first. LLM-assisted and Graphiti-native reasoning available as fallbacks.
- **Trigger model?** Periodic scan (300s interval), not event-driven. Avoids coupling to ingest path.
