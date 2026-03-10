---
title: "Simplicity reviewer blind spot: LLM-read JSON files are implicit consumers"
category: process/code-review
tags:
  - code-review
  - compound-engineering
  - simplicity-reviewer
  - llm-orchestration
  - skills
  - dead-code
  - process
symptom:
  - "Simplicity reviewer flags functions as dead code with 'zero consumers in SKILL.md'"
  - "P1 todo filed to remove functional code that feeds a file the LLM reads"
  - "Review agent cannot find field-name references in prose for data that is LLM-consumed"
date_solved: "2026-03-10"
pr: "338"
concrete_example: "todos/136-complete-p3-synergy-labels-watercooler-specific.md"
---

# Simplicity reviewer blind spot: LLM-read JSON files are implicit consumers

## Symptom

During a code review, the `code-simplicity-reviewer` agent flags functions like
`analyze_opportunities()`, `analyze_cross_references()`, or `analyze_synergies()` as **P1 dead
code** on the grounds that "zero consumers reference these fields in SKILL.md." The agent cannot
find any prose in SKILL.md that names the specific JSON fields (`synergies`, `opportunities`,
`cross_references`) and concludes the functions are unused.

The finding is incorrect. The functions are live. The LLM is the consumer.

## Root Cause

The simplicity reviewer determines code liveness by searching for explicit references to function
names or output field names in prose (SKILL.md, README, other docs). This heuristic works for
traditional code: if nothing names a symbol, nothing uses it.

**It breaks for LLM-orchestration contexts.**

In a SKILL.md skill, a step like:

> "Read `.sprint/tmp/ps_relationships.json`. Use the relationship data to propose cluster names
> and score collections."

…grants the LLM access to the **entire contents** of the file — including every key the file
contains: `dependencies`, `synergies`, `opportunities`, `conflicts`, `cross_references`. The LLM
incorporates whatever context it finds useful. No field name needs to appear in SKILL.md prose for
the LLM to use it.

The reviewer's heuristic sees no explicit reference to `synergies` → concludes no consumer →
files P1 removal recommendation. But the LLM reads all keys in context.

## Concrete Example

**PR #338, todo #136** (initial filing — corrected after re-reading SKILL.md):

- Functions: `analyze_synergies()`, `analyze_opportunities()`, `analyze_cross_references()` in
  `analyze_relationships.py`
- Reviewer verdict: "Zero consumers in SKILL.md — dead code, remove all three (P1)"
- Actual situation: SKILL.md Step 4 instructs the orchestrating LLM to read
  `ps_relationships.json` in full before proposing cluster names and scoring collections. All
  output fields — including `synergies`, `opportunities`, and `cross_references` — are available
  to the LLM as implicit clustering context.
- Corrected verdict: Functions are live (P3 portability issue for `SYNERGY_LABELS` only)
- Impact of erroneous removal: LLM clustering would have had less context; degraded output quality
  with no error or warning.

## Detection Pattern

Before flagging a function as dead code in a skill or LLM-orchestration context:

### Step 1 — Identify all files the LLM reads

Look for SKILL.md steps that instruct the LLM to read JSON files:

```
Read `.sprint/tmp/ps_relationships.json`.
Read `.sprint/tmp/ps_candidates.json`.
Read `.sprint/tmp/ps_issues.json` for the full issue list.
```

### Step 2 — Check whether the function's output lands in any of those files

Trace the data flow: does the function under review write to (or contribute data to) a file the
LLM reads? Check script outputs, JSON schemas, and intermediate file paths.

```python
# analyze_relationships.py — output written to ps_relationships.json
output = {
    "dependencies": dependencies,   # LLM reads this
    "synergies": synergies,         # LLM reads this — even if SKILL.md doesn't name it
    "conflicts": conflicts,          # LLM reads this
    "opportunities": opportunities, # LLM reads this
    "cross_references": ...,        # LLM reads this
}
print(json.dumps(output, indent=2))  # → ps_relationships.json
```

### Step 3 — Apply the consumer rule

| Situation | Verdict |
|-----------|---------|
| Function output lands in a file the LLM reads in full | **Live — LLM is the consumer** |
| Function output lands in a file partially read (specific fields only named) | **Caution — verify named fields include this output** |
| Function output goes to a file no LLM, human, or code reads | **Dead — safe to remove** |

### Step 4 — Check SKILL.md read scope

Look at the exact instruction. "Read the file" with no field restriction = full context. "Read the
`dependencies` key from…" = partial context (only named fields consumed).

```
# Full context — ALL keys are consumed:
"Read ps_relationships.json. Use the relationship data to..."

# Partial context — only named keys consumed:
"From ps_relationships.json, extract the dependencies array."
```

## Counterpart: Truly Dead Code

Code is genuinely dead if its output goes to a file that **no consumer** (human, LLM, or
downstream code) ever reads. Signs:

- File is written but never referenced in SKILL.md or downstream scripts
- Output is overwritten immediately without being consumed first
- File appears only in cleanup/deletion steps, never in read steps
- A `--dry-run` trace shows the file is produced but never opened

## Prevention

### For review agents

When reviewing code in a skill or LLM-orchestration context, add an explicit check before filing
a dead-code finding:

> "Does this function's output feed a file that a SKILL.md step reads (either by name or with a
> broad 'read the full file' instruction)? If yes, the LLM is the consumer — not dead code."

### For skill authors

Make LLM-read file schemas explicit in SKILL.md. If a step reads a file, document which keys are
consumed:

```markdown
### Step 4 — LLM clustering
Read `ps_relationships.json` (all keys used: `dependencies`, `synergies`, `opportunities`,
`conflicts`, `cross_references`).
```

This makes the consumer relationship machine-discoverable and prevents future false dead-code
findings.

### For code reviewers (human)

When a simplicity agent flags dead code in a skill, before accepting the finding:

1. Find the JSON output file the function writes to
2. `grep -r "ps_relationships.json" .claude/skills/` — find all SKILL.md steps that reference it
3. Read those steps. If any say "Read [filename]" without field qualification → LLM is the consumer

## Related

- `dev_docs/solutions/process/automated-pr-review-multi-pass-inefficiency.md` — related review
  process patterns
- Todo #136 — the concrete corrected finding from PR #338
- PR #338 — parallel-sprint skill where this blind spot was first identified
