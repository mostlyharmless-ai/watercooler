---
title: "Documentation Drift: Source-Verified Parameter and Schema Accuracy"
category: docs
tags:
  - documentation
  - accuracy
  - drift-prevention
  - config
  - mcp-tools
  - cli
date_solved: "2026-03-04"
pr_numbers:
  - 285
  - 289
todos_addressed:
  - "063"
  - "064"
  - "065"
  - "066"
  - "067"
  - "068"
  - "076"
symptom: "CLI flags, MCP tool parameters, and TOML config keys documented incorrectly; users hit errors immediately on setup"
root_cause: "Docs written from memory or prior docs, never verified against source"
---

# Documentation Drift: Source-Verified Parameter and Schema Accuracy

## Symptom

Users follow the documentation and immediately hit errors:

- `watercooler --version` fails (flag doesn't exist)
- `[mcp.git]` config silently ignored (wrong TOML key `name` instead of `author`)
- MCP tool call rejected with unexpected-keyword-argument error (`role` param on `ack`, `status` param on `say`)
- Memory credentials not loaded (wrong section names `[llm]`/`[embeddings]` in credentials.toml)
- Config not applied (singular/plural mismatch: `memory.embeddings.*` vs `memory.embedding.*`)

All of these were real bugs in the PR #285 docs refresh, caught by the multi-agent code review.

## Root Cause

Docs are written by composing from memory, prior docs, and intuition — not from source.
The bugs are invisible during writing because each one looks plausible:

| Drift | What docs said | What source says |
|-------|---------------|-----------------|
| CLI verify step | `watercooler --version` | No `--version` flag; use `--help` |
| `[mcp.git]` key | `name = "Claude Code"` | `author` (`config_schema.py:69`) |
| `ack` parameter | `role` row in table | `_ack_impl` has no `role` param |
| `say` parameter | `status` row in table | `_say_impl` has no `status` param |
| credentials.toml sections | `[llm]`, `[embeddings]` | `[openai]`, `[anthropic]` (provider-named) |
| embedding config prefix | `memory.embeddings.*` | `memory.embedding.*` (singular) |
| `--role` default | "defaults to `implementer`" | No default; uses `_get_git_user()` |
| `ack` ball semantics | "still working" implies ball required | `ack` does not require holding the ball |

## Working Solution

### 1. Verify CLI flags against `cli.py`

Never trust prior docs or intuition for CLI flags. For each documented command:

```bash
# Check available options for a command
python -m watercooler say --help
python -m watercooler ack --help
```

Or read the decorator directly:
```bash
grep -n "@click.option\|@click.argument\|def say" src/watercooler/cli.py
```

For default values, look for `default=` in `@click.option`. If there is no `default=`,
the parameter has no default — it either requires user input or falls back to a helper
function (grep the function body).

### 2. Verify MCP tool parameters against `tools/*.py`

For each documented MCP tool, find its `@mcp.tool()` function signature:

```bash
grep -n "def watercooler_ack\|def watercooler_say\|def watercooler_memory" \
  src/watercooler_mcp/tools/*.py
```

Then read the function signature — the parameters in the signature are the only valid
parameters. Anything not in the signature will produce an `unexpected keyword argument` error.

For tools that delegate to `_say_impl` / `_ack_impl`, read the impl function too:

```bash
grep -n "def _say_impl\|def _ack_impl" src/watercooler_mcp/tools/thread_write.py
```

### 3. Verify TOML config keys against `config_schema.py`

The Pydantic model is the ground truth. Before documenting any TOML key:

```bash
grep -n "class MemoryConfig\|class GitConfig\|class MCPConfig\|class CommonConfig" \
  src/watercooler/config_schema.py
```

Read the field names. Pay attention to:
- **Singular vs plural**: `memory.embedding` (not `memory.embeddings`)
- **Exact key names**: `author` (not `name`) in `[mcp.git]`
- **Nested sections**: Verify the full dotted path, not just the leaf key

### 4. Verify credentials.toml section names against `credentials.py`

The credentials loader uses provider-named sections, not category-named sections:

```python
# WRONG — these sections don't exist
[llm]
api_key = "..."

[embeddings]
api_key = "..."

# RIGHT — provider-named sections
[openai]
api_key = "sk-..."

[anthropic]
api_key = "sk-ant-..."
```

Verify by reading `get_provider_api_key()` in `src/watercooler/credentials.py`:

```bash
grep -n "def get_provider_api_key\|\[\"openai\"\]\|\[\"anthropic\"\]" \
  src/watercooler/credentials.py
```

### 5. Verify env var TOML equivalents against `config_loader.py`

The env var mapping table in docs must match what `config_loader.py` actually reads.
The loader is the authoritative source:

```bash
grep -n "WATERCOOLER_\|memory.embedding\|memory.embeddings" \
  src/watercooler/config_loader.py
```

Lines 194–196 (as of PR #289) confirm `memory.embedding.*` (singular) for `EMBEDDING_*` env vars.

### 6. Verify ball semantics by reading the `ack` docstring

Ball behavior is subtle. The claim "ack requires holding the ball" is wrong — `ack`
is explicitly designed to allow any agent to acknowledge regardless of ball state.
Verify by reading the tool docstring:

```bash
grep -A 20 "def watercooler_ack" src/watercooler_mcp/tools/thread_write.py
```

## Source Verification Checklist

Run before merging any documentation PR:

```bash
# 1. No --source/--target flags (migration uses positional args)
grep -n "\-\-source\|\-\-target" docs/*.md

# 2. No 'name = ' in [mcp.git] examples
grep -n 'name = "' docs/CONFIGURATION.md

# 3. No --version in quickstart/install docs
grep -n "watercooler --version" docs/QUICKSTART.md docs/AUTHENTICATION.md

# 4. No [llm] or [embeddings] sections in credentials examples
grep -n '^\[llm\]\|^\[embeddings\]' docs/CONFIGURATION.md docs/AUTHENTICATION.md

# 5. No memory.embeddings. (plural) in config docs
grep -n "memory\.embeddings\." docs/CONFIGURATION.md

# 6. No 'role' parameter documented for watercooler_ack
grep -B2 -A5 "watercooler_ack" docs/TOOLS-REFERENCE.md | grep "role"
```

## Special Case: `resources.py` (Agent-Facing Resource)

`src/watercooler_mcp/resources.py` has the highest accuracy requirement of any file
in the repository. It is the `watercooler://instructions` MCP resource — the only
documentation that AI agents can access directly. Agents cannot fall back to reading
docs files if `resources.py` is wrong.

Consequences of inaccuracy in `resources.py`:
- Agent calls a tool with a non-existent parameter → runtime error
- Agent follows wrong ball semantics → coordination breaks down
- Agent doesn't know about a tool → capability never used
- Agent uses wrong identity method → entries are mis-attributed

**Apply the source verification steps above to `resources.py` before any merge.**
Additionally, verify against the most current tool implementations, not against other
docs files (which may themselves be outdated).

See also: the `resources.py`-specific checklist in CLAUDE.md under "Documentation Standards".

## Maintenance Rules (from `docs-user-docs-refresh-rollup` thread)

From the Codex rollup, these maintenance rules apply to all future user-doc updates:

1. Verify every command example against live `--help` output before merge
2. Verify MCP tool parameters/safety from source (`src/watercooler_mcp/tools/*`), not inferred behavior
3. Keep configuration docs schema-accurate: keys/sections/examples must match `config.example.toml` and active schema
4. Use explicit safety labels for tools (`read-only`, `idempotent`, `mutating`, `destructive`) and state prerequisites clearly
5. Maintain docs/resource parity: any MCP behavior guidance change in docs must be reflected in `watercooler://instructions` in the same PR

## Related

- `dev_docs/solutions/process/pr-branch-discipline-push-hygiene.md` — how excessive review rounds amplify documentation drift
- `dev_docs/solutions/process/automated-pr-review-multi-pass-inefficiency.md` — multi-round review patterns
- `dev_docs/plans/2026-03-03-fix-docs-review-findings-063-076-plan.md` — the 14 fixes that motivated this solution
