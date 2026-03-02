---
title: "Todo Audit: Parallel agent team verification of pending todo completion state"
category: process
tags:
  - todos
  - agent-native
  - parallel-agents
  - knowledge-management
  - process
  - compound-engineering
symptom:
  - "Pending todo files exist for problems already fixed in the codebase"
  - "Todo count inflated with completed work, obscuring genuine backlog"
  - "Time wasted re-investigating issues already resolved"
date_solved: "2026-03-02"
todos_audited: 27
todos_found_complete: 22
todos_genuinely_pending: 5
---

# Todo Audit: Parallel Agent Team Verification

## Symptom

27 pending todos existed in the `todos/` directory. Suspicion: most were already completed in the
codebase — implemented during PR work but never marked done. The todo files had drifted from reality.

Manual review of 27 files against a codebase spread across multiple modules would take significant time.

## Root Cause

Todo files are written at review time (often during code review rounds) but are never systematically
reconciled with the code that implements them. The natural flow:

1. Code review identifies issue → todo created
2. PR author fixes the issue in a subsequent commit
3. Todo file is never updated — stays `pending` indefinitely

Over time, pending todo count grows while the actual backlog shrinks. The signal-to-noise ratio
of the todo list degrades.

## Working Solution

### Parallel Agent Team Verification

Run a parallel agent team where each agent is responsible for one logical group of todos. Each agent:

1. Reads every pending todo in its group (file name, status, problem description, acceptance criteria)
2. Checks the relevant source files using code search or direct reads
3. Determines: **COMPLETE** (already fixed), **PENDING** (not done), or **PARTIAL**
4. Returns a structured report with evidence (quoted lines from actual files)

The orchestrator collects all reports, then:
- Renames complete files (`pending` → `complete` in filename)
- Updates the `status:` frontmatter field

### Grouping Strategy

Group todos by the PR or feature area they belong to. This keeps each agent's investigation
focused on a coherent set of files:

```
Agent 1: todos/001-011  →  issue-ranker skill (SKILL.md, scripts/*.py)
Agent 2: todos/041-045  →  federation PR #190 follow-ups
Agent 3: todos/079-089  →  memory dedup PR #245 follow-ups
```

Three agents ran in parallel; investigation complete in ~80 seconds.

### Prompt Template for Each Agent

```
Investigate whether todos [IDs] have been addressed. For each todo:

1. Read the todo file to understand: problem, affected files, acceptance criteria
2. Check current state of the affected files
3. Return: COMPLETE / PENDING / PARTIAL with evidence (quoted lines)

[List each todo with its key acceptance criterion and the file to check]
```

Key prompt requirements:
- Specify exact file paths to check (agents work faster with directed searches)
- Ask for evidence, not just a verdict (prevents false positives)
- Group by coherent theme so agents have focused context

### Batch File Updates

After collecting reports, update files in one coordinated pass:

```bash
# Rename files (pending → complete in filename)
for f in [list]; do
  mv "$f" "${f/pending/complete}"
done

# Update status field in all renamed files
for f in [list]; do
  sed -i 's/^status: pending$/status: complete/' "$f"
done
```

### Results from 2026-03-02 Audit

| Group | Todos | Complete | Pending | Rate |
|-------|-------|----------|---------|------|
| Issue-ranker (001-011) | 11 | 11 | 0 | 100% |
| Federation (041-045) | 5 | 0 | 5 | 0% |
| Memory dedup (079-089) | 11 | 11 | 0 | 100% |
| **Total** | **27** | **22** | **5** | **81%** |

The 5 genuinely pending todos (all federation follow-ups) were confirmed by code inspection showing
the original issues still present in the source files.

## Prevention Strategies

### 1. Mark Todos at PR Merge Time

When a PR closes issues from the todo list, update the todo files in the same PR. Add to the PR
checklist:

```markdown
- [ ] Todos addressed by this PR marked complete in todos/
```

### 2. Regular Audit Cadence

Run the parallel audit before any "what should we work on next?" planning session. It takes ~2
minutes with a 3-agent team and immediately reveals the true backlog size.

### 3. Close Todos from Git Commit Messages

Consider a convention: if a commit message references a todo ID, a pre-commit hook or CI check
can verify the corresponding todo file has been updated.

### 4. Keep Acceptance Criteria Specific

Todos with vague acceptance criteria are harder to audit. The clearer the "what to check" in
the todo file, the faster agents can verify completion:

```markdown
# Good (specific, checkable):
## Acceptance Criteria
- [ ] `import hashlib` is at module level in memory_sync.py (not inside loop)

# Bad (vague):
## Acceptance Criteria
- [ ] Code is cleaner
```

## When to Use This Pattern

- Before sprint planning or backlog grooming sessions
- After a burst of implementation work where todo tracking may have lagged
- When the `pending` todo count seems higher than expected given recent work
- When inheriting a codebase that has accumulated a large backlog of unverified todos

## Scaling

For larger todo sets (50+), add more agents or split into finer-grained groups. The pattern scales
linearly — each agent handles ~10–15 todos effectively. Beyond 15 per agent, context window
pressure may reduce accuracy.

For very large sets, consider a two-phase approach:
1. Phase 1: Each agent reads todos only (no code checking) and sorts by "likely complete" vs "likely pending" based on timestamps, PR references, and content
2. Phase 2: Agents investigate only the "likely complete" set, skipping clearly-open items
