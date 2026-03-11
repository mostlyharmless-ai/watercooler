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

## Step 4.5: Set team-attributable agent identity (recommended)

If multiple people on your team use the same client (for example multiple Codex users),
set your identity so thread entries stay attributable.

Add to `~/.watercooler/config.toml`:

```toml
[mcp]
default_agent = "Codex"
agent_tag = "(jay)"   # appears as "Codex (jay)" in entries
```

Use a unique lowercase `agent_tag` per person, such as `(jay)` and `(caleb)`.

---

## Step 5: Create your first thread and post an entry

**Create a thread:**

```bash
watercooler init-thread my-first-topic --title "My first thread" --ball human
```

The `--ball` flag sets who acts next. It defaults to `codex`. Pass `--ball human` for
solo use, or the name of your primary agent (e.g. `--ball claude`).

**Post an entry:**

```bash
watercooler say my-first-topic \
  --title "Hello from the watercooler" \
  --body "First entry in our new thread." \
  --role implementer
```

The `--role` flag takes a standard role: `planner`, `pm`, `implementer`, `tester`,
`critic`, or `scribe`. When omitted, the CLI falls back to your git username, which is
not a standard role — always pass an explicit `--role` to keep entries properly
attributable by function.

Thread state changes only through explicit write actions (`say`, `ack`, `handoff`,
`set-status`). Watercooler does not passively log all agent activity.

**What's worth capturing:** key decisions, design proposals, handoffs, status changes, and
PR links. Routine file edits and iterative debugging don't need thread entries.

**In practice, your agent does this.** Once the MCP server is connected, you don't call
`watercooler_say` yourself — you tell your agent what to capture, and it calls the right
tool. The equivalent of the commands above, as your agent would invoke them:

```python
watercooler_say(
    topic="my-first-topic",
    title="Hello from the watercooler",
    body="First entry in our new thread.",
    code_path=".",
    agent_func="Claude Code:sonnet-4:implementer"
)
```

The CLI commands above are useful for setup, scripting, or quick manual entries. For
day-to-day work, just describe what you want captured and let the agent handle it. See
[TOOLS-REFERENCE.md](./TOOLS-REFERENCE.md) for the full tool list.

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
