# Memory Module

The `watercooler_memory` module provides tools for building a memory graph from watercooler threads, enabling semantic search, entity extraction, and integration with LeanRAG.

## Installation

The memory module requires additional dependencies. Install with:

```bash
pip install 'watercooler-cloud[memory]'
```

Or using uvx:

```bash
uvx watercooler-cloud[memory]
```

## Quick Start

```python
from watercooler_memory import MemoryGraph, GraphConfig

# Create a graph with default config (no API calls)
config = GraphConfig(
    generate_summaries=False,
    generate_embeddings=False,
)
graph = MemoryGraph(config)

# Build from threads directory
graph.build("/path/to/threads")

# Export to LeanRAG format
from watercooler_memory import export_to_leanrag
manifest = export_to_leanrag(graph, "/path/to/output")

# Save graph for later use
graph.save("/path/to/graph.json")
```

## Required Infrastructure

The memory system uses standardized infrastructure across all tiers (MemoryGraph, Graphiti, LeanRAG). This ensures embedding compatibility and enables cross-tier retrieval.

### Component Matrix

| Component | Standard | Port/Path | Purpose |
|-----------|----------|-----------|---------|
| **Embedding Server** | BGE-M3 (1024-d) | `localhost:8080/v1` | OpenAI-compatible embeddings |
| **Graph Database** | FalkorDB | `localhost:6379` | Graph storage + native vectors |
| **Summarization** | DeepSeek/Local | `localhost:8000/v1` | Optional LLM summaries |

### Environment Variables

All tiers share these standardized environment variables:

```bash
# Embedding configuration (required for semantic search)
EMBEDDING_API_BASE=http://localhost:8080/v1
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIM=1024

# Graph database
FALKORDB_HOST=localhost
FALKORDB_PORT=6379

# LLM for summaries (optional)
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_API_BASE=https://api.deepseek.com/v1
```

### Dimension Enforcement

All embedding vectors are validated to be exactly 1024 dimensions:

```python
from watercooler_memory.infrastructure import (
    validate_embedding_dimension,
    enforce_dimension,
    DimensionMismatchError,
)

# Validate single embedding
validate_embedding_dimension(embedding)  # Raises if not 1024-d

# Decorator for embedder functions
@enforce_dimension
def my_embedder(text: str) -> list[float]:
    return model.encode(text)  # Validated on return
```

This prevents dimension mismatches that would corrupt vector similarity searches.

### Quick Infrastructure Setup

**1. Start FalkorDB (graph + vectors):**
```bash
docker run -d -p 6379:6379 falkordb/falkordb:latest
```

**2. Start embedding server (choose one):**
```bash
# Option A: Local server (offline, ~2GB model download)
python -m watercooler_memory.embedding_server

# Option B: External API (requires internet + API key)
export EMBEDDING_API_BASE=https://your-embedding-api.com/v1
export EMBEDDING_API_KEY=sk-...
```

**3. Verify infrastructure:**
```bash
# Check FalkorDB
redis-cli -p 6379 PING  # Should return PONG

# Check embedding server
curl http://localhost:8080/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"input": "test", "model": "bge-m3"}'
```

### Three-Tier Architecture

```
Tier 1: MemoryGraph (raw chunks with provenance)
    ↓ feeds into
Tier 2: Graphiti (entity extraction + temporal tracking)
    ↓ feeds into
Tier 3: LeanRAG (hierarchical clustering + semantic aggregation)
```

All tiers share the same embedding space (BGE-M3, 1024-d) for cross-tier retrieval.

---

## Backend Adapters

The memory module supports multiple backend implementations through a pluggable adapter architecture. Each backend provides different memory and retrieval capabilities.

### Available Backends

#### LeanRAG - Hierarchical Graph RAG

**Type:** Entity extraction + Semantic graph
**Version:** Pinned to commit `1ea1360caa50bec5531ac665d39a73bb152d8fb4`
**License:** AAAI-26 acceptance pending
**Graph DB:** FalkorDB (Redis-compatible)
**Vector DB:** Milvus (optional)

**Capabilities:**
- Entity and relation extraction from documents
- Hierarchical semantic clustering (GMM + UMAP)
- Multi-layer graph construction
- Reduced redundancy (~46% vs flat baselines)
- Batch document processing

**Use Cases:**
- Large document corpus indexing
- Knowledge base construction
- Semantic search with redundancy reduction

**Setup Guide:** [LEANRAG_SETUP.md](LEANRAG_SETUP.md)

---

#### Graphiti - Episodic Memory + Hybrid Search

**Type:** Episodic memory + Temporal graph
**Version:** Pinned to commit `1de752646a9557682c762b83a679d46ffc67e821`
**License:** Apache-2.0
**Graph DB:** FalkorDB or Neo4j
**Vector DB:** Built-in (embeddings in graph nodes)

**Capabilities:**
- Episodic ingestion (one episode per entry)
- Temporal entity tracking with time-aware edges
- Automatic fact extraction and deduplication
- Hybrid search (semantic + graph traversal)
- Chronological reasoning

**Use Cases:**
- Conversation tracking and audit trails
- Time-sensitive knowledge retrieval
- Who-said-what-when queries

**Setup Guide:** [GRAPHITI_SETUP.md](GRAPHITI_SETUP.md)

---

### Comparison Matrix

| Feature | LeanRAG | Graphiti |
|---------|---------|----------|
| **Memory Model** | Entity extraction + clustering | Episodic temporal events |
| **Ingestion** | Batch documents | Sequential episodes |
| **Graph Structure** | Hierarchical semantic layers | Entities + temporal edges |
| **Search** | Hierarchical retrieval | Hybrid (semantic + graph) |
| **Time Awareness** | No | Yes (built-in) |
| **Deduplication** | Clustering-based | Automatic fact merging |
| **LLM Requirement** | Optional (local models) | Required (OpenAI/compatible) |
| **Best For** | Knowledge bases | Conversation tracking |

### Backend Architecture

The memory module provides a **pluggable backend architecture** that allows you to swap memory implementations without changing application code. This is implemented through a Python Protocol (not inheritance), enabling clean decoupling and runtime type checking.

#### MemoryBackend Protocol

All backends implement the `MemoryBackend` protocol:

```python
from watercooler_memory.backends import MemoryBackend, CorpusPayload, QueryPayload

class MemoryBackend(Protocol):
    """Protocol defining the contract all memory backends must implement."""

    def prepare(self, corpus: CorpusPayload) -> PrepareResult:
        """Prepare watercooler corpus for indexing (e.g., chunk, extract entities)."""

    def index(self, chunks: ChunkPayload) -> IndexResult:
        """Index prepared chunks into the backend's storage."""

    def query(self, queries: QueryPayload) -> QueryResult:
        """Execute semantic search queries and return results."""

    def healthcheck(self) -> HealthStatus:
        """Check backend health and dependencies."""
```

#### Canonical Payloads

The protocol uses versioned payload types for all operations:

**CorpusPayload** - Input for `prepare()`:
```python
{
    "manifest_version": "1.0.0",
    "threads": [...],        # Thread metadata
    "entries": [...]         # Entry data with agent, role, timestamp, body
}
```

**ChunkPayload** - Input for `index()`:
```python
{
    "manifest_version": "1.0.0",
    "chunks": [              # Text chunks with metadata
        {
            "hash_code": "abc123",
            "text": "chunk content",
            "metadata": {...}
        }
    ]
}
```

**QueryPayload** - Input for `query()`:
```python
{
    "manifest_version": "1.0.0",
    "queries": [
        {"query": "What is the authentication approach?"}
    ]
}
```

**Result Types** - Outputs from operations:
```python
PrepareResult(prepared_count: int, ...)
IndexResult(indexed_count: int, ...)
QueryResult(results: List[QueryResultItem], ...)
HealthStatus(ok: bool, details: str, ...)
```

#### Exception Hierarchy

The backend architecture provides structured error handling:

```python
from watercooler_memory.backends import BackendError, ConfigError, TransientError

try:
    backend.prepare(corpus)
except ConfigError as e:
    # Configuration issues (missing API keys, invalid paths, etc.)
    print(f"Configuration error: {e}")
except TransientError as e:
    # Temporary failures (database timeouts, API rate limits)
    # Safe to retry
    print(f"Transient error (retryable): {e}")
except BackendError as e:
    # General backend errors
    print(f"Backend error: {e}")
```

#### Using Backends

**Get a backend by name:**
```python
from watercooler_memory.backends import get_backend, LeanRAGConfig

config = LeanRAGConfig(work_dir=Path("./memory"))
backend = get_backend("leanrag", config)

# Use the backend
backend.prepare(corpus)
backend.index(chunks)
results = backend.query(queries)
```

**Direct instantiation:**
```python
from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig

config = LeanRAGConfig(work_dir=Path("./memory"))
backend = LeanRAGBackend(config)
```

#### Architecture Benefits

- **Pluggability**: Swap backends without changing application code
- **Testability**: Use `NullBackend` for unit tests without external dependencies
- **Extensibility**: Add new backends by implementing the `MemoryBackend` protocol
- **Type Safety**: `@runtime_checkable` protocol provides runtime type validation
- **Versioning**: Manifest versioning enables backward compatibility

See [ADR 0001](adr/0001-memory-backend-contract.md) for complete contract specification and design rationale.

---

## Multi-Tier Query Strategy

The memory module includes an intelligent query orchestrator that routes queries across tiers and automatically escalates when needed. This implements the principle: **"Always choose the cheapest tier that can satisfy the intent."**

### Tier Characteristics

| Tier | Backend | Cost | Best For |
|------|---------|------|----------|
| **T1** | Baseline Graph (JSONL) | Lowest (no LLM) | Keyword search, time-based queries, simple lookups |
| **T2** | Graphiti (FalkorDB) | Medium | Entity relationships, temporal queries, verified facts |
| **T3** | LeanRAG (Hierarchical) | Highest | Synthesis, complex multi-hop reasoning, narratives |

### Orchestration Flow

```
Query Intent Detection
         ↓
  Select Starting Tier (based on intent)
         ↓
  Query Tier → Evaluate Sufficiency
         ↓
  Sufficient? → Return Results
         ↓ No
  Escalate to Next Tier (if allowed)
         ↓
  Repeat until sufficient or max_tiers reached
```

### Query Intent Detection

The orchestrator detects query intent to select the optimal starting tier:

| Intent | Keywords | Starting Tier |
|--------|----------|---------------|
| `lookup` | (default) | T1 |
| `temporal` | when, before, after, timeline | T2 |
| `entity` | who, what is, find, class, function | T2 |
| `relational` | related to, depends on, uses | T2 |
| `summarize` | summarize, overview, explain | T2 |
| `multi_hop` | how did, why did, trace, path from | T3 |

### MCP Tool: `watercooler_smart_query`

The `watercooler_smart_query` tool provides unified access to multi-tier querying:

```python
# Example usage
smart_query(
    query="What authentication patterns did we implement?",
    code_path=".",
    max_tiers=2,  # Query at most 2 tiers
)
```

**Parameters:**
- `query`: Search query string
- `code_path`: Path to code repository (for T2/T3)
- `threads_dir`: Path to threads directory (for T1)
- `max_tiers`: Maximum tiers to query (default: 2)
- `force_tier`: Force specific tier ("T1", "T2", "T3")
- `group_ids`: Optional list of group IDs to filter

**Response Format:**
```json
{
  "query": "What authentication patterns did we implement?",
  "result_count": 5,
  "tiers_queried": ["T1", "T2"],
  "primary_tier": "T2",
  "escalation_reason": "Only 2 results (need 3)",
  "sufficient": true,
  "evidence": [
    {
      "tier": "T1",
      "id": "entry-123",
      "content": "OAuth2 with JWT tokens...",
      "score": 0.85,
      "provenance": {...}
    }
  ],
  "message": "Found 5 results from T2"
}
```

### Configuration

Environment variables for tier control:

```bash
# Enable/disable tiers
WATERCOOLER_TIER_T1_ENABLED=1    # Default: enabled
WATERCOOLER_TIER_T2_ENABLED=1    # Requires WATERCOOLER_GRAPHITI_ENABLED=1
WATERCOOLER_TIER_T3_ENABLED=0    # Expensive, explicit opt-in

# Tuning
WATERCOOLER_TIER_MAX_TIERS=2     # Max tiers per query (default: 2)
WATERCOOLER_TIER_MIN_RESULTS=3   # Min results for sufficiency
```

### Sufficiency Evaluation

Escalation occurs when:
1. **Insufficient results**: Fewer than `min_results` (default: 3)
2. **Low confidence**: Average score below `min_confidence` (default: 0.5)

### Safety Rules

The orchestrator enforces these safety principles:
- **Never escalate to T3 "just to be helpful"** - T3 is expensive
- **Never allow T3 to invent facts** - T3 synthesizes, doesn't create
- **Surface uncertainty explicitly** - Don't hallucinate to fill gaps

### Python API

```python
from watercooler_memory.tier_strategy import (
    TierOrchestrator,
    TierConfig,
    load_tier_config,
    smart_query,
    Tier,
)

# Quick usage
result = smart_query(
    "What error handling patterns did we use?",
    threads_dir=Path("./threads"),
    code_path=Path("."),
)

# Or with explicit configuration
config = load_tier_config(
    threads_dir=Path("./threads"),
    code_path=Path("."),
)
config.max_tiers = 2
config.min_results = 5

orchestrator = TierOrchestrator(config)
result = orchestrator.query("authentication implementation")

# Access results
for evidence in result.top_results(5):
    print(f"[{evidence.tier}] {evidence.content[:100]}")
```

---

## Querying Memory via MCP

> **⚠️ Tool Consolidation Notice (January 2026)**
>
> `watercooler_query_memory` has been replaced by `watercooler_smart_query`, which provides multi-tier intelligent querying with auto-escalation. See [mcp-server.md](./mcp-server.md#memory-query-tools) for the full replacement mapping.

The Watercooler MCP server provides memory query tools for querying thread history using Graphiti's temporal graph memory. This enables agents to ask natural language questions about project context, implementation details, and decisions.

### Quick Setup

**1. Install memory extras:**
```bash
pip install watercooler-cloud[memory]
```

**2. Configure MCP server** (example for Codex):
```toml
[mcp_servers.watercooler_cloud.env]
WATERCOOLER_GRAPHITI_ENABLED = "1"
OPENAI_API_KEY = "sk-..."
```

**3. Start FalkorDB:**
```bash
docker run -d -p 6379:6379 falkordb/falkordb:latest
```

**4. Build index:**

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
  --topics auth-feature memory-backend graphiti-integration

# Or use a thread list file
python -m watercooler_memory.pipeline run \
  --backend graphiti \
  --threads /path/to/watercooler-cloud-threads \
  --thread-list threads-to-index.txt
```

Example `threads-to-index.txt`:
```
auth-feature.md
memory-backend.md
graphiti-integration.md
```

**5. Query via MCP:**
```python
watercooler_query_memory(
    query="How was authentication implemented?",
    code_path=".",
    limit=10
)
```

### Query Capabilities

**Cross-thread queries** (search entire project):
```python
# Find context across all threads
watercooler_query_memory(
    query="What error handling patterns were used?",
    code_path="."
)
```

**Single-thread queries** (focused search):
```python
# Search within specific thread
watercooler_query_memory(
    query="What tests were added?",
    topic="auth-feature",
    code_path="."
)
```

**Temporal queries:**
```python
# Discover evolution over time
watercooler_query_memory(
    query="How did the API design change over time?",
    code_path=".",
    limit=20
)
```

### Complete Documentation

- **MCP Tool Reference**: [mcp-server.md#watercooler_query_memory](./mcp-server.md#watercooler_query_memory)
- **Environment Variables**: [ENVIRONMENT_VARS.md#graphiti-memory-variables](./ENVIRONMENT_VARS.md#graphiti-memory-variables)
- **Graphiti Setup Guide**: [GRAPHITI_SETUP.md](./GRAPHITI_SETUP.md)

---

## Architecture

The graph uses a hierarchical structure:

```
Thread → Entry → Chunk
```

With hyperedges for membership and temporal edges for sequencing.

### Node Types

- **ThreadNode**: Represents a watercooler thread with metadata (status, ball, timestamps)
- **EntryNode**: Represents an individual entry in a thread (agent, role, type, body)
- **ChunkNode**: Text chunks created by splitting entry bodies for embedding
- **EntityNode**: Named entities extracted from chunks (future feature)

### Edge Types

- **CONTAINS**: Thread contains entries, entries contain chunks
- **FOLLOWS**: Temporal sequence between entries
- **MENTIONS**: Entity mentions (future feature)

## Configuration

### GraphConfig

```python
from watercooler_memory import GraphConfig

config = GraphConfig(
    # Enable/disable LLM summary generation
    generate_summaries=True,

    # Enable/disable embedding generation
    generate_embeddings=True,

    # Chunker settings
    chunk_max_tokens=1024,
    chunk_overlap_tokens=100,
)
```

### Environment Variables

For summary generation:
- `DEEPSEEK_API_KEY`: API key for DeepSeek LLM
- `LLM_API_BASE`: LLM API endpoint (default: `https://api.deepseek.com/v1`)

For embedding generation:
- `EMBEDDING_API_BASE`: bge-m3 API endpoint (default: `http://localhost:8080/v1`)

## Local Server Setup (Free Tier)

For fully offline operation without external APIs, use the built-in llama-cpp-python servers:

### Start Both Servers

```bash
# Terminal 1: Summarization server (port 8000)
python -m watercooler_memory.local_server

# Terminal 2: Embedding server (port 8080)
python -m watercooler_memory.embedding_server
```

Both servers auto-download their default models on first run:
- **Summarization**: Qwen2.5-3B-Instruct (~2GB)
- **Embeddings**: bge-m3 (~2GB)

### Configure Environment

```bash
export LLM_API_BASE=http://localhost:8000/v1
export LLM_MODEL=local
export EMBEDDING_API_BASE=http://localhost:8080/v1
export EMBEDDING_MODEL=bge-m3
```

### Architecture

| Function | Model | Port | Endpoint |
|----------|-------|------|----------|
| Summarization | Qwen2.5-3B | 8000 | `/v1/chat/completions` |
| Embeddings | bge-m3 | 8080 | `/v1/embeddings` |

Both servers use llama-cpp-python with OpenAI-compatible APIs. No external dependencies required.

## CLI Usage

Build a memory graph:

```bash
# Basic build (no API calls)
python scripts/build_memory_graph.py /path/to/threads -o graph.json

# With summaries (requires DEEPSEEK_API_KEY)
python scripts/build_memory_graph.py /path/to/threads -o graph.json

# Skip summaries
python scripts/build_memory_graph.py /path/to/threads --no-summaries -o graph.json

# Skip embeddings
python scripts/build_memory_graph.py /path/to/threads --no-embeddings -o graph.json

# Export to LeanRAG format
python scripts/build_memory_graph.py /path/to/threads --export-leanrag ./leanrag-output
```

## LeanRAG Export

The module exports to LeanRAG-compatible format for knowledge graph building.

### Export Format

The export creates three files:

**manifest.json**
```json
{
  "format": "leanrag",
  "version": "1.0",
  "source": "watercooler-cloud",
  "statistics": {
    "threads": 51,
    "documents": 530,
    "chunks": 569,
    "embeddings_included": true
  },
  "files": {
    "documents": "documents.json",
    "threads": "threads.json"
  }
}
```

**documents.json** - Entries as documents with chunks:
```json
[
  {
    "doc_id": "entry-001",
    "title": "Implementation Complete",
    "content": "Full entry body...",
    "summary": "Generated summary...",
    "chunks": [
      {
        "hash_code": "abc123",
        "text": "Chunk text...",
        "token_count": 100,
        "embedding": [0.1, 0.2, ...]
      }
    ],
    "metadata": {
      "thread_id": "feature-auth",
      "agent": "Claude",
      "role": "implementer",
      "entry_type": "Note",
      "timestamp": "2025-01-01T12:00:00Z"
    }
  }
]
```

**threads.json** - Thread metadata:
```json
[
  {
    "thread_id": "feature-auth",
    "title": "Authentication Feature",
    "status": "OPEN",
    "ball": "Claude",
    "entry_count": 10,
    "summary": "Thread summary..."
  }
]
```

### Validation

Exports are validated by default against the LeanRAG schema:

```python
from watercooler_memory import export_to_leanrag, ValidationError

try:
    manifest = export_to_leanrag(graph, output_dir)
except ValidationError as e:
    print(f"Validation failed: {e}")
    for error in e.errors:
        print(f"  {error['type']}: {error['errors']}")

# Skip validation if needed
manifest = export_to_leanrag(graph, output_dir, validate=False)
```

### Direct Validation

```python
from watercooler_memory import (
    validate_chunk,
    validate_document,
    validate_export,
    validate_pipeline_chunks,
    ValidationError,
)

# Validate a single chunk
errors = validate_chunk({"hash_code": "abc", "text": "Hello"})
if errors:
    print("Invalid chunk:", errors)

# Validate full export
try:
    validate_export(documents, threads, manifest)
except ValidationError as e:
    print("Export validation failed")
```

## Schema Reference

### JSON Schemas

The module provides JSON schemas for validation:

```python
from watercooler_memory import (
    LEANRAG_CHUNK_SCHEMA,
    LEANRAG_DOCUMENT_SCHEMA,
    LEANRAG_MANIFEST_SCHEMA,
    LEANRAG_PIPELINE_CHUNK_SCHEMA,
)
```

### Chunk Schema (Required Fields)

```json
{
  "hash_code": "string (required, non-empty)",
  "text": "string (required)",
  "embedding": "array of numbers or null (optional)",
  "metadata": "object (optional)"
}
```

### Document Schema (Required Fields)

```json
{
  "doc_id": "string (required, non-empty)",
  "content": "string (required)",
  "chunks": "array of chunks (required)"
}
```

## Caching

The module includes caching for summaries and embeddings to avoid redundant API calls:

```python
from watercooler_memory import (
    SummaryCache,
    EmbeddingCache,
    cache_stats,
    clear_cache,
)

# View cache statistics
stats = cache_stats()
print(f"Summary cache: {stats['summaries']} entries")
print(f"Embedding cache: {stats['embeddings']} entries")

# Clear caches
clear_cache()
```

## API Reference

### MemoryGraph

```python
class MemoryGraph:
    def __init__(self, config: GraphConfig = None): ...
    def build(self, threads_dir: Path, branch_context: str = None,
              progress_callback: Callable = None): ...
    def add_thread(self, thread_path: Path) -> ThreadNode: ...
    def chunk_all_entries(self) -> list[ChunkNode]: ...
    def stats(self) -> dict[str, int]: ...
    def to_dict(self) -> dict: ...
    def save(self, path: Path): ...
    @classmethod
    def load(cls, path: Path) -> "MemoryGraph": ...
```

### Export Functions

```python
def export_to_leanrag(
    graph: MemoryGraph,
    output_dir: Path,
    include_embeddings: bool = True,
    validate: bool = True,
) -> dict[str, Any]: ...

def export_for_leanrag_pipeline(
    graph: MemoryGraph,
    output_path: Path,
    validate: bool = True,
) -> None: ...
```

### Validation Functions

```python
def validate_chunk(chunk: dict) -> list[str]: ...
def validate_document(doc: dict) -> list[str]: ...
def validate_manifest(manifest: dict) -> list[str]: ...
def validate_export(documents: list, threads: list, manifest: dict) -> None: ...
def validate_pipeline_chunks(chunks: list) -> None: ...
```

## Checkpointing and Recovery

For long-running graph builds, use checkpointing to save progress and enable recovery from failures.

### Using Checkpoints

```python
from watercooler_memory import MemoryGraph, GraphConfig
from pathlib import Path

graph = MemoryGraph()

# Build with checkpointing enabled
graph.build(
    threads_dir=Path("./threads"),
    checkpoint_path=Path("./graph-checkpoint.json"),
    timeout=3600,  # 1 hour timeout
)
```

The checkpoint file is saved atomically after each stage (parsing, chunking, summarization, embeddings).

### Recovery Workflow

If a build fails partway through:

```python
from watercooler_memory import MemoryGraph
from pathlib import Path

checkpoint_path = Path("./graph-checkpoint.json")

if checkpoint_path.exists():
    # Load from checkpoint
    print("Resuming from checkpoint...")
    graph = MemoryGraph.load(checkpoint_path)

    # Check what's already done
    stats = graph.stats()
    print(f"Loaded: {stats['threads']} threads, {stats['entries']} entries")
    print(f"Summaries: {stats['entries_with_summaries']}/{stats['entries']}")

    # Continue remaining steps manually
    if stats['entries_with_summaries'] < stats['entries']:
        graph.generate_summaries()
    if stats['entries_with_embeddings'] < stats['entries']:
        graph.generate_embeddings()

    # Save final result
    graph.save(Path("./graph.json"))
else:
    # Fresh build
    graph = MemoryGraph()
    graph.build(
        threads_dir=Path("./threads"),
        checkpoint_path=checkpoint_path,
    )
```

### Caching Benefits

Even without explicit checkpoints, the disk caches for summaries and embeddings survive pipeline failures:

- **Summary cache**: `~/.cache/watercooler/summaries/`
- **Embedding cache**: `~/.cache/watercooler/embeddings/`

Re-running `generate_summaries()` or `generate_embeddings()` automatically reuses cached results, so failed builds can be restarted without re-processing already-completed items.

### Timeout Handling

```python
try:
    graph.build(
        threads_dir=Path("./threads"),
        timeout=1800,  # 30 minutes
        checkpoint_path=Path("./checkpoint.json"),
    )
except TimeoutError:
    print("Build timed out - checkpoint saved")
    # Load checkpoint and continue in a new session
```

## Graceful Degradation

The module supports graceful degradation when optional dependencies are missing:

```python
from watercooler_memory import MEMORY_AVAILABLE

if MEMORY_AVAILABLE:
    # Full functionality available
    from watercooler_memory import MemoryGraph
    graph = MemoryGraph()
else:
    # Validation is always available (no external deps)
    from watercooler_memory import validate_chunk
    errors = validate_chunk({"hash_code": "abc", "text": "test"})
```

## Integration with LeanRAG

The exported format is designed to work with LeanRAG's entity extraction and graph building pipeline:

```bash
# 1. Build memory graph from watercooler threads
python scripts/build_memory_graph.py ./threads --export-leanrag ./export

# 2. Run LeanRAG entity extraction on exported chunks
python /path/to/LeanRAG/process_markdown_pipeline.py ./export/documents.json

# 3. Build LeanRAG knowledge graph
python /path/to/LeanRAG/build_graph.py ./export
```

The chunks contain the required `hash_code` and `text` fields that LeanRAG's pipeline expects.

---

## MCP Tools for Memory Integration

### Phase 1 Tools (Implemented)

The following MCP tools provide memory backend integration:

#### Write Tools

| Tool | Description |
|------|-------------|
| `watercooler_graphiti_add_episode` | Add content directly to Graphiti temporal graph |
| `watercooler_leanrag_run_pipeline` | Trigger LeanRAG clustering pipeline |

#### Search Tools

| Tool | Description |
|------|-------------|
| `watercooler_smart_query` | Multi-tier intelligent query with auto-escalation |
| `watercooler_search` | Unified search with tier-aware routing (`mode`: entries/entities/episodes) |
| `watercooler_get_entity_edge` | Get specific entity/edge by UUID |
| `watercooler_diagnose_memory` | Diagnose memory backend status |

> **Removed tools** (use replacements above):
> - `watercooler_query_memory` → `watercooler_smart_query`
> - `watercooler_search_nodes` → `watercooler_search(mode="entities")`
> - `watercooler_search_memory_facts` → `watercooler_smart_query`
> - `watercooler_get_episodes` → `watercooler_search(mode="episodes")`

#### Migration

Migration tools have been moved to a standalone script due to MCP SDK timeout limitations.
Use `scripts/index_graphiti.py` to migrate threads to Graphiti:

```bash
# Set required environment variables
export LLM_API_KEY=sk-...  # or "local" for local LLM
export EMBEDDING_API_KEY=sk-...  # or "local" for local embeddings
export LLM_API_BASE=http://localhost:8081/v1  # optional, for local LLM
export EMBEDDING_API_BASE=http://localhost:8080/v1  # optional, for local embeddings

# Migrate specific threads
python scripts/index_graphiti.py --threads auth-feature api-design

# Or migrate from a list file
python scripts/index_graphiti.py --thread-list threads-to-index.txt
```

See [MCP SDK Issue #245](https://github.com/modelcontextprotocol/typescript-sdk/issues/245) for why migration was moved out of MCP.

### Search Routing

The `watercooler_search` tool supports tier-aware routing:

```python
# Use baseline graph (free tier, always available)
watercooler_search(query="authentication", backend="baseline")

# Use Graphiti memory backend (requires setup)
watercooler_search(query="authentication", backend="graphiti")

# Auto-detect based on WATERCOOLER_MEMORY_BACKEND env var
watercooler_search(query="authentication", backend="auto")

# Search modes
watercooler_search(query="OAuth2", mode="entries")   # Search entries
watercooler_search(query="OAuth2", mode="entities")  # Search entity nodes
watercooler_search(query="OAuth2", mode="episodes")  # Search episodes
```

### Automatic Sync Hook

When `WATERCOOLER_MEMORY_BACKEND` is set, new entries are automatically synced to the memory backend after baseline graph sync:

```bash
export WATERCOOLER_MEMORY_BACKEND=graphiti
```

The sync is non-blocking - errors are logged but never fail the baseline sync.

---

## Troubleshooting

### No Tiers Available

If `smart_query` returns "No memory tiers available":

1. **Check T1 is enabled**: `WATERCOOLER_TIER_T1_ENABLED=1`
2. **Verify graph files exist**: `threads/graph/baseline/nodes.jsonl` should be present
3. **For T2**: Ensure `WATERCOOLER_GRAPHITI_ENABLED=1` and FalkorDB is running
4. **For T3**: Confirm `LEANRAG_PATH` is set and points to valid LeanRAG installation

### Context Resolution Errors

If you see "Failed to resolve context" or similar errors:

1. **Check code_path**: Ensure it points to a valid git repository
2. **Verify threads directory**: The sibling `{repo-name}-threads` directory must exist
3. **Check git status**: Run `git status` in the code_path to verify it's a valid repo
4. **Permissions**: Ensure read access to both the code repo and threads directory

### Low Confidence Results

If queries return results but with low confidence:

1. **Check embedding server**: Verify `EMBEDDING_API_BASE` is reachable
2. **Rebuild baseline graph**: Run the indexer to refresh embeddings
3. **Try force_tier**: Use `force_tier="T2"` to bypass T1 if T2 has better coverage

---

## See Also

- [Configuration Guide](CONFIGURATION.md) - Memory backend configuration
- [Tier Migration Guide](TIER_MIGRATION.md) - Migrating to memory backends
- [Graphiti Setup](GRAPHITI_SETUP.md) - Graphiti installation
- [LeanRAG Setup](LEANRAG_SETUP.md) - LeanRAG installation
