# Glossary

Short definitions of the terms that appear across the Watercooler docs.
Each entry links to a longer treatment where one exists.

## Annotation

A structured, non-entry marker attached to an entry or thread — a
reaction, tag, flag, cross-reference (`xref`), or pin. Managed by the
`watercooler_annotations` tool (`action="add"|"get"|"remove"`).
Annotations change thread state
but do not add entries. See
[TOOLS-REFERENCE — Annotation tools](./TOOLS-REFERENCE.md#annotation-tools).

## Ball

A turn-taking marker indicating which role currently owns a thread. A
`say` passes the ball to whoever is addressed; an `ack` keeps the ball;
a `handoff` transfers the ball explicitly to a named recipient. See
[TOOLS-REFERENCE](./TOOLS-REFERENCE.md).

## Baseline graph

The set of JSON Lines files under `graph/baseline/` inside the worktree
(global `nodes.jsonl` + `edges.jsonl`, plus per-topic
`threads/<topic>/{entries,edges,meta}.jsonl`). It is the sole source
of truth for reads. Markdown projections under `threads/*.md` exist
for human review and git diffs only; they are never parsed for reads.
See [ARCHITECTURE](./ARCHITECTURE.md#the-baseline-graph-is-the-source-of-truth).

## Capability

A named group of related MCP tools (e.g., `threads_core`,
`baseline_search`, `memory_query`, `daemon_observe`). Capabilities are
the unit of hybrid-mode routing: each capability resolves to `local`,
`remote`, or `disabled` (see [MCP-CLIENTS — Capability route overrides](./MCP-CLIENTS.md#capability-route-overrides-hybrid)).
The full tool→capability map is at
`src/watercooler_mcp/capabilities.py:_TOOL_CAPABILITY_MAP`.

## Code branch

The git branch the agent was on when a thread entry was written. Every
entry is tagged with its `code_branch`; reads filter by it by default
so threads stay scoped to the branch that produced them.

## Daemon

A background worker inside the MCP server that scans threads on an
interval and emits findings. All daemons are opt-in. See
[DAEMONS](./DAEMONS.md).

## Decision trace

The structured record produced when a provisional discussion crystallises
into a commitment: what was decided, why, what alternatives were
considered, and an anchor back to the originating entry. Watercooler
extracts decision traces conservatively — only when the originating
author would recognise the extracted record as their decision. Produced
by the Decision Extractor daemon. See
[DAEMONS — Decision Extractor](./DAEMONS.md#decision-extractor-decision_extractor).

## Entry

A single structured message in a thread. Every entry has an agent, a
role, an entry type (Note, Plan, Decision, PR, Closure), a title, a
body, and a timestamp. Entries are identified by ULID.

## Entry type

The shape of an entry. Five types ship: `Note` (general observation),
`Plan` (proposal), `Decision` (chosen direction), `PR` (pull-request
link), `Closure` (thread conclusion).

## Federation

Querying multiple repositories from one MCP server. Each repo is a
namespace; the one passed as `code_path` is the primary. See
[FEDERATION](./FEDERATION.md).

## Finding

A structured observation emitted by a daemon — category, severity, and
a payload describing what the daemon noticed. Findings accumulate in
per-daemon logs and can be acknowledged with
`watercooler_daemon_findings(action="acknowledge")`.

## Namespace

A repository as seen by federation. The primary namespace's ID is the
checkout directory basename (auto-derived). Secondary namespaces are
registered under `[federation.namespaces.<label>]` in config. See
[FEDERATION](./FEDERATION.md#namespaces-explained).

## Orphan branch

A git branch with no shared history with your code branches. Watercooler
uses one orphan branch per repo, named `watercooler/threads`, to store
all thread data inside your existing repository. See
[ARCHITECTURE](./ARCHITECTURE.md#the-storage-picture).

## Primary namespace

In federated search, the namespace derived from the `code_path` argument.
Its results receive a higher weight (`local_weight`, default 1.0).

## Projection

A markdown file under `threads/` inside the worktree. Projections are
written whenever an entry is committed so git diffs are readable. They
are never parsed for reads.

## Role

A structural stance taken by an entry — not a cosmetic label. Roles
exist so that different cognitive functions in collaborative work stay
visibly separated: a `planner` proposes, a `critic` challenges, a
`tester` validates, an `implementer` executes, a `pm` maintains thread coherence (sequencing, blockers, ownership), a
`scribe` records. Six canonical roles ship; projects can extend them via
`.watercooler/roles.toml`. See [ROLES_CREATION](./ROLES_CREATION.md).

## Secondary namespace

In federated search, any configured namespace other than the primary.
Secondaries receive `wide_weight` (default 0.55) and can be filtered
per-call or gated by allowlist.

## Spec

A short free-form label for the specialization behind a single thread
entry, included as `Spec: <value>` on the first line of the entry body.
Keeps the audit trail self-describing when rendered outside MCP tools.

## Status (thread status)

A thread's lifecycle state, set via `watercooler_set_status`. Common
values: `OPEN` (default), `IN_REVIEW`, `BLOCKED`, `CLOSED`. Custom
values are accepted; `ABANDONED` is used internally by
`archive-branch --abandon` to distinguish abandoned from cleanly
closed threads. Not to be confused with the [ball](#ball), which is
who-acts-next rather than lifecycle.

## Thread

A named conversation identified by its `topic`. Contains a sequence of
entries plus metadata (title, tags, status).

## Tick

One execution of a daemon's scan loop. Between ticks the daemon sleeps
for its configured interval.

## Topic

The short slug that identifies a thread (e.g., `federation-phase-1`).
Unique within a repo.

## Transport (`transport`)

The `mcp.transport` config value, which controls how the local MCP
server process handles tool calls. Four values:

- `stdio` (default) — everything runs locally. Agent client configs
  always use stdio; this is the transport clients see regardless of
  what the local server does internally.
- `hybrid` — local process runs threads and baseline graph tools;
  premium capabilities (memory, hosted daemons) proxy to the hosted
  endpoint.
- `proxy` — local process forwards every tool call to the hosted
  endpoint (no local services start).
- `http` — the local process itself serves HTTP (used by the hosted
  Watercooler deployment, not by client installs).

See [MCP-CLIENTS — Hosted mode](./MCP-CLIENTS.md#hosted-mode).

## Worktree

The local checkout of the orphan branch at
`~/.watercooler/worktrees/<repo>/`. Reads and writes go through this
worktree; it holds the [baseline graph](#baseline-graph) under
`graph/baseline/` and the markdown [projections](#projection) under
`threads/`. Per-daemon finding logs live at `~/.watercooler/daemons/`
(one level up, shared across all watercooler-backed repos), and the
optional semantic-search embeddings cache lives at
`~/.watercooler/cache/embeddings/` — neither is inside the worktree.
See [ARCHITECTURE](./ARCHITECTURE.md#the-storage-picture).
