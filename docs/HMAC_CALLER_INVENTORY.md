# HMAC caller inventory

Sprint 2 deliverable for Move 2.5 of the security consolidation plan
v5.1. This document enumerates every component that signs an HMAC
request to `watercooler-cloud`'s MCP server, plus the metadata needed
to issue it a v3 key and migrate the call site.

The point of the inventory is to make the migration tractable: once
each caller has a row here, Sprint 3's caller migration is a
mechanical loop over the rows.

## Schema

| Column | Meaning |
|---|---|
| Caller | Component name (repo, service, or daemon process) |
| Repo | Source repo + entry-point file |
| Key type | `per_user` or `service` per the v3 plan |
| Subject binding | For `per_user`: the `bound_user_id`. For `service`: `service_identity` and (delegation policy: `no_user_delegation` or `allow_list[user_id]`) |
| Repo authorization | For `per_user`: derived from token `repos` claim. For `service`: server-configured `repo_allow_list` |
| Secret source | Where the caller obtains its v3 key today (and post-migration) |
| Status | `v2-only` / `v3-ready` / `migrated` |
| PR | The PR that migrated this caller (Sprint 3 work) |

## Inventory

| Caller | Repo | Key type | Subject binding | Repo authorization | Secret source | Status | PR |
|---|---|---|---|---|---|---|---|
| watercooler-site dashboard proxy | `mostlyharmless-ai/watercooler-site` — `lib/wcMcpClient.ts` | `service` | `service_identity = "dashboard"`, delegation = `allow_list` of user_ids whose tokens the proxy holds | `repo_allow_list` covering all org+user repos the dashboard proxies for | env var `WATERCOOLER_HMAC_KEY_dashboard_SECRET` (today: legacy `WATERCOOLER_INTERNAL_SECRET`) | v2-only | TBD |
| Local hybrid client → premium tools | `watercooler-cloud` — `src/watercooler_mcp/premium_client.py` | `per_user` | `bound_user_id` = local user's hosted ID (from the bearer-token issuer) | derived from the local user's GitHub token `repos` claim | dashboard-issued per-user key fetched alongside the bearer token; no env-var fallback | v2-only | TBD |
| capture-hook → hosted memory queue | `watercooler-cloud` — `src/watercooler_mcp/capture_hook.py` | `per_user` | `bound_user_id` = capture-hook's invoking user | derived from the user's GitHub token `repos` claim | dashboard-issued per-user key (same as premium_client) | v2-only | TBD |
| Slack ingress webhook (deferred) | `mostlyharmless-ai/watercooler-site` — `app/api/slack/events/route.ts` | `service` | `service_identity = "slack-ingress"`, delegation = `no_user_delegation` (Slack→user mapping happens server-side) | `repo_allow_list` of all Slack-mapped repos | env var `WATERCOOLER_HMAC_KEY_slack_ingress_SECRET` | v2-only | TBD |
| ops scripts (`scripts/ops/*`) | `watercooler-cloud` — `scripts/ops/falkordb_admin.sh` and friends | `service` | `service_identity = "ops"`, delegation = `no_user_delegation` | full `repo_allow_list` (admin) | env var `WATERCOOLER_HMAC_KEY_ops_SECRET` (operator-supplied) | v2-only | TBD |
| Test-cjh CI smokes | `watercooler-cloud` — `tests/integration/test_railway_smoke.py` (local mode); `.github/workflows/*` runs them | `service` | `service_identity = "smoke-test-user"`, delegation = `self` (no_user_delegation) | `repo_allow_list = ["mostlyharmless-ai/watercooler"]` | env var `WATERCOOLER_HMAC_KEY_ci_smoke_SECRET` (test fixture sets it; CI inherits) | **migrated** | #714 |

> **Important — table reads as historical baseline.** The `Status` and
> `PR` columns above are the inventory **as drafted at the start of
> Sprint 1**, when every caller still flowed v2 traffic through the
> legacy `WATERCOOLER_INTERNAL_SECRET` global key and v3 verification
> only accepted it under `WATERCOOLER_HMAC_REQUIRE_V3=warn` via the
> back-compat shim. Those columns have **not** been updated in place;
> use the per-row Sprint 4 status note immediately below for current
> operational reality.
>
> **Current operational status as of 2026-05-01 (Plan v5.1 Sprint 4
> closure).** All non-CI rows above are now operating on v3 in
> production. This is the inverse of the table — every row that reads
> `v2-only / TBD` has been migrated to v3:
>
> - `WATERCOOLER_HMAC_REQUIRE_V3=enforce` is the production default on
>   Railway; the v2 verification path is dead code (deleted by issue
>   #733).
> - `WATERCOOLER_INTERNAL_SECRET` is unset in the Railway production
>   runtime (removed in PR #731 + watercooler-site companion).
> - The slack-sync ingress no longer falls back to the legacy global
>   secret — it reads `WATERCOOLER_SLACK_SYNC_SECRET` only.
> - Per-user dashboard-issued keys back the local hybrid client and the
>   capture-hook caller (Sprint 3).
> - Service keys for the dashboard proxy, slack ingress, and ops scripts
>   are configured server-side via the
>   `WATERCOOLER_HMAC_KEY_<KEY_ID>_*` env-var convention.
>
> An operator provisioning a new hosted deployment should expect to
> configure per-`KEY_ID` env vars for every service caller in this
> inventory, plus dashboard-side issuance for the per-user keys.
> Following the migration-order guidance below remains useful for
> staging a fresh rollout, but is not a current backlog.

## Migration order (proposed, Sprint 3)

1. **ops scripts** — internal-only, low blast radius if rolled back.
   Validates the per-key registry path end-to-end without exposing
   external traffic.
2. **CI smokes** — same shape as ops, slightly broader because
   GitHub Actions runners need to mint signatures.
3. **dashboard proxy** — biggest blast radius (all hosted user
   traffic), but also the one with the most existing test coverage
   in `watercooler-site`. Stage on test-cjh first.
4. **premium_client** — local-side, customer-facing. Requires the
   dashboard side to be issuing per-user keys alongside bearer
   tokens (Sprint 2-3 prerequisite), so this can only land after
   the issuance path ships.
5. **capture_hook** — depends on the same per-user-key issuance.
6. **Slack ingress** — Slack ingress isn't on the critical path for
   tenant isolation (it doesn't carry user-tied repo access); can
   ship anytime in Sprint 3.

## Per-key env-var convention

Service keys are configured server-side via env vars:

```
WATERCOOLER_HMAC_KEY_<KEY_ID>_SECRET=<utf8-string>
WATERCOOLER_HMAC_KEY_<KEY_ID>_TYPE=service
WATERCOOLER_HMAC_KEY_<KEY_ID>_SERVICE_IDENTITY=<user_id>
WATERCOOLER_HMAC_KEY_<KEY_ID>_DELEGATION=self  # or "alice,bob,carol"
WATERCOOLER_HMAC_KEY_<KEY_ID>_REPOS=org/repo-1,org/repo-2
```

`KEY_ID` must match `[A-Za-z0-9_-]+`. The pattern is enforced in
`auth/hmac_keys.py:_is_valid_key_id` (called from
`_load_service_keys_from_env`); malformed key_ids are skipped at
load time with a warning. The character restriction is
load-bearing — a key_id containing ``\n`` would silently corrupt
the newline-delimited HMAC v3 canonical string for every request
using that key.

The `SECRET` value is interpreted as the literal UTF-8 bytes of the
env-var string. Operators who want hex- or base64-encoded key
material must decode it themselves before setting the var; the load
path does NOT decode. Both signing and verifying sides must agree
on the same convention.

`REPOS` MUST be set and non-empty for any service key. An empty or
missing repos list is treated as an operator misconfiguration and
rejected unconditionally — see `RepoAuthError.fatal` in
`auth/hmac_keys.py`. Honouring `WATERCOOLER_HMAC_REQUIRE_V3=warn`
for an empty allow-list would let the key authenticate against any
`X-Repo`, which is the opposite of fail-closed.

### `DELEGATION` is NOT a superset of `service_identity`

The four `DELEGATION` shapes and their semantics:

| Value | Meaning | Notes |
|---|---|---|
| omitted | Defaults to `self` (no_user_delegation) | Same as `DELEGATION=self`. |
| `self` | Only `service_identity` may sign | The `no_user_delegation` policy. |
| `alice,bob,carol` | Exactly these subjects may sign | `service_identity` is **NOT** auto-included. |
| `` (empty string) | **Non-functional key** — every request fails 401 | Distinct from `self`. A load-time WARNING fires (`hmac_keys.py:_load_service_keys_from_env`). |

If the service should authenticate as itself in addition to
delegating to others, `service_identity` must appear explicitly
in the CSV:

```
WATERCOOLER_HMAC_KEY_dashboard_SERVICE_IDENTITY=dashboard
WATERCOOLER_HMAC_KEY_dashboard_DELEGATION=dashboard,alice,bob
```

This is deliberate — `DELEGATION` is the explicit allow-list, not
"these users in addition to the service identity". A subject not
in the list (including `service_identity`, when omitted) fails
`check_subject_binding` and the request returns a generic 401.

**Empty string vs `self`:** an operator who sets `DELEGATION=`
expecting "no delegated users" gets a non-functional key — the
empty allow-list rejects every signed `X-User-ID` including the
service identity itself. The PR #703 round 6 load-time warning
makes this loud at registry load (so the misconfiguration shows
up in startup logs rather than only via 401s in production), but
operators reading this doc should know that `self` and the empty
string are NOT equivalent. Always use `DELEGATION=self` for the
"only the service may sign" case.

Per-user keys are not env-configured — they come from the
token-issuing service (dashboard) alongside bearer tokens (Sprint 3
PR β).

## H1-H14 verification matrix → test file mapping

| Finding | Test |
|---|---|
| H1 signature integrity | `tests/unit/test_hmac_v3_primitives.py::TestSignatureVerification` |
| H2-H3 per-user repo claim | `TestRepoAuthorisation::test_per_user_*` |
| H4-H5 service repo allow-list | `TestRepoAuthorisation::test_service_*` + `tests/integration/test_hmac_v3.py::TestServiceKey` |
| H6-H8 v2/v3 coexistence | `tests/integration/test_hmac_v3.py::TestV3MigrationCoexistence` |
| H9 replay window | `tests/integration/test_hmac_v3.py::TestTimestampReplayWindow` |
| H10 cross-subject blocked | `TestSubjectBinding::test_per_user_key_rejects_other_subject` + `tests/integration/test_hmac_v3.py::TestCrossSubjectAssertionBlockedEndToEnd` |
| H11-H12 service delegation | `TestSubjectBinding::test_service_*` |
| H13 startup fail-fast | `TestStartupFailFast::test_enforce_mode_multi_tenant_with_global_fails` (under `WATERCOOLER_HMAC_REQUIRE_V3=enforce`, the legacy global `WATERCOOLER_INTERNAL_SECRET` MUST be absent on multi-tenant deployments — Plan v5.1 Sprint 4 removed it from the Railway runtime; this guard catches accidental re-introduction) |
| H14 unknown / revoked key | `TestKeyRegistry::test_lookup_unknown_returns_none`, `TestKeyRegistry::test_revoked_key_lookup_returns_none`, `tests/integration/test_hmac_v3.py::TestV3MigrationCoexistence::test_v3_unknown_key_returns_401` |
