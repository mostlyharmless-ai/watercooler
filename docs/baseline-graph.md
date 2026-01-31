# Baseline Graph Module

**Baseline graph JSONL (canonical representation) for Watercooler collaboration.**

The baseline graph module maintains Watercooler’s canonical, git-friendly graph representation in JSONL.
Markdown thread files remain available as a **human projection**, but the baseline graph JSONL is the
authoritative record going forward.

## Overview

### What It Does

- Maintains a canonical graph representation of threads and entries (JSONL)
- Optionally parses markdown thread projections (for backfill / legacy import)
- Generates summaries for threads and entries (LLM or extractive)
- Exports to JSONL format suitable for graph databases or visualization
- Extracts cross-references (file paths, PR numbers, commit SHAs)

### Use Cases

- Building knowledge graphs from conversation history
- Powering the Watercooler Dashboard graph view
- Analyzing thread relationships and patterns
- Creating searchable indexes of past discussions

### Dependencies

The baseline graph module has two modes with different requirements:

| Mode | Dependencies | Use Case |
|------|--------------|----------|
| **Extractive** | None (stdlib only) | Fast, offline summarization |
| **LLM** | `httpx` | Higher quality summaries via local LLM |

**Extractive mode** works out of the box with no additional dependencies. It extracts key sentences and headers from text.

**LLM mode** requires `httpx` for HTTP communication with the LLM API:

```bash
pip install httpx
# or
pip install watercooler-cloud[baseline]
```

If `httpx` is not installed and LLM mode is configured, the module automatically falls back to extractive mode with a warning.

## Quick Start

### Basic Export (No LLM Required)

```python
from pathlib import Path
from watercooler.baseline_graph import export_all_threads, SummarizerConfig

# Use extractive summarization (no LLM needed)
config = SummarizerConfig(prefer_extractive=True)

# Export all threads to JSONL
manifest = export_all_threads(
    threads_dir=Path("./my-project-threads"),
    output_dir=Path("./graph-output"),
    config=config,
)

print(f"Exported {manifest['threads_exported']} threads")
print(f"Generated {manifest['nodes_written']} nodes, {manifest['edges_written']} edges")
```

### With Local LLM (llama-server)

```python
from watercooler.baseline_graph import export_all_threads, SummarizerConfig

# Configure for local llama-server instance
config = SummarizerConfig(
    api_base="http://localhost:8000/v1",
    model="llama3.2:3b",
    timeout=30.0,
)

manifest = export_all_threads(
    threads_dir=Path("./threads"),
    output_dir=Path("./graph"),
    config=config,
)
```

## Configuration

### SummarizerConfig Options

| Option | Default | Description |
|--------|---------|-------------|
| `api_base` | `http://localhost:8000/v1` | OpenAI-compatible API endpoint |
| `model` | `llama3.2:3b` | Model name for LLM summarization |
| `api_key` | `local` | API key (local llama-server doesn't require one) |
| `timeout` | `30.0` | Request timeout in seconds |
| `max_tokens` | `256` | Maximum tokens for LLM response |
| `extractive_max_chars` | `200` | Max chars for extractive summaries |
| `include_headers` | `True` | Include markdown headers in extractive summary |
| `max_headers` | `3` | Max headers to include |
| `max_thread_entries` | `10` | Max entries to include in thread summaries |
| `prefer_extractive` | `False` | Force extractive mode (skip LLM) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BASELINE_GRAPH_API_BASE` | `http://localhost:8000/v1` | LLM API endpoint |
| `BASELINE_GRAPH_MODEL` | `llama3.2:3b` | LLM model name |
| `BASELINE_GRAPH_API_KEY` | `local` | API key for LLM |
| `BASELINE_GRAPH_TIMEOUT` | `30.0` | Request timeout (seconds) |
| `BASELINE_GRAPH_MAX_TOKENS` | `256` | Max response tokens |
| `BASELINE_GRAPH_EXTRACTIVE_ONLY` | `false` | Force extractive mode (`1`, `true`, `yes`) |

### Config File (`config.toml`)

```toml
[baseline_graph]
prefer_extractive = false

[baseline_graph.llm]
api_base = "http://localhost:8000/v1"
model = "llama3.2:3b"
api_key = "local"
timeout = 30.0
max_tokens = 256

[baseline_graph.extractive]
max_chars = 200
include_headers = true
max_headers = 3
```

### Configuration Precedence

1. Environment variables (highest priority)
2. `config.toml` `[baseline_graph]` section
3. Built-in defaults (lowest priority)

## Output Format

### Directory Structure

```
graph-output/
├── nodes.jsonl      # Graph nodes (threads + entries)
├── edges.jsonl      # Graph edges (relationships)
└── manifest.json    # Export metadata
```

### Node Types

**Thread Node:**
```json
{
  "id": "thread:feature-auth",
  "type": "thread",
  "topic": "feature-auth",
  "title": "Authentication Refactor",
  "status": "OPEN",
  "ball": "Claude",
  "last_updated": "2024-01-15",
  "summary": "Discussion about refactoring auth system...",
  "entry_count": 5
}
```

**Entry Node:**
```json
{
  "id": "entry:feature-auth:1",
  "type": "entry",
  "entry_id": "feature-auth:1",
  "thread_topic": "feature-auth",
  "index": 1,
  "agent": "Claude",
  "role": "implementer",
  "entry_type": "Note",
  "title": "Initial analysis",
  "timestamp": "2024-01-15T10:00:00Z",
  "summary": "Analyzed current auth implementation...",
  "file_refs": ["src/auth.py", "tests/test_auth.py"],
  "pr_refs": [42],
  "commit_refs": ["abc1234"]
}
```

### Edge Types

| Type | Source | Target | Description |
|------|--------|--------|-------------|
| `contains` | Thread | Entry | Thread contains this entry |
| `followed_by` | Entry | Entry | Sequential entry relationship |

### Manifest

```json
{
  "version": "1.0",
  "generated_at": "2024-01-15T12:00:00Z",
  "source_dir": "/path/to/threads",
  "threads_exported": 10,
  "entries_exported": 45,
  "nodes_written": 55,
  "edges_written": 80,
  "files": {
    "nodes": "nodes.jsonl",
    "edges": "edges.jsonl"
  }
}
```

## API Reference

### Summarization

```python
from watercooler.baseline_graph import (
    summarize_entry,
    summarize_thread,
    extractive_summary,
    SummarizerConfig,
    create_summarizer_config,
)

# Create config from environment and config.toml
config = create_summarizer_config()

# Summarize a single entry
summary = summarize_entry(
    entry_body="Full entry content...",
    entry_title="Entry Title",
    entry_type="Note",
    config=config,
)

# Summarize entire thread from entries
entries = [
    {"body": "Entry 1 content", "title": "Title 1", "type": "Note"},
    {"body": "Entry 2 content", "title": "Title 2", "type": "Decision"},
]
thread_summary = summarize_thread(entries, thread_title="My Thread", config=config)

# Pure extractive (no LLM)
extract = extractive_summary(
    text="# Header\nContent here...",
    max_chars=200,
    include_headers=True,
)
```

### Parsing

```python
from pathlib import Path
from watercooler.baseline_graph import (
    parse_thread_file,
    iter_threads,
    parse_all_threads,
    get_thread_stats,
    ParsedThread,
    ParsedEntry,
)

# Parse single thread
thread = parse_thread_file(Path("threads/feature-auth.md"))
print(f"Topic: {thread.topic}, Entries: {thread.entry_count}")

# Iterate all threads
for thread in iter_threads(Path("threads/"), skip_closed=True):
    print(f"{thread.topic}: {thread.summary}")

# Get statistics
stats = get_thread_stats(Path("threads/"))
print(f"Total: {stats['total_threads']} threads, {stats['total_entries']} entries")
```

### Export and Load

```python
from pathlib import Path
from watercooler.baseline_graph import (
    export_thread_graph,
    export_all_threads,
    load_nodes,
    load_edges,
    load_graph,
)

# Export single thread
nodes, edges = export_thread_graph(thread, Path("output/"))

# Export all threads
manifest = export_all_threads(
    threads_dir=Path("threads/"),
    output_dir=Path("graph/"),
    skip_closed=True,
)

# Load exported graph
nodes, edges = load_graph(Path("graph/"))
for node in nodes:
    if node["type"] == "thread":
        print(f"Thread: {node['topic']}")
```

## LLM Backend Options

### llama-server (Default)

llama-server auto-starts when configured for localhost endpoints. Models auto-download from HuggingFace on first use.

```toml
[baseline_graph.llm]
api_base = "http://localhost:8000/v1"
model = "llama3.2:3b"
```

To disable auto-provisioning:

```toml
[mcp.service_provision]
llama_server = false  # Don't auto-download llama-server binary
models = false        # Don't auto-download GGUF models
```

### Any OpenAI-Compatible API

```toml
[baseline_graph.llm]
api_base = "https://your-api.example.com/v1"
model = "your-model"
api_key = "your-api-key"
```

## Fallback Behavior

The module gracefully degrades when LLM is unavailable:

1. **LLM available:** Uses LLM for high-quality summaries
2. **LLM unavailable:** Falls back to extractive summarization
3. **httpx not installed:** Uses extractive mode (no HTTP dependency)

Extractive summaries include:
- First N characters of content (truncated at sentence boundaries)
- Markdown headers as topic indicators
- No external dependencies required

## Relationship to watercooler_memory

| Module | Purpose | Dependencies |
|--------|---------|--------------|
| `baseline_graph` | Free-tier JSONL export | Local LLM or none |
| `watercooler_memory` | Full RAG pipeline | Embeddings, vector DB |

Use `baseline_graph` when you need:
- Zero-cost graph generation
- Simple JSONL output
- No external API dependencies

Use `watercooler_memory` when you need:
- Semantic search capabilities
- Vector embeddings
- Advanced RAG features

## MCP Server Integration

When using the Watercooler MCP server, graph generation happens automatically on each thread entry write. Configure via `config.toml`:

```toml
[mcp.graph]
# Enable LLM summaries and embeddings for new entries
generate_summaries = true
generate_embeddings = true

# Check service availability before generation (default: true)
# When true, skips generation gracefully if services are unavailable
auto_detect_services = true

# Service endpoints
summarizer_api_base = "http://localhost:8000/v1"  # llama-server (LLM)
summarizer_model = "llama3.2:3b"
embedding_api_base = "http://localhost:8080/v1"   # llama-server (embeddings)
embedding_model = "bge-m3"
```

### Service Health Checking

The MCP server checks service availability before attempting generation:

1. **LLM service check**: Pings the `/models` endpoint with a 5-second timeout
2. **Embedding service check**: Similar check for embedding API
3. **Graceful degradation**: If services are unavailable, generation is skipped with a warning

Check service status via the health tool:

```
watercooler_health

# Output includes:
# Graph Services:
#   Summaries Enabled: True
#   LLM Service: available (http://localhost:8000/v1)
#   Embeddings Enabled: True
#   Embedding Service: unavailable (http://localhost:8080/v1)
#   Auto-Detect Services: True
```

### Auto-Start Services (Optional)

If you have `watercooler_memory` installed with `ServerManager`, you can enable auto-start:

```bash
# Enable auto-start via environment variable
export WATERCOOLER_AUTO_START_SERVICES=true

# Or in config.toml:
[mcp.graph]
auto_start_services = true
```

When enabled, the MCP server will attempt to start local LLM/embedding services if they're unavailable.

### Troubleshooting

**Problem: "LLM service unavailable" warning**

1. Run `watercooler_health` to check service status
2. Verify llama-server is running: `curl http://localhost:8000/v1/models`
3. Check auto-provisioning config if services fail to start

**Problem: "Embedding service unavailable" warning**

1. Run `watercooler_health` to check service status
2. Verify llama-server (embedding) is running: `curl http://localhost:8080/v1/embeddings`
3. Check auto-provisioning config if services fail to start

**Problem: Summaries/embeddings not generating**

1. Check config: `generate_summaries = true` and `generate_embeddings = true`
2. Run health check: `watercooler_health`
3. Enable debug logging: `WATERCOOLER_LOG_LEVEL=DEBUG`

## See Also

- [Configuration Guide](CONFIGURATION.md) - Config file reference
- [Environment Variables](ENVIRONMENT_VARS.md) - All environment variables
- [Memory Module](MEMORY.md) - Full RAG pipeline documentation
- [Graph Sync](GRAPH_SYNC.md) - Real-time graph synchronization
