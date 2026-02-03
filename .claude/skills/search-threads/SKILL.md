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

## Steps

1. **Parse arguments** into filters and query text

2. **Check schema**:
   ```bash
   mcp-cli info watercooler-mcp/watercooler_search
   ```

3. **Build and execute search**:
   ```bash
   mcp-cli call watercooler-mcp/watercooler_search '{
     "query": "<search text>",
     "mode": "entries",
     "filters": {
       "role": "<if specified>",
       "type": "<if specified>",
       "topic": "<if thread: specified>",
       "after": "<if specified>",
       "before": "<if specified>"
     }
   }'
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
