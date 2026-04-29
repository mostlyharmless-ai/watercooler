# Federation

Federation lets a single Watercooler MCP server search across multiple
repositories in one call. Ask a question once; get ranked results from
your primary repo and any configured secondary repos side-by-side.

## When to use federation

Federation is useful when your project spans multiple repositories that
share context — for example, a backend API repo and a frontend app repo.
Instead of switching repos and re-running searches, `watercooler_federated_search`
fans the query out to every configured namespace and merges the results.

Federation is **read-only** and **keyword-based** (Phase 1). It surfaces
awareness of neighbouring repos; it does not merge their data.

## Exposure, not integration

Federation is designed around a single invariant: it is **epistemic
exposure, not epistemic integration**. Each namespace keeps its own
orphan branch, its own baseline graph, and its own authority over its
threads. A federated search surfaces what other namespaces are thinking;
it never rewrites one namespace's state from another, never promotes a
remote entry to local authority, and never reconciles contradictions
into a unified worldview.

This matters when two repos disagree. Federation is built to preserve
that disagreement — a conflicting result from a secondary namespace is
reported alongside the primary's answer, not averaged against it. If you
want a single consolidated record, that is a different tool; federation
deliberately is not it.

## Namespaces explained

Every repo Watercooler touches is identified by a **namespace ID**. There
are two kinds:

**Primary namespace** — the repo you pass as `code_path` in the tool call.
Its namespace ID is derived automatically from the checkout directory's
basename. There is nothing to configure. If your repo lives at
`/home/alice/projects/api-backend`, the primary namespace ID is
`api-backend`.

**Secondary namespaces** — other repos you want to include in federated
searches. Each one is registered under `[federation.namespaces.<label>]`
in your config. The `<label>` is a short name you choose freely; it has
no required relationship to the repo's directory name or URL. It is used
as an identifier in tool responses, access control rules, and log output.

The worktree for a secondary is located at
`~/.watercooler/worktrees/<repo-basename>/`, where `<repo-basename>` is
the last component of the `code_path` you supply. This is separate from
the label.

Example showing the distinction:

```toml
# Label is "frontend" — you chose this name.
# Repo basename is "ui-app" — comes from the directory.
# These are independent; the label does not need to match the directory name.
[federation.namespaces.frontend]
code_path = "/home/alice/projects/ui-app"
```

## Quick start

### Step 1 — Enable federation in config

Add a `[federation]` block to `~/.watercooler/config.toml`. Register each
secondary repo as its own namespace with a label of your choice:

```toml
[federation]
enabled = true

[federation.namespaces.frontend]
code_path = "/home/alice/projects/ui-app"
```

### Step 2 — Bootstrap the secondary namespace

On the first call the MCP server checks that the secondary repo has an
initialised watercooler worktree. If it has not been used before, run
`watercooler_health` for that repo to set it up:

```
watercooler_health(code_path="/home/alice/projects/ui-app")
```

### Step 3 — Search across namespaces

```
watercooler_federated_search(
    query="authentication design",
    code_path="/home/alice/projects/api-backend"
)
```

The `api-backend` repo becomes the primary namespace. Watercooler searches
it and every configured secondary in parallel and returns merged results
ranked by relevance, weighted toward the primary and toward recent entries.

## Configuration reference

All federation settings live under `[federation]` in
`~/.watercooler/config.toml` (user-level) or `.watercooler/config.toml`
(project-level).

### Top-level keys

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Feature gate. Must be `true` to use federated search. |
| `namespace_timeout` | float | `0.4` | Per-namespace search budget in seconds (max 30). |
| `max_namespaces` | int | `5` | Max secondary namespaces queried per call (max 20). |
| `max_total_timeout` | float | `2.0` | Total wall-clock budget for all searches (max 60). |

`namespace_timeout` must be ≤ `max_total_timeout`. Validation fails at
startup if this constraint is violated.

### Namespace definitions

Each secondary namespace gets its own subsection under
`[federation.namespaces.<label>]`. The label is a short alphanumeric key
(hyphens and underscores allowed).

```toml
[federation.namespaces.frontend]
code_path = "/home/alice/projects/ui-app"
deny_topics = ["internal-hiring"]
```

| Key | Type | Required | Description |
|---|---|---|---|
| `code_path` | string | yes | Absolute path to the secondary repository root. |
| `deny_topics` | list of strings | no | Thread topics to exclude from federated results (case-insensitive). |

No two secondary namespaces may point to repos with the same directory
basename, because both would map to the same worktree path. Config
validation will report a clear error if this happens.

### Access control

By default all configured secondaries are queryable from any primary.
Use `[federation.access]` allowlists to restrict this.

Allowlist keys are primary namespace IDs — which are the **checkout
directory basenames**, not configured labels. If your primary repo is
checked out at `/home/alice/projects/api-backend`, use `"api-backend"` as
the key. Allowlist values are the secondary namespace labels you defined
under `[federation.namespaces]`.

```toml
[federation.access]
# "api-backend" is the primary's directory basename (auto-derived).
# "frontend" is the secondary's label (configured above).
allowlists = { "api-backend" = ["frontend"] }
```

Any secondary not listed in a primary's allowlist returns
`status: "access_denied"` rather than results.

### Scoring parameters

```toml
[federation.scoring]
local_weight = 1.0          # Weight applied to primary namespace results
wide_weight = 0.55          # Weight applied to secondary namespace results
recency_floor = 0.7         # Minimum recency multiplier (for old entries)
recency_half_life_days = 60 # Age at which recency decay reaches its midpoint
```

The final ranking score for each result is:

```
score = normalize(base_score) × namespace_weight × recency_decay
```

Where `normalize` maps raw keyword scores from [1.0, 2.4] into [0.0, 1.0].
Entries from the primary namespace receive `local_weight`; entries from
secondaries receive `wide_weight`.

### Full example config

```toml
[federation]
enabled = true
namespace_timeout = 0.4
max_namespaces = 5
max_total_timeout = 2.0

# Label "frontend" is arbitrary; repo directory is "ui-app".
[federation.namespaces.frontend]
code_path = "/home/alice/projects/ui-app"
deny_topics = ["internal-hiring", "employee-reviews"]

[federation.access]
# Primary ID "api-backend" comes from the checkout directory basename.
# Secondary ID "frontend" is the label defined above.
allowlists = { "api-backend" = ["frontend"] }

[federation.scoring]
local_weight = 1.0
wide_weight = 0.55
recency_floor = 0.7
recency_half_life_days = 60
```

## `watercooler_federated_search` tool

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | Search text. Max 500 characters. |
| `code_path` | string | no | Primary repository root. Its directory basename becomes the primary namespace ID. |
| `namespaces` | string | no | Comma-separated secondary namespace labels to query. Leave empty to query all configured secondaries. |
| `limit` | integer | no | Max results to return. Range 1–100, default 10. |

### Response structure

```json
{
  "schema_version": 1,
  "primary_namespace": "api-backend",
  "queried_namespaces": ["api-backend", "frontend"],
  "namespace_status": {
    "api-backend": { "status": "ok" },
    "frontend": { "status": "ok" }
  },
  "result_count": 3,
  "total_candidates_before_truncation": 8,
  "results_complete": true,
  "warnings": [],
  "results": [
    {
      "entry_id": "01JKLM...",
      "origin_namespace": "api-backend",
      "ranking_score": 0.87,
      "score_breakdown": {
        "raw_score": 1.9,
        "normalized_score": 0.64,
        "namespace_weight": 1.0,
        "recency_decay": 0.95
      },
      "entry_data": {
        "topic": "auth-protocol",
        "title": "Use OAuth2 with PKCE",
        "entry_id": "01JKLM...",
        "role": "implementer",
        "agent": "Claude",
        "entry_type": "Decision",
        "summary": "...",
        "timestamp": "2026-02-15T10:30:00Z"
      }
    }
  ]
}
```

`results_complete` is `false` when the result set was truncated by `limit`.
`total_candidates_before_truncation` tells you how many matched before
the cut.

### Tool errors

| Error code | Meaning |
|---|---|
| `EMPTY_QUERY` | Query is empty or whitespace-only. |
| `VALIDATION_ERROR` | Query exceeds 500 characters, or a namespace label is malformed. |
| `FEDERATION_DISABLED` | `federation.enabled` is not `true` in config. |
| `FEDERATION_NOT_AVAILABLE` | Hosted mode — federation is not available in this environment. |
| `TOO_MANY_NAMESPACES` | The `namespaces` override lists more than `max_namespaces` secondaries. |
| `PRIMARY_SEARCH_FAILED` | The primary namespace search failed. No partial results are returned. |
| `INTERNAL_ERROR` | Unexpected exception. Check MCP server logs. |

## Namespace status codes

Each namespace in the response has a `status` field:

| Status | Meaning | Action |
|---|---|---|
| `ok` | Search succeeded. | — |
| `timeout` | Per-namespace search exceeded `namespace_timeout`. | Raise `namespace_timeout` or reduce query scope. |
| `error` | Search failed with an exception. | Check MCP server logs for details. |
| `not_initialized` | No worktree found for this namespace. | Run `watercooler_health(code_path=...)` for the secondary repo. |
| `security_rejected` | Worktree path is a symlink or escapes the worktree base directory. | Fix the worktree path; symlinks are not supported. |
| `access_denied` | Allowlist blocked this namespace. | Add the namespace label to the allowlist in `[federation.access]`. |

Partial failures are not fatal. If a secondary namespace times out or
errors, its `status` reflects that and the call still returns results
from all other namespaces that succeeded.

## Troubleshooting

### "FEDERATION_DISABLED"

Set `enabled = true` under `[federation]` in your config:

```toml
[federation]
enabled = true
```

Run `watercooler config validate` to confirm the setting is picked up.

### Secondary namespace shows `not_initialized`

The secondary repo has not had its watercooler worktree created yet.
Run `watercooler_health` once for that repo:

```
watercooler_health(code_path="/path/to/secondary-repo")
```

### Secondary namespace shows `timeout`

The default `namespace_timeout` (0.4 s) may be too tight for large repos.
Increase it:

```toml
[federation]
namespace_timeout = 1.0
max_total_timeout = 3.0
```

### Results missing from a secondary namespace

Check `deny_topics` — if the thread topic you expect is in the deny list,
it will be silently filtered. Also confirm the namespace label appears in
`queried_namespaces` in the response.

### Allowlist not matching

Allowlist keys must match the primary's **checkout directory basename**,
not a configured label. If your primary repo is at
`/home/alice/projects/api-backend`, the key is `"api-backend"`. Check
`primary_namespace` in the tool response to see the exact ID being used.

### Namespace collision error

If two secondary repos have the same directory basename (e.g., both
checked out in a folder called `api`), they would share a worktree path.
Config validation will report this. Check out one of the repos under a
different directory name.

## Limitations

Phase 1 federation has the following known constraints:

- **Baseline graph only**: federation covers the baseline graph. Federated
  search over richer memory backends is part of the hosted roadmap.
- **Local namespaces only**: all secondary namespaces must be on the same
  machine as the MCP server.
- **No thread cross-references**: explicit `Ref: namespace/topic` links are
  not yet parsed or scored differently. This is planned for Phase 2.
- **Uniform secondary weight**: all secondaries use the same `wide_weight`.
  Reference-aware boosting is planned for Phase 2.

## Related documentation

- [Configuration](CONFIGURATION.md) — full config reference
- [Tools reference](TOOLS-REFERENCE.md) — `watercooler_federated_search` entry
- [Troubleshooting](TROUBLESHOOTING.md) — general MCP and sync issues
