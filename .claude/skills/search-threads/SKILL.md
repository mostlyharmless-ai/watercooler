---
name: search-threads
description: Search threads with filters. Supports filters like role:planner, type:Decision, after:2024-01, thread:topic-name, status:OPEN
allowed-tools:
  - Bash(mcp-cli *)
---

# Search Threads with Filters

Search: $ARGUMENTS

## Filter Syntax

Parse these filters from arguments:
- `role:X` - Filter by role (planner, implementer, critic, tester, pm, scribe)
- `type:X` - Filter by entry type (Note, Plan, Decision, PR, Closure)
- `after:DATE` / `before:DATE` - Time range (ISO format: 2024-01-15)
- `thread:X` - Specific thread topic
- `status:X` - Thread status (OPEN, CLOSED)
- `agent:X` - Filter by agent name
- Remaining text becomes the search query

## Argument Safety & Parsing Rules

### Tokenization

1. Split `$ARGUMENTS` on whitespace into tokens
2. A token is a **filter** if it matches `^(role|type|after|before|thread|status|agent):[^\s]+$`
3. The filter key is everything before the first `:`, the value is everything after
4. All non-filter tokens are joined with spaces to form the **query text**

### Edge Cases

- `role:planner:advanced` → key=`role`, value=`planner:advanced` (first colon splits)
- Multiple values for same key → last one wins
- Empty value (`role:`) → ignore, treat entire token as query text
- No query text → search with filters only (empty query string)

### Safe JSON Construction

**Never** interpolate filter values or query text directly into JSON strings.
Use `jq` for safe construction:

```bash
jq -n \
  --arg q "<query text>" \
  --arg role "<role or empty>" \
  --arg type "<type or empty>" \
  --arg topic "<thread or empty>" \
  --arg after "<after or empty>" \
  --arg before "<before or empty>" \
  '{query: $q, mode: "entries"} +
   (if $role != "" then {filters: {role: $role}} else {} end) +
   (if $type != "" then {filters: {type: $type}} else {} end) +
   (if $topic != "" then {filters: {topic: $topic}} else {} end) +
   (if $after != "" then {filters: {after: $after}} else {} end) +
   (if $before != "" then {filters: {before: $before}} else {} end)'
```

## Steps

1. **Parse arguments** into filters and query text

   Apply the tokenization rules above. Extract filter tokens (key:value pairs) and
   collect remaining text as the query. Only include filter keys that were explicitly
   provided — omit keys with no value.

2. **Check schema**:
   ```bash
   mcp-cli info watercooler-cloud/watercooler_search
   ```

3. **Build and execute search** (use `jq` for safe JSON — see Argument Safety above):
   ```bash
   mcp-cli call watercooler-cloud/watercooler_search "$(jq -n \
     --arg q "<search text>" \
     '{query: $q, mode: "entries", filters: {<only include specified filters>}}')"
   ```

4. **Present results**:
   - Show matching entries with context
   - Include entry metadata (role, type, timestamp)
   - Link to full entries for deeper reading

5. **Handle empty results**:
   - Suggest relaxing filters
   - Try alternative search terms

## Example Invocations

- `/search-threads role:planner config` - Planner entries about config
- `/search-threads type:Decision after:2024-01` - Recent decisions
- `/search-threads thread:mcp-migration status:OPEN` - Search in specific thread
- `/search-threads agent:Claude architecture` - Claude's architecture discussions
