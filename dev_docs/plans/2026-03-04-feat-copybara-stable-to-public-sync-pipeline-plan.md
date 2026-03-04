---
title: "feat: Copybara stable-to-public release sync pipeline"
type: feat
status: active
date: 2026-03-04
---

# feat: Copybara stable-to-public release sync pipeline

## Overview

Automated Copybara pipeline that publishes a sanitized snapshot of `watercooler-cloud@stable` to
the public GitHub repo `mostlyharmless-ai/watercooler` on every push to `stable`. Internal content
is stripped via allowlist transforms; the public repo receives only the user-facing library, docs,
and tests. A dry-run CI gate on PRs to `stable` catches leakage before it fires.

**Public destination:** `github.com/mostlyharmless-ai/watercooler` (confirmed)

---

## Problem Statement

`watercooler-cloud` is fully private. For open-source distribution and external contribution,
a public mirror must exist at `mostlyharmless-ai/watercooler`. Manual publishing is error-prone —
a single operator mistake could leak internal architecture docs, private planning threads,
deployment credentials, or private dependency URLs. An automated pipeline with security gates
is required to ensure internal content is never exposed.

---

## Proposed Solution

1. **Copybara** syncs `stable → mostlyharmless-ai/watercooler@main` in SQUASH mode
2. **Allowlist posture** (not denylist): only known-safe paths are synced; new internal files are
   silently excluded by default
3. **Content transforms** strip private pip dependencies, rename the package, and sanitize
   `pyproject.toml` and `ci.yml` for public consumption
4. **Dry-run gate** on PRs to `stable` catches leakage before the push fires
5. **Destination guard** in the public repo provides defense-in-depth
6. **GitHub App token** (not PAT) provides scoped, rotatable credentials with no user-account
   dependency

---

## Technical Approach

### Architecture: Publish Flow

```
         watercooler-cloud (private)
                   │
          push to `stable`
                   │
                   ▼
     ┌─────────────────────────┐
     │  GitHub Actions         │
     │  copybara-publish.yml   │
     │  (concurrency: queued)  │
     └───────────┬─────────────┘
                 │
                 ▼
     ┌─────────────────────────┐
     │  GitHub App Token mint  │
     │  (actions/create-github-│
     │   app-token@v1)         │
     └───────────┬─────────────┘
                 │
                 ▼
     ┌─────────────────────────┐
     │  Copybara SQUASH sync   │
     │  ┌─────────────────┐    │
     │  │ Allowlist filter│    │
     │  │ content transforms    │
     │  │ - pkg rename    │    │
     │  │ - strip privdeps│    │
     │  │ - fix ci.yml    │    │
     │  │ - fix pkg-data  │    │
     │  └─────────────────┘    │
     └───────────┬─────────────┘
                 │
                 ▼
     mostlyharmless-ai/watercooler
     (public, push to `main`)
                 │
                 ▼
     ┌─────────────────────────┐
     │  Destination guard CI   │
     │  (defense-in-depth)     │
     └─────────────────────────┘
```

### Architecture: PR Dry-Run Gate

```
   PR targeting `stable` (push/update)
                   │
                   ▼
     ┌─────────────────────────┐
     │  copybara-dry-run.yml   │
     │  copybara --dry-run     │
     │  + forbidden-path check │
     └───────────┬─────────────┘
                 │
        ┌────────┴────────┐
        │                 │
        ▼                 ▼
   CI: PASS           CI: FAIL
   (PR unblocked)     (PR blocked;
                       leakage found)
```

---

### Allowlist: `copy.bara.sky`

The following is the complete Copybara configuration file to be created at the repository root.

```python
# copy.bara.sky
# Publishes watercooler-cloud@stable → mostlyharmless-ai/watercooler@main
#
# SECURITY: This file is NOT in the sync allowlist (it reveals the private repo URL).
# It lives only in the private repo.

core.workflow(
    name = "default",
    origin = git.origin(
        url = "https://github.com/mostlyharmless-ai/watercooler-cloud.git",
        ref = "stable",
    ),
    destination = git.destination(
        url = "https://github.com/mostlyharmless-ai/watercooler.git",
        push = "main",
    ),
    origin_files = glob(
        [
            # Core library & MCP server
            "src/**",
            # Tests (see exclude below)
            "tests/**",
            # User-facing documentation
            "docs/**",
            # JSON schemas
            "schemas/**",
            # User-facing scripts only (4 confirmed scripts)
            "scripts/install-mcp.sh",
            "scripts/install-mcp.ps1",
            "scripts/mcp-server-daemon.sh",
            "scripts/git-credential-watercooler",
            # Public CI workflows only (2 files)
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            # Root config & community files
            # NOTE: pyproject.toml is NOT in the allowlist — see pyproject.public.toml below.
            # The sanitized public version is maintained as a separate file and renamed by transform.
            "pyproject.public.toml",  # maintained in private repo; moved to pyproject.toml in dest
            "README.md",
            "LICENSE",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "fastmcp.json",  # confirmed safe: only contains relative path to server.py
            ".gitignore",
            ".gitattributes",
            ".python-version",
            ".tool-versions",
            # Server entry points (pure import shims, audited safe)
            "server.py",
            "main.py",
        ],
        exclude = [
            # Internal benchmark fixtures
            "tests/benchmarks/**",
            # Real conversation history stored as integration test fixtures
            "tests/integration/.cli-threads/**",
            # Root-level cli-threads fixture dir (thread-style conversation data)
            "tests/.cli-threads/**",
            # LeanRAG test artifacts (JSON graph data from test runs)
            "tests/test_artifacts/**",
            # Package-internal scripts directory — excluded because:
            # (a) src/watercooler_mcp/scripts/git-credential-watercooler is identical
            #     to the user-facing root scripts/git-credential-watercooler (which IS
            #     published). Publishing both would create a duplicate.
            # (b) This directory is a package deployment artifact, not user documentation.
            # The root scripts/ copy is the canonical user-facing version.
            "src/watercooler_mcp/scripts/**",
            # Real internal project thread captured as a test fixture — not synthetic.
            # Contains: real developer identity, real internal topic, implementation details.
            # Entire directory excluded until all files are replaced with synthetic data.
            "tests/fixtures/threads/**",
            # Transient test output directory — excluded to prevent test-run artifacts
            # (e.g. alpha.md stubs) from accumulating in the public repo over time.
            "tests/tmp_threads/**",
        ],
    ),
    authoring = authoring.overwrite("Watercooler Sync Bot <sync@watercoolerdev.com>"),
    mode = "SQUASH",
    transformations = [
        # --- Replace private pyproject.toml with sanitized public version ---
        # pyproject.toml is NOT in origin_files; pyproject.public.toml is.
        # This move renames it to pyproject.toml in the destination.
        # Doing this via core.move avoids string-replacement inside TOML quoted values,
        # which would produce invalid PEP 508 requirements.
        core.move("pyproject.public.toml", "pyproject.toml"),

        # --- Fix runtime package name lookups in src/ ---
        # importlib.metadata.version("watercooler-cloud") raises PackageNotFoundError on
        # public builds where the distribution is named "watercooler". Without this transform,
        # `watercooler --version` and the MCP /version endpoint would report "0.0.0-dev".
        # Affects: src/watercooler/__init__.py, src/watercooler_mcp/__init__.py
        core.replace(
            before = 'version("watercooler-cloud")',
            after  = 'version("watercooler")',
            paths  = glob(["src/**"]),
        ),
        # config.py get_version() tries a list of dist names; update the private name.
        # Affects: src/watercooler_mcp/config.py get_version()
        core.replace(
            before = '("watercooler-cloud", "watercooler-mcp")',
            after  = '("watercooler", "watercooler-mcp")',
            paths  = glob(["src/**"]),
        ),

        # --- Rewrite private repo URL in test/schema/doc references ---
        # tests/unit/test_dual_stream_sufficiency.py and test_mcp_entry_tools.py embed
        # "mostlyharmless-ai/watercooler-cloud"; public users should see the public repo URL.
        core.replace(
            before = "mostlyharmless-ai/watercooler-cloud",
            after  = "mostlyharmless-ai/watercooler",
            paths  = glob(["tests/**", "schemas/**", "docs/**"]),
        ),

        # --- NOTE: bare package name rename deferred ---
        # Replacing "watercooler-cloud" → "watercooler" in schemas/**, docs/**, and other
        # non-src locations is a branding/full-rename decision deferred to a later milestone.
        # The transforms above handle the two cases that ARE required for the metadata rename:
        #   (a) importlib.metadata.version() lookups in src/** (correctness requirement)
        #   (b) private GitHub repo URLs in tests/schemas/docs (security requirement)
        # A bare string replace across docs would be part of the full-rename PR.

        # --- Fix CI install command: drop [graphiti] extra (private dep) ---
        core.replace(
            before = '".[dev,graphiti]"',
            after  = '".[dev]"',
            paths  = glob([".github/workflows/ci.yml"]),
        ),
        # --- Strip two-line internal comment from ci.yml (references LeanRAG/graphiti extras) ---
        core.replace(
            before = "      # Note: Using graphiti instead of memory to avoid private LeanRAG dependency\n"
                     "      # LeanRAG tests are skipped anyway (integration_leanrag_llm marker)\n",
            after  = "",
            paths  = glob([".github/workflows/ci.yml"]),
        ),
        # --- Strip internal `staging` branch from ci.yml workflow triggers ---
        # Public repo has no `staging` branch; publishing this trigger leaks internal branch naming.
        core.replace(
            before = "branches: [ main, staging ]",
            after  = "branches: [ main ]",
            paths  = glob([".github/workflows/ci.yml"]),
        ),

        # --- Strip internal release process comment from release.yml ---
        # Comment discloses the staging→stable→tag internal release flow.
        # All three lines of the comment must be covered; stripping only the first two
        # would leave the third line ("  # all tests pass before code reaches stable.\n")
        # verbatim in the public repo, still disclosing the internal flow.
        core.replace(
            before = "  # Note: No test job - code is already validated on staging before tagging.\n"
                     "  # Running tests here would be redundant. The staging branch CI ensures\n"
                     "  # all tests pass before code reaches stable.\n",
            after  = "",
            paths  = glob([".github/workflows/release.yml"]),
        ),

        # --- Fix install-mcp.sh: sentinel check and user messages use public package name ---
        # The sentinel `grep -q "watercooler-cloud" pyproject.toml` would fail for public users
        # because the public pyproject.toml (from pyproject.public.toml) has name="watercooler".
        # Use the full `name = "watercooler"` field match instead of the bare substring "watercooler"
        # to avoid a false-positive on private-repo checkouts where pyproject.toml also contains
        # the word "watercooler" (as a prefix of "watercooler-cloud").
        # The ^ anchor ensures we match the actual TOML key line, not a comment such as
        # "# name = "watercooler" is the public package name" which would also satisfy an
        # unanchored grep and cause a false-positive on any repo with such a comment.
        core.replace(
            before = 'grep -q "watercooler-cloud" pyproject.toml',
            after  = "grep -q '^name = \"watercooler\"' pyproject.toml",
            paths  = glob(["scripts/install-mcp.sh"]),
        ),
        core.replace(
            before = 'error "This script must be run from the watercooler-cloud directory"',
            after  = 'error "This script must be run from the watercooler directory"',
            paths  = glob(["scripts/install-mcp.sh"]),
        ),
        core.replace(
            before = "watercooler-cloud[mcp]",
            after  = "watercooler[mcp]",
            paths  = glob(["scripts/install-mcp.sh", "scripts/install-mcp.ps1"]),
        ),

        # --- Fixed public commit message (no squash_notes = no author identity leak) ---
        metadata.replace_message(
            "Watercooler release sync\n\nPublished from watercooler-cloud@stable.\n"
        ),
    ],
)
```

> **Note on `uv.lock`:** `uv.lock` is intentionally excluded from the allowlist. The lockfile
> contains resolved private fork SHAs (`mostlyharmless-ai/graphiti`, `mostlyharmless-ai/LeanRAG`)
> and hashes for packages not on PyPI. Publishing it would (a) leak private repo commit SHAs and
> (b) cause `uv sync` to fail for public users. Public users install via `pip install -e .`
> from the transformed `pyproject.toml`.

---

### What Is Excluded (and Why)

| Path | Reason |
|---|---|
| `dev_docs/**` | Internal architecture, plans, brainstorms |
| `todos/**` | 89 internal task tracking files |
| `watercooler/**` | Internal thread/graph data; conversation history |
| `tests/.cli-threads/**` | Root-level thread fixtures containing real conversation data |
| `tests/test_artifacts/**` | LeanRAG test run artifacts (JSON graph data from test runs) |
| `external/**` | Private submodule forks (~131 MB) |
| `scripts/` (all except 4 named scripts) | Operator-only: migrations, indexing, memory tooling |
| `src/watercooler_mcp/scripts/**` | Package-internal duplicate of root `scripts/git-credential-watercooler` (which IS published). The src copy is a deployment artifact; publishing it would create an identical duplicate. |
| `scripts/README.md` | Documents internal scripts; would expose operational details |
| `.gitmodules` | All 3 submodule entries are private |
| `AGENTS.md`, `CLAUDE.md` | Internal agent protocol guidelines |
| `Dockerfile`, `nixpacks.toml`, `Procfile`, `railway.json` | Internal infra/deployment configs |
| `.claude/**`, `.serena/**`, `.githooks/**` | Dev tooling config |
| `.github/workflows/claude*.yml` | Internal Claude CI workflows |
| `.github/workflows/branch-pairing-audit.yml` | Internal audit workflow |
| `copy.bara.sky` | Reveals private repo URL |
| `tests/benchmarks/**` | May reference internal infra/data |
| `tests/integration/.cli-threads/**` | Real project conversation history stored as fixtures |
| `tests/fixtures/threads/**` | Real internal project thread data (not synthetic); excluded until replaced with synthetic fixtures |
| `tests/tmp_threads/**` | Transient test output directory; excluded to prevent test-run artifacts from accumulating in public repo |
| `uv.lock` | Private fork SHAs + non-PyPI package hashes |
| `pyproject.toml` (raw) | Contains private git dep URLs; replaced by `pyproject.public.toml` via `core.move` |

---

## Implementation Phases

### Phase Dependencies

Work must proceed in this order — each phase depends on the ones listed:

| Phase | Depends On | Notes |
|---|---|---|
| Phase 0 | — | All of Phase 0 must complete before Phase 1 |
| Phase 1 | Phase 0 complete (0.2 audits finished; 0.3 App created; 0.4 public repo created) | `copy.bara.sky` contents depend on audit results |
| Phase 2 | Phase 1 (`copy.bara.sky` written and reviewed) | Workflows reference `copy.bara.sky` |
| Phase 3 | Phase 0.4 (public repo exists) | Can run in parallel with Phase 2 |
| Phase 4 | Phases 0, 1, 2, **and 3.1** | Destination guard must be installed before first push |
| Phase 5 | Phase 4 (bootstrap complete, pipeline running) | |
| Phase 6 | Phase 4 (pipeline running) | Hardening steps; can be phased in incrementally |

> **Critical ordering note:** Phase 3.1 (destination guard CI in the public repo) must be
> created **before** Phase 4 bootstrap fires the first push. A push to an unguarded public
> repo cannot be retroactively protected.

---

### Phase 0: Prerequisites & Decisions (manual, one-time)

**0.1 — Resolve open questions**

| Question | Decision |
|---|---|
| Destination repo | ✅ `mostlyharmless-ai/watercooler` (confirmed) |
| Package rename | Rename `watercooler-cloud` → `watercooler` via `pyproject.public.toml` + `core.move` transform |
| Private pip deps | Remove `graphiti`, `leanrag`, and `memory` extras entirely from `pyproject.public.toml` (whole TOML array block) — no stubs |
| `uv.lock` | Remove from allowlist; public users get unlocked install |
| `scripts/README.md` | Exclude (exposes internal script names) |
| Release tagging | Phase 4: manually push tags to public repo after each private release (automation deferred) |
| Changelog | GitHub Releases auto-notes sufficient for now; `CHANGELOG.md` deferred |
| `-dev` version gating | Convention: `stable` branch never carries `-dev` suffix (enforced by existing release process). Add optional workflow guard as belt-and-suspenders. |

**0.2 — Pre-sync audits (required before writing `copy.bara.sky`)**

- [x] `src/watercooler_mcp/scripts/` — ✅ Confirmed: directory exists and contains
      `git-credential-watercooler` (hardcodes `watercoolerdev.com`). Added as unconditional
      exclusion in `copy.bara.sky`: `"src/watercooler_mcp/scripts/**"`.
- [ ] Confirm `server.py` and `main.py` contain no deployment-specific config.
      **Known finding:** `server.py` contains `# This works because FastMCP Cloud runs pip install .`
      and `main.py` contains a docstring referencing Railway/ASGI deployments. These are not
      credentials; the deployment platform names are accepted as public knowledge. Verify no
      internal URLs, API keys, or config values are embedded beyond these comments.
- [x] `fastmcp.json` confirmed safe. Content: `{"source": {"type": "filesystem", "path":
      "src/watercooler_mcp/server.py", "entrypoint": "mcp"}}` — contains only a relative path
      reference to `server.py`. No API keys, internal URLs, or deployment credentials.
- [ ] Audit `tests/integration/.cli-threads/` fixtures — determine if they contain real
      project conversation data. If yes, replace with synthetic fixtures or add to exclude list.
- [ ] Audit the broader `tests/integration/` directory (all files except `.cli-threads/`) —
      check each file for real project URLs, internal commit SHAs, private repo references, or
      internal identifiers. The `tests/integration/.cli-threads/**` exclusion only covers the
      known conversation history directory; other integration fixtures may also contain real data.
- [ ] Check full `pyproject.toml` for all extras groups that reference private git URLs
      (confirm `graphiti`, `leanrag`, and any others like `bench`, `local`, `visualization`)
- [x] `src/**` grep for private GitHub URLs — run `grep -ri "github.com.*watercooler-cloud" src/`
      before initial sync. **Finding:** `diagnostic.py:106` contained
      `https://github.com/MostlyHarmless-AI/watercooler-cloud/docs/SETUP.md` (mixed-case; would
      NOT have been caught by the URL transform even if `src/**` were added to its `paths`).
      Fixed at source: URL changed to `https://github.com/mostlyharmless-ai/watercooler/docs/SETUP.md`.
      The dry-run CI gate also includes a grep step for belt-and-suspenders (see Phase 2.1).
- [x] `release.yml` audited — see findings note below.
- [ ] `tests/fixtures/` directory — audit all files for real project data. **Finding:**
      `tests/fixtures/threads/unified-branch-parity-protocol.md` is a real internal project thread
      (author: `Claude Code (caleb)`, topic: `unified-branch-parity-protocol`, created 2025-12-09).
      Added `tests/fixtures/threads/**` to the exclude block. `tests/fixtures/threads/cross_tier_test.md`
      appears synthetic (generic auth-system content, no real identifiers); safe to publish.
      Action required: replace `unified-branch-parity-protocol.md` with a synthetic fixture, or
      confirm the entire `tests/fixtures/threads/` directory is excluded (it is, per the exclude block).
- [x] `tests/tmp_threads/` — exists at repo root with `alpha.md` (Status: OPEN, Topic: alpha).
      Transient test output directory. Added to exclude block to prevent test-run artifacts from
      accumulating in the public repo.
- [ ] Note: `schemas/README.md` and other docs may contain the bare string `watercooler-cloud`
      (package name). The private GitHub URL form is rewritten by the URL transform. The bare
      name is a known cosmetic issue — deferred to the full-rename milestone. No action needed
      before initial sync.

**0.3 — Create GitHub App**

1. Go to `github.com/organizations/mostlyharmless-ai/settings/apps/new`
2. Name: `watercooler-copybara-sync`
3. Permissions:
   - **`watercooler-cloud`**: Contents: Read-only
   - **`watercooler`**: Contents: Read & write, Workflows: Read & write
4. Install the App on both `mostlyharmless-ai/watercooler-cloud` and `mostlyharmless-ai/watercooler`
5. Store as secrets on `watercooler-cloud` (private repo):
   - `COPYBARA_APP_ID` — App ID (integer, from App settings page)
   - `COPYBARA_APP_PRIVATE_KEY` — PEM-encoded private key (generated in App settings)

**0.4 — Create public repo**

1. Create `mostlyharmless-ai/watercooler` as a public repository (GitHub UI or `gh repo create`)
2. Set default branch to `main`
3. Do NOT add any initial commit (Copybara bootstrap will populate it)
4. Disable "allow squash merging" and "allow rebase merging" — force merge commits only
5. Add a placeholder README only if needed to unlock the repo UI

---

### Phase 1: Copybara Config & Transforms

**1.1 — Create `copy.bara.sky`**

Write the file at the repository root using the exact content in the Allowlist section above.

> `copy.bara.sky` must NOT appear in the allowlist — it reveals the private repo URL.

**1.2 — Create and maintain `pyproject.public.toml`**

`pyproject.public.toml` is the source of truth for the public package. It must be created in the
private repo alongside `pyproject.toml` and kept in sync as `pyproject.toml` evolves.

**Why not `core.replace`?** The private deps appear as quoted strings inside a TOML array.
Replacing the string content with a comment would produce `"# graphiti-core: ..."` — an invalid
PEP 508 requirement that breaks `pip install`. The file-level swap via `core.move` is the
correct approach.

**Initial creation checklist for `pyproject.public.toml`:**
- [ ] Start from a copy of `pyproject.toml`
- [ ] Change `name = "watercooler-cloud"` → `name = "watercooler"`
- [ ] Remove `memory`, `graphiti`, `leanrag` extras entirely (the whole TOML array block)
- [ ] Remove `[tool.setuptools.package-data]` `watercooler_mcp = ["scripts/*"]` entry
      (the `scripts/` directory is not in the public allowlist)
- [ ] Audit remaining extras (`local`, `bench`, `visualization`) for any other private git URLs
- [ ] Verify `pip install -e .` succeeds from the public version
- [ ] Verify `python -c "import watercooler; print(watercooler.__version__)"` returns a real
      version string (not `0.0.0-dev`) — the `core.replace` transform in `copy.bara.sky`
      rewrites `version("watercooler-cloud")` → `version("watercooler")` in `src/**`

**Ongoing maintenance:** When `pyproject.toml` changes (new deps, version bump, new extras),
update `pyproject.public.toml` in the same commit. A CI check that diffs the two files and
alerts on non-public-safe additions is worth adding in Phase 6.

**1.3 — Verify `ci.yml` transform**

- [ ] Confirm the `.[dev,graphiti]` install string exists in `ci.yml` and the transform will match
- [ ] Confirm `.[dev]` is a valid, working install after the `graphiti` extra is stripped

---

### Phase 2: Private Repo CI Workflows

**2.1 — Dry-run gate (`copybara-dry-run.yml`)**

Add `.github/workflows/copybara-dry-run.yml` to the private repo (NOT in the sync allowlist).

The gate works by statically validating `copy.bara.sky`'s `origin_files` allowlist against a
hardcoded forbidden-path list. This is more reliable than parsing Copybara's dry-run output
(whose format is undocumented and may change). The primary risk being guarded against is
someone accidentally modifying `copy.bara.sky` to add a forbidden path to the allowlist.

Also add `scripts/validate-copybara-allowlist.py` to the private repo (NOT in the allowlist):

```python
#!/usr/bin/env python3
"""Validates copy.bara.sky's origin_files against a forbidden list.

Parses the allowlist patterns from copy.bara.sky and fails if any
forbidden path or prefix is explicitly included.

Usage: python3 scripts/validate-copybara-allowlist.py
"""
import re
import sys

# Patterns that match the entire repo — never safe in an allowlist.
FORBIDDEN_BROAD = {"**", "*", ".", "./", "./**", "*/**"}

# The ONLY subtree globs (ending /**) approved for the include list.
# Any new "something/**" pattern must be added here after explicit security review.
# This prevents accidentally broad expansions (e.g. "scripts/**", ".github/workflows/**")
# from slipping past the validator even when they are not in FORBIDDEN_PREFIXES.
PERMITTED_SUBTREES = {
    "src/**",
    "tests/**",
    "docs/**",
    "schemas/**",
}

FORBIDDEN_EXACT = {
    "AGENTS.md", "CLAUDE.md", "Dockerfile", "nixpacks.toml",
    "Procfile", "railway.json", ".gitmodules", "uv.lock",
    "copy.bara.sky", "pyproject.toml",  # raw; sanitized version is pyproject.public.toml
}

FORBIDDEN_PREFIXES = [
    "dev_docs/", "todos/", "watercooler/", "external/",
    ".claude/", ".serena/", ".githooks/",
    "scripts/build_memory_graph", "scripts/index_", "scripts/migrate_",
    "scripts/check-mcp-servers", "scripts/cleanup-mcp-servers",
    "tests/benchmarks/", "tests/integration/.cli-threads/",
]

# The ONLY scripts/ exact paths approved for the include list.
# Any other scripts/xxx exact path would pass FORBIDDEN_PREFIXES (which only catches
# specific name prefixes, not arbitrary operator scripts like enrich_baseline_graph.py).
# A future maintainer adding an internal script as a "convenience" would pass unchallenged
# without this check.
PERMITTED_EXACT_SCRIPTS = {
    "scripts/install-mcp.sh",
    "scripts/install-mcp.ps1",
    "scripts/mcp-server-daemon.sh",
    "scripts/git-credential-watercooler",
}

# The ONLY non-/** wildcard patterns (containing * but not ending /**) that are permitted.
# Currently empty: the allowlist uses only exact paths or PERMITTED_SUBTREES /** globs.
# Patterns like ".github/workflows/*" or "*.md" pass FORBIDDEN_BROAD (which only blocks
# literal full-repo globs) and skip PERMITTED_SUBTREES (which only catches /**), but
# they still expose broad sets of files without explicit per-file review.
# Any such pattern requires explicit addition here after security review.
PERMITTED_GLOBS: set[str] = set()

# These patterns MUST be present in the exclude block — missing any is a hard failure.
# NOTE: This check verifies presence, not reachability. If the include list is restructured
# (e.g., tests/** split into subdirectory patterns), update REQUIRED_EXCLUDES accordingly.
# The security invariant is: these paths must never appear in the public repo.
REQUIRED_EXCLUDES = {
    "tests/benchmarks/**",
    "tests/integration/.cli-threads/**",
    "tests/.cli-threads/**",
    "tests/test_artifacts/**",
    "tests/fixtures/threads/**",
    "tests/tmp_threads/**",
    "src/watercooler_mcp/scripts/**",
}


def _extract_bracketed(text: str, pos: int) -> tuple[str, int]:
    """Return (content, end_pos) of balanced [...] starting at text[pos].

    Skips Starlark line comments (# to end-of-line) and respects string
    literals so a ] inside either does not prematurely decrement bracket depth.

    Without comment-awareness a ] in a comment such as:
        # old pattern: "tests/fixtures/**"]   <- stray ]
    would close the list early, silently truncate the captured patterns, and
    produce a false PASS.

    State machine:
      - inside '...': only ' exits; # and [ ] are inert.
      - inside "...": only " exits; # and [ ] are inert.
      - outside strings: # skips to end-of-line; [ increments depth; ] decrements.
    """
    assert text[pos] == "[", f"Expected '[' at pos {pos}, got {text[pos]!r}"
    depth = 0
    in_single = False   # inside '...'
    in_double = False   # inside "..."
    i = pos
    while i < len(text):
        ch = text[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "#":
            # Line comment — skip to newline; brackets inside don't count.
            while i < len(text) and text[i] != "\n":
                i += 1
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[pos + 1 : i], i
        i += 1
    raise ValueError("Unbalanced '[' in copy.bara.sky; cannot parse origin_files")


def parse_origin_files(sky_content: str) -> tuple[list[str], list[str]]:
    """Extract include and exclude pattern lists from the origin_files glob() call.

    Uses _extract_bracketed (bracket-depth counting) instead of a regex-based list
    extractor to avoid truncation at a stray ] inside a Starlark comment.

    Returns:
        Tuple of (include_patterns, exclude_patterns).
    """
    m = re.search(r"origin_files\s*=\s*glob\s*\(", sky_content)
    if not m:
        print("ERROR: Could not find 'origin_files = glob(' in copy.bara.sky", file=sys.stderr)
        sys.exit(1)
    # Find the '[' that opens the include list immediately after 'glob('
    inc_bracket = sky_content.index("[", m.end())
    include_content, inc_end = _extract_bracketed(sky_content, inc_bracket)
    # Look for exclude=[...] after the include list
    exc_m = re.search(r"exclude\s*=\s*\[", sky_content[inc_end:])
    if not exc_m:
        # No exclude block — include-only
        includes = re.findall(r"""["']([^"']+)["']""", include_content)
        return includes, []
    # exc_m.end() points past the '['; subtract 1 to get the '[' position in the slice,
    # then add inc_end to convert to an index in the full string.
    exc_bracket = inc_end + exc_m.end() - 1
    exclude_content, _ = _extract_bracketed(sky_content, exc_bracket)
    # Match both double-quoted ("pattern") and single-quoted ('pattern') Starlark strings.
    # Starlark allows both forms; matching only double quotes would silently miss single-quoted
    # patterns and produce a false PASS.
    includes = re.findall(r"""["']([^"']+)["']""", include_content)
    excludes = re.findall(r"""["']([^"']+)["']""", exclude_content)
    return includes, excludes


def include_could_reach(include_pattern: str, exclude_prefix: str) -> bool:
    """True if include_pattern could expose files under exclude_prefix.

    Uses ancestor/descendant matching on directory components, not just the first
    path component. The previous implementation — base = exclude_prefix.split("/")[0]
    — returned True for include="tests/integration/**" vs exclude="tests/benchmarks/**"
    because both share "tests/" as the first component. That is incorrect: a
    tests/integration/** include cannot expose tests/benchmarks/.

    Rules (after stripping trailing /** from /** patterns):
      - "**" or "*" reaches everything.
      - covered_dir reaches exclude_dir when:
          * they are equal (exact subtree match), OR
          * exclude_dir is a descendant of covered_dir (include is broader), OR
          * covered_dir is a descendant of exclude_dir (include is narrower but inside
            the excluded subtree — e.g. include="tests/benchmarks/slow/**" would
            still trigger the "tests/benchmarks/**" exclude).

    Used to distinguish "required exclude is genuinely needed" from
    "required exclude is now redundant because no include covers it".
    """
    if include_pattern in ("**", "*"):
        return True
    covered = include_pattern[:-3] if include_pattern.endswith("/**") else include_pattern
    excluded = exclude_prefix[:-3] if exclude_prefix.endswith("/**") else exclude_prefix
    return (
        covered == excluded
        or excluded.startswith(covered + "/")   # include is broader: tests/** → tests/benchmarks
        or covered.startswith(excluded + "/")   # include is narrower but inside: tests/benchmarks/slow/**
    )


def check(includes: list[str], excludes: list[str]) -> bool:
    ok = True
    for p in includes:
        if p in FORBIDDEN_BROAD:
            print(f"ERROR: Overly broad pattern '{p}' in include list — would sync entire repo")
            ok = False
        # Block non-/** wildcards not in PERMITTED_GLOBS.
        # FORBIDDEN_BROAD only catches literal full-repo globs (**, *, etc.).
        # A pattern like ".github/workflows/*" or "*.md" contains * but passes FORBIDDEN_BROAD
        # and is not caught by PERMITTED_SUBTREES (only /**). Such patterns expose broad sets
        # of files without per-file review. Reject unless explicitly added to PERMITTED_GLOBS.
        if "*" in p and not p.endswith("/**") and p not in FORBIDDEN_BROAD and p not in PERMITTED_GLOBS:
            print(f"ERROR: Non-subtree wildcard '{p}' is not permitted — "
                  "allowlist only accepts exact paths or approved /** subtrees; "
                  "add to PERMITTED_GLOBS after security review if intentional")
            ok = False
        if p.endswith("/**") and p not in PERMITTED_SUBTREES:
            print(f"ERROR: Subtree glob '{p}' is not in PERMITTED_SUBTREES — add after security review")
            ok = False
        for forbidden in FORBIDDEN_EXACT:
            if p == forbidden:
                print(f"ERROR: Forbidden exact path '{p}' in include list")
                ok = False
        for prefix in FORBIDDEN_PREFIXES:
            if p == prefix or p.startswith(prefix) or p == prefix.rstrip("/"):
                print(f"ERROR: Forbidden prefix '{p}' matches forbidden '{prefix}'")
                ok = False
        # Enforce that only the 4 approved scripts are allowed as exact scripts/ paths.
        # FORBIDDEN_PREFIXES only covers specific name prefixes — an arbitrary operator script
        # added as an exact path (e.g. "scripts/enrich_baseline_graph.py") would slip through.
        if p.startswith("scripts/") and not p.endswith("/**"):
            if p not in PERMITTED_EXACT_SCRIPTS:
                print(f"ERROR: Unpermitted script path '{p}' — add to PERMITTED_EXACT_SCRIPTS after security review")
                ok = False
    exclude_set = set(excludes)
    for required in REQUIRED_EXCLUDES:
        reachable = any(include_could_reach(inc, required) for inc in includes)
        if required not in exclude_set:
            if reachable:
                print(f"ERROR: Required exclusion '{required}' is missing and include list exposes this path")
                ok = False
            else:
                print(f"WARNING: Required exclusion '{required}' is missing but no include pattern reaches it (currently redundant)")
        else:
            # Exclude is present — also verify the include list actually reaches this path.
            # If no include covers it, the exclude is currently redundant. This may be fine,
            # but it can mask a narrowed include list: a maintainer who later removes the
            # exclude (seeing it as unnecessary) would expose the path if includes are
            # subsequently broadened again. Warn so maintainers can verify intent.
            if not reachable:
                print(f"WARNING: Required exclusion '{required}' is present but no current "
                      "include reaches it — exclude is redundant, or the include list is too narrow")
    return ok


if __name__ == "__main__":
    with open("copy.bara.sky") as f:
        content = f.read()
    includes, excludes = parse_origin_files(content)
    print(f"Checking {len(includes)} include patterns, {len(excludes)} exclude patterns ...")
    if check(includes, excludes):
        print("Allowlist validation passed.")
    else:
        print("Allowlist validation FAILED.")
        sys.exit(1)
```

```yaml
# .github/workflows/copybara-dry-run.yml
name: Copybara Allowlist Validation

on:
  pull_request:
    branches: [stable]
  # Also run on direct pushes to stable (belt-and-suspenders if branch protection is bypassed).
  # Branch protection requiring PR + this check as a required status is the primary gate;
  # this trigger ensures validation still fires even if protection is temporarily relaxed.
  push:
    branches: [stable]

jobs:
  validate-allowlist:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          # Full history required so git diff origin/<base> resolves correctly.
          # Default shallow clone (depth: 1) causes "bad revision" errors on the diff steps.
          fetch-depth: 0

      - name: Validate copy.bara.sky allowlist
        run: python3 scripts/validate-copybara-allowlist.py

      - name: Scan for secrets in files that will be synced
        uses: gitleaks/gitleaks-action@v2
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Confirm pyproject.public.toml is updated when pyproject.toml changes
        # Only meaningful on pull_request where github.base_ref is set.
        # On push events (direct-to-stable), github.base_ref is empty and the diff would fail.
        if: github.event_name == 'pull_request'
        run: |
          TOML_CHANGED=$(git diff origin/${{ github.base_ref }} -- pyproject.toml | wc -l)
          PUBLIC_CHANGED=$(git diff origin/${{ github.base_ref }} -- pyproject.public.toml | wc -l)
          if [ "$TOML_CHANGED" -gt 0 ] && [ "$PUBLIC_CHANGED" -eq 0 ]; then
            echo "ERROR: pyproject.toml changed but pyproject.public.toml was not updated."
            echo "Update pyproject.public.toml to strip any new private dependencies before merging."
            exit 1
          fi

      - name: Scan pyproject.public.toml for private git dependency URLs
        run: |
          # Match "git+https://github.com/mostlyharmless-ai/" — private pip dep pattern.
          # Do NOT match bare "mostlyharmless-ai/" which also appears in [project.urls]
          # (Homepage, Repository) and is safe to publish.
          if grep -q "git+https://github.com/mostlyharmless-ai/" pyproject.public.toml; then
            echo "ERROR: pyproject.public.toml contains private git dependency URLs:"
            grep "git+https://github.com/mostlyharmless-ai/" pyproject.public.toml
            exit 1
          fi
          echo "pyproject.public.toml: no private git dependency URLs found."

      - name: Scan src/ for private GitHub URLs
        run: |
          # The URL transform only covers tests/schemas/docs — not src/. Any hardcoded
          # github.com/..../watercooler-cloud URL in src/** would be published verbatim.
          # Case-insensitive: catches MostlyHarmless-AI/watercooler-cloud (mixed case).
          if grep -ri "github\.com[^\"']*watercooler-cloud" src/; then
            echo "ERROR: Private repo URL found in src/**. Fix at source or add a src/** transform."
            exit 1
          fi
          echo "src/: no private GitHub URLs found."
```

**2.2 — Publish workflow (`copybara-publish.yml`)**

Add `.github/workflows/copybara-publish.yml` to the private repo (NOT in the sync allowlist).

> **Action choice:** `google/copybara-action` does not exist as a standalone GitHub Action with
> a stable contract. The correct action is `Olivr/copybara-action`, which uses the
> `olivr/copybara` Docker image internally. It requires `ssh_key` as a mandatory input —
> the easiest alternative that avoids managing an additional SSH deploy key is to run the
> Copybara Docker image directly with HTTPS credentials from the GitHub App token.
> The workflow below uses the Docker approach. Pin `Olivr/copybara-action@v1.2.5`
> (SHA: `042b439`) if you prefer the action wrapper and can provision an SSH deploy key.

```yaml
# .github/workflows/copybara-publish.yml
# Syncs watercooler-cloud@stable → mostlyharmless-ai/watercooler@main on each push to stable.
name: Copybara Publish

on:
  push:
    branches: [stable]
  workflow_dispatch:
    inputs:
      force_bootstrap:
        description: 'Pass --force to Copybara (use after rollback or re-bootstrap)'
        type: boolean
        default: false

permissions:
  contents: read
  issues: write  # required for gh issue create in failure alerting step

concurrency:
  group: copybara-publish
  # false = queue; GitHub allows at most ONE pending run alongside a running one.
  # If 3+ pushes arrive rapidly, only the most-recent queued run will execute
  # after the current one completes — intermediate pushes are collapsed.
  # This is accepted: public@main always reflects the latest stable state.
  cancel-in-progress: false

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Guard against -dev version on stable
        run: |
          # Use tomllib (Python 3.11+ stdlib; tomli backport for 3.10) for reliable TOML parsing.
          # sed-based extraction is fragile: fails on single-quoted or unquoted TOML values.
          VERSION=$(python3 -c "
          try:
              import tomllib
          except ImportError:
              import tomli as tomllib
          with open('pyproject.toml', 'rb') as f:
              d = tomllib.load(f)
          print(d['project']['version'])
          ")
          if echo "$VERSION" | grep -q "\-dev"; then
            echo "ERROR: stable branch contains a -dev version ($VERSION). Aborting sync."
            exit 1
          fi
          echo "Version $VERSION is a release version. Proceeding."

      - name: Generate GitHub App Token
        id: app-token
        uses: actions/create-github-app-token@v1
        with:
          app-id: ${{ secrets.COPYBARA_APP_ID }}
          private-key: ${{ secrets.COPYBARA_APP_PRIVATE_KEY }}
          owner: mostlyharmless-ai
          repositories: "watercooler-cloud,watercooler"

      - name: Configure git credentials for Copybara
        env:
          TOKEN: ${{ steps.app-token.outputs.token }}
        run: |
          # Write credentials to file — avoids token appearing in process args or shell history
          echo "https://x-access-token:${TOKEN}@github.com" > "$HOME/.git-credentials"
          git config --global credential.helper store
          chmod 600 "$HOME/.git-credentials"

      - name: Run Copybara Sync
        run: |
          FORCE_FLAG=""
          if [ "${{ inputs.force_bootstrap }}" = "true" ]; then
            FORCE_FLAG="--force"
          fi
          docker run --rm \
            -v "$HOME/.git-credentials:/root/.git-credentials:ro" \
            -v "$HOME/.gitconfig:/root/.gitconfig:ro" \
            -v "$GITHUB_WORKSPACE:/usr/src/app" \
            -w /usr/src/app \
            olivr/copybara:1.2.5 \
            copybara copy.bara.sky default $FORCE_FLAG

      - name: Clean up credentials
        if: always()
        run: rm -f "$HOME/.git-credentials"

      - name: Notify on failure
        # This step IS the failure alerting mechanism. The `issues: write` permission in the
        # workflow's `permissions:` block (above) enables this step. No additional hardening
        # task is needed — failure alerting is fully implemented here.
        if: failure()
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh issue create \
            --repo mostlyharmless-ai/watercooler-cloud \
            --title "Copybara publish failed at ${{ github.sha }}" \
            --body "Automated sync to public repo failed. [View run](${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }})"
```

---

### Phase 3: Public Repo Setup (`mostlyharmless-ai/watercooler`)

**3.1 — Destination guard workflow**

Create `.github/workflows/check-forbidden-paths.yml` in the PUBLIC repo
(this file will not be in the private repo and must be added manually or via GitHub UI).

```yaml
# .github/workflows/check-forbidden-paths.yml (in public repo only)
# Defense-in-depth: fails if any forbidden path appears in the public repo.
# This runs AFTER Copybara has already pushed; it cannot prevent leakage,
# but it catches regressions and provides an audit signal.
name: Forbidden Path Guard

on:
  push:
    branches: [main]

jobs:
  check-forbidden:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Check for forbidden files and directories
        run: |
          # Root-level forbidden paths (only expected at repo root)
          FORBIDDEN_ROOT=(
            "AGENTS.md" "CLAUDE.md" "todos" "dev_docs" "watercooler"
            ".gitmodules" "Dockerfile" "nixpacks.toml" "Procfile" "railway.json"
            ".claude" ".serena" ".githooks" "uv.lock" "copy.bara.sky"
          )
          # Forbidden names that may appear nested (checked recursively via find)
          FORBIDDEN_NAMES=(
            ".cli-threads"
            "test_artifacts"
          )
          # Forbidden paths at specific repo-relative locations (mirrors REQUIRED_EXCLUDES in
          # validate-copybara-allowlist.py). Unlike FORBIDDEN_NAMES which match by basename,
          # these are checked at their exact expected location — leakage under a different parent
          # would still be caught by the allowlist validator before publish.
          FORBIDDEN_PATHS=(
            "tests/benchmarks"
            "tests/fixtures/threads"
            "tests/tmp_threads"
            "src/watercooler_mcp/scripts"
          )
          FOUND=0
          for f in "${FORBIDDEN_ROOT[@]}"; do
            if [ -e "$f" ]; then
              echo "ERROR: Forbidden root path found: $f"
              FOUND=1
            fi
          done
          for name in "${FORBIDDEN_NAMES[@]}"; do
            results=$(find . -name "$name" -not -path "./.git/*" 2>/dev/null)
            if [ -n "$results" ]; then
              echo "ERROR: Forbidden nested path found: $results"
              FOUND=1
            fi
          done
          for fp in "${FORBIDDEN_PATHS[@]}"; do
            if [ -e "$fp" ]; then
              echo "ERROR: Forbidden repo-relative path found: $fp"
              FOUND=1
            fi
          done
          if [ $FOUND -eq 1 ]; then
            echo "SECURITY: Forbidden content detected. Investigate immediately."
            exit 1
          fi
          echo "No forbidden paths found. Public repo content is clean."
```

**3.1b — Branch protection on private `stable` (required)**

The publish workflow fires on every `push` to `stable`. Without branch protection on `stable`,
someone can push directly and trigger publish without the dry-run gate ever running — the gate
only fires on `pull_request` events (plus as a belt-and-suspenders trigger on push, but that
runs in parallel with publish and cannot block it).

Configure in the **private** repo's GitHub UI (or via `gh api`):
- **Enable "Require a pull request before merging"** — forces all changes through a PR review
  - "Required approvals": 1 (or 0 for solo projects, relying solely on required status checks)
- **Enable "Require status checks to pass before merging"** — add the required check:
  - `validate-allowlist` (the job name in `copybara-dry-run.yml`)
  - Without this, branch protection prevents direct push but doesn't enforce the dry-run gate
- **Do not allow force pushes** (except for emergency rollback — requires temp disable)
- **Allow push** for the GitHub App or bot account used for automated merges (if any)

> **Why this is the primary gate:** `github.base_ref` on push events is empty — the pyproject
> diff step is PR-only. The allowlist validation, gitleaks, and src/ URL scan do run on push
> (as of the `push: branches: [stable]` trigger added in Phase 2.1), but they run *in parallel*
> with publish. Branch protection is the only mechanism that can *block* a push before publish
> fires. The push trigger is belt-and-suspenders for cases where protection is temporarily relaxed.

**3.2 — Branch protection on public `main`**

> **Important:** The `check-forbidden-paths` workflow is a **post-push audit**, not a pre-push
> gate. It runs AFTER Copybara has already pushed. "Require status checks" applies to PRs and
> cannot block a direct App push — enabling it alongside a direct-push bypass would make the
> check non-enforcing. The dry-run gate in the private repo (Phase 2.1) is the actual prevention
> layer; the destination guard provides a detection and alerting signal only.

Configure in GitHub UI (or via `gh api`):
- **Enable "Restrict who can push to matching branches"** — limit to:
  - The `watercooler-copybara-sync` GitHub App installation
  - Org owners (for emergency direct access)
  - Without this, any org member with `Write` on the public repo can push directly,
    bypassing the Copybara pipeline entirely.
- **Allow direct push** from the GitHub App (add App to the push-allowed list above)
- Do NOT enable "Require a pull request before merging" — Copybara pushes directly
- Do NOT enable "Require status checks to pass before merging" — incompatible with direct App push
- **Do** enable: "Do not allow force pushes" (except for emergency rollback — requires temp disable)

**3.3 — Enable GitHub Releases**

Public `release.yml` (synced from allowlist) creates GitHub Releases on `v*` tags.
For now, tags are pushed manually to the public repo after each private release (see Phase 4).

---

### Phase 4: Bootstrap Procedure (one-time runbook)

The public repo must be bootstrapped before the automated workflow can run. Copybara cannot push
to a completely empty repo without `--force`.

**Runbook: First Copybara sync**

```bash
# Prerequisites: Docker available locally, GitHub App installation token ready

# 1. Store the App installation token in an env var — never put it in a command argument
#    (shell history and `ps aux` would expose it otherwise)
export COPYBARA_TOKEN="<App installation token from GitHub App settings>"

# 2. Write credentials file and a Docker-scoped gitconfig — do NOT touch ~/.gitconfig
#    Setting global credential.helper would clobber any existing user configuration.
echo "https://x-access-token:${COPYBARA_TOKEN}@github.com" > ~/.git-credentials-copybara
chmod 600 ~/.git-credentials-copybara
cat > /tmp/copybara-gitconfig << 'GITCONFIG'
[credential]
	helper = store --file=/root/.git-credentials
GITCONFIG

# 3. Run the first sync via Docker (same image as CI)
#    --force bootstraps the public repo from the current stable HEAD
#    Mount the scoped gitconfig (not ~/.gitconfig) to avoid polluting user git config.
docker run --rm \
  -v "$HOME/.git-credentials-copybara:/root/.git-credentials:ro" \
  -v "/tmp/copybara-gitconfig:/root/.gitconfig:ro" \
  -v "$(pwd):/usr/src/app" \
  -w /usr/src/app \
  olivr/copybara:1.2.5 \
  copybara copy.bara.sky default --force

# 4. Clean up — no global git config was modified, nothing to unset
rm -f ~/.git-credentials-copybara /tmp/copybara-gitconfig

# 5. Verify the public repo contents
gh browse --repo mostlyharmless-ai/watercooler

# 6. Run the destination guard check manually
cd /tmp && git clone https://github.com/mostlyharmless-ai/watercooler.git wc-public
ls wc-public/  # confirm allowlist content looks right
# Spot-check for forbidden paths:
for f in AGENTS.md CLAUDE.md todos dev_docs .gitmodules uv.lock copy.bara.sky; do
  [ -e "wc-public/$f" ] && echo "ERROR: $f present!" || echo "OK: $f absent"
done

# 7. Enable the copybara-publish.yml workflow in the private repo (if disabled)
# 8. Push a no-op commit to stable to trigger the first automated sync
```

**Rollback runbook (if leakage reaches the public repo)**

> Git history is permanent for anyone who has already cloned or indexed the repo. A response
> plan must be executed quickly.

1. **Immediately**: Disable the `copybara-publish.yml` workflow (GitHub UI → Actions → Disable)
2. **Communicate immediately**: Post a public notice on `mostlyharmless-ai/watercooler` (GitHub
   issue or security advisory) acknowledging a history rewrite is pending. Users who cloned
   before the fix should be notified to re-clone.
3. **Assess scope**: `git log --oneline --all` on the public repo — identify leaked commits
4. **Disable branch protection**: In GitHub UI, uncheck "Do not allow force pushes" on
   `mostlyharmless-ai/watercooler@main` (required before force-push will succeed)
4b. **Prepare the rollback state locally**:
   ```bash
   git clone https://github.com/mostlyharmless-ai/watercooler.git /tmp/wc-rollback
   cd /tmp/wc-rollback
   # Identify the leaking commit(s) — look for the SHA that introduced the bad content
   git log --oneline -20
   # Reset to the last clean commit (the one BEFORE the bad push)
   git reset --hard <last-clean-sha>
   # Verify the leaked content is gone
   ls  # confirm forbidden paths (AGENTS.md, dev_docs, todos, etc.) are absent
   ```
5. **Force-push** (from the `/tmp/wc-rollback` clone prepared above):
   ```bash
   git push --force origin main  # rewrite history to remove leaked content
   ```
6. **Re-enable branch protection**: Re-check "Do not allow force pushes" on public `main`
7. **Root cause**: Identify which Copybara transform failed and fix it in `copy.bara.sky`
8. **Fix dry-run gate**: Add the missed pattern to the forbidden-path check
9. **Re-enable workflow** and re-bootstrap using `workflow_dispatch` with `force_bootstrap: true`
   (avoids a no-op commit to `stable`)

---

### Phase 5: Release Tag Coordination (deferred, but documented)

Currently, tags are pushed manually to the public repo after each private release:

```bash
# After tagging stable@v0.3.0 on the private repo and the Copybara sync completes:
gh release create v0.3.0 --repo mostlyharmless-ai/watercooler \
  --title "v0.3.0" \
  --generate-notes
```

**Future automation (deferred):** Add a step to `copybara-publish.yml` that creates a matching
annotated tag on the public repo when the private `stable` branch receives a release tag. This
requires the workflow to detect whether the triggering push is a tag push or a branch push and
act accordingly.

---

### Phase 6: Hardening & Monitoring

**6.1 — `scripts/README.md` disposition**

`scripts/README.md` is NOT in the allowlist (correctly excluded). No action needed for the sync.
The 4 user-facing scripts (`install-mcp.sh`, `install-mcp.ps1`, `mcp-server-daemon.sh`,
`git-credential-watercooler`) are self-documenting; users are directed to them from
`docs/INSTALLATION.md` and `docs/AUTHENTICATION.md`.

**6.2 — Allowlist audit cadence**

Add a quarterly reminder (or a `branch-pairing-audit.yml`-style scheduled workflow) to:
- List all top-level directories in `stable` that are NOT covered by the allowlist
- Confirm each is intentionally excluded (internal) or should be added (new user-facing feature)

**6.3 — Secret scanning**

Enable **GitHub Advanced Security** (secret scanning) on the public repo. This catches
credential patterns that slip through transform errors. Configure:
- Alert on any push containing AWS credentials, API key patterns, or private GitHub URLs
- Set up email/Slack notifications for scan failures

**6.4 — (Completed in Phase 2.2)**

Failure alerting is fully implemented by the "Notify on failure" step in `copybara-publish.yml`
(Phase 2.2). The `issues: write` permission in that workflow's `permissions:` block enables
`gh issue create` on failure. No additional work needed here.

---

## Alternative Approaches Considered

| Approach | Why Rejected |
|---|---|
| **Denylist (`glob(["**"], exclude=[...])`** | Fragile: requires perfect enumeration of all internal files. Leaks new internal files by default. Correctly rejected in security review. |
| **Personal Access Token (PAT)** | User-account dependency; no auto-rotation; coarser permission scope. GitHub App is strictly better. |
| **Squash with `squash_notes`** | Leaks internal author identities in public commit message bodies (`show_author=True`). Removed to avoid identity exposure. |
| **Manual release publishing** | Error-prone; inconsistent; does not scale as release frequency increases. |
| **Separate `-public` branch in private repo** | Adds operational complexity; still requires the same transform logic; Copybara is a better abstraction. |

---

## Acceptance Criteria

### Functional Requirements

- [ ] Push to `stable` triggers Copybara sync to `mostlyharmless-ai/watercooler@main`
- [ ] Only allowlisted paths appear in the public repo (verified by destination guard)
- [ ] `pyproject.toml` in public repo has package name `watercooler`, no private git URLs
- [ ] `ci.yml` in public repo installs `.[dev]` (not `.[dev,graphiti]`)
- [ ] `uv.lock`, `copy.bara.sky`, `dev_docs/`, `todos/`, `.gitmodules` are absent from public repo
- [ ] Public commit messages read "Watercooler release sync" with no internal author names
- [ ] Rapid successive pushes to `stable` are handled: at most one run executes while one is queued;
      if N>2 pushes arrive, only the most-recent queued run executes next (intermediate are collapsed).
      The public repo always reflects the latest stable state after the queue drains.
- [ ] PR to `stable` that would leak forbidden content fails CI (dry-run gate)
- [ ] Push to `stable` with `-dev` version in `pyproject.toml` aborts the publish workflow

### Non-Functional Requirements

- [ ] GitHub App token (not PAT) — scoped to read `watercooler-cloud` + write `watercooler`
- [ ] No user-account dependency for authentication
- [ ] Bootstrap runbook documented and executable by any maintainer
- [ ] Rollback runbook documented with step-by-step instructions
- [ ] Failure alerting: pipeline failures result in a GitHub issue on the private repo
- [ ] Destination guard CI in public repo detects and alerts on any forbidden path (post-push signal; does not prevent the push — the dry-run gate is the prevention layer)

### Quality Gates

- [ ] Pre-flight audits completed (server.py, main.py, fastmcp.json, tests/integration/.cli-threads/)
      — `src/watercooler_mcp/scripts/` ✅ audited; unconditional exclusion already in `copy.bara.sky`
- [ ] Manual dry-run executed locally before enabling the automated workflow
- [ ] First automated sync verified against checklist of forbidden paths
- [ ] GitHub Advanced Security (secret scanning) enabled on public repo

---

## Risk Analysis

| Risk | Severity | Mitigation |
|---|---|---|
| Internal file leaked to public repo | Critical | Allowlist posture; dry-run PR gate (primary); destination guard (secondary); secret scanning |
| Private git dep URL survives transform | High | File-level swap: `pyproject.public.toml` (maintained without private URLs) replaces `pyproject.toml` via `core.move`. CI hard-fails if `pyproject.public.toml` contains any `mostlyharmless-ai` git URL (Phase 2.1 grep step). |
| `tests/integration/.cli-threads/` fixtures contain real conversation data | High | Audit fixtures before sync; add to exclude list if real data found |
| `src/watercooler_mcp/scripts/` contains internal scripts | High | ✅ Resolved: confirmed present; unconditional `"src/watercooler_mcp/scripts/**"` exclusion added to `copy.bara.sky` |
| Copybara merge conflict stalls pipeline | Medium | Define manual resolution runbook; provide `workflow_dispatch` escape hatch |
| GitHub Actions concurrency drops intermediate pushes | Low | Documented; accepted; only the last queued push syncs |
| GitHub App private key exposure | High | Store as encrypted secret; rotate on any suspected compromise |
| `uv.lock` exposes private SHA | High | Excluded from allowlist |
| `copy.bara.sky` accidentally included in allowlist | Critical | File not in allowlist by design; verify after each update to `copy.bara.sky` |

---

## Open Questions (Remaining)

| # | Question | Disposition |
|---|---|---|
| Q1 | Destination repo name | ✅ `mostlyharmless-ai/watercooler` |
| Q2 | Package rename | ✅ Rename to `watercooler` via `pyproject.public.toml` maintained in private repo + `core.move` to rename it in destination |
| Q3 | Private pip deps | ✅ Remove graphiti/leanrag/memory extras entirely — no stubs (whole TOML array block deleted from pyproject.public.toml) |
| Q4 | `server.py` / `main.py` audit | Pre-flight audit required (Phase 0.2) |
| Q5 | `fastmcp.json` audit | Pre-flight audit required (Phase 0.2) |
| Q6 | Release tagging | Manual for now; automation deferred to Phase 5 |
| Q7 | Changelog | GitHub Releases auto-notes; `CHANGELOG.md` deferred |
| Q8 | `scripts/README.md` | Excluded (not in allowlist) |
| Q9 | `uv.lock` | Excluded from allowlist |
| Q10 | `tests/integration/.cli-threads/` content | Pre-flight audit required (Phase 0.2) |
| Q11 | `src/watercooler_mcp/scripts/` existence | ✅ Confirmed present; unconditionally excluded via `"src/watercooler_mcp/scripts/**"` in `copy.bara.sky` |

---

## Success Metrics

- Zero internal files published to `mostlyharmless-ai/watercooler` after the first 30 days
- Zero manual interventions required for routine `stable` → public sync
- PR dry-run gate catches at least one leakage attempt in testing (validates the gate works)
- Public repo `pip install watercooler` succeeds without error

---

## Dependencies & Prerequisites

- [ ] `mostlyharmless-ai/watercooler` public repo created
- [ ] GitHub App `watercooler-copybara-sync` created and installed on both repos
- [ ] `COPYBARA_APP_ID` and `COPYBARA_APP_PRIVATE_KEY` secrets stored on `watercooler-cloud`
- [ ] Pre-flight audits completed (server.py, fastmcp.json, test fixtures)
      — `src/watercooler_mcp/scripts/` ✅ audited; excluded unconditionally
- [ ] `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md` at repo root confirmed public-safe ✅
- [ ] Docker available on GitHub Actions runners (default ubuntu-latest: yes)
- [ ] `olivr/copybara:1.2.5` Docker image accessible (public DockerHub image; version tag matching `Olivr/copybara-action@v1.2.5` — stable Copybara release)
  - **Note on image pinning:** `olivr/copybara:1.2.5` uses a mutable tag. Similarly, the
    GitHub Actions in the workflows use floating semver tags (`@v4`, `@v2`, `@v1`). These are
    accepted risks for this internal-only private-repo pipeline: supply-chain risk is lower than
    for a widely-distributed open-source project, and the pipeline only runs on a push to `stable`.
    If pinning is desired in the future: replace the Docker tag with a digest
    (`olivr/copybara@sha256:<digest>`) and replace `actions/create-github-app-token@v1` with
    its full SHA (highest-risk action — it handles `COPYBARA_APP_PRIVATE_KEY`).
- [ ] If using `Olivr/copybara-action@v1.2.5` instead of Docker directly: SSH deploy key
      generated, public key added to both repos, private key stored as `COPYBARA_SSH_KEY` secret

---

## Documentation Plan

Files to update after implementation:

- `docs/INSTALLATION.md` — reference the public `pip install watercooler` command
- `dev_docs/CONTRIBUTING.md` — add "Public release sync" section explaining the Copybara pipeline
- Root `CONTRIBUTING.md` — already references public repo URL; review for accuracy post-launch
- Add `docs/plans/copybara-runbooks.md` — bootstrap + rollback runbooks in permanent form

---

## References

### Internal References

- Brainstorm: `dev_docs/brainstorms/2026-02-26-copybara-public-release-sync-brainstorm.md`
- Release workflow (private): `.github/workflows/release.yml`
- CI workflow (private): `.github/workflows/ci.yml`
- Branch strategy & release process: `dev_docs/CONTRIBUTING.md`
- Contributing guide (public): `CONTRIBUTING.md`
- Security policy: `SECURITY.md`

### External References

- [Copybara documentation](https://github.com/google/copybara)
- [Olivr/copybara-action v1.2.5](https://github.com/Olivr/copybara-action/releases/tag/v1.2.5) — GitHub Actions wrapper (requires `ssh_key`; alternative to Docker-direct approach)
- [olivr/copybara Docker image](https://hub.docker.com/r/olivr/copybara) — Docker image used directly in workflows
- [actions/create-github-app-token](https://github.com/actions/create-github-app-token) — App token minting
- [GitHub App token docs](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app)
- [GitHub Actions concurrency](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/control-the-concurrency-of-workflows-and-jobs)
