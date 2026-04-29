# Operating the hosted FalkorDB on Railway

Admin runbook for the Railway-hosted FalkorDB instance that backs the T2
(Graphiti) knowledge graph and, after Plan v20 Phase 8, the Falkor-backed
T1 semantic sub-operations in hybrid mode.

**Access tier: A (infrastructure).** Anyone who needs to touch this surface
should already be a member of the `proud-blessing` Railway project. No
separate code-level allowlist exists — platform-native access control
applies. Dashboard-tier "admin" observability (Tier B, `User.role = "admin"`
on Neon) is deferred to watercooler-site and is not required for anything
in this runbook.

This runbook does **not** depend on any new MCP admin tools. All access is
via Railway CLI + SSH; see "MCP admin tools" below for why that's
deliberate.

## Prerequisites

- Railway CLI installed and logged in (`railway login`).
- Membership in the `proud-blessing` Railway project (Caleb + Jay today).
- Repo checked out locally (`watercooler-cloud`).
- Optional: `redis-cli` installed locally for ad-hoc queries. The Railway
  container also has it, so this is only needed for out-of-container work.

Verify membership:

```bash
railway whoami
railway status        # from the repo root, confirms linked project
railway list          # lists projects you can access
```

If `proud-blessing` is not listed, ask an existing admin to add you before
anything else in this runbook will work.

## Common ops

Every admin action should go through the `scripts/ops/falkordb_admin.sh`
wrapper so it gets captured in `~/.watercooler/admin_log.jsonl`. The raw
commands are documented here for traceability.

### Link the repo to Railway (one-time per checkout)

```bash
cd <watercooler-cloud>
railway link --project proud-blessing
```

### Open a Redis shell against the hosted FalkorDB

```bash
scripts/ops/falkordb_admin.sh shell
# or raw:
railway run --service falkordb --environment production -- redis-cli
```

The session is scoped to the internal hostname `falkordb.railway.internal`
on port 6379. FalkorDB is a Redis module — everything you can do via
`redis-cli` works.

### List graphs and counts

```bash
# Through the wrapper (logs the action):
scripts/ops/falkordb_admin.sh graphs
# Or raw, inside the Redis shell:
GRAPH.LIST
# Per-graph label counts:
GRAPH.QUERY <graph-name> "MATCH (n) RETURN labels(n), count(n)"
```

Expected production graphs (as of Plan v20 Phase 0):

- `watercooler_cloud` — primary T2 graph (Graphiti episodic + entity) for
  the main project. Will be renamed to `mostlyharmless_ai_watercooler_cloud_t2`
  during Phase 6 migration.
- `watercooler_site` — secondary project (dashboard/watercooler-site).
  Will rename similarly in Phase 6.

### SSH into the FalkorDB container

```bash
scripts/ops/falkordb_admin.sh ssh
# Raw:
railway ssh --service falkordb --environment production
```

SSH is required for in-container backup-snapshot creation, log
inspection, and FalkorDB config introspection beyond what
`redis-cli INFO` returns.

If `railway ssh` reports "SSH not enabled on this service," enable it
via the Railway dashboard: Services → `falkordb` → Settings → SSH →
Enable. After enabling, the admin with Railway project access can SSH
into the container for the next deploy onwards.

## Backup

Plan v20 Phase 3 stands up a separate `falkordb-backup` Railway cron
service. The cron service is not running until that phase is deployed.
Until then, run the backup manually when needed (see below). The strategy
is the same in both cases:

1. **Volume snapshot (daily, infra-level).** RDB dump from the running
   FalkorDB container, shipped to an S3-compatible object store.
   Retention: 14 days rolling.
2. **Logical export (weekly, per-graph).** Cypher dumps to JSONL, one
   file per graph, under the `logical/` prefix. Retention: 8 weeks
   rolling (two months). Inspectable offline without a FalkorDB.

### Manual backup (pre-Phase 3, or ad-hoc)

```bash
scripts/ops/falkordb_admin.sh backup
```

The wrapper runs:

1. `redis-cli --rdb /tmp/dump-<ts>.rdb` inside the falkordb container
   (`railway run --service falkordb -- ...` triggers `BGSAVE` and pulls
   the resulting `dump.rdb`).
2. Uploads the file to the configured S3-compatible bucket using the
   credentials in the operator's local env.
3. Runs per-graph `GRAPH.QUERY` exports to JSONL and uploads those too.

The credentials the wrapper reads are **local read credentials** for the
backup bucket — separate from the write-only IAM identity the cron
service uses in production. This mirrors the principle of least
privilege from the v3 backup design and should be rotated on the
`authentication-secrets-rotation` cadence.

### Restore from a snapshot

⚠️ **Destructive.** Confirms with Caleb first before running in
production.

1. Identify the snapshot to restore. RDB files live under
   `s3://<BACKUP_S3_BUCKET>/<BACKUP_S3_PREFIX>/rdb/`. Logical exports
   under `.../logical/<graph_name>/...`.
2. Stop the FalkorDB service (from the Railway dashboard or CLI):
   ```bash
   railway service stop --service falkordb --environment production
   ```
3. SSH into the container's persistent volume mount point
   (`/var/lib/falkordb/data/`) and replace `dump.rdb` with the snapshot:
   ```bash
   railway ssh --service falkordb -- 'mv /var/lib/falkordb/data/dump.rdb{,.pre-restore}'
   railway ssh --service falkordb -- 'aws s3 cp s3://.../dump-<ts>.rdb /var/lib/falkordb/data/dump.rdb'
   ```
4. Restart the FalkorDB service:
   ```bash
   railway service start --service falkordb --environment production
   ```
5. Verify with `GRAPH.LIST` + node counts (see "List graphs and counts"
   above) that the restored state matches expectations.

Logical-export restore is different: the JSONL files are source data for
a rebuild via `watercooler_bulk_index`, not a drop-in file replacement.
See Plan v20 Phase 7 for the git/T1-authoritative rebuild flow.

### Dry-run restore (validation)

Before trusting a backup, verify an RDB restores cleanly against a
disposable FalkorDB:

```bash
# Pull the snapshot locally
aws s3 cp s3://<bucket>/<prefix>/rdb/dump-<ts>.rdb /tmp/restore-test.rdb
# Boot a disposable local FalkorDB with this snapshot
docker run -d --name falkordb-restore-test \
  -p 16379:6379 \
  -v /tmp:/data \
  falkordb/falkordb:latest --dbfilename restore-test.rdb
# Verify
redis-cli -p 16379 GRAPH.LIST
redis-cli -p 16379 GRAPH.QUERY watercooler_cloud "MATCH (n) RETURN count(n)"
# Tear down
docker rm -f falkordb-restore-test
```

Run this drill at least once after Phase 3 lands and quarterly thereafter.

## Wedge recovery

If the hosted FalkorDB becomes unresponsive:

1. Check service status: `railway status --service falkordb --environment production`.
2. Check container logs: `railway logs --service falkordb --environment production --lines 200`.
3. Common causes (prioritised by likelihood):
   - **Slow query**: `redis-cli CLIENT LIST` + `redis-cli CLIENT KILL ID <id>` for a long-running connection. Verify via `redis-cli INFO commandstats`.
   - **Index fragmentation**: `redis-cli DEBUG SLEEP 0.01` as liveness probe; `CALL db.indexes()` per graph to inspect index state; rebuild the index if fragmentation is the cause.
   - **Memory pressure**: `redis-cli INFO memory` — if `used_memory_rss` is near the Railway service limit, either scale the service or evict low-value graphs (test/benchmark fixtures, see Phase 0 housekeeping).
   - **Persistence stall**: `redis-cli INFO persistence` — a stuck BGSAVE can block writes. `DEBUG AOF-STAT` if AOF is enabled.
4. Last resort: restart the service (`railway service restart --service falkordb`). Coordinate with Caleb first; a restart will drop open Graphiti/LeanRAG connections on watercooler-cloud.

## Access management

Adding an admin:

1. An existing admin invites the new operator to the `proud-blessing`
   Railway project via the Railway dashboard.
2. New operator runs `railway login` + `railway whoami` + `railway list`
   to confirm access.
3. New operator clones watercooler-cloud, `cd`s in, runs `railway link
   --project proud-blessing`.
4. Set up the `BACKUP_S3_*` read credentials in their local env (the
   write-only IAM identity for the cron service stays on Railway; an
   operator needs a separate read identity for manual pulls / dry-run
   restores).

Removing an admin: revoke Railway project access + ensure they no longer
hold S3 read credentials for the backup bucket. No code or config change
is required on the watercooler-cloud side.

## MCP admin tools

**There are none by design.** Plan v20 principle 7 preserves PR #653's
config-driven daemon model and does not grow the MCP tool surface for
admin use. The `memory_admin_graph` capability is declared in
`src/watercooler_mcp/capabilities.py` but dormant — reserved for a
future narrow admin MCP tool if a common, high-value use case emerges
that can't be served by CLI/SSH + runbook.

If you find yourself repeatedly running the same ad-hoc admin operation
in production, that's a signal to discuss promoting it to a `scripts/ops/`
wrapper or — for the highest-value cases — a scoped MCP admin tool.
Raise the case on the `hybrid-falkordb-state-vs-intent` thread first.

## Audit trail

`scripts/ops/falkordb_admin.sh` appends a JSON line per action to
`~/.watercooler/admin_log.jsonl`:

```json
{"ts":"2026-04-24T03:30:00Z","operator":"caleb","action":"shell","target":"falkordb","environment":"production","args":"","exit_code":0}
```

Railway's own service logs are the authoritative source for what
actually happened inside the container. Operator-local logs are a
supplement for cross-operator visibility, not a replacement for Railway
logs.

If Caleb and Jay decide shared cross-operator audit is worth the
complexity, a future enhancement can append these lines to an admin
thread on the watercooler orphan branch. That's a deferred decision —
not a blocker for this runbook to ship.

## References

- Plan v20 (thread `hybrid-falkordb-state-vs-intent/01KPYRYW43VPKBJKXVCRZYA8Q5`) —
  this runbook is the Phase 3 deliverable.
- `docs/OPS_T2_REBUILD.md` — Phase 7 rebuild runbook. Uses the admin
  wrapper here for pre/post snapshots.
- `scripts/migrate_splitgraphs.py` — Phase 6 split-graphs migration.
- `docs/DEPLOYMENT_HOSTED.md` — general Railway + Vercel + Neon topology
  for the hosted control plane.
- `docs/TROUBLESHOOTING.md` — user-facing troubleshooting that links here
  for hosted FalkorDB issues.
- `authentication-secrets-rotation` thread — canonical rotation process
  that the backup-bucket credentials fold into.
