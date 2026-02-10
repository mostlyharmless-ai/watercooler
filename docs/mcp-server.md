# Watercooler MCP Server

FastMCP server that exposes watercooler-cloud tools to AI agents through the Model Context Protocol (MCP).

## Overview

The Watercooler Cloud MCP server allows AI agents (like Claude, Codex, etc.) to naturally discover and use Watercooler Cloud tools without manual CLI commands. All tools are namespaced as `watercooler_*` for provider compatibility.

**Current Status:** Production Ready (Phase 1A/1B/2A complete)  
**Version:** v0.0.1 + Phase 2A git sync

## Installation

Install watercooler-cloud with MCP support:

```bash
pip install -e .[mcp]
```

This installs `fastmcp>=2.0` and creates the `watercooler-mcp` command.

## Transport Modes

The MCP server supports two transport modes:

### STDIO Mode (Default)

Standard transport for local MCP clients (Claude Code, Cursor, Codex, Claude Desktop).

```bash
# Runs with STDIO transport by default
watercooler-mcp
```

### HTTP Mode (Hosted Deployment)

HTTP transport for hosted deployments, enabling web applications to call MCP tools directly.

```bash
# Run as HTTP server
WATERCOOLER_MCP_TRANSPORT=http python -m watercooler_mcp

# Or with explicit host/port
WATERCOOLER_MCP_TRANSPORT=http \
WATERCOOLER_MCP_HOST=0.0.0.0 \
WATERCOOLER_MCP_PORT=8080 \
python -m watercooler_mcp
```

**HTTP Server Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | API information |
| `/health` | GET | Health check for load balancers |
| `/mcp` | POST | MCP protocol endpoint |
| `/docs` | GET | OpenAPI documentation |

**When to use HTTP mode:**
- Running as a centralized service for watercooler-site dashboard
- Slack integration backend
- Multi-user hosted deployments
- Serverless platforms (Vercel, Railway, Fly.io)

**Required for hosted mode:**
- `WATERCOOLER_AUTH_MODE=hosted` - Enable token service authentication
- `WATERCOOLER_TOKEN_API_URL` - Token service URL
- `WATERCOOLER_TOKEN_API_KEY` - Token service API key

See [HTTP Transport Guide](./http-transport.md) for detailed deployment instructions.

## Quick Start

**For complete setup instructions, see [SETUP_AND_QUICKSTART.md](./SETUP_AND_QUICKSTART.md)**

### Configuration Examples

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.watercooler_cloud]
command = "uvx"
args = ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"]

[mcp_servers.watercooler_cloud.env]
WATERCOOLER_AGENT = "Codex"
WATERCOOLER_THREADS_PATTERN = "https://github.com/{org}/{repo}-threads.git"
WATERCOOLER_AUTO_BRANCH = "1"
```

**Claude Desktop (`~/.config/Claude/claude_desktop_config.json` on Linux, `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable",
        "watercooler-mcp"
      ],
      "env": {
        "WATERCOOLER_AGENT": "Claude@Desktop",
        "WATERCOOLER_THREADS_PATTERN": "https://github.com/{org}/{repo}-threads.git",
        "WATERCOOLER_AUTO_BRANCH": "1"
      }
    }
  }
}
```

**Claude Code (`~/.claude.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable",
        "watercooler-mcp"
      ],
      "env": {
        "WATERCOOLER_AGENT": "Claude@Code",
        "WATERCOOLER_THREADS_PATTERN": "https://github.com/{org}/{repo}-threads.git",
        "WATERCOOLER_AUTO_BRANCH": "1"
      }
    }
  }
}
```

**Cursor (`~/.cursor/mcp.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable",
        "watercooler-mcp"
      ],
      "env": {
        "WATERCOOLER_AGENT": "Cursor",
        "WATERCOOLER_THREADS_PATTERN": "https://github.com/{org}/{repo}-threads.git",
        "WATERCOOLER_AUTO_BRANCH": "1"
      }
    }
  }
}
```

**Note:** `uvx` must be in your PATH. If it's not found, use the full path (e.g., `~/.local/bin/uvx` on Linux/macOS). The `uvx` command ensures you always get the latest code from the repository and runs in an isolated environment.

## Environment Variables

### WATERCOOLER_AGENT (Required)
Your agent identity (e.g., "Claude", "Codex"). Set in MCP config.

### WATERCOOLER_DIR (Optional)
Explicit override for bespoke setups. Universal mode clones threads beside your
code repository as a sibling `<code-root>-threads` directory (for example
`/workspace/my-app` ↔ `/workspace/my-app-threads`); you usually do not need to
set this variable.

Only set `WATERCOOLER_DIR` when you require a fixed threads directory (for example, while debugging environments where the server cannot infer the correct repository).

### Universal thread location controls

| Variable | Purpose | Default |
|----------|---------|---------|
| `WATERCOOLER_THREADS_BASE` | Optional root for local thread clones | _Sibling of the code repo_ |
| `WATERCOOLER_THREADS_PATTERN` | Remote URL pattern for auto-clone | `https://github.com/{org}/{repo}-threads.git` |
| `WATERCOOLER_THREADS_AUTO_PROVISION` | Opt-in creation of missing threads repos | `0` |
| `WATERCOOLER_THREADS_CREATE_CMD` | Command template for provisioning | _Unset_ |
| `WATERCOOLER_AUTO_BRANCH` | Auto create / checkout matching branch | `1` |

Example (Claude Desktop, macOS):

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable",
        "watercooler-mcp"
      ],
      "env": {
        "WATERCOOLER_AGENT": "Claude@Desktop",
        "WATERCOOLER_THREADS_PATTERN": "https://github.com/{org}/{repo}-threads.git",
        "WATERCOOLER_AUTO_BRANCH": "1"
      }
    }
  }
}
```

**Manual override:**

If you set `WATERCOOLER_DIR`, that path takes priority and the sibling repo rules are skipped. Use the override sparingly—it's easy to create stray repo-local thread folders inside the code repo when this variable stays set.

**Auto-provisioning (optional):**

- Set `WATERCOOLER_THREADS_AUTO_PROVISION=1` to allow the server to create the
  missing `<repo>-threads` repository when the initial clone returns
  "repository not found".
- Provide `WATERCOOLER_THREADS_CREATE_CMD` with a one-line shell command (for
  example, `gh repo create {slug} --private`). The command receives useful
  placeholders (`{slug}`, `{repo_url}`, `{code_repo}`, `{namespace}`, `{repo}`,
  `{org}`) and its stdout/stderr is surfaced on failure.
- Auto-provisioning is skipped when `WATERCOOLER_DIR` is set or when the remote
  uses HTTPS.

## Available Tools

All tools are namespaced as `watercooler_*`:

### Diagnostic Tools

#### `watercooler_health`
Check server health and configuration including branch parity, git authentication, and GitHub API status.

**Parameters:**
- `code_path` (str): Path to code repository for parity checks (optional)

**Returns:** Comprehensive health status including:
- Server version and agent identity
- Threads directory configuration
- Graph services status (LLM, embeddings)
- Branch parity health
- Git authentication status
- GitHub CLI version and API rate limits

**Example output:**
```
Watercooler MCP Server v0.2.0
Status: Healthy
Agent: Codex
Threads Dir: /workspace/watercooler-cloud-threads
Threads Dir Exists: True
Threads Repo URL: https://github.com/org/repo-threads.git
Code Branch: feature/auth
Auto-Branch: enabled
Python: /usr/bin/python3
fastmcp: 2.14.0

Graph Services:
  Summaries Enabled: True
  LLM Service: available (http://localhost:8000/v1)
  Embeddings Enabled: True
  Embedding Service: available (http://localhost:8080/v1)

Branch Parity:
  Status: clean
  Code Branch: feature/auth
  Threads Branch: feature/auth
  Code Ahead/Behind: 0/0
  Threads Ahead/Behind: 0/0
  Pending Push: False

Git Authentication:
  Protocol: https
  Connectivity: ok
  Credential Helper: !/usr/bin/gh auth git-credential
  GitHub CLI Auth: valid

GitHub:
  gh Version: 2.83.2 ✓
  Rate Limit: 4500/5000 (90%) - resets in 45min
```

**Rate Limit Warnings:**
When rate limit is low (<10%) or exhausted:
```
GitHub:
  gh Version: 2.83.2 ✓
  Rate Limit: 0/5000 (0%) ⚠️ RATE LIMITED
    → Resets in 23 minutes
    → Wait for reset or reduce API calls
```

#### `watercooler_whoami`
Get your resolved agent identity.

**Returns:** Current agent name

**Example output:**
```
You are: Codex
```

### Thread Management Tools

#### `watercooler_list_threads`
List all threads with ball ownership and NEW markers.

**Parameters:**
- `open_only` (bool | None): Filter by status (True=open only, False=closed only, None=all)
- `limit` (int): Max threads (not yet implemented - returns all)
- `cursor` (str | None): Pagination cursor (not yet implemented)
- `format` (str): Output format - "markdown" (json support deferred)

**Returns:** Formatted thread list organized by:
- 🎾 Your Turn - Threads where you have the ball
- 🆕 NEW Entries - Threads with unread updates
- ⏳ Waiting on Others - Threads where others have the ball

Each thread includes an LLM-generated summary (when available from the baseline graph) below the status line, giving agents quick context without opening the thread.

#### `watercooler_read_thread`
Read complete thread content, or a condensed summary-only view.

**Parameters:**
- `topic` (str): Thread topic identifier (e.g., "feature-auth")
- `from_entry` (int): Starting entry index (not yet implemented - returns from start)
- `limit` (int): Max entries (not yet implemented - returns all)
- `format` (str): Output format - `"markdown"` (default) or `"json"`
- `summary_only` (bool): When `true`, returns only entry summaries (no bodies). Reduces token usage by ~90% — ideal for scanning threads. Default: `false`.

**Returns:**
- Markdown (full): original thread markdown
- Markdown (summary_only): condensed view with thread summary, status, and per-entry summaries
- JSON (full): structured payload with `meta` (including thread `summary`) and `entries[]` array (each with `summary` and `body`)
- JSON (summary_only): same structure but entries contain `summary` only (no `body`)

**Usage Tips:**
- Use `summary_only=true` to scan a thread's narrative in ~500 tokens instead of ~5,000 — then fetch specific entries with `get_thread_entry` for full bodies.
- Prefer `format="json"` when a client needs to examine individual entries without reparsing markdown. Each element in `entries[]` mirrors the structures returned by the entry tools.
- Remember that large JSON payloads can still exceed stdio limits; paginate with the entry tools when you only need a subset.
- When preparing a human-facing summary, stick with the default markdown output so you can reuse the canonical thread text verbatim.

#### `watercooler_list_thread_entries`
List entry headers (metadata only) for a thread so clients can select specific entries without downloading the entire file.

**Parameters:**
- `topic` (str): Thread topic identifier
- `offset` (int): Zero-based entry offset (default: 0)
- `limit` (int | None): Maximum entries to return (default: all from `offset`)
- `format` (str): `"json"` (default) for structured data or `"markdown"` for a human-readable list
- `code_path` (str): Code repository root (required to resolve the paired threads repo)

**Returns:**
- JSON: `entry_count`, effective `offset`, and an array of entry headers with summaries (`index`, `entry_id`, `agent`, `summary`, etc.)
- Markdown: bullet list summarising the selected entries (with summaries shown when available)

This tool acts as a **"TOC with abstracts"** — agents can scan entry summaries to decide which entries need full bodies.

**Usage Tips:**
- Use pagination (`offset`, `limit`) to stay well below stdio response limits in very long threads.
- Programmatic clients should stick with the JSON default and feed the `entry_id`/`index` into follow-up calls to `get_thread_entry` or `get_thread_entry_range`.
- Markdown mode is convenient when you only need a quick, human-readable index to relay back to a user.

**Example (JSON request):**
```python
tool_result = list_thread_entries(
    topic="entry-access-tools",
    offset=0,
    limit=5,
    format="json",
    code_path="/path/to/watercooler-cloud",
)
payload = json.loads(tool_result.content[0].text)
entries = payload["entries"]
```

Each entry header contains:
- `index`, `entry_id`
- `agent`, `timestamp`, `role`, `type`, `title`
- `summary` — LLM-generated 1-2 sentence summary (empty string if unavailable)

#### `watercooler_get_thread_entry`
Retrieve a single entry (header + body) either by index or by `entry_id`.

**Parameters:**
- `topic` (str): Thread topic identifier
- `index` (int | None): Zero-based entry index (optional)
- `entry_id` (str | None): ULID captured in the entry footer (optional)
- `format` (str): `"json"` (default) or `"markdown"`
- `code_path` (str): Code repository root (required)

**Returns:**
- JSON: entry metadata, `summary`, and `body` in a structured object
- Markdown: raw entry header + body block

**Usage Tips:**
- Provide both `index` and `entry_id` when you want an extra guard that you are inspecting the expected entry; the tool will error if they disagree.
- JSON output includes the `summary` alongside the full `body`, letting clients show a preview or use the summary for downstream reasoning.
- Markdown output is perfect for quoting the entry as-is in a response.

**Example (markdown slice):**
```python
markdown_entry = get_thread_entry(
    topic="entry-access-tools",
    index=3,
    format="markdown",
    code_path="/path/to/watercooler-cloud",
)
entry_text = markdown_entry.content[0].text
```

Provide either `index` or `entry_id` (or both, if you want validation that they refer to the same entry).

#### `watercooler_get_thread_entry_range`
Return a contiguous, inclusive range of entries for streaming scenarios.

**Parameters:**
- `topic` (str): Thread topic identifier
- `start_index` (int): Starting entry index (default: 0)
- `end_index` (int | None): Inclusive end index (defaults to last entry)
- `format` (str): `"json"` (default) or `"markdown"`
- `summary_only` (bool): When `true`, returns only entry summaries (no bodies). Reduces token usage by ~90%. Default: `false`.
- `code_path` (str): Code repository root (required)

**Returns:**
- JSON (full): `entries` array (with `summary` and `body` per entry) and `start_index`/`end_index`
- JSON (summary_only): same structure but entries contain `summary` only (no `body`)
- Markdown (full): concatenated entry blocks separated by `---`
- Markdown (summary_only): bullet list with entry timestamps, titles, and summaries

**Usage Tips:**
- Use `summary_only=true` to scan a range for relevant content before fetching full bodies.
- Request smaller windows (e.g., batches of 5–10 entries) to reduce payload size and allow streaming consumption on the client side.
- Markdown output mirrors the thread file layout, making it easy to forward directly to a user after light editing.
- Combine `list_thread_entries` (for navigation) with `get_thread_entry_range` to fetch just the span you need.

**Example (JSON window):**
```python
window_result = get_thread_entry_range(
    topic="entry-access-tools",
    start_index=10,
    end_index=12,
    format="json",
    code_path="/path/to/watercooler-cloud",
)
window_payload = json.loads(window_result.content[0].text)
entries = window_payload["entries"]
```

#### `watercooler_say`
Add your response to a thread and flip the ball to your counterpart.

**Parameters:**
- `topic` (str): Thread topic identifier
- `title` (str): Entry title - brief summary
- `body` (str): Full entry content (markdown supported). In general, threads follow an arc:
  - **Start**: Persist the state of the project at the start, describe why the thread exists, and lay out the desired state change for the code/project
  - **Middle**: Reason towards the appropriate solution
  - **End**: Describe the effective solution reached
  - **Often**: Recap that arc in a closing message to the thread
  Thread entries should explicitly reference any files changed, using file paths (e.g., `src/watercooler_mcp/server.py`, `docs/README.md`) to maintain clear traceability of what was modified.
- `role` (str): Your role - planner, critic, implementer, tester, pm, scribe (default: implementer)
- `entry_type` (str): Entry type - Note, Plan, Decision, PR, Closure (default: Note)

**Returns:** Confirmation with new ball owner

**Example:**
```python
say("feature-auth", "Implementation complete", "All tests passing. Ready for review.", role="implementer")
```

#### `watercooler_ack`
Acknowledge a thread without flipping the ball.

**Parameters:**
- `topic` (str): Thread topic identifier
- `title` (str): Optional acknowledgment title (default: "Ack")
- `body` (str): Optional acknowledgment message (default: "ack")

**Returns:** Confirmation (ball remains with current owner)

#### `watercooler_handoff`
Hand off the ball to another agent.

**Parameters:**
- `topic` (str): Thread topic identifier
- `note` (str): Optional handoff message
- `target_agent` (str | None): Specific agent name (optional, uses counterpart if None)

**Returns:** Confirmation with new ball owner

**Example:**
```python
handoff("feature-auth", "Ready for your review", target_agent="Claude")
```

#### `watercooler_set_status`
Update thread status.

**Parameters:**
- `topic` (str): Thread topic identifier
- `status` (str): New status (e.g., "OPEN", "IN_REVIEW", "CLOSED", "BLOCKED")

**Returns:** Confirmation message


#### `watercooler_sync`
Synchronize the local threads repository with its remote using the same flow as the MCP server (pull → commit → push). Useful when CLI or other tools mutate threads outside the MCP session and you want parity.

**Parameters:**
- `code_path` (str): Code repo root, same as other tools
- `agent_func` (str): Optional agent identity in format `<platform>:<model>:<role>` for provenance

**Returns:** Confirmation once sync completes (errors if remote unreachable).

**Returns:** Confirmation once sync completes (errors if remote unreachable).

#### `watercooler_reindex`
Generate index summary of all threads.

**Returns:** Markdown index organized by:
- Actionable threads (where you have the ball)
- Open threads (waiting on others)
- In Review threads
- Closed threads (limited to 10 most recent)

### Memory Query Tools

Tools for querying thread history using Graphiti temporal graph memory. These tools enable semantic search across project context to answer questions about implementation details, decisions, and temporal evolution.

> **⚠️ Tool Consolidation Notice (January 2026)**
>
> The following tools have been removed and consolidated into unified alternatives:
>
> | Removed Tool | Replacement |
> |--------------|-------------|
> | `watercooler_query_memory` | `watercooler_smart_query` |
> | `watercooler_search_nodes` | `watercooler_search(mode="entities")` |
> | `watercooler_search_memory_facts` | `watercooler_smart_query` |
> | `watercooler_get_episodes` | `watercooler_search(mode="episodes")` |
>
> **Active tools:**
> - `watercooler_smart_query` - Multi-tier intelligent query with auto-escalation
> - `watercooler_search` - Unified search with `mode` parameter (`entries`, `entities`, `episodes`)
> - `watercooler_get_entity_edge` - Get specific entity/edge by UUID
> - `watercooler_diagnose_memory` - Diagnose memory backend status
>
> The documentation below is preserved for reference but describes removed tools.

#### `watercooler_query_memory` *(Removed)*
Query thread history using Graphiti temporal graph memory.

**Purpose:** Ask natural language questions about thread history, implementation details, architectural decisions, and temporal evolution of the project.

**Prerequisites:**
- Graphiti enabled: `WATERCOOLER_GRAPHITI_ENABLED="1"`
- FalkorDB running (default: localhost:6379)
- OpenAI API key configured
- Memory extras installed: `pip install watercooler-cloud[memory]`
- Index built via CLI: `python -m watercooler_memory.pipeline run --backend graphiti --threads /path/to/threads`

**Parameters:**
- `query` (str): Natural language search query (e.g., "What authentication was implemented?")
- `code_path` (str): Code repository root (for resolving threads directory)
- `limit` (int): Maximum results to return (1-50, default: 10)
- `topic` (str | None): Optional thread filter - omit for cross-thread search, specify for single-thread queries

**Returns:** JSON object with:
```json
{
  "query": "What authentication was implemented?",
  "result_count": 2,
  "results": [
    {
      "content": "Claude implemented OAuth2 with JWT tokens",
      "score": 0.0,
      "metadata": {
        "uuid": "edge-uuid",
        "thread_id": "auth-feature",
        "entry_id": "01ABC...",
        "valid_at": "2025-10-01T10:00:00Z",
        "invalid_at": null,
        "created_at": "2025-10-01T10:05:00Z"
      }
    }
  ],
  "message": "Found 2 results"
}
```

**Query Scope:**
- **Cross-thread queries** (recommended): Omit `topic` parameter to search across ALL threads in the project
  - Example: `query_memory(query="How was error handling implemented?", code_path=".")`
  - Useful for discovering context across different features/threads
- **Single-thread queries**: Specify `topic` to filter results to one thread
  - Example: `query_memory(query="What tests were added?", topic="auth-feature", code_path=".")`
  - Useful for focused investigation within a specific feature

**Usage Examples:**

**Cross-thread implementation search:**
```python
watercooler_query_memory(
    query="How was authentication implemented?",
    code_path=".",
    limit=10
)
```

**Single-thread decision search:**
```python
watercooler_query_memory(
    query="Why was PostgreSQL chosen over MySQL?",
    topic="database-selection",
    code_path=".",
    limit=5
)
```

**Temporal evolution query:**
```python
watercooler_query_memory(
    query="What changes were made to the API between v1 and v2?",
    code_path=".",
    limit=20
)
```

**Error Responses:**

All errors return JSON format (never raises exceptions):

**Graphiti disabled:**
```json
{
  "error": "graphiti_disabled",
  "message": "Graphiti memory queries are disabled. Set WATERCOOLER_GRAPHITI_ENABLED=1",
  "query": "...",
  "result_count": 0,
  "results": []
}
```

**Missing dependencies:**
```json
{
  "error": "import_error",
  "message": "Graphiti backend unavailable: No module named 'watercooler_memory'. Install with: pip install watercooler-cloud[memory]",
  "query": "...",
  "result_count": 0,
  "results": []
}
```

**Backend initialization failed:**
```json
{
  "error": "backend_init_failed",
  "message": "Failed to initialize Graphiti backend. Check FalkorDB is running (docker run -d -p 6379:6379 falkordb/falkordb:latest)",
  "query": "...",
  "result_count": 0,
  "results": []
}
```

**Query execution failed:**
```json
{
  "error": "query_failed",
  "message": "Query execution failed: connection refused",
  "query": "...",
  "result_count": 0,
  "results": []
}
```

**Performance Notes:**
- Query latency: Typically 2-5 seconds (depends on graph size, query complexity, and OpenAI API latency)
- API costs: Each query incurs OpenAI API costs (embedding generation + LLM processing)
- Result quality: Improves with richer thread context and clearer queries
- Index freshness: Manually rebuild index after significant thread updates

**Best Practices:**
- Use specific, focused queries rather than broad open-ended questions
- Omit `topic` parameter to discover context across the entire project
- Specify `topic` only when you know which thread contains the answer
- Build index regularly to keep memory up-to-date with latest threads
- Use higher `limit` values (20-50) for exploratory queries
- Use lower `limit` values (5-10) for targeted fact retrieval

**Related Documentation:**
- [GRAPHITI_SETUP.md](./GRAPHITI_SETUP.md) - Complete setup guide
- [MEMORY.md](./MEMORY.md) - Memory backend overview
- [ENVIRONMENT_VARS.md](./ENVIRONMENT_VARS.md#graphiti-memory-variables) - Configuration reference

**Index Building:**
```bash
# Build index for all threads in current project
python -m watercooler_memory.pipeline run \
  --backend graphiti \
  --threads /path/to/watercooler-cloud-threads

# Update index after thread changes
python -m watercooler_memory.pipeline run \
  --backend graphiti \
  --threads /path/to/watercooler-cloud-threads \
  --force
```

**FalkorDB Setup:**
```bash
# Docker (recommended)
docker run -d -p 6379:6379 falkordb/falkordb:latest

# Or docker-compose
services:
  falkordb:
    image: falkordb/falkordb:latest
    ports:
      - "6379:6379"
```

#### `watercooler_search_nodes` *(Removed)*

Search for entity nodes in the Graphiti knowledge graph using hybrid semantic search.

> **Replacement:** Use `watercooler_search(mode="entities", query="...", limit=10)`

**Purpose:** Discover entities (people, concepts, technologies, components) mentioned in threads and explore their relationships.

**Prerequisites:** Same as `watercooler_query_memory` (Graphiti enabled, index built, FalkorDB running)

**Parameters:**
- `query` (required): Search query string (e.g., "authentication")
- `code_path` (optional): Path to code repository
- `group_ids` (optional): List of thread topics to filter by
- `max_nodes` (optional): Maximum nodes to return (default: 10, max: 50)
- `entity_types` (optional): List of entity type names to filter

**Returns:**
```json
{
  "query": "OAuth2 implementation",
  "result_count": 2,
  "results": [
    {
      "uuid": "01ABC...",
      "name": "OAuth2Provider",
      "labels": ["Class", "Authentication"],
      "summary": "OAuth2 provider implementation with JWT token support",
      "created_at": "2025-10-01T10:00:00Z",
      "group_id": "auth-feature",
      "score": 0.89
    }
  ],
  "message": "Found 2 nodes"
}
```

**Example:**
```python
watercooler_search_nodes(
    query="database",
    code_path=".",
    group_ids=["backend-refactor"],
    max_nodes=5
)
```

#### `watercooler_get_entity_edge`

Retrieve a specific relationship (edge) between entities by UUID.

**Purpose:** Inspect detailed information about a specific relationship discovered through searches.

**Prerequisites:** Same as `watercooler_query_memory`

**Parameters:**
- `uuid` (required): Edge UUID to retrieve
- `code_path` (optional): Path to code repository

**Returns:**
```json
{
  "uuid": "01ABC...",
  "fact": "Claude implemented OAuth2 authentication with JWT tokens",
  "source_node_uuid": "01DEF...",
  "target_node_uuid": "01GHI...",
  "valid_at": "2025-10-01T10:00:00Z",
  "created_at": "2025-10-01T10:00:00Z",
  "group_id": "auth-feature",
  "message": "Retrieved edge 01ABC..."
}
```

**Example:**
```python
watercooler_get_entity_edge(
    uuid="01ABC123...",
    code_path="."
)
```

**Error Response (edge not found):**
```json
{
  "error": "Edge retrieval failed",
  "message": "Entity edge '01ABC...' not found",
  "operation": "get_entity_edge",
  "uuid": "01ABC..."
}
```

#### `watercooler_search_memory_facts` *(Removed)*

Search for facts (relationships) with optional center-node traversal.

> **Replacement:** Use `watercooler_smart_query(query="...")`

**Purpose:** Discover specific relationships and facts about entities, optionally centered around a specific entity node.

**Prerequisites:** Same as `watercooler_query_memory`

**Parameters:**
- `query` (required): Search query string
- `code_path` (optional): Path to code repository
- `group_ids` (optional): List of thread topics to filter by
- `max_facts` (optional): Maximum facts to return (default: 10, max: 50)
- `center_node_uuid` (optional): UUID of node to center search around

**Returns:**
```json
{
  "query": "authentication decisions",
  "result_count": 3,
  "results": [
    {
      "uuid": "01ABC...",
      "fact": "Claude implemented OAuth2 with JWT tokens",
      "source_node_uuid": "01DEF...",
      "target_node_uuid": "01GHI...",
      "score": 0.89,
      "valid_at": "2025-10-01T10:00:00Z",
      "group_id": "auth-feature"
    }
  ],
  "message": "Found 3 fact(s)",
  "centered_on_node": "01DEF..."
}
```

**Example (basic search):**
```python
watercooler_search_memory_facts(
    query="API design decisions",
    code_path=".",
    group_ids=["api-v2"],
    max_facts=10
)
```

**Example (center-node traversal):**
```python
# First find a node
nodes = watercooler_search_nodes(query="AuthService", ...)
node_uuid = nodes["results"][0]["uuid"]

# Then search facts connected to that node
watercooler_search_memory_facts(
    query="security considerations",
    center_node_uuid=node_uuid,
    max_facts=20
)
```

#### `watercooler_get_episodes` *(Removed)*

Search for episodic content using semantic search.

> **Replacement:** Use `watercooler_search(mode="episodes", query="...", limit=10)`

**Purpose:** Retrieve episodes (thread entry content) from Graphiti memory based on semantic relevance.

**Note:** Graphiti doesn't support enumerating all episodes. This tool performs semantic search, so a query string is required.

**Prerequisites:** Same as `watercooler_query_memory`

**Parameters:**
- `query` (required): Search query string (must be non-empty)
- `code_path` (optional): Path to code repository
- `group_ids` (optional): List of thread topics to filter by
- `max_episodes` (optional): Maximum episodes to return (default: 10, max: 50)

**Returns:**
```json
{
  "query": "authentication implementation",
  "result_count": 2,
  "results": [
    {
      "uuid": "01ABC...",
      "name": "Entry 01ABC...",
      "content": "Implemented OAuth2 authentication with JWT token support...",
      "created_at": "2025-10-01T10:00:00Z",
      "source": "thread_entry",
      "source_description": "Watercooler thread entry",
      "group_id": "auth-feature",
      "valid_at": "2025-10-01T10:00:00Z",
      "score": 1.0
    }
  ],
  "message": "Found 2 episode(s)"
}
```

**Example:**
```python
watercooler_get_episodes(
    query="authentication implementation",
    code_path=".",
    group_ids=["auth-feature", "api-design"],
    max_episodes=5
)
```

**Note:** Query parameter is required. Episodes are ranked by semantic relevance to the query.

**Typical Workflow:**

1. **Discovery** - Use `search_nodes` to find relevant entities:
   ```python
   nodes = watercooler_search_nodes(
       query="database schema",
       max_nodes=10
   )
   ```

2. **Exploration** - Use `search_memory_facts` to explore relationships:
   ```python
   facts = watercooler_search_memory_facts(
       query="migration strategy",
       center_node_uuid=nodes["results"][0]["uuid"],
       max_facts=20
   )
   ```

3. **Deep Dive** - Use `get_entity_edge` to inspect specific relationships:
   ```python
   edge = watercooler_get_entity_edge(
       uuid=facts["results"][0]["uuid"]
   )
   ```

4. **Context** - Use `get_episodes` to see source content:
   ```python
   episodes = watercooler_get_episodes(
       group_ids=[edge["group_id"]],
       max_episodes=10
   )
   ```

5. **Analysis** - Use `query_memory` for natural language questions:
   ```python
   watercooler_query_memory(
       query="Why was this migration approach chosen?",
       topic=edge["group_id"]
   )
   ```

### Branch Sync Enforcement Tools

These tools ensure that code and threads repos maintain 1:1 branch correspondence, preventing drift and enforcing the branch pairing protocol.

#### `watercooler_validate_branch_pairing`
Validate branch pairing between code and threads repos.

**Parameters:**
- `code_path` (str): Path to code repository directory (default: current directory)
- `strict` (bool): If True, return valid=False on any mismatch (default: True)

**Returns:** JSON result with:
- `valid` (bool): Whether branches are properly paired
- `code_branch` (str | None): Current code repo branch
- `threads_branch` (str | None): Current threads repo branch
- `mismatches` (list): List of detected mismatches with recovery steps
- `warnings` (list): Non-critical warnings

**Note:** This validation is automatically performed before all write operations (`say`, `ack`, `handoff`, `set_status`). Use this tool for explicit checking.

#### `watercooler_sync_branch_state`
Synchronize branch state between code and threads repos.

**Parameters:**
- `code_path` (str): Path to code repository directory
- `branch` (str | None): Specific branch to sync (default: current branch)
- `operation` (str): One of "create", "delete", "merge", "checkout" (default: "checkout")
- `force` (bool): Skip safety checks (default: False)

> **Note:** This operational tool does **not** accept `agent_func`. Unlike write
> operations that create thread entries, it only performs git lifecycle work, so
> pass just `code_path`, `branch`, `operation`, and `force`.

**Operations:**
- **checkout**: Ensure both repos are on the same branch (creates threads branch if missing)
- **create**: Create threads branch if code branch exists
- **delete**: Delete threads branch (blocks if OPEN threads exist, unless `force=True`)
- **merge**: Merge threads branch to threads:main (for when code branch was merged)

**Returns:** JSON result with operation status and any warnings

#### `watercooler_audit_branch_pairing`
Comprehensive audit of branch pairing across entire repo pair.

**Parameters:**
- `code_path` (str): Path to code repository directory
- `include_merged` (bool): Include fully merged branches in report (default: False)

**Returns:** JSON report with:
- `synced_branches`: Branches that exist in both repos with same name
- `code_only_branches`: Branches that exist only in code repo
- `threads_only_branches`: Branches that exist only in threads repo (orphaned)
- `mismatched_branches`: Future: detect name mismatches
- `recommendations`: Suggested actions for each drift case

**Use Cases:**
- Identify orphaned branches (e.g., `health-badge` deleted from code but remains in threads)
- Find branches that need threads counterparts
- Get recommendations for cleanup

#### `watercooler_recover_branch_state`
Recover from branch state inconsistencies.

**Parameters:**
- `code_path` (str): Path to code repository directory
- `auto_fix` (bool): Automatically apply safe fixes (default: False)
- `diagnose_only` (bool): Only report issues, don't fix (default: False)

**Detects:**
- Branch name mismatches
- Orphaned threads branches (code branch deleted)
- Missing threads branches (code branch exists)
- Git state issues (rebase conflicts, detached HEAD, etc.)

**Returns:** JSON diagnostic report with:
- `issues_found`: Number of issues detected
- `issues`: List of issues with severity and recovery steps
- `fixes_applied`: List of fixes that were automatically applied (if `auto_fix=True`)
- `warnings`: Non-critical warnings

**Example Workflow:**
1. Run `watercooler_recover_branch_state` with `diagnose_only=True` to see issues
2. Review the diagnostic report
3. Run again with `auto_fix=True` to apply safe fixes automatically
4. Use `watercooler_sync_branch_state` for manual fixes if needed

#### `watercooler_reconcile_parity`
Reconcile branch parity state when preflight blocks.

When a write operation (say, ack, handoff, set_status) fails due to branch parity issues (threads behind origin, pending push failed, etc.), use this tool to attempt automatic recovery.

**Parameters:**
- `code_path` (str): Path to code repository directory (required)

**Actions performed:**
1. **Pull threads if behind origin**: If threads is behind, pulls with rebase to sync
2. **Run preflight with auto-fix**: Reruns parity checks with auto-remediation enabled
3. **Push pending commits**: If there are local commits waiting to be pushed, attempts push with retry

**Returns:** JSON object with:
- `success` (bool): Whether reconciliation succeeded
- `status` (str): Final parity status after reconciliation
- `actions` (list): Actions taken during reconciliation
- `push_result` (object): Result of push attempt if applicable (success, error)
- `error` (str): Error message if reconciliation failed
- `blocking_reason` (str): Reason if still blocked after reconciliation

**Example Usage:**
```python
# When say() fails with "Threads branch is 2 commits behind origin"
result = watercooler_reconcile_parity(code_path=".")
if result["success"]:
    # Retry the original write operation
    watercooler_say(topic="my-topic", title="Entry", body="...", code_path=".")
```

**Common Scenarios:**
- **Threads behind origin**: Another agent pushed commits; reconcile pulls them
- **Push failed**: Network issue during push; reconcile retries the push
- **Branch mismatch after checkout**: Auto-fixes branch alignment

## Configuration

### Environment Variables

- **`WATERCOOLER_AGENT`**: Agent identity (default: `Agent`). Determines entry authorship and ball ownership.

- **Universal overrides (optional):**
  - `WATERCOOLER_THREADS_BASE` — optional override when you want all threads repos under a fixed root (otherwise the sibling `<code>-threads` directory is used)
- `WATERCOOLER_THREADS_PATTERN` — pattern for building the remote URL (`https://github.com/{org}/{repo}-threads.git` by default)
  - `WATERCOOLER_AUTO_BRANCH` — set to `0` to skip auto-creating the matching branch
  - `WATERCOOLER_GIT_AUTHOR` / `WATERCOOLER_GIT_EMAIL` — override commit metadata in the threads repo

- **Manual override:** `WATERCOOLER_DIR` forces a specific threads directory. Use only if you must disable universal repo discovery.

### Required parameters

Every tool call must include:

- `code_path` — points to the code repository root (e.g., `"."`). The server resolves repo/branch/commit from this path.
- `agent_func` — required on write operations; format `<platform>:<model>:<role>` (e.g., `"Cursor:Composer 1:implementer"`). The platform should be the actual IDE/platform name (e.g., `Cursor`, `Claude Code`, `Codex`), model should be the exact model identifier, and role should be the agent role. This information is recorded in commit footers for traceability.

### Deferred Features

Some features are available as parameters but deferred for future implementation (see [ROADMAP.md](../ROADMAP.md) for details):

- **JSON format**: `format` parameter accepts "json" but currently only "markdown" is supported
- **Pagination**: `limit` and `cursor` parameters are accepted but not yet implemented (returns all results)

These features will be implemented if real-world usage demonstrates the need.

## Usage Examples

### Example 1: Check Server Health

```python
watercooler_health(code_path=".")

# Sample response:
# Watercooler MCP Server v0.2.0
# Status: Healthy
# Agent: Codex
# Threads Dir: /workspace/repo-threads
# Threads Dir Exists: True
```

### Example 2: List threads where you have the ball

```python
watercooler_list_threads(code_path=".")
```

### Example 3: Respond to a thread

```python
watercooler_say(
    topic="feature-auth",
    title="Implementation complete",
    body="Spec: implementer-code — unit tests passing, integration tests added.",
    role="implementer",
    entry_type="Note",
    code_path=".",
    agent_func="Cursor:Composer 1:implementer"
)
```

### Example 4: Hand off to a specific teammate

```python
watercooler_handoff(
    topic="feature-auth",
    note="Security review needed for OAuth implementation",
    target_agent="SecurityBot",
    code_path=".",
    agent_func="Claude Code:sonnet-4:pm"
)
```

## Troubleshooting
### Git authentication issues

- HTTPS is the default and uses your credential helper; ensure a manual `git push https://…` succeeds.
- For SSH, set `WATERCOOLER_THREADS_PATTERN` to `git@github.com:{org}/{repo}-threads.git` and load your SSH key.
- After changing the pattern, restart the MCP server/client so the env var is picked up.


### Server Not Found

If `watercooler-mcp` command is not found:

```bash
# Check installation
pip list | grep watercooler-cloud

# Reinstall with MCP extras
pip install -e .[mcp]

# Find command path
which watercooler-mcp
```

### Wrong Agent Identity

If tools show wrong agent name:

```bash
# Check current identity
python -c "from watercooler_mcp.config import get_agent_name; print(get_agent_name())"

# Set in environment
export WATERCOOLER_AGENT="YourAgentName"

# Or configure in MCP client settings
```

### Threads Directory Not Found

If the server can't resolve the threads repository:

```bash
# Inspect resolved context
python -c "from pathlib import Path; from watercooler_mcp.config import resolve_thread_context; print(resolve_thread_context(Path('.')).threads_dir)"
```

- Ensure `code_path` points inside a git repository with a configured remote.
- Run `watercooler_health(code_path=".")` to confirm the expected sibling directory (for example `/workspace/my-app-threads`).
- If health reports any location inside the code repository (for example `./threads-local`), remove stale overrides, copy the data into the sibling `<repo>-threads` directory, and delete the stray directory.
- As a last resort, set `WATERCOOLER_DIR` to a specific path (see Environment Variables) while you move data into the sibling `<repo>-threads` repository.

### Format Not Supported Error

Currently, only `format="markdown"` is supported (JSON support is deferred):

```python
# This works:
list_threads(format="markdown")

# This will error:
list_threads(format="json")  # Error: Only format='markdown' is currently supported
```

## Development

### Running Tests

```bash
# Install dev dependencies
pip install -e .[dev,mcp]

# Run tests
pytest tests/
```

### Viewing Tool Schemas

```python
import asyncio
from watercooler_mcp.server import mcp

async def show_tools():
    tools = await mcp.get_tools()
    for name, tool in tools.items():
        print(f"\n{name}:")
        print(f"  Description: {tool.description}")
        print(f"  Parameters: {tool.parameters}")

asyncio.run(show_tools())
```

## Project Status

**See [ROADMAP.md](../ROADMAP.md) for complete phase history and future plans.**

### Completed ✅
- **Phase 1A (v0.1.0)**: MVP MCP server with 9 tools + 1 resource
- **Phase 1B (v0.2.0)**: Upward directory search, comprehensive documentation, Python 3.10+
- **Phase 2A**: Git-based cloud sync with Entry-ID idempotency and retry logic
- **Branch Sync Enforcement**: Automatic validation and tools for maintaining 1:1 branch correspondence
- **Auto-Remediation**: Preflight state machine with per-topic locking, automatic branch checkout/creation, and safe push-with-retry

### Deferred Features (Evaluate Based on Usage)
- **JSON format support**: Structured output for programmatic clients
- **Pagination**: Handle large thread counts efficiently
- **Additional tools**: `search_threads`, `create_thread`, `list_updates`, `break_lock`
- **Enhanced validation**: Error classes, enumeration helpers

### Planned (When Needed)
- **Managed cloud deployment**: OAuth authentication, multi-tenant hosting (Phase 2B/3)

## See Also

- [watercooler-cloud README](../README.md) - Main project documentation
- [L5 MCP Plan](../L5_MCP_PLAN.md) - Detailed implementation plan
- [Python API Reference](./archive/integration.md#python-api-reference) - Watercooler library API
- [Integration Guide](./archive/integration.md) - Using watercooler-cloud in projects

## Support

- **Issues**: https://github.com/mostlyharmless-ai/watercooler-cloud/issues
- **Discussions**: Use GitHub Discussions for questions
- **MCP Protocol**: https://spec.modelcontextprotocol.io/
- **FastMCP Docs**: https://gofastmcp.com/
