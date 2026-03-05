---
title: "fix: resolve code review findings from PR #285 (todos 063–076)"
type: fix
status: completed
date: 2026-03-03
branch: docs/user-docs-refresh
pr: 285
---

# Fix Docs Review Findings 063–076

## Overview

14 issues were identified by multi-agent review of PR #285
(docs/user-docs-refresh). This plan addresses all of them in priority order:
3 P1 accuracy bugs that block merge, 7 P2 issues that should ship with the PR,
and 4 P3 polish items.

All changes are confined to documentation and the `resources.py` agent
instructions resource. No Python library code is modified.

---

## Affected files

| File | Todos |
|---|---|
| `docs/TROUBLESHOOTING.md` | 063, 075 |
| `docs/CONFIGURATION.md` | 064 |
| `docs/TOOLS-REFERENCE.md` | 065, 067, 068, 074, 076 |
| `docs/QUICKSTART.md` | 066 |
| `src/watercooler_mcp/resources.py` | 069, 070, 071, 072, 073 |

---

## Phase 1 — P1 fixes (blocks merge)

### 063 · Migration script wrong invocation in TROUBLESHOOTING.md

**File:** `docs/TROUBLESHOOTING.md`

**Problem:** Two sections document the migration script with `--source` /
`--target` flags that do not exist. The script uses positional arguments:
`code-repo` first, then `threads-repo`. The issue also has a duplicate section.

**Target state — Issue #10 "Migration from separate threads repository" after fix:**

The current Fix block (a single wrong script invocation + loose prose) should become
a numbered step list that also absorbs the two unique items from the standalone
section before that section is removed:

```
**Fix:**

1. **Dry run** (default — shows what would be migrated without changing anything):
   ```bash
   python scripts/migrate_to_orphan_branch.py /path/to/code-repo /path/to/threads-repo
   ```

2. **Execute** once the dry-run output looks correct:
   ```bash
   python scripts/migrate_to_orphan_branch.py /path/to/code-repo /path/to/threads-repo --execute
   ```

3. **Verify** the migration:
   ```python
   watercooler_health(code_path=".")
   ```

4. **Clean up config:** Remove any `threads_suffix` or `threads_pattern` settings
   from `config.toml` — these are not needed with the orphan-branch model.

5. **Archive the old repo** once migration is confirmed (the script does not delete it).
```

**Remove the standalone "Migration guide: orphan-branch model" section** (lines ~250–276)
entirely — all its content is now covered by the updated Issue #10.

---

### 064 · `[mcp.git]` uses `name` instead of `author` in CONFIGURATION.md

**File:** `docs/CONFIGURATION.md`

**Problem:** The `[mcp.git]` example uses `name = "Claude Code"` but the
schema field is `author` (`config_schema.py:69`). The key `name` is silently
ignored by the TOML parser, so commits use the fallback identity.

**Fix:** Change the example block:

```toml
# Before
[mcp.git]
name = "Claude Code"
email = "claude@example.com"

# After
[mcp.git]
author = "Claude Code"
email = "claude@example.com"
```

---

### 065 · `watercooler_ack` documents `role` param that doesn't exist

**File:** `docs/TOOLS-REFERENCE.md`

**Problem:** The `watercooler_ack` parameter table includes a `role` row.
`_ack_impl` in `thread_write.py` has no `role` parameter. Agents supplying
`role=` to `ack` will get an unexpected-keyword-argument error.

**Fix:** Remove the `role` row from the `watercooler_ack` parameter table.
Add a note below the table:

> **Tip:** To attribute an entry to a specific role, include `Spec: <role>` as
> the first line of the `body` field. Role tracking in thread history is
> achieved via the `Spec:` marker, not a separate parameter.

---

## Phase 2 — P2 fixes (should ship with PR)

### 066 · `watercooler --version` doesn't exist in QUICKSTART.md

**File:** `docs/QUICKSTART.md`

**Problem:** Step 1 "Verify" calls `watercooler --version`. The CLI has no
`--version` flag. Users running this step immediately hit an error and may
conclude installation failed.

**Fix:** Replace:
```bash
watercooler --version
```

With:
```bash
watercooler --help
```

---

### 067 · `watercooler_say` MCP table documents `status` param that doesn't exist

**File:** `docs/TOOLS-REFERENCE.md`

**Problem:** The MCP `watercooler_say` parameter table includes a `status` row.
`_say_impl` has no `status` parameter. The `--status` flag exists on the CLI
command but is absent from the MCP tool. Agents passing `status=` to `say`
will get an error.

**Fix:** Remove the `status` row from the `watercooler_say` parameter table.
Add a note:

> **Note:** To update thread status, call `watercooler_set_status` separately
> after `say`.

---

### 068 · `watercooler_memory_task_status` says "No parameters" — wrong

**File:** `docs/TOOLS-REFERENCE.md`

**Problem:** The `watercooler_memory_task_status` section says "No parameters."
The implementation (`tools/memory.py:1312`) has three optional parameters.

**Fix:** Replace "No parameters." with the full parameter table:

| Parameter | Type | Required | Description |
|---|---|---|---|
| `task_id` | string | no | Task ID to check. Omit for queue summary. |
| `recover` | bool | no | If `true`, reset stale "running" tasks to "pending" (default: false) |
| `retry_dead_letters` | bool | no | If `true`, move dead-letter tasks back to queue (default: false) |

**Example:**
```python
# Queue summary
watercooler_memory_task_status()

# Check a specific task
watercooler_memory_task_status(task_id="01ABCDEF...")

# Recover stale tasks
watercooler_memory_task_status(recover=True)
```

---

### 069 · Thread auto-creation not documented in `resources.py`

**File:** `src/watercooler_mcp/resources.py`

**Problem:** The Writing entries section of the agent instructions doesn't
mention that `say` creates the thread automatically if it doesn't exist.
Agents may unnecessarily call `init-thread` first, or may avoid `say` on
unfamiliar topics.

**Fix:** In `resources.py`, locate the `## Writing entries` section (the block that
starts with `watercooler_say(...)`). Add one sentence directly after the closing
code fence:

> In local (stdio) mode, `say` creates the thread automatically if it does not
> exist — no prior `init-thread` call is required.

Qualifier required: in hosted mode, `say_hosted` uses `create_if_missing=False`
by default and raises `ThreadNotFoundError` if the thread is absent. The
resources.py already scopes itself to local stdio mode ("local stdio mode (the
standard new-user setup)"), so this qualifier is consistent with the document's
existing framing.

---

### 070 · `watercooler_whoami` missing from `resources.py` Identity section

**File:** `src/watercooler_mcp/resources.py`

**Problem:** The Identity section tells agents to set `agent_func` but doesn't
mention `watercooler_whoami()` as the way to verify resolved identity.

**Fix:** Add one line at the end of the Identity section:

> To verify your resolved identity, call `watercooler_whoami()` — no parameters required.

---

### 071 · `ack` ball semantics parenthetical misleads in `resources.py`

**File:** `src/watercooler_mcp/resources.py`

**Problem:** The ball pattern section describes `ack` as `"still working" /
acknowledgement`. The phrase "still working" implies the caller must be holding
the ball. In fact, `ack` does not require ball ownership — any agent can call
it regardless of who holds the ball.

**Fix:** Change:
```
- **`ack`** — add an entry without changing ball ownership ("still working" / acknowledgement)
```

To:
```
- **`ack`** — add an entry without changing ball ownership; does not require holding the ball
```

---

### 072 · Utility tools sentence over-generalizes in `resources.py`

**File:** `src/watercooler_mcp/resources.py`

**Problem:** The Identity section says:
> Utility tools (`whoami`, `reindex`, `daemon_status`) accept neither.

This is accurate for those three tools but agents may apply the rule to all
"utility-like" tools, including `baseline_graph_stats`, `baseline_sync_status`,
and `access_stats`, which DO require `code_path`.

**Fix:** Change to name the three tools explicitly:
```
`watercooler_whoami`, `watercooler_reindex`, and `watercooler_daemon_status` accept neither.
```

---

## Phase 3 — P3 fixes (polish)

### 073 · Add one-line stubs for missing tools in `resources.py`

**File:** `src/watercooler_mcp/resources.py`

**Problem:** Several tools are not mentioned in `resources.py` at all:
- `watercooler_get_thread_entry_range`
- `watercooler_find_similar`
- `watercooler_federated_search`
- `watercooler_get_entry_provenance`

Agents without filesystem access can't read TOOLS-REFERENCE.md to discover them.

**Fix (Option A — recommended):** Add stubs in two places in `resources.py`:

**In `## Reading threads efficiently`** (after the existing Stage 3 `get_thread_entry` example):
```python
# Fetch a contiguous range of entries in one call
watercooler_get_thread_entry_range(topic="feature-auth", start_index=0, end_index=9, code_path=".")
```

**In `## Memory and search`** (after the existing `smart_query` block):
```python
# Semantic neighbor lookup (T1 embeddings required)
watercooler_find_similar(entry_id="<ulid>", code_path=".")

# Cross-repo keyword search (federation config required)
watercooler_federated_search(query="...", code_path=".")

# Bidirectional T1↔T2 provenance lookup
watercooler_get_entry_provenance(episode_uuid="...", code_path=".")
```

---

### 074 · `watercooler_search` section header prereq inconsistency

**File:** `docs/TOOLS-REFERENCE.md`

**Problem:** The safety table (line 87) correctly says:
```
| `watercooler_search` | read-only | none (T2 for `mode="facts"`) |
```

But the section header (line 404) says:
```
Prerequisites: none
```

**Fix:** Change the section header:
```
Unified search across entries, entities, episodes, and temporal facts. | Safety: read-only | Prerequisites: none (T2 for mode="facts")
```

---

### 075 · Add clarifying note for `uv cache clean` syntax in TROUBLESHOOTING.md

**File:** `docs/TROUBLESHOOTING.md`

**Problem:** The "Stale install after upgrade" fix uses `uv cache clean
watercooler-cloud`. The todo required verification that positional package arguments
are supported before treating the command as correct.

**Verification (done):** `uv cache clean --help` confirms `Usage: uv cache clean
[OPTIONS] [PACKAGE]...` — positional package arguments are supported. The command
`uv cache clean watercooler-cloud` is valid.

**Fix:** Add a clarifying note below the commands block (matching the note already
in QUICKSTART.md) so users don't second-guess the syntax:

> **Note:** Use the positional argument form (`uv cache clean watercooler-cloud`
> without `--package`). The `--package` flag syntax differs between subcommands.

---

### 076 · `watercooler_smart_query` omits `group_ids` parameter

**File:** `docs/TOOLS-REFERENCE.md`

**Problem:** The `watercooler_smart_query` parameter table omits `group_ids`.
The implementation accepts `group_ids: list[str] | None = None`. This matters
for multi-project setups that use federation group filtering.

**Fix:** Add the row to the parameter table:

| `group_ids` | list | no | Optional project group IDs to filter results (default: all groups) |

---

## Acceptance criteria

- [x] **063** Issue #10 has correct positional invocation with dry-run + execute steps, plus config-cleanup and archive steps merged from the standalone section; standalone "Migration guide" section removed
- [x] **064** `[mcp.git]` example uses `author` not `name`
- [x] **065** `watercooler_ack` table has no `role` row; Spec tip added
- [x] **066** QUICKSTART Step 1 verify uses `watercooler --help`
- [x] **067** `watercooler_say` table has no `status` row; set_status note added
- [x] **068** `watercooler_memory_task_status` has full parameter table
- [x] **069** resources.py mentions thread auto-creation
- [x] **070** resources.py mentions `watercooler_whoami()` for identity verification
- [x] **071** `ack` ball semantics description is accurate (no ball ownership required)
- [x] **072** resources.py names three specific tools that accept neither param
- [x] **073** resources.py has one-line stubs for all four missing tools
- [x] **074** `watercooler_search` section header matches safety table prereq
- [x] **075** TROUBLESHOOTING "stale install" section has note clarifying positional syntax
- [x] **076** `watercooler_smart_query` table includes `group_ids` row

## Implementation notes

- Branch: `docs/user-docs-refresh` — all changes push to PR #285
- No Python source changes; all edits are to `.md` and `resources.py` (string literal)
- Edit order: P1 → P2 → P3 within each file to minimize re-reads
- Commit strategy: one commit per phase (P1, P2, P3) for clean review history
- After all edits: run `grep -n "\-\-source\|\-\-target\|name = \"Claude" docs/*.md` to confirm no regressions
  - Note: don't grep for `watercooler --version` — it appears legitimately in TROUBLESHOOTING.md as symptom text (the stale-install issue describes `watercooler --version` showing an old version)

## Post-deploy monitoring

No operational impact — documentation-only change. No additional monitoring required.
