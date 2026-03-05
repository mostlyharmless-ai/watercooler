# Configuration

## Minimum viable config

Most users only need these settings. Create `~/.watercooler/config.toml` with:

```toml
# ~/.watercooler/config.toml
version = 1                       # schema version; do not modify

[mcp]
default_agent = "Claude Code"     # your MCP client name (usually auto-detected)
agent_tag = "(yourname)"          # optional: appended to agent name in thread entries
```

Generate an annotated version with `watercooler config init --user`.

---

## Config vs credentials

| File | What it stores | Safe to commit? |
|---|---|---|
| `~/.watercooler/config.toml` | Behavior and preferences | Yes |
| `~/.watercooler/credentials.toml` | Secrets (tokens, API keys) | Never |

Both files are TOML. The config file is also supported at project level:
`.watercooler/config.toml` (inside your repo, for per-project overrides).

---

## Config commands

**Initialize config from template:**

```bash
watercooler config init --user      # creates ~/.watercooler/config.toml
watercooler config init --project   # creates .watercooler/config.toml in current dir
```

Pass `--force` to overwrite an existing file.

**Show resolved config** (merged user + project + env vars):

```bash
watercooler config show
watercooler config show --json                    # machine-readable output
watercooler config show --sources                 # show which file each key came from
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
| `agent_tag` | `""` | Short tag appended to agent name, e.g. `"(alice)"` |
| `threads_dir` | (auto) | Explicit threads directory; leave empty for auto-discovery |
| `transport` | `"stdio"` | Transport mode: `stdio` (local) or `http` |
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
| `WATERCOOLER_CODE_REPO` | — | (auto) | Override code repo detection |

### Git commit identity

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_GIT_AUTHOR` | `mcp.git.author` | `""` (uses agent name) | Git commit author name |
| `WATERCOOLER_GIT_EMAIL` | `mcp.git.email` | `"mcp@watercooler.dev"` | Git commit email |
| `WATERCOOLER_GIT_SSH_KEY` | `mcp.git.ssh_key` | `""` | Path to SSH private key |

### Authentication

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `GITHUB_TOKEN` | — | — | GitHub token for git operations (or `GH_TOKEN`) |
| `GH_TOKEN` | — | — | Alternative to `GITHUB_TOKEN`; same precedence |
| `WATERCOOLER_AUTH_MODE` | — | `"local"` | Auth mode for hosted deployments |
| `WATERCOOLER_TOKEN_API_URL` | — | — | Token API URL (hosted mode only) |
| `WATERCOOLER_TOKEN_API_KEY` | — | — | Token API key (hosted mode only) |

### Memory and search

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_MEMORY_BACKEND` | `memory.backend` | (disabled) | Memory backend: `graphiti` or `leanrag` |
| `WATERCOOLER_MEMORY_QUEUE` | `memory.queue_enabled` | `false` | Enable async memory indexing |
| `WATERCOOLER_MEMORY_DISABLED` | — | — | Set to `1` to disable memory even if configured |
| `LLM_API_KEY` | `memory.llm.api_key` | — | LLM provider API key |
| `LLM_API_BASE` | `memory.llm.api_base` | — | LLM endpoint URL |
| `LLM_MODEL` | `memory.llm.model` | — | LLM model name |
| `EMBEDDING_API_KEY` | `memory.embedding.api_key` | — | Embedding provider API key |
| `EMBEDDING_API_BASE` | `memory.embedding.api_base` | — | Embedding endpoint URL |
| `EMBEDDING_MODEL` | `memory.embedding.model` | — | Embedding model name |
| `EMBEDDING_DIM` | `memory.embedding.dim` | — | Embedding dimension |

### MCP server

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_MCP_TRANSPORT` | `mcp.transport` | `"stdio"` | Transport: `stdio` or `http` |
| `WATERCOOLER_MCP_HOST` | `mcp.host` | `"127.0.0.1"` | HTTP mode: bind address |
| `WATERCOOLER_MCP_PORT` | `mcp.port` | `3000` | HTTP mode: port |

### Logging

| Env var | Default | Description |
|---|---|---|
| `WATERCOOLER_LOG_LEVEL` | `"INFO"` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `WATERCOOLER_LOG_DIR` | `~/.watercooler/logs/` | Log file directory |
| `WATERCOOLER_LOG_DISABLE_FILE` | `false` | Set to `1` to disable file logging |

---

## Precedence rules

Later sources override earlier ones, on a per-key basis:

1. Built-in defaults
2. User config: `~/.watercooler/config.toml`
3. Project config: `<project>/.watercooler/config.toml`
4. Environment variables

To see the resolved value and source of each key, run `watercooler config show --sources`.

---

## Tier label glossary

| Label | What it adds |
|---|---|
| T1 — Baseline | Thread graph, zero config, included with all installs. `say`, `ack`, `handoff`, `list`, `search` all work at T1. |
| T2 — Semantic memory | Persistent memory and semantic search across sessions. Requires memory backend configuration. |
| T3 — Hierarchical memory | Summarized context and full semantic graph with community detection. Requires T2 setup plus additional resources. |
