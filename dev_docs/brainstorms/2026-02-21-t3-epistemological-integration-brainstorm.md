# T3 Epistemological Integration — LeanRAG as Justified Knowledge

**Date**: 2026-02-21 (updated 2026-02-23)
**Status**: Brainstorm complete, ready for planning
**Thread**: `leanrag-tier3-integration-audit` (entries 0-14)
**Participants**: Jay, Claude Code (opus-4-6), Codex (critic)
**Update (2026-02-23)**: Added "Concrete Data Flow" section with actual code paths from live-test session

---

## What We're Building

A T3 (LeanRAG) memory integration that embodies the epistemological model of
the tiered memory system: progressive distillation from observation (T1) through
belief (T2) to justified true belief (T3), with bidirectional traceability so
that any T3 concept can be traced back to the source experiences that justify it.

### The Problem

T3 is currently broken at two levels:

1. **Plumbing**: Three-way work_dir inconsistency, bare `GraphitiBackend()`
   construction, broken incremental imports. T3 can't reliably index or query
   the same graph. (See audit thread entries 0-6.)

2. **Epistemology**: Even when the plumbing works, T3 blindly indexes all
   content — speculation, low-confidence observations, rejected alternatives —
   alongside justified decisions and confirmed facts. The distillation is
   unqualified. And once content enters T3, the provenance chain back to source
   observations is weak: T3 `source_id` prefers Graphiti episode UUIDs when
   available (`tools/memory.py:843`, `backends/leanrag.py:550`) but falls back
   to MD5 chunk hashes when UUIDs are missing — and there is no reverse lookup
   from either format to T1 entry_ids.

### The Vision

The three tiers represent a progressive distillation:

- **T1 (Baseline JSONL)**: Raw observations — what was said, by whom, when
- **T2 (Graphiti)**: Episodic memory — entity extraction, temporal tracking,
  decision traces capturing the evolution of belief
- **T3 (LeanRAG)**: Justified true belief — distilled knowledge that has
  survived critique, organized hierarchically

The system must work in both directions:
- **Forward (T1 → T3)**: Observations are distilled into beliefs, beliefs are
  qualified and promoted into knowledge
- **Reverse (T3 → T1)**: High-level concepts trace back to the source
  experiences that justify them, potentially fanning out to many distributed
  and diverse points of origin across threads

---

## Why This Approach

### Key Design Decisions

**1. Qualified distillation, not blind indexing**

T3 content must be earned, not inherited. A JTB (justified true belief)
certification gate between T2 and T3 ensures only content that has survived
scrutiny gets promoted. Types of T3-eligible content:

- Decision traces (confidence >= 3, passes validation gates)
- Validated facts (confirmed through implementation/testing)
- Confirmed patterns (established and reinforced across PRs)
- Established conventions (norms, enforced)

What they share: evidence, scrutiny survival, non-speculative status.
Content that fails certification stays in T2 — available but not promoted.

**2. Semantic bridging for reverse provenance (not structural links)**

Rather than building elaborate cross-tier reference structures (entity UUID
mappings, cross-tier indexes), reverse provenance uses semantic querying:

```
Agent queries T3 → gets high-level concept + leaf entity descriptions
Agent queries T2 with T3 result → T2 returns episodes with source metadata
Agent resolves episode_uuid → entry_id via EntryEpisodeIndex
  → fan-out to T1 entries across threads
```

**Important**: T2 episode search results currently include `uuid`,
`source_description`, and `content` but do NOT include `entry_id` directly
(`graphiti.py:2427`). The final T2→T1 step requires an explicit
`EntryEpisodeIndex` lookup (episode_uuid → entry_id). Non-chunked sync
writes also omit entry_id from `source_description` (`memory_sync.py:92`).
Phase 1 must document this lookup step; Phase 2 should evaluate whether
enriching episode search output with entry_id is worthwhile.

This works because T3 leaf descriptions are derived from T2 content, so
semantic overlap is high by construction. Benefits:

- No new cross-tier index to build, maintain, or keep in sync
- No coupling between T3 and T2 data models
- Fan-out happens naturally via T2 query results
- Degrades gracefully (unlike rigid structural links that break on rebuild)
- Zero new infrastructure beyond what the audit already identified

The only structural requirement: T3 leaf entity descriptions must be
semantically rich enough to serve as effective T2 queries. The Phase 4
Episodic Bridge template (enriched descriptions from Graphiti entity
summaries + episode snippets) satisfies this.

**3. Leaf nodes are the provenance anchor**

LeanRAG's hierarchy (Community → clusters → leaf entities via HAS_CHILD edges)
already provides structural traceability within T3. Provenance anchors at the
leaf level, not the community/cluster level. Community summaries remain
synthetic; leaves carry the connection to source material.

**4. Progressive implementation**

Ship plumbing fixes and semantic bridge first (Approach A). Collect empirical
data on T3 quality and semantic bridge effectiveness. Then build the JTB
certification gate with evidence-based calibration (Approach B).

---

## Key Decisions

1. **T3 should index certified content, not raw T2 content** — The T2→T3
   boundary is an epistemic gate, not just a pipeline stage. Note: Phases 1-2
   use temporary blind indexing; certification is implemented in Phase 3
2. **Reverse provenance via semantic bridging** — No structural cross-tier
   links; agents use T3 results to query T2, which returns T1 provenance
3. **Leaf nodes anchor provenance** — Within T3, trace down the hierarchy to
   leaves; leaves serve as semantic query keys against T2
4. **Progressive rollout** — Plumbing first, certification gate second, with
   empirical calibration between phases
5. **Decision trace rubric as starting point for JTB gate** — Adapt the
   existing 0-5 confidence scale and 8 validation gates for broader content
   types (facts, patterns, conventions)

---

## Design Constraints

### Reference Data Must Flow Through T2

Reference material (white papers, research documents, technical specs) must
enter the memory system through T2 (Graphiti), not skip directly to T3:

- **T2 handles temporal tracking**: Graphiti's bi-temporal model (`valid_at`/
  `invalid_at`) enables supersession tracking. A newer paper can invalidate
  specific claims from an older foundational paper while leaving others intact.
- **Out-of-order ingestion is normal**: Discovery is non-linear — foundational
  work may be indexed after derivative work. The system must not require
  chronological ingestion order.
- **Lazy conflict resolution**: When a newly ingested paper's entities overlap
  with existing facts, conflicting claims coexist in T2. Resolution happens at
  query time — agents see both perspectives and resolve in context. No eager
  auto-resolution in Phase 1. A narrow exception policy for high-confidence
  cases (explicit retractions, direct citation contradictions with matching
  DOIs) may be introduced in Phase 3 alongside the JTB gate, with concrete
  guardrails and tests.
- **Publication date as reference_time**: Reference data episodes should use
  the paper's publication date (not ingestion time) as the episode's
  `reference_time`. This allows temporal queries to reflect when knowledge
  was produced, not when we encountered it.
- **JTB gate applies to reference data too**: Only non-conflicted or resolved
  facts from reference material qualify for T3 promotion. Unresolved tensions
  between papers stay in T2 until an agent or review process resolves them.

This constraint ensures the architecture accommodates both thread-sourced and
reference-sourced knowledge through the same epistemological pipeline. The
reverse provenance path (T3 → T2 → source) naturally extends to reference
material since both thread entries and paper chunks live in T2 as episodes.

**Not in scope for Phase 1**: Reference data ingestion pipeline. But Phase 1
design must not preclude it — specifically, `code_path` and `group_id`
handling must work for non-thread episode sources.

---

## Implementation Phases

### Phase 1: Plumbing + Semantic Bridge (Ship First)

Fix the audit items, establish project-context propagation, and expose
the reverse provenance mechanism:

1. **Config convergence** — Replace all ad-hoc `LeanRAGConfig()` construction
   with `load_leanrag_config(code_path=...)`. All factory calls must receive
   explicit `code_path` (or resolve from validated context); without it,
   `derive_group_id` falls back to generic `"watercooler"` instead of
   project-scoped names. Files: `tools/memory.py`, `tools/graph.py`
2. **Graphiti backend factory** — Eliminate bare `GraphitiBackend()` at 2
   sites. Files: `memory.py`, `tools/memory.py`, `memory_sync.py`
3. **Incremental guard** — Wrap broken `leanrag.pipelines.incremental` import
   in try/except. Primary site: `backends/leanrag.py:849` (owns the import).
   `memory_sync.py` calls into this but does not own the import site.
4. **E2E smoke test** — Verify index → query uses same work_dir, correct
   FalkorDB graph name
5. **Semantic bridge documentation** — Document the T3→T2→T1 provenance
   query pattern for agents, including the explicit EntryEpisodeIndex lookup
   step (episode_uuid → entry_id).
6. **`code_path` propagation** — Add `code_path: str = ""` parameter to
   `_leanrag_run_pipeline_impl` and as a first-class field on `MemoryTask`
   dataclass. Executor consumes it via `load_leanrag_config(code_path=...)`.
   **Persistence rule**: `MemoryTask` persists the derived `group_id` (from
   `derive_group_id(code_path)`) as the authoritative routing key. `code_path`
   is optional — stored only when needed for local config resolution, never
   as the primary key. This avoids leaking absolute host paths into persistent
   queue state and preserves portability when repos move.
   Do NOT overload `source_description` for context encoding — keep it
   human/audit-oriented only.
7. **Pipeline group_id normalization** — Pipeline uses project-scoped
   `group_id` (derived from config, same as sync path at `memory_sync.py:77`).
   Reject thread-topic IDs with clear error — they yield zero episodes since
   sync stores under `config.database`, not topic name.
8. **EntryEpisodeIndex MCP exposure** — Expose `get_entry(episode_uuid)`
   lookup as an MCP-accessible utility for reverse provenance. Define fallback
   behavior for misses (legacy/non-indexed episodes): return
   `{"provenance_available": false}` rather than failing.
9. **Wire date filters** — Pass `start_date`/`end_date` through to
   `get_episodes()` in both direct (`tools/memory.py:798`) and queued
   (`memory_sync.py:752`) execution paths. Currently documented but no-op.

Estimated: ~200-250 lines across 8 files. Low-medium risk.

### Phase 2: Graphiti-Seeded LeanRAG (Episodic Bridge)

Implement Phase 4 from the consolidation roadmap to produce semantically rich
leaf descriptions:

1. Entity exporter (Graphiti → LeanRAG entity.jsonl with enriched descriptions)
2. Episodic Bridge template (entity name + summary + episode snippets)
3. LeanRAG clustering on enriched entities
4. Validate that leaf descriptions serve as effective T2 query keys

This phase makes the semantic bridge *work well* — without enriched
descriptions, leaf entities may be too sparse for effective T2 querying.

### Phase 3: JTB Certification Gate (Build with Evidence)

After Phase 2 is running and agents have used the semantic bridge:

1. Collect data on T3 quality — which clusters contain noise vs knowledge?
2. Design certification rubric (adapting decision trace rubric for broader
   content types)
3. Implement certification step in the T2→T3 pipeline
4. Calibrate threshold based on collected evidence
5. Add confidence metadata to T3 leaf entities

---

## Resolved Questions (Codex Review, 2026-02-21)

1. **Episode search output lacks entry_id** (Finding 1, High): Confirmed.
   T2 episode search results do not include `entry_id`. Reverse provenance
   requires explicit `EntryEpisodeIndex` lookup. Phase 1 must document this;
   Phase 2 should evaluate enriching episode output with entry_id.

2. **Certification vs progressive rollout sequencing** (Finding 2, Medium):
   Resolved by clarifying wording — "T3 *should* index certified content" is
   the target state. Phases 1-2 use temporary blind indexing. Phase 3
   implements certification. No policy contradiction.

3. **source_id is conditionally MD5, not always** (Finding 3, Medium):
   Corrected. Pipeline prefers Graphiti episode UUID for chunk_id
   (`tools/memory.py:843`), falls back to MD5 only when UUID is missing
   (`backends/leanrag.py:550-554`).

4. **Incremental import site ownership** (Finding 4, Low): Corrected.
   The broken import lives in `backends/leanrag.py:849`, not `memory_sync.py`.

5. **Should episode UUID be mandatory LeanRAG source_id?** (Codex Q3):
   Deferred to Phase 2. When pipeline ingestion runs through the Graphiti-seeded
   path, episode UUIDs should be available. Making MD5 fallback a warning
   (not silent) is a reasonable Phase 1 hardening step.

6. **code_path needs explicit API/context strategy** (Entry #11, High):
   Resolved. Option A chosen: add `code_path` as first-class field on tool
   API + `MemoryTask` schema. Option B (resolve from context, parse from
   `source_description`) rejected as brittle. Hosted-mode refinement deferred.

7. **group_id misuse yields empty results, not narrow results** (Entry #11,
   Medium→High): Confirmed. Sync stores episodes under `config.database`
   (project-scoped). Thread-topic IDs match zero episodes. Severity upgraded.

8. **EntryEpisodeIndex miss handling** (Entry #11, hardening note): Accepted.
   MCP provenance tool returns `{"provenance_available": false}` for misses
   rather than failing.

9. **Date filters are documented but no-op** (Entry #13, Medium): Confirmed.
   Both execution paths ignore `start_date`/`end_date`. Will wire through
   to `get_episodes()` in Phase 1.

10. **Don't overload source_description** (Entry #13, Low): Agreed. Keep
    `source_description` human/audit-oriented. Use structured `code_path`
    field on `MemoryTask` instead.

---

## Open Questions

1. **Certification rubric scope**: The decision trace rubric has 8 gates and
   a 0-5 scale. How much of this transfers to non-decision content (facts,
   patterns, conventions)? Likely a subset — need empirical data from Phase 2.

2. **LLM cost of certification**: The JTB gate requires evaluating content
   against qualification criteria. Can this be done with lightweight heuristics
   (keyword patterns, confidence signals from T2 entities) or does it require
   LLM evaluation? Phase 2 data will inform this.

3. **Rebuild vs incremental certification**: When T3 rebuilds (full LeanRAG
   re-index), does all content need re-certification, or can certification
   results be cached? Relates to the immutable batch vs incremental tension.

4. **Semantic bridge precision**: How well does the T3→T2 semantic query
   actually work in practice? What's the recall? Phase 2 provides the test bed.

---

## Relationship to Existing Plans

| Document | Relationship |
|----------|-------------|
| `MEMORY_CONSOLIDATION_PHASE4.md` | Phase 2 here implements Phase 4.1-4.2 from the consolidation roadmap |
| `MEMORY_INTEGRATION_ROADMAP.md` | This brainstorm extends Milestone 4 (EntryEpisodeIndex) with the semantic bridge concept |
| `DECISION_TRACE_EXTRACTION_GUIDE.md` | JTB certification gate (Phase 3) adapts the decision trace rubric |
| Audit thread (entries 0-8) | Phase 1 here implements the 4-step execution order from the audit |
| `THE_WATERCOOLER_EFFECT_v2.md` | The epistemological tier model described there is the philosophical foundation |

---

## What We're NOT Building

- **Structural cross-tier entity mapping** — No EntityChunkMapping, no xref_
  graph, no cross-tier index. Semantic bridging replaces these.
- **Inline provenance in community summaries** — Community reports stay
  synthetic. Provenance lives at the leaf level and is resolved via T2 queries.
- **Incremental LeanRAG updates** — T3 remains batch rebuild until the module
  exists. Incremental is guarded, not implemented.
- **Automated provenance resolution** — Agents perform the T3→T2→T1 trace
  manually (query T3, then query T2). This is a deliberate choice — the agent
  decides when provenance matters, not the system.
- **Reference data ingestion pipeline** — Phase 1 must not preclude it, but
  the actual PDF→chunk→T2 pipeline is separate future work.
- **Eager conflict resolution for reference data (Phase 1)** — Conflicting
  claims from different papers coexist in T2. Resolution is strictly lazy (at
  query time) in Phases 1-2. Phase 3 may introduce a narrow eager exception
  for high-confidence cases (explicit retractions, DOI-matched contradictions)
  with concrete guardrails and tests.

---

## Concrete Data Flow (Added 2026-02-23)

This section documents the actual code path for each tier transition, derived
from a live-test session and codebase audit.

### Write Flow — Parallel Population

All three tiers are populated via async callbacks at write time. T1 is
synchronous; T2 and T3 are fire-and-forget:

```
Thread entry (ULID entry_id)
  │
  ├─► T1: Graph write (synchronous)
  │       nodes.jsonl + edges.jsonl updated
  │       Async enrichment: summaries, embeddings (background)
  │
  ├─► T2: _graphiti_sync_callback() [fire-and-forget]
  │       Entry content → add_episode_direct() or add_episode_chunked()
  │       EpisodeRecord { uuid, content, source_description, group_id }
  │       EntryEpisodeIndex records: entry_id (ULID) ↔ episode_uuid
  │
  └─► T3: _leanrag_sync_callback() [fire-and-forget]
          Appends to .leanrag_queue.jsonl
          Pipeline NOT triggered automatically — must be called explicitly:
          watercooler_leanrag_run_pipeline (BULK task)
```

### T2 → T3 Conversion (BULK Pipeline)

```
get_group_episodes(group_id="watercooler_cloud")
  │   → ALL EpisodeRecord objects under that group_id (no quality filter)
  │   → Optionally filtered by start_date/end_date (currently no-op)
  │
  episodes_to_chunk_payload(episodes, group_id)
  │   → 1 episode = 1 chunk (no sub-chunking)
  │   → chunk_id = episode.uuid  ← this IS the provenance anchor
  │   → text    = episode.content
  │   → metadata = {group_id, source="graphiti_episode"}
  │       NOTE: entry_id NOT stored in metadata (gap — see below)
  │       NOTE: temporal ordering (previous_episode_uuids) NOT preserved
  │
  backend.index(chunk_payload)
  │
  ├─ Stage 1: triple_extraction(chunks_dict, llm, work_dir)
  │       LLM reads each chunk's text
  │       Extracts entities + relations (held in memory — no checkpointing)
  │       Writes entity.jsonl + relation.jsonl to work_dir
  │       hash_code in these files = chunk_id = episode UUID
  │
  └─ Stage 2: build_hierarchical_graph(working_dir)
          GMM-based clustering of entity embeddings
          Builds Community → cluster → leaf entity hierarchy in FalkorDB
          Leaf entity carries source hash_code from entity.jsonl
```

### Provenance Chain

**Forward (entry → T3)**:
```
entry_id (ULID)
  → EntryEpisodeIndex: entry_id → episode_uuid
  → episodes_to_chunk_payload: chunk_id = episode_uuid
  → triple_extraction: entity source_id = chunk_key = episode_uuid
  → build_native: source_ids accumulated (pipe-delimited, cap 5)
  → FalkorDB Entity node: n.source_id = "uuid1|uuid2|..."
```

**Reverse (T3 → T1, structural)**:
```
T3 CoreResult.source = "uuid1|uuid2|uuid3"   (≤5 pipe-delimited episode UUIDs)
  → split on "|"
  → EntryEpisodeIndex.get_entry(episode_uuid) → entry_id
  → T1 thread entry
```

**Correction from earlier brainstorm**: The original framing of "semantic
bridging" as the primary reverse-provenance mechanism was incomplete. The
actual model is **semantic discovery + structural citation resolution**:

- **Semantic discovery** (query → relevant entities): Vector search finds
  entities by semantic similarity. This step is inherently probabilistic.
- **Structural citation resolution** (entity → source entries): Once an
  entity is found, `CoreResult.source` contains the contributing episode
  UUIDs. `source_id` is set by `_handle_single_entity_extraction()` to
  `chunk_key` (= episode UUID), propagated through `entity.jsonl` →
  `build_native.py` → FalkorDB `Entity.source_id` → query results. This
  step is deterministic — no semantic matching required.
- **Fallback path**: When structural resolution fails (see limitations
  below), a semantic query against T2 using the entity description recovers
  approximate provenance.

**Provenance truth table**:

| Condition | Path | Certainty |
|-----------|------|-----------|
| `ep.uuid` present + EntryEpisodeIndex hit | `source_id` → UUID → `get_entry()` | Deterministic |
| `ep.uuid` present + EntryEpisodeIndex miss | Semantic query against T2 | Probabilistic fallback |
| `ep.uuid` missing (MD5 chunk_id used) | Semantic query against T2 | Probabilistic fallback |
| source_id truncated (>5 episodes) | Partial — first 5 deterministic, rest fallback | Mixed |

**MD5 fallback caveat** (`memory_sync.py:686`): When `ep.uuid` is missing,
`episodes_to_chunk_payload()` falls back to `MD5(content)` as the chunk_id.
This MD5 is not a Graphiti episode UUID, so `EntryEpisodeIndex` cannot
resolve it. These episodes fall through to the semantic fallback path.

**Chunked entries**: For large entries split into multiple T2 episodes, each
chunk episode has its own UUID. `add_chunk_mapping(chunk_id, episode_uuid,
entry_id, thread_id, chunk_index, total_chunks)` records all of them. Each
chunk's episode UUID flows independently through T3 as a separate source_id
on the entities extracted from it.

### Provenance Limitations

**Limitation 1: source_id cap at 5 per entity, with non-deterministic ordering** (`falkordb.py:163`):
```python
source_id = "|".join(entity['source_id'].split("|")[:5])
```
When the same entity name is extracted from more than 5 episodes, only the
first 5 source UUIDs are retained. Additionally, upstream clustering uses
`set()` to compose source_ids before truncation (`hierarchical.py:552, 920,
925`):
```python
data['source_id'] = "|".join(set([n['source_id'] for n in cluster_nodes]))
```
Python set ordering is non-deterministic, so *which* 5 UUIDs survive the
cap is effectively random when more than 5 exist. This affects common/broad
entities ("FalkorDB", "memory backend") across many episodes. Specific
entities (a named decision, a particular fix) typically appear in 1–5 chunks
and are fully covered.

Implication: T3 provenance is complete and deterministic for specific
knowledge (good) and partially random for generic concepts (acceptable —
they don't need precise sourcing, and the semantic fallback still works).

**Limitation 2: EntryEpisodeIndex coverage gaps**:
The index is only as complete as what was synced with a valid `entry_id`.
Episodes ingested via legacy paths, or without `entry_id` being passed, will
not resolve. The brainstorm's proposed `{"provenance_available": false}`
fallback handles this gracefully. Coverage improves as all new entries are
synced through the current code path.

### What T3 Indexes Today

Everything in T2 for the group, unfiltered. This is Phase 1 behavior —
the JTB certification gate (Phase 3) is the mechanism that will filter
to only quality-certified content. Until then, T3 includes speculation,
low-confidence observations, and rejected alternatives alongside justified
decisions.

### T3 is Disabled by Default

T3 opt-in requires explicit config:
```toml
[memory.tiers]
t3_enabled = true
```
The query escalation system (TierOrchestrator) will not route to T3 unless
enabled. Default max_tiers = 2 (T1+T2 only).

---

## Next Step

Run `/workflows:plan` to create an implementation plan for Phase 1 (plumbing +
semantic bridge + project-context propagation), with Phase 2 and Phase 3
scoped as follow-on work.
