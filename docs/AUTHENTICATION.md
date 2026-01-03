# Seamless Authentication

Watercooler provides seamless authentication between the web dashboard and MCP server using a single GitHub OAuth flow.

## Overview

One GitHub authorization enables:

- **Web Dashboard** - Manage threads and repositories through the web interface
- **MCP Server** - Git operations from Claude Code, Cursor, and other AI tools
- **AI Agents** - Automatic credential sharing without re-authentication

## How It Works

### 1. Initial Setup

When you first sign up at the Watercooler dashboard:

1. Navigate to the dashboard (e.g., https://watercooler-site.vercel.app)
2. Click "Sign in with GitHub"
3. Grant access to your GitHub organizations
4. Select which organizations to enable

This single authorization flow:
- Creates your dashboard account
- Stores your GitHub OAuth token securely
- Makes your token available to the MCP server

### 2. MCP Server Authentication

The MCP server uses a **git credential helper** to automatically fetch your GitHub token for git operations.

#### How the Credential Helper Works

When the MCP server needs to perform git operations (clone, push, pull):

1. Git asks the credential helper for credentials
2. The credential helper checks sources in this priority:
   - `~/.watercooler/credentials.json` (downloaded from dashboard)
   - `WATERCOOLER_GITHUB_TOKEN` (dedicated Watercooler token)
   - `GITHUB_TOKEN` (standard GitHub token)
   - `GH_TOKEN` (GitHub CLI token)
3. Returns credentials to git
4. Git completes the operation seamlessly

#### Auto-Configuration

The MCP server **automatically configures** the credential helper on first run:

```python
# Happens automatically in src/watercooler_mcp/git_sync.py
config.set_value(
    'credential "https://github.com"',
    'helper',
    str(helper_script)
)
```

No manual git configuration required!

## Setup Options

### Option 1: Using Dashboard Credentials (Recommended)

**Seamless authentication with minimal setup**

1. **Sign in to the dashboard** at https://watercooler-site.vercel.app
   - Click "Sign in with GitHub"
   - Grant access to your organizations
   - Complete onboarding

2. **Download credentials file**
   - Go to Settings → GitHub Connection
   - Click "Download Credentials" button
   - File downloads as `credentials.json`

3. **Place credentials file**
   ```bash
   # Create directory
   mkdir -p ~/.watercooler

   # Move downloaded file
   mv ~/Downloads/credentials.json ~/.watercooler/
   ```

4. **Done!** MCP server will automatically use the credentials
   - No environment variables needed
   - No MCP configuration changes required
   - Credentials auto-detected by git credential helper

### Option 2: Using Environment Variables (Advanced)

For advanced users or CI/CD environments, set your GitHub token as an environment variable:

```bash
# Dedicated Watercooler token (second priority)
export WATERCOOLER_GITHUB_TOKEN=ghp_your_github_token_here

# Or use standard GitHub token
export GITHUB_TOKEN=ghp_your_github_token_here

# Or use GitHub CLI token
export GH_TOKEN=ghp_your_github_token_here
```

Add to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.) to persist across sessions.

#### Creating a GitHub Personal Access Token

If using environment variables, create a GitHub Personal Access Token:

1. Go to https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Select scopes:
   - `repo` (Full control of private repositories)
   - `read:org` (Read org and team membership)
   - `read:user` (Read user profile data)
4. Click "Generate token"
5. Copy the token and save it securely

## MCP Server Configuration

### Claude Code

Add to `~/.config/claude/claude-code/mcp-settings.json`:

**Minimal configuration** (using credentials file from Option 1):

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"]
    }
  }
}
```

**With environment variable** (Option 2):

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"],
      "env": {
        "WATERCOOLER_GITHUB_TOKEN": "ghp_your_token_here"
      }
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json`:

**Minimal configuration** (using credentials file from Option 1):

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"]
    }
  }
}
```

**With environment variable** (Option 2):

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"],
      "env": {
        "WATERCOOLER_GITHUB_TOKEN": "ghp_your_token_here"
      }
    }
  }
}
```

## Git Credential Helper Details

### Location

`scripts/git-credential-watercooler`

### Protocol

The credential helper implements the git credential protocol:

**Input (from git via stdin):**
```
protocol=https
host=github.com
```

**Output (to git via stdout):**
```
username=token
password=ghp_your_github_token_here
```

### Actions

- **get** - Fetch credentials for GitHub operations
- **store** - No-op (credentials managed by dashboard/environment)
- **erase** - No-op (credentials managed by dashboard/environment)

### Auto-Configuration Details

The MCP server automatically configures git on initialization:

1. Locates the credential helper script: `repo_root/scripts/git-credential-watercooler`
2. Configures git to use the helper for github.com:
   ```
   [credential "https://github.com"]
       helper = /path/to/scripts/git-credential-watercooler
   ```
3. Git automatically calls the helper for all github.com operations

## Security

- **Token Storage**: Tokens are stored securely in environment variables or dashboard database
- **HTTPS Only**: Credential helper only activates for HTTPS GitHub URLs
- **Scoped Access**: Tokens have specific GitHub permissions (repo, read:org, read:user)
- **No Sharing**: Tokens are never shared with third parties
- **Encrypted Transit**: All API communication uses HTTPS

## Troubleshooting

### MCP server can't push to GitHub

**Problem:** Git operations fail with authentication errors

**Solutions:**

1. **Check environment variable:**
   ```bash
   echo $WATERCOOLER_GITHUB_TOKEN
   ```
   Should print your token. If empty, set it:
   ```bash
   export WATERCOOLER_GITHUB_TOKEN=ghp_your_token_here
   ```

2. **Check token permissions:**
   - Go to https://github.com/settings/tokens
   - Verify token has `repo`, `read:org`, `read:user` scopes
   - Regenerate if necessary

3. **Check credential helper:**
   ```bash
   git config --get credential."https://github.com".helper
   ```
   Should point to the credential helper script

4. **Test credential helper directly:**
   ```bash
   echo -e "protocol=https\nhost=github.com\n" | scripts/git-credential-watercooler get
   ```
   Should output:
   ```
   username=token
   password=ghp_your_token_here
   ```

### Credential helper not configured

**Problem:** Auto-configuration didn't run or failed

**Solution:** Manually configure git:

```bash
cd /path/to/watercooler-cloud
git config --global credential."https://github.com".helper "$(pwd)/scripts/git-credential-watercooler"
```

### Using SSH instead of HTTPS

**Problem:** Your repository uses SSH URLs (git@github.com:...)

**Solution:** The credential helper only works with HTTPS URLs. Either:

1. **Switch to HTTPS:**
   ```bash
   git remote set-url origin https://github.com/org/repo.git
   ```

2. **Use SSH keys:**
   - Set up SSH keys in GitHub: https://docs.github.com/en/authentication/connecting-to-github-with-ssh
   - The credential helper won't be used (SSH authentication is separate)

### ⚠️ SSH Agent Required for SSH Protocol (Critical for MCP/Headless)

**Problem:** Git operations silently fail or hang when using SSH protocol without an SSH agent running.

**Why this happens:**
- SSH authentication requires either an unlocked key or an SSH agent with the key loaded
- In headless environments (MCP servers, background services, WSL2 without GUI), there's no TTY for password prompts
- Without an SSH agent, SSH key passphrase prompts fail silently or hang indefinitely
- This is especially problematic for Watercooler MCP which runs as a background service

**Symptoms:**
- `watercooler_say` or other write operations hang or timeout
- Git operations fail with no clear error message
- `ssh -T git@github.com` prompts for passphrase but MCP context can't respond

**Solution: Use HTTPS with GitHub CLI (Recommended for MCP)**

The most reliable approach for MCP servers and headless environments:

1. **Configure GitHub CLI to use HTTPS:**
   ```bash
   # Check current protocol
   gh config get git_protocol

   # Switch to HTTPS
   gh config set git_protocol https
   ```

   Or edit `~/.config/gh/hosts.yml`:
   ```yaml
   github.com:
       git_protocol: https
       user: your-username
   ```

2. **Set up the credential helper:**
   ```bash
   gh auth setup-git
   ```

   This configures git to use `gh` as a credential helper, which:
   - Uses your existing `gh auth login` session
   - Works in headless environments (no TTY required)
   - Handles token refresh automatically

3. **Verify the setup:**
   ```bash
   git config --global credential.helper
   # Should show: !/usr/bin/gh auth git-credential
   ```

**Alternative: SSH with Agent (if you must use SSH)**

If you require SSH protocol:

1. **Start SSH agent:**
   ```bash
   eval "$(ssh-agent -s)"
   ```

2. **Add your key:**
   ```bash
   ssh-add ~/.ssh/id_ed25519
   ```

3. **For persistent agent (WSL2/Linux):**
   Add to `~/.bashrc` or `~/.zshrc`:
   ```bash
   if [ -z "$SSH_AUTH_SOCK" ]; then
       eval "$(ssh-agent -s)" > /dev/null
       ssh-add ~/.ssh/id_ed25519 2>/dev/null
   fi
   ```

4. **Verify agent is working:**
   ```bash
   ssh-add -l
   # Should list your key fingerprint

   ssh -T git@github.com
   # Should authenticate without prompting for passphrase
   ```

**See Also:** [TROUBLESHOOTING.md](TROUBLESHOOTING.md#ssh-agent-issues-wsl2headless) for detailed diagnostics

## Future Enhancements

### Dashboard API Integration (Planned)

Future versions will fetch tokens directly from the dashboard API:

1. MCP server makes authenticated request to `/api/mcp/credentials`
2. Dashboard returns GitHub token for current user
3. Credential helper uses token from API
4. No environment variables needed

This will provide:
- **Single sign-on**: Authenticate once in dashboard, use everywhere
- **Token rotation**: Dashboard can rotate tokens without MCP server restart
- **Centralized management**: Manage all credentials in one place

### Session-Based Authentication (Planned)

Support for session cookies or API keys:

1. User logs into dashboard
2. Dashboard issues session cookie or API key
3. Credential helper uses session for API requests
4. Seamless authentication without manual token management

## Related Documentation

- [Environment Variables](ENVIRONMENT_VARS.md) - Complete environment variable reference
- [MCP Server](mcp-server.md) - MCP server setup and usage
- [Setup and Quickstart](SETUP_AND_QUICKSTART.md) - Getting started guide
- [Troubleshooting](TROUBLESHOOTING.md) - Common issues and solutions
