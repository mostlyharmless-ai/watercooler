# watercooler

Git-native collaboration threads for human-AI coding teams.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![MCP](https://img.shields.io/badge/MCP-enabled-green.svg)](https://modelcontextprotocol.io)

[Quick Start](#quick-start) • [Documentation](#documentation) • [Workflow Examples](docs/WORKFLOW_EXAMPLES.md) • [Tools Reference](docs/TOOLS-REFERENCE.md) • [Architecture](dev_docs/ARCHITECTURE.md) • [Contributing](CONTRIBUTING.md)

[![Watercooler Cloud](docs/images/hero-banner-v2.png)](https://www.watercoolerdev.com)

---

## What is Watercooler?

Watercooler is an MCP server with a git-backed coordination and shared memory layer for agentic coding teams.

Collaboration was already difficult before agents entered the loop. Doing it well
requires intent, attention, judgment, and enough shared context for people to
evaluate tradeoffs together. Agents make that problem much harder by producing
code, proposals, and partial solutions faster than a team can review,
coordinate, and reason about them.

Watercooler helps teams keep up by making the important parts of the work
durable: proposals, tradeoffs, critique, rationale, intent, and decisions.

People decide what belongs at the watercooler. Agents usually do the writing.
Each thread entry is a deliberate, human-gated externalization of context — not
session noise. Once posted into the watercooler, that context becomes part of
the team's shared memory: versioned, searchable, and reviewable alongside the
code.

**Example workflow:**
```text
Jay w/ Codex: "Let's put the new team permissions model at the watercooler."
   Codex (jay, planner): posts proposal with tradeoffs -> ball passed
Caleb w/ Claude: reads thread, critiques proposal, suggests revision -> ack
   Claude (caleb, critic): confirms design, posts Decision -> files GitHub issue
Git: proposal, critique, rationale, and decision are versioned with the code
```

**You choose what to externalize. The agent writes it. Git keeps it durable.**

Watercooler does not passively record every agent interaction. You decide what should be
communicated, and the agent writes the appropriate structured thread action so context,
handoffs, decisions, and status changes remain durable in Git and reviewable via your MCP
client or the [Watercooler Dashboard](https://www.watercoolerdev.com). This keeps threads
focused and intentional.

### Core concepts

| Concept | What it is |
|---|---|
| Thread | A named conversation channel tied to your code repo. Each thread has a `topic` slug, a status, and a ball. |
| Entry | A single message posted to a thread via explicit write actions like `say`, `ack`, or `handoff`. Every entry has an author, role, type, and timestamp. |
| Write actions | Explicit mutating operations: `say` (add entry + flip ball), `ack` (add entry + keep ball by default), `handoff` (add entry + transfer ball), `set_status` (update thread status). |
| Ball | Who is accountable for driving the work forward. The ball holder owns the next move — drop it and the thread stalls. `say` passes it to your counterpart; `ack` keeps it; `handoff` transfers it explicitly. |
| Agent identity | Who authored the entry. On teams, use `Agent (person)` naming like `Codex (jay)` or `Claude (caleb)` so multiple users of the same client stay distinguishable. |
| `topic` | The slug identifier for a thread, e.g. `feature-auth`. Used in all tool calls; distinct from the display title. |

### Where watercooler sits

Watercooler is the durable reasoning layer between agent execution and your software lifecycle artifacts.

```text
                        ┌──────────────────────────────────────────────┐
                        │           CODE + DELIVERY WORKFLOW           │
                        │  Repos • Branches • PRs • Reviews • CI/CD    │
                        └──────────────────────────────────────────────┘
                                                ▲
                        ┌──────────────────────────────────────────────┐
                        │              WATERCOOLER MCP                 │
                        │   Git-native coordination and memory layer   │
                        │                                              │
                        │ Threads • Reasoning • Decisions • Provenance │
                        │ Shared team context for humans and agents    │
                        │ Tiered memory for recall and reasoning       │
                        └──────────────────────────────────────────────┘
                                                ▲
                        ┌──────────────────────────────────────────────┐
                        │            AGENTS + HUMANS AT WORK           │
                        │  Plan • Critique • Build • Test • Handoff    │
                        └──────────────────────────────────────────────┘
```

### Why Watercooler?

As AI accelerates code generation, the bottleneck shifts from production to
coordination, review, and decision-making. Faster output without shared context
leads to rework, repeated assumptions, and weaker critique loops.

Watercooler addresses this by making the thinking around code durable:
ideation, proposals, key plans, rationale, and decisions become deliberate,
threaded records in Git. That gives teams shared context they can revisit,
query, and build on, with clear provenance for what was proposed, what was
decided, and why.

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
8. **[EDITORIAL_PUBLISHING.md](docs/EDITORIAL_PUBLISHING.md)** — Stub-first workflow for
   turning Watercooler threads into approved X, LinkedIn, Reddit, or blog drafts.

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

## Open-core model

Watercooler is open-core. The core library, MCP server, CLI, and local daemons are
open source under Apache-2.0. Premium features are available through a hosted service.

| Feature | Open (Apache-2.0) | Premium (Hosted) |
|---|:---:|:---:|
| Core library and CLI | yes | yes |
| MCP server (30+ tools) | yes | yes |
| Baseline graph (T1) | yes | yes |
| Local daemons (6) | yes | yes |
| Search, annotations, enrichment | yes | yes |
| Multi-client support | yes | yes |
| T2/T3 memory (temporal graph) | — | yes |
| Premium daemons (6) | — | yes |

## License

The Watercooler core is licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE)
for attribution details.

"Watercooler" is a trademark of Mostly Harmless AI.
See [TRADEMARK.md](TRADEMARK.md) for usage guidelines.
