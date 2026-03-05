---
title: "PR Branch Discipline: Sub-PR Anti-Pattern and Premature Push Prevention"
category: process
tags:
  - workflow
  - pull-requests
  - git-branch
  - code-review
  - compound-engineering
  - automation
date_solved: "2026-03-04"
pr_numbers:
  - 285
  - 289
symptom: "Agent created a new branch + PR instead of pushing fixes to the existing PR branch; multiple premature pushes triggered paid review rounds"
root_cause: "/workflows:work default behavior creates new branches; no explicit branch check before push"
---

# PR Branch Discipline: Sub-PR Anti-Pattern and Premature Push Prevention

## Symptom

While fixing code review comments for PR #285 (`docs/user-docs-refresh`), the agent:

1. Created a new branch (`docs/fix-pr285-review-findings`)
2. Opened a new PR (#289) instead of pushing to PR #285's branch
3. Pushed before the user approved pushing (multiple times)

Each premature push to a PR with an automated review bot triggered a new paid review
cycle. The sub-PR creation required an extra merge step to get fixes onto the correct branch.

User experience: "You created a new PR!? This was supposed to be an update to PR 285"
and "Note that there are also new review comments to address since you clumsily kicked
off another round (which isn't free, by the way)".

## Root Cause

### Sub-PR creation

`/workflows:work` reads the current git branch at startup. If it is not the intended
PR branch, it creates a new branch. The plan file specified `branch: docs/user-docs-refresh`
and `pr: 285`, but the agent was checked out on the wrong branch and created a new one.

### Premature push

The workflow's "ship it" phase pushes and creates a PR. Without an explicit
"don't push until I say so" instruction, the agent follows the default workflow:
implement → commit → push → PR.

## Working Solution

### 1. Verify branch before running `/workflows:work` on existing-PR fixes

```bash
# Before invoking /workflows:work for review comment fixes:
git branch --show-current  # Confirm you are on the PR's branch
git log --oneline -3       # Confirm commits match the PR
```

If not on the correct branch:
```bash
git checkout <pr-branch-name>
```

### 2. Explicitly tell the agent which branch and PR to target

When giving the agent a plan to fix review comments, include in the instructions:

```
"Push these fixes to branch [branch-name] (PR #[number]).
Do NOT create a new branch. Do NOT push until I explicitly say to."
```

Bare-minimum instruction in the plan file or task:
```yaml
branch: docs/user-docs-refresh  # must be present in plan frontmatter
pr: 285                          # must be present; agent should check before pushing
```

### 3. Batch all fixes before pushing

For review comment fixes, the lowest-cost approach is:

1. Fix ALL comments (even across multiple files/phases)
2. Review the changes locally (`git diff`, read changed files)
3. Ask the user "ready to push?" with a summary of what changed
4. Push once, with a comprehensive commit message

The cost of one push with 14 fixes = one review cycle.
The cost of 14 sequential pushes = 14 review cycles.

Example batching pattern:
```bash
# Fix all changes
# ...

# Verify no regressions
grep -n "\-\-source\|\-\-target\|name = \"Claude" docs/*.md

# Stage and commit everything at once
git add docs/ src/watercooler_mcp/resources.py
git commit -s -m "fix(docs): address PR #285 review findings (todos 063–076)"

# Then ask user before pushing
```

### 4. Review CI trigger cost awareness

When a PR has an automated review bot (`.github/workflows/claude-code-review.yml`):

- Every push to the PR branch triggers a new review cycle
- Each cycle costs real money (LLM API calls for the review agent)
- 14 sequential fix-and-push cycles is ~14× the cost of 1 batched push
- Philosophical/style comments in each review add latency with no value

Mitigations (apply to the workflow yml):
- Scope the review bot prompt to correctness bugs only (see "Review Bot Signal-to-Noise" below)
- Trigger reviews only on PR open / explicit re-review label, not on every push
- Consider `if: github.event.action == 'opened' || contains(github.event.pull_request.labels.*.name, 're-review')`

### 5. Review Bot Signal-to-Noise for Pre-Launch Projects

For a project with no production users yet, the review bot's job is to catch correctness
bugs that would break things for early adopters — not to enforce code style, comment on
process choices, or provide philosophical guidance about documentation philosophy.

Example review prompt tightening (applied in `.github/workflows/claude-code-review.yml`
after PR #285):

```yaml
# BEFORE (too broad, produces philosophical commentary)
- Review this PR for quality and correctness

# AFTER (scoped to correctness bugs)
- Review this PR for correctness bugs only.
  Focus on: wrong flags/parameters, broken commands, incorrect API behavior.
  Skip: style preferences, process suggestions, hypothetical improvements,
  commentary about whether the project needs users before docs.
  Be terse. Skip the compliments.
```

This reduced review noise significantly. The "no users" context is relevant: for a
greenfield project, stricter style enforcement has a lower ROI than clear correctness
validation.

## Prevention Checklist

Before running `/workflows:work` on review comment fixes:

- [ ] `git branch --show-current` confirms you are on the PR's branch
- [ ] Plan file has correct `branch:` and `pr:` in frontmatter
- [ ] Instruction to agent explicitly says "do NOT push until I say so"
- [ ] Instruction lists all fixes to make (batch them all in one round)
- [ ] After implementing, review changes before asking user to approve push

Before each push to a PR with a review bot:

- [ ] All planned fixes are committed (no partial fixes)
- [ ] Local verification checks pass (no regressions)
- [ ] User has explicitly said "go ahead and push"

## PR Cost Model

| Approach | Review cycles | Cost multiplier |
|----------|--------------|----------------|
| One push with all 14 fixes | 1 | 1× |
| Sequential fix-and-push per finding | 14 | ~14× |
| Sub-PR then merge to main PR | 2+ | 2–3× |

The batched approach also produces cleaner git history: one commit per phase (P1/P2/P3)
rather than 14 incremental "fix: address finding N" commits.

## Branch Name Reference (watercooler-cloud)

| Branch type | Naming pattern |
|------------|---------------|
| Feature/docs | `docs/<feature>`, `feat/<feature>` |
| Fix | `fix/<description>` |
| PR review fixes | Push to the same branch as the original PR |
| Never | Create `fix/pr-NNN-review-comments` sub-PR |

## Related

- `dev_docs/solutions/process/automated-pr-review-multi-pass-inefficiency.md` — re-raised findings across rounds
- `dev_docs/solutions/docs/documentation-source-verification-pattern.md` — what to verify before each push
- PR #285, PR #289 — the concrete example this solution is drawn from
