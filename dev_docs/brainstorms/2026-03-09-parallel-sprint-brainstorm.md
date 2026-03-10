---
date: 2026-03-09
topic: parallel-sprint
---

# Parallel Sprint: parallel issue collection and agent orchestration

## What We're Building

A Claude Code skill (`/parallel-sprint`) that solves two linked problems:

1. **Discovery**: Given a GitHub issue tracker, find issue collections that are both *coherent*
   (thematically related, producing a meaningful unit of progress) and *separable* (low file
   overlap so multiple agents can execute safely). Present top-ranked collections with estimated
   effort, impact, and risk.
2. **Execution**: After the user selects a collection, spin up one Task subagent per issue (each
   in its own git worktree), work them in parallel, and let each produce a PR independently.

The human stays in the loop at the selection boundary - they choose which collection to run.
Agents then work without further coordination, reporting back via PRs.

## Problem Statement

Issue trackers often contain sets of related work that are good candidates for parallel execution,
but manually identifying safe-to-parallelize clusters is slow and inconsistent. Existing ranking
tools optimize for issue importance, not multi-agent execution safety. We need a single workflow
that optimizes for both throughput and merge safety.

## Why This Approach

**Two separate skills** - More composable but adds ceremony. Since the phases are tightly coupled
(execution needs discovery output), unifying them is cleaner.

**Sequential agents with handoffs** - Safer but defeats the purpose. Parallelism is the core
value proposition.

**Discovery-only (defer execution)** - The execution phase is what makes the skill compelling and
is not significantly more complex to design.

Unified skill wins: single entry point, natural human-in-the-loop pause between phases, and direct
reuse of existing `issue-ranker` infrastructure.

## Scope

In scope:

- Identify candidate issue collections from current repository issues
- Score and rank collections for impact, effort, and separability
- Ask user to choose one collection
- Execute selected collection in parallel via isolated worktrees
- Return per-issue outcomes (PR URL or failure reason)

Out of scope:

- Auto-merge, auto-deploy, or release orchestration
- Cross-repository issue execution
- Inter-agent negotiation or synchronization during execution
- Long-running orchestration after command completion

## Key Decisions

- **Name**: `parallel-sprint` - evokes both parallelism and a time-boxed burst of work
- **Issue eligibility**: Open, unassigned issues with no active PR. This defines a "ready to
  start" pool and avoids collisions with in-flight work.
- **Optional label filter**: `/parallel-sprint [label]` scopes the candidate pool to issues
  matching that label. No argument = all open unassigned issues.
- **Coherence signals** (combined):
  1. GitHub labels (immediate, cheap signal)
  2. LLM thematic clustering (resolves label ambiguity, surfaces latent groupings)
  3. Codebase area (shared module or directory implies synergistic impact)
- **Separability criterion**: No shared file edits for high-confidence scopes. File scope uses a
  tiered strategy: LLM inference from issue text first -> Serena semantic tools if the plugin is
  present (symbol discovery, reference finding) -> grep as fallback.
- **Separability confidence tiers**:
  1. High confidence: concrete file list with strong agreement between methods
  2. Medium confidence: partial file list with at least one concrete signal
  3. Low confidence: broad or uncertain area; allowed but scored lower
- **Effort estimation**: Check for existing `size:*` / `effort:*` / `complexity:*` labels first;
  fall back to LLM estimate (S / M / L / XL) based on issue title, body, and inferred file scope.
- **Collection size**: No hard limit; clustering should produce natural-sized groups.
- **Ranking model**: `score = (coherence * impact * parallelism_confidence) / risk_penalty`
  where each factor is normalized to `0..1` and `risk_penalty >= 1.0`.
- **Output format**: Top 3-5 ranked collections, each with:
  - Theme name and description
  - Member issues (number and title)
  - Estimated total effort (sum of individual S/M/L/XL estimates)
  - Project impact (High / Medium / Low with rationale)
  - Risk (file-scope confidence, merge conflict risk, external dependency risk)
  - Parallelism score (how confidently separable the issues are)
  - Deterministic selection key (`C1`, `C2`, `C3`, ...)
- **Agent context**: Each spawned agent receives full collection context (all issues being worked
  in parallel) plus its own issue details (title, body, inferred file scope). Collective context
  helps avoid accidental overlap.
- **Execution model**: `Task(subagent_type="general-purpose", isolation="worktree")` - each agent
  gets an isolated worktree, works independently, and opens a PR.
- **Failure handling**: Best-effort - report failed issue(s) with reason and surface successful
  PRs for the rest. User decides whether to retry failures.
- **Target repo**: Auto-detected from `git remote get-url origin`. No parameter needed.
- **Watercooler integration**: None - PRs are the record. No watercooler dependency.

## Two-Phase Flow

```text
/parallel-sprint [optional: label-filter]

Phase 1: Discovery
  |- Fetch open, unassigned issues with no active PR (gh issue list --json)
  |- Enrich each issue: labels, effort estimate, inferred file scope (LLM -> Serena -> grep)
  |- Cluster by: labels + LLM themes + codebase area
  |- Score each cluster: coherence x impact x parallelism / risk
  |- Verify separability: check file-scope overlap within each cluster
  `- Present top 3-5 ranked collections to user

[USER SELECTS COLLECTION]

Phase 2: Execution
  Preflight (gate — abort before spawning anything if any check fails):
    |- gh auth status
    |- base branch clean and available
    `- worktree creation permissions verified
  Spawn:
    |- For each issue: spawn Task(subagent_type="general-purpose", isolation="worktree")
    |    Agent receives: full collection context + this issue's title/body/file scope
    |    Agent task: implement, test, open PR
  Report:
    `- PR URL (or failure reason) for each issue after all agents complete
```

## Discovery Output Contract

Each ranked collection should include:

- `collection_id`: stable key (`C1`, `C2`, ...)
- `theme`: short name
- `summary`: 2-3 sentences explaining why these issues belong together
- `issues`: list of issue numbers and titles
- `effort`: per-issue and total effort estimate
- `impact`: High/Medium/Low with rationale
- `risk`: categorized risk notes and confidence level
- `parallelism_score`: numeric score plus short explanation
- `file_overlap_report`: explicit overlap-check result

## Execution Safeguards

- Run preflight checks before spawning agents (`gh auth status`, worktree writable, base branch
  available).
- Use per-issue branch naming convention to avoid collisions (for example:
  `parallel-sprint/<issue-number>-<slug>`).
- Fail fast on duplicate branch or worktree path conflicts before any agent work begins.
- Preserve partial success: one failing issue must not cancel successful issue PRs.
- Return a final summary table with issue number, status, PR URL (if any), and failure reason.

## Relationship to Existing Skills

- **issue-ranker**: `fetch_issues.py` and `analyze_relationships.py` are copied into
  `parallel-sprint/scripts/` as a starting point. Skills remain independent; changes do not
  automatically propagate. Different user-facing purpose: issue-ranker labels and ranks;
  parallel-sprint clusters and executes.
- **compound-engineering:workflows:work**: Each spawned agent task prompt references
  `/workflows:work` as the implementation workflow to follow.
- **compound-engineering:git-worktree**: Available for worktree management in spawned agents
  if needed.

## Suggested Implementation Phases _(hint for `/workflows:plan`)_

1. **Discovery MVP** — candidate fetch/filter, clustering, score computation, ranked output with collection IDs
2. **Execution MVP** — selection parsing, worktree-spawned agents, consolidated completion report
3. **Hardening** — preflight checks, separability confidence calibration, prompt/scoring refinement

## Success Criteria

- Command returns at least one ranked collection when eligible issues exist.
- Selected collection executes with one isolated agent per issue.
- For each issue, result is explicit: PR URL or actionable failure reason.
- User can choose a collection with a stable ID (`C1`, `C2`, etc.) without ambiguity.

## Open Questions

_(None - all questions resolved during brainstorm session.)_

## Next Steps

-> `/workflows:plan` for implementation details
