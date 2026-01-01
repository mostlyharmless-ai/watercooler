# Tier Migration Guide

This guide explains how to migrate existing Watercooler threads to a memory backend (Graphiti or LeanRAG) for enhanced search and analysis capabilities.

## Overview

Watercooler supports three search tiers:

| Tier | Backend | Features | Requirements |
|------|---------|----------|--------------|
| Free | Baseline Graph | Keyword search, metadata filters | None (works out-of-box) |
| Standard | Graphiti | Entity extraction, temporal queries, semantic search | FalkorDB, embedding server |
| Advanced | LeanRAG | Hierarchical clustering, cross-thread insights | Graphiti + LeanRAG pipeline |

Migration moves your existing thread entries from the baseline graph to a memory backend, enabling advanced search features.

## Prerequisites

Before migrating, ensure you have:

### 1. Memory Backend Configured

**For Graphiti:**

```bash
# Environment variables
export WATERCOOLER_MEMORY_BACKEND=graphiti
export WATERCOOLER_GRAPHITI_ENABLED=1

# FalkorDB running
docker run -d -p 6379:6379 falkordb/falkordb:latest

# Embedding server (optional but recommended)
# See docs/MEMORY.md for setup
```

**Or in `~/.watercooler/config.toml`:**

```toml
[memory]
backend = "graphiti"

[memory.graphiti]
enabled = true

[memory.embedding]
api_base = "http://localhost:8080/v1"
model = "bge-m3"
dimension = 1024
```

### 2. Threads Repository

Your threads must be in a Watercooler-compatible format:
- Markdown files with structured entries
- Each entry has ID, timestamp, agent, role, and type metadata

## Migration Process

### Step 1: Run Preflight Checks

Before migrating, verify prerequisites with the preflight tool:

```python
# Via MCP tool
result = watercooler_migration_preflight(
    code_path="/path/to/repo",
    backend="graphiti"
)
```

The preflight check verifies:
- Threads directory exists and contains threads
- Target backend is available and configured
- Estimates number of entries to migrate
- Detects any existing checkpoint for resume

**Example output:**
```json
{
  "threads_dir_exists": true,
  "thread_count": 15,
  "estimated_entries": 247,
  "backend_available": true,
  "backend_version": "1.0.0",
  "has_checkpoint": false,
  "ready": true,
  "issues": []
}
```

### Step 2: Dry Run (Recommended)

Preview what would be migrated without making changes:

```python
# Dry run is the default
result = watercooler_migrate_to_memory_backend(
    code_path="/path/to/repo",
    backend="graphiti",
    dry_run=True  # Default
)
```

**Example output:**
```json
{
  "dry_run": true,
  "backend": "graphiti",
  "entries_migrated": 0,
  "threads_processed": 15,
  "would_migrate": [
    {
      "topic": "auth-feature",
      "entry_id": "01ABC123",
      "timestamp": "2025-01-15T10:00:00Z",
      "agent": "Claude (dev)",
      "body_preview": "Implemented OAuth2 authentication with..."
    },
    // ... more entries
  ]
}
```

### Step 3: Execute Migration

When ready, run the actual migration:

```python
result = watercooler_migrate_to_memory_backend(
    code_path="/path/to/repo",
    backend="graphiti",
    dry_run=False
)
```

**Example output:**
```json
{
  "dry_run": false,
  "backend": "graphiti",
  "entries_migrated": 247,
  "entries_failed": 0,
  "entries_skipped": 0,
  "threads_processed": 15,
  "success": true
}
```

## Migration Options

### Filter by Topics

Migrate specific threads only:

```python
watercooler_migrate_to_memory_backend(
    code_path="/path/to/repo",
    backend="graphiti",
    topics="auth-feature,api-design",  # Comma-separated
    dry_run=False
)
```

### Skip Closed Threads

Migrate only active threads:

```python
watercooler_migrate_to_memory_backend(
    code_path="/path/to/repo",
    backend="graphiti",
    skip_closed=True,
    dry_run=False
)
```

### Resume Interrupted Migration

If migration is interrupted, it automatically resumes from checkpoint:

```python
# Checkpoint is saved after each entry
# Simply re-run to continue
watercooler_migrate_to_memory_backend(
    code_path="/path/to/repo",
    backend="graphiti",
    resume=True,  # Default
    dry_run=False
)
```

The checkpoint file (`.migration_checkpoint.json`) tracks:
- Which entries have been migrated
- Target backend
- Last update timestamp

## Post-Migration

### Verify Migration

Check that entries are searchable:

```python
# Search using memory backend
result = watercooler_search(
    code_path="/path/to/repo",
    query="authentication",
    backend="graphiti",
    mode="entries"
)

# Search for entities (Graphiti only)
result = watercooler_search(
    code_path="/path/to/repo",
    query="OAuth2",
    backend="graphiti",
    mode="entities"
)
```

### Clean Up Checkpoint

After successful migration, you can remove the checkpoint file:

```bash
rm /path/to/threads/.migration_checkpoint.json
```

## Troubleshooting

### Backend Not Available

```json
{
  "success": false,
  "error": "Backend unavailable: Graphiti not enabled"
}
```

**Solution:** Ensure environment variables are set and services are running:

```bash
# Check FalkorDB
docker ps | grep falkordb

# Check environment
echo $WATERCOOLER_GRAPHITI_ENABLED
```

### Entries Failed

```json
{
  "entries_migrated": 100,
  "entries_failed": 5,
  "errors": [
    "Entry 01ABC123: Connection timeout"
  ]
}
```

**Solution:** Check backend logs, then resume:

```python
# Resume will skip already-migrated entries
watercooler_migrate_to_memory_backend(
    code_path="/path/to/repo",
    backend="graphiti",
    resume=True,
    dry_run=False
)
```

### Thread Parsing Errors

```json
{
  "issues": [
    "Error parsing auth-feature.md: Invalid entry format"
  ]
}
```

**Solution:** Verify thread file format matches Watercooler structure:

```markdown
# Thread: Auth Feature
Status: OPEN
Ball: Claude

---

## Entry 1

**Agent**: Claude (dev)
**Role**: implementer
**Type**: Note
**Timestamp**: 2025-01-15T10:00:00Z
**ID**: 01ABC123

Entry content here.
```

## Best Practices

1. **Always dry-run first** - Review what will be migrated before executing
2. **Migrate in batches** - Use topic filters for large repositories
3. **Keep backups** - Migration is additive but checkpoint enables resume
4. **Monitor progress** - Check `entries_migrated` vs `entries_failed`
5. **Test search after** - Verify entries are searchable in target backend

## See Also

- [Configuration Guide](CONFIGURATION.md) - Memory backend configuration
- [Memory Documentation](MEMORY.md) - Memory backend details
- [MCP Server Reference](mcp-server.md) - Tool documentation
