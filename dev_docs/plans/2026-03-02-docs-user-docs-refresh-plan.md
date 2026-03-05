---
title: "docs: User documentation refresh — 6-file docs set with root README hub"
type: docs
status: completed
date: 2026-03-02
brainstorm: dev_docs/brainstorms/2026-03-02-user-docs-refresh-brainstorm.md
---

# docs: User documentation refresh — 6-file docs set with root README hub

## Overview

Replace the existing 9-file `docs/` directory with a tighter 6-file set written from
scratch for new users, staged in `docs_draft/` until review is complete. Keep a single
documentation hub in the project-root `README.md` (no `docs/README.md`). The goal is a
documentation set where a new user can install, connect their MCP client, and post their
first thread entry in under 10 minutes. Existing `docs/` content that doesn't fit the new
structure moves to `dev_docs/displaced/` — not deleted.

## Problem Statement

The current `docs/` directory has 9 files (~4,492 total lines) with overlapping scope,
no hub document, and no clear entry point for a new user. Key friction points:

- `INSTALLATION.md` and `QUICKSTART.md` duplicate setup steps
- `FAQ.md` and `TROUBLESHOOTING.md` have overlapping Q&A content
- Core vocabulary (thread, ball, entry, agent identity) is never defined upfront; these
  terms appear in tool names and parameters before any explanation exists
- `watercooler_health` (the setup doctor) is buried in troubleshooting, not in the
  install flow
- Auth options are listed without guiding the choice
- `CLI_REFERENCE.md` and `mcp-server.md` are separate; users don't distinguish
  between CLI and MCP interfaces

Additionally, the current docs have CLI flag drift (e.g., `--closed-only` in docs
vs `--closed` in code), broken links in root `README.md` (`docs/ARCHITECTURE.md` →
should be `dev_docs/ARCHITECTURE.md`), and the `watercooler://instructions` resource
is not agent-ready (contains CLI bash examples, no `agent_func` guidance).

## Proposed Solution

Stage 6 curated files in `docs_draft/`, then promote to `docs/` in a single PR.
Update the project-root `README.md` as the only documentation hub:

```
docs_draft/
├── QUICKSTART.md          — 5-min path: install → auth → connect MCP → health check → first thread
├── AUTHENTICATION.md      — Decision callout at top: gh CLI / env var / PAT / SSH
├── MCP-CLIENTS.md         — All 4 clients in one file, self-contained config blocks
├── CONFIGURATION.md       — Minimum viable config inline, then full reference
├── TOOLS-REFERENCE.md     — Unified: CLI commands table + MCP tools (3-part entries)
└── TROUBLESHOOTING.md     — Top 10 issues + mermaid setup flowchart

README.md (root)
└── Documentation hub that links to all 6 docs files in `docs/`
```

Source material: existing `docs/` files (content reuse, not copy-paste). No new
screenshots this pass.

## Technical Approach

### Phase 0: Repository setup

Create working directories before writing begins.

- [x] `mkdir docs_draft/` — staging area for new documentation
- [x] `mkdir dev_docs/displaced/` — holding area for retiring content

Create each with a `.gitkeep` (git doesn't track empty directories) before any writing
begins.

---

### Phase 1: Pre-writing audit (parallelizable, blocks Phase 2)

Three audit tasks that can run in parallel. Results feed directly into the writing phase.

#### 1a. CLI flag audit (blocks TOOLS-REFERENCE.md CLI section)

For every CLI command referenced in the new docs, run `watercooler <cmd> --help` and
verify the actual flag names and signatures. Existing docs have drift; do not copy-paste
from the old docs.

Commands to audit (Group 1 core + Group 2 commands confirmed from `cli.py` as of
2026-03-02). Verify each still holds before writing examples. For remaining Group 2
commands (reindex, web-export, unlock, baseline-graph) and all Group 3 commands, run
`--help` during writing:

| Command | Confirmed flags (from cli.py) | Flags to double-check |
|---|---|---|
| `init-thread <topic>` | `--title`, `--status` (default: open), `--ball` (default: codex), `--threads-dir` | Verify `--ball` default is still "codex" |
| `list` | `--threads-dir`, `--open-only`, `--closed` | No `--status` or `--json` flag exists |
| `say <topic>` | `--agent`, `--role`, `--title` (required), `--type`, `--body` (required), `--status`, `--ball`, `--threads-dir` | Verify `--type` choices |
| `ack <topic>` | `--agent`, `--role`, `--title`, `--type`, `--body`, `--status`, `--ball`, `--threads-dir` | Verify optional/required status of each |
| `handoff <topic>` | `--agent`, `--role` (default: pm), `--note`, `--threads-dir` | No `--to/--type/--title/--body` |
| `set-status <topic> <status>` | two positional args; `--threads-dir` | Verify valid `status` values |
| `search <query>` | positional `query` only; `--threads-dir` | No `--query/--limit/--json` |
| `config init` | `--user`, `--project`, `--force` | — |
| `config show` | `--project-path`, `--json`, `--sources` | — |
| `config validate` | `--project-path`, `--strict` | — |
| `set-ball <topic> <ball>` | two positional args; `--threads-dir` | — |
| `sync` | `--code-path`, `--threads-dir`, `--status` (show queue), `--now` (force push) | — |

Record any flag changes found during verification. Do not write CLI examples until
this step is complete.

#### 1b. MCP safety annotation audit (non-blocking; can run concurrently with writing)

Classify all ~34 MCP tools into safety categories for the TOOLS-REFERENCE.md safety
annotations table. Starting point from brainstorm Key Decision #9:

**Already confirmed:**
- `watercooler_clear_graph_group` — **destructive** (requires `confirm=true`; cannot be undone)
- `watercooler_graph_recover` — **instruction-only** (returns instructions, does not execute; not destructive)
- `watercooler_migrate_to_memory_backend` — **mutating but resumable** (defaults to `dry_run=true`; not irreversibly destructive)
- `watercooler_diagnose_memory` — **read-only** (returns diagnostic info)
- `watercooler_graphiti_add_episode` — **writes to graph** (deduplicates only when `entry_id` provided; not destructive)

**Remaining tools to classify** (against `src/watercooler_mcp/tools/` source):
- Thread read tools (5) — expected: read-only
- Thread write tools (3: say, ack, handoff) — **mutating** (each call appends a new
  entry and triggers a git sync; calling twice creates two entries — not idempotent)
- `set_status` — **mutating** (always updates `last_updated` and rewrites the thread
  projection via `project_and_write_thread`; not a no-op even when status is unchanged)
- `reindex` — expected: idempotent (rebuilds index from source of truth)
- `whoami` — expected: read-only
- `health` — expected: read-only
- `smart_query`, `search`, `find_similar` — expected: read-only
- `baseline_graph_stats`, `baseline_sync_status` — expected: read-only
- `get_entry_provenance` — expected: read-only
- `get_entity_edge` — expected: read-only
- `bulk_index` — expected: mutating but resumable (idempotent with dedup)
- `graph_enrich` — expected: mutating but resumable
- `graph_project` — expected: mutating but resumable
- `leanrag_run_pipeline` — expected: mutating
- `migration_preflight` — expected: read-only (dry-run check)
- `daemon_status`, `daemon_findings` — expected: read-only
- `federated_search` — expected: read-only
- `access_stats` — expected: read-only
- `memory_task_status` — expected: read-only

Document findings in a row of the TOOLS-REFERENCE.md safety table.

#### 1c. Link inventory (blocks Phase 4 promotion)

Audit inbound links to all retiring filenames. This is a required promotion step (GitHub
Markdown has no redirect support).

**Files retiring in this refresh:**

| Retiring file | Replacement | Search target |
|---|---|---|
| `docs/CLI_REFERENCE.md` | `TOOLS-REFERENCE.md` | Link to this file in `README.md`, `docs/mcp-server.md`, `docs/QUICKSTART.md` |
| `docs/mcp-server.md` | `TOOLS-REFERENCE.md` | Inbound links from README.md, CLAUDE.md |
| `docs/INSTALLATION.md` | `QUICKSTART.md` (or anchor) | Root README, docs/QUICKSTART.md references |
| `docs/FAQ.md` | `README.md` (comparison table) + `TROUBLESHOOTING.md` (Q&A) | Any inbound links |

**Files to search for inbound links:**
- `README.md` (root)
- `CLAUDE.md` (root)
- `docs/*.md` (all current docs files)
- `dev_docs/README.md`

Record findings as a checklist in Phase 4.

**Known broken links in root README.md (fix in promotion PR):**
- `docs/ARCHITECTURE.md` → fix to `dev_docs/ARCHITECTURE.md`
- `docs/README.md` → replace with `docs/QUICKSTART.md` (or `docs/TOOLS-REFERENCE.md`)

---

### Phase 2: Writing sprint (6 docs files + root README hub update)

Write files in the order below. Files 2–4 have no dependencies and can be written in
parallel. A "soft dependency" means the file references anchors in another file (e.g.,
`./AUTHENTICATION.md#method-x`) — the linked file doesn't need to be finalized first,
just written far enough that the anchor name is known.

#### File 1: `README.md` (root hub)

**Purpose:** The only documentation hub. Root README must point users into `docs/` and
must be the only README in user-facing documentation (no `docs/README.md`).

**Depends on:** files 2–7 drafted enough to know final filenames/anchors

**Required sections (in order):**

1. **One-sentence tagline** — What watercooler is in one sentence.

2. **"How it works"** — Vocabulary table defining core concepts before any commands
   appear. This is the single biggest new-user comprehension fix.

   | Concept | What it is |
   |---|---|
   | Thread | ... |
   | Entry | ... |
   | Ball | ... |
   | Agent identity | ... |
   | `topic` | ... |
   | `code_path` | ... |
   | `counterpart` | ... |
   | `code_branch` | ... |
   | `orphan branch` | ... |
   | `worktree` | ... |
   | `agent_func` | ... |

   See brainstorm Key Decision #5 for the exact definitions to use.

3. **"How does watercooler compare to..."** — Comparison table from `docs/FAQ.md`
   (Slack, GitHub Issues, Linear). Adapted as a brief table, not a Q&A.

4. **Learning path** — Ordered list linking to the 6 files in `docs/`. This is the only
   cross-document navigation hub.

5. **Quick command reference** — Compact table of the 5 most-used CLI commands
   (init-thread, say, ack, list, config init) with one-line synopsis each.

6. **"For AI agents" callout** — Brief note: "Before calling any tool, read the
   `watercooler://instructions` MCP resource for workflow guidance."

**Source material:** `docs/FAQ.md` (comparison table only), brainstorm Key Decision #5
(vocabulary), brainstorm Key Decision #14 (agent callout).

**Target length:** ~120–150 lines.

---

#### File 2: `docs_draft/AUTHENTICATION.md`

**Purpose:** Complete auth reference. One file, all four methods.

**Depends on:** nothing (write in parallel with files 3 and 4)

**Required opening:** Decision callout at the very top (before any setup instructions):

> **Choose your authentication method:**
> - **Start here (recommended):** Run `gh auth login && gh auth setup-git`. This sets up
>   both git and MCP authentication in one step.
> - Prefer an explicit token? Set `WATERCOOLER_GITHUB_TOKEN` in your shell.
> - Headless/CI environment? Use a GitHub PAT stored in `credentials.toml`.
> - SSH-only setup? See the SSH section below.

**Required sections (in order):**

1. Decision callout (above)
2. **Method 1: GitHub CLI (recommended)** — `gh auth login` + `gh auth setup-git`;
   what each does; how to verify.
3. **Method 2: Environment variable** — `WATERCOOLER_GITHUB_TOKEN`; how to set
   persistently in shell; how to verify.
4. **Method 3: credentials.toml (headless/CI)** — Location:
   `~/.watercooler/credentials.toml`. Include the minimal template snippet
   (not the full 139-line template). Format: `credentials.toml` (TOML only). Note:
   template is bundled at `<package>/templates/credentials.example.toml` (locatable
   via `python -c "import watercooler; print(watercooler.__file__)"`).
   > **Intentional omission:** The code supports legacy `credentials.json` with
   > auto-migration, but this is pre-deprecation and explicitly undocumented for new
   > users to avoid confusion. Do not mention JSON credentials format anywhere in
   > user docs.
5. **Method 4: SSH-only** — For setups where HTTPS is unavailable.
6. **Verifying authentication** — How to confirm auth is working (MCP health check,
   git push test).
7. **Revoking / rotating tokens** — Brief note on where to manage tokens in GitHub
   settings.

**Do NOT include:**
- `scripts/git-credential-watercooler` — this is a dashboard-specific helper
  (watercooler-site); not part of standalone user setup

**Source material:** `docs/AUTHENTICATION.md` (full content reuse with restructuring).

**Target length:** ~150–180 lines.

---

#### File 3: `docs_draft/CONFIGURATION.md`

**Purpose:** Complete configuration reference. Opens with minimum viable config; ends
with full reference.

**Depends on:** nothing (write in parallel with files 2 and 4)

**Opening structure:**

1. **Minimum viable config** (inline code block, ~8–10 lines) — The smallest
   `config.toml` that covers what most users actually change. Place this at the very
   top of the file before any explanation.

   ```toml
   # ~/.watercooler/config.toml
   version = 1                       # required; do not change

   [mcp]
   default_agent = "Claude Code"     # your MCP client name (usually auto-detected)
   agent_tag = "(yourname)"          # optional: appended to agent name in entries
   ```

   Note: schema sections are `[common]` (thread location, templates) and `[mcp]`
   (server settings, agent identity). Do not use `[threads]` or `[agent]` — those are
   not valid section names. Verify the snippet against `config.example.toml` before
   publishing.

2. **Config vs credentials** — One paragraph distinguishing the two files:
   - `config.toml` = behavior and preferences (safe to commit)
   - `credentials.toml` = secrets (never commit)

3. **Initialize config** — `watercooler config init [--user|--project]` command.
   What it generates (annotated `config.toml`).

4. **Show resolved config** — `watercooler config show [--project-path <dir>] [--json] [--sources]`.

5. **Validate config** — `watercooler config validate [--strict] [--project-path <dir>]`.

6. **Key settings by category** — Structured reference for the settings most users
   actually change. Verify each key name against `config.example.toml` before writing:
   - `[common]` — `threads_suffix` or `threads_pattern` (how watercooler finds threads)
   - `[mcp]` — `default_agent`, `agent_tag`, `threads_dir` (server and identity settings)
   - `[mcp.git]` — git author settings for threads commits
   - `[memory]` — backend selection (feature opt-in)

7. **Memory backend** (anchor: `#memory-backend`) — How to enable T2/T3 memory features.
   User-facing language only: "adds persistent memory and semantic search across sessions."
   No backend names in prose. A few config lines to enable, choice of local vs cloud LLM
   provider. Reference `credentials.toml` for API key placement.

8. **Environment variable reference** — Complete table of all supported env vars.
   4-column format: `Env Var | TOML equivalent | Default | Description`.
   Sources (grep for `getenv(` and `os.environ` across all of these):
   - `src/watercooler/config_loader.py` — primary config env vars
   - `src/watercooler/config_schema.py` — schema defaults
   - `src/watercooler_mcp/tools/memory.py` — tier and memory backend vars
   - `src/watercooler/credentials.py` — auth vars (`GITHUB_TOKEN`, `GH_TOKEN`)
   - `src/watercooler_mcp/auth.py` — hosted-mode vars (`WATERCOOLER_AUTH_MODE`,
     `WATERCOOLER_TOKEN_API_URL`, `WATERCOOLER_TOKEN_API_KEY`)
   - `src/watercooler_mcp/config.py` — MCP-specific vars
   This replaces the now-deleted `ENVIRONMENT_VARS.md` (merged into this file per PR #272).

9. **Precedence rules** — Brief: env vars > project config.toml > user config.toml >
   defaults.

10. **Tier label glossary** — Maps `force_tier` values to plain-language names:
    - `T1` — Baseline: thread graph, zero config, included with all installs
    - `T2` — Semantic memory: adds persistent memory and semantic search across sessions
    - `T3` — Hierarchical memory: adds summarized context and full semantic graph

**Source material:** `docs/CONFIGURATION.md` (976 lines), `src/watercooler/templates/config.example.toml`,
`src/watercooler/config_schema.py`, `src/watercooler/config_loader.py`.

**Target length:** ~300–350 lines (much shorter than current 976-line CONFIGURATION.md,
which duplicates the template file verbatim).

---

#### File 4: `docs_draft/MCP-CLIENTS.md`

**Purpose:** One file for all four supported MCP clients.

**Depends on:** nothing (write in parallel with files 2 and 3)

**Key constraint:** Every client section must be **fully self-contained** — no "similar
to above, but change X." Each section includes:
- Complete, copy-pasteable config (JSON or TOML as appropriate for that client)
- Explicit config file path with platform variants (macOS/Linux/Windows) where they differ
- The one-liner CLI command where available (`claude mcp add`, `codex mcp add`),
  falling back to manual JSON only where necessary

**Required sections (one per client):**

1. **Claude Code** — `claude mcp add watercooler` one-liner. Include config file path
   for reference.
2. **Codex** (OpenAI) — `codex mcp add` if available; otherwise manual JSON config.
   Config file location with platform variants.
3. **Cursor** — Manual `~/.cursor/mcp.json` config. Include macOS/Linux and Windows
   paths. Paste-ready JSON block.
4. **ChatGPT** (stub) — Brief: "ChatGPT MCP is desktop-only, currently in limited
   rollout. Follow the Cursor config pattern above, adjusted for your ChatGPT client.
   See [official ChatGPT MCP docs](**TODO: find real URL before shipping**) for current
   setup instructions." Do NOT include ngrok setup. Keep the stub short (3–4 lines).
   > **Writer gate:** Replace `TODO: find real URL` with the actual OpenAI/ChatGPT MCP
   > documentation URL before the promotion PR. Do not merge with a placeholder link.

**For each full client entry, include:**
- Config file path (absolute, platform-specific)
- Complete JSON/TOML config block
- How to verify the connection (use `watercooler_health`)
- One-sentence note on where to find logs if connection fails

**Source material:** `docs/CHATGPT_MCP_INTEGRATION.md` (for context only; ChatGPT
section is a stub), existing client setup guidance scattered across `docs/INSTALLATION.md`
and `docs/QUICKSTART.md`.

**Target length:** ~120–150 lines.

---

#### File 5: `docs_draft/QUICKSTART.md` (5-minute path)

**Purpose:** The single most important file. One happy path only.

**Depends on:** AUTHENTICATION.md and CONFIGURATION.md (soft dependency — link anchors
only; write after files 2 and 3 are drafted far enough to know anchor names)

**Required sections (in order):**

1. **Prerequisites** — Two lines: Python 3.10+ and `uv` (with install commands for
   Linux/macOS and Windows).

2. **Step 1: Install** — `uv` install command. Distribution name: `watercooler-cloud`
   (from `pyproject.toml`). Include the exact `uv` invocation.

3. **Step 2: Authenticate** — Exactly two lines:
   ```bash
   gh auth login
   gh auth setup-git
   ```
   One-line note after: "For other auth methods (PAT, SSH, env var), see [AUTHENTICATION.md](./AUTHENTICATION.md)."

4. **Step 3: Connect your MCP client** — One sentence + link: "Add watercooler to your
   MCP client config. See [MCP-CLIENTS.md](./MCP-CLIENTS.md) for your editor."

5. **Step 4: Run the health check** — Call `watercooler_health` from your MCP client
   immediately after connection. This is the setup doctor step.
   - Include: "If the health check reports any issues, stop here. See
     [TROUBLESHOOTING.md — setup issues](./TROUBLESHOOTING.md#server-not-loading) for
     the most common fixes before continuing."

6. **Step 5: Create your first thread and post an entry** — Three commands:
   `init-thread`, `say`, `list`. Show complete copy-pasteable examples with all flags.

7. **Upgrade path** (brief subsection):
   ```bash
   uv cache clean watercooler-cloud
   ```
   Then restart your MCP client. Note: positional arg syntax (not `--package` flag).

**Do NOT include:**
- `watercooler credentials init` — this command does not exist
- `watercooler read` — does not exist as a CLI command
- Any T2/T3 memory details (reference CONFIGURATION.md for that)

**Source material:** `docs/QUICKSTART.md`, `docs/INSTALLATION.md`. Do not copy-paste;
verify every command against `--help`.

**Target length:** ~80–100 lines.

---

#### File 6: `docs_draft/TROUBLESHOOTING.md`

**Purpose:** Top 10 issues with diagnosis flowchart.

**Depends on:** QUICKSTART, AUTHENTICATION, CONFIGURATION (soft dependency — routing links
only; write after file 5 is drafted far enough to know the `#server-not-loading` anchor)

**Required sections:**

1. **Setup flowchart** (mermaid) — Visual diagnosis tree covering the most common failure
   path: install → auth → MCP connection → first write. The flowchart should answer
   "where am I stuck?" before the user reads any prose.

   ```mermaid
   flowchart TD
     A[watercooler installed?] -->|No| B[QUICKSTART Step 1]
     A -->|Yes| C[Auth working?]
     C -->|No| D[AUTHENTICATION.md]
     C -->|Yes| E[MCP client connected?]
     E -->|No| F[MCP-CLIENTS.md]
     E -->|Yes| G[Health check passing?]
     G -->|No| H[Check issues below]
     G -->|Yes| I[Ready]
   ```

2. **Top 10 issues** — One `###` heading per issue, in frequency order. Each entry:
   - Symptom (what the user sees)
   - Cause (one line)
   - Fix (copy-pasteable commands)
   - Link to deeper reference if needed

   Priority issues to include (anchor names in parentheses):
   - Server not loading (`#server-not-loading`) — MCP client can't find the server
   - Auth failure (`#auth-failure`) — 401 / permission denied
   - Thread not found (`#thread-not-found`) — code_branch scoping confusion
   - "Ball is not mine" error
   - Git sync conflict
   - Memory backend connection failure (T2/T3)
   - Config not loading
   - Wrong threads directory
   - `uv cache` stale install (version mismatch after upgrade)
   - Migration from `-threads` repository to orphan-branch model
     (reference `scripts/migrate_to_orphan_branch.py`)

3. **Migration guide subsection** — For users migrating from the old separate
   `-threads` repository model to the orphan-branch model. Reference the migration
   script: `scripts/migrate_to_orphan_branch.py`.

**Source material:** `docs/TROUBLESHOOTING.md` (822 lines — heavily trim), `docs/FAQ.md`
(Q&A content).

**Target length:** ~200–250 lines.

---

#### File 7: `docs_draft/TOOLS-REFERENCE.md` (most work)

**Purpose:** Unified reference — CLI commands + MCP tools.

**Depends on:** Phase 1a (CLI audit) must be complete before writing. Phase 1b (safety
annotation audit) does not block writing — classify each tool's safety when writing its
entry, using Phase 1b findings for the ~5 pre-confirmed tools and inferring the rest
from tool names and descriptions. Mark any uncertain tools as `pending-audit` in the
safety table; resolve before promotion.

**Structure: two sections**

##### Section 1: CLI commands

All shipped commands in one table, grouped into three tiers. Each group is introduced
with a single sentence. No prose beyond one-line description per command.

**Group 1 — Core (day-1):** init-thread, say, ack, handoff, list, search, config init,
config show, config validate.

**Group 2 — Extended (T1, less common):** set-status, set-ball, sync, reindex,
web-export, unlock, baseline-graph (build, stats).

**Group 3 — Advanced / out of scope for new users:** check-branches, check-branch,
merge-branch, archive-branch, install-hooks, slack (setup/test/status/disable),
memory (build/export/stats), append-entry (legacy).

Within each group, use the compact 4-column table:

| Command | Synopsis | Key flags | Example |
|---|---|---|---|
| `init-thread <topic>` | Create a new thread | `--title`, `--ball` | `watercooler init-thread feature-auth` |
| ... | | | |

Use confirmed flags from Phase 1a for core (Group 1) commands. For Group 2 and Group 3
commands not in the Phase 1a audit table, run `watercooler <cmd> --help` during writing
to confirm flags before writing examples. Do not invent flags for any group.

##### Section 2: MCP tools

**Required opening callout (before the tool entries):**

> **AI agents:** Before calling any tool, read the `watercooler://instructions` MCP
> resource for workflow guidance and ball mechanics.

**Required-parameters table (before tool entries):**

```markdown
Parameters vary by tool category — no single rule applies across all tools. The table
below describes **local stdio mode** (the standard new-user setup). In hosted mode,
`code_path` is derived from request context and `agent_func` is not required for
`set_status`.

| Category | Tools | `code_path` | `agent_func` |
|---|---|---|---|
| Thread read | list_threads, read_thread, list_thread_entries, get_thread_entry, get_thread_entry_range | required (absolute or `.`) | not used |
| Thread write | say, ack, handoff, set_status | required | required |
| Memory / graph | smart_query, search, find_similar, graphiti_add_episode, clear_graph_group, migrate_to_memory_backend, etc. | varies — check each tool | not used |
| Utility / status | whoami, reindex, daemon_status, daemon_findings, memory_task_status | not accepted | not used |

> `agent_func` format: `"<platform>:<model>:<role>"` — e.g., `"Claude Code:sonnet-4:implementer"`.
> Valid roles: `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`.
>
> Passing `code_path` to the tools in the Utility / status row will cause the call to
> fail. Diagnostic tools (`watercooler_health`, `watercooler_diagnose_memory`) accept
> `code_path` as an optional parameter for context-aware checks.
```

**Safety annotations table:**

| Tool | Safety | Prerequisites |
|---|---|---|
| `watercooler_clear_graph_group` | **destructive** — cannot be undone; requires `confirm=true` | T2 |
| ... | | |

**Memory tools gate (before memory tool group):**

> **Memory features require additional setup.** See
> [CONFIGURATION.md — memory backend](./CONFIGURATION.md#memory-backend) to enable.
> If you haven't set this up yet, skip this section — the core thread tools work without it.

**Per-tool entry template (3-part, used for all ~34 tools):**

```markdown
### watercooler_<tool_name>
<one-line summary> | Safety: read-only / idempotent / mutating / destructive / pending-audit | Prerequisites: none / T2 / T3

| Parameter | Type | Required | Description |
|---|---|---|---|
| param | string | yes | ... |

**Example:**
[code block with all required parameters]
```

**Required and optional sections per entry:**
- Summary line + safety + prerequisites: **required**
- Parameters table: **required** (even for zero-parameter tools — note "No parameters")
- Example: **required** (include all required parameters; must be copy-pasteable)

**Common agent workflows section:**

1. **Session start sequence:** `watercooler_health` → `watercooler_list_threads` →
   `watercooler_smart_query` (for recent context)
2. **Entry type selection guide:**
   - `Note` — status update, observation
   - `Plan` — design proposal
   - `Decision` — resolved choice
   - `PR` — linked pull request
   - `Closure` — end of thread
3. **Thread closure sequence:** `watercooler_set_status` (CLOSED) →
   `watercooler_say` (Closure entry)

Each workflow example must include all required parameters (`code_path` on read/query
tools; `agent_func` on write tools).

**Source material:** `docs/mcp-server.md` (766 lines — the authoritative tool reference),
`docs/CLI_REFERENCE.md` (420 lines), Phase 1a and 1b audit results.

**Target length:** ~600–700 lines (the largest file in the set).

---

### Phase 3: Resource update (`watercooler://instructions`)

**File:** `src/watercooler_mcp/resources.py`

The current `watercooler://instructions` resource is markdown prose with CLI bash
examples. It needs to be updated to be agent-ready (per brainstorm Key Decision #14).

**Required changes:**
- Remove all CLI bash examples (these are not relevant to MCP clients)
- Add explicit `agent_func` requirement and format (`"<platform>:<model>:<role>"`)
- Add memory tier gate (mention T2/T3 gate without backend details)
- Reference the `watercooler://instructions` resource at the top of
  TOOLS-REFERENCE.md MCP section (done in Phase 2, File 7)

**Scope:** Include in the same promotion PR as the documentation files. The resource
content and TOOLS-REFERENCE.md must be consistent — releasing them separately creates
a window where the resource references tool behavior that the docs don't reflect yet.

**Source:** Current `src/watercooler_mcp/resources.py` — read before editing.

---

### Phase 4: Promotion PR

Run after all 6 files in `docs_draft/` have passed internal review and root `README.md`
hub updates are ready.

#### Step 1: Content displacement

Move retiring files to `dev_docs/displaced/`:

| Retiring file | Destination | Notes |
|---|---|---|
| `docs/FAQ.md` | `dev_docs/displaced/FAQ.md` | Comparison table → README.md; Q&A → TROUBLESHOOTING.md |
| `docs/CHATGPT_MCP_INTEGRATION.md` | `dev_docs/displaced/CHATGPT_MCP_INTEGRATION.md` | ChatGPT → stub in MCP-CLIENTS.md |
| `docs/INSTALLATION.md` | `dev_docs/displaced/INSTALLATION.md` | Essentials → QUICKSTART.md |
| `docs/images/` | `dev_docs/displaced/images/` | No new screenshots this pass |

**Files replaced in-place** (same name in `docs_draft/`):
- `docs/QUICKSTART.md` → `docs_draft/QUICKSTART.md`
- `docs/AUTHENTICATION.md` → `docs_draft/AUTHENTICATION.md`
- `docs/CONFIGURATION.md` → `docs_draft/CONFIGURATION.md`
- `docs/TROUBLESHOOTING.md` → `docs_draft/TROUBLESHOOTING.md`

**Files merged into single file:**
- `docs/CLI_REFERENCE.md` + `docs/mcp-server.md` → `docs_draft/TOOLS-REFERENCE.md`

**Files out of scope:**
- `scripts/install-mcp.sh`, `scripts/install-mcp.ps1` — neither promoted nor displaced

#### Step 2: Link audit and repair

Using the inventory from Phase 1c, fix all inbound links to retiring files:

**In `README.md` (root):**
- `docs/CLI_REFERENCE.md` → `docs/TOOLS-REFERENCE.md`
- `docs/mcp-server.md` → `docs/TOOLS-REFERENCE.md`
- `docs/INSTALLATION.md` → `docs/QUICKSTART.md`
- `docs/ARCHITECTURE.md` → `dev_docs/ARCHITECTURE.md` (broken link fix)
- `docs/README.md` → remove; replace with `docs/QUICKSTART.md` or `docs/TOOLS-REFERENCE.md`

**In `CLAUDE.md`:**
- Any references to `docs/mcp-server.md` → `docs/TOOLS-REFERENCE.md`

**In `dev_docs/README.md`:**
- Update documentation navigation links to the new filenames

**In `dev_docs/` files:**
- Update any links pointing to `docs/CLI_REFERENCE.md` or `docs/mcp-server.md`

#### Step 3: Promote `docs_draft/` to `docs/`

```bash
# Move retiring files to displaced
git mv docs/FAQ.md dev_docs/displaced/FAQ.md
git mv docs/CHATGPT_MCP_INTEGRATION.md dev_docs/displaced/CHATGPT_MCP_INTEGRATION.md
git mv docs/INSTALLATION.md dev_docs/displaced/INSTALLATION.md
git mv docs/images dev_docs/displaced/images

# Move CLI_REFERENCE and mcp-server.md to displaced (merged into TOOLS-REFERENCE)
git mv docs/CLI_REFERENCE.md dev_docs/displaced/CLI_REFERENCE.md
git mv docs/mcp-server.md dev_docs/displaced/mcp-server.md

# Remove existing files being replaced in-place (git mv won't overwrite without -f)
git rm docs/QUICKSTART.md
git rm docs/AUTHENTICATION.md
git rm docs/CONFIGURATION.md
git rm docs/TROUBLESHOOTING.md

# Promote docs_draft: move each new file into docs/, then remove staging dir
git mv docs_draft/QUICKSTART.md docs/QUICKSTART.md
git mv docs_draft/AUTHENTICATION.md docs/AUTHENTICATION.md
git mv docs_draft/MCP-CLIENTS.md docs/MCP-CLIENTS.md
git mv docs_draft/CONFIGURATION.md docs/CONFIGURATION.md
git mv docs_draft/TOOLS-REFERENCE.md docs/TOOLS-REFERENCE.md
git mv docs_draft/TROUBLESHOOTING.md docs/TROUBLESHOOTING.md

# Root README remains in place and is edited directly as the docs hub
git rm docs_draft/.gitkeep
rmdir docs_draft
```

#### Step 4: Promotion PR

PR title: `docs: replace docs/ with 6-file set and root README documentation hub`

PR checklist:
- [x] All 6 `docs_draft/` files reviewed and approved
- [x] Root `README.md` updated as the only documentation hub
- [x] Phase 1c link inventory complete — all inbound links repaired
- [x] Root README.md dead links fixed
- [x] `CLAUDE.md` references updated
- [x] `dev_docs/README.md` navigation updated
- [x] `watercooler://instructions` resource updated (Phase 3)
- [x] Retiring files moved to `dev_docs/displaced/`
- [x] `docs_draft/` removed (empty after promotion)
- [x] No `watercooler credentials init` or `watercooler read` commands in any new file
- [x] All code blocks verified against `--help` output

---

## Acceptance Criteria

### Global (applies to all 6 docs files + root README docs sections)

- [ ] Sentence-case headings throughout
- [ ] Imperative voice for instructions ("Run this command", not "You can run")
- [ ] Every code block is copy-pasteable and produces the expected output
- [ ] No references to unshipped commands (`credentials init`, `read`)
- [ ] No T2/T3 architecture details (FalkorDB, Graphiti, LeanRAG) in prose
- [ ] Progressive disclosure: "if you need X, see [link]" rather than explaining inline

### Per-file

**Root README.md**
- [ ] Vocabulary table covers all 11 terms from brainstorm Key Decision #5
- [ ] Comparison table from FAQ.md included
- [ ] Learning path links to all 6 docs files in `docs/`
- [ ] "For AI agents" callout present
- [ ] No links to `docs/README.md`

**QUICKSTART.md**
- [ ] Steps 1–5 complete (install, auth, connect, health check, first thread)
- [ ] Health check failure routing present (link to TROUBLESHOOTING.md#server-not-loading)
- [ ] Upgrade path subsection present (`uv cache clean watercooler-cloud`)
- [ ] Auth step is exactly 2 lines + 1-line note
- [ ] No references to unshipped commands

**AUTHENTICATION.md**
- [ ] Decision callout at the very top (before any instructions)
- [ ] All 4 methods covered (gh CLI, env var, credentials.toml, SSH)
- [ ] Credentials format is `credentials.toml` (no JSON mention)
- [ ] `git-credential-watercooler` not mentioned (dashboard-only helper)

**MCP-CLIENTS.md**
- [x] 3 clients covered (Claude Code, Codex, Cursor); ChatGPT deferred to separate issue
- [ ] Each section is self-contained (no cross-references like "similar to above")
- [ ] Each section includes: config path + platform variants + copy-pasteable config
- [ ] ChatGPT is a stub (3–4 lines, links to official docs)

**CONFIGURATION.md**
- [ ] Opens with minimum viable config code block
- [ ] `#memory-backend` anchor present
- [ ] Environment variable reference table present (4 columns)
- [ ] Tier label glossary present (T1/T2/T3 plain-language descriptions)
- [ ] Precedence rules documented

**TOOLS-REFERENCE.md**
- [ ] Section 1: CLI commands table (all core commands, post-audit flags)
- [ ] Section 2: MCP tools — required-parameters table present
- [ ] Safety annotations table present (all ~34 tools classified)
- [ ] Memory tools gate callout present
- [ ] Every tool entry has: summary + safety + prerequisites + parameters table + example
- [ ] Common agent workflows section present (3 workflows)
- [ ] "AI agents" callout at top of MCP section

**TROUBLESHOOTING.md**
- [ ] Mermaid setup flowchart present
- [ ] 10 issues covered (see list in Phase 2, File 6)
- [ ] `#server-not-loading` anchor present (required by QUICKSTART.md)
- [ ] Migration guide subsection present (orphan-branch model)

---

## Content Disposition Summary

| Current file | Action | New location |
|---|---|---|
| `docs/QUICKSTART.md` | replaced | `docs/QUICKSTART.md` (from docs_draft) |
| `docs/INSTALLATION.md` | displaced | `dev_docs/displaced/INSTALLATION.md` |
| `docs/AUTHENTICATION.md` | replaced | `docs/AUTHENTICATION.md` (from docs_draft) |
| `docs/CONFIGURATION.md` | replaced | `docs/CONFIGURATION.md` (from docs_draft) |
| `docs/CLI_REFERENCE.md` | displaced + merged | `dev_docs/displaced/CLI_REFERENCE.md` |
| `docs/mcp-server.md` | displaced + merged | `dev_docs/displaced/mcp-server.md` |
| `docs/FAQ.md` | displaced | `dev_docs/displaced/FAQ.md` |
| `docs/TROUBLESHOOTING.md` | replaced | `docs/TROUBLESHOOTING.md` (from docs_draft) |
| `docs/CHATGPT_MCP_INTEGRATION.md` | displaced | `dev_docs/displaced/CHATGPT_MCP_INTEGRATION.md` |
| `docs/images/` | displaced | `dev_docs/displaced/images/` |
| Root `README.md` | updated in-place | Root `README.md` (links fixed) |

New file in `docs_draft/` (no existing counterpart):
- `docs_draft/MCP-CLIENTS.md` → `docs/MCP-CLIENTS.md`
- `docs_draft/TOOLS-REFERENCE.md` → `docs/TOOLS-REFERENCE.md`

---

## Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| CLI flag drift in examples | High | Medium | Phase 1a audit is blocking for TOOLS-REFERENCE; verify every example |
| Broken links on promotion | High | Medium | Phase 1c link inventory is a required pre-promotion step |
| MCP tool safety misclassification | Low | Medium | Phase 1b audit; mark uncertain tools `pending-audit` in table; resolve before promotion |
| `watercooler://instructions` resource out of sync | Medium | Low | Phase 3 is in-scope; coordinate with docs files |
| CONFIGURATION.md env var table drift | Medium | Low | Audit against `config_loader.py` and `config_schema.py` directly |
| T2/T3 implementation details leaking into prose | Medium | Low | Review each file against acceptance criteria before promotion |

---

## Dependencies & Prerequisites

- `uv` must be available in the test environment to verify install commands
- `watercooler` CLI must be installed to run `--help` audit (Phase 1a)
- MCP client (at least one: Claude Code) must be available to verify health check step
- `dev_docs/displaced/` must be created before Phase 4 (can be done in Phase 0)

---

## References

### Internal
- Brainstorm: `dev_docs/brainstorms/2026-03-02-user-docs-refresh-brainstorm.md`
- Source material: `docs/` (all 9 files)
- Config templates: `src/watercooler/templates/config.example.toml`, `credentials.example.toml`
- MCP tools source: `src/watercooler_mcp/tools/` (tool implementations)
- Resources: `src/watercooler_mcp/resources.py` (watercooler://instructions)
- CLI source: `src/watercooler/cli.py`
- Config schema: `src/watercooler/config_schema.py`, `src/watercooler/config_loader.py`
- Prior docs plan: `dev_docs/plans/2026-02-27-docs-configuration-md-parity-update-plan.md`
- Migration script: `scripts/migrate_to_orphan_branch.py`

### External
- Google developer documentation style guide — imperative voice, code block conventions
- MCP spec concepts: https://modelcontextprotocol.io/docs/concepts/tools
- Filesystem MCP server — safety annotations table pattern
- GitHub MCP server — self-contained client config blocks pattern
- Crawl4AI docs — setup doctor as first post-install step pattern
- Linear developer docs — auth decision paragraph pattern
