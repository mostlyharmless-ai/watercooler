# Graphiti Backend Setup

This guide covers setting up Graphiti as a memory backend for watercooler-cloud.

## Quick Start for Teammates

**Get up and running in 5 minutes:**

### 1. Start FalkorDB (Graph Database)

```bash
# Start FalkorDB container with increased query timeout (if not already running)
docker run -d -p 6379:6379 -p 3000:3000 --name falkordb \
  -v falkordb_data:/var/lib/falkordb/data \
  -e FALKORDB_ARGS="TIMEOUT 120000" \
  falkordb/falkordb:latest

# Verify it's running
docker exec falkordb redis-cli PING  # Should return: PONG
```

> **Important:** The `FALKORDB_ARGS="TIMEOUT 120000"` sets the query timeout to 120 seconds. Without this, complex graph queries (especially fulltext search on large datasets) will timeout after just 1 second (the default).

### 2. Install Graphiti Dependencies

**Option A: Install as package (recommended for users)**

```bash
# Using uvx (self-contained, nothing to install locally)
uvx --from 'watercooler-cloud[graphiti]' watercooler-mcp

# Or using pip (includes graphiti-core[falkordb] automatically)
pip install 'watercooler-cloud[graphiti]'
```

**Option B: Development with submodules (for contributors)**

```bash
# Clone with submodules
git clone --recurse-submodules https://github.com/mostlyharmless-ai/watercooler-cloud

# Install graphiti from submodule (editable, with FalkorDB support)
uv pip install -e "external/graphiti[falkordb]"
```

### 3. Configure Environment Variables

Add to your MCP config (`.mcp.json` for Claude Code):

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_GRAPHITI_ENABLED": "1",
        "LLM_API_KEY": "sk-...",
        "EMBEDDING_API_KEY": "sk-..."
      }
    }
  }
}
```

Or for local LLM/embedding servers:

```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "env": {
        "WATERCOOLER_GRAPHITI_ENABLED": "1",
        "LLM_API_BASE": "http://localhost:11434/v1",
        "LLM_API_KEY": "not-needed-for-local",
        "LLM_MODEL": "llama3.2:3b",
        "EMBEDDING_API_BASE": "http://localhost:8080/v1",
        "EMBEDDING_API_KEY": "not-needed-for-local",
        "EMBEDDING_MODEL": "bge-m3"
      }
    }
  }
}
```

### 4. Restart MCP Server

In Claude Code, run `/mcp` to reconnect the MCP server and pick up the new configuration.

### 5. Test It Works

**Add a test episode:**
```
Use watercooler_graphiti_add_episode to add:
- content: "Testing the Graphiti memory backend"
- group_id: "test-graphiti"
- title: "Test Episode"
```

**Query the memory:**
```
Use watercooler_smart_query with:
- query: "Graphiti memory"
- code_path: "."
```

> **Note:** `watercooler_query_memory` has been replaced by `watercooler_smart_query`.

You should see the test episode returned with extracted entities and facts.

### Troubleshooting Quick Start

| Symptom | Solution |
|---------|----------|
| `Query timed out` on search | Increase timeout: `docker exec falkordb redis-cli GRAPH.CONFIG SET TIMEOUT 120000` (see [Troubleshooting](#falkordb-query-timeout)) |
| `No module named 'graphiti_core'` | Run `uv pip install -e "external/graphiti[falkordb]"` and restart MCP server |
| `Database connection failed` | Ensure FalkorDB is running: `docker ps \| grep falkor` |
| `no episode UUID` error | Update to latest code (fixed in PR #93) |
| `unexpected keyword argument 'max_nodes'` | Update to latest code (fixed in PR #93) |

---

## Overview

**Graphiti** is a temporal, entity-based memory layer featuring episodic ingestion and hybrid search. As a watercooler memory backend, it provides:

- **Episodic ingestion** from thread entries
- **Entity-centric knowledge graph** with temporal edges
- **Hybrid search** combining semantic similarity and graph traversal
- **Fact extraction** with automatic deduplication
- **Time-aware retrieval** for chronological reasoning

**Backend type:** Episodic memory + Hybrid search
**Graph database:** FalkorDB (or Neo4j)
**Vector database:** Built-in (embeddings stored in graph nodes)
**License:** Apache-2.0

---

## Version & License

**Pinned commit:** `1de752646a9557682c762b83a679d46ffc67e821`
**License:** Apache-2.0
**Repository:** https://github.com/mostlyharmless-ai/graphiti
**Submodule location:** `external/graphiti/`

---

## Prerequisites

### 1. FalkorDB (Graph Database)

**Recommended:** FalkorDB (Redis-compatible graph database)

Install via Homebrew (macOS):
```bash
brew tap falkordb/tap
brew install falkordb
```

Install via Docker:
```bash
docker run -d -p 6379:6379 -p 3000:3000 --name falkordb \
  -v falkordb_data:/var/lib/falkordb/data \
  -e FALKORDB_ARGS="TIMEOUT 120000" \
  falkordb/falkordb:latest
```

> **Note:** The `TIMEOUT 120000` setting is critical—FalkorDB defaults to a 1-second query timeout which causes failures on complex graph queries.

Start FalkorDB:
```bash
falkordb-server
```

Verify connection:
```bash
redis-cli ping  # Should return: PONG
```

**Connection details:**
- Host: `localhost`
- Port: `6379`
- Protocol: Redis-compatible

### 2. OpenAI API (or Alternative)

Graphiti requires an LLM for entity extraction and fact generation. Options:

**Option 1: OpenAI (Recommended)**
```bash
export OPENAI_API_KEY=your_openai_api_key
```

**Option 2: OpenAI-compatible Local LLM (llama-server)**
```bash
# llama-server auto-starts when configured for localhost
# Configure Graphiti to use local endpoint
export OPENAI_API_BASE=http://localhost:8000/v1
export OPENAI_API_KEY=local  # Still required, but not validated
```

**Option 3: DeepSeek API (Cost-effective)**
```bash
export OPENAI_API_KEY=your_deepseek_api_key
export OPENAI_API_BASE=https://api.deepseek.com/v1
```

### 3. Graphiti Dependencies

Install Graphiti's Python dependencies:

```bash
cd external/graphiti
pip install -e .

# For FalkorDB support (optional, requires building from source)
pip install -e ".[falkordb]"
```

**Core dependencies:**
- `neo4j` - Neo4j/FalkorDB Python driver
- `openai` - For LLM calls (required)
- `numpy`, `pydantic` - Data structures
- `diskcache` - Local caching
- `posthog` - Analytics (optional)

---

## Configuration

### Environment Variables

Create a `.env.local` file (gitignored):

```bash
# Graphiti Backend Configuration

# FalkorDB Connection
FALKORDB_HOST=localhost
FALKORDB_PORT=6379

# LLM for Entity Extraction (required)
## Option 1: OpenAI
OPENAI_API_KEY=your_openai_api_key

## Option 2: DeepSeek API
# OPENAI_API_KEY=your_deepseek_api_key
# OPENAI_API_BASE=https://api.deepseek.com/v1

## Option 3: Local LLM (llama-server)
# OPENAI_API_BASE=http://localhost:8000/v1
# OPENAI_API_KEY=local

# Embeddings (built-in, uses same LLM API)
# EMBEDDING_MODEL=text-embedding-3-small  # OpenAI default
```

### Graphiti Configuration

Graphiti uses environment variables for configuration. Key settings:

```bash
# Graph database URI
NEO4J_URI=bolt://localhost:6379  # For FalkorDB

# Entity extraction
GRAPHITI_ENTITY_TYPES=Person,Organization,Location,Concept  # Custom entity types

# Chunking (optional override)
GRAPHITI_CHUNK_SIZE=1024
GRAPHITI_CHUNK_OVERLAP=100

# Search parameters
GRAPHITI_SEARCH_LIMIT=10  # Max results per query
```

**Precedence:** Environment variables take precedence over defaults.

---

## Installation Steps

### Step 1: Initialize Submodule

If you haven't already:
```bash
git submodule update --init external/graphiti
cd external/graphiti
git checkout 1de752646a9557682c762b83a679d46ffc67e821
```

### Step 2: Install Dependencies

```bash
cd external/graphiti
pip install -e .

# Optional: Install FalkorDB support
pip install -e ".[falkordb]"
```

### Step 3: Verify FalkorDB Connection

```bash
python3 -c "from neo4j import GraphDatabase; driver = GraphDatabase.driver('bolt://localhost:6379'); print('Connected:', driver.verify_connectivity())"
```

Expected output: `Connected: <neo4j.SessionDetails object>`

### Step 4: Test Graphiti Initialization

```bash
cd external/graphiti
python3 -c "from graphiti_core import Graphiti; g = Graphiti('bolt://localhost:6379', 'OPENAI_API_KEY'); print('Initialized:', g)"
```

---

## Usage with Watercooler

### Export Threads to Graphiti Format

```python
from watercooler_memory import MemoryGraph, export_to_graphiti
from pathlib import Path

# Build memory graph from threads
graph = MemoryGraph()
graph.build("/path/to/threads")

# Export to Graphiti format (episodic)
output_dir = Path("./graphiti-export")
manifest = export_to_graphiti(
    graph,
    output_dir=output_dir,
    include_metadata=True,
    validate=True,
)

print(f"Exported {manifest['episode_count']} episodes")
print(f"Output: {output_dir}")
```

### Ingest into Graphiti

```python
from graphiti_core import Graphiti
from pathlib import Path
import json

# Initialize Graphiti client
graphiti = Graphiti(
    uri="bolt://localhost:6379",
    user=None,  # FalkorDB doesn't require auth
    password=None,
)

# Load exported episodes
episodes_path = Path("./graphiti-export/episodes.json")
episodes = json.loads(episodes_path.read_text())

# Ingest each episode
for episode in episodes:
    graphiti.add_episode(
        name=episode["name"],
        episode_body=episode["content"],
        source_description=episode["source"],
        reference_time=episode["timestamp"],
    )

print(f"Ingested {len(episodes)} episodes into Graphiti")
```

### Query Graphiti

```python
# Search for relevant context
results = graphiti.search(
    query="What are the main authentication features?",
    num_results=5,
)

for result in results:
    print(f"Score: {result.score}")
    print(f"Content: {result.content[:200]}...")
    print(f"Source: {result.metadata['source']}")
    print("---")
```

---

## MCP Integration

The Watercooler MCP server includes `watercooler_smart_query` for querying Graphiti-indexed thread history. This enables agents to ask natural language questions about project context.

> **Note:** `watercooler_query_memory` has been replaced by `watercooler_smart_query`, which provides multi-tier intelligent querying with auto-escalation.

### Quick Setup

**1. Configure MCP server** (example for Codex):
```toml
[mcp_servers.watercooler_cloud.env]
WATERCOOLER_GRAPHITI_ENABLED = "1"
OPENAI_API_KEY = "sk-..."
```

**2. Build index:**

Full corpus:
```bash
python -m watercooler_memory.pipeline run \
  --backend graphiti \
  --threads /path/to/watercooler-cloud-threads
```

Specific threads (for testing or focused analysis):
```bash
# Index specific threads by topic
python -m watercooler_memory.pipeline run \
  --backend graphiti \
  --threads /path/to/watercooler-cloud-threads \
  --topics auth-feature memory-backend

# Or use a thread list file
python -m watercooler_memory.pipeline run \
  --backend graphiti \
  --threads /path/to/watercooler-cloud-threads \
  --thread-list threads-to-index.txt
```

**3. Query via MCP:**
```python
watercooler_smart_query(
    query="How was authentication implemented?",
    code_path="."
)
```

**Database structure:** All threads are stored in a single FalkorDB database with logical partitioning via `group_id`. Queries can search across all threads or filter to a single thread.

**Complete documentation:**
- **MCP Tool Reference**: [mcp-server.md#memory-query-tools](./mcp-server.md#memory-query-tools)
- **Environment Variables**: [ENVIRONMENT_VARS.md#graphiti-memory-variables](./ENVIRONMENT_VARS.md#graphiti-memory-variables)
- **MCP Querying Guide**: [MEMORY.md#querying-memory-via-mcp](./MEMORY.md#querying-memory-via-mcp)

---

## Comparison with LeanRAG

| Feature | Graphiti | LeanRAG |
|---------|----------|---------|
| **Memory Model** | Episodic (temporal events) | Entity extraction (semantic clusters) |
| **Ingestion** | Sequential episodes | Batch document processing |
| **Graph Structure** | Entities + temporal edges | Hierarchical semantic layers |
| **Search** | Hybrid (semantic + graph) | Hierarchical retrieval |
| **Deduplication** | Automatic fact merging | Manual clustering |
| **Time Awareness** | Built-in temporal reasoning | Not time-aware |
| **LLM Required** | Yes (OpenAI or compatible) | Optional (can use local) |
| **Use Case** | Conversation tracking, audit trails | Knowledge base, semantic search |

**When to use Graphiti:**
- Need chronological reasoning
- Tracking conversation flow
- Episodic memory (who said what when)

**When to use LeanRAG:**
- Large document corpus
- Reduced redundancy priority
- Hierarchical semantic search

---

## Troubleshooting

### FalkorDB Query Timeout

**Error:** `Query timed out` during fulltext search or complex graph queries

**Cause:** FalkorDB defaults to a 1-second query timeout (`TIMEOUT 1000`), which is too short for complex queries like fulltext search on large datasets.

**Fix (temporary):**
```bash
# Increase timeout to 120 seconds for current session
docker exec falkordb redis-cli GRAPH.CONFIG SET TIMEOUT 120000

# Verify
docker exec falkordb redis-cli GRAPH.CONFIG GET TIMEOUT
# Should show: TIMEOUT 120000
```

**Fix (permanent):** Recreate the container with the timeout setting:
```bash
# Stop and remove (volume preserves data)
docker stop falkordb && docker rm falkordb

# Restart with permanent timeout
docker run -d -p 6379:6379 -p 3000:3000 --name falkordb \
  -v falkordb_data:/var/lib/falkordb/data \
  -e FALKORDB_ARGS="TIMEOUT 120000" \
  falkordb/falkordb:latest
```

> **Note:** The timeout resets to default (1 second) on container restart unless you use `FALKORDB_ARGS`.

### FalkorDB Connection Failed

**Error:** `ServiceUnavailable: Could not connect to bolt://localhost:6379`

**Fix:**
```bash
# Check if FalkorDB is running
ps aux | grep falkordb-server

# Start FalkorDB if not running
falkordb-server &

# Verify with redis-cli
redis-cli ping
```

### OpenAI API Key Missing

**Error:** `openai.OpenAIError: No API key provided`

**Fix:** Ensure `OPENAI_API_KEY` is set in environment or `.env.local`:
```bash
export OPENAI_API_KEY=your_key_here
```

### Import Error: graphiti_core

**Error:** `ModuleNotFoundError: No module named 'graphiti_core'`

**Fix:** Install Graphiti from submodule:
```bash
cd external/graphiti
pip install -e .
```

### FalkorDB vs Neo4j

Graphiti defaults to Neo4j, but FalkorDB is compatible. Key differences:

- **URI format**: Use `bolt://localhost:6379` (not `neo4j://...`)
- **Authentication**: FalkorDB doesn't require username/password by default
- **Driver**: Use `neo4j` Python driver (compatible with FalkorDB)

**Example initialization:**
```python
from graphiti_core import Graphiti

# For FalkorDB (no auth)
g = Graphiti(uri="bolt://localhost:6379", user=None, password=None)

# For Neo4j (with auth)
# g = Graphiti(uri="neo4j://localhost:7687", user="neo4j", password="password")
```

---

## Architecture Notes

### How Graphiti Integrates with Watercooler

1. **Watercooler** parses threads → builds MemoryGraph
2. **Export** stage converts entries to episodic format (one episode per entry)
3. **Graphiti** ingests episodes → extracts entities → builds temporal graph
4. **Adapter** (future) wraps Graphiti behind MemoryBackend contract

**Current status:** Direct integration via export/import. Backend adapter layer coming in Phase 2.

### Data Flow

```
Watercooler Threads (.md)
    ↓ (parse entries)
 MemoryGraph (watercooler_memory)
    ↓ (export_to_graphiti)
 Episodic Format (episodes.json)
    ↓ (graphiti.add_episode)
 FalkorDB Temporal Graph
    ↓ (graphiti.search)
 Hybrid Retrieval Results
```

### Episodic Model

Each watercooler entry becomes a Graphiti **episode**:
- **Name**: Entry title
- **Content**: Entry body
- **Source**: Thread topic + entry metadata (agent, role, type)
- **Reference time**: Entry timestamp

Graphiti automatically:
- Extracts entities (people, concepts, etc.)
- Generates facts with confidence scores
- Creates temporal edges between related episodes
- Deduplicates entities across episodes

---

## Next Steps

1. **Test the pipeline** with a small thread (2-3 entries)
2. **Verify FalkorDB** has expected nodes/edges after ingestion
3. **Try a search query** to validate retrieval works
4. **Scale up** to full thread corpus once validated

For integration with the MemoryBackend contract, see `docs/adr/NNN-memory-backend-contract.md` (coming in Phase 2).

---

## See Also

- [Memory Module Documentation](MEMORY.md)
- [LeanRAG Setup Guide](LEANRAG_SETUP.md) (alternative backend)
- [Graphiti Official Docs](https://github.com/mostlyharmless-ai/graphiti)
- [FalkorDB Documentation](https://docs.falkordb.com/)
