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

[common]
threads_pattern = "git@github.com:myorg/{repo}-threads.git"

[mcp.sync]
batch_window = 10.0
interval = 60.0

[validation]
fail_on_violation = true
```

## Configuration Reference

### `[common]` Section

Shared settings for MCP and Dashboard:

```toml
[common]
# URL pattern for threads repos
# Placeholders: {org}, {repo}, {namespace}
threads_pattern = "git@github.com:{org}/{repo}-threads.git"

# Suffix for threads repo naming
threads_suffix = "-threads"

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

# Behavior
auto_branch = true      # Auto-create matching threads branches
auto_provision = true   # Auto-create threads repos if missing

# Explicit paths (empty = auto-discover)
threads_dir = ""
threads_base = ""
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
async = true           # Enable async git operations
batch_window = 5.0     # Seconds to batch commits before push
max_delay = 30.0       # Maximum delay before forcing push
max_batch_size = 50    # Maximum entries per batch
max_retries = 5        # Retry attempts for failed operations
max_backoff = 300.0    # Maximum backoff delay (seconds)
interval = 30.0        # Background sync interval (seconds)
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

### `[validation]` Section

Protocol validation settings:

```toml
[validation]
on_write = true              # Validate on write operations
on_commit = true             # Validate on commit
fail_on_violation = false    # Fail vs warn on violation
check_branch_pairing = true  # Validate branch pairing
check_commit_footers = true  # Validate commit footers
check_entry_format = true    # Validate entry format
```

### `[baseline_graph]` Section

Settings for the baseline graph module (free-tier knowledge graph generation):

```toml
[baseline_graph]
prefer_extractive = false    # Force extractive mode (skip LLM)

[baseline_graph.llm]
api_base = "http://localhost:11434/v1"  # Ollama default
model = "llama3.2:3b"                   # Small local model
api_key = "ollama"                      # Ollama doesn't require key
timeout = 30.0                          # Request timeout (seconds)
max_tokens = 256                        # Max response tokens

[baseline_graph.extractive]
max_chars = 200              # Max chars for extractive summary
include_headers = true       # Include markdown headers
max_headers = 3              # Max headers to include
```

See [Baseline Graph Documentation](baseline-graph.md) for full usage guide.

### Memory Backends

Memory backends (Graphiti, LeanRAG) are configured **exclusively via environment variables**.
This is intentional - each backend has its own config system, and env vars provide a consistent interface.

> **Note:** There are no `[memory]` or `[servers]` sections in the TOML config.
> Memory configuration is env-only.

#### Memory Backend Environment Variables

| Environment Variable | Required | Default | Description |
|---------------------|----------|---------|-------------|
| `WATERCOOLER_GRAPHITI_ENABLED` | No | `0` | Enable Graphiti backend |
| `WATERCOOLER_MEMORY_DISABLED` | No | `0` | Disable all memory backends |
| `LLM_API_KEY` | **Yes**¹ | Error | LLM authentication (no fallback) |
| `LLM_API_BASE` | No | OpenAI | LLM endpoint URL |
| `LLM_MODEL` | No | `gpt-4o-mini` | LLM model name |
| `EMBEDDING_API_KEY` | **Yes**¹ | Error | Embedding authentication (no fallback) |
| `EMBEDDING_API_BASE` | No | OpenAI | Embedding endpoint URL |
| `EMBEDDING_MODEL` | No | `text-embedding-3-small` | Embedding model name |
| `EMBEDDING_DIM` | No | `1536` | Embedding vector dimension |
| `FALKORDB_HOST` | No | `localhost` | FalkorDB host (Graphiti) |
| `FALKORDB_PORT` | No | `6379` | FalkorDB port (Graphiti) |

¹ Required when `WATERCOOLER_GRAPHITI_ENABLED=1`. There is **no fallback** to `OPENAI_API_KEY`.

#### Example: Local LLM/Embedding Setup

```bash
# Enable Graphiti with local services
export WATERCOOLER_GRAPHITI_ENABLED=1

# LLM via Ollama
export LLM_API_KEY="ollama"  # Ollama doesn't require a real key
export LLM_API_BASE="http://localhost:11434/v1"
export LLM_MODEL="llama3.2:3b"

# Embeddings via llama.cpp server
export EMBEDDING_API_KEY="local"  # Local server doesn't require a real key
export EMBEDDING_API_BASE="http://localhost:8080/v1"
export EMBEDDING_MODEL="bge-m3"
export EMBEDDING_DIM=1024
```

#### Example: OpenAI Setup

```bash
export WATERCOOLER_GRAPHITI_ENABLED=1
export LLM_API_KEY="sk-your-openai-key"
export EMBEDDING_API_KEY="sk-your-openai-key"
# Uses OpenAI defaults: gpt-4o-mini, text-embedding-3-small
```

#### MCP Memory Tools

When memory backend is enabled, the following MCP tools become available:

| Tool | Description |
|------|-------------|
| `watercooler_graphiti_add_episode` | Add content directly to Graphiti temporal graph |
| `watercooler_leanrag_run_pipeline` | Trigger LeanRAG clustering pipeline |
| `watercooler_query_memory` | Query memory backend for facts/entities |
| `watercooler_search_nodes` | Search for entity nodes |
| `watercooler_search_memory_facts` | Search for facts/relationships |

#### Example: Adding an Episode to Graphiti

```python
# Add content to Graphiti with entity extraction
result = watercooler_graphiti_add_episode(
    code_path="/path/to/repo",
    content="Implemented OAuth2 authentication using JWT tokens",
    group_id="auth-feature",           # Thread topic for grouping
    source_id="01ABC123",              # Optional: entry ID for provenance
    timestamp="2025-01-15T10:00:00Z",  # Optional: defaults to now
)

# Response includes extracted entities
{
  "success": true,
  "episode_uuid": "ep-abc-123",
  "entities_extracted": ["OAuth2", "JWT", "authentication"]
}
```

#### Example: Triggering LeanRAG Pipeline

```python
# Run LeanRAG clustering on Graphiti data
result = watercooler_leanrag_run_pipeline(
    code_path="/path/to/repo",
    group_id="auth-feature",           # Optional: filter by topic
    force_rebuild=False,               # Optional: rebuild from scratch
)

# Response includes clustering stats
{
  "success": true,
  "clusters_created": 5,
  "entities_processed": 42
}
```

See [Memory Documentation](MEMORY.md) for full usage guide.

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

### Environment Variable Mapping

| Environment Variable | Config Path |
|---------------------|-------------|
| `WATERCOOLER_AGENT` | `mcp.default_agent` |
| `WATERCOOLER_AGENT_TAG` | `mcp.agent_tag` |
| `WATERCOOLER_AUTO_BRANCH` | `mcp.auto_branch` |
| `WATERCOOLER_AUTO_PROVISION` | `mcp.auto_provision` |
| `WATERCOOLER_DIR` | `mcp.threads_dir` |
| `WATERCOOLER_THREADS_BASE` | `mcp.threads_base` |
| `WATERCOOLER_THREADS_PATTERN` | `common.threads_pattern` |
| `WATERCOOLER_GIT_AUTHOR` | `mcp.git.author` |
| `WATERCOOLER_GIT_EMAIL` | `mcp.git.email` |
| `WATERCOOLER_GIT_SSH_KEY` | `mcp.git.ssh_key` |
| `WATERCOOLER_ASYNC_SYNC` | `mcp.sync.async` |
| `WATERCOOLER_BATCH_WINDOW` | `mcp.sync.batch_window` |
| `WATERCOOLER_SYNC_INTERVAL` | `mcp.sync.interval` |
| `WATERCOOLER_LOG_LEVEL` | `mcp.logging.level` |
| `WATERCOOLER_LOG_DIR` | `mcp.logging.dir` |

**Note:** Environment variables still work and override config file values.

## Credentials

Credentials (GitHub tokens, SSH keys) are stored separately in `credentials.toml`:

```toml
# ~/.watercooler/credentials.toml

[github]
token = "ghp_xxxxxxxxxxxx"
ssh_key = "~/.ssh/id_ed25519"

[dashboard]
session_secret = "your-secret-key"
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
| Repo-specific settings (threads pattern) | Project config |
| Secrets and tokens | Credentials file or env vars |

### CI/CD Environments

For CI/CD, prefer environment variables over config files:

```yaml
# GitHub Actions example
env:
  WATERCOOLER_LOG_LEVEL: "DEBUG"
  WATERCOOLER_ASYNC_SYNC: "false"
  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Multi-Project Setup

For multiple projects with different threads repos:

```toml
# Project A: .watercooler/config.toml
[common]
threads_pattern = "git@github.com:team-a/{repo}-threads.git"

# Project B: .watercooler/config.toml
[common]
threads_pattern = "git@github.com:team-b/{repo}-threads.git"
```

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
sync_enabled = cfg.mcp.sync.async_sync

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

- [Environment Variables Reference](ENVIRONMENT_VARS.md) - All environment variables
- [Installation Guide](INSTALLATION.md) - Getting started
- [MCP Server Reference](mcp-server.md) - MCP tool documentation
