# MCP clients

Connect watercooler to Claude Code, Codex, or Cursor. Each section is fully self-contained —
no cross-references between sections.

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
claude mcp add --transport stdio watercooler --scope user \
  -- uvx --from 'git+https://github.com/mostlyharmless-ai/watercooler@main[local]' watercooler-mcp
```

This adds the server to your user-level Claude Code config. Restart Claude Code after
running.

**Config file location** (for manual edits):

- macOS/Linux: `~/.claude.json`
- Windows: `%USERPROFILE%\.claude.json`

**Manual config block** (if you prefer to edit directly):

If `~/.claude.json` already exists with other MCP servers, add only the
`"watercooler"` block inside the existing `"mcpServers"` object — do not
replace the whole file.

```json
{
  "mcpServers": {
    "watercooler": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler@main[local]",
        "watercooler-mcp"
      ]
    }
  }
}
```

**Verify the connection:** In Claude Code, call `watercooler_health`. You should see a
status report. If the tool is not found, restart Claude Code and try `/mcp` to check that
`watercooler` is listed.

**Logs:** `~/.claude/logs/` (check `mcp-*.log` for server startup errors).

---

## Codex (OpenAI)

**One-liner setup:**

```bash
codex mcp add watercooler \
  -- uvx --from 'git+https://github.com/mostlyharmless-ai/watercooler@main[local]' watercooler-mcp
```

**Config file location:**

- macOS/Linux: `~/.codex/config.toml`
- Windows: `%USERPROFILE%\.codex\config.toml`

**Manual config block:**

```toml
[mcp_servers.watercooler]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/mostlyharmless-ai/watercooler@main[local]",
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
add the `watercooler` block inside the existing `mcpServers` object.

**Config block:**

```json
{
  "mcpServers": {
    "watercooler": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler@main[local]",
        "watercooler-mcp"
      ]
    }
  }
}
```

Restart Cursor after saving.

**Verify the connection:** In Cursor's MCP panel (Settings → MCP), confirm
`watercooler` shows a green status. Then call `watercooler_health` to check the
server internals.

**Logs:** Cursor's Output panel → select `MCP` from the dropdown for server startup logs.

---

## Hosted mode

The configurations above use **local mode** (stdio transport, local git). For teams using
the hosted control plane, use **proxy mode** (recommended) or direct HTTP.

### Proxy mode (recommended)

Proxy mode forwards all tool calls through the local MCP server to the remote endpoint.
Agent config stays unchanged (stdio) — no per-agent Bearer tokens or URL changes needed.

**One-time setup:**

1. Log into the dashboard → **Settings → Security → Agent API Keys → Create Key**
2. Copy the `wc_...` key (shown once)
3. Add to `~/.watercooler/credentials.toml`:

```toml
[hosted]
api_key = "wc_..."
```

4. Add to `~/.watercooler/config.toml`:

```toml
[mcp]
transport = "proxy"
url = "https://your-mcp-host.example.com/mcp/"
```

5. Restart MCP server — all agents connect via their existing stdio config

No local services (llama-server, FalkorDB) start in proxy mode. To switch back to
local mode, set `transport = "stdio"` (or remove the line).

**Note:** The proxy URL uses the `/mcp/` path — this is the full tool surface where all
calls are forwarded to the remote endpoint.

### Hybrid mode

Hybrid mode runs thread and baseline graph tools locally while routing premium
capabilities (memory query, memory observe, memory ingest) to the hosted endpoint.
Unlike proxy mode, hybrid **starts local services** (llama-server, FalkorDB) for
baseline search and graph operations.

**Setup:**

1. Complete the credentials step from proxy mode above (`~/.watercooler/credentials.toml`)
2. Add to `~/.watercooler/config.toml`:

```toml
[mcp]
transport = "hybrid"
url = "https://your-mcp-host.example.com/mcp/premium/"
```

The hybrid URL uses `/mcp/premium/` (the premium-only surface), not `/mcp/`.

Alternatively, set the env var `WATERCOOLER_MCP_TRANSPORT=hybrid`.

**Capability route overrides (optional):**

The default routing sends `threads_core`, `baseline_search`, `diagnostics`, and other
core capabilities to `local`, while `memory_query`, `memory_observe`, and
`memory_ingest` go to `remote`. You can override individual routes in config:

```toml
[mcp.capability_routes]
memory_query = "local"       # run memory queries locally instead of remote
federation_search = "remote" # route federated search to the hosted endpoint
```

### Remote repo context (proxy and hybrid)

When running in an environment without local git context (e.g., a cloud sandbox or
container), the MCP server cannot auto-detect the repository. Set these in
`~/.watercooler/config.toml`:

```toml
[mcp]
proxy_repo = "org/your-repo"
proxy_branch = "main"
```

Or via environment variables: `WATERCOOLER_CODE_REPO` and `WATERCOOLER_CODE_BRANCH`.

### Direct HTTP (alternative)

If you prefer agents to connect directly to the hosted endpoint (without the local
proxy), configure each agent's MCP client with HTTP transport:

### Claude Code (hosted, direct HTTP)

```json
{
  "mcpServers": {
    "watercooler": {
      "type": "http",
      "url": "https://your-mcp-host.example.com/mcp",
      "headers": {
        "Authorization": "Bearer wc_YOUR_KEY_FROM_CREDENTIALS_TOML",
        "X-Repo": "org/your-repo",
        "X-Branch": "main"
      }
    }
  }
}
```

### Codex (hosted, direct HTTP)

```toml
[mcp_servers.watercooler]
type = "http"
url = "https://your-mcp-host.example.com/mcp"

[mcp_servers.watercooler.headers]
Authorization = "Bearer wc_YOUR_KEY_FROM_CREDENTIALS_TOML"
X-Repo = "org/your-repo"
X-Branch = "main"
```

### Cursor (hosted, direct HTTP)

```json
{
  "mcpServers": {
    "watercooler": {
      "type": "http",
      "url": "https://your-mcp-host.example.com/mcp",
      "headers": {
        "Authorization": "Bearer wc_YOUR_KEY_FROM_CREDENTIALS_TOML",
        "X-Repo": "org/your-repo",
        "X-Branch": "main"
      }
    }
  }
}
```

### Verifying hosted connection

After configuring, call `watercooler_health` from your client. The response should show
`"mode": "hosted"` and `"token_service": {"configured": true}`.

### Switching between modes

For **direct HTTP** vs **local stdio**: these are separate MCP server entries in your
client config — switch by enabling/disabling the appropriate entry.

For **proxy** and **hybrid** modes: switching happens in `~/.watercooler/config.toml`
(change `transport` between `"stdio"`, `"proxy"`, or `"hybrid"`), not in the MCP client
config. The client always sees the same stdio server.

### Webhook setup

When you connect a repo in the dashboard, a GitHub webhook is created automatically.
No manual webhook configuration is needed. The webhook syncs thread changes to the
dashboard in real time.
