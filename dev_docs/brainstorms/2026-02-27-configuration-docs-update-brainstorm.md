---
date: 2026-02-27
topic: configuration-docs-update
---

# Configuration Documentation Update

## What We're Building

A targeted update to `docs/CONFIGURATION.md` to bring it into alignment with the actual
state of the codebase (`config_schema.py`, `config_loader.py`, `config.example.toml`).
The update also consolidates the environment variable reference (removing
`docs/ENVIRONMENT_VARS.md`), audits the schema for undocumented or stale fields, and
ensures all cross-referenced user docs agree with the configuration guide.

The goal is a *user-focused* reference — not an exhaustive schema dump — that gives a
typical user enough information to configure watercooler from scratch, while pointing
advanced users to `config.example.toml` for the full schema.

## Why This Approach

The schema has grown significantly (daemons, tier orchestration, federation, LeanRAG,
Graphiti chunking, caching) since CONFIGURATION.md was last updated. Rather than
documenting every field (risking doc bloat that drifts again quickly), we:

1. Cover all sections a real user touches during setup fully
2. Give advanced/internal sections a brief acknowledgment + pointer to the example config
3. Consolidate the env var reference into one authoritative place (CONFIGURATION.md)
   and delete the redundant ENVIRONMENT_VARS.md

## Key Decisions

- **User-focused scope**: Fully document `[common]`, `[mcp]` core, `[mcp.git]`,
  `[mcp.sync]`, `[mcp.logging]`, `[memory]`, `[memory.graphiti]`, `[memory.leanrag]`,
  `[memory.tiers]`, `[validation]`, `[federation]` basics, and credentials. Sections
  like `[mcp.http]`, `[mcp.cache]`, `[mcp.service_provision]`, `[mcp.daemons]`, and
  `[mcp.slack]` get brief mentions with a pointer to `config.example.toml`.

- **Delete ENVIRONMENT_VARS.md**: Consolidate the complete env var table into
  CONFIGURATION.md. Remove the standalone file to eliminate sync risk. Update any
  cross-references in README.md and other docs.

- **Docs + schema audit**: Alongside doc updates, produce a list of schema fields that
  have no env var coverage and flag fields that may be stale/unused. Code fixes only
  if obviously broken defaults are found (not a refactor).

## Scope of Work

### CONFIGURATION.md changes
1. Add missing section: `[memory.tiers]` — tier orchestration (t1/t2/t3 enable flags,
   limits, confidence thresholds)
2. Add missing section: `[memory.leanrag]` — LeanRAG path, LLM + embedding overrides,
   max_workers
3. Expand `[memory.graphiti]` — document chunking settings (chunk_on_sync,
   chunk_max_tokens, chunk_overlap, use_summary)
4. Add brief `[mcp.daemons]` mention — compound daemon, thread auditor (pointer to
   example config for full fields)
5. Add brief `[mcp.cache]` and `[mcp.http]` mentions
6. **Complete the env var table** — add ~20+ missing vars (Slack, graph, cache,
   federation, Graphiti-specific, embedding divergence threshold, auth mode)
7. Normalize the table format: columns = Env Var | TOML Path | Default | Description

### Cross-doc changes
- **ENVIRONMENT_VARS.md**: Replace content with a redirect to CONFIGURATION.md and
  delete the standalone table
- **README.md**: Update any link to ENVIRONMENT_VARS.md → CONFIGURATION.md#environment-variables
- **docs/QUICKSTART.md**: Verify configuration examples match current schema defaults
- **docs/mcp-server.md**: Verify config examples referenced are still valid
- **docs/INSTALLATION.md**: Verify any config snippets are current

### Schema audit deliverable
A concise table (in the brainstorm or as a dev note) of:
- Schema fields with no env var coverage (potential oversight vs. intentionally internal)
- Config fields present in schema but absent from config.example.toml (potential dead weight)

## Files Touched

| File | Change |
|------|--------|
| `docs/CONFIGURATION.md` | Major update — add sections, complete env var table |
| `docs/ENVIRONMENT_VARS.md` | Delete (consolidate into CONFIGURATION.md) |
| `README.md` | Update link from ENVIRONMENT_VARS.md → CONFIGURATION.md anchor |
| `docs/QUICKSTART.md` | Verify / minor alignment |
| `docs/mcp-server.md` | Verify / minor alignment |
| `docs/INSTALLATION.md` | Verify / minor alignment |

No code changes expected unless schema audit reveals obviously broken defaults.

## Open Questions

*None — all resolved during brainstorm.*

## Resolved Questions

- **How comprehensive?** User-focused. Advanced sections get brief mention +
  pointer to config.example.toml.
- **ENVIRONMENT_VARS.md?** Delete it. CONFIGURATION.md owns the authoritative table.
- **Config issues?** Docs + schema audit. Flag stale/undocumented fields. Code changes
  only if obviously broken.

## Next Steps

→ `/workflows:plan` for implementation details
