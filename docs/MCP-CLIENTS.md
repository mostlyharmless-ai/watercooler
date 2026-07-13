# MCP clients

Connect watercooler to Claude Code, Codex, or Cursor. Each section is fully self-contained —
no cross-references between sections.

> This page covers manual MCP server setup. For packaged Claude Code / Codex plugins
> that bundle the MCP server with the skills in one install, see
> [PLUGINS.md](./PLUGINS.md).

After connecting, `watercooler_health` is a recommended sanity check for
diagnosing setup problems early — but it is not required. You can skip
straight to posting your first thread; thread actions don't gate on
health or on the optional local enrichment services it may start.

If multiple people on your team use the same client type, set unique lowercase
`agent_tag` values in `~/.watercooler/config.toml` so entry authors are distinguishable
(for example `Codex (jay)` and `Codex (caleb)`). See
[CONFIGURATION.md](./CONFIGURATION.md#team-identity-convention).

---

## Claude Code

**One-liner setup (macOS / Linux):**

```bash
claude mcp add --transport stdio --scope user watercooler \
  -- uvx --from 'git+https://github.com/mostlyharmless-ai/watercooler@main[local]' watercooler-mcp
```

**One-liner setup (Windows PowerShell):**

```powershell
cmd /c "claude mcp add --transport stdio --scope user watercooler -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler@main[local] watercooler-mcp"
```

> **Why `cmd /c` on Windows?** PowerShell does not pass the `--` separator correctly
> to `claude mcp add`, causing it to misparse the server command flags. Wrapping in
> `cmd /c` fixes this.

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

**Verify the connection (optional):** In Claude Code, call `watercooler_health`. You
should see a status report. If the tool is not found, restart Claude Code and try `/mcp`
to check that `watercooler` is listed. You can also skip this step and go straight
to posting your first thread.

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

**Verify the connection (optional):** In Codex, call `watercooler_health`. If the tool
is not found, check that `uvx` is on your PATH (`which uvx`) and restart Codex. You can
also skip this step and go straight to posting your first thread.

**Logs:** Check Codex's developer console or `~/.codex/logs/` for MCP server errors.
See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md#server-not-loading) for log paths across
all supported clients and the common startup failures to look for.

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

**Verify the connection (optional):** In Cursor's MCP panel (Settings → MCP), confirm
`watercooler` shows a green status. Optionally call `watercooler_health` to check
the server internals, or skip straight to posting your first thread.

**Logs:** Cursor's Output panel → select `MCP` from the dropdown for server startup logs.

---

## Hosted mode

The client configs from the sections above use **stdio** transport, pointing at the
local `watercooler-mcp` process. **That does not change when you move to hosted.** What
changes is `~/.watercooler/config.toml`, which controls whether the local process
handles tool calls itself, proxies them to a hosted endpoint, or splits work across
local and hosted.

> **⚠ Terminology note: "transport" is overloaded.**
>
> Two different things share the name "transport" and they are **not the same**:
>
> 1. **The MCP pipe** between your agent (Claude Code / Codex / Cursor) and the
>    `watercooler-mcp` process — this is *always* **stdio** today. That's the
>    `--transport stdio` you see in `claude mcp add`, and it is not configurable
>    from `config.toml`.
> 2. **The `transport` key in `~/.watercooler/config.toml`** — this controls
>    **where tool calls execute** (all local, all remote, or split). A more
>    accurate name would be "execution routing" or "mode"; we inherited
>    `transport` from an earlier design and haven't renamed it yet.
>
> So `claude mcp add --transport stdio` and `transport = "stdio"` in
> `config.toml` are unrelated settings that happen to share a word. The former
> is fixed; the latter is what the table below lets you choose.
>
> Tracking issue: see the repository GitHub issues for the naming-overlap
> cleanup proposal.

If you are new to the storage and transport model behind these modes — how the orphan
branch, worktree, and baseline graph fit together — see
[ARCHITECTURE.md](./ARCHITECTURE.md) before picking a transport.

Three modes, selected via `transport` in `~/.watercooler/config.toml` (or the
`WATERCOOLER_MCP_TRANSPORT` environment variable):

| Mode | What runs where | When to use | Tool surface |
|---|---|---|---|
| `stdio` (default) | Everything runs locally. Premium tools are not registered (no hosted endpoint configured). Local FalkorDB starts via Docker; local LLM + embedding services start. `watercooler_daemon_status` shows local daemons. | Open-source install, no hosted account. Offline / airgapped. | Open-core baseline. The private `watercooler[memory]` install adds the T2/T3 memory and premium daemon tools that register for the `local_full` surface. Call `tools/list` to see the exact set for your install. |
| `hybrid` | Threads, baseline graph, and local daemons run in the local process. Premium capabilities (memory query/observe/ingest, hosted coordinator daemons) are forwarded to the hosted endpoint. Local LLM and embedding services still start; FalkorDB does NOT start locally. **Observability splits by design**: `watercooler_health` reports local daemon state (tick counts, errors, findings), and `watercooler_daemon_status` reports the hosted daemon set — together they cover both halves. | Recommended for teams on the hosted plan. | Local baseline tools plus premium tools registered as per-call wrappers that forward to the hosted endpoint, asserting each call's repo from its `code_path` (R3 / #1063 — nothing is bare-mounted; a bare mount froze the boot repo's scope for the session). The exact set depends on the entitlements granted to your agent API key and on any `capability_routes` overrides in your config. |
| `proxy` | All tool calls forwarded to the hosted endpoint. No local services start; the local process is a thin passthrough. **Multi-repo per session (pool-routed, #1082)**: the session pins one `X-Repo` at startup (`[mcp].proxy_repo` or the boot cwd's git remote) as its default; a call whose `code_path` positively derives a *different* repo is transparently routed to that repo's pooled premium client — reads and writes — subject to the hosted ownership check (an unclaimed repo is refused per request). Calls with no `code_path`, or one that doesn't resolve to a git repo with an origin remote, stay on the pinned default. Routing failures surface as structured `proxy_route_error` results; a cross-repo call is never silently served from the pinned repo's data. | Environments without local git access, or validating the full hosted surface. | Hosted-full surface (37 tools on production today, observed via `tools/list`). Local-maintenance graph tools are held back by `graph_tools_for_surface()`, which returns `GRAPH_TOOL_NAMES - _HOSTED_EXCLUDED_GRAPH_TOOLS` — removing `watercooler_graph_enrich`, `watercooler_graph_project`, and `watercooler_sync_repair` from the hosted graph set (all three frozenset members). `watercooler_reindex` was retired in PR4b (superseded by the graph-first `watercooler_list_threads`), so `register_sync_tools` now registers no tools. |

Agent client configs stay stdio regardless of mode — the choice lives in `config.toml`,
not in each client's MCP registration.

### Tier alignment (reference)

The three modes map to the product tiers laid out in the
`dual-mode-architecture-brainstorm` Watercooler thread:

- **`stdio`** — Tier 1 "Local mode" (open-core, Apache 2.0). Runs with no
  Railway dependency. Public `watercooler` users get the T1 baseline surface;
  the private `watercooler[memory]` install adds T2/T3 locally.
- **`hybrid`** — Tier 2 "Hybrid hosted mode" (premium, primary tier). Local T1
  plus proxied T2/T3, authenticated with a dashboard-issued agent API key.
- **`proxy`** — Tier 3 "Fully hosted" (deferred in the brainstorm, wired up
  today). Thin local shim; everything executes on Railway.

### Choosing between `hybrid` and `proxy`

Both are authenticated hosted-premium clients; they differ in where the
everyday substrate runs. Design rationale of record: Design Spec v4
(`dual-mode-architecture-brainstorm:32`).

Choose **`hybrid`** when you want:

- **Local thread operations** — reads/writes against your local git
  worktree: no network round-trip on the highest-frequency calls, works
  offline, and the thread record is a git artifact on your own disk.
- The premium T2 graph **without local infrastructure** — hybrid never
  requires Docker or FalkorDB.
- The trade: hybrid runs two local `llama-server` processes (LLM +
  embedder) for baseline enrichment and summaries. If you don't want
  *any* local services, hybrid is the wrong mode.

Choose **`proxy`** when you want:

- **Zero local services** — no llama-server, no FalkorDB, no local
  daemons; models, graph, daemons, and thread storage all execute on the
  hosted instance.
- The trade: every call (including thread reads/writes) is an HTTPS
  round-trip, and thread operations go through the hosted server's
  GitHub-API path rather than a local worktree — higher per-call
  latency, no offline operation.
- Multi-repo sessions work in both modes: proxy sessions boot pinned to
  one repo (`[mcp].proxy_repo` or the boot cwd) as their *default*, and
  a call whose `code_path` derives a different claimed repo is routed to
  that repo per call (#1091). Unclaimed repos are refused per request.

Cost note for proxy: the model work largely runs hosted in both modes;
what moves is per-entry baseline enrichment (fractions of a cent per
entry on the hosted LLM) and request/API volume on the hosted service.

### Credentials (required for `hybrid` and `proxy`)

1. Log into the dashboard → **Settings → Security → Agent API Keys → Create Key**
2. Copy the `wc_...` key (shown once)
3. Add to `~/.watercooler/credentials.toml`:

```toml
[hosted]
api_key = "wc_..."
```

### Hybrid mode

```toml
[mcp]
transport = "hybrid"
url = "https://your-mcp-host.example.com/mcp/premium/"
```

Hybrid keeps thread, baseline-graph, and local-daemon work in the local process and
forwards premium capability calls to the hosted endpoint. The URL points at
`/mcp/premium/` — the premium-only surface — not `/mcp/`. Local LLM and embedding
services still start (first-run downloads still apply); FalkorDB does not start
locally in hybrid (memory queries route to the hosted endpoint instead).

### Proxy mode

```toml
[mcp]
transport = "proxy"
url = "https://your-mcp-host.example.com/mcp/"
```

Proxy forwards every tool call to the hosted endpoint. The URL uses the `/mcp/` path
(the full tool surface). No local services start.

### Remote repo context (`hybrid` and `proxy`)

When running in an environment without local git context (e.g., a cloud sandbox or
container), the MCP server cannot auto-detect the repository. Set these in
`~/.watercooler/config.toml`:

```toml
[mcp]
proxy_repo = "org/your-repo"
proxy_branch = "main"
```

Or via environment variables: `WATERCOOLER_CODE_REPO` and `WATERCOOLER_CODE_BRANCH`.

`proxy_repo` (or the boot cwd's git remote) sets the *default* hosted
repo context only. Multi-repo sessions — e.g. launched from a parent
directory of several repos, passing an explicit `code_path` per write —
are supported: hybrid memory submissions assert each call's repo in
`X-Repo` via a per-(repo, branch) premium-client pool, and the hosted
server validates every asserted repo against the identity's connected-repo
claim. A repo that isn't connected on the dashboard fails with a
`repo_claim_mismatch` receipt rather than being silently re-scoped.

### Capability route overrides (hybrid)

Hybrid mode's default per-capability routing:

| Capability | Default route |
|---|---|
| `threads_core`, `thread_state_admin`, `annotation_admin` | `local` |
| `baseline_search`, `semantic_similarity`, `baseline_maintenance` | `local` |
| `federation_search`, `diagnostics` | `local` |
| `memory_query`, `memory_observe`, `memory_ingest` | `remote` |
| `daemon_observe`, `daemon_control` | `remote` |
| `memory_admin_graph`, `memory_admin_cluster`, `memory_migration` | `disabled` |

Override individual routes in config:

```toml
[mcp.capability_routes]
memory_query = "local"       # run memory queries locally instead of remote
federation_search = "remote" # route federated search to the hosted endpoint
```

Valid values are `"local"`, `"remote"`, and `"disabled"`. `disabled` leaves the tool
registered but refuses to execute — used as a hard off-switch for capabilities an
install should never exercise.

### Daemon observability in hybrid

Hybrid has two tools for daemon state and they answer **different** questions.
Neither is the single source of truth — each labels its own authority scope.

**`watercooler_health`** (always local, no matter the mode) reports:

- Local client state: transport, resolved capability routes, canonical identity
  (`repo_slug`, `repo_name`, `project_group_id`, `t1_database`, `t2_database`).
- Local daemons from the in-process `DaemonManager`, grouped via
  `daemon_runtime_location()` (the PR #653 API; the older `LOCAL_DAEMON_NAMES`
  export is deprecated and always returns an empty frozenset).
- Local submission-queue depth and dead letters, plus remote-handoff receipt
  summaries (post-Phase 5).
- Mismatch warnings (e.g., local FalkorDB reachable while `memory_ingest`
  resolves `remote` — a leftover-state hazard).

**`watercooler_daemon_status` / `watercooler_daemon_findings`** carry a
top-level `authority_scope` + `execution_mode` label so the tool never lies
about who it is. Three cases:

| Hybrid config | `authority_scope` | What the tool shows |
|---|---|---|
| Default (`daemon_observe = "remote"`, all premium daemons `route = "auto"` or `"hosted"`) | `hosted_premium_daemons` | Hosted premium daemons on Railway. Tool registers as a per-call forwarder (R3): `daemon_findings` / `pulse_snapshot` select the premium client from the call's `code_path`; `daemon_status` has no `code_path` and forwards on the boot client. |
| Exception A: `[mcp.capability_routes] daemon_observe = "local"` | `local_daemons_hybrid_override` | Local daemons. Hosted daemons **not** surfaced. |
| Exception B: `[mcp.daemons.<name>] route = "local"` on a premium daemon | `local_daemons_hybrid_override` | Local daemons (including the pinned premium one). The local tool implementations register instead of the remote forwarders. Hosted daemons **not** surfaced. |

**Important:** the clean split-authority contract (`watercooler_health` for
local, `watercooler_daemon_status` for hosted) requires both **default
routes**: `daemon_observe = "remote"` AND the premium daemons on their
default `auto` route. Either exception selects a local daemon tool surface
and drops hosted visibility through the tool. There is no simultaneous
local + hosted daemon-tool view under the same hybrid tool names today;
merging both views is an explicitly deferred refactor (see the code
comment at `src/watercooler_mcp/server_factory.py:424-436`).

When either exception is active, the `authority_scope` field's `note`
explains the limitation so operators aren't surprised by missing hosted
daemons in the output.

### Verifying a hosted configuration

Call `watercooler_health` from your client. The output is plain text and includes a
`Daemons:` block that lists local daemons with their state (running, stopped,
disabled, interval, tick count, findings, errors), each labelled `[local]` or
`[hosted]`. In hybrid mode you will also see a reminder line:

```
  ℹ Premium daemons run on the hosted service — use watercooler_daemon_status for full view
```

For detailed status of the hosted daemons themselves, call `watercooler_daemon_status`.
In hybrid mode this tool forwards to the hosted endpoint per call (R3) and returns the
`HostedDaemonCoordinator` view. A successful response (or a `capability_not_enabled`
JSON error returned from the hosted side) confirms hybrid routing is wired up. A
"tool not registered" error means the client did not see the premium surface — re-check
`transport` and `url` in `config.toml` and restart the MCP server.

For hosted-server diagnostics not exposed through the MCP surface (token service state,
rate limiter state, deployment profile), `curl` the hosted server's `/health` HTTP
endpoint directly — it is a separate diagnostic surface that returns JSON:

```bash
curl https://your-mcp-host.example.com/health
```

That endpoint returns JSON with `mode`, `token_service.configured`, and `rate_limit`
fields. Load balancers and dashboards hit it; agent clients do not.

### Switching modes

Edit `~/.watercooler/config.toml` and change `transport` between `"stdio"`, `"hybrid"`,
and `"proxy"`. Remove the `transport` line or set it to `"stdio"` to revert to
everything-local. Restart the local MCP server to apply.

Agent client configs (`~/.claude.json`, `~/.codex/config.toml`, `~/.cursor/mcp.json`)
do not change between modes — they always use stdio. This keeps per-agent config
stable across environment changes.

### Webhook setup

When you connect a repo in the dashboard, a GitHub webhook is created automatically —
no manual webhook configuration is needed. The webhook syncs thread changes to the
dashboard in real time.
