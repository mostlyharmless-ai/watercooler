---
name: watercooler-onboarding
description: Bootstrap Watercooler memory for a repository by inspecting local code, docs, CI, git history, and existing Watercooler threads, then writing a small set of durable, provenance-backed seed threads that future agents can query and extend. Use when entering a repo for the first time, seeding a repo with Watercooler context, or refreshing foundational repository context.
allowed-tools:
  - Bash
  - Glob
  - Grep
  - Read
  - Skill
  - Task
  - ToolSearch
  - WebFetch
  - WebSearch
  - mcp__watercooler__watercooler_health
  - mcp__watercooler__watercooler_roles
  - mcp__watercooler__watercooler_list_threads
  - mcp__watercooler__watercooler_read_thread
  - mcp__watercooler__watercooler_list_thread_entries
  - mcp__watercooler__watercooler_get_thread_entry
  - mcp__watercooler__watercooler_pulse_snapshot
  - mcp__watercooler__watercooler_smart_query
  - mcp__watercooler__watercooler_daemon_findings
  - mcp__watercooler__watercooler_search
  - mcp__watercooler__watercooler_say
  - mcp__watercooler__watercooler_annotations
---

# Watercooler Repository Bootstrap

Create durable Watercooler seed context for a repository.

Arguments: $ARGUMENTS

Default behavior is **Bootstrap**: inspect the repository and write a bounded set of
Watercooler entries. Read-only output exists only as a dry run:

- `dry-run`, `preview`, `read-only`, or `orient`: do not write; print the exact seed
  entries that would be written
- `refresh`: inspect existing seed threads and write additive refresh entries instead of
  trying to replace prior context
- `--update-agent-context` (or `update-agent-context`): after seeds are written and
  Step 5.5 verifies the `onboarding` tag landed, chain into the
  `update-agent-context` skill (Phase 1) so `CLAUDE.md` / `AGENTS.md` reflect the
  freshly-seeded threads. Existing files are backed up first (see Step 5.6).
  No-op in dry-run mode and when Step 5.5 has unresolved tag failures. Without
  the flag, Step 6 prints the equivalent command as a recommendation instead.
- research pre-pass (**default-on**): before deep-history and the seeds, run the **Step 2.3
  research pre-pass** — harvest the subject repo's external references (papers, source links)
  from the README + docs into an `onboarding-biblio` thread, and when a principal source paper
  exists, fetch + parse its bibliography and pull the salient secondary references in too.
  Auto-skips the network steps under `no-biblio` / `no-github` / `local-only` / `offline` and in
  dry-run (prints planned entries, writes nothing); the offline harvest of explicit README links
  still runs. Detail in `references/research-prepass.md`.
- `deep-history` (opt-in): after the research pre-pass, run the **Step 2.4 deep-history /
  PR-reasoning layer** — mine PR history for abandoned/superseded approaches and write the
  `history-*` threads. Off by default (forge/PR mining is expensive); honors dry-run (prints
  planned findings, writes nothing). Detail in `references/deep-history.md`.
- role hints (`implementer`, `planner`, `critic`, `tester`, `pm`, `scribe`) shift the
  recommended entry path and risk emphasis
- any other text is extra prioritization context

Do not produce a standalone read-only summary as the final artifact. A summary is not
repository memory. The useful artifact is a small set of typed, sourced Watercooler
entries.

> Interpretation guidance, anti-laundering rules, and provenance standards live in
> `references/thesis.md`. Load it when claims are uncertain or inferred.

---

## Step 0: System health, identity, and write safety

Load and run in parallel:

```
ToolSearch: select:mcp__watercooler__watercooler_health,mcp__watercooler__watercooler_roles
```

Call:

- `watercooler_health(code_path=".")`
- `watercooler_health(code_path=".", detail="identity")`
- `watercooler_roles(code_path=".")`

If health fails or reports write-path degradation:

1. Report the failed operation and likely cause.
2. Continue with local inspection only.
3. Print a dry-run seed plan.
4. Do not call `watercooler_say`.

If identity is unknown:

1. Continue with local inspection.
2. Print a dry-run seed plan.
3. Tell the user to set identity or supply a valid per-call `agent_func`.
4. Do not write.

For any write, supply an explicit `agent_func` that matches the actual caller and role.
Do not hardcode a different platform or model. Examples:

- `Codex:gpt-5:planner`
- `Codex:gpt-5:scribe`
- `Claude Code:sonnet-4:tester`

Every written body must begin with `Spec: <spec>`.

---

## Step 1: Establish repo anchors

```bash
pwd
git remote get-url origin 2>/dev/null || echo "no remote"
git branch --show-current
git log --oneline -1
git status --short
```

Record:

- repo root / `code_path`
- remote origin, if present
- current branch
- current commit
- dirty worktree summary

These values go into every seed entry's provenance section. Mark branch-local claims
explicitly.

---

## Step 2: Local-first repository inspection

Step 2 is where most quality is gained or lost. File *discovery* (listing) is not the same as file *reading*. The recipes in 2a–2e are discovery aids; the mandatory reads in 2.0 must run alongside them.

### Step 2.0: Mandatory reads (when present)

Before writing any seed entry, READ — not just list — the files that match the universal patterns below, when they exist. Absence is informative; note it but do not fail on it.

This list is **intentionally minimal**. Each entry earns its slot by being a high-density signal that weak models otherwise miss. Future additions need justification — extending the list reflexively defeats the universality goal.

Universal MUST-READ patterns (stack-agnostic):

- `README*` at the repo root and every top-level package.
- The main entry file of every top-level package — examples by stack: `src/index.*`, `src/main.*`, `cmd/*/main.*`, package `__init__.py`, `lib/*.ex` `application` module, `app/*.rb`.
- Every CI workflow file under `.github/workflows/`, or its equivalent (`.gitlab-ci.yml`, `.circleci/config.yml`, `.buildkite/**`, `azure-pipelines.yml`, `Jenkinsfile`).
- Every package / build manifest at root and in package roots (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle*`, `Gemfile`, `mix.exs`).
- Every release / distribution manifest at root and in package roots (`Dockerfile`, `Containerfile`, `server.json`, `manifest.json`, `*.mcpb` manifests, Helm `Chart.yaml`).
- Every release-config file (`.changeset/config.json`, `.goreleaser.yaml`, `release-please-config.json`, `.bumpversion.cfg`).
- Every source file matching common security-sensitive name patterns: `*encryption*`, `*jwt*`, `*auth*`, `*crypto*`, `*security*`, `*token*`.
- Top-level governance files: `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `CODEOWNERS`.
- Top-level positioning artifacts when present: `*THESIS*`, `ROADMAP*`, `VISION*`, `*MANIFESTO*`, `dev_docs/THE_*`, `docs/PHILOSOPHY*`, `docs/PRODUCT*`, `docs/OVERVIEW*`. These carry product framing that the engineering surfaces alone cannot supply, and they are the primary evidence base for `product-charter` and `overview` seeds.

A run that skips these produces visibly empty Provenance sections downstream. The MUST-READ list is the single biggest determinant of seed-entry depth.

### Step 2.1: Discovery recipes

Use the bash recipes below in parallel for breadth and corroboration. Discovery without reading is not Step 2 — every recipe below is a setup for the reads above, not a substitute for them.

### 2a. Structure and entrypoints

```bash
ls -1
rg --files -g 'README*' -g 'docs/**' -g 'src/**' -g 'tests/**' -g 'pyproject.toml' -g 'package.json' -g 'Cargo.toml' -g 'Makefile' -g 'justfile' -g 'Taskfile*' | head -200
```

Read the primary README and top-level config files. Identify:

- repository purpose and language stack
- package/module boundaries
- CLI or service entrypoints
- public APIs or exported modules
- important generated or derived files

### 2b. Build, scripts, and developer experience

Inspect package scripts and common commands from:

- `pyproject.toml`, `setup.cfg`, `tox.ini`, `noxfile.py`
- `package.json`, `pnpm-workspace.yaml`, `turbo.json`
- `Makefile`, `justfile`, `Taskfile*`
- Docker and compose files

Capture install, lint, typecheck, test, and run commands when discoverable.

### 2c. Tests and validation surface

```bash
rg --files tests src -g 'test_*.py' -g '*_test.py' -g '*.spec.*' -g '*.test.*' 2>/dev/null | head -200
rg -n "pytest|unittest|mypy|ruff|black|coverage|playwright|vitest|jest|cargo test|go test" pyproject.toml setup.cfg tox.ini noxfile.py package.json Makefile justfile Taskfile* .github/workflows 2>/dev/null
```

Identify:

- test framework and test layout
- CI gates
- coverage or missing validation areas
- commands future agents should run before handoff

### 2d. Docs, contracts, and release flow

```bash
rg --files docs README* SECURITY* CHANGELOG* RELEASE* CONTRIBUTING* .github/workflows 2>/dev/null | head -200
rg -n "OpenAPI|schema|contract|API|release|version|changelog|security|migration|deprecation" docs README* pyproject.toml package.json .github/workflows 2>/dev/null
```

Identify docs that must stay synchronized with code: API docs, generated schemas,
configuration references, MCP/tool docs, release notes, and security policy.

### 2e. Recent churn and branch scope

```bash
git log --oneline -20 --name-only --diff-filter=AMR 2>/dev/null
git log --oneline main..HEAD 2>/dev/null | head -20
git diff --stat main...HEAD 2>/dev/null
```

Use git history as change evidence only. Do not infer rationale unless a commit message
states it directly.

### Step 2.2: GitHub history layer (when GitHub source repo is resolvable)

After Step 2.0 (mandatory reads) and Step 2.1 (discovery recipes), enrich the local-evidence picture with rationale from GitHub: PR body excerpts associated with recent commits, the most recent GitHub Release notes excerpt, and recently-closed issues. Output lands in a conditional `recent-activity` seed thread (see Step 4 conditional topics).

**Skip this step entirely when any of the following are true:**

- Skill arguments include `no-github`, `skip-github`, `local-only`, or `offline`.
- `gh` CLI is not on PATH.
- No GitHub source repo can be resolved from any of: the active remote (`git remote get-url origin`), the GitHub `parent` field of `gh repo view`, or high-confidence canonical GitHub URLs in local metadata files (`package.json` `repository.url` / `bugs.url` / `homepage`, equivalents in `Cargo.toml`, `pyproject.toml`, etc.).

**Soft-gate posture:** when running, treat each `gh` query as independently failable. Per-query failures (missing auth, network, rate limit, repo-private-without-permission) are caught, recorded, and reported in Step 6. The run continues with whatever succeeded. If all queries fail or all categories return empty results across all resolved sources, do not write `recent-activity`; report the absence in Step 6 as either "GitHub layer unavailable" or "GitHub reachable but no recent activity surfaced" depending on which case applied.

**Detail location:** the source-repo resolver (the ordered list of GitHub source repos to consult), the PR / release / closed-issue fetch algorithms, the discovery-then-body capping rule, the portable-date computation for closed-issue queries, the per-category source fallback rules, and the `recent-activity` body template all live in `references/github-layer.md`. Load that reference at the start of this step.

---

## Step 2.3: Research pre-pass — external references → `onboarding-biblio` (default-on)

Runs by default, **before** deep-history (Step 2.4) and the analytical seeds (Step 4/5), so the
discerned intent and history threads can be framed against the repo's external scholarship. Many
repos implement or extend a founding paper / method / prior art that never surfaces from code and
git alone.

**What it does (detail in `references/research-prepass.md`):**
1. **Harvest (always, offline-safe):** scan `README*`, `CITATION*`, `docs/`, `REFERENCES.md`,
   `*.bib`, positioning artifacts for external references (arXiv/DOI/PDF-URL/`Author (Year)`
   patterns — reuse the `fetch-papers` Phase-2 patterns), classified resolvable vs paywalled.
2. **Identify the principal source paper(s)** — heuristic, recorded as `Inferred` with confidence
   (named in CITATION/README "based on", or the most prominently/repeatedly cited foundational
   source). May be zero.
3. **Deep parse (network-gated):** for each principal paper, fetch the PDF to a **persistent reuse
   cache** (`~/.watercooler/cache/biblio/<repo>/`, off the subject repo and threads branch; re-runs
   skip already-fetched/parsed papers so the deep-parse cost is paid once per repo) with
   `fetch-papers` curl safety, extract to markdown via the `pdf-to-md` per-PDF Task subagent
   (which writes the entry itself, or returns the markdown to the parent if it lacks MCP write
   tools — never drops it), isolate its References via `whitepaper_parser`, and rank → take the top
   **~8–12** salient secondary references (`+N more folded`, no silent caps).

**Skip the network steps (§0)** under `no-biblio` / `no-github` / `local-only` / `offline`, with no
network/fetch tool, or in dry-run (print planned entries; the offline link-harvest still runs).
Soft-gate posture: each fetch/parse is independently failable, recorded, never aborts onboarding.

**Outputs** (`onboarding-biblio` thread; titles prefixed `Onboarding: `; tags `onboarding` +
`biblio`; honors dry-run): an **index entry** (full harvested catalogue + principal-paper
identification + resolvable/paywalled split + parsed-entry index), **one entry per principal
paper** (the pdf-to-md markdown, written by the per-paper subagent), and **one entry per
high-relevance secondary** (full markdown when open-access, else a reference stub). Add
`onboarding-biblio` to the `onboarding-overview` sibling index. Biblio entries are `Note`s, never
`Decision`s — context, not authority.

---

## Step 2.4: Deep-history / decision-history reconstruction (opt-in, `deep-history`)

Runs only when the skill arguments include `deep-history`. Off by default — history mining is
expensive. Layers on Step 2.2: reuses the source-repo resolver, so for a **fork** it mines the
**parent**, not the empty fork `origin`.

**Goal.** Forensically reconstruct, per code **segment**, the repo's **decision and supersession
history** from PR/commit history — growing threads that read like the decision log Watercooler
*would* have captured live. **Honest value: a faithful, cited reconstruction + recall/synthesis
of the team's own (reconstructed) record** — NOT an agent-capability gap, never "the repo is
broken." A landed successor is the *most* valuable signal (it explains the current code), so
there is **no `forge-only` / `no-successor` keep-drop gate** here.

**Two phases — split on fact vs inference (do not collapse them):**
1. **Phase 1 — atomic change ledger (deterministic, FULL coverage, no LLM).** Per segment, the
   ordered factual record: PR/issue/SHA, files, symbols added/removed/renamed (`git log -S`,
   `--find-renames`), co-change, and a deterministic symbol/file **supersession graph**
   (introduced@A → rewritten@B → removed@C). Rank nodes by significance. Facts only.
2. **Phase 2 — decision-evolution narrative (bounded LLM over the ranked + supersession nodes).**
   Read intent (title/body/**linked design issue**/review/diff), classify the moment, append a
   segment-thread entry. **Every inferred-intent claim must cite a specific Phase-1 fact or a
   quoted rationale**, or be marked `pure sequence-inference`.

**Load-bearing honesty rules** (this method infers intent — without these it launders
speculation as decision):
- **Reconstruction voice, never the maintainer's first-person words** ("Reconstructed: at PR #N
  X→Y", not "we chose X because…").
- **Recorded rationale is quoted-or-declared-absent; inferred intent is always labeled +
  evidence-cited + confidence-scored.** Never fabricate a "why." When ≥2 intent-readings fit the
  diffs equally, preserve both.
- **Time-aware segments:** follow renames; record segment birth/split/merge as first-class
  moments.
- **Self-checks:** every inferred claim resolves to ≥1 ledger fact; claimed supersessions match
  the deterministic supersession graph; run one **calibration probe** (reconstruct a segment
  whose rationale *was* recorded, without reading it, then compare).

**Outputs** (Step 5 machinery + Step 5.5 tag-verify; topics prefixed `history-`, tags
`onboarding` + `history`; honors dry-run). The full output spec is deep-history.md §E. The thread
set is **a function of the segment count from §1.2**, not a fixed list — typically **8–15
`history-seg-*` threads** for a real repo (one per co-change cluster, including stable and
historical subsystems), plus one `history-overview` and one `history-synthesis` arc entry per
segment. **Producing a single `history-seg` thread means §1.2 was skipped and segments were
guessed** — see the §C segment-count self-check. Each `history-seg` entry is one PER MOMENT
(never consolidate a segment into a summary).

**Execution (one invocation writes everything — no hand-authoring):** Phase 1 builds the shared
deterministic ledger once; Phase 2 computes per-segment moments in parallel and **fans out one
worker per segment, writing concurrently** — the single-writer group-commit committer (#906/#908)
now owns the shared worktree, so writers only append + enqueue and the daemon serializes every
commit, making the concurrent-clobber race (#904) and the synchronous-summarizer timeout/stale-lock
failure (#903) structurally impossible. **Do NOT re-introduce per-worker `watercooler_sync_repair`
self-flush** — workers breaking each other's worktree locks was the original #904 root cause and
would reintroduce the data loss despite the committer fix. Each worker persists its own per-moment
entries via `watercooler_say`; **Phase 3** then generates the readable per-segment arc from the
committed moments; `history-overview` is generated from the real write results. **Post-run verify:**
writes are accepted durably but committed/pushed *eventually* by the committer — audit **origin**
`entries.jsonl` counts per thread to confirm the queue drained; on a stuck backlog →
`watercooler_sync_repair` + a flush write. Detail in `references/deep-history.md`.

**You cannot run this step from this summary.** The segment-derivation algorithm (§1.2) — the
part that decides how many `history-seg-*` threads exist — is **not** reproduced here, on
purpose. Do not derive segments from intuition or from "what the repo is currently working on."
Before doing anything else in Step 2.4, open `references/deep-history.md` and run §1.2. If you
have not read that file, stop — any segmentation you produce without it is invalid. (The file
also holds: the Phase-1 ledger recipes + supersession graph, significance ranking, Phase-2
intent-read + moment taxonomy, entry template, self-checks + calibration probe,
native-supersession reuse, format discipline, and verified datasette fixtures.)

---

## Step 3: Existing Watercooler context

Front-load all tools for context discovery:

```
ToolSearch: select:mcp__watercooler__watercooler_list_threads,mcp__watercooler__watercooler_pulse_snapshot,mcp__watercooler__watercooler_smart_query,mcp__watercooler__watercooler_daemon_findings,mcp__watercooler__watercooler_search,mcp__watercooler__watercooler_get_thread_entry
```

Run in parallel:

- `watercooler_list_threads(code_path=".", scan=true, format="json")`
- `watercooler_pulse_snapshot(code_path=".")`
- `watercooler_search(query="architecture decision implementation test docs release", mode="entries", code_path=".", limit=10, query_operator="OR")`

Interpret outcomes carefully:

- **Existing history found:** use it as source material. Seed entries should summarize,
  connect, and point to existing threads rather than duplicate them.
- **Confirmed empty history:** `list_threads` returns no threads and broad search returns a
  successful empty result. This is the strongest signal to write first-run seed threads.
- **History unavailable:** read tools return errors, indexing is unavailable, or search
  status is ambiguous. Do not claim the repo has no history. You may still write local-first
  seed entries if health and identity are valid, but each body must state that Watercooler
  history was unavailable during bootstrap.

For existing history, fetch full entry bodies only when needed:

- a summary references a decision or constraint that needs precise wording
- a thread is directly relevant to seed context
- a coordinator lead points to a specific range

Use:

```
watercooler_get_thread_entry(topic=<topic>, entry_id=<id>, code_path=".")
watercooler_get_thread_entry(topic=<topic>, index=<n>, to_index=<m>, code_path=".")
```

Then run:

```
watercooler_smart_query(
  query="What architectural decisions or constraints are currently in force?",
  code_path="."
)
watercooler_daemon_findings(daemon="project_coordinator", category="coordinator_lead", code_path=".")
```

Execute coordinator `details.lead.suggested_action` calls directly when present.

---

## Step 4: Build the seed entry set

Create a compact set of entries. Prefer the canonical nine entries listed below. The first three (`overview`, `product-charter`, `team-map`) supply the navigational, product, and human-accountability surfaces; the remaining six (`architecture`, `working-map`, `risk-register`, `test-surface`, `docs-contracts`, `entry-path`) supply the engineering surfaces. Add optional entries only when the repo evidence justifies them.

The same rules apply to **bootstrap** runs and **refresh** runs — refresh entries accumulate in the same threads and must be at least as well-cited as bootstrap entries.

### Required entry template (strict)

Every seed entry body MUST use the following skeleton. Sections are not optional except where explicitly noted.

```
Spec: <spec>

Purpose: <one to two sentences>

Observed:
- <fact> (`<path>:<line-range>`)
- <fact> (`<path>:<line-range>`)

Inferred:
- <claim> — confidence: high/medium/low — basis: <what surface this rests on>

Drift findings:                          # required for risk-register, docs-contracts, and team-map; OMIT for other topics
- Found: <what is out of sync> — <evidence A at path:line> vs <evidence B at path:line>
- None found after checking: <explicit list of cross-checks performed>

Next query: `watercooler_search(query="...", thread_topic="...", code_path=".")`

Related:
- <sibling-topic> — <one-line reason this entry depends on or extends that sibling>
- <sibling-topic> — <one-line reason>

Provenance: <every cited file with line ranges, every command run, every thread consulted, every Related sibling's entry_id>
```

### Cross-linking rule (Related slot)

Every seed entry MUST populate `Related:` with at least one sibling. The minimum-viable rule:

- Every non-`overview` entry MUST list `overview` as a Related sibling.
- Every entry that draws a Drift finding from another seed MUST list that other seed as Related (e.g., a `risk-register` Drift finding sourced from `docs-contracts` MUST list `docs-contracts`).
- The `overview` entry's Related: lists every sibling it indexes.

In the body of `Related:` (and elsewhere in body text, e.g., Drift findings or Inferred claims), reference siblings by topic string — not by raw ULID. Reserve raw `entry_id` ULIDs for the `Provenance:` section, where each Related sibling's `entry_id` is recorded for machine traversal. This keeps the body human-readable and the Provenance machine-precise.

If a sibling has not yet been written (e.g., during the first bootstrap pass), record its planned topic in `Related:` and note `entry_id: pending` in Provenance; the writer is responsible for back-filling the ULID once the sibling lands.

Worked example for the Drift findings section (using a real finding from a prior bootstrap):

```
Drift findings:
- Found: SECURITY.md lists 1.0.x as the only supported version (`SECURITY.md:9`)
  but production ships 2.x (`packages/mcp/package.json:3` shows `2.2.1`).
- Found: version skew across release manifests — `package.json:3` (2.2.1) vs
  `mcpb/manifest.json:5` (2.1.0) vs `server.json:17` (2.0.0). Confirm intent
  before next release.
```

### When the Drift findings section is required

The section is required on `risk-register`, `docs-contracts`, and `team-map` entries, omitted elsewhere. The agent must either list concrete findings (with file:line citations) or state explicitly that a numbered cross-check returned no finding.

#### Required cross-checks (numbered, enforceable)

Run all five general cross-checks below in order on `risk-register` and `docs-contracts`. The Drift findings section MUST report against each check by name with one of `[done]`, `[done — finding recorded]`, or `[n/a — <reason>]`. Reporting "None found after checking: [list]" without the numbered enumeration is an invalid run — re-run.

1. **Version coherence**: `[done | n/a — <reason>]` — compare every `package.json` version, every release manifest version, and any committed registry file version. Record any drift as a Found item.
2. **SECURITY.md supported vs shipped**: `[done | n/a — no SECURITY.md]` — compare the "supported versions" claim in `SECURITY.md` against the current major of every shipped package.
3. **Generated contracts**: `[done | n/a — none committed]` — compare generated/committed contract files (`*openapi*.json`, generated SDK clients) against the modification recency of their source code.
4. **Translated documentation**: `[done | n/a — no translations]` — compare every translated doc (e.g. `i18n/README.<locale>.md`) against the primary documentation.
5. **MCP / API tool descriptions vs manifests**: `[done | n/a — no manifests]` — compare tool/API descriptions in source vs descriptions in published manifests (e.g. `mcpb/manifest.json`, `server.json`).

`team-map` runs its own dedicated checklist (CODEOWNERS path existence, owner-vs-committer alignment) — see the `team-map` body contract below — not the five general checks above.

If a cross-check is genuinely not applicable (no manifests, no SECURITY.md), say so by name with `n/a — <reason>`. Skipping a check silently is the failure mode this section exists to prevent.

### Citation format

Every factual claim carries a citation in the body or in Provenance.

- **Single-file claims:** inline `` `path:line-range` `` (e.g. `` `packages/mcp/src/index.ts:55-65` ``).
- **Two-file claims:** inline `pathA:N, pathB:M`.
- **Three or more files corroborating the same claim:** footnote style. Body references `[1]`, `[2]`, etc.; Provenance expands each footnote: `[1] pathA:N / pathB:M / pathC:K`. Threshold is three so prose readability degrades only when it has to.
- Where the relevant unit is a section name rather than a line range (e.g. "the `exports` field of package.json"), `path:section-name` is acceptable.

### Exhaustive-list directives

If a seed entry mentions any of the following collections, it MUST enumerate them. Partial lists betray a partial inspection:

- **CI workflows** → name every file under the CI directory (`.github/workflows/*` or equivalent).
- **Distribution channels, packages, or release surfaces** → enumerate all of them, cross-referencing `server.json`, `Dockerfile`, every release manifest, and every plugin / skill / extension manifest.
- **Test files** → give the total count and the per-package layout.
- **Top-level documentation** → list everything in `docs/` plus the conventional root-level docs (`README.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`).
- **Translated documentation** → list every locale variant by code (e.g. `i18n/README.{ar,de,es,...}.md`).

### Topic / role table

Use these seed topics and entry types. Write `onboarding-overview` first; the other entries reference it via the `Related:` slot, so its entry_id needs to exist by the time siblings are written (or recorded as `pending` and back-filled — see the cross-linking rule above).

**Topic naming convention (mandatory).** Every onboarding-written topic slug
MUST begin with `onboarding-`. Watercooler thread titles default to
`topic.replace("-", " ")` (per `src/watercooler/baseline_graph/writer.py:310,654`),
so the `onboarding-` slug prefix produces a thread title that begins with
"onboarding " (e.g. `onboarding-architecture` renders as "onboarding
architecture") in the dashboard thread list. This is the only way to surface
"onboarding" in the *thread title* itself today — the MCP tool surface does
not expose a `set_thread_title` mutation. The slug prefix complements the
`Onboarding: ` entry-title prefix (per Step 5) and the `onboarding` thread
tag (per Step 5) to give three independent discoverability surfaces.

| Topic | Type | Role | Spec | Purpose |
|-------|------|------|------|---------|
| `onboarding-overview` | `Note` | `pm` or `scribe` | `pm` | Front-door TOC: plain-language framing + sibling index + reading order. **Write first.** |
| `onboarding-product-charter` | `Note` | `pm` | `pm` | What this product is, who it's for, what bet it represents |
| `onboarding-team-map` | `Note` | `pm` | `pm` | CODEOWNERS-derived ownership, recent contributor activity, ownership drift |
| `onboarding-architecture` | `Plan` | `planner` | `planner-architecture` | Repo purpose, subsystem boundaries, active architectural constraints |
| `onboarding-working-map` | `Note` | `scribe` | `docs` | Important directories, entrypoints, public surfaces, ownership hints |
| `onboarding-risk-register` | `Note` | `critic` | `security-audit` or `general-purpose` | Volatile paths, drift risks, unclear contracts, ambiguous ownership |
| `onboarding-test-surface` | `Plan` | `tester` | `tester` | Test inventory, CI gates, validation commands, coverage gaps |
| `onboarding-docs-contracts` | `Plan` | `scribe` | `docs` | Docs/API/config/schema surfaces that must stay synchronized |
| `onboarding-entry-path` | `Plan` | `pm` | `pm` | Recommended first tasks for implementer, planner, tester, and reviewer |

Optional/conditional topics (Step 4) follow the same prefix:
`onboarding-developer-experience`, `onboarding-release-process`,
`onboarding-security`, `onboarding-recent-activity`.

`onboarding-biblio` is also a core thread, but it is written **earlier** by the Step 2.3 research
pre-pass (not in Step 5) — `scribe` role, `Note` entries (index + one per parsed paper). It still
takes the `Onboarding: ` title prefix and the `onboarding` thread tag, and the `overview` sibling
index must include it. Detail: `references/research-prepass.md`.

In `Related:` body sections and elsewhere in prose, reference siblings by
their full prefixed topic slug (e.g. ``Related: `onboarding-architecture` ``).

### Body contracts for overview, product-charter, and team-map

Each new canonical topic carries a stricter body contract than the engineering seeds. The strict template (Spec / Purpose / Observed / Inferred / Drift / Next query / Related / Provenance) still applies; the additional rules below specify what must appear inside Observed, Inferred, and Drift for each topic.

#### `overview` body contract

The `overview` entry is the bootstrap's front door. Its body MUST contain, in order:

1. **Plain-language framing** (one paragraph, in `Purpose:` or the first bullet of `Observed:`). Distill the product framing from the highest-trust positioning sources discoverable in the repo: `*THESIS*` / `dev_docs/THE_*` / `docs/PHILOSOPHY*` / `README` / `package.json` description fields. Use everyday vocabulary; do not assume the reader is an engineer. If positioning sources are absent or thin, say so explicitly with low confidence.
2. **Sibling index** (a list under `Observed:`). Enumerate every other seed topic written in this bootstrap with a one-line purpose. Format: `` - `<topic>` — <one-line purpose> (entry_id `<ULID>`) ``. If a sibling has not been written yet, mark it `(entry_id: pending)`. When the Step 2.3 research pre-pass wrote `onboarding-biblio`, include it here as the scholarly-context sibling (and in the reading order below) so readers reach the source material first.
3. **"Five questions this seed answers"** (under `Observed:`). Map five concrete reader questions to the sibling that answers each — for example: "What does this product do? → `product-charter`. Who is responsible for which path? → `team-map`. What runs in CI? → `test-surface`."
4. **Reading order for first-time readers** (under `Inferred:`). Recommend an explicit order — typically `product-charter` → `team-map` → `architecture` → `working-map` → `entry-path`, then risk/test/docs as needed. Note when the recommended order differs by reader role (e.g., a security reviewer starts at `risk-register`).
5. **`Related:`** lists every sibling the overview indexes (this is the only entry whose `Related:` is exhaustive across siblings).

`overview` does NOT carry a Drift findings section.

#### `product-charter` body contract

The `product-charter` entry answers "what is this product, who is it for, what bet does it represent." Its body MUST contain:

1. **Product framing** under `Observed:`. What does the product do? Who is the intended user? What is the committed bet (stack, market posture, design principle)? Cite each claim with a `path:line-range` from `*THESIS*`, `ROADMAP*`, `VISION*`, README, or package descriptions.
2. **User / job-to-be-done sketch** under `Observed:`. Even if the repo does not name users explicitly, infer them with confidence labels from README and landing-page copy. If the source is silent, say so — "users not named in source surfaces; charter pending maintainer-authored Decision."
3. **Decision-source carve-out**: if a maintainer-authored thesis/mission/positioning doc states a committed choice with scope and rationale, it is acceptable to write a `Decision` for the product framing — but only when the source meets that bar. Otherwise write a `Note` with low confidence and recommend the maintainer write the Decision.

`product-charter` does NOT carry a Drift findings section.

#### `team-map` body contract

The `team-map` entry surfaces human accountability, not just code structure. Its body MUST contain:

1. **CODEOWNERS-derived ownership claims** under `Observed:`. Parse `.github/CODEOWNERS` (or equivalent) into per-path owner claims. If `CODEOWNERS` is absent, record that absence as the primary finding rather than skipping the topic.
2. **Recent contributor enumeration** under `Observed:`. Run:
   ```bash
   git shortlog -sn --use-mailmap --since="6 months ago" HEAD
   ```
   and record the result. The `--use-mailmap` flag applies any `.mailmap` rewrites in the repo so duplicate identities for the same contributor (e.g. `Jane Doe <work@example.com>` vs `jdoe <personal@example.com>`) collapse into a single entry; when no `.mailmap` is present, the flag is a safe no-op. If `shortlog` returns no output (some repo configurations do), fall back to `git log --use-mailmap --since="6 months ago" --format='%an' | sort | uniq -c | sort -rn`. The `HEAD` argument is required — `git shortlog -sn --since="6 months ago"` (without `HEAD`) silently returns no output in some repos.
3. **Pulse aggregates** (premium-tier enrichment, optional): `watercooler_pulse_snapshot(code_path=".")` returns `per_contributor` data when the PulseSnapshotDaemon is enabled. The PulseSnapshot daemon is a premium feature — open-core deployments will receive `{"status": "unavailable", "reason": "disabled"}` and that is expected, not a failure. When the response is `ok`, cross-reference its per-contributor counts against the git-shortlog result above and record any discrepancies. When the response is `unavailable` / `disabled`, record `Pulse aggregates: n/a — premium daemon not available in this deployment` and let git-shortlog stand alone as the contributor-activity source. Use `code_path="."` (a relative dot), not an absolute path, when calling this tool — pulse_snapshot's path check rejects absolute paths in some MCP subprocess configurations.
4. **Required Drift findings** (this is the dedicated checklist for `team-map`, not the five-item general checklist):
   - Found / None: CODEOWNERS paths that do not currently exist in the working tree.
   - Found / None: recent committers (last 6 months) absent from CODEOWNERS.
   - Found / None: CODEOWNERS-listed owners who have no commits in the last 6 months.
   Each must be reported by name with `[done]` or `[done — finding recorded]`. `[n/a — no CODEOWNERS]` is acceptable for the first two when applicable.

`team-map` requires Drift findings. The other Drift-required topics (`risk-register`, `docs-contracts`) run the five-item general checklist; `team-map` runs the three-item ownership checklist above instead.

### Optional topics

- `onboarding-developer-experience`: use when scripts, local setup, or tooling are non-trivial
- `onboarding-release-process`: use when release/versioning flow is discoverable
- `onboarding-security`: use when security policy, secrets handling, auth, or external inputs are
  material surfaces

Conditional topics (write only when their data trigger is met):

- `onboarding-recent-activity`: write when Step 2.2 (GitHub history layer) returned at least one non-empty category (PRs, release, or closed issues) across the resolved source repos. Type `Note`, Role `scribe`, Spec `docs`. The body summarizes latest release excerpt + resolved recent PRs + recently-closed issues per `references/github-layer.md`. Skip writing this topic when Step 2.2 was skipped, when all `gh` queries failed, or when every category returned empty across every resolved source.

Do not create Decision entries unless the decision is defensible from explicit source
material. Good sources include existing `Decision` entries, maintainer docs, architecture
docs, or unambiguous project policy. If a useful rule is inferred, write it as provisional
inside a `Plan` or `Note`, not as a `Decision`.

Acceptable Decision example when backed by source:

```
Contract-affecting changes must update docs and generated contract references in the same PR.
```

If the source is indirect or incomplete, phrase it as:

```
Provisional rule: contract-affecting changes appear to require paired docs/contract updates.
Source confidence is medium because this is inferred from docs and test layout, not a
maintainer-authored Decision entry.
```

---

## Step 5: Write seed entries

Skip this step only in dry-run mode or when Step 0 says writing is unsafe.

Load:

```
ToolSearch: select:mcp__watercooler__watercooler_say,mcp__watercooler__watercooler_annotations
```

Write each seed entry with `watercooler_say`:

```
watercooler_say(
  topic="<seed-topic>",
  title="Onboarding: <specific title>",
  body="<seed body>",
  role="<role>",
  entry_type="<Plan|Note|Decision>",
  create_if_missing=true,
  code_path=".",
  agent_func="<actual platform>:<actual model>:<role>"
)
```

This step uses `watercooler_say`, not the otherwise-preferred `watercooler_write`
wrapper: seeding needs `create_if_missing=true`, a mandatory `Onboarding: ` title
prefix, and an explicit `entry_type` — none of which `watercooler_write` exposes.
This is the documented "title / entry-type override" case for using a primitive
directly.

**Title rule (mandatory).** Every onboarding-written entry title MUST begin
with `Onboarding: ` (bootstrap) or `Onboarding refresh: ` (refresh runs). The
prefix makes seed entries discoverable in the dashboard search box without
requiring readers to know the canonical topic names. Refresh entries also keep
the prefix so future readers can distinguish onboarding-authored content from
ad-hoc additions in the same thread.

**Tag rule (mandatory).** After the first successful `watercooler_say` to a
seed topic, apply the `onboarding` tag at thread level:

```
watercooler_annotations(
  action="add",
  topic="<seed-topic>",
  target_id="<seed-topic>",
  target_type="thread",
  kind="tag",
  value="onboarding",
  actor="<your agent_func>",
  code_path="."
)
```

The annotation is idempotent — re-running on a refresh is safe. The tag is
how `update-agent-context` and other downstream skills discover seed threads
without hardcoding the canonical topic list. The dashboard renders thread
tags inline and supports `#onboarding` in the search box.

Write entries one at a time. If a write fails:

1. Stop subsequent writes.
2. Report which topic failed.
3. Include the non-secret error summary.
4. Print the remaining unwritten seed entries as a dry-run continuation.

If the *tag* write fails (rare: write_path degradation, network blip), record
the failure but do not abort — the title prefix still provides discoverability
and the tag can be back-filled by re-running the skill.

Normal title examples (with mandatory `Onboarding: ` prefix):

- `Onboarding: repository overview and reading order`
- `Onboarding: product charter from positioning surfaces`
- `Onboarding: team map from CODEOWNERS and recent committers`
- `Onboarding: repository architecture map`
- `Onboarding: working map from local inspection`
- `Onboarding: risk register from bootstrap inspection`
- `Onboarding: test and CI surface map`
- `Onboarding: docs and contract surface map`
- `Onboarding: entry path for future contributors`
- `Onboarding: GitHub recent activity` (only when Step 2.2 returned non-empty signal — see `references/github-layer.md` for the body template)

Refresh runs use `Onboarding refresh: <descriptive title>` instead.

---

## Step 5.5: Verify the title prefix and the `onboarding` tag landed

Before reporting in Step 6, run a tight verification pass. The "MUST" rules
in Step 5 are prose contracts; this step is the mechanical check that catches
silent partial failures.

1. **Tag presence (preferred — single batch).** Call:
   ```
   watercooler_list_threads(tags="onboarding", code_path=".", format="json")
   ```
   Confirm every seed topic written this run appears in the result —
   **including `onboarding-biblio` if the Step 2.3 research pre-pass wrote it**
   (it is written earlier than the Step 5 loop but is still a this-run seed and
   carries the `onboarding` tag). Any missing topic means the
   `watercooler_annotations` write silently failed (likely a write-path race or
   transient lock contention). For each missing topic, re-issue the tag-write
   call and re-run this batch query.

2. **Per-thread tag fallback (only if Step 1 still misses anything after
   one re-attempt).** For each topic still absent from the tag-filter
   result, call:
   ```
   watercooler_annotations(action="get", topic=<name>, code_path=".")
   ```
   and inspect `annotation_states[<name>].tags` for the literal value
   `onboarding`. This distinguishes "tag exists but list-filter is stale"
   (rare cache lag) from "tag genuinely missing" (real write failure).

3. **Title prefix sanity check (in-memory, no extra calls).** Confirm every
   title you sent to `watercooler_say` this run started with `Onboarding: `
   (bootstrap) or `Onboarding refresh: ` (refresh). Entries are
   append-only, so an unprefixed title cannot be edited in place — the
   only remediation is a follow-up corrective entry. If any title violated
   the rule, surface it as a hard error in Step 6 with the affected entry
   IDs.

4. **Hard-fail on any unresolved miss.** If after the re-attempt any seed
   thread still lacks the `onboarding` tag, do NOT silently complete
   Step 6. List each affected topic with the verbatim error from the
   re-attempt. Downstream skills (especially `update-agent-context`) rely
   on the tag for discovery — a missing tag means seeds become invisible
   to tag-based queries on that repo.

In dry-run mode, skip this step entirely (no writes happened, nothing to
verify).

---

## Step 5.6: Optional chain to `update-agent-context`

Run only when **all** of these hold:

- The skill arguments include `--update-agent-context` (or
  `update-agent-context`).
- This run is not a dry-run / preview / read-only / orient run.
- At least the canonical core seeds in Step 5 wrote successfully.
- Step 5.5 reported "verified ✅" (after at most one re-attempt).
  Without the `onboarding` tag, `update-agent-context` cannot discover
  the seeds via tag query — abort the chain, surface the tag failures
  in Step 6, and instruct the user to re-run
  `/watercooler-onboarding refresh` then chain manually.

If any gating condition fails, skip this step. The Step 6
recommendation still applies.

### Step 5.6.1: Back up existing agent-context files

Phase 1 of `update-agent-context` rewrites `CLAUDE.md` in place. If the
user has uncommitted local edits, or the file is not tracked in git,
the rewrite is the only thing between them and lost work. Take
timestamped backups before invoking the chain:

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
for f in CLAUDE.md AGENTS.md; do
  if [ -f "$f" ]; then
    cp "$f" "$f.pre-onboarding.$TS.bak"
    echo "backed up: $f -> $f.pre-onboarding.$TS.bak"
  else
    echo "no existing file: $f (will be created if Phase 1 emits it)"
  fi
done
```

Record the backup paths and the "no existing file" lines for the
Step 6 report. Backups land alongside the originals as untracked
`*.pre-onboarding.<TS>.bak` files; the user can restore with
`mv <bak> <original>` if they reject the chain's diff, or delete the
backups once satisfied.

### Step 5.6.2: Invoke `update-agent-context --phase1`

Run via the `Skill` tool:

```
Skill(skill="update-agent-context", args="--phase1")
```

Phase 1 is the right entry point for the chain because:

- For greenfield repos with no `CLAUDE.md`, Phase 1 establishes the
  canonical structure (Project Snapshot, Behavioral Principles,
  Repository Conventions, etc.) that Phase 2 later patches. Phase 2
  alone has no scaffold to patch into.
- For repos with an existing `CLAUDE.md`, Phase 1 grounds its rewrite
  in the freshly-seeded onboarding threads — its Step 4 reads them via
  the `onboarding` tag we just applied — so the new file reflects
  current repo reality, not the prior state.

Phase 2 is intentionally not auto-invoked from this chain. It extracts
durable conventions from `Decision` entries since the last update; the
seeds we just wrote are predominantly `Note` / `Plan` entries, so
Phase 2 right after onboarding has little to extract. Run it later as
Decisions accumulate.

If the chain invocation fails (skill error, mid-run abort), do not
retry automatically. The seeds are already durable in Watercooler
memory; nothing has been lost. Report the failure verbatim in Step 6
along with the backup paths so the user can re-run
`/update-agent-context --phase1` manually after diagnosing.

In dry-run mode for onboarding, skip this step entirely — there are no
verified seeds for `update-agent-context` to ground its rewrite in.

---

## Step 6: Final response

After successful writes, do not paste the full entry bodies. Report:

- seed topics written (count + each topic, every title prefixed
  `Onboarding: ` per the Step 5 rule)
- **Step 5.5 verification result**: tagged-thread count from the
  `tags="onboarding"` query, listed alongside the written-topic count.
  When they match, state "verified ✅". When they diverge, list each
  topic missing the tag and the verbatim re-attempt error.
- any title-prefix violations caught by Step 5.5 (the in-memory check) —
  list affected entry IDs so a follow-up corrective entry can be written
- any optional topics skipped and why
- conditional `recent-activity` status: written / skipped (Step 2.2 was skipped) / unavailable (gh failed) / empty (no signal across resolved sources)
- GitHub layer summary when Step 2.2 ran: which source repos were consulted (including any non-active sources labelled `[github/<owner>/<repo>]`), which categories landed (PRs / release / closed issues), and which were skipped, failed, or empty
- high-confidence decisions, if any
- important uncertainties
- chain status:
  - when `--update-agent-context` was supplied: the outcome of the
    Phase 1 invocation (success / failure with verbatim error if
    applicable), every backup path created in Step 5.6.1 (or a "no
    existing file" line for greenfield repos), and a one-line
    reminder that backups can be restored via `mv <bak> <original>`
    or deleted once the diff is accepted.
  - when not supplied: print the recommendation `Onboarding complete.
    To fold these seeds into CLAUDE.md/AGENTS.md, run
    /update-agent-context --phase1.` Phase 1 establishes structure
    from the seeds; `--phase2` is for periodic convention refreshes
    later, once the structure exists.
- suggested next query, for example:
  `watercooler_search(query="test surface", thread_topic="test-surface", code_path=".")`

In dry-run mode, print the proposed seed entries with their target topic, role, entry type,
and body. Make clear that no Watercooler memory was written.

---

## Design notes

- Durable memory over ephemeral summaries.
- Local repo evidence is first-class source material, especially on first run.
- Empty Watercooler history increases the value of seeding; it is not a stopping condition.
- `list_threads == 0` is not enough to prove no history; distinguish confirmed empty,
  scoped empty, and unavailable history.
- Seed entries must be additive and provenance-backed.
- Never upgrade inferred project norms into Decisions without explicit source support.
- Keep each seed body compact enough to remain queryable and reviewable.
- Respect branch locality and mark branch-specific observations explicitly.
