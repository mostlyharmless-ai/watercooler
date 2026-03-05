---
title: "Memory Dedup Guard: Three-guard correctness hardening for Graphiti episode ingestion"
category: logic-errors
tags:
  - memory
  - graphiti
  - dedup
  - correctness
  - performance
  - observability
  - entry-episode-index
  - bulk-index
  - chunking
module:
  - src/watercooler_mcp/memory_sync.py
  - src/watercooler_mcp/tools/memory.py
  - src/watercooler_memory/entry_episode_index.py
  - tests/unit/test_entry_episode_index.py
symptom:
  - "Agent retries on timeout create duplicate Graphiti episodes for the same watercooler entry"
  - "Re-running bulk_index on an already-indexed project re-queues all entries"
  - "Crash mid-chunk-loop leaves partial mappings; next retry silently skips remaining chunks"
  - "Guard 2a triggers after chunk_text() — wasted tokenization on re-run entries"
  - "Dedup index load failures are silent in production (DEBUG level log)"
  - "watercooler_graphiti_add_episode MCP tool has no dedup guard at all"
root_cause:
  - "Dedup guard placed after chunk_text() instead of before — expensive work precedes the skip decision"
  - "Guard checks has_entry() only, missing entries indexed via add_chunk_mapping() in _chunks_by_entry"
  - "Direct MCP tool path (_graphiti_add_episode_impl) bypasses all existing guards via fire-and-forget closure"
  - "Exception in dedup index load logged at DEBUG — invisible when production logs are INFO+"
  - "Two-index model (live in-memory vs disk snapshot) not documented — Guard 3 staleness behavior unclear"
date_solved: "2026-02-25"
pr_number: 245
follow_up_todos:
  - "079: Move Guard 2a before chunk_text()"
  - "080: WARNING log for partial-mapping crash recovery"
  - "081: Add dedup guard to watercooler_graphiti_add_episode tool"
  - "082: Expand bulk_index docstring with already_indexed field"
  - "083: Differentiate FileNotFoundError (DEBUG) vs other exceptions (WARNING)"
  - "084: Move import hashlib to module level"
  - "085: Add skip_reason field to Guard 1 and Guard 2a returns"
  - "086: Expand has_any_mapping docstring with Args/Returns/distinction"
  - "087: Document two-index model in _bulk_index_impl comment"
  - "088: Fix TestEntryEpisodeIndexChunkMethods docstring"
  - "089: Extract _get_cached_provenance_index helper to DRY path resolution"
---

# Memory Dedup Guard: Three-Guard Correctness Hardening

## Symptom

PR #245 added dedup guards to prevent duplicate Graphiti episodes from bulk re-runs and agent retries.
After merge, code review identified 11 follow-up issues (todos 079–089) covering guard placement, missing
coverage, observability, and documentation. All 11 were implemented in a subsequent hardening pass.

Observable symptoms before hardening:

- Re-running `watercooler_bulk_index` created duplicate episodes — every entry re-queued
- Agent retry after timeout (common pattern) created 2× or 3× episode copies in Graphiti
- Post-crash restart silently abandoned partially-indexed chunked entries with no warning
- `has_any_mapping()` didn't exist — guard used `has_entry()` which misses chunked entries

## Root Cause

### 1. Guard Ordering: After Expensive Work

Guard 2a in `_call_graphiti_add_episode_chunked` was placed **after** `chunk_text()`:

```python
# BAD — chunks computed before guard fires
chunks = chunk_text(content, chunker_config)  # ← wasted on re-runs
if len(chunks) <= 1:
    return await _call_graphiti_add_episode(...)

# Guard fires here — but chunking already happened
if entry_id and backend.entry_episode_index is not None:
    if backend.entry_episode_index.has_any_mapping(entry_id):
        return {"success": True, "chunk_count": 0, "total_chunks": 0, ...}
```

At 500 already-indexed large entries on re-run: 250–1,000ms wasted in tokenization.

### 2. Index Only Covers Non-Chunked Entries

`EntryEpisodeIndex` tracked two separate dicts:
- `_by_entry`: full-entry mappings (single episode per entry)
- `_chunks_by_entry`: chunk mappings (N episodes per entry)

The guard used `has_entry()` which only checks `_by_entry`. Chunked entries lived exclusively in
`_chunks_by_entry` — `has_entry()` returned False even for fully-indexed chunked content.

### 3. Fire-and-Forget MCP Tool Bypasses All Guards

`_graphiti_add_episode_impl` uses a fire-and-forget async closure:

```python
async def _do_add_episode():
    result = await backend.add_episode_direct(...)  # ← no guard

asyncio.create_task(_do_add_episode())  # guard never reached
```

Guards 1 and 2 in `memory_sync.py` were never reached by this tool path.

### 4. Silent Guard Degradation

When the dedup index file failed to load, the exception was swallowed at DEBUG level:

```python
except Exception as exc:
    logger.debug("MEMORY: Could not load entry index for dedup guard: %s", exc)
```

In production (INFO+ logging), this failure was completely invisible — the dedup guard silently became
a no-op on every call.

## Working Solution

### Three-Guard Architecture

Guards operate at three sequential points in the indexing pipeline:

```
MCP Tool Call                    Guard 1: _graphiti_add_episode_impl
     ↓                           (direct tool path — fire-and-forget closure entry)
Real-time Sync                  Guard 2a: _call_graphiti_add_episode_chunked
     ↓                           (chunked sync — fires BEFORE chunk_text())
Bulk Index Queue Fill           Guard 3: _bulk_index_impl
                                 (disk snapshot pre-flight filter)
```

**Guard 1 — Direct MCP Tool** (`src/watercooler_mcp/tools/memory.py`):

```python
# Pre-flight dedup: skip if this entry is already indexed.
# Protects against agent retries creating duplicate episodes.
if entry_id and backend.entry_episode_index is not None:
    if backend.entry_episode_index.has_any_mapping(entry_id):
        logger.debug("MEMORY: Skipping already-indexed entry %s (direct tool)", entry_id)
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "success": True,
                "status": "skipped",
                "skip_reason": "already_indexed",
                "entry_id": entry_id,
            }, indent=2)
        )])
```

**Guard 2a — Chunked Sync** (`src/watercooler_mcp/memory_sync.py`):

```python
# Pre-flight dedup: skip entire entry if any chunk already indexed.
# All-or-nothing: if a crash left partial chunks, remaining chunks are NOT
# retried. Clear entry_episode_index manually to force re-indexing.
if entry_id and backend.entry_episode_index is not None:
    if backend.entry_episode_index.has_any_mapping(entry_id):
        if not backend.entry_episode_index.has_entry(entry_id):
            # Partial indexing — crash recovery path
            logger.warning(
                "MEMORY: Entry %s has partial chunk mapping (crash recovery path). "
                "Remaining chunks will NOT be re-indexed. Clear index to retry.",
                entry_id,
            )
        else:
            logger.debug("MEMORY: Skipping already-indexed entry %s (chunked)", entry_id)
        return {
            "success": True, "chunk_count": 0, "total_chunks": 0,
            "episode_uuids": [], "skipped": True, "skip_reason": "already_indexed",
        }

# Only now do expensive chunking work:
chunks = chunk_text(content, chunker_config)
```

**Guard 3 — Bulk Index Pre-flight** (`src/watercooler_mcp/tools/memory.py`):

```python
# Guard 3: pre-flight dedup using the on-disk index snapshot.
# NOTE: This is a SEPARATE object from backend.entry_episode_index (the live
# in-memory index used by Guards 1/2 at execution time). Guard 3 requires
# auto_save_index=True (the default) to stay current. It is a best-effort
# filter; Guards 1/2 remain authoritative.
_dedup_index = None
try:
    graphiti_config = load_graphiti_config(code_path=code_path)
    if graphiti_config and graphiti_config.entry_episode_index_path:
        _dedup_index = _get_cached_provenance_index(graphiti_config.entry_episode_index_path)
except FileNotFoundError:
    logger.debug("MEMORY: No entry index found for dedup guard (first run)")
except Exception as exc:
    logger.warning(
        "MEMORY: Could not load entry index for dedup guard — dedup skipped: %s", exc
    )
```

### Two-Index Model

The `EntryEpisodeIndex` maintains two internal dicts with distinct semantics:

| Dict | Key | Value | Use case |
|------|-----|-------|----------|
| `_by_entry` | `entry_id` | `episode_uuid` | Non-chunked entries (single episode) |
| `_chunks_by_entry` | `entry_id` | `[ChunkEpisodeMapping]` | Chunked entries (N episodes) |

**Critical**: Guards must use `has_any_mapping()` — NOT `has_entry()`:

```python
def has_any_mapping(self, entry_id: str) -> bool:
    """Check if an entry has any index mapping (non-chunked or chunked).

    Unlike has_entry(), this also checks the chunked-entries index
    (_chunks_by_entry), so it correctly returns True for entries indexed
    via add_chunk_mapping() rather than add(). Use this for dedup guards
    — has_entry() alone is insufficient for chunked entries.
    """
    with self._lock:
        return entry_id in self._by_entry or entry_id in self._chunks_by_entry
```

### Partial-Indexing Detection

Uses the two-method check to distinguish states:

| `has_any_mapping()` | `has_entry()` | State | Log level |
|---------------------|---------------|-------|-----------|
| False | False | Not indexed — proceed | — |
| True | True | Fully indexed — skip | DEBUG |
| True | False | **Partial indexing (crash)** | **WARNING** |

The WARNING includes explicit recovery steps: "Clear index to retry" — operators know exactly how
to force re-indexing when needed.

### skip_reason Field

Both Guard 1 and Guard 2a return `skip_reason: "already_indexed"` to help callers distinguish
a dedup skip from an error or empty result:

```python
# Guard 1 response:
{"success": True, "status": "skipped", "skip_reason": "already_indexed", "entry_id": "..."}

# Guard 2a response:
{"success": True, "chunk_count": 0, "total_chunks": 0,
 "episode_uuids": [], "skipped": True, "skip_reason": "already_indexed"}
```

An agent observing `entries_queued: 0, already_indexed: 847` on a second `bulk_index` run can
confirm this is the expected idempotent outcome — not a failure.

### Module-Level Import

`import hashlib` moved from inside the per-chunk loop to module level in `memory_sync.py`.
Python caches the module after first import, so the runtime cost was minimal — but the in-loop
placement was misleading and established a bad pattern:

```python
# Before (wrong):
for i, (chunk_text_content, token_count) in enumerate(chunks):
    import hashlib  # ← inside loop
    chunk_id = hashlib.sha256(...)

# After (correct):
import hashlib  # ← module level, with other stdlib imports

for i, (chunk_text_content, token_count) in enumerate(chunks):
    chunk_id = hashlib.sha256(...)
```

## Prevention Strategies

### 1. Guard Before Expensive Work

Place any dedup/skip check **before** chunking, tokenization, or LLM calls:

```python
# ✓ Guard fires first — no wasted work
if _already_indexed(entry_id, index):
    return skip_result()
chunks = chunk_text(content, config)  # ← only reached if not indexed
```

### 2. Use has_any_mapping() Not has_entry()

When adding new dedup guards against `EntryEpisodeIndex`, always use `has_any_mapping()`.
`has_entry()` is insufficient for any code path that creates chunked entries.

### 3. Guard Every Entry Point

When adding a new code path that calls `add_episode_direct` or similar, add a dedup guard at
that entry point. Fire-and-forget async closures are especially easy to miss — the guard must be
**inside** the closure, not before `create_task()`.

### 4. Log Degradation at WARNING

If loading the dedup index fails unexpectedly, log at WARNING (not DEBUG). The guard becomes a
no-op, which must be visible in production logs. First-run `FileNotFoundError` (expected) can
stay at DEBUG.

### 5. Document the Two-Index Model

Any new code that loads `_dedup_index` (disk snapshot) should document that it is separate from
`backend.entry_episode_index` (live in-memory) and explain the staleness implications.

## Code Review Checklist

When reviewing code that adds or modifies dedup guard logic:

- [ ] Guard fires BEFORE expensive operations (chunk_text, tokenization, LLM calls)
- [ ] Guard uses `has_any_mapping()` not `has_entry()`
- [ ] All entry points (tools, sync paths, bulk ops) have consistent guards
- [ ] Fire-and-forget async closures have guard at closure entry
- [ ] Unexpected index load failures logged at WARNING, not DEBUG
- [ ] Partial-indexing case (chunk mapping exists, full-entry mapping absent) logged at WARNING
- [ ] Return dicts include `skip_reason` for diagnostic clarity
- [ ] Two-index model (live vs disk) documented in comments
- [ ] New tests: guard before expensive ops, partial-indexing detection, idempotent retry

## Historical Context

### Why Dedup Became Necessary

Memory sync was originally coupled inside `enrich_graph_entry()` (PR #147 / PR #147 thread:
`bug-graphiti-indexing-not-running-on-writes`). When enrichment was decoupled and moved to run
unconditionally from middleware, every write began triggering memory sync regardless of enrichment
config. This was correct — but it meant agent retries and `bulk_index` re-runs now reliably
double-indexed, making dedup non-optional.

A secondary double-indexing risk was also identified in that PR review: the deprecated
`sync_entry_to_graph()` still has its own `sync_to_memory_backend()` call. If any code path
invokes both the middleware and `sync_entry_to_graph()`, entries would be double-indexed. The
dedup guard is the backstop for this case.

### Crash Recovery: Two Separate Layers

PR #150 added crash recovery for the **task queue** (enqueue 5 entries → kill -9 mid-flight →
restart → stale tasks recovered and completed). This was tested with 5 live entries in the
`memory-queue-e2e-test` thread.

The chunk-level partial indexing gap addressed in todos 079-089 is a **different and lower layer**:
the task queue recovers the task, but if a crash occurs mid-chunk-loop *within* a single task,
only some chunks are committed. The dedup guard detects this state (chunks in index but no
full-entry mapping) and warns at WARNING level rather than silently skipping.

### Chunk Provenance and T3 Link

Each chunk indexed via `_call_graphiti_add_episode_chunked` records:
- `source_description = "thread:{topic} | entry:{entry_id} | chunk:{N}/{M}"`
- `EntryEpisodeIndex.add_chunk_mapping(episode_uuid, entry_id, chunk_index)` for structural T3→T1 reverse lookup

This is the structural link that T3 (LeanRAG) uses for reverse provenance. T2 chunking uses
`chunk_text()` directly — agent/role/type header fields are NOT in the episode text. Provenance
travels via `source_description` and `EntryEpisodeIndex` only. This is why `has_any_mapping()`
must check `_chunks_by_entry` in addition to `_by_entry`.

## Related

- Plan: `dev_docs/plans/2026-02-24-fix-memory-bulk-index-dedup-guard-plan.md`
- PR: #245 (`fix(memory): pre-indexing dedup guard to prevent bulk_index duplicate episodes`)
- PR: #147 (`fix: decouple T2 memory sync from enrichment pipeline`) — introduced the need for dedup
- PR: #150 (`feat: persistent memory task queue`) — task-level crash recovery (different layer)
- ADR: `dev_docs/watercooler-planning/adr-0001-memory-backend-contract.md`
- Roadmap: `dev_docs/watercooler-planning/MEMORY_INTEGRATION_ROADMAP.md` (Milestone 4.1)
- MCP tool docs: `docs/mcp-server.md` (watercooler_bulk_index section)
- Test coverage: `tests/unit/test_entry_episode_index.py::TestEntryEpisodeIndexChunkMethods`
- Crash recovery tests: `memory-queue-e2e-test` thread (task-queue layer, not chunk layer)
