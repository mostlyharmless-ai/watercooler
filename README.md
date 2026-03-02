# watercooler-cloud

File-based collaboration protocol for agentic coding projects

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![MCP](https://img.shields.io/badge/MCP-enabled-green.svg)](https://modelcontextprotocol.io)

[Installation](#quick-start) • [CLI Reference](docs/CLI_REFERENCE.md) • [Architecture](docs/ARCHITECTURE.md) • [Memory Backends](#memory-backends) • [Documentation](docs/README.md) • [Contributing](CONTRIBUTING.md)

---

[![Watercooler Cloud](docs/images/hero-banner.png)](https://www.watercoolerdev.com)


**Example workflow:**
```text
Your Task → Claude plans → Codex implements → Claude reviews → State persists in Git
```

Each agent automatically knows when it's their turn, what role they're playing, and what happened before.

---

## Quick Start

### 1. Authentication Setup

**Set a GitHub token** to enable git sync for your threads:

```bash
export WATERCOOLER_GITHUB_TOKEN="ghp_your_token_here"
```

Or use the GitHub CLI for automatic token management (recommended for headless/MCP use):

```bash
gh auth login
gh auth setup-git
```

> **Legacy option:** A `~/.watercooler/credentials.json` file (downloaded from the dashboard) is
> also checked, but env vars and the GitHub CLI take priority and are the recommended path.

See [Authentication Guide](docs/AUTHENTICATION.md) for details including SSH setup, PAT creation,
and headless environment tips.

### 2. Configure Your AI Agents

**Minimal configuration** - once authenticated, setup is just command + args!

<details open>
<summary><b>Claude Code</b></summary>

Update `~/.claude.json`:

```json
    "watercooler-cloud": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable",
        "watercooler-mcp"
      ]
    },

```

</details>

<details>
<summary><b>Codex</b></summary>

Update `~/.codex/config.toml`:

```toml
[mcp_servers.watercooler_cloud]
command = "uvx"
args = ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"]

```

</details>

<details>
<summary><b>Cursor</b></summary>

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

</details>

<details>
<summary><b>Claude Desktop</b></summary>

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

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

</details>

<details>
<summary><b>Other MCP Clients</b></summary>

See the [Installation Guide](docs/INSTALLATION.md) for:
- Helper scripts (macOS/Linux/Windows)
- fastmcp setup
- [Advanced configuration](docs/CONFIGURATION.md#environment-variables-reference) (environment variables)

</details>

### Create Your First Thread

Most collaborators never touch the raw CLI anymore—we stay inside Codex, Claude,
Cursor, etc., and let them call the MCP tools for us. A typical spin-up looks
like this:

1. **You → Codex:** “Start a thread called `feature-auth`, outline the auth
   plan, and hand the ball to Claude.”
2. **Codex:** Calls `watercooler_say` (with your `agent_func`) which creates
   the thread, writes the entry, commits, and pushes via `run_with_sync`.
3. **Claude:** Sees the ball, continues refining the plan in the same thread,
   again using `watercooler_say` so git stays in sync.
4. **Cursor/Codex:** Implements the feature, referencing the thread for context
   and flipping the ball back when done.

That’s the workflow we recommend because the MCP layer enforces formatting,
git sync, commits, and identity footers automatically. If you do need
to work manually (for example, repairing a thread offline), the legacy CLI is
still available:

```bash
watercooler init-thread feature-auth --ball Claude
watercooler say feature-auth \
  --agent Claude \
  --role planner \
  --title "Authentication Design" \
  --body "Proposing OAuth2 with JWT tokens"
```

See the [CLI Reference](docs/CLI_REFERENCE.md) for every flag if you go that
route.

---

## Example: Multi-Agent Collaboration

1. **You** ask Codex: “Plan the payments feature in the `feature-payment`
   thread.” Codex hits `watercooler_say`, adds the plan entry, and the ball
   flips to Claude.
2. **Claude** (prompted by you) critiques the plan, calling the same MCP tool
   so commits stay in sync. Ball now sits with Cursor.
3. **Cursor/Codex** implements the feature, updates tests, and posts a
   completion note via `watercooler_say`, flipping the ball to Claude for
   review.
4. **Claude** runs `watercooler_ack` to approve, then `watercooler_set_status`
   to mark the thread `CLOSED` after merge.

No manual git work, no hand-written metadata—each MCP call bundles the entry,
ball movement, commit footers, and push.

---

## Contributing

We welcome contributions! Please see:
- **[CONTRIBUTING.md](CONTRIBUTING.md)** - Guidelines and DCO requirements
- **[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)** - Community standards
- **[SECURITY.md](SECURITY.md)** - Security policy

---

## License

Apache 2.0 License - see [LICENSE](LICENSE)
