# Daemons

Watercooler daemons are background processes that run inside the MCP server
and periodically scan your threads for signals worth surfacing. They emit
**findings** — structured observations stored in a local log — which you
can query at any time via MCP tools.

All daemons are opt-in. They observe and report; they do not modify your
threads unless that is their explicit documented purpose.

**Assistive, not sovereign.** Daemons exist to surface things a busy
team would otherwise miss — stalled threads, missing metadata, entries
that look like decisions. They do not replace accountable human
reasoning. A daemon's role is to raise a flag; deciding what to do about
it stays with you. Even the daemons that write back to threads
(currently only the Decision Extractor) are constrained to entries that
link back to a validated source entry — they cannot silently rewrite
existing state.

## Concepts

**Daemon** — A named background worker that wakes on a configurable
interval, scans data, and appends findings to its log.

**Finding** — A structured record with a category, severity, and payload
describing something the daemon noticed. Findings accumulate and can be
acknowledged once acted on.

**Tick** — One execution of a daemon's scan loop. Between ticks the daemon
sleeps.

**Findings log** — A per-daemon JSONL file stored under
`~/.watercooler/daemons/<daemon_name>/`.

## Open-core daemons

The following five daemons ship in the open-source build:

| Daemon | Key | Default interval | Writes to threads? | Requires LLM? |
|---|---|---|---|---|
| Sync Guard | `sync_guard` | 3 min | No | No |
| Thread Auditor | `thread_auditor` | 5 min | No | No |
| Decision Detector | `decision_detector` | 5 min | No | No |
| Decision Extractor | `decision_extractor` | 30 min | Yes | **Yes** |
| Decision Stance | `decision_stance` | 10 min | No | No |

Additional daemons (content scouting, project pulse, cross-thread
coordination, T2 indexing) ship only in premium and hosted deployments.
Configuring them in an open-source install has no effect — the daemon
entries are absent from the registry.

## Enabling daemons

The master switch and per-daemon settings live under `[mcp.daemons]` in
`~/.watercooler/config.toml` (user-level) or `.watercooler/config.toml`
(project-level).

```toml
[mcp.daemons]
enabled = true          # master switch; daemons do nothing when false

[mcp.daemons.sync_guard]
enabled = true

[mcp.daemons.thread_auditor]
enabled = true

[mcp.daemons.decision_detector]
enabled = true
```

The Decision Extractor requires a configured LLM endpoint before it will
run. See [LLM configuration](#llm-configuration) below. The other three
daemons have no external dependencies.

`[mcp.daemons.enabled]` must be `true` for any daemon to run. Individual
daemons must also have `enabled = true` under their own key.

Sync Guard defaults to `enabled = true`; all others default to `false`.

## Querying daemons via MCP tools

### `watercooler_daemon_status`

Check daemon health and last-run metadata.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `daemon` | string | no | Daemon key (e.g. `"thread_auditor"`). Omit for all daemons. |
| `trigger` | bool | no | If `true`, wake the daemon for an immediate async tick. |
| `code_path` | string | no | Repository root. |

### `watercooler_daemon_findings`

Query findings from one or all daemons.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `daemon` | string | no | Daemon key. Omit to query all daemons. |
| `category` | string | no | Filter by finding category (e.g. `"missing_status"`). |
| `severity` | string | no | Filter by severity: `"info"`, `"warning"`, or `"error"`. |
| `topic` | string | no | Filter by thread topic. |
| `limit` | int | no | Max findings to return (default 50). |
| `unacknowledged_only` | bool | no | Return only unacknowledged findings. |
| `code_path` | string | no | Repository root. |

---

## Sync Guard (`sync_guard`)

Keeps your local threads worktree in sync with the remote. On each tick it
checks parity between the local worktree and the remote branch and
auto-heals bounded divergence states (e.g. local behind remote, or dirty
state that can be cleanly rebased). States that require manual intervention
are emitted as warnings.

**Default behaviour:** enabled at startup.

### Configuration

```toml
[mcp.daemons.sync_guard]
enabled = true
interval = 180.0    # seconds between checks (minimum 30)
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable this daemon. |
| `interval` | float | `180.0` | Seconds between parity checks (min 30). |

### Finding categories

| Category | Severity | Meaning |
|---|---|---|
| `sync_guard_healed` | info | A parity issue was detected and auto-resolved. |
| `sync_guard_warning` | warning | Parity issue detected; manual intervention needed. |

If you see a `sync_guard_warning` finding, run
`watercooler_sync_repair(code_path=".")` and check the result.

---

## Thread Auditor (`thread_auditor`)

Scans thread and entry metadata for hygiene issues: missing header fields,
entries without IDs, stale open threads, and classification suggestions.
The auditor never modifies thread files.

### Configuration

```toml
[mcp.daemons.thread_auditor]
enabled = true
interval = 300.0            # seconds between scans (minimum 10)
stale_days = 14             # threads idle longer than this are flagged
max_findings_per_run = 200  # cap findings emitted in one tick
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable this daemon. |
| `interval` | float | `300.0` | Seconds between scans (min 10). |
| `check_missing_status` | bool | `true` | Flag threads with no `Status:` header. |
| `check_missing_ball` | bool | `true` | Flag threads with no `Ball:` header. |
| `check_missing_entry_ids` | bool | `true` | Flag entries with no Entry-ID. |
| `check_missing_summaries` | bool | `true` | Flag graph nodes with no summary. |
| `check_stale_threads` | bool | `true` | Flag threads with no recent activity. |
| `stale_days` | int | `14` | Days of inactivity before a thread is stale (min 1). |
| `check_classification` | bool | `true` | Suggest topic reclassification. |
| `max_findings_per_run` | int | `200` | Max findings emitted per tick (min 1). |

### Finding categories

| Category | Severity | Meaning |
|---|---|---|
| `missing_status` | info | Thread has no `Status:` header line. |
| `missing_ball` | info | Thread has no `Ball:` header line. |
| `missing_entry_id` | info | An entry has no Entry-ID comment. |
| `missing_thread_summary` | info | Thread node in graph has no summary. |
| `missing_entry_summary` | info | Entry node in graph has no summary. |
| `stale_thread` | info | Thread has had no activity in `stale_days` days. |
| `classification_suggestion` | info | Thread may belong in a different directory. |

---

## Decision Detector (`decision_detector`)

Scans all entries in the baseline graph using deterministic NLP scoring
and flags entries that look like they record a decision. It produces
`decision_candidate` findings for human review or for consumption by the
Decision Extractor daemon.

The detector never writes to threads. It is safe to leave running
continuously.

### Configuration

```toml
[mcp.daemons.decision_detector]
enabled = true
interval = 300.0            # seconds between scans (minimum 10)
min_score = 2               # entries scoring below this are ignored (1–10)
max_findings_per_run = 200
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable this daemon. |
| `interval` | float | `300.0` | Seconds between scans (min 10). |
| `min_score` | int | `2` | Minimum NLP score to emit a finding (1–10). Higher = stricter. |
| `max_findings_per_run` | int | `200` | Max findings emitted per tick (min 1). |
| `fuzzy_threshold` | int | `85` | Fuzzy-match threshold for deduplicating near-identical entries (0–100). |
| `scan_closed_threads` | bool | `true` | Include entries from closed threads. |
| `exclude_agents` | list | `["ExtractDecisionsDaemon"]` | Skip entries written by these agents (prevents re-detecting extracted decisions). |

### Finding categories

| Category | Severity | Meaning |
|---|---|---|
| `decision_candidate` | info | Entry scored above `min_score`; may record a decision. |

Each finding includes the entry ID, thread topic, score, and the
matched signal phrases, so you can review it before taking action.

---

## Decision Extractor (`decision_extractor`)

Consumes `decision_candidate` findings from the Decision Detector and
uses an LLM to validate and structure them. For each candidate that passes
an 8-gate validity checklist, the extractor writes a new `Decision` entry
back to the originating thread via the daemon write path.

**This daemon writes to threads.** Enable it deliberately.

It has a configurable daily rate limit and per-candidate attempt cap to
keep LLM costs bounded.

### Why decisions are extracted, not invented

The 8-gate validity checklist is the most important part of this daemon.
Its purpose is to resist a specific failure mode: **retroactive authority
laundering** — promoting a summary, a hope, or a half-formed plan into
something that reads like a committed decision when it never was.

The guiding question behind every gate is:

> Would the original author recognise this as their decision?

If the answer is anything other than yes, the candidate is rejected. The
gates enforce that test mechanically: verbatim quotes from the source
entry must exist, an explicit choice (not a discussion) must be
identifiable, the extraction must stay within the scope actually
expressed, and the output must link back to a specific `entry_id` so any
future reader can check the extraction against the source.

This is why rejection is common and expected. A high rejection rate
means the gate is working — the daemon is refusing to invent decisions
that were not made. A `Decision` entry that makes it through the gates
is a claim the system is willing to stand behind, because it can be
traced back to recognisable words from a real author at a real moment.

### Requires

- Decision Detector must be enabled — the extractor reads its findings
  as a queue.
- An LLM endpoint configured in `[mcp.daemons.llm]` or
  `[mcp.daemons.decision_extractor.llm]`.

### Configuration

```toml
[mcp.daemons.decision_extractor]
enabled = true
interval = 1800.0              # seconds between ticks (minimum 60)
min_extraction_score = 4       # only process candidates scoring ≥ this (1–10)
max_candidates_per_tick = 3    # max candidates processed per tick (1–20)
max_extractions_per_day = 20   # daily cap on successful extractions (1–100)
min_confidence = 3             # LLM confidence threshold 1–5; skip if below
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable this daemon. |
| `interval` | float | `1800.0` | Seconds between ticks (min 60). |
| `min_extraction_score` | int | `4` | Only process candidates at or above this NLP score (1–10). |
| `max_candidates_per_tick` | int | `3` | Candidates processed per tick (1–20). |
| `max_extractions_per_day` | int | `20` | Daily cap on successful extractions (1–100). |
| `max_body_chars` | int | `4000` | Max entry body characters sent to the LLM (500–32000). |
| `min_confidence` | int | `3` | Reject extracted decisions below this LLM confidence score (1–5). |
| `max_tick_duration` | float | `300.0` | Wall-clock budget per tick in seconds (min 30). |
| `max_extraction_attempts` | int | `3` | Max LLM attempts per candidate before giving up (1–10). |
| `max_write_failure_attempts` | int | `5` | Max write retries per extracted decision (1–20). |

### Finding categories

| Category | Severity | Meaning |
|---|---|---|
| `extraction_success` | info | Decision entry written to thread. |
| `extraction_rejected` | info | Candidate failed the validity checklist. |
| `extraction_rate_limited` | info | Daily extraction cap reached; processing paused until tomorrow. |
| `extraction_cap_reached` | info | Per-candidate attempt cap reached; candidate skipped. |
| `extraction_failed` | warning | LLM extraction failed. |
| `extraction_parse_failure` | warning | LLM response could not be parsed. |
| `extraction_push_failed` | warning | Extraction succeeded but writing the entry failed. |

### LLM configuration

The Decision Extractor requires an LLM. Configure it either globally for
all daemons or specifically for this one:

```toml
# Shared config for all LLM-using daemons
[mcp.daemons.llm]
api_base = "http://localhost:8000/v1"   # default port of the auto-started llama-server (llama.cpp). Any OpenAI-compat endpoint works — Ollama's default 11434, a hosted endpoint, etc.
model = "qwen3:1.7b"                    # matches DEFAULT_LLM_GGUF_MODEL in src/watercooler/models.py
api_key = "local"
timeout = 60.0
max_tokens = 2048

# Or override for just this daemon
[mcp.daemons.decision_extractor.llm]
model = "qwen3:4b"
```

| Key | Type | Description |
|---|---|---|
| `api_base` | string | LLM API base URL (OpenAI-compatible). |
| `model` | string | Model identifier. |
| `api_key` | string | API key. Use `"local"` (or any non-empty placeholder) for unauthenticated local endpoints. For real providers, prefer `~/.watercooler/credentials.toml` (e.g. `[openai] api_key = "..."`) or environment variables over putting the key in `config.toml`. The schema marks this field with `exclude=True` so it is stripped from serialized config dumps, but the TOML file itself is plain-text. |
| `timeout` | float | Request timeout in seconds (1–600). |
| `max_tokens` | int | Max response tokens (1–32768). |

---

## Decision Stance (`decision_stance`)

Reads `decision_candidate` and extraction findings emitted by the
Decision Detector and Decision Extractor, and converts them into
per-role **stance advisories** — short, signed recommendations about
how a given role should hold the line in the current context (e.g.,
"the planner should slow down: too many high-tier decisions are
piling up unextracted").

The daemon never writes to threads. It runs only on open-core
installs; on premium and hosted builds the `project_coordinator`
daemon supersedes it and `decision_stance` is silently skipped.

### Requires

- Decision Detector enabled — its `decision_candidate` findings are
  the primary input.
- Decision Extractor enabled — its extraction outcome findings
  (`extraction_success`, `extraction_rejected`,
  `extraction_rate_limited`) round out the signal set.
- **No LLM.** All stance modulation is rule-based.

### Configuration

```toml
[mcp.daemons.decision_stance]
enabled = true
interval = 600.0          # seconds between ticks (minimum 60)
window_seconds = 86400.0  # rolling window for input findings (24 h)
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable this daemon. |
| `interval` | float | `600.0` | Seconds between ticks (min 60). |
| `window_seconds` | float | `86400.0` | How far back the daemon looks at decision-pipeline findings each tick (min 600). |

### Roles covered

Stance advisories are a **fixed protocol feature** emitted for three
canonical stance roles: **`planner`**, **`critic`**, and
**`tester`**. These map to durable collaboration modes —
direction-setting, challenge/review, and validation/evidence — and
are the intentional stance surface. They are not a transitional set.

Custom roles defined in `.watercooler/roles.toml` are first-class
vocabulary for entry authoring and write-time validation, but they
do **not** participate in stance modulation, even when
`canonical_role` is set. A custom `security-audit` role can exist
alongside `critic`, but it will not produce a separate
`stance:security-audit` advisory. This keeps daemon output
predictable (stable `stance:planner`, `stance:critic`,
`stance:tester` topics) and avoids duplicate or conflicting findings
between a custom role and the canonical it maps to.

### Finding categories

| Category | Severity | Meaning |
|---|---|---|
| `stance_advisory` | info | Level-1 advisory (a soft nudge for the named role). |
| `stance_advisory` | warning | Level-2 advisory (a stronger pull for the named role). |

Topics are namespaced as `stance:{role}` — e.g., `stance:planner`,
`stance:critic`. A "cleared" tombstone advisory is emitted once when
a role drops back to Level 0, so consumers can tell the difference
between "no advisory" and "previous advisory has lapsed".

### Artifact trail (`source_lead_ids`)

Every Level-1+ advisory cites the decision-pipeline findings that
triggered it. The citation lives at
`details.advisory.source_lead_ids` — a list of up to 10 finding IDs
drawn from `decision_detector` (high-tier candidates only) and
`decision_extractor` (success / rejection / rate-limit categories,
matched to the signals that fired).

If the pool of qualifying findings exceeds the 10-ID cap, the daemon
sets `details.source_lead_ids_truncated = true` so consumers know
the cited list is partial rather than complete.

To trace from a stance advisory back to the source entries:

1. Pull the advisory:
   ```
   watercooler_daemon_findings(daemon="decision_stance",
                               category="stance_advisory")
   ```
2. Read `details.advisory.source_lead_ids`.
3. For each ID, query its source daemon and match `finding_id` in
   the response (the same pattern works against
   `decision_extractor`):
   ```
   watercooler_daemon_findings(daemon="decision_detector",
                               category="decision_candidate",
                               limit=500)
   ```
4. Each detector/extractor finding carries `topic` + `entry_id`,
   which `watercooler_get_thread_entry` can resolve to the source
   entry.

### Schema version

`details.advisory.schema_version` is `1`. Future protocol-level
evolutions — including hand-authored Decision counting — are tracked
in issue #681; expanding the stance role set is **not** part of that
roadmap.

---

## Acknowledging findings

Findings accumulate until acknowledged. Use `watercooler_acknowledge_finding`
to mark one as seen:

```
watercooler_acknowledge_finding(
    daemon_name="decision_detector",
    finding_id="<finding_id>"
)
```

The `finding_id` comes from the `id` field on each finding returned by
`watercooler_daemon_findings`.

To list only unacknowledged findings:

```
watercooler_daemon_findings(
    daemon="decision_detector",
    unacknowledged_only=True,
    code_path="."
)
```

> **Note:** `watercooler_acknowledge_finding` is distinct from
> `watercooler_ack`. The latter is a thread-entry tool that takes a `topic`
> and writes an `Ack` entry to the thread without flipping the ball; it does
> not touch daemon findings.

---

## Troubleshooting

### Daemon shows `disabled` in status

Check that both the master switch and the individual daemon are enabled:

```toml
[mcp.daemons]
enabled = true

[mcp.daemons.thread_auditor]
enabled = true
```

Then restart the MCP server.

### No findings after enabling a daemon

The daemon may not have completed its first tick yet. Check the last-run
time with `watercooler_daemon_status(daemon="thread_auditor", code_path=".")`.
To force an immediate run, set `trigger=True`:

```
watercooler_daemon_status(daemon="thread_auditor", trigger=True, code_path=".")
```

### Decision Extractor not writing decisions

- Confirm the Decision Detector is enabled and has produced
  `decision_candidate` findings.
- Confirm `min_extraction_score` is not set higher than the scores the
  detector is emitting.
- Check `extraction_rate_limited` findings — the daily cap may have been
  reached.
- Check that an LLM is configured under `[mcp.daemons.llm]` and reachable.

### LLM connection errors

Test the LLM endpoint independently (substitute port for your configured
endpoint — `8000` is the default for the auto-started `llama-server`):

```bash
curl http://localhost:8000/v1/models
```

If the endpoint requires an API key, ensure `api_key` is set correctly in
the config (not as a plain `"local"` string for authenticated endpoints).

---

## Related documentation

- [Configuration](CONFIGURATION.md) — full config reference
- [Tools reference](TOOLS-REFERENCE.md) — `watercooler_daemon_status`, `watercooler_daemon_findings`
- [Troubleshooting](TROUBLESHOOTING.md) — general MCP and sync issues
