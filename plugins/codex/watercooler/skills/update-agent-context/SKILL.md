---
name: update-agent-context
description: |
  This skill should be used to keep CLAUDE.md, AGENTS.md, and the skill files
  themselves compact, current, and internally consistent. It runs in three phases:
  Phase 1 performs a one-time structural refactor of CLAUDE.md using a
  Karpathy-inspired behavioral scaffold and derives AGENTS.md from it by stripping
  Claude Code-specific sections. Phase 2 extracts durable project conventions from
  Watercooler Decision entries since the last update and patches a bounded
  generated section. Phase 3 audits the local skill surface against the live MCP
  tool surface and the in-process alias registry (`TOOL_ALIASES` in
  `aliases.py`), surfacing skill files that reference retired or renamed tools.
  Use when CLAUDE.md has grown verbose or stale, when AGENTS.md has drifted
  from CLAUDE.md, when significant project decisions have accumulated since the
  last refresh, or after a watercooler-cloud tool-surface consolidation lands.
---

# Update Agent Context

## Overview

Keeps `CLAUDE.md` and `AGENTS.md` short, current, and auditable. `CLAUDE.md` is the
source of truth; `AGENTS.md` is derived from it. The skill edits the working tree and
shows a diff — it does not commit. Use `/ship` to commit after review.

Arguments: `/update-agent-context [--phase1 | --phase2 | --phase3 | --all]`

`--all` runs Phase 1, then Phase 2, then Phase 3 in sequence. Default when no
flag is given: ask the user which phase to run.

Phase 3 is read-only — it audits the skill surface and produces a punch list.
It does not edit any file. Run it after any watercooler-cloud tool-surface
consolidation, or whenever a skill call unexpectedly fails because its tool
name no longer exists.

---

## Phase 1 — Structural Refactor or From-Scratch Seed

Run to (re)build `CLAUDE.md` on the approved structure and derive a fresh
`AGENTS.md`. Phase 1 has two modes; Step 0 selects between them.

### Step 0: Select mode

Check whether `CLAUDE.md` exists at the repo root.

- **Refactor mode** — `CLAUDE.md` exists. The current file is the primary
  content reservoir: carry its section content forward, fact-checked against
  source (Step 4). This is the common case (also use it after a major tool or
  role refactor).
- **From-scratch seed mode** — `CLAUDE.md` is absent. There is no reservoir;
  every section's content must be built from the grounding sources named in
  Steps 3–4 (source files, onboarding seeds, observed codebase patterns) plus
  the per-section content checklists in Step 5. Steps that read or diff the old
  file — Step 2, and the "carry forward" half of Step 4 — are skipped. Where a
  micro-convention has no file or seed source, infer it from the dominant
  pattern in the codebase and **mark it `inferred` in the Step 8 diff** so the
  user can confirm or correct it.

State the selected mode at the top of the run.

### Step 1: Measure

Compute current sizes, measuring only files that exist (in from-scratch seed
mode neither file is present, and a new repo may lack `AGENTS.md` even in
refactor mode — a bare `wc CLAUDE.md AGENTS.md` would exit non-zero):

```bash
for f in CLAUDE.md AGENTS.md; do [ -f "$f" ] && wc -l -w "$f"; done
```

Record the numbers. The target for `CLAUDE.md` is **≤ 300 lines and ≤ 2,000
words** (proxy for 2,000–3,000 tokens). Report both before and after. When a
file does not exist its "before" is `0` — the loop above simply prints nothing
for it.

### Step 2: Record drift facts

**Refactor mode only — skip in from-scratch seed mode (there is no prior file
to drift from).**

Read `CLAUDE.md` and `AGENTS.md` and note concrete mismatches that must be fixed,
for example:
- `AGENTS.md` opens with file-based framing while `CLAUDE.md` says graph-first.
- Stale "stdlib-only" absolutes that conflict with current dependencies.
- Sections that duplicate role prose or tool inventory already covered by MCP.

### Step 3: Fetch live role inventory (if Watercooler is available)

Load and call `mcp__watercooler__watercooler_roles` with `code_path` set to
the repo root. Use the returned role names and summaries to confirm the six canonical
roles (`planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`). Do not copy
role prose into `CLAUDE.md`; point to `watercooler_roles` instead.

If Watercooler is unavailable, proceed with local files only and note the limitation.

### Step 4: Verify project facts against source

Before drafting prose, ground every concrete claim that will appear in the
**Project Snapshot**, **Repository Conventions**, **Development Workflow**, and
**Security & Publishing** sections. Read these source files directly and use only
what they say. In refactor mode, do not carry a claim over from the old
`CLAUDE.md` / `AGENTS.md` without re-checking it here; in from-scratch seed mode
there is nothing to carry — build every claim directly from these sources.

Required reads:

- `pyproject.toml` — extract `requires-python` (the supported Python range),
  declared dependencies, and any deprecation notes. Do not state a wider support
  range than `requires-python` allows.
- `.github/workflows/*.yml` — extract the CI matrix (`os:` and `python-version:`
  lists). The "CI must pass on …" sentence in Development Workflow must name only
  the OSes that actually appear in the matrix.
- `tests/` directory — list its top-level layout (`tests/`, `tests/integration/`,
  `tests/unit/`, etc.). Any "tests mirror sources" example must reference a file
  that actually exists. Prefer pointing at the directory pattern over a single
  example path so the bullet does not rot when files move.
- `README.md` "Requirements" / "Install" sections — cross-check Python version
  and install-extras claims.
- `pyproject.toml` `[tool.black]` / `[tool.ruff]` / `[tool.mypy]` tables
  **(where present)** — ground the Repository Conventions claims that *are*
  mechanically enforced: line length, import ordering (isort/ruff), and the
  type-check gate. Config-enforced conventions are facts, not preferences.
  Pared-down builds may omit these tables; when a table is absent, fall back to
  observed code and mark the item `inferred`.
- `SECURITY.md` — the vulnerability-disclosure contact and process; the single
  source for the Security & Publishing "Vulnerabilities" claim.
- A release / publish-pipeline config (a Copybara file, a release-automation
  workflow, etc.) **if the repo has one** — the source for any
  Security & Publishing "publish pipeline" claim. Many repos have none (and a
  downstream/open-core mirror typically does not ship its own publish config);
  skip silently when absent.
- `CONTRIBUTING.md` / `.github/PULL_REQUEST_TEMPLATE*` (if present) — branch
  and commit conventions and PR expectations.
- `git log --format='%H %s%n%b' -30` — confirm the actual commit-message style
  (Conventional Commits or not) and whether commits carry a `Signed-off-by`
  trailer. The Development Workflow commit rules must match observed history,
  not an aspiration. The sign-off identity itself is **not** invented here — if
  a sign-off convention is in use, point at it; do not hardcode an address.
- **Watercooler onboarding seeds (when present)** — discover seed threads
  written by the `watercooler-onboarding` skill via the `onboarding` thread
  tag. These are higher-trust than the prior `CLAUDE.md` because their bodies
  cite source files with `path:line-range` provenance and were curated
  specifically as durable foundation material.

  **Discovery (preferred — tag-based):**
  ```
  watercooler_list_threads(tags="onboarding", code_path=<repo_root>)
  ```
  Returns every onboarding-tagged thread without requiring you to know the
  canonical topic names. New canonical topics added by future onboarding
  releases are picked up automatically.

  **Discovery (fallback — when no threads carry the tag):**
  Probe the canonical topic list directly. This is needed for repos seeded
  before the tagging convention landed (2026-05-04). After the first
  successful read, surface in the report that the seeds are untagged and
  recommend a `watercooler-onboarding refresh` run to apply the tag.

  Read the most recent entry of each topic that exists:
  - `onboarding-architecture` — subsystem boundaries + active architectural
    constraints. Use to ground **Project Snapshot** and the **Watercooler
    Protocol** section (graph-first reads, tier model, transport).
  - `onboarding-working-map` — directory map and entrypoints. Use to ground
    **Project Snapshot** layout claims.
  - `onboarding-team-map` — CODEOWNERS-derived ownership + recent
    contributors. Use when **Repository Conventions** mentions ownership /
    sign-off conventions.
  - `onboarding-risk-register` — volatile paths and drift findings. Use to
    surface cautions an agent should know before changing the system;
    record any drift the seed flags as candidates for fixing in this same
    patch.
  - `onboarding-docs-contracts` — synchronized doc / API / schema surfaces.
    Use to ground claims about what stays in sync (e.g. CHANGELOG,
    manifests, OpenAPI).
  - `onboarding-entry-path` — recommended first tasks per role. Cross-check
    against **Development Workflow** wording.
  - `onboarding-overview` / `onboarding-product-charter` — front-door framing
    and product bet. Use to ground the one-line product description in
    **Project Snapshot**.

  Canonical topic names (for fallback probing only — tag-based discovery is
  preferred): `onboarding-overview`, `onboarding-product-charter`,
  `onboarding-team-map`, `onboarding-architecture`, `onboarding-working-map`,
  `onboarding-risk-register`, `onboarding-test-surface`,
  `onboarding-docs-contracts`, `onboarding-entry-path`. Optional:
  `onboarding-developer-experience`, `onboarding-release-process`,
  `onboarding-security`, `onboarding-recent-activity`. The slug prefix
  produces a default thread title beginning with "onboarding " in the
  dashboard. Onboarding-written entries also start with the entry-title
  prefix `Onboarding: ` (or `Onboarding refresh: ` for refresh runs) —
  useful for distinguishing seed entries from ad-hoc additions in the same
  thread.

  Legacy / pre-2026-05-04 seeds may exist under bare slugs (`overview`,
  `architecture`, etc.). When the prefixed-slug fallback returns empty,
  fall through to the bare-slug list as a last resort and recommend a
  `/watercooler-onboarding refresh` to migrate.

  Fetch each discovered thread with `watercooler_read_thread(topic=<name>,
  code_path=<repo_root>, code_branch="*")`. If neither tag-based discovery
  nor canonical-topic probing returns any seeds, none exist — proceed with
  local files only and note in Step 2's drift facts that running
  `/watercooler-onboarding` would strengthen the next refresh.

  Treat seed entries as snapshots in time: cross-check named symbols /
  files / flags against the current source the same way Phase 2 Step 7
  validates existing convention bullets.

If any existing claim in the current `CLAUDE.md` / `AGENTS.md` contradicts these
sources, treat the source files as authoritative and rewrite the claim. Record each
correction in the drift-fact list from Step 2 so it is visible in the diff.

### Step 5: Rewrite CLAUDE.md

Apply the approved section structure below. Write the full replacement in one pass.

**Target structure:**

```
## Project Snapshot          (~150 tokens)
## Behavioral Principles     (~350–450 tokens)
## Repository Conventions    (~500–700 tokens)
## Development Workflow      (~250–350 tokens)
## Watercooler Protocol      (~400–600 tokens)
## Security & Publishing     (~150–250 tokens)
## Project Conventions       (~300–500 tokens; generated block — see Phase 2)
<!-- agent-context-refresh:last-updated=YYYY-MM-DD -->
```

**Behavioral Principles** — include all four Karpathy principles with short
project-local adaptations. Do not copy a large external block; name each principle
and add one sentence of local context:

1. **Think Before Coding** — State assumptions explicitly. Surface tradeoffs. If
   multiple interpretations exist, present them; do not pick silently.
2. **Simplicity First** — Minimum code that solves the stated problem. No speculative
   features, no abstractions for single-use code.
3. **Surgical Changes** — Touch only what you must. Match existing style. Do not
   improve adjacent code unless it is directly in scope.
4. **Goal-Driven Execution** — Transform tasks into verifiable goals. State a brief
   plan for multi-step work; loop until verified.

**Project Snapshot** — a ~150-token orientation: one-line product description,
supported language/runtime range, core tooling, test layout, and the docs vs
internal-docs split. Ground the facts per Step 4; the product one-liner comes
from `onboarding-overview` / `onboarding-product-charter` (or `README.md` when no
seed exists). The `docs/` (user-facing) vs internal-docs split and any
solution-writeup location are conventions — source them from an onboarding seed
or a Decision entry, not invention.

**Repository Conventions** — enumerate the project's concrete conventions, one
short bullet each. Cover this checklist; source each item as noted, and in
from-scratch mode mark any item with no file/seed source as `inferred`:
- **Layout** — package layout, test-directory layout, bundled-data location.
  Source: observed directory structure (Step 4 `tests/` read + a top-level `ls`).
- **Type hints / type-check gate** — whether hints are required and the checker.
  Source: `[tool.mypy]` config + observed signatures.
- **Docstrings** — style (e.g. Google) and where required. Source: onboarding
  seed or the dominant codebase pattern.
- **Errors** — exception-raising norms. Source: seed or dominant pattern.
- **Imports** — ordering, absolute vs relative. Source: `[tool.ruff]` isort
  config (config-enforced = fact, not preference).
- **Naming** — casing per identifier kind, CLI command casing, role names,
  entry-type names. Source: role names from `watercooler_roles`; entry types
  from the code enum; casing from observed code.
- **Dependencies** — core-minimal policy and the bar for adding a dependency.
  Source: a Decision entry or onboarding seed.
- **Comments** — default density. Source: Behavioral Principles + dominant pattern.
- **Coverage** — target, if any. Source: CI config or a Decision entry; mark
  `inferred` if neither states a number.
- **Markdown** — wrap width and heading style. Source: `[tool.black]` /
  editorconfig line length where it applies; otherwise observed.

**Development Workflow** — cover this checklist, grounded per Step 4:
- **Branches** — naming / prefix scheme. Source: `CONTRIBUTING.md` or observed
  branch names in `git log` / `git branch -a`.
- **Commits** — message convention and any `Signed-off-by` requirement. Source:
  the `git log` read in Step 4 — state only what history actually shows.
- **Pull requests** — required CI and merge style. Source: `.github/workflows/`
  for the CI matrix (name only OSes/versions that appear); merge style from
  `CONTRIBUTING.md` or observed merge commits.
- **Tests** — how to run them and notable markers. Source: `pyproject.toml`
  pytest config + `tests/` layout.
- **Quality gate** — the exact lint/format/type command(s). Source: the
  `[tool.*]` configs + any `Makefile` / CI lint step.
- **Install** — the dev-install command and required extras. Source:
  `pyproject.toml` extras + `README.md`.
- **Risky actions** — the confirm-before rule for destructive / shared-state
  operations. Source: Behavioral Principles (carry the project-local wording).

**Watercooler Protocol** — condense to a single focused section. Must preserve:
- Identity setup: per-call `agent_func` in the format `<platform>:<model>:<role>`. There
  is no `set_agent` / `watercooler_set_agent` shortcut — set `agent_func` on every
  write call (`write`, `say`, `ack`, `handoff`, `set_status`). Verify against the
  registered tool surface in `src/watercooler_mcp/capabilities.py` before claiming a
  setter tool exists.
- `code_path` discipline: always set to the repo root being worked on.
- Graph-first reads: baseline graph (JSON) is the sole source of truth; `.md` files
  are write-only projections. Always start with `summary_only=true`.
- Mandatory MCP use: never read/write thread files directly.
- Write discipline: `watercooler_write` is the preferred first-choice write tool
  (it wraps `say`/`ack`/`handoff`); the primitives `say` / `ack` / `handoff` /
  `set_status` remain available for explicit coordination control. All writes go
  through MCP tools only.
- `Spec:` marker: first line of every entry body.
- Pointer to `watercooler_roles` for full role behavior — do not repeat prose.
- **Intent-first modification rule**: before modifying existing code or state, query
  watercooler threads and semantic search to understand the *intent* behind the current
  state. Do not infer design constraints from code alone.
- Proactive recall: run `watercooler_smart_query` before starting significant work.

**Security & Publishing** — cover this checklist, grounded per Step 4. Omit any
bullet whose subject the project does not have — the checklist is a menu, not a
mandate:
- **Secrets** — the never-commit rule and where required env vars are documented.
- **Input validation** — the validate-at-boundaries norm. Source: Behavioral
  Principles or a Decision entry.
- **Vulnerabilities** — the disclosure contact and process, taken from
  `SECURITY.md` verbatim. Do not invent an address.
- **Reference-only code surfaces** — when the project has a deployed-vs-reference
  split (e.g. a Slack formatter whose production copy lives elsewhere), keep a
  one-line warning naming which file is authoritative. Load-bearing: it stops
  agents editing the wrong file.
- **Publish pipeline** — include only when the repo publishes a downstream
  artifact or mirror: the pipeline and the rule against hand-pushing it. Source:
  the release/publish-pipeline config found in Step 4. A repo that *is* the
  published downstream (e.g. an open-core mirror) usually has no such config —
  omit the bullet.

**Sections to eliminate or replace with pointers:**
- Full docstring / type-hint examples → keep as one-line bullet, remove code blocks
- Testing guidelines with code blocks → keep as bullets only
- Module organization inventory → remove (derivable from codebase)
- Role definitions section → replace with pointer to `watercooler_roles`
- Full tool usage protocol → replace with condensed Watercooler Protocol section
- Verbose Slack integration *architecture* prose → cut. But keep the one-line
  reference-only warning (per the Security & Publishing checklist) — that warning
  must survive the refactor, not be removed with the prose around it.

### Step 6: Validate invariants

Before writing, verify the draft preserves all of these. If any is missing, add it.

- [ ] Graph-first reads stated; `.md` files are projections, not source of truth
- [ ] Mandatory MCP use for all thread / watercooler operations
- [ ] Identity setup names per-call `agent_func` only — no `set_agent` /
      `watercooler_set_agent` reference (verify against
      `src/watercooler_mcp/capabilities.py`)
- [ ] `Spec:` marker write discipline preserved
- [ ] Write discipline names `watercooler_write` as the preferred write tool,
      with `say` / `ack` / `handoff` / `set_status` as the explicit-control fallback
- [ ] `code_path` alignment rule
- [ ] Intent-first modification rule (query threads before changing state)
- [ ] No repeated role prose (pointer to `watercooler_roles` only)
- [ ] No large tool inventories
- [ ] No stale "stdlib-only" absolutes that conflict with current deps
- [ ] Project Snapshot facts match source: Python range matches
      `pyproject.toml:requires-python`, OS list matches `.github/workflows/`,
      test-layout examples reference paths that actually exist
- [ ] `## Project Conventions` block present (may be empty; Phase 2 fills it)
- [ ] Footer comment: `<!-- agent-context-refresh:last-updated=YYYY-MM-DD -->`

### Step 7: Derive AGENTS.md

`AGENTS.md` is `CLAUDE.md` with the Claude-Code-specific *mechanics* removed — it
keeps every section, including `## Watercooler Protocol`. Apply these edits to a
copy of the new `CLAUDE.md`:

- **Drop the `**Tool invocation:**` subsection** of `## Watercooler Protocol` —
  the `ToolSearch` / `mcp__<server>__<tool>` / Serena call mechanics are
  Claude-Code-specific. Keep the rest of the section (graph-first reads,
  intent-first rule, identity, write discipline, storage contract).
- **Neutralize Claude-Code-flavored examples** — `agent_func` examples and the
  like should use platform-neutral identities, not a single client's.
- **Drop skill-name references** — replace `<skill-name>` skill mentions (e.g.
  "follow the `editorial-publishing` skill") with the underlying doc or process;
  AGENTS.md is read by agents that have no Claude Code skill catalog.
- **Cut anything referencing Claude Code hooks** or MCP-client-specific identity
  details.

Do not strip whole sections by header name — the difference between the two
files is small and subsection-level. Preserve everything else verbatim in
intent: repo facts, graph-first claims, security rules, commit policy (including
the sign-off rule), Karpathy behavioral principles, mandatory Watercooler MCP
requirements, intent-first rule, and the full `## Project Conventions` block.

Add this marker as the first line of `AGENTS.md`:
```
<!-- generated-from: CLAUDE.md by /update-agent-context -->
```

### Step 8: Check budget and show diff

Re-run `wc -l -w CLAUDE.md AGENTS.md`. If `CLAUDE.md` exceeds 300 lines or 2,000
words, trim verbose prose — prefer bullets over paragraphs. Then show the diff and
stop. The user reviews and commits via `/ship`.

---

## Phase 2 — Convention Extraction

Run periodically (monthly or after a significant batch of decisions) to patch the
`## Project Conventions` generated block with durable conventions from Watercooler
memory.

### Step 1: Determine the extraction window

Read the footer comment from `CLAUDE.md`:
```
<!-- agent-context-refresh:last-updated=YYYY-MM-DD -->
```

If absent, default to the last 30 days and state that this is a bootstrap run.

### Step 2: Fetch Decision entries (T1 — always attempt)

Load and call:
```
mcp__watercooler__watercooler_list_decisions(
    since=<last_update_date>,
    limit=500,
    code_path=<repo_root>
)
```

If Watercooler is unavailable, stop Phase 2 with a clear message. Convention
extraction without evidence would be unauditable.

### Step 3: Broad entry search (T1 — always attempt)

Load and call:
```
mcp__watercooler__watercooler_search(
    mode="entries",
    query="convention naming workflow must never always rule pattern",
    query_operator="OR",
    start_time=<last_update_iso>,
    code_path=<repo_root>
)
```

### Step 4: Optional enrichment (T2 — catch errors and continue)

Load and call:
```
mcp__watercooler__watercooler_smart_query(
    query="What durable project conventions were established or changed since <date>?",
    max_tiers=2,
    resolve_provenance=True,
    code_path=<repo_root>
)
```

If this call errors (open-core T1-only install, network issue, etc.), continue with
T1 evidence only. Do not gate on `watercooler_health` — call directly and inspect
the result.

### Step 4.5: Pull onboarding seed entries (when present)

The `watercooler-onboarding` skill writes a canonical set of seed threads with
strict body templates (`Spec` / `Purpose` / `Observed` / `Inferred` / `Drift
findings` / `Provenance`). Their `Observed:` "active architectural constraints"
items, `Inferred:` high-confidence claims, and `Drift findings:` rules are
high-quality convention material — curated, file:line-cited, human-authored,
and explicitly designed as durable foundation knowledge.

**Discovery (preferred — tag-based):**
```
watercooler_list_threads(tags="onboarding", code_path=<repo_root>)
```

Filter the convention-relevant subset (`onboarding-architecture`,
`onboarding-risk-register`, `onboarding-docs-contracts`,
`onboarding-team-map`, `onboarding-entry-path`) from the returned topic list,
then read each:

```
mcp__watercooler__watercooler_read_thread(
    topic=<name>, code_path=<repo_root>, code_branch="*"
)
```

**Discovery (fallback — when tag query returns empty):** probe the canonical
topic names directly. Repos seeded before the onboarding-tagging convention
landed (2026-05-04) won't have the tag yet. After successfully reading any
untagged seed, surface in the candidate report that a
`/watercooler-onboarding refresh` would apply the tag and recommend running it.

For each topic that returns content, scan the body for rule-like passages
in **two** sections:

**(a) `Observed:` and `Drift findings:` — primary scan.** This is where
forward rules and sync obligations are most often stated outright.

- `architecture` — items under "Active architectural constraints" or
  "Load-bearing invariants" are convention candidates.
- `risk-register` — `Drift findings:` items framed as "X must stay in sync
  with Y" are convention candidates; one-off operational risks are not.
- `docs-contracts` — synchronization rules (e.g. "contract-affecting changes
  must update docs and generated references in the same PR") are convention
  candidates.
- `team-map` — CODEOWNERS-derived ownership rules are convention candidates
  only when they encode an enforceable workflow (e.g. mandatory reviewers).
- `entry-path` — recommended workflows per role are convention candidates
  when they apply to all PRs in their scope.

**(b) `Inferred:` — secondary scan, filtered.** Most inferred items are
framing or positioning prose ("repo is built for X") that does not
translate to a forward rule and should be skipped. A subset, however,
is genuine convention material. Promote an inferred item to a
candidate **only when all four** of these hold:

1. `confidence: high` (medium / low never qualify);
2. `basis:` cites a file, line range, or code surface — not a vibe;
3. The claim is framed as a behavioral norm or a framing-trap warning,
   not as descriptive prose. Recognizable shapes:
   - "X is the authority for Y" (where to look for ownership / source-of-truth)
   - "Conflating A and B is a [known] trap" (axis-orthogonality warnings)
   - "Treat C as D, not E" (interpretive norms)
   - "Don't infer X from Y" (anti-pattern guidance)
4. The norm is durable and cross-cutting — it would still apply six
   months from now and across more than one narrow file.

Worked example (2026-05-04): `onboarding-team-map` Inferred item *"the
repo treats CODEOWNERS as 'default reviewer signal,' not 'per-path
accountability' — this seed is the canonical authority for ownership
questions, not CODEOWNERS itself"* is a candidate (high confidence,
basis cites `.github/CODEOWNERS:1-2` + `git shortlog`, framed as
"X is the authority for Y", durable until the team grows past
single-glob CODEOWNERS). Counter-example: `onboarding-architecture`
Inferred item *"the repo is structured for a long-lived multi-tier
memory product, not a small CLI"* is descriptive positioning — skip.

**(c) `Inferred:` — risk-signal scan (parallel output, not convention
material).** Items that fail the section (b) filter may still carry
useful "watch this" or "monitor that" value. These do **not** belong
in CLAUDE.md's Project Conventions block (which is for forward
behavioral rules) but should be surfaced in the Step 6 candidate
report as "Risk signals to forward to next risk-register refresh"
so the user can decide whether to fold them into the next
`/watercooler-onboarding refresh` of `onboarding-risk-register`.

Promote an inferred item to a risk signal when all three hold:

1. `confidence: medium` or `confidence: high` (low never qualifies);
2. `basis:` cites a measurable surface — a number, a counter, an
   observable ratio, a recurring pattern with at least one concrete
   instance — not pure speculation;
3. The claim is framed as monitoring guidance or a recurring-pattern
   warning, not as descriptive prose. Recognizable shapes:
   - "X suggests Y" / "X indicates Y" (interpretive monitoring claim)
   - "Watch for X" / "X is worth monitoring" (explicit monitoring call)
   - "X is a recurring pattern" / "this class of drift" (pattern
     escalation candidate — codify a forward convention if observed
     again)
   - Numeric thresholds without a current rule ("ratio of N% suggests
     ..." where N has no codified band).

Risk signals are reported informationally only — they never auto-patch
CLAUDE.md. The output is a list the user reviews and optionally
forwards to a risk-register refresh.

Worked examples (2026-05-04 — three items today's Phase 2 would
have surfaced if this scan had been in place):

- *DLQ ratio.* `onboarding-risk-register` Inferred item *"The
  dead-letter ratio (~37%) suggests the memory pipeline has been
  working through a real backlog"* — medium confidence, basis cites
  `watercooler_health` counters, framed as "X suggests Y". Forward
  to risk-register; consider noting a healthy DLQ band.
- *CHANGELOG lag as recurring pattern.* `onboarding-docs-contracts`
  Inferred item *"the team ships releases via main → staging → stable
  → tag but CHANGELOG updates don't ride along automatically"* —
  medium confidence, basis cites `[0.4.0]` only documented vs three
  shipped tags, framed as recurring pattern. Forward to risk-register;
  if observed twice more post-fix, codify as a forward convention.
- *Self-description framing drift as a class.* The specific
  "File-based" instance from `onboarding-risk-register` is being
  fixed in PR #757, but the *class* — package docstring / README /
  thesis must agree on framing — is a recurring drift surface.
  Forward to risk-register as a monitoring item for major doc
  refreshes.

Skip `overview`, `product-charter`, `working-map`, `test-surface` for
convention extraction — they are framing or navigational, not
rule-establishing.

If no canonical onboarding topics exist, skip this step. Note in the
candidate report that `/watercooler-onboarding` has not run for this repo;
running it would expose another high-trust convention source on the next
refresh.

### Step 5: Pull daemon advisory findings

The decision-stance, pulse-snapshot, and project-coordinator daemons emit findings
that surface convention drift, stalled threads, role-imbalance signals, and
coordinator-led improvement opportunities. These are complementary to the
keyword/semantic search in Steps 2–4 and feed both candidate scoring (Step 6)
and existing-bullet re-validation (Step 7).

Call all three; non-running daemons return empty results, so calling speculatively
is safe:

```
mcp__watercooler__watercooler_daemon_findings(
    daemon="project_coordinator", limit=30, unacknowledged_only=True,
    enrich=True, code_path=<repo_root>
)
mcp__watercooler__watercooler_daemon_findings(
    daemon="decision_stance", category="stance_advisory", limit=20,
    unacknowledged_only=True, code_path=<repo_root>
)
mcp__watercooler__watercooler_daemon_findings(
    daemon="pulse_snapshot", enrich=True, limit=20, code_path=<repo_root>
)
```

Notes on these calls:

- `project_coordinator` and `decision_stance` are **mutually exclusive** — only
  one is registered at a time (`daemons/__init__.py` deregisters
  `decision_stance` when the coordinator is active). Whichever is unregistered
  returns an empty list.
- `enrich=True` on coordinator/pulse calls overlays S1/S2/S3 context (hygiene
  tags, decision candidates, dimension scores) onto `coordinator_lead`-style
  findings; without it you only get the bare finding.
- `unacknowledged_only=True` filters out findings that have already been triaged
  via `watercooler_daemon_findings(action="acknowledge")`, so the skill doesn't
  repeatedly re-surface the same items run after run.

How to use the findings:

- **Scoring input** (Step 6): A `coordinator_lead` or `stance_advisory` that
  recurs across multiple threads or repeats over weeks hints at a durable
  convention worth promoting to a bullet.
- **Re-validation input** (Step 7): A fresh finding that contradicts an
  existing bullet (e.g., `stalled_handoff` repeatedly firing in a domain whose
  bullet claims the handoff convention is working) is direct evidence the
  bullet is no longer holding.

**Findings are not citations.** Daemon emissions are signal inputs, not
evidence-backed Watercooler entries. A bullet promoted on the strength of a
finding still requires a Decision-entry ULID for its citation slot per Step 8
acceptance rule #4 — trace the finding back to the underlying Decision (often
linked via `details.source_lead_ids` on enriched findings) and cite that.

### Step 6: Score and present candidates

For each candidate convention, assign a relevance score:

- **High-onboarding**: Sourced from a canonical onboarding seed entry's
  "Active architectural constraints", "Drift findings", or a qualifying
  `Inferred:` item (per Step 4.5's four-clause filter — high confidence,
  code-grounded basis, behavioral-norm framing, durable + cross-cutting).
  Promote above same-content candidates from other sources — the seed
  was curated specifically to expose durable architectural truths and
  carries file:line provenance.
- **High**: Keywords like `must`, `never`, `always`, `convention`, `rule`, `naming`,
  `workflow` in the entry title or body. Appears in a `Decision` entry.
- **Medium**: Describes a recurring pattern. Mentioned in multiple entries.
- **Low**: One-off implementation detail. Unresolved debate. Daemon finding without
  human follow-up.

Present a scored list with source entry IDs and titles. Ask the user which to
incorporate. Do not auto-promote Low candidates.

**Risk signals to forward to next risk-register refresh** — present this as
a separate, parallel section in the candidate report (not mixed in with
convention candidates). Sourced from the Step 4.5 (c) scan of seed
`Inferred:` items that qualified as monitoring guidance or
recurring-pattern warnings. For each, include:

- Source seed topic + entry_id
- The verbatim Inferred quote
- Confidence level (medium / high)
- Provenance basis (the file, counter, or surface cited)
- Suggested monitoring shape (what to track) and a candidate
  mitigation (if obvious from the seed body)

These items are **informational only** — they never auto-patch
CLAUDE.md. The user's options are:

1. Forward to the next `/watercooler-onboarding refresh` to fold into
   `onboarding-risk-register`'s next entry;
2. Open a dedicated thread to track the risk;
3. Skip — judgment that the signal isn't worth tracking.

If no qualifying risk signals were found, omit this section entirely
rather than printing "(none)" — silence here is the common case and
shouldn't add noise to the report.

### Step 7: Re-validate existing convention bullets

Before adding new bullets, re-read every bullet currently inside the
`## Project Conventions` block and check that it still describes reality. The
generated block is a snapshot of past decisions; underlying code, flags, or APIs
may have changed since extraction. Stale bullets actively mislead agents and must
be revised or removed in the same patch.

For each existing bullet, verify:

- **Named symbols / files / flags exist and behave as described.** Grep the
  source. If a bullet says "registration uses `LOCAL_DAEMON_NAMES`" and the
  symbol is gone or marked deprecated, the bullet is stale.
- **Source entry has not been superseded.** Look for a newer Decision entry
  that overturns the cited entry. Use either
  `watercooler_list_decisions(topic=<topic>, since=<entry_timestamp>,
  code_path=<repo_root>)` (cleanest — it natively filters by topic and time)
  or `watercooler_search(mode="entries", entry_type="Decision",
  start_time=<entry_iso>, backend="baseline", code_path=<repo_root>, ...)` and
  inspect the returned entries' `timestamp` fields manually. Notes:
  the parameter is `entry_type`, not `type`; `watercooler_search` exposes no
  sort argument; entry-type filtering is unsupported on the graphiti backend
  so pass `backend="baseline"` when filtering by `entry_type`.
- **Referenced docs still say the same thing.** If a bullet points to a doc
  path, open it and confirm the convention still matches.

Categorize each existing bullet as:

| State | Action |
|---|---|
| Still accurate | Keep as-is |
| Wording stale, intent intact | Revise the bullet, keep the entry ID |
| Superseded by newer decision | Replace with the new convention; cite the
  newer entry ID. Delete the old bullet. |
| Convention no longer holds | Remove the bullet entirely |

Common supersession signals to look for in source code: `deprecated`,
`superseded`, `replaced by`, `no longer used`, `# DEPRECATED`, an empty
return where the symbol used to do work. When any of these is found near a
named symbol from a bullet, the bullet is suspect — verify with a fresh search
before keeping it.

Present the re-validation result alongside the new candidates from Step 6 so the
user sees both adds and revisions/removals in one diff.

### Step 8: Patch the generated block

Edit only the `## Project Conventions` section of `CLAUDE.md`. Do not touch any
other section.

**Hand-added bullets survive re-runs.** Any bullet already in the block that carries
a valid trailing ULID comment (`<!-- 01KXXXXX... -->`) was hand-authored against a
Decision entry and is subject to Step 7 re-validation like any other bullet — it is
not dropped merely because it was hand-added rather than machine-extracted. Only
bullets *without* a ULID (unprovenanced additions) must be removed.

**Criterion projections are NOT yours to edit.** A bullet whose provenance comment
carries a `derived-from <ULID>@<version>` marker (e.g.
`<!-- derived-from 01KXXXXX...@1 | approval: … | evidence: … -->`) is a **Commons
criterion projection**, written and owned by `scripts/commons_export.py` under a
human approval Decision (L3). The comment does not start with a bare ULID — it is
provenanced anyway; never classify it as unprovenanced. For these bullets:

- **Preserve verbatim** — text and comment byte-for-byte. Step 7's
  "revise the bullet, keep the entry ID" action does NOT apply: wording changes to
  a ratified criterion travel through a **superseding version** (a new approval
  Decision + `commons_export.py --version N+1`), never an in-place edit. The
  export verifies its projections content-exactly and will refuse over a
  hand-modified bullet.
- **Never remove**, except: (a) the bullet is already `[STALE — superseded by @N]`
  flagged (retirement of staled versions follows the Commons retirement policy),
  or (b) a human retirement decision explicitly directs it.
- **Route concerns forward** — if re-validation finds a criterion stale or wrong,
  record that as a candidate for a superseding version (cite this skill run as
  evidence); do not act on the block.
- Criterion bullets do not count against the 20-bullet/500-token cap below — the
  cap governs this skill's own extraction budget, not the export pipeline's output.

Cap: **20 bullets maximum, ≤ 500 tokens**. Each bullet must meet all seven
candidate acceptance rules:

1. **Durable** — likely to hold across multiple PRs or releases
2. **Actionable** — changes what an agent should do, not just what happened once
3. **Cross-cutting** — relevant to more than one narrow file or incident
4. **Evidence-backed** — supported by EITHER (a) a Decision entry, (b) ≥ 2
   independent entries, OR (c) a single canonical onboarding-seed entry from
   `architecture` / `risk-register` / `docs-contracts` / `team-map` /
   `entry-path` (the rule-establishing subset; see Step 4.5). Onboarding seeds
   carry curated, file:line-cited provenance and were authored specifically as
   durable foundation material, so they satisfy the auditability bar as a
   single source. Bullets cited from a seed should typically paraphrase an
   item from that seed's `Observed:` (active constraints) or `Drift findings:`
   section.

   Include the source identifier in an HTML comment next to the bullet. The
   identifier **must** be a Watercooler entry ID matching the regex
   `01[A-Z0-9]{24}` (a ULID like `01KQCJRTFGJPJ75APFG5ESGRJA`). PR numbers
   (`PR #685`), issue numbers (`#694`), plan paths (`dev_docs/plans/...`), and
   commit SHAs are **not** entry IDs and must be rejected — find the underlying
   Decision or seed entry via `watercooler_search` and cite that ID instead.
   If no Watercooler entry exists, the convention is not yet evidence-backed
   and does not belong in this block.
5. **Non-duplicative** — not already covered by a shorter existing instruction
6. **Tier-complete + axis-orthogonal** — when the convention names a specific
   producer, owner, file, flag, or behavior, stress-test by grepping the code
   for alternate / fallback paths at the same boundary. If an alternate path
   exists (e.g., a mutex'd open-core fallback for a premium daemon, an `else`
   branch handling a different tier, a deprecated symbol replaced by a new
   one), the bullet must either name both paths or be reframed at a higher
   abstraction level. Cite a second ULID covering the mutex / fallback /
   alternate-path contract when both paths exist. **Single-path bullets that
   mask alternate-path reality are not durable** — they mislead agents working
   on the other path.

   *Multi-concept claims need a stronger check.* When a single bullet equates
   two or more domain concepts in one statement (e.g. "X uses Y via Z",
   "open-core ⇔ local backend"), grep each concept independently and verify
   the equivalence is enforced in code. Many natural-language framings collapse
   independent axes (transport ↔ backend ↔ distribution; daemon ↔ tier;
   permission ↔ role) into a single phrase, producing false 1:1 equivalences.
   If the named axes are independently configurable in the schema or routed by
   different switches, the equivalence is false — disentangle the axes into
   separate bullets, or skip and point agents at the authoritative source
   (config schema, capability table) instead of restating a conflated rule.
   This applies equally to seed-derived candidates: the seed body is a
   summary, not a contract — verify the underlying code matches the seed's
   framing before citing.
7. **Provenance-preferred** — when both a hand-authored Decision and an
   `ExtractDecisionsDaemon`-extracted scribe twin exist for the same content,
   cite the hand-authored entry. Its body carries the original rationale and
   author voice; the extracted twin is auto-generated with weaker provenance.
   Find the source by reading the extracted entry's body — it includes a
   `Source entry: #N <ULID>` line naming the original. When in doubt, prefer
   the entry whose `agent` field is a human (e.g. `Claude Code (jay)`) over
   `ExtractDecisionsDaemon (system)`.

**Tier-complete worked example (2026-05-04)**: A first-pass bullet *"stance
emission lives on `ProjectCoordinatorDaemon`, not `PulseSnapshotDaemon`"* failed
Rule 6. Grepping `src/watercooler_mcp/daemons/__init__.py` revealed
`DecisionStanceDaemon` registers as the open-core fallback under a
registration-time mutex (lines 631-648). The corrected bullet named both
producers and cited a second ULID for the mutex contract. When a single ULID
cannot capture both paths, you need two. The first-pass citation also failed
Rule 7 — it pointed at the `ExtractDecisionsDaemon`-extracted scribe twin
(`01KPV2X1PPVJBK4RST8KTQPFD9`) instead of the hand-authored Decision
(`01KNWMHCNH11EJXGV8X300XP0B`) it mirrored.

**Axis-orthogonal worked example (2026-05-04)**: A seed-derived candidate from
`architecture` (`01KQN00MXR5VMPD86GHW8E2QHA`) proposed *"Hybrid memory transport
boundary: open-core uses local FalkorDB; hosted uses remote Graphiti via hybrid
HTTP transport."* Grepping `src/watercooler/config_schema.py:1395-1428`
revealed three independent axes, not one:
(a) `transport` — execution-routing mode for the local mcp process, values
    `stdio` / `http` / `proxy` / `hybrid` (default `stdio`);
(b) backend / extras — T1 baseline always available, T2 Graphiti+FalkorDB
    opt-in via the `[memory]` extra, T3 LeanRAG via `[leanrag]`;
(c) distribution — open-core vs hosted Railway deployment.
The bullet collapsed all three into a single false equivalence ("open-core ⇔
local FalkorDB"; "hosted ⇔ hybrid transport") that the code does not enforce —
open-core users can configure any transport and are baseline-only by default;
hosted runs `http` transport, not `hybrid`. The bullet was skipped in favor of
pointing at the config schema and `docs/MCP-CLIENTS.md` as authoritative.
**The seed entry itself had the framing error** — Step 4.5's "snapshot in
time" warning applies even to high-trust seeds; verify the underlying code
matches the seed's summary before citing.

### Step 9: Re-derive AGENTS.md and check budget

Repeat Phase 1 Steps 7–8: re-derive `AGENTS.md` from the updated `CLAUDE.md`, run
`wc -l -w`, trim if over budget, show the diff, and stop for review.

Update the footer comment to today's date:
```
<!-- agent-context-refresh:last-updated=YYYY-MM-DD -->
```

---

## Phase 3 — Skill-Surface Audit

Read-only. Detects drift between local skill files and the live MCP tool
surface, classifying every reference as OK, DRIFTED, COSMETIC, or
NEEDS-REVIEW. Produces a punch list; does not edit any file.

Phase 3 exists because watercooler-cloud tool consolidations (e.g. the
2026-05 PR1a–PR6 arc that took the surface from 52 to 34) retire tool names
behind non-discoverable alias forwarders. Per-PR doc sweeps catch most
references inside watercooler-cloud, but skill copies in
`~/.claude/skills/`, downstream repos, and personal `CLAUDE.md` files
routinely drift — silently when an alias still forwards, loudly when it is
removed in the next minor release. Phase 3 catches both classes.

Phase 3 reads only public-shippable sources: `TOOL_MATRIX` (live surface)
and `TOOL_ALIASES` (renamed-with-forwarder map) from `src/watercooler_mcp/`.
Both ship with every open-core install via Copybara's `src/**` glob, so the
audit works identically in private and public contexts. Behavioral-change
signals (same name, shifted semantics — e.g. PR2's keyword-search AND→OR
default) are intentionally not tracked here; they are handled at retirement
time by sweeping affected skill text in the same PR (see PR #828 as the
pattern), not by a post-hoc audit.

### Step 1: Enumerate the live MCP tool surface

The authoritative list of currently registered tools is whatever the agent's
session has loaded. Inside Claude Code, list every tool whose name matches
`mcp__[a-z0-9_-]+__watercooler_[a-z_]+` from the deferred-tool roster (the
"available tools" surface, not just the tools already invoked). Record this
as the **live set**.

If running outside an MCP-aware session, derive the live set from
`TOOL_MATRIX` keys specifically — that dict is the registration-only source
of truth for what FastMCP exposes. **Do not free-text grep
`capabilities.py`**: the file legitimately contains retired tool names
inside helper docstrings (e.g. `resolve_find_similar_capability()` mentions
`watercooler_find_similar` in its docstring even though the name is no
longer discoverable), and a bare grep would readmit those names into the
live set and silently misclassify stale references as OK.

Preferred — import the dict directly when the package is installed:

```bash
python3 -c "from watercooler_mcp.capabilities import TOOL_MATRIX; print('\n'.join(sorted(TOOL_MATRIX.keys())))"
```

Fallback — AST-parse the file when the package is not importable (e.g.
auditing a checkout without an env):

```bash
python3 - <<'PY'
import ast, pathlib
tree = ast.parse(pathlib.Path("src/watercooler_mcp/capabilities.py").read_text())
for node in ast.walk(tree):
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "TOOL_MATRIX"
    ):
        for key in node.value.keys:
            if isinstance(key, ast.Constant):
                print(key.value)
        break
PY
```

Both forms return only the dict keys, so retired names cited in helper
docstrings or in `_ARG_RESOLVERS` keyed by old name cannot leak into the
live set. (Aliases are also intentionally excluded — they are forwarders
registered by `AliasForwardingMiddleware`, not entries in `TOOL_MATRIX`.)

Cross-check: the count should match the surface count reported in the most
recent consolidation thread entry (34 after PR6 on 2026-05-22). A mismatch
means a registration changed since you last pulled — refresh the checkout
before continuing.

### Step 2: Load the alias registry

Read `TOOL_ALIASES` from `src/watercooler_mcp/aliases.py`. Each key is a
retired tool name; each value is a `ToolAlias` dataclass naming the
canonical replacement plus `rename_args` / `inject_args` for argument
adaptation and the `since` PR tag. Preferred — import directly when the
package is installed:

```bash
python3 -c "from watercooler_mcp.aliases import TOOL_ALIASES; \
  import json; print(json.dumps({k: vars(v) for k, v in TOOL_ALIASES.items()}, default=str))"
```

Fallback — AST-parse the file when the package is not importable (same
pattern as Step 1's `TOOL_MATRIX` fallback). The registry is declared as
an annotated assignment (`TOOL_ALIASES: dict[str, ToolAlias] = {...}`)
whose value is a dict literal of `<retired_name>: ToolAlias(...)` pairs;
walk for that shape and extract the four fields Phase 3 uses
(`canonical`, `rename_args`, `inject_args`, `since`):

```bash
python3 - <<'PY'
import ast, json, pathlib
tree = ast.parse(pathlib.Path("src/watercooler_mcp/aliases.py").read_text())

def lit(node):
    """Render an ast.Constant / ast.Dict as a JSON-safe value;
    fall back to source for callables (e.g. `guard=lambda args: ...`)."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        return {lit(k): lit(v) for k, v in zip(node.keys, node.values)}
    return ast.unparse(node)

aliases = {}
for node in ast.walk(tree):
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "TOOL_ALIASES"
        and isinstance(node.value, ast.Dict)
    ):
        for key_node, val_node in zip(node.value.keys, node.value.values):
            if not (isinstance(key_node, ast.Constant)
                    and isinstance(val_node, ast.Call)):
                continue
            aliases[key_node.value] = {
                kw.arg: lit(kw.value)
                for kw in val_node.keywords
                if kw.arg in {"canonical", "rename_args", "inject_args", "since"}
            }
        break

print(json.dumps(aliases, indent=2, default=str))
PY
```

`TOOL_ALIASES` is the source of truth for live alias forwarding (it's what
`AliasForwardingMiddleware` reads at request time). It covers the only
class of drift Phase 3 needs to label specifically: names that still call
through but are scheduled for removal one minor release later.

Retirements with no forwarder ("the name is just gone") are deliberately
not enumerated anywhere — when one of those names appears in a skill file,
Step 4 surfaces it as `NEEDS-REVIEW` and the user investigates by hand
(read `aliases.py` git history, or ask a maintainer).

### Step 3: Scan skill files for tool references

Grep these locations (paths exist when applicable):

- `~/.claude/skills/**/SKILL.md` and `**/SKILL.public.md` — user-level
  skills
- `~/.claude/CLAUDE.md` — user-global instructions
- `<repo_root>/.claude/skills/**/SKILL.md` — project-level skills
- `<repo_root>/CLAUDE.md`, `<repo_root>/AGENTS.md` — project agent context

Pattern (single grep, both forms):

```bash
grep -rnE "(mcp__[a-z0-9_-]+__)?watercooler_[a-z_]+" \
  ~/.claude/skills/ ~/.claude/CLAUDE.md \
  .claude/skills/ CLAUDE.md AGENTS.md 2>/dev/null
```

Strip the `mcp__<server>__` prefix so each hit is a bare `watercooler_*`
name comparable to the live set and manifest keys.

### Step 4: Classify each hit

For every grep result, look up the bare name in (live set, `TOOL_ALIASES`)
and apply this classification:

| Classification | Meaning | Trigger |
|---|---|---|
| **OK** | Name is in the live set | No action needed — skip from the report |
| **DRIFTED** | Call works via alias forwarder; will break when the alias is removed (one minor release later) | Name absent from live set AND present in `TOOL_ALIASES`. Report the canonical replacement plus any `rename_args` / `inject_args` from the alias entry. |
| **COSMETIC** | Historical reference in audit/inventory/change-log text — not a call site | Name present in `TOOL_ALIASES` AND the surrounding line is explanatory (e.g. inside a table comparing old→new, inside `.claude/skills/watercooler-tool-audit/`, or framed as past tense — "was replaced by", "is now"). Use judgment; when ambiguous, prefer DRIFTED. |
| **NEEDS-REVIEW** | Name absent from both the live set and `TOOL_ALIASES` | Either a retirement that landed without a forwarder, a typo, or a name from an unfamiliar fork. The user investigates by hand — read `src/watercooler_mcp/aliases.py` git history, search the consolidation thread, or ask a maintainer. The audit cannot disambiguate further from public sources alone. |

Behavioral-change tracking is intentionally not part of Phase 3. Shifts
that retain a tool's name (e.g. PR2's AND→OR keyword-search default,
PR1b's authority resolution) are addressed at retirement time by sweeping
affected skill text in the same PR — see PR #828 as the established
pattern.

### Step 5: Report

Render the result as a single markdown table sorted by `file:line`, plus a
per-file rollup with DRIFTED / COSMETIC / NEEDS-REVIEW counts and a
one-line suggested fix per file. For DRIFTED hits, include the canonical
replacement and any `rename_args` / `inject_args` the caller will need.

Table columns: `File`, `Line`, `Hit`, `Class`, `Canonical`, `Notes` (the
alias entry's `since` PR tag, when present).

Stop here. Phase 3 does **not** edit any file. The punch list is the
deliverable; the user decides whether to patch in-place or wait for the
next downstream skill-sync.

### Step 6: Footer (rarely needed)

If Phase 3 was the sole reason for the run and it surfaced no actionable
drift in CLAUDE.md / AGENTS.md, leave the `agent-context-refresh`
last-updated footer unchanged. CLAUDE.md/AGENTS.md staleness is the
domain of Phase 1's drift-fact step; Phase 3's domain is the wider skill
ecosystem.

---

## Degraded Paths

| Condition | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Watercooler unavailable | Proceed with static refactor; skip role fetch; note limitation | Stop with clear error — no evidence, no extraction | No impact — Phase 3 reads source files only, not the graph |
| T2 unavailable (open-core) | No impact | Continue with T1 evidence only; skip `smart_query` | No impact |
| No decisions since last update | No impact | Report "no new candidates found"; skip patch | n/a |
| No onboarding seeds present (no canonical topics from `watercooler-onboarding` exist) | Skip the seed reads in Step 4; note in the run report that running `/watercooler-onboarding` would strengthen the next refresh | Skip Step 4.5; report the gap in the candidate list and recommend running `/watercooler-onboarding` before the next Phase 2 | No impact |
| From-scratch seed mode + no onboarding seeds (hardest case — no `CLAUDE.md`, no seeds) | Build Repository Conventions / Development Workflow / Security & Publishing from source files and observed codebase patterns; mark every unsourced micro-convention `inferred` in the Step 8 diff; recommend `/watercooler-onboarding` then a Phase 2 run to firm them up | n/a | No impact |
| Outside Claude Code (no deferred-tool roster to enumerate the live set) | n/a | n/a | Fall back to the `TOOL_MATRIX` and `TOOL_ALIASES` AST/import paths in Steps 1 and 2; note the fallback in the report header |

---

## Conventions Not to Touch

Phase 2 should reject or leave in Watercooler when the candidate is:
- A one-off implementation detail
- An unresolved debate
- A daemon finding without human follow-up
- A policy that conflicts with current repo docs or code
- Already covered by a shorter existing instruction
