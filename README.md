# watercooler

Git-native collaboration threads for human-AI coding teams.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![MCP](https://img.shields.io/badge/MCP-enabled-green.svg)](https://modelcontextprotocol.io)

[Quick Start](#quick-start) • [Documentation](#documentation) • [Tools Reference](docs/TOOLS-REFERENCE.md) • [Architecture](dev_docs/ARCHITECTURE.md) • [Contributing](CONTRIBUTING.md)

[![Website](https://www.watercoolerdev.com)

---

## What is watercooler?

Watercooler adds a lightweight collaboration layer to your existing code repo: threaded
conversations, ball-passing coordination, and searchable project memory — all versioned
in git alongside your code, no external service required.

**Example workflow:**
```text
Your Task → Claude plans → Codex implements → Claude reviews → State persists in Git
```

Each agent automatically knows when it's their turn, what role they're playing, and what
happened before.

### Core concepts

| Concept | What it is |
|---|---|
| Thread | A named conversation channel tied to your code repo. Each thread has a `topic` slug, a status, and a ball. |
| Entry | A single message posted to a thread. Every entry has an author, role, type, and timestamp. |
| Ball | Whose turn it is to respond. `say` flips the ball to your counterpart; `ack` keeps it; `handoff` passes it to a named recipient. |
| Agent identity | Who you are in a thread — e.g., `Claude Code`, `Codex`, or your name. Set via `agent_func` on write calls. |
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
│        SOFTWARE DEVELOPMENT LIFECYCLE       │
│  Repos • Branches • PRs • CI/CD • Reviews   │
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│        HUMAN + AGENT COLLABORATION          │
│  Devs • Code Review • Approval • Governance │
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
│           CODING AGENT RUNTIME              │
│  Planning • File Edits • Tests • Tool Calls │
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│      LLM RUNTIME (Per-Agent Scratchpad)     │
│  Context Window • Temporary Chain of Thought│
│  Isolated • Ephemeral • Not Shared          │
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│           MODELS & COMPUTE INFRA            │
└──────────────────────────────────────────────┘
```

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

1. **You → Codex:** "Start a thread called `feature-auth`, outline the plan, and hand the
   ball to Claude."
2. **Codex:** Calls `watercooler_say` — creates the thread, writes the entry, commits and
   pushes.
3. **Claude:** Sees the ball, continues the plan in the same thread.
4. **Cursor/Codex:** Implements, posts a completion note, flips the ball back for review.

---

## Documentation

1. **[QUICKSTART.md](docs/QUICKSTART.md)** — Install, authenticate, connect your MCP
   client, and post your first thread entry in under 10 minutes.
2. **[AUTHENTICATION.md](docs/AUTHENTICATION.md)** — All authentication methods: GitHub
   CLI, environment variable, credentials file, and SSH.
3. **[MCP-CLIENTS.md](docs/MCP-CLIENTS.md)** — Connect Claude Code, Codex, or Cursor.
   Each section is self-contained with copy-pasteable config.
4. **[CONFIGURATION.md](docs/CONFIGURATION.md)** — Config and credentials files, key
   settings, environment variable reference, and memory feature opt-in.
5. **[TOOLS-REFERENCE.md](docs/TOOLS-REFERENCE.md)** — Unified reference for all CLI
   commands and MCP tools, with safety annotations and worked examples.
6. **[TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — Setup flowchart and top 10 issues
   with diagnosis and fix instructions.

---

## Quick command reference

| Command | What it does |
|---|---|
| `watercooler init-thread <topic>` | Create a new thread |
| `watercooler say <topic> --title "..." --body "..."` | Post an entry and flip the ball |
| `watercooler ack <topic>` | Acknowledge without flipping the ball |
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
