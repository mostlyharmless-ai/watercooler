# Railway operations runbook

Operator guide for changing environment variables and triggering redeploys
on the Railway-hosted watercooler services. This runbook covers the
**asymmetry between the Railway CLI and the Railway dashboard** when
mutating env vars: some operations auto-trigger a redeploy, others do
not.

The runbook applies to every service in the `proud-blessing` Railway
project (e.g., `watercooler-cloud`, `falkordb`, `falkordb-backup`). For
FalkorDB-specific operations (backups, restores, wedge recovery) see
[`docs/OPS_RAILWAY_FALKORDB.md`](OPS_RAILWAY_FALKORDB.md). For the
broader Railway/Vercel/Neon topology see
[`docs/DEPLOYMENT_HOSTED.md`](DEPLOYMENT_HOSTED.md).

## TL;DR

- **Dashboard env-var deletion does NOT auto-redeploy.** Always follow
  up with `railway redeploy --service <name>` (or click "Redeploy
  latest" in the dashboard) when you delete a variable via the Railway
  web UI.
- **CLI `--set` and `--set-from-stdin` DO auto-redeploy.** No manual
  follow-up is needed when you mutate variables through the CLI.
- **Verify the redeploy landed** by tailing the deploy log and
  confirming the fresh-container startup line:
  `INFO:     Uvicorn running on http://0.0.0.0:8080`.

## Background — why this runbook exists

On 2026-05-01 an operator deleted `WATERCOOLER_INTERNAL_SECRET` from
the `watercooler-cloud` production service via the Railway dashboard
UI. The dashboard reported the deletion as successful and the
project's stored env-var list updated immediately, but the running
container kept its previously-loaded environment. Authenticated HMAC
requests continued to succeed against the live container until an
explicit `railway redeploy` was invoked. The pre/post-redeploy 401
body comparison from F1.3 of the incident response confirmed: only
the post-redeploy container reflected the deletion.

The asymmetry is not documented in Railway's UI today, so it is easy
for a new operator to assume that "the dashboard deleted it" implies
"the running container no longer has it." That assumption is unsafe
and led directly to the 2026-05-01 incident requiring a follow-up
redeploy step. This runbook captures the gap so future operators do
not learn it by surprise.

## Env-var operation matrix

| Operation | Auto-redeploys? | Notes |
|-----------|-----------------|-------|
| `railway variables --set KEY=VALUE` | Yes | The CLI queues a redeploy on the linked service automatically. |
| `railway variables --set-from-stdin` | Yes | Same redeploy semantics as `--set`. |
| Dashboard "Add variable" | Yes | Adding a new variable through the UI triggers a redeploy. |
| Dashboard "Edit variable" (value change) | Yes | Editing an existing value through the UI triggers a redeploy. |
| **Dashboard "Delete variable"** | **No** | Updates the stored env-var list but leaves the running container with the old value loaded. Operator must trigger a redeploy explicitly. |
| Dashboard "Redeploy latest" | N/A (manual deploy) | Use this after a dashboard deletion to pick up the new env state. |
| `railway redeploy --service <name>` | N/A (manual deploy) | The canonical CLI follow-up after a dashboard deletion. |

There is no Railway CLI flag equivalent to a dashboard delete (no
`railway variables --remove KEY`), so deletions today must go through
the dashboard UI and require the explicit follow-up redeploy.

## Standard operating procedure: deleting an env var

When you need to delete an env var from a Railway service:

1. **Confirm scope.** Identify the service (`railway status`) and the
   environment (`production`, `staging`, etc.). Sensitive variables —
   secrets, internal HMAC keys, database URLs — should always be
   double-checked against the deployment topology in
   [`docs/DEPLOYMENT_HOSTED.md`](DEPLOYMENT_HOSTED.md) before deletion.
2. **Delete via the Railway dashboard UI.** Navigate to the service
   → Variables tab → click the row → Delete. The dashboard will
   confirm the deletion was applied to the stored variable list.
3. **Trigger an explicit redeploy.** From the repo root:

   ```bash
   railway redeploy --service <service-name>
   ```

   Or use the dashboard: open the service → Deployments → click
   "Redeploy latest" on the most recent deployment. Either path forces
   a fresh container that reflects the updated env-var state.
4. **Verify the redeploy landed.** Tail the deploy log and look for
   the fresh-container startup line:

   ```bash
   railway logs --service <service-name> --environment production --lines 100
   ```

   Confirm a new `INFO:     Uvicorn running on http://0.0.0.0:8080`
   line (or the equivalent startup banner for the affected service)
   has appeared after the redeploy timestamp. If the startup line is
   missing or older than the redeploy time, the deploy did not roll —
   investigate before assuming the env change took effect.
5. **Verify behavior.** For an env var that gates runtime behavior
   (e.g., `WATERCOOLER_INTERNAL_SECRET`), re-run the smoke test that
   exercises the gated path. The pre/post-redeploy response should
   change as expected (e.g., authenticated calls now 401 because the
   secret is no longer loaded).

## Standard operating procedure: setting / changing an env var

The CLI path is the recommended default because it auto-redeploys and
the change is captured in shell history:

```bash
railway variables --set KEY=VALUE --service <service-name>
```

For multi-line values or secrets you don't want in shell history:

```bash
printf '%s' "$SECRET_VALUE" | railway variables --set-from-stdin KEY --service <service-name>
```

Use `printf` rather than `echo` to avoid the trailing newline that
`echo` appends — that newline corrupts secrets that are compared
byte-for-byte (e.g., HMAC keys).

The dashboard "Add variable" and "Edit variable" flows also
auto-redeploy and are fine for one-off interactive use. Prefer the
CLI when scripting or batching changes.

## Failure mode to watch for

The dominant failure mode this runbook prevents:

> "I deleted the variable in the dashboard. The new behavior didn't
> happen. Did the deletion fail?"

In almost every case the deletion succeeded — but the running
container hasn't restarted, so it's still serving requests with the
old env loaded. The fix is always: trigger a redeploy and verify the
startup line.

If after an explicit redeploy the new behavior still hasn't appeared,
check:

- Did the redeploy actually finish? `railway status --service <name>`
  should show the new deployment as `SUCCESS`.
- Are there multiple services holding the same secret? Each service
  has its own env-var scope and must be redeployed independently if
  you mean to change the secret everywhere.

Note: the redeploy requirement is independent of *when* a particular
code path reads its env vars. Some watercooler-cloud paths read env
at process startup (e.g., the unified config facade), while others
read raw `os.getenv()` per-request inside middleware (e.g.,
`WATERCOOLER_INTERNAL_SECRET` in the legacy v2 HMAC path). In both
cases, Railway cannot mutate the process environment of a running
container — env changes only become visible after the container
restarts. The redeploy step is therefore required regardless of read
timing, and the verification is always the same: a new
`Uvicorn running on http://0.0.0.0:8080` startup line in the deploy
log.

## Quick reference

```bash
# Set or change a variable (auto-redeploys):
railway variables --set KEY=VALUE --service watercooler-cloud

# Set from stdin (auto-redeploys, no shell history leakage):
printf '%s' "$VALUE" | railway variables --set-from-stdin KEY --service watercooler-cloud

# Delete a variable (dashboard only — does NOT auto-redeploy):
# 1. Delete in Railway dashboard UI.
# 2. Then:
railway redeploy --service watercooler-cloud

# Verify a redeploy landed:
railway logs --service watercooler-cloud --environment production --lines 100
# Look for: INFO:     Uvicorn running on http://0.0.0.0:8080
```

## References

- [`docs/OPS_RAILWAY_FALKORDB.md`](OPS_RAILWAY_FALKORDB.md) — FalkorDB
  service operations (backups, restores, wedge recovery).
- [`docs/DEPLOYMENT_HOSTED.md`](DEPLOYMENT_HOSTED.md) — Railway +
  Vercel + Neon topology and env-var ownership map.
- [`RELEASING.md`](../RELEASING.md) — release protocol; links here
  from the Railway env-var operations section.
