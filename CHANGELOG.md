# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.2] - 2026-04-29

### Added

- ProjectCoordinatorDaemon Phase 2 + Phase 3 series — T2 analysis context in
  coordinator leads, pulse-report Signal 4, `t2_context` schema v2
  (`stalled` → `analysis_stalled`), stance provenance + trend wiring,
  `connect_role_complement` detector, expanded `aware_*` suppression
  (#593, #617, #618, #619, #621, #624, #627)
- `watercooler_acknowledge_finding` MCP tool for acknowledging coordinator
  findings (#605)

### Changed

- Open-core review remediation across docs (5 passes); post-v0.4.1 public
  docs audit aligned 16 files plus 3 code cleanups (#612, #616)

### Fixed

- Copybara: `multiline=True` on `core.replace` and `regex_groups` transforms
  so multi-line patterns survive the public sync (#598, #600)
- Windows: unified embedding-server spawn and refused startup on orphan ports
  (#611)
- Diagnostic honesty in `/health`, branch-discovery hint, GitHub-backed write
  guard, and stale-read signal (#613)

### Internal / build

- Promote main → staging → stable for v0.4.1 (#603, #604)
- Exclude orphan skill tests from public Copybara sync (#614)
- Skip `TestEnrichLeadsS3` on open-core builds (#615)

[0.4.2]: https://github.com/mostlyharmless-ai/watercooler/releases/tag/v0.4.2

## [0.4.1] - 2026-04-16

### Added

- ProjectCoordinatorDaemon v1B — follow-on `coordinator_lead` findings
  (Phase 1) plus `enrich=True` read-time overlay (Phase 1.5) (#584, #586)
- Hosted companion documentation set (`*_HOSTED.md` doc-pair convention)
  (#585, #588)

### Changed

- Refactored coordinator enrichment with a two-path accessor and
  `enrichment_stats` (#587)
- Improved dashboard guide with screenshots (#581)
- Refined public documentation split; tactical-docs cleanup pass (#591)

### Fixed

- Public release audit: broken links, missing extras, infra leaks (#592)
- Windows release hardening — QUICKSTART, enrichment defaults, platform
  fixes (#583)

### Internal / build

- Include `data/*.toml` in public `pyproject` package-data (#579)
- Exclude project-pulse capture hook from public Copybara publish (#590)
- Add `RELEASING.md`; remove redundant `dev_docs/CONTRIBUTING.md` (#594)

[0.4.1]: https://github.com/mostlyharmless-ai/watercooler/releases/tag/v0.4.1

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
