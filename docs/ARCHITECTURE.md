# Architecture & Design

Design principles, architecture, and features of watercooler-cloud.

## Design Principles

### Stdlib-only
No external runtime dependencies - uses only Python standard library for core functionality. This ensures maximum compatibility and minimal installation friction.

### File-based
Git-friendly markdown threads with explicit Status/Ball tracking. All collaboration state is stored in plain text files that can be versioned, diffed, and merged using standard Git workflows.

### Zero-config
Works out-of-box for standard project layouts. Watercooler automatically discovers project structure and creates threads directories as needed without requiring configuration files.

### CLI parity
Drop-in replacement for existing watercooler.py workflows. All capabilities available via both CLI commands and Python API, ensuring flexibility for different use cases.

---

## Architecture

Watercooler implements thread-based collaboration with the following components:

### Graph-First Data Model

Watercooler uses a **graph-first architecture** where:

- **Graph (JSONL) is the source of truth** - All thread and entry data is stored in structured JSONL files at `graph/baseline/threads/{topic}/`
- **Markdown is a derived projection** - Human-readable `.md` files are generated from graph data for convenience

**Write Flow:**
1. Entry data written to graph (`meta.json`, `entries.jsonl`, `edges.jsonl`)
2. Markdown projected from graph (for human readability)
3. Enrichment (summaries, embeddings) added to graph asynchronously

**Benefits:**
- Fast queries without parsing markdown
- Semantic search via embeddings
- Cross-reference tracking
- Structured data for analytics

See [Baseline Graph](baseline-graph.md) and [Graph Sync](GRAPH_SYNC.md) for details.

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
- **Agent** - Entry author with user tagging (e.g., "Claude (agent)")
- **Role** - Specialization (planner, critic, implementer, tester, pm, scribe)
- **Type** - Entry category (Note, Plan, Decision, PR, Closure)
- **Title** - Brief summary
- **Timestamp** - ISO 8601 format
- **Body** - Main content

Example entry structure:
```markdown
---
Entry: Claude (agent) 2025-10-06T12:00:00Z
Role: critic
Type: Decision
Title: Security Review Complete

Authentication approach approved. All edge cases covered.
```

### Agent Registry
Canonical agent names, counterpart mappings, and multi-agent coordination chains.

**Features:**
- Define agent identities and roles
- Map counterpart relationships for automatic ball flipping
- Configure multi-agent workflows
- Support for both human and AI agents

See [Agent Registry](archive/AGENT_REGISTRY.md) for configuration details.

### Template System
Customizable thread and entry templates with placeholder support.

**Template Discovery Order:**
1. CLI argument
2. Environment variable (`WATERCOOLER_TEMPLATES`)
3. Project-local templates directory
4. Bundled default templates

Templates support placeholders for dynamic content injection. See [Templates](archive/TEMPLATES.md) for syntax details.

### Advisory File Locking
PID-aware locks with TTL for concurrent safety. Prevents multiple processes from modifying the same thread simultaneously.

**Lock Behavior:**
- Automatic lock acquisition on write operations
- PID tracking to detect stale locks
- Configurable TTL (default: 5 minutes)
- Manual unlock via CLI if needed

### Automatic Backups
Rolling backups per thread in `.bak/<topic>/` directory.

**Backup Strategy:**
- Automatic backup before each modification
- Timestamped backup files
- Configurable retention policy
- Easy recovery from accidental changes

### Index Generation
Actionable/Open/In Review summaries with NEW markers.

**Index Features:**
- Auto-generated from thread metadata
- NEW markers when last entry author ≠ ball owner
- CLOSED filtering (exclude resolved threads)
- Markdown and HTML export options

---

## Features

### 12 CLI Commands

| Command | Purpose |
|---------|---------|
| `init-thread` | Initialize new thread |
| `append-entry` | Add structured entry |
| `say` | Quick note with ball flip |
| `ack` | Acknowledge without flip |
| `handoff` | Explicit agent handoff |
| `set-status` | Update thread status |
| `set-ball` | Update ball owner |
| `list` | List threads |
| `reindex` | Rebuild index |
| `search` | Search across threads |
| `web-export` | Generate HTML index |
| `unlock` | Clear stuck lock |

See [CLI Reference](CLI_REFERENCE.md) for detailed command documentation.

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

Agents are identified using the format: `Agent (user)`

**Examples:**
- `Claude (agent)` - AI agent
- `Alice (developer)` - Human developer
- `Team (pm)` - Team/group entity

### NEW Markers

Threads are flagged with NEW when:
- Last entry author ≠ current ball owner
- Indicates action required from ball owner
- Helps prioritize attention in thread lists

### CLOSED Filtering

Threads with status `CLOSED`, `DONE`, `MERGED`, or `RESOLVED` are:
- Excluded from default `list` output
- Included only with `--closed-only` flag
- Still searchable and accessible

---

## Development

### Setup Development Environment

```bash
# Clone repository
git clone https://github.com/mostlyharmless-ai/watercooler-cloud.git
cd watercooler-cloud

# Install with dev dependencies
pip install -e ".[dev]"
```

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run specific test suites
pytest tests/test_templates.py -v
pytest tests/test_config.py -v
pytest tests/test_structured_entries.py -v

# Run with coverage
pytest tests/ --cov=watercooler --cov-report=html
```

### Project Structure

```
watercooler-cloud/
├── src/
│   ├── watercooler/          # Core library
│   ├── watercooler_mcp/      # MCP server
│   └── watercooler_dashboard/ # Web dashboard
├── tests/                    # Test suite
├── docs/                     # Documentation
├── scripts/                  # Helper scripts
└── .githooks/               # Git hooks for validation
```

### Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for:
- Code style guidelines
- Pull request process
- Developer Certificate of Origin
- Testing requirements

---

## MCP Integration

Watercooler implements the Model Context Protocol (MCP) for AI agent integration.

### MCP Server Architecture

**Universal Mode:**
- Single MCP server instance per user
- Automatically discovers active repository
- Branch-paired threads repositories
- Dynamic context switching based on working directory

**Tools Provided:**
- `watercooler_list_threads` - List available threads
- `watercooler_read_thread` - Read thread content
- `watercooler_say` - Add entry with ball flip
- `watercooler_ack` - Acknowledge without flip
- `watercooler_handoff` - Explicit handoff
- `watercooler_set_status` - Update thread status
- `watercooler_reindex` - Rebuild index
- `watercooler_health` - Server health check

See [MCP Server Guide](mcp-server.md) for complete tool reference.

---

## Git-Based Cloud Sync

Watercooler supports team collaboration via git-based cloud sync (Phase 2A).

### Cloud Sync Features

**Idempotency:**
- Safe concurrent access from multiple users
- Conflict-free merges using union strategy
- PID-aware locking prevents race conditions

**Retry Logic:**
- Automatic retry on transient failures
- Exponential backoff for network issues
- Graceful degradation on sync failures

**Observability:**
- Detailed logging of sync operations
- Health checks for repository state
- Metrics for monitoring performance

### Required Git Configuration

```bash
# Enable "ours" merge driver
git config merge.ours.driver true

# Enable pre-commit hooks
git config core.hooksPath .githooks
```

See [WATERCOOLER_SETUP.md](../.github/WATERCOOLER_SETUP.md) for complete setup guide.

---

## HTTP Deployment Architecture

The MCP server supports both STDIO (local) and HTTP (hosted) transport modes,
enabling flexible deployment options.

### Transport Modes

**STDIO Mode (Default):**
- Used by local AI assistants (Claude Code, Cursor, etc.)
- Launched as subprocess via MCP configuration
- No network exposure, direct process communication

**HTTP Mode:**
- Used for hosted deployments (Vercel, Railway, etc.)
- Exposes MCP tools via HTTP endpoints
- Supports token-based authentication

### HTTP Server Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    HTTP Server                          │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │   auth.py   │  │  cache.py   │  │ server.py   │     │
│  │ Token Svc   │  │  Memory +   │  │  FastMCP    │     │
│  │  Client     │  │  Database   │  │   Tools     │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
                         │
                         ▼
        ┌────────────────────────────────┐
        │     watercooler-site API       │
        │  /api/github/token (auth)      │
        │  /api/cache (caching)          │
        └────────────────────────────────┘
```

### Key Modules

| Module | Purpose |
|--------|---------|
| `server.py` | Main FastMCP server with tool registration |
| `server_http.py` | HTTP transport wrapper with auth middleware |
| `auth.py` | Token resolution from watercooler-site API |
| `cache.py` | In-memory + database cache abstraction |

### Environment Variables

**Transport Configuration:**
```bash
WATERCOOLER_MCP_TRANSPORT=http  # "stdio" or "http"
WATERCOOLER_MCP_HOST=0.0.0.0     # HTTP bind host
WATERCOOLER_MCP_PORT=8080        # HTTP bind port
```

**Authentication (Hosted Mode):**
```bash
WATERCOOLER_AUTH_MODE=hosted            # "local" or "hosted"
WATERCOOLER_TOKEN_API_URL=https://...   # Token service URL
WATERCOOLER_TOKEN_API_KEY=...           # API key for token service
```

**Caching:**
```bash
WATERCOOLER_CACHE_BACKEND=memory  # "memory" or "database"
WATERCOOLER_CACHE_TTL=300         # Default TTL in seconds
```

### Deployment Options

**1. Standalone HTTP Server:**
```bash
WATERCOOLER_MCP_TRANSPORT=http python -m watercooler_mcp
```

**2. Vercel Serverless (Python Runtime):**
```python
# api/mcp.py
from watercooler_mcp.server_http import app
```

**3. Docker Container:**
```dockerfile
FROM python:3.11-slim
RUN pip install watercooler-cloud[http]
CMD ["python", "-m", "watercooler_mcp.server_http"]
```

### Graph-First Write Flow (HTTP)

```
Client Request
      │
      ▼
┌─────────────────────┐
│  Auth Middleware    │  ← Validate token, extract user context
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  MCP Tool Handler   │  ← watercooler_say, etc.
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ run_with_graph_sync │  ← Graph JSONL (source of truth)
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Markdown Projection │  ← Derived view
└─────────────────────┘
```

---

## Additional Resources

- **[Installation Guide](INSTALLATION.md)** - Setup and configuration
- **[CLI Reference](CLI_REFERENCE.md)** - Command documentation
- **[Use Cases Guide](archive/USE_CASES.md)** - Real-world examples
- **[Integration Guide](archive/integration.md)** - Python API reference
- **[MCP Server Guide](mcp-server.md)** - AI agent integration
- **[HTTP Transport Guide](http-transport.md)** - HTTP deployment details
