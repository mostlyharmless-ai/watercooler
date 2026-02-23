---
name: serena-init
description: Read Serena initial instructions and activate the current project. Use when starting a session that needs Serena's semantic code tools.
allowed-tools:
  - ToolSearch
  - mcp__plugin_serena_serena__initial_instructions
  - mcp__plugin_serena_serena__activate_project
---

# Serena Init

Read Serena's initial instructions and activate the current project directory.

## Steps

1. **Load both Serena tools**:
   ```
   ToolSearch: select:mcp__plugin_serena_serena__initial_instructions
   ToolSearch: select:mcp__plugin_serena_serena__activate_project
   ```
   These can be loaded in parallel.

2. **Read initial instructions**:
   ```
   mcp__plugin_serena_serena__initial_instructions()
   ```

3. **Activate current project** (use the current working directory, i.e. the output of `pwd`):
   ```
   mcp__plugin_serena_serena__activate_project(project="<current working directory>")
   ```

4. **Handle errors**: If either call fails, report the error to the user and stop. Do not proceed with partial initialization. If activation fails, the Serena plugin may be registered under a different MCP server name — check your Claude Code MCP server configuration to find the correct tool prefix.

5. **Confirm** to the user:
   - Serena instructions loaded
   - Project activated at the current directory path
   - Ready for semantic code operations

## Example Invocations

- `/serena-init` - Load instructions and activate current directory
