---
name: memory-health
description: Check watercooler memory system health and configuration. Use when memory queries fail or return unexpected results.
allowed-tools:
  - Bash(mcp-cli *)
---

# Memory System Health Check

Check memory system health and configuration.

## Steps

1. **Check diagnose schema**:
   ```bash
   mcp-cli info watercooler-cloud/watercooler_diagnose_memory
   ```

2. **Run memory diagnostics**:
   ```bash
   mcp-cli call watercooler-cloud/watercooler_diagnose_memory '{}'
   ```

3. **Check baseline sync status schema**:
   ```bash
   mcp-cli info watercooler-cloud/watercooler_baseline_sync_status
   ```

4. **Run baseline sync status check**:
   ```bash
   mcp-cli call watercooler-cloud/watercooler_baseline_sync_status '{}'
   ```

5. **Report status**:

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

6. **Suggest fixes** for common issues:
   - Missing environment variables
   - Connection problems
   - Sync issues

## Example Invocations

- `/memory-health` - Full health check
- Use when searches return no results unexpectedly
- Use when memory tools throw errors
