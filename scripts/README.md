# Watercooler Scripts

Utility scripts for watercooler-cloud development and deployment.

## Baseline Graph Tools

The baseline graph is a lightweight knowledge graph built from threads using locally-hosted
LLMs (llama-server). These scripts provide maintenance and recovery operations.

### enrich_baseline_graph.py

Generate or regenerate summaries and embeddings for graph entries.

```bash
# Fill missing embeddings only (safe default)
./scripts/enrich_baseline_graph.py /path/to/threads --mode missing --embeddings

# Regenerate embeddings for specific topics
./scripts/enrich_baseline_graph.py /path/to/threads --mode selective --topics topic-a,topic-b --embeddings

# Full refresh of all embeddings (use with caution)
./scripts/enrich_baseline_graph.py /path/to/threads --mode all --embeddings

# Preview what would be processed
./scripts/enrich_baseline_graph.py /path/to/threads --mode all --embeddings --dry-run
```

Modes:
- `missing` - Only fill entries with missing values (default, safe)
- `selective` - Process only specified topics (force regenerate)
- `all` - Regenerate everything (global refresh)

### recover_baseline_graph.py

Rebuild graph from markdown files (emergency recovery).

```bash
# Recover only stale/error threads (auto-detected)
./scripts/recover_baseline_graph.py /path/to/threads --mode stale

# Recover specific topics
./scripts/recover_baseline_graph.py /path/to/threads --mode selective --topics topic-a,topic-b

# Full rebuild from all markdown (slow, destructive)
./scripts/recover_baseline_graph.py /path/to/threads --mode all

# Preview what would be recovered
./scripts/recover_baseline_graph.py /path/to/threads --mode all --dry-run
```

**WARNING**: In normal operation, the graph is the source of truth.
This tool is the exception for recovery scenarios.

### project_baseline_graph.py

Generate markdown files from graph data (source of truth).

```bash
# Create markdown for topics missing .md files (safe default)
./scripts/project_baseline_graph.py /path/to/threads --mode missing

# Project specific topics
./scripts/project_baseline_graph.py /path/to/threads --mode selective --topics topic-a,topic-b

# Full regeneration of all markdown
./scripts/project_baseline_graph.py /path/to/threads --mode all --overwrite

# Preview what would be created
./scripts/project_baseline_graph.py /path/to/threads --mode all --overwrite --dry-run
```

Use cases:
- Initial markdown generation after graph import
- Regenerating corrupted markdown files
- Syncing after direct graph edits

## Memory Graph

### build_memory_graph.py

Builds a memory graph from watercooler threads and exports to LeanRAG format.

```bash
# Basic usage - build graph from threads
python scripts/build_memory_graph.py /path/to/threads-repo

# Export to LeanRAG format
python scripts/build_memory_graph.py /path/to/threads-repo --export-leanrag ./output

# Save intermediate graph JSON
python scripts/build_memory_graph.py /path/to/threads-repo -o graph.json --export-leanrag ./output
```

Output:
- `documents.json` - Entries with chunks for LeanRAG processing
- `threads.json` - Thread metadata
- `manifest.json` - Export statistics

## MCP Server

### install-mcp.sh / install-mcp.ps1

Install the watercooler MCP server for AI coding assistants.

```bash
# Linux/macOS
./scripts/install-mcp.sh

# Windows (PowerShell)
.\scripts\install-mcp.ps1
```

Configures:
- Claude Code (`~/.claude/claude_desktop_config.json`)
- Codex CLI (`~/.codex/config.toml`)

### mcp-server-daemon.sh

Run the MCP server as a persistent daemon with HTTP transport.

```bash
./scripts/mcp-server-daemon.sh [host] [port]
# Default: 127.0.0.1:8080
```

## Git Integration

### git-credential-watercooler

Git credential helper for GitHub authentication via watercooler-site dashboard.

```bash
# Install as git credential helper
git config --global credential.helper /path/to/scripts/git-credential-watercooler

# Usage (automatic - git calls this when credentials needed)
echo "protocol=https\nhost=github.com" | ./scripts/git-credential-watercooler get
```

Retrieves GitHub tokens from the watercooler dashboard OAuth flow.
