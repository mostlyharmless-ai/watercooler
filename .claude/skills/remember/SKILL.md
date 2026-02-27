---
name: remember
description: Add important context or knowledge to project memory. Use for external knowledge, decisions made outside threads, or important context that should be searchable.
allowed-tools:
  - ToolSearch
  - mcp__watercooler-cloud__watercooler_diagnose_memory
  - mcp__watercooler-cloud__watercooler_graphiti_add_episode
  - mcp__watercooler-cloud__watercooler_say
  - mcp__watercooler-cloud__watercooler_list_threads
---

# Add to Memory

Remember: $ARGUMENTS

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
    │ graphiti_  │ ┌──▼──────────┐
    │ add_episode│ │ Prepare:     │
    └─────┬─────┘ │ title + body │
          │       └──┬──────────┘
          │          │
          │    ┌─────▼─────────┐
          │    │ watercooler_   │
          │    │ say → thread   │
          │    │ entry (T1)     │
          │    └──┬────────────┘
          │       │
    ┌─────▼───────▼─────┐
    │ Confirm storage:   │
    │ - Where stored     │
    │ - How to retrieve  │
    └───────────────────┘
```

## Steps

1. **Load diagnostic tool and check memory status**:
   ```
   ToolSearch: select:mcp__watercooler-cloud__watercooler_diagnose_memory
   ```
   Then call:
   ```
   mcp__watercooler-cloud__watercooler_diagnose_memory()
   ```

2. **If T2 (Graphiti) is available** (diagnose shows `t2.enabled: true` and `t2.connected: true`):

   Load tool:
   ```
   ToolSearch: select:mcp__watercooler-cloud__watercooler_graphiti_add_episode
   ```
   Then call:
   ```
   mcp__watercooler-cloud__watercooler_graphiti_add_episode(
     content="<content to remember>",
     source="manual",
     source_description="User-provided context via /remember"
   )
   ```

   Report:
   - Episode UUID created
   - Entities extracted
   - How to retrieve later

3. **If T2 unavailable** — fall back to T1 thread entry:

   Explain to the user that T2 is unavailable and the content will be stored as a thread entry instead.

   Load tools:
   ```
   ToolSearch: select:mcp__watercooler-cloud__watercooler_say
   ToolSearch: select:mcp__watercooler-cloud__watercooler_list_threads
   ```

   Check if the target thread exists:
   ```
   mcp__watercooler-cloud__watercooler_list_threads()
   ```

   Prepare the entry content:
   - **Title**: Generate a concise, descriptive title (5-12 words). Do NOT use
     the placeholder `Memory: <brief summary>`. Examples:
     - "PostgreSQL chosen over MongoDB for ACID compliance"
     - "API rate limit is 100 requests per minute"
     - "Legacy auth uses JWT tokens in localStorage"
   - **Body**: Structure the content clearly. For short facts, keep as-is.
     For longer content (3+ sentences), organize with context and key takeaways.
     Always include `Spec: scribe` as the first line of the body.

   Create the entry:
   ```
   mcp__watercooler-cloud__watercooler_say(
     topic="<thread-topic>",
     title="<concise descriptive title>",
     body="Spec: scribe\n\n<content>",
     role="scribe",
     entry_type="Note",
     code_path="<repo-root>",
     agent_func="Claude Code:opus-4-6:scribe"
   )
   ```

   Thread selection:
   - Knowledge/context → `project-context` thread (if it exists)
   - Decisions → `decisions` thread (if it exists)
   - If neither exists, ask the user which thread to use or whether to create one
   - The baseline graph (T1) will index it automatically on next sync

4. **Confirm storage**:
   - What was stored
   - Where it was stored (T2 episode or T1 thread entry)
   - How to find it later (search terms, entry_id, or `/ask-memory`)

## Example Invocations

- `/remember The API rate limit is 100 requests per minute`
- `/remember We decided to use PostgreSQL over MongoDB for ACID compliance`
- `/remember The legacy auth system uses JWT tokens stored in localStorage`
