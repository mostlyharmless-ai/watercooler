# Tools reference

Reference for public CLI commands and MCP tools in open-source Watercooler.

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
| Thread read | `list_threads`, `read_thread`, `list_thread_entries`, `get_thread_entry`, `get_thread_entry_range` | required | not used |
| Thread write | `say`, `ack`, `handoff`, `set_status` | required | required |
| Thread admin | `delete_entry`, `delete_thread`, `archive_thread` | optional | not used |
| Annotations | `annotate`, `remove_annotation`, `get_annotations` | optional | not used |
| Search / graph | `search`, `smart_query`, `find_similar`, `federated_search`, `graph_enrich`, `graph_project`, `graph_recover`, `sync_repair` | varies | not used |
| Utility / status | `health`, `whoami`, `roles`, `role_details`, `reindex` | varies | not used |

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
| `watercooler_get_thread_entry_range` | read-only |
| `watercooler_roles` | read-only |
| `watercooler_role_details` | read-only |
| `watercooler_health` | read-only |
| `watercooler_whoami` | read-only |
| `watercooler_baseline_graph_stats` | read-only |
| `watercooler_baseline_sync_status` | read-only |
| `watercooler_access_stats` | read-only |
| `watercooler_search` | read-only |
| `watercooler_smart_query` | read-only |
| `watercooler_find_similar` | read-only |
| `watercooler_federated_search` | read-only |
| `watercooler_graph_recover` | instruction-only |
| `watercooler_reindex` | idempotent |
| `watercooler_say` | mutating |
| `watercooler_ack` | mutating |
| `watercooler_handoff` | mutating |
| `watercooler_set_status` | mutating |
| `watercooler_annotate` | mutating |
| `watercooler_remove_annotation` | mutating |
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
watercooler_list_threads(code_path=".", tags="editorial_candidate")
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

**Example:**
```python
watercooler_list_thread_entries(topic="feature-auth", code_path=".", offset=0, limit=5)
```

### `watercooler_get_thread_entry`

Get a single entry by index or entry ID.

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

### `watercooler_get_thread_entry_range`

Return a contiguous range of entries.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | yes | Path to code repo root |
| `start_index` | int | no | Start index (default: 0) |
| `end_index` | int | no | Inclusive end index (default: last entry) |
| `summary_only` | bool | no | Return summaries only (default: false) |
| `format` | string | no | `"json"` (default) or `"markdown"` |
| `code_branch` | string | no | Branch filter; pass `"*"` for all |

**Example:**
```python
watercooler_get_thread_entry_range(topic="feature-auth", code_path=".", start_index=0, end_index=4)
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

Check server health, git auth, and setup status.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | no | Repo path for context-aware checks |

**Example:**
```python
watercooler_health()
```

### `watercooler_whoami`

Get your resolved agent identity.

No parameters.

**Example:**
```python
watercooler_whoami()
```

### `watercooler_roles`

List all valid role names for a project.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to repo root |
| `format` | string | no | `"markdown"` (default) or `"json"` |

**Example:**
```python
watercooler_roles(code_path=".")
```

### `watercooler_role_details`

Return the full behavioral specification for a single role.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to repo root |
| `role` | string | yes | Role name to look up |
| `format` | string | no | `"markdown"` (default) or `"json"` |

**Example:**
```python
watercooler_role_details(code_path=".", role="critic")
```

### `watercooler_reindex`

Rebuild the thread index from source data.

No parameters.

**Example:**
```python
watercooler_reindex()
```

### `watercooler_baseline_graph_stats`

Get thread and entry counts from the baseline graph.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Path to code repo root |

### `watercooler_baseline_sync_status`

Check whether each thread's baseline graph is up to date.

| Parameter | Type | Required | Description |
|---|---|---|---|
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
| `mode` | string | no | `"auto"`, `"entries"`, `"entities"`, `"episodes"`, or `"facts"` |
| `limit` | int | no | Max results (default: 10) |
| `query_operator` | string | no | `"AND"` (default) or `"OR"` |
| `semantic` | bool | no | Use embedding search (default: false) |
| `tags` | string | no | Comma-separated tag names; all must be present |
| `flag` | string | no | Flag value substring match |
| `pinned` | bool | no | `true` = has pinned entries, `false` = has no pinned entries |

**Example:**
```python
watercooler_search(query="OAuth decision", code_path=".", mode="entries")
watercooler_search(query="sync", code_path=".", tags="sync-hardening")
```

### `watercooler_smart_query`

Ask a natural-language question over local thread history and baseline context.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Natural language question |
| `code_path` | string | yes | Path to code repo root |
| `max_tiers` | int | no | Max tiers to query (default: 2) |
| `force_tier` | string | no | Force a specific tier |
| `group_ids` | list | no | Optional project group IDs to filter results |
| `resolve_provenance` | bool | no | Enrich evidence with `provenance.thread_entry_id` when available |

**Example:**
```python
watercooler_smart_query(
    query="What authentication method was decided?",
    code_path=".",
    max_tiers=2
)
```

### `watercooler_find_similar`

Find entries semantically similar to a given entry.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | yes | Source entry ULID |
| `code_path` | string | yes | Path to code repo root |
| `limit` | int | no | Max results (default: 5) |
| `similarity_threshold` | float | no | Minimum cosine similarity, 0.0–1.0 |
| `use_embeddings` | bool | no | Use embedding similarity (default: true) |

### `watercooler_federated_search`

Search across configured namespaces or repositories.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Search query |
| `code_path` | string | no | Primary repo root |
| `namespaces` | string | no | Comma-separated namespace IDs |
| `limit` | int | no | Max results (default: 10) |

### `watercooler_graph_recover`

Return instructions for graph recovery.

No parameters. Returns instructions for manual recovery.

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

### `watercooler_annotate`

Add a reaction, tag, flag, cross-reference, or pin to an entry or thread.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `target_id` | string | yes | Entry ID or thread topic |
| `target_type` | string | yes | `"entry"` or `"thread"` |
| `kind` | string | yes | `reaction`, `tag`, `flag`, `xref`, or `pin` |
| `value` | string | yes* | Annotation payload; ignored for `pin` |
| `code_path` | string | no | Path to code repo root |
| `actor` | string | no | Who is adding the annotation |

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

### `watercooler_remove_annotation`

Remove an annotation from an entry or thread.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `target_id` | string | yes | Entry ID or thread topic |
| `target_type` | string | yes | `"entry"` or `"thread"` |
| `kind` | string | yes | `tag_remove`, `flag_clear`, `xref_remove`, `unpin`, or `reaction_remove` |
| `value` | string | no | Annotation payload for removal |
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

### `watercooler_get_annotations`

Read annotations for an entry or thread.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Thread topic identifier |
| `code_path` | string | no | Path to code repo root |
| `target_id` | string | no | Entry ID or thread topic. Omit to return all annotation states for the thread |

**Example:**
```python
watercooler_get_annotations(topic="feature-auth", code_path=".")
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
| `enrich` | bool | no | When `true`, overlay S1/S2/S3 context onto `coordinator_lead` findings before returning and emit an `enrichment_stats` key in the response (default: false). Overlays are local-only — hosted deployments skip S1/S2/S3 but still return `enrichment_stats`. |
| `code_path` | string | no | Path to the code repository root (default: `.`). Used to derive `repo_key` for S3 pulse-context enrichment. Supply an explicit value (e.g., from `watercooler_whoami()["code_path"]`) in multi-repo workspaces or when the MCP server's working directory may differ from the target repository. |

**Available daemons:** `thread_auditor`, `content_scout`, `content_refiner`, `decision_detector`, `decision_extractor`, `t2_indexer`, `pulse_snapshot`, `project_coordinator`

**`project_coordinator` finding categories:**

| Category | Description |
|---|---|
| `stalled_open_loop` | Thread has Plan entries but no Decision or Closure |
| `stalled_dropout` | Previously active contributor stopped participating |
| `aware_burst` | Sudden activity spike relative to rolling baseline |
| `aware_role_concentration` | Single role dominates thread participation |
| `aware_new_contributor` | First appearance of a contributor across the corpus |
| `stance_advisory` | Cross-role stance elevation (v1B — `topic` = `stance:<role>`) |
| `coordinator_lead` | Pre-built investigation lead layered on a v1A finding (v1B follow-on); `details["lead"]` carries a read-only `suggested_action` the agent can execute directly |

**Example — fetch leads only:**
```python
watercooler_daemon_findings(
    daemon="project_coordinator",
    category="coordinator_lead",
    unacknowledged_only=True,
)
```

Each `coordinator_lead` finding nests the full lead payload under
`details["lead"]` with fields `source_category`, `source_topic`, `summary`,
`relevance_tags`, and `suggested_action` (a `{phase, tool, arguments, reason}`
dict restricted to read-only tools).

**Read-time enrichment overlays** (applied per-lead when `enrich=true`):

| Key | Source | Presence |
|---|---|---|
| `hygiene_tags` | ThreadAuditor findings for the same topic | present when unacknowledged hygiene findings exist |
| `pending_decision_candidates` | DetectDecisionsDaemon candidates for the same topic | present when candidates exist |
| `suggested_action_override` | Replaces `details.lead.suggested_action` — same `{phase, tool, arguments, reason}` schema | present when `pending_decision_candidates > 0` |
| `pulse_context` | PulseSnapshot dimension scores (`goal_clarity`, `constraint_pressure`, `evidence_quality`, `execution_momentum`) | present when snapshot available; individual keys absent when their score is not yet computed |

All overlay keys are omitted when the source is unavailable — key absence is not an error.
In hosted mode all overlays are silently skipped (no overlay keys added).

**`enrichment_stats` response object** (present in the top-level response when `enrich=true`):

| Field | Type | Description |
|---|---|---|
| `attempted` | int | Number of signals attempted (always 3 in local mode when coordinator_lead findings exist; 0 when no leads or hosted context fails) |
| `succeeded` | int | Number of signals that contributed at least one overlay to at least one lead. `succeeded=0` on a clean repo (no hygiene issues, no pending decisions, no snapshot yet) is normal — it does not indicate a signal failure. |
| `skipped` | int | Number of signals skipped (3 in hosted mode; 0 in local mode) |
| `mode` | string | `"local"` or `"hosted"` |
| `error` | bool | Present and `true` only when `enrich_leads` threw an unexpected exception. Absent on clean runs (including zero-overlay runs). |

`enrichment_stats` is present whenever `enrich=true`, regardless of whether any
coordinator_lead findings were returned.  When no coordinator_lead findings exist,
`attempted=0` and `succeeded=0`.

To force fresh enrichment data, trigger a new snapshot:
```python
watercooler_daemon_status(daemon="pulse_snapshot", trigger=True)
```
Then re-call `watercooler_daemon_findings` — enrichment is recomputed on every call.

**Example — enriched leads:**
```python
watercooler_daemon_findings(
    daemon="project_coordinator",
    category="coordinator_lead",
    enrich=True,
    unacknowledged_only=True,
)
```

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

## Common agent workflows

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
    agent_func="Codex:gpt-5:planner"
)
```

### Diagnose sync state safely

```python
watercooler_sync_repair(code_path=".", diagnose_only=True)
watercooler_sync_repair(code_path=".", dry_run=True)
```
