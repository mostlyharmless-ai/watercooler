---
title: "Automated PR Review Bot: Multi-Pass Inefficiency from Re-raised Decided Items"
category: process
tags:
  - code-review
  - automated-workflows
  - compound-engineering
  - review-bot
  - process-efficiency
date_solved: "2026-02-26"
updated: "2026-03-04"
pr_numbers:
  - 255
  - 285
review_rounds: 8
symptom: "Bot re-raised previously-decided items across ~8 review rounds, treating each pass as stateless"
root_cause: "Review agent runs on each push with no memory of prior decisions or declined findings"
follow_up_issues:
  - "#258: wire time_key=invalid_at"
  - "#259: total_before_filter signal"
  - "#260: Protocol/kwargs mismatch cleanup"
  - "#261: active_only warning test coverage"
---

# Automated PR Review Bot: Multi-Pass Inefficiency

## Symptom

PR #255 (T2 supersession phase 1) required ~8 review rounds before merge. The same underlying issues were re-raised across multiple passes in different forms, and items explicitly declined by the author were resurfaced without new evidence.

## Root Cause

The review agent (`compound-engineering:workflows:review`) runs fresh on each invocation with no memory of prior decisions. Each pass:

1. Treats the full PR diff as unreviewed
2. Has no record of what the author explicitly declined and why
3. Can comment on code already changed in a later commit (stale diff reading)
4. Fragments findings — e.g., the Protocol mismatch was raised in 3 separate forms across 3 different passes

### Specific re-raised items in PR #255

| Finding | Times raised | Author decision |
|---|---|---|
| LeanRAG `active_only`/`start_time`/`end_time` params | 3× | Explicitly declined: T2 concepts don't belong in T3 backend |
| `except Exception` breadth | 2× | Documented as intentional with inline comment |
| Protocol/`**kwargs` mismatch | 3× (different framing) | Ultimately addressed in final round |
| Test name `excludes_before` vs `excludes_after` | 2× | Fixed in second mention |

## Working Solution

### 1. Run the review agent once on the full diff

Do not invoke the review agent after each fix commit. Run it once on the complete PR diff, collect all findings, fix them in batch, then do a single scoped re-review if needed.

```bash
# Once at PR open / ready-for-review
/workflows:review

# NOT after each commit push — this produces the cascade of re-reviews
```

### 2. Pass prior decisions as explicit context on re-review

When a second review pass is genuinely needed, prepend a `DECIDED_ITEMS` block to the review prompt:

```markdown
## DECIDED_ITEMS — do not re-raise

The following were explicitly discussed and resolved. Do not reframe them:

- **LeanRAG temporal params**: Declined (commit 19a2cf1). T2 bi-temporal concepts
  (active_only, start_time, end_time) do not belong in LeanRAG (T3). Absorbed via **kwargs
  for Protocol conformance only.
- **`except Exception` in facts branch**: Intentional (documented inline). MCP callers
  must always receive structured JSON, never bare exception strings.

If you believe a decided item should be reconsidered, flag it as RECONSIDERATION REQUEST
with new evidence. Do not silently re-raise it.
```

### 3. Scope re-reviews to changed files only

```bash
# Get files changed since last review pass
git diff <last-review-commit>...HEAD --name-only

# Pass only those diffs to the review agent, not the full PR
```

Add explicit prompt instruction:
> "This is a follow-up review. Scope is limited to files changed since commit `<sha>`. Do not re-evaluate unchanged code."

### 4. Classify findings as blockers vs. issue candidates before commenting

Before each round, apply this filter:

**Blocker** (must fix before merge): introduced by this PR, causes incorrect behavior, breaks MCP contract, security issue in changed code.

**Issue candidate** (file and defer): pre-existing code not touched by this PR, style/naming with no behavioral impact, raised ≥2 times without resolution progress, architectural concern requiring separate discussion.

**Hard stop rule**: Any finding raised in ≥2 passes without resolution must be filed as a GitHub issue and marked deferred. The bot must not raise it again in the same PR.

## Prevention Checklist

Before invoking the review agent on a follow-up pass:

- [ ] Has the full diff been reviewed at least once? (If not, this is pass 1 — no prior context needed)
- [ ] Is there a `DECIDED_ITEMS` list from prior rounds to inject?
- [ ] Are you passing only the incremental diff (changed files) rather than the full PR?
- [ ] Have open blockers from the prior pass been fixed? (If not, address those first before re-review)
- [ ] Have you checked whether any "findings" are actually pre-existing code not in the diff?

## Signs a Finding Is Scope Creep (File as Issue, Not Blocker)

- Code predates this PR's first commit (`git log -p --follow` to verify)
- Would require touching files not in the diff
- Same pattern appears in 5+ places across the codebase (systemic, not PR-specific)
- Framed as "consider" or "you might want to" rather than "this will cause"
- For this repo specifically: `graphiti.py` decomposition, `DocumentNode/DocumentChunk` unification, batch summarization — these are explicitly deferred in MEMORY.md and should not resurface as PR blockers

## Minimal Prompt Additions for Immediate Improvement

Add these four lines to the review agent prompt in `compound-engineering.local.md`:

```markdown
1. SCOPE: Review only lines marked + in the diff. Unchanged lines are out of scope.
2. DECIDED_ITEMS: [inject from prior review log] — do not re-raise any item listed here.
3. STOP: If all prior open items are resolved and no new blockers exist, output
   REVIEW_COMPLETE rather than new findings.
4. THRESHOLD: Any finding raised in a prior pass without resolution must be output as
   ISSUE_CANDIDATE (with suggested title), not repeated as a review comment.
```

## Review Bot Prompt Correctness Scoping (PR #285 addendum)

The above patterns address re-raising decided items. A separate problem emerged in PR #285
(documentation-only PR): the review bot produced philosophical commentary, process suggestions,
and hypothetical improvements that added noise without catching actual bugs.

For a **pre-launch project with no production users**, the signal-to-noise ratio matters more
than exhaustive style enforcement. Applied to `.github/workflows/claude-code-review.yml`:

```yaml
# Scoped prompt (applied after PR #285 experience)
Review this PR for correctness bugs only.
Focus on: wrong flags/parameters, broken commands, incorrect API behavior, broken links.
Skip: style preferences, process suggestions, commentary on project maturity,
philosophical guidance about documentation philosophy, compliments.
Be terse.
```

The key principle: match review scope to project phase. Greenfield / pre-launch projects
benefit from correctness-focused review. Exhaustive style enforcement has lower ROI until
the codebase stabilizes and user patterns emerge.

For documentation PRs specifically, "correctness" means:
- Command examples that actually work
- Parameter tables that match implementation
- Config keys that match schema
- Links that resolve

It does NOT mean: opinionated word choices, section ordering preferences, or whether
the project is "ready for users."

## Related

- `dev_docs/solutions/logic-errors/federation-phase1-code-review-fixes.md` — PR #190 had 14 review rounds with similar pattern; pre-dates this solution
- `dev_docs/solutions/process/pr-branch-discipline-push-hygiene.md` — sub-PR anti-pattern + push hygiene from PR #285
- `dev_docs/solutions/docs/documentation-source-verification-pattern.md` — source verification pattern from PR #285
- Issues filed from PR #255: #258, #259, #260, #261
