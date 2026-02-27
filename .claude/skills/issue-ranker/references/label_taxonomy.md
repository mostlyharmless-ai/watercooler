# Label Taxonomy — watercooler-cloud

This document defines the canonical label set for the watercooler-cloud GitHub repository. When running backlog refinement, use this as the reference for what to add, preserve, and avoid conflicting with.

---

## Priority Labels (managed by issue-ranker)

These labels indicate when an issue should be addressed. Always clean up conflicting priority labels when applying a new one.

| Label | Color | Meaning |
|-------|-------|---------|
| `priority:now` | #d93f0b (red-orange) | Critical path — must address this sprint |
| `priority:next` | #e4e669 (yellow) | Address next sprint |
| `priority:soon` | #fbca04 (gold) | This quarter — meaningful but not this sprint |
| `priority:backlog` | #c2e0c6 (light green) | Tracked but intentionally deferred |

**Conflict rule:** An issue may have only one `priority:*` label. When applying a new one, remove the other three.

---

## Severity Labels (managed by issue-ranker)

These labels indicate impact severity. Always clean up conflicting severity labels when applying a new one.

| Label | Color | Meaning |
|-------|-------|---------|
| `sev:critical` | #b60205 (dark red) | System broken / data loss / security risk |
| `sev:high` | #e11d48 (red) | Major feature broken, no workaround |
| `sev:medium` | #f97316 (orange) | Partial impact, workaround available |
| `sev:low` | #fde68a (light yellow) | Minor / cosmetic / edge case |

**Conflict rule:** An issue may have only one `sev:*` label. When applying a new one, remove the other three.

---

## Existing Domain Labels (preserve — do not remove)

These labels are applied by maintainers and describe the functional area or type of work. Always preserve these when adding priority/severity labels.

| Label | Meaning |
|-------|---------|
| `bug` | Something isn't working correctly |
| `enhancement` | New feature or improvement request |
| `documentation` | Docs improvements |
| `refactor` | Code restructuring without behavior change |
| `testing` | Test coverage improvements |
| `cleanup` | Code hygiene / dead code removal |
| `performance` | Performance optimization |
| `upstream` | Fix required in an upstream dependency |
| `good first issue` | Suitable for new contributors |
| `nit` | Minor nitpick, low urgency |

### Feature Area Labels

| Label | Meaning |
|-------|---------|
| `memory-tiers` | T1/T2/T3 memory distillation pipeline |
| `federation` | Cross-namespace federated search |
| `leanrag` | LeanRAG T3 memory pipeline |
| `daemon` | Daemon system (auditor, findings) |
| `graph-first` | Graph-first architecture concerns |

### Phase Labels

| Label | Meaning |
|-------|---------|
| `Phase 2` | Deferred to Phase 2 of the roadmap |

**Important:** Issues labeled `Phase 2` should default to `priority:backlog` and `importance=1` in scoring unless recent watercooler context indicates Phase 2 work has been activated.

---

## Scoring Signal Labels

These labels provide direct input to the scoring rubric:

| Label | Scoring signal |
|-------|---------------|
| `bug` | Severity ≥ 2; check body for data loss indicators |
| `upstream` | Risk +1 (external dependency, less control) |
| `daemon` | Risk +1 (background system, silent failures) |
| `memory-tiers` | Check watercooler for current T-tier priority |
| `federation` | Check watercooler for federation roadmap status |
| `nit` | Severity = 1 unless combined with `bug` |
| `Phase 2` | Importance = 1 (deferred by design) |
| `good first issue` | Importance ≤ 2 (not a priority item) |

---

## Label Application Rules

1. **Add, don't replace**: Use `gh issue edit --add-label` for domain/area labels; only remove conflicting `priority:*` or `sev:*` labels.
2. **Preserve history**: Never remove `bug`, `enhancement`, or feature-area labels.
3. **One priority, one severity**: Enforce mutual exclusivity within each namespace.
4. **`Phase 2` override**: Removing a `Phase 2` label requires explicit user confirmation — it signals a scope change.
