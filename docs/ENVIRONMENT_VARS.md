# Environment Variables Reference

**Advanced Configuration Guide** - Complete reference for all watercooler-cloud environment variables.

> **Note:** Basic setup requires NO environment variables. The [Installation Guide](INSTALLATION.md) covers the minimal configuration using credentials file + MCP config. Use these environment variables only for advanced customization.

---

## Quick Reference

| Variable | Required | Default | Used By | Purpose |
|----------|----------|---------|---------|---------|
| [`WATERCOOLER_AGENT`](#watercooler_agent) | No (auto-detected) | Auto from client | MCP Server | Override agent identity |
| [`WATERCOOLER_THREADS_BASE`](#watercooler_threads_base) | No | _Sibling `<repo>-threads`_ | MCP Server | Optional central root for threads repos |
| [`WATERCOOLER_THREADS_PATTERN`](#watercooler_threads_pattern) | No | `https://github.com/{org}/{repo}-threads.git` | MCP Server | Remote threads repo URL template |
| [`WATERCOOLER_THREADS_AUTO_PROVISION`](#watercooler_threads_auto_provision) | No | `"0"` | MCP Server | Opt-in creation of missing threads repos |
| [`WATERCOOLER_THREADS_CREATE_CMD`](#watercooler_threads_create_cmd) | No | _Unset_ | MCP Server | Command template for auto-provisioning |
| [`WATERCOOLER_AUTO_BRANCH`](#watercooler_auto_branch) | No | `"1"` | MCP Server | Auto-create/check out matching branch |
| [`WATERCOOLER_DIR`](#watercooler_dir) | No | _Unset_ | MCP & CLI | Manual override for a fixed threads directory |
| [`WATERCOOLER_GIT_REPO`](#watercooler_git_repo) | Cloud: Yes<br>Local: No | None | MCP Server | Git repository URL (enables cloud sync) |
| [`WATERCOOLER_GIT_SSH_KEY`](#watercooler_git_ssh_key) | No | None | MCP Server | Path to SSH private key |
| [`WATERCOOLER_GIT_AUTHOR`](#watercooler_git_author) | No | `"Watercooler MCP"` | MCP Server | Git commit author name |
| [`WATERCOOLER_GIT_EMAIL`](#watercooler_git_email) | No | `"mcp@watercooler.dev"` | MCP Server | Git commit author email |
| [`WATERCOOLER_TEMPLATES`](#watercooler_templates) | No | Built-in | MCP & CLI | Custom templates directory |
| [`WATERCOOLER_USER`](#watercooler_user) | No | OS username | Lock System | Override username in lock files |
| [`BASELINE_GRAPH_API_BASE`](#baseline_graph_api_base) | No | `http://localhost:8000/v1` | Baseline Graph | LLM API endpoint |
| [`BASELINE_GRAPH_MODEL`](#baseline_graph_model) | No | `qwen3:1.7b` | Baseline Graph | LLM model name |
| [`BASELINE_GRAPH_EXTRACTIVE_ONLY`](#baseline_graph_extractive_only) | No | `false` | Baseline Graph | Force extractive mode |
| [`WATERCOOLER_GRAPHITI_ENABLED`](#watercooler_graphiti_enabled) | No | `"0"` | MCP Memory | Enable Graphiti memory queries |
| [`WATERCOOLER_MEMORY_DISABLED`](#watercooler_memory_disabled) | No | Not set | All Memory | Disable all memory backends |
| [`WATERCOOLER_MEMORY_BACKEND`](#watercooler_memory_backend) | No | Not set | Memory Sync | Select memory backend (graphiti/leanrag) |

### HTTP Transport Variables

| Variable | Required | Default | Used By | Purpose |
|----------|----------|---------|---------|---------|
| [`WATERCOOLER_MCP_TRANSPORT`](#watercooler_mcp_transport) | No | `stdio` | MCP Server | Transport mode (`stdio` or `http`) |
| [`WATERCOOLER_MCP_HOST`](#watercooler_mcp_host) | No | `0.0.0.0` | HTTP Server | HTTP bind host |
| [`WATERCOOLER_MCP_PORT`](#watercooler_mcp_port) | No | `8080` | HTTP Server | HTTP bind port |
| [`WATERCOOLER_AUTH_MODE`](#watercooler_auth_mode) | No | `local` | HTTP Server | Authentication mode (`local` or `hosted`) |
| [`WATERCOOLER_TOKEN_API_URL`](#watercooler_token_api_url) | Hosted: Yes | - | HTTP Server | Token service endpoint URL |
| [`WATERCOOLER_TOKEN_API_KEY`](#watercooler_token_api_key) | Hosted: Yes | - | HTTP Server | Token service API key |
| [`WATERCOOLER_CACHE_BACKEND`](#watercooler_cache_backend) | No | `memory` | HTTP Server | Cache backend (`memory` or `database`) |
| [`WATERCOOLER_CACHE_TTL`](#watercooler_cache_ttl) | No | `300` | HTTP Server | Default cache TTL in seconds |
| [`WATERCOOLER_CORS_ORIGINS`](#watercooler_cors_origins) | No | `*` | HTTP Server | Allowed CORS origins |

---

## Core Variables

### WATERCOOLER_AGENT

**Purpose:** Override agent identity used in thread entries and ball ownership.

**Required:** **No** - Auto-detected from MCP client

**Default:** Auto-detected based on MCP client (e.g., "Claude Code", "Codex", "Cursor")

**Format:** String (e.g., `"Claude"`, `"Codex"`, `"GPT-4"`)

**Used by:** MCP Server

**Details:**

**With new authentication (recommended):**
Agent identity is automatically detected from your MCP client. No configuration needed!

**Auto-detection mapping:**
- Claude Code → "Claude Code"
- Claude Desktop → "Claude"
- Codex → "Codex"
- Cursor → "Cursor"

**When you create entries, they appear as:**
```
Entry: Claude Code (user) 2025-10-10T08:00:00Z
```

Where:
- `Claude Code` = Auto-detected from MCP client (or override from `WATERCOOLER_AGENT`)
- `(user)` = OS username from `getpass.getuser()` (automatically appended)

**Override precedence:**
1. `WATERCOOLER_AGENT` env var (if set - overrides auto-detection)
2. `client_id` from MCP Context (auto-detected from client name)
3. Fallback: `"Agent"`

**When to use this variable:**
- Override auto-detected client name
- Running multiple agents with different identities on same client
- CI/CD environments where client detection doesn't work

**Configuration examples:**

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_AGENT = "Codex"
```

**Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_AGENT": "Claude"
      }
    }
  }
}
```

**Claude Code (`.mcp.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_AGENT": "Claude"
      }
    }
  }
}
```

**Shell:**
```bash
export WATERCOOLER_AGENT="Claude"
```

**Related:**
- See [MCP Server Guide](./mcp-server.md) for complete MCP setup
- See [WATERCOOLER_USER](#watercooler_user) for username customization

---

### WATERCOOLER_THREADS_BASE

**Purpose:** Optional override for where the MCP server stores threads clones.

**Required:** No

**Default:** Sibling `<repo>-threads` directory beside the detected code repo

**Format:** Absolute path (environment variables are expanded)

**Used by:** MCP Server

```bash
# Example: central cache for all threads repos
export WATERCOOLER_THREADS_BASE="/srv/watercooler-threads"
```

---

### WATERCOOLER_THREADS_PATTERN

**Purpose:** Template for constructing the remote threads repository URL from `{org}` and `{repo}`.

**Required:** No

**Default:** `https://github.com/{org}/{repo}-threads.git`

**Used by:** MCP Server

```bash
export WATERCOOLER_THREADS_PATTERN="https://git.example.com/{org}/{repo}-threads.git"
```

---

### WATERCOOLER_THREADS_AUTO_PROVISION

**Purpose:** Controls automatic creation of missing threads repositories when a
`git clone` fails with "repository not found".

**Required:** No

**Default:** `"0"` (disabled)

**Format:** Boolean-like string (`"1"`, `"true"`, `"yes"`, `"on"` to enable; `"0"` to disable)

**Used by:** MCP Server

**Details:**

- Only applies when the threads repository is derived dynamically from the code
  repo (no `WATERCOOLER_DIR`) and uses an SSH remote (starting with `git@`).
- When enabled, the server will execute the command defined in
  [`WATERCOOLER_THREADS_CREATE_CMD`](#watercooler_threads_create_cmd) after a
  failed clone. If provisioning succeeds the clone is retried, otherwise the
  operation aborts with a detailed error.
- Branch bootstrapping continues to respect `WATERCOOLER_AUTO_BRANCH`.

```bash
# Enable auto-provisioning (if you want automatic repo creation)
export WATERCOOLER_THREADS_AUTO_PROVISION="1"
```

---

### WATERCOOLER_THREADS_CREATE_CMD

**Purpose:** Command template that provisions the remote threads repository
when auto-provisioning is enabled.

**Required:** No

**Default:** _Unset_ (required when auto-provisioning is enabled)

**Format:** String template executed via the system shell. The following
placeholders are available:

- `{slug}` – Resolved threads repo slug (e.g. `mostlyharmless-ai/watercooler-dashboard-threads`)
- `{repo_url}` – Full git URL (e.g. `https://github.com/mostlyharmless-ai/watercooler-dashboard-threads.git`)
- `{code_repo}` – Paired code repo (e.g. `mostlyharmless-ai/watercooler-dashboard`)
- `{namespace}` – Namespace/organisation portion of the slug (`mostlyharmless-ai`)
- `{repo}` – Repository name portion of the slug (`watercooler-dashboard-threads`)
- `{org}` – Shortcut to the top-level org (`mostlyharmless-ai`)

**Used by:** MCP Server

**Details:**

- Executed with `shell=True`; stdout and stderr are captured and surfaced on
  failure.
- Override to call an internal provisioning script or other tooling if needed
- If the command succeeds but the remote is still empty, the MCP server falls
  back to initialising a local git repo and will push on the first write.

```bash
# Override with custom provisioning command
export WATERCOOLER_THREADS_CREATE_CMD='my-org-tool create-repo {slug} --private'
```

---

### WATERCOOLER_AUTO_BRANCH

**Purpose:** Controls automatic creation and checkout of the matching branch in the threads repository.

**Required:** No

**Default:** `"1"` (enabled)

**Used by:** MCP Server

```bash
# Disable automatic branch creation
export WATERCOOLER_AUTO_BRANCH="0"
```

---

### WATERCOOLER_DIR

**Purpose:** Manual override for a fixed threads directory (disables automatic discovery).

**Required:** No

**Default:** _Unset_

**Format:** Absolute or relative path (e.g., `"/srv/watercooler/custom-project-threads"`, `"../my-repo-threads"`)

**Used by:** MCP Server & CLI

**Details:**

Universal mode derives the threads repository from git metadata (`code_path`, repo origin, branch). Set this variable only when you intentionally keep threads in a specific location that differs from the sibling `<repo>-threads` directory.

**When to set explicitly:**
- Running in an environment without git metadata (rare)
- Executing targeted tests that need an isolated threads sandbox
- Temporarily pointing at a staging directory while you relocate it into the sibling `<repo>-threads` repository

**Configuration examples:**

**Absolute path example:**
```bash
export WATERCOOLER_DIR="/srv/watercooler/custom-project-threads"
```

**Alternative location:**
```bash
export WATERCOOLER_DIR="/Volumes/threads-cache/project-threads"
```

**MCP config example:**
```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_DIR = "/srv/watercooler/custom-project-threads"
```

**VS Code workspace example:**
```json
{
  "mcp.servers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_DIR": "/srv/watercooler/custom-project-threads"
      }
    }
  }
}
```

**Related:**
- See [SETUP_AND_QUICKSTART.md](./SETUP_AND_QUICKSTART.md#3-optional-global-overrides) for the canonical setup flow
- See [CLOUD_SYNC_GUIDE.md](../.mothballed/docs/CLOUD_SYNC_GUIDE.md) for team collaboration

---

## Authentication Variables

### WATERCOOLER_GITHUB_TOKEN

**Purpose:** GitHub personal access token for git credential helper (seamless authentication).

**Required:** No (but recommended for seamless authentication)

**Default:** Falls back to `GITHUB_TOKEN`, then `GH_TOKEN`

**Format:** GitHub personal access token string (e.g., `"ghp_xxxxxxxxxxxxxxxxxxxx"`)

**Used by:** Git Credential Helper (`scripts/git-credential-watercooler`)

**Details:**

Enables seamless GitHub authentication for git operations across the web dashboard and MCP server. The git credential helper checks tokens in this priority order:

1. `WATERCOOLER_GITHUB_TOKEN` (dedicated Watercooler token, highest priority)
2. `GITHUB_TOKEN` (standard GitHub token)
3. `GH_TOKEN` (GitHub CLI token)

When the MCP server performs git operations (clone, push, pull), git automatically calls the credential helper script, which returns the token from one of these environment variables.

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"
```

**Claude Code (`.mcp.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_GITHUB_TOKEN": "ghp_xxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

**Cursor (`.cursor/mcp.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_GITHUB_TOKEN": "ghp_xxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

**Creating a GitHub Personal Access Token:**

1. Go to https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Select scopes:
   - `repo` (Full control of private repositories)
   - `read:org` (Read org and team membership)
   - `read:user` (Read user profile data)
4. Click "Generate token"
5. Copy the token and add to environment

**Auto-Configuration:**

The MCP server automatically configures git to use the credential helper on first run:

```python
# Configured automatically in src/watercooler_mcp/git_sync.py
config.set_value(
    'credential "https://github.com"',
    'helper',
    str(helper_script)
)
```

**Security:**
- Tokens are stored in environment variables (not committed to git)
- Credential helper only activates for HTTPS GitHub URLs
- Tokens have specific scoped permissions
- Never shared with third parties

**Related:**
- See [AUTHENTICATION.md](./AUTHENTICATION.md) for complete authentication flow
- See [WATERCOOLER_GIT_REPO](#watercooler_git_repo) for cloud sync configuration

---

## Cloud Sync Variables

These variables enable git-based cloud synchronization for team collaboration. Only used by the MCP server.

### WATERCOOLER_GIT_REPO

**Purpose:** Git repository URL for cloud sync (enables cloud mode).

**Required:** Yes (for cloud sync), No (for local mode)

**Default:** None

**Format:** Git URL (SSH or HTTPS)

**Used by:** MCP Server (cloud sync)

**Details:**

Setting this variable **enables cloud sync mode**:
- **Pull before read** - Always fetches latest thread content
- **Commit + push after write** - Automatic sync on every change
- **Entry-ID idempotency** - Prevents duplicate entries on retry
- **Retry logic** - Handles concurrent writes gracefully

**Supported URL formats:**

**SSH (recommended):**
```bash
export WATERCOOLER_GIT_REPO="git@github.com:my-team/watercooler-threads.git"
export WATERCOOLER_GIT_REPO="git@gitlab.com:org/threads.git"
```

**HTTPS:**
```bash
export WATERCOOLER_GIT_REPO="https://github.com/my-team/watercooler-threads.git"
```

**Local mode (unset):**
```bash
unset WATERCOOLER_GIT_REPO  # Disables cloud sync
```

**Configuration examples:**

**MCP config with cloud sync:**
```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_AGENT = "Claude"
WATERCOOLER_GIT_REPO = "git@github.com:team/threads.git"
WATERCOOLER_THREADS_BASE = "/srv/watercooler-threads"
```

**Best practices:**
- Use **dedicated repository** for threads (not mixed with code)
- Use **SSH** with deploy keys for security
- Set [`WATERCOOLER_GIT_SSH_KEY`](#watercooler_git_ssh_key) for custom keys
- Let the server clone into `WATERCOOLER_THREADS_BASE` (avoid manual per-project overrides)

**Related:**
- See [CLOUD_SYNC_GUIDE.md](../.mothballed/docs/CLOUD_SYNC_GUIDE.md) for complete setup
- See [CLOUD_SYNC_STRATEGY.md](../.mothballed/docs/CLOUD_SYNC_STRATEGY.md) for architecture details

---

## Cloud Remote MCP (Cloudflare Worker) Variables

These variables are configured in the Cloudflare Worker (`wrangler.toml` and Wrangler secrets) and control the Remote MCP gateway.

| Variable | Type | Default | Purpose |
|----------|------|---------|---------|
| `BACKEND_URL` | Var | — | HTTPS URL of the Python HTTP facade (backend) |
| `DEFAULT_AGENT` | Var | `"Agent"` | Fallback agent label when not provided by client |
| `KV_PROJECTS` | Binding | — | KV namespace for per‑user ACLs, session metadata, rate limits |
| `INTERNAL_AUTH_SECRET` | Secret | — | Shared secret for Worker ↔ Backend auth (sent as `X‑Internal‑Auth`) |
| `ALLOW_DEV_SESSION` | Var | `"false"` | Optional (staging only). If `"true"`, allows temporary `?session=dev` testing; never enable in production |
| `AUTO_ENROLL_PROJECTS` | Var | `"false"` | If `"true"`, `set_project`/`create_project` may auto‑add requested project to caller’s ACL after backend validation; prefer explicit ACL seeding |

Notes
- Staging posture is auth‑only by default: keep `ALLOW_DEV_SESSION="false"` and use OAuth or tokens issued at `/console`.
- See `cloudflare-worker/scripts/README.md` for deployment and security guidance.

### WATERCOOLER_GIT_SSH_KEY

**Purpose:** Path to SSH private key for git authentication.

**Required:** No

**Default:** None (uses default SSH keys from `~/.ssh/`)

**Format:** Absolute path to private key file (e.g., `"/Users/agent/.ssh/id_ed25519_watercooler"`)

**Used by:** MCP Server (cloud sync)

**Details:**

Specifies a custom SSH private key for git operations. If not set, git uses the default SSH key resolution:
1. `~/.ssh/id_ed25519`
2. `~/.ssh/id_rsa`
3. SSH agent keys

**When to set:**
- Using dedicated deploy key for watercooler
- Multiple SSH keys for different repositories
- Need to isolate watercooler git access

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_GIT_SSH_KEY="$HOME/.ssh/id_ed25519_watercooler"
```

**MCP config:**
```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_GIT_REPO = "git@github.com:team/threads.git"
WATERCOOLER_GIT_SSH_KEY = "/Users/agent/.ssh/id_ed25519_watercooler"
```

**Generate dedicated key:**
```bash
ssh-keygen -t ed25519 -C "watercooler@myteam" -f ~/.ssh/id_ed25519_watercooler
ssh-add ~/.ssh/id_ed25519_watercooler
```

**Add to GitHub/GitLab:**
1. Copy public key: `cat ~/.ssh/id_ed25519_watercooler.pub`
2. Add as deploy key with write access in repository settings

**Related:**
- See [CLOUD_SYNC_GUIDE.md](../.mothballed/docs/CLOUD_SYNC_GUIDE.md#ssh-key-setup) for detailed setup
- See [WATERCOOLER_GIT_REPO](#watercooler_git_repo) for enabling cloud sync

---

### WATERCOOLER_GIT_AUTHOR

**Purpose:** Git commit author name for cloud sync commits.

**Required:** No

**Default:** `"Watercooler MCP"`

**Format:** String (e.g., `"Claude Agent"`, `"Alice's Claude"`)

**Used by:** MCP Server (cloud sync)

**Details:**

Sets the git author name for commits made by the MCP server. Appears in git history as:
```
Author: Claude Agent <mcp@watercooler.dev>
Date:   Wed Oct 10 08:00:00 2025 +0000

    Claude: Implementation complete (feature-auth)

    Watercooler-Entry-ID: 01HQZXY...
    Watercooler-Topic: feature-auth
```

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_GIT_AUTHOR="Claude Agent"
```

**MCP config:**
```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_GIT_AUTHOR = "Claude Agent"
WATERCOOLER_GIT_EMAIL = "claude@team.com"
```

**Team setup with per-user agents:**
```bash
# Alice's machine
export WATERCOOLER_GIT_AUTHOR="Alice's Claude"

# Bob's machine
export WATERCOOLER_GIT_AUTHOR="Bob's Claude"
```

**Related:**
- See [WATERCOOLER_GIT_EMAIL](#watercooler_git_email) for author email
- See [CLOUD_SYNC_GUIDE.md](../.mothballed/docs/CLOUD_SYNC_GUIDE.md) for team setup

---

### WATERCOOLER_GIT_EMAIL

**Purpose:** Git commit author email for cloud sync commits.

**Required:** No

**Default:** `"mcp@watercooler.dev"`

**Format:** Email string (e.g., `"claude@team.com"`)

**Used by:** MCP Server (cloud sync)

**Details:**

Sets the git author email for commits made by the MCP server.

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_GIT_EMAIL="claude@team.com"
```

**MCP config:**
```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_GIT_AUTHOR = "Claude Agent"
WATERCOOLER_GIT_EMAIL = "claude@team.com"
```

**Related:**
- See [WATERCOOLER_GIT_AUTHOR](#watercooler_git_author) for author name
- See [CLOUD_SYNC_GUIDE.md](../.mothballed/docs/CLOUD_SYNC_GUIDE.md) for team setup

---

## Baseline Graph Variables

Variables for the baseline graph module (free-tier knowledge graph generation).

### BASELINE_GRAPH_API_BASE

**Purpose:** OpenAI-compatible API endpoint for LLM summarization.

**Required:** No

**Default:** `"http://localhost:8000/v1"` (llama-server default)

**Format:** URL string

**Used by:** Baseline Graph Module

**Details:**

The baseline graph module uses local LLMs for generating summaries. This variable sets the API endpoint.

**Configuration examples:**

**Shell:**
```bash
# llama-server (default)
export BASELINE_GRAPH_API_BASE="http://localhost:8000/v1"

# OpenAI
export BASELINE_GRAPH_API_BASE="https://api.openai.com/v1"
```

---

### BASELINE_GRAPH_MODEL

**Purpose:** LLM model name for summarization.

**Required:** No

**Default:** `"qwen3:1.7b"` (recommended)

**Format:** Model identifier string

**Used by:** Baseline Graph Module

**Details:**

Specifies which model to use for LLM-based summarization. The system auto-detects model family and applies optimal prompting (e.g., `/no_think` prefix for Qwen3 models).

**Recommended models:**
- `qwen3:1.7b` - Fast, efficient (auto `/no_think` applied)
- `qwen2.5:3b` - Higher quality, slightly slower
- `llama3.2:3b` - Balanced option

**Configuration examples:**

**Shell:**
```bash
export BASELINE_GRAPH_MODEL="qwen3:1.7b"
```

---

### BASELINE_GRAPH_API_KEY

**Purpose:** API key for LLM endpoint (if required).

**Required:** No

**Default:** `"local"` (local llama-server doesn't require authentication)

**Format:** API key string

**Used by:** Baseline Graph Module

**Details:**

Local llama-server doesn't require authentication. Set this if your endpoint requires an API key (e.g., OpenAI).

---

### BASELINE_GRAPH_TIMEOUT

**Purpose:** Request timeout for LLM calls.

**Required:** No

**Default:** `30.0` seconds

**Format:** Float (seconds)

**Used by:** Baseline Graph Module

**Details:**

If the LLM doesn't respond within this timeout, the module falls back to extractive summarization.

---

### BASELINE_GRAPH_MAX_TOKENS

**Purpose:** Maximum tokens in LLM response.

**Required:** No

**Default:** `256`

**Format:** Integer

**Used by:** Baseline Graph Module

**Details:**

Controls the length of generated summaries. Lower values produce shorter, more concise summaries.

---

### BASELINE_GRAPH_EXTRACTIVE_ONLY

**Purpose:** Force extractive summarization (skip LLM).

**Required:** No

**Default:** `"false"`

**Format:** Boolean string (`"1"`, `"true"`, `"yes"` for enabled)

**Used by:** Baseline Graph Module

**Details:**

When enabled, the module uses pure extractive summarization without calling any LLM. Useful when:
- No local LLM is available
- You want faster processing without network calls
- You want deterministic, reproducible output

**Configuration examples:**

**Shell:**
```bash
export BASELINE_GRAPH_EXTRACTIVE_ONLY="true"
```

**Related:**
- See [Baseline Graph Documentation](baseline-graph.md) for full module documentation

---

### LLM_SYSTEM_PROMPT

**Purpose:** System prompt for chat-style LLMs in summarization.

**Required:** No

**Default:** Auto-detected based on model family

**Format:** String

**Used by:** Baseline Graph Module

**Details:**

When empty (default), the system auto-detects the appropriate system prompt based on model family:
- Qwen3: No system prompt (uses `/no_think` prefix instead)
- Qwen2.5, Llama, others: `"You summarize technical entries concisely with relevant tags."`

Set explicitly to override auto-detection.

**Configuration examples:**

**Shell:**
```bash
export LLM_SYSTEM_PROMPT="You are a technical summarizer."
```

---

### LLM_PROMPT_PREFIX

**Purpose:** Prefix added to user prompt for LLM summarization.

**Required:** No

**Default:** Auto-detected based on model family

**Format:** String

**Used by:** Baseline Graph Module

**Details:**

When empty (default), the system auto-detects the appropriate prefix based on model family:
- Qwen3: `/no_think ` (disables thinking mode for direct output)
- Others: Empty (not needed)

Set explicitly to override auto-detection.

**Configuration examples:**

**Shell:**
```bash
export LLM_PROMPT_PREFIX="/no_think "
```

---

## Graphiti Memory Variables

Variables for querying thread history via Graphiti temporal graph memory. These enable the `watercooler_smart_query` and `watercooler_search` MCP tools.

> **Note:** `watercooler_query_memory` has been replaced by `watercooler_smart_query`. See [mcp-server.md#memory-query-tools](./mcp-server.md#memory-query-tools).

### WATERCOOLER_GRAPHITI_ENABLED

**Purpose:** Master switch to enable Graphiti memory query functionality.

**Required:** No

**Default:** `"0"` (disabled)

**Format:** Boolean string (`"1"` to enable, `"0"` to disable)

**Used by:** MCP Server (memory queries)

**Details:**

Enables the memory query tools (`watercooler_smart_query`, `watercooler_search`) for asking questions about thread history using Graphiti's temporal graph memory. When disabled, the tools return an error message directing users to enable it.

**Prerequisites** (when enabled):
- LLM API key configured (via `LLM_API_KEY` env var or `[memory.llm].api_key` in TOML)
- FalkorDB running locally: `docker run -d -p 6379:6379 falkordb/falkordb:latest`
- Memory extras installed: `pip install 'watercooler-cloud[memory]'`
- Index built via CLI: `python -m watercooler_memory.pipeline run --backend graphiti --threads /path/to/threads`

**Configuration (TOML recommended):**
```toml
# ~/.watercooler/config.toml
[memory]
enabled = true
backend = "graphiti"

[memory.llm]
api_key = "local"  # or your API key
api_base = "http://localhost:8000/v1"  # llama-server
model = "qwen3:30b"
```

**Defaults:**
- Graphiti path: Installed as package (or `WATERCOOLER_GRAPHITI_PATH` for submodule dev)
- Work directory: `~/.watercooler/graphiti`
- FalkorDB host: `localhost`
- FalkorDB port: `6379`
- LLM model: `gpt-4o-mini`

**Configuration examples:**

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.watercooler_cloud.env]
WATERCOOLER_GRAPHITI_ENABLED = "1"
OPENAI_API_KEY = "sk-..."
```

**Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_GRAPHITI_ENABLED": "1",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

**Claude Code (`.mcp.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_GRAPHITI_ENABLED": "1",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

**Shell:**
```bash
export WATERCOOLER_GRAPHITI_ENABLED="1"
export OPENAI_API_KEY="sk-..."
```

**Related:**
- See [GRAPHITI_SETUP.md](./GRAPHITI_SETUP.md) for complete setup guide
- See [MEMORY.md](./MEMORY.md) for memory backend overview
- See [mcp-server.md](./mcp-server.md#memory-query-tools) for tool reference

---

## Memory Backend Configuration

Variables for configuring LLM and embedding servers across memory backends (Graphiti, LeanRAG, MemoryGraph). Each backend has its own configuration, but all can share the same underlying servers.

### WATERCOOLER_MEMORY_DISABLED

**Purpose:** Master switch to disable all memory backend functionality.

**Required:** No

**Default:** Not set (memory enabled)

**Format:** Boolean string (`"1"`, `"true"`, `"yes"` to disable)

**Used by:** All memory backends

**Details:**

When set, completely bypasses all memory backend functionality. Useful for:
- Non-memory workflows that don't need graph backends
- CI environments where memory servers aren't available
- Quick local testing without server dependencies

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_MEMORY_DISABLED="1"
```

**MCP config:**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_MEMORY_DISABLED": "1"
      }
    }
  }
}
```

---

### WATERCOOLER_MEMORY_BACKEND

**Purpose:** Select which memory backend to use for sync operations.

**Required:** No

**Default:** Not set (sync disabled)

**Format:** String (`"graphiti"` or `"leanrag"`)

**Used by:** Memory sync hook

**Details:**

Configures which memory backend receives entry syncs after writes. Options:
- `"graphiti"` - Sync to Graphiti temporal graph (real-time)
- `"leanrag"` - Queue for LeanRAG clustering pipeline (batch)

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_MEMORY_BACKEND="graphiti"
```

---

### Graphiti Backend Variables

Variables used by the Graphiti memory backend.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `WATERCOOLER_GRAPHITI_ENABLED` | No | `"0"` | Enable Graphiti memory queries |
| `LLM_API_BASE` | No | OpenAI | LLM server endpoint |
| `LLM_API_KEY` | Yes* | - | LLM API key (required, no fallback) |
| `LLM_MODEL` | No | `gpt-4o-mini` | LLM model name |
| `EMBEDDING_API_BASE` | No | OpenAI | Embedding server endpoint |
| `EMBEDDING_API_KEY` | Yes* | - | Embedding API key (required, no fallback) |
| `EMBEDDING_MODEL` | No | `text-embedding-3-small` | Embedding model name |
| `WATERCOOLER_GRAPHITI_RERANKER` | No | `rrf` | Reranker algorithm |

*Required when Graphiti is enabled. For local servers, use a dummy value like `"not-needed-for-local"`.

**Note:** If `LLM_API_KEY` or `EMBEDDING_API_KEY` is not set, there is a temporary fallback to `OPENAI_API_KEY` with a deprecation warning. This fallback will be removed in a future release - please set `LLM_API_KEY` and `EMBEDDING_API_KEY` explicitly.

**Local server example:**
```bash
# Local embedding server (port 8080)
EMBEDDING_API_BASE=http://localhost:8080/v1
EMBEDDING_API_KEY=not-needed-for-local
EMBEDDING_MODEL=bge-m3

# Local LLM server (port 8000)
LLM_API_BASE=http://localhost:8000/v1
LLM_API_KEY=not-needed-for-local
LLM_MODEL=local
```

---

### LeanRAG Backend Variables

Variables used by the LeanRAG memory backend (loaded from `external/LeanRAG/config.yaml`).

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `GLM_BASE_URL` | No | - | Embedding server endpoint |
| `GLM_MODEL` | No | - | Embedding model name |
| `GLM_EMBEDDING_MODEL` | No | - | Embedding model (alternative) |
| `DEEPSEEK_BASE_URL` | No | - | LLM server endpoint |
| `DEEPSEEK_API_KEY` | No | - | LLM API key |
| `DEEPSEEK_MODEL` | No | - | LLM model name |

**Local server example (matching Graphiti):**
```bash
# LeanRAG embedding config (same server as Graphiti)
GLM_BASE_URL=http://localhost:8080/v1
GLM_MODEL=bge-m3
GLM_EMBEDDING_MODEL=bge-m3

# LeanRAG LLM config (same server as Graphiti)
DEEPSEEK_BASE_URL=http://localhost:8000/v1
DEEPSEEK_API_KEY=not-needed-for-local
DEEPSEEK_MODEL=local
```

---

### FalkorDB Variables

Variables for the FalkorDB graph database (used by Graphiti and LeanRAG).

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `FALKORDB_HOST` | No | `localhost` | FalkorDB host |
| `FALKORDB_PORT` | No | `6379` | FalkorDB port |
| `FALKORDB_PASSWORD` | No | - | FalkorDB password |

**Start FalkorDB:**
```bash
docker run -d -p 6379:6379 falkordb/falkordb:latest
```

---

### Validating Memory Configuration

Use the validation script to check all backends are configured consistently:

```bash
# Basic check (warn-only)
python scripts/check-memory-config.py

# Strict mode (exit non-zero on issues)
python scripts/check-memory-config.py --strict

# Skip health checks (for offline/CI)
python scripts/check-memory-config.py --skip-health-check

# Generate .env template
python scripts/check-memory-config.py --generate-env
```

**Example output:**
```
Memory Backend Configuration Check
===================================

Embedding Server
================
  ✓ http://localhost:8080/v1: healthy (bge-m3)

  Backend Configuration:
    Graphiti (EMBEDDING_API_BASE): http://localhost:8080/v1
    LeanRAG (GLM_BASE_URL):        http://localhost:8080/v1

LLM Server
==========
  ✓ http://localhost:8000/v1: healthy (local)

FalkorDB
========
  ✓ localhost:6379: connected

Summary
=======
All backends configured consistently ✓
```

---

## HTTP Transport Variables (Hosted Mode)

These variables configure the HTTP transport for hosted MCP deployment. The HTTP server enables running watercooler-cloud as a hosted service that web applications can call directly.

### WATERCOOLER_MCP_TRANSPORT

**Purpose:** Select transport mode for the MCP server.

**Required:** No

**Default:** `"stdio"`

**Format:** String (`"stdio"` or `"http"`)

**Used by:** MCP Server

**Details:**

- `stdio` - Standard input/output mode for local MCP clients (Claude Code, Cursor, etc.)
- `http` - HTTP server mode for hosted deployments (watercooler-site, other web apps)

**Configuration examples:**

**Shell:**
```bash
WATERCOOLER_MCP_TRANSPORT=http python -m watercooler_mcp
```

---

### WATERCOOLER_MCP_HOST

**Purpose:** HTTP server bind host address.

**Required:** No

**Default:** `"0.0.0.0"` (all interfaces)

**Format:** IP address or hostname string

**Used by:** HTTP Server

---

### WATERCOOLER_MCP_PORT

**Purpose:** HTTP server bind port.

**Required:** No

**Default:** `8080`

**Format:** Integer port number

**Used by:** HTTP Server

---

### WATERCOOLER_AUTH_MODE

**Purpose:** Authentication mode for GitHub API access.

**Required:** No

**Default:** `"local"`

**Format:** String (`"local"` or `"hosted"`)

**Used by:** HTTP Server (auth module)

**Details:**

- `local` - Use local git credentials (SSH keys, credential helper, environment tokens)
- `hosted` - Fetch GitHub OAuth tokens from the token service API

**When to use `hosted`:**
- Running as a multi-user service (watercooler-site dashboard, Slack integration)
- Each request includes a `X-User-ID` header identifying the user
- Tokens are fetched from the token service and cached for the request

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_AUTH_MODE="hosted"
export WATERCOOLER_TOKEN_API_URL="https://watercoolerdev.com"
export WATERCOOLER_TOKEN_API_KEY="your-api-key"
```

---

### WATERCOOLER_TOKEN_API_URL

**Purpose:** Base URL of the token service that provides GitHub OAuth tokens.

**Required:** Yes (in hosted mode)

**Default:** Empty (token service disabled)

**Format:** URL string (e.g., `"https://watercoolerdev.com"`)

**Used by:** HTTP Server (auth module)

**Details:**

The MCP server calls `GET {url}/api/github/token?userId={user_id}` to fetch tokens.
The token service must:
1. Authenticate the request using the API key
2. Look up the user's encrypted GitHub OAuth token
3. Decrypt and return the token

**Related:**
- See [watercooler-site Token Service](#token-service-api) for API contract

---

### WATERCOOLER_TOKEN_API_KEY

**Purpose:** API key for authenticating with the token service.

**Required:** Yes (in hosted mode)

**Default:** Empty

**Format:** String

**Used by:** HTTP Server (auth module)

**Details:**

Sent as `x-api-key` header when fetching tokens from the token service.
Generate a secure random key and configure it in both the MCP server and token service.

---

### WATERCOOLER_CACHE_BACKEND

**Purpose:** Select cache backend for reducing API calls.

**Required:** No

**Default:** `"memory"`

**Format:** String (`"memory"` or `"database"`)

**Used by:** HTTP Server (cache module)

**Details:**

- `memory` - In-memory cache (per-process, lost on restart). Suitable for:
  - Local MCP (STDIO mode)
  - Single-process deployments
  - Development/testing

- `database` - Remote cache via watercooler-site API. Suitable for:
  - Serverless deployments (Vercel, AWS Lambda)
  - Multi-process/multi-instance deployments
  - Persistent caching across restarts

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_CACHE_BACKEND="memory"
export WATERCOOLER_CACHE_TTL="300"
```

---

### WATERCOOLER_CACHE_TTL

**Purpose:** Default time-to-live for cached entries.

**Required:** No

**Default:** `300` (5 minutes)

**Format:** Integer (seconds)

**Used by:** HTTP Server (cache module)

**Details:**

Controls how long cached data (thread content, graph nodes, API responses) is retained before expiring.

- Lower values (60-120): More API calls, fresher data
- Higher values (300-600): Fewer API calls, potential staleness

---

### WATERCOOLER_CORS_ORIGINS

**Purpose:** Allowed origins for CORS requests.

**Required:** No

**Default:** `"*"` (all origins)

**Format:** Comma-separated list of origins

**Used by:** HTTP Server

**Details:**

Configure for production deployments to restrict which domains can call the API.

**Configuration examples:**

**Shell:**
```bash
# Allow specific origins
export WATERCOOLER_CORS_ORIGINS="https://watercoolerdev.com,https://app.watercooler.dev"

# Allow all (development)
export WATERCOOLER_CORS_ORIGINS="*"
```

---

## Advanced Variables

### WATERCOOLER_TEMPLATES

**Purpose:** Override path to custom templates directory.

**Required:** No

**Default:** Built-in templates (from `src/watercooler/templates/`)

**Format:** Absolute path to directory containing template files

**Used by:** MCP Server & CLI

**Details:**

Allows customization of thread and entry templates. Templates use placeholder syntax:
- `{{KEY}}` or `<KEY>` for variable substitution
- Available placeholders: `TOPIC`, `AGENT`, `UTC`, `TITLE`, `BODY`, `TYPE`, `ROLE`, `BALL`, `STATUS`

**Template files:**
- `_TEMPLATE_topic_thread.md` - New thread initialization
- `_TEMPLATE_entry_block.md` - Entry format

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_TEMPLATES="/Users/agent/my-templates"
```

**CLI:**
```bash
watercooler say feature-auth --title "Update" --body "..." --templates-dir /path/to/templates
```

**Custom template example (`_TEMPLATE_entry_block.md`):**
```markdown
---
Entry: {{AGENT}} {{UTC}}
Type: {{TYPE}}
Title: {{TITLE}}

{{BODY}}
```

**Related:**
- See [archive/TEMPLATES.md](./archive/TEMPLATES.md) for template customization guide
- See [archive/integration.md](./archive/integration.md) for Python API usage

---

### WATERCOOLER_USER

**Purpose:** Override username for lock file metadata.

**Required:** No

**Default:** OS username from `getpass.getuser()`

**Format:** String (e.g., `"agent"`, `"alice"`)

**Used by:** Lock System (internal)

**Details:**

**This is a low-level variable that most users don't need to set.**

Used only by the advisory locking system to record who owns a lock. Does **not** affect agent identity in entries (see [`WATERCOOLER_AGENT`](#watercooler_agent) for that).

**Automatic behavior:**

The lock system automatically determines username via `getpass.getuser()` which checks:
1. `os.getlogin()` - login name of user running process
2. Environment variables: `$LOGNAME`, `$USER`, `$LNAME`, `$USERNAME`
3. System user database: `pwd.getpwuid(os.getuid())[0]`

**When to set:**
- Running in containerized environment with wrong username
- Testing lock behavior with different identities
- Debugging lock contention issues

**Configuration examples:**

**Shell:**
```bash
export WATERCOOLER_USER="alice"
```

**Lock file format:**
```
PID: 12345
User: alice
Host: macbook.local
Created: 2025-10-10T08:00:00Z
```

**Related:**
- See [archive/integration.md](./archive/integration.md#locking-configuration) for lock system details
- See [FAQ.md](./FAQ.md) for lock behavior explanation

---

## Configuration Patterns

### Basic MCP Setup (Local Mode)

**Minimal configuration for single developer:**

```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_AGENT = "Claude"
```

**Explanation:**
- Threads are stored automatically in the sibling `<repo>-threads` directory
- No cloud sync
- Works from any subdirectory in the project (pass `code_path` with each tool call)

---

### Manual Threads Directory

**Use only for short-lived experiments or specialized testing:**

```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_AGENT = "Codex"
WATERCOOLER_DIR = "/srv/watercooler/custom-project-threads"
```

**Use cases:**
- Temporarily reading threads left in a repo-local staging folder before relocating them
- Running in environments without git metadata (e.g., ad-hoc scripts)
- Experimental setups where a bespoke location is required

---

### Cloud Sync (Team Collaboration)

**Full setup for distributed team:**

```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_AGENT = "Claude"
WATERCOOLER_GIT_REPO = "git@github.com:team/threads.git"
WATERCOOLER_THREADS_BASE = "/srv/watercooler-threads"
WATERCOOLER_GIT_SSH_KEY = "/Users/agent/.ssh/id_ed25519_watercooler"
WATERCOOLER_GIT_AUTHOR = "Alice's Claude"
WATERCOOLER_GIT_EMAIL = "alice+claude@team.com"
```

**Explanation:**
- `WATERCOOLER_GIT_REPO` enables cloud mode
- `WATERCOOLER_THREADS_BASE` controls where clones live locally
- `WATERCOOLER_GIT_SSH_KEY` uses dedicated deploy key
- `WATERCOOLER_GIT_AUTHOR/EMAIL` customize git identity

**Related:**
- See [CLOUD_SYNC_GUIDE.md](../.mothballed/docs/CLOUD_SYNC_GUIDE.md) for complete cloud setup

---

### Multiple Projects

**Dynamic directory per project:**

```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_AGENT = "Claude"
# No additional environment variables needed
```

The server resolves the correct sibling threads repository for whichever code repo you open (based on `code_path`).

**Per-project overrides:**

If you still maintain repo-local threads directories, you can set `WATERCOOLER_DIR` in a `.envrc`, but this disables universal discovery. Prefer converting those projects to the sibling repo layout.

---

## Troubleshooting
### Git authentication errors

If Watercooler cannot push/pull threads, double-check the remote pattern:

- **HTTPS (default)** – requires Git Credential Manager / PAT access to `https://github.com/{org}/{repo}-threads.git`.
- **SSH** – requires your SSH key/agent and uses `git@github.com:{org}/{repo}-threads.git`.

Switch by exporting `WATERCOOLER_THREADS_PATTERN` before launching the MCP server, then retry the command.


### Wrong Agent Name

**Symptom:** Entries show incorrect agent name

**Check current identity:**
```bash
# MCP: Ask agent to call watercooler_whoami
# CLI: Check environment
echo $WATERCOOLER_AGENT
```

**Fix:**
```bash
export WATERCOOLER_AGENT="CorrectName"
# Or update MCP config and restart client
```

---

### Directory Not Found

**Symptom:** MCP server can't find threads directory

**Check resolution:**
```bash
# MCP: Ask agent to call watercooler_health
# Look for "Threads Dir: /path" in output
```

**Fix options:**

1. **Use universal defaults:** `watercooler_health(code_path=".")` will report the expected sibling directory (for example `/workspace/<repo>-threads`). Ensure that path exists and is writable.

2. **Override location (manual):**
   ```bash
   export WATERCOOLER_DIR="/full/path/to/custom-threads"
   ```
   Only do this when relocating a staging folder that you created manually inside the code repo.

---

### Cloud Sync Not Working

**Symptom:** Changes not syncing across machines

**Verify cloud mode enabled:**
```bash
echo $WATERCOOLER_GIT_REPO
# Should output git URL, not empty
```

**Check git access:**
```bash
cd ../<repo>-threads
git pull
# Should succeed without errors
```

**Common issues:**
- SSH key not configured → See [CLOUD_SYNC_GUIDE.md](../.mothballed/docs/CLOUD_SYNC_GUIDE.md#ssh-key-setup)
- Wrong repository URL → Verify `WATERCOOLER_GIT_REPO`
- Directory not a git clone → Re-clone repository

---

## See Also

- **[QUICKSTART.md](./QUICKSTART.md)** - Basic setup and configuration
- **[MCP Server Guide](./mcp-server.md)** - MCP server documentation
- **[CLOUD_SYNC_GUIDE.md](../.mothballed/docs/CLOUD_SYNC_GUIDE.md)** - Team collaboration setup
- **[archive/integration.md](./archive/integration.md)** - Python library configuration
- **[TROUBLESHOOTING.md](./TROUBLESHOOTING.md)** - Common issues and solutions
- **[archive/TEMPLATES.md](./archive/TEMPLATES.md)** - Template customization

---

*Last updated: 2025-10-10*
