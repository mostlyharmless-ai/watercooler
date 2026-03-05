# Brainstorm: Minimal but Complete User Documentation Refresh

**Date:** 2026-03-02
**Status:** Design complete — ready for planning
**Author:** Claude Code (docs)
**Thread:** n/a

---

## What We're Building

A curated, new-user-first documentation set written from scratch (using the existing `docs/` as source material), living in a temporary `docs_draft/` directory at the repo root until reviewed and promoted. The goal is a documentation set that is pleasant and easy: a new user should be able to install, connect their MCP client, and post their first thread entry in under 10 minutes, with no confusion about where to look next.

This **replaces `docs/` entirely** on promotion. Content from the current `docs/` that does not fit the new simplified structure is not deleted — it moves to `dev_docs/displaced/` as a holding area until the team decides what to do with it (archive, restructure as advanced guides, etc.). Deep technical details belong in user docs only where they directly relate to installation, setup, and configuration.

---

## Why This Approach

The existing `docs/` is thorough but sprawling — 9 files with overlapping scope (INSTALLATION + QUICKSTART, FAQ + TROUBLESHOOTING) and no clear entry point. A new user faces decision paralysis before they've done anything. Rather than trimming the existing files, we replace them entirely with a tighter set that uses progressive disclosure: a 5-minute path gets most users running, with deeper references available without cluttering the onramp.

Content that doesn't fit the new structure isn't lost — it moves to `dev_docs/displaced/` where it can be repurposed as advanced guides or archived at a later date. (Key Decision #13 specifies the full disposition.)

Configuration is handled at two levels because the config surface is large (`config.example.toml` is 930+ lines). Most users never need to open it; users who do need a guide, not just an annotated file.

---

## Key Decisions

### 1. Working directory: `docs_draft/` → replaces `docs/`
All new files go to `docs_draft/` at the repo root as a staging area. On promotion:
- `docs_draft/` becomes the new `docs/`
- Existing `docs/` files that are not carried forward move to `dev_docs/displaced/` (or similar) as a holding area — not deleted, pending a future decision on what to do with them (archive, restructure as advanced guides, etc.)

This keeps the promotion PR reviewable as a clean diff and ensures no content is permanently lost.

### 2. File structure — 6 curated docs files + root README hub

```
docs_draft/
├── QUICKSTART.md          — 5-min path: install → auth → connect MCP client → first thread
├── AUTHENTICATION.md      — GitHub token setup (OAuth, env var, gh CLI, SSH headless)
├── MCP-CLIENTS.md         — MCP client setup for all supported editors in one place
│                            (Claude Code, Codex, Cursor, ChatGPT — unified, not separate files)
├── CONFIGURATION.md       — Config guide: minimum viable config inline, then full reference
│                            (TOML files, credentials, key settings, env var reference,
│                             config init/show/validate commands)
├── TOOLS-REFERENCE.md     — Unified reference: CLI commands section + MCP tools section
│                            (replaces separate CLI_REFERENCE.md and mcp-server.md tool listing)
└── TROUBLESHOOTING.md     — Top 10 issues with diagnosis flowchart

README.md (root)
└── Documentation hub: vocabulary, quick start, and learning path into the 6 docs files
```

**Rationale for each:**
- Root `README.md` — the only hub and GitHub-rendered landing page; includes the "How it works" vocabulary section
- `QUICKSTART.md` — the single most important file; one happy path, `watercooler_health` as step 3
- `AUTHENTICATION.md` — auth is the #1 new-user friction point; opens with a "choose your method" decision paragraph
- `MCP-CLIENTS.md` — four MCP clients consolidated from multiple files; every block self-contained with full paths and platform variants
- `CONFIGURATION.md` — opens with a minimum viable config snippet, then the full reference
- `TOOLS-REFERENCE.md` — unified CLI + MCP tools reference; includes safety annotations table; memory tools gated with prerequisite callout
- `TROUBLESHOOTING.md` — top 10 issues with mermaid flowchart; covers install failures, auth errors, MCP connection, git sync

**Constraint:** No `docs/README.md`. The project-root `README.md` is the only documentation hub.

### 3. Configuration: two-tier coverage

- **Quickstart** mentions `watercooler config init` to generate the annotated `config.toml`. No prose needed for most users at this stage.
- **Credentials bootstrap** — there is no `credentials init` CLI command. The recommended path depends on install method:
  - **Primary (all installs):** Set `GITHUB_TOKEN` as an env var — no file needed, works immediately.
  - **Secondary (persistent credentials):** Manually create `~/.watercooler/credentials.toml`. The template is bundled with the package at `<package>/templates/credentials.example.toml` (locatable via `python -c "import watercooler; print(watercooler.__file__)"`), but the QUICKSTART should not surface this complexity — the env var path is the right default. Only `CONFIGURATION.md` needs to document the file-based path.
- **`CONFIGURATION.md`** opens with a "minimum viable config" inline code block (~8–10 lines covering the settings most users actually change), then transitions to a full guide: config vs. credentials (config = behavior, credentials = secrets), precedence rules, key settings by category, environment variable reference for CI/headless use, and links to the full annotated template files.
- **Not in user docs:** The raw source template files — users access `config.toml` via `watercooler config init`; the `credentials.toml` template is documented inline in `CONFIGURATION.md` as a minimal snippet (not the full 140-line template).

### 4. T2/T3 memory features: present but not detailed

Memory and search enhancements are real user-facing features, but implementation details (FalkorDB, Graphiti, LeanRAG internals) are not user-facing. References in user docs are limited to:
- What the feature does for the user (smarter search, persistent context across sessions)
- How to enable it (a few config lines, choice of local vs. cloud LLM provider)
- Where to find credentials setup (credentials.toml)
No architecture details, no backend names in prose, no graph/indexing internals.

### 5. Vocabulary-first: define concepts before using them

Watercooler introduces vocabulary that has no analog in standard developer tools — "the ball," "say vs. ack vs. handoff," "thread," "entry," "agent identity." These terms appear in tool names, parameter names, and examples before any explanation is given. This is the single biggest new-user comprehension barrier.

A brief **"How it works"** section at the top of `README.md` (and a compact version at the top of `QUICKSTART.md`) defines the core vocabulary before any commands appear:

| Concept | What it is |
|---|---|
| Thread | A named conversation channel tied to your code repo |
| Entry | A single message posted to a thread |
| Ball | Whose turn it is to respond — flipped by `say`, explicitly passed by `handoff` |
| Agent identity | Who you are in the thread (`Claude Code`, `Codex`, or your custom name) |
| `topic` | The slug identifier for a thread (e.g., `feature-auth`) — used in all tool calls, distinct from the display title |
| `code_path` | Path to your repo root (absolute or `.`) — required on nearly every tool call |
| `counterpart` | Who the ball flips to when you call `say` — set per-thread or per-call |
| `code_branch` | Git branch scoping — thread reads are filtered by branch by default; switching branches can make threads appear to disappear |
| `orphan branch` | The isolated git branch (`watercooler/threads`) where thread data lives, separate from your code history |
| `worktree` | The local checkout of the orphan branch at `~/.watercooler/worktrees/<repo>/`, created automatically on first write |
| `agent_func` | Structured identity for write tools: `"<platform>:<model>:<role>"` — e.g., `"Claude Code:sonnet-4:implementer"` |

**FAQ comparison table placement**: The "How does watercooler compare to Slack/GitHub Issues/Linear?" table from `docs/FAQ.md` belongs in README.md's "How it works" section — not TROUBLESHOOTING.md. Content disposition in Key Decision #13 is updated accordingly.

This pattern is established by Commitlint (concepts section before setup), LangChain (vocabulary page before quickstart), and the MCP spec itself (server concepts overview before server reference).

### 6. Auth decision paragraph at top of AUTHENTICATION.md

The current docs list auth options without guiding the choice. Linear's docs open authentication with: "If you're building for others, use OAuth2. Otherwise, personal API keys are simpler." Watercooler needs the same pattern:

- **Start here (recommended):** Run `gh auth login && gh auth setup-git`. This sets up both git and MCP authentication in one step.
- Prefer an explicit token? Set `GITHUB_TOKEN` in your shell.
- Headless/CI environment? Use a GitHub PAT stored in `credentials.toml`.
- SSH-only setup? See the SSH section below.

This goes at the very top of `AUTHENTICATION.md` as a decision callout before any instructions.

**QUICKSTART inline auth (step 2):** The QUICKSTART contains two lines inline — no link-out to AUTHENTICATION.md that would break the 5-minute path:

```bash
gh auth login
gh auth setup-git
```

One-line note after: "For other auth methods (explicit token, PAT, SSH), see [AUTHENTICATION.md](./AUTHENTICATION.md)."

> **Note:** `scripts/git-credential-watercooler` is a dashboard-specific helper (watercooler-site). It is not part of standalone user setup and is not documented in user docs. Credential file format is `credentials.toml`.

### 7. Setup doctor as quickstart step 3

`watercooler_health` (the MCP health-check tool) is the equivalent of Crawl4AI's `crawl4ai-doctor` — a command that checks your environment and tells you what's broken. It should be **step 3 of the quickstart**, immediately after connecting the MCP client, before any thread operations. This surfaces config errors at the right moment and sets user expectations. Currently it is buried in the troubleshooting section.

**Failure routing:** If the health check reports issues, QUICKSTART step 3 must not leave the user stranded. After the health check step, add: "If the health check reports any issues, stop here. See [TROUBLESHOOTING.md — setup issues](./TROUBLESHOOTING.md#server-not-loading) for the most common fixes before continuing."

### 8. Memory tier opt-in: prerequisite callouts before memory tools

T1 (baseline graph) works with zero config. T2/T3 (enhanced search and memory) require additional services. The pattern from LangChain and Crawl4AI: gate advanced features with an explicit "this requires X" callout block *before* listing those tools, not after. In `TOOLS-REFERENCE.md`, memory-enhanced tools are grouped separately with a visible prerequisite block at the top of that group:

> **Memory features require additional setup.** See [CONFIGURATION.md — memory backend](./CONFIGURATION.md#memory-backend) to enable. If you haven't set this up yet, skip this section — the core thread tools work without it.

No backend names, no architecture details — just "enable this to get smarter search and persistent context."

### 9. MCP tool annotations: safety profile table

The MCP ecosystem standard (filesystem server, GitHub MCP server) includes a compact annotations table mapping each tool to its safety characteristics. For `TOOLS-REFERENCE.md`, a table near the top of the MCP tools section flags:

- `read-only` — tools that only read, never modify
- `idempotent` — safe to retry
- `destructive` — irreversible operations requiring explicit confirmation

Annotations must be verified against the actual tool documentation — not assumed. Based on confirmed tool behavior:
- `watercooler_clear_graph_group` — **confirmed destructive**: "cannot be undone," requires `confirm=true`
- `watercooler_graph_recover` — instruction-only (returns instructions, does not execute); **not** destructive
- `watercooler_migrate_to_memory_backend` — mutating but resumable; defaults to `dry_run=true`; **not** irreversibly destructive
- `watercooler_diagnose_memory` — **read-only** (returns diagnostic info; not yet in existing docs)
- `watercooler_graphiti_add_episode` — writes to Graphiti graph; deduplicates **only when `entry_id` is provided** (skips if already indexed); repeated calls without `entry_id` create duplicate episodes; **not** irreversibly destructive (not yet in existing docs)

**Day-1 scope:** Annotate only confirmed-destructive tools on day 1; defer the full ~30-tool safety audit to the planning checklist. A full pre-writing audit gate is over-engineered — most tool classifications are obvious from their names and descriptions.

**Prerequisites column:** Safety annotations must be at the **tool-entry level** (inline), not section-level only. Add a `Prerequisites` column to each tool entry: `none` | `T2 (memory)` | `T3 (memory)` | `federation config` | `daemon`. Agents scanning directly to a tool entry won't pass through section-level callouts.

### 10. Multi-client config blocks: self-contained, full paths, platform variants

Following the filesystem and GitHub MCP server pattern: every client section in `MCP-CLIENTS.md` must be fully self-contained — no "similar to above, but change X." Each block includes:
- Complete, copy-pasteable config (JSON or TOML as appropriate)
- Explicit config file path with platform variants (macOS/Linux/Windows) where they differ
- The one-liner CLI command where available (`claude mcp add`, `codex mcp add`), falling back to manual JSON only where necessary

### 11. Tone and style

- Sentence-case headings throughout
- Imperative voice for instructions ("Run this command", not "You can run this command")
- Short paragraphs, generous use of code blocks
- Every code block is copy-pasteable and correct
- Progressive disclosure: "If you need X, see [link]" rather than explaining everything upfront

### 12. Link compatibility: full link audit required on promotion

The promotion PR must audit and fix four categories of links. Plain Markdown on GitHub has no redirect support — this is a **required step**, not optional cleanup.

1. **Inbound links to retiring files** — `CLI_REFERENCE.md`, `mcp-server.md`, `INSTALLATION.md` have inbound links from `README.md` and within `docs/`. Replace with:
   - `CLI_REFERENCE.md` → `TOOLS-REFERENCE.md`
   - `mcp-server.md` → `TOOLS-REFERENCE.md`
   - `INSTALLATION.md` → `QUICKSTART.md` (or appropriate anchor)
   _(Full inbound-link inventory per file moves to the implementation plan.)_

2. **Forward links from `docs/` into `dev_docs/`** — Several current docs link to files that live in `dev_docs/`, not `docs/` (e.g., `baseline-graph.md`, `STRUCTURED_ENTRIES.md`, `SEMANTIC_BRIDGE.md`). These links are already broken on GitHub. Fix or remove in the new files.

3. **Root `README.md` dead link** — Root `README.md` links to `docs/ARCHITECTURE.md`, which does not exist. Fix in the promotion PR: point to `dev_docs/ARCHITECTURE.md` or remove.

4. **Root `README.md` inbound-link update** — Root README links to several retiring filenames and must be updated in the same PR.

### 13. Content disposition on promotion

Files from the current `docs/` that are not carried forward move to `dev_docs/displaced/` — not deleted.

**Displaced (moved to `dev_docs/displaced/`):**
- `docs/FAQ.md` — comparison table ("How does this compare to...") folds into README.md "How it works" section; Q&A content folds into TROUBLESHOOTING.md
- `docs/CHATGPT_MCP_INTEGRATION.md` — ChatGPT gets a **stub entry** in MCP-CLIENTS.md (limited rollout, desktop-only; links to official ChatGPT MCP docs). Ngrok setup content displaced; not merged.
- `docs/INSTALLATION.md` — essentials absorbed into QUICKSTART.md; detail may become an advanced install guide later
- `docs/images/` — no new screenshots this pass

**`scripts/install-mcp.sh` and `scripts/install-mcp.ps1`** — explicitly out of scope for this refresh; neither promoted to user docs nor displaced.

**Updated in-place (same location, same filename):**
- Root `README.md` — updated in the promotion PR: dead link to `docs/ARCHITECTURE.md` fixed; inbound links to retiring filenames updated

**Replaced by `docs_draft/` counterpart:**
- `docs/QUICKSTART.md`, `docs/AUTHENTICATION.md`, `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`
- `docs/CLI_REFERENCE.md` + `docs/mcp-server.md` → merged into `docs_draft/TOOLS-REFERENCE.md`

_(Full per-file link inventory and concrete redirect mapping move to the implementation plan.)_

### 14. TOOLS-REFERENCE.md structural design and agent-facing requirements

Define structure and templates before writing begins — retrofitting 30+ entries is expensive.

**Two-section file:**
- **Section 1: CLI Commands** — flat compact table (command | synopsis | key flags | example). Exhaustive. No prose.
- **Section 2: MCP Tools** — grouped per-tool entries with a standard 3-part template:
  1. One-line summary | `Safety: read-only / idempotent / destructive` | `Prerequisites: none / T2 / T3 / federation config`
  2. Parameters table (name, type, required/optional, description)
  3. Example call

**Required-parameters callout (before the MCP tool table):**

Parameters vary by tool category — no single rule applies across all tools. The table below describes **local stdio mode** (the standard new-user setup). In hosted mode, `code_path` is derived from request context and `agent_func` is not required for `set_status`.

| Category | Tools | `code_path` | `agent_func` |
|---|---|---|---|
| Thread read | `list_threads`, `read_thread`, `list_thread_entries`, `get_thread_entry`, `get_thread_entry_range` | required (absolute or `.`) | not used |
| Thread write | `say`, `ack`, `handoff`, `set_status` | required | required |
| Memory / graph | `smart_query`, `search`, `find_similar`, `graphiti_add_episode`, `clear_graph_group`, `migrate_to_memory_backend`, etc. | varies — check each tool | not used |
| Utility / status | `whoami`, `reindex`, `daemon_status`, `daemon_findings`, `memory_task_status` | not accepted | not used |

> `agent_func` format: `"<platform>:<model>:<role>"` — e.g., `"Claude Code:sonnet-4:implementer"`. Valid roles: `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`.
>
> Passing `code_path` to the tools named in the Utility / status row (`whoami`, `reindex`, `daemon_status`, `daemon_findings`, `memory_task_status`) will cause the call to fail. Diagnostic tools like `watercooler_health` and `watercooler_diagnose_memory` accept `code_path` as an optional parameter for context-aware checks.

**Agent onboarding reference** (near top of MCP section):
> **AI agents:** Before calling any tool, read the `watercooler://instructions` MCP resource for workflow guidance and ball mechanics.

Note: the current resource (`resources.py`) is markdown prose with CLI bash examples — not MCP-ready. **Updating this resource is in-scope for the refresh** (per todo 050): remove CLI bash examples, add `agent_func` requirement, add memory tier gate. The resource description above reflects the post-update state. Add a "For AI agents" callout in README.md routing to this resource.

**Common agent workflows section** (in MCP tools section):
1. **Session start:** health check → list threads → smart query for recent context
2. **Entry type selection guide:** Note (status/observation), Plan (design proposal), Decision (resolved choice), PR (linked pull request), Closure (end of thread)
3. **Thread closure sequence:** set status CLOSED → post Closure entry

Each workflow example in TOOLS-REFERENCE.md must include all required parameters (`code_path` on list/query tools; `agent_func` on write tools). The sequences above are schematic — the written examples must be complete and copy-pasteable.

**Tier label glossary in CONFIGURATION.md** (maps `force_tier` parameter values to plain-language names):
- `T1` — Baseline: thread graph, zero config, included with all installs
- `T2` — Semantic memory: adds persistent memory and semantic search across sessions (requires additional setup — see CONFIGURATION.md#memory-backend)
- `T3` — Hierarchical memory: adds summarized context and full semantic graph (requires T2 plus additional setup)

---

## Scope Boundaries

**In scope:**
- Install → auth → connect → first thread (day-1 path)
- `uv` package manager installation as an explicit prerequisite in QUICKSTART (Linux/macOS/Windows install commands)
- Core vocabulary and mental model (thread, ball, entry, agent identity, plus: topic, code_path, code_branch, agent_func)
- All four MCP clients (Claude Code, Codex, Cursor, ChatGPT — ChatGPT as a stub)
- Core CLI commands: `init-thread`, `say`, `ack`, `handoff`, `list`, `search`, `config`
- Configuration essentials + in-depth reference (including tier label glossary for T1/T2/T3)
- Memory feature opt-in (user-facing description and config only, no backend internals)
- Top troubleshooting issues + setup health check
- Upgrade path: `uv cache clean watercooler-cloud` + MCP client restart (QUICKSTART subsection; positional arg to `uv cache clean`; distribution name from pyproject.toml)
- Migration path: from separate `-threads` repository to orphan-branch model (TROUBLESHOOTING section, referencing `scripts/migrate_to_orphan_branch.py`)

**Out of scope:**
- Advanced federation configuration
- Memory backend architecture
- CI/CD integration patterns
- Slack integration setup
- Dashboard (watercooler-site) — separate product

---

## Resolved Questions

1. **Primary audience** — Both solo developers and team leads, solo-first. Solo setup is the primary flow; multi-user notes (shared repo config, multiple agent identities) appear inline where naturally relevant rather than as a separate section.

2. **CLI reference scope** — `cli-reference.md` replaced by a unified `tools-reference.md` with two sections: (1) CLI commands and (2) MCP tools. Users don't distinguish between the two interfaces — they just want to know how to do something. Consolidates the existing `CLI_REFERENCE.md` and the tools listing from `mcp-server.md`.

3. **Starter config snippet** — Inline "minimum viable config" code block at the top of `configuration.md`. No separate file. Avoids duplication risk while giving users a quick copy-paste starting point before the full reference.

4. **Commands scope** — Document only currently shipped commands. No forward references to unshipped features. `watercooler credentials init` and `read` must NOT appear in `docs_draft/`. Use `config init` for setup; use MCP tools (`watercooler_read_thread`) for reading threads.

5. **Filename casing** — Preserve existing uppercase convention (`QUICKSTART.md`, `AUTHENTICATION.md`, etc.) to match the current `docs/` standard and avoid link breakage on promotion. File structure in `docs_draft/` uses uppercase names.

## Known Issues (from Codex reviews — must address in planning)

**High — resolved in this document:**
- ~~`watercooler credentials init` does not exist~~ — resolved: primary path is `GITHUB_TOKEN` env var; secondary is manual `credentials.toml`. No CLI command referenced.
- ~~`read` does not exist as a CLI command~~ — resolved: removed from scope. Thread reading is MCP-only or via `web-export`.
- ~~Lowercase filenames would break links~~ — resolved: uppercase convention confirmed throughout.
- ~~Path-compatibility unplanned~~ — resolved: Key Decision #12 documents the full link-audit requirement as a mandatory promotion step.

**Medium — resolved in this document:**
- ~~Safety table misclassified `graph_recover` and `migrate_to_memory_backend` as destructive~~ — resolved: Key Decision #9 corrects this with verified tool behavior; only `clear_graph_group` is confirmed destructive.
- ~~Credentials bootstrap underspecified for package installs~~ — resolved: Key Decision #3 specifies env var as primary path; file-based credentials covered in `CONFIGURATION.md` only; template location documented for reference.

**Medium — action required in planning:**
- Existing `docs/` has CLI flag drift (e.g., `--closed-only` in docs vs `--closed` in code). The plan must include an explicit step: **verify every CLI example against `watercooler <cmd> --help`** before writing copy-pasteable examples.
- MCP tool safety annotations: per Key Decision #9, the full ~30-tool audit is **not** a pre-write gate. Confirmed-destructive tools are annotated day-1 (`clear_graph_group`); remaining tool classifications are completed during the writing sprint. The plan must include this as a parallel task, not a blocking step.

## Open Questions

_(None — all questions resolved. See Resolved Questions above and Known Issues for plan-phase action items.)_

---

## Source Material

**Existing docs (content source):**
- `docs/QUICKSTART.md`, `docs/INSTALLATION.md`, `docs/AUTHENTICATION.md`
- `docs/CONFIGURATION.md`, `docs/CLI_REFERENCE.md`, `docs/mcp-server.md`
- `docs/FAQ.md`, `docs/TROUBLESHOOTING.md`, `docs/CHATGPT_MCP_INTEGRATION.md`
- `src/watercooler/templates/config.example.toml`
- `src/watercooler/templates/credentials.example.toml`
- `README.md` (project root)

**External research (best practices):**
- Divio documentation system — tutorial/how-to/reference/explanation separation
- Google developer documentation style guide — imperative voice, syntax notation, code blocks
- Microsoft Writing Style Guide — sentence-case headings, active voice, contractions
- Write the Docs — FAQ anti-pattern, progressive disclosure
- MCP spec: [modelcontextprotocol.io/docs/concepts/tools](https://modelcontextprotocol.io/docs/concepts/tools)
- SEP-1382: MCP documentation best practices (community proposal)
- Filesystem MCP server — tool annotations table (readOnlyHint/destructiveHint) pattern
- GitHub MCP server — toolset grouping, self-contained client config blocks
- Commitlint docs — concepts section before setup, collapsed by default
- Crawl4AI CLI docs — setup doctor as first post-install step
- Linear developer docs — "choose your auth method" decision paragraph
- clig.dev — CLI command syntax and reference conventions
