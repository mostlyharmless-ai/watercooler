# Issue Scoring Rubric — watercooler-cloud

## Formula

```
total_score = (severity × 3) + (risk × 2) + (importance × 2)
```

Maximum possible score: 36 (4×3 + 4×2 + 4×2)

| Score Range | Priority Tier | Label |
|-------------|---------------|-------|
| 25–36 | Critical path | `priority:now` |
| 15–24 | Next sprint | `priority:next` |
| 12–14 | This quarter | `priority:soon` |
| 0–11  | Deferred | `priority:backlog` |

---

## Dimension 1: Severity (×3 weight)

How bad is the impact if this issue is left unaddressed?

| Score | Label | Criteria |
|-------|-------|----------|
| 4 | `sev:critical` | System broken, crashes, data loss, data corruption, or security vulnerability. No workaround exists. |
| 3 | `sev:high` | Core feature broken or severely degraded. Workaround may exist but is painful or unreliable. |
| 2 | `sev:medium` | Feature partially broken. Workaround is available and reasonable. |
| 1 | `sev:low` | Minor issue, cosmetic, edge case, or technical debt with no user-visible impact. |

**Severity signals in issue body/labels:**
- Contains "data loss", "corruption", "crash", "exception" → severity ≥ 3
- Label `bug` with no workaround mentioned → severity ≥ 3
- Label `bug` with workaround mentioned → severity = 2
- Label `nit`, `cleanup`, `refactor` → severity = 1
- Label `documentation` → severity = 1
- Label `testing` → severity = 1–2 (higher if the gap causes false confidence)
- Label `performance` → severity = 2–3 (based on impact scale described)
- Contains "timeout", "deadlock", "race condition" → severity ≥ 3

---

## Dimension 2: Risk (×2 weight)

What is the probability or consequence of this issue causing downstream harm, regression, or external dependency failure?

| Score | Criteria |
|-------|----------|
| 4 | Data integrity at risk, sync correctness affected, or upstream dependency actively broken in a way that blocks us. No mitigation in place. |
| 3 | High chance of regression if not addressed. Affects background daemon, graph consistency, or multi-user sync. |
| 2 | Moderate regression risk in a non-critical path. Well-tested area with some coverage. |
| 1 | Low risk. Well-isolated code, no shared state, strong test coverage, or cosmetic change only. |

**Risk signals in issue body/labels:**
- Label `upstream` → risk +1 (external dependency, less control, unpredictable timeline)
- Label `daemon` → risk +1 (background system — failures are silent)
- Label `memory-tiers` with data mutation involved → risk = 3–4
- Mentions "race condition", "deadlock", "lock", "stale" → risk ≥ 3
- Mentions "worktree", "git push", "commit" in context of data writes → risk ≥ 3
- Label `leanrag` with pipeline integrity concern → risk = 2–3
- Label `federation` → risk = 2 (bounded namespace, not core path)
- Label `testing`, `documentation`, `cleanup` → risk = 1

---

## Dimension 3: Importance (×2 weight)

How aligned is this issue with what the team currently cares about? This dimension is calibrated from watercooler context gathered in Step 1.

| Score | Criteria |
|-------|----------|
| 4 | Directly blocks or degrades an **active work stream** named in recent watercooler discussions. The team is actively working in this area right now. |
| 3 | Related to a **current sprint priority** or a core **design principle** the team has explicitly committed to (graph-first reads, zero-config, minimal stdlib-only core). |
| 2 | Future roadmap item or Phase 2 area **that is not currently active**. Tracked, not forgotten, but not in scope yet. |
| 1 | Explicitly deferred, historical cleanup, or out of scope per team decisions. Includes all issues labeled `Phase 2` unless watercooler context says otherwise. |

**Importance signals:**
- Issue area matches an active work stream from watercooler → importance = 4
- Issue is a follow-up from a recently merged PR (referenced in watercooler) → importance = 3–4
- Issue labeled `Phase 2` → importance = 1 (default; override only with watercooler evidence)
- Issue is in an area with recent `Decision`-type watercooler entries → importance = 3–4
- Issue is `good first issue` with no team urgency signal → importance ≤ 2
- Issue has many comments (≥5) → signal of community interest; nudge importance +0.5 (round up)
- Issue references an upstream dependency the team is waiting on → importance = 2–3

**Ambiguity handling:**
When importance is unclear (no watercooler signal for the area), default to importance=2 and mark the issue with `*` in the report to flag for human review.

---

## Worked Examples

### Example 1: Data loss bug in graph sync
```
labels: [bug, memory-tiers]
body: "entries written during concurrent graph_recover are silently dropped"
```
- Severity: 4 (data loss, no workaround)
- Risk: 4 (daemon path + graph data integrity)
- Importance: 4 (memory-tiers = active work stream per T2 PR)
- **Score: 4×3 + 4×2 + 4×2 = 12+8+8 = 28 → priority:now + sev:critical**

### Example 2: Documentation gap
```
labels: [documentation]
body: "docs/mcp-server.md doesn't document watercooler_search parameters"
```
- Severity: 1 (no operational impact)
- Risk: 1 (read-only, no regressions)
- Importance: 2 (useful but not blocking anything active)
- **Score: 1×3 + 1×2 + 2×2 = 3+2+4 = 9 → priority:backlog + sev:low**

### Example 3: Federation dedup edge case
```
labels: [bug, federation, Phase 2]
body: "secondary namespace wins over primary in dedup when both have same score"
```
- Severity: 2 (partial impact, primary still works)
- Risk: 2 (bounded to federation path)
- Importance: 1 (Phase 2 label → deferred by design)
- **Score: 2×3 + 2×2 + 1×2 = 6+4+2 = 12 → priority:soon + sev:medium**
  *(Phase 2 sets importance=1 which is the "deferred" setting, but the issue still scores in the `priority:soon` tier — accurately reflecting "tracked, not this sprint" rather than forgotten.)*

### Example 4: Upstream socket disconnect
```
labels: [bug, upstream]
body: "MCP stdio connection drops silently when graphiti socket disconnects"
```
- Severity: 3 (major path broken, workaround is restart)
- Risk: 3 (upstream + silent failure pattern)
- Importance: 3 (affects MCP server reliability — core team concern)
- **Score: 3×3 + 3×2 + 3×2 = 9+6+6 = 21 → priority:next + sev:high**

---

## Relationship Modifiers (applied in Step 3.5)

After initial scores are computed, apply adjustments based on relational structure. These modifiers are computed by `analyze_relationships.py` — do not estimate them manually.

### Dependency Bonus

| Condition | Score Adjustment |
|-----------|-----------------|
| Issue blocks ≥1 `priority:now` issue | +4 |
| Issue blocks ≥1 `priority:next` or `priority:soon` issue | +2 |
| Both conditions above | +6 (cap — no further stacking) |

A prerequisite issue that would otherwise sit in `priority:backlog` may be promoted to `priority:soon` or `priority:next` by the dependency bonus. Add "promoted by dependency" to the rationale and note what it unblocks.

### Synergy Notation

No score change. Issues in the same synergy cluster (detected by shared area label) are marked `~` in the report and listed in the Relationship Map with a batch recommendation. The benefit is reduced context-switching cost for the developer, not a higher priority score.

### Conflict Flag

No score change. Two issues that both reference the same issue with fix/close intent are flagged `⚠` and listed in the Relationship Map for human review. Never auto-resolve conflicts — schedule them only after explicit confirmation that they are not duplicates.

### Opportunity Window

No score change. An issue is an "opportunity pick" if:
- It shares an area label with one or more `priority:now` issues (`synergy_window`)
- It unblocks ≥2 other issues in `priority:now`/`priority:next` (`blocker_removal`)
- It is low-severity in the same cluster as active sprint work (`cheap_win`)

Opportunity picks are surfaced in the Relationship Map as tactical recommendations. They represent strategic efficiency, not elevated urgency.

---

## Quick Reference Card

```
Total = (S × 3) + (R × 2) + (I × 2)   [max 36, +6 from dependency bonus]

Severity:   4=data loss/crash  3=major broken  2=partial  1=cosmetic
Risk:       4=data integrity   3=regression    2=moderate 1=isolated
Importance: 4=blocks active    3=sprint goal   2=roadmap  1=deferred

Score 25-36 → priority:now
Score 15-24 → priority:next
Score 12-14 → priority:soon
Score  0-11 → priority:backlog

Severity score → sev label: 4=critical 3=high 2=medium 1=low

Relationship modifiers (Step 3.5):
  Dependency bonus: +4 if blocks priority:now / +2 if blocks priority:next|soon (cap +6)
  Synergy: ~ notation + batch recommendation (no score change)
  Conflict: ⚠ flag for human review (no score change)
  Opportunity: ✨ tactical pick annotation (no score change)
```
