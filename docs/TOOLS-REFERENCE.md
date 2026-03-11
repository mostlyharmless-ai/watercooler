# Tools reference

Unified reference for all CLI commands and MCP tools.

---

## CLI commands

### Group 1 — Core (day-1)

| Command | Synopsis | Key flags | Example |
|---|---|---|---|
| `init-thread <topic>` | Create a new thread | `--title`, `--ball` (default: codex), `--status` | `watercooler init-thread feature-auth --title "Auth design"` |
| `say <topic>` | Post an entry; flip ball to counterpart | `--title`, `--body`, `--role`, `--type`, `--agent`, `--ball`, `--status` | `watercooler say feature-auth --title "Ready" --body "Done with design."` |
| `ack <topic>` | Post an entry; ball ownership unchanged by default | `--title`, `--body`, `--role`, `--type`, `--agent`, `--ball` | `watercooler ack feature-auth --title "Got it"` |
| `handoff <topic>` | Pass ball to counterpart; append note | `--note`, `--role` (default: pm), `--agent` | `watercooler handoff feature-auth --note "Ready for review"` |
| `list` | List all threads | `--open-only`, `--closed`, `--threads-dir` | `watercooler list --open-only` |
| `search <query>` | Search thread content | `--threads-dir` | `watercooler search "authentication flow"` |
| `config init` | Generate annotated `config.toml` | `--user`, `--project`, `--force` | `watercooler config init --user` |
| `config show` | Show resolved config | `--json`, `--sources`, `--project-path` | `watercooler config show --sources` |
| `config validate` | Validate config files | `--strict`, `--project-path` | `watercooler config validate --strict` |

### Group 2 — Extended (T1, less common)

| Command | Synopsis | Key flags |
|---|---|---|
| `set-status <topic> <status>` | Update thread status | `--threads-dir` |
| `set-ball <topic> <ball>` | Transfer ball ownership | `--threads-dir` |
| `sync` | **Removed.** Prints a deprecation message and exits. Thread sync is now automatic via the orphan branch worktree. | — | — |
| `reindex` | Rebuild thread index | `--threads-dir` |
| `web-export` | Generate HTML index | — |
| `unlock <topic>` | Clear advisory lock (debugging) | `--threads-dir` |
| `baseline-graph build` | Build baseline graph from thread data | — |
| `baseline-graph stats` | Show graph entry/thread counts | — |

For full flag details on any command, run `watercooler <cmd> --help`.

### Group 3 — Advanced / out of scope for most users

`check-branches`, `check-branch`, `merge-branch`, `archive-branch`, `install-hooks`,
`slack` (setup/test/status/disable), `memory` (build/export/stats), `append-entry`
(legacy).

Run `watercooler <cmd> --help` for flag details.

---

## MCP tools

These tools are called by your AI agent on your behalf — you describe what you want
captured ("document the decision and hand off to review"), and the agent selects and
invokes the appropriate tool. You can specify tools or parameters directly if you want
fine-grained control, but you rarely need to.

> **AI agents:** Before calling any tool, read the `watercooler://instructions` MCP
> resource for workflow guidance and ball mechanics. Call it with no arguments.

### Required parameters

Parameters vary by tool category. The table below describes **local stdio mode** (the
standard new-user setup). **Hosted mode** refers to a cloud deployment (e.g. via
[watercoolerdev.com](https://www.watercoolerdev.com)) where `code_path` is derived from
request context and some defaults differ — it is not the standard setup for new users.

| Category | Tools | `code_path` | `agent_func` |
|---|---|---|---|
| Thread read | `list_threads`, `read_thread`, `list_thread_entries`, `get_thread_entry`, `get_thread_entry_range` | required | not used |
| Thread write | `say`, `ack`, `handoff`, `set_status` | required | required |
| Memory / graph | `smart_query`, `search`, `find_similar`, `graphiti_add_episode`, `clear_graph_group`, `migrate_to_memory_backend`, etc. | varies — check each tool | not used |
| Utility / status | `whoami`, `reindex`, `daemon_status`, `daemon_findings`, `memory_task_status` | not accepted | not used |

`agent_func` format: `"<platform>:<model>:<role>"` — e.g., `"Claude Code:sonnet-4:implementer"`.
Valid roles: `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`.

Entry author display names come from your configured agent identity (see
[CONFIGURATION.md](./CONFIGURATION.md)). For teams where multiple people use the same
client type, use `Agent (person)` naming with lowercase tags such as `Codex (jay)` and
`Codex (caleb)`.

> Passing `code_path` to Utility / status tools will cause the call to fail. Diagnostic
> tools (`watercooler_health`, `watercooler_diagnose_memory`) accept `code_path` as an
> optional parameter for context-aware checks.

### Safety annotations

> **Memory tiers:** T1 = baseline graph with summaries and embeddings; T2 = episodic
> knowledge graph (requires FalkorDB + LLM config); T3 = semantic hierarchical index.
> Most users start without any memory tier — core thread tools work without it. See
> [CONFIGURATION.md — memory backend](./CONFIGURATION.md#memory-backend) to enable.

| Tool | Safety | Prerequisites |
|---|---|---|
| `watercooler_list_threads` | read-only | none |
| `watercooler_read_thread` | read-only | none |
| `watercooler_list_thread_entries` | read-only | none |
| `watercooler_get_thread_entry` | read-only | none |
| `watercooler_get_thread_entry_range` | read-only | none |
| `watercooler_health` | read-only | none |
| `watercooler_whoami` | read-only | none |
| `watercooler_baseline_graph_stats` | read-only | none |
| `watercooler_baseline_sync_status` | read-only | none |
| `watercooler_access_stats` | read-only | none |
| `watercooler_memory_task_status` | read-only; mutating when `recover=True` or `retry_dead_letters=True` | none |
| `watercooler_search` | read-only | none (T2 for `mode="facts"`) |
| `watercooler_smart_query` | read-only | none (T2/T3 for higher tiers) |
| `watercooler_find_similar` | read-only | T1 embeddings |
| `watercooler_federated_search` | read-only | federation config |
| `watercooler_get_entry_provenance` | read-only | none |
| `watercooler_get_entity_edge` | read-only | T2 |
| `watercooler_migration_preflight` | read-only | none |
| `watercooler_graph_recover` | instruction-only — returns instructions; does not modify data | none |
| `watercooler_reindex` | idempotent | none |
| `watercooler_say` | mutating — appends entry, triggers git sync; calling twice creates two entries | none |
| `watercooler_ack` | mutating — appends entry, triggers git sync | none |
| `watercooler_handoff` | mutating — appends entry, triggers git sync | none |
| `watercooler_set_status` | mutating — always updates `last_updated` and rewrites projection | none |
| `watercooler_graphiti_add_episode` | mutating — deduplicates only when `entry_id` provided | T2 |
| `watercooler_bulk_index` | mutating but resumable — idempotent with dedup | T2 |
| `watercooler_graph_enrich` | mutating but resumable — processes only missing items by default | T1 |
| `watercooler_graph_project` | mutating but resumable — writes derived markdown projections | T1 |
| `watercooler_leanrag_run_pipeline` | mutating | T3 |
| `watercooler_diagnose_memory` | read-only | T2 |
| `watercooler_migrate_to_memory_backend` | mutating but resumable — defaults to `dry_run=true` | T2 |
| `watercooler_daemon_status` | read-only | daemon |
| `watercooler_daemon_findings` | read-only | daemon |
| `watercooler_clear_graph_group` | **destructive** — cannot be undone; requires `confirm=true` | T2 |

---

### `watercooler_list_threads`
List all threads with ball ownership and NEW markers. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `open_only` | bool | no | `true` = open threads only, `false` = closed only, omit = all |
| `format` | string | no | `"markdown"` (default) |
| `scan` | bool | no | Include per-entry summaries for every thread (default: false) |
| `limit` | int | no | Max threads to return (default: 50) |

**Example:**
```python
watercooler_list_threads(code_path=".")
```

---

### `watercooler_read_thread`
Read a thread's full content or a condensed summary. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `format` | string | no | `"markdown"` (default) or `"json"` |
| `summary_only` | bool | no | Return only summaries, not full bodies (~90% token reduction) |
| `code_branch` | string | no | Branch filter (default: current branch; pass `"*"` for all) |

**Example:**
```python
watercooler_read_thread(topic="feature-auth", code_path=".", summary_only=True)
```

---

### `watercooler_list_thread_entries`
List entry headers with summaries; use for large threads before fetching full bodies. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `offset` | int | no | Zero-based entry offset (default: 0) |
| `limit` | int | no | Max entries (default: all from offset) |
| `format` | string | no | `"json"` (default) or `"markdown"` |
| `code_branch` | string | no | Branch filter (default: current branch; pass `"*"` for all) |

**Example:**
```python
watercooler_list_thread_entries(topic="feature-auth", code_path=".", offset=0, limit=5)
```

---

### `watercooler_get_thread_entry`
Get a single entry by index or entry ID. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `index` | int | one of† | Zero-based entry index |
| `entry_id` | string | one of† | ULID from entry footer |
| `format` | string | no | `"json"` (default) or `"markdown"` |

† Provide `index` or `entry_id`, not both.

**Example:**
```python
watercooler_get_thread_entry(topic="feature-auth", code_path=".", index=0)
```

---

### `watercooler_get_thread_entry_range`
Return a contiguous range of entries. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `start_index` | int | no | Start index (default: 0) |
| `end_index` | int | no | Inclusive end index (default: last entry) |
| `summary_only` | bool | no | Return summaries only (default: false) |
| `format` | string | no | `"json"` (default) or `"markdown"` |
| `code_branch` | string | no | Branch filter (default: current branch; pass `"*"` for all) |

**Example:**
```python
watercooler_get_thread_entry_range(topic="feature-auth", code_path=".", start_index=0, end_index=4)
```

---

### Thread write tools

> Thread entries and status are updated only when a mutating write tool is called:
> `watercooler_say`, `watercooler_ack`, `watercooler_handoff`, or
> `watercooler_set_status`. Watercooler does not passively capture background agent
> activity. Memory and graph tools (e.g. `watercooler_bulk_index`) are also mutating
> but operate on the memory tier, not thread entries or ball state.

### `watercooler_say`
Add an entry and flip the ball to your counterpart. | Safety: mutating | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `title` | string | yes | Entry title (brief summary) |
| `body` | string | yes | Entry content (markdown supported) |
| `code_path` | string | yes | Path to code repo root |
| `agent_func` | string | yes | `"<platform>:<model>:<role>"` |
| `role` | string | no | `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe` (default: implementer) |
| `entry_type` | string | no | `Note`, `Plan`, `Decision`, `PR`, `Closure` (default: Note) |

> **Note:** To update thread status, call `watercooler_set_status` separately after `say`.

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

---

### `watercooler_ack`
Add an entry without flipping the ball. | Safety: mutating | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `agent_func` | string | yes | `"<platform>:<model>:<role>"` |
| `title` | string | no | Entry title (default: "Ack") |
| `body` | string | no | Entry content (default: `"ack"` in local mode, `"Acknowledged"` in hosted mode) |

> **Tip:** `watercooler_ack` has no explicit `role` parameter (unlike `watercooler_say`).
> Include `Spec: <role>` as the first line of `body` to make your specialization explicit
> in the thread record — e.g. `body="Spec: implementer\n\nStarting implementation."`.

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

---

### `watercooler_handoff`
Pass the ball to another agent explicitly. | Safety: mutating | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `agent_func` | string | yes | `"<platform>:<model>:<role>"` |
| `note` | string | no | Handoff message |
| `target_agent` | string | no | Recipient agent name (uses counterpart if omitted) |

**Example:**
```python
watercooler_handoff(
    topic="feature-auth",
    note="Design approved. Ready to implement.",
    code_path=".",
    agent_func="Claude Code:sonnet-4:pm"
)
```

---

### `watercooler_set_status`
Update thread status. | Safety: mutating — always writes `last_updated` and rewrites projection | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `status` | string | yes | New status: `OPEN`, `IN_REVIEW`, `CLOSED`, `BLOCKED`, or custom |
| `code_path` | string | yes | Path to code repo root |
| `agent_func` | string | yes | `"<platform>:<model>:<role>"` |

**Example:**
```python
watercooler_set_status(topic="feature-auth", status="CLOSED", code_path=".", agent_func="Claude Code:sonnet-4:pm")
```

---

### Utility tools

### `watercooler_health`
Check server health, git auth, and setup status. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | no | Repo path for context-aware checks |

**Example:**
```python
watercooler_health()
```

---

### `watercooler_whoami`
Get your resolved agent identity. | Safety: read-only | Prerequisites: none

No parameters.

**Example:**
```python
watercooler_whoami()
```

---

### `watercooler_reindex`
Rebuild the thread index from source data. | Safety: idempotent | Prerequisites: none

No parameters.

**Example:**
```python
watercooler_reindex()
```

---

### `watercooler_baseline_graph_stats`
Get thread and entry counts from the baseline graph. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |

---

### `watercooler_baseline_sync_status`
Check whether each thread's baseline graph is up to date. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |

---

### `watercooler_access_stats`
Report access patterns and usage statistics. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |

---

### `watercooler_memory_task_status`
Show status of queued memory indexing tasks. | Safety: read-only (mutating when `recover=True` or `retry_dead_letters=True`) | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `task_id` | string | no | Task ID to check. Omit for queue summary. |
| `recover` | bool | no | If `true`, reset stale "running" tasks to "pending" (default: false) |
| `retry_dead_letters` | bool | no | If `true`, move dead-letter tasks back to queue (default: false) |

**Examples:**
```python
# Queue summary
watercooler_memory_task_status()

# Check a specific task
watercooler_memory_task_status(task_id="01ABCDEF...")

# Recover stale tasks
watercooler_memory_task_status(recover=True)
```

---

### Memory and search tools

> **Memory features require additional setup.** See
> [CONFIGURATION.md — memory backend](./CONFIGURATION.md#memory-backend) to enable.
> If you haven't set this up yet, skip this section — the core thread tools work
> without it.

### `watercooler_smart_query`
Multi-tier intelligent query; recommended for most recall tasks. | Safety: read-only | Prerequisites: none (T2/T3 for higher tiers)

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Natural language question |
| `code_path` | string | yes | Path to code repo root |
| `max_tiers` | int | no | Max tiers to query (default: 2) |
| `force_tier` | string | no | Force a specific tier: `"T1"`, `"T2"`, or `"T3"` |
| `group_ids` | list | no | Optional project group IDs to filter results (default: all groups) |

**Example:**
```python
watercooler_smart_query(
    query="What authentication method was decided?",
    code_path=".",
    max_tiers=2
)
```

---

### `watercooler_search`
Unified search across entries, entities, episodes, and temporal facts. | Safety: read-only | Prerequisites: none (T2 for `mode="facts"`)

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Search query |
| `code_path` | string | yes | Path to code repo root |
| `mode` | string | no | `"auto"` (default; resolves to `"entries"`), `"entries"`, `"entities"`, `"episodes"`, `"facts"` |
| `limit` | int | no | Max results (default: 10) |
| `semantic` | bool | no | Use embedding search (default: false) |

**Example:**
```python
watercooler_search(query="OAuth decision", code_path=".", mode="entries")
```

---

### `watercooler_find_similar`
Find entries semantically similar to a given entry. | Safety: read-only | Prerequisites: T1 embeddings

| Parameter | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | yes | Source entry ULID |
| `code_path` | string | yes | Path to code repo root |
| `limit` | int | no | Max results (default: 5) |
| `similarity_threshold` | float | no | Minimum cosine similarity, 0.0–1.0 (default: 0.5) |
| `use_embeddings` | bool | no | Use embedding similarity (default: true) |

---

### `watercooler_federated_search`
Cross-namespace keyword search across configured repositories. | Safety: read-only | Prerequisites: federation config

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Search query (max 500 chars) |
| `code_path` | string | no | Primary repo root |
| `namespaces` | string | no | Comma-separated namespace IDs (empty = all configured) |
| `limit` | int | no | Max results (default: 10) |

---

### `watercooler_get_entry_provenance`
Bidirectional lookup between T1 entries and T2 episodes. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | one of† | Entry ULID (entry → episodes direction) |
| `episode_uuid` | string | one of† | Episode UUID (episode → entry direction) |
| `code_path` | string | yes | Path to code repo root |

† Provide `entry_id` or `episode_uuid`, not both.

**Example:**
```python
watercooler_get_entry_provenance(episode_uuid="ep-uuid-123", code_path=".")
```

---

### `watercooler_get_entity_edge`
Look up an entity relationship edge from the T2 graph. | Safety: read-only | Prerequisites: T2

| Parameter | Type | Required | Description |
|---|---|---|---|
| `uuid` | string | yes | Edge UUID |
| `code_path` | string | no | Path to code repo root |

---

### `watercooler_migration_preflight`
Dry-run check before migrating to a new memory backend. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |

---

### Graph management tools

### `watercooler_graph_recover`
Returns step-by-step recovery instructions. Does not modify data directly. | Safety: instruction-only | Prerequisites: none

No parameters. Returns instructions for manual recovery using `scripts/recover_baseline_graph.py`.

---

### `watercooler_graph_enrich`
Generate or regenerate summaries and embeddings. | Safety: mutating but resumable | Prerequisites: T1

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `summaries` | bool | no | Generate entry summaries (default: true) |
| `embeddings` | bool | no | Generate embeddings (default: true) |
| `mode` | string | no | `missing` (default, safe), `selective`, `all` |
| `topics` | string | no | Comma-separated topics (for `selective` mode) |
| `dry_run` | bool | no | Preview without modifying data |

---

### `watercooler_graph_project`
Write derived markdown projections from the graph. | Safety: mutating but resumable | Prerequisites: T1

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `mode` | string | no | `missing` (default), `selective`, `all` |
| `topics` | string | no | Comma-separated topics (required for `selective` mode) |
| `overwrite` | bool | no | Required when `mode="all"` |
| `dry_run` | bool | no | Preview without writing |

---

### `watercooler_graphiti_add_episode`
Index an entry or content into the T2 memory graph. | Safety: mutating | Prerequisites: T2

| Parameter | Type | Required | Description |
|---|---|---|---|
| `content` | string | yes | Text to index |
| `group_id` | string | yes | Graph group ID for partitioning (e.g. `"watercooler_cloud"`) |
| `code_path` | string | no | Path to code repo root |
| `entry_id` | string | no | Entry ULID — when provided, deduplicates (skips if already indexed) |
| `timestamp` | string | no | ISO 8601 timestamp (default: now) |
| `title` | string | no | Episode title |
| `source_description` | string | no | Description of the content source |

---

### `watercooler_bulk_index`
Bulk-index thread entries into memory. Idempotent with dedup. | Safety: mutating but resumable | Prerequisites: T2

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `threads` | string | no | Comma-separated thread topics to index (default: all) |
| `backend` | string | no | Target backend: `"graphiti"` (default) or `"leanrag"` |

---

### `watercooler_leanrag_run_pipeline`
Run the T3 hierarchical indexing pipeline. | Safety: mutating | Prerequisites: T3

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `start_date` | string | no | ISO 8601 start date filter |
| `end_date` | string | no | ISO 8601 end date filter |
| `dry_run` | bool | no | Preview without executing (default: false) |
| `incremental` | bool | no | Use incremental update if cluster state exists (default: true) |

---

### `watercooler_diagnose_memory`
Diagnose T2 memory tier health and connectivity. | Safety: read-only | Prerequisites: T2

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | no | Path to code repo root |

---

### `watercooler_migrate_to_memory_backend`
Migrate thread data to a memory backend. Defaults to dry run. | Safety: mutating but resumable | Prerequisites: T2

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `dry_run` | bool | no | Preview without migrating (default: true) |
| `backend` | string | no | Target backend: `"graphiti"` (default) or `"leanrag"` |
| `topics` | string | no | Comma-separated thread topics to migrate (default: all) |

---

### `watercooler_clear_graph_group`
**Destructive.** Permanently delete a graph group. Cannot be undone. | Safety: **destructive** | Prerequisites: T2

| Parameter | Type | Required | Description |
|---|---|---|---|
| `group_id` | string | yes | Graph group identifier |
| `confirm` | bool | yes | Must be `true` to proceed — prevents accidental deletion |
| `code_path` | string | yes | Path to code repo root |

**Example:**
```python
watercooler_clear_graph_group(group_id="my-group", confirm=True, code_path=".")
```

---

### Daemon tools

### `watercooler_daemon_status`
Check daemon health and configuration. | Safety: read-only | Prerequisites: daemon

| Parameter | Type | Required | Description |
|---|---|---|---|
| `daemon` | string | no | Filter by daemon name (default: all) |

---

### `watercooler_daemon_findings`
Retrieve findings reported by the background daemon. | Safety: read-only | Prerequisites: daemon

| Parameter | Type | Required | Description |
|---|---|---|---|
| `daemon` | string | no | Filter by daemon name |
| `severity` | string | no | Filter by severity level |
| `category` | string | no | Filter by finding category |
| `topic` | string | no | Filter by thread topic |
| `limit` | int | no | Max results (default: 50) |
| `unacknowledged_only` | bool | no | Return only unacknowledged findings (default: false) |

---

## Common agent workflows

### Session start sequence

Run these three tools at the start of a work session to orient yourself:

```python
# 1. Verify setup is healthy
watercooler_health()

# 2. See where you have the ball
watercooler_list_threads(code_path=".")

# 3. Recall recent context for the topic you're working on
watercooler_smart_query(query="recent decisions about feature-auth", code_path=".")
```

---

### Entry type selection guide

| Type | Use when |
|---|---|
| `Note` | Status update, observation, or general message (default) |
| `Plan` | Proposing a design or approach |
| `Decision` | Recording a resolved choice |
| `PR` | Linking to or commenting on a pull request |
| `Closure` | Wrapping up a thread before closing |

---

### Thread closure sequence

```python
# 1. Post a closure entry
watercooler_say(
    topic="feature-auth",
    title="Feature complete",
    body="Spec: pm\n\nMerged in PR #42. Closing thread.",
    entry_type="Closure",
    role="pm",
    code_path=".",
    agent_func="Claude Code:sonnet-4:pm"
)

# 2. Update status
watercooler_set_status(topic="feature-auth", status="CLOSED", code_path=".", agent_func="Claude Code:sonnet-4:pm")
```

Each workflow example includes all required parameters (`code_path` on all tools;
`agent_func` on all write tools).
