# Watercooler MCP Server - Troubleshooting Guide

Common issues and solutions for the watercooler MCP server.

> Threads live on the `watercooler/threads` orphan branch, accessed via a worktree at `~/.watercooler/worktrees/<repo>/`.

> Start with [INSTALLATION.md](INSTALLATION.md) to ensure you're following the universal flow. Many issues disappear once `code_path` and identity are configured there.

## Table of Contents

- [Quick Diagnostic Flowchart](#quick-diagnostic-flowchart)
- [Quick Health Check](#quick-health-check)
- [Common Issues](#common-issues)
  - [Server Not Loading](#server-not-loading)
  - [Wrong Agent Identity](#wrong-agent-identity)
  - [Threads Directory Not Found](#threads-directory-not-found)
  - [Permission Errors](#permission-errors)
  - [Client ID is None](#client-id-is-none)
  - [Tools Not Working](#tools-not-working)
  - [JSON Parse Error: Unexpected EOF (mcp-cli)](#json-parse-error-unexpected-eof-mcp-cli)
  - [mcp-cli call Returns Empty Output](#mcp-cli-call-returns-empty-output)
  - [Git Not Found](#git-not-found)
  - [Git Authentication](#git-authentication)
  - [GitHub CLI Token Expiration](#github-cli-token-expiration)
  - [SSH Agent Issues (WSL2/Headless)](#ssh-agent-issues-wsl2headless)
  - [Git Sync Issues (Cloud Mode)](#git-sync-issues-cloud-mode)
- [Worktree Issues](#worktree-issues)
- [Thread folder inside code repo](#thread-folder-inside-code-repo)
- [Ball Not Flipping](#ball-not-flipping)
- [Server Crashes or Hangs](#server-crashes-or-hangs)
- [Cache Management](#cache-management)
- [Format Parameter Errors](#format-parameter-errors)
- [Getting More Help](#getting-more-help)

---

## Quick Diagnostic Flowchart

Use this decision tree to quickly find the solution to your problem:

```mermaid
graph TD
    Start[What's the problem?] --> Q1{Are tools<br/>appearing in<br/>your client?}

    Q1 -->|No| ServerNotLoading[<b>Server Not Loading</b><br/>Jump to section below]
    Q1 -->|Yes| Q2{Are tools<br/>working when<br/>called?}

    Q2 -->|No| Q3{What error<br/>do you see?}
    Q2 -->|Yes| Q4{What specific<br/>issue?}

    Q3 -->|"format not supported"| FormatError[<b>Format Parameter Errors</b><br/>Jump to section below]
    Q3 -->|"directory not found"| DirNotFound[<b>Threads Directory Not Found</b><br/>Jump to section below]
    Q3 -->|"permission denied"| PermError[<b>Permission Errors</b><br/>Jump to section below]
    Q3 -->|"git command not found"| GitNotFound[<b>Git Not Found</b><br/>Jump to section below]
    Q3 -->|Git sync errors| GitSync[<b>Git Sync Issues</b><br/>Jump to section below]
    Q3 -->|"authentication failed"| GHAuth[<b>GitHub CLI Token Expiration</b><br/>Jump to section below]
    Q3 -->|"worktree" or "orphan branch"| WorktreeError[<b>Worktree Issues</b><br/>Jump to section below]
    Q3 -->|"JSON Parse error"| JsonEof[<b>JSON Parse Error: Unexpected EOF</b><br/>Jump to section below]
    Q3 -->|Other errors| ToolError[<b>Tools Not Working</b><br/>Jump to section below]

    Q4 -->|Wrong agent name| WrongAgent[<b>Wrong Agent Identity</b><br/>Jump to section below]
    Q4 -->|Ball not flipping| BallNotFlip[<b>Ball Not Flipping</b><br/>Jump to section below]
    Q4 -->|Can't find threads| StrayPaths[<b>Thread Folder Inside Repo</b><br/>Jump to section below]
    Q4 -->|Server crashes| Crashes[<b>Server Crashes or Hangs</b><br/>Jump to section below]
    Q4 -->|"Client ID is None"| ClientIDNone[<b>Client ID is None</b><br/>Jump to section below]

    style ServerNotLoading fill:#ffcccc
    style FormatError fill:#ffcccc
    style DirNotFound fill:#ffcccc
    style PermError fill:#ffcccc
    style GitNotFound fill:#ffcccc
    style GitSync fill:#ffcccc
    style GHAuth fill:#ffcccc
    style WorktreeError fill:#ffcccc
    style JsonEof fill:#ffcccc
    style ToolError fill:#ffcccc
    style WrongAgent fill:#ffffcc
    style BallNotFlip fill:#ffffcc
    style StrayPaths fill:#ffffcc
    style Crashes fill:#ffcccc
    style ClientIDNone fill:#ccffcc
```

**Legend:**
- Red boxes: Critical issues preventing basic functionality
- Yellow boxes: Configuration issues affecting behavior
- Green boxes: Informational (not actually a problem)

---

## Quick Health Check

Before diving into specific issues, always start with the health check:

```bash
# In your MCP client, call:
watercooler_health
```

This returns:
- Server version
- Agent identity
- Threads directory location
- Directory existence status
- Python version
- FastMCP version

**Use this output when reporting issues!**

---

## Common Issues

## Server Not Loading

### Symptom
MCP tools don't appear in your client (Claude Desktop, Claude Code, Codex).

### Solutions

1. **Verify installation**
   ```bash
   python3 -m watercooler_mcp
   ```
   Should display FastMCP banner and start server.

2. **Check configuration file syntax**
   - **Codex**: Verify `~/.codex/config.toml` is valid TOML
   - **Claude Desktop**: Verify `claude_desktop_config.json` is valid JSON
   - **Claude Code**: Verify `.mcp.json` is valid JSON

3. **Restart your client**
   - Codex: Restart the CLI session
   - Claude Desktop: Quit and relaunch the app
   - Claude Code: Reload window (Cmd+R or Ctrl+R)

4. **Check Python path**
   ```bash
   which python3
   ```
   Ensure the `python3` in your config matches your installation.

5. **Verify dependencies**
   ```bash
   pip list | grep -E "(fastmcp|mcp)"
   ```
   Should show `fastmcp>=2.0` and `mcp>=1.0`.

## Wrong Agent Identity

### Symptom
`watercooler_whoami` shows incorrect or unexpected agent name.

### Solutions

1. **Use identity tool before writing**
   Call `watercooler_set_agent(base="Claude Code", spec="implementer-code")` before any write operations (say, ack, handoff, set_status).

2. **Alternative: Use agent_func parameter**
   Supply `agent_func="<platform>:<model>:<role>"` on each write call (e.g., `"Claude Code:sonnet-4:implementer"`).

3. **Client ID auto-detection**
   - "Claude Desktop" -> "Claude"
   - "Claude Code" -> "Claude Code"
   - "Codex" -> "Codex"
   - "Cursor" -> "Cursor"
   - Other values passed through as-is

See [STRUCTURED_ENTRIES.md](STRUCTURED_ENTRIES.md#identity-pre-flight) for complete identity requirements.

## Threads Directory Not Found

### Symptom
```
No threads directory found at: /some/path/threads-local
```

### Solutions

1. **Confirm `code_path` is present**
   - Every tool call must include `code_path` (e.g., `"."`) so the server can resolve the repo/branch
   - Missing `code_path` is the most common cause of this error in universal mode

2. **Check the health output**
   ```bash
   watercooler_health(code_path=".")
   ```
   Expect `Threads Dir` to live in the worktree at `~/.watercooler/worktrees/<repo>/`

3. **Remove manual overrides**
   - Unset `WATERCOOLER_DIR` in your environment or MCP config
   - Re-register the MCP server using the universal command in the installation guide

4. **Ensure git metadata is available**
   - `code_path` must point to a git repository with a configured `origin`
   - If the repo is detached (no remote), set `WATERCOOLER_CODE_REPO` manually or add a remote

5. **Advanced: force a directory**
   - If you intentionally need a bespoke location, set `WATERCOOLER_DIR` to an absolute path and create it ahead of time
   - Remember this disables universal discovery -- use sparingly

## Permission Errors

### Symptom
```
PermissionError: [Errno 13] Permission denied: '/home/user/.watercooler/worktrees/my-repo/thread.md'
```

### Solutions

1. **Check directory permissions**
   ```bash
   THREADS_DIR="$HOME/.watercooler/worktrees/<repo>"
   ls -la "$THREADS_DIR"
   ```

   Should be writable by your user:
   ```bash
   chmod 755 "$THREADS_DIR"
   ```

2. **Check file permissions**
   ```bash
   chmod 644 "$THREADS_DIR"/*.md
   ```

3. **Verify ownership**
   ```bash
   chown -R "$USER" "$THREADS_DIR"
   ```

## Client ID is None

### Symptom
`watercooler_whoami` shows `Client ID: None`

### Explanation
This is **normal for local STDIO connections**. The `client_id` is:
- Populated when using OAuth authentication (FastMCP Cloud)
- `None` for local STDIO transport (Claude Desktop, Claude Code, Codex)

### Solutions

1. **For local usage**: This is expected and doesn't affect functionality
   - Agent identity is set via `watercooler_set_agent` tool or `agent_func` parameter
   - Everything works normally

## Tools Not Working

### Symptom
Tool calls fail or return errors.

### Solutions

1. **Check tool name**
   All tools are namespaced: `watercooler_*`

   Correct:
   ```
   watercooler_list_threads
   watercooler_say
   ```

   Incorrect:
   ```
   list_threads
   say
   ```

2. **Verify tool availability**
   Check your client's tool list:
   - All prefixed with `watercooler_`

3. **Check parameters**
   Each tool has required parameters. Example:
   ```
   watercooler_say(
       topic="required",
       title="required",
       body="required"
   )
   ```

4. **Review error message**
   Error messages include helpful context:
   ```
   Error adding entry to 'topic': [specific error]
   ```

## JSON Parse Error: Unexpected EOF (mcp-cli)

### Symptom
```
Error: Invalid JSON arguments
SyntaxError: JSON Parse error: Unexpected EOF
```
When calling watercooler tools via `mcp-cli` in Claude Code.

### Cause
The `mcp-cli` binary does not support file redirection to stdin:
```bash
# BROKEN
mcp-cli call watercooler-cloud/watercooler_say - < /tmp/payload.json
```

### Solution
Use the two-step variable pattern — assign JSON to a variable first, then pass it:
```bash
PAYLOAD=$(jq -n \
  --arg topic 'my-topic' \
  --arg title 'My Title' \
  --arg body 'Body content' \
  '{topic: $topic, title: $title, body: $body}') && \
mcp-cli call watercooler-cloud/watercooler_say "$PAYLOAD"
```

## mcp-cli call Returns Empty Output

### Symptom
`mcp-cli call` returns no output, no error, and no side effect. The command
appears to succeed (exit code 0) but nothing happens.

### Cause
Inline `$(...)` command substitution in `mcp-cli call` arguments can be
corrupted by Claude Code's Bash tool eval wrapper. This is especially
common with complex payloads (~2 KB+ bodies, 5+ `--arg` flags, or multi-line
content).

```bash
# UNRELIABLE — may silently produce empty output
mcp-cli call watercooler-cloud/watercooler_say "$(jq -n \
  --arg topic 'my-topic' \
  --arg title 'My Title' \
  --arg body 'Long multi-line body...' \
  '{topic: $topic, title: $title, body: $body}')"
```

Note: `mcp-cli info` always works because it takes no complex arguments.

### Solution
Use the two-step variable pattern:
```bash
PAYLOAD=$(jq -n \
  --arg topic 'my-topic' \
  --arg title 'My Title' \
  --arg body 'Long multi-line body...' \
  '{topic: $topic, title: $title, body: $body}') && \
mcp-cli call watercooler-cloud/watercooler_say "$PAYLOAD"
```

The variable assignment happens outside the eval context, so `mcp-cli`
receives a plain string. See Claude Code issues
[#24956](https://github.com/anthropics/claude-code/issues/24956) and
[#11551](https://github.com/anthropics/claude-code/issues/11551).

### Also Check
- `mcp-cli call tool '{}' > /tmp/out.json` will fail with `command not found`
  because `mcp-cli` is a shell alias and redirects break alias resolution.
- Pipes (`cmd | mcp-cli call tool -`) also lose output in Claude Code's Bash tool.

## Git Not Found

### Symptom
```
FileNotFoundError: git command not found
```

### Solutions

1. **Install git**
   ```bash
   # macOS
   brew install git

   # Linux
   sudo apt-get install git
   ```

2. **Verify git in PATH**
   ```bash
   which git
   git --version
   ```

3. **Fallback behavior**
   If git is not available:
   - Upward search stops at HOME directory

## Git Authentication

### Symptom
Git pushes/pulls fail, or you see `Permission denied (publickey)` / `fatal: Authentication failed` errors.

### Solutions

1. **Verify credentials file**
   - Check that `~/.watercooler/credentials.json` exists
   - File should contain `{"github_token": "ghp_..."}`
   - File permissions should be 0600 (Unix/Mac) for security

2. **Re-authenticate**
   - Set `WATERCOOLER_GITHUB_TOKEN` to a valid GitHub Personal Access Token (PAT)
   - Or visit the Watercooler dashboard if available with your plan
   - Download fresh credentials file from Settings -> GitHub Connection
   - Replace `~/.watercooler/credentials.json`

3. **Verify git credential helper**
   - The MCP server automatically configures the git credential helper
   - Test manually: `echo "protocol=https\nhost=github.com\n" | git credential fill`
   - Should return your token

4. **Check token permissions**
   - Token must have `repo` scope for private repositories
   - Visit GitHub Settings -> Developer settings -> Personal access tokens
   - Regenerate token if scopes are incorrect

5. **Use a GitHub PAT directly**
   - Set `WATERCOOLER_GITHUB_TOKEN` environment variable
   - Or set `GITHUB_TOKEN` / `GH_TOKEN`

---

## GitHub CLI Token Expiration

### Symptom
- Git operations fail silently or with authentication errors
- API rate limits show unauthenticated (60 req/hr instead of 5,000)
- Running `gh auth status` shows "authentication failed"

### Solution
**Re-authenticate with GitHub CLI:**
```bash
# Interactive (opens browser)
gh auth login -h github.com --web

# Verify the fix
gh auth status
```

---

## Outdated GitHub CLI Version

### Symptom
- `watercooler_health` shows "gh Version: X.X.X -- outdated"

### Solution (Ubuntu/Debian)
```bash
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
  sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
  sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt upgrade gh
```

### Solution (macOS)
```bash
brew upgrade gh
```

---

## GitHub API Rate Limiting

### Symptom
- `watercooler_health` shows "Rate Limit: 0/5000 (0%) -- RATE LIMITED"

### Solution
1. **Wait for reset**: The health check shows reset time (usually ~1 hour max)
2. **Reduce API usage**: Batch operations, increase poll intervals
3. **Check for runaway processes**: Multiple agents or polling loops can burn through limits quickly

---

## SSH Agent Issues (WSL2/Headless)

### Symptom
Git operations silently fail, hang indefinitely, or timeout in MCP server context, but work fine from interactive terminal.

### Root Cause
SSH protocol requires either an unlocked SSH key or an SSH agent with the key loaded. In headless contexts, there's no TTY for passphrase prompts.

### Solution 1: Switch to HTTPS (Recommended)

```bash
gh config set git_protocol https
gh auth setup-git
```

### Solution 2: Fix SSH Agent (if SSH required)

**Quick fix (current session):**
```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
```

**Persistent fix (WSL2/Linux):**
Add to `~/.bashrc` or `~/.zshrc`:
```bash
if [ -z "$SSH_AUTH_SOCK" ]; then
    eval "$(ssh-agent -s)" > /dev/null
    ssh-add ~/.ssh/id_ed25519 2>/dev/null
fi
```

---

## Stale MCP Server Processes

### Symptom
- Code changes don't take effect despite restarting client
- Multiple watercooler MCP processes running

### Solution
```bash
# Kill all watercooler MCP processes
pkill -f watercooler_mcp
```

**After cleanup:** Restart your MCP client to reconnect with fresh server processes.

---

## Git Sync Issues (Cloud Mode)

If you enabled cloud sync via `WATERCOOLER_GIT_REPO`, here are common problems and fixes:

- **Authentication failed**: Ensure the deploy key/token has access to the repo
- **Rebase in progress**: Run `git rebase --abort` in the threads repo directory, then retry
- **Push rejected (non-fast-forward)**: Pull (`git pull --rebase --autostash`) and retry push
- **Rate limits / GitHub API**: Check `watercooler_health(code_path=".")` for rate limit status

## Worktree Issues

### Symptom
Write operations (`say`, `ack`, `handoff`, `set_status`) fail with errors like:
- "Worktree not found"
- "Orphan branch does not exist"
- "Failed to create worktree"

### Common Issues

| Issue | Meaning | Auto-Fixed? |
|-------|---------|-------------|
| Missing worktree | Worktree at `~/.watercooler/worktrees/<repo>/` doesn't exist | Yes (on first write) |
| Missing orphan branch | `watercooler/threads` branch not in repo | Yes (on first write) |
| Detached HEAD | Code repo not on a branch | No |
| Rebase in progress | Incomplete git rebase in worktree | No |

### Solutions

**Missing worktree/orphan branch (auto-fixed)**
- The server creates both on first write via `_ensure_worktree()`
- If creation fails, check git permissions and remote access

**Detached HEAD**
```bash
cd /path/to/code-repo
git checkout main  # or your target branch
```

**Lock Timeout**

Per-topic advisory locks prevent concurrent writes to the same thread.

**Lock Mechanism:**
- Locks are stored in the worktree at `<worktree>/.wc-locks/<topic>.lock`
- **Automatic TTL cleanup**: Locks expire after 60 seconds

**Resolution:**
1. **Wait**: If another operation is genuinely in progress
2. **Wait for TTL**: If the holder crashed, wait up to 60 seconds for auto-cleanup
3. **Force unlock**:
   ```bash
   watercooler unlock <topic> --force
   # If using a non-standard worktree path:
   watercooler unlock <topic> --threads-dir /path/to/worktree --force
   ```

### Sync Recovery

Git sync is handled automatically by write-path middleware (`lock → pull →
write → commit → push` with rebase+retry). Use the health tool to diagnose:

```python
watercooler_health(code_path=".")
```

---

## Migrating from a separate `-threads` repository

If you previously used the separate `<repo>-threads` repository model, your
thread data needs to move to the orphan branch:

1. **Copy thread files** into the worktree:
   ```bash
   WORKTREE="$HOME/.watercooler/worktrees/<repo>"
   mkdir -p "$WORKTREE"
   # Trigger worktree creation first:
   watercooler_health(code_path=".")
   # Then copy your thread files:
   cp ../<repo>-threads/*.md "$WORKTREE"/
   cp -r ../<repo>-threads/graph/ "$WORKTREE"/graph/ 2>/dev/null || true
   ```

2. **Commit on the orphan branch**:
   ```bash
   cd "$WORKTREE"
   git add -A && git commit -m "migrate threads from separate repo"
   git push origin watercooler/threads
   ```

3. **Verify** with `watercooler_health(code_path=".")` — threads should appear.

4. **Archive** the old `-threads` repo once you confirm everything works.

---

## Thread folder inside code repo

### Symptom
Server resolves threads inside the code repository instead of the orphan branch worktree.

### Solutions

1. **Confirm worktree location**
   ```bash
   watercooler_health(code_path=".")
   ```
   Check the `Threads Dir` line (should be `~/.watercooler/worktrees/<repo>/`).

2. **Remove manual overrides**
   Delete any `WATERCOOLER_DIR` overrides unless you intentionally need them.
   The server will auto-create the orphan branch and worktree on next write.

## Ball Not Flipping

### Symptom
`watercooler_say` doesn't flip the ball to counterpart.

### Solutions

1. **Check agents.json configuration**
   ```bash
   cat ~/.watercooler/worktrees/<repo>/agents.json
   ```

   Should define counterparts:
   ```json
   {
     "agents": {
       "Claude": {"counterpart": "Codex"},
       "Codex": {"counterpart": "Claude"}
     }
   }
   ```

2. **Create agents.json if missing**
   See [CONFIGURATION.md](./CONFIGURATION.md) for configuration guide.

3. **Verify with read_thread**
   ```
   watercooler_read_thread(topic="your-topic")
   ```
   Check `Ball:` line in output.

## Server Crashes or Hangs

### Symptom
MCP server stops responding or crashes.

### Solutions

1. **Check server logs**
   - Codex: Check terminal output
   - Claude Desktop: Check console logs
   - Claude Code: Check Developer Console

2. **Verify Python version**
   ```bash
   python3 --version
   ```
   Required: Python 3.10 or later

3. **Update dependencies**
   ```bash
   pip install -e .[mcp] --upgrade
   ```

4. **Test server directly**
   ```bash
   python3 -m watercooler_mcp
   ```
   Should start without errors.

## Cache Management

The watercooler MCP server uses multiple caches that can sometimes get out of sync.

### Understanding the Caches

| Cache | Location | Contains |
|-------|----------|----------|
| **Watercooler binaries** | `~/.watercooler/bin/` | Optional local LLM binaries and shared libraries |
| **Watercooler models** | `~/.watercooler/models/` | Downloaded GGUF model files |
| **uvx package cache** | `~/.cache/uv/archive-v0/` | Built Python packages |
| **uvx git cache** | `~/.cache/uv/git-v0/` | Git repository checkouts |

### Quick Reset

```bash
watercooler-mcp --reset-cache
```

### Full Reset (Including uvx)

```bash
watercooler-mcp --reset-cache
uv cache clean
```

### When to Reset Caches

Reset caches when you experience:
- Old version of code running despite pulling updates
- Model download failures or corrupted models
- Unexplained behavior after updating watercooler

## Format Parameter Errors

### Symptom
```
Error: Only format='markdown' is currently supported
```

### Solutions

1. **Use markdown format (default)**
   ```
   watercooler_list_threads()
   # or explicitly
   watercooler_list_threads(format="markdown")
   ```

## Getting More Help

### 1. Run Diagnostic Tools

**Health Check:**
```bash
# In your MCP client:
watercooler_health
```

**Identity Check:**
```bash
# In your MCP client:
watercooler_whoami
```

### 2. Enable Verbose Logging

Run server directly to see detailed output:

```bash
# Test server startup
python3 -m watercooler_mcp
```

### 3. Check Documentation

- **[Installation Guide](./INSTALLATION.md)** - Setup instructions
- **[Environment Variables](./ENVIRONMENT_VARS.md)** - Complete configuration reference
- **[MCP Server Guide](./mcp-server.md)** - Tool reference and usage examples

### 4. Report Issues

If you encounter a bug, open an issue with:

**Required Information:**
1. Output from `watercooler_health`
2. Your MCP configuration (sanitized - remove secrets!)
3. Error messages and stack traces
4. Steps to reproduce

**Where to Report:**
- GitHub Issues: https://github.com/mostlyharmless-ai/watercooler-cloud/issues

---

## Quick Reference: Diagnostic Commands

| Problem | Command | What to Look For |
|---------|---------|-----------------|
| Server not loading | `python3 -m watercooler_mcp` | FastMCP banner appears? |
| Wrong agent | `watercooler_whoami` | Agent name matches config? |
| Wrong directory | `watercooler_health` | Threads Dir path correct? |
| Git issues | `which git && git --version` | Git installed and in PATH? |
| Python issues | `python3 --version` | Python 3.10 or later? |
| Package issues | `pip list \| grep -E "(fastmcp\|mcp)"` | fastmcp>=2.0 installed? |

---

**Still having trouble?** Open an issue with the diagnostic information above. We're here to help!
