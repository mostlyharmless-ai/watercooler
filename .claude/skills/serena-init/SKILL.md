---
name: serena-init
description: Read Serena initial instructions and activate the current project. Use when starting a session that needs Serena's semantic code tools.
allowed-tools:
  - Bash(mcp-cli *)
  - Bash(jq *)
---

# Serena Init

Read Serena's initial instructions and activate the current project directory.

## Prerequisites

The tool paths below use `plugin_serena_serena` as the MCP plugin name. If Serena is registered under a different name in your environment, run `mcp-cli tools` to find the correct prefix.

## Steps

1. **Check initial_instructions schema** (mandatory):
   ```bash
   mcp-cli info plugin_serena_serena/initial_instructions
   ```

2. **Read initial instructions**:
   ```bash
   mcp-cli call plugin_serena_serena/initial_instructions '{}'
   ```

3. **Check activate_project schema** (mandatory):
   ```bash
   mcp-cli info plugin_serena_serena/activate_project
   ```

4. **Activate current project** (use the current working directory):
   ```bash
   PAYLOAD=$(jq -n --arg p "$(pwd)" '{project: $p}') && mcp-cli call plugin_serena_serena/activate_project "$PAYLOAD"
   ```

5. **Handle errors**: If either `mcp-cli call` fails, report the error to the user and stop. Do not proceed with partial initialization.

6. **Confirm** to the user:
   - Serena instructions loaded
   - Project activated at the current directory path
   - Ready for semantic code operations

## Example Invocations

- `/serena-init` - Load instructions and activate current directory
