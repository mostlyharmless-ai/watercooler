# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Pre-existing poisoned summaries on structured entries are now retired**
  (#910). A structured entry skipped by the summarizer (`enrich_structured=False`)
  is never re-summarized, so a stale (pre-#902) stored summary would persist in the
  graph and re-sync to the memory backend. The enrichment executor now clears such
  summaries (`clear_entry_summary` sets `summary=""` and stamps the current schema
  version); the structured body is self-describing, so an empty summary is correct.
  Complements #902 (which prevents new fabrication and regenerates thread summaries).
- **Summarizer no longer fabricates OAuth2/JWT auth claims** (#902/#788). The
  baseline-graph summarizer's few-shot example was literally an OAuth2/JWT auth
  feature, which weak local models echoed into unrelated entries — producing
  summaries that invented authentication/JWT for code with none (dangerous for
  security-themed threads). Fix: a domain-neutral few-shot example, an explicit
  grounding clause in the entry/thread prompts, and a deterministic guard that
  detects a summary asserting an auth/credential mechanism absent from the source
  (negation-aware, so "no JWT" does not license a JWT claim), regenerating once
  then falling back to grounded extractive prose. `SUMMARY_SCHEMA_VERSION` bumped
  to 3 so v2 (poisoned-era) summaries are treated as stale and regenerated.

### Changed

- **Hosted repo-claim enforcement is now the code default** (Wave 6,
  2026-07-15). `repo_claim_mode()` defaults to `enforce` when
  `WATERCOOLER_REQUIRE_REPO_CLAIM` is unset: a hosted request whose token
  lacks a `repos` claim is rejected with 403 instead of logged-and-accepted.
  Ratified after the R4 telemetry window (7 days sustained-zero
  `repo_claim_absent`, thread `audit-transport-modes-hosted-db-2026-07:44`);
  Railway production has run enforce via env override since 2026-04-30, so
  this aligns the shipped default with production reality. Behavior change
  is confined to fresh installs and environments without the override —
  exactly the population that should fail closed. Set
  `WATERCOOLER_REQUIRE_REPO_CLAIM=warn` to opt down (rollout/debug escape
  hatch). Rejections surface actionably in proxy transport since #1120.

- **Write contract is now write-behind by default** (#906). With `async_sync`
  enabled (the default), a single-writer committer daemon owns commit+push
  (batched), and ordinary writes return under a new `[mcp.sync].default_confirm`
  level defaulting to `accepted`: the entry is durable in the append-only graph +
  the persisted commit queue before the tool returns, but **commit/push to origin
  is eventual** (typically bounded by `batch_window` + one push), not synchronous.
  A `say()` return no longer implies "visible on origin." Set
  `default_confirm = "committed"` (or `"pushed"`) to restore the blocking
  "returned == on origin" contract. `Decision`/`Closure` writes are always forced
  to `pushed`. See `docs/CONFIGURATION.md` → `[mcp.sync]` → "Write contract." No
  data is lost on crash — the queue is persisted and re-drained on restart.

### Added

- High-concurrency write path for the fan-out / agents-spawning-agents case
  (#906, addresses ops bugs #902–#905): async enrichment off the write lock
  (#903), a single-writer group-commit `CommitterDaemon` that removes the
  concurrent-commit data-loss race (#904) by serializing all worktree mutation
  through one writer (with the worktree lock as the backstop for the red-tier
  inline-commit fallback), and tiered commit-queue backpressure
  (`[mcp.sync].commit_queue_max_depth`).
- Committer reconciliation sweep (#907): the `CommitterDaemon` periodically flushes
  any worktree state with no pending task — committing uncommitted graph entries
  and pushing unpushed commits — closing the crash window between a write's graph
  append and its commit-task enqueue, and self-healing any dropped task.

## [0.5.3] - 2026-06-02

### Added

- `watercooler promote-candidate` — CLI + MCP tool that promotes a candidate Note
  to a Decision with carried-forward provenance and an append-only
  `CandidateDisposition` Note (#859)
- `watercooler orchestration-metric` — CLI reporting candidate-emission,
  promotion, agent-authored-Decision ratio, and coordination-pattern counts over
  a window (#870)
- Inert authority-ladder provenance fields on entry nodes (`actor_class`,
  `decision_origin`, `source_entry_id`, `confidence`, `gate_results`, plus
  `authority_source`/`authority_basis` written on the human `promote-candidate`
  path) — optional, unenforced descriptive metadata, with a one-time
  `human_grandfathered` backfill of legacy Decision/Closure nodes (#868). The
  Phase 1a server-side *enforcement* engine (#860–#862, #870) was built ahead of
  its trigger and cut before release per standing Decision
  `01KSV7Z3P9NJJ2RYYQYAVEX26X`; its design is preserved as a triggered
  contingency in the master-plan Appendix C. No authority field is enforced.
- TypeScript schema parity for the (inert) authority provenance fields in the
  watercooler-site bindings (cross-repo `lib/authorityFields.ts` + parity test),
  completing the third binding (Python ↔ JSON Schema ↔ TypeScript) of the Phase-4a
  parity acceptance criterion

### Changed

- Decision extractor routes weak-quote rejections (`hallucinated_quote`,
  `summary_only_quote_evidence`) to thread-visible candidate Notes instead of
  dropping them to private findings (#858)

### Internal / build

- Deterministic public-test-leak lint (#856)

[0.5.3]: https://github.com/mostlyharmless-ai/watercooler/releases/tag/v0.5.3

## [0.5.2] - 2026-05-28

### Fixed

- Copybara: exclude 10 test files unmasked by the v0.5.1 public sync

[0.5.2]: https://github.com/mostlyharmless-ai/watercooler/releases/tag/v0.5.2

## [0.5.1] - 2026-05-28

### Added

- `watercooler_write` gains an explicit `title` parameter with smarter
  auto-derivation and an advisory warning suffix (#845)

### Fixed

- Scrub CR/LF from explicit `watercooler_write` titles to prevent header-line
  forgery (#846 review)
- Copybara: exclude convergence-telemetry e2e tests from public sync

[0.5.1]: https://github.com/mostlyharmless-ai/watercooler/releases/tag/v0.5.1

## [0.5.0] - 2026-05-27

### Added

- Workflow-topology layer: stop-naturally affordances, the `watercooler_write`
  v1 canonical write path, and the default workflow skill (#803)
- Authority ladder Phases 1b/1c/1d/5a: candidate-Note fallback for soft-gate
  extraction failures (#800); elicitation options, Mem0 boundary, critic
  cost-accounting, and active-disagreement conventions (#801); convergence
  telemetry in `PulseSnapshotDaemon` (#802); Stop hook surfacing candidate Notes
  (#811); `watercooler setup-stop-hook` CLI (#812)
- In-thread keyword filter on `list_thread_entries` (#826)
- T1-only baseline-graph integrity checks for sync-status (#808)
- Per-repo daemon PID lock + sibling-fleet visibility (#791); structural
  local-mode registration errors (#789)

### Changed

- Agent-context and skill-surface refactor — CLAUDE.md/AGENTS.md slimming,
  tool-alias manifest, skill refresh (#828–#832)

### Fixed

- sync-repair: detect/repair an orphan branch with no remote (#809); preserve
  local-only commits instead of discarding (#804)
- Hosted atomic single-commit writes via the Git Trees API (#775, #781);
  URL-encode Contents API path segments (#772)

[0.5.0]: https://github.com/mostlyharmless-ai/watercooler/releases/tag/v0.5.0

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
