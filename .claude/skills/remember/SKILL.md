---
name: remember
description: Add important context or knowledge to project memory. Use for external knowledge, decisions made outside threads, or important context that should be searchable.
allowed-tools:
  - Bash(mcp-cli *)
---

# Add to Memory

Remember: $ARGUMENTS

## Steps

1. **Check memory status first**:
   ```bash
   mcp-cli info watercooler-mcp/watercooler_diagnose_memory
   mcp-cli call watercooler-mcp/watercooler_diagnose_memory '{}'
   ```

2. **If T2 (Graphiti) is available**:

   Check schema:
   ```bash
   mcp-cli info watercooler-mcp/watercooler_graphiti_add_episode
   ```

   Add episode:
   ```bash
   mcp-cli call watercooler-mcp/watercooler_graphiti_add_episode '{
     "content": "<content to remember>",
     "source": "manual",
     "source_description": "User-provided context via /remember"
   }'
   ```

   Report:
   - Episode UUID created
   - Entities extracted
   - How to retrieve later

3. **If T2 unavailable** - Suggest thread entry:

   Explain that the content can be stored as a thread entry:
   ```bash
   mcp-cli info watercooler-mcp/watercooler_say
   ```

   Offer to create entry in appropriate thread:
   - Knowledge/context → `project-context` or `decisions` thread
   - The baseline graph will index it automatically

4. **Confirm storage**:
   - What was stored
   - Where it was stored (T2 episode or T1 thread entry)
   - How to find it later (search terms, entry_id)

## Example Invocations

- `/remember The API rate limit is 100 requests per minute`
- `/remember We decided to use PostgreSQL over MongoDB for ACID compliance`
- `/remember The legacy auth system uses JWT tokens stored in localStorage`
