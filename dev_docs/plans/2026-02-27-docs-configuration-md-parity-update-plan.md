---
title: "docs: Update CONFIGURATION.md for Schema Parity and Env Var Consolidation"
type: docs
status: completed
date: 2026-02-27
brainstorm: dev_docs/brainstorms/2026-02-27-configuration-docs-update-brainstorm.md
---

# docs: Update CONFIGURATION.md for Schema Parity and Env Var Consolidation

## Overview

`docs/CONFIGURATION.md` has drifted from the actual config system (`config_schema.py`,
`config_loader.py`, `config.example.toml`). This plan captures the precise, verified
changes needed to bring docs into alignment with the codebase, consolidate the env var
reference, and deliver a schema audit of undocumented/stale fields.

All research validated against `main` as of 2026-02-27.

> **Note:** Some default values in this plan diverge from the final schema (e.g.,
> `prefer_extractive` true→false, `min_confidence` 0.7→0.5, `t1_limit` 20→10).
> `docs/CONFIGURATION.md` is authoritative — it was verified against `config_schema.py`
> during implementation.

## Problem Statement

1. **Stale docs**: CONFIGURATION.md documents `[baseline_graph.extractive.*]` fields
   (`max_chars`, `include_headers`, `max_headers`) that no longer exist in
   `config_schema.py`. These must be removed.

2. **Missing sections**: 5+ schema sections are completely absent from
   CONFIGURATION.md — including `[memory.tiers]`, `[mcp.graph]`, expanded
   `[memory.graphiti]`/`[memory.leanrag]` fields — all user-facing.

3. **Env var gap**: ~40+ env vars defined in `config_loader.py` appear in neither
   `CONFIGURATION.md` nor `ENVIRONMENT_VARS.md`.

4. **Redundant docs**: CONFIGURATION.md and ENVIRONMENT_VARS.md both have env var
   tables that are inconsistent with each other and with the loader code.

5. **Missing constraint docs**: Federation section omits that
   `namespace_timeout ≤ max_total_timeout` is enforced at startup by a Pydantic
   `model_validator` (added in PR #190).

## Proposed Solution

1. Remove stale `baseline_graph.extractive.*` fields from CONFIGURATION.md.
2. Add missing user-facing sections (mcp.graph, memory.tiers, memory.leanrag,
   memory.graphiti expansion, advanced-features brief).
3. Replace the env var table with a complete, normalized reference.
4. Delete `ENVIRONMENT_VARS.md`; update all 5 cross-references.
5. Verify/align QUICKSTART.md, mcp-server.md, INSTALLATION.md, README.md.
6. Schema audit findings captured in this plan for dev reference.

## Technical Considerations

- **Source of truth for schema**: `src/watercooler/config_schema.py`
- **Source of truth for env vars**: `src/watercooler/config_loader.py`
- **Canonical example**: `src/watercooler/templates/config.example.toml` (975 lines)
- **Credentials**: `src/watercooler/templates/credentials.example.toml` — separate doc section
- **Federation constraint**: `namespace_timeout ≤ max_total_timeout` enforced by
  Pydantic `model_validator(mode="after")` — must be documented
- **Default mismatch**: `mcp.graph.generate_summaries/generate_embeddings` default to
  `false` in schema, `true` in example config — document schema defaults, note that
  example shows recommended production values
- **No code changes** expected unless schema audit reveals obviously broken defaults

## Acceptance Criteria

- [x] `baseline_graph.extractive.*` fields removed from CONFIGURATION.md
- [x] `[mcp.graph]` section added and documented fully
- [x] `[memory.tiers]` section added and documented fully
- [x] `[memory.graphiti]` expanded with chunking + episode tracking fields
- [x] `[memory.leanrag]` section added and documented fully
- [x] Federation section documents `namespace_timeout ≤ max_total_timeout` constraint
- [x] Advanced-features section added (mcp.daemons, mcp.cache, mcp.http, mcp.slack)
  with brief description + pointer to `config.example.toml`
- [x] Env var table in CONFIGURATION.md is complete and normalized (4 columns)
- [x] All env vars from `config_loader.py` appear in the table
- [x] `ENVIRONMENT_VARS.md` deleted
- [x] `README.md` updated: `docs/ENVIRONMENT_VARS.md` → `docs/CONFIGURATION.md#environment-variables-reference`
- [x] `docs/INSTALLATION.md` links updated (×2): same redirect
- [x] `CONFIGURATION.md` "See Also" self-reference to ENVIRONMENT_VARS.md removed
- [x] `grep -r "ENVIRONMENT_VARS.md" docs/ README.md src/watercooler/` returns zero results
- [x] QUICKSTART.md config examples verified (clean — no stale snippets)
- [x] mcp-server.md config examples verified + cross-reference pointer added
- [x] TROUBLESHOOTING.md, AUTHENTICATION.md links updated
- [x] `src/watercooler/credentials.py` deprecation messages updated
- [x] `src/watercooler/templates/config.example.toml` comment updated

---

## Implementation Plan

### Phase 1: Remove Stale Content from CONFIGURATION.md

**File**: `docs/CONFIGURATION.md`

Remove the `[baseline_graph.extractive]` subsection. The following fields are documented
but do NOT exist in `config_schema.py` (verified against current `main`):

```toml
# REMOVE — not in schema:
[baseline_graph.extractive]
max_chars = 200         # ~line 222
include_headers = true  # ~line 223
max_headers = 3         # ~line 224
```

Also audit the rest of the `[baseline_graph]` section in CONFIGURATION.md. The current
schema puts `generate_summaries` and `generate_embeddings` under `[mcp.graph]`, not
`[baseline_graph]`. If these appear under `[baseline_graph]` in the docs, they are also
misplaced and should be removed (they'll be added correctly in Phase 2a).

---

### Phase 2: Add/Expand Missing Sections

Add sections in priority order. Base field descriptions on `config_schema.py` defaults
and `config.example.toml` inline comments.

---

#### Phase 2a — Add `[mcp.graph]` section

This section controls graph/knowledge-graph features. Currently completely absent from
CONFIGURATION.md. Place after `[mcp.logging]` in the reference section.

**Full field reference:**

| Field | Type | Default | Env Var | Description |
|-------|------|---------|---------|-------------|
| `generate_summaries` | bool | `false` | `WATERCOOLER_GRAPH_SUMMARIES` | Generate LLM summaries when indexing thread entries |
| `generate_embeddings` | bool | `false` | `WATERCOOLER_GRAPH_EMBEDDINGS` | Generate embeddings when indexing thread entries |
| `summarizer_api_base` | str | `""` | — | Override LLM endpoint for summarization (falls back to `memory.llm.api_base`) |
| `summarizer_model` | str | `""` | — | Override model for summarization |
| `embedding_api_base` | str | `""` | — | Override endpoint for graph embeddings |
| `embedding_model` | str | `""` | — | Override model for graph embeddings |
| `prefer_extractive` | bool | `true` | — | Prefer extractive over abstractive summarization |
| `auto_detect_services` | bool | `true` | `WATERCOOLER_GRAPH_AUTO_DETECT` | Auto-detect running LLM/embedding services |
| `auto_start_services` | bool | `false` | `WATERCOOLER_AUTO_START_SERVICES` | Auto-start LLM/embedding services if not detected |
| `embedding_divergence_threshold` | float | `0.6` | `WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD` | Cosine divergence threshold for embedding mismatch warnings |

**Note to include**: `generate_summaries` and `generate_embeddings` default to `false`
(conservative schema default). The example config shows `true` as the recommended value
for users with LLM services configured. Enable after configuring `[memory.llm]` and
`[memory.embedding]`.

---

#### Phase 2b — Add `[memory.tiers]` section

Tier orchestration (T1/T2/T3 multi-tier search) is completely undocumented. Place after
the backend-specific sections (`[memory.graphiti]`, `[memory.leanrag]`) in the memory
reference.

**Full field reference:**

| Field | Type | Default | Env Var | Description |
|-------|------|---------|---------|-------------|
| `t1_enabled` | bool | `true` | `WATERCOOLER_TIER_T1_ENABLED` | Enable T1 keyword/BM25 search tier |
| `t2_enabled` | bool | `true` | `WATERCOOLER_TIER_T2_ENABLED` | Enable T2 Graphiti graph search tier |
| `t3_enabled` | bool | `false` | `WATERCOOLER_TIER_T3_ENABLED` | Enable T3 LeanRAG full-text tier (requires `leanrag.path`) |
| `max_tiers` | int | `2` | `WATERCOOLER_TIER_MAX_TIERS` | Maximum tiers to query before stopping |
| `min_results` | int | `3` | `WATERCOOLER_TIER_MIN_RESULTS` | Minimum results required to stop tier escalation |
| `min_confidence` | float | `0.7` | — | Minimum confidence score to stop escalation early |
| `t1_limit` | int | `20` | — | Max results fetched from T1 |
| `t2_limit` | int | `10` | — | Max results fetched from T2 |
| `t3_limit` | int | `10` | — | Max results fetched from T3 |

---

#### Phase 2c — Expand `[memory.graphiti]` section

The section exists in CONFIGURATION.md but is missing several fields. Add the following:

| Field | Type | Default | Env Var | Description |
|-------|------|---------|---------|-------------|
| `chunk_on_sync` | bool | `true` | `WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC` | Split entries into chunks before Graphiti indexing |
| `chunk_max_tokens` | int | `768` | `WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS` | Maximum tokens per chunk |
| `chunk_overlap` | int | `64` | `WATERCOOLER_GRAPHITI_CHUNK_OVERLAP` | Token overlap between adjacent chunks |
| `use_summary` | bool | `false` | — | Use entry summary instead of full body for indexing |
| `track_entry_episodes` | bool | `true` | — | Track entry-level episodes in the Graphiti graph |

---

#### Phase 2d — Add `[memory.leanrag]` section

Currently shown in `config.example.toml` but absent from CONFIGURATION.md. Place after
`[memory.graphiti]`.

**Full field reference:**

| Field | Type | Default | Env Var | Description |
|-------|------|---------|---------|-------------|
| `path` | str | `""` | `LEANRAG_PATH` | Path to LeanRAG installation. **Required if T3 is enabled.** |
| `llm_model` | str | `""` | — | Override LLM model (falls back to `memory.llm.model`) |
| `llm_api_base` | str | `""` | — | Override LLM endpoint for LeanRAG |
| `embedding_model` | str | `""` | — | Override embedding model |
| `embedding_api_base` | str | `""` | — | Override embedding endpoint |
| `max_workers` | int | `4` | — | Max parallel indexing workers |

---

#### Phase 2e — Update federation section: document constraint

In the `[federation]` section, add an explicit note:

> **Enforced constraint**: `namespace_timeout` must be ≤ `max_total_timeout`. Violating
> this is caught at startup with a clear error message. The defaults satisfy this
> constraint (`namespace_timeout=30 ≤ max_total_timeout=60`).

Also add `WATERCOOLER_FEDERATION_ENABLED` to the env var mapping for the section.

---

#### Phase 2f — Add advanced-features brief section

Add a new "Advanced Configuration" section near the end of CONFIGURATION.md documenting
sections that are internal or rarely user-configured. Each gets a 2-3 sentence description
plus a pointer to `config.example.toml`.

**`[mcp.daemons]`**: Controls background daemon processes — the compound daemon (auto
suggestions, learnings on closure) and thread auditor (finds stale/malformed threads).
Enable with `enabled = true` under `[mcp.daemons]`. Full field reference in
`config.example.toml`.

**`[mcp.cache]`**: In-memory or Redis-backed result cache. Set `backend = "redis"` and
`api_url` for Redis. Default: in-memory with 300s TTL. Full field reference in
`config.example.toml`.

**`[mcp.http]`**: HTTP transport settings — CORS origins, max request size, timeout.
Only relevant when `transport = "http"`. Full field reference in `config.example.toml`.

**`[mcp.slack]`**: Slack notification integration. Set `webhook_url` or `bot_token` +
`app_token` to enable. See `docs/SLACK.md` for full setup guide. Full field reference in
`config.example.toml`. (No env vars; use credentials.toml pattern for tokens.)

---

### Phase 3: Complete and Normalize the Env Var Table

**Replace** the existing env var table in CONFIGURATION.md with a complete normalized
table grouped by functional section. Use 4 columns:

```
| Env Var | TOML Path | Default | Description |
```

The complete inventory below is drawn from `config_loader.py` and validated against
`config_schema.py`. Group by section with `####` subheadings.

#### Core / Common

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_THREADS_PATTERN` | `common.threads_pattern` | `""` | URL pattern for threads remote |
| `WATERCOOLER_TEMPLATES` | `common.templates_dir` | `""` | Override built-in templates directory |

#### MCP Core

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_DIR` | `mcp.threads_dir` | auto | Override threads directory path |
| `WATERCOOLER_THREADS_BASE` | `mcp.threads_base` | `""` | Base path for thread storage |
| `WATERCOOLER_AGENT` | `mcp.default_agent` | auto | Default agent identity |
| `WATERCOOLER_AGENT_TAG` | `mcp.agent_tag` | `""` | Tag appended to agent identity |
| `WATERCOOLER_AUTO_BRANCH` | `mcp.auto_branch` | `true` | Auto-detect and use current git branch |
| `WATERCOOLER_AUTO_PROVISION` | `mcp.auto_provision` | `true` | Auto-provision thread git infrastructure |
| `WATERCOOLER_MCP_TRANSPORT` | `mcp.transport` | `"stdio"` | Transport: `stdio` or `http` |
| `WATERCOOLER_MCP_HOST` | `mcp.host` | `"127.0.0.1"` | HTTP transport bind host |
| `WATERCOOLER_MCP_PORT` | `mcp.port` | `3000` | HTTP transport port |

#### Git

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_GIT_AUTHOR` | `mcp.git.author` | `""` | Git author name for thread commits |
| `WATERCOOLER_GIT_EMAIL` | `mcp.git.email` | `"mcp@watercooler.dev"` | Git author email for thread commits |
| `WATERCOOLER_GIT_SSH_KEY` | `mcp.git.ssh_key` | `""` | Path to SSH private key for git operations |

#### Sync

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_ASYNC_SYNC` | `mcp.sync.async_sync` | `true` | Enable async (non-blocking) sync |
| `WATERCOOLER_BATCH_WINDOW` | `mcp.sync.batch_window` | `5.0` | Seconds to batch coalesced writes |
| `WATERCOOLER_SYNC_INTERVAL` | `mcp.sync.interval` | `30.0` | Background sync interval in seconds |
| `WATERCOOLER_SYNC_MAX_RETRIES` | `mcp.sync.max_retries` | `5` | Maximum sync retry attempts |
| `WATERCOOLER_SYNC_MAX_BACKOFF` | `mcp.sync.max_backoff` | `300.0` | Maximum backoff in seconds |

#### Logging

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_LOG_LEVEL` | `mcp.logging.level` | `"INFO"` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `WATERCOOLER_LOG_DIR` | `mcp.logging.dir` | `""` | Log file directory (empty = no file logging) |
| `WATERCOOLER_LOG_MAX_BYTES` | `mcp.logging.max_bytes` | `10485760` | Max log file size before rotation (bytes) |
| `WATERCOOLER_LOG_BACKUP_COUNT` | `mcp.logging.backup_count` | `5` | Number of rotated log files to retain |
| `WATERCOOLER_LOG_DISABLE_FILE` | `mcp.logging.disable_file` | `false` | Disable file logging entirely |

#### Graph

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_GRAPH_SUMMARIES` | `mcp.graph.generate_summaries` | `false` | Generate LLM summaries during graph indexing |
| `WATERCOOLER_GRAPH_EMBEDDINGS` | `mcp.graph.generate_embeddings` | `false` | Generate embeddings during graph indexing |
| `WATERCOOLER_GRAPH_AUTO_DETECT` | `mcp.graph.auto_detect_services` | `true` | Auto-detect running LLM/embedding services |
| `WATERCOOLER_AUTO_START_SERVICES` | `mcp.graph.auto_start_services` | `false` | Auto-start LLM/embedding services |
| `WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD` | `mcp.graph.embedding_divergence_threshold` | `0.6` | Cosine divergence threshold for mismatch warnings |

#### Validation

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_VALIDATE_ON_WRITE` | `validation.on_write` | `true` | Validate entries on write |
| `WATERCOOLER_FAIL_ON_VIOLATION` | `validation.fail_on_violation` | `false` | Raise error (vs warn) on validation failure |

#### Memory

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_MEMORY_BACKEND` | `memory.backend` | `"null"` | Backend: `null`, `graphiti`, `leanrag` |
| `WATERCOOLER_MEMORY_QUEUE` | `memory.queue_enabled` | `false` | Enable async memory task queue |
| `WATERCOOLER_MEMORY_DISABLED` | `memory.enabled` (inverted) | `false` | Disable memory entirely |

#### LLM Service

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `LLM_API_KEY` | credentials | `""` | LLM API key (prefer `credentials.toml`) |
| `LLM_API_BASE` | `memory.llm.api_base` | `""` | LLM service base URL |
| `LLM_MODEL` | `memory.llm.model` | `""` | LLM model name |
| `LLM_TIMEOUT` | `memory.llm.timeout` | `60.0` | LLM request timeout in seconds |
| `LLM_MAX_TOKENS` | `memory.llm.max_tokens` | `512` | Max tokens per LLM response |
| `LLM_CONTEXT_SIZE` | `memory.llm.context_size` | `8192` | LLM context window size |
| `LLM_SYSTEM_PROMPT` | `memory.llm.system_prompt` | `""` | Override system prompt |
| `LLM_PROMPT_PREFIX` | `memory.llm.prompt_prefix` | `""` | Prefix added to all LLM prompts |

#### Embedding Service

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `EMBEDDING_API_KEY` | credentials | `""` | Embedding API key (prefer `credentials.toml`) |
| `EMBEDDING_API_BASE` | `memory.embedding.api_base` | `""` | Embedding service base URL |
| `EMBEDDING_MODEL` | `memory.embedding.model` | `"bge-m3"` | Embedding model name |
| `EMBEDDING_DIM` | `memory.embedding.dim` | `1024` | Embedding dimension |
| `EMBEDDING_TIMEOUT` | `memory.embedding.timeout` | `60.0` | Embedding request timeout in seconds |
| `EMBEDDING_BATCH_SIZE` | `memory.embedding.batch_size` | `32` | Batch size for embedding requests |
| `EMBEDDING_CONTEXT_SIZE` | `memory.embedding.context_size` | `8192` | Embedding context window size |

#### Database (FalkorDB / Graphiti)

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `FALKORDB_HOST` | `memory.database.host` | `"localhost"` | FalkorDB host |
| `FALKORDB_PORT` | `memory.database.port` | `6379` | FalkorDB port |
| `FALKORDB_USERNAME` | `memory.database.username` | `""` | FalkorDB authentication username |
| `FALKORDB_PASSWORD` | `memory.database.password` | `""` | FalkorDB authentication password |

#### Graphiti Backend

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_GRAPHITI_RERANKER` | `memory.graphiti.reranker` | `"none"` | Reranker: `none`, `cohere`, `cross-encoder` |
| `WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC` | `memory.graphiti.chunk_on_sync` | `true` | Chunk entries before Graphiti indexing |
| `WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS` | `memory.graphiti.chunk_max_tokens` | `768` | Max tokens per chunk |
| `WATERCOOLER_GRAPHITI_CHUNK_OVERLAP` | `memory.graphiti.chunk_overlap` | `64` | Token overlap between chunks |

#### LeanRAG Backend

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `LEANRAG_PATH` | `memory.leanrag.path` | `""` | Path to LeanRAG installation (required for T3) |

#### Federation

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_FEDERATION_ENABLED` | `federation.enabled` | `false` | Enable cross-namespace federation search |

#### Tier Orchestration

| Env Var | TOML Path | Default | Description |
|---------|-----------|---------|-------------|
| `WATERCOOLER_TIER_T1_ENABLED` | `memory.tiers.t1_enabled` | `true` | Enable T1 keyword search tier |
| `WATERCOOLER_TIER_T2_ENABLED` | `memory.tiers.t2_enabled` | `true` | Enable T2 Graphiti graph tier |
| `WATERCOOLER_TIER_T3_ENABLED` | `memory.tiers.t3_enabled` | `false` | Enable T3 LeanRAG full-text tier |
| `WATERCOOLER_TIER_MAX_TIERS` | `memory.tiers.max_tiers` | `2` | Max tiers to query before stopping |
| `WATERCOOLER_TIER_MIN_RESULTS` | `memory.tiers.min_results` | `3` | Min results to stop tier escalation |

---

### Phase 4: Delete ENVIRONMENT_VARS.md and Update Cross-References

All 5 references to `ENVIRONMENT_VARS.md` found in the codebase must be updated:

| File | Location | Change |
|------|----------|--------|
| `docs/CONFIGURATION.md` | "See Also" section (~line 513) | Remove `ENVIRONMENT_VARS.md` link (file will be gone) |
| `README.md` | ~line 125 | `[Advanced configuration](docs/ENVIRONMENT_VARS.md)` → `[Environment variables](docs/CONFIGURATION.md#environment-variables)` |
| `docs/INSTALLATION.md` | ~line 61 | `[Environment Variables Reference](ENVIRONMENT_VARS.md)` → `[Environment variables reference](CONFIGURATION.md#environment-variables)` |
| `docs/INSTALLATION.md` | ~line 151 | `[Environment Variables](ENVIRONMENT_VARS.md)` → `[Environment variables](CONFIGURATION.md#environment-variables)` |
| `src/watercooler/templates/config.example.toml` | ~line 854 | Update comment reference if present |

**Action after updating**: `git rm docs/ENVIRONMENT_VARS.md`

**Verify**: `grep -r "ENVIRONMENT_VARS.md" docs/ README.md src/` should return zero results.

---

### Phase 5: Verify Cross-Referenced Docs (Light Pass)

Do a targeted read of each file to spot-check config examples for currency. No
structural changes expected — just fix any stale snippets found.

**`docs/QUICKSTART.md`**:
- Verify `config.toml` snippets use valid field names from current schema
- Verify `~/.watercooler/config.toml` vs `.watercooler/config.toml` references are accurate
- Verify `memory.backend` examples match current backend names

**`docs/mcp-server.md`**:
- Verify any configuration table examples match current schema defaults
- Verify credential and config setup references are current

**`docs/INSTALLATION.md`**:
- After link updates (Phase 4), light pass for any other stale references
- Verify config snippets in installation steps match current schema

---

## Schema Audit Findings (Dev Reference)

> These findings require no immediate action but are captured for future reference.

### Stale Fields: In CONFIGURATION.md but Not in Schema

| Field | Location in CONFIGURATION.md | Action |
|-------|-------------------------------|--------|
| `baseline_graph.extractive.max_chars` | ~line 222 | **Remove** (Phase 1) |
| `baseline_graph.extractive.include_headers` | ~line 223 | **Remove** (Phase 1) |
| `baseline_graph.extractive.max_headers` | ~line 224 | **Remove** (Phase 1) |

### Schema Fields Intentionally Without Env Vars (TOML-only)

These are considered internal or advanced enough that TOML-only configuration is appropriate:

| Section | Fields | Rationale |
|---------|--------|-----------|
| `mcp.sync` | `max_delay`, `max_batch_size`, `stale_threshold` | Sync tuning; reasonable defaults; TOML-only |
| `mcp.logging` | `max_bytes`, `backup_count`, `disable_file` | Log rotation; TOML-only |
| `validation` | All entry/commit validation fields | Policy config; TOML-only |
| `dashboard` | All fields | Dashboard-specific; TOML-only |
| `mcp.daemons` | All fields | Daemon config; TOML-only |
| `mcp.http` | All fields | HTTP transport config; TOML-only |
| `mcp.cache` | All fields | Cache config; TOML-only |
| `mcp.slack` | Most fields | Slack integration; credentials.toml pattern |
| `memory.llm` | `timeout`, `max_tokens`, `context_size`, `system_prompt`, `prompt_prefix`, summary prompts | LLM tuning; TOML-only |
| `memory.embedding` | `timeout`, `batch_size`, `context_size` | Embedding tuning; TOML-only |
| `memory.graphiti` | `track_entry_episodes`, `use_summary` | Advanced tuning; TOML-only |
| `memory.tiers` | `min_confidence`, `t1/t2/t3_limit` | Tier tuning; TOML-only |

### Default Mismatch (Note in Docs — Not a Bug)

| Field | Schema Default | Example Config Value | Recommendation |
|-------|---------------|----------------------|----------------|
| `mcp.graph.generate_summaries` | `false` | `true` | Document schema default; add note that example shows recommended production value |
| `mcp.graph.generate_embeddings` | `false` | `true` | Same as above |

---

## Dependencies & Risks

- **No code changes** expected. All changes are documentation only.
- Deleting `ENVIRONMENT_VARS.md` removes a file some users may have bookmarked.
  The redirected links in `README.md` and `INSTALLATION.md` will guide them.
- Removing `baseline_graph.extractive.*` from CONFIGURATION.md is safe — these fields
  are already absent from the schema.

## References

### Internal References

- Config schema: `src/watercooler/config_schema.py`
- Env var loader: `src/watercooler/config_loader.py`
- Canonical example: `src/watercooler/templates/config.example.toml`
- Credentials example: `src/watercooler/templates/credentials.example.toml`
- Federation constraint source: PR #190 — `model_validator(mode="after")` in `FederationConfig`
- Brainstorm: `dev_docs/brainstorms/2026-02-27-configuration-docs-update-brainstorm.md`

### Files Changed

| File | Change Type |
|------|-------------|
| `docs/CONFIGURATION.md` | Major update — remove stale, add 5+ sections, complete env var table |
| `docs/ENVIRONMENT_VARS.md` | Delete (git rm) |
| `README.md` | 1 link update |
| `docs/INSTALLATION.md` | 2 link updates |
| `docs/QUICKSTART.md` | Verify / minor alignment |
| `docs/mcp-server.md` | Verify / minor alignment |
