# Watercooler-Cloud Documentation

**Status:** Production Ready | **Latest Version:** 0.0.1 | **Phases Complete:** 1A/1B/2A

File-based collaboration protocol for agentic coding projects with CLI tools and AI agent integration.

---

## 🚀 Quick Start (Choose Your Path)

> ℹ️ Looking for legacy appendices or CLI deep-dives? They now live under [`docs/archive/`](archive/).

### I want to use watercooler with AI agents (Claude, Codex)
**→ [Setup & Quickstart](SETUP_AND_QUICKSTART.md)** - Universal dev mode + first-call walkthrough
**→ [MCP Server Guide](mcp-server.md)** - Tool reference and parameters
**→ [Claude Code Setup](archive/CLAUDE_CODE_SETUP.md)** - Client-specific appendix
**→ [Claude Desktop Setup](archive/CLAUDE_DESKTOP_SETUP.md)** - Desktop appendix
**→ [Troubleshooting](TROUBLESHOOTING.md)** - MCP setup issues

**Why MCP?** AI agents automatically discover watercooler tools - no manual commands needed.

### I want to use watercooler CLI commands
**→ [Setup & Quickstart](SETUP_AND_QUICKSTART.md)** - Same universal guide (CLI applies the same rules)
**→ [Main README](../README.md)** - Installation and command reference
**→ [Claude Collaboration (CLI)](archive/claude-collab.md)** - Manual CLI workflows

**When to use CLI:** Manual control, scripting, or when MCP isn't available.

### I want to integrate watercooler in my Python project
**→ [Integration Guide](archive/integration.md)** - Library usage and configuration
**→ [API Reference](archive/integration.md#python-api-reference)** - Complete Python API documentation

---

## 📚 Core Concepts

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
- **Cloud sync** - Git-based team collaboration (Phase 2A)

### When to Use Watercooler
**✅ Great for:**
- AI agent collaboration (Claude, Codex working together)
- Extended context across LLM sessions
- Async team coordination across timezones
- Decision tracking and architectural records
- Handoff workflows (dev→reviewer, human→agent)

**❌ Not ideal for:**
- Real-time chat (but can [sync with Slack](SLACK_INTEGRATION.md) for hybrid workflows)
- Large group discussions (>5 participants)
- Ad-hoc brainstorming without structure

**→ See [Use Cases Guide](archive/USE_CASES.md)** for detailed examples

---

## 🎯 Common Tasks

### Getting Started
- [Install watercooler](QUICKSTART.md#installation) - CLI or MCP setup
- [Create your first thread](QUICKSTART.md#creating-threads) - Initialize and add entries
- [Set up for AI agents](archive/CLAUDE_CODE_SETUP.md) - MCP configuration

### Multi-Agent Collaboration
- [Configure agent registry](archive/AGENT_REGISTRY.md) - Define agents and counterparts
- [Set up ball flipping](STRUCTURED_ENTRIES.md#ball-auto-flip) - Automatic handoffs
- [Multi-agent use case](archive/USE_CASES.md#multi-agent-collaboration) - Complete example

### Team Collaboration
- [Enable cloud sync](.mothballed/docs/CLOUD_SYNC_STRATEGY.md) - Git-based team mode (Phase 2A)
- [Configure git merge strategy](../.github/WATERCOOLER_SETUP.md) - Required setup
- [Async collaboration use case](archive/USE_CASES.md#async-team-collaboration) - Cross-timezone example

### Slack Integration
- [Slack Integration Guide](SLACK_INTEGRATION.md) - Bidirectional sync with Slack
- Set up channels per repository (`#wc-<repo-name>`)
- Entries sync as threaded replies
- Reply in Slack to create watercooler entries

### Customization
- [Customize templates](archive/TEMPLATES.md) - Thread and entry formatting
- [Configure environment variables](archive/integration.md#environment-variables) - WATERCOOLER_* vars
- [Set up pre-commit hooks](../.github/WATERCOOLER_SETUP.md) - Enforce append-only

---

## 📖 Complete Documentation Index

### Getting Started
- **[Installation Guide](INSTALLATION.md)** - Complete setup for all platforms and MCP clients
- **[Main README](../README.md)** - Project overview and quick start
- **[FAQ](FAQ.md)** - Common questions and troubleshooting

### MCP Server (AI Agent Integration)
- **[MCP Server Guide](mcp-server.md)** - Tool reference and architecture
- **[Claude Code Setup](archive/CLAUDE_CODE_SETUP.md)** - Register with Claude Code CLI
- **[Claude Desktop Setup](archive/CLAUDE_DESKTOP_SETUP.md)** - Register with Claude Desktop app
- **[Troubleshooting](TROUBLESHOOTING.md)** - MCP setup issues and solutions
- **[Claude Collaboration (CLI)](archive/claude-collab.md)** - Manual CLI workflows

### Guides & Workflows
- **[Use Cases Guide](archive/USE_CASES.md)** ⭐ - 6 comprehensive practical examples:
  1. Multi-Agent Collaboration
  2. Extended Context for LLMs
  3. Handoff Workflows
  4. Async Team Collaboration
  5. Decision Tracking
  6. PR Review Workflow
- **[Integration Guide](archive/integration.md)** - Python library usage and configuration

### Reference Documentation
- **[CLI Reference](CLI_REFERENCE.md)** - Complete command-line interface documentation
- **[Architecture](ARCHITECTURE.md)** - Design principles, features, and development guide
- **[API Reference](archive/integration.md#python-api-reference)** - Complete Python library API
- **[Structured Entries](STRUCTURED_ENTRIES.md)** - Entry format, 6 roles, 5 types
- **[Agent Registry](archive/AGENT_REGISTRY.md)** - Agent configuration and counterparts
- **[Templates](archive/TEMPLATES.md)** - Template syntax and customization

### Integrations
- **[Slack Integration](SLACK_INTEGRATION.md)** - Bidirectional sync with Slack channels and threads

### Advanced Topics
- **[Cloud Sync Strategy](.mothballed/docs/CLOUD_SYNC_STRATEGY.md)** - Git-based cloud sync (Phase 2A)
- **[Git Configuration](../.github/WATERCOOLER_SETUP.md)** - Merge strategies and pre-commit hooks
- Historical docs (design overviews, rollout plans) are archived under `.mothballed/docs/`

### Migration & History
- **[Testing Results](TESTING_RESULTS.md)** - Phase 1A validation (historical)

---

## 🗺️ Project Status & Roadmap

See **[ROADMAP.md](ROADMAP.md)** for detailed phase status.

### Completed Phases ✅
- **Phase 1A (v0.1.0)** - MVP MCP server with 9 tools, multi-tenant support
- **Phase 1B (v0.2.0)** - Upward directory search, comprehensive docs, Python 3.10+
- **Phase 2A** - Git-based cloud sync with idempotency, retry logic, observability

### Current Status
- **Production ready** for local and git-based cloud sync
- 56 passing tests covering all features
- Full feature parity with acpmonkey

### Planned (Evaluate based on usage)
- JSON format support for MCP tools (deferred from Phase 1B)
- Pagination for large result sets (deferred from Phase 1B)
- Additional tools: search_threads, create_thread, list_updates (deferred from Phase 1B)
- Managed cloud deployment with OAuth (Phase 2B/3)

---

## 🎓 Learning Path

### Beginner
1. Read [QUICKSTART](QUICKSTART.md) - Get basic understanding
2. Follow [First Thread Tutorial](QUICKSTART.md#creating-threads) - Hands-on practice
3. Try [Claude Code Setup](archive/CLAUDE_CODE_SETUP.md) - Enable AI agent tools

### Intermediate
4. Explore [Use Cases Guide](archive/USE_CASES.md) - See real-world patterns
5. Configure [Agent Registry](archive/AGENT_REGISTRY.md) - Set up multi-agent workflows
6. Customize [Templates](archive/TEMPLATES.md) - Make threads your own

### Advanced
7. Enable [Cloud Sync](.mothballed/docs/CLOUD_SYNC_STRATEGY.md) - Team collaboration
8. Study [API Reference](archive/integration.md#python-api-reference) - Python library integration
9. Read [Architecture](.mothballed/docs/CLOUD_SYNC_STRATEGY.md) - Implementation deep-dive

---

## 🛠️ Quick Command Reference

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

For complete command reference, see [Main README](../README.md).

---

## 🤝 Contributing to Documentation

Documentation improvements welcome! Please:
1. Follow existing structure and tone
2. Include practical examples
3. Cross-reference related guides
4. Add entries to this hub for new documents
5. Mark audience level: [Beginner] [Intermediate] [Advanced] [Reference]

---

## 📞 Support & Community

- **Repository**: https://github.com/mostlyharmless-ai/watercooler-cloud
- **Issues**: https://github.com/mostlyharmless-ai/watercooler-cloud/issues
- **Discussions**: Use watercooler threads in your project!

---

*Last updated: 2025-11-03 | Documentation version: 1.0*
