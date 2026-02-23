---
name: find-related
description: Find discussions and entries related to a specific topic or entry. Use to discover connected context across threads.
allowed-tools:
  - ToolSearch
  - mcp__watercooler-cloud__watercooler_find_similar
  - mcp__watercooler-cloud__watercooler_search
---

# Find Related Content

Find content related to: $ARGUMENTS

## Steps

1. **Determine argument type**:
   - **Entry ID**: ULID format (e.g., `01HQXYZ123ABC456DEF789GHJ`) — exactly 26 characters matching `^[0-9A-HJKMNP-TV-Z]{26}$` (Crockford base32)
   - **Description**: Any other text

2. **For Entry ID** — use similarity search:

   ```
   ToolSearch: select:mcp__watercooler-cloud__watercooler_find_similar
   ```
   Then call:
   ```
   mcp__watercooler-cloud__watercooler_find_similar(entry_id="<ulid>")
   ```

3. **For Description** — use semantic search:

   ```
   ToolSearch: select:mcp__watercooler-cloud__watercooler_search
   ```
   Then call:
   ```
   mcp__watercooler-cloud__watercooler_search(query="<description>", mode="entries", semantic=true, code_path="<repo root>")
   ```

4. **Present results**:
   - Group by thread/topic
   - Show similarity scores for transparency
   - Include brief context for each match

5. **Suggest deeper exploration**:
   - Related threads to explore
   - Follow-up queries

## Example Invocations

- `/find-related 01HQXYZ123ABC456DEF789GHI` - Find entries similar to this one
- `/find-related git sync conflict resolution` - Find related discussions
- `/find-related branch parity implementation` - Discover connected context
