# LeanRAG Backend Setup

This guide covers setting up LeanRAG as a memory backend for watercooler-cloud.

## Overview

**LeanRAG** is a Knowledge-Graph-Based RAG framework featuring semantic aggregation and hierarchical retrieval. As a watercooler memory backend, it provides:

- **Entity extraction** from thread content
- **Knowledge graph construction** with semantic clustering
- **Hierarchical retrieval** through graph layers
- **Reduced redundancy** (~46% lower than flat baselines)

**Backend type:** Entity extraction + Graph RAG
**Graph database:** FalkorDB (or Neo4j, MySQL)
**Vector database:** Milvus (optional, for embeddings)

---

## Version & License

**Branch:** `feature/falkordb-vector-backend`
**License:** Accepted by AAAI-26
**Repository:** https://github.com/mostlyharmless-ai/LeanRAG
**Submodule location (dev only):** `external/LeanRAG/`

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
docker run -p 6379:6379 falkordb/falkordb:latest
```

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

### 2. LeanRAG Dependencies

Install LeanRAG's Python dependencies:

```bash
cd external/LeanRAG
pip install -r requirements.txt
```

**Core dependencies:**
- `falkordb` - FalkorDB Python client
- `openai` - For LLM calls (optional, can use local)
- `numpy`, `scikit-learn` - Clustering
- `umap-learn` - Dimensionality reduction

---

## Configuration

### Environment Variables

Create a `.env.local` file (gitignored):

```bash
# LeanRAG Backend Configuration

# FalkorDB Connection
FALKORDB_HOST=localhost
FALKORDB_PORT=6379

# LLM for Entity Extraction (choose one)
## Option 1: DeepSeek API
DEEPSEEK_API_KEY=your_deepseek_api_key
LLM_API_BASE=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

## Option 2: Local LLM (llama-server)
# LLM_API_BASE=http://localhost:8000/v1
# LLM_MODEL=llama3.2:3b

# Embeddings (optional, for vector search)
EMBEDDING_API_BASE=http://localhost:8080/v1
EMBEDDING_MODEL=bge-m3

# Milvus (optional, for vector search)
MILVUS_HOST=localhost
MILVUS_PORT=19530
```

### LeanRAG Configuration File

LeanRAG uses `config.yaml` for pipeline configuration. Default location: `external/LeanRAG/config.yaml`

**Key settings:**
```yaml
# Graph database backend
database: falkordb  # or: neo4j, mysql

# Chunking
chunk_size: 1024
chunk_overlap: 100

# Entity extraction
extract_entities: true
extract_relations: true

# Clustering
clustering_method: GMM  # Gaussian Mixture Model
umap_dimensions: 5
```

**Precedence:** Environment variables override config.yaml values.

---

## Installation Steps

### Option A: Install as Package (Recommended for Users)

The simplest approach - LeanRAG is installed automatically as a dependency:

```bash
# Using uvx (self-contained, nothing to install locally)
uvx --from 'watercooler-cloud[leanrag]' watercooler-mcp

# Or using pip
pip install 'watercooler-cloud[leanrag]'

# Or install both LeanRAG and Graphiti backends
pip install 'watercooler-cloud[memory]'
```

### Option B: Development with Submodules (For Contributors)

For development where you need to edit LeanRAG code:

#### Step 1: Initialize Submodule

```bash
git clone --recurse-submodules https://github.com/mostlyharmless-ai/watercooler-cloud
# Or if already cloned:
git submodule update --init external/LeanRAG
```

#### Step 2: Install Dependencies

```bash
cd external/LeanRAG
pip install -r requirements.txt
# Or install as editable package
pip install -e .
```

### Step 3: Verify FalkorDB Connection

```bash
python -c "from falkordb import FalkorDB; db = FalkorDB(host='localhost', port=6379); print('Connected:', db.ping())"
```

Expected output: `Connected: True`

### Step 4: Test LeanRAG Pipeline

```bash
cd external/LeanRAG
python leanrag/pipelines/healthcheck.py
```

(Create `healthcheck.py` if it doesn't exist, or test with a simple import)

---

## Usage with Watercooler

### Export Threads to LeanRAG Format

```python
from watercooler_memory import MemoryGraph, export_to_leanrag
from pathlib import Path

# Build memory graph from threads
graph = MemoryGraph()
graph.build("/path/to/threads")

# Export to LeanRAG format
output_dir = Path("./leanrag-export")
manifest = export_to_leanrag(
    graph,
    output_dir=output_dir,
    include_embeddings=False,  # Optional: set True if using Milvus
    validate=True,
)

print(f"Exported {manifest['document_count']} documents")
print(f"Output: {output_dir}")
```

### Run LeanRAG Pipeline

```bash
cd external/LeanRAG

# Step 1: Entity extraction
python leanrag/pipelines/process.py \
  --input ../leanrag-export/chunks.json \
  --output ./output

# Step 2: Build knowledge graph
python leanrag/pipelines/build.py \
  --input ./output/entity.jsonl \
  --database falkordb

# Step 3: Query
python leanrag/pipelines/query.py \
  --query "What are the main themes in the threads?" \
  --database falkordb
```

---

## Optional: Milvus Vector Search

For enhanced semantic search, install Milvus:

### Install Milvus (Docker)

```bash
# Download docker-compose.yml
wget https://github.com/milvus-io/milvus/releases/download/v2.3.0/milvus-standalone-docker-compose.yml -O docker-compose.yml

# Start Milvus
docker-compose up -d
```

Verify:
```bash
curl http://localhost:19530/healthz
```

### Configure Watercooler to Use Milvus

In `.env.local`:
```bash
MILVUS_HOST=localhost
MILVUS_PORT=19530
```

Export with embeddings:
```python
manifest = export_to_leanrag(
    graph,
    output_dir=output_dir,
    include_embeddings=True,  # Enable embeddings
    validate=True,
)
```

---

## Optional: BGE-M3 Embeddings

For high-quality embeddings without external APIs:

### Start Local Embedding Server

```bash
# Install llama-cpp-python with server extras
pip install 'llama-cpp-python[server]>=0.2.50'

# Download BGE-M3 model (GGUF format)
# Place in: ~/.cache/lm-studio/models/bge-m3.gguf

# Start server
python -m watercooler_memory.embedding_server
```

Server runs at: `http://localhost:8080/v1`

### Verify Embedding Server

```bash
curl http://localhost:8080/v1/health
```

---

## Troubleshooting

### FalkorDB Connection Failed

**Error:** `ConnectionError: Could not connect to FalkorDB`

**Fix:**
```bash
# Check if FalkorDB is running
ps aux | grep falkordb-server

# Start FalkorDB if not running
falkordb-server &

# Verify with redis-cli
redis-cli ping
```

### Entity Extraction Fails

**Error:** `openai.AuthenticationError: Invalid API key`

**Fix:** Ensure `DEEPSEEK_API_KEY` is set in `.env.local`, or switch to local LLM:
```bash
# Use local llama-server instead
export LLM_API_BASE=http://localhost:8000/v1
export LLM_MODEL=llama3.2:3b
```

### Out of Memory During Clustering

**Error:** `MemoryError` during UMAP/GMM clustering

**Fix:** Reduce dimensions in `config.yaml`:
```yaml
umap_dimensions: 3  # Lower from default 5
max_clusters: 10     # Limit cluster count
```

---

## Architecture Notes

### How LeanRAG Integrates with Watercooler

1. **Watercooler** parses threads → builds MemoryGraph
2. **Export** stage converts graph to LeanRAG JSON format (`chunks.json`, `documents.json`)
3. **LeanRAG** reads JSON → extracts entities → builds knowledge graph in FalkorDB
4. **Adapter** (future) wraps LeanRAG behind MemoryBackend contract

**Current status:** Direct integration via export/import. Backend adapter layer coming in Phase 2.

### Data Flow

```
Watercooler Threads (.md)
    ↓ (parse, chunk)
MemoryGraph (watercooler_memory)
    ↓ (export_to_leanrag)
LeanRAG Format (chunks.json)
    ↓ (process.py, build.py)
FalkorDB Knowledge Graph
    ↓ (query.py)
RAG Retrieval Results
```

---

## Next Steps

1. **Test the pipeline** with a small set of threads (2-3 threads)
2. **Verify FalkorDB** has expected nodes/edges after build
3. **Try a query** to validate retrieval works
4. **Scale up** to full thread corpus once validated

For integration with the MemoryBackend contract, see `docs/adr/NNN-memory-backend-contract.md` (coming in Phase 2).

---

## See Also

- [Memory Module Documentation](MEMORY.md)
- [Graphiti Setup Guide](GRAPHITI_SETUP.md) (alternative backend)
- [LeanRAG Official Docs](https://github.com/mostlyharmless-ai/LeanRAG)
- [FalkorDB Documentation](https://docs.falkordb.com/)
