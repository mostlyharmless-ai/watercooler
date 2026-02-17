# Watercooler-Cloud Documentation

File-based collaboration protocol for agentic coding projects with CLI tools and AI agent integration.

---

## Quick Start (Choose Your Path)

### I want to use watercooler with AI agents (Claude, Codex)
**-> [Quickstart](QUICKSTART.md)** - Universal dev mode + first-call walkthrough
**-> [MCP Server Guide](mcp-server.md)** - Tool reference and parameters
**-> [Troubleshooting](TROUBLESHOOTING.md)** - MCP setup issues

**Why MCP?** AI agents automatically discover watercooler tools - no manual commands needed.

### I want to use watercooler CLI commands
**-> [Quickstart](QUICKSTART.md)** - Same universal guide (CLI applies the same rules)
**-> [CLI Reference](CLI_REFERENCE.md)** - Complete command-line interface documentation
**-> [Main README](../README.md)** - Installation and command reference

**When to use CLI:** Manual control, scripting, or when MCP isn't available.

### I want to integrate watercooler in my Python project
**-> [Architecture](ARCHITECTURE.md)** - Design principles and library structure
**-> [Structured Entries](STRUCTURED_ENTRIES.md)** - Entry format, roles, and types

---

## Core Concepts

### What is Watercooler?
**File-based collaboration protocol** for:
- **Thread-based discussions** with explicit ball ownership ("whose turn is it?")
- **Structured entries** with roles (planner, critic, implementer, tester, pm, scribe)
- **Multi-agent coordination** with automatic ball flipping
- **Git-friendly markdown** for versioning and async collaboration

### Key Features
- **Ball ownership** - Explicit "next action" tracking
- **Agent roles** - Specialized entry types for different tasks
- **Structured entries** - Metadata (timestamp, author, role, type, title)
- **Template system** - Customizable thread and entry formats
- **Advisory locking** - Safe concurrent access
- **MCP integration** - AI agents discover tools automatically
- **Cloud sync** - Git-based team collaboration

### When to Use Watercooler
**Great for:**
- AI agent collaboration (Claude, Codex working together)
- Extended context across LLM sessions
- Async team coordination across timezones
- Decision tracking and architectural records
- Handoff workflows (dev->reviewer, human->agent)

**Not ideal for:**
- Real-time chat
- Large group discussions (>5 participants)
- Ad-hoc brainstorming without structure

---

## Common Tasks

### Getting Started
- [Install watercooler](QUICKSTART.md#installation) - CLI or MCP setup
- [Create your first thread](QUICKSTART.md#creating-threads) - Initialize and add entries

### Multi-Agent Collaboration
- [Set up ball flipping](STRUCTURED_ENTRIES.md#ball-auto-flip) - Automatic handoffs

### Team Collaboration
- [Configure branch pairing](BRANCH_PAIRING.md) - Code + threads repo pairing
- [Understand the threads lifecycle](THREADS_REPO_LIFECYCLE.md) - Repo bootstrap and sync

### Customization
- [Configure environment variables](ENVIRONMENT_VARS.md) - WATERCOOLER_* vars
- [Configure TOML settings](CONFIGURATION.md) - `~/.watercooler/config.toml`

---

## Complete Documentation Index

### Getting Started
- **[Installation Guide](INSTALLATION.md)** - Complete setup for all platforms and MCP clients
- **[Quickstart](QUICKSTART.md)** - First-call walkthrough and setup
- **[Configuration](CONFIGURATION.md)** - TOML configuration reference
- **[Environment Variables](ENVIRONMENT_VARS.md)** - Complete environment variable reference
- **[FAQ](FAQ.md)** - Common questions and troubleshooting

### MCP Server (AI Agent Integration)
- **[MCP Server Guide](mcp-server.md)** - Tool reference and architecture
- **[ChatGPT MCP Integration](CHATGPT_MCP_INTEGRATION.md)** - ChatGPT-specific setup
- **[Troubleshooting](TROUBLESHOOTING.md)** - MCP setup issues and solutions

### Reference Documentation
- **[CLI Reference](CLI_REFERENCE.md)** - Complete command-line interface documentation
- **[Architecture](ARCHITECTURE.md)** - Design principles, features, and development guide
- **[Structured Entries](STRUCTURED_ENTRIES.md)** - Entry format, 6 roles, 5 types
- **[Authentication](AUTHENTICATION.md)** - GitHub OAuth, credential helpers, SSH setup
- **[Contributing](CONTRIBUTING.md)** - Contribution guidelines and workflow

### Git Sync & Branch Management
- **[Branch Pairing](BRANCH_PAIRING.md)** - Code/threads repo pairing contract
- **[Graph Sync](GRAPH_SYNC.md)** - Baseline graph synchronization
- **[Threads Repo Lifecycle](THREADS_REPO_LIFECYCLE.md)** - Repository bootstrap and states

### Advanced Topics
- **[HTTP Transport](http-transport.md)** - Local HTTP daemon setup
- **[Baseline Graph](baseline-graph.md)** - Memory pipeline and graph format
- **[Graph Visualization](visualization.md)** - Interactive force-directed graph visualization
- **[Decision Trace Extraction Guide](DECISION_TRACE_EXTRACTION_GUIDE.md)** - Extracting decision traces

### Subdirectories
- **[images/](images/)** - Documentation images

---

## Learning Path

### Beginner
1. Read [Quickstart](QUICKSTART.md) - Get basic understanding
2. Follow [First Thread Tutorial](QUICKSTART.md#creating-threads) - Hands-on practice
3. Set up [MCP Server](mcp-server.md) - Enable AI agent tools

### Intermediate
4. Configure [Structured Entries](STRUCTURED_ENTRIES.md) - Understand entry format
5. Set up [Branch Pairing](BRANCH_PAIRING.md) - Team collaboration
6. Explore [CLI Reference](CLI_REFERENCE.md) - Full command reference

### Advanced
7. Study [Architecture](ARCHITECTURE.md) - Design principles
8. Configure [Graph Sync](GRAPH_SYNC.md) - Baseline graph pipeline
9. Set up [HTTP Transport](http-transport.md) - Local HTTP daemon

---

## Quick Command Reference

```bash
# Thread Management
watercooler init-thread <topic>          # Create new thread
watercooler list [--open-only|--closed]  # List threads
watercooler search <query>                # Search threads

# Structured Entries
watercooler say <topic> --agent <name> --role <role> --title <title> --body <text>
watercooler ack <topic>                  # Acknowledge without ball flip
watercooler handoff <topic> --note <msg> # Explicit handoff

# Status & Ball
watercooler set-status <topic> <status>  # Update status (OPEN, IN_REVIEW, CLOSED)
watercooler set-ball <topic> <agent>     # Update ball owner

# Export & Index
watercooler reindex                      # Rebuild markdown index
watercooler web-export                   # Generate HTML index

# Debugging
watercooler unlock <topic> [--force]     # Clear stuck lock
```

For complete command reference, see [CLI Reference](CLI_REFERENCE.md) or [Main README](../README.md).

---

## Contributing to Documentation

Documentation improvements welcome! Please:
1. Follow existing structure and tone
2. Include practical examples
3. Cross-reference related guides
4. Add entries to this hub for new documents
5. Mark audience level: [Beginner] [Intermediate] [Advanced] [Reference]

---

## Support & Community

- **Repository**: https://github.com/mostlyharmless-ai/watercooler-cloud
- **Issues**: https://github.com/mostlyharmless-ai/watercooler-cloud/issues
- **Discussions**: Use watercooler threads in your project!
