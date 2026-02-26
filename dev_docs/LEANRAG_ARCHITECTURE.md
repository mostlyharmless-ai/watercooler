# LeanRAG Graph Architecture Research: Complete Analysis

## Executive Summary

Completed comprehensive exploration of the LeanRAG/Tier 3 memory system architecture in watercooler-cloud.

**Key Finding**: LeanRAG graphs are isolated from Graphiti using a database naming convention (`leanrag_*` prefix) within a shared FalkorDB instance. The system includes native FalkorDB graph schema with hierarchical support and uses entity names (not UUIDs) as identifiers.

---

## 1. LeanRAG Graph Structure in FalkorDB

### Graph Naming and Isolation Strategy

**Primary Strategy**: Database-level isolation via `leanrag_` prefix

- **Graphiti**: Database name derived from code path
  Example: `/home/jay/projects/watercooler-cloud` → `watercooler_cloud`

- **LeanRAG**: Prefixed with `leanrag_`
  Example: `leanrag_watercooler_cloud`

- **Explicit Override**: `WATERCOOLER_LEANRAG_DATABASE` env var overrides derived name

**Documentation Location**: `src/watercooler_mcp/memory.py:267-273`

This is **database-level isolation** (separate FalkorDB graphs in the same Redis instance), not namespace-based isolation within a single graph.

### Node Types and Schema

**Entity Nodes** (primary):
```cypher
(:Entity:Type {
  entity_name: str,           # Primary identifier (NOT UUID)
  description: str,            # Text summary
  source_id: str,              # Where extracted from
  degree: int,                 # Graph connectivity
  parent: str,                 # Hierarchical parent name
  level: int,                  # 0=base, 1+=clusters
  entity_type: str             # Semantic type
})
```

**Community Nodes** (aggregated):
```cypher
(:Community {
  entity_name: str,
  entity_description: str,
  findings: str                # JSON serialized
})
```

**Relationship Types**:
- **HAS_PARENT / HAS_CHILD**: Tree hierarchy
- **RELATES_TO**: Semantic relationships with metadata (description, weight, level, src_tgt, tgt_src)

### Hierarchical Structure

Multi-level organization:
- **Level 0**: Base entities (original extracted knowledge)
- **Level 1+**: Aggregated clusters (semantic communities)

Enables:
1. **Level-mode filtering in search** (base only / clusters only / all)
2. **Hierarchical path traversal** (entity → parent → grandparent → root)
3. **Cluster summarization** (knowledge distillation)

### FalkorDB Integration

**Key Functions**:
- `db.select_graph(graph_name)` - creates graph if not exists
- `CREATE VECTOR INDEX` - semantic similarity search
- `db.idx.vector.queryNodes()` - vector search API
- Native Cypher for traversal

**Schema Creation**: `external/LeanRAG/leanrag/database/falkordb.py`
- Indexes on entity_name, level, parent
- Batch operations via UNWIND (1000 items/batch)
- Entity type-specific labels (Technology, Person, Event, etc.)

---

## 2. LeanRAG Backend Implementation

### Architecture Overview

**File**: `src/watercooler_memory/backends/leanrag.py` (1600+ lines)

**Main Class**: `LeanRAGBackend` implementing `MemoryBackend` contract

**Core Components**:

1. **LeanRAGConfig**:
   - LeanRAG submodule path (None if installed as pip package)
   - FalkorDB settings (host, port, password)
   - LLM config (API key, base URL, model)
   - Embedding config (API base, model)
   - Work directory
   - Max workers for parallelization
   - Test mode (pytest__ prefix for isolation)

2. **Import Context Manager** (`_leanrag_import_context`):
   - Thread-safe directory changes (uses `_chdir_lock` RLock)
   - Persists sys.path entries (Python caches modules)
   - Handles config.yaml loading at import time

3. **Installation Detection** (`_is_leanrag_installed()`):
   - Checks if in site-packages (installed package)
   - Falls back to submodule from `external/LeanRAG`
   - Cached result in `_LEANRAG_INSTALLED_AS_PACKAGE`

### Pipeline: Prepare → Index → Query

**Stage 1: Prepare** (`prepare()`):
- Maps canonical payload to LeanRAG JSON format
- Creates documents.json, threads.json, threads_chunk.json
- Generates manifest with chunker metadata

**Stage 2: Index** (`index()`):
- Triple extraction via LLM (entity/relation/gleaning)
- Hierarchical graph building via `build_hierarchical_graph()` (native)
- Semantic clustering (GMM + UMAP)
- Supports progress callbacks
- Returns cluster count and build duration

**Stage 3: Incremental Index** (`incremental_index()`):
- Reuses saved cluster state (`.cluster_state/`)
- Assigns new entities to existing clusters
- Falls back to full index if no saved state
- Guards against degenerate UMAP (requires >= 5 chunks)

**Stage 4: Query** (`query()`):
- Direct `query_graph()` invocation with embeddings
- Supports topk and level modes
- Returns answer + context

### Search Methods

**search_nodes()**: Vector similarity on entity embeddings
- Uses `search_vector_search()` from LeanRAG
- Supports level_mode filtering (0=base, 1=clusters, 2=all)
- Returns normalized CoreResult with parent field

**search_facts()**: Entity search + hierarchical edge traversal
- Implements LeanRAG's reasoning chain
- Strategy: Find entities → Get hierarchical paths → Search relationships
- Performance caps: max 10 paths, prevents combinatorial explosion
- **CRITICAL**: Sorts facts by score BEFORE truncating

**search_episodes()**: Intentionally unsupported
- LeanRAG chunks lack provenance (who/when)
- Raises UnsupportedOperationError

### Node and Edge Retrieval

**get_node()**: Entity lookup by name
- Validates node_id is entity name (not UUID)
- Queries at ANY level (not just level 0)
- Returns parent, degree, level metadata

**get_edge()**: Relationship lookup via synthetic ID
- Format: `SOURCE||TARGET`
- Uses `search_nodes_link()` to find relationship
- Preserves original directionality

### Configuration Priority

**Unified System**:
1. Environment variables (LLM_*, EMBEDDING_*, FALKORDB_*, DEEPSEEK_*, GLM_*)
2. Backend-specific TOML: `[memory.leanrag]`
3. Shared TOML: `[memory.llm]`, `[memory.embedding]`, `[memory.database]`
4. Built-in defaults

**Variable Bridging** (`_apply_config_to_env()`):
- Maps standard watercooler vars to LeanRAG equivalents:
  - `LLM_API_KEY` → `DEEPSEEK_API_KEY`
  - `LLM_MODEL` → `DEEPSEEK_MODEL`
  - `EMBEDDING_MODEL` → `GLM_MODEL`

### Thread Safety

**Critical Section**: os.chdir()
- Protected by `_chdir_lock` (threading.RLock())
- All LeanRAG imports within lock context
- Prevents race conditions in MCP server
- sys.path entries persist (connection reuse)

---

## 3. External LeanRAG Submodule

### Directory Structure

```
external/LeanRAG/
├── leanrag/
│   ├── database/
│   │   ├── falkordb.py          # FalkorDB integration
│   │   ├── adapter.py           # search_nodes_link, find_tree_root
│   │   ├── vector.py            # search_vector_search
│   │   └── ... (milvus, mysql alternatives)
│   ├── extraction/
│   │   └── chunk.py             # triple_extraction()
│   ├── pipelines/
│   │   ├── build_native.py      # build_hierarchical_graph()
│   │   ├── incremental.py       # incremental_update()
│   │   └── query.py             # query_graph()
│   ├── clustering/
│   │   └── state_manager.py     # StateManager
│   ├── core/
│   │   ├── llm.py               # LLM API (OpenAI-compatible)
│   │   └── config.py            # config.yaml loading
│   └── utils/
├── config.yaml                  # LeanRAG config (loaded at import time)
└── requirements.txt
```

### Key Functions

**Entity Extraction**:
```python
from leanrag.extraction.chunk import triple_extraction
await triple_extraction(chunks_dict, llm_func, work_dir, save_filtered=False)
# Outputs: entity.jsonl, relation.jsonl, gleaning.jsonl
```

**Graph Building**:
```python
from leanrag.pipelines.build_native import build_hierarchical_graph
result = build_hierarchical_graph(
    working_dir=str(work_dir),
    max_workers=8,
    fresh_start=False,
    progress_callback=callback
)
# Returns BuildResult with cluster count, entity count, duration
```

**Incremental Updates**:
```python
from leanrag.pipelines.incremental import incremental_update
result = incremental_update(
    working_dir=str(work_dir),
    new_entity_embeddings=embeddings,
    new_entity_metadata=entities_meta,
    state_dir=state_dir,
    llm_func=generate_text
)
```

**Graph Queries**:
```python
from leanrag.pipelines.query import query_graph
context, answer = query_graph(global_config, None, query_text)
```

**Graph Operations**:
```python
from leanrag.database.adapter import search_nodes_link, find_tree_root

# Find relationship between entities
link = search_nodes_link(entity1, entity2, working_dir, level=None)
# Returns (src_tgt, tgt_src, description, weight, level)

# Get hierarchical path to root
path = find_tree_root(working_dir, entity)
# Returns [entity, parent, grandparent, ..., root]
```

**Vector Search**:
```python
from leanrag.database.vector import search_vector_search
results = search_vector_search(
    work_dir,
    query_embedding,
    topk=10,
    level_mode=2  # 0=base, 1=clusters, 2=all
)
# Returns [(entity_name, parent, description, source_id), ...]
```

**LLM and Embeddings**:
```python
from leanrag.core.llm import (
    generate_text_async,      # async LLM (DEEPSEEK_*)
    generate_text,            # sync LLM
    embedding                 # embeddings (GLM_*, raw HTTP)
)
```

### Three-Phase Pipeline

1. **Entity/Relation Extraction**:
   - LLM extracts entities, relations, gleaning
   - Outputs: entity.jsonl, relation.jsonl, gleaning.jsonl

2. **Hierarchical Clustering**:
   - Semantic clustering via GMM + UMAP
   - Outputs: all_entities.json, generate_relations.json, community.json

3. **Graph Materialization**:
   - Loads data into FalkorDB
   - Creates Entity and Community nodes
   - Builds HAS_PARENT/HAS_CHILD and RELATES_TO edges

---

## 4. Graph Isolation Analysis

### Isolation Mechanism

**Database-Level Isolation**:
- Graphiti and LeanRAG use separate FalkorDB graphs
- Naming prevents collision: `watercooler_cloud` vs `leanrag_watercooler_cloud`
- Achieved via `os.path.basename(work_dir)` as graph name
- Env var `WATERCOOLER_LEANRAG_DATABASE` allows overrides

**Same FalkorDB Server**:
✅ Multiple databases coexist
✅ Different node types (Entity, Community, Entry)
✅ Shared connection pool via falkordb.py:get_falkordb_connection()

**No ID Collisions**:
- Graphiti: UUIDs (ULID format, 26 chars alphanumeric)
- LeanRAG: Entity names (strings like "OAUTH2")
- Entry store: ULID (same as Graphiti)
- Different labels prevent accidental traversal

### Coexistence Constraints

**Connection Management**:
- Global pool keyed by host:port
- Reuses connections per key
- Thread-safe for concurrent requests

**Data Namespace Separation**:
- FalkorDB select_graph() creates/selects by name
- No name collisions with explicit prefix
- Clean separation for cleanup and migration

---

## 5. Entity Mapping and Naming

### LeanRAG Entity Identification

**Key Pattern**: **Entity names are primary identifiers**, not UUIDs.

**Normalization** (`_normalize_entity_name()`):
```python
def _normalize_entity_name(name: str | None) -> str | None:
    if name is None:
        return None
    return name.strip().strip('"').strip()
```

**Why**: Milvus stores quoted names, FalkorDB expects clean names.

Example: `'"OAUTH2"  '` → `'OAUTH2'`

**Consistency**: LeanRAG's falkordb.py applies identical normalization at line 161.

### Entity Metadata

Each entity carries hierarchical metadata:
```python
{
  'entity_name': str,         # Primary key
  'description': str,         # Text summary
  'source_id': str,          # Where extracted
  'degree': int,             # Connectivity
  'parent': str,             # Hierarchical parent
  'level': int,              # 0=base, 1+=clusters
  'entity_type': str         # Semantic type
}
```

### Cross-Tier Mapping Gap

**Current State**: Tier 2 (Graphiti) and Tier 3 (LeanRAG) operate independently.
- Different extraction algorithms
- Different node schemas
- No shared entity references

**Future Enhancement** (Issues #193-204):
- Entity deduplication across tiers
- Hierarchical path alignment
- Score combination strategies

---

## 6. Configuration System

### Unified Configuration

**Files**:
- `src/watercooler/memory_config.py` - Config resolution
- `src/watercooler_mcp/memory.py` - MCP loaders
- `src/watercooler/config_schema.py` - Pydantic schema
- `~/.watercooler/config.toml` - User config

### LeanRAG Configuration Resolution

**Priority Chain** (`load_leanrag_config()`):

1. **Global Memory Switch**:
   - `WATERCOOLER_MEMORY_DISABLED=1` (disables all)

2. **LeanRAG Enable Switch**:
   - `WATERCOOLER_LEANRAG_ENABLED=1` explicit enable
   - `memory.tiers.t3_enabled=true` TOML
   - `memory.backend="leanrag"` TOML
   - Defaults to disabled (opt-in)

3. **Path Resolution**:
   - `LEANRAG_PATH` env var
   - `memory.leanrag.path` TOML
   - Defaults to `external/LeanRAG`

4. **LLM/Embedding/Database**:
   - Calls `LeanRAGConfig.from_unified()`
   - Env var overrides (DEEPSEEK_*, GLM_*)
   - Falls back to shared TOML

5. **Database Name**:
   - `WATERCOOLER_LEANRAG_DATABASE` override
   - Derived with `leanrag_` prefix
   - Work dir: `~/.watercooler/{database_name}/`

### Example TOML

```toml
[memory]
enabled = true
backend = "leanrag"

[memory.database]
host = "localhost"
port = 6379

[memory.llm]
api_key = ""  # LLM_API_KEY or DEEPSEEK_API_KEY
api_base = "https://api.deepseek.com/v1"
model = "deepseek-chat"

[memory.embedding]
api_key = ""
api_base = "http://localhost:8000/v1"
model = "bge-m3"

[memory.leanrag]
path = "external/LeanRAG"
max_workers = 8

[memory.tiers]
t3_enabled = true  # Enable Tier 3 (LeanRAG)
```

---

## 7. Key Implementation Insights

### Performance Characteristics

**Indexing**:
- Prepare: Fast (JSON serialization)
- Triple extraction: ~5 LLM calls per chunk, ~10s per chunk
- Graph building: ~1-5 minutes for 100+ entities
- Incremental: Much faster (assignment only)

**Querying**:
- Vector search: O(log N) via HNSW
- Fact search: O(N²) worst case, capped at 10 paths max
- Topk filtering: Fast (vector index)

**Storage**:
- Hierarchical graphs: ~2x flat graphs
- Typical: 100 entities → 50 clusters across 3-4 levels
- ~46% redundancy reduction via summarization

### Thread Safety

**os.chdir Protection**:
- Uses `_chdir_lock` (threading.RLock())
- Only one thread changes directory at a time
- All LeanRAG imports within lock

**Connection Management**:
- FalkorDB client connection pooled by host:port
- Thread-safe connections
- Entry store uses background event loop

### Error Handling

**Configuration**:
- Missing LEANRAG_PATH: ConfigError with remediation
- Missing config.yaml: Explicit check and report
- Failed import: Returns None, not exception

**Indexing**:
- Triple extraction failure: Logged, continues
- Graph build failure: Raises BackendError
- Checkpoint corruption: Falls back to full index

**Querying**:
- Entity not found: Returns empty result
- Vector search failure: Returns empty list
- FalkorDB error: Raises TransientError for retry

---

## 8. Related Findings

### Pre-existing Test Failures

Not caused by recent changes:
- `test_sync_to_memory_backend_graphiti` - race condition in callback
- `test_load_config_missing_llm_api_key` - monkeypatch isolation
- `TestLeanRAGSmoke::test_prepare_index_query` - leanrag_path None

### Architectural Debt (PR #167)

Deferred refactoring:
- DocumentNode/DocumentChunk dual schemas
- Batch summarization (N+1 LLM calls)
- graphiti.py decomposition (god module)
- EntryNode.index consolidation

---

## 9. Next Steps for Federation Phase 2

**Issues #193-204** address cross-tier coordination:
- #193: Logging & observability
- #194: Dedup merge conflicts
- #195: search_graph thread-safety
- #196: E2E tests with git worktrees
- #197: Envelope field ordering
- #201: Federation cleanup bundle

**Key Gap**: No unified entity mapping between T2 and T3 yet.

---

## Summary of Key Findings

1. **Database Naming Isolation**: `leanrag_*` prefix separates LeanRAG from Graphiti in shared FalkorDB
2. **Entity Names as IDs**: LeanRAG uses entity names (not UUIDs) with normalization
3. **Hierarchical Graph**: Multi-level organization (base + clusters) enables efficient reasoning
4. **Thread-Safe Import**: Uses RLock for os.chdir() during LLM imports
5. **Unified Configuration**: Standard priority chain (env > backend-specific TOML > shared TOML > defaults)
6. **Incremental Support**: Can reuse cluster state for fast updates on new entities
7. **Performance Capping**: Limits entity paths and pairs in fact search to prevent explosion
8. **Cross-Tier Gap**: T2 and T3 operate independently (future federation enhancement needed)

