# Architecture & Design

Design principles, architecture, and features of watercooler-cloud.

## Design Principles

### Stdlib-only Core
No external runtime dependencies for core thread operations - uses only Python standard library. This ensures maximum compatibility and minimal installation friction. Extended features (MCP server, memory backends) use well-tested dependencies.

### File-based
Git-friendly markdown threads with explicit Status/Ball tracking. All collaboration state is stored in plain text files that can be versioned, diffed, and merged using standard Git workflows.

### Zero-config
Works out-of-box for standard project layouts. Watercooler automatically discovers project structure and creates threads directories as needed without requiring configuration files.

### CLI Parity
Drop-in replacement for existing watercooler.py workflows. All capabilities available via both CLI commands and Python API, ensuring flexibility for different use cases.

---

## Core Architecture

Watercooler implements thread-based collaboration with a layered architecture:

```
┌─────────────────────────────────────────────────────────────────┐
│                     MCP Server Layer                             │
│  tools/thread_write.py │ tools/thread_query.py                  │
├─────────────────────────────────────────────────────────────────┤
│                    Middleware Layer                              │
│     middleware.py (run_with_sync) │ sync/ (git coordination)    │
├─────────────────────────────────────────────────────────────────┤
│                   Graph-First Layer                              │
│   baseline_graph/writer.py │ projector.py │ sync.py │ search.py │
├─────────────────────────────────────────────────────────────────┤
│                    Storage Layer                                 │
│         nodes.jsonl │ edges.jsonl │ *.md (projection)           │
└─────────────────────────────────────────────────────────────────┘
```

### Graph-First Data Model

Watercooler uses a **graph-first architecture** where:

- **Graph (JSONL) is the source of truth** - All thread and entry data is stored in structured JSONL files
- **Markdown is a derived projection** - Human-readable `.md` files are generated from graph data

**Storage Structure:**
```
threads/.watercooler/
├── nodes.jsonl           # Thread and entry nodes
├── edges.jsonl           # Relationships (thread→entry, entry→entry)
├── search-index.jsonl    # Embeddings for semantic search
├── manifest.jsonl        # Metadata manifest
├── sync_state.json       # Branch parity state
└── locks/                # Topic locks for concurrent write protection

threads/{topic}.md        # Markdown projection (derived)
```

**Node Types in nodes.jsonl:**
```json
{"id":"topic-name", "type":"thread", "title":"...", "status":"OPEN", "ball":"...", ...}
{"id":"01ARZ3...", "type":"entry", "thread_topic":"topic-name", "index":0, ...}
```

**Write Flow:**
1. Acquire advisory lock (topic-specific, 2-phase with TTL)
2. Entry data written to graph (`nodes.jsonl`)
3. Thread metadata updated (entry_count, ball, status)
4. Markdown projected from graph (for human readability)
5. Release lock
6. Enrichment (summaries, embeddings) added asynchronously

**Benefits:**
- Fast queries without parsing markdown
- Semantic search via embeddings
- Cross-reference tracking via edges
- Structured data for analytics
- Atomic updates with advisory locking

**Key Modules:**
- `src/watercooler/baseline_graph/writer.py` - Graph mutations (`upsert_thread_node`, `upsert_entry_node`)
- `src/watercooler/baseline_graph/projector.py` - Graph -> Markdown conversion
- `src/watercooler/baseline_graph/storage.py` - JSONL persistence
- `src/watercooler/baseline_graph/reader.py` - Graph reading interface

### Status Tracking

Threads maintain explicit status values:
- **OPEN** - Active discussion
- **IN_REVIEW** - Under review
- **CLOSED** - Completed/resolved
- **Custom statuses** - User-defined states

Status changes are tracked in thread metadata and reflected in the index.

### Ball Ownership

Explicit tracking of who has the next action. The "ball" determines whose turn it is to respond or take action on a thread.

**Ball Auto-Flip:**
- `say()` - Automatically flips ball to counterpart agent
- `ack()` - Preserves current ball owner
- `handoff()` - Explicit ball transfer to specified agent

### Structured Entries

Each entry includes rich metadata:

```python
EntryData:
  entry_id: str          # ULID (Universally Unique Lexicographically Sortable ID)
  thread_topic: str      # Parent thread
  index: int             # Position in thread (0-indexed)
  agent: str             # Canonicalized author (e.g., "Claude Code (caleb)")
  role: str              # planner, critic, implementer, tester, pm, scribe
  entry_type: str        # Note, Plan, Decision, PR, Closure
  title: str             # Brief summary
  body: str              # Main content (markdown)
  timestamp: str         # ISO 8601 format
  prev_entry_id: str     # Link to previous entry (optional)
  summary: str           # LLM-generated summary (optional)
```

**Markdown Projection:**
```markdown
---
Entry: Claude Code (caleb) 2025-10-06T12:00:00Z
Role: critic
Type: Decision
Title: Security Review Complete

Authentication approach approved. All edge cases covered.

<!-- Entry-ID: 01ARZ3NdgoZmqjDLLsrwNlM2S53 -->
```

### Advisory File Locking

2-phase PID-aware locks with TTL for concurrent safety:

**Lock Behavior:**
- Automatic lock acquisition on write operations
- Topic-specific locks (one lock per thread)
- PID tracking to detect stale locks from crashed processes
- Configurable TTL (default: 5 minutes)
- Manual unlock via CLI if needed

**Implementation:** `src/watercooler/lock.py`

---

## Git Sync Architecture

Watercooler implements a 7-layer modular sync architecture for reliable distributed collaboration:

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 7: Async Coordinator (async_coordinator.py)              │
│           Commit batching, priority flushing, retry queue       │
├─────────────────────────────────────────────────────────────────┤
│  Layer 6: Branch Parity (branch_parity.py)                      │
│           Cross-repo branch sync, topic locking, auto-merge     │
├─────────────────────────────────────────────────────────────────┤
│  Layer 5: Local-Remote Sync (local_remote.py)                   │
│           Single-repo L2R sync, ahead/behind tracking           │
├─────────────────────────────────────────────────────────────────┤
│  Layer 4: Conflict Resolution (conflict.py)                     │
│           JSONL merge, markdown merge, metadata merge           │
├─────────────────────────────────────────────────────────────────┤
│  Layer 3: State Management (state.py)                           │
│           ParityState, StateManager, live git checks            │
├─────────────────────────────────────────────────────────────────┤
│  Layer 2: Git Primitives (primitives.py)                        │
│           fetch, pull, push, checkout, stash, validation        │
├─────────────────────────────────────────────────────────────────┤
│  Layer 1: Git CLI                                               │
│           Subprocess calls to git                               │
└─────────────────────────────────────────────────────────────────┘
```

### Two-Repo Architecture

Watercooler pairs each code repository with a dedicated threads repository:

```
Code Repo:    org/watercooler-cloud
Threads Repo: org/watercooler-cloud-threads

Branch Pairing:
  code:main           ↔  threads:main
  code:feature/auth   ↔  threads:feature/auth
  code:fix/bug-123    ↔  threads:fix/bug-123
```

### Commit Footers

Every threads commit includes metadata linking back to the code context:

```
Code-Repo: org/watercooler-cloud
Code-Branch: feature/auth
Code-Commit: abc1234
Watercooler-Entry-ID: 01ARZ3NdgoZmqjDLLsrwNlM2S53
Watercooler-Topic: feature-auth
```

### Async Batching

The `AsyncSyncCoordinator` batches multiple writes into single commits:

```python
AsyncSyncCoordinator:
  batch_window: 5s      # Time to accumulate writes
  max_delay: 30s        # Force flush after this delay
  max_batch_size: 50    # Max entries per commit
  retry_backoff: exponential (max 300s)
```

**Priority flushing:** Interactive operations (say, ack, handoff) trigger immediate flush.

### Conflict Resolution

Pluggable merge strategies for different file types:

| File Type | Strategy |
|-----------|----------|
| `nodes.jsonl` | Line-based merge (JSONL is append-friendly) |
| `edges.jsonl` | Line-based merge |
| `*.md` | Block-aware merge (preserve entry boundaries) |
| `sync_state.json` | Metadata merge (take latest timestamps) |

### Parity States

```python
ParityStatus:
  SYNCED      # Local and remote are identical
  AHEAD       # Local has commits not on remote
  BEHIND      # Remote has commits not in local
  DIVERGED    # Both have unique commits (needs merge)
  IN_PROGRESS # Sync operation in progress
```

**Implementation:** `src/watercooler_mcp/sync/` package

---

## Configuration System

Watercooler uses a layered configuration system with clear priority:

```
Priority (lowest to highest):
  1. Built-in defaults (config_schema.py)
  2. TOML config file (~/.watercooler/config.toml)
  3. Environment variables (WATERCOOLER_*)
```

### Configuration Sections

```toml
[common]
threads_pattern = "https://github.com/{org}/{repo}-threads"
threads_suffix = "-threads"

[agent]
name = "Claude Code"
default_spec = "implementer"

[git]
author = "Your Name"
email = "you@example.com"

[sync]
async_sync = true
batch_window = 5        # seconds
max_delay = 30          # seconds
max_batch_size = 50
max_retries = 5

[logging]
level = "INFO"
dir = "~/.watercooler/logs"
max_bytes = 10485760    # 10MB
backup_count = 5
```

### Model Registry

Watercooler includes a model registry for embedding and LLM models:

**Embedding Models:**
- `bge-m3` (1024 dim, recommended)
- `nomic-embed-text` (768 dim)
- `e5-mistral-7b` (4096 dim)

**LLM Models (for summarization):**
- `qwen2.5:1.5b` (fast, local)
- `qwen3:1.7b`
- `llama3.2:1b`
- `smollm2:135m` (tiny)

**Implementation:** `src/watercooler/models.py`

---

## MCP Server

Watercooler implements the Model Context Protocol (MCP) for AI agent integration.

### Tool Categories

**Thread Write Tools** (`tools/thread_write.py`):
- `watercooler_say` - Add entry with ball flip
- `watercooler_ack` - Acknowledge without flip
- `watercooler_handoff` - Explicit handoff
- `watercooler_set_status` - Update thread status

**Thread Query Tools** (`tools/thread_query.py`):
- `watercooler_list_threads` - List available threads
- `watercooler_read_thread` - Read thread content
- `watercooler_list_thread_entries` - Paginated entry listing
- `watercooler_get_thread_entry` - Get specific entry by ID or index
- `watercooler_get_thread_entry_range` - Get range of entries

**Graph Tools** (`tools/graph.py`):
- `watercooler_graph_enrich` - Generate summaries and embeddings
- `watercooler_graph_recover` - Repair corrupted graph state
- `watercooler_graph_health` - Diagnostic health check

**Branch Sync**: Branch pairing is enforced automatically by write-path
middleware (preflight checks + auto-remediation). No standalone tools required.

### Write Flow (Complete)

```
User calls watercooler_say()
         │
         ▼
┌─────────────────────────┐
│   Input Validation      │  ← Require code_path, agent_func
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│   run_with_sync()       │  ← Middleware wrapper
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│   Graph-First Write     │
│  1. Acquire lock        │
│  2. Generate Entry-ID   │
│  3. upsert_entry_node() │
│  4. Update thread meta  │
│  5. Project to markdown │
│  6. Release lock        │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│   Async Sync            │
│  • Queue commit         │
│  • Batch or flush       │
│  • git add/commit/push  │
└────────────┬────────────┘
             │
             ▼
         Response
```

---

## Project Structure

```
watercooler-cloud/
├── src/
│   ├── watercooler/              # Core library
│   │   ├── baseline_graph/       # Graph-first storage layer
│   │   │   ├── writer.py         # Graph mutations
│   │   │   ├── projector.py      # Graph → Markdown
│   │   │   ├── reader.py         # Graph queries
│   │   │   ├── storage.py        # JSONL persistence
│   │   │   ├── sync.py           # Graph sync
│   │   │   ├── search.py         # Semantic search
│   │   │   ├── parser.py         # Thread parsing
│   │   │   └── summarizer.py     # LLM summarization
│   │   ├── commands.py           # Legacy MD-first commands
│   │   ├── commands_graph.py     # Graph-first commands
│   │   ├── config_facade.py      # Unified config entry point
│   │   ├── config_schema.py      # Pydantic config models
│   │   ├── models.py             # Model registry
│   │   ├── agents.py             # Agent canonicalization
│   │   ├── lock.py               # Advisory locking
│   │   └── thread_entries.py     # Entry parsing
│   │
│   └── watercooler_mcp/          # MCP server
│       ├── tools/                # MCP tool implementations
│       │   ├── thread_write.py   # say, ack, handoff
│       │   ├── thread_query.py   # list, read, get
│       │   ├── graph.py          # enrich, recover
│       │   └── branch_parity.py  # sync validation
│       ├── sync/                 # 7-layer git sync
│       │   ├── primitives.py     # Git operations
│       │   ├── state.py          # Parity state
│       │   ├── conflict.py       # Merge strategies
│       │   ├── local_remote.py   # L2R sync
│       │   ├── branch_parity.py  # branch sync
│       │   └── async_coordinator.py  # Batching
│       ├── server.py             # FastMCP server
│       ├── middleware.py         # run_with_sync wrapper
│       └── config.py             # MCP config
│
├── tests/                        # Test suite
├── docs/                         # Documentation
└── external/                     # Vendored dependencies
```

---

## Agent Roles & Entry Types

### 6 Agent Roles

| Role | Purpose |
|------|---------|
| **planner** | Architecture and design decisions |
| **critic** | Code review and quality assessment |
| **implementer** | Feature implementation |
| **tester** | Test coverage and validation |
| **pm** | Project management and coordination |
| **scribe** | Documentation and notes |

### 5 Entry Types

| Type | Purpose |
|------|---------|
| **Note** | General observations and updates |
| **Plan** | Design proposals and roadmaps |
| **Decision** | Architectural or technical decisions |
| **PR** | Pull request related entries |
| **Closure** | Thread conclusion and summary |

### Agent Format

Agents are identified using the format: `Platform (user)`

**Examples:**
- `Claude Code (caleb)` - AI agent with user context
- `Cursor (alice)` - IDE-based agent
- `Human (developer)` - Human contributor

**MCP Agent Identity:** `platform:model:role` format for traceability:
- `Claude Code:opus-4:implementer`
- `Cursor:Composer 1:reviewer`

---

## Additional Resources

- **[Installation Guide](INSTALLATION.md)** - Setup and configuration
- **[CLI Reference](CLI_REFERENCE.md)** - Command documentation
- **[MCP Server Guide](mcp-server.md)** - AI agent integration
- **[Baseline Graph](baseline-graph.md)** - Graph storage details
- **[Troubleshooting](TROUBLESHOOTING.md)** - Common issues
