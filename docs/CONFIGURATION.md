# Configuration

## Minimum viable config

Most users only need these settings. Create `~/.watercooler/config.toml` with:

```toml
# ~/.watercooler/config.toml
version = 1                       # schema version; do not modify

[mcp]
default_agent = "Claude Code"     # your MCP client name (usually auto-detected)
agent_tag = "(jay)"               # optional: appended to agent name in thread entries
```

Generate an annotated version with `watercooler config init --user`.

---

## Team identity convention

When multiple people use the same client type, set a unique lowercase `agent_tag` for
each person so entries remain attributable.

Examples:

```toml
[mcp]
default_agent = "Codex"
agent_tag = "(jay)"      # entry author shows as "Codex (jay)"
```

```toml
[mcp]
default_agent = "Codex"
agent_tag = "(caleb)"    # entry author shows as "Codex (caleb)"
```

---

## Product mode

Watercooler operates in one of two modes, configured via the `mode` key or `WATERCOOLER_MODE`
environment variable:

| Mode | Default | Typical transport | Memory tiers | Auth | Description |
|---|---|---|---|---|---|
| `local` | Yes | stdio | T1 only | Local git credentials | Open-core. Works with zero hosted infrastructure. |
| `hosted` | No | HTTP | T1/T2/T3 | Token service + HMAC | Full control plane. Requires hosted infrastructure. |

> **Note:** Mode and transport are orthogonal. For example, `mode = "local"` can be
> combined with `transport = "hybrid"` to keep threads local while routing memory
> queries to a remote hosted service. The table above shows typical pairings, not
> hard requirements.

```toml
# ~/.watercooler/config.toml
mode = "local"   # or "hosted"
```

Or via environment variable (takes precedence over TOML):

```bash
export WATERCOOLER_MODE=hosted
```

### Local mode (open-core, Apache 2.0)

Default mode. All T1 features work out of the box:
- Thread CRUD (say, ack, handoff, set_status)
- Baseline graph (JSON source of truth)
- Search (keyword + semantic with local embeddings)
- Annotations (tags, flags, pins, reactions)
- Graph enrichment (summaries, embeddings)

Requires: local git credentials only.

### Hosted mode (control plane)

Full-featured mode for multi-user teams with a hosted control plane:
- Everything in local mode, plus:
- T2 memory (Graphiti knowledge graph)
- T3 memory (LeanRAG hierarchical clusters)
- HTTP MCP transport with HMAC auth
- Agent API key authentication
- Per-user rate limiting
- Token service integration

Requires: `WATERCOOLER_TOKEN_API_URL`, `WATERCOOLER_TOKEN_API_KEY`, `WATERCOOLER_INTERNAL_SECRET`.

### Proxy mode (recommended for hosted)

Proxy mode is the simplest way to connect agents to the hosted control plane.
The local MCP server reads the API key from `credentials.toml` and forwards all
tool calls to the remote endpoint. Agent config stays unchanged (stdio) — no
per-agent Bearer tokens or URL changes needed.

```toml
# ~/.watercooler/config.toml
[mcp]
transport = "proxy"
url = "https://<your-hosted-url>/mcp/"
```

```toml
# ~/.watercooler/credentials.toml
[hosted]
api_key = "wc_..."
```

No local services (llama-server, FalkorDB) start in proxy mode.

### Hybrid mode (local threads + remote premium)

Hybrid mode runs thread and baseline graph operations locally while routing
premium capabilities (memory query, T2/T3 indexing) to the hosted service.
Local services (llama-server, FalkorDB for baseline) **do** start.

```toml
# ~/.watercooler/config.toml
[mcp]
transport = "hybrid"
url = "https://<your-hosted-url>/mcp/premium"

# Optional: override capability routes
[mcp.capability_routes]
# memory_query = "remote"     # default for hybrid
# memory_admin_graph = "disabled"    # default for hybrid
# memory_admin_cluster = "disabled"  # default for hybrid
# baseline_search = "local"   # default for hybrid
```

```toml
# ~/.watercooler/credentials.toml
[hosted]
api_key = "wc_..."
```

**Capability routing**: Each capability can be routed to `auto`, `local`,
`remote`, or `disabled`. The defaults are tuned so threads and baseline
search stay local while memory operations go remote. Admin and migration
capabilities are disabled by default in hybrid mode.

All 14 capability IDs and their hybrid-mode defaults:

| Capability ID | Hybrid default | Description |
|---|---|---|
| `threads_core` | `local` | Thread CRUD (say, ack, handoff, read, list) |
| `thread_state_admin` | `local` | Thread admin (delete, archive) |
| `baseline_search` | `local` | Keyword and semantic search over baseline graph |
| `semantic_similarity` | `local` | Find-similar entry lookups |
| `baseline_maintenance` | `local` | Graph enrichment, projection, recovery |
| `annotation_admin` | `local` | Annotation management |
| `memory_query` | `remote` | Smart query and memory search (T2/T3) |
| `memory_observe` | `remote` | Observe/index new episodes into memory |
| `memory_ingest` | `remote` | Bulk ingest and reindex operations |
| `memory_admin_graph` | `disabled` | Graph group clearing |
| `memory_admin_cluster` | `disabled` | Cluster-level memory admin |
| `memory_migration` | `disabled` | Cross-backend migration operations |
| `daemon_observe` | `local` | Read daemon findings and status |
| `daemon_control` | `local` | Start/stop/configure daemons |
| `federation_search` | `local` | Cross-repo federated search |
| `diagnostics` | `local` | Health checks and diagnostics |

---

## Config vs credentials

| File | What it stores | Safe to commit? |
|---|---|---|
| `~/.watercooler/config.toml` | Behavior and preferences | Yes |
| `~/.watercooler/credentials.toml` | Secrets (tokens, API keys) | Never |

Both files are TOML. The config file is also supported at project level:
`.watercooler/config.toml` (inside your repo, for per-project overrides).

**Hosted mode credentials:** Add `[hosted].api_key` to `credentials.toml` for
agent API key auth. This is a per-user Bearer token (`wc_...`) generated from the
dashboard Settings → Security → Agent API Keys. All agents on the machine read it
automatically.

```toml
# ~/.watercooler/credentials.toml
[hosted]
api_key = "wc_..."
```

---

## Config commands

**Initialize config from template:**

```bash
watercooler config init --user      # creates ~/.watercooler/config.toml
watercooler config init --project   # creates .watercooler/config.toml in current dir
```

Pass `--force` to overwrite an existing file.

After creating your user config, wire up the PostCompact capture hook so that
Project Pulse session themes are captured automatically:

```bash
watercooler setup-pulse-hook
```

This registers `watercooler-capture-theme` as a `PostCompact` hook in
`~/.claude/settings.json`. Restart your Claude Code session after running.

**Show resolved config** (merged user + project + env vars):

```bash
watercooler config show
watercooler config show --json                    # machine-readable output
watercooler config show --sources                 # show which config files are active and loaded
watercooler config show --project-path /path/to/repo   # check config for another project
```

**Validate config** (check for errors or warnings):

```bash
watercooler config validate
watercooler config validate --strict    # treat warnings as errors
```

---

## Key settings by category

### `[common]` — thread location

| Key | Default | Description |
|---|---|---|
| `templates_dir` | (bundled) | Custom templates directory |
| `threads_suffix` | `"-threads"` | **Legacy.** Suffix for a separate threads repo. Silently ignored in the default orphan-branch setup — only needed when migrating from the old model. |
| `threads_pattern` | (derived) | **Legacy.** Full URL pattern for a separate threads repo. Silently ignored unless `threads_suffix` is also set. |

### `[mcp]` — server and identity

| Key | Default | Description |
|---|---|---|
| `default_agent` | `"Agent"` | Agent name shown in thread entries |
| `agent_tag` | `""` | Short lowercase tag appended to agent name, e.g. `"(jay)"` |
| `threads_dir` | (auto) | Explicit threads directory; leave empty for auto-discovery |
| `transport` | `"stdio"` | Transport mode: `stdio`, `http`, `proxy`, or `hybrid` |
| `url` | `""` | Remote endpoint URL (required for `proxy` and `hybrid` transport) |
| `proxy_repo` | `""` | Override code repo sent to the remote in proxy/hybrid mode |
| `proxy_branch` | `""` | Override code branch sent to the remote in proxy/hybrid mode |
| `auto_branch` | `true` | Auto-create threads branches for new code branches |

### `[mcp.git]` — commit identity

Controls the git author for thread commits:

| Key | Default | Description |
|---|---|---|
| `author` | `""` (uses agent name) | Git commit author name |
| `email` | `"mcp@watercooler.dev"` | Git commit email |
| `ssh_key` | `""` | Path to SSH private key (empty = use default ssh-agent) |

```toml
[mcp.git]
author = "Claude Code"
email = "claude@example.com"
# ssh_key = "~/.ssh/id_ed25519"   # optional; omit to use ssh-agent default
```

### `[memory]` — enhanced search features

Enable persistent memory and semantic search across sessions (optional):

```toml
[memory]
backend = "graphiti"   # or "leanrag" for local-only setup
```

See [Memory backend](#memory-backend) below for full setup instructions.

### `[dashboard]` — dashboard UI defaults

These settings are only used by the separate `watercooler-site` dashboard.

| Key | Default | Description |
|---|---|---|
| `default_repo` | `""` | Repo pre-selected on dashboard load |
| `default_branch` | `"main"` | Default branch for new selections |
| `poll_interval_active` | `15` | Refresh interval in seconds when tab is focused |
| `poll_interval_moderate` | `30` | Refresh interval in seconds when tab is visible but inactive |
| `poll_interval_idle` | `60` | Refresh interval in seconds when tab is hidden |
| `expand_threads_by_default` | `false` | Expand thread rows on load |
| `show_closed_threads` | `false` | Show closed threads by default |

For self-hosted dashboard setup and credential-helper details, see
[DASHBOARD.md](./DASHBOARD.md).

---

## Memory backend

Watercooler's baseline features work with zero additional configuration. The memory
backend is an optional upgrade that adds persistent memory and semantic search across
sessions.

To enable:

```toml
[memory]
backend = "graphiti"     # cloud LLM provider (OpenAI, Anthropic, etc.)
# or
backend = "leanrag"      # local-only, no external API required
```

Credentials for LLM and embedding providers go in `~/.watercooler/credentials.toml`,
using a provider-named section:

```toml
[openai]
api_key = "sk-..."

# or for Anthropic:
[anthropic]
api_key = "sk-ant-..."
```

The model and endpoint are set in `config.toml` under `[memory.llm]` and `[memory.embedding]`
(see `watercooler config init --user` for an annotated template). Supported providers:
`openai`, `anthropic`, `groq`, `voyage`, `google`.

For a local (no-API) setup, point both `api_base` fields at a local `llama-server`
or `ollama` endpoint.

---

## Environment variable reference

Environment variables override all config file settings. Format: set in shell or pass
to the MCP server's `env` block in your client config.

### Thread and agent settings

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_AGENT` | `mcp.default_agent` | `"Agent"` | Agent name in thread entries |
| `WATERCOOLER_AGENT_TAG` | `mcp.agent_tag` | `""` | Tag appended to agent name |
| `WATERCOOLER_DIR` | `mcp.threads_dir` | (auto) | Explicit threads directory path |
| `WATERCOOLER_THREADS_BASE` | `mcp.threads_base` | (auto) | Base directory for threads repos |
| `WATERCOOLER_THREADS_PATTERN` | `common.threads_pattern` | (derived) | Full URL pattern for threads repo |
| `WATERCOOLER_AUTO_BRANCH` | `mcp.auto_branch` | `true` | Auto-create threads branches |
| `WATERCOOLER_AUTO_PROVISION` | `mcp.auto_provision` | `true` | Auto-create threads repos |
| `WATERCOOLER_CODE_REPO` | — | (auto) | Override code repo detection. Required in proxy/hybrid mode when auto-detection is unavailable (e.g. containerized agents). |
| `WATERCOOLER_CODE_BRANCH` | — | (auto) | Override code branch detection. Same proxy/hybrid caveat as `CODE_REPO`. |
| `WATERCOOLER_MCP_TRANSPORT` | `mcp.transport` | `"stdio"` | Transport mode: `stdio`, `http`, `proxy`, or `hybrid` |
| `WATERCOOLER_MCP_URL` | `mcp.url` | `""` | Remote endpoint URL (proxy/hybrid transport) |

### Git commit identity

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_GIT_AUTHOR` | `mcp.git.author` | `""` (uses agent name) | Git commit author name |
| `WATERCOOLER_GIT_EMAIL` | `mcp.git.email` | `"mcp@watercooler.dev"` | Git commit email |
| `WATERCOOLER_GIT_SSH_KEY` | `mcp.git.ssh_key` | `""` | Path to SSH private key |

### Product mode

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_MODE` | `mode` | `"local"` | Product mode: `local` (open-core) or `hosted` (control plane) |

### Authentication

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `GITHUB_TOKEN` | — | — | GitHub token for git operations (or `GH_TOKEN`) |
| `GH_TOKEN` | — | — | Alternative to `GITHUB_TOKEN`; same precedence |
| `WATERCOOLER_AUTH_MODE` | — | `"local"` | **Deprecated.** Use `WATERCOOLER_MODE` instead. |
| `WATERCOOLER_TOKEN_API_URL` | — | — | Token API URL (hosted mode only) |
| `WATERCOOLER_TOKEN_API_KEY` | — | — | Token API key (hosted mode only) |
| `WATERCOOLER_INTERNAL_SECRET` | — | — | HMAC signing secret for hosted mode request auth |

### Hosted mode settings

| Env var | Default | Description |
|---|---|---|
| `WATERCOOLER_RATE_LIMIT_RPM` | `0` (disabled) | Per-user requests per minute limit for `/mcp` endpoint |
| `WATERCOOLER_HMAC_WINDOW` | `300` | Max age (seconds) for HMAC v2 timestamps. Requests older than this are rejected. |
| `WATERCOOLER_STALE_EXTENSION_FACTOR` | `6` | Stale-while-revalidate multiplier on TOKEN_CACHE_TTL. During outages, cached tokens are served for up to TTL x this factor (default 30 min). |
| `WATERCOOLER_CB_FAILURE_THRESHOLD` | `5` | Token service circuit breaker: failures before opening |
| `WATERCOOLER_CB_RECOVERY_TIMEOUT` | `60.0` | Token service circuit breaker: seconds before half-open |
| `WATERCOOLER_TOKEN_CACHE_TTL` | `300` | Token cache TTL in seconds |

### Memory and search

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_MEMORY_BACKEND` | `memory.backend` | `"null"` | Memory backend: `graphiti`, `leanrag`, or `null` — T2/T3 won't activate without credentials/services regardless |
| `WATERCOOLER_MEMORY_QUEUE` | `memory.queue_enabled` | `false` | Enable async memory indexing |
| `WATERCOOLER_MEMORY_DISABLED` | — | — | Set to `1` to disable memory even if configured |
| `LLM_API_KEY` | — (use `credentials.toml`) | — | LLM provider API key — prefer `[openai].api_key` etc. in `credentials.toml` |
| `LLM_API_BASE` | `memory.llm.api_base` | — | LLM endpoint URL |
| `LLM_MODEL` | `memory.llm.model` | — | LLM model name |
| `EMBEDDING_API_KEY` | — (use `credentials.toml`) | — | Embedding provider API key — prefer `[openai].api_key` etc. in `credentials.toml` |
| `EMBEDDING_API_BASE` | `memory.embedding.api_base` | — | Embedding endpoint URL |
| `EMBEDDING_MODEL` | `memory.embedding.model` | — | Embedding model name |
| `EMBEDDING_DIM` | `memory.embedding.dim` | — | Embedding dimension |
| `EMBEDDING_TIMEOUT` | `memory.embedding.timeout` | — | Embedding request timeout (seconds) |

**FalkorDB connection** (required when `WATERCOOLER_MEMORY_BACKEND=graphiti`):

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `FALKORDB_HOST` | `memory.database.host` | — | FalkorDB server hostname |
| `FALKORDB_PORT` | `memory.database.port` | `6379` | FalkorDB server port |
| `FALKORDB_USERNAME` | `memory.database.username` | — | FalkorDB username |
| `FALKORDB_PASSWORD` | `memory.database.password` | — | FalkorDB password |
| `FALKORDB_SOCKET_TIMEOUT` | `memory.database.socket_timeout` | — | Connection socket timeout (seconds) |

**Graphiti graph naming:**

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_GRAPHITI_DATABASE` | `memory.graphiti.database` | `"watercooler"` | Graph database name in FalkorDB. Must be consistent across environments sharing a FalkorDB instance. Recommended: `watercooler_cloud`. |

**LeanRAG** (when `WATERCOOLER_MEMORY_BACKEND=leanrag` or T3 is enabled):

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `LEANRAG_PATH` | `memory.leanrag.path` | — | Path to LeanRAG installation directory |
| `WATERCOOLER_LEANRAG_ENABLED` | — | — | Set to `1` to force-enable LeanRAG T3 tier, `0` to disable. Read directly via `os.getenv()`; not normalised through config_loader. |
| `WATERCOOLER_LEANRAG_DATABASE` | — | — | Override the derived LeanRAG database name. Read directly via `os.getenv()`; not normalised through config_loader. |

### MCP server

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_MCP_HOST` | `mcp.host` | `"127.0.0.1"` | HTTP mode: bind address |
| `WATERCOOLER_MCP_PORT` | `mcp.port` | `3000` | HTTP mode: port |

### Logging

| Env var | Default | Description |
|---|---|---|
| `WATERCOOLER_LOG_LEVEL` | `"INFO"` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `WATERCOOLER_LOG_DIR` | `~/.watercooler/logs/` | Log file directory |
| `WATERCOOLER_LOG_DISABLE_FILE` | `false` | Set to `1` to disable file logging |

### Daemons

Background daemons scan threads on a configurable interval. Enable globally, then
opt-in per daemon.

#### Local vs Railway daemons

Daemons are split into **local** (run in your MCP server process) and **premium**
(run on Railway via the hosted coordinator):

| Local daemons | Premium daemons (Railway) |
|---------------|--------------------------|
| `thread_auditor` | `decision_detector` |
| `content_scout` | `decision_extractor` |
| `content_refiner` | `pulse_snapshot` |
| `sync_guard` | `pulse_report` |
| `project_coordinator` | `analysis_snapshot` |
| | `trend_snapshot` |
| | `t2_indexer` |

Premium daemons are blocked from local registration even if `enabled = true` is set
in config. This prevents duplicate execution on dev machines where all modules are
present. Config overrides (interval, LLM, thresholds) for premium daemons are sent
to Railway via the `X-Daemon-Config` header.

**Dev override:** Set `WATERCOOLER_DEV_LOCAL_DAEMONS=1` to force all daemons to
register locally (for testing/development only).

#### Railway worktree (hosted mode)

In hosted mode, the daemon coordinator clones a shallow copy of the orphan branch
into `/tmp/wc-worktree/<scope>/`. Daemons read from this local filesystem clone
instead of the GitHub Contents API — identical code path to local mode. The worktree
refreshes via `git fetch` every 120 seconds (configurable). If the clone fails,
daemons fall back to the GitHub API path transparently.

#### Configuration

```toml
[mcp.daemons]
enabled = true               # global on/off for all daemons

[mcp.daemons.decision_detector]
enabled = true               # detect decision candidates in threads
interval = 300.0             # seconds between scans (default 300)
min_score = 2                # 2 = Medium+High (84.6% precision), 4 = High only (90%)
max_findings_per_run = 200   # cap per tick to prevent runaway on first scan
fuzzy_threshold = 85         # rapidfuzz threshold (0 = disabled)
scan_closed_threads = true   # include closed threads (decisions may exist there)
```

Findings appear in `watercooler_daemon_findings(daemon="decision_detector")`.

### Decision Extractor (DTE Pipeline Stage 2)

The decision extractor daemon consumes High-tier findings from the decision detector,
applies an 8-gate validity checklist via LLM, and writes structured Decision entries
back to threads. This completes the continuous DTE pipeline:

```
write activity → detect (deterministic NLP, zero LLM) → extract (LLM, writes Decisions)
```

```toml
[mcp.daemons.decision_extractor]
enabled = true
interval = 1800.0              # 30 min between extraction cycles
min_extraction_score = 4       # High tier only (score >= 4 from detector)
max_candidates_per_tick = 3    # LLM cost control
max_extractions_per_day = 20   # daily cap (resets at midnight UTC)
max_body_chars = 4000          # max entry body chars sent to LLM
min_confidence = 3             # minimum LLM confidence to emit Decision entry
max_tick_duration = 300.0      # hard timeout per tick (seconds)

# Optional: daemon-specific LLM (falls through to [mcp.daemons.llm])
# [mcp.daemons.decision_extractor.llm]
# api_base = "http://localhost:8000/v1"
# model = "qwen3-30b-a3b"
```

Extraction findings appear in `watercooler_daemon_findings(daemon="decision_extractor")`.

**Identifying daemon-written Decision entries:** All entries written by the extractor
include `[automated: decision_extractor]` as a provenance marker on line 2 of the body
(after `Spec: decision-extractor`). The agent field shows
`ExtractDecisionsDaemon (system)`. The decision detector automatically excludes these
entries from re-detection via the `exclude_agents` config.

**Prerequisites:** Requires the decision detector to be enabled and producing findings.
Requires an LLM endpoint (configured via `[mcp.daemons.llm]` or per-daemon override).

**Annotation side-effects (written atomically with each extracted Decision):**

- Source entry receives a `decision_extracted` tag — prevents re-detection on subsequent
  daemon cycles.
- Thread receives a `has_decisions` tag — allows filtering to threads with extracted decisions.
- A cross-reference annotation links the extracted Decision entry back to its source entry
  (and vice versa).

To find all threads with extracted decisions:
```python
watercooler_list_threads(code_path=".", tags="has_decisions")
```

---

### Pulse Snapshot Daemon

Maintains a cached Project Pulse snapshot in the background. Multiple consumers
(OPG P3, Project Coordinator, PulseReportDaemon) can read the snapshot via
`watercooler_pulse_snapshot` without independently computing freshness or
aggregating session threads.

**Disabled by default** — opt in via config:

```toml
[mcp.daemons.pulse_snapshot]
enabled = true
interval = 600.0          # seconds between scans (default: 600, min: 30)
session_window_days = 7   # look-back window for session themes (short window)
long_window_days = 30     # look-back window for baseline score computation; must be > session_window_days
stale_thread_days = 14    # days of inactivity before flagging a thread stale
analysis_freshness_days = 7  # days before analysis report is considered stale
```

**Findings emitted** (read via `watercooler_daemon_findings(daemon="pulse_snapshot")`):

| Category | Severity | Meaning |
|---|---|---|
| `capture_gap_no_threads` | warning | No session-context-* threads exist — PostCompactHook may not be configured |
| `capture_gap_no_window_sessions` | info | session-context-* threads exist but no sessions fall within the look-back window |
| `missing_analysis_report` | info | Analysis report was previously detected but is no longer present in `dev_docs/reports/usage-analysis/` |
| `stale_thread` | info | Thread inactive longer than `stale_thread_days` |
| `stale_analysis_input` | info | Most recent analysis report older than `analysis_freshness_days` |
| `queue_backlog` | warning | `pulse_queue.jsonl` has pending unprocessed entries |

**Reading the snapshot:**
```python
watercooler_pulse_snapshot(code_path=".")
# Returns {"status": "ok", "snapshot": {...}} when a snapshot is available
```

**Cross-process snapshot access:** When no daemon is running in the current process,
`watercooler_pulse_snapshot` falls back to the most recent on-disk checkpoint. The
response includes additional fields when served from a checkpoint:

| Field | Type | Description |
|---|---|---|
| `source` | string | `"daemon"` (live) or `"checkpoint"` (fallback read from disk) |
| `age_seconds` | number | Seconds since the snapshot was written (checkpoint reads only) |
| `enrichment_status` | string | `"available"` · `"pending"` · `"error"` · `"not_configured"` |

`enrichment_status` reflects whether the daemon's LLM analysis step completed
(`"available"`), is still in progress (`"pending"`), failed (`"error"`), or the daemon
is not configured (`"not_configured"`). Checkpoint fallback is suppressed when
`enabled = false`.

---

### Pulse Report Daemon

Posts automated daily project pulse reports to the `project-pulse-report` thread.
Reads T1 snapshot from `PulseSnapshotDaemon`, T2 analysis from `AnalysisSnapshotDaemon`,
and T3 trend signals from `TrendSnapshotDaemon`.

**Disabled by default** — opt in via config:

```toml
[mcp.daemons.pulse_report]
enabled = true
interval = 86400.0            # 24h between reports (default)
report_thread = "project-pulse-report"
report_branch = "main"
```

**LLM executive summary:** When an LLM is configured, the daemon calls
`synthesize_executive_summary()` at report time. If no LLM is configured or the
endpoint is unreachable, the report falls back to a deterministic summary silently
(no finding emitted).

```toml
# Optional: per-daemon LLM for executive summary.
# Must be configured explicitly — shared [mcp.daemons.llm] is NOT consulted.
# Omit this block entirely to use the deterministic summary fallback.
# [mcp.daemons.pulse_report.llm]
# api_base = "http://localhost:8000/v1"
# model = "deepseek-chat"
```

**Findings emitted** (read via `watercooler_daemon_findings(daemon="pulse_report")`):

| Category | Severity | Meaning |
|---|---|---|
| `pulse_snapshot_not_ready` | warning | No `pulse_snapshot` checkpoint found — `PulseSnapshotDaemon` may not be running |
| `pulse_snapshot_stale` | warning | Snapshot older than `snapshot_max_age_hours` — report skipped |
| `pulse_report_write_failed` | warning | `daemon_write_entry` returned `written=False` |

---

### `[mcp.daemons.trend_snapshot]`

Tier 3 graph volatility metrics. Queries the Graphiti knowledge graph for superseded facts and
computes a churn rate. When enabled, the supersession rate is fed into `pulse_snapshot` dimension
score computation (as `supersession_rate` for the `evidence_quality` hazard adjustment) and the
raw volatility metrics appear in the **Trend Signals** section of pulse reports.

> **Note:** `trend_snapshot` is disabled by default. Add `[mcp.daemons.trend_snapshot]`
> with `enabled = true` to your `~/.watercooler/config.toml` to activate Tier 3 trend signals.

`trend_snapshot` is independent of `pulse_snapshot`. It requires a Graphiti-backed
memory configuration (`[memory] backend = "graphiti"`). When unavailable, Tier 3
degrades gracefully to "no data" in pulse reports.

```toml
# Tier 3 graph volatility metrics (Graphiti required)
# Queries the knowledge graph for superseded facts and computes churn rate.
# Disabled by default — requires [memory] backend = "graphiti".
[mcp.daemons.trend_snapshot]
enabled = true          # set to true to activate; requires [memory] backend = "graphiti"
interval = 3600         # seconds between runs (default 1h)
query = "decided committed architecture approach design"  # semantic search query
max_facts = 50          # hard cap (GraphitiBackend maximum)
```

---

## Precedence rules

Later sources override earlier ones, on a per-key basis:

1. Built-in defaults
2. User config: `~/.watercooler/config.toml`
3. Project config: `<project>/.watercooler/config.toml`
4. Environment variables

To see which config files are active and in what order, run `watercooler config show --sources`.

---

## Tier label glossary

| Label | What it adds |
|---|---|
| T1 — Baseline | Thread graph, zero config, included with all installs. `say`, `ack`, `handoff`, `list`, `search` all work at T1. |
| T2 — Semantic memory | Persistent memory and semantic search across sessions. Requires memory backend configuration. |
| T3 — Hierarchical memory | Summarized context and full semantic graph with community detection. Requires T2 setup plus additional resources. |

---

## Custom roles (`.watercooler/roles.toml`)

Watercooler ships six canonical roles (`planner`, `critic`, `implementer`, `tester`,
`pm`, `scribe`) defined in the bundled package. Projects can extend or override these
by creating `.watercooler/roles.toml` in the repository root. Custom role definitions
merge over the bundled defaults — undefined fields fall back to the bundled version for
that role.

**When to create a custom role:** When a team has a recurring contribution type that
doesn't fit cleanly into any canonical role (e.g. `security-audit`, `data-analyst`,
`devops`). Prefer the six canonical roles for most entries — custom roles add overhead
and require explanation to new contributors.

### Field reference

| Field | Required | Type | Description |
|---|---|---|---|
| `description` | yes | string | One-line summary of what this role does |
| `canonical_role` | yes | string | Must be one of: `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`. Used for analytics rollups — custom role names are never valid here. |
| `produces` | yes | list | Entry types this role typically writes. Valid values: `Note`, `Plan`, `Decision`, `PR`, `Closure` |
| `boundary` | recommended | string | What this role explicitly does NOT do — helps agents apply the role consistently |
| `handoff_to` | recommended | list | Role names this role commonly passes work to |
| `instructions` | recommended | string | Behavioral guidance for agents wearing this mask |
| `entry_style` | optional | string | Style guidance for entry body format |
| `when_to_use` | optional | string | Conditions under which to choose this role |
| `collaborate_with` | optional | string | Which other roles this role works alongside |

**`canonical_role` is required for all custom roles.** The analytics pipeline and
daemon filtering operate on canonical role names. A custom role without `canonical_role`
will fail validation.

### Minimal example

```toml
# .watercooler/roles.toml

[roles.security-audit]
description    = "Review code and configs for security vulnerabilities"
canonical_role = "critic"   # maps to critic for analytics
produces       = ["Note", "Decision"]
boundary       = "Does not implement fixes — hands off to implementer."
handoff_to     = ["implementer", "pm"]
instructions   = """
Focus on input validation, authentication, authorization, secrets handling,
and dependency risks. Cite exact file paths and line numbers. Classify each
finding by severity (critical / high / medium / low).
"""
```

### Verifying custom roles

```python
# List all active roles (bundled + project overrides)
watercooler_roles(code_path=".")

# Get full behavioral spec for a specific role
watercooler_role_details(code_path=".", role="security-audit")
```

Or via CLI:

```bash
watercooler config validate   # validates config + role file format
```

Custom roles are available immediately after saving `.watercooler/roles.toml` — no
server restart required. `watercooler_say` and `watercooler ack` will accept the new
role name and reject any role not in the active set.

To see the full bundled role definitions (all nine fields for each canonical role),
run `watercooler_role_details(code_path=".", role="<name>")` or inspect
`src/watercooler/data/roles.toml` in the package source. Copying that file to
`.watercooler/roles.toml` in your project gives you a fully annotated starting point
for customisation.
