# Tools reference

Reference for public CLI commands and MCP tools in open-source Watercooler.

## Contents

- [CLI commands](#cli-commands)
- [MCP tools](#mcp-tools)
  - [Required parameters](#required-parameters)
  - [Safety annotations](#safety-annotations)
- [Thread read tools](#thread-read-tools)
- [Thread write tools](#thread-write-tools)
- [Utility tools](#utility-tools)
- [Search and graph tools](#search-and-graph-tools)
- [Annotation tools](#annotation-tools)
- [Destructive tools](#destructive-tools)
- [Daemon tools](#daemon-tools)
- [Common agent workflows](#common-agent-workflows)

## CLI commands

### Group 1 — Core

| Command | Synopsis | Key flags | Example |
|---|---|---|---|
| `init-thread <topic>` | Create a new thread | `--title`, `--ball`, `--status` | `watercooler init-thread feature-auth --title "Auth design"` |
| `say <topic>` | Post an entry and flip the ball | `--title`, `--body`, `--role`, `--type`, `--agent`, `--ball`, `--status` | `watercooler say feature-auth --title "Ready" --body "Done with design."` |
| `ack <topic>` | Post an entry without changing the ball by default | `--title`, `--body`, `--role`, `--type`, `--agent`, `--ball` | `watercooler ack feature-auth --title "Got it"` |
| `handoff <topic>` | Pass the ball and append a note | `--note`, `--role`, `--agent` | `watercooler handoff feature-auth --note "Ready for review"` |
| `list` | List all threads | `--open-only`, `--closed`, `--threads-dir` | `watercooler list --open-only` |
| `search <query>` | Search thread content | `--threads-dir` | `watercooler search "authentication flow"` |
| `config init` | Generate annotated `config.toml` | `--user`, `--project`, `--force` | `watercooler config init --user` |
| `config show` | Show resolved config | `--json`, `--sources`, `--project-path` | `watercooler config show --sources` |
| `config validate` | Validate config files | `--strict`, `--project-path` | `watercooler config validate --strict` |
| `setup-stop-hook` | Wire `watercooler-stop-hook` as a Stop hook | — | `watercooler setup-stop-hook` |

### Group 2 — Extended

| Command | Synopsis | Key flags |
|---|---|---|
| `set-status <topic> <status>` | Update thread status | `--threads-dir` |
| `set-ball <topic> <ball>` | Transfer ball ownership | `--threads-dir` |
| `reindex` | Rebuild thread index | `--threads-dir` |
| `web-export` | Generate HTML index | — |
| `unlock <topic>` | Clear advisory lock | `--threads-dir` |
| `baseline-graph build` | Build baseline graph from thread data | — |
| `baseline-graph stats` | Show graph entry/thread counts | — |
| `sync-repair` | Diagnose and repair orphan-branch sync issues | `--diagnose`, `--dry-run`, `--regenerate-cache`, `--migrate`, `--json` |
| `sync` | Inspect or flush the async git sync queue | `--code-path`, `--threads-dir` |

### Group 3 — Branch lifecycle

Manage the interaction between code branches and the `watercooler/threads`
orphan branch. These are infrequent operations, usually run once per
feature-branch lifecycle.

| Command | Synopsis | Key flags |
|---|---|---|
| `check-branch <branch>` | Validate branch pairing for a specific code branch | `--code-root` |
| `check-branches` | Comprehensive audit of all branch pairings | `--code-root`, `--include-merged` |
| `merge-branch <branch>` | Merge the paired threads branch to `main` | `--code-root`, `--force` |
| `archive-branch <branch>` | Close OPEN threads on the branch, merge to `main`, then delete the threads branch | `--code-root`, `--abandon` (sets OPEN → `ABANDONED` instead of `CLOSED`), `--force` (skip confirmation prompts) |
| `install-hooks` | Install git hooks that validate branch pairing on commit/push | `--code-root`, `--hooks-dir`, `--force` |

### Group 4 — Slack integration

Configure the Slack webhook integration defined in `[mcp.slack]` of
[CONFIGURATION.md](./CONFIGURATION.md#mcpslack--slack-integration).

| Command | Synopsis |
|---|---|
| `slack setup` | Interactive webhook setup — prompts for webhook URL, bot token, channel |
| `slack status` | Show current Slack configuration |
| `slack test` | Send a test notification to verify the webhook |
| `slack disable` | Disable Slack notifications |

### Group 5 — Memory graph (premium)

Memory operations depend on the `[memory]` backend configured in
`~/.watercooler/config.toml`. **These require the `watercooler_memory`
package**, which ships only in premium/hosted builds — on open-core
installs the CLI exits with an actionable message pointing at the
premium extras.

| Command | Synopsis |
|---|---|
| `memory build` | Build the memory graph from thread data |
| `memory export` | Export the memory graph to an external format |
| `memory stats` | Show memory graph statistics (node / edge / episode counts) |

### Scripting alternative — `append-entry`

`append-entry` is a low-level CLI for adding entries with explicit flags.
It predates the agent-driven MCP `say` tool and is useful for scripts,
CI hooks, or one-off tooling where passing `--agent`, `--role`, `--type`,
`--title`, and `--body` on the command line is simpler than calling the
MCP tool. Prefer `say` / `ack` / `handoff` for interactive agent work.

```bash
watercooler append-entry feature-auth \
  --agent "CI" --role implementer --title "Build passed" \
  --body "GitHub Actions run 12345 green on all targets" \
  --type Note
```

For full flag details on any command, run `watercooler <cmd> --help`.

## MCP tools

These tools are called by your AI agent on your behalf. You describe the intent
("document the decision and hand off to review"), and the agent chooses the
appropriate tool.

> **AI agents:** Before calling any tool, read the `watercooler://instructions`
> MCP resource for workflow guidance and ball mechanics.

### Required parameters

| Category | Tools | `code_path` | `agent_func` |
|---|---|---|---|
| Thread read | `list_threads`, `read_thread`, `list_thread_entries`, `get_thread_entry` | required | not used |
| Thread write | `say`, `ack`, `handoff`, `set_status` | required | required |
| Thread admin | `delete_entry`, `delete_thread`, `archive_thread` | optional | not used |
| Annotations | `annotations` | optional | not used |
| Search / graph | `search` (incl. `seed_entry_id=`/`federated=` modes), `smart_query`, `graph_enrich`, `graph_project`, `sync_repair` | varies | not used |
| Utility / status | `health`, `roles` | varies | not used |

`agent_func` format:
`"<platform>:<model>:<role>"` — for example,
`"Claude Code:sonnet-4:implementer"`.

Canonical roles: `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`.

### Safety annotations

| Tool | Safety |
|---|---|
| `watercooler_list_threads` | read-only |
| `watercooler_read_thread` | read-only |
| `watercooler_list_thread_entries` | read-only |
| `watercooler_get_thread_entry` | read-only |
| `watercooler_roles` | read-only |
| `watercooler_health` | read-only |
| `watercooler_baseline_graph` | read-only |
| `watercooler_access_stats` | read-only |
| `watercooler_search` | read-only |
| `watercooler_smart_query` | read-only |
| `watercooler_follow_xref` | read-only |
| `watercooler_say` | mutating |
| `watercooler_ack` | mutating |
| `watercooler_handoff` | mutating |
| `watercooler_set_status` | mutating |
| `watercooler_annotations` | mutating (`action="get"` is read-only) |
| `watercooler_archive_thread` | mutating |
| `watercooler_graph_enrich` | mutating but resumable |
| `watercooler_graph_project` | mutating but resumable |
| `watercooler_sync_repair` | read-only when `diagnose_only=True`; mutating otherwise |
| `watercooler_delete_entry` | destructive |
| `watercooler_delete_thread` | destructive |

## Thread read tools

### `watercooler_list_threads`

List all threads with ball ownership and NEW markers.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `open_only` | bool | no | `true` = open threads only, `false` = closed only, omit = all |
| `format` | string | no | `"markdown"` (default) |
| `scan` | bool | no | Include per-entry summaries for every thread |
| `tags` | string | no | Comma-separated tag names; all must be present on at least one entry |
| `flag` | string | no | Flag value substring match |
| `pinned` | bool | no | `true` = threads with pinned entries, `false` = threads without, omit = all |
| `limit` | int | no | Max threads to return (default: 50) |

**Example:**
```python
watercooler_list_threads(code_path=".")
watercooler_list_threads(code_path=".", tags="needs_review")
```

### `watercooler_read_thread`

Read a thread's full content or a condensed summary.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `format` | string | no | `"markdown"` (default) or `"json"` |
| `summary_only` | bool | no | Return only summaries, not full bodies |
| `code_branch` | string | no | Branch filter; pass `"*"` for all |

**Example:**
```python
watercooler_read_thread(topic="feature-auth", code_path=".", summary_only=True)
```

### `watercooler_list_thread_entries`

List entry headers with summaries before fetching full bodies.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `offset` | int | no | Zero-based entry offset (default: 0) |
| `limit` | int | no | Max entries (default: all from offset) |
| `format` | string | no | `"json"` (default) or `"markdown"` |
| `code_branch` | string | no | Branch filter; pass `"*"` for all |
| `filter` | string | no | In-thread keyword search — case-insensitive substring matched against each entry's title and body; only matching entries are returned |

**Example:**
```python
watercooler_list_thread_entries(topic="feature-auth", code_path=".", offset=0, limit=5)
```

### `watercooler_get_thread_entry`

Get a single entry, or a contiguous range of entries.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `index` | int | one of† | Zero-based entry index; the range start when `to_index` is set |
| `entry_id` | string | one of† | ULID from entry footer |
| `to_index` | int | no | Inclusive end index — set it to return the range `index .. to_index` |
| `summary_only` | bool | no | Range form only: return summaries, no bodies (default: false) |
| `format` | string | no | `"json"` (default) or `"markdown"` |
| `code_branch` | string | no | Range form only: branch filter; pass `"*"` for all |

† Provide `index` or `entry_id` for a single entry, not both. For a range,
pass `index` (the start, default 0) together with `to_index`; `entry_id`
cannot be combined with `to_index`.

**Examples:**
```python
# Single entry
watercooler_get_thread_entry(topic="feature-auth", code_path=".", index=0)
# Contiguous range (inclusive)
watercooler_get_thread_entry(topic="feature-auth", code_path=".", index=0, to_index=4)
```

## Thread write tools

### `watercooler_say`

Add an entry and flip the ball to your counterpart.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `title` | string | yes | Entry title |
| `body` | string | yes | Entry content (markdown supported) |
| `code_path` | string | yes | Path to code repo root |
| `agent_func` | string | yes | `"<platform>:<model>:<role>"` |
| `role` | string | no | Role name (default: `implementer`) |
| `entry_type` | string | no | `Note`, `Plan`, `Decision`, `PR`, `Closure` |

**Example:**
```python
watercooler_say(
    topic="feature-auth",
    title="Implementation complete",
    body="Spec: implementer\n\nPR #42 ready for review.",
    code_path=".",
    agent_func="Claude Code:sonnet-4:implementer",
    entry_type="PR"
)
```

### `watercooler_ack`

Add an entry without flipping the ball.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `agent_func` | string | yes | `"<platform>:<model>:<role>"` |
| `title` | string | no | Entry title (default: `"Ack"`) |
| `body` | string | no | Entry content |

**Tip:** `watercooler_ack` has no explicit `role` parameter. Include
`Spec: <role>` as the first line of `body` if you want the specialization to be
explicit in the thread record.

> **Not to be confused with daemon findings.** To acknowledge a daemon finding,
> use `watercooler_daemon_findings(action="acknowledge")`. `watercooler_ack`
> writes a thread entry; it does not mark findings as seen.

**Example:**
```python
watercooler_ack(
    topic="feature-auth",
    title="Building",
    body="Spec: implementer\n\nStarting implementation, keeping ball.",
    code_path=".",
    agent_func="Claude Code:sonnet-4:implementer"
)
```

### `watercooler_handoff`

Pass the ball to another agent explicitly.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `agent_func` | string | yes | `"<platform>:<model>:<role>"` |
| `note` | string | no | Handoff message |
| `target_agent` | string | no | Recipient agent name |

**Example:**
```python
watercooler_handoff(
    topic="feature-auth",
    note="Design approved. Ready to implement.",
    code_path=".",
    agent_func="Claude Code:sonnet-4:pm"
)
```

### `watercooler_set_status`

Update thread status.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `status` | string | yes | New status: `OPEN`, `IN_REVIEW`, `CLOSED`, `BLOCKED`, or custom |
| `code_path` | string | yes | Path to code repo root |
| `agent_func` | string | yes | `"<platform>:<model>:<role>"` |

**Example:**
```python
watercooler_set_status(
    topic="feature-auth",
    status="CLOSED",
    code_path=".",
    agent_func="Claude Code:sonnet-4:pm"
)
```

## Utility tools

### `watercooler_health`

Check server health, git auth, and setup status. With `detail="identity"`,
returns the resolved agent identity and a write-readiness assessment instead
(folded-in `watercooler_whoami`).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | no | Repo path for context-aware checks |
| `detail` | string | no | `"identity"` → resolved agent identity + write-readiness |

**Example:**
```python
watercooler_health()                      # full health check
watercooler_health(detail="identity")      # who am I + write-readiness
```

### `watercooler_roles`

List the project's roles, or — with `role` — return one role's full
behavioral specification.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to repo root |
| `role` | string | no | Role name. Empty → catalog; set → that role's full spec |

**Example:**
```python
watercooler_roles(code_path=".")                  # catalog
watercooler_roles(code_path=".", role="critic")   # one role's full spec
```

### `watercooler_baseline_graph`

Baseline-graph diagnostics, selected by `scope`.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `scope` | string | no | `"stats"` (default — thread/entry counts + status breakdown) or `"sync"` (per-thread baseline-graph sync health) |
| `code_path` | string | yes | Path to code repo root |

### `watercooler_access_stats`

Report access patterns and usage statistics.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |

## Search and graph tools

### `watercooler_search`

Search entries and thread content.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Search query |
| `code_path` | string | yes | Path to code repo root |
| `mode` | string | no | `"auto"` or `"entries"` on open-core; `"entities"`, `"episodes"`, and `"facts"` require a memory backend (hosted builds) |
| `limit` | int | no | Max results (default: 10) |
| `query_operator` | string | no | `"OR"` (default — any token matches, ranked by token-match count) or `"AND"` (every token required) |
| `semantic` | bool | no | Use embedding search (default: false) |
| `tags` | string | no | Comma-separated tag names; all must be present |
| `flag` | string | no | Flag value substring match |
| `pinned` | bool | no | `true` = has pinned entries, `false` = has no pinned entries |

> **Mode availability.** Open-core installs answer `"auto"` and `"entries"`
> from the baseline graph. The `"entities"`, `"episodes"`, and `"facts"` modes
> depend on a memory backend and are available in hosted builds; calling them
> on an open-core install returns a memory-unavailable error rather than
> results.

**Example:**
```python
watercooler_search(query="OAuth decision", code_path=".", mode="entries")
watercooler_search(query="sync", code_path=".", tags="sync-hardening")
```

### `watercooler_smart_query`

Ask a natural-language question over local thread history and baseline context.

> **Backend scope.** On open-core this tool answers from the baseline graph
> only. Tier escalation and provenance resolution require a memory backend,
> which ships in hosted builds. The tier- and provenance-related parameters
> below are accepted on open-core for API compatibility but have no effect
> without a memory backend configured.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Natural language question |
| `code_path` | string | yes | Path to code repo root |
| `max_tiers` | int | no | Max tiers to query (default: 2) *(memory backend only; no-op on open-core)* |
| `force_tier` | string | no | Force a specific tier *(memory backend only; no-op on open-core)* |
| `group_ids` | list | no | Optional project group IDs to filter results |
| `resolve_provenance` | bool | no | Enrich evidence with `provenance.thread_entry_id` when available *(memory backend only; no-op on open-core)* |

**Example:**
```python
watercooler_smart_query(
    query="What authentication method was decided?",
    code_path=".",
    max_tiers=2
)
```

### `watercooler_search(seed_entry_id=...)` — seeded similarity

Find entries semantically similar to a given entry — the seeded-similarity
mode of `watercooler_search` (folded-in `watercooler_find_similar`).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `seed_entry_id` | string | yes | Source entry ULID |
| `code_path` | string | no | Path to code repo root |
| `limit` | int | no | Max results (default: 10) |
| `semantic_threshold` | float | no | Minimum cosine similarity, 0.0–1.0 |

### `watercooler_search(federated=True)` — federated search

Fan a keyword query across configured namespaces and return merged, ranked
results — the federated mode of `watercooler_search` (folded-in
`watercooler_federated_search`). Requires `federation.enabled = true` in
config. See [Federation](FEDERATION.md) for setup details.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `federated` | bool | yes | `True` triggers federated search |
| `query` | string | yes | Search text. Max 500 characters. |
| `code_path` | string | no | Primary repo root. Determines which namespace is "local". |
| `namespaces` | string | no | Comma-separated namespace IDs to query. Leave empty to query all configured namespaces. |
| `limit` | int | no | Max results 1–100 (default: 10). |

Results are scored with a multiplicative formula: `normalize(base_score) × namespace_weight × recency_decay`. Primary namespace results use `local_weight` (default 1.0); secondaries use `wide_weight` (default 0.55).

The response includes a `namespace_status` map so you can tell whether
each secondary succeeded, timed out, or was skipped.

### `watercooler_graph_enrich`

Generate or regenerate summaries and embeddings.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `summaries` | bool | no | Generate entry summaries (default: true) |
| `embeddings` | bool | no | Generate embeddings (default: true) |
| `mode` | string | no | `missing` (default), `selective`, `all` |
| `topics` | string | no | Comma-separated topics for `selective` mode |
| `dry_run` | bool | no | Preview without modifying data |

### `watercooler_graph_project`

Write derived markdown projections from the graph.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `mode` | string | no | `missing` (default), `selective`, `all` |
| `topics` | string | no | Comma-separated topics for `selective` mode |
| `overwrite` | bool | no | Required when `mode="all"` |
| `dry_run` | bool | no | Preview without writing |

### `watercooler_sync_repair`

Diagnose and fix orphan-branch sync issues.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `code_path` | string | yes | — | Path to repo root |
| `diagnose_only` | bool | no | `false` | Report state without making changes |
| `dry_run` | bool | no | `false` | Show what would be done without executing |
| `regenerate_cache` | bool | no | `false` | Rebuild manifest + search index from per-thread data |
| `migrate` | bool | no | `false` | One-time cleanup of globally committed derived files |
| `confirm_migrate` | bool | no | `false` | Required to execute `migrate` without `dry_run` |

**Recommended workflow:**
```python
watercooler_sync_repair(code_path=".", diagnose_only=True)
watercooler_sync_repair(code_path=".", dry_run=True)
watercooler_sync_repair(code_path=".")
```

## Annotation tools

### `watercooler_annotations`

Add, read, or remove annotations on an entry or thread, selected by `action`.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string | yes | `"add"`, `"get"`, or `"remove"` |
| `topic` | string | yes | Thread topic identifier |
| `target_id` | string | varies | Entry ID or thread topic. Required for `add`/`remove`; for `get`, omit to return all annotation states for the thread |
| `target_type` | string | for add/remove | `"entry"` or `"thread"` |
| `kind` | string | for add/remove | `add`: `reaction`, `tag`, `flag`, `xref`, `pin`. `remove`: `tag_remove`, `flag_clear`, `xref_remove`, `unpin`, `reaction_remove` |
| `value` | string | varies | Annotation payload (ignored for `pin`/`unpin`) |
| `code_path` | string | no | Path to code repo root |
| `actor` | string | no | Who is making the change (add/remove) |

**Example:**
```python
watercooler_annotations(
    action="add",
    topic="feature-auth",
    target_id="01ABC...",
    target_type="entry",
    kind="tag",
    value="needs_review",
    code_path="."
)
watercooler_annotations(action="get", topic="feature-auth", code_path=".")
```

### `watercooler_follow_xref`

Resolve an entry's annotation xrefs into entry summaries in a single call.
Bundles `watercooler_annotations` (`action="get"`) + per-xref `watercooler_get_thread_entry`
into one round-trip. Output ordering mirrors `annotation_state.xrefs`.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic that contains the source entry |
| `target_id` | string | yes | Entry ID (ULID, with or without `entry:` prefix) whose xrefs should be resolved |
| `code_path` | string | no | Path to code repo root (ignored in hosted mode) |

**Returns:** JSON with `{schema_version, topic, target_id, count, xrefs: [...]}`.
Each xref record is `{entry_id, topic, title, type, role, agent, timestamp, summary}`.
Unresolved xrefs (entry_id not found anywhere) appear as placeholders with
`missing: true` and a human-readable `note`, never a 500.

**Example:**
```python
watercooler_follow_xref(
    topic="feat-option-b",
    target_id="DEC1234567890ABCDEFGHJKMNP",
    code_path=".",
)
```

## Destructive tools

### `watercooler_delete_entry`

Permanently delete a specific entry from a thread. Uses a two-step confirmation
flow.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `entry_id` | string | yes | Entry ID (ULID) to delete |
| `code_path` | string | no | Path to code repo root |
| `confirmation_token` | string | no | Token from first call to confirm deletion |

### `watercooler_delete_thread`

Permanently delete an entire thread and all its entries. Uses a two-step
confirmation flow.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | no | Path to code repo root |
| `confirmation_token` | string | no | Token from first call to confirm deletion |

### `watercooler_archive_thread`

Archive or unarchive a thread.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | no | Path to code repo root |
| `reason` | string | no | Why the thread is being archived |
| `unarchive` | bool | no | If `true`, unarchive instead of archive |

---

## Daemon tools

See [Daemons](DAEMONS.md) for daemon setup, configuration, and finding categories.

### `watercooler_daemon_status`

Check daemon health and configuration.

**Safety:** read-only (side-effecting when `trigger=True`)
**Prerequisites:** daemon

| Parameter | Type | Required | Description |
|---|---|---|---|
| `daemon` | string | no | Filter by daemon name (default: all) |
| `trigger` | bool | no | Wake the target daemon immediately (default: false); target is `daemon` if given, else `t2_indexer` *(premium)* |

When `trigger=True` the response shape changes: daemon status is nested under `"daemons"` and a top-level `"triggered": true` (plus optional `"trigger_error"`) is added. The wake is **asynchronous** — call this tool again after a short wait to see updated `last_tick_*` metrics.

---

### `watercooler_daemon_findings`

Retrieve findings reported by the background daemon.

**Safety:** read-only
**Prerequisites:** daemon

| Parameter | Type | Required | Description |
|---|---|---|---|
| `daemon` | string | no | Filter by daemon name (e.g., `thread_auditor`, `decision_detector`) |
| `severity` | string | no | Filter by severity level |
| `category` | string | no | Filter by finding category (e.g., `decision_candidate`) |
| `topic` | string | no | Filter by thread topic |
| `limit` | int | no | Max results (default: 50) |
| `unacknowledged_only` | bool | no | Return only unacknowledged findings (default: false) |
| `enrich` | bool | no | Overlay additional context onto premium `coordinator_lead` findings (default: false). Has no effect on open-core daemons. |
| `code_path` | string | no | Path to the code repository root (default: `.`). Used to derive `repo_key` for S3 pulse-context enrichment. Supply an explicit value in multi-repo workspaces or when the MCP server's working directory may differ from the target repository. |

**Available daemons (open-core):** `sync_guard`, `thread_auditor`, `decision_detector`, `decision_extractor`, `decision_stance`

Premium and hosted builds expose additional daemons — `content_scout`,
`content_refiner`, `pulse_snapshot`, `analysis_snapshot`, `trend_snapshot`,
`t2_indexer`, `project_coordinator`, and `coordinator_refiner`. Filtering by
a premium daemon name on an open-core install returns an empty list (no
error).

> **`project_coordinator` (premium).** The `project_coordinator` daemon
> emits investigation leads (`coordinator_lead`) with read-only suggested
> actions, plus a handful of awareness and stalled-state categories. Its
> full finding schema, enrichment overlays, and example payloads are
> documented in the hosted reference at
> [watercoolerdev.com/docs](https://www.watercoolerdev.com/docs).
> `watercooler_daemon_findings(daemon="project_coordinator", ...)` on
> open-core returns an empty list (no error).
>
> **`connect_role_complement` (info, disabled by default).** Fires when
> thread A lacks a monitored role (default: `tester`, `critic`) that is
> actively exercised in a related thread B. Relation evidence is
> multi-tier: Tier 1 (explicit xref annotation), Tier 2 (shared `pair:`
> tag), or Tier 3+4 (pulse_block co-affected risk cluster ∩ shared
> workflow shape — both required). Ships disabled; enable with
> `role_complement_enabled = true` in `[coordinator]` config.
>
> `details` keys on each finding: `missing_role`, `related_thread_topic`,
> `related_thread_role_entry_count`, `relation_evidence` (list of
> evidence items, each with a `tier` key from `{"xref", "pair_tag",
> "pulse_block+workflow_shape"}`). Tier-specific fields:
> `xref` → `source_entry_id`, `target_entry_id`, `direction`;
> `pair_tag` → `tags` (sorted list of all shared `pair:`-prefixed tags);
> `pulse_block+workflow_shape` → `risk_rule_id`, `risk_text`,
> `workflow_shape_name`. Wrapped as `coordinator_lead` with
> `source_category = "connect_role_complement"` when `leads_enabled =
> true`. See [CONFIGURATION.md](CONFIGURATION.md) for
> `role_complement_*` config keys.
>
> **`decision_stance` (open-core).** Converts decision-pipeline
> findings into per-role `stance_advisory` findings (one per
> canonical role: `planner`, `critic`, `tester`). Each advisory
> cites the detector/extractor finding IDs that drove it via
> `details.advisory.source_lead_ids` (capped at 10; check
> `details.source_lead_ids_truncated` for partial citations). Topics
> are namespaced `stance:{role}`. See
> [DAEMONS.md](DAEMONS.md#decision-stance-decision_stance) for the
> full spec, including the rule that `decision_stance` is skipped
> when premium `project_coordinator` is active.
>
> **`coordinator_xref_suppression` (info).** When a thread with unresolved
> `Plan` entries has an xref annotation pointing at a `Decision` entry in
> another thread, the coordinator suppresses the `stalled_open_loop`
> finding and emits a `coordinator_xref_suppression` info finding instead.
> `details.xref_resolves_to` names the resolving target entry; agents and
> operators can audit suppressions with
> `watercooler_daemon_findings(daemon="project_coordinator", category="coordinator_xref_suppression")`.
>
> **Tag-based suppression (`suppression_tags`).** When a thread carries
> an annotation tag that matches `project_coordinator.suppression_tags`
> (default `parked`, `wontfix`, `deferred`), coordinator findings on
> that thread acquire a `details.suppressed_by: "tag:<name>"` marker.
> `stalled_open_loop` and `stalled_dropout` findings are downgraded from
> `warning` to `info` severity; `aware_burst` and `aware_role_concentration`
> findings preserve their native `info` severity and add the marker
> without changing severity. Leads generated from suppressed source
> findings inherit the same severity and `suppressed_by` marker so
> parked threads stay quiet end-to-end.
>
> **`coordinator_lead` `t2_context` schema (v2).** Each `coordinator_lead`
> finding's `details.lead.t2_context` is a dict with the following shape
> when `AnalysisSnapshotDaemon` data is available (`null` otherwise):
>
> | Key | Type | Description |
> |---|---|---|
> | `schema_version` | int | `2` for payloads written by Phase 3b-1+. v1 payloads used `"stalled"` instead of `"analysis_stalled"`. |
> | `analysis_stalled` | bool | Whether the analysis snapshot marked this thread stalled within the analysis window. Renamed from `"stalled"` in v1 to avoid confusion with coordinator staleness semantics. |
> | `days_since_last` | int or null | Days since the last thread entry, per the analysis snapshot. |
> | `workflow_shape_id` | str or null | Workflow shape identifier from the analysis snapshot. |
> | `workflow_shape_name` | str or null | Human-readable workflow shape name. |
> | `workflow_confidence` | float or null | Workflow shape classification confidence (0–1). |
> | `has_decision` | bool | Thread has at least one `Decision` entry, per analysis. |
> | `has_closure` | bool | Thread has at least one `Closure` entry, per analysis. |
> | `entry_count_total` | int or null | Total entry count from the analysis snapshot. |
> | `recommendation_rule_ids` | list[str] | Analysis rule IDs that flagged this thread (sorted, deduplicated). |
>
> **Backward compat:** `CoordinatorLead.from_dict()` migrates v1 payloads
> on read — if `"stalled"` is present without `"analysis_stalled"`, the key
> is renamed automatically. Writers (the daemon) always emit v2.

> **`coordinator_refiner` (premium).** Layer-2 LLM synthesis daemon that
> reads unacknowledged `coordinator_lead` findings produced by
> `project_coordinator` and emits `refined_coordinator_lead` findings under
> its own producer identity. Per-lead 1:1 refinement: one refined finding
> per raw lead, no clustering. Narrative-only output — no multi-dimensional
> scoring. Findings-only posture: the refiner does not write thread entries,
> annotate source leads, or mutate `project_coordinator` state.
>
> Query with
> `watercooler_daemon_findings(daemon="coordinator_refiner", category="refined_coordinator_lead")`.
> On open-core installs this returns an empty list (no error).
>
> `details` schema (v1) on each refined finding:
>
> | Key | Type | Description |
> |---|---|---|
> | `schema_version` | int | `1` for v1 payloads. |
> | `source_finding_id` | str | Raw `coordinator_lead` finding id this refinement came from (singular; no clustering in v1). |
> | `source_category` | str | Copied from the source lead's `source_category` so reviewers can filter without re-resolving the source finding. |
> | `source_topic` | str | Source lead's `source_topic`; also equal to the outer Finding `topic` field. |
> | `source_summary` | str | Verbatim copy of the source lead's `summary`. |
> | `assessment` | str | LLM-produced prose, 2–4 sentences. |
> | `recommended_next_step` | str | LLM-produced prose, 1–2 sentences. One concrete investigation step; no specific agent is nominated. |
> | `relevance_tags` | list[str] | Verbatim passthrough from source lead. |
> | `suggested_action` | dict or null | Verbatim passthrough from source lead; not rewritten by the LLM. |
> | `source_t2_context` | dict or null | Verbatim passthrough of the source lead's `t2_context` (same v2 schema documented above). Key is always present; value is `null` when the raw lead had no `t2_context`. |
>
> Refined findings have `severity = "info"` (suggestions, never alerts) and
> acknowledge independently of the source lead: acking the raw
> `coordinator_lead` does not ack its refined finding and vice versa. See
> [CONFIGURATION.md](CONFIGURATION.md) for `coordinator_refiner` config
> keys.

---

### `watercooler_pulse_snapshot` *(premium)*
Read the cached Project Pulse snapshot maintained by `PulseSnapshotDaemon`. | Safety: read-only | Prerequisites: daemon (`pulse_snapshot` enabled)

> **Premium only.** The `pulse_snapshot` daemon is excluded from open-core
> builds. This tool will report `status: "unavailable"` with `reason:
> "daemon_not_running"` on an open-source install.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | no | Path to the code repository root (default: `.`) |

**Response status/reason codes:**

| `status` | `reason` | Meaning |
|---|---|---|
| `unavailable` | `daemon_not_running` | MCP server started without the daemon manager |
| `unavailable` | `disabled` | `[mcp.daemons.pulse_snapshot] enabled = false` in config (default) |
| `error` | `invalid_code_path` | `code_path` does not exist or is not a directory |
| `unavailable` | `no_snapshot` | Daemon running but hasn't ticked yet, or `code_path` doesn't match daemon's repo. Includes `daemon_repo_key` and `requested_repo_key` for path-mismatch diagnosis. |
| `ok` | — | Fresh snapshot available under `"snapshot"` key |

**To trigger a fresh snapshot:**
```python
# 1. Wake the daemon
watercooler_daemon_status(daemon="pulse_snapshot", trigger=True)
# 2. Wait ~interval seconds (default 600), then read
watercooler_pulse_snapshot(code_path=".")
```

### `watercooler_daemon_findings(action="acknowledge")`
Mark one or more daemon findings as acknowledged. | Safety: **mutating** (`daemon_control`, L3) | Prerequisites: daemon

The acknowledge action of `watercooler_daemon_findings` (default `action="list"`).
Acknowledged findings are excluded from future
`watercooler_daemon_findings(unacknowledged_only=True)` queries.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string | yes | `"acknowledge"` |
| `daemon` | string | yes | Daemon that owns the finding(s) (from `findings[].daemon_name`, e.g., `project_coordinator`) |
| `finding_id` | string | one of† | A single finding ID to acknowledge |
| `finding_ids` | list[str] | one of† | A list of finding IDs to acknowledge in one call (bulk) |

† Provide `finding_id` and/or `finding_ids`.

**Response shape:**

| Field | Type | Description |
|---|---|---|
| `status` | string | `"ok"`, `"partial"`, `"not_found"`, or `"error"` |
| `daemon_name` | string | The owning daemon |
| `acknowledged` | list[str] | Finding IDs successfully marked acknowledged |
| `not_found` | list[str] | Finding IDs that did not resolve |
| `errors` | list | Per-id errors, if any |

**Example — consume, act, acknowledge loop:**
```python
# 1. Fetch unacknowledged findings (e.g. from decision_detector)
result = watercooler_daemon_findings(
    daemon="decision_detector",
    unacknowledged_only=True,
)
# 2. Act on each finding, collecting the ids you've handled
done = [f["finding_id"] for f in result["findings"]]
# 3. Acknowledge them in one bulk call
watercooler_daemon_findings(
    action="acknowledge",
    daemon="decision_detector",
    finding_ids=done,
)
# → {"status": "ok", "acknowledged": [...], "not_found": [], "errors": []}
```

---

## Common agent workflows

For the higher-level narrative patterns these snippets serve (ideation → plan, blocked,
handoff, closure, etc.), see [WORKFLOW_EXAMPLES.md](./WORKFLOW_EXAMPLES.md).

### Session start sequence

Run these three tools at the start of a work session:

```python
watercooler_health()
watercooler_list_threads(code_path=".")
watercooler_list_thread_entries(topic="your-topic", code_path=".")
```

### Capture a decision

```python
watercooler_say(
    topic="feature-auth",
    title="Decision: use OAuth app flow",
    body="Chosen approach and rationale...",
    role="planner",
    entry_type="Decision",
    code_path=".",
    agent_func="Codex:gpt-5-codex:planner"
)
```

### Diagnose sync state safely

```python
watercooler_sync_repair(code_path=".", diagnose_only=True)
watercooler_sync_repair(code_path=".", dry_run=True)
```
