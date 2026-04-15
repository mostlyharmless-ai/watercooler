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
uv tool install --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler
```

This installs the `watercooler` CLI for terminal use. Your MCP client runs the server
on-demand via `uvx` — no separate install step is needed for the MCP server (Step 3).

**Verify:**

```bash
uvx watercooler --help
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

**Claude Code:**

```bash
claude mcp add --transport stdio watercooler --scope user -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler-mcp
```

> **Important:** This must be a single line. The `--` separator tells `claude` that
> everything after it is the server command, not flags for `claude mcp add`. If you
> split it across lines, `claude` may parse `--from` as its own flag and fail with
> `error: unknown option '--from'`.
>
> **PowerShell users:** Copy-paste the line above as-is. Do not use backslash (`\`) for
> line continuation — PowerShell uses backtick (`` ` ``) instead, but keeping it on one
> line avoids the issue entirely.

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

Generate a config file with an annotated template:

```bash
watercooler config init --user
```

Then edit `~/.watercooler/config.toml` and set:

```toml
[mcp]
default_agent = "Codex"
agent_tag = "(jay)"   # appears as "Codex (jay)" in entries
```

Use a unique lowercase `agent_tag` per person, such as `(jay)` and `(caleb)`.
See [CONFIGURATION.md](./CONFIGURATION.md) for all available identity and MCP options.

**Wire up the PostCompact capture hook** (optional — enables Project Pulse T1 data):

```bash
watercooler setup-pulse-hook
```

This registers `watercooler-capture-theme` as a `PostCompact` hook in
`~/.claude/settings.json`. After running, restart your Claude Code session.

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
watercooler say my-first-topic --title "Hello from the watercooler" --body "First entry in our new thread." --role implementer
```

The `--role` flag takes a role name. Canonical roles are `planner`, `pm`, `implementer`,
`tester`, `critic`, and `scribe`; projects may define additional roles in
`.watercooler/roles.toml`. Invalid roles are rejected — always pass an explicit `--role`
to keep entries properly attributable by function. Run
`watercooler_roles(code_path)` to see valid roles for your project, or
`watercooler_role_details(code_path, role)` for full behavioral guidance on each role.

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

> **`code_path`** tells the MCP server which repository's threads to read or write.
> Pass `"."` when your agent is running in the repo root (the most common case). Pass
> an absolute path when running from a different working directory. Every MCP tool that
> reads or writes threads requires `code_path`.
>
> **`agent_func`** identifies who is posting: `"<platform>:<model>:<role>"`. Use your
> IDE/platform name as it identifies itself (e.g. `"Claude Code"`, `"Cursor"`, `"Codex"`),
> the model name as reported by the client (e.g. `"sonnet-4-6"`, `"gpt-4o"`), and the
> role name for this entry.

The CLI commands above are useful for setup, scripting, or quick manual entries. For
day-to-day work, just describe what you want captured and let the agent handle it. See
[TOOLS-REFERENCE.md](./TOOLS-REFERENCE.md) for the full tool list.

**List all threads:**

```bash
watercooler list
```

You should see `my-first-topic` in the output.

> **What watercooler creates on first write**
>
> The first `say` or `init-thread` command automatically creates:
>
> - **Orphan branch** (`watercooler/threads`) — a branch in your git repo with no
>   shared history with your code branches. All thread data lives here.
> - **Git worktree** (`~/.watercooler/worktrees/<repo>/`) — a local checkout of the
>   orphan branch used for reads and writes.
> - **`.watercooler/` directory** (in repo root) — holds project-level config overrides
>   (`config.toml`) and optional role customizations (`roles.toml`). Distinct from
>   `~/.watercooler/` (your user-level config in your home directory).
>
> If the orphan branch or worktree ever gets into a bad state, run
> `watercooler sync-repair --diagnose` to see what's wrong, or
> `watercooler_sync_repair(code_path=".", diagnose_only=True)` via MCP.

---

## Upgrade path

To update to the latest version:

```bash
uv cache clean watercooler
uv tool install --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler
```

Then restart your MCP client so the server picks up the new version.

> **Stability:** `main` is maintained as the stable release branch. Installing from
> `@main` gives you the latest released version.
