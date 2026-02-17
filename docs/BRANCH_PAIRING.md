# Watercooler Branch Pairing Contract

This document defines a simple, durable pairing between a code repository and its watercooler threads repository. It enables tight linkage between code changes and conversations with minimal moving parts.

## Principles
- 1:1 Repositories: Each code repo pairs with exactly one threads repo.
- 1:1 Branches: Each code branch has a same-named branch in the threads repo.
- Ground Truth: Every thread entry commit records the code branch and exact commit SHA.

## Pairing
- Pair: `code: <org>/<repo>` ↔ `threads: <org>/<repo>-threads`
- Branch mirror: `code:<branch>` ↔ `threads:<branch>`
- Topics: Files at the root of the threads branch (one file per topic)

## Client Behavior (Local MCP)
### Read/List
1) Detect current code branch (if inside a git repo)
2) Checkout/create the same branch in the threads repo
3) Pull latest, then list/read

### Write (say/ack/handoff/set_status)
1) **Validate branch pairing** - Automatically checks that code and threads repos are on matching branches
2) Ensure a same-named branch exists in the threads repo
3) Append entry and commit
4) Push with rebase + retry on rejection
5) Include the following footers in the commit message:
```
Code-Repo: <org>/<repo>
Code-Branch: <branch>
Code-Commit: <short-sha>
Watercooler-Entry-ID: <ULID>
Watercooler-Topic: <topic>
```

## Why this is effective
- Clean separation: No CI noise in the code repo; threads can be chatty
- Strong linkage: Branch name + commit SHA make relationships verifiable
- Natural workflow: Checking out a code branch naturally scopes threads
- Minimal friction: No hosted infra needed; just consistent git discipline

## Authoring Rules
- Topic slugs are flat (e.g., `feature-auth-refactor`)
- Entries include a visible `Spec: <value>` (session specialization) and a Role aligned to the spec
- Include code pointers in body when relevant (branch, commit, PR)

## Closure Strategy
- On code PR merge:
  - Option A (simple): Post a Closure entry on the threads branch referencing the merged PR
  - Option B (optional): Merge the threads branch into `threads:main` with a summary; only if you want a canonical main log

## Edge Cases
- Not in a git repo: default to `threads:main`
- Code branch renamed: first write auto-creates the new threads branch; history remains
- Heavy concurrency: start with one file per topic and `*.md merge=union`; switch to per-entry files only if conflicts are frequent
- Threads repo missing: enable `WATERCOOLER_THREADS_AUTO_PROVISION` and provide
  `WATERCOOLER_THREADS_CREATE_CMD` so the MCP server can run your approved
  provisioning command when the initial clone fails. Otherwise, create the
  `<repo>-threads` repository manually before writing.

## Real-World Example: v0.1.0 Launch (November 2025)

This section documents the actual execution of the branch pairing protocol during the watercooler-cloud v0.1.0 open source launch.

### Context
- **Code Repo**: `mostlyharmless-ai/watercooler-cloud`
- **Threads Repo**: `mostlyharmless-ai/watercooler-cloud-threads`
- **Branch Pair**: `open-source-prep` (both repos)
- **Goal**: Merge both branches to `main` following branch pairing protocol

### Execution Timeline

**Phase 1: Code Repo Merge**
1. PR #7 created: `open-source-prep` → `main` in watercooler-cloud
2. All CI tests passing (81 tests across Python 3.10-3.12)
3. PR merged via GitHub UI
4. Tagged v0.1.0 on merged main (commit 383d50b)
5. Repository made public

**Phase 2: Threads Repo Merge**
- **Initial assumption**: threads already merged
- **Reality**: `threads:main` had 1 file, `threads:open-source-prep` had 21 files
- **Critical learning**: Merging code branch does NOT automatically merge threads branch
- **Action required**: Explicit manual merge of threads branch

### Technical Details

**Command that worked:**
```bash
cd /path/to/watercooler-cloud-threads && \
  git checkout main && \
  git pull origin main && \
  git merge open-source-prep --no-ff -m "Merge open-source-prep threads to main following branch pairing protocol" && \
  git push origin main
```

**Why this approach:**
- **Absolute path with chained commands**: Working directory can reset between separate commands in some tool contexts
- **`--no-ff` flag**: Preserve branch history even when fast-forward possible
- **Explicit message**: Document protocol adherence in commit message

**Merge Result:**
- Commit: 104eab8
- Files merged: 21 threads
- Insertions: 8,696 lines
- Auto-merged: `open-source-launch.md` (updated on both branches)
- Merge strategy: `ort` (default, worked well)

### Key Learnings

1. **Branch pairing is not automatic** - Merging code branch does NOT trigger threads branch merge. Must explicitly merge threads branch separately.

2. **Timing matters** - Merge threads branch immediately after code branch to keep repos synchronized and prevent confusion.

3. **Working directory gotchas** - Individual `cd` commands may not persist across tool invocations. Solution: Use absolute paths + chained commands with `&&`.

4. **Merge message is documentation** - Explicit mention of "branch pairing protocol" helps future maintainers understand the why.

### Best Practices from This Execution

**When merging code branch → main:**
1. Merge code PR
2. Tag release on code repo (if applicable)
3. **Immediately** merge corresponding threads branch
4. Verify both repos show merged state

**Merge command template:**
```bash
# In threads repo
git checkout main
git pull origin main
git merge <branch-name> --no-ff -m "Merge <branch-name> threads to main following branch pairing protocol"
git push origin main
```

**Verification checklist:**
- [ ] Code branch merged to main
- [ ] Threads branch merged to main
- [ ] Both repos pushed to remote
- [ ] File counts match expectations
- [ ] Git log shows merge commits on both repos

## Branch Sync Enforcement

**Automatic Validation**: All write operations (`say`, `ack`, `handoff`, `set_status`) automatically validate branch pairing before execution. If branches don't match, the operation is blocked with a clear error message and recovery steps.

**Branch Sync**: Branch pairing is enforced automatically by the write-path
middleware. All write operations run preflight checks that detect mismatches
and auto-remediate when safe. Use `watercooler_health(code_path=".")` to
inspect current parity status.

**Enforcement Rules**:

1. **Strict Validation**: By default, write operations block if branches don't match
2. **Branch Deletion Safeguards**: Cannot delete threads branch with OPEN threads (unless `force=True`)
3. **Automatic Sync**: Branch lifecycle operations can be synchronized using `sync_branch_state` tool
4. **Recovery Tools**: Use `recover_branch_state` to diagnose and fix inconsistencies

**Common Scenarios**:

- **Branch mismatch detected**: The preflight auto-remediation will attempt to sync branches on the next write operation
- **Git state issues**: Check `watercooler_health(code_path=".")` for diagnostic details
- **Threads behind origin**: The preflight system automatically pulls with rebase when safe

## Auto-Remediation System

The MCP server includes an auto-remediating preflight state machine that runs before every write operation (`say`, `ack`, `handoff`, `set_status`). This system automatically fixes common branch parity issues while ensuring safety through neutral-origin guarantees.

### Design Principles

- **Neutral Origin**: No force-push, no history rewrites, no auto-merge to main
- **Per-Topic Locking**: Serializes concurrent writes to prevent race conditions
- **State Persistence**: Tracks parity state in `branch_parity_state.json`
- **Fail-Safe**: Blocks on issues that require manual intervention

### Parity States

The preflight system tracks these states:

| State | Description | Auto-Fix? |
|-------|-------------|-----------|
| `clean` | Branches are aligned, no action needed | N/A |
| `pending_push` | Commit made, awaiting push | Yes - push with retry |
| `branch_mismatch` | Code and threads on different branches | Yes - checkout/create |
| `main_protection` | Branch mismatch involving main (see below) | Partial - see details |
| `code_behind_origin` | Code repo is behind origin | No - manual pull required |
| `remote_unreachable` | Cannot reach git remote | No - retry later |
| `rebase_in_progress` | Git rebase not completed | No - abort/continue manually |
| `detached_head` | Code repo in detached HEAD state | No - checkout a branch |
| `diverged` | Threads is behind origin | No - use `reconcile_parity` |
| `needs_manual_recover` | Complex issue detected | No - use recover tools |
| `orphan_branch` | Threads branch exists but code branch deleted | No - use sync tools |
| `error` | Unexpected error occurred | No - check logs |

**Note on `main_protection`**: This state covers two scenarios:
1. **Forward** (code=feature, threads=main): Auto-fixed by creating/checking out threads feature branch
2. **Inverse** (code=main, threads=feature): Blocks - user must decide whether to checkout code to feature or merge threads to main

### Auto-Remediation Behaviors

When `WATERCOOLER_AUTO_BRANCH=1` (default), the preflight automatically:

1. **Branch Mismatch → Checkout/Create**
   - If threads branch exists: `git checkout <branch>`
   - If threads branch missing: `git checkout -b <branch>` from current position

2. **Main Protection (Forward) → Create Feature Branch**
   - Detects when code is on a feature branch but threads would write to main
   - Creates the matching threads branch before proceeding

3. **Pending Push → Push with Retry**
   - After commit, pushes to origin
   - On rejection: pulls with rebase, then retries push
   - Configurable retry limit (default: 3 attempts)

4. **Fetch Before Check**
   - Optionally fetches from origin before running checks
   - Ensures state reflects remote reality

### Blocking Behaviors (Require Manual Intervention)

The following states block writes and require explicit action:

1. **Main Protection (Inverse) → Block**
   - Detects when code is on main but threads is on a feature branch
   - Blocks to prevent entries with incorrect `Code-Branch` metadata
   - User must either checkout code to the feature branch or merge threads to main

2. **Threads Behind Origin → Block (auto-remediation pulls)**
   - Detects when threads repo is behind origin (another agent pushed commits)
   - Blocks to prevent auto-pulling changes that may conflict
   - Auto-remediation pulls with rebase when safe; retry the write operation

3. **Code Behind Origin → Block**
   - Detects when code repo is behind origin
   - Blocks because we never mutate the code repo
   - User must `git pull` in the code repo manually

### State Persistence

The system maintains state in `.wc-parity/branch_parity_state.json`:

```json
{
  "status": "clean",
  "last_check_at": "2025-01-15T10:30:00Z",
  "code_branch": "feature-auth",
  "threads_branch": "feature-auth",
  "actions_taken": ["Checked out threads branch 'feature-auth'"],
  "pending_push": false,
  "last_error": null
}
```

This file is git-ignored and local to each clone.

### Per-Topic Locking

To prevent concurrent writes from corrupting thread files, the system uses advisory file locks:

- Lock files stored in `.wc-locks/<topic>.lock`
- 30-second timeout for acquiring locks
- 60-second TTL to prevent stale locks
- Topic names with `/` are sanitized (e.g., `feature/auth` → `feature_auth.lock`)

### Health Reporting

The `watercooler_health` tool now includes branch parity status:

```
Branch Parity:
  Status: clean
  Code Branch: feature-auth
  Threads Branch: feature-auth
  Pending Push: False
  Code Ahead/Behind Origin: 0/0
  Last Check: 2025-01-15T10:30:00Z
```

### Blocking Scenarios

The preflight blocks (does not auto-fix) when:

1. **Detached HEAD**: Cannot determine which branch to target
2. **Code Behind Origin**: Risk of losing remote changes
3. **Rebase in Progress**: Git state must be resolved first
4. **Diverged History**: Requires explicit merge/rebase decision
5. **Remote Unreachable**: Cannot verify or push state

When blocked, the error message includes:
- The detected issue
- Specific recovery steps
- Which MCP tool can help (e.g., `recover_branch_state`)

### Offline / Remote Unreachable Policy

When the git remote is unreachable (network issues, VPN disconnected, etc.):

**Default Behavior**: Write operations are **blocked**.

**Rationale**: Without remote access, the preflight cannot:
- Verify the current state of origin
- Push commits after write operations
- Detect divergence or conflicts with other agents

**Error Message**: `"Cannot reach origin for either repository"`

**Recovery**:
1. Restore network connectivity
2. Retry the write operation
3. If commits were made locally before disconnect, retry the write operation — the preflight system will push pending commits

**Future Enhancement** (not implemented): Opt-in local-only mode for offline work with explicit sync on reconnect.

### Force-Push Detection

Force-push scenarios are detected through **divergence detection**: when the local threads branch is both ahead AND behind origin, or when it's behind origin after a remote force-push.

**Detection Mechanism**:
- Preflight checks `threads_behind_origin` count
- If `threads_behind > 0`, status is set to `DIVERGED`
- This covers both regular divergence and force-push scenarios

**Behavior**: Write operations are **blocked** with status `diverged`.

**Error Message**:
```
Threads branch is N commits behind origin.
```

**Recovery**: The preflight auto-remediation pulls threads with rebase and
retries the push. If auto-remediation cannot resolve the divergence, manual
git intervention may be needed.

**Note**: The system never force-pushes (neutral origin principle). If remote was force-pushed, the local agent must explicitly recover.

### Async Path Scope (Known Limitation)

> ⚠️ **Important**: The async write path has different reliability guarantees than
> the synchronous path. Choose the appropriate mode based on your consistency needs.

The async write path (`_with_sync_async`) currently uses the legacy push mechanism
rather than `push_after_commit()` with rebase-on-reject retry. This is a known
architectural limitation that affects how push failures are reported.

**Sync vs Async Write Paths**:

| Aspect | Sync Path | Async Path |
|--------|-----------|------------|
| Push timing | Immediate, blocking | Background, non-blocking |
| Push mechanism | `push_after_commit()` with rebase+retry | `push_pending()` with basic retry |
| Parity state update | Updated after push completes | Not updated after background push |
| Failure visibility | Immediate exception | May be silent; check queue status |
| Use case | Critical writes, ball handoffs | High-throughput, latency-sensitive |

**Current Async Behavior**:
- Commits locally and enqueues for background push via `AsyncPushWorker`
- Background worker uses `push_pending()` which has basic retry logic (5 attempts)
- Parity state file is **not** updated after background push completes or fails
- Push failures are logged but do not raise exceptions to the caller

**Implications**:
- Async writes may not immediately reflect push failures in parity state
- State file may show `pending_push=False` even when background push failed
- CLI parity output may be stale until next sync write or manual reconcile
- In high-contention scenarios, async pushes may queue up

**When to Use Each Path**:
- **Use sync** (`WATERCOOLER_SYNC_MODE=sync`): Ball handoffs, critical decisions,
  closure entries, or when immediate confirmation of remote persistence is required
- **Use async** (default): Regular notes, high-frequency updates, or when latency
  is more important than immediate push confirmation

**Workarounds for Async Limitations**:
- Use `watercooler_sync(action='status')` to check actual async queue state
- Use `watercooler_sync(action='now')` to flush the queue and push immediately
- Retry the write operation — the preflight system will sync and update parity state
- Set `priority_flush=True` on critical writes to flush queue after that entry

**Future Enhancement**: Upgrade async path to use unified `push_after_commit()`
mechanism and update parity state on background push completion (tracked as a
post-v1.0 improvement).

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WATERCOOLER_AUTO_BRANCH` | `1` | Enable auto-remediation |
| `WATERCOOLER_PARITY_FETCH_FIRST` | `1` | Fetch from origin before checks |
| `WATERCOOLER_PARITY_MAX_RETRIES` | `3` | Push retry attempts |

## CLI Commands

**Branch Management Commands:**

- `watercooler check-branches` - Comprehensive audit of all branches across repo pair
- `watercooler check-branch <branch>` - Validate pairing for specific branch
- `watercooler merge-branch <branch>` - Merge threads branch to main (with OPEN thread warnings)
- `watercooler archive-branch <branch>` - Close OPEN threads, merge to main, then delete branch

**Examples:**

```bash
# Audit all branches
watercooler check-branches

# Check specific branch
watercooler check-branch feature-auth

# Merge threads branch after code PR merged
watercooler merge-branch feature-auth

# Archive abandoned branch
watercooler archive-branch feature-experimental --abandon --force
```

## Related Documentation
- [Graph Sync](GRAPH_SYNC.md) - Baseline graph synchronization contract
- [Threads Repo Lifecycle](THREADS_REPO_LIFECYCLE.md) - Repository bootstrap and branch states
- [Configuration](CONFIGURATION.md) - Environment variables and settings
