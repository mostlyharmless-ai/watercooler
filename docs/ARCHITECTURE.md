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
│  tools/thread_write.py │ tools/thread_query.py │ tools/memory.py│
├─────────────────────────────────────────────────────────────────┤
│                    Middleware Layer                              │
│     middleware.py (run_with_sync) │ sync/ (git coordination)    │
├─────────────────────────────────────────────────────────────────┤
│                   Graph-First Layer                              │
│   baseline_graph/writer.py │ projector.py │ sync.py │ search.py │
├─────────────────────────────────────────────────────────────────┤
│                    Memory Layer                                  │
│  T1 (Baseline JSONL) │ T2 (Graphiti/FalkorDB) │ T3 (LeanRAG)   │
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
7. Memory backend sync (Graphiti/LeanRAG indexing)

**Benefits:**
- Fast queries without parsing markdown
- Semantic search via embeddings
- Cross-reference tracking via edges
- Structured data for analytics
- Atomic updates with advisory locking

**Key Modules:**
- `src/watercooler/baseline_graph/writer.py` - Graph mutations (`upsert_thread_node`, `upsert_entry_node`)
- `src/watercooler/baseline_graph/projector.py` - Graph → Markdown conversion
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

## Multi-Tier Memory System

Watercooler implements a sophisticated three-tier memory architecture for intelligent retrieval:

```
┌─────────────────────────────────────────────────────────────────┐
│                    Smart Query Orchestrator                      │
│              (tier_strategy.py - intent detection)               │
├─────────────────────────────────────────────────────────────────┤
│  T1 (Baseline)    │  T2 (Graphiti)      │  T3 (LeanRAG)         │
│  Cost: 1 unit     │  Cost: 10 units     │  Cost: 100 units      │
│  JSONL + embed    │  FalkorDB temporal  │  Hierarchical cluster │
│  No LLM @ query   │  LLM @ index time   │  LLM @ query time     │
└─────────────────────────────────────────────────────────────────┘
```

### Tier Overview

| Tier | Backend | Cost | LLM Usage | Best For |
|------|---------|------|-----------|----------|
| **T1** | Baseline JSONL | 1 | None at query time | Simple lookups, keyword search |
| **T2** | FalkorDB (Graphiti) | 10 | Entity extraction at index | Entity search, temporal queries, relationships |
| **T3** | LeanRAG | 100 | Clustering + reasoning | Multi-hop reasoning, synthesis, narratives |

### T1: Baseline Graph

- **Storage:** `nodes.jsonl` + `search-index.jsonl` (embeddings)
- **Search:** Keyword matching + cosine similarity on embeddings
- **No LLM calls** during search - just vector operations
- **Always available** (default tier)

### T2: Graphiti (FalkorDB Temporal Graph)

- **Storage:** FalkorDB graph database
- **Entity extraction:** LLM extracts entities/relationships during indexing
- **Temporal queries:** "What was decided last week about auth?"
- **Relationship traversal:** Find connected concepts
- **Enable:** `WATERCOOLER_TIER_T2_ENABLED=1`

**Implementation:** `src/watercooler_memory/backends/graphiti.py`

### T3: LeanRAG (Hierarchical Clustering)

- **Storage:** Hierarchical cluster structure
- **GMM clustering:** Groups related content at multiple levels
- **Multi-hop reasoning:** Synthesizes across clusters
- **Expensive:** Uses LLM for clustering and reasoning
- **Enable:** `WATERCOOLER_TIER_T3_ENABLED=1` (explicit opt-in)

**Implementation:** `src/watercooler_memory/backends/leanrag.py`

### Query Intent Detection

The orchestrator detects query intent to select the optimal starting tier:

| Intent | Starting Tier | Example Query |
|--------|---------------|---------------|
| `LOOKUP` | T1 | "What is the API endpoint for auth?" |
| `ENTITY_SEARCH` | T2 | "Find all discussions about Redis" |
| `TEMPORAL` | T2 | "What changed in the last sprint?" |
| `RELATIONAL` | T2 | "How is auth related to the user service?" |
| `SUMMARIZE` | T2/T3 | "Summarize the architecture decisions" |
| `MULTI_HOP` | T3 | "Trace the evolution of our caching strategy" |

### Escalation Path

```
T1 (Scout) → T2 (Resolve) → T3 (Synthesize)
```

**Escalation triggers:**
- Insufficient results (fewer than `min_results`, default: 3)
- Low confidence scores
- Query intent requires higher tier

**Safety rules:**
- Never escalate to T3 "just to be helpful"
- Never allow T3 to invent facts beyond what T1/T2 provide
- Surface uncertainty explicitly instead of hallucinating

### Unified Result Format

All tiers return normalized `TierEvidence`:

```python
TierEvidence:
  tier: Tier              # T1, T2, or T3
  id: str                 # Backend-specific ID
  content: str            # The actual content
  score: float            # Relevance score (0.0-1.0)
  name: str               # Entity/fact name (optional)
  provenance: dict        # Source tracking
  metadata: dict          # Backend-specific metadata
```

**Implementation:** `src/watercooler_memory/tier_strategy.py`

### Configuration

```toml
[memory]
enabled = true
backend = "graphiti"  # or "leanrag", "null"

[memory.tiers]
t1_enabled = true
t2_enabled = false    # Requires FalkorDB
t3_enabled = false    # Expensive, explicit opt-in
max_tiers = 2         # Don't go beyond T2 by default
min_results = 3       # Escalate if fewer results
min_confidence = 0.5

[memory.embeddings]
api_base = "http://localhost:8080/v1"  # Local llama.cpp server
model = "bge-m3"
dim = 1024

[memory.llm]
api_base = "http://localhost:8000/v1"  # Local llama-server
model = "qwen2.5:1.5b"
```

---

## Git Sync Architecture

Watercooler implements a 7-layer modular sync architecture for reliable distributed collaboration:

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 7: Async Coordinator (async_coordinator.py)              │
│           Commit batching, priority flushing, retry queue       │
├─────────────────────────────────────────────────────────────────┤
│  Layer 6: Branch Parity (branch_parity.py)                      │
│           Cross-repo T2C sync, topic locking, auto-merge        │
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

[memory]
enabled = true
backend = "graphiti"

[memory.tiers]
t1_enabled = true
t2_enabled = false
t3_enabled = false
max_tiers = 2
min_results = 3

[memory.embeddings]
api_base = "http://localhost:8080/v1"
model = "bge-m3"

[memory.llm]
api_base = "http://localhost:8000/v1"
model = "qwen2.5:1.5b"

[logging]
level = "INFO"
dir = "~/.watercooler/logs"
max_bytes = 10485760    # 10MB
backup_count = 5

[slack]
webhook_url = ""        # For notifications
bot_token = ""          # For full API access
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

### Dual Mode Operations

The MCP server supports two operational modes:

**Local Mode (Default):**
- Reads/writes directly to filesystem
- Uses advisory file locking
- Git sync via subprocess

**Hosted Mode:**
- Uses GitHub API for persistence
- Token-based authentication
- Concurrent write handling via API

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

**Memory Tools** (`tools/memory.py`):
- `watercooler_smart_query` - Multi-tier intelligent query with auto-escalation
- `watercooler_search` - Unified search (entries, entities, episodes)
- `watercooler_find_similar` - Find similar entries by embedding

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
│   Mode Detection        │  ← is_hosted_context()?
└────────────┬────────────┘
             │
    ┌────────┴────────┐
    ▼                 ▼
[LOCAL]           [HOSTED]
    │                 │
    ▼                 ▼
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
┌─────────────────────────┐
│   Memory Backend Sync   │  ← Graphiti/LeanRAG indexing
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│   Slack Notification    │  ← Fire and forget
└────────────┬────────────┘
             │
             ▼
         Response
```

---

## HTTP Deployment

The MCP server supports both STDIO (local) and HTTP (hosted) transport modes.

### Transport Modes

**STDIO Mode (Default):**
- Used by local AI assistants (Claude Code, Cursor, etc.)
- Launched as subprocess via MCP configuration
- No network exposure, direct process communication

**HTTP Mode:**
- Used for hosted deployments (Vercel, Railway, etc.)
- Exposes MCP tools via HTTP endpoints
- Supports token-based authentication

### Environment Variables

```bash
# Transport
WATERCOOLER_MCP_TRANSPORT=http  # "stdio" or "http"
WATERCOOLER_MCP_HOST=0.0.0.0
WATERCOOLER_MCP_PORT=8080

# Authentication (Hosted)
WATERCOOLER_AUTH_MODE=hosted
WATERCOOLER_TOKEN_API_URL=https://...
WATERCOOLER_TOKEN_API_KEY=...

# Memory Tiers
WATERCOOLER_TIER_T1_ENABLED=1
WATERCOOLER_TIER_T2_ENABLED=1
WATERCOOLER_TIER_T3_ENABLED=0
WATERCOOLER_TIER_MAX_TIERS=2
```

---

## Slack Integration

Two-phase Slack integration for notifications and bidirectional sync:

**Phase 1: Webhooks (Notifications)**
- Fire-and-forget notifications
- New entry alerts
- Status change alerts

**Phase 2: Bot API (Bidirectional Sync)**
- Full Slack API access
- Thread ↔ Channel mapping
- Message sync

**Note:** Block Kit formatting is implemented in TypeScript (watercooler-site) for the production service. Python implementation in `src/watercooler_mcp/slack/blocks.py` is for reference only.

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
│   │   │   ├── sync.py           # FalkorDB sync
│   │   │   ├── search.py         # Semantic search
│   │   │   ├── parser.py         # Thread parsing
│   │   │   └── summarizer.py     # LLM summarization
│   │   ├── commands.py           # Legacy MD-first commands
│   │   ├── commands_graph.py     # Graph-first commands
│   │   ├── config_facade.py      # Unified config entry point
│   │   ├── config_schema.py      # Pydantic config models
│   │   ├── memory_config.py      # Memory backend config
│   │   ├── models.py             # Model registry
│   │   ├── agents.py             # Agent canonicalization
│   │   ├── lock.py               # Advisory locking
│   │   └── thread_entries.py     # Entry parsing
│   │
│   ├── watercooler_mcp/          # MCP server
│   │   ├── tools/                # MCP tool implementations
│   │   │   ├── thread_write.py   # say, ack, handoff
│   │   │   ├── thread_query.py   # list, read, get
│   │   │   ├── memory.py         # smart_query, search
│   │   │   ├── graph.py          # enrich, recover
│   │   │   └── branch_parity.py  # sync validation
│   │   ├── sync/                 # 7-layer git sync
│   │   │   ├── primitives.py     # Git operations
│   │   │   ├── state.py          # Parity state
│   │   │   ├── conflict.py       # Merge strategies
│   │   │   ├── local_remote.py   # L2R sync
│   │   │   ├── branch_parity.py  # T2C sync
│   │   │   └── async_coordinator.py  # Batching
│   │   ├── slack/                # Slack integration
│   │   ├── server.py             # FastMCP server
│   │   ├── middleware.py         # run_with_sync wrapper
│   │   └── config.py             # MCP config
│   │
│   └── watercooler_memory/       # Memory backends
│       ├── backends/
│       │   ├── graphiti.py       # FalkorDB temporal graph
│       │   ├── leanrag.py        # Hierarchical clustering
│       │   └── null.py           # No-op backend
│       ├── tier_strategy.py      # Multi-tier orchestrator
│       ├── embeddings.py         # Embedding client
│       └── chunker.py            # Document chunking
│
├── tests/                        # Test suite
│   ├── unit/                     # Unit tests
│   └── integration/              # Integration tests
├── docs/                         # Documentation
└── external/                     # Vendored dependencies
    └── graphiti/                 # Graphiti core
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
- **[Graph Sync](GRAPH_SYNC.md)** - Sync architecture
- **[Troubleshooting](TROUBLESHOOTING.md)** - Common issues
