# Copybara Public Release Sync — Brainstorm

**Date:** 2026-02-26
**Thread:** `copybara-public-release-sync`
**Status:** Draft

---

## What We're Building

An automated Copybara pipeline that publishes a sanitized snapshot of the `stable` branch to the public GitHub repo (`mostlyharmless-ai/watercooler`) whenever a push lands on `stable`. Internal content is stripped during transformation; the public repo sees only the user-facing library, docs, and tests.

**Audience:** Open-source community — external contributors, GitHub users, installers. Polish matters: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and clean commit history are all important.

---

## Why This Approach

- **Copybara** is the right tool: purpose-built for internal→public repo sync, battle-tested at Google, native GitHub Actions support.
- **SQUASH mode** keeps public history clean by collapsing WIP/internal commits into single sync commits.
- **Allowlist** (not denylist) is the correct security posture: explicitly include only known-safe paths. A denylist requires perfect enumeration of all internal files, which is fragile as the repo grows.

---

## Key Decisions

### 1. Allowlist over Denylist

The original proposal used `glob(["**"], exclude=[...])` (denylist). The security review correctly identified that this leaks `todos/`, `AGENTS.md`, `CLAUDE.md`, `watercooler/threads/**`, `.gitmodules`, `.githooks/**`, deployment configs, and more.

**Decision:** Switch to an explicit allowlist in `origin_files`.

**Allowlist (confirmed public-safe):**

```python
origin_files = glob(
    [
        # Core library & MCP server
        "src/**",
        # Tests
        "tests/**",
        # User documentation
        "docs/**",
        # JSON schemas
        "schemas/**",
        # User-facing scripts only (verified against public docs)
        "scripts/install-mcp.sh",
        "scripts/install-mcp.ps1",
        "scripts/mcp-server-daemon.sh",
        "scripts/git-credential-watercooler",
        # Public CI workflows
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        # Root config & community files
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "fastmcp.json",
        ".gitignore",
        ".gitattributes",
        ".python-version",
        ".tool-versions",
        # Server entry points
        "server.py",
        "main.py",
    ],
    exclude = [
        "tests/benchmarks/**",
    ],
)
```

**Not included (all internal):**

| Path | Reason |
|---|---|
| `dev_docs/**` | Internal architecture, plans, brainstorms |
| `todos/**` | 89 internal task tracking files (missed by original proposal) |
| `watercooler/**` | Internal thread/graph data; would leak conversation history |
| `external/**` | Private forks (~131 MB); users install via pip |
| `scripts/` (migration/indexing) | Operator-only: `build_memory_graph.py`, `index_graphiti.py`, etc. |
| `.gitmodules` | All 3 submodule entries are private (LeanRAG and graphiti are private forks; watercooler-planning is SSH-only) |
| `AGENTS.md`, `CLAUDE.md` | Internal agent protocol guidelines |
| `Dockerfile`, `nixpacks.toml`, `Procfile`, `railway.json` | Internal infra/deployment configs |
| `.claude/**`, `.serena/**` | Dev tooling config |
| `.githooks/**` | Internal git hooks |
| `.github/workflows/claude*.yml` | Internal Claude CI |
| `.github/workflows/branch-pairing-audit.yml` | Internal CI |
| `copy.bara.sky` | Reveals private repo URL |
| `tests/benchmarks/**` | May reference internal infra/data |

### 2. GitHub App Token (not PAT)

**Decision:** Use a GitHub App with installation token instead of a Personal Access Token.

- App token is scoped to exactly the two repos (read on `watercooler-cloud`, write on `watercooler`)
- No user account dependency — app survives team member departures
- Token rotates automatically (no manual renewal)
- Better audit trail for open-source trust

**Setup:** Create a GitHub App on `mostlyharmless-ai`, install on both repos, store `APP_ID` and `APP_PRIVATE_KEY` as secrets on the private repo. Use `actions/create-github-app-token` in the workflow to mint an installation token at runtime.

### 3. Dry-Run CI Gate on PRs to `stable`

**Decision:** Add a `pull_request` workflow that runs `copybara --dry-run` when a PR targets `stable`. Fails the PR if any forbidden path appears in the output.

This catches leakage before the actual sync fires — avoiding post-push remediation on the public repo.

### 4. Commit Note Sanitization

**Decision:** Remove `squash_notes` entirely.

The original `authoring.overwrite(...)` only controls the Git commit author field — it does not sanitize the squash note body. With `show_author=True`, internal author identities appear verbatim in the public commit message body. Removing `squash_notes` eliminates the leakage risk entirely; the public commit message will simply be `"Watercooler release sync"`. Changelog needs are addressed separately (see Open Question 5).

### 5. Workflow Concurrency

**Decision:** Add a `concurrency:` block to the publish workflow to serialize rapid pushes on `stable`.

```yaml
concurrency:
  group: copybara-publish
  cancel-in-progress: false  # queue, don't cancel; each stable push should sync
```

### 6. Destination Repo Guard (CI in Public Repo)

**Decision:** Add a CI check in the *destination* (public) repo that fails if forbidden paths appear (e.g., `AGENTS.md`, `CLAUDE.md`, `watercooler/`, `todos/`, `dev_docs/`).

This is **defence-in-depth only** — not the primary prevention gate. By the time destination CI runs, content has already been pushed to `main` and is publicly visible. The dry-run gate (Decision 3) is the real prevention layer — it must catch leakage before the push fires. The destination guard exists to catch regressions introduced by future source changes that slip past the dry-run.

---

## Resolved Questions

| Question | Resolution |
|---|---|
| Should `tests/benchmarks/` be excluded? | Yes — may reference internal infra/data |
| Which scripts are user-facing? | `install-mcp.sh`, `install-mcp.ps1`, `mcp-server-daemon.sh`, `git-credential-watercooler` — verified in `docs/INSTALLATION.md`, `docs/AUTHENTICATION.md`, `docs/CHATGPT_MCP_INTEGRATION.md`. `check-mcp-servers.sh` and `cleanup-mcp-servers.sh` are only in internal planning docs; excluded. |
| Is `mostlyharmless-ai/watercooler` the correct destination? | Unconfirmed — see Open Questions |
| Should `.gitmodules` be stripped or excluded? | Excluded entirely — all 3 entries are private |
| PAT vs GitHub App? | GitHub App preferred |
| Include deployment configs? | No — all internal |
| Dry-run CI gate? | Yes |

---

## Open Questions

1. **Destination repo name**: Is `mostlyharmless-ai/watercooler` confirmed as the public repo name, or is it `watercooler-cloud` / something else?
2. **Package naming**: `pyproject.toml` names the package `watercooler-cloud`. The public repo URL suggests `watercooler`. Do we rename the package (requires a Copybara `core.replace` transformation on `pyproject.toml`), keep `watercooler-cloud`, or publish under a different name entirely? This must be decided before release tagging is wired up.
3. **Private pip dependencies**: `pyproject.toml` memory/graphiti/leanrag extras reference private git repos. Public users running `pip install watercooler-cloud[memory]` will hit install failures. Options: (a) strip those extras from the public `pyproject.toml` via transformation, (b) publish graphiti/LeanRAG forks to PyPI, (c) document that memory extras require internal access.
4. **`server.py` and `main.py` at root**: These are tiny files (~250 bytes each). Need a quick audit to confirm no internal config is embedded before including in allowlist.
5. **`fastmcp.json`**: Contains only a path reference to `server.py`. Confirm it's safe to publish (no API keys, internal URLs).
6. **Release tagging on destination**: Should the workflow also create a Git tag on the public repo when syncing (to enable `pip install watercooler==X.Y.Z` against the public repo)? Depends on resolution of Q2 (package naming).
7. **Changelog / release notes strategy**: `squash_notes` stripped — does the public repo need a `CHANGELOG.md` kept in sync, or is the GitHub Releases page sufficient?
8. **`scripts/README.md`**: Documents both user-facing and internal scripts. Should it be published as-is (exposes internal script names), trimmed, or excluded?

---

## Next Step

Run `/workflows:plan` to produce a concrete implementation plan with exact file contents.
