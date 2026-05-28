# Configuration

Configuration reference for local/open-source Watercooler.

## Minimum viable config

Most users only need these settings. Create `~/.watercooler/config.toml` with:

```toml
# ~/.watercooler/config.toml
version = 1

[mcp]
default_agent = "Claude Code"
agent_tag = "jay"
```

Watercooler renders this as `Claude Code (jay)` in entry authors — the
parentheses are added automatically, so the `agent_tag` value itself is just
the bare tag (e.g., `"jay"`, not `"(jay)"`).

Generate an annotated version with:

```bash
watercooler config init --user
```

## Team identity convention

When multiple people use the same client type, set a unique lowercase
`agent_tag` so entries remain attributable.

Examples:

```toml
[mcp]
default_agent = "Codex"
agent_tag = "jay"
```

```toml
[mcp]
default_agent = "Codex"
agent_tag = "caleb"
```

These render as `Codex (jay)` and `Codex (caleb)` respectively.

## Local mode

This guide covers local/open-source Watercooler:

- local git-backed threads
- local MCP transport (`stdio` by default)
- baseline graph and thread operations
- local config and credentials files

Most users do not need any additional infrastructure to get started.

## Config vs credentials

| File | What it stores | Safe to commit? |
|---|---|---|
| `~/.watercooler/config.toml` | Behavior and preferences | Yes |
| `~/.watercooler/credentials.toml` | Secrets (tokens, API keys) | Never |

Project-level overrides are also supported at:

```text
.watercooler/config.toml
```

## Config commands

**Initialize config from template:**

```bash
watercooler config init --user
watercooler config init --project
```

Pass `--force` to overwrite an existing file.

**Show resolved config** (merged user + project + env vars):

```bash
watercooler config show
watercooler config show --json
watercooler config show --sources
watercooler config show --project-path /path/to/repo
```

**Validate config:**

```bash
watercooler config validate
watercooler config validate --strict
```

## Key settings by category

### `[common]` — thread location

| Key | Default | Description |
|---|---|---|
| `templates_dir` | (bundled) | Custom templates directory |
| `threads_suffix` | `"-threads"` | **Deprecated.** Legacy separate-threads-repo suffix from the pre-orphan-branch model. Ignored under the default orphan-branch layout. |
| `threads_pattern` | (derived) | **Deprecated.** Legacy full URL pattern for a separate threads repo. Ignored unless migrating from the old model. |

### `[mcp]` — server and identity

| Key | Default | Description |
|---|---|---|
| `default_agent` | `"Agent"` | Agent name shown in thread entries |
| `agent_tag` | `""` | Short lowercase tag appended to agent name |
| `threads_dir` | `""` (auto) | Explicit threads directory; leave empty for auto-discovery |
| `threads_base` | `""` (auto) | Base directory used by the legacy sibling-threads-repo fallback. Resolves to the parent of the code repo (or parent of cwd) when not set. Note: the orphan-branch worktree at `~/.watercooler/worktrees/<repo>/` is controlled by a hardcoded `WORKTREE_BASE` constant — it does NOT come from this key. |
| `transport` | `"stdio"` | **Execution-routing mode for the local MCP process.** The name is a historical artefact — it does *not* control the agent↔mcp pipe (always stdio). `stdio` = run every tool call locally (default). `http` = the server itself serves HTTP (used by the hosted Railway deployment). `proxy` / `hybrid` = local process forwards some or all tool calls to a remote hosted endpoint. See [MCP-CLIENTS.md — Hosted mode](./MCP-CLIENTS.md#hosted-mode) for the full table and the naming-overlap caveat. |
| `url` | `""` | Remote hosted endpoint URL for `proxy` or `hybrid`. Empty when `transport = "stdio"`. |
| `proxy_repo` | `""` | Repo name (`org/repo`) sent in proxy/hybrid headers when local git discovery can't find one. |
| `proxy_branch` | `""` | Branch name sent in proxy/hybrid headers (same fallback role). |
| `capability_routes` | `{}` | Per-capability route overrides for hybrid mode. See [MCP-CLIENTS.md — Capability route overrides](./MCP-CLIENTS.md#capability-route-overrides-hybrid). |
| `host` | `"127.0.0.1"` | HTTP-server bind address (only used when `transport = "http"`). |
| `port` | `3000` | HTTP-server port (only used when `transport = "http"`). |
| `auto_branch` | `true` | Auto-create threads branches for new code branches |
| `auto_provision` | `true` | Auto-create the orphan branch and worktree on first use. Set to `false` to require explicit setup. |

### `[mcp.git]` — commit identity

| Key | Default | Description |
|---|---|---|
| `author` | `""` (uses agent name) | Git commit author name |
| `email` | `"mcp@watercooler.dev"` | Git commit email |
| `ssh_key` | `""` | Path to SSH private key (empty = use default ssh-agent) |

```toml
[mcp.git]
author = "Claude Code"
email = "claude@example.com"
# ssh_key = "~/.ssh/id_ed25519"
```

### `[mcp.graph]` — baseline graph enrichment

Controls the LLM and embedding services that generate entry summaries and
semantic search vectors for the baseline graph.

**Why local-first by default.** Watercooler ships with
`auto_start_services = true`, which starts a local `llama-server` and
downloads the models it needs on first health check (~2.5 GB total —
see the Quickstart callout for the breakdown). Local-first is the default
because:

- **Privacy** — entry text never leaves your machine
- **Zero-config** — no API keys to provision before you can post an entry
- **No per-token cost** — local inference is free to run

If you'd rather use an external OpenAI-compatible endpoint, set
`auto_start_services = false` and point the `*_api_base` overrides at your
service (example below). Thread ops (`say`, `ack`, `handoff`, etc.) work
whether or not enrichment is configured.

| Key | Default | Description |
|---|---|---|
| `generate_summaries` | `true` | Generate LLM summaries for entries on write. Requires a reachable LLM service. |
| `generate_embeddings` | `true` | Generate embedding vectors for entries on write. Requires a reachable embedding service. |
| `prefer_extractive` | `false` | Use extractive summaries (no LLM) instead of generated ones. |
| `auto_detect_services` | `true` | Probe LLM/embedding service availability before each call; skip gracefully if unavailable. |
| `auto_start_services` | `true` | Auto-start a local `llama-server` when services are unavailable. On first run, downloads llama-server (~50 MB) and GGUF summarizer/embedding models (~2.5 GB combined). Set to `false` to point at an external endpoint (see examples below) or disable enrichment. |
| `summarizer_api_base` | `""` | Override URL for the summarization API. Empty = resolve from unified config. |
| `summarizer_model` | `""` | Override model identifier for summarization. Empty = resolve from unified config. |
| `embedding_api_base` | `""` | Override URL for the embedding API. Empty = resolve from unified config. |
| `embedding_model` | `""` | Override model identifier for embeddings. Empty = resolve from unified config. |
| `embedding_divergence_threshold` | `0.6` | Cosine similarity threshold below which a new entry triggers thread-summary regeneration (0.0–1.0). |

Example — disable the local 2.5 GB download and point at an external
OpenAI-compatible endpoint:

```toml
[mcp.graph]
auto_start_services = false
summarizer_api_base = "https://api.example.com/v1"
summarizer_model = "gpt-4o-mini"
embedding_api_base = "https://api.example.com/v1"
embedding_model = "text-embedding-3-small"
```

Example — turn off enrichment entirely (threads still work; summaries and
semantic search simply aren't generated):

```toml
[mcp.graph]
generate_summaries = false
generate_embeddings = false
auto_start_services = false
```

### `[mcp.sync]` — git sync behavior

Controls the async commit/push pipeline.

| Key | Default | Description |
|---|---|---|
| `async_sync` | `true` | Enable async (non-blocking) git operations. Env: `WATERCOOLER_ASYNC_SYNC`. |
| `batch_window` | `5.0` | Seconds to batch commits before pushing. Env: `WATERCOOLER_BATCH_WINDOW`. |
| `max_delay` | `30.0` | Maximum seconds before forcing a push even if the batch isn't full. |
| `max_batch_size` | `50` | Maximum entries per batch commit. |
| `max_retries` | `5` | Retry attempts for failed push operations. Env: `WATERCOOLER_SYNC_MAX_RETRIES`. |
| `max_backoff` | `300.0` | Maximum backoff between retries in seconds. Env: `WATERCOOLER_SYNC_MAX_BACKOFF`. |
| `interval` | `30.0` | Background sync interval in seconds. Env: `WATERCOOLER_SYNC_INTERVAL`. |
| `stale_threshold` | `60.0` | Seconds before considering sync stale. |

### `[mcp.logging]` — server logging

| Key | Default | Description |
|---|---|---|
| `level` | `"INFO"` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Env: `WATERCOOLER_LOG_LEVEL`. |
| `dir` | `""` | Log directory. Empty = `~/.watercooler/logs/`. Env: `WATERCOOLER_LOG_DIR`. |
| `max_bytes` | `10485760` | Rotating log file size limit (10 MB default). Env: `WATERCOOLER_LOG_MAX_BYTES`. |
| `backup_count` | `5` | Number of rotated backup files to retain. Env: `WATERCOOLER_LOG_BACKUP_COUNT`. |
| `disable_file` | `false` | Skip file logging entirely (stderr only). Env: `WATERCOOLER_LOG_DISABLE_FILE`. |

### `[mcp.service_provision]` — auto-download policy

Controls the one-time download of `llama-server` and GGUF models on first
run. See [QUICKSTART.md — Step 3](./QUICKSTART.md) for the full first-run
download breakdown.

| Key | Default | Description |
|---|---|---|
| `models` | `true` | Auto-download GGUF models from HuggingFace when unavailable. Data-only (no executable). Env: `WATERCOOLER_AUTO_PROVISION_MODELS`. |
| `llama_server` | `true` | Auto-download the `llama-server` binary from GitHub releases. Checksum-verified when available; set `WATERCOOLER_LLAMA_SERVER_VERIFY=strict` to require verification. Env: `WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER`. |

### `[mcp.http]` — HTTP server (transport = "http")

Only used when `transport = "http"` — i.e., when the MCP server is itself
serving HTTP requests (the hosted Watercooler deployment, or a
self-hosted HTTP server). Client MCP configurations are always stdio;
see [MCP-CLIENTS.md](./MCP-CLIENTS.md) for the client-side story.

| Key | Default | Description |
|---|---|---|
| `cors_origins` | `""` | Comma-separated allowed origins. Empty = allow-all (no-credentials mode); explicit = allow credentials for those origins. Env: `WATERCOOLER_CORS_ORIGINS`. |
| `max_request_size` | `1048576` | Maximum request body size in bytes (1 MB default). Env: `WATERCOOLER_MAX_REQUEST_SIZE`. |
| `request_timeout` | `30` | Request timeout in seconds. Env: `WATERCOOLER_REQUEST_TIMEOUT`. |

### `[mcp.cache]` — response cache

| Key | Default | Description |
|---|---|---|
| `backend` | `"memory"` | Cache backend: `memory` (local LRU) or `database` (hosted). Env: `WATERCOOLER_CACHE_BACKEND`. |
| `default_ttl` | `300.0` | Default cache TTL in seconds. Env: `WATERCOOLER_CACHE_TTL`. |
| `max_entries` | `10000` | Maximum entries in memory cache before LRU eviction. Env: `WATERCOOLER_CACHE_MAX_ENTRIES`. |
| `api_url` | `""` | Database-cache API URL (only used with `backend = "database"`). Env: `WATERCOOLER_CACHE_API_URL`. |

### `[mcp.hosted]` — hosted-service integration

| Key | Default | Description |
|---|---|---|
| `api_url` | `""` | Watercooler hosted API URL. Env: `WATERCOOLER_TOKEN_API_URL`. |

Hosted-mode auth secrets (HMAC v3 per-key registry,
`WATERCOOLER_SLACK_SYNC_SECRET`) are env-only (never in TOML) — see
[CONFIGURATION_HOSTED.md](./CONFIGURATION_HOSTED.md) for the full set.
The legacy `WATERCOOLER_INTERNAL_SECRET` global key was removed from
the Railway production runtime in Plan v5.1 Sprint 4 (2026-05-01) and
is no longer consulted under `WATERCOOLER_HMAC_REQUIRE_V3=enforce`.

### `[mcp.slack]` — Slack integration

Optional. All fields empty by default; notifications only fire when a
webhook URL or bot token is configured.

| Key | Default | Description |
|---|---|---|
| `webhook_url` | `""` | Slack incoming webhook URL. Env: `WATERCOOLER_SLACK_WEBHOOK`. |
| `bot_token` | `""` | Slack bot token (`xoxb-...`) for rich interactions. Env: `WATERCOOLER_SLACK_BOT_TOKEN`. |
| `app_token` | `""` | Slack app token (`xapp-...`) for socket mode. Env: `WATERCOOLER_SLACK_APP_TOKEN`. |
| `default_channel` | `""` | Default channel for notifications. Env: `WATERCOOLER_SLACK_CHANNEL`. |
| `channel_prefix` | `""` | Prefix for auto-created channels (e.g., `"wc-"`). Env: `WATERCOOLER_SLACK_CHANNEL_PREFIX`. |
| `auto_create_channels` | `true` | Auto-create a Slack channel per thread (requires `bot_token`). Set to `false` to opt out. |
| `notify_on_say` | `true` | Send notification on entry writes. |
| `notify_on_ball_flip` | `true` | Send notification when the ball changes hands. |
| `notify_on_status_change` | `true` | Send notification on thread status change. |
| `notify_on_handoff` | `true` | Send notification on explicit handoffs. |
| `min_notification_interval` | `1.0` | Minimum seconds between notifications for the same thread (rate limit). |

### `[validation]` — protocol conformance checks

Entry + commit format validation. When `fail_on_violation = true`,
violations are errors; otherwise warnings.

| Key | Default | Description |
|---|---|---|
| `on_write` | `true` | Validate when writing an entry. Env: `WATERCOOLER_VALIDATE_ON_WRITE`. |
| `on_commit` | `true` | Validate before committing. |
| `fail_on_violation` | `false` | Treat violations as errors (true) or warnings (false). Env: `WATERCOOLER_FAIL_ON_VIOLATION`. |
| `check_branch_pairing` | `true` | Verify code-branch / thread-entry pairing. |
| `check_commit_footers` | `true` | Require the canonical commit-footer fields. |
| `check_entry_format` | `true` | Verify entry format against `[validation.entry]` rules. |
| `check_status_values` | `true` | Verify `Status:` header values against the allowed set. |

#### `[validation.entry]`

| Key | Default | Description |
|---|---|---|
| `require_metadata` | `true` | Require `Agent`, `Role`, `Type` fields in every entry. |
| `allowed_roles` | `["planner", "critic", "implementer", "tester", "pm", "scribe"]` | Valid `Role:` values. |
| `allowed_types` | `["Note", "Plan", "Decision", "PR", "Closure"]` | Valid `Type:` values. |
| `require_spec_field` | `true` | Require a `Spec:` line in the entry body. |

#### `[validation.commit]`

| Key | Default | Description |
|---|---|---|
| `require_footers` | `true` | Require the canonical footer block on thread commits. |
| `required_footer_fields` | `["Code-Repo", "Code-Branch", "Code-Commit", "Watercooler-Entry-ID"]` | Required footer field names. |

### `[memory]` — semantic memory (premium)

Memory backends (Graphiti / LeanRAG) are premium capabilities. The config
schema ships in open-core for forward-compatibility, but the backend
implementations are not installed. See
[CONFIGURATION_HOSTED.md](./CONFIGURATION_HOSTED.md) for hosted-mode
memory configuration details, and `config.example.toml` for the full
nested TOML layout of `[memory.llm]`, `[memory.embedding]`,
`[memory.database]`, `[memory.graphiti]`, and `[memory.leanrag]`.

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Enable memory backends globally. Env: `WATERCOOLER_MEMORY_DISABLED` (inverted: `"1"` = disabled). |
| `backend` | `"null"` | Backend: `"graphiti"`, `"leanrag"`, or `"null"` (no-op). Env: `WATERCOOLER_MEMORY_BACKEND`. |
| `queue_enabled` | `false` | Use the async indexing queue instead of blocking on backend writes. |
| `queue_max_workers` | `1` | Concurrent indexing workers. |
| `queue_task_timeout` | `300.0` | Base per-task timeout in seconds. Retries escalate via `base * 2^(attempt-1)`, capped at `stale_timeout`. |

### `[federation]` — cross-namespace search

Federation lets one MCP server search across multiple repositories in a
single call. Disabled by default.

```toml
[federation]
enabled = true
```

See [FEDERATION.md](./FEDERATION.md) for the full schema — namespace
registration, scoring parameters, access allowlists, and timeouts.

### `[dashboard]` — dashboard UI defaults

These settings control optional dashboard UI defaults.

| Key | Default | Description |
|---|---|---|
| `default_repo` | `""` | Repo pre-selected on dashboard load |
| `default_branch` | `"main"` | Default branch for new selections |
| `poll_interval_active` | `15` | Refresh interval in seconds when tab is focused |
| `poll_interval_moderate` | `30` | Refresh interval in seconds when tab is visible but inactive |
| `poll_interval_idle` | `60` | Refresh interval in seconds when tab is hidden |
| `expand_threads_by_default` | `false` | Expand thread rows on load |
| `show_closed_threads` | `false` | Show closed threads by default |

## Environment variable reference

Environment variables override TOML settings.

### Thread and agent settings

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_AGENT` | `mcp.default_agent` | `"Agent"` | Agent name in thread entries |
| `WATERCOOLER_AGENT_TAG` | `mcp.agent_tag` | `""` | Tag appended to agent name |
| `WATERCOOLER_DIR` | `mcp.threads_dir` | (auto) | Explicit threads directory path |
| `WATERCOOLER_THREADS_BASE` | `mcp.threads_base` | (auto) | Base directory for threads repos |
| `WATERCOOLER_THREADS_PATTERN` | `common.threads_pattern` | (derived) | Full URL pattern for a legacy threads repo |
| `WATERCOOLER_AUTO_BRANCH` | `mcp.auto_branch` | `true` | Auto-create threads branches |
| `WATERCOOLER_AUTO_PROVISION` | `mcp.auto_provision` | `true` | Auto-create threads repos |
| `WATERCOOLER_MCP_TRANSPORT` | `mcp.transport` | `"stdio"` | MCP transport: `stdio`, `http`, `proxy`, or `hybrid` |
| `WATERCOOLER_MCP_URL` | `mcp.url` | `""` | Remote MCP endpoint URL for `proxy` or `hybrid` transport |
| `WATERCOOLER_CODE_REPO` | `mcp.proxy_repo` | `""` | Override repo sent to remote MCP in `proxy` or `hybrid` mode |
| `WATERCOOLER_CODE_BRANCH` | `mcp.proxy_branch` | `""` | Override branch sent to remote MCP in `proxy` or `hybrid` mode |
| `WATERCOOLER_ALLOW_LOCAL_ONLY` | _(no TOML)_ | `""` | Set to `1` to explicitly allow thread writes into a directory that is not backed by a GitHub repository. Default behavior refuses such writes with an actionable error. Threads written in local-only mode are **not pushed to any remote**. See [TROUBLESHOOTING.md#local-only-mode](./TROUBLESHOOTING.md#local-only-mode). |
| `WATERCOOLER_GITHUB_HOSTS` | _(no TOML)_ | `""` | Comma-separated allowlist of additional hostnames the write guard should treat as GitHub Enterprise (e.g. `github.acme.com,*.ghe.example`). Default behavior only trusts `github.com` and its subdomains. Each entry is either an exact hostname or a `*.suffix` pattern that matches any subdomain of the suffix. Leave unset unless you push threads to a GHE instance whose hostname doesn't match `*.github.com`. |

### Git commit identity

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_GIT_AUTHOR` | `mcp.git.author` | `""` | Git commit author name |
| `WATERCOOLER_GIT_EMAIL` | `mcp.git.email` | `"mcp@watercooler.dev"` | Git commit email |
| `WATERCOOLER_GIT_SSH_KEY` | `mcp.git.ssh_key` | `""` | Path to SSH private key |

### Authentication

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `GITHUB_TOKEN` | — | — | GitHub token for git operations |
| `GH_TOKEN` | — | — | Alternative to `GITHUB_TOKEN` |

### MCP server

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_MCP_HOST` | `mcp.host` | `"127.0.0.1"` | HTTP mode bind address |
| `WATERCOOLER_MCP_PORT` | `mcp.port` | `3000` | HTTP mode port |

### Logging

| Env var | Default | Description |
|---|---|---|
| `WATERCOOLER_LOG_LEVEL` | `"INFO"` | Log level |
| `WATERCOOLER_LOG_DIR` | `~/.watercooler/logs/` | Log file directory |
| `WATERCOOLER_LOG_DISABLE_FILE` | `false` | Set to `1` to disable file logging |

### Daemons

Watercooler includes a small set of optional local daemons in open-core mode.
They are disabled by default unless you enable them in config.

> Additional daemons (content scout, content refiner, project coordinator,
> pulse snapshot, analysis snapshot, trend snapshot, pulse report,
> compound, t2 indexer) are closed-source and not present in the
> open-core build. Their config schemas still parse, but no daemon
> registers to consume them — adding those sections is a no-op here.

| Daemon | What it does |
|---|---|
| `thread_auditor` | Scans threads for hygiene issues and missing structure |
| `sync_guard` | Detects and reports sync problems in the threads worktree |
| `decision_detector` | Scans thread activity for likely decision candidates |
| `decision_extractor` | Turns high-signal decision candidates into structured Decision entries |

Enable daemons globally, then opt in per daemon:

```toml
[mcp.daemons]
enabled = true                    # master on/off

[mcp.daemons.thread_auditor]
enabled = true

# sync_guard is the one daemon whose `enabled` defaults to true — when
# the master switch is on, it runs automatically unless you opt out.
[mcp.daemons.sync_guard]
# enabled = true

[mcp.daemons.decision_detector]
enabled = true

[mcp.daemons.decision_extractor]
enabled = true
```

> `decision_extractor` additionally requires an LLM endpoint configured
> under `[mcp.daemons.llm]` (or a per-daemon override at
> `[mcp.daemons.decision_extractor.llm]`). See
> [DAEMONS.md → LLM configuration](./DAEMONS.md#llm-configuration) for the
> full schema and a working example.

#### Per-daemon config keys

**`[mcp.daemons.thread_auditor]`**

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Opt-in; master `[mcp.daemons].enabled` must also be `true`. |
| `interval` | `300.0` | Seconds between scans. |
| `check_missing_status` | `true` | Flag threads missing a `Status:` header. |
| `check_missing_ball` | `true` | Flag threads missing a `Ball:` header. |
| `check_missing_entry_ids` | `true` | Flag entries missing their `Entry-ID` comment. |
| `check_missing_summaries` | `true` | Flag entries/threads missing graph summaries. |
| `check_stale_threads` | `true` | Flag threads with no recent activity. |
| `stale_days` | `14` | Days of inactivity before a thread is considered stale. |
| `check_classification` | `true` | Suggest directory reclassification. |
| `max_findings_per_run` | `200` | Cap findings per tick. |

**`[mcp.daemons.sync_guard]`**

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Proactive worktree parity checker — on by default (data integrity). |
| `interval` | `180.0` | Seconds between parity checks. |

**`[mcp.daemons.decision_detector]`**

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Opt-in. |
| `interval` | `300.0` | Seconds between scans. |
| `min_score` | `2` | Minimum score to report; `2` = Medium+High (84.6% precision), `4` = High only. |
| `max_findings_per_run` | `200` | Cap findings per tick. |
| `fuzzy_threshold` | `85` | rapidfuzz threshold (0 = disabled). |
| `scan_closed_threads` | `true` | Include closed threads in scanning. |
| `exclude_agents` | `["ExtractDecisionsDaemon"]` | Agent name prefixes to skip (feedback-loop prevention). |

**`[mcp.daemons.decision_extractor]`**

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Opt-in. Requires LLM configuration. |
| `interval` | `1800.0` | Seconds between extraction cycles (30 min). |
| `min_extraction_score` | `4` | Minimum detector score to attempt extraction (High tier only). |
| `max_candidates_per_tick` | `3` | Candidates processed per tick (LLM cost control). |
| `max_extractions_per_day` | `20` | Daily cap (resets at midnight UTC). |
| `max_body_chars` | `4000` | Max entry body chars sent to LLM. |
| `min_confidence` | `3` | Minimum LLM confidence (1–5) to emit a Decision entry. |
| `max_tick_duration` | `300.0` | Hard timeout per tick in seconds. |
| `max_extraction_attempts` | `3` | Per-entry cap on LLM-caused extraction failures before permanent skip. |
| `max_write_failure_attempts` | `5` | Per-entry cap on infrastructure write failures. |

**`[mcp.daemons.project_coordinator]`** — *hosted-only daemon; section
shown here because `suppression_tags` governs annotation authoring
conventions that remain visible in open-core workflows.*

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Opt-in; hosted-only daemon (section is inert in open-core builds). |
| `interval` | `600.0` | Seconds between coordination scans (10 min). |
| `max_findings_per_run` | `200` | Cap findings per tick. |
| `suppression_tags` | `["parked", "wontfix", "deferred"]` | Thread annotation tags that soft-suppress coordinator findings. Matching tags downgrade `stalled_*` severity from `warning` to `info`; `aware_burst` and `aware_role_concentration` findings preserve their base `info` severity and acquire a `details.suppressed_by: "tag:<name>"` marker. See [TOOLS-REFERENCE.md → Tag-based suppression](./TOOLS-REFERENCE.md) for the full contract. |
| `role_complement_enabled` | `false` | Enable `connect_role_complement` detector (Phase 3d-1). Ships disabled — opt in after validating false-positive rate on your repo. |
| `role_complement_monitored_roles` | `["tester", "critic"]` | Canonical roles checked for cross-thread gaps. Must be a subset of `{planner, critic, implementer, tester, pm, scribe}`. |
| `role_complement_max_per_thread` | `3` | Max `connect_role_complement` findings emitted per source thread per tick. |
| `role_complement_pair_tag_prefix` | `"pair:"` | Thread annotation tag prefix for explicit thread pairing (e.g. tag both threads `pair:auth-rework` to make them complements). |
| `role_complement_min_role_entries_in_related` | `2` | Minimum entries in related thread B carrying the missing role for B to qualify as "actively exercising" it. |

**`[mcp.daemons.coordinator_refiner]`** — *hosted-only L2 LLM synthesis daemon.*

Reads unacknowledged `coordinator_lead` findings produced by
`project_coordinator` and emits `refined_coordinator_lead` findings under its
own producer identity. Narrative-only output: 2–4 sentence `assessment` plus
1–2 sentence `recommended_next_step`. `suggested_action` and `t2_context` are
passed through verbatim from the source lead — the refiner does not rewrite
them. Findings-only posture: no thread writes, no annotations on source leads,
no cross-daemon state mutation.

Production execution is hosted via `HostedDaemonCoordinator`. Local
execution registers when `coordinator_refiner.enabled = true` in
config.toml, but the in-process `tick()` skips when the server runs in
hosted mode (`WATERCOOLER_MODE=hosted`) — the refiner does not yet
support hosted per-scope execution.

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Opt-in; hosted-only daemon (section is inert in open-core builds). |
| `interval` | `600.0` | Seconds between refinement ticks (≥ 60). Matches `project_coordinator.interval` by default so refinement cadence aligns with lead production. |
| `max_leads_per_tick` | `5` | Cap refinements per tick to bound LLM cost. Raise during evaluation for faster convergence against a backlog. |
| `cursor_gc_interval` | `24` | Prune the progressive cursor (`extras["refined_lead_ids"]`) of stale source-lead ids every N ticks. Cosmetic; does not affect refined output. |
| `llm_max_tokens` | `512` | LLM response cap — narrative output is short. Raise only if live output shows truncation. |
| `llm_temperature` | `0.3` | Synthesis temperature — low; not a creative task. |
| `llm_timeout_seconds` | `30` | Per-lead LLM call timeout. |

Query refined findings with
`watercooler_daemon_findings(daemon="coordinator_refiner")`. Refined and raw
`coordinator_lead` findings acknowledge independently: acking a raw lead does
not ack its refined finding and vice versa.

**`[mcp.daemons.llm]`** — shared LLM fallback for daemons that accept it
(content_refiner, decision_extractor):

| Key | Default | Description |
|---|---|---|
| `api_base` | `""` | LLM API base URL. Empty falls through to the baseline graph LLM config. |
| `model` | `""` | LLM model name. Empty falls through. |
| `timeout` | `None` | Request timeout in seconds (None falls through). |
| `max_tokens` | `None` | Max tokens in LLM response (None falls through). |

> API keys for daemon LLM calls belong in `~/.watercooler/credentials.toml`
> under the provider's section — never in `config.toml`.

Inspect daemon state and findings with:

```python
watercooler_daemon_status()
watercooler_daemon_findings(daemon="thread_auditor")
watercooler_daemon_findings(daemon="decision_detector")
```

## Precedence rules

Later sources override earlier ones, on a per-key basis:

1. Built-in defaults
2. User config: `~/.watercooler/config.toml`
3. Project config: `<project>/.watercooler/config.toml`
4. Environment variables

To see which config files are active and in what order, run:

```bash
watercooler config show --sources
```

## Custom roles (`.watercooler/roles.toml`)

Watercooler ships six canonical roles (`planner`, `critic`, `implementer`,
`tester`, `pm`, `scribe`). Projects can extend or override these by creating
`.watercooler/roles.toml` in the repository root.

### Field reference

| Field | Recommended | Type | Description |
|---|---|---|---|
| `description` | strongly recommended | string | One-line summary of what this role does; defaults to `""` if omitted |
| `canonical_role` | strongly recommended | string | Should be one of: `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`. Documents the canonical mapping; defaults to the role name if omitted |
| `produces` | strongly recommended | list | Entry types this role typically writes; defaults to `[]` if omitted |
| `boundary` | recommended | string | What this role explicitly does not do |
| `handoff_to` | recommended | list | Role names this role commonly passes work to |
| `instructions` | recommended | string | Behavioral guidance for agents wearing this role |
| `entry_style` | optional | string | Style guidance for entry body format |
| `when_to_use` | optional | string | Conditions under which to choose this role |
| `collaborate_with` | optional | string | Which other roles this role works alongside |

The only enforced constraint is that the role name exists in the active role set.
Missing fields default to empty strings or lists, so include `description`,
`canonical_role`, and `produces` to make the role useful and discoverable.

### Minimal example

```toml
# .watercooler/roles.toml

[roles.security-audit]
description    = "Review code and configs for security vulnerabilities"
canonical_role = "critic"
produces       = ["Note", "Decision"]
boundary       = "Does not implement fixes — hands off to implementer."
handoff_to     = ["implementer", "pm"]
instructions   = """
Focus on input validation, authentication, authorization, secrets handling,
and dependency risks. Cite exact file paths and line numbers.
"""
```

### Verifying custom roles

```python
watercooler_roles(code_path=".")                          # catalog
watercooler_roles(code_path=".", role="security-audit")   # one role's full spec
```

Or via CLI:

```bash
watercooler config validate
```
