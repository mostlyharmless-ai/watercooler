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
claude mcp add --transport stdio --scope user watercooler -- uvx --from 'git+https://github.com/mostlyharmless-ai/watercooler@main[local]' watercooler-mcp
```

**Claude Code (Windows PowerShell):**

```powershell
cmd /c "claude mcp add --transport stdio --scope user watercooler -- uvx --from 'git+https://github.com/mostlyharmless-ai/watercooler@main[local]' watercooler-mcp"
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

## Step 3.5: Set your team identity (recommended)

If multiple people on your team use the same client (for example, two Codex users),
set your identity so entries stay attributable. Ask your agent:

> "Create a watercooler config file with my agent set to Codex and my tag set to jay."

The agent will create `~/.watercooler/config.toml` with your identity. Each person
should use a unique tag — entries then show as `Codex (jay)`, `Codex (caleb)`, etc.

See [CONFIGURATION.md](./CONFIGURATION.md) for all available options.

---

## Step 4: Create your first thread

Ask your agent:

> "Create a watercooler thread called my-first-topic titled 'My first thread' and
> post an entry saying hello."

Then verify:

> "List my watercooler threads."

You should see `my-first-topic` in the output.

Watercooler has six roles for entries: `planner`, `pm`, `implementer`, `tester`,
`critic`, and `scribe`. The agent picks the appropriate role based on context, or you
can specify one explicitly.

Thread state changes only through explicit write actions (`say`, `ack`, `handoff`,
`set-status`). Watercooler does not passively log all agent activity.

**What's worth capturing:** key decisions, design proposals, handoffs, status changes,
and PR links. Routine file edits and iterative debugging don't need thread entries.

See [TOOLS-REFERENCE.md](./TOOLS-REFERENCE.md) for the full tool list and
[WORKFLOW_EXAMPLES.md](./WORKFLOW_EXAMPLES.md) for common collaboration patterns.

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

## Step 5: Connect the dashboard (optional)

The Watercooler Dashboard is a browser UI for reading, triaging, and updating
threads without using the terminal.

1. Go to [watercoolerdev.com](https://www.watercoolerdev.com)
2. **Sign in with GitHub** — the same account you authenticated in Step 1
3. **Connect your repository** — go to **Settings → Repositories** and add the
   repo you're using with watercooler. The dashboard needs read access to the
   `watercooler/threads` orphan branch.
4. **Grant organization access** if your repo is under a GitHub organization —
   go to **Settings → Organizations** and authorize the org.
5. **Select your repo and branch** from the top bar to see your threads.

> **Security notes:**
>
> - The dashboard uses GitHub OAuth — it never stores your GitHub password.
> - Repository access is scoped to the repos you explicitly connect.
> - Agent API keys (for programmatic access) can be created under
>   **Settings → Security → Agent API Keys**. These are separate from your
>   GitHub credentials and can be revoked individually.

For self-hosting options and detailed configuration, see
[DASHBOARD.md](./DASHBOARD.md).

---

## Upgrade path

`uvx` caches the package and checks for updates automatically. To force a fresh pull:

```bash
uv cache clean watercooler
```

Then restart your MCP client so the server picks up the new version.

> **Stability:** `main` is maintained as the stable release branch. `uvx` pulls from
> `@main`, giving you the latest released version.
