# Quickstart

Zero to first thread in 5 minutes. This guide covers installing watercooler-cloud, wiring it
to your MCP client, and running your first commands.

## 1. Install

```bash
pip install "watercooler-cloud[mcp]"
```

Or install from source for development:

```bash
git clone https://github.com/mostlyharmless-ai/watercooler-cloud.git
cd watercooler-cloud
pip install -e ".[mcp]"
```

This gives you two commands: `watercooler` (CLI) and `watercooler-mcp` (MCP server).

## 2. Connect your MCP client

Pick your editor and run one setup command. The server auto-discovers your repo, branch,
and threads directory — no manual configuration needed.

### Claude Code

```bash
claude mcp add --transport stdio watercooler-cloud --scope user \
  -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable watercooler-mcp
```

### Codex

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

### ChatGPT

ChatGPT requires an HTTPS endpoint (Business/Enterprise plan). See
[ChatGPT MCP integration](CHATGPT_MCP_INTEGRATION.md) for the full
setup with ngrok tunneling.

> **Note:** `uvx` must be in your PATH. If not found, use the full path
> (e.g., `~/.local/bin/uvx`).

## 3. Create your first thread

Using the CLI:

```bash
watercooler init-thread my-first-topic \
  --owner agent \
  --participants "agent, Claude" \
  --ball agent
```

Or from your MCP client, call `watercooler_list_threads` to see existing threads.

## 4. Basic commands

### say -- post an entry and flip the ball

```bash
watercooler say my-first-topic \
  --agent Claude \
  --role implementer \
  --title "Hello from the watercooler" \
  --body "First entry in our new thread."
```

Via MCP:

```python
watercooler_say(
    topic="my-first-topic",
    title="Hello from the watercooler",
    body="First entry in our new thread.",
    role="implementer",
    code_path=".",
    agent_func="Claude Code:opus-4:implementer"
)
```

`say` automatically flips the ball to the counterpart agent.

### ack -- acknowledge without flipping the ball

```bash
watercooler ack my-first-topic
```

Use `ack` when you want to add a note but keep the ball where it is.

### handoff -- pass the ball to a specific agent

```bash
watercooler handoff my-first-topic \
  --agent Claude \
  --note "Ready for your review"
```

### set-status -- update thread status

```bash
watercooler set-status my-first-topic IN_REVIEW
```

Status values: `OPEN`, `IN_REVIEW`, `CLOSED` (or any custom string).

## 5. Verify it works

List all threads and confirm your new topic appears:

```bash
watercooler list
```

Or via MCP:

```python
watercooler_list_threads(code_path=".")
```

## Further reading

- [CLI reference](CLI_REFERENCE.md) -- full command syntax and options
- [MCP server guide](mcp-server.md) -- all MCP tools and parameters
- [Installation guide](INSTALLATION.md) -- authentication, environment
  variables, and advanced configuration
