# Environment Variables Reference

**Advanced Configuration Guide** - Complete reference for all watercooler-cloud environment variables.

> **Note:** Basic setup requires NO environment variables. The [Installation Guide](INSTALLATION.md) covers the minimal configuration using credentials file + MCP config. Use these environment variables only for advanced customization.

---

## Quick Reference

| Variable | Required | Default | Used By | Purpose |
|----------|----------|---------|---------|---------|
| [`WATERCOOLER_AGENT`](#watercooler_agent) | No (auto-detected) | Auto from client | MCP Server | Override agent identity |
| [`WATERCOOLER_DIR`](#watercooler_dir) | No | _Unset_ | MCP & CLI | Manual override for threads directory (bypasses orphan branch worktree) |
| [`WATERCOOLER_GIT_REPO`](#watercooler_git_repo) | Cloud: Yes<br>Local: No | None | MCP Server | Git repository URL (enables cloud sync) |
| [`WATERCOOLER_GIT_SSH_KEY`](#watercooler_git_ssh_key) | No | None | MCP Server | Path to SSH private key |
| [`WATERCOOLER_GIT_AUTHOR`](#watercooler_git_author) | No | `"Watercooler MCP"` | MCP Server | Git commit author name |
| [`WATERCOOLER_GIT_EMAIL`](#watercooler_git_email) | No | `"mcp@watercooler.dev"` | MCP Server | Git commit author email |
| [`WATERCOOLER_TEMPLATES`](#watercooler_templates) | No | Built-in | MCP & CLI | Custom templates directory |
| [`WATERCOOLER_USER`](#watercooler_user) | No | OS username | Lock System | Override username in lock files |
| [`BASELINE_GRAPH_API_BASE`](#baseline_graph_api_base) | No | `http://localhost:8000/v1` | Baseline Graph | LLM API endpoint |
| [`BASELINE_GRAPH_MODEL`](#baseline_graph_model) | No | `qwen3:1.7b` | Baseline Graph | LLM model name |
| [`BASELINE_GRAPH_EXTRACTIVE_ONLY`](#baseline_graph_extractive_only) | No | `false` | Baseline Graph | Force extractive mode |

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

### WATERCOOLER_DIR

**Purpose:** Manual override for threads directory (bypasses orphan branch worktree).

**Required:** No

**Default:** _Unset_ (uses `~/.watercooler/worktrees/<repo>/` via orphan branch)

**Format:** Absolute or relative path (e.g., `"/srv/watercooler/custom-threads"`)

**Used by:** MCP Server & CLI

**Details:**

By default, threads are stored on a `watercooler/threads` orphan branch accessed
via a git worktree at `~/.watercooler/worktrees/<repo>/`. Set this variable only
when you need threads in a custom location (e.g., testing, debugging).

**When to set explicitly:**
- Running in an environment without git metadata (rare)
- Executing targeted tests that need an isolated threads sandbox
- Temporarily pointing at a staging directory for debugging

**Configuration examples:**

**Absolute path example:**
```bash
export WATERCOOLER_DIR="/srv/watercooler/custom-project-threads"
```

**MCP config example:**
```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_DIR = "/srv/watercooler/custom-project-threads"
```

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

**Creating a GitHub Personal Access Token:**

1. Go to https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Select scopes:
   - `repo` (Full control of private repositories)
   - `read:org` (Read org and team membership)
   - `read:user` (Read user profile data)
4. Click "Generate token"
5. Copy the token and add to environment

**Security:**
- Tokens are stored in environment variables (not committed to git)
- Credential helper only activates for HTTPS GitHub URLs
- Tokens have specific scoped permissions
- Never shared with third parties

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
```

**HTTPS:**
```bash
export WATERCOOLER_GIT_REPO="https://github.com/my-team/watercooler-threads.git"
```

---

### WATERCOOLER_GIT_SSH_KEY

**Purpose:** Path to SSH private key for git authentication.

**Required:** No

**Default:** None (uses default SSH keys from `~/.ssh/`)

**Format:** Absolute path to private key file

**Used by:** MCP Server (cloud sync)

**Details:**

Specifies a custom SSH private key for git operations. If not set, git uses the default SSH key resolution:
1. `~/.ssh/id_ed25519`
2. `~/.ssh/id_rsa`
3. SSH agent keys

**Generate dedicated key:**
```bash
ssh-keygen -t ed25519 -C "watercooler@myteam" -f ~/.ssh/id_ed25519_watercooler
ssh-add ~/.ssh/id_ed25519_watercooler
```

---

### WATERCOOLER_GIT_AUTHOR

**Purpose:** Git commit author name for cloud sync commits.

**Required:** No

**Default:** `"Watercooler MCP"`

**Format:** String (e.g., `"Claude Agent"`, `"Alice's Claude"`)

**Used by:** MCP Server (cloud sync)

```bash
export WATERCOOLER_GIT_AUTHOR="Claude Agent"
```

---

### WATERCOOLER_GIT_EMAIL

**Purpose:** Git commit author email for cloud sync commits.

**Required:** No

**Default:** `"mcp@watercooler.dev"`

**Format:** Email string (e.g., `"claude@team.com"`)

**Used by:** MCP Server (cloud sync)

```bash
export WATERCOOLER_GIT_EMAIL="claude@team.com"
```

---

## Baseline Graph Variables

Variables for the baseline graph module (free-tier knowledge graph generation).

### BASELINE_GRAPH_API_BASE

**Purpose:** OpenAI-compatible API endpoint for LLM summarization.

**Required:** No

**Default:** `"http://localhost:8000/v1"` (llama-server default)

**Format:** URL string

**Used by:** Baseline Graph Module

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

**Recommended models:**
- `qwen3:1.7b` - Fast, efficient (auto `/no_think` applied)
- `qwen2.5:3b` - Higher quality, slightly slower
- `llama3.2:3b` - Balanced option

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

---

### BASELINE_GRAPH_TIMEOUT

**Purpose:** Request timeout for LLM calls.

**Required:** No

**Default:** `30.0` seconds

**Format:** Float (seconds)

**Used by:** Baseline Graph Module

If the LLM doesn't respond within this timeout, the module falls back to extractive summarization.

---

### BASELINE_GRAPH_MAX_TOKENS

**Purpose:** Maximum tokens in LLM response.

**Required:** No

**Default:** `256`

**Format:** Integer

**Used by:** Baseline Graph Module

Controls the length of generated summaries. Lower values produce shorter, more concise summaries.

---

### BASELINE_GRAPH_EXTRACTIVE_ONLY

**Purpose:** Force extractive summarization (skip LLM).

**Required:** No

**Default:** `"false"`

**Format:** Boolean string (`"1"`, `"true"`, `"yes"` for enabled)

**Used by:** Baseline Graph Module

When enabled, the module uses pure extractive summarization without calling any LLM. Useful when:
- No local LLM is available
- You want faster processing without network calls
- You want deterministic, reproducible output

**Related:**
- See [Baseline Graph Documentation](baseline-graph.md) for full module documentation

---

### LLM_SYSTEM_PROMPT

**Purpose:** System prompt for chat-style LLMs in summarization.

**Required:** No

**Default:** Auto-detected based on model family

**Format:** String

**Used by:** Baseline Graph Module

When empty (default), the system auto-detects the appropriate system prompt based on model family:
- Qwen3: No system prompt (uses `/no_think` prefix instead)
- Qwen2.5, Llama, others: `"You summarize technical entries concisely with relevant tags."`

---

### LLM_PROMPT_PREFIX

**Purpose:** Prefix added to user prompt for LLM summarization.

**Required:** No

**Default:** Auto-detected based on model family

**Format:** String

**Used by:** Baseline Graph Module

When empty (default), the system auto-detects the appropriate prefix based on model family:
- Qwen3: `/no_think ` (disables thinking mode for direct output)
- Others: Empty (not needed)

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

```bash
export WATERCOOLER_TEMPLATES="/Users/agent/my-templates"
```

---

### WATERCOOLER_USER

**Purpose:** Override username for lock file metadata.

**Required:** No

**Default:** OS username from `getpass.getuser()`

**Format:** String (e.g., `"agent"`, `"alice"`)

**Used by:** Lock System (internal)

**This is a low-level variable that most users don't need to set.**

Used only by the advisory locking system to record who owns a lock. Does **not** affect agent identity in entries (see [`WATERCOOLER_AGENT`](#watercooler_agent) for that).

---

## Configuration Patterns

### Basic MCP Setup (Local Mode)

**Minimal configuration for single developer:**

```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_AGENT = "Claude"
```

**Explanation:**
- Threads are stored on the `watercooler/threads` orphan branch, accessed via worktree
- Synced to origin automatically on push
- Works from any subdirectory in the project (pass `code_path` with each tool call)

---

### Cloud Sync (Team Collaboration)

**Full setup for distributed team:**

```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_AGENT = "Claude"
WATERCOOLER_GIT_SSH_KEY = "/Users/agent/.ssh/id_ed25519_watercooler"
WATERCOOLER_GIT_AUTHOR = "Alice's Claude"
WATERCOOLER_GIT_EMAIL = "alice+claude@team.com"
```

Threads are automatically pushed to the code repo's origin on the orphan branch.

---

### Multiple Projects

**Dynamic directory per project:**

```toml
[mcp_servers.wc_universal.env]
WATERCOOLER_AGENT = "Claude"
# No additional environment variables needed
```

Each project's threads live on its own `watercooler/threads` orphan branch. No
extra configuration is needed — threads are automatically scoped to the code
repository. Just pass `code_path` pointing to each project root.

---

## Troubleshooting
### Git authentication errors

If Watercooler cannot push/pull the orphan branch, double-check your git credentials:

- **HTTPS (default)** -- requires Git Credential Manager or PAT for the code repo's origin.
- **SSH** -- requires your SSH key/agent for the code repo's SSH remote.

Ensure a manual `git push` succeeds in your code repo, then restart the MCP server.


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

1. **Use universal defaults:** `watercooler_health(code_path=".")` will report the worktree path (e.g., `~/.watercooler/worktrees/<repo>/`). The worktree is created automatically on first write.

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
cd ~/.watercooler/worktrees/<repo>
git pull
# Should succeed without errors
```

---

## See Also

- **[QUICKSTART.md](./QUICKSTART.md)** - Basic setup and configuration
- **[MCP Server Guide](./mcp-server.md)** - MCP server documentation
- **[TROUBLESHOOTING.md](./TROUBLESHOOTING.md)** - Common issues and solutions

---

*Last updated: 2025-10-10*
