# Quickstart

Zero to first thread entry in under 10 minutes.

## Prerequisites

- Python 3.10 or later
- `uv` package manager

Install `uv` (pick one):

```bash
# macOS / Linux — standalone installer (no Python required)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows — standalone installer
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# If you already have Python — via pip
pip install uv
```

---

## Step 1: Install

```bash
uv tool install --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler-cloud
```

This installs the `watercooler` CLI for terminal use. Your MCP client runs the server
on-demand via `uvx` — no separate install step is needed for the MCP server (Step 3).

**Verify:**

```bash
watercooler --help
```

---

## Step 2: Authenticate

```bash
gh auth login
gh auth setup-git
```

These two commands set up GitHub authentication for both git operations and the MCP
server. For other auth methods (PAT, environment variable, SSH), see
[AUTHENTICATION.md](./AUTHENTICATION.md).

---

## Step 3: Connect your MCP client

**Claude Code (one-liner):**

```bash
claude mcp add --transport stdio watercooler-cloud --scope user \
  -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler-mcp
```

Restart Claude Code after running. For Codex, Cursor, or manual config, see
[MCP-CLIENTS.md](./MCP-CLIENTS.md) — each section is self-contained.

---

## Step 4: Run the health check

From inside your MCP client, call:

```python
watercooler_health(code_path=".")
```

This runs the setup doctor and reports the status of git auth, the MCP server, and your
threads directory.

> If the health check reports any issues, stop here. See
> [TROUBLESHOOTING.md — server not loading](./TROUBLESHOOTING.md#server-not-loading)
> for the most common fixes before continuing.

---

## Step 5: Create your first thread and post an entry

**Create a thread:**

```bash
watercooler init-thread my-first-topic --title "My first thread" --ball human
```

The `--ball` flag sets who acts next. It defaults to `codex` — pass `--ball human` for
solo use so the ball starts with you.

**Post an entry:**

```bash
watercooler say my-first-topic \
  --title "Hello from the watercooler" \
  --body "First entry in our new thread."
```

The `--role` flag defaults to your git username. Pass `--role planner`, `--role pm`,
`--role implementer`, etc. to match what you're doing.

**List all threads:**

```bash
watercooler list
```

You should see `my-first-topic` in the output.

---

## Upgrade path

To update to the latest version:

```bash
uv cache clean watercooler-cloud
uv tool install --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler-cloud
```

Then restart your MCP client so the server picks up the new version.

> **Stability:** `main` is maintained as the stable release branch. Installing from
> `@main` gives you the latest released version.
