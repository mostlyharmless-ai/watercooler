# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-04-13

First public open-core release.

### Added

- MCP server with 30+ tools for thread management, search, and graph operations
- CLI with core commands: `init-thread`, `say`, `ack`, `handoff`, `list`, `search`, `config`
- Baseline graph (T1) — JSON graph as sole source of truth for reads
- 6 local daemons for automated thread analysis and enrichment
- Unified search across threads with keyword and semantic modes
- Annotation system for tagging thread entries
- Graph enrichment and projection tools
- Multi-client support: Claude Code, Codex, Cursor
- `watercooler://instructions` MCP resource for agent workflow guidance
- Ball mechanics for accountability tracking across handoffs
- Structured entry types: Note, Plan, Decision, PR, Closure
- Role-based agent identity: planner, critic, implementer, tester, pm, scribe
- Git-native storage on orphan branch (`watercooler/threads`)
- DCO sign-off for contributions
- Apache-2.0 license with trademark policy

[0.4.0]: https://github.com/mostlyharmless-ai/watercooler/releases/tag/v0.4.0
