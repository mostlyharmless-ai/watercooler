# Watercooler MCP Tool Design Principles

Reference for tool audits. Use to calibrate whether each tool fits the intended design philosophy.

---

## Core Philosophy

### Graph-First

The baseline graph (JSONL in `.graph/`) is the **sole source of truth** for reads. Markdown `.md` files are write-only projections for human review and git diffs — they are never read back. Every read tool must go through the graph, not through `.md` files.

**Implication for audit:** Tools that offer overlapping read paths (e.g., reading `.md` directly vs. reading from graph) represent an architecture violation.

### Zero-Config

The system should work out-of-box for standard git layouts. Tools should auto-resolve `threads_dir` from `code_path` without requiring explicit configuration. The `code_path` parameter is the single entry point for context resolution.

**Implication for audit:** Tools that require complex prerequisite knowledge (e.g., must know the exact Graphiti URL) are design gaps for typical users.

### Ball Mechanics

The "ball" tracks which agent has the action obligation. Three modes:

| Action | Effect |
|--------|--------|
| `say`  | Write entry + flip ball to counterpart |
| `ack`  | Write entry + keep ball |
| `handoff` | Flip ball explicitly (optionally write note) |

These three operations should be orthogonal and complete — any collaboration move is one of these three. `set_status` is orthogonal (metadata only, no ball change).

### Agent Identity

Every write requires `agent_func` in format `<platform>:<model>:<role>`. This is recorded in commit footers for full traceability. Read tools never require identity.

---

## Tier Architecture

### T1 — Baseline (Free)

- Storage: JSONL graph in `.graph/nodes.jsonl`
- Search: Keyword token matching + optional cosine similarity embeddings
- Cost: Zero (local only)
- Tools: `search` (default mode), `find_similar`, `federated_search`, thread read tools
- Required: Always available

### T2 — Graphiti (Paid)

- Storage: FalkorDB graph database with bi-temporal edges
- Search: Hybrid keyword + semantic, entity/episode/fact modes
- Cost: Requires FalkorDB (Docker) + LLM service
- Tools: `search` (mode=entities/episodes/facts), `get_entity_edge`, `graphiti_add_episode`, `clear_graph_group`, `smart_query` (T2 tier)
- Required: `WATERCOOLER_GRAPHITI_ENABLED=1` + FalkorDB running

### T3 — LeanRAG (Paid)

- Storage: Hierarchical cluster tree over thread content
- Search: Multi-hop cluster traversal
- Cost: Expensive (LLM calls per query)
- Tools: `leanrag_run_pipeline`, `smart_query` (T3 tier)
- Required: `LEANRAG_PATH` + `WATERCOOLER_TIER_T3_ENABLED=1`

### Tier Escalation

`smart_query` auto-escalates from T1 → T2 → T3 when results are insufficient. The principle: "always use the cheapest tier that satisfies the query intent."

---

## Tool Category Goals

### Thread Read — Progressive Disclosure

Read tools form a hierarchy optimized for token efficiency:

```
list_threads          → titles/statuses (scan)
  └─ read_thread (summary_only=True)  → thread narrative in ~500 tokens
       └─ list_thread_entries         → entry TOC with abstracts + pagination
            └─ get_thread_entry       → single entry full body
                 └─ get_thread_entry_range → contiguous span of entries
```

Design goal: Start at the top, drill down only as needed.

### Thread Write — Minimal Interface

Write tools map to the three ball mechanics. No overlap between them. `set_status` is orthogonal (metadata-only).

Design goal: Exactly one tool per collaboration mode.

### Search & Memory — Layered Discovery

Three levels of search:

1. **`search`** — Explicit single-tier search with full filter control. Use when you know what backend/mode you want.
2. **`smart_query`** — Auto-escalating multi-tier. Use for natural language questions; lets the system choose the right tier.
3. **`federated_search`** — Cross-namespace keyword search. Use when context spans multiple repositories.

Design goal: Each search tool has a distinct scope (tier control vs. auto-escalation vs. cross-namespace).

### Admin/Ops — Diagnostic Hierarchy

```
health               → comprehensive system snapshot (includes daemon summary)
  ├─ daemon_status   → daemon-specific health (drill-down from health)
  │     └─ daemon_findings → actual findings from daemon analysis
  ├─ diagnose_memory → memory backend-specific diagnosis
  └─ whoami          → identity resolution check
```

Design goal: `health` is the entry point; specialist tools drill down.

---

## Known Design Tensions

### `search` vs `smart_query`

Both search memory. The distinction:
- `search`: explicit routing, all filter parameters, single tier
- `smart_query`: natural language, auto-escalates, returns tier metadata

**Tension:** When the user just wants "find something," both tools are plausible. The correct rule — use `search` when you know what you want and which tier; use `smart_query` for open-ended questions — is not always obvious from tool descriptions alone.

### `bulk_index` vs `migrate_to_memory_backend`

Both index content into paid memory backends. The distinction:
- `bulk_index`: queues tasks for persistent async background processing (via memory queue); idempotent; preferred
- `migrate_to_memory_backend`: direct synchronous write with checkpoint resume; intended for initial bootstrap

**Tension:** Both do the same end goal. `migrate_to_memory_backend` predates the memory queue and is now partially superseded.

### `graph_recover` (stub)

`graph_recover` was moved out of the MCP runtime to a script (`scripts/recover_baseline_graph.py`). The tool now returns instructions. This is intentional (extraordinary operations don't belong in MCP) but creates a gap: the tool appears in the tool list but does nothing.

### `handoff` vs `say` with explicit ball

`handoff` can do everything `say` does (write a note + flip ball) but with an explicit target. `say` flips to a preconfigured counterpart. When the counterpart is known, both tools overlap.

### `reindex` — markdown output only

`reindex` produces a human-readable markdown index. There's no structured JSON mode for programmatic use (e.g., to feed `bulk_index`).

---

## Orthogonality Criteria

A tool surface is orthogonal when:
1. Each operation can only be accomplished by one tool (no duplicates)
2. Tools do not silently overlap (e.g., two tools that search the same data without different scope/cost)
3. Parameters don't encode branching logic that belongs in separate tools (e.g., `mode=` that makes the tool behave radically differently)

## Complementarity Criteria

Tools are complementary when:
1. They form natural chains: one tool's output is input to the next
2. They form diagnostic drill-downs: general → specific
3. They form lifecycle pairs: preflight → execute, trigger → monitor
