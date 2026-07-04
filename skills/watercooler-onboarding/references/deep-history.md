# Deep-history / decision-history reconstruction (`deep-history`)

Detail reference for SKILL.md **Step 2.4**. Forensically reconstructs, per code **segment**, the
repo's **decision and supersession history** from PR/commit history, as growing
`history-seg-<name>` threads that read like the decision log Watercooler would have captured had
it been in use during development.

**What this is, and is not (read first).** Value = a **faithful, cited reconstruction** +
recall/synthesis of the team's own (reconstructed) record. It is **NOT** an agent-capability gap
("an agent couldn't know this") and **NOT** a teardown ("the repo is broken"). A landed successor
is the *most* valuable signal — it explains the current code — so there is **no `forge-only` /
`no-landed-successor` keep-drop gate** (those v1 gates are removed). The method **infers intent
from change-progression**, which is plausibility-extraction; the entire design below exists to
keep inference fenced by deterministic fact and clearly labeled, so it never launders speculation
as a recorded decision.

## Method: two phases, split on fact vs inference

Do not collapse these. Phase 1 is the deterministic fact substrate; Phase 2 is bounded inference
that **cites** it. The split makes the honesty gate structural, gives full factual coverage at
low cost, and makes Phase 2 re-runnable against a stable ledger.

---

## Phase 1 — atomic change ledger (deterministic, FULL coverage, no LLM)

### 1.0 Gating
Run only with `deep-history`. Need `gh` + a resolvable source repo for PR/issue metadata;
without it, fall back to local `.git` only (reduced coverage — no PR bodies/review/issues) and
say so. Phase 1 covers the **whole** history; only Phase 2 is windowed.

### 1.1 Source resolution (forks)
Reuse Step 2.2's resolver; for a fork, mine the **parent**:
```bash
gh repo view --json nameWithOwner,isFork,parent \
  --jq '{repo:.nameWithOwner, isFork:.isFork, parent:(.parent.nameWithOwner // "none")}'
```
Record the resolved `OWNER/REPO` in `history-overview`; flag fork parents (provenance is
upstream).

### 1.2 Time-aware segments

**Fix the target before you start (the test you will otherwise fail):** §1.2 is deterministic, so
a fresh agent running only this reference must derive the *same segment set* every time. If your
run's segment set could not be reproduced by someone who never saw this repo's recent commits —
e.g. because you weighted "what's being worked on now" and spotlighted the active feature — you
are running a different, non-deterministic algorithm and the output is wrong, even if it looks
complete. The number of `history-seg-*` threads is an output of *this* step; it is not yours to
choose.

Partition the codebase into tracked **segments** (subsystems) — but segments **evolve**, so
they are time-aware, not a static snapshot:
- Derive from the **full-history co-change graph** (files that change together) + directory
  structure — not just the current tree (early code may have moved).
- **Follow renames** (`git log --follow`, `--find-renames`) so a file's segment membership is
  continuous across moves.
- Treat **segment birth / split / merge as first-class moments** (e.g. `views.py` → `views/`):
  record them in the segment thread ("segment emerged from X at PR #N").
Record the segment map (and its changes over time) in `history-overview`. Datasette example
segments: `views-dispatch`, `write-sql-path`, `permissions-auth`, `csrf`, `plugins-hooks`,
`facets-filters`, `rendering-formats`, `cli`, `config-metadata`, `internals-api`, `ci-release`.

**Mandatory artifact gate (do this before writing a single `history-seg-*` entry).** Paste into
`history-overview`: (a) the exact `git log` / co-change command(s) you ran, (b) their raw output
(or a faithful reduction of it), and (c) the derived segment list with the file globs mapped to
each segment. This is not documentation — it is the *proof that §1.2 actually ran*. A
`history-overview` whose segment map is asserted without the command output is an invalid run;
regenerate it. **If your segment list has length 1 for a repo with more than one top-level source
unit or more than ~100 commits, you have not run §1.2 — you have guessed the active front.** The
segment set must be reproducible by an agent who never saw this repo's recent activity; if it
could only have been produced by weighting "what's being worked on now," it is wrong by
construction (see Acceptance, below).

### 1.3 Ordered timeline (the chronology)
**PR is the default unit**, ordered by merge/close date; drop to commit level when a PR bundles
distinct intents (`gh api repos/$REPO/pulls/$N/commits` exposes pre-squash branch commits).
**Squash-merge caveat:** on a squash-merged `main` all commits share one import timestamp, so
chronology comes from **PR `mergedAt`/`closedAt` + `gh` author dates + the PR's own commit
series**, never `git log` order on main.

### 1.4 Per-item facts (no interpretation)
For each PR/commit, record only facts:
- segment routing (changed files → segment map, rename-aware; may touch >1 segment);
- PR/issue numbers + state, SHAs, files, **symbols added/removed/renamed** (`git log -S '<sym>'`,
  `--find-renames`), diff size, co-changed files;
- whether a rationale source *exists* (PR body non-empty? linked issue? review comments?) — a
  pointer, not its content (content is Phase 2).

### 1.5 Deterministic supersession graph
Mechanically link, at symbol/file granularity: `introduced@<PR/SHA>` → `rewritten@…` →
`removed@…` (pickaxe `git log -S`, `--diff-filter=D`, `--find-renames`). These are **facts**.
Phase 2 will interpret *which* of these are decision-supersessions vs incidental refactors — it
may not assert a supersession the graph doesn't contain.

### 1.6 Significance ranking
Rank nodes (do not drop): architectural-file impact, discussion depth (comment count),
rationale-marker density in messages (reuse the `detect-decisions` lexicon if present),
supersession-node, churn. Phase 2 spends its budget top-down this ranking.

**Phase-1 output:** a per-segment ordered ledger + the supersession graph + the ranked node
list. This is a working substrate (a scratch artifact / `history-overview` appendix), not a
narrative. It must be complete and checkable.

---

## Phase 2 — decision-evolution narrative (bounded LLM, cites the ledger)

Run **only** over the ranked-significant + supersession nodes (window/cap per §B). For each
node, in chronological order within its segment:

### 2.1 Read intent
Title + body + **linked design issue** (richest — resolve via GraphQL `closingIssuesReferences`,
body-regex fallback) + review discussion + the diff shape from the ledger.

### 2.2 Classify the moment (for that segment's story)
`new-capability` | `refactor` | `supersession` (must correspond to a Phase-1 supersession-graph
edge) | `gotcha/post-mortem` | `rejection/deferral` | `convention-set` | `segment-topology`
(birth/split/merge).

### 2.3 Append a segment-thread entry — the honesty gate (§A template)
Cite Phase-1 facts; quote recorded rationale or declare it absent; label + evidence-cite +
confidence-score all inferred intent; link supersession to the prior entry.

---

## Execution — a single `deep-history` invocation writes everything (no hand-authoring)

The writes are **intrinsic to the skill's own run** — there is no legitimate step where a
coordinator collects prose and hand-authors or consolidates the entries. One invocation:
1. **Phase 1 (once):** build the shared deterministic ledger + supersession graph + ranked nodes
   over the full history (§1).
2. **Phase 2 (compute + write in parallel — fan out one worker per segment):** segment workers
   COMPUTE their per-moment entries in parallel (the expensive narration + PR/issue reads) AND
   write them concurrently via `watercooler_say`. This is now safe: the single-writer group-commit
   committer (#906/#908) owns the shared `watercooler/threads` worktree — workers only append to the
   append-only graph (fast, under a short per-topic lock) and enqueue a commit task; the daemon is
   the *only* writer that touches the orphan worktree/index/HEAD, batching and committing under the
   worktree lock. So the concurrent-clobber race that regressed `history-seg-cli` 15→2 (#904) and
   the synchronous-summarizer 50s timeout / stale-lock self-block (#903) are both structurally gone
   — no per-worker `git worktree` isolation needed. **Do NOT re-introduce per-worker
   `watercooler_sync_repair` self-flush**: workers breaking each other's worktree locks mid-commit
   was the original #904 root cause, and re-adding it would reintroduce the data loss even with the
   single-writer committer in place. Each entry is still its OWN `history-seg-<name>` per-moment
   entry (§A template; native supersession links per §D) — no coordinator authors or collapses them.
3. **`history-overview` is generated from the actual write results** (per-segment moment counts,
   recorded-vs-inferred + pure-inference tallies, calibration result, provenance) — not
   pre-written.
4. **Step 5.5 tag-verify** (`onboarding` + `history`) over every written `history-*` topic.
5. **Phase 3 (derived arc layer):** after the moments are committed + tag-verified, generate one
   readable per-segment **arc** from the committed moments (see the Phase 3 section) — secondary,
   traceable, never replacing the moments.
6. **Write-path verification (mandatory — confirm the committer drained):** writes are accepted
   *durably* (into the append-only graph + the persisted commit queue) but committed/pushed to
   origin *eventually* by the committer, so after all writes **audit the `origin` `entries.jsonl`
   count for each `history-*` thread against the expected count** to confirm the queue fully drained
   before declaring the run complete — the write path is now eventually-consistent, not synchronous.
   On a mismatch: a still-draining or stuck backlog → **first wait for the committer (with #908's
   reconcile sweep it auto-drains; a count mismatch right after a run usually just means the queue
   hasn't flushed yet)**, then — only if genuinely stuck — the **coordinator** runs
   `watercooler_sync_repair` + an idempotent flush write **post-run, single-threaded (NEVER per
   worker, never during the fan-out)** and re-verifies. A run that leaves any thread short on origin
   is not yet complete. (Clobber/truncation
   is no longer expected — the single-writer committer eliminated that race — so a true regression
   now indicates a committer bug, not a fan-out hazard.)

**Reproducibility.** Phase-1 facts are deterministic (same git/gh ledger every run → moments +
citations stable); Phase-2 narration is LLM prose (wording varies). A re-run yields the same
per-segment moments / citations / lineage — finer-grained than any hand-consolidation — differing
only in wording. **Acceptance: a fresh agent reading ONLY this reference + SKILL.md (no thread, no
chat context) can run it and produce per-moment per-segment `history-seg-*` threads + the
canonical seeds, structurally identical across runs.**

---

## Phase 3 — derived arc narrative layer (readable, on top of the committed moments)

Closes the readability gap: the per-moment logs are honest + queryable but don't read like a
subsystem-evolution *arc*. Phase 3 generates a **secondary, fully-traceable arc** from the
**committed** moments — never hand-authored, never replacing them.

**When / inputs.** Runs once per segment (may fan out) AFTER Phase 2 commits + Step 5.5
tag-verify, reading that segment's committed `history-seg-<name>` entries via
`watercooler_read_thread` (each `entry_id`, `When · PR# · SHA`, kind, recorded-rationale,
inferred-intent + confidence, supersedes). The moments stay the source of truth; the arc
introduces nothing not already in them.

**Output — one arc entry per segment** into the `history-synthesis` thread (§E), optionally also
written into `history-seg-<name>` and **pinned** (`watercooler_annotations … kind=pinned`) so the
dashboard leads the thread with arc-then-moments.

```
Arc: <segment> — <one-line throughline>
Span: <first date> → <last date> · N moments · M supersessions
Narrative:
  <2–5 short paragraphs, reconstruction voice. EVERY substantive claim carries an inline
   moment ref [entry_id]; recorded rationale may be quoted; inferred reads stay explicitly
   marked "(inferred)". No smoothing of guesses into assertion.>
Lineage: moment_1 [entry_id] -> moment_2 [entry_id] -> …   (the supersession spine)
Open / unsettled: still-open issues, stalled migrations
Confidence: recorded-vs-inferred ratio carried up from the moment-level tally
Provenance: every moment entry_id this arc summarizes
```

**Honesty (carried up from §A — the laundering guard the per-moment form protects):**
1. Reconstruction voice, never the maintainer's first-person.
2. Every narrative claim traces to a moment (inline `[entry_id]`); no claim absent from the
   atomic layer.
3. Inferred stays marked inferred; quoted rationale may be quoted — never dissolve inference into
   confident prose.
4. The arc is explicitly **secondary**: it summarizes, never replaces, the per-moment log; every
   sentence is verifiable against a linked moment.

**Generation discipline.** Produced BY the skill from the committed moments — a bounded LLM pass
over real entry data, reproducible in structure (same moments → same arc skeleton + lineage; only
prose wording varies). Not pre-written, not coordinator-consolidated outside the run. This is what
makes it legitimate where the earlier hand-written arcs were not: the same readable output,
derived by the skill from the durable atomic record.

---

## A. Entry template (reconstruction voice — the load-bearing gate)

```
Moment: <segment> — Reconstructed: <what changed, analyzer voice>   [kind: new|refactor|supersession|gotcha|rejection|convention|segment-topology]
When:        <date> · PR #N (state) · issue #M · commits SHA…
Recorded rationale:  "<verbatim quote from PR/issue/commit/review>" — <deep link>   |   not recorded
Inferred intent:     <evolution read> — evidence: <ledger fact / quote / diff fact>  |  PURE SEQUENCE-INFERENCE (no recorded rationale)
                     confidence: high|med|low     [if ≥2 readings fit equally, list both — do not synthesize]
Supersedes:          <prior entry_id in this thread> — <what it changes>   (must match the §1.5 graph)   |   (none)
gap-class:           git-recoverable | forge-only      (INFORMATIONAL only — never a keep/drop gate)
```

**Rules.** (1) **Reconstruction voice**: entries read as analyzer reconstruction, never the
maintainer's first-person words ("Reconstructed: dispatch changed X→Y", not "we moved dispatch
to Y because…") — impersonating recorded intent is the anthropomorphism / false-authority
failure. (2) Recorded rationale is **quoted or declared absent**; never paraphrased into a
decision. (3) Inferred intent is **always labeled, evidence-cited, confidence-scored**; the
weakest kind — `PURE SEQUENCE-INFERENCE` (diff sequence only, no recorded rationale) — is marked
as such and its proportion surfaced in the overview. (4) **Never fabricate a "why."** (5)
Competing intent-readings are **preserved, not synthesized**.

## B. Selection / bounding / cost
Phase 1 = full coverage (cheap, deterministic). Phase 2 = bounded: window it and **log the
window** (since first commit for small repos; since vN / last ~200 PRs for large). Per segment,
cap to the most significant moments + supersession nodes with an explicit `+N minor changes
folded` line (no silent caps). Rank by signal (§1.6), never by a pass/fail gate.

## C. Self-checks + calibration (instead of "feels right")
- **Segment-count sanity (run FIRST):** count your `history-seg-*` threads. If the count is 1, or
  is small relative to the number of top-level source units / co-change clusters, **halt and
  re-run §1.2** — single/under-segmentation is the dominant failure mode of this skill and it
  produces output that looks complete. A correct run on a multi-subsystem repo yields one thread
  per co-change cluster, *including stable and historical subsystems* (Phase 2 is capped per
  segment per §B, never dropped). "This subsystem isn't the active front" is **not** a reason to
  omit its thread — that keep/drop gate was removed (§ intro; §1.6 "do not drop"). Verify the
  §1.2 artifact gate (command + output + segment list) is present in `history-overview` before
  proceeding.
- **Fact-resolution:** every `Inferred intent` resolves to ≥1 Phase-1 ledger fact or a quoted
  rationale; free-floating inferences are invalid.
- **Lineage consistency:** every `supersession` moment corresponds to a §1.5 supersession-graph
  edge; no invented lineage.
- **Calibration probe:** pick one segment with recorded rationale (a linked design issue),
  reconstruct its intent **without reading the issue**, then compare to the issue. Report the
  match rate in `history-overview` — it quantifies how often pure sequence-inference matches
  recorded truth (the Epistemia-risk meter).

## D. Native watercooler machinery (reuse, don't reinvent)
- A moment that records a genuine decision with quoted rationale → write as a `Decision` entry
  using **native supersession links** (not a text-only `supersedes:` field), so the
  reconstructed history is graph-queryable like live history.
- Reuse the **`extract-decisions` rubric/lexicon** for rationale detection rather than
  re-deriving it.

## E. Outputs
Step 5 write machinery + Step 5.5 tag-verify; topics prefixed `history-`, tagged `onboarding` +
`history`; honors dry-run (print planned entries).
- **`history-seg-<name>`** — one growing decision/supersession log per active segment, read
  top-to-bottom as that subsystem's evolution. **ONE ENTRY PER MOMENT (§A) — never consolidate a
  segment into a single summary entry**; entries are chronological with **native supersession
  links (§D)** between successive moments. Type `Note` (decision-recording entries may be
  `Decision`), Role `scribe`.
- **`history-overview`** — segment map (+ topology changes; each segment line indexes its arc and
  moment count, `→ [arc entry_id] + N moments`), timeline window + coverage (PRs walked / total,
  commit drilldowns used), method, **recorded-vs-inferred ratio + pure-inference proportion**,
  **calibration result**, squash-merge note, caveats.
- **`history-synthesis`** — the **Phase 3 arc layer**: one readable, fully-traceable arc entry per
  segment, generated from that segment's committed moments (template + honesty rules in the
  Phase 3 section). Secondary by rule — summarizes, never replaces, the per-moment logs; every
  claim links to a moment `[entry_id]`. Type `Note`, Role `scribe`.

## F. Format discipline (from spyc)
Keep the temporal/lineage structure (that is the value), but: structured + scannable +
PR/SHA/issue-cited; **no literary voice** ("the surface that kept saying it wasn't done");
organize by **segment**, not arc numbers; one moment = one queryable entry.

## G. Summarizer grounding (RESOLVED — #902/#909/#912; no skill-side action)
Historically the auto-summarizer bled off-topic "OAuth2/JWT/refresh-rotation" boilerplate into
`history-*` summaries (#902). This is **fixed on `main`**: structured / `history-*` / `onboarding-*`
entries now **skip the LLM summarizer** (`enrich_structured=False`, #906); the summarizer is
grounded with a fabrication guard so it cannot invent auth mechanisms absent from the source (#909);
and pre-existing poisoned summaries on structured entries are cleared on enrichment (#912). **No
skill-side workaround is needed — do not emit a summarizer-bug caveat.**

## H. Worked example / fixture (datasette `simonw/datasette`)
Validate the `views-dispatch` segment thread, top-to-bottom, reconstructs:
1. legacy `BaseView` / `DataView` dispatch (the original capability);
2. → the new async **`View`** base class (#2078 / #2080; **recorded rationale** in design issue
   **#878**, 26 comments — quote it) — `kind: supersession`, `Supersedes:` the legacy entry,
   matching the §1.5 graph;
3. → the **still-stalled migration** (some views not yet ported) — `kind: refactor`, inferred
   from the diff/sequence, labeled;
with the closed-unmerged `AsyncBase` / **#1512** present as a **`shipped-elsewhere`** node in the
lineage (extracted to a library) — **kept as a lineage node, NOT dropped** (the v1 gates are
gone). Every inferred step cites a ledger fact or quoted rationale; #878's text is quoted, not
paraphrased. `gap-class` is recorded but informational.
