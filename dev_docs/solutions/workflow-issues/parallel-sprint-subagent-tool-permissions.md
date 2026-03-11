---
title: "Parallel-sprint subagents denied Edit/Bash tool permissions despite acceptEdits mode"
problem_type: workflow-issues
component: parallel-sprint skill (Agent tool subagent spawning)
symptoms:
  - Background agents spend entire timeout (30 min) requesting Edit/Bash permissions that never arrive
  - Retrying with mode="acceptEdits" still results in denial within ~45 seconds
  - Worktree has no commits after agent completes — git log unchanged from baseline
  - No error message distinguishes why some agents succeed and others fail
  - Orchestrator session can use Edit/Bash freely; only background agents are affected
tags:
  - parallel-sprint
  - agent-tool
  - permissions
  - edit-tool
  - bash-tool
  - subagent
  - background-agent
  - claude-code
severity: high
affected_components:
  - .claude/skills/parallel-sprint/SKILL.md (Step 10, Step 11)
date: 2026-03-10
related:
  - dev_docs/solutions/process/todo-audit-parallel-agent-verification-pattern.md
  - dev_docs/solutions/process/pr-branch-discipline-push-hygiene.md
---

# Parallel-sprint subagents denied Edit/Bash tool permissions

## Problem

When `parallel-sprint` spawns background subagents via `Agent(run_in_background=True, ...)`,
those subagents cannot use `Edit` or `Bash` tools. The `mode` parameter on the `Agent` call
does not reliably propagate permission grants into the background agent context.

Affected agents silently stall: they emit permission-request text in their output, receive no
reply (they're background), and eventually timeout or return early with an explanation of what
they *would* have done if they had access. The worktree is left clean — no commits, no partial
work.

### Observed during

Sprint C01 (Deprecated Code Purge, 2026-03-10). Four agents launched simultaneously:

| Issue | Result | Notes |
|-------|--------|-------|
| #282 | ✓ success | Only needed Read/Grep for investigation; Edit was incidental |
| #283 | ✓ success | Full Edit/Bash access worked |
| #212 | ✗ timed out (30 min) | Agents repeatedly asked for Edit/Bash, never granted |
| #280 | ✗ timed out (30 min) | Same as #212 |

Retry attempt with `mode: "acceptEdits"` on #212 and #280: still denied within ~45 seconds.

## Root Cause

Background agents spawned via `Agent(run_in_background=True)` run in a sandboxed context where
`Edit` and `Bash` require interactive permission grants. Since background agents have no
interactive channel, every gated tool call results in denial.

The `mode: "acceptEdits"` flag is a hint to the UI layer for **foreground** agents, which can
show a permission dialog and auto-accept. Background agents have no mechanism to receive or act
on that flag — the permission gate fires before any mode context is applied.

Additionally, the parallel-sprint skill's own `allowed-tools` frontmatter in `SKILL.md` does
not list `Edit` or unrestricted `Bash`. Background subagents inherit the orchestrator's
permission context, not a new unrestricted one.

**Bottom line:** `mode: "acceptEdits"` on background agents is a no-op. It does not unlock
`Edit` or `Bash` for background subagents.

## Working Solution

### Orchestrator fallback (what worked)

When agents produce no commits, the orchestrator implements the changes directly:

1. After polling completes, check each worktree for new commits:
   ```bash
   git -C <worktree_path> log --oneline HEAD ^origin/main
   # Empty output = agent made no commits = likely stalled on permissions
   ```

2. For "no commits" worktrees, implement directly in the orchestrator session:
   - Read the target files (`Read` tool)
   - Apply edits (`Edit` tool)
   - Verify with `pytest`
   - Commit with `git commit -s`
   - Push and open PR via `gh pr create`
   - Write `result.json` with `status: "completed_by_orchestrator"`

3. For `#212` the orchestrator directly:
   - Deleted `scripts/migrate_to_structured_layout.py`
   - Removed the `migrate_to_structured_layout()` function from `src/watercooler/fs.py`
   - Removed `TestMigrateToStructuredLayout` class from `tests/unit/test_fs.py`

4. For `#280` the orchestrator directly:
   - Removed 7 deprecated functions + `_get_server_defaults` + `import logging` from
     `src/watercooler/credentials.py`
   - Removed `TestDeprecatedCredentialsFunctions` from `tests/unit/test_config_facade.py`

### When orchestrator fallback is appropriate

Use it for **well-scoped deletions and dead-code removal**:
- Removing deprecated functions
- Deleting a file and its references
- Removing an import, constant, or dead class

Escalate (don't attempt orchestrator fallback) for:
- New feature implementation requiring business logic
- Multi-file refactors with non-obvious ripple effects
- Anything requiring runtime understanding or debugging

## Recommended Skill Update (parallel-sprint Step 11)

Update the result-collection logic in Step 11 to triage by commit state, not just result.json:

```python
# After polling completes for each issue:
def triage_outcome(issue_n, worktree_path, baseline_sha):
    result_path = Path(f".sprint/tmp/sprint-{sprint_id}/issue-{issue_n}/result.json")
    current_sha = run(["git", "-C", worktree_path, "rev-parse", "HEAD"]).stdout.strip()
    has_result = result_path.exists()
    has_commits = current_sha != baseline_sha

    if has_result and has_commits:
        return "success"           # Agent did the work
    if has_result and not has_commits:
        return "partial"           # Agent wrote result but no commits — flag for review
    if not has_result and has_commits:
        return "success_no_result" # Agent committed but forgot result.json — synthesize
    if not has_result and not has_commits:
        return "orchestrator_fallback"  # Stalled — orchestrator should implement
```

For `orchestrator_fallback` cases, the orchestrator implements directly (as above) if the
change is a well-scoped deletion or simple edit. Otherwise it escalates to the user.

## Prevention

### 1. Run Claude Code with `--dangerously-skip-permissions`

This is the correct long-term fix for automated parallel-sprint use. It pre-authorizes all
tools for all agents without interactive prompts:

```bash
claude --dangerously-skip-permissions
# Then run: /parallel-sprint
```

Background agents spawned in this session inherit the bypass and can use `Edit`/`Bash` freely.

### 2. Canary check before spawning

Before launching background agents, run a quick foreground canary that probes write access:

```python
# In Step 9.5 (before Step 10: Spawn agents)
canary_result = Agent(
    prompt="Try: touch /tmp/wc_canary && rm /tmp/wc_canary. Report CANARY_OK or CANARY_BLOCKED.",
    run_in_background=False  # MUST be foreground
)
if "CANARY_BLOCKED" in canary_result:
    # Warn user and offer sequential fallback
    print("WARNING: background agents will not have Edit/Bash access.")
    print("Run with --dangerously-skip-permissions, or use sequential mode.")
```

A foreground canary failure is synchronous and visible. It saves 30 minutes of wasted
background agent timeout.

### 3. Post-spawn git check (lagging indicator)

After each background agent completes, immediately check for commits before waiting for
`result.json`:

```bash
git -C .worktrees/<slug> log --oneline HEAD ^origin/main
# If empty: agent stalled — implement directly now instead of waiting full timeout
```

This can cut recovery time from 30 minutes to seconds.

### 4. Do not use `mode: "acceptEdits"` for background agents

This mode parameter does not work for background agents. Do not retry with it — it wastes
another full timeout. Implement directly or requeue as a foreground agent instead.

## Checklist: When Background Agents Return Without Commits

- [ ] Check worktree: `git -C <worktree> log --oneline HEAD ^origin/main` — empty?
- [ ] Check agent output for "I need permission" or "Edit tool was denied" patterns
- [ ] Decide: is this a well-scoped deletion/removal or complex feature work?
  - Simple removal → implement in orchestrator directly
  - Complex → post a Plan entry to the watercooler thread and escalate to user
- [ ] Do NOT retry with `mode: "acceptEdits"` — it won't help
- [ ] After implementing, write `result.json` with `status: "completed_by_orchestrator"`

## Related

- [`dev_docs/solutions/process/todo-audit-parallel-agent-verification-pattern.md`](../process/todo-audit-parallel-agent-verification-pattern.md) —
  parallel agent verification using read-only agents (unaffected by this limitation)
- [`dev_docs/solutions/process/pr-branch-discipline-push-hygiene.md`](../process/pr-branch-discipline-push-hygiene.md) —
  push discipline for agents that do have write access
- `.claude/skills/parallel-sprint/SKILL.md` Step 10 and 11 — agent spawning and result collection
