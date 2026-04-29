# Quickstart

Zero to first thread entry in under 10 minutes.

> **How Watercooler stores threads.** Thread data lives on an
> [orphan branch](./GLOSSARY.md#orphan-branch) called
> `watercooler/threads` inside your existing repository. Reads and
> writes go through a local [worktree](./GLOSSARY.md#worktree) at
> `~/.watercooler/worktrees/<repo>/`, which holds the
> [baseline graph](./GLOSSARY.md#baseline-graph) (JSON — the source of
> truth for reads) and markdown projections. Both are created
> automatically on first write. If you want the full picture before
> continuing, read [ARCHITECTURE.md](./ARCHITECTURE.md) (~5 minutes).

## Prerequisites

- A git repository (Watercooler stores threads as an orphan branch inside it)
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

Watercooler only needs credentials for the git remote your thread branch will
push to. If your repository is hosted on GitHub:

```bash
gh auth login
gh auth setup-git
```

These two commands set up GitHub authentication for both git operations and the MCP
server.

If your repository is on GitLab, Gitea, a self-hosted server, or any other
non-GitHub remote, skip the `gh` commands and use your remote's credentials
instead — PAT, SSH key, or `GITHUB_TOKEN`-style environment variable. See
[AUTHENTICATION.md](./AUTHENTICATION.md) for each option.

> The hosted [Watercooler Dashboard](https://watercoolerdev.com) currently
> assumes GitHub for OAuth sign-in and repo access. The core CLI and MCP
> server are remote-agnostic — the dashboard is the GitHub-specific piece.

---

## Step 2: Connect your MCP client

**Claude Code (macOS / Linux):**

```bash
claude mcp add --transport stdio --scope user watercooler -- uvx --from 'git+https://github.com/mostlyharmless-ai/watercooler@main[local]' watercooler-mcp
```

**Claude Code (Windows PowerShell):**

```powershell
cmd /c "claude mcp add --transport stdio --scope user watercooler -- uvx --from git+https://github.com/mostlyharmless-ai/watercooler@main[local] watercooler-mcp"
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

> "Please run a watercooler health check."

The agent will call `watercooler_health(code_path=".")`, which reports the status of
git auth, the MCP server, and your threads directory.

> **Health is a sanity check, not a gate.** `watercooler_health` is
> optional — you can skip straight to Step 4 and post your first thread
> without running it. Thread actions (`say`, `ack`, `handoff`,
> `set_status`) do not block on enrichment services, and entries are
> indexed asynchronously once the local LLM + embedding models are
> ready. The health check is useful for diagnosing setup problems up
> front and for kicking off the one-time model download below.

> **About the first-run download (~1.7 GB, local LLM services).**
>
> Watercooler enriches thread entries with auto-generated summaries and
> embedding vectors for semantic search. These are part of the open-core
> feature set — not premium add-ons.
>
> **Why local-first by default.** Out of the box, Watercooler runs the
> enrichment LLM and embedding model on your own machine rather than
> calling a third-party API. That means:
>
> - **Privacy** — entry text never leaves your machine
> - **Zero-config** — no API keys to provision before you can post a
>   thread entry
> - **No per-token cost** — local inference is free to run
>
> **What gets downloaded** (one-time, on first `watercooler_health`):
>
> - **llama-server** binary (~50 MB) from llama.cpp releases
> - **LLM summarizer model** (~1.1 GB GGUF — default `qwen3:1.7b`
>   quantised to Q4_K_M) for generating entry summaries
> - **Embedding model** (~600 MB GGUF — default `bge-m3` quantised to
>   Q8_0) for semantic search vectors
>
> **When it happens.** The first health check triggers the download and
> may take several minutes. Subsequent starts are fast.
>
> **If you'd rather use an external API** (OpenAI-compatible endpoint,
> a self-hosted inference server, etc.), set `auto_start_services = false`
> in `~/.watercooler/config.toml` under `[mcp.graph]` and point the
> summarizer/embedding base URLs at your endpoint. See
> [CONFIGURATION — `[mcp.graph]`](./CONFIGURATION.md#mcpgraph--baseline-graph-enrichment)
> for the full option list. Thread ops (`say`, `ack`, `handoff`, etc.)
> work with or without enrichment configured.

> If the health check reports any issues, stop here. See
> [TROUBLESHOOTING.md — server not loading](./TROUBLESHOOTING.md#server-not-loading)
> for the most common fixes before continuing.

---

## Step 3.5: Set your team identity (recommended)

If multiple people on your team use the same client (for example, two Claude Code
users), set your identity so entries stay attributable. Ask your agent:

> "Please create a watercooler config file with my agent set to my current MCP
> client and my tag set to jay."

The agent will create `~/.watercooler/config.toml` with your identity. Each person
should use a unique tag — entries then show as `Claude (jay)`, `Claude (caleb)`,
`Codex (jay)`, etc., depending on the client.

See [CONFIGURATION.md](./CONFIGURATION.md) for all available options.

---

## Step 4: Create your first thread

Ask your agent:

> "Please create a watercooler thread called my-first-topic titled 'My first thread' and
> post an entry saying hello."

Then verify by asking your coding agent client:

> "Please list my watercooler threads."

You should see `my-first-topic` in the output.

Watercooler has six roles for entries: `planner`, `pm`, `implementer`, `tester`,
`critic`, and `scribe`. The agent picks the appropriate role based on context, or you
can specify one explicitly.

Thread state changes only through explicit write actions (`say`, `ack`, `handoff`,
`set_status`). Watercooler does not passively log all agent activity.

**What's worth capturing:** key decisions, design proposals, handoffs, status changes,
and PR links. Routine file edits and iterative debugging don't need thread entries.

See [TOOLS-REFERENCE.md](./TOOLS-REFERENCE.md) for the full tool list and
[WORKFLOW_EXAMPLES.md](./WORKFLOW_EXAMPLES.md) for common collaboration patterns.

> **What watercooler creates on first write**
>
> The first `say` or `init-thread` call automatically creates the
> orphan branch, the worktree, and the project-level `.watercooler/`
> directory described in the storage callout at the top of this page.
> See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full picture.
>
> If the orphan branch or worktree ever gets into a bad state, ask your
> agent to run
> `watercooler_sync_repair(code_path=".", diagnose_only=True)`.

> **Optional: scaffold custom roles ahead of time**
>
> Run `watercooler roles init` from your project root before your first
> thread write to drop a copy of the bundled `roles.toml` (with all six
> canonical roles and their annotations) into `.watercooler/roles.toml`.
> This makes the role catalog visible and immediately editable — useful
> if you want to add custom roles like `security-audit` or
> `data-analyst` before contributors start posting. The command is
> idempotent (use `--force` to overwrite an existing file).
> See [ROLES_CREATION.md](./ROLES_CREATION.md) for the full guide.

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

`uvx` normally resolves a new version from the configured git ref on launch, but
the resolution is cache-dependent — if `uv` still has a valid cached archive for
`@main`, it will reuse it rather than re-pull. To force a fresh version:

```bash
uv cache clean watercooler
```

Then restart your MCP client completely so the server picks up the new version.

If `watercooler --version` still shows an old release after this, see
[TROUBLESHOOTING — Stale install after upgrade](./TROUBLESHOOTING.md#stale-install-after-upgrade),
which covers refreshing both the MCP server (`uvx`) and the CLI (`uv tool install`).

> **Stability:** `main` is maintained as the stable release branch. `uvx` pulls from
> `@main`, giving you the latest released version.
