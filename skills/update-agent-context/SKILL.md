---
name: update-agent-context
description: |
  This skill should be used to keep CLAUDE.md and AGENTS.md compact, current, and
  internally consistent. It runs in two phases: Phase 1 performs a one-time structural
  refactor of CLAUDE.md using a Karpathy-inspired behavioral scaffold and derives
  AGENTS.md from it by stripping Claude Code-specific sections. Phase 2 extracts
  durable project conventions from Watercooler Decision entries since the last update
  and patches a bounded generated section. Use when CLAUDE.md has grown verbose or
  stale, when AGENTS.md has drifted from CLAUDE.md, or when significant project
  decisions have accumulated since the last refresh.
---

# Update Agent Context

## Overview

Keeps `CLAUDE.md` and `AGENTS.md` short, current, and auditable. `CLAUDE.md` is the
source of truth; `AGENTS.md` is derived from it. The skill edits the working tree and
shows a diff — it does not commit. Use `/ship` to commit after review.

Arguments: `/update-agent-context [--phase1 | --phase2 | --both]`

Default when no flag is given: ask the user which phase to run.

---

## Phase 1 — Structural Refactor

Run once (or after a major tool or role refactor) to rewrite `CLAUDE.md` using the
approved structure and derive a fresh `AGENTS.md`.

### Step 1: Measure

Compute current sizes:

```bash
wc -l -w CLAUDE.md AGENTS.md
```

Record the numbers. The target for `CLAUDE.md` post-refactor is **≤ 300 lines and
≤ 2,000 words** (proxy for 2,000–3,000 tokens). Report both before and after.

### Step 2: Record drift facts

Read `CLAUDE.md` and `AGENTS.md` and note concrete mismatches that must be fixed,
for example:
- `AGENTS.md` opens with file-based framing while `CLAUDE.md` says graph-first.
- Stale "stdlib-only" absolutes that conflict with current dependencies.
- Sections that duplicate role prose or tool inventory already covered by MCP.

### Step 3: Fetch live role inventory (if Watercooler is available)

Load and call `mcp__watercooler__watercooler_roles` with `code_path` set to
the repo root. Use the returned role names and summaries to confirm the six canonical
roles (`planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`). Do not copy
role prose into `CLAUDE.md`; point to `watercooler_role_details` instead.

If Watercooler is unavailable, proceed with local files only and note the limitation.

### Step 4: Verify project facts against source

Before drafting prose, ground every concrete claim that will appear in the **Project
Snapshot** and **Development Workflow** sections. Read these source files directly
and use only what they say — do not carry over claims from the old `CLAUDE.md` /
`AGENTS.md` without re-checking.

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

**Watercooler Protocol** — condense to a single focused section. Must preserve:
- Identity setup: per-call `agent_func` in the format `<platform>:<model>:<role>`. There
  is no `set_agent` / `watercooler_set_agent` shortcut — set `agent_func` on every
  write call (`say`, `ack`, `handoff`, `set_status`). Verify against the registered
  tool surface in `src/watercooler_mcp/capabilities.py` before claiming a setter
  tool exists.
- `code_path` discipline: always set to the repo root being worked on.
- Graph-first reads: baseline graph (JSON) is the sole source of truth; `.md` files
  are write-only projections. Always start with `summary_only=true`.
- Mandatory MCP use: never read/write thread files directly.
- Write discipline: `say` / `ack` / `handoff` / `set_status` only via MCP tools.
- `Spec:` marker: first line of every entry body.
- Pointer to `watercooler_role_details` for full role behavior — do not repeat prose.
- **Intent-first modification rule**: before modifying existing code or state, query
  watercooler threads and semantic search to understand the *intent* behind the current
  state. Do not infer design constraints from code alone.
- Proactive recall: run `watercooler_smart_query` before starting significant work.

**Sections to eliminate or replace with pointers:**
- Full docstring / type-hint examples → keep as one-line bullet, remove code blocks
- Testing guidelines with code blocks → keep as bullets only
- Module organization inventory → remove (derivable from codebase)
- Role definitions section → replace with pointer to `watercooler_role_details`
- Full tool usage protocol → replace with condensed Watercooler Protocol section
- Slack integration architecture → remove or link out; not needed in `CLAUDE.md`

### Step 6: Validate invariants

Before writing, verify the draft preserves all of these. If any is missing, add it.

- [ ] Graph-first reads stated; `.md` files are projections, not source of truth
- [ ] Mandatory MCP use for all thread / watercooler operations
- [ ] Identity setup names per-call `agent_func` only — no `set_agent` /
      `watercooler_set_agent` reference (verify against
      `src/watercooler_mcp/capabilities.py`)
- [ ] `Spec:` marker write discipline preserved
- [ ] `code_path` alignment rule
- [ ] Intent-first modification rule (query threads before changing state)
- [ ] No repeated role prose (pointer to `watercooler_role_details` only)
- [ ] No large tool inventories
- [ ] No stale "stdlib-only" absolutes that conflict with current deps
- [ ] Project Snapshot facts match source: Python range matches
      `pyproject.toml:requires-python`, OS list matches `.github/workflows/`,
      test-layout examples reference paths that actually exist
- [ ] `## Project Conventions` block present (may be empty; Phase 2 fills it)
- [ ] Footer comment: `<!-- agent-context-refresh:last-updated=YYYY-MM-DD -->`

### Step 7: Derive AGENTS.md

Generate `AGENTS.md` from the new `CLAUDE.md` by stripping Claude Code-specific
sections **by header name**:

Sections to strip:
- `## Claude Watercooler Protocol (Session Rules)` → replace with a condensed
  equivalent that removes MCP tool invocation mechanics but keeps the behavioral
  rules (graph-first, intent-first, proactive recall).
- Any section that references Claude Code hooks, `ToolSearch` protocol, or
  MCP-client-specific identity details.

Preserve: repo facts, graph-first claims, security rules, commit policy, Karpathy
behavioral principles, mandatory Watercooler MCP requirements, intent-first rule.

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
  via `watercooler_acknowledge_finding`, so the skill doesn't repeatedly re-surface
  the same items run after run.

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

## Degraded Paths

| Condition | Phase 1 | Phase 2 |
|---|---|---|
| Watercooler unavailable | Proceed with static refactor; skip role fetch; note limitation | Stop with clear error — no evidence, no extraction |
| T2 unavailable (open-core) | No impact | Continue with T1 evidence only; skip `smart_query` |
| No decisions since last update | No impact | Report "no new candidates found"; skip patch |
| No onboarding seeds present (no canonical topics from `watercooler-onboarding` exist) | Skip the seed reads in Step 4; note in drift facts that running `/watercooler-onboarding` would strengthen the next refresh | Skip Step 4.5; report the gap in the candidate list and recommend running `/watercooler-onboarding` before the next Phase 2 |

---

## Conventions Not to Touch

Phase 2 should reject or leave in Watercooler when the candidate is:
- A one-off implementation detail
- An unresolved debate
- A daemon finding without human follow-up
- A policy that conflicts with current repo docs or code
- Already covered by a shorter existing instruction
