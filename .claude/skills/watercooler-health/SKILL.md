---
name: watercooler-health
description: Check watercooler system health — MCP server, memory tiers (T1/T2/T3), git auth, GitHub rate limit, and daemons. Use when memory queries fail, syncs break, or anything in the watercooler stack behaves unexpectedly.
allowed-tools:
  - ToolSearch
  - mcp__watercooler__watercooler_health
  - mcp__watercooler__watercooler_diagnose_memory
  - mcp__watercooler__watercooler_baseline_sync_status
---

# Watercooler Health Check

Run a full health check across all watercooler subsystems.

## Steps

1. **Load all diagnostic tools** in parallel:
   ```
   ToolSearch: select:mcp__watercooler__watercooler_health
   ToolSearch: select:mcp__watercooler__watercooler_diagnose_memory
   ToolSearch: select:mcp__watercooler__watercooler_baseline_sync_status
   ```

2. **Run all three checks** in parallel:
   ```
   mcp__watercooler__watercooler_health(code_path="<repo root>")
   mcp__watercooler__watercooler_diagnose_memory(code_path="<repo root>")
   mcp__watercooler__watercooler_baseline_sync_status(code_path="<repo root>")
   ```

3. **Report status** — organize output into these sections:

   ---

   **Server Health** (from `watercooler_health`):
   - Server version and status
   - Agent identity and threads directory
   - Code branch and auto-branch mode
   - Python / fastmcp versions

   **Graph Services** (from `watercooler_health`):
   - Summaries enabled, LLM service URL and availability
   - Embeddings enabled, embedding service URL and availability

   **Backend Services** (from `watercooler_health`):
   - Auto-start services (llm, embedding, falkordb) with state icons ✓/✗/○
   - Include startup time (ms) for running services
   - Surface setup instructions for any failed services

   **Daemons** (from `watercooler_health`):
   - Each daemon: state, interval, ticks, findings, errors

   **Thread Storage** (from `watercooler_health`):
   - Mode (orphan worktree), path, code branch

   **Git Authentication** (from `watercooler_health`):
   - Protocol (ssh/https), connectivity status
   - Credential helper or SSH agent/key status
   - Any auth warnings and recommended fixes

   **GitHub** (from `watercooler_health`):
   - gh CLI version (warn if outdated)
   - API rate limit: remaining/total (%) and reset time
   - Any warnings and recommendations

   ---

   **T1 — Baseline Graph** (from `baseline_sync_status`):
   - Total / synced / stale / error threads
   - Recommendations (if any)

   **T2 — Graphiti** (from top-level fields in `diagnose_memory`):
   - `graphiti_enabled`: true/false
   - `graphiti_backend_import`: ✓/✗
   - `llm_api_key_set`, `llm_api_base`, `llm_model`
   - `falkordb_host` / `falkordb_port`: FalkorDB connection target
   - `graph_name`: FalkorDB database name (from config `database` field; falls back to `"watercooler"`)
   - `embedding_model`, `embedding_api_base`, `embedding_api_key_set`: embedding config
   - `backend_init`: ✓/✗ (FalkorDB connection)
   - `openai_key_set`: warn if true (deprecated field)

   **T3 — LeanRAG** (from `t3_leanrag` key in `diagnose_memory`):
   - `leanrag_backend_import`: ✓/✗
   - `leanrag_enabled`: true/false — if false, show `config_issue`
   - `leanrag_path` + `leanrag_path_exists`: submodule path and disk presence
   - `work_dir`: full path to LeanRAG working directory
   - `graph_name`: FalkorDB graph name (`basename(work_dir)`, e.g., `leanrag_watercooler_cloud`)
   - `falkordb_host` / `falkordb_port`: FalkorDB connection target
   - `llm_api_key_set`, `llm_api_base`, `llm_model`
   - `embedding_api_base`, `embedding_model` (no `embedding_api_key_set` — T3 uses keyless raw HTTP)
   - `backend_init`: ✓/✗
   - `has_incremental_state`: whether a saved cluster state exists (only present on success)
   - Env vars: `leanrag_path_env`, `leanrag_enabled_env`, `leanrag_database_env`

4. **Suggest fixes** for common issues:

   *T2 issues:*
   - Not enabled: set `WATERCOOLER_GRAPHITI_ENABLED=1` or `[memory] backend = "graphiti"` in config.toml
   - `backend_init` failed + FalkorDB not running: start FalkorDB via Docker (see Backend Services instructions)
   - Missing embedding key: set `EMBEDDING_API_KEY` env var or `[memory.embedding]` in config.toml

   *T3 issues:*
   - Not enabled: set `WATERCOOLER_LEANRAG_ENABLED=1` + `LEANRAG_PATH`, or add `[memory.tiers] t3_enabled = true` in config.toml
   - `leanrag_path_exists: false`: clone/update the LeanRAG submodule at the configured path
   - `leanrag_backend_import` failed: install with `pip install watercooler-cloud[memory]`
   - Missing embedding service: set `WATERCOOLER_EMBEDDING_API_BASE` or configure `[memory.embedding]`

   *T1 issues:*
   - Stale threads: run `watercooler_baseline_sync_status` and reconcile stale entries

   *Git / GitHub issues:*
   - SSH without agent: `eval "$(ssh-agent -s)" && ssh-add`
   - Expired GitHub CLI token: `gh auth login -h github.com --web`
   - Rate limited: pause automated operations; check reset time

## Example Invocations

- `/watercooler-health` — Full system health check
- Use when memory searches return no results unexpectedly
- Use when memory tools throw errors
- Use when syncs or push/pull operations fail
- Use when daemons are not running or producing no findings
