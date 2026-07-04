# Authority Ladder — status

**The server-side authority-ladder enforcement layer is not shipped.** The framing
remains accepted team design: *agents may infer relevance, but they may not infer
authority* (Level 1 retrieval/candidate-preparation is automatic; Level 2 candidate
elicitation; Level 3 `Decision`/`Closure`/supersession/status mutation requires explicit
human direction).

What is **live** today is the *behavioral* layer only:

- The `ExtractDecisionsDaemon` candidate-Note fallback (a daemon prepares a non-authoritative
  candidate `Note`; a human confers authority via `watercooler promote-candidate`).
- Inert provenance fields (`actor_class`, `decision_origin`, `authority_basis`,
  `source_entry_id`, `human_authorized_by`, `confidence`, `gate_results`) stamped on
  daemon-emitted, promoted, and human-authorized entries — descriptive metadata, not enforced.
- The `planner` / `critic` / `pm` bounded-output and disagreement-preservation conventions.

### `human_authorized_by` — accountable human ownership (#879)

`human_authorized_by` records the **accountable human or institution** behind a
human-authorized or human-promoted `Decision`/`Closure`, as queryable graph metadata
rather than only body prose. It is written by:

- `watercooler_promote_candidate` — sets `decision_origin=human_promoted`,
  `authority_basis=human_promoted`, `source_entry_id=<candidate>`, and `human_authorized_by`.
- `watercooler_write(authority_mode="decision"|"closure", human_authorized_by=...)` —
  sets `decision_origin=agent_authored`, `authority_basis=human_endorsed` (when an owner is
  given, else `none`), and `human_authorized_by`.

Key rules:

- **Actor vs. authority are distinct.** `actor_class` records the *writer*; an agent
  executing a write under human authorization stays `actor_class=agent` with the accountable
  human in `human_authorized_by`. The field is never inferred from `authorization_text` prose.
- **Identifier form:** namespace-qualified — `github:<login>` or `wc:user:<handle>`; never a
  bare email. A single principal today; multi-principal co-authorization is a future extension.
- **Privacy / permanence:** the value lands in an append-only, git-committed,
  federation-visible record and **cannot be redacted later**. It is scrubbed at the write
  boundary (CR/LF collapsed, angle-bracket markup stripped, length bounded to 256). Prefer a
  stable handle or opaque account id over raw PII.
- **Migration:** additive and optional — legacy entries simply omit it; no backfill needed.
- **Scope:** local baseline-graph writes only. Hosted (GitHub-API) writes do not persist this
  graph metadata (the body authorization marker is still written for human review).

What is **not** shipped: the server-side write gate, the policy resolver, the source-entry
chain validator, the standing-policy registry, and the `activate-policy` / `retire-policy`
CLI. An implementation of these (build-mode Phases 4a–4h) landed ahead of its trigger,
against standing Decision `01KSV7Z3P9NJJ2RYYQYAVEX26X` ("Phase 1a stays deferred — no
implementation work initiated"), and was reverted; see
[`dev_docs/plans/2026-06-01-refactor-cut-authority-ladder-phase-1a-build.md`](../dev_docs/plans/2026-06-01-refactor-cut-authority-ladder-phase-1a-build.md).

The full enforcement design is preserved as a triggered contingency in **Appendix C** of
[`dev_docs/plans/2026-05-18-feat-agent-authority-ladder-master-plan.md`](../dev_docs/plans/2026-05-18-feat-agent-authority-ladder-master-plan.md).
Lifting it requires an explicit **Phase 1a lift Decision authored on the technical thread**
(`agent-authority-ladder-proposal-2026-05-13`). Until then there is no operator gate surface.
