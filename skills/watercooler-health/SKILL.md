---
name: watercooler-health
description: Check watercooler system health — MCP server, baseline graph (T1), git auth, GitHub rate limit, and daemons. Use when syncs break or anything in the watercooler stack behaves unexpectedly.
allowed-tools:
  - ToolSearch
  - mcp__watercooler__watercooler_health
  - mcp__watercooler__watercooler_baseline_sync_status
---

# Watercooler Health Check

Run a full health check across all watercooler subsystems.

## Steps

1. **Load diagnostic tools** in parallel:
   ```
   ToolSearch: select:mcp__watercooler__watercooler_health
   ToolSearch: select:mcp__watercooler__watercooler_baseline_sync_status
   ```

2. **Run checks** in parallel:
   ```
   mcp__watercooler__watercooler_health(code_path="<repo root>")
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
   - Auto-start services (llm, embedding, falkordb) with state icons
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

4. **Suggest fixes** for common issues:

   *T1 issues:*
   - Stale threads: run `watercooler_baseline_sync_status` and reconcile stale entries

   *Git / GitHub issues:*
   - SSH without agent: `eval "$(ssh-agent -s)" && ssh-add`
   - Expired GitHub CLI token: `gh auth login -h github.com --web`
   - Rate limited: pause automated operations; check reset time

## Example Invocations

- `/watercooler-health` — Full system health check
- Use when syncs or push/pull operations fail
- Use when daemons are not running or producing no findings
