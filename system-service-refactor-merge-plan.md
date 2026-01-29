# System Service Refactor Merge Plan

## Executive Summary

This plan details the coordinated release of watercooler-cloud (feature/slack-integration → main → stable) and watercooler-site (main → staging → stable) to production.

**Critical Ordering**: watercooler-cloud MUST be released FIRST because watercooler-site depends on the MCP server.

---

## Part 1: watercooler-cloud Merge Plan

### 1.1 Branch Analysis

| Branch | Commits | Files Changed | Lines Added | Lines Removed |
|--------|---------|---------------|-------------|---------------|
| feature/slack-integration | 50+ | 77 | 14,656 | 1,157 |
| main (incoming) | 30+ | 40 | 7,749 | 776 |

**Merge Result**: NO TEXTUAL CONFLICTS - Git auto-merge succeeds

### 1.2 Feature Branch Changes (Our Work)

| Category | Changes |
|----------|---------|
| **Slack Integration** | Complete `src/watercooler_mcp/slack/` module (8 files) |
| **HTTP Deployment** | `server_http.py` for Railway hosted mode |
| **Graph-First** | Per-thread graph format, dual-write, centralized storage |
| **Thread Tools** | Expanded MCP tools for thread operations |
| **SlackConfig** | Added to `config_schema.py` |
| **Validation** | Enhanced `validation.py` with code_path detection |

### 1.3 Main Branch Changes (Incoming)

| Category | Changes |
|----------|---------|
| **Memory Config** | NEW `src/watercooler/memory_config.py` (460 lines) |
| **Tier Strategy** | NEW `src/watercooler_memory/tier_strategy.py` (963 lines) |
| **Config Schema** | Expanded memory backend configuration |
| **Credentials** | Refactored credential resolution |
| **Memory Tools** | Expanded MCP memory tools |
| **Graphiti Backend** | Enhanced with chunking, better error handling |
| **Migration Tools** | Moved to standalone scripts |

### 1.4 Semantic Integration Analysis

#### Configuration System Impact

**Current State (feature branch)**:
- `GraphConfig` hardcodes llama-server defaults: `localhost:8000/v1`, `llama3.2:3b`
- `startup.py` auto-starts llama-server for local inference
- `memory.py` uses simple env var loading

**After Merge (with main changes)**:
- Unified `memory_config.py` resolves configuration with priority chain:
  1. Environment variables (highest)
  2. Backend-specific TOML overrides
  3. Shared TOML settings
  4. Built-in defaults (lowest)
- `startup.py` uses `resolve_baseline_graph_llm_config()` for dynamic URL resolution
- `memory.py` imports from `memory_config` for consistent resolution

**Impact Assessment**: LOW RISK
- Default localhost behavior with llama-server auto-start
- Users with remote LLM services gain proper configuration support
- No breaking changes to existing workflows

#### Enrichment Services (llama-server) Impact

| Service | Current (Feature) | After Merge |
|---------|------------------|-------------|
| Summarizer | `localhost:8000/v1` | Resolved via `memory_config.resolve_baseline_graph_llm_config()` |
| Embedding | `localhost:8080/v1` | Resolved via `memory_config.resolve_embedding_config()` |

**Migration Required**: NONE
- Defaults match current hardcoded values
- Existing `.env` configurations continue working
- New TOML options available but optional

#### MCP Tools Integration

**Feature Branch MCP Changes**:
- Thread tools: `say`, `ack`, `handoff`, `set_status`, `set_ball`
- Graph tools: `search`, `find_similar`, `health`, `reconcile`, `backfill`
- Branch parity tools: `validate`, `sync`, `audit`, `recover`
- Slack tools: `sync_slack`, `rebuild_slack`

**Main Branch MCP Changes**:
- Memory tools: `query_memory`, `search_nodes`, `get_entity_edge`, `search_memory_facts`
- Migration tools: `migrate_to_memory_backend`, `migration_preflight`
- Tier query tools: Multi-tier orchestration support

**Integration**: CLEAN MERGE
- No overlapping tool definitions
- Both sets of tools can coexist
- Memory tools use unified config; thread tools use graph-first path

### 1.5 Merge Procedure

#### Pre-Merge Checklist

- [ ] Ensure feature/slack-integration is up-to-date with origin
- [ ] Run full test suite on feature branch: `pytest`
- [ ] Verify Railway test deployment is working

#### Merge Steps

```bash
# 1. Fetch latest from origin
git fetch origin main

# 2. Checkout feature branch
git checkout feature/slack-integration

# 3. Create merge commit (preserves history)
git merge origin/main -m "chore: merge main into feature/slack-integration

Integrates:
- Unified memory_config.py for configuration resolution
- Multi-tier query strategy (T1/T2/T3)
- Enhanced Graphiti backend with chunking
- Expanded memory MCP tools
- Migration tools moved to scripts"

# 4. Run tests to verify integration
pytest

# 5. Manual verification
python -c "from watercooler.memory_config import resolve_llm_config; print(resolve_llm_config())"
```

#### Post-Merge Verification

- [ ] All tests pass
- [ ] MCP server starts without errors
- [ ] Graph-first operations work with new config resolution
- [ ] Memory tools work (if enabled)
- [ ] llama-server auto-start works (if configured)

### 1.6 Release to Main

After merge verification:

```bash
# 1. Push merged feature branch
git push origin feature/slack-integration

# 2. Create PR: feature/slack-integration → main
gh pr create --base main --head feature/slack-integration \
  --title "feat: Slack integration with graph-first architecture"

# 3. Merge PR (after CI passes)
gh pr merge --squash
```

### 1.7 Release to Stable

Follow CONTRIBUTING.md release process:

```bash
# Phase 2: Prepare Release
git checkout main
git pull origin main
# Edit pyproject.toml: remove -dev suffix
git commit -m "chore(release): prepare v0.2.0"
git push origin main

# Phase 3: Create Release PR
gh pr create --base staging --head main --title "Release v0.2.0"
# Wait for CI, merge

# Phase 4: Release to Production
git fetch origin
git checkout stable
git merge --ff-only origin/staging
git tag -a v0.2.0 -m "Release v0.2.0 - Slack integration with graph-first architecture"
git push origin stable --tags

# Phase 5: Post-Release
git checkout main
# Edit pyproject.toml: bump to 0.2.1-dev
git commit -m "chore: bump version to 0.2.1-dev"
git push origin main
```

### 1.8 Railway Deployment Verification

After stable release:

```bash
# Verify Railway main environment updated
curl https://watercooler-cloud.up.railway.app/health

# Test MCP HTTP endpoint
curl -X POST https://watercooler-cloud.up.railway.app/mcp \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/list"}'
```

---

## Part 2: watercooler-site Release Plan

### 2.1 Current State

- Branch: `main` (already up-to-date)
- No merge required - all work already on main
- Ready for staging/stable promotion

### 2.2 Pre-Release Verification

Ensure watercooler-cloud is on stable FIRST:

```bash
# Verify Railway production has new version
curl https://watercooler-cloud.up.railway.app/health

# Verify MCP tools available
curl -X POST https://watercooler-cloud.up.railway.app/mcp \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/list"}' | jq '.result.tools | length'
```

### 2.3 Release Process

```bash
# 1. Verify main is ready
cd /home/caleb/Work/Personal/MostlyHarmless-AI/repo/watercooler-site
git checkout main
git pull origin main

# 2. Run local tests
pnpm test
pnpm build

# 3. Create staging PR
gh pr create --base staging --head main \
  --title "Release: Slack integration features"

# 4. Merge to staging (after CI)
gh pr merge --squash

# 5. Release to stable
git fetch origin
git checkout stable
git merge --ff-only origin/staging
git push origin stable

# 6. Verify Vercel production deployment
vercel ls watercooler-site --environment=production | head -3
```

### 2.4 Post-Release Verification

```bash
# 1. Verify production URL
curl -I https://watercoolerdev.com

# 2. Test Slack integration (if configured)
# - OAuth flow at /settings/integrations
# - Slash command in Slack workspace

# 3. Verify branch alias updated
vercel alias ls | grep watercoolerdev
```

---

## Part 3: Rollback Procedures

### 3.1 watercooler-cloud Rollback

```bash
# If issues discovered after stable release:

# Option 1: Quick patch (preferred)
git checkout main
# Fix the issue
git commit -m "fix: critical issue in v0.2.0"
# Follow release process for v0.2.1

# Option 2: Revert stable (emergency)
git checkout stable
git revert HEAD  # Revert the merge commit
git push origin stable
# Railway auto-redeploys previous code
```

### 3.2 watercooler-site Rollback

```bash
# Vercel rollback (instant, no code changes)
vercel rollback watercooler-site

# Or manual alias change
vercel alias set <previous-deployment-url> watercoolerdev.com
```

---

## Part 4: Risk Assessment

### 4.1 Low Risk Items

| Item | Mitigation |
|------|------------|
| Memory config resolution | Defaults preserved, backward compatible |
| llama-server auto-start | Localhost-only, won't affect remote users |
| Graph format migration | Dual-write ensures backward compatibility |

### 4.2 Medium Risk Items

| Item | Mitigation |
|------|------------|
| Slack timeout issues | Async pattern already applied |
| Branch alias stale | CI workflow auto-fixes |
| Memory tool errors | Feature-flagged, won't affect core |

### 4.3 Monitoring

After release, monitor:

1. **Railway logs**: `railway logs -f`
2. **Vercel logs**: Vercel dashboard → Deployments → Logs
3. **Slack notifications**: Watch for error messages in linked channels
4. **Thread operations**: Test say/ack/handoff cycle

---

## Part 5: Release Checklist

### Phase A: watercooler-cloud (MCP Server)

- [ ] A1. Merge origin/main into feature/slack-integration
- [ ] A2. Run full test suite
- [ ] A3. Push merged branch
- [ ] A4. Create PR: feature/slack-integration → main
- [ ] A5. Wait for CI, merge PR
- [ ] A6. Bump version, create PR: main → staging
- [ ] A7. Wait for CI, merge to staging
- [ ] A8. Fast-forward stable, create tag v0.2.0
- [ ] A9. Push stable and tags
- [ ] A10. Verify Railway production deployment
- [ ] A11. Bump main to 0.2.1-dev

### Phase B: watercooler-site (Dashboard/Slack)

- [ ] B1. Verify watercooler-cloud v0.2.0 on Railway production
- [ ] B2. Test main branch on Vercel preview
- [ ] B3. Create PR: main → staging
- [ ] B4. Wait for CI, merge to staging
- [ ] B5. Fast-forward stable
- [ ] B6. Push stable
- [ ] B7. Verify Vercel production deployment

### Phase C: Validation

- [ ] C1. Test dashboard thread operations
- [ ] C2. Test Slack integration (if configured)
- [ ] C3. Verify no regressions in existing features
- [ ] C4. Monitor logs for 24 hours
