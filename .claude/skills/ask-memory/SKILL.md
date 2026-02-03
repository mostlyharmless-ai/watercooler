---
name: ask-memory
description: Ask a question about project history, decisions, or context. Use for questions like "What was decided about X?" or "Why did we choose Y?"
allowed-tools:
  - Bash(mcp-cli *)
---

# Ask Memory

Question: $ARGUMENTS

## Steps

1. **Check schema first** (mandatory):
   ```bash
   mcp-cli info watercooler-mcp/watercooler_smart_query
   ```

2. **Execute query**:
   ```bash
   mcp-cli call watercooler-mcp/watercooler_smart_query '{"query": "<natural language question>"}'
   ```

3. **Present answer** with:
   - **Direct answer** to the question
   - **Evidence** - cite specific entries with entry_id
   - **Confidence level** and tier used
   - **Escalation path** if T1 wasn't sufficient

4. **Handle uncertainty**:
   - If uncertain, say so explicitly
   - Don't fabricate answers from incomplete data

5. **Suggest follow-ups**:
   - Related questions that might help
   - Alternative search approaches

## Example Invocations

- `/ask-memory What was decided about the config system?`
- `/ask-memory Why did we choose markdown for threads?`
- `/ask-memory Who implemented the branch parity feature?`
- `/ask-memory What issues have we had with git sync?`
