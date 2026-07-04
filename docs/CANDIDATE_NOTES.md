# Candidate Notes and Promotion

Candidate Notes are the authority ladder's "draft Decision" surface. When an
automated decision extractor finds a likely commitment in a thread but cannot
confidently emit a Decision — either because the LLM's evidence is shaky, or
because soft gates failed, or because the confidence rubric scored a 3 — the
daemon writes a thread-visible Note marked
`Candidate-Status: needs_human_confirmation` instead of a private rejection.

A human (or human-authorized agent) reviews the candidate and either:

- **Promotes** it: a supported durable entry is appended to the same thread,
  carrying forward target-specific provenance from the candidate. A
  `CandidateDisposition` Note is appended to mark the candidate as `promoted`.
  The candidate Note itself is never edited.
- **Rejects** it: a `CandidateDisposition` Note is appended marking the
  candidate as `rejected`. (CLI / MCP for rejection ships in a follow-up PR;
  for Phase 1 rejection is recorded by writing the Note by hand.)

This document covers the promotion path. The candidate-emission path is owned
by `ExtractDecisionsDaemon` (see `src/watercooler_mcp/daemons/decision_extractor.py`).

## Candidate Note body shape

`format_candidate_note_body()` in `src/watercooler/decision_extraction.py`
emits Notes with this structure (Phase 1b, v0.10 §10.5 Note conventions):

```
Spec: decision-extractor
[automated: decision_extractor]
Candidate-Type: Decision
Candidate-Status: needs_human_confirmation
Surface-Kind: decision
Promotable: true
Authority: none
Confidence: 4/5
Failed-Gates: g6_temporal
Quote-Evidence-Status: verified | weak_unverified
Source-Entry: <source_entry_ulid>

## Candidate Decision
<extracted decision statement>

## Why this is a candidate, not a Decision
<gate-failure reason or low-confidence explanation>

## Evidence            ← or "## Evidence (unverified)" for weak-quote candidates
> <verbatim quote 1>
> <verbatim quote 2>

## Source
Source entry: #<index> `<ulid>` — "<title>" (thread: <topic>)
Agent: <agent> | Role: <role> | <timestamp>
```

`Quote-Evidence-Status: weak_unverified` signals that the LLM produced
confident quotes that did not validate against the source body. Treat the
quotes as "suggestions for where to look," not as direct evidence — verify
against the source before promoting.

## Promoting a candidate

### MCP tool

```python
mcp__watercooler-cloud__watercooler_promote_candidate(
    candidate_entry_id="01HZA8...",
    topic="feature-storage",
    target_type="Decision",
    human_authorized_by="caleb",
    code_path="/path/to/repo",
    agent_func="Claude Code:claude-opus-4-7:implementer",
    edits=None,                   # optional dict — see below
)
```

Supported targets are `target_type="Decision"` and `target_type="Learning"`.
Closure / Supersession / Plan / StatusChange still need target-specific
validators; weak validators shipped together would produce low-quality lifecycle
entries — exactly the failure mode the ladder exists to prevent.

`human_authorized_by` is required. Decision promotion writes a `Decision` entry,
which is a Level 3 act under the authority ladder; the authorizing identifier is
stamped on the new entry's `Human-Authorized-By` marker. Learning promotion
writes a durable `## Lesson` Note plus the same append-only disposition record,
without stamping `decision_origin`.

Optional `edits` dict can override or extend the carried-forward content:

```python
edits = {
    "decision_statement": "Use PostgreSQL 16 for session storage",
    "rationale": "Vector ops + JSON support justify the upgrade",
    "scope": "watercooler-site/api",
}
```

### CLI

```sh
watercooler promote-candidate <candidate_entry_id> \
    --topic feature-storage \
    --target-type Decision \
    --human-authorized-by caleb \
    [--edit-decision-statement "Use PostgreSQL 16 for session storage"] \
    [--edit-rationale "Vector ops + JSON support"] \
    [--edit-scope "watercooler-site/api"]
```

The CLI uses the same `watercooler.promotion` library as the MCP tool and
writes via the local `commands_graph.say` path.

## What promotion produces

Two append-only writes on the candidate's thread:

### 1. The promoted entry

For `target_type="Decision"`, promotion writes a Decision:

```
Spec: decision-extractor-promoted
Promoted-From: <candidate_entry_id>
Source-Entry: <source_entry_ulid>           ← carried forward
Authority-Source: human
Authority-Basis: human_promoted
Human-Authorized-By: <authorizer>
Confidence: 4/5 (from candidate)
Failed-Gates-At-Extraction: g6_temporal
Quote-Evidence-Status-At-Extraction: verified | weak_unverified

## Decision
<decision statement — from candidate or --edit-decision-statement>

## Rationale          ← only if --edit-rationale supplied
<rationale>

## Scope              ← only if --edit-scope supplied
<scope>

## Original candidate caveat (carried forward)
<the candidate's "Why this is a candidate" text>

## Evidence (carried forward)        ← or "(carried forward, unverified at extraction time)"
> <quote 1>
> <quote 2>

## Promotion provenance
<one-paragraph audit trail with thread + authorizer>
```

For `Quote-Evidence-Status-At-Extraction: weak_unverified`, the Evidence
section header reads "carried forward, unverified at extraction time" and
includes an italicized note that the promoting human has reviewed the
unverified quotes.

For `target_type="Learning"`, promotion writes a Note with a bare `## Lesson`
heading, the promoted lesson text, and promotion provenance. It intentionally
does not write a Decision or mutate AGENTS/CLAUDE conventions directly.

### 2. The CandidateDisposition Note

```
Spec: candidate-disposition
CandidateDisposition: promoted
Disposition-Target: <candidate_entry_id>
Promoted-To: <promoted_entry_ulid>
Disposition-Authorized-By: <authorizer>

## Disposition
Candidate `<candidate>` on thread `<topic>` has been **promoted** to
`<target_type>` `<promoted_entry>` by `<authorizer>`.

## Why this Note exists
<append-only discipline explanation>
```

Queries that need a candidate's current disposition should look for the
latest `CandidateDisposition` Note whose `Disposition-Target` matches the
candidate's entry ID. The candidate Note's body never changes.

## Failure modes

`watercooler_promote_candidate` returns an `❌ ...` error string (and the CLI
exits non-zero) when:

- `candidate_entry_id` or `topic` is empty.
- The candidate cannot be found on the thread.
- The candidate has an empty body.
- `target_type` is not `"Decision"` or `"Learning"`.
- `human_authorized_by` is missing or whitespace-only.
- The candidate's `Candidate-Status` is not `needs_human_confirmation`
  (e.g. it has already been promoted or rejected).
- The candidate has no section matching the requested target (`## Candidate
  Decision` for Decisions, `## Candidate learning` for Learnings).

When the promoted-entry write succeeds but the subsequent Disposition write
fails, the response carries the promoted Entry-ID and an explicit
`CandidateDisposition Entry-ID: (write failed — verify)` line so the operator
can manually post the disposition Note via `watercooler_write` or
`watercooler-mcp` to keep the audit trail intact.

## Why append-only

Watercooler threads are append-only on the `watercooler/threads` orphan
branch. A candidate's status change is recorded as a separate
`CandidateDisposition` Note rather than by editing the candidate body. This
preserves the full audit trail — the candidate exists in its original form
forever, and the disposition is itself a thread entry with its own commit
footer.

## References

- v0.10 proposal, §Phase 1b "Minimal Phase 1 promotion helper" —
  `dev_docs/proposals/watercooler-agent-authority-ladder-proposal-updated.md`
- §10.5 Phase 1 entry-type discipline (Note conventions) — same proposal.
- §5.4.1 evidence-light Decision lint — same proposal. Promoted Decisions carry
  `Authority-Basis: human_promoted` to satisfy the lint's `authority_basis` enum
  requirement.
- Master plan Phase 1b spec —
  `dev_docs/plans/2026-05-18-feat-agent-authority-ladder-master-plan.md`
