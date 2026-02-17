# Decision Trace Extraction Guide (Watercooler)

This document is a **working prompt and rubric** for agents that process Watercooler threads and extract **decision traces**.

Its goals are:

* Extract *only real decisions*, not speculation
* Preserve provenance and uncertainty
* Prevent false authority and retroactive certainty

---

## 1. Definition: Decision Trace

A **decision trace** is the minimal, structured record that allows a future reader (human or AI) to understand:

* **What was decided**
* **Why it was decided**
* **What alternatives or constraints shaped it**
* **Where the decision came from in the source material**

A decision trace must be *reconstructible* without access to anything beyond the watercooler thread it was extracted from. Watercooler threads are the source of truth — they are the original conversation, not a transcript of one.

---

## 2. Extraction Scope

This guide covers two scenarios:

1. **Mining implicit decisions**: Extracting decisions embedded in entries typed as `Note`, `Plan`, or other non-Decision types. These are choices that were made but not flagged as decisions at write-time.
2. **Re-scoring existing decisions**: Evaluating entries already typed `Decision` against the quality rubric to assess completeness and confidence.

An entry already typed `Decision` by its author carries stronger prior authority than a decision inferred from a Note or Plan. When re-scoring existing Decision entries, start from a baseline of score 3 (the author's explicit intent) and adjust based on the rubric. This baseline can be downgraded below 3 if the entry is ambiguous, lacks rationale, or is contradicted by a later entry in the same thread.

---

## 3. Decision vs Non-Decision

### A decision **is**:

* A committed choice or resolution
* Expressed with clear intent ("we decided", "we will", "the plan is")
* Binds future action or interpretation

### A decision **is not**:

* An idea, suggestion, or preference
* A tentative consensus
* An unresolved discussion
* An action item without a committed choice

When in doubt, **do not promote** content to a Decision.

---

## 4. Decision Trace Quality Rubric (0-5)

> **Note**: This rubric is proposed by this guide as a project convention. It is not documenting existing practice — no prior thread establishes a numeric scoring scale for decisions.

Score each candidate decision trace using the rubric below.

### Score 5 — Strong / Canonical

* Explicit decision language
* Rationale stated locally
* Alternatives or tradeoffs mentioned
* Clear temporal scope
* Anchored provenance (entry_id + verbatim quotes)

### Score 4 — Strong but Incomplete

* Explicit decision
* Clear rationale
* Minor gaps (e.g. alternatives implied, not stated)

### Score 3 — Plausible / Needs Caution

* Decision intent implied but not explicit
* Rationale partially reconstructible
* Should be marked **low confidence**

### Score 2 — Weak / Candidate Only

* Ambiguous language
* Rationale inferred across context
* Do **not** emit as `Decision` without flagging

### Score 1 — Very Weak

* Reads like consensus drift or suggestion
* No clear commitment

### Score 0 — Not a Decision

* Notes, ideas, questions, background

Only scores **>=3** may be emitted as `Decision` entries.

---

## 5. Output Format: Watercooler Entry Schema

Extracted decision traces are written back as standard watercooler entries. Each entry must follow the watercooler protocol:

* **Type**: `Decision`
* **Role**: `scribe` (for extraction from existing threads) or the extracting agent's active role
* **Title**: Short, neutral summary of the decision
* **Body** (in order):

  1. `Spec: <spec>` — required first line per watercooler protocol
  2. `Confidence: N/5` — score from the rubric
  3. If confidence < 4: a **warning note** explaining the uncertainty
  4. **Decision statement** (normalized)
  5. **Rationale**
  6. **Constraints / scope**
  7. **Known alternatives** (if any)
  8. **Evidence** block (see Section 6)

### Example body

```markdown
Spec: scribe

Confidence: 5/5

## Decision

Never auto-merge threads into main without an explicit PR event.

## Rationale

Auto-merge in `_detect_behind_main_divergence()` at `git_sync.py:1710-1728`
polluted `origin/main` with feature-branch commits. The condition `code_synced`
(tree hash equality) does not distinguish a legitimate PR merge from a
coincidental content match.

## Alternatives Considered

- Return divergence info without merging, prompt user to run
  `watercooler merge-threads <branch>` explicitly
- Keep auto-merge but add a PR-event verification step

## Scope

Applies to watercooler-cloud branch parity protocol. Core Invariant #4 of the
unified protocol design. No expiration — permanent unless the invariant list
is revised.

## Evidence

Source entry: `01KC0FDPSVV2GBZN0FN68A61NB` (thread: unified-branch-parity-protocol)
Agent: Claude Code (caleb) | Role: planner | 2025-12-09T02:35:37Z

> 4. **No Auto-Merge to Main**: Never auto-merge threads into main without explicit PR event

> **Action**: Remove the auto-merge block entirely.
```

---

## 6. Provenance Rules (Strict)

* **Always cite the source `entry_id`** (ULID). This is the durable, portable reference — entry_ids are permanent and survive thread edits. Include the thread topic for navigability.
* Always quote the source text **verbatim**
* Never invent rationale or intent
* Never merge multiple distant passages unless explicitly connected by the author
* If a decision draws evidence from multiple entries, cite each source separately with its own entry_id, agent, timestamp, and quotes. Do not mix provenance across sources in a single Evidence block.
* Quote only the minimum text that establishes commitment and rationale. Verbatim does not mean exhaustive — anchor the claim without bloating the entry.
* If provenance is unclear, downgrade confidence
* Include the source entry's **agent**, **role**, and **timestamp** for attribution

Line/offset references are supplementary — they shift when entries are added to a thread. The entry_id is the primary anchor.

---

## 7. Temporal and Scope Handling

Agents must identify:

* Whether the decision is provisional or permanent
* Any versioning, sprint, or timebox language
* Conditions under which the decision may no longer apply

Use watercooler's own temporal signals:

* **Entry timestamps** establish when the decision was made
* **Thread status** (OPEN vs CLOSED) indicates finality — a decision in a CLOSED thread with a Closure entry has stronger standing than one in an active OPEN thread. Use `list_threads` (with `scan: true` to also get entry summaries) or `read_thread` (with `summary_only: true`) to check thread status and scan for Decision-type entries without loading full bodies.
* **Thread topic** provides implicit domain scope (e.g., a decision in `release-protocol-simplification` is scoped to release process)

If time scope is missing, explicitly note it. If the thread is still OPEN, note that the decision may be subject to further discussion.

---

## 8. Decision Trace Preflight Checklist

### Pre-scan: Identify candidates efficiently

Before running the full checklist, scan the thread to identify candidate entries:

1. **Use `list_threads(scan=true)`** to get all entry summaries across all threads in one call. Each entry summary includes its index, entry_id, type, and a 1-2 sentence summary.
2. **Filter for Decision-type entries** (already typed) and entries whose summaries contain decision language ("decided", "will use", "committed to", "selected").
3. **Only load full bodies** (via `get_thread_entry`) for entries that pass this summary-level filter. Summaries are sufficient to identify non-candidates — skip entries whose summaries describe research, options, or status updates.

This summary-first approach reduces token usage by ~90% compared to loading every entry body for analysis.

### Gate checklist

An agent must answer **YES** to all mandatory gates before emitting a Decision entry. The checklist is pass/fail validity; the rubric (Section 4) assigns confidence scores.

### Gate 1: Is there an explicit or defensible commitment?

Is there a clear point where a choice was made, not merely discussed? Can you point to specific language that indicates commitment?

**Acceptable signals**: "We decided...", "The plan is...", "We will...", an entry explicitly typed `Decision` by the author.

**Reject if**: Language is speculative ("seems like", "probably", "I think"), consensus is implied but not stated, silence or lack of objection is the only signal.

If NO: **stop. Do not emit a Decision.**

### Gate 2: Has the decision been superseded?

Search subsequent entries in the same thread for language that reverses, supersedes, or narrows this decision. Check Closure entries especially — if the Closure summary describes a different outcome, the candidate is not a committed decision.

**Reject if**: A later entry in the same thread explicitly contradicts, reverses, or replaces the candidate.

If superseded: **score 0. Do not emit.**

### Gate 3: Can you quote the decision verbatim?

Can you quote one or more sentences that directly support the decision?

**Requirements**: Verbatim quote(s), anchored by `entry_id`. If evidence comes from multiple entries, cite each source separately with its own entry_id, agent, timestamp, and quotes (see Section 6).

**Reject if**: You must paraphrase to make it sound like a decision, or the decision only exists as a summary of multiple people's opinions.

If NO: **downgrade to Note or candidate-only.**

### Gate 4: Is the rationale supported by the source?

Is the "why" stated, or are you inferring it?

**Acceptable**: Explicit reasons, constraints, tradeoffs, clear cause-effect statements. Rationale must be reconstructible from the same entry or immediately adjacent entries, not inferred across distant context.

**Reject if**: Rationale requires inference across unrelated entries, or you are filling in reasoning that the source does not state or clearly imply locally.

If NO: **max confidence score is 3, and a warning is mandatory.**

### Gate 5: Is scope bounded?

Do you know what this decision applies to?

**Scope signals**: Repo or subsystem, feature or milestone, thread topic.

**Reject if**: The decision could be misapplied globally, or scope exists only in your head.

If NO: **emit with explicit scope warning, or do not emit.**

### Gate 6: Is the decision temporally situated?

Do you know when this decision applies? Is it provisional, final, or timeboxed?

**Signals**: Entry timestamp, thread status, language like "for now", "for v1", "until..."

**Reject if**: Decision appears timeless but clearly isn't, or you cannot tell whether it is still in force.

If NO: **must explicitly mark temporal uncertainty.**

### Gate 7: Are you upgrading authority?

This is the most important check. Would the original author recognize this as their decision?

**Reject if**: You are "cleaning up" ambiguity, resolving disagreement, or acting as arbiter rather than recorder.

If YES (you are upgrading): **stop. Do not emit.**

### Gate 8: Does this survive deletion of context?

If the rest of the thread vanished, would this decision trace still be fair, accurate, and non-misleading?

**Reject if**: Missing assumptions would materially change interpretation, or the trace would mislead a future reader.

If NO: **do not emit.**

### Emission Rule

An agent may emit a Decision entry only if:

* Gates 1-3 pass cleanly (commitment exists, not superseded, quotable)
* Gates 4-6 are satisfied or explicitly caveated
* Gate 7 passes (no authority laundering)
* Gate 8 passes (self-contained truth)

Otherwise, emit a `Note` explaining why no decision was extracted, or emit nothing. This ensures the extraction effort is itself recorded in the thread history when relevant.

---

## 9. Agent Reminder

> **Your job is not to be helpful — it is to be correct.**

Future systems will treat extracted decisions as authoritative.
Only extract what the source actually supports.
