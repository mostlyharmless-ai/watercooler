---
name: threads
description: List and navigate watercooler threads. Use to see active discussions, find specific threads, or get an overview of project threads.
allowed-tools:
  - ToolSearch
  - mcp__watercooler-cloud__watercooler_list_threads
  - mcp__watercooler-cloud__watercooler_read_thread
---

# List and Navigate Threads

Arguments: $ARGUMENTS

## Modes

- **No arguments**: List all threads grouped by status
- **`open`** or **`closed`**: Filter by status
- **`<topic>`**: Show specific thread details
- **`--raw`**: Show full JSON response

## Steps

### Mode 1: List All Threads (no arguments or status filter)

1. **Load tool**:
   ```
   ToolSearch: select:mcp__watercooler-cloud__watercooler_list_threads
   ```

2. **Execute**:
   ```
   mcp__watercooler-cloud__watercooler_list_threads()
   ```

3. **Present results**:
   - Group by status (OPEN first, then CLOSED)
   - Show: topic, title, last activity, ball holder
   - Highlight actionable threads (where Claude has the ball)

### Mode 2: Show Specific Thread (topic provided)

1. **Load tool**:
   ```
   ToolSearch: select:mcp__watercooler-cloud__watercooler_read_thread
   ```

2. **Execute** (use `summary_only=true` to keep response under ~500 tokens):
   ```
   mcp__watercooler-cloud__watercooler_read_thread(topic="<topic>", summary_only=true)
   ```

3. **Present**:
   - Thread metadata
   - Recent entries summary
   - Current ball holder
   - Offer to fetch full entries if the user needs more detail

### Mode 3: Filter by Status

1. Parse status from arguments (case-insensitive: `open`/`OPEN`, `closed`/`CLOSED`)
2. List all threads using `watercooler_list_threads` (same as Mode 1)
3. Filter results client-side by matching the thread `status` field
4. Present filtered list using the same format as Mode 1

### `--raw` Flag

When `--raw` is present (in any mode), show the full JSON response after the
human-readable summary. This is consistent with the `recall` skill behavior.

## Example Invocations

- `/threads` - List all threads
- `/threads open` - Show only open threads
- `/threads mcp-migration` - Show specific thread details
- `/threads --raw` - Full JSON output
