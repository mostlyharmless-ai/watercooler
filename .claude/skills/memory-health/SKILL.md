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

2. **Run memory diagnostics**:
   ```
   mcp__watercooler-cloud__watercooler_diagnose_memory()
   ```

3. **Run baseline sync status check**:
   ```
   mcp__watercooler-cloud__watercooler_baseline_sync_status()
   ```

4. **Report status**:

   **Tier Status**:
   - T1 (Baseline Graph): enabled/disabled, connection status
   - T2 (Graphiti): enabled/disabled, connection status
   - T3 (LeanRAG): enabled/disabled, connection status

   **Backend Connections**:
   - Neo4j: connected/disconnected
   - Embedding service: available/unavailable
   - LLM service: available/unavailable

   **Graph Sync Status**:
   - Synced threads count
   - Stale threads count
   - Error threads (if any)

   **Configuration**:
   - Current code_path
   - Active group_id
   - Any missing required config

5. **Suggest fixes** for common issues:
   - Missing environment variables
   - Connection problems
   - Sync issues

## Example Invocations

- `/memory-health` - Full health check
- Use when searches return no results unexpectedly
- Use when memory tools throw errors
