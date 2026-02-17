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

For the T1 fallback (`watercooler_say`), the same principle applies — note that `$BODY`
is a variable you construct (not raw `$ARGUMENTS`):

```bash
mcp-cli call watercooler-cloud/watercooler_say "$(jq -n \
  --arg topic 'project-context' \
  --arg title '<GENERATED: concise descriptive title>' \
  --arg body "$BODY" \
  --arg role 'scribe' \
  --arg entry_type 'Note' \
  --arg code_path '<repo-root>' \
  --arg agent_func 'Claude Code:opus-4-5:scribe' \
  '{topic: $topic, title: $title, body: $body, role: $role, entry_type: $entry_type, code_path: $code_path, agent_func: $agent_func}')"
```

**IMPORTANT**: Never use `mcp-cli call tool - < /tmp/file.json` — file redirection
to stdin does not work with `mcp-cli`. Always use `jq` command substitution or pipe
(`cat file.json | mcp-cli call tool -`) instead.

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

   Check if the target thread exists before writing:
   ```bash
   mcp-cli call watercooler-cloud/watercooler_list_threads '{}'
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

   Assign the prepared body to `BODY` and create the entry (use `jq` for safe JSON construction):
   ```bash
   mcp-cli call watercooler-cloud/watercooler_say "$(jq -n \
     --arg topic '<thread-topic>' \
     --arg title '<GENERATED: concise descriptive title>' \
     --arg body "$BODY" \
     --arg role 'scribe' \
     --arg entry_type 'Note' \
     --arg code_path '<repo-root>' \
     --arg agent_func 'Claude Code:opus-4-5:scribe' \
     '{topic: $topic, title: $title, body: $body, role: $role, entry_type: $entry_type, code_path: $code_path, agent_func: $agent_func}')"
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
