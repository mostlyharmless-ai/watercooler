---
name: recall
description: Recall project context or answer questions about history and decisions. Use before starting work, when investigating unfamiliar code, or asking "What was decided about X?" / "Why did we choose Y?"
allowed-tools:
  - ToolSearch
  - mcp__watercooler-cloud__watercooler_smart_query
---

# Recall

Query: $ARGUMENTS

## Framing Detection

After stripping flags (e.g. `--raw`), inspect the remaining text:

- **Question framing** if text ends with `?` OR starts with a question word:
  What, Why, How, Who, When, Where, Which, Is, Are, Was, Were, Did, Does, Can, Should
- **Context framing** otherwise (topic or task description)

## Steps

1. **Load MCP tool**:
   ```
   ToolSearch: select:mcp__watercooler-cloud__watercooler_smart_query
   ```

2. **Execute query** (scope to current repo with `code_path`):
   ```
   mcp__watercooler-cloud__watercooler_smart_query(query="<$ARGUMENTS minus flags>", code_path="<repo root>")
   ```

3. **Present results** based on framing:

   **Question framing** — answer the question:
   - **Direct answer** to the question
   - **Evidence** — cite specific entries by entry_id
   - **Confidence** and tier used (T1/T2/T3)
   - **Escalation note** if tier escalated beyond T1
   - **Suggested follow-ups** if answer is incomplete or uncertain

   **Context framing** — summarize relevant context:
   - **Prior decisions** related to this topic
   - **Relevant patterns** or implementations
   - **Known issues** or gotchas
   - **Related threads** for deeper reading
   - **Tier used** and escalation reason

4. **Handle empty results**:
   - Suggest alternative search terms
   - Note whether memory backends are configured

5. **Raw output** (if `--raw` flag present):
   - Append full JSON response after the summary

## Example Invocations

- `/recall authentication flow` — context before implementing auth
- `/recall What was decided about the config system?` — direct question
- `/recall Why did we choose markdown for threads?` — direct question
- `/recall --raw branch parity sync` — raw JSON output
- `/recall Who implemented the branch parity feature?` — attribution lookup
