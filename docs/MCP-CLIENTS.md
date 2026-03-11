# MCP clients

Connect watercooler to Claude Code, Codex, or Cursor. Each section is fully self-contained —
no cross-references between sections.

**ChatGPT:** Setup is tracked in [issue #287](https://github.com/mostlyharmless-ai/watercooler/issues/287).

After connecting, run `watercooler_health` from inside your client to verify the
connection before starting any thread operations.

If multiple people on your team use the same client type, set unique lowercase
`agent_tag` values in `~/.watercooler/config.toml` so entry authors are distinguishable
(for example `Codex (jay)` and `Codex (caleb)`). See
[CONFIGURATION.md](./CONFIGURATION.md#team-identity-convention).

---

## Claude Code

**One-liner setup:**

```bash
claude mcp add --transport stdio watercooler-cloud --scope user \
  -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler-mcp
```

This adds the server to your user-level Claude Code config. Restart Claude Code after
running.

**Config file location** (for manual edits):

- macOS/Linux: `~/.claude.json`
- Windows: `%USERPROFILE%\.claude.json`

**Manual config block** (if you prefer to edit directly):

If `~/.claude.json` already exists with other MCP servers, add only the
`"watercooler-cloud"` block inside the existing `"mcpServers"` object — do not
replace the whole file.

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler@main",
        "watercooler-mcp"
      ]
    }
  }
}
```

**Verify the connection:** In Claude Code, call `watercooler_health`. You should see a
status report. If the tool is not found, restart Claude Code and try `/mcp` to check that
`watercooler-cloud` is listed.

**Logs:** `~/.claude/logs/` (check `mcp-*.log` for server startup errors).

---

## Codex (OpenAI)

**One-liner setup:**

```bash
codex mcp add watercooler-cloud \
  -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler-mcp
```

**Config file location:**

- macOS/Linux: `~/.codex/config.toml`
- Windows: `%USERPROFILE%\.codex\config.toml`

**Manual config block:**

```toml
[mcp_servers.watercooler_cloud]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/mostlyharmless-ai/watercooler@main",
  "watercooler-mcp"
]
```

**Verify the connection:** In Codex, call `watercooler_health`. If the tool is not found,
check that `uvx` is on your PATH (`which uvx`) and restart Codex.

**Logs:** Check Codex's developer console or `~/.codex/logs/` for MCP server errors.

---

## Cursor

Cursor requires manual config file editing — no one-liner CLI is available.

**Config file location:**

- macOS/Linux: `~/.cursor/mcp.json`
- Windows: `%USERPROFILE%\.cursor\mcp.json`

Create the file if it doesn't exist. If the file already exists with other MCP servers,
add the `watercooler-cloud` block inside the existing `mcpServers` object.

**Config block:**

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler@main",
        "watercooler-mcp"
      ]
    }
  }
}
```

Restart Cursor after saving.

**Verify the connection:** In Cursor's MCP panel (Settings → MCP), confirm
`watercooler-cloud` shows a green status. Then call `watercooler_health` to check the
server internals.

**Logs:** Cursor's Output panel → select `MCP` from the dropdown for server startup logs.
