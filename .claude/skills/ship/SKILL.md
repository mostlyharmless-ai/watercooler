---
name: ship
description: Stage, commit with a detailed conventional-commits message, and push. Use after completing work to ship changes.
allowed-tools:
  - Bash(git *)
  - AskUserQuestion
---

# Ship

Stage, commit, and push current changes with a detailed commit message.

Optional arguments via `$ARGUMENTS`:
- A commit message hint or scope (e.g., `/ship fix memory queue race condition`)
- `--no-push` to skip the push step
- `--amend` to amend the previous commit instead of creating a new one

## Steps

1. **Gather context** (run all three in parallel):
   - `git status` — see untracked and modified files
   - `git diff` and `git diff --staged` — see all changes
   - `git log --oneline -10` — recent commit style reference

2. **Review changes carefully**:
   - Identify what changed and why
   - Flag any files that should NOT be committed (secrets, binaries, generated files)
   - If nothing has changed, tell the user and stop

3. **Stage files**:
   - Stage specific files by name (prefer `git add <file>...` over `git add -A`)
   - Never stage `.env`, credentials, or secrets files — warn the user if they exist
   - If user provided `$ARGUMENTS` hinting at scope, only stage relevant files

4. **Draft commit message** following Conventional Commits and the repo's CLAUDE.md guidelines:
   ```
   <type>(<scope>): <subject>

   <body — explain what changed and why, not just what files were touched>

   <footer — Closes/Fixes #issue if applicable>

   Co-Authored-By: Claude <noreply@anthropic.com>
   ```
   Note: `Signed-off-by` is added automatically by the `-s` flag in step 6 — do not include it in the `-m` string.
   Substitute the running model name in `Co-Authored-By` if the project convention includes it (e.g., `Claude Opus 4.6`).

   **Type**: feat, fix, refactor, test, docs, style, chore
   **Scope**: module or area affected (e.g., mcp, cli, memory, sync, skills)
   **Subject**: imperative, lowercase, no period, under 70 chars
   **Body**: wrap at ~72 cols, explain the "why" not just the "what", reference related issues

5. **Show the user** the proposed commit message and staged files before committing.
   Wait for approval unless the changes are trivially obvious.

6. **Commit** using a HEREDOC for proper formatting:
   ```bash
   git commit -s -m "$(cat <<'EOF'
   <type>(<scope>): <subject>

   <body>

   Co-Authored-By: Claude <noreply@anthropic.com>
   EOF
   )"
   ```
   Note: `-s` adds the Signed-off-by line automatically.
   If `--amend` was requested, use `git commit --amend -s -m` instead.
   If the previous commit already has a `Signed-off-by`, omit `-s` from the amend to avoid duplicates.

7. **Verify** the commit succeeded:
   ```bash
   git log --oneline -3
   ```
   If a pre-commit hook fails, fix the issue, re-stage, and create a NEW commit (never `--amend` unless explicitly requested).

8. **Push** (unless `--no-push` was specified):
   ```bash
   git push
   ```
   - If the branch has no upstream, use `git push -u origin <branch>`
   - If push is rejected (non-fast-forward), inform the user — never force-push without explicit permission
   - Never force-push to main/master

9. **Report** the final result:
   - Commit SHA and message
   - Push status
   - Any warnings or issues encountered

## Example Invocations

- `/ship` — commit and push all current changes
- `/ship add serena-init skill` — hint at the commit subject
- `/ship --no-push` — commit only, skip push
- `/ship --amend` — amend the previous commit
