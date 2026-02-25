---
name: memory-health
description: Check watercooler memory system health and configuration. Use when memory queries fail or return unexpected results.
allowed-tools:
  - ToolSearch
  - mcp__watercooler-cloud__watercooler_diagnose_memory
  - mcp__watercooler-cloud__watercooler_baseline_sync_status
---

# Memory System Health Check

Check memory system health and configuration.

## Steps

1. **Load diagnostic tools**:
   ```
   ToolSearch: select:mcp__watercooler-cloud__watercooler_diagnose_memory
   ToolSearch: select:mcp__watercooler-cloud__watercooler_baseline_sync_status
   ```
   Load both in parallel.

2. **Run memory diagnostics and baseline sync** in parallel:
   ```
   mcp__watercooler-cloud__watercooler_diagnose_memory(code_path="<repo root>")
   mcp__watercooler-cloud__watercooler_baseline_sync_status(code_path="<repo root>")
   ```

3. **Report status**:

   **T1 — Baseline Graph** (from `baseline_sync_status`):
   - Total / synced / stale / error threads
   - Recommendations (if any)

   **T2 — Graphiti** (from top-level fields in `diagnose_memory`):
   - `graphiti_enabled`: true/false
   - `graphiti_backend_import`: ✓/✗
   - `llm_api_key_set`, `llm_api_base`, `llm_model`
   - `backend_init`: ✓/✗ (connection to FalkorDB/Neo4j)
   - `openai_key_set`: warn if true (deprecated field)

   **T3 — LeanRAG** (from `t3_leanrag` key in `diagnose_memory`):
   - `leanrag_backend_import`: ✓/✗
   - `leanrag_enabled`: true/false — if false, show `config_issue`
   - `leanrag_path` + `leanrag_path_exists`: path to submodule and whether it exists on disk
   - `work_dir`: derived FalkorDB graph directory
   - `falkordb_host` / `falkordb_port`: connection target
   - `llm_api_key_set`, `llm_api_base`, `llm_model`
   - `embedding_api_base`, `embedding_model`
   - `backend_init`: ✓/✗
   - `has_incremental_state`: whether a saved cluster state exists (only present on success)
   - Env vars: `leanrag_path_env`, `leanrag_enabled_env`, `leanrag_database_env`

4. **Suggest fixes** for common issues:
   - T3 not enabled: set `WATERCOOLER_LEANRAG_ENABLED=1` + `LEANRAG_PATH`, or add `[memory.tiers] t3_enabled = true` in config.toml
   - `leanrag_path_exists: false`: clone/update the LeanRAG submodule at the configured path
   - `leanrag_backend_import` failed: install with `pip install watercooler-cloud[memory]`
   - Missing embedding service: set `WATERCOOLER_EMBEDDING_API_BASE` or configure `[memory.embedding]`
   - T2 not enabled: set `WATERCOOLER_GRAPHITI_ENABLED=1` or `[memory] backend = "graphiti"` in config.toml
   - T1 stale threads: run `watercooler_baseline_sync_status` and reconcile stale entries

## Example Invocations

- `/memory-health` - Full health check
- Use when searches return no results unexpectedly
- Use when memory tools throw errors
