# Installation Guide

Complete setup instructions for watercooler-cloud.

## Prerequisites

- **Python 3.10+**
- **Git** (authentication handled automatically via credentials file)
- Basic GitHub permissions to push to threads repositories

## Installation Methods

### Option 1: Install from Source (Recommended for Development)

```bash
git clone https://github.com/mostlyharmless-ai/watercooler-cloud.git
cd watercooler-cloud
pip install -e .
```

### Option 2: Install via pip

```bash
# Production (recommended)
pip install git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable

# Pinned version (most stable)
pip install git+https://github.com/mostlyharmless-ai/watercooler-cloud@v0.1.0

# Development (bleeding edge)
pip install git+https://github.com/mostlyharmless-ai/watercooler-cloud@main
```

### Option 3: Install MCP Extras

For MCP server integration with AI agents:

```bash
pip install -e ".[mcp]"
```

> **Windows tip:** If your shell exposes `py`, use `py -3 -m pip install -e .` or `python -m pip ...`

---

## Authentication Setup

**Set a GitHub Personal Access Token (PAT)** to enable git sync for your threads:

```bash
export WATERCOOLER_GITHUB_TOKEN="ghp_your_token_here"
```

Or use standard GitHub environment variables (`GITHUB_TOKEN` or `GH_TOKEN`).

**Alternative authentication methods:**
- Place a credentials file at `~/.watercooler/credentials.json`
- CI/CD: Use GitHub Actions secrets or environment-specific tokens

**Advanced configuration:**
For fine-grained control, see [Environment Variables Reference](ENVIRONMENT_VARS.md) to customize:
- Agent identity (`WATERCOOLER_AGENT`)
- Repository patterns (`WATERCOOLER_THREADS_PATTERN`)
- Git authorship (`WATERCOOLER_GIT_AUTHOR`, `WATERCOOLER_GIT_EMAIL`)
- And 20+ other optional settings

---

## MCP Client Configuration

**Minimal setup** - authentication is automatic!

### Helper Scripts (Prompt-Driven)

**macOS/Linux/Git Bash:**
```bash
./scripts/install-mcp.sh
```

**Windows PowerShell:**
```powershell
./scripts/install-mcp.ps1 -Python python -Agent "Claude@Code"
```

Override `-Python` with `py`/`python3` as needed. Additional flags are documented at the top of the script.

### Claude CLI

```bash
claude mcp add --transport stdio watercooler-cloud --scope user \
  -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable watercooler-mcp
```

> If you previously registered `watercooler-universal`, remove it first with `claude mcp remove watercooler-universal`.

**Note:** `uvx` must be in your PATH. If not found, use the full path (e.g., `~/.local/bin/uvx` on Linux/macOS).

### Codex CLI

```bash
codex mcp add watercooler-cloud \
  -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable watercooler-mcp
```

### Cursor

Edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable",
        "watercooler-mcp"
      ]
    }
  }
}
```

### Using fastmcp (Any Shell)

```bash
fastmcp install claude-code src/watercooler_mcp/server.py \
  --server-name watercooler-cloud
```

---

## Git Configuration (Multi-User Collaboration)

For team collaboration, configure git merge strategies and pre-commit hooks:

```bash
# Required: Enable "ours" merge driver
git config merge.ours.driver true

# Recommended: Enable pre-commit hook (enforces append-only protocol)
git config core.hooksPath .githooks
```

See [WATERCOOLER_SETUP.md](../.github/WATERCOOLER_SETUP.md) for the detailed setup guide.

---

## Additional Resources

- **[Environment Variables](ENVIRONMENT_VARS.md)** - Advanced configuration reference
- **[MCP Server Guide](mcp-server.md)** - Tool reference and parameters
- **[Troubleshooting](TROUBLESHOOTING.md)** - Common issues and solutions

---

## Next Steps

After installation:
1. Configure your MCP client using one of the methods above
2. See the [CLI Reference](CLI_REFERENCE.md) for command examples
3. Read the [Quick Start](QUICKSTART.md) to create your first thread
