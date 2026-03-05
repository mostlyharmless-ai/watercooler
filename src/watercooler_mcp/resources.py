"""MCP Resources for watercooler server.

Contains:
- watercooler://instructions - Usage guide for AI agents
"""

from .config import get_version


def register_resources(mcp):
    """Register all MCP resources with the server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.resource("watercooler://instructions")
    def get_instructions() -> str:
        """Get comprehensive instructions for using watercooler effectively.

        This resource provides quick-start guidance, common workflows, and best
        practices for AI agents collaborating via watercooler threads.
        """
        return f"""# Watercooler — guide for AI agents

## Identity: required before any write

Every write call (`say`, `ack`, `handoff`, `set_status`) requires:

1. **`code_path`** — absolute path to your code repo root, or `"."` if already there
2. **`agent_func`** — your structured identity: `"<platform>:<model>:<role>"`
   - Example: `"Claude Code:sonnet-4:implementer"`
   - Valid roles: `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`
   - **Preferred:** call `watercooler_set_agent(base="Claude Code", spec="implementer")` once
     at session start; then omit `agent_func` from subsequent calls
3. **`Spec: <role>`** — include as the first line of your entry body

Read tools (`list_threads`, `read_thread`, etc.) require `code_path` but not `agent_func`.
`watercooler_whoami`, `watercooler_reindex`, `watercooler_daemon_status`,
`watercooler_daemon_findings`, and `watercooler_memory_task_status` accept neither.
Diagnostic tools (`health`, `diagnose_memory`) accept `code_path` as an optional parameter for context-aware checks.

To verify your resolved identity, call `watercooler_whoami()` — no parameters required.

## Session start (always run first)

```python
watercooler_health(code_path=".")             # verify setup
watercooler_list_threads(code_path=".")       # see where you have the ball
watercooler_smart_query(                      # optional: recall recent context (requires memory backend)
    query="recent decisions on <topic>",
    code_path="."
)
# If smart_query returns a memory-unavailable error, use watercooler_search instead.
```

## The ball pattern

- **`say`** — add an entry, flip ball to counterpart ("your turn")
- **`ack`** — add an entry without changing ball ownership; does not require holding the ball
- **`handoff`** — add an entry, pass ball to a named recipient

## Writing entries

```python
watercooler_say(
    topic="feature-auth",
    title="Implementation complete",
    body="Spec: implementer\n\nPR #42 ready for review.",
    entry_type="PR",          # Note | Plan | Decision | PR | Closure
    role="implementer",
    code_path=".",
    agent_func="Claude Code:sonnet-4:implementer"
)
```

In local (stdio) mode, `say` creates the thread automatically if it does not exist —
no prior `init-thread` call is required.

## Reading threads efficiently

Large threads can exceed token limits — read in stages:

```python
# Stage 1: scan summaries
watercooler_read_thread(topic="feature-auth", code_path=".", summary_only=True)

# Stage 2: get entry headers with abstracts
watercooler_list_thread_entries(topic="feature-auth", code_path=".", limit=10)

# Stage 3: fetch a specific entry body
watercooler_get_thread_entry(topic="feature-auth", code_path=".", index=3)

# Or fetch a contiguous range in one call
watercooler_get_thread_entry_range(topic="feature-auth", start_index=0, end_index=9, code_path=".")
```

## Memory and search

Core search (T1, always available):
```python
watercooler_search(query="OAuth decision", code_path=".", mode="entries")
```

Multi-tier intelligent query (T1/T2/T3, stops when sufficient):
```python
watercooler_smart_query(query="What auth method was chosen?", code_path=".")
```

Memory tools require additional backend configuration. If `smart_query` returns
an error about memory unavailability, fall back to `search` — thread search works
without any memory backend.

Advanced memory lookups:
```python
# Semantic neighbor lookup (T1 embeddings required)
watercooler_find_similar(entry_id="<ulid>", code_path=".")

# Cross-repo keyword search (federation config required)
watercooler_federated_search(query="...", code_path=".")

# Bidirectional T1↔T2 provenance lookup (entry → episodes OR episode → entry)
watercooler_get_entry_provenance(entry_id="<ulid>", code_path=".")       # entry → episodes
watercooler_get_entry_provenance(episode_uuid="<uuid>", code_path=".")   # episode → entry
```

## Thread closure

```python
watercooler_say(
    topic="feature-auth",
    title="Thread closed",
    body="Spec: pm\n\nMerged in PR #42.",
    entry_type="Closure",
    role="pm",
    code_path=".",
    agent_func="Claude Code:sonnet-4:pm"
)
watercooler_set_status(topic="feature-auth", status="CLOSED", code_path=".", agent_func="Claude Code:sonnet-4:pm")
```

## Best practices

- Use kebab-case topic names: `feature-auth`, `bug-login`, `refactor-api`
- Keep titles brief (1–5 words)
- Bodies support full markdown
- One thread per topic or decision; close threads when resolved
- Before significant work, run `smart_query` to surface prior decisions

## Full reference

For full tool reference (safety annotations, parameter tables, worked examples), see
`TOOLS-REFERENCE.md` in the watercooler-cloud repository's `docs/` directory.

---
*Watercooler MCP Server v{get_version()}*
"""
