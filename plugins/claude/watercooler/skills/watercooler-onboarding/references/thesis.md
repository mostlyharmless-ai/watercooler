# Watercooler Bootstrap — Thesis Alignment

Load this reference when you need to calibrate how to interpret and write
Watercooler seed context during repository bootstrapping. These rules exist because
fast generation increases the burden of review and can launder ambiguity into false
authority.

---

## What this skill is and is not

**Is:**
- A repository seeding workflow
- A local-first inspection procedure for code, docs, CI, git history, and existing
  Watercooler context
- A way to create durable, queryable seed entries for future contributors (humans and agents in any role)
- A bounded synthesis that reduces cold-start confusion without inventing consensus

**Is not:**
- A replacement for reading source files
- An authority to declare project truth
- A substitute for code, tests, issue tracking, or direct inspection
- A reason to create `Decision` entries from weak inference
- A daemon or standing Watercooler role

---

## Anti-laundering rules (mandatory)

### Never present inference as settled fact

Use plain language:
- "appears to" / "seems to" / "likely" / "unclear from the current surface"

Wrong: "The team has decided to use Graphiti as the memory backend."
Right: "Thread summaries suggest Graphiti is the current memory backend, though this was not confirmed from a Decision entry."

### Do not manufacture consensus

If threads disagree, say they disagree.
If a decision looks provisional or contested, say so.
If ownership is unclear, say ownership is unclear.

### Do not upgrade notes into decisions casually

A decision must be recognizable as a committed choice with scope and rationale. If that standard is not met, refer to it as a discussion, leaning, or open question.

### Keep provenance visible

Every important claim in a seed entry should be traceable to:
- a `path:line-range` citation (or `path:section-name` when the relevant unit is a named section, not a line range)
- a Watercooler thread or entry
- a decision artifact
- an explicit uncertainty note

Generic citations like "package.json scripts" or "files, commands, threads" are insufficient. The citation format and footnote rules for multi-file claims are specified in `SKILL.md` under **Step 4 — Citation format**. A weak run that produces empty or generic Provenance sections is an indicator that the MUST-READ list in **Step 2.0** was skipped.

### Respect branch locality

Do not silently universalize branch-specific work as repo-wide truth. Mark branch-local items explicitly.

### Respect namespace boundaries

Remote/federated context is informative but not automatically authoritative for the local namespace.

---

## Retrieval escalation order

Use the least expensive surface that can answer the current need. Escalate only when simpler surfaces are insufficient.

1. **Local structure** — `ls`, README, config files. Fastest, zero MCP cost.
2. **Git history** — churn patterns, branch divergence, commit themes. Shows *what changed* and *where*, not *why*.
3. **Thread summaries** — `summary_only=true`. ~500 tokens per thread. Shows active coordination context.
4. **Pulse snapshot** — cached cross-thread activity themes. High signal, single call.
5. **Smart query** — cross-tier search for decisions and constraints. Use sparingly; can be slow.
6. **Entry bodies** — full text of specific entries. Only fetch what the summary flagged as relevant.
7. **Coordinator leads** — pre-built daemon findings. Use when available; skips re-derivation.
8. **Federated search** — cross-namespace, only when explicitly requested or local surface is clearly sparse for a mature repo.

---

## What to do with git history

Git history shows *chronology and change evidence*, not rationale. Use it to answer:
- Which parts of the repo are active now?
- Which surfaces are stable vs. volatile?
- Is documentation drifting from code?
- Was a directory recently introduced or actively refactored?

Do not infer rationale from commit history unless the rationale is explicitly present in the message.

---

## Failure modes to avoid

- Producing a summary blob with no clear entry path
- Treating commit history as a substitute for decision provenance
- Flattening disagreement into consensus
- Treating old thread content as current truth without caveat
- Inventing rationale missing from the source surface
- Overloading the user with every thread or every directory
- Ignoring code reality and relying only on conversation
- Confusing repo structure with collaborative state
