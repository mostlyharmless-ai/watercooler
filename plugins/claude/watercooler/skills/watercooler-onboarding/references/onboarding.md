# Watercooler — First-Run Repository Seeding Guide

Load this reference when Watercooler history is confirmed empty or when the user asks
to bootstrap/seed a repository.

Confirmed empty means:

- `watercooler_list_threads(code_path=".", scan=true, format="json")` returns no
  threads, and
- a broad `watercooler_search(..., mode="entries")` returns a successful empty result.

Do not load this reference merely because `list_threads` returned no threads for the
current user. Threads may be user-filtered, branch-scoped, or unavailable because an
index is missing. If history is unavailable, seed from local evidence only and state the
limitation in every entry that depends on it.

---

## What Watercooler is

Watercooler is a graph-first collaboration protocol for agentic coding projects. It
provides a shared thread space where human developers and AI agents record plans,
decisions, notes, and handoffs. The graph, not markdown projections, is the source of
truth. Threads are the primary coordination surface.

It is not a chat tool, ticket system, or generic documentation store. It is a durable,
attributable, searchable reasoning record.

---

## First-run behavior

When a repo has no Watercooler entries, do not stop with advice. This is when the skill
should create the most value: inspect local repository evidence and write seed entries
that future contributors (humans and agents in any role) can query.

The default first-run output is not a packet. It is the following seed thread set. Write `overview` first; the other entries cite it via `Related:`. The first three (`overview`, `product-charter`, `team-map`) supply the navigational, product, and human-accountability surfaces; the remaining six supply the engineering surfaces.

| Topic | Type | Role | Purpose |
|-------|------|------|---------|
| `overview` | `Note` | `pm` or `scribe` | Front-door TOC: plain-language framing + sibling index + reading order |
| `product-charter` | `Note` | `pm` | What this product is, who it's for, what bet it represents |
| `team-map` | `Note` | `pm` | CODEOWNERS-derived ownership, recent contributor activity, ownership drift |
| `architecture` | `Plan` | `planner` | Initial architecture map, boundaries, and known constraints |
| `working-map` | `Note` | `scribe` | Important directories, entrypoints, public surfaces, and local commands |
| `risk-register` | `Note` | `critic` | Drift risks, volatile files, unclear ownership, and weak assumptions |
| `test-surface` | `Plan` | `tester` | Test layout, CI gates, validation commands, and coverage gaps |
| `docs-contracts` | `Plan` | `scribe` | Docs, APIs, schemas, generated references, and update obligations |
| `entry-path` | `Plan` | `pm` | Recommended first tasks by role |

Body contracts for `overview`, `product-charter`, and `team-map` (mandatory body shape, including `team-map`'s dedicated three-item Drift checklist) are defined in `SKILL.md` under **Step 4 — Body contracts**.

Use `create_if_missing=true` on the write calls.

---

## Required body shape

The canonical entry template is defined in `SKILL.md` under **Step 4 — Required entry template (strict)**. Use that template; do not invent a parallel structure here.

In summary, every seed entry contains:

- `Spec: <spec>` as the first line
- `Purpose:` (one to two sentences)
- `Observed:` — facts with `path:line-range` citations
- `Inferred:` — claims with `confidence: high/medium/low` and basis
- `Drift findings:` — required for `risk-register`, `docs-contracts`, and `team-map` only (the first two run the five-item general checklist; `team-map` runs its own three-item ownership checklist)
- `Next query:` — a `watercooler_search(...)` call
- `Related:` — sibling topics this entry depends on (every non-`overview` entry MUST cite `overview` here; topic strings in body, ULIDs in `Provenance`)
- `Provenance:` — every cited file with line ranges, every command, every thread consulted, every Related sibling's `entry_id`

Keep entries compact. Prefer several focused seed entries over one large general note.

When Watercooler history is confirmed empty, the source of every claim is local repo evidence — note this explicitly in the Provenance section by including `Watercooler history status: confirmed empty`. When history is unavailable, say so per claim that depends on it; do not silently treat unavailable as empty.

---

## Dry-run behavior

Use dry-run only when:

- the user explicitly asks for `dry-run`, `preview`, `read-only`, or `orient`
- Watercooler health or write-path checks fail
- agent identity is unknown

Dry-run output should show the exact proposed seed writes:

```text
Would write:
- topic: architecture
  role: planner
  entry_type: Plan
  title: Initial repository architecture map
  body: ...
```

Do not call this a Getting Started Packet. The useful preview is the entry plan itself.

---

## Notes for the presenting agent

- Local repo evidence is source material, not filler.
- Never create a `Decision` entry unless explicit source material supports it.
- Provisional norms belong in `Plan` or `Note` entries with confidence labels.
- If history is unavailable, do not claim no history exists.
- If writes succeed, summarize topics written rather than pasting full entry bodies.
