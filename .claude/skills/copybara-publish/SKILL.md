---
name: copybara-publish
description: This skill should be used to publish watercooler-cloud to the public mostlyharmless-ai/watercooler repo via the Copybara pipeline. It handles the full release promotion flow — main → staging → stable — or partial flows starting from staging or stable, including version bumping, PR creation, CI monitoring, and verifying the public sync. Use when releasing a new version or when the user asks to "publish to the public repo", "run copybara", "promote to stable", or "cut a release".
allowed-tools:
  - Bash(git *)
  - Bash(gh *)
  - Bash(python3 *)
  - AskUserQuestion
---

# Copybara Publish

Publish watercooler-cloud to the public `mostlyharmless-ai/watercooler` repo.

The pipeline is: `main` → `staging` → `stable` → Copybara auto-syncs to public `main`.

Copybara triggers automatically on every push to `stable` via `.github/workflows/copybara-publish.yml`.
The `stable` branch also runs a pre-publish allowlist validation check (`copybara-dry-run.yml`) on every PR targeting `stable`.

## Step 1: Determine starting point

Ask the user which step they're starting from (call `AskUserQuestion`):

> "Where are we starting the publish from?
> 1. **Full flow** — promote from `main` → `staging` → `stable` → publish
> 2. **From staging** — `staging` is already ready, just merge to `stable` → publish
> 3. **From stable** — `stable` already has the release commit, just trigger/verify the Copybara sync"

Then proceed to the matching path below.

---

## Path A: Full flow (main → staging → stable → publish)

### A1. Check current version

```bash
python3 -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.toml', 'rb') as f:
    d = tomllib.load(f)
print('pyproject.toml:', d['project']['version'])
with open('pyproject.public.toml', 'rb') as f:
    d = tomllib.load(f)
print('pyproject.public.toml:', d['project']['version'])
"
```

If either version ends in `-dev`, proceed to A2. If already a release version, confirm with the user before proceeding (may be a re-publish of an already-tagged version).

### A2. Bump version — remove `-dev` suffix

Determine the release version (strip `-dev` from the current value, e.g. `0.2.7-dev` → `0.2.7`).
Confirm the target version with the user before editing.

Use the Read + Edit tools to update **both** files. The version line looks like:

```toml
version = "0.2.7-dev"
```

Change it to `version = "0.2.7"` in both `pyproject.toml` and `pyproject.public.toml`.

**Important**: `pyproject.public.toml` must always be updated whenever `pyproject.toml` changes — the `copybara-dry-run.yml` CI check enforces this and will block the stable PR if out of sync.

### A3. Commit the version bump on main

```bash
git add pyproject.toml pyproject.public.toml
git commit -s -m "$(cat <<'EOF'
chore(release): bump version to <VERSION>

Remove -dev suffix in preparation for release.

Signed-off-by: jay-reynolds <jay.reynolds@github.com>
EOF
)"
git push
```

### A4. Open PR: main → staging

```bash
gh pr create \
  --base staging \
  --head main \
  --title "chore(release): promote main → staging for v<VERSION>" \
  --body "Release promotion PR. Bumps version to <VERSION> and merges all changes from main into staging for CI validation before stable."
```

Monitor CI and merge when green:

```bash
gh pr checks <PR_NUMBER> --watch
gh pr merge <PR_NUMBER> --merge --delete-branch=false
```

Do **not** delete `main` or `staging` — use `--delete-branch=false`.

### A5. Continue with Path B (staging → stable)

---

## Path B: From staging (staging → stable → publish)

### B1. Verify staging CI is green

```bash
gh run list --branch staging --limit 5
```

All checks must pass before promoting. If any are failing, stop and report to the user.

### B2. Open PR: staging → stable

```bash
gh pr create \
  --base stable \
  --head staging \
  --title "chore(release): promote staging → stable for v<VERSION>" \
  --body "Release promotion to stable. Triggers Copybara sync to public repo on merge."
```

The `copybara-dry-run.yml` check runs automatically on PRs targeting `stable` and validates:
- The `copy.bara.sky` allowlist (via `scripts/validate-copybara-allowlist.py`)
- Gitleaks secret scan on files that will be synced
- `pyproject.public.toml` is updated if `pyproject.toml` changed
- No private git dependency URLs in `pyproject.public.toml`
- No private GitHub URLs in `src/`

Wait for all checks to pass:

```bash
gh pr checks <PR_NUMBER> --watch
```

### B3. Merge staging → stable

```bash
gh pr merge <PR_NUMBER> --merge --delete-branch=false
```

The Copybara publish workflow fires automatically on the push to `stable`. Continue with Path C to verify.

---

## Path C: From stable (verify/trigger Copybara sync)

### C1. Confirm stable is at the expected version

```bash
git fetch origin
git log origin/stable --oneline -5
python3 -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('pyproject.public.toml', 'rb') as f:
    d = tomllib.load(f)
print('public version:', d['project']['version'])
"
```

The version must **not** end in `-dev`. The `copybara-publish.yml` workflow has a guard step that aborts if it detects a `-dev` version on stable.

### C2. Check the Copybara workflow run

```bash
gh run list --workflow=copybara-publish.yml --limit 5
```

If a run is in progress, watch it:

```bash
gh run watch <RUN_ID>
```

If no run was triggered (e.g. the push happened in a previous session), trigger manually:

```bash
gh workflow run copybara-publish.yml --ref stable
```

For a first-time bootstrap or after a rollback, use `force_bootstrap`:

```bash
gh workflow run copybara-publish.yml --ref stable --field force_bootstrap=true
```

### C3. Verify the public repo received the sync

```bash
# Check HEAD of public repo main branch
gh api repos/mostlyharmless-ai/watercooler/commits/main --jq '.sha + " " + .commit.message[0:80]'

# Verify the version in the public pyproject.toml matches
gh api repos/mostlyharmless-ai/watercooler/contents/pyproject.toml \
  --jq '.content' | base64 -d | grep '^version'
```

The public commit message will be `Watercooler release sync` (set by `metadata.replace_message` in `copy.bara.sky`).

### C4. Tag the release

```bash
git tag v<VERSION> origin/stable
git push origin v<VERSION>
```

The private repo's `release.yml` workflow auto-creates a GitHub Release on tag push.
Verify both private and public releases were created:

```bash
gh release view v<VERSION>
gh release view v<VERSION> --repo mostlyharmless-ai/watercooler
```

### C5. Bump main to the next dev version

```bash
git checkout main
```

Edit `pyproject.toml` and `pyproject.public.toml`: set version to `<NEXT_VERSION>-dev` (e.g. `0.2.8-dev`).

```bash
git add pyproject.toml pyproject.public.toml
git commit -s -m "$(cat <<'EOF'
chore(release): bump version to <NEXT_VERSION>-dev

Begin development cycle for <NEXT_VERSION>.

Signed-off-by: jay-reynolds <jay.reynolds@github.com>
EOF
)"
git push
```

---

## Troubleshooting

**"no origin sha-1 found" / Copybara fails without GitOrigin-RevId**
First-time bootstrap or after a rollback. Use `force_bootstrap=true` when triggering via workflow dispatch.

**Git user identity error in Copybara run**
The workflow configures `user.name`/`user.email` in the credentials step — check that step ran without error. Lesson learned: GitHub Actions runners have no git identity by default.

**`-dev` version guard fires on stable**
The `Guard against -dev version on stable` step aborts if `pyproject.public.toml` contains a `-dev` version. Ensure the version bump commit landed on `stable` before triggering Copybara.

**`pyproject.public.toml` sync check fails on PR**
`pyproject.toml` changed but `pyproject.public.toml` was not updated. Update it to match (strip any new private deps). The `copybara-dry-run` check blocks merges to `stable` until this is fixed.

**Allowlist validation fails**
Run locally to diagnose:
```bash
python3 scripts/validate-copybara-allowlist.py
```

**`olivr/copybara` wrapper ignores CLI args**
The wrapper reads options from the `COPYBARA_OPTIONS` env var, not CLI positional args. The workflow sets this correctly via `COPYBARA_OPTIONS="${COPYBARA_OPTIONS_VAL}"`. Do not append flags after the image name in `docker run`.

---

## Example Invocations

- `/copybara-publish` — start from scratch, asks for starting point
- `/copybara-publish full` — full flow from main → staging → stable → publish
- `/copybara-publish from-staging` — staging is ready, promote to stable and publish
- `/copybara-publish from-stable` — stable is ready, verify/trigger Copybara sync
