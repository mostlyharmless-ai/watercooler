# Editorial publishing workflow

Turn implementation threads into publishable material without skipping the editorial step.

This guide describes an agent-neutral workflow for:

- detecting notable source material in Watercooler threads
- capturing evidence from Watercooler, git, and code state
- drafting terse content stubs first
- iterating until approval
- packaging final copy for X, LinkedIn, Reddit, or blog posts

The canonical design lives in
`watercooler-planning/EDITORIAL_PUBLISHING_WORKFLOW.md`.
This document is the practical operator guide.

---

## What this is for

Use this workflow when:

- a thread contains novel functionality worth explaining
- a benchmark or experiment produced a transferable lesson
- a design decision would make a good post or blog section
- a process pattern between humans and agents deserves to be written up

Do not use it for:

- routine status updates
- unsupported feature claims
- publication without explicit approval

---

## Core idea

The workflow separates **detection** from **drafting**.

- A daemon or human notices promising source material.
- Watercooler keeps the evidence and revisions durable.
- The first output is a stub, not polished copy.
- Approval happens before anything is treated as publish-ready.

---

## Source priority

When building external copy, gather evidence in this order:

1. Watercooler source thread and entry history
2. daemon findings and annotations
3. git context: changed files, branch, PRs, commits
4. current code state
5. human recollection only to fill gaps

That keeps the public claim aligned with what the project actually recorded.

---

## Daemon-assisted discovery

The `content_scout` daemon behaves like the other Watercooler daemons:

- incremental thread scanning
- findings-first output
- no auto-publishing
- optional annotation of strong candidates

### Finding categories

The daemon emits findings in these categories:

- `content_opportunity_blog`
- `content_opportunity_social`
- `novel_feature_candidate`
- `strong_process_story`

Each finding includes:

- `topic`
- optional `entry_id`
- a short rationale
- likely media
- evidence refs
- confidence

### Flag semantics

Use a flag for post-worthiness.

Canonical flag reason:

`editorial_candidate`

Use pinning only for the strongest supporting entry.

Flags are neutral attention markers in search scoring — flagged entries are not
penalized. Using `editorial_candidate` as a flag reason works naturally with
search and retrieval.

---

## Thread model

Keep source material and editorial work in separate threads.

### Source thread

The implementation, benchmark, or design thread where the material originated.

Possible additions:

- `editorial_candidate` flag
- pin on the best evidence entry
- xref to the editorial thread

### Editorial thread

Create a new thread for the writing process.

Recommended topic:

`editorial-<source-topic>`

Examples:

- `editorial-landing-page-design`
- `editorial-value-aligned-benchmarks`
- `editorial-content-scout-daemon`

---

## Workflow

### 1. Detect

Either:

- review `watercooler_daemon_findings` output, or
- manually nominate a thread

### 2. Capture

Summarize:

- what happened
- why it matters
- which entries and files are evidence
- what claims are safe

### 3. Stub

Create the shortest useful draft for the intended medium:

- X comment seed
- X post seed
- X thread outline
- LinkedIn seed
- Reddit seed
- blog outline

### 4. Revise

Run explicit revision passes:

- factual tightening
- channel fit
- audience fit
- tone and language

### 5. Approve

Mark the editorial state clearly:

- approved
- revise
- shelved

### 6. Package

Produce the final copy in the target medium’s format.

Record the approved version in Watercooler before publishing externally.

---

## Entry expectations

Use standard Watercooler entry types inside the editorial thread.

### `Note`

Use for:

- capture summary
- daemon finding summary
- stub drafts
- rewrite drafts

### `Plan`

Use for:

- channel decision
- audience decision
- angle choice

### `Decision`

Use for:

- final approved copy
- final publication choice

### `Closure`

Use for:

- published
- shelved
- superseded

Remember to include `Spec: <spec>` as the first line of entry bodies, following
normal Watercooler protocol.

---

## Platform guidance

### X comment

- add value first
- keep it compact
- mention the product only if it helps the discussion

### X post

- lead with one clear insight
- keep proof points tight
- avoid sounding promotional

### X thread

- use only when the argument needs steps
- keep each post self-contained

### LinkedIn

- allow more context than X
- keep it specific and informed

### Reddit

- adapt to the community
- lead with substance, not promotion

### Blog

- use when the idea needs durable exposition
- include evidence, examples, and interpretation

---

## Minimal artifact schema

Each editorial thread should preserve these fields in practice, even if not in a
formal JSON block:

- source topic
- source entry IDs
- candidate type
- target audience
- target platform
- angle
- claim status
- evidence list
- stub
- current draft
- approval state

---

## Quick operator checklist

- [ ] Confirm the source thread is genuinely worth externalizing
- [ ] Gather source entry IDs before drafting
- [ ] Separate shipped claims from in-progress claims
- [ ] Start with a stub, not polished prose
- [ ] Keep revisions in the editorial thread
- [ ] Record approval explicitly
- [ ] Publish only after approval

---

## Engagement logging

After publishing, record engagement data in the editorial thread so future
content decisions can learn from what worked.

### Publish receipt

Post a `Note` or `Closure` entry with:

```markdown
Spec: docs

## Publish Receipt

- **Platform**: x_post | linkedin | reddit | blog
- **URL**: https://...
- **Published**: 2026-03-19T14:00:00Z
```

### Engagement update

Periodically post a follow-up `Note` with engagement numbers:

```markdown
Spec: docs

## Engagement

- **Impressions**: 1200
- **Likes**: 45
- **Reposts**: 12
- **Replies**: 8
- **Clicks**: 90
- **Measured**: 2026-03-22T10:00:00Z
```

Not all fields are required — include what the platform provides. The format is
designed so a future daemon could write these Notes automatically from platform
APIs.

### How engagement data is used

- The editorial skill checks past engagement Notes when shaping new content angle
  and channel decisions.
- The content scout daemon can (in the future) weight candidate detection based on
  engagement patterns: content types that produced high engagement get boosted,
  shelved or low-engagement patterns get dampened.
- No platform API integrations are required — all manual entry for now.

---

## Related docs

- [TOOLS-REFERENCE.md](./TOOLS-REFERENCE.md)
- [WORKFLOW_EXAMPLES.md](./WORKFLOW_EXAMPLES.md)
- [MCP-CLIENTS.md](./MCP-CLIENTS.md)
