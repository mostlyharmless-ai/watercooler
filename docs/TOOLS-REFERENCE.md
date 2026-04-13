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
| `setup-pulse-hook` | Wire `watercooler-capture-theme` as a PostCompact hook | — | `watercooler setup-pulse-hook` |

### Group 2 — Extended (T1, less common)

| Command | Synopsis | Key flags |
|---|---|---|
| `set-status <topic> <status>` | Update thread status | `--threads-dir` |
| `set-ball <topic> <ball>` | Transfer ball ownership | `--threads-dir` |
| `reindex` | Rebuild thread index | `--threads-dir` |
| `web-export` | Generate HTML index | — |
| `unlock <topic>` | Clear advisory lock (debugging) | `--threads-dir` |
| `baseline-graph build` | Build baseline graph from thread data | — |
| `baseline-graph stats` | Show graph entry/thread counts | — |
| `sync-repair` | Diagnose and repair orphan branch sync issues | `--diagnose`, `--dry-run`, `--regenerate-cache`, `--migrate`, `--json` |
| `sync` | Inspect or flush the async git sync queue | `--code-path`, `--threads-dir` |

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

### Dual-surface architecture

The MCP server exposes two HTTP surfaces:

| Surface | Path | Description |
|---|---|---|
| **Full** | `/mcp` | All tools available. Used by the dashboard and standalone local clients. |
| **Premium** | `/mcp/premium` | Memory, migration, and diagnostic tools only. Used by hybrid clients that run thread/graph tools locally but delegate memory-intensive work to a hosted endpoint. |

In **hybrid mode**, the local MCP server registers all tools but routes some calls to the
remote premium endpoint. Thread and graph tools execute locally; memory tools
(`smart_query`, `bulk_index`, `diagnose_memory`, etc.) are proxied to the hosted
service. The capability routing table in
[capabilities.py](../src/watercooler_mcp/capabilities.py) controls which tools run
locally, remotely, or are disabled. See
[CONFIGURATION.md](./CONFIGURATION.md) for hybrid setup.

### Required parameters

Parameters vary by tool category. The table below describes **local stdio mode** (the
standard new-user setup). **Hosted mode** refers to a cloud deployment (e.g. via
[watercoolerdev.com](https://www.watercoolerdev.com)) where `code_path` is derived from
request context and some defaults differ — it is not the standard setup for new users.

| Category | Tools | `code_path` | `agent_func` |
|---|---|---|---|
| Thread read | `list_threads`, `read_thread`, `list_thread_entries`, `get_thread_entry`, `get_thread_entry_range` | required | not used |
| Thread write | `say`, `ack`, `handoff`, `set_status` | required | required |
| Thread admin | `delete_entry`, `delete_thread`, `archive_thread` | optional | not used |
| Annotations | `annotate`, `remove_annotation`, `get_annotations` | optional | not used |
| Memory / graph | `smart_query`, `search`, `find_similar`, `graphiti_add_episode`, `clear_graph_group`, `migrate_to_memory_backend`, etc. | varies — check each tool | not used |
| Sync / repair | `sync_repair` | optional | not used |
| Utility / status | `whoami`, `reindex`, `daemon_status`, `daemon_findings`, `memory_task_status` | not accepted | not used |

`agent_func` format: `"<platform>:<model>:<role>"` — e.g., `"Claude Code:sonnet-4:implementer"`.
Canonical roles: `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`.
Projects may define additional roles in `.watercooler/roles.toml`. Use
`watercooler_roles(code_path)` to see the active role set, or
`watercooler_role_details(code_path, role)` for full behavioral guidance.

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
| `watercooler_roles` | read-only | none |
| `watercooler_role_details` | read-only | none |
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
| `watercooler_get_annotations` | read-only | none |
| `watercooler_reindex` | idempotent | none |
| `watercooler_say` | mutating — appends entry, triggers git sync; calling twice creates two entries | none |
| `watercooler_ack` | mutating — appends entry, triggers git sync | none |
| `watercooler_handoff` | mutating — appends entry, triggers git sync | none |
| `watercooler_set_status` | mutating — always updates `last_updated` and rewrites projection | none |
| `watercooler_annotate` | mutating — appends annotation event to thread graph | none |
| `watercooler_remove_annotation` | mutating — appends removal event to thread graph | none |
| `watercooler_archive_thread` | mutating — sets archived flag in meta.json, sets status to CLOSED | none |
| `watercooler_sync_repair` | mutating — diagnoses and repairs orphan branch sync issues | none |
| `watercooler_graphiti_add_episode` | mutating — deduplicates only when `entry_id` provided | T2 |
| `watercooler_bulk_index` | mutating but resumable — idempotent with dedup | T2 |
| `watercooler_graph_enrich` | mutating but resumable — processes only missing items by default | T1 |
| `watercooler_graph_project` | mutating but resumable — writes derived markdown projections | T1 |
| `watercooler_leanrag_run_pipeline` | mutating | T3 |
| `watercooler_diagnose_memory` | read-only | T2 |
| `watercooler_migrate_to_memory_backend` | mutating but resumable — defaults to `dry_run=true` | T2 |
| `watercooler_daemon_status` | read-only (side-effecting with `trigger=True`) | daemon |
| `watercooler_daemon_findings` | read-only | daemon |
| `watercooler_pulse_snapshot` | read-only | daemon (`pulse_snapshot` enabled) |
| `watercooler_delete_entry` | **destructive** — permanently removes entry from graph; requires confirmation token | none |
| `watercooler_delete_thread` | **destructive** — permanently removes thread and all entries; requires confirmation token | none |
| `watercooler_clear_graph_group` | **destructive** — cannot be undone; requires `confirm=true` | T2 |
| `watercooler_sync_repair` | read-only when `diagnose_only=True`; mutating otherwise — repairs git worktree state | none |

---

### `watercooler_list_threads`
List all threads with ball ownership and NEW markers. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |
| `open_only` | bool | no | `true` = open threads only, `false` = closed only, omit = all |
| `format` | string | no | `"markdown"` (default) |
| `scan` | bool | no | Include per-entry summaries for every thread (default: false) |
| `tags` | string | no | Comma-separated tag names; all must be present on at least one entry (AND, case-insensitive) |
| `flag` | string | no | Flag value substring match (case-insensitive); threads with a matching flag |
| `pinned` | bool | no | `true` = threads with pinned entries, `false` = threads without, omit = all |
| `limit` | int | no | Max threads to return (default: 50) |

**Example:**
```python
watercooler_list_threads(code_path=".")
watercooler_list_threads(code_path=".", tags="editorial_candidate")
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
| `role` | string | no | Role name (default: implementer). Invalid values are rejected. Use `watercooler_roles(code_path)` to list valid roles for a project. |
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

**Output includes:**
- Server version, agent identity, threads directory
- Graph services (LLM, embedding availability)
- Backend services (auto-start status)
- **Daemons** — each labeled `[local]` or `[Railway]` with tick/findings/error counts
- **Service telemetry** — per-service call counts, error rates, cache hit rates
- **Branch parity** — canonical state (clean, diverged, dirty_mixed, etc.) with recommended actions

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

### `watercooler_roles`
List all valid role names for a project — bundled defaults merged with any project-level overrides. | Safety: read-only | Prerequisites: none

The returned list is the exact set of values accepted by the `role` parameter of `watercooler_say`.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to repo root |
| `format` | string | no | `"markdown"` (default) or `"json"` |

**Example:**
```python
watercooler_roles(code_path=".")
```

---

### `watercooler_role_details`
Return the full behavioral specification for a single role. | Safety: read-only | Prerequisites: none

Returns: `description`, `produces`, `boundary`, `instructions`, `entry_style`, `when_to_use`, `handoff_to`, `collaborate_with`. For custom roles, also returns `canonical_role`. See [CONFIGURATION.md — Custom roles](./CONFIGURATION.md#custom-roles-watercoolerrolestoml) for how to define project-level roles.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to repo root |
| `role` | string | yes | Role name to look up (e.g. `"critic"`, `"security-audit"`) |
| `format` | string | no | `"markdown"` (default) or `"json"` |

**Example:**
```python
watercooler_role_details(code_path=".", role="critic")
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
| `resolve_provenance` | boolean | no | If `true`, auto-follows the 3-hop backtrace for T2 evidence (edge_uuid → episodes → entry_id) and enriches each T2 fact item with `provenance.thread_entry_id`. Call `watercooler_get_thread_entry(entry_id=thread_entry_id)` to fetch the full entry body. Best-effort: failures are silently skipped. Default: `false`. |

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
| `query_operator` | string | no | `"AND"` (default) requires all query tokens, `"OR"` matches any token |
| `semantic` | bool | no | Use embedding search (default: false) |
| `tags` | string | no | Comma-separated tag names; all must be present (AND, case-insensitive) |
| `flag` | string | no | Flag value substring match (case-insensitive) |
| `pinned` | bool | no | `true` = has pinned entries, `false` = has no pinned entries, omit = all |

**Example:**
```python
watercooler_search(query="OAuth decision", code_path=".", mode="entries")
watercooler_search(query="sync", code_path=".", tags="sync-hardening")
watercooler_search(query="", code_path=".", pinned=True)
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

### `watercooler_sync_repair`
Diagnose and fix orphan branch sync issues — stuck rebases, stale manifests, globally-committed derived files. | Safety: read-only when `diagnose_only=True`; mutating otherwise | Prerequisites: none

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `code_path` | string | yes | — | Path to repo root |
| `diagnose_only` | bool | no | `false` | Report state without making changes — always safe to run first |
| `dry_run` | bool | no | `false` | Show what would be done without executing |
| `regenerate_cache` | bool | no | `false` | Rebuild manifest + search index from per-thread data |
| `migrate` | bool | no | `false` | One-time cleanup of globally-committed derived files |
| `confirm_migrate` | bool | no | `false` | Required to execute `migrate` without `dry_run` |

**Recommended workflow:**
```python
# 1. Diagnose first — always safe
watercooler_sync_repair(code_path=".", diagnose_only=True)

# 2. Preview the repair
watercooler_sync_repair(code_path=".", dry_run=True)

# 3. Execute
watercooler_sync_repair(code_path=".")

# For migrate: two-step confirmation required
watercooler_sync_repair(code_path=".", migrate=True, dry_run=True)          # preview
watercooler_sync_repair(code_path=".", migrate=True, confirm_migrate=True)  # execute
```

**CLI equivalent:** `watercooler sync-repair [--diagnose] [--dry-run] [--regenerate-cache] [--migrate]`

> **Note:** If `migrate=True` is passed without `confirm_migrate=True` (and without
> `dry_run=True`), the tool returns `{"action": "confirm_migrate", "message": "..."}` rather
> than executing. This is intentional — migrate is destructive (git rm + push).

---

### `watercooler_annotate`
Add an annotation (reaction, tag, flag, cross-reference, or pin) to an entry or thread. | Safety: mutating | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `target_id` | string | yes | Entry ID (for entries) or thread topic (for threads) |
| `target_type` | string | yes | `"entry"` or `"thread"` |
| `kind` | string | yes | Annotation kind: `reaction`, `tag`, `flag`, `xref`, or `pin` |
| `value` | string | yes* | Emoji name (reaction), tag name (tag), agent/reason (flag), target entry_id (xref). Ignored for `pin`. |
| `code_path` | string | no | Path to code repo root |
| `actor` | string | no | Who is adding the annotation (defaults to HTTP context user or "unknown") |

**Example:**
```python
watercooler_annotate(
    topic="feature-auth",
    target_id="01ABC...",
    target_type="entry",
    kind="tag",
    value="editorial_candidate",
    code_path="."
)
```

---

### `watercooler_remove_annotation`
Remove an annotation from an entry or thread. | Safety: mutating | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `target_id` | string | yes | Entry ID (for entries) or thread topic (for threads) |
| `target_type` | string | yes | `"entry"` or `"thread"` |
| `kind` | string | yes | Removal kind: `tag_remove`, `flag_clear`, `xref_remove`, `unpin`, or `reaction_remove` |
| `value` | string | no | Tag name, flag reason, or xref entry_id to remove. Ignored for `unpin`. |
| `code_path` | string | no | Path to code repo root |
| `actor` | string | no | Who is removing the annotation |

**Example:**
```python
watercooler_remove_annotation(
    topic="feature-auth",
    target_id="01ABC...",
    target_type="entry",
    kind="tag_remove",
    value="editorial_candidate",
    code_path="."
)
```

---

### `watercooler_get_annotations`
Read annotations for an entry or thread. | Safety: read-only | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | no | Path to code repo root |
| `target_id` | string | no | Entry ID or thread topic. Omit to return all annotation states for the thread. |

**Example:**
```python
# Get annotations for a specific entry
watercooler_get_annotations(topic="feature-auth", target_id="01ABC...", code_path=".")

# Get all annotations for a thread
watercooler_get_annotations(topic="feature-auth", code_path=".")
```

---

### `watercooler_delete_entry`
**Destructive.** Permanently delete a specific entry from a thread. Uses a two-step confirmation flow. | Safety: **destructive** | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `entry_id` | string | yes | Entry ID (ULID) to delete |
| `code_path` | string | no | Path to code repo root |
| `confirmation_token` | string | no | Token from first call to confirm deletion |

Call once without `confirmation_token` to receive a token, then call again with the token
to execute the deletion.

**Example:**
```python
# Step 1: get confirmation token
watercooler_delete_entry(topic="feature-auth", entry_id="01ABC...", code_path=".")
# Returns: {"action": "confirm_delete", "confirmation_token": "a1b2c3..."}

# Step 2: confirm deletion
watercooler_delete_entry(
    topic="feature-auth",
    entry_id="01ABC...",
    code_path=".",
    confirmation_token="a1b2c3..."
)
```

---

### `watercooler_delete_thread`
**Destructive.** Permanently delete an entire thread and all its entries. Uses a two-step confirmation flow. | Safety: **destructive** | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | no | Path to code repo root |
| `confirmation_token` | string | no | Token from first call to confirm deletion |

Call once without `confirmation_token` to receive a token, then call again with the token
to execute the deletion.

**Example:**
```python
# Step 1: get confirmation token
watercooler_delete_thread(topic="old-thread", code_path=".")

# Step 2: confirm deletion
watercooler_delete_thread(topic="old-thread", code_path=".", confirmation_token="a1b2c3...")
```

---

### `watercooler_archive_thread`
Archive or unarchive a thread (soft-delete). Archived threads keep their data but are marked as archived and set to CLOSED status. | Safety: mutating | Prerequisites: none

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | no | Path to code repo root |
| `reason` | string | no | Why the thread is being archived (e.g., `resolved`, `abandoned`, `superseded`) |
| `unarchive` | bool | no | If `true`, unarchive instead of archive (sets status back to OPEN) |

**Example:**
```python
# Archive a thread
watercooler_archive_thread(topic="old-feature", reason="resolved", code_path=".")

# Unarchive a thread
watercooler_archive_thread(topic="old-feature", unarchive=True, code_path=".")
```

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
Check daemon health and configuration. | Safety: read-only (side-effecting when `trigger=True`) | Prerequisites: daemon

| Parameter | Type | Required | Description |
|---|---|---|---|
| `daemon` | string | no | Filter by daemon name (default: all) |
| `trigger` | bool | no | Wake the target daemon immediately (default: false); target is `daemon` if given, else `t2_indexer` |

When `trigger=True` the response shape changes: daemon status is nested under `"daemons"` and a top-level `"triggered": true` (plus optional `"trigger_error"`) is added. The wake is **asynchronous** — call this tool again after a short wait to see updated `last_tick_*` metrics.

---

### `watercooler_daemon_findings`
Retrieve findings reported by the background daemon. | Safety: read-only | Prerequisites: daemon

| Parameter | Type | Required | Description |
|---|---|---|---|
| `daemon` | string | no | Filter by daemon name (e.g., `thread_auditor`, `decision_detector`) |
| `severity` | string | no | Filter by severity level |
| `category` | string | no | Filter by finding category (e.g., `decision_candidate`) |
| `topic` | string | no | Filter by thread topic |
| `limit` | int | no | Max results (default: 50) |
| `unacknowledged_only` | bool | no | Return only unacknowledged findings (default: false) |

**Available daemons:** `thread_auditor`, `content_scout`, `content_refiner`, `decision_detector`, `decision_extractor`, `t2_indexer`, `pulse_snapshot`

---

### `watercooler_pulse_snapshot`
Read the cached Project Pulse snapshot maintained by `PulseSnapshotDaemon`. | Safety: read-only | Prerequisites: daemon (`pulse_snapshot` enabled)

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
