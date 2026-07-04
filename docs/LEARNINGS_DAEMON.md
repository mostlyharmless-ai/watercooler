# Learnings daemon

The Learnings daemon is the **extract/propose tier** of the Commons loop: it
reads closed threads and surfaces the reusable learning each one did — or didn't
— capture. It is a sibling to the Decision family (detector → extractor →
stance), but where those mine *decisions* from entries, the Learnings daemon
mines *learnings* from whole resolved threads.

This document is the deep companion to the user-facing
[Daemons reference](DAEMONS.md#learnings-learnings). Start there for enabling,
configuration, and finding categories; read this for the model behind them.

> **Status.** This is the Phase 1 build (deterministic capture-gap + index layer
> plus an optional shadow-synthesis layer), authorised on thread
> `workflow-packs-prepare-work-discovery-2026-05-29`. It is **default-off** and
> **watch-only** (`emit_mode="monitor"`): reversible L1 graph annotations and
> daemon findings only, never a thread entry. Thread-visible emission is gated
> on an explicit human go-decision (see [The emission gate](#the-emission-gate)).

## The capture model

For each **closed** thread (status `CLOSED`, `RESOLVED`, `MERGED`, or `DONE`),
the daemon produces one verdict, in strict priority order:

1. **`has_learning` — matched solution write-up.** The thread references a merged
   PR that has a `pr:`-tagged write-up in one of the configured `solutions_dirs`.
   This is the highest-fidelity signal: the learning is already captured.
2. **`has_learning` — in-thread lesson.** An entry in the thread carries an
   explicit lesson section (a heading that *is* `## Lesson`, `## Lessons learned`,
   or `## Learnings` — anchored to the end of the line so `## Learnings daemon`
   does not false-match).
3. **`capture_gap`.** The thread references a merged PR but has neither a matched
   write-up nor an in-thread lesson — a candidate *missing* learning.
4. **`not_applicable`.** No PR reference at all; nothing to assess.

PR references are extracted conservatively from entry titles, summaries, and
bodies: GitHub pull URLs (`.../pull/950`), explicit `PR #950` / `PR-950` forms,
and squash-merge subjects (`subject (#950)`). A bare `#950` is trusted only in
the title of a `PR`-type entry, to avoid matching issue references.

## Solution write-ups: the PR join

The "consume-not-duplicate" half of the model. The daemon walks each configured
directory recursively for `*.md` files, reads the YAML frontmatter, and indexes
any file declaring a `pr:` field into a `{pr_number: path}` map. A thread whose
PR is in that map already has a captured learning, so it is not a gap.

```yaml
---
module: daemons
pr: 952
tags: [capture-gap, indexing]
---
# Deterministic capture-gap + index layer
...
```

`solutions_dirs` is **code-repo-relative** and scanned **in order**, with
first-dir-wins on a duplicate `pr:`. The open-core default is `["docs/solutions"]`
(Compound Engineering's convention); override per project — this repo uses
`dev_docs/solutions`, pinned in its committed `.watercooler/config.toml`. Missing
directories are skipped, so the daemon degrades cleanly to graph-only signals on
a hosted variant with no code checkout.

Matched paths are stored **prefixed with their `solutions_dir`** (e.g.
`dev_docs/solutions/daemon-patterns/x.md`), so the `solution-doc:<path>`
annotation and `matched_doc` finding field stay unambiguous when more than one
directory is configured.

### The `capture_gap` empty-index guard

`capture_gap` fires only when the project **demonstrably writes solution docs at
all** — i.e. the solutions index is non-empty. If the configured `solutions_dirs`
produce an empty index, the `capture_gap` criterion is dropped (and the
suppression is logged at debug level).

The reason: an empty index cannot distinguish *"this project doesn't write
solution docs"* from *"write-ups exist but the configured directories missed
them"* (a misconfigured path, or the open-core default on a repo that uses a
different directory). Without the guard, every PR-bearing closed thread would
false-fire `capture_gap` against the project's own, perfectly good write-ups.

A consequence worth knowing: the guard is **bootstrap-silent**. A project that
has adopted the convention but not yet written its *first* write-up will not get
a `capture_gap` until at least one indexed doc exists. That is the intended
semantics, not a bug — the debug log line explains the silence to an operator.

## The five-stage pipeline

The daemon is structured as `sources → candidate-gen → score/filter →
authority-policy → emit`, so future analyzers can become configs on the same
engine rather than new code paths:

| Stage | What it does |
|---|---|
| **sources** | Resolve the threads dir + code root; list closed-thread topics; build the solutions index. |
| **candidate-gen** | For each closed thread, read entries and extract PR references. |
| **score/filter** | `assess_thread_learning` applies the criteria to produce the verdict. |
| **authority-policy** | The empty-index guard; `emit_mode` gating; the synthesis cost caps. |
| **emit** | Reversible L1 annotations + findings. (Thread-visible emission is gated off.) |

### Criteria-as-data

Each classification rule is a named `LearningCriterion` carrying an `id`, a
`kind` (`positive` / `capture_gap`), a `severity`, and a maturity `status`
(`experimental` / `stable` / `deprecated`). Every emitted finding traces back to
the `id` of the criterion that fired (`triggering_criterion_id`). The criteria
list is a parameter to `assess_thread_learning`, so maturity is a field to filter
on — not a fork in the code. The empty-index guard is exactly this: it removes
the `capture_gap`-kind criterion from the active set for that tick.

## Shadow synthesis (the propose layer)

When `synthesize_notes` is on, a capture-gap thread also gets an LLM pass that
*drafts the missing learning* — the "would-have-written" note. Under `monitor`
mode this is recorded as a `shadow_learning_note` **finding**, never a thread
Note. It is the artifact a human reviews at the emission gate.

A draft must clear five validity gates to pass (mirroring the Decision
Extractor's resist-laundering posture):

| Rejection reason | Meaning |
|---|---|
| `no_llm_response` | The LLM returned nothing (endpoint down / empty). |
| `unparseable_response` | The response was not valid structured output. |
| `below_confidence` | The draft's self-rated confidence is below `min_confidence`. |
| `no_root_cause_or_lesson` | The draft is missing a stated root cause or lesson. |
| `ungrounded_quotes` | No `verbatim_quote` is grounded word-for-word in the thread source. |

The grounded-quote gate is the load-bearing one: a draft that cannot quote the
real thread verbatim is rejected, which is what keeps synthesis from inventing a
plausible-but-unfounded learning.

### Cost controls

Synthesis issues live LLM calls, so it is bounded three ways:

- **`max_syntheses_per_tick`** (default 5) — a hard cap on synthesis calls per
  tick. A draft rejected by the confidence floor still cost a call, so it still
  charges a slot; an unreachable client does not.
- **`max_tick_duration`** (default 120s) — a **soft, between-topic** wall-clock
  budget. It is checked at the top of each topic iteration, so an in-flight call
  can overrun by up to one synthesis; it is a burst bound, not a hard ceiling.
  The scan restarts from the first topic each tick (see *Known limits*).
- **Once-per-tick availability probe** — `is_available()` is a live probe, so the
  daemon resolves it at most once per tick (lazily, on the first synthesis-
  eligible gap), not once per gap thread.

Across ticks, total spend is additionally bounded by finding dedup: once a
thread's `shadow_learning_note` is recorded, it is not re-synthesized, so total
calls approach the count of distinct capture-gap threads, drained at
≤ `max_syntheses_per_tick` per tick.

## The emission gate

The daemon honours the agent-authority ladder (guardrail
`01KS0JTK0RT4EC0M92PMX19XRA`): it never writes Decision / Closure / supersession
/ status, and L3 promotion stays human (`update-agent-context` /
`watercooler_promote_candidate`). The `emit_mode` dial is the graduation control:
`monitor` (the validated default) writes only reversible annotations + findings;
`warn` / `enforce` are reserved for a later increment that would emit
thread-visible learning Notes.

**Enabling thread-visible emission is a human go/no-go — not a measured eval.** A
precision-eval harness was considered and dropped as circular (a human judging
drafts to decide whether humans should judge drafts has no external oracle).
Instead, before `emit_mode` advances past `monitor` with `emit_learning_notes`,
a human reviews real shadow-mode drafts and records an explicit go-decision. No
corpus, no threshold — a logged human look that gates the flip.

## Known limits (deferred follow-ons)

- **No duration-guard resume cursor.** The `max_tick_duration` cutoff restarts
  the scan from the first topic each tick. The *synthesis* budget self-heals via
  finding dedup, but the duration cutoff does not — so a corpus whose cheap I/O
  scan alone exceeds the budget would permanently starve tail topics. Closing
  this needs a persisted resume offset (the same persisted-state shape as a
  per-day synthesis cap), tracked as a follow-on.
- **Reserved promotion-candidate knobs.** `emit_promotion_candidates` and
  `recurrence_threshold` exist in config but have no runtime effect today; only
  `emit_learning_notes` is wired into the current thread-visible learning Note
  gate.

## Provenance

Design and build history live on thread
`workflow-packs-prepare-work-discovery-2026-05-29`. The Phase 1 build was
authorised in entry 13 (`01KV2D50W2FYBW0SA6Y9Y081V9`); the emission-gate
resolution (drop the eval harness, keep the human go/no-go) is entries 21–22
(`01KV55H4KC3FW7MN03JJTPSK9Y`, `01KV5YJX15Y3FWZRJN4XP0S9WT`).

## Related documentation

- [Daemons](DAEMONS.md) — enabling, configuration, finding categories
- [Decision gates](DECISION_GATES.md) — the sibling Decision Extractor's validity model
- [Configuration](CONFIGURATION.md) — full config reference
