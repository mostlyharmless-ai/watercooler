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

## Step 1: Authenticate

```bash
gh auth login
gh auth setup-git
```

These two commands set up GitHub authentication for both git operations and the MCP
server. For other auth methods (PAT, environment variable, SSH), see
[AUTHENTICATION.md](./AUTHENTICATION.md).

---

## Step 2: Connect your MCP client

**Claude Code (macOS / Linux):**

```bash
claude mcp add --transport stdio --scope user watercooler -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler-mcp
```

**Claude Code (Windows PowerShell):**

```powershell
cmd /c "claude mcp add --transport stdio --scope user watercooler -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler@main watercooler-mcp"
```

> **Why `cmd /c` on Windows?** PowerShell does not pass the `--` separator correctly
> to `claude mcp add`, causing it to misparse the server command flags. Wrapping in
> `cmd /c` fixes this.

Restart Claude Code after running. For Codex, Cursor, or manual config, see
[MCP-CLIENTS.md](./MCP-CLIENTS.md) — each section is self-contained.

> **How this works:** `uvx` downloads and runs the MCP server on demand — no separate
> install step is needed. Claude Code launches the server automatically when it needs
> watercooler tools.

---

## Step 3: Run the health check

After restarting Claude Code, ask your agent:

> "Run a watercooler health check."

The agent will call `watercooler_health(code_path=".")`, which reports the status of
git auth, the MCP server, and your threads directory.

> If the health check reports any issues, stop here. See
> [TROUBLESHOOTING.md — server not loading](./TROUBLESHOOTING.md#server-not-loading)
> for the most common fixes before continuing.

---

## Step 3.5: Set team-attributable agent identity (recommended)

If multiple people on your team use the same client (for example multiple Codex users),
set your identity so thread entries stay attributable.

Create `~/.watercooler/config.toml` and set:

```toml
[mcp]
default_agent = "Codex"
agent_tag = "(jay)"   # appears as "Codex (jay)" in entries
```

Use a unique lowercase `agent_tag` per person, such as `(jay)` and `(caleb)`.
See [CONFIGURATION.md](./CONFIGURATION.md) for all available identity and MCP options.

---

## Step 4: Create your first thread and post an entry

Once your MCP client is connected, you work through the agent — not the CLI. Tell your
agent what to capture, and it calls the right tool.

**Create a thread and post an entry:**

Ask your agent:

> "Create a watercooler thread called my-first-topic with the title 'My first thread',
> and post an entry saying hello."

The agent will call tools like:

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

**Verify it worked:**

Ask your agent: "List my watercooler threads." You should see `my-first-topic` in the
output.

The `--role` flag (or `agent_func` role field) takes a role name. Canonical roles are
`planner`, `pm`, `implementer`, `tester`, `critic`, and `scribe`; projects may define
additional roles in `.watercooler/roles.toml`. Ask your agent
`watercooler_roles(code_path=".")` to see valid roles for your project.

Thread state changes only through explicit write actions (`say`, `ack`, `handoff`,
`set-status`). Watercooler does not passively log all agent activity.

**What's worth capturing:** key decisions, design proposals, handoffs, status changes, and
PR links. Routine file edits and iterative debugging don't need thread entries.

See [TOOLS-REFERENCE.md](./TOOLS-REFERENCE.md) for the full tool list.

> **What watercooler creates on first write**
>
> The first `say` or `init-thread` call automatically creates:
>
> - **Orphan branch** (`watercooler/threads`) — a branch in your git repo with no
>   shared history with your code branches. All thread data lives here.
> - **Git worktree** (`~/.watercooler/worktrees/<repo>/`) — a local checkout of the
>   orphan branch used for reads and writes.
> - **`.watercooler/` directory** (in repo root) — holds project-level config overrides
>   (`config.toml`) and optional role customizations (`roles.toml`). Distinct from
>   `~/.watercooler/` (your user-level config in your home directory).
>
> If the orphan branch or worktree ever gets into a bad state, ask your agent to run
> `watercooler_sync_repair(code_path=".", diagnose_only=True)`.

---

## Upgrade path

`uvx` caches the package and checks for updates automatically. To force a fresh pull:

```bash
uv cache clean watercooler
```

Then restart your MCP client so the server picks up the new version.

> **Stability:** `main` is maintained as the stable release branch. `uvx` pulls from
> `@main`, giving you the latest released version.
