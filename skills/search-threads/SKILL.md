---
name: search-threads
description: Search threads with filters. Supports filters like role:planner, type:Decision, after:2024-01, thread:topic-name, status:OPEN
allowed-tools:
  - ToolSearch
  - mcp__watercooler__watercooler_search
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

## Parsing Rules

1. Split `$ARGUMENTS` on whitespace into tokens
2. A token is a **filter** if it matches `^(role|type|after|before|thread|status|agent):[^\s]+$`
3. The filter key is everything before the first `:`, the value is everything after
4. All non-filter tokens are joined with spaces to form the **query text**

### Edge Cases

- `role:planner:advanced` → key=`role`, value=`planner:advanced` (first colon splits)
- Multiple values for same key → last one wins
- Empty value (`role:`) → ignore, treat entire token as query text
- No query text → search with filters only (empty query string)

## Steps

1. **Parse arguments** into filters and query text using rules above.

2. **Load MCP tool**:
   ```
   ToolSearch: select:mcp__watercooler__watercooler_search
   ```

3. **Execute search** with parsed query and filters. Pass each filter as its
   own named parameter — omit any filter param that was not explicitly parsed
   from the arguments. Do NOT use a `filters={}` dict (no such parameter exists):
   ```
   # Example: only role was given
   mcp__watercooler__watercooler_search(
     query="config", mode="entries", role="planner"
   )
   # Example: multiple filters
   mcp__watercooler__watercooler_search(
     query="", mode="entries", role="planner", entry_type="Decision"
   )
   # Example: thread + status filters
   mcp__watercooler__watercooler_search(
     query="", mode="entries", thread_topic="mcp-migration", thread_status="OPEN"
   )
   # Example: no filters, just query text
   mcp__watercooler__watercooler_search(
     query="config migration", mode="entries"
   )
   ```

   Filter parameter mapping:
   - `role:X` → `role="X"`
   - `type:X` → `entry_type="X"`
   - `thread:X` → `thread_topic="X"`
   - `status:X` → `thread_status="X"`
   - `agent:X` → `agent="X"`
   - `after:DATE` → `start_time="DATE"`
   - `before:DATE` → `end_time="DATE"`

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
