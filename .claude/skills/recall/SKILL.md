---
name: recall
description: Recall relevant project context before starting work. Use when beginning a new task, investigating unfamiliar code, or needing background on past decisions.
allowed-tools:
  - Bash(mcp-cli *)
---

# Recall Context

Recall context for: $ARGUMENTS

## Output Mode

If arguments contain `--raw`, show full JSON response after summary.
Otherwise, show human-readable summary only.

## Steps

1. **Check schema first** (mandatory):
   ```bash
   mcp-cli info watercooler-mcp/watercooler_smart_query
   ```

2. **Execute query**:
   ```bash
   mcp-cli call watercooler-mcp/watercooler_smart_query '{"query": "<topic/task description>"}'
   ```

3. **Summarize findings** in categories:
   - **Prior decisions** related to this topic
   - **Relevant patterns** or implementations
   - **Known issues** or gotchas
   - **Related threads** or discussions

4. **Show transparency**:
   - Which tier was used (T1/T2/T3)
   - Escalation reason if any
   - Confidence level

5. **Handle empty results**:
   - If no results, suggest alternative search terms
   - Check if memory backends are configured

6. **Raw output** (if `--raw` flag present):
   - Append full JSON response after summary

## Example Invocations

- `/recall authentication flow` - Find context about auth implementation
- `/recall --raw config system` - Get raw JSON about config discussions
- `/recall branch parity sync` - Background on git sync features
