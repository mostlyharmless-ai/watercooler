---
title: "Copybara CI Pipeline Bootstrap: Six-Step Failure Chain from Starlark Syntax to Git Identity"
date: "2026-03-05"
category: "process"
tags:
  - copybara
  - github-actions
  - docker
  - starlark
  - git
  - ci-cd
  - bootstrap
  - olivr-copybara
  - sync-pipeline
  - public-release
symptoms:
  - "Starlark syntax error on multi-line string literals in copy.bara.sky"
  - "Docker pull fails with 'tag not found' for olivr/copybara:1.2.5"
  - "Copybara exits with 'Cannot find last imported revision' on first run"
  - "--force and --last-rev flags passed as CLI args are silently ignored by olivr/copybara wrapper"
  - "Docker container exits 0 but runs bash instead of Copybara (false success)"
  - "Transformation exits 2 with 'Transformation was a no-op' when core.replace finds no match"
  - "Copybara destination commit fails: 'user.name and/or user.email are not configured'"
root_causes:
  - "Starlark does not support implicit string concatenation; multi-line strings require explicit + operator"
  - "olivr/copybara uses dated image tags (e.g. 20230129), not semantic version tags"
  - "olivr/copybara wrapper script reads COPYBARA_OPTIONS env var, not CLI args; args after image name are silently discarded"
  - "Docker CMD must be explicitly specified as 'copybara'; omitting it runs the default shell (bash) which exits 0"
  - "core.replace exits 2 when the search pattern is not found; use core.transform([...], ignore_noop = True) to make optional replacements safe"
  - "GitHub Actions runners have no git user.name/user.email configured; Copybara needs git identity for destination commits"
components:
  - "Copybara sync pipeline"
  - "GitHub Actions workflow"
  - "olivr/copybara Docker image"
  - "Starlark (copy.bara.sky)"
  - "git credential and identity configuration"
related_files:
  - ".github/workflows/copybara-publish.yml"
  - "copy.bara.sky"
severity: "high"
time_to_solve: "2-4 hours (six sequential failure modes, each requiring a separate fix-redeploy cycle)"
---

# Copybara CI Pipeline Bootstrap: Six-Step Failure Chain

Setting up the `watercooler-cloud@stable → mostlyharmless-ai/watercooler@main` Copybara sync pipeline
for the v0.2.6 public release involved twelve failed workflow runs spanning six distinct root causes.
Each was a silent or misleading failure. This document captures all six with precise fixes.

## Symptoms

The pipeline failed in the following order during bootstrap:

1. **Starlark parse error on workflow load.** `copy.bara.sky` failed immediately with a syntax error before any git or network operation occurred.

2. **Docker pull failure: image not found.** After fixing the syntax error, the workflow failed at `docker run` with a "tag not found" error because the specified image tag did not exist.

3. **Silent no-op runs returning exit 0.** After fixing the image tag, `docker run` appeared to succeed (exit 0) but Copybara performed no sync. No commits appeared in the destination repository.

4. **Copybara flags had no effect.** Bootstrap flags (`--force`, `--last-rev`) passed as CLI arguments after the image name were silently ignored. The sync still refused to proceed without a prior `GitOrigin-RevId` label.

5. **Exit code 2 on no-op transforms.** When Copybara processed origin snapshots that did not contain the target text for certain `core.replace` transforms, it exited 2 and aborted the sync.

6. **Destination commit rejected: no git identity.** When a destination commit was finally produced, it was rejected because `user.name` and `user.email` were not configured on the GitHub Actions runner.

7. **Push-triggered runs could not bootstrap.** Runs triggered by a `push` event had no `inputs` context and could not pass `--force`, causing them to fail on first run before any `GitOrigin-RevId` existed in the destination.

## Root Causes

**1. Starlark does not support adjacent string literal concatenation.**

Copybara uses a Starlark interpreter (not Python). Starlark does not allow implicit string concatenation of adjacent string literals. Multi-line strings that relied on Python's implicit join-across-lines syntax caused a parse error at startup.

**2. The `olivr/copybara` image uses dated tags, not semantic version tags.**

The image `olivr/copybara:1.2.5` does not exist. The registry uses date-stamped tags of the form `YYYYMMDD`. Using a version number tag produced a Docker pull failure.

**3. The `olivr/copybara` wrapper is a bash script that reads only environment variables.**

The wrapper at `/usr/local/bin/copybara` inside the image is a thin bash script:

```bash
java -jar /opt/copybara/copybara_deploy.jar $COPYBARA_OPTIONS $COPYBARA_SUBCOMMAND $COPYBARA_CONFIG $COPYBARA_WORKFLOW $COPYBARA_SOURCEREF
```

It expands exactly those five environment variable positions and nothing else. Any additional arguments appended after the image name in `docker run` are passed to the container's default entrypoint, not to this wrapper, and are silently dropped.

**4. The default Docker CMD is `bash`, not `copybara`.**

Without an explicit command at the end of the `docker run` invocation, Docker ran the container's default CMD (`bash`), which exited 0 immediately. This produced a false-success run with no work done.

**5. `core.replace` exits 2 when no match is found.**

Copybara's `core.replace` transform is not idempotent across origin snapshots. If the target text is absent (e.g., a comment added in a later commit is not present in the `--last-rev` baseline), Copybara treats it as an error and exits 2. There is no default grace behavior.

**6. GitHub Actions runners have no git identity configured.**

The runner environment provides no `user.name` or `user.email` by default. Copybara mounts `~/.gitconfig` into the container, so the git identity must be written to that file before the container runs. Without it, destination commits fail.

**7. Push-triggered workflow runs have no `inputs` context.**

The `inputs` context is populated only for `workflow_dispatch` events. On `push` events, `inputs.force_bootstrap` and `inputs.last_rev` are empty strings. Bootstrap-only flags cannot be passed in a push-triggered run, making the first-run bootstrap exclusively a manual `workflow_dispatch` operation.

## Solution

### Fix 1: Explicit `+` for multi-line string concatenation in Starlark

Every multi-line string in `copy.bara.sky` must use explicit `+` concatenation.

```python
# BAD: adjacent string literals (Python syntax, not valid Starlark)
before = "      # Note: Using graphiti instead of memory...\n"
         "      # LeanRAG tests are skipped anyway...\n"

# GOOD: explicit + operator
before = "      # Note: Using graphiti instead of memory...\n" +
         "      # LeanRAG tests are skipped anyway...\n"
```

### Fix 2: Use the dated image tag

Replace `olivr/copybara:1.2.5` with `olivr/copybara:20230129`:

```yaml
olivr/copybara:20230129   # correct — dated tag exists
# olivr/copybara:1.2.5   # wrong — tag does not exist
```

### Fix 3: Pass all Copybara flags through environment variables

Bootstrap flags are assembled into `COPYBARA_OPTIONS` before calling `docker run`. The `COPYBARA_CONFIG` and `COPYBARA_WORKFLOW` variables are also set as environment variables, not CLI arguments. Note the explicit `copybara` CMD at the end — without it, Docker runs default `bash` and exits 0 (false success).

```yaml
- name: Run Copybara Sync
  run: |
    COPYBARA_OPTIONS_VAL=""
    if [ "${{ inputs.force_bootstrap }}" = "true" ]; then
      COPYBARA_OPTIONS_VAL="--force"
    fi
    LAST_REV="${{ inputs.last_rev }}"
    if [ -n "$LAST_REV" ]; then
      COPYBARA_OPTIONS_VAL="${COPYBARA_OPTIONS_VAL} --last-rev ${LAST_REV}"
    fi
    docker run --rm \
      -v "$HOME/.git-credentials:/root/.git-credentials:ro" \
      -v "$HOME/.gitconfig:/root/.gitconfig:ro" \
      -v "$GITHUB_WORKSPACE:/usr/src/app" \
      -w /usr/src/app \
      -e COPYBARA_CONFIG="copy.bara.sky" \
      -e COPYBARA_WORKFLOW="default" \
      -e COPYBARA_OPTIONS="${COPYBARA_OPTIONS_VAL}" \
      olivr/copybara:20230129 \
      copybara
```

### Fix 4: Wrap optional transforms in `core.transform([...], ignore_noop = True)`

Any `core.replace` that may legitimately find no matches is wrapped to suppress the exit-2 error:

```python
# BEFORE (fails if comment absent in origin snapshot):
core.replace(
    before = "      # Note: Using graphiti instead of memory...\n" +
             "      # LeanRAG tests are skipped anyway...\n",
    after  = "",
    paths  = glob([".github/workflows/ci.yml"]),
),

# AFTER (silently skips if absent):
# ignore_noop = True: comment may be absent in some origin snapshots
# (e.g. first-run bootstrap with --last-rev pointing to a commit
#  predating this comment).
core.transform([
    core.replace(
        before = "      # Note: Using graphiti instead of memory...\n" +
                 "      # LeanRAG tests are skipped anyway...\n",
        after  = "",
        paths  = glob([".github/workflows/ci.yml"]),
    ),
], ignore_noop = True),
```

### Fix 5: Write git identity before mounting `~/.gitconfig`

```yaml
- name: Configure git credentials for Copybara
  env:
    TOKEN: ${{ steps.app-token.outputs.token }}
  run: |
    echo "https://x-access-token:${TOKEN}@github.com" > "$HOME/.git-credentials"
    git config --global credential.helper store
    chmod 600 "$HOME/.git-credentials"
    # Copybara requires user.name/email for destination commits (read from mounted ~/.gitconfig)
    git config --global user.name "Watercooler Sync Bot"
    git config --global user.email "sync@watercoolerdev.com"
```

### Bootstrap Procedure (one-time)

The very first run must be triggered via `workflow_dispatch` with `force_bootstrap=true`. Push-triggered runs have no `inputs` context and cannot pass `--force`. After the first successful sync writes `GitOrigin-RevId` into the destination HEAD commit, all subsequent push-triggered runs work automatically.

```bash
# One-time bootstrap trigger:
gh workflow run copybara-publish.yml --ref stable -f force_bootstrap=true

# Optional: specify last_rev if destination is partially populated
gh workflow run copybara-publish.yml --ref stable -f force_bootstrap=true -f last_rev=<sha>
```

## Prevention Strategies

### Starlark syntax

- Require explicit `+` for all multi-line string building in `.bara.sky` files.
- Run `copybara validate copy.bara.sky` as a pre-flight before the full sync:
  ```bash
  docker run --rm \
    -v "$PWD:/usr/src/app" -w /usr/src/app \
    -e COPYBARA_CONFIG=copy.bara.sky \
    -e COPYBARA_SUBCOMMAND=validate \
    olivr/copybara:20230129 copybara
  ```

### Docker image tag

- Pin the tag explicitly. Track it in a single `COPYBARA_IMAGE` variable at the top of the workflow file.
- To find the current latest dated tag: browse `https://hub.docker.com/r/olivr/copybara/tags`.

### Flags and CMD

- Never place Copybara flags after the image name. Always use the env-var interface (`-e COPYBARA_OPTIONS=...`).
- Always end the `docker run` command with the explicit `copybara` subcommand.
- Add comments at every `docker run` call explaining the env-var constraint so future editors don't "fix" it.

### No-op transforms

- Default new `core.replace` calls to `ignore_noop = True` unless the text is guaranteed to exist in every origin commit reachable from the baseline.
- Add a comment above each wrapped transform explaining why it may be absent.

### Git identity

- Assert identity is set before `docker run`:
  ```bash
  git config --global user.name || (echo "ERROR: user.name not set"; exit 1)
  git config --global user.email || (echo "ERROR: user.email not set"; exit 1)
  ```

## Pre-Run Validation Checklist

Before triggering a Copybara sync (especially after modifying `copy.bara.sky` or adding new transforms):

**Starlark syntax**
- [ ] All multi-line strings use explicit `+` concatenation
- [ ] `copybara validate` passes (see command above)

**Transforms**
- [ ] Every `core.replace` whose `before` text may be absent is wrapped in `core.transform([...], ignore_noop=True)`
- [ ] New `core.replace` text was copy-pasted from the actual file (not from memory)
- [ ] `python3 scripts/validate-copybara-allowlist.py` passes

**Docker invocation**
- [ ] No Copybara flags appear after the image name — all flags are in `COPYBARA_OPTIONS` env var
- [ ] Explicit `copybara` subcommand appears at the end of the `docker run` line
- [ ] Pinned image tag matches the documented stable tag

**Git identity**
- [ ] `user.name` and `user.email` are set before `docker run`
- [ ] `~/.gitconfig` is mounted read-only into the container

**Credentials**
- [ ] `~/.git-credentials` is written with the GitHub App token before `docker run`
- [ ] `~/.git-credentials` is removed in the `if: always()` cleanup step

**Bootstrap vs incremental**
- [ ] If first run: trigger via `workflow_dispatch` with `force_bootstrap=true`
- [ ] If incremental: verify destination already contains a `GitOrigin-RevId` trailer

**Security scan**
- [ ] `copybara-dry-run.yml` CI check passes on the PR to `stable` before merge
- [ ] `pyproject.public.toml` updated in same PR if `pyproject.toml` changed
- [ ] No private GitHub URLs in `src/**`

## Common Pitfalls

| Symptom | Root Cause | Fix |
|---|---|---|
| Parse error in `copy.bara.sky` | Starlark does not support implicit string concatenation | Use explicit `+` operator on every line |
| Docker pull fails: tag not found | `olivr/copybara` uses date-stamp tags, not semver | Pin to a known date tag (e.g. `20230129`) |
| `--force`/`--last-rev` flags have no effect | `olivr/copybara` wrapper only reads `COPYBARA_OPTIONS` env var; CLI args after the image name are silently dropped | Set flags via `-e COPYBARA_OPTIONS="--force --last-rev <sha>"` |
| CI step exits 0 but destination repo unchanged | Docker ran default `bash` CMD; `copybara` subcommand was omitted | Always end `docker run` with `olivr/copybara:<tag> copybara` |
| Sync fails with exit 2: "Transformation was a no-op" | `core.replace` exits 2 when `before` text is not found in origin snapshot | Wrap optional transforms in `core.transform([core.replace(...)], ignore_noop=True)` |
| Sync fails: "Author identity unknown" | GitHub Actions runners have no global git identity | Set `git config --global user.name/email` before `docker run`; mount `~/.gitconfig` |
| First-ever sync fails: "GitOrigin-RevId not found" | Destination has no prior `GitOrigin-RevId` trailer | Trigger via `workflow_dispatch` with `force_bootstrap=true` |
| Incremental sync replays all history | `--force` used when baseline already exists | Reserve `--force`/`--last-rev` for `workflow_dispatch` bootstrap only |
| Private repo URL leaks into public destination | URL-rewrite transform no-ops silently on old snapshots | Wrap with `ignore_noop=True` AND verify the rewrite fires on a dry-run |
| `pyproject.public.toml` stale after dep change | `pyproject.toml` updated but public variant not updated | `copybara-dry-run.yml` enforces this; keep both files in sync |

## Related Files

**Core pipeline files:**
- `.github/workflows/copybara-publish.yml` — Publish workflow triggered on push to `stable`
- `.github/workflows/copybara-dry-run.yml` — PR gate: allowlist validation, gitleaks scan, public TOML diff check
- `copy.bara.sky` — Starlark sync configuration (allowlist, transforms, destination)
- `scripts/validate-copybara-allowlist.py` — Static validator enforcing allowlist security rules
- `pyproject.public.toml` — Sanitized public variant of pyproject.toml (strips private git deps)
- `.gitleaks.toml` — Suppresses false positives from `.venv`, `__pycache__`, `.serena` during secret scanning

**Planning and design:**
- `dev_docs/brainstorms/2026-02-26-copybara-public-release-sync-brainstorm.md` — Original design decisions (allowlist vs denylist, GitHub App token, SQUASH mode, concurrency)
- `dev_docs/plans/2026-03-04-feat-copybara-stable-to-public-sync-pipeline-plan.md` — Full implementation plan

**Related solution docs:**
- `dev_docs/solutions/security-issues/copybara-allowlist-validator-hardening.md` — 9 security defects found during pipeline plan review (PR #290)
- `dev_docs/solutions/process/automated-pr-review-multi-pass-inefficiency.md` — Multi-round review inefficiency pattern
- `dev_docs/solutions/process/pr-branch-discipline-push-hygiene.md` — Batch-commit strategy to reduce review round cost

## External References

- Copybara source and docs: https://github.com/google/copybara
- olivr Docker Hub: https://hub.docker.com/r/olivr/copybara
- gitleaks: https://github.com/gitleaks/gitleaks
