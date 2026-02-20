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
│     middleware.py (run_with_sync) │ sync/ (git primitives)      │
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
├── sync_state.json       # Per-topic graph→markdown sync status
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

## Thread Storage & Git Sync

Threads live on an **orphan branch** (`watercooler/threads`) inside the code
repository, accessed via a git worktree at `~/.watercooler/worktrees/<repo>/`
(where `<repo>` is your repository's directory name).

```
Code Repo (main, feature/…)
  └── orphan branch: watercooler/threads   ← thread files live here
        accessed via worktree at ~/.watercooler/worktrees/<repo>/
```

### Why an orphan branch?

- **Clean separation**: Thread commits never appear in code history or trigger CI
- **Single repo**: No companion `-threads` repository to manage
- **Automatic setup**: `_ensure_worktree()` creates the branch and worktree on
  first write — zero manual steps

### Sync Flow

Every write operation follows a simple linear flow:

```
lock → pull → write → commit → push (with rebase+retry)
```

**Implementation:**
- `middleware.py` → `run_with_sync()` orchestrates the flow
- `sync/__init__.py` → per-topic advisory locks (serialize concurrent writes)
- `sync/primitives.py` → git operations (fetch, pull, push with retry)
- `sync/errors.py` → rich exception hierarchy

### Branch Scoping via Metadata

Entries are tagged with `code_branch` metadata (auto-populated from the current
code branch). Read operations filter by `code_branch` so you only see entries
relevant to the branch you're working on. Pass `code_branch="*"` to see all
entries across branches.

### Commit Footers

Every threads commit includes metadata linking back to the code context:

```
Code-Repo: org/watercooler-cloud
Code-Branch: feature/auth
Code-Commit: abc1234
Watercooler-Entry-ID: 01ARZ3NdgoZmqjDLLsrwNlM2S53
Watercooler-Topic: feature-auth
```

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
[agent]
name = "Claude Code"
default_spec = "implementer"

[git]
author = "Your Name"
email = "you@example.com"

[sync]
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

**Sync Tools** (`tools/sync.py`):
- `watercooler_health` - Git and system health check

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
│   Git Sync              │
│  • git add/commit       │
│  • push (rebase+retry)  │
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
│       │   ├── graph.py          # enrich, recover, health
│       │   ├── diagnostic.py     # diagnostic tools
│       │   ├── memory.py         # memory backend tools
│       │   ├── migration.py      # migration tools
│       │   └── sync.py           # health, sync tools
│       ├── sync/                 # Git sync primitives
│       │   ├── __init__.py       # Advisory locking, topic sanitization
│       │   ├── primitives.py     # Git operations (fetch, pull, push, stash)
│       │   └── errors.py         # Rich exception hierarchy
│       ├── server.py             # FastMCP server
│       ├── middleware.py         # run_with_sync wrapper
│       ├── validation.py         # Context validation
│       └── config.py             # MCP config (orphan branch, worktree)
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
