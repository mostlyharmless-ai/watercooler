---
name: remember
description: Add important context or knowledge to project memory. Use for external knowledge, decisions made outside threads, or important context that should be searchable.
allowed-tools:
  - Bash(mcp-cli *)
---

# Add to Memory

Remember: $ARGUMENTS

## Argument Safety

When constructing JSON for `mcp-cli call`, **never** interpolate `$ARGUMENTS` directly into a JSON string.
Instead, use `jq` for safe construction:

```bash
mcp-cli call watercooler-cloud/watercooler_graphiti_add_episode "$(jq -n --arg content "$ARGUMENTS" '{content: $content, source: "manual", source_description: "User-provided context via /remember"}')"
```

This prevents JSON injection from user input containing quotes, backslashes, or control characters.

## Decision Tree

```
┌─────────────────────────────────┐
│  1. Run watercooler_diagnose_memory  │
└─────────────┬───────────────────┘
              │
      ┌───────▼───────┐
      │ T2 (Graphiti)  │
      │  available?    │
      └───┬───────┬───┘
        YES      NO
          │       │
    ┌─────▼─────┐ │
    │ Add via    │ │
    │ graphiti_  │ │
    │ add_episode│ │
    └─────┬─────┘ │
          │    ┌──▼──────────┐
          │    │ Fallback:    │
          │    │ watercooler_ │
          │    │ say → thread │
          │    │ entry (T1)   │
          │    └──┬──────────┘
          │       │
    ┌─────▼───────▼─────┐
    │ Confirm storage:   │
    │ - Where stored     │
    │ - How to retrieve  │
    └───────────────────┘
```

## Steps

1. **Check memory status first**:
   ```bash
   mcp-cli info watercooler-cloud/watercooler_diagnose_memory
   mcp-cli call watercooler-cloud/watercooler_diagnose_memory '{}'
   ```

2. **If T2 (Graphiti) is available** (diagnose shows `t2.enabled: true` and `t2.connected: true`):

   Check schema:
   ```bash
   mcp-cli info watercooler-cloud/watercooler_graphiti_add_episode
   ```

   Add episode:
   ```bash
   mcp-cli call watercooler-cloud/watercooler_graphiti_add_episode '{
     "content": "<content to remember>",
     "source": "manual",
     "source_description": "User-provided context via /remember"
   }'
   ```

   Report:
   - Episode UUID created
   - Entities extracted
   - How to retrieve later

3. **If T2 unavailable** (diagnose shows `t2.enabled: false` or `t2.connected: false`) — fall back to T1 thread entry:

   Explain to the user that T2 is unavailable and the content will be stored as a thread entry instead.

   Check schema:
   ```bash
   mcp-cli info watercooler-cloud/watercooler_say
   ```

   Create entry in appropriate thread:
   - Knowledge/context → `project-context` thread
   - Decisions → `decisions` thread
   - The baseline graph (T1) will index it automatically on next sync

4. **Confirm storage**:
   - What was stored
   - Where it was stored (T2 episode or T1 thread entry)
   - How to find it later (search terms, entry_id, or `/ask-memory`)

## Example Invocations

- `/remember The API rate limit is 100 requests per minute`
- `/remember We decided to use PostgreSQL over MongoDB for ACID compliance`
- `/remember The legacy auth system uses JWT tokens stored in localStorage`
