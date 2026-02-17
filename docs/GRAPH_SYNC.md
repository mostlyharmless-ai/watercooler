# Watercooler Graph Sync

This document describes the synchronization contract between Watercooler’s
**baseline graph (JSONL)** and the **human-readable markdown thread view**.

**Forward-looking contract (source of truth):**
- **Baseline graph JSONL is canonical** (`graph/baseline/*` in the threads repo)
- **Markdown is a derived projection** (maintained for human usability)

## Overview

Every write operation (`say`, `ack`, `handoff`, `set_status`) produces a canonical
update to the baseline graph JSONL, and then updates (or regenerates) the markdown
projection.

This enables:

- **Fast queries**: Read operations can query the graph instead of parsing markdown
- **Semantic search**: Graph nodes include embeddings for similarity search
- **Cross-references**: Automatic detection of references between threads/entries
- **Usage analytics**: Access counting for identifying hot topics

## Architecture

```
Write Operation
      │
      ▼
┌──────────────────────┐
│ Baseline Graph Write │  ← Source of truth (JSONL)
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Markdown Projection  │  ← Derived view (human)
└──────────────────────┘
```

### Key Principles

1. **Graph JSONL is source of truth** - Markdown is a derived view
2. **Entry validity** - An entry is “real” only when it exists in the graph JSONL
3. **Non-blocking projection** - Markdown projection failures do not invalidate the graph write
4. **Atomic operations** - JSONL writes use temp file + rename (and projections should be atomic too)
5. **Eventually consistent** - Reconciliation tools fix drift between graph and projections
6. **Per-topic locking** - Concurrent writes serialized per topic

## Storage Format

Canonical graph data is stored in JSONL format at `{threads-repo}/graph/baseline/`:

```
graph/baseline/
├── nodes.jsonl      # Thread and entry nodes
├── edges.jsonl      # Relationships between nodes
├── manifest.json    # Metadata and checksums
└── sync_state.json  # Per-topic sync status
```

### Node Schema

**Thread Node:**
```json
{
  "id": "thread:feature-auth",
  "type": "thread",
  "topic": "feature-auth",
  "title": "Authentication Feature Thread",
  "status": "OPEN",
  "ball": "Claude (user)",
  "last_updated": "2025-01-15T10:30:00Z",
  "summary": "Discussion about OAuth2 implementation...",
  "entry_count": 12
}
```

**Entry Node:**
```json
{
  "id": "entry:01KB6VPBN440PJEYBV3RWYW9NC",
  "type": "entry",
  "entry_id": "01KB6VPBN440PJEYBV3RWYW9NC",
  "thread_topic": "feature-auth",
  "index": 0,
  "agent": "Claude Code (user)",
  "role": "planner",
  "entry_type": "Plan",
  "title": "Authentication Architecture Plan",
  "timestamp": "2025-01-15T10:00:00Z",
  "summary": "Proposed OAuth2 with PKCE flow...",
  "file_refs": ["src/auth/oauth.py"],
  "pr_refs": ["#123"],
  "commit_refs": ["abc1234"]
}
```

### Edge Schema

```json
{"source": "thread:feature-auth", "target": "entry:01KB...", "type": "contains"}
{"source": "entry:01KB...", "target": "entry:01KC...", "type": "followed_by"}
{"source": "entry:01KB...", "target": "thread:other-topic", "type": "references_thread"}
```

## Sync State

Each topic tracks its sync status in `sync_state.json`:

```json
{
  "topics": {
    "feature-auth": {
      "status": "ok",
      "last_synced_entry_id": "01KC0534JYTZS6Y915MHBJ432J",
      "last_sync_at": "2025-01-15T10:30:00Z",
      "error_message": null,
      "entries_synced": 12
    }
  },
  "last_updated": "2025-01-15T10:30:00Z"
}
```

### Status Values

| Status | Description |
|--------|-------------|
| `ok` | Markdown projection is in sync with the graph |
| `error` | Projection failed - see error_message |
| `pending` | Projection queued but not yet complete |

## Failure Handling

When markdown projection fails:

1. **Error is logged** and recorded in sync state
2. **Graph write remains authoritative**
3. **Reconciliation can regenerate markdown from graph**

This ensures projection issues never corrupt or block the canonical record.

## Health Checking

Check graph sync health:

```bash
# CLI (coming soon)
watercooler graph-health

# MCP tool (coming soon)
watercooler_v1_graph_health
```

Health report includes:
- Total threads in directory
- Threads with successful sync
- Threads with sync errors
- Stale threads (no sync state)

## Reconciliation

Fix drift by reconciling markdown projections with the graph:

```python
from watercooler.baseline_graph.sync import reconcile_graph

# Reconcile all stale/error topics
results = reconcile_graph(threads_dir)

# Reconcile specific topics
results = reconcile_graph(threads_dir, topics=["feature-auth"])
```

Reconciliation:
1. Identifies stale/error topics
2. Rebuilds markdown projections from graph JSONL
3. Updates sync state

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WATERCOOLER_GRAPH_SYNC` | `1` | Enable graph sync on writes |
| `WATERCOOLER_GRAPH_SUMMARIES` | `0` | Generate LLM summaries (slow) |

### Disabling Graph Sync

To disable graph sync (e.g., for performance testing):

```bash
export WATERCOOLER_GRAPH_SYNC=0
```

## Concurrency

Graph sync is safe under concurrent writes:

1. **Per-topic locking**: MCP operations acquire topic lock before write
2. **Atomic JSONL writes**: temp file + rename prevents corruption
3. **Deduplication**: JSONL append merges by node/edge ID

## Conflict Resolution

The sync layer handles conflicts between concurrent writers using a JSONL-native
strategy that preserves all writes while avoiding duplicate entries.

### JSONL Conflict Strategy

**Key Insight**: JSONL is append-only, so "conflicts" are really just duplicate
entries with the same ID that need deduplication.

**Resolution Steps**:
1. Read existing JSONL into a dict keyed by node/edge ID
2. Apply new entries (upsert semantics - newer wins)
3. Write merged result atomically (temp file + rename)

```python
# From sync/conflict.py
def upsert_jsonl_nodes(path: Path, new_nodes: list[dict]) -> int:
    """Upsert nodes into JSONL file.

    Args:
        path: Path to nodes.jsonl
        new_nodes: List of node dicts with 'id' field

    Returns:
        Number of nodes written (new + updated)
    """
    existing = _read_jsonl_as_dict(path, key="id")
    for node in new_nodes:
        existing[node["id"]] = node  # Upsert
    _write_jsonl_atomic(path, list(existing.values()))
    return len(new_nodes)
```

### Markdown Conflict Strategy

Markdown projections are append-only (new entries at end), which makes them
inherently conflict-resistant. However, metadata updates (status, ball) can
conflict.

**Resolution**: On markdown projection, regenerate from graph JSONL source of
truth rather than patching existing markdown.

### Git Merge Conflicts

When git pull/rebase encounters conflicts in graph JSONL:

1. **Ours strategy**: Keep local version, retry push
2. **Theirs strategy**: Accept remote version, re-apply local changes
3. **Union strategy**: Merge both (default for JSONL)

The `sync/primitives.py` module handles these cases:

```python
def resolve_jsonl_merge_conflict(path: Path) -> bool:
    """Resolve JSONL merge conflict by keeping both sides.

    JSONL files can safely keep both sides of a conflict since
    duplicate entries are deduplicated on next upsert.
    """
    # Read both sides from conflict markers
    ours, theirs = _parse_conflict_markers(path)
    merged = _merge_jsonl_entries(ours, theirs)
    _write_jsonl_atomic(path, merged)
    return True
```

### Multi-Writer Scenarios

| Scenario | Resolution |
|----------|------------|
| Two agents write same topic simultaneously | Per-topic lock serializes writes |
| Dashboard and MCP write same entry | JSONL upsert keeps latest timestamp |
| Slack and Dashboard update status | Last write wins (by timestamp) |
| Git push race condition | Rebase + retry (up to 3 attempts) |

## Performance

### Sync Latency

| Operation | Typical Time |
|-----------|--------------|
| Entry sync (no summaries) | ~10-50ms |
| Entry sync (with LLM summary) | ~500-2000ms |
| Full thread sync | ~50-200ms |
| Reconcile all topics | ~1-5s |

### Optimization Tips

1. **Disable LLM summaries** for fast writes (`generate_summaries=False`)
2. **Use extractive summaries** when needed (faster than LLM)
3. **Batch reconciliation** during low-activity periods

## Known Limitations

### LLM Summaries and Embeddings Disabled by Default

⚠️ **Current Status**: LLM summaries and embedding vector generation are **disabled by default**.

| Feature | Default | Impact When Disabled |
|---------|---------|---------------------|
| `generate_summaries` | `false` | Summaries are truncated body text (~200 chars) |
| `generate_embeddings` | `false` | Semantic search falls back to keyword matching |

**Why disabled?** These features require llama-server to be running. Enabling them without the service running would cause errors.

**To enable LLM features:**

1. llama-server auto-starts when configured for localhost endpoints
2. Models auto-download from HuggingFace on first use
3. Update your config (`~/.watercooler/config.toml`):
   ```toml
   [mcp.graph]
   generate_summaries = true
   generate_embeddings = true

   [memory.llm]
   api_base = "http://localhost:8000/v1"
   model = "qwen3:1.7b"

   [memory.embedding]
   api_base = "http://localhost:8080/v1"
   model = "bge-m3"
   ```

**Service availability is checked automatically** before each enrichment attempt:
- LLM and embedding services are checked independently
- Partial enrichment is supported (e.g., generate summary if LLM available, skip embedding if not)
- Unavailable services result in a debug log message, not an error
- Entries are saved without enrichment; use `watercooler_backfill_graph` to add later

### Access Counters Disabled

The odometer/access counter feature is currently disabled because counter writes dirty the working tree and block auto-sync. This will be re-enabled with a chosen sync strategy in a future release.

### O(n) Node Scanning

Graph reads scan the entire `nodes.jsonl` linearly. For graphs with 1000+ threads, lookups may take 100ms+. Node indexing is planned for future releases.

## Future Enhancements

1. **Graph read operations** - Query graph instead of parsing markdown
2. **Unified search** - Keyword, semantic, and time-boxed search via graph
3. **Odometer counters** - Track access counts for analytics
4. **FalkorDB backend** - Optional graph database for complex queries
5. **Incremental sync** - Only sync changed entries (sidecar index)

## Related Documentation

- Thread: `graph-driven-mcp-architecture` - Architecture planning
- Thread: `baseline-graph-thread-parser` - Parser implementation
- Thread: `baseline-graph-enhancements` - Cross-references and summaries
