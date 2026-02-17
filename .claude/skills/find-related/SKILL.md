---
name: find-related
description: Find discussions and entries related to a specific topic or entry. Use to discover connected context across threads.
allowed-tools:
  - Bash(mcp-cli *)
---

# Find Related Content

Find content related to: $ARGUMENTS

## Argument Safety

When constructing JSON for `mcp-cli call`, **never** interpolate `$ARGUMENTS` directly into a JSON string.
Use `jq` for safe construction:

```bash
# For entry IDs
mcp-cli call watercooler-cloud/watercooler_find_similar "$(jq -n --arg id "$ARGUMENTS" '{entry_id: $id}')"

# For descriptions
mcp-cli call watercooler-cloud/watercooler_search "$(jq -n --arg q "$ARGUMENTS" '{query: $q, mode: "entries", semantic: true}')"
```

## Steps

1. **Determine argument type**:
   - **Entry ID**: ULID format — exactly 26 characters matching `^[0-9A-HJKMNP-TV-Z]{26}$` (Crockford base32, e.g., `01HQXYZ123ABC456DEF789GHJ`)
   - **Description**: Any other text (does not match ULID pattern)

2. **For Entry ID** - Use similarity search:

   Check schema:
   ```bash
   mcp-cli info watercooler-cloud/watercooler_find_similar
   ```

   Execute (use `jq` for safe JSON):
   ```bash
   mcp-cli call watercooler-cloud/watercooler_find_similar "$(jq -n --arg id "<ulid>" '{entry_id: $id}')"
   ```

3. **For Description** - Use semantic search:

   Check schema:
   ```bash
   mcp-cli info watercooler-cloud/watercooler_search
   ```

   Execute (use `jq` for safe JSON):
   ```bash
   mcp-cli call watercooler-cloud/watercooler_search "$(jq -n --arg q "<description>" '{query: $q, mode: "entries", semantic: true}')"
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
