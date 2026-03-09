---
name: watercooler-tool-audit
description: This skill should be used to audit the watercooler MCP tool surface — analyzing how each tool is implemented, what functionality it provides, where behaviors overlap, where complementary patterns exist, and where gaps remain. Produces a structured inventory table and a descriptive report covering orthogonality, complementarity, and gaps. Use when the user asks to "audit the MCP tools", "analyze tool coverage", "find gaps in the tool surface", "review orthogonality of watercooler tools", or "produce a tool inventory report".
allowed-tools:
  - Bash(python3 */watercooler-tool-audit/scripts/extract_tools.py*)
  - Read
  - Glob
  - Grep
  - mcp__plugin_serena_serena__find_symbol
  - mcp__plugin_serena_serena__get_symbols_overview
  - mcp__plugin_serena_serena__read_memory
  - mcp__watercooler-cloud__watercooler_smart_query
  - mcp__watercooler-cloud__watercooler_search
  - ToolSearch
---

# Watercooler Tool Audit

Produces a comprehensive analysis of the watercooler MCP tool surface: what tools exist, how they're implemented, where they overlap, how they complement each other, and where gaps remain.

---

## Step 1: Extract Live Tool Inventory

Run the extraction script to get the current tool list directly from source:

```bash
python3 "$(git rev-parse --show-toplevel)/.claude/skills/watercooler-tool-audit/scripts/extract_tools.py" > /tmp/wc_tools.json
```

This parses all `src/watercooler_mcp/tools/*.py` files using AST analysis and extracts tool names, descriptions, parameters, R/W classification, and minimum tier requirement.

Read the output: note `summary.total_tools`, `summary.by_category`, and the full `tools` array. Record tool count per category for the report.

Cross-check the count against `docs/TOOLS-REFERENCE.md` (look for the safety annotations table). If counts differ, the script may have missed non-standard registration patterns — check manually.

---

## Step 2: Read Design Principles

Read `references/design-principles.md` from this skill directory to anchor the analysis.

Internalize:
- **Ball mechanics**: `say` / `ack` / `handoff` are the three and only collaboration modes
- **Progressive disclosure for reads**: `list_threads` → `read_thread(summary_only)` → `list_thread_entries` → `get_thread_entry`
- **Three search tools**: `search` (explicit routing), `smart_query` (auto-escalation), `federated_search` (cross-namespace)
- **Tier architecture**: T1 (baseline JSONL, always free) → T2 (Graphiti+FalkorDB) → T3 (LeanRAG)
- **Orthogonality goal**: each tool should do exactly one thing; no silent behavior overlap

---

## Step 3: Query Watercooler Context (if tools are available)

Run these queries in parallel to surface relevant design decisions:

```
watercooler_smart_query(
    query="What design decisions were made about the MCP tool surface, tool consolidation, or removed tools?",
    code_path="."
)
watercooler_search(
    query="tool consolidation removed deprecated search memory orthogonality",
    mode="entries",
    code_path="."
)
```

Note any intentionally removed tools (e.g., `watercooler_search_nodes` → `search(mode="entities")`), planned future tools, or deferred simplifications.

If watercooler tools are unavailable, skip this step and note it in the report.

---

## Step 4: Build the Tool Inventory Table

Using the JSON from Step 1 and design principles from Step 2, produce the inventory table.

### Table Format

```markdown
| Tool Name | Category | R/W | Min Tier | Description | Key Parameters | Complementary Tools |
|-----------|----------|-----|----------|-------------|----------------|---------------------|
```

**Column definitions:**
- **Tool Name**: Full `watercooler_*` MCP name
- **Category**: Thread Write / Thread Read / Graph & Search / Memory / Daemon / Diagnostic / Sync / Migration / Federation
- **R/W**: `read` (never mutates) / `write` (appends entries or modifies data) / `admin` (diagnostic/ops; may be read-only or idempotent)
- **Min Tier**: `T1` (always available) / `T2` (requires Graphiti+FalkorDB) / `T3` (requires LeanRAG)
- **Description**: One-line purpose from docstring
- **Key Parameters**: Required params + most important optional ones (e.g., `topic`, `code_path`, `query`, `mode`, `agent_func`)
- **Complementary Tools**: Natural downstream or upstream tools

Group rows by Category with a blank separator between groups.

---

## Step 5: Orthogonality Analysis

For each category, identify **within-category overlaps** — cases where two tools can accomplish the same thing.

Use this template for each overlap found:

```
OVERLAP: <tool_A> vs <tool_B>
  Shared behavior: <what both can do>
  Distinction (if any): <what separates them in scope or cost>
  Verdict: true overlap | distinct scopes | confusing but justified
  Recommendation: <if true overlap, what to consolidate or clarify>
```

**Areas to analyze:**

1. `search` vs `smart_query` — both search memory; what's the selection rule?
2. `bulk_index` vs `migrate_to_memory_backend` — both index into paid backends
3. `handoff` vs `say` — both can write a note + flip the ball
4. `health` vs `daemon_status` — health embeds a daemon summary; daemon_status drills down
5. `list_thread_entries(summary)` vs `read_thread(summary_only=True)` — both compact thread views
6. `graph_recover` stub — appears in tool list but only returns script instructions
7. `reindex` — produces markdown index only; no structured output

If a category has no overlaps, state that explicitly.

---

## Step 6: Complementarity Map

Document **cross-category tool chains** — natural workflows that use multiple tools in sequence.

For each workflow:

```
WORKFLOW: <name>
  Trigger: <when to use>
  Chain: tool_A → tool_B → tool_C
  Notes: <edge cases, parameter handoffs>
```

**Known chains to document:**

1. **Session start**: `health` → `whoami` → `reindex`
2. **Thread reading (progressive disclosure)**: `list_threads` → `read_thread(summary_only=True)` → `list_thread_entries` → `get_thread_entry`
3. **Memory onboarding**: `migration_preflight` → `bulk_index` → `memory_task_status` (poll)
4. **Graph enrichment → semantic search**: `graph_enrich` → `search(semantic=True)`
5. **Memory investigation with provenance**: `smart_query` → `get_entry_provenance` → `get_thread_entry`
6. **Daemon monitoring drill-down**: `health` (summary) → `daemon_status` (detail) → `daemon_findings` (findings list)
7. **Cross-repo discovery**: `federated_search` → `get_thread_entry` (using returned `entry_id`)
8. **Similarity expansion**: `search` → `find_similar` (seed from a high-scoring result)

---

## Step 7: Gap Analysis

Identify behavioral gaps — scenarios that are awkward or impossible with the current tool surface.

Rate each gap:
- **🔴 High**: Common scenario, no good workaround
- **🟡 Medium**: Possible but requires multiple steps or obscure parameters
- **🟢 Low**: Edge case or has a reasonable workaround

**Known gaps to verify and rate:**

1. **No explicit thread creation tool** — threads auto-create via `say(create_if_missing=True)` but there's no dedicated `create_thread` with full metadata (title, initial ball, initial status)
2. **`graph_recover` is a stub** — exists in tool list, does nothing useful; creates confusion in tool discovery
3. **`reindex` markdown-only** — no JSON output mode for programmatic use (e.g., feeding `bulk_index`)
4. **No daemon finding acknowledgment** — `daemon_findings` has `unacknowledged_only` filter but no MCP tool to ack/dismiss
5. **No in-thread text search** — read tools don't support keyword filtering; must use `search(thread_topic=X)` as workaround
6. **No `list_threads` pagination** — fine now but breaks at scale
7. **`federated_search` is T1-only** — no semantic search across namespaces
8. **`search` vs `smart_query` selection ambiguity** — descriptions don't make the selection rule obvious to agents
9. **No task cancellation in memory queue** — `memory_task_status` can retry but not cancel individual tasks
10. **`whoami` validates nothing useful** — shows client_id but doesn't confirm whether `agent_func` is configured; an agent calling `whoami` before writing gets misleading confidence

For each gap, note: what triggers it, current workaround (if any), and suggested addition.

---

## Step 8: Produce the Report

Output the full report in this structure:

```markdown
# Watercooler MCP Tool Audit
*Generated: <date> | Total tools: N*

## Executive Summary
<3–5 bullet points: total tools, read/write split, orthogonality verdict, top 3 gaps>

---

## Tool Inventory

<full inventory table>

---

## Orthogonality Analysis

### Thread Write
<analysis or "No overlaps — all four tools are distinct.">

### Thread Read
...

### Graph & Search
...

### Memory
...

### Diagnostic
...

### [Other categories]
...

---

## Complementarity Map

<documented workflows>

---

## Gap Analysis

### 🔴 High Priority Gaps
...

### 🟡 Medium Priority Gaps
...

### 🟢 Low Priority Gaps
...

---

## Summary Scorecard

| Dimension | Assessment |
|-----------|------------|
| Total tools | N (N read, N write, N admin) |
| Tier coverage | T1: N, T2: N, T3: N |
| Orthogonality | [Good / Needs work] — N overlaps |
| Complementarity | N natural chains |
| Gap count | N high, N medium, N low |
| Top recommendation | <single most impactful change> |
```

---

## Notes

- Run the extraction script fresh each time — tool metadata evolves with code changes
- The script uses AST analysis; it may miss tools registered via decorators on lambdas or inline functions — cross-check against `docs/TOOLS-REFERENCE.md` if counts are off
- Removed/consolidated tools are listed in the `memory.py` module docstring (e.g., `watercooler_query_memory` → `watercooler_smart_query`)
- `graph_recover` returning only instructions is intentional design; its presence in the tool list is a known tradeoff
- Overlap between `search` and `smart_query` is partially justified — they serve different caller sophistication levels; the gap is in documentation, not necessarily the tool surface itself
