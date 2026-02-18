# Frequently Asked Questions

Common questions about watercooler-cloud, based on real-world usage patterns.

## Table of Contents

- [General](#general)
- [Getting Started](#getting-started)
- [Architecture & Design](#architecture--design)
- [Daily Usage](#daily-usage)
- [Memory & Search](#memory--search)
- [Git & Collaboration](#git--collaboration)
- [Privacy & Security](#privacy--security)
- [Operations](#operations)
- [Troubleshooting Tips](#troubleshooting-tips)

---

## General

### What is watercooler-cloud?

A file-based collaboration protocol for agentic coding projects. It provides:

- **Threaded discussions** that live alongside your code in git
- **Ball ownership** tracking - always clear who has the next action
- **Structured entries** with roles (planner, critic, implementer) and types (Plan, Decision, Note)
- **Search** for recalling past decisions and context
- **Branch scoping** - entries auto-tagged with code branch for context

Think of it as "git-native project memory" - every decision, discussion, and handoff is versioned and searchable.

### How does this compare to Slack, GitHub Issues, or Linear?

| Aspect | Slack/Discord | GitHub Issues | Linear | Watercooler |
|--------|---------------|---------------|--------|-------------|
| **Versioned with code** | No | No | No | Yes |
| **Offline access** | No | Limited | No | Yes |
| **Structured roles** | No | No | No | Yes |
| **Ball ownership** | No | Assignees | Assignees | Explicit |
| **AI-agent native** | No | No | No | Yes |
| **Survives tool changes** | No | Vendor lock-in | Vendor lock-in | Plain files |

Watercooler threads travel with your code. When you `git clone`, you get the full discussion history. No separate tool login, no lost context when switching jobs, no vendor lock-in.

### Can I use this without AI agents? Is it human-only friendly?

Yes. Watercooler works for any team collaboration:

- **Human-to-human**: Developer hands off to reviewer, PM coordinates with team
- **Human-to-AI**: Delegate research to Claude, get structured response
- **AI-to-AI**: Planner agent designs, implementer agent builds, critic agent reviews
- **Mixed teams**: Any combination of the above

The protocol doesn't care who's writing entries - it just tracks who has the ball and what role they're playing.

### Do I need to run my own LLM or embedding server?

**For basic thread operations**: No. `say`, `ack`, `handoff`, `list`, `read` work with zero external dependencies.

**For enhanced features**:
- **Entry/thread summaries**: Requires LLM (local or API)
- **Semantic search**: Requires embedding model (local or API)

> Memory integration is available as an optional add-on for advanced search and recall features.

A typical local setup uses:
- `llama-server` with `qwen2.5:1.5b` for summaries
- `llama.cpp` with `bge-m3` for embeddings

See [INSTALLATION.md](INSTALLATION.md) for setup options.

---

## Getting Started

### What's the minimum I need to start using watercooler?

1. Configure your MCP client with the watercooler-cloud server
2. Start writing via MCP tools

That's it. The MCP server handles installation automatically via `uvx`, and the orphan branch and worktree are created on first write.

### How do I configure my MCP client?

Add watercooler-cloud to your MCP client's configuration file:

**Claude Code (`~/.claude.json`):**
```json
{
  "mcpServers": {
    "watercooler-cloud": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"]
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
      "args": ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"]
    }
  }
}
```

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.watercooler_cloud]
command = "uvx"
args = ["--from", "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable", "watercooler-mcp"]
```

The `uvx` command (from `uv`) automatically downloads and runs watercooler-mcp in an isolated environment. No manual installation required.

### Do I need to install anything manually?

**For agent usage**: No. The `uvx` command in the MCP config handles everything.

**For CLI usage or development**: Yes, use `pip install -e .[mcp]` from a clone of the repository.

### Where do threads live?

Threads live on an **orphan branch** (`watercooler/threads`) inside your code
repository. The MCP server accesses this branch via a git worktree at
`~/.watercooler/worktrees/<repo>/`.

There is no separate `-threads` repository. Everything stays in the same repo.

On first write, the server:
1. Detects your code repo from `code_path`
2. Creates the `watercooler/threads` orphan branch (if it doesn't exist)
3. Sets up a worktree at `~/.watercooler/worktrees/<repo>/`
4. Tags entries with `code_branch` metadata for branch scoping

```
~/.watercooler/worktrees/myproject/    # Worktree (orphan branch)
  ├── my-feature.md                    # Human-readable markdown
  └── .watercooler/
      ├── nodes.jsonl                  # Thread and entry nodes
      ├── edges.jsonl                  # Relationships
      ├── search-index.jsonl           # Embeddings for semantic search
      ├── manifest.jsonl               # Metadata manifest
      └── locks/                       # Topic locks
```

### How do I set my agent identity?

Pass your identity on each write call using the `agent_func` parameter:

```python
# Format: '<platform>:<model>:<role>'
watercooler_say(
    topic="my-feature",
    title="Starting implementation",
    body="...",
    code_path=".",
    agent_func="Claude Code:opus-4:implementer"
)
```

Include `Spec: <role>` as the first line of your entry body for self-documenting threads.

---

## Architecture & Design

### Why "graph-first" instead of just markdown files?

The JSONL graph is the source of truth; markdown is a projection for human readability.

**Benefits:**
- **Fast queries**: No parsing markdown to list threads or search
- **Embeddings**: Stored in graph for semantic search
- **Relationships**: Track cross-references between entries
- **Atomic updates**: Advisory locking prevents corruption
- **Enrichment**: Summaries added without modifying original entries

You can always read the `.md` files directly - they're real markdown. But writes go through the graph layer.

### How are threads scoped to my code branch?

Entries are tagged with **`code_branch` metadata** (auto-populated from the
current code branch). When you read a thread, only entries for the active
branch are shown by default:

```
Working on feature/auth  →  see entries tagged code_branch=feature/auth
Switch to main           →  see entries tagged code_branch=main
Pass code_branch="*"     →  see all entries across branches
```

No branch switching happens in the threads worktree — all branches share the
same orphan branch. Scoping is purely metadata-based.

### Why an orphan branch instead of a folder in my code repo?

- **Clean git history**: Thread commits never appear in code `git log` or trigger CI
- **Single repo**: No companion `-threads` repository to create, manage, or sync
- **Automatic setup**: Worktree and branch created on first write — zero config
- **No branch mirroring**: Entries tagged with `code_branch` instead of separate git branches

---

## Daily Usage

### When should I use `say` vs `ack` vs `handoff`?

| Command | Ball behavior | Use when |
|---------|---------------|----------|
| `say` | Flips to counterpart | Normal back-and-forth ("here's my update, your turn") |
| `ack` | Stays with you | Acknowledging without giving up ownership ("got it, still working") |
| `handoff` | Explicit target | Passing to specific person ("security team, please review") |

**Example workflow:**
```
Human (pm) → say → Claude (planner)     # "Design this feature"
Claude → say → Codex (implementer)      # "Here's the design, please build"
Codex → ack                              # "Building..." (keeps ball)
Codex → say → Claude (critic)           # "Done, please review"
Claude → say → Human (pm)               # "Approved, ready to merge"
```

### What roles should I use?

| Role | Purpose | Typical entries |
|------|---------|-----------------|
| `planner` | Architecture, design | Plan, Decision |
| `critic` | Review, quality | Decision, Note |
| `implementer` | Building, coding | Note, PR |
| `tester` | Validation | Note |
| `pm` | Coordination | Note, Closure |
| `scribe` | Documentation | Note |

Match role to what you're doing, not who you are. The same agent might be `planner` when designing and `critic` when reviewing.

### How do I close a thread properly?

Use a Closure entry with status change:

```python
watercooler_say(
    topic="feature-auth",
    title="Feature complete",
    body="Spec: pm\n\nMerged in PR #123. Deployed to production.",
    entry_type="Closure",
    role="pm",
    code_path=".",
    agent_func="Claude Code:opus-4:pm"
)
watercooler_set_status(topic="feature-auth", status="CLOSED", code_path=".")
```

Closed threads remain in listings by default. Use `open_only=True` to filter to open threads only.

---

## Memory & Search

### How do I find past discussions?

**Quick search:**
```python
watercooler_search(query="authentication decision", code_path=".")
```

**Smart query (with context):**
```python
watercooler_smart_query(
    query="What was decided about the caching strategy?",
    code_path="."
)
```

Smart query automatically searches across available memory tiers if initial results are insufficient.

### When do thread summaries get regenerated?

Thread summaries update automatically when:
1. **Entry count threshold**: First few entries (<=3), or every 5th entry
2. **Arc change detection**: When entry types shift (e.g., Plan -> Decision -> Closure)
3. **Embedding divergence**: When new entry's embedding diverges significantly from previous entry

To force-regenerate thread summaries:
```python
watercooler_graph_enrich(thread_summaries=True, mode="selective", topics="my-topic")
```

### How do I recall context before starting work?

Use the recall pattern:

```python
# Before implementing a feature
watercooler_smart_query(
    query="What decisions were made about user authentication?",
    code_path="."
)
```

This surfaces relevant past discussions, decisions, and context - avoiding re-litigation of settled questions.

---

## Git & Collaboration

### How do I handle merge conflicts in threads?

**Short answer**: You usually don't have to.

Watercooler uses **append-only entries** with **content-aware merge strategies**:

- **Thread files (.md)**: Entry-level merge by Entry-ID (ULID)
- **JSONL files**: Deduplicate by UUID
- **Manifest files**: Take newer timestamp, merge topics

When two people add entries concurrently:

```
Alice adds entry at 10:00 → pushes
Bob adds entry at 10:05 → pulls → auto-merge → pushes
```

Since each entry has a unique ULID, Git can merge both entries automatically.

**When conflicts can occur:**
- Editing thread header (status, ball) simultaneously
- Corrupted graph state

**If you do get a conflict:**
1. Check `graph/baseline/threads/<topic>/entries.jsonl` - it's line-based JSONL, usually auto-merges
2. For header conflicts, pick the more recent state
3. Run `watercooler_graph_recover()` if graph is corrupted

### What happens if two agents write at the same time?

**Advisory locking** prevents corruption:

1. Agent A acquires lock on `feature-auth`
2. Agent B tries to write -> waits (or fails after timeout)
3. Agent A completes -> releases lock
4. Agent B acquires lock -> writes

Locks are topic-specific and have TTL (default 30 seconds, configurable via `WCOOLER_LOCK_TTL`) to prevent deadlocks from crashed processes.

### Can I work offline?

Yes. Threads are local files in the worktree until you push:

1. Write entries offline (commits to orphan branch locally)
2. Push when back online (rebase+retry handles divergence)
3. JSONL append-only format minimizes conflicts

---

## Privacy & Security

### Where does my data go?

**Local mode (default)**: Everything stays on your machine and your git remote.

- Thread files: Your filesystem + your git remote (GitHub, GitLab, etc.)
- Embeddings: Local `graph/baseline/search-index.jsonl`
- LLM calls: Your configured endpoint (local llama-server or API)

**No data is sent to Anthropic, OpenAI, or watercooler servers** unless you explicitly configure an external API.

### What about sensitive information in threads?

Threads are **plain text files in git**. Apply the same judgment as code:

- Don't commit secrets, credentials, or PII
- Use `.gitignore` patterns if needed
- Consider access controls on threads repo
- For sensitive projects, use private repos

Threads support the same security model as your code - if your code repo is private, your threads repo can be too.

### Can I audit who wrote what?

Yes. Every entry includes:

- **Agent**: Who wrote it (`Claude Code (caleb)`)
- **Timestamp**: When (ISO 8601)
- **Entry-ID**: Unique identifier (ULID)

Commits include footers linking to code context:
```
Code-Repo: org/myproject
Code-Branch: feature/auth
Code-Commit: abc1234
Watercooler-Entry-ID: 01ARZ3NdgoZmqjDLLsrwNlM2S53
```

Full git history provides complete audit trail.

---

## Operations

### How much disk space do threads use?

Threads are lightweight text:

| Content | Typical size |
|---------|--------------|
| Single entry | 1-5 KB |
| Active thread (20 entries) | 20-100 KB |
| Embeddings per entry | ~4 KB (1024-dim float32) |
| Mature project (100 threads) | 10-50 MB |

The git history grows over time, but threads repos are much smaller than code repos (no binaries, no node_modules).

### How do I back up my threads?

Threads are in git - your backup strategy is your git remote:

- Push regularly (async coordinator does this automatically)
- Your git host (GitHub, GitLab) provides redundancy
- Clone to multiple machines if desired

### Can I migrate from another tool?

Watercooler uses plain text. To migrate:

1. **From Slack/Discord**: Export conversations, convert to thread entries
2. **From GitHub Issues**: Use GitHub API to extract, format as entries
3. **From Notion/Confluence**: Export markdown, structure as threads

There's no automated importer yet, but the format is simple enough for scripted migration.

---

## Troubleshooting Tips

### My agent identity shows wrong

**Symptom**: Entry header shows "Agent (user)" instead of "Claude Code (caleb)"

**Fix**: Pass `agent_func` on each write call:
```python
watercooler_say(
    ...,
    agent_func="Claude Code:opus-4:implementer"
)
```

The format is `<platform>:<model>:<role>`. Ensure you include all three parts separated by colons.

### Ball isn't flipping correctly

**Symptom**: Ball stays with same agent after `say`

**Check**:
1. Counterpart mapping configured? Check agent registry
2. Using `ack` instead of `say`? `ack` preserves ball
3. Explicit `--ball` override in command?

### Threads not syncing to remote

**Symptom**: Local changes not appearing on GitHub

**Check**:
1. Run `watercooler_health()` - check git and worktree status
2. Verify SSH keys / GitHub auth: `gh auth status`
3. Check server logs: `~/.watercooler/logs/`
4. Verify worktree exists: `ls ~/.watercooler/worktrees/<repo>/`

### Entry summaries not generating

**Symptom**: Entries have no summary field

**Check**:
1. LLM service available? Check `watercooler_health()`
2. Summaries enabled? `watercooler_graph_enrich(summaries=True)`
3. LLM endpoint configured? Check `LLM_API_BASE` or `[memory.llm].api_base` in config

---

## More Resources

- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** - Detailed problem-solving guide with flowcharts
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - System design and internals
- **[mcp-server.md](mcp-server.md)** - MCP tool reference
- **[QUICKSTART.md](QUICKSTART.md)** - Getting started guide
