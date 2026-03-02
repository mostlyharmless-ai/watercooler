# Configuration Guide

This guide explains how to configure Watercooler using TOML configuration files.

## Overview

Watercooler supports configuration through:
1. **TOML config files** (recommended for persistent settings)
2. **Environment variables** (for overrides and CI/CD)
3. **CLI arguments** (for one-off commands)

**No configuration is required to get started.** Watercooler works out-of-box with sensible defaults.

## Configuration Precedence

Settings are applied in this order (later sources override earlier):

```
Built-in defaults → User config → Project config → Environment variables → CLI args
```

| Source | Location | Scope |
|--------|----------|-------|
| Built-in defaults | Hardcoded | All projects |
| User config | `~/.watercooler/config.toml` | All projects for this user |
| Project config | `.watercooler/config.toml` | This project only |
| Environment variables | Shell/process env | Current session |
| CLI arguments | Command line | Current command |

## Quick Start

### 1. Initialize Configuration

```bash
# Create user config (recommended for personal settings)
watercooler config init

# Or create project config (for team-shared settings)
watercooler config init --project
```

### 2. View Current Configuration

```bash
# Show resolved config (all sources merged)
watercooler config show

# Show as JSON
watercooler config show --json

# Show config file locations
watercooler config show --sources
```

### 3. Validate Configuration

```bash
# Check for errors
watercooler config validate
```

## Config File Locations

### User Config (`~/.watercooler/config.toml`)

Personal settings that apply to all your projects:

```toml
# ~/.watercooler/config.toml

[mcp]
default_agent = "Claude Code"
agent_tag = "yourname"

[mcp.git]
author = "Your Name"
email = "you@example.com"

[mcp.logging]
level = "INFO"
```

### Project Config (`.watercooler/config.toml`)

Team-shared settings checked into the repository:

```toml
# .watercooler/config.toml

[mcp.sync]
interval = 60.0

[validation]
fail_on_violation = true
```

## Configuration Reference

### `[common]` Section

Shared settings for MCP and Dashboard:

```toml
[common]
# Custom templates directory (empty = use bundled)
templates_dir = ""
```

### `[mcp]` Section

MCP server settings:

```toml
[mcp]
# Transport mode: "stdio" or "http"
transport = "stdio"

# HTTP settings (only used when transport = "http")
host = "127.0.0.1"
port = 3000

# Agent identity
default_agent = "Agent"
agent_tag = ""

# Explicit threads directory (empty = auto-discover via orphan branch worktree)
threads_dir = ""
```

### `[mcp.git]` Section

Git commit settings:

```toml
[mcp.git]
author = ""                    # Empty = use agent name
email = "mcp@watercooler.dev"
ssh_key = ""                   # Path to SSH key (empty = default)
```

### `[mcp.sync]` Section

Git sync behavior:

```toml
[mcp.sync]
max_retries = 5        # Retry attempts for failed push (rebase + retry)
max_backoff = 300.0    # Maximum backoff delay (seconds)
```

### `[mcp.logging]` Section

Logging settings:

```toml
[mcp.logging]
level = "INFO"              # DEBUG, INFO, WARNING, ERROR
dir = ""                    # Log directory (empty = ~/.watercooler/logs)
max_bytes = 10485760        # 10MB per log file
backup_count = 5            # Number of backup files
disable_file = false        # Disable file logging (stderr only)
```

### `[mcp.agents]` Section

Agent-specific overrides:

```toml
[mcp.agents.claude-code]
name = "Claude Code"
default_spec = "implementer-code"

[mcp.agents.cursor]
name = "Cursor"
default_spec = "implementer-code"

[mcp.agents.codex]
name = "Codex"
default_spec = "planner-architecture"
```

### `[mcp.graph]` Section

Baseline graph generation settings (knowledge graph indexing, summarization, embeddings):

```toml
[mcp.graph]
# Generate LLM summaries when indexing thread entries into the baseline graph.
# Default: false (conservative). Enable after configuring [memory.llm].
generate_summaries = false

# Generate vector embeddings when indexing. Default: false.
# Enable after configuring [memory.embedding].
generate_embeddings = false

# Override LLM endpoint for summarization (falls back to memory.llm.api_base)
summarizer_api_base = ""
summarizer_model = ""

# Override embedding endpoint for graph embeddings (falls back to memory.embedding.api_base)
embedding_api_base = ""
embedding_model = ""

# Set to true to prefer extractive (no LLM) over LLM-based summarization
prefer_extractive = false

# Auto-detect running LLM/embedding services on startup
auto_detect_services = true

# Auto-start LLM/embedding services if not detected (requires service provisioning)
auto_start_services = false

# Cosine divergence threshold for embedding model mismatch warnings
embedding_divergence_threshold = 0.6
```

> **Note:** `generate_summaries` and `generate_embeddings` default to `false` (no LLM
> dependency by default). Enable them once you have an LLM service configured in
> `[memory.llm]` (local Ollama, OpenAI, or any OpenAI-compatible endpoint). The canonical
> `config.example.toml` shows `true` as the recommended value for production setups.

| Field | Env Var |
|-------|---------|
| `auto_start_services` | `WATERCOOLER_AUTO_START_SERVICES` |
| `embedding_divergence_threshold` | `WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD` |
| *(other fields)* | TOML-only |

### `[validation]` Section

Protocol validation settings:

```toml
[validation]
on_write = true              # Validate on write operations
on_commit = true             # Validate on commit
fail_on_violation = false    # Fail vs warn on violation
check_commit_footers = true  # Validate commit footers
check_entry_format = true    # Validate entry format
```

> **Note:** Removed config keys (e.g., `check_branch_pairing` from pre-orphan-branch versions) are
> silently ignored — no config file changes needed after upgrading.

### `[baseline_graph]` Section

Settings for the baseline graph module (free-tier knowledge graph generation).

> **Note:** The `[baseline_graph]` section is read directly by the baseline graph module — it is **not** part of the main `WatercoolerConfig` schema. Keys are parsed by `BaselineGraphConfig` in the baseline graph module, not by `config_loader.py`. Unknown keys in this section are silently ignored.

```toml
[baseline_graph]
prefer_extractive = false    # Force extractive mode (skip LLM)

[baseline_graph.llm]
api_base = "http://localhost:8000/v1"   # llama-server default
model = "qwen3:1.7b"                    # Recommended for summarization
api_key = "local"                       # Local server doesn't require key
timeout = 30.0                          # Request timeout (seconds)
max_tokens = 256                        # Max response tokens

# Prompt configuration (auto-detected from model if empty)
# system_prompt = ""          # Empty = auto-detect by model family
# prompt_prefix = ""          # Empty = auto-detect (e.g., /no_think for Qwen3)

# Few-shot example for format compliance (optional)
# summary_example_input = "Implemented OAuth2 authentication..."
# summary_example_output = "OAuth2 authentication implemented...\ntags: #auth #OAuth2"
```

**Recommended models:** `qwen3:1.7b` (fast, auto `/no_think`), `qwen2.5:3b` (quality), `llama3.2:3b` (balanced).

> **Legacy environment variables (still supported, read directly by the baseline graph module):**
> These predate the TOML config system. Prefer TOML `[baseline_graph.llm]` settings going forward;
> env vars are lower priority than `LLM_API_BASE` / `LLM_MODEL` for the memory config path.
>
> | Env Var | Overrides |
> |---------|-----------|
> | `BASELINE_GRAPH_API_BASE` | `[baseline_graph.llm] api_base` |
> | `BASELINE_GRAPH_MODEL` | `[baseline_graph.llm] model` |
> | `BASELINE_GRAPH_API_KEY` | `[baseline_graph.llm] api_key` |
> | `BASELINE_GRAPH_TIMEOUT` | `[baseline_graph.llm] timeout` |
> | `BASELINE_GRAPH_MAX_TOKENS` | `[baseline_graph.llm] max_tokens` |
> | `BASELINE_GRAPH_EXTRACTIVE_ONLY` | `[baseline_graph] prefer_extractive` (1/true/yes) |
> | `BASELINE_GRAPH_EMBEDDING_API_BASE` | embedding endpoint for baseline graph |
> | `BASELINE_GRAPH_EMBEDDING_MODEL` | embedding model for baseline graph |
> | `BASELINE_GRAPH_EMBEDDING_API_KEY` | embedding API key for baseline graph |

See [Baseline Graph Documentation](baseline-graph.md) for full usage guide.

### `[federation]` Section

Cross-namespace federated search settings:

> All `[federation]` fields are **TOML-only** — no environment variable override.
> Use `config.toml` to configure federation settings.

> **Enforced constraint:** `namespace_timeout` must be ≤ `max_total_timeout`. Violating
> this is caught at startup. The defaults satisfy this constraint
> (`namespace_timeout=0.4 ≤ max_total_timeout=2.0`).

```toml
[federation]
enabled = false              # Enable federation features
namespace_timeout = 0.4      # Per-namespace search timeout (seconds, max 30)
max_namespaces = 5           # Max secondary namespaces (primary doesn't count)
max_total_timeout = 2.0      # Total wall-clock budget for all searches (max 60s)

[federation.scoring]
local_weight = 1.0           # Weight for primary namespace results (max 10.0)
wide_weight = 0.55           # Weight for secondary namespace results (max 10.0)
recency_half_life_days = 60  # Half-life for recency decay (days)
recency_floor = 0.7          # Minimum recency multiplier

[federation.access]
# Allowlist: which primary namespaces can search which secondaries
# Format: { "primary-ns-id" = ["secondary-1", "secondary-2"] }
# allowlists = {}

# Per-namespace configuration
# [federation.namespaces.my-other-repo]
# code_path = "/home/user/my-other-repo"
# deny_topics = ["secret-planning"]
```

> **Note:** Federation is feature-gated. Set `federation.enabled = true` to activate.
> See [MCP Server Reference](mcp-server.md) for `watercooler_federated_search` tool docs.

### Advanced Configuration (Rarely Needed)

The following sections exist in the schema for advanced use cases. Most users never need
to set them. See `src/watercooler/templates/config.example.toml` for the full field
reference with inline comments.

**`[mcp.daemons]`** — Background daemon processes: the compound daemon (auto suggestions,
learnings on closure) and thread auditor (finds stale or malformed threads). Enable with
`[mcp.daemons] enabled = true`. All daemon sub-settings are TOML-only (no env vars).

**`[mcp.cache]`** — In-memory or Redis-backed result cache. Set `backend = "database"` (Redis
mode) and `api_url` for the Redis endpoint. Default: `backend = "memory"` with 300s TTL,
10,000 max entries.

**`[mcp.http]`** — HTTP transport settings (CORS origins, max request size, timeout).
Only relevant when `transport = "http"` in `[mcp]`.

**`[mcp.slack]`** — Slack notification integration. Set `webhook_url` (for simple
webhooks) or `bot_token` + `app_token` (for full bot integration) to enable
notifications on say/ack/handoff events. Slack tokens belong in `credentials.toml`.
See `config.example.toml` for the full notification toggle reference.

**`[dashboard]`** — Settings for the Watercooler web dashboard (default repo, branch,
poll intervals, thread display). TOML-only; all fields have sensible defaults.

### `[memory]` Section

Memory backend settings. The default backend is `"graphiti"`. Set `backend = "null"` to disable memory.

```toml
[memory]
# Backend: "null" (disabled), "graphiti" (graph + T2), "leanrag" (full-text + T3)
backend = "graphiti"

# Enable async memory task queue (recommended when using graphiti or leanrag)
queue_enabled = false
```

| Field | Env Var |
|-------|---------|
| `backend` | `WATERCOOLER_MEMORY_BACKEND` |
| `queue_enabled` | `WATERCOOLER_MEMORY_QUEUE` |
| `enabled` *(inverted)* | `WATERCOOLER_MEMORY_DISABLED=1` disables memory even if `backend` is set |

### `[memory.llm]` Section

LLM service used for Graphiti entity extraction, summarization, and memory operations:

```toml
[memory.llm]
api_base = ""          # LLM service URL (e.g., "http://localhost:8000/v1")
model = ""             # Model name (e.g., "deepseek-r1:7b", "gpt-4o-mini")
timeout = 60.0         # Request timeout in seconds
max_tokens = 512       # Max tokens per response
context_size = 8192    # Context window size

# Optional prompt overrides (leave unset to use built-in defaults)
# system_prompt = ""          # Empty = auto-detect based on model family
# prompt_prefix = ""          # Empty = auto-detect (e.g., /no_think for Qwen3)
# summary_prompt = ""         # Leave unset to use built-in summarization prompt; override to customize
# thread_summary_prompt = ""  # Leave unset to use built-in thread-summary prompt; override to customize
```

| Field | Env Var |
|-------|---------|
| `api_base` | `LLM_API_BASE` |
| `model` | `LLM_MODEL` |
| `timeout` | `LLM_TIMEOUT` |
| `max_tokens` | `LLM_MAX_TOKENS` |
| `context_size` | `LLM_CONTEXT_SIZE` |
| `system_prompt` | `LLM_SYSTEM_PROMPT` |
| `prompt_prefix` | `LLM_PROMPT_PREFIX` |
| `summary_prompt` | `LLM_SUMMARY_PROMPT` |
| `thread_summary_prompt` | `LLM_THREAD_SUMMARY_PROMPT` |

API keys for the LLM service go in `credentials.toml` (auto-detected from `api_base`).
See [Credentials vs Configuration](#credentials-vs-configuration) for details.

### `[memory.embedding]` Section

Embedding service for vector search (T2/T3 tiers):

```toml
[memory.embedding]
api_base = "http://localhost:8080/v1"  # Embedding service URL
model = "bge-m3"                       # Embedding model name
dim = 1024                             # Embedding dimension (must match model)
timeout = 60.0                         # Request timeout in seconds
batch_size = 32                        # Embeddings per API call
context_size = 8192                    # Context window size
```

| Field | Env Var |
|-------|---------|
| `api_base` | `EMBEDDING_API_BASE` |
| `model` | `EMBEDDING_MODEL` |
| `dim` | `EMBEDDING_DIM` |
| `timeout` | `EMBEDDING_TIMEOUT` |
| `batch_size` | `EMBEDDING_BATCH_SIZE` |
| `context_size` | `EMBEDDING_CONTEXT_SIZE` |

### `[memory.database]` Section

Database connection for Graphiti (FalkorDB or Redis-compatible):

```toml
[memory.database]
host = "localhost"
port = 6379
username = ""    # Authentication username (if required)
# password: set via FALKORDB_PASSWORD env var or credentials.toml — do not put in config.toml
```

> **Security:** Set `password` via the `FALKORDB_PASSWORD` environment variable or in
> `~/.watercooler/credentials.toml`, not in `config.toml` (which is typically committed to
> version control).

| Field | Env Var |
|-------|---------|
| `host` | `FALKORDB_HOST` |
| `port` | `FALKORDB_PORT` |
| `password` | `FALKORDB_PASSWORD` |
| `username` | TOML-only |

### `[memory.graphiti]` Section

Graphiti backend settings (used when `backend = "graphiti"`):

```toml
[memory.graphiti]
# Reranker for search result quality.
# Options: "rrf" (default), "mmr", "cross_encoder", "node_distance", "episode_mentions"
reranker = "rrf"

# Chunking: split long entries into overlapping chunks before indexing
chunk_on_sync = true          # Enable chunking (recommended)
chunk_max_tokens = 768        # Max tokens per chunk
chunk_overlap = 64            # Token overlap between adjacent chunks

# Whether to index the entry summary instead of the full body
use_summary = false

# Track entry-level episodes in the Graphiti graph
track_entry_episodes = true

# Override LLM and embedding endpoints (falls back to [memory.llm] / [memory.embedding])
# llm_model = ""
# llm_api_base = ""
# embedding_model = ""
# embedding_api_base = ""
```

| Field | Env Var |
|-------|---------|
| `reranker` | `WATERCOOLER_GRAPHITI_RERANKER` |
| `chunk_on_sync` | `WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC` |
| `chunk_max_tokens` | `WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS` |
| `chunk_overlap` | `WATERCOOLER_GRAPHITI_CHUNK_OVERLAP` |
| `use_summary` | `WATERCOOLER_GRAPHITI_USE_SUMMARY` |
| *(other fields)* | TOML-only |

### `[memory.leanrag]` Section

LeanRAG backend settings (used when `backend = "leanrag"` or when T3 tier is enabled):

```toml
[memory.leanrag]
# Path to LeanRAG installation. Required if T3 is enabled.
path = ""

# Max parallel workers for indexing
max_workers = 8

# Override LLM and embedding endpoints (falls back to [memory.llm] / [memory.embedding])
# llm_model = ""
# llm_api_base = ""
# embedding_model = ""
# embedding_api_base = ""
```

| Field | Env Var |
|-------|---------|
| `path` | `LEANRAG_PATH` |
| *(other fields)* | TOML-only |

### `[memory.tiers]` Section

Multi-tier search orchestration. Watercooler searches T1 (keyword), T2 (Graphiti graph),
and T3 (LeanRAG full-text) in order, stopping when enough high-quality results are found:

```toml
[memory.tiers]
# Enable/disable individual tiers
t1_enabled = true    # T1: BM25 keyword search (always fast, no external service)
t2_enabled = true    # T2: Graphiti graph search (requires graphiti backend)
t3_enabled = false   # T3: LeanRAG full-text (requires leanrag.path)

# Escalation policy
max_tiers = 2        # Max tiers to query before stopping
min_results = 3      # Stop escalating once this many results are found
min_confidence = 0.5 # Stop escalating if top result exceeds this confidence

# Per-tier result limits
t1_limit = 10        # Max results fetched from T1
t2_limit = 10        # Max results fetched from T2
t3_limit = 5         # Max results fetched from T3
```

| Field | Env Var |
|-------|---------|
| `t1_enabled` | `WATERCOOLER_TIER_T1_ENABLED` |
| `t2_enabled` | `WATERCOOLER_TIER_T2_ENABLED` |
| `t3_enabled` | `WATERCOOLER_TIER_T3_ENABLED` |
| `max_tiers` | `WATERCOOLER_TIER_MAX_TIERS` |
| `min_results` | `WATERCOOLER_TIER_MIN_RESULTS` |
| `min_confidence` | `WATERCOOLER_TIER_MIN_CONFIDENCE` |

## Migrating from Environment Variables

If you're currently using environment variables, you can migrate to config files:

### Before (Environment Variables)

```bash
export WATERCOOLER_AGENT="Claude Code"
export WATERCOOLER_GIT_AUTHOR="Your Name"
export WATERCOOLER_GIT_EMAIL="you@example.com"
export WATERCOOLER_LOG_LEVEL="DEBUG"
```

### After (Config File)

```toml
# ~/.watercooler/config.toml

[mcp]
default_agent = "Claude Code"

[mcp.git]
author = "Your Name"
email = "you@example.com"

[mcp.logging]
level = "DEBUG"
```

Environment variables always override config file values (highest precedence after CLI
args). See [Configuration Precedence](#configuration-precedence).

## Environment Variables Reference

Complete reference of all supported environment variables. All `WATERCOOLER_*` variables
map to TOML config paths; the TOML path is always authoritative for defaults.

> **Note on defaults:** `""` (empty string) means "use the built-in default or auto-detect".
> Setting a variable to `""` is the same as not setting it. Fields not listed here are
> **TOML-only** — setting them via environment variable will have no effect.

#### Core / Common

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_THREADS_PATTERN` | `common.threads_pattern` | `""` | URL pattern for threads remote |
| `WATERCOOLER_THREADS_SUFFIX` | `common.threads_suffix` | `"-threads"` | Suffix appended to code repo name for the threads repo (e.g., `my-app` → `my-app-threads`) |
| `WATERCOOLER_TEMPLATES` | `common.templates_dir` | `""` | Override built-in templates directory |

#### MCP Core

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_DIR` | `mcp.threads_dir` | auto | Override threads directory path |
| `WATERCOOLER_THREADS_BASE` | `mcp.threads_base` | `""` | Base path for thread storage |
| `WATERCOOLER_AGENT` | `mcp.default_agent` | auto | Default agent identity |
| `WATERCOOLER_AGENT_TAG` | `mcp.agent_tag` | `""` | Tag appended to agent identity |
| `WATERCOOLER_AUTO_BRANCH` | `mcp.auto_branch` | `true` | Auto-detect and use current git branch |
| `WATERCOOLER_AUTO_PROVISION` | `mcp.auto_provision` | `true` | Auto-provision thread git infrastructure |
| `WATERCOOLER_AUTO_PROVISION_MODELS` | `mcp.service_provision.models` | `true` | Auto-download GGUF models from HuggingFace when needed for local LLM |
| `WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER` | `mcp.service_provision.llama_server` | `true` | Auto-download llama-server binary from GitHub releases when needed |
| `WATERCOOLER_MCP_TRANSPORT` | `mcp.transport` | `"stdio"` | Transport: `stdio` or `http` |
| `WATERCOOLER_MCP_HOST` | `mcp.host` | `"127.0.0.1"` | HTTP transport bind host |
| `WATERCOOLER_MCP_PORT` | `mcp.port` | `3000` | HTTP transport port |

#### Git

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_GIT_AUTHOR` | `mcp.git.author` | `""` | Git author name for thread commits |
| `WATERCOOLER_GIT_EMAIL` | `mcp.git.email` | `"mcp@watercooler.dev"` | Git author email |
| `WATERCOOLER_GIT_SSH_KEY` | `mcp.git.ssh_key` | `""` | Path to SSH private key. See [Authentication Guide](AUTHENTICATION.md) for SSH setup and the recommended HTTPS+credential-helper alternative for headless/MCP use. |

> **No env var for remote URL override:** There is no working env var to set the remote repository
> URL. Use `common.threads_pattern` (TOML) to override the remote. `WATERCOOLER_GIT_REPO` is
> recognized by the path resolver and validation tools for dynamic context detection (error message
> hints), but `GitConfig` has no `repo` field so it does **not** configure git remotes.

#### Sync

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_ASYNC_SYNC` | `mcp.sync.async_sync` | `true` | Enable async (non-blocking) sync |
| `WATERCOOLER_BATCH_WINDOW` | `mcp.sync.batch_window` | `5.0` | Seconds to batch coalesced writes |
| `WATERCOOLER_SYNC_INTERVAL` | `mcp.sync.interval` | `30.0` | Background sync interval (seconds) |
| `WATERCOOLER_SYNC_MAX_RETRIES` | `mcp.sync.max_retries` | `5` | Maximum sync retry attempts |
| `WATERCOOLER_SYNC_MAX_BACKOFF` | `mcp.sync.max_backoff` | `300.0` | Maximum backoff delay between retries (seconds) |

#### Logging

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_LOG_LEVEL` | `mcp.logging.level` | `"INFO"` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `WATERCOOLER_LOG_DIR` | `mcp.logging.dir` | `""` | Log file directory (empty = no file logging) |
| `WATERCOOLER_LOG_MAX_BYTES` | `mcp.logging.max_bytes` | `10485760` | Max log file size before rotation (bytes, max 10 GB) |
| `WATERCOOLER_LOG_BACKUP_COUNT` | `mcp.logging.backup_count` | `5` | Rotated log files to retain (max 100) |
| `WATERCOOLER_LOG_DISABLE_FILE` | `mcp.logging.disable_file` | `false` | Disable file logging entirely |

#### Graph

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_AUTO_START_SERVICES` | `mcp.graph.auto_start_services` | `false` | Auto-start LLM/embedding services if not detected |
| `WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD` | `mcp.graph.embedding_divergence_threshold` | `0.6` | Cosine divergence threshold for embedding model mismatch warnings |

#### Validation

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_VALIDATE_ON_WRITE` | `validation.on_write` | `true` | Validate entries on write |
| `WATERCOOLER_FAIL_ON_VIOLATION` | `validation.fail_on_violation` | `false` | Raise error (vs warn) on violation |

#### Memory

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_MEMORY_BACKEND` | `memory.backend` | `"graphiti"` | Backend: `graphiti` (default), `leanrag`, `null` (disabled) |
| `WATERCOOLER_MEMORY_QUEUE` | `memory.queue_enabled` | `false` | Enable async memory task queue |
| `WATERCOOLER_MEMORY_DISABLED` | `memory.enabled` (inverted) | `unset` | Set to `1`/`true`/`yes` to **disable** memory, even if `backend` is set |
| `WATERCOOLER_GRAPHITI_ENABLED` | *(alias)* | `unset` | Legacy shorthand: `"1"` enables Graphiti (equivalent to `WATERCOOLER_MEMORY_BACKEND=graphiti`); `"0"` disables |
| `WATERCOOLER_LEANRAG_ENABLED` | *(alias)* | `unset` | Legacy shorthand: `"1"` enables LeanRAG T3 tier (equivalent to `memory.tiers.t3_enabled = true`) |

> **Architecture note:** Several env vars bypass `config_loader.py`'s ENV_MAPPING and are
> read directly by their respective modules. This includes vars in the **Graph** section
> (`WATERCOOLER_AUTO_START_SERVICES`, `WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD`), the
> **MCP Core** service provision vars (`WATERCOOLER_AUTO_PROVISION_MODELS`,
> `WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER`), and all vars in the **sections below** (LLM
> Service through Tier Orchestration). When both a TOML value and env var are set, **the
> env var wins**. TOML values are still respected when no env var is present — the code
> falls back to the Pydantic config object populated by `config_loader.py`. A future
> release will consolidate these into a single TOML-driven resolution path.

#### LLM Service

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `LLM_API_KEY` | credentials | `""` | LLM API key (prefer `credentials.toml`) |
| `LLM_API_BASE` | `memory.llm.api_base` | `""` | LLM service base URL |
| `LLM_MODEL` | `memory.llm.model` | `""` | LLM model name |
| `LLM_TIMEOUT` | `memory.llm.timeout` | `60.0` | LLM request timeout (seconds) |
| `LLM_MAX_TOKENS` | `memory.llm.max_tokens` | `512` | Max tokens per LLM response |
| `LLM_CONTEXT_SIZE` | `memory.llm.context_size` | `8192` | LLM context window size |
| `LLM_SYSTEM_PROMPT` | `memory.llm.system_prompt` | `""` | Override system prompt |
| `LLM_PROMPT_PREFIX` | `memory.llm.prompt_prefix` | `""` | Prefix added to all LLM prompts |
| `LLM_SUMMARY_PROMPT` | `memory.llm.summary_prompt` | built-in | Override prompt for baseline graph entry summarization (leave unset to use built-in default) |
| `LLM_THREAD_SUMMARY_PROMPT` | `memory.llm.thread_summary_prompt` | built-in | Override prompt for thread-level summarization (leave unset to use built-in default) |

#### Embedding Service

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `EMBEDDING_API_KEY` | credentials | `""` | Embedding API key (prefer `credentials.toml`) |
| `EMBEDDING_API_BASE` | `memory.embedding.api_base` | `""` | Embedding service base URL |
| `EMBEDDING_MODEL` | `memory.embedding.model` | `"bge-m3"` | Embedding model name |
| `EMBEDDING_DIM` | `memory.embedding.dim` | `1024` | Embedding dimension |
| `EMBEDDING_TIMEOUT` | `memory.embedding.timeout` | `60.0` | Embedding request timeout (seconds) |
| `EMBEDDING_BATCH_SIZE` | `memory.embedding.batch_size` | `32` | Batch size for embedding requests |
| `EMBEDDING_CONTEXT_SIZE` | `memory.embedding.context_size` | `8192` | Embedding context window size |

#### Database (FalkorDB / Graphiti)

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `FALKORDB_HOST` | `memory.database.host` | `"localhost"` | FalkorDB host |
| `FALKORDB_PORT` | `memory.database.port` | `6379` | FalkorDB port |
| `FALKORDB_PASSWORD` | `memory.database.password` | `""` | FalkorDB authentication password |

#### Graphiti Backend

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_GRAPHITI_RERANKER` | `memory.graphiti.reranker` | `"rrf"` | Reranker: `rrf`, `mmr`, `cross_encoder`, `node_distance`, `episode_mentions` |
| `WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC` | `memory.graphiti.chunk_on_sync` | `true` | Chunk entries before Graphiti indexing |
| `WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS` | `memory.graphiti.chunk_max_tokens` | `768` | Max tokens per chunk |
| `WATERCOOLER_GRAPHITI_CHUNK_OVERLAP` | `memory.graphiti.chunk_overlap` | `64` | Token overlap between chunks |
| `WATERCOOLER_GRAPHITI_USE_SUMMARY` | `memory.graphiti.use_summary` | `false` | Index entry summary instead of full body |

#### LeanRAG Backend

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `LEANRAG_PATH` | `memory.leanrag.path` | `""` | Path to LeanRAG installation (required for T3) |
| `WATERCOOLER_LEANRAG_DATABASE` | *(no TOML equivalent)* | auto-derived | Override the derived LeanRAG database name (default: `leanrag_<repo-name>`) |

#### Tier Orchestration

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_TIER_T1_ENABLED` | `memory.tiers.t1_enabled` | `true` | Enable T1 keyword search tier |
| `WATERCOOLER_TIER_T2_ENABLED` | `memory.tiers.t2_enabled` | `true` | Enable T2 Graphiti graph tier |
| `WATERCOOLER_TIER_T3_ENABLED` | `memory.tiers.t3_enabled` | `false` | Enable T3 LeanRAG full-text tier |
| `WATERCOOLER_TIER_MAX_TIERS` | `memory.tiers.max_tiers` | `2` | Max tiers to query before stopping |
| `WATERCOOLER_TIER_MIN_RESULTS` | `memory.tiers.min_results` | `3` | Min results to stop tier escalation |
| `WATERCOOLER_TIER_MIN_CONFIDENCE` | `memory.tiers.min_confidence` | `0.5` | Min confidence score to stop escalation |

#### Baseline Graph (Legacy)

These env vars are read directly by the baseline graph module (not via config_loader). Prefer
`[baseline_graph.llm]` TOML config going forward. See the [`[baseline_graph]` section](#baseline_graph-section).

| Env Var | Overrides | Description |
|---------|-----------|-------------|
| `BASELINE_GRAPH_API_BASE` | `[baseline_graph.llm] api_base` | LLM endpoint for baseline graph |
| `BASELINE_GRAPH_MODEL` | `[baseline_graph.llm] model` | LLM model name |
| `BASELINE_GRAPH_API_KEY` | `[baseline_graph.llm] api_key` | API key for the LLM |
| `BASELINE_GRAPH_TIMEOUT` | `[baseline_graph.llm] timeout` | Request timeout (seconds) |
| `BASELINE_GRAPH_MAX_TOKENS` | `[baseline_graph.llm] max_tokens` | Max tokens per response |
| `BASELINE_GRAPH_EXTRACTIVE_ONLY` | `[baseline_graph] prefer_extractive` | `1`/`true`/`yes` forces extractive mode (no LLM) |
| `BASELINE_GRAPH_EMBEDDING_API_BASE` | embedding endpoint | Embedding service URL for baseline graph |
| `BASELINE_GRAPH_EMBEDDING_MODEL` | embedding model | Embedding model name |
| `BASELINE_GRAPH_EMBEDDING_API_KEY` | embedding key | API key for the embedding service |

#### Miscellaneous

These env vars are read directly by individual modules (not via the TOML config system):

| Env Var | Module | Description |
|---------|--------|-------------|
| `WATERCOOLER_USER` | `lock.py` | Override OS username used in advisory lock file names |
| `WATERCOOLER_GITHUB_TOKEN` | git credential helper | GitHub token for HTTPS auth (alternative to `credentials.toml`). See [Authentication Guide](AUTHENTICATION.md). |
| `WATERCOOLER_GRAPHITI_PATH` | `backends/graphiti.py` | Override path to the graphiti submodule (development setups only — not needed when graphiti is installed as a package via `[memory]` extras). No TOML equivalent. |

## Credentials vs Configuration

Watercooler separates **credentials** (secrets) from **configuration** (settings):

| File | Purpose | Permissions | Git |
|------|---------|-------------|-----|
| `~/.watercooler/config.toml` | Settings (api_base, model, etc.) | Normal | Can commit |
| `~/.watercooler/credentials.toml` | Secrets (API keys, tokens) | 0600 | **Never commit** |

### Why Separate Files?

1. **Security**: Different access patterns - credentials need 0600 permissions
2. **Version control**: Config can be shared in repos; credentials cannot
3. **Environment parity**: Same config across dev/prod, different credentials
4. **Intuitive**: "I have an OpenAI key" vs "I have an LLM key"

### API Key Storage

Store API keys in `credentials.toml` by **provider name**:

```toml
# ~/.watercooler/credentials.toml

[github]
token = "ghp_xxxxxxxxxxxx"
ssh_key = "~/.ssh/id_ed25519"

[openai]
api_key = "sk-proj-..."

[anthropic]
api_key = "sk-ant-..."

[groq]
api_key = "gsk_..."

[voyage]
api_key = "vg-..."

[google]
api_key = "AIza..."

[dashboard]
session_secret = "your-secret-key"
```

The system auto-detects which provider to use based on `api_base` in config.toml.

### API Key Resolution Priority

When resolving API keys for LLM or embedding services:

```
1. Env var (highest):     LLM_API_KEY / EMBEDDING_API_KEY
2. Provider-specific env: OPENAI_API_KEY (auto-detected from api_base)
3. Provider credentials:  [openai].api_key in credentials.toml
4. Empty string (lowest): Local servers often don't need keys
```

**Security:** Credentials files are automatically set to mode 0600 (owner read/write only).

**Never commit credentials to version control.** The `.watercooler/credentials.toml` pattern is already in `.gitignore`.

## Best Practices

### User Config vs Project Config

| Setting Type | Where to Put It |
|--------------|-----------------|
| Personal identity (name, email) | User config |
| Personal preferences (log level) | User config |
| Team standards (validation rules) | Project config |
| Repo-specific settings (sync interval, log level) | Project config |
| Secrets and tokens | Credentials file or env vars |

### CI/CD Environments

For CI/CD, prefer environment variables over config files:

```yaml
# GitHub Actions example
env:
  WATERCOOLER_LOG_LEVEL: "DEBUG"
  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Multi-Project Setup

Each project's threads live on its own `watercooler/threads` orphan branch. No
extra configuration is needed — threads are automatically scoped to the code
repository. Just pass `code_path` pointing to each project root.

## Troubleshooting

### Config Not Loading

1. Check file location: `watercooler config show --sources`
2. Validate syntax: `watercooler config validate`
3. Check for TOML errors in output

### Environment Override Not Working

Environment variables override config files. If a setting isn't taking effect:

1. Check for typos in the variable name
2. Verify the variable is exported: `echo $WATERCOOLER_LOG_LEVEL`
3. Restart your shell/IDE after changes

### Permission Errors on Credentials

Credentials files must have secure permissions:

```bash
chmod 600 ~/.watercooler/credentials.toml
```

## Programmatic Configuration (Python API)

For developers building on Watercooler, use the unified `config_facade` module:

### Basic Usage

```python
from watercooler.config_facade import config

# Path resolution (lightweight, stdlib-only)
threads_dir = config.paths.threads_dir
templates_dir = config.paths.templates_dir

# Full config (lazy-loads TOML + Pydantic)
cfg = config.full()
log_level = cfg.mcp.logging.level

# Environment variables with type coercion
level = config.env.get("WATERCOOLER_LOG_LEVEL", "INFO")
debug = config.env.get_bool("DEBUG", False)
port = config.env.get_int("WATERCOOLER_PORT", 8080)
timeout = config.env.get_float("TIMEOUT", 30.0)

# Runtime context (for MCP server)
ctx = config.context(code_root="/path/to/repo")
print(ctx.code_branch)
print(ctx.threads_repo_url)

# Credentials
token = config.get_github_token()
```

### Environment Variable Helpers

The `config.env` class provides type-safe access:

| Method | Description | Example |
|--------|-------------|---------|
| `get(key, default)` | String value | `config.env.get("MY_VAR", "default")` |
| `get_bool(key, default)` | Boolean (1/true/yes/on) | `config.env.get_bool("DEBUG", False)` |
| `get_int(key, default)` | Integer | `config.env.get_int("PORT", 8080)` |
| `get_float(key, default)` | Float | `config.env.get_float("TIMEOUT", 30.0)` |
| `get_path(key, default)` | Path with ~ expansion | `config.env.get_path("DATA_DIR")` |

### Testing Support

Reset cached state for test isolation:

```python
def test_something():
    config.reset()  # Clear cached config
    # ... test code ...
    config.reset()  # Clean up
```

For more advanced testing utilities, see `watercooler.testing`:

```python
from watercooler.testing import temp_config, mock_env_vars

# Temporarily override configuration
with temp_config(threads_dir=Path("/tmp/test-threads")):
    assert config.paths.threads_dir == Path("/tmp/test-threads")

# Temporarily set environment variables
with mock_env_vars(WATERCOOLER_LOG_LEVEL="DEBUG"):
    assert config.env.get("WATERCOOLER_LOG_LEVEL") == "DEBUG"
```

### Architecture

The config facade provides a single entry point while maintaining:

- **Lazy loading**: Config components loaded only when accessed
- **Thread-safe**: Uses locks in underlying modules
- **Backward compatible**: Old imports continue working
- **Testable**: Easy reset for test isolation

**Module hierarchy:**
```
config_facade.py    → Unified entry point
├── path_resolver.py   → Git-aware path discovery
├── config_loader.py   → TOML loading + Pydantic validation
├── config_schema.py   → Pydantic models
├── credentials.py     → Credential management
└── testing.py         → Test utilities
```

## See Also

- [Quickstart Guide](QUICKSTART.md) - Get up and running in minutes
- [Installation Guide](INSTALLATION.md) - Getting started
- [MCP Server Reference](mcp-server.md) - MCP tool documentation
- [Baseline Graph Documentation](baseline-graph.md) - Knowledge graph generation
