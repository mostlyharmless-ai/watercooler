# Memory-tier migration: stdio ↔ hybrid

`watercooler migrate` moves your accumulated T1 (entry embeddings) and
T2 (Graphiti episodes/entities) between **local** (`transport = stdio`)
and **hosted** (`transport = hybrid`) FalkorDB storage.

Two customer use cases drive this:

1. **Stdio → hybrid**: you're upgrading from local-only operation to the
   hosted multi-user product and want your existing memory to come along
   without re-indexing every thread by hand.
2. **Hybrid → stdio**: you're going off-network (airgap, vendor exit, or
   simply running a local-only fork) and want to keep what's been built
   on the hosted side.

Both directions are **idempotent** (MERGE on entry_id at the FalkorDB
layer) and **resumable** (per-direction checkpoint files in
`~/.watercooler/migration/`). Running the same command twice does no
harm; running after a Ctrl-C picks up where it left off.

## Quick start

### T1 (entry embeddings)

```bash
# Dry-run first — see how much you'd push, no writes
watercooler migrate t1 --to hybrid --dry-run

# Real push to hosted
watercooler migrate t1 --to hybrid

# Pull hosted T1 down into a local FalkorDB (started locally on :6379)
watercooler migrate t1 --to stdio
```

### T2 (Graphiti episodes/entities)

```bash
# Trigger the hosted bulk_index pipeline against the orphan-branch corpus
watercooler migrate t2 --to hybrid

# Pull hosted T2 down into a local instance
# (NOT YET IMPLEMENTED — see "Limitations" below)
watercooler migrate t2 --to stdio
```

## What gets moved

### T1 (per entry)

- `entry_id`, `thread_topic`, `group_id`
- 1024-dim bge-m3 embedding vector
- Metadata: `role`, `entry_type`, `agent`, `timestamp`

The migration uses **two complementary sources** for stdio→hybrid:

1. The orphan-branch worktree (`~/.watercooler/worktrees/<repo>/`) is the
   authoritative entry list — every entry that ever made it into a
   thread is in there with full metadata.
2. The local FalkorDB (Docker volume or native install at `:6379`) is
   the **embedding cache** — entries that have already been embedded by
   a prior local-mode session land here.

For each orphan-branch entry:
- **Cache hit** (local FalkorDB has the embedding): push as-is. Free.
- **Cache miss** (no cached embedding): generate fresh via the
  configured embedding API (typically OpenRouter `baai/bge-m3`), then
  push. ~$0.000007 per entry.

### T2 (per entry)

- `stdio→hybrid` triggers the existing hosted `watercooler_bulk_index`
  tool. The hosted side reads the orphan branch via the GitHub API and
  enqueues each entry through the same async pipeline that T2 daemons
  use. Deterministic chunk IDs make this idempotent.
- `hybrid→stdio` is currently **not implemented**; see "Limitations".

## CLI reference

```
watercooler migrate {t1,t2} --to {hybrid,stdio} [options]

Common options:
  --dry-run                  Count what would happen, no writes.
  --limit N                  Cap entries processed (0 = no cap).
  --code-path PATH           Repo root for canonical name resolution
                             (default: cwd).
  --target-group-id ID       Override canonical group_id. The hosted
                             FalkorDB database name is server-derived
                             from group_id (`<group_id>_t1` /
                             `_t2`); not separately overridable.
  --checkpoint PATH          Path to resume checkpoint file.

T1-specific options:
  --local-host HOST          Local FalkorDB host (default: localhost).
  --local-port PORT          Local FalkorDB port (default: 6379).
  --local-graph-name NAME    Override local FalkorDB graph name
                             (default: derived from --target-group-id).
                             Use `--local-graph-name watercooler_cloud`
                             when migrating from a legacy pre-Plan-v20
                             local volume that uses the old hardcoded
                             "watercooler_cloud" graph name.

T2-specific options:
  --threads CSV              Comma-separated thread topics (default: all).
```

## Authentication

The migration tool reuses your existing Claude Code / hybrid-mode
credentials — no separate setup. Specifically:

- `[mcp].url` from `~/.watercooler/config.toml` (or `WATERCOOLER_MCP_URL`
  env var) → hosted endpoint
- `~/.watercooler/credentials.toml` → API token
- `WATERCOOLER_CODE_REPO` / `WATERCOOLER_CODE_BRANCH` → context headers

If you're not already configured for hybrid mode, the tool errors with
a clear message pointing at `docs/MCP-CLIENTS.md` for setup.

## `--limit` semantics

`--limit N` caps **`total_scanned`** — i.e. it's a hard cap on iteration
count, not a target for successful upserts. Both `to_hybrid` and
`to_stdio` apply this consistently: an entry that fails dim validation
or upsert still counts against the limit.

Why: predictable iteration cost. `--limit 100` always processes at most
100 entries; the run won't keep going trying to find 100 successes if
the data is bad.

To compute "successful pushes" from a run, read `pushed` (and
`errored`) from the JSON summary — they always sum to `total_scanned`
minus the dry-run-skipped entries.

`skipped_already_present` (entries the checkpoint already contains) is
**not** counted against `--limit`. So `--limit 50` on a resumed run
means "process 50 more entries beyond what's already checkpointed."

## Resumability

Each direction has its own checkpoint:
- `~/.watercooler/migration/t1_to_hybrid_cursor.jsonl`
- `~/.watercooler/migration/t1_to_stdio_cursor.jsonl`

The checkpoint is a plain append-only JSONL of completed `entry_id`s.
On re-run, entries listed there are skipped. To force a fresh run:

```bash
rm ~/.watercooler/migration/t1_to_hybrid_cursor.jsonl
```

## Idempotency

Both directions use **MERGE on `entry_id`** at the FalkorDB layer:
- Same `entry_id` → in-place update (no duplicate node)
- Different `entry_id` → new node
- Last write wins on the embedding values

Two collaborators (e.g. you and a teammate) running the same migration
to the same target are safe — the merge is at the database level. The
only divergence is whose embedding wins for entries both have cached.
For semantic search this is cosmetic — embeddings are approximations.

## Concurrency

The MCP API serialises writes per-FalkorDB-connection. The migration
runs single-threaded by default (no parallel push). If you want to
speed up large migrations, run multiple `watercooler migrate` processes
in parallel against disjoint `--limit`/checkpoint windows.

## Limitations

### T2 hybrid→stdio is not yet implemented

A proper transport would need:
1. A new server-side `watercooler_t2_dump` tool that paginates Episodic
   + Entity nodes plus their MENTIONS / RELATES_TO edges (with
   embeddings + temporal metadata) preserving UUIDs.
2. A local writer that reconstructs the graph faithfully — including
   Graphiti's internal chunk-mapping state.

The canonical workaround today: rebuild T2 locally from git+T1 via the
local-mode runbook in `docs/OPS_T2_REBUILD.md`. That uses
`watercooler_bulk_index` against a local-mode MCP, which extracts a
fresh T2 from your existing entries — drop-and-rebuild semantics rather
than transport.

A `t2_dump`/restore implementation is tracked as a follow-on for the
v0.5.x cycle.

### Cache misses incur embedding-API cost

Entries on the orphan branch but not in your local FalkorDB cache are
re-embedded on push via the configured `EMBEDDING_API_BASE` /
`EMBEDDING_MODEL`. With OpenRouter `baai/bge-m3` this is ~$0.000007 per
entry — negligible for typical repos. Local llama-server (free) is also
supported.

### Wire-protocol overhead

T1 stdio→hybrid issues one MCP roundtrip per entry. For a 3,000-entry
repo, expect ~10-30 minutes wall time (network-latency dominated).
Future work could batch upserts; for now the per-entry path keeps the
hosted MCP API simple and the cross-tenant guard granular.

## Verification

After running, sanity-check the result:

```bash
# T1 to hybrid: hosted T1 should now be populated
watercooler migrate t1 --to hybrid --dry-run    # should report 0 pushed

# Cross-check: seeded similarity against an entry that previously errored
mcp-cli call watercooler_search \
  --arg seed_entry_id=01YOUR_ENTRY_ID --arg limit=5
# Expect: list of similar entries, no "no_embedding" error

# T1 to stdio: count the local Entry nodes
redis-cli GRAPH.QUERY <local_graph> "MATCH (n:Entry) RETURN count(n)"
```

## Operator runbook — stdio → hybrid (the customer-graduation flow)

This runbook walks an operator (the human running the migration on
their laptop) through the typical "I have a working stdio repo and want
to bring its memory along when I switch to hybrid" flow. It mirrors the
sequence used to backfill production T1 on 2026-04-27 (3033 entries,
0 errors, ~85 min wall-clock) — the lessons are baked in.

### 1. Prerequisites

Before running the migration:

- **Hybrid-mode credentials configured.** Your `~/.watercooler/config.toml`
  should have `[mcp].url` pointing at the hosted endpoint (e.g.,
  `https://watercooler-cloud-production.up.railway.app/mcp/premium/`)
  and `transport = "hybrid"`. Your `~/.watercooler/credentials.toml`
  should have a valid `[hosted].api_key`. If you've been using stdio
  mode and your config has a commented-out hosted block, just toggle
  the comments — see `docs/MCP-CLIENTS.md` for the canonical layout.
- **Restart your MCP client** (Claude Code, Cursor, etc.) after
  flipping config so it picks up the new endpoint. The migration tool
  uses the same config the MCP client uses.
- **Take a backup of the hosted FalkorDB.** The migration is
  idempotent (MERGE on `entry_id`), so a recovery point isn't strictly
  required — but for production workloads it's cheap insurance. The
  fastest no-S3 path:
  ```bash
  TS=$(date -u +%Y%m%d_%H%M%SZ)
  mkdir -p ~/.watercooler/backups
  railway ssh "redis-cli BGSAVE"
  railway ssh "redis-cli LASTSAVE"   # confirm timestamp advances
  railway ssh "cat /var/lib/falkordb/data/dump.rdb" \
    > ~/.watercooler/backups/prod_${TS}.rdb
  ```
  (Link `railway` to the hosted FalkorDB service first via
  `railway environment <env> && railway service`.)
- **Optional but high-value: spin up a local FalkorDB with your
  cached embeddings.** If you've been running the watercooler MCP in
  stdio mode for a while, your local FalkorDB Docker volume has
  embeddings for entries you've already touched. The migration uses
  these as a free cache (saves the embedding API call per entry).
  ```bash
  docker run --rm --name falkor-temp \
    -v falkordb_data:/var/lib/falkordb/data \
    -p 16379:6379 -d falkordb/falkordb:latest
  # wait for "LOADING" → "PONG":
  until docker exec falkor-temp redis-cli PING | grep -q PONG; do sleep 2; done
  # confirm it loaded your data:
  docker exec falkor-temp redis-cli GRAPH.LIST
  ```
  Then pass `--local-port 16379` (or whatever non-default port you
  used) to the migration so it reads from this temp instance.
  Without a local cache, every entry incurs an embedding API call —
  still cheap with bge-m3 ($0.02 for 3000 entries) but slower.

### 2. Dry-run

Always dry-run first. The summary tells you exactly what the real run
will do.

```bash
watercooler migrate t1 --to hybrid --dry-run --local-port 16379
```

Expected output (JSON to stdout, logs to stderr):

```json
{
  "tier": "t1",
  "direction": "stdio_to_hybrid",
  "dry_run": true,
  "total_scanned": 3034,
  "pushed": 3034,
  "skipped_already_present": 0,
  "cache_hits": 2297,
  "api_calls": 737,
  "errored": 0,
  "elapsed_seconds": 0.93,
  "notes": ["Cached embeddings available: 2310"]
}
```

Read this carefully:
- `total_scanned`: how many entries are on your orphan branch
- `cache_hits` / `api_calls`: cost preview — `api_calls × ~$0.000007`
  is your projected embedding cost
- `pushed`: in dry-run, this is the would-push count (= `cache_hits +
  api_calls`)
- `errored: 0` is the success criterion — anything else, investigate
  before doing the real run

### 3. Ramping batches

For large migrations (>500 entries), ramp in three stages so you can
spot trouble early. Use a separate checkpoint file per migration so
prior runs don't accidentally interfere:

```bash
CKPT=~/.watercooler/migration/t1_to_hybrid_PROD_cursor.jsonl

# Stage A: confirm the write path works on a small batch
watercooler migrate t1 --to hybrid --limit 100 \
  --local-port 16379 --checkpoint $CKPT

# Verify a few of the entries from Stage A landed in hosted T1:
mcp-cli call watercooler_search \
  --arg seed_entry_id=<one_of_the_pushed_entry_ids> --arg limit=3
# Expect: 3 hits via "hosted_t1_hnsw"

# Stage B: medium batch
watercooler migrate t1 --to hybrid --limit 500 \
  --local-port 16379 --checkpoint $CKPT
# Should report skipped_already_present=100, pushed=500

# Stage C: full run (no --limit)
watercooler migrate t1 --to hybrid \
  --local-port 16379 --checkpoint $CKPT
```

Each stage prints a final JSON summary. Total wall-clock for ~3000
entries with a healthy cache: **~85 min** (the per-entry HTTP
roundtrip dominates, not the embedding generation).

### 4. Verification

After the full run completes:

```bash
# Final dry-run should report 0 pushed (everything's already there):
watercooler migrate t1 --to hybrid --dry-run \
  --local-port 16379 --checkpoint $CKPT

# Authoritative count (requires railway ssh access to FalkorDB):
railway ssh "redis-cli GRAPH.QUERY <org>_<repo>_t1 \
  'MATCH (n:Entry) RETURN count(n)'"

# Semantic-search smoke test against an old entry that previously
# returned no_embedding:
mcp-cli call watercooler_search \
  --arg seed_entry_id=<your_oldest_entry_id> --arg limit=5
```

### 5. Cleanup

```bash
# Tear down the temp local FalkorDB
docker rm -f falkor-temp

# Optional: drop the local volume after confirming the hosted side is
# good and you don't plan to flip back to stdio anytime soon.
# (Be conservative — wait 30 days of stable hosted operation first.)
docker volume rm falkordb_data
```

The checkpoint file (`$CKPT`) can stay — it's useful documentation of
what was migrated and harmless to keep.

## Troubleshooting

**"Hybrid migration requires `[mcp].url` in config.toml or
`WATERCOOLER_MCP_URL` env var."** — your config still points at stdio.
Toggle the hybrid block in `~/.watercooler/config.toml` and restart
your MCP client.

**"Local FalkorDB SDK not installed."** — your env doesn't have the
`falkordb` Python package. `uv sync` in the project root resolves it
(the dep is declared explicitly in `pyproject.toml` since v0.4.2-dev).
Without it the migration still runs, just without local-cache hits —
every entry becomes an API call.

**"Local FalkorDB unreachable."** — the migration tried to talk to
the local FalkorDB but couldn't connect. Either you didn't start the
temp container, or you used a non-default port and forgot
`--local-port`. The migration falls through to API-only mode in this
case (logs a warning, doesn't fail), so the run still succeeds — you
just pay the API cost for everything.

**The migration crashed mid-run.** — the boundary catch in
`cmd_migrate` translated the unhandled exception into a JSON summary
on stdout with `errored: 1` and a `notes` entry pointing at stderr
for the traceback. Read the traceback, fix the underlying issue, and
re-run the same command — the checkpoint will skip what was already
written.

**Counts don't match orphan-branch entries.** — known issue prior to
v0.4.2-dev: nested topics (e.g. `fix/something`) were silently
skipped by the orphan-branch scanner. Fixed in PR #<TBD>; if you're
running an older watercooler-cloud, manually push affected entries
via `watercooler_semantic` (`action="upsert"`) or upgrade.

**`railway ssh ... -- "<command>"` errors with `--: command not
found`.** — your Railway CLI version handles the `--` separator
differently. Quote the command directly: `railway ssh "<command>"`
without the `--`.

**Hosted side returned 405 / 5xx during migration.** — open a
support ticket. The migration's idempotent MERGE means re-running
after the issue is fixed is safe; the checkpoint preserves your
progress.

## Cross-references

- `src/watercooler/migration/` — implementation
- `src/watercooler_mcp/premium_client.py` — auth / transport reused
- `src/watercooler_mcp/hosted_semantic.py` — `upsert_embedding`,
  `list_embeddings_t1` (the server-side primitives)
- `docs/OPS_T2_REBUILD.md` — canonical T2 rebuild runbook
- `docs/MCP-CLIENTS.md` — initial hybrid-mode setup
