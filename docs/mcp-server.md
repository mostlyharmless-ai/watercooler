# Watercooler MCP Server

FastMCP server that exposes watercooler-cloud tools to AI agents through the Model Context Protocol (MCP).

## Overview

The Watercooler Cloud MCP server allows AI agents (like Claude, Codex, etc.) to naturally discover and use Watercooler Cloud tools without manual CLI commands. All tools are namespaced as `watercooler_*` for provider compatibility.

**Current Status:** Production Ready (Phase 1A/1B/2A complete)
**Version:** v0.0.1 + Phase 2A git sync

## Installation

Install watercooler-cloud with MCP support:

```bash
pip install -e .[mcp]
```

This installs `fastmcp>=2.0` and creates the `watercooler-mcp` command.

## Transport Modes

The MCP server supports two transport modes:

### STDIO Mode (Default)

Standard transport for local MCP clients (Claude Code, Cursor, Codex, Claude Desktop).

```bash
# Runs with STDIO transport by default
watercooler-mcp
```

## Quick Start

**For complete setup instructions, see [INSTALLATION.md](./INSTALLATION.md)**

### Configuration Examples

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.watercooler_cloud]
command = "uvx"
args = ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"]

[mcp_servers.watercooler_cloud.env]
WATERCOOLER_AGENT = "Codex"
```

**Claude Desktop (`~/.config/Claude/claude_desktop_config.json` on Linux, `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable",
        "watercooler-mcp"
      ],
      "env": {
        "WATERCOOLER_AGENT": "Claude@Desktop"
      }
    }
  }
}
```

**Claude Code (`~/.claude.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable",
        "watercooler-mcp"
      ],
      "env": {
        "WATERCOOLER_AGENT": "Claude@Code"
      }
    }
  }
}
```

**Cursor (`~/.cursor/mcp.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable",
        "watercooler-mcp"
      ],
      "env": {
        "WATERCOOLER_AGENT": "Cursor"
      }
    }
  }
}
```

**Note:** `uvx` must be in your PATH. If it's not found, use the full path (e.g., `~/.local/bin/uvx` on Linux/macOS). The `uvx` command ensures you always get the latest code from the repository and runs in an isolated environment.

## Environment Variables

### WATERCOOLER_AGENT (Required)
Your agent identity (e.g., "Claude", "Codex"). Set in MCP config.

### WATERCOOLER_DIR (Optional)
Explicit override for bespoke setups. Universal mode clones threads beside your
code repository as a sibling `<code-root>-threads` directory (for example
`/workspace/my-app` ↔ `/workspace/my-app-threads`); you usually do not need to
set this variable.

Only set `WATERCOOLER_DIR` when you require a fixed threads directory (for example, while debugging environments where the server cannot infer the correct repository).

### Thread storage

Threads are stored on a `watercooler/threads` orphan branch in the code repository,
accessed via a git worktree at `~/.watercooler/worktrees/<repo>/`. The orphan branch
and worktree are created automatically on first write — no separate threads
repository is needed.

**Manual override:**

If you set `WATERCOOLER_DIR`, that path takes priority and the orphan branch
worktree is skipped. Use the override sparingly — it's mainly useful for
testing or debugging.

## Available Tools

All tools are namespaced as `watercooler_*`:

### Diagnostic Tools

#### `watercooler_health`
Check server health and configuration including worktree status, git authentication, and GitHub API status.

**Parameters:**
- `code_path` (str): Path to code repository for parity checks (optional)

**Returns:** Comprehensive health status including:
- Server version and agent identity
- Threads directory configuration
- Graph services status (LLM, embeddings)
- Worktree and orphan branch status
- Git authentication status
- GitHub CLI version and API rate limits

#### `watercooler_whoami`
Get your resolved agent identity.

**Returns:** Current agent name

### Thread Management Tools

#### `watercooler_list_threads`
List all threads with ball ownership and NEW markers.

**Parameters:**
- `open_only` (bool | None): Filter by status (True=open only, False=closed only, None=all)
- `limit` (int): Max threads (not yet implemented - returns all)
- `cursor` (str | None): Pagination cursor (not yet implemented)
- `format` (str): Output format - "markdown" (json support deferred)

**Returns:** Formatted thread list organized by:
- Your Turn - Threads where you have the ball
- NEW Entries - Threads with unread updates
- Waiting on Others - Threads where others have the ball

Each thread includes an LLM-generated summary (when available from the baseline graph) below the status line, giving agents quick context without opening the thread.

#### `watercooler_read_thread`
Read complete thread content, or a condensed summary-only view.

**Parameters:**
- `topic` (str): Thread topic identifier (e.g., "feature-auth")
- `from_entry` (int): Starting entry index (not yet implemented - returns from start)
- `limit` (int): Max entries (not yet implemented - returns all)
- `format` (str): Output format - `"markdown"` (default) or `"json"`
- `summary_only` (bool): When `true`, returns only entry summaries (no bodies). Reduces token usage by ~90%. Default: `false`.

**Returns:**
- Markdown (full): original thread markdown
- Markdown (summary_only): condensed view with thread summary, status, and per-entry summaries
- JSON (full): structured payload with `meta` (including thread `summary`) and `entries[]` array (each with `summary` and `body`)
- JSON (summary_only): same structure but entries contain `summary` only (no `body`)

**Usage Tips:**
- Use `summary_only=true` to scan a thread's narrative in ~500 tokens instead of ~5,000 -- then fetch specific entries with `get_thread_entry` for full bodies.
- Prefer `format="json"` when a client needs to examine individual entries without reparsing markdown.

#### `watercooler_list_thread_entries`
List entry headers (metadata only) for a thread so clients can select specific entries without downloading the entire file.

**Parameters:**
- `topic` (str): Thread topic identifier
- `offset` (int): Zero-based entry offset (default: 0)
- `limit` (int | None): Maximum entries to return (default: all from `offset`)
- `format` (str): `"json"` (default) for structured data or `"markdown"` for a human-readable list
- `code_path` (str): Code repository root (required to resolve the paired threads repo)

**Returns:**
- JSON: `entry_count`, effective `offset`, and an array of entry headers with summaries
- Markdown: bullet list summarising the selected entries

#### `watercooler_get_thread_entry`
Retrieve a single entry (header + body) either by index or by `entry_id`.

**Parameters:**
- `topic` (str): Thread topic identifier
- `index` (int | None): Zero-based entry index (optional)
- `entry_id` (str | None): ULID captured in the entry footer (optional)
- `format` (str): `"json"` (default) or `"markdown"`
- `code_path` (str): Code repository root (required)

Provide either `index` or `entry_id` (or both, if you want validation that they refer to the same entry).

#### `watercooler_get_thread_entry_range`
Return a contiguous, inclusive range of entries for streaming scenarios.

**Parameters:**
- `topic` (str): Thread topic identifier
- `start_index` (int): Starting entry index (default: 0)
- `end_index` (int | None): Inclusive end index (defaults to last entry)
- `format` (str): `"json"` (default) or `"markdown"`
- `summary_only` (bool): When `true`, returns only entry summaries (no bodies). Default: `false`.
- `code_path` (str): Code repository root (required)

#### `watercooler_say`
Add your response to a thread and flip the ball to your counterpart.

**Parameters:**
- `topic` (str): Thread topic identifier
- `title` (str): Entry title - brief summary
- `body` (str): Full entry content (markdown supported)
- `role` (str): Your role - planner, critic, implementer, tester, pm, scribe (default: implementer)
- `entry_type` (str): Entry type - Note, Plan, Decision, PR, Closure (default: Note)

**Returns:** Confirmation with new ball owner

#### `watercooler_ack`
Acknowledge a thread without flipping the ball.

**Parameters:**
- `topic` (str): Thread topic identifier
- `title` (str): Optional acknowledgment title (default: "Ack")
- `body` (str): Optional acknowledgment message (default: "ack")

#### `watercooler_handoff`
Hand off the ball to another agent.

**Parameters:**
- `topic` (str): Thread topic identifier
- `note` (str): Optional handoff message
- `target_agent` (str | None): Specific agent name (optional, uses counterpart if None)

#### `watercooler_set_status`
Update thread status.

**Parameters:**
- `topic` (str): Thread topic identifier
- `status` (str): New status (e.g., "OPEN", "IN_REVIEW", "CLOSED", "BLOCKED")

#### `watercooler_sync`
Synchronize the local threads repository with its remote.

**Parameters:**
- `code_path` (str): Code repo root, same as other tools
- `agent_func` (str): Optional agent identity for provenance

#### `watercooler_reindex`
Generate index summary of all threads.

**Returns:** Markdown index organized by:
- Actionable threads (where you have the ball)
- Open threads (waiting on others)
- In Review threads
- Closed threads (limited to 10 most recent)

### Federation Tools

#### `watercooler_federated_search`
Search across federated watercooler namespaces. Performs read-only keyword search
across configured watercooler repositories with scored, ranked results.

**Parameters:**
- `query` (str, required): Search query (max 500 chars)
- `code_path` (str): Primary repository root path
- `namespaces` (str): Comma-separated namespace IDs to search (leave empty for all configured)
- `limit` (int): Max results to return (1-100, default 10)

**Returns:** JSON envelope with:
- `schema_version`: Protocol version (currently 1)
- `results[]`: Scored results with `entry_id`, `origin_namespace`, `ranking_score`, `score_breakdown`, and `entry_data`
- `namespace_status`: Per-namespace status (`ok`, `timeout`, `not_initialized`, `access_denied`, `security_rejected`)
- `queried_namespaces`: All namespace IDs that were part of the query

**Prerequisites:** Requires `[federation]` section in config.toml with `enabled = true` and at least one
namespace defined. See `config.example.toml` for the full schema.

**Example:**
```python
watercooler_federated_search(
    query="authentication",
    code_path=".",
    limit=5,
)
```

> Memory query tools are available as an optional add-on. When the memory backend is enabled, additional search and query tools become available for semantic search across project context.

### Git Sync

Write operations follow a `lock → pull → write → commit → push` flow on the
orphan branch worktree. Use `watercooler_health(code_path=".")` to inspect the
current worktree and sync status.

## Configuration

### Environment Variables

- **`WATERCOOLER_AGENT`**: Agent identity (default: `Agent`). Determines entry authorship and ball ownership.

- **Git overrides (optional):**
  - `WATERCOOLER_GIT_AUTHOR` / `WATERCOOLER_GIT_EMAIL` -- override commit metadata on the orphan branch

- **Manual override:** `WATERCOOLER_DIR` forces a specific threads directory. Use only if you must disable universal repo discovery.

### Required parameters

Every tool call must include:

- `code_path` -- points to the code repository root (e.g., `"."`). The server resolves repo/branch/commit from this path.
- `agent_func` -- required on write operations; format `<platform>:<model>:<role>` (e.g., `"Cursor:Composer 1:implementer"`).

## Usage Examples

### Example 1: Check Server Health

```python
watercooler_health(code_path=".")
```

### Example 2: List threads where you have the ball

```python
watercooler_list_threads(code_path=".")
```

### Example 3: Respond to a thread

```python
watercooler_say(
    topic="feature-auth",
    title="Implementation complete",
    body="Spec: implementer-code — unit tests passing, integration tests added.",
    role="implementer",
    entry_type="Note",
    code_path=".",
    agent_func="Cursor:Composer 1:implementer"
)
```

### Example 4: Hand off to a specific teammate

```python
watercooler_handoff(
    topic="feature-auth",
    note="Security review needed for OAuth implementation",
    target_agent="SecurityBot",
    code_path=".",
    agent_func="Claude Code:sonnet-4:pm"
)
```

## Troubleshooting
### Git authentication issues

- HTTPS is the default and uses your credential helper; ensure a manual `git push` succeeds in your code repo.
- For SSH remotes, ensure your SSH key is loaded (`ssh-add -l`).
- After changing credentials, restart the MCP server/client.


### Server Not Found

If `watercooler-mcp` command is not found:

```bash
# Check installation
pip list | grep watercooler-cloud

# Reinstall with MCP extras
pip install -e .[mcp]

# Find command path
which watercooler-mcp
```

### Wrong Agent Identity

If tools show wrong agent name:

```bash
# Check current identity
python -c "from watercooler_mcp.config import get_agent_name; print(get_agent_name())"

# Set in environment
export WATERCOOLER_AGENT="YourAgentName"

# Or configure in MCP client settings
```

### Threads Directory Not Found

If the server can't resolve the threads worktree:

- Ensure `code_path` points inside a git repository.
- Run `watercooler_health(code_path=".")` to check worktree status.
- Verify the worktree exists: `ls ~/.watercooler/worktrees/<repo>/`
- If the worktree is missing, the server creates it on next write.
- As a last resort, set `WATERCOOLER_DIR` to a specific path.

## Development

### Running Tests

```bash
# Install dev dependencies
pip install -e .[dev,mcp]

# Run tests
pytest tests/
```

### Viewing Tool Schemas

```python
import asyncio
from watercooler_mcp.server import mcp

async def show_tools():
    tools = await mcp.get_tools()
    for name, tool in tools.items():
        print(f"\n{name}:")
        print(f"  Description: {tool.description}")
        print(f"  Parameters: {tool.parameters}")

asyncio.run(show_tools())
```

## Project Status

**See [ROADMAP.md](../ROADMAP.md) for complete phase history and future plans.**

### Milestones
- **Phase 1A (v0.1.0)**: MVP MCP server with 9 tools + 1 resource
- **Phase 1B (v0.2.0)**: Upward directory search, comprehensive documentation, Python 3.10+
- **Phase 2A**: Git-based cloud sync with Entry-ID idempotency and retry logic
- **Phase 2B**: Orphan branch migration — simplified thread storage to single orphan branch + worktree model (PR #179)

### Next
- Migrate to fastmcp 3.x ([#189](https://github.com/mostlyharmless-ai/watercooler-cloud/issues/189))

## See Also

- [watercooler-cloud README](../README.md) - Main project documentation

## Support

- **Issues**: https://github.com/mostlyharmless-ai/watercooler-cloud/issues
- **Discussions**: Use GitHub Discussions for questions
- **MCP Protocol**: https://spec.modelcontextprotocol.io/
- **FastMCP Docs**: https://gofastmcp.com/
