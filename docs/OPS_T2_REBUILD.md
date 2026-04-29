# Rebuilding hosted T2 from git + T1

Plan v20 Phase 7 runbook. Use this after Phase 6 has split a legacy shared
FalkorDB graph into canonical `<org>_<repo>_t1` / `_t2` targets, or any
time the hosted T2 Graphiti graph needs to be reconstructed from the
authoritative source (git orphan-branch thread entries plus the T1
baseline graph).

**Audience:** Tier-A operators with access to the `proud-blessing`
Railway project. Expect the full rebuild of `watercooler-cloud` (~3k
entries) to take 45–90 min; other projects scale roughly linearly.

## Authoritative sources

1. **Git orphan branch** — `watercooler/threads` in the code repo. Every
   thread entry commit is the canonical record: body, title, timestamp,
   agent/role footer. This is what MCP reads when clients call
   `watercooler_read_thread`.
2. **T1 baseline graph** — `<org>_<repo>_t1` on hosted FalkorDB. Holds
   entry embeddings, thread metadata, entity xref seeds. Useful for
   semantic-order hints during rebuild but not strictly required.

T2 (Graphiti episodic + entity) is **derived** state — there is no write
path that is canonical there. Everything in `_t2` can be reconstructed
from (1); (2) accelerates the rebuild because we can reuse embeddings
rather than recompute them.

## Prerequisites

- Phase 6 migration completed: `<org>_<repo>_t1` / `_t2` exist, legacy
  combined graph is either renamed to `_t1` or split appropriately.
- Hosted MCP deployed with Plan v20 Phase 4 (durable-queue) changes.
- Railway CLI logged in and linked to `proud-blessing`.
- A quiet period — rebuild adds episodes; hosted writers should be
  paused or configured to skip rebuild entries. The usual convention is
  to set `WATERCOOLER_MEMORY_BACKEND=` to empty on the hosted service
  for the duration, or to set the hosted daemons to `route = "disabled"`
  in the operator-owned `deploy/hosted/config.toml`.

## Step-by-step

### 1. Snapshot the current `_t2` graph

Even though T2 is derived state, take a snapshot so a failed rebuild can
be rolled back without replaying every thread:

```bash
scripts/ops/falkordb_admin.sh backup production
# Verify: confirm s3://<bucket>/<prefix>rdb/dump-<ts>.rdb is present.
```

### 2. Confirm the source inventory

```bash
# Count thread entries in the orphan branch.
git -C ~/.watercooler/worktrees/watercooler-cloud rev-list --count watercooler/threads

# Confirm T1 node counts (optional, informational).
scripts/ops/falkordb_admin.sh graphs production | head -60
```

Record both numbers. After the rebuild, `_t2` should roughly mirror the
entry-count denominator — each entry produces 1 episode plus a variable
number of entity/edge nodes.

### 3. Clear the rebuild target

If the target `<org>_<repo>_t2` already holds stale post-Phase 6 content
(e.g., a failed earlier rebuild), clear it before seeding:

```bash
railway run --service falkordb --environment production -- \
  redis-cli GRAPH.DELETE mostlyharmless_ai_watercooler_cloud_t2
```

If the target is empty (fresh Phase 6 output), skip this step.

### 4. Run `watercooler_bulk_index` against the hosted endpoint

Against the hosted MCP, enqueue one project's entries:

```bash
# With the hosted MCP mounted (see docs/MCP-CLIENTS.md for setup):
mcp-cli call watercooler_bulk_index \
  --arg backend=graphiti \
  --arg threads='' \
  --arg code_path='' \
  --arg max_entries=0 \
  --arg confirm=true
```

Arguments:

- `backend=graphiti` — enqueue into the T2 pipeline.
- `threads=''` — empty string means "all threads".
- `code_path=''` — hosted resolves via `http_ctx.repo`; the Phase 6
  identity fix derives `group_id` as `<org>_<repo>` from the repo header.
- `max_entries=0` — no cap.
- `confirm=true` — acknowledges rebuild against a populated graph.

This returns immediately with a `queued` count and a `task_id` per
enqueued entry. The hosted memory queue worker processes them at the
configured concurrency.

### 5. Poll progress

```bash
# Queue summary:
mcp-cli call watercooler_memory_task_status

# Spot-check a single task from the enqueue response:
mcp-cli call watercooler_memory_task_status --arg task_id=<id>
```

Expect `queue_depth` to drop steadily. The worker's
`active_backend_count` and `total_timeouts` surface stuck or slow
backends. A graph that is not draining despite `queue_depth > 0` and
`active_backend_count == 0` means the worker has died — check
`railway logs --service watercooler-hosted-mcp`.

### 6. Verify the rebuild

After the queue drains:

```bash
# Episode / entity counts.
railway run --service falkordb --environment production -- \
  redis-cli GRAPH.QUERY mostlyharmless_ai_watercooler_cloud_t2 \
    "MATCH (n) RETURN labels(n), count(n)"

# Compare to Phase 6 pre-split totals (from the profile report in step 2).
```

Minimum expected classes in `_t2`:

- `Episode` / `EpisodicNode` — ~1 per indexed entry (plus 1 per chunk
  for large bodies).
- `Entity` — variable; depends on LLM extraction yield.
- `Community` — present only after a run of community detection.

Entity and community counts will NOT match the pre-split totals exactly;
LLM extraction is non-deterministic. Episode count, however, should be
≥ entry count (chunked entries produce multiple episodes).

### 7. Retire the legacy T2 remnants

If Phase 6 left a legacy graph tagged `<legacy>_t2_bak` (future
enhancement), drop it now. Otherwise, if the operator ran
`scripts/migrate_splitgraphs.py --delete-legacy` during Phase 6, there
is nothing to retire.

Also verify there is no accidentally-created local T2:

```bash
# On the operator's laptop:
docker ps --filter name=falkordb   # expect nothing, or a non-hosted dev instance
```

In `local_hybrid` mode post-Phase 5, `get_graphiti_backend` returns
`hybrid_refused` so the local FalkorDB instance should have no
`_t2` graphs. If a stale local `_t2` graph exists, it dates from
pre-Phase 5 — drop it.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `queue_depth` stays flat, `active_backend_count == 0` | Worker crashed | Restart hosted MCP via `railway service restart` |
| Task dead-letters with `TimeoutError` | Slow LLM provider | Check DeepSeek status; retry via `retry_dead_letters=true` |
| Task dead-letters with `hybrid_refused` | Rebuild accidentally hit a local-hybrid client | Run from hosted side, not a hybrid client |
| Entity count far below expectations | LLM extraction disabled | Confirm `WATERCOOLER_GRAPHITI_LLM_EXTRACT=1` on the hosted service |
| GRAPH.COPY fails during Phase 6 rerun | Target already populated | Clear via `GRAPH.DELETE <target>` and re-run |

## Rollback

If the rebuild produces obviously-wrong state (wrong entity counts,
missing episodes), roll back by restoring the snapshot taken in step 1:

```bash
# Follow the restore procedure in docs/OPS_RAILWAY_FALKORDB.md §Restore.
```

After restore, investigate the root cause before re-running the rebuild.
Do not keep re-running `watercooler_bulk_index` with successive partial
failures — each run will add duplicate episodes unless the
`entry_episode_index` dedup file is aligned with the graph state.

## References

- `docs/OPS_RAILWAY_FALKORDB.md` — hosted admin runbook (Phase 3
  deliverable).
- `scripts/migrate_splitgraphs.py` — Phase 6 migration tool that
  produces the `_t2` graph this runbook rebuilds into.
- `src/watercooler_mcp/tools/memory.py` — `_bulk_index_hosted_impl`
  (Phase 6 identity cleanup) and `_graphiti_add_episode_impl`
  (Phase 4 durable queue).
- Plan thread `hybrid-falkordb-state-vs-intent` (`01KPYRYW43VPKBJKXVCRZYA8Q5`) —
  the authoritative plan.
