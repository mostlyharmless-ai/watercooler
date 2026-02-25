---
title: "fix(memory): pre-indexing dedup guard to prevent bulk_index duplicate episodes"
type: fix
status: completed
date: 2026-02-24
issues: ["#243", "#244"]
---

# fix(memory): Pre-indexing Dedup Guard for `bulk_index`

## Overview

`watercooler_bulk_index` has no guard against re-indexing entries already committed to
Graphiti in a previous session. Re-running `bulk_index` re-queues every entry and creates
duplicate `Episodic` nodes in FalkorDB. The real-time sync path (`_graphiti_sync_callback`)
has the same problem for live `say`/`ack` operations. This plan wires the existing
`EntryEpisodeIndex` as an O(1) pre-flight dedup guard at three call sites.

**Closes:** #243 (bug), #244 (feature/wiring task)

---

## Problem Statement

### How duplication happens

1. **Session A** (January 2026): `bulk_index` queues 2,661 entries → all indexed → the
   `entry_episode_index.json` file records each mapping.
2. **Session B** (February 2026): `bulk_index` is re-run accidentally. The queue's dedup key
   (`f"{backend}:{topic}:{entry_id}:{task_type}"`) only blocks tasks that are already *pending
   or in-flight*. Once a task reaches `DONE` status it is invisible to the check, so all
   1,575 overlapping entries are re-queued freely.
3. `add_episode_direct` is append-only — Graphiti never upserts by content. Each re-queued
   entry creates a fresh `Episodic` node on top of the existing one.

**Entity layer is safe:** `resolve_extracted_nodes` deduplicates entity nodes by name+type.
Only the `Episodic` (episode) layer accumulates duplicates.

### Two distinct failure modes

This fix addresses two related but distinct duplication paths:

| Path | Trigger | Guards needed |
|---|---|---|
| **bulk re-run** | `bulk_index` called more than once | Guard 3 (queue-fill layer) |
| **real-time sync** | `_graphiti_sync_callback` fires on already-indexed entries | Guards 1, 2a |

These share the same fix mechanism (`EntryEpisodeIndex`) but protect different code paths.
The queue executor used by `bulk_index` always calls `_call_graphiti_add_episode` (non-chunked
only — the chunked variant is never reached from the queue). Guards 1 and 2a protect the
real-time sync path; Guard 3 protects the bulk path.

### Why `find_episode_by_chunk_id_async` doesn't help (as-is)

Issue #244 proposes wiring `find_episode_by_chunk_id_async` (a FalkorDB Cypher query
matching `source_description CONTAINS "chunk:{chunk_id[:12]}"`) as the pre-flight check.
However the chunked path writes:

```
source_description = "thread:{topic} | entry:{entry_id} | chunk:{chunk_num}/{total_chunks}"
```

The positional `chunk:1/3` token does not contain the hash `chunk_id`. The DB query always
misses. The correct and cheaper approach is the **local index** — `EntryEpisodeIndex`
already tracks every successfully-indexed entry with O(1) dict lookups.

### Critical: `_by_entry` vs `_chunks_by_entry` — two separate tracking structures

`EntryEpisodeIndex` maintains **two independent** mapping dicts that do not cross-populate:

| Method | Writes to | Populated by |
|---|---|---|
| `add(entry_id, episode_uuid, thread_id)` | `_by_entry` | `index_entry_as_episode` (non-chunked path) |
| `add_chunk_mapping(chunk_id, ...)` | `_by_chunk`, `_chunks_by_entry` | chunked path in `memory_sync.py` |

`has_entry(entry_id)` checks **only `_by_entry`**. For any entry indexed via the chunked
path, `has_entry` returns `False` even when all its chunks are in the index. Using `has_entry`
alone as the guard silently fails for all installations using `chunk_on_sync=True`.

The fix requires a new `has_any_mapping(entry_id)` method on `EntryEpisodeIndex` that checks
both `_by_entry` and `_chunks_by_entry`.

---

## Proposed Solution

### New helper: `EntryEpisodeIndex.has_any_mapping`

**File:** `src/watercooler_memory/entry_episode_index.py`

Add a public method that checks both tracking dicts:

```python
def has_any_mapping(self, entry_id: str) -> bool:
    """Check if an entry_id has any index mapping (non-chunked or chunked)."""
    with self._lock:
        return entry_id in self._by_entry or entry_id in self._chunks_by_entry
```

All three guards use this method instead of `has_entry`.

---

### Guard 1 — `_call_graphiti_add_episode` (non-chunked real-time path)

**File:** `src/watercooler_mcp/memory_sync.py` ~line 70
(after backend is loaded, before `add_episode_direct`)

```python
# Pre-flight dedup: skip if already indexed
if entry_id and backend.entry_episode_index is not None:
    if backend.entry_episode_index.has_any_mapping(entry_id):
        logger.debug("MEMORY: Skipping already-indexed entry %s", entry_id)
        return {"success": True, "skipped": True}
```

### Guard 2a — `_call_graphiti_add_episode_chunked` (chunked real-time path)

**File:** `src/watercooler_mcp/memory_sync.py` ~line 185
(after backend is loaded, before the chunk loop)

```python
# Pre-flight dedup: skip entire entry if any chunk already indexed
if entry_id and backend.entry_episode_index is not None:
    if backend.entry_episode_index.has_any_mapping(entry_id):
        logger.debug("MEMORY: Skipping already-indexed entry %s (chunked)", entry_id)
        return {"success": True, "chunk_count": 0, "total_chunks": 0,
                "episode_uuids": [], "skipped": True}
```

Note: return dict does not call `get_episode_uuids_for_entry` — the caller
(`_graphiti_sync_callback`) only inspects `success: True`. The UUID list is unnecessary.

### Guard 3 — `_bulk_index_impl` (queue-fill path)

**File:** `src/watercooler_mcp/tools/memory.py` ~line 1381

Reuse the existing `_get_cached_provenance_index` helper (lines 76–106) — it already
provides mtime-aware cached reads of `EntryEpisodeIndex` without constructing
`GraphitiBackend`. The path-resolution pattern also already exists in `_get_entry_provenance_impl`.

```python
# Load index once before the loop using existing cached helper
index_path = ...   # same two-step resolution already in _get_entry_provenance_impl
index = _get_cached_provenance_index(index_path)  # returns None if file absent

already_indexed = 0
for entry in entries:
    entry_id = entry.get("entry_id", "")
    content = entry.get("body", "")
    if not content:
        skipped += 1
        continue
    # Skip entries already committed to Graphiti
    if index is not None and entry_id and index.has_any_mapping(entry_id):
        already_indexed += 1
        continue
    task_id = enqueue_memory_task(...)
    ...

# Surface already_indexed count in tool response
return {..., "already_indexed": already_indexed, ...}
```

**Do NOT introduce a new `_load_index_readonly` helper or `_index_loader.py` module.**
`_get_cached_provenance_index` is the correct existing infrastructure.

---

### Guard 2b (chunk-level partial retry) — DEFERRED

The plan originally included a per-chunk guard inside the chunk loop (Guard 2b) to handle
partial-failure retry (chunk 1 indexed, chunks 2–3 failed on prior run). This is a separate
concern from the `bulk_index` re-run bug and adds significant complexity:
`previous_episode_uuids` must be correctly threaded through skipped chunks to preserve chain
order. The chunk methods (`add_chunk_mapping`, `has_chunk`) also currently have zero test
coverage.

**Decision:** Defer Guard 2b to a follow-up issue. It has no code path from `bulk_index`
(the queue executor always calls the non-chunked variant) and it is only reachable via
the real-time sync path in a partial-failure scenario that is not currently tested.

---

## Technical Considerations

### `_get_cached_provenance_index` is the right tool for Guard 3

`_get_cached_provenance_index` at `tools/memory.py:76` resolves the index path from
`GraphitiConfig`, loads `EntryEpisodeIndex`, and caches it with mtime-based invalidation.
It handles `FileNotFoundError` gracefully (returns `None`). It is already used by
`_get_entry_provenance_impl` for read-only index access from a tool function.

Using it for Guard 3 means Guard 3 gets cache benefits across repeated `bulk_index` calls in
the same session.

### Guard semantics: snapshot vs. live instance

- **Guard 3** uses `_get_cached_provenance_index` — a read-only snapshot loaded before the
  loop. Under concurrent `bulk_index` calls, both calls load the same snapshot. The queue's
  own `_find_duplicate` check (by `dedup_key`) provides the final correctness barrier in
  that case. The stale snapshot only affects the `already_indexed` counter accuracy, not
  correctness.
- **Guards 1 and 2a** use `backend.entry_episode_index` — the live backend-held instance
  updated by `index_entry_as_episode`. These are protected by `EntryEpisodeIndex`'s
  `threading.RLock`.

### Index availability

All guards are conditional on `entry_episode_index is not None` (Guards 1/2a) or
`index is not None` (Guard 3). When the index is disabled (`track_entry_episodes=False`)
or absent (first run), guards are no-ops and re-indexing is allowed.

---

## Acceptance Criteria

### Functional

- [x] `EntryEpisodeIndex.has_any_mapping(entry_id)` returns `True` for both non-chunked
  (`_by_entry`) and chunked (`_chunks_by_entry`) entries
- [x] `_call_graphiti_add_episode` skips `add_episode_direct` when `has_any_mapping` is True
- [x] `_call_graphiti_add_episode_chunked` skips the chunk loop when `has_any_mapping` is True
- [x] `_bulk_index_impl` does not enqueue tasks for entries present in the index
- [x] `_bulk_index_impl` returns `already_indexed` count in its response JSON
- [x] All guards are no-ops when `entry_episode_index is None`
- [x] Skipped entries emit a `debug`-level log with `entry_id` and reason
- [x] No new helper functions introduced — Guard 3 uses existing `_get_cached_provenance_index`

### Tests

**`tests/unit/test_entry_episode_index.py`** — new `TestHasAnyMapping` class:
- `has_any_mapping` returns `False` for unknown entry
- `has_any_mapping` returns `True` after `add()` (non-chunked path)
- `has_any_mapping` returns `True` after `add_chunk_mapping()` (chunked path, confirming
  the `_chunks_by_entry` check fires — this is the correctness case the plan guards against)

**`tests/unit/test_memory_sync.py`** — new cases:
- `_call_graphiti_add_episode`: `has_any_mapping=True` → `add_episode_direct` NOT called,
  returns `success=True, skipped=True`
- `_call_graphiti_add_episode_chunked`: `has_any_mapping=True` → chunk loop NOT entered,
  returns `success=True, skipped=True`
- `_call_graphiti_add_episode`: `entry_episode_index is None` → `add_episode_direct` still
  called (guard is a no-op)

**`tests/unit/test_bulk_index.py`** (or `test_memory_tools.py`) — new cases:
- `_bulk_index_impl` with some entries in index → those entries not enqueued,
  `already_indexed` count correct
- `_bulk_index_impl` with index file absent → all entries enqueued normally

**Phase 3 — chunk method coverage (independent of this fix):**
Add `TestEntryEpisodeIndexChunkMethods` covering `add_chunk_mapping`, `has_chunk`,
`get_episode_for_chunk`, `get_chunks_for_entry`, `get_episode_uuids_for_entry`,
`remove_chunks_for_entry`. These are currently untested and the gap exists regardless of
whether Guard 2b ships.

### Non-regression

- [x] Full `test_memory_sync.py` suite passes
- [x] Full `test_entry_episode_index.py` suite passes

---

## Implementation Phases

### Phase 1: `has_any_mapping` + Guards 1, 2a

**Files:**
- `src/watercooler_memory/entry_episode_index.py` — add `has_any_mapping`
- `src/watercooler_mcp/memory_sync.py` — Guards 1 and 2a

Steps:
1. Add `has_any_mapping(entry_id)` to `EntryEpisodeIndex` (3 lines)
2. Add Guard 1 to `_call_graphiti_add_episode` (4 lines)
3. Add Guard 2a to `_call_graphiti_add_episode_chunked` (5 lines)
4. Write tests: `TestHasAnyMapping` + two new `test_memory_sync.py` cases

### Phase 2: Guard 3 in `_bulk_index_impl`

**Files:** `src/watercooler_mcp/tools/memory.py`

Steps:
1. Add `already_indexed` counter + index load using `_get_cached_provenance_index`
2. Add `has_any_mapping` skip inside entry enumeration loop
3. Surface `already_indexed` in tool response JSON
4. Write tests: bulk_index with entries in index, bulk_index with absent index file

### Phase 3: Chunk method test coverage (independent)

**File:** `tests/unit/test_entry_episode_index.py`

Add `TestEntryEpisodeIndexChunkMethods`. This is independent technical debt that should
ship regardless of the Guard 2b decision.

---

## Dependencies & Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Index absent (first run) | Low | `_get_cached_provenance_index` returns `None`; guards are no-ops |
| Index file corrupt | Very low | `EntryEpisodeIndex.load()` catches `JSONDecodeError`, returns empty index |
| `track_entry_episodes=False` | Intentional | All guards check `is not None` first |
| Concurrent `bulk_index` calls | Low | Queue's `_find_duplicate` is the correctness barrier; stale snapshot only affects `already_indexed` counter |
| Entries from non-chunked and chunked paths mixed in same index file | Normal | `has_any_mapping` checks both dicts; correct for both |

---

## References

### Internal

- `src/watercooler_mcp/memory_sync.py:36` — `_call_graphiti_add_episode`
- `src/watercooler_mcp/memory_sync.py:123` — `_call_graphiti_add_episode_chunked`
- `src/watercooler_mcp/tools/memory.py:76` — `_get_cached_provenance_index` (reuse for Guard 3)
- `src/watercooler_mcp/tools/memory.py:1381` — `_bulk_index_impl` entry enumeration loop
- `src/watercooler_memory/entry_episode_index.py:312` — `has_entry` (checks `_by_entry` only)
- `src/watercooler_memory/entry_episode_index.py:196` — `_chunks_by_entry` dict declaration
- `src/watercooler_memory/entry_episode_index.py:330` — `add_chunk_mapping` (writes `_by_chunk` and `_chunks_by_entry` only)
- `src/watercooler_memory/backends/graphiti.py:2585` — `find_episode_by_chunk_id_async` (not used by this fix)
- `tests/unit/test_memory_sync.py` — existing memory sync tests
- `tests/unit/test_entry_episode_index.py` — existing index tests (chunk methods untested)

### Related Issues

- #243 — Bug: `bulk_index` re-queues already-indexed entries
- #244 — Feature: wire dedup guard in queue worker
