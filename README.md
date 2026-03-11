# watercooler

Git-native collaboration threads for human-AI coding teams.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![MCP](https://img.shields.io/badge/MCP-enabled-green.svg)](https://modelcontextprotocol.io)

[Quick Start](#quick-start) • [Documentation](#documentation) • [Workflow Examples](docs/WORKFLOW_EXAMPLES.md) • [Tools Reference](docs/TOOLS-REFERENCE.md) • [Architecture](dev_docs/ARCHITECTURE.md) • [Contributing](CONTRIBUTING.md)

[![Watercooler Cloud](docs/images/hero-banner.png)](https://www.watercoolerdev.com)

---

## What is watercooler?

Watercooler adds a lightweight collaboration layer to your existing code repo: threaded
conversations, ball-passing coordination, and searchable project memory — all versioned
in git alongside your code, no external service required.

**Example workflow:**
```text
You: "Explore options for the new team permissions model"
Codex (jay, planner): documents tradeoffs, posts proposal -> ball passed
Claude (caleb, critic): elaborates on proposal -> ack
Claude (caleb, critic): confirms design, posts Decision -> files GitHub issue
Git: ideation and decision versioned alongside your code
```

**You choose what to share. The agent writes it. Git keeps it.**
**User-signaled, agent-authored, Git-persisted.**

Watercooler does not passively record every agent interaction. You decide what should be
communicated, and the agent writes the appropriate structured thread action so context,
handoffs, decisions, and status changes remain durable in Git and reviewable via your MCP
client or the [Watercooler Dashboard](https://www.watercoolerdev.com). This keeps threads
focused and intentional, but important context can still be lost if you do not
externalize it. Build the habit of capturing key decisions and handoffs.

### Core concepts

| Concept | What it is |
|---|---|
| Thread | A named conversation channel tied to your code repo. Each thread has a `topic` slug, a status, and a ball. |
| Entry | A single message posted to a thread via explicit write actions like `say`, `ack`, or `handoff`. Every entry has an author, role, type, and timestamp. |
| Write actions | Explicit mutating operations: `say` (add entry + flip ball), `ack` (add entry + keep ball by default), `handoff` (add entry + transfer ball), `set_status` (update thread status). |
| Ball | Whose turn it is to respond. `say` flips the ball to your counterpart; `ack` keeps it by default; `handoff` passes it to a named recipient. |
| Agent identity | Who authored the entry. On teams, use `Agent (person)` naming like `Codex (jay)` or `Claude (caleb)` so multiple users of the same client stay distinguishable. |
| `topic` | The slug identifier for a thread, e.g. `feature-auth`. Used in all tool calls; distinct from the display title. |
| `code_path` | Path to your repo root (`"."` or absolute). Required on nearly every MCP tool call. |
| `counterpart` | Who the ball flips to when you call `say`. Configured per-agent or per-call. |
| `code_branch` | Git branch scoping. Thread reads are filtered to your current branch by default. |
| `orphan branch` | The isolated git branch (`watercooler/threads`) where thread data lives, separate from your code history. |
| `worktree` | A local checkout of the orphan branch at `~/.watercooler/worktrees/<repo>/`, created automatically on first write. |
| `agent_func` | Structured identity for write tools: `"<platform>:<model>:<role>"` — e.g., `"Claude Code:sonnet-4:implementer"`. |

### Where watercooler sits

Watercooler is the durable reasoning layer between agent execution and your software lifecycle artifacts.

```text
┌──────────────────────────────────────────────┐
│        SOFTWARE DEVELOPMENT LIFECYCLE        │
│  Repos • Branches • PRs • CI/CD • Reviews    │
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│        HUMAN + AGENT COLLABORATION           │
│  Devs • Code Review • Approval • Governance  │
└──────────────────────────────────────────────┘
════════════════════════════════════════════════
                 WATERCOOLER
           VERSIONED REASONING LAYER
════════════════════════════════════════════════
         • Structured reasoning graph
         • Why behind the code
         • Shared across agents
         • Merge-aware reasoning branches
         • Deterministic replay
         • Full decision provenance
════════════════════════════════════════════════
┌──────────────────────────────────────────────┐
│           CODING AGENT RUNTIME               │
│  Planning • File Edits • Tests • Tool Calls  │
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│      LLM RUNTIME (Per-Agent Scratchpad)      │
│  Context Window • Temporary Chain of Thought │
│  Isolated • Ephemeral • Not Shared           │
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│           MODELS & COMPUTE INFRA             │
└──────────────────────────────────────────────┘
```

### Why watercooler?

As AI accelerates code generation, the team bottleneck shifts to coordination, review,
and decision traceability. Faster output without shared context leads to rework, repeated
assumptions, and weaker critique loops. Watercooler addresses this by turning the thinking
around code, including ideation, proposals, key plans, and decisions, into threaded records in Git
that show who said what and why.

---

## Quick start

### 1. Authenticate

```bash
gh auth login
gh auth setup-git
```

For other methods (PAT, SSH, environment variable), see [AUTHENTICATION.md](docs/AUTHENTICATION.md).

### 2. Connect your MCP client

See [MCP-CLIENTS.md](docs/MCP-CLIENTS.md) for Claude Code, Codex, and Cursor.
After connecting, call `watercooler_health` to verify the setup.

### 3. Create your first thread

Most collaborators work entirely through their MCP client:

1. **You:** "Start a thread called `feature-auth`, capture the plan, and pass it for
   review."
2. **Agent:** Calls the appropriate write tool (`watercooler_say`, `watercooler_ack`,
   `watercooler_handoff`, or `watercooler_set_status`) and writes a structured update.
3. **Another agent or teammate:** Reads the thread context and continues from the current
   ball owner/state.
4. **You + agents:** Post key updates, decisions, and handoffs to the thread as work progresses.

---

## Documentation

1. **[QUICKSTART.md](docs/QUICKSTART.md)** — Install, authenticate, connect your MCP
   client, and post your first thread entry in under 10 minutes.
2. **[WORKFLOW_EXAMPLES.md](docs/WORKFLOW_EXAMPLES.md)** — Canonical, condensed
   collaboration patterns
   for single-agent, multi-agent, team, and async handoff workflows.
3. **[AUTHENTICATION.md](docs/AUTHENTICATION.md)** — All authentication methods: GitHub
   CLI, environment variable, credentials file, and SSH.
4. **[MCP-CLIENTS.md](docs/MCP-CLIENTS.md)** — Connect Claude Code, Codex, or Cursor.
   Each section is self-contained with copy-pasteable config.
5. **[CONFIGURATION.md](docs/CONFIGURATION.md)** — Config and credentials files, key
   settings, environment variable reference, and memory feature opt-in.
6. **[TOOLS-REFERENCE.md](docs/TOOLS-REFERENCE.md)** — Unified reference for all CLI
   commands and MCP tools, with safety annotations and worked examples.
7. **[TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — Setup flowchart and top 10 issues
   with diagnosis and fix instructions.

---

## Quick command reference

| Command | What it does |
|---------|--------------|
| `watercooler init-thread <topic>` | Create a new thread |
| `watercooler say <topic> --title "..." --body "..."` | Post an entry and flip the ball |
| `watercooler ack <topic>` | Acknowledge without flipping the ball (default behavior) |
| `watercooler list` | List all threads |
| `watercooler config init` | Generate an annotated `config.toml` |

For the full command list with all flags, see [TOOLS-REFERENCE.md](docs/TOOLS-REFERENCE.md).

---

## For AI agents

The server exposes a `watercooler://instructions` MCP resource containing workflow
guidance, ball mechanics, and required parameter formats.

---

## Contributing

We welcome contributions! Please see:
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — Guidelines and DCO requirements
- **[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)** — Community standards
- **[SECURITY.md](SECURITY.md)** — Security policy

---

## License

Apache 2.0 License — see [LICENSE](LICENSE)
