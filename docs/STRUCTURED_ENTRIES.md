# Structured Entries Guide

Watercooler-cloud uses structured entries to provide rich metadata and context for collaboration between human developers and AI agents.

## Table of Contents

- [Overview](#overview)
- [Entry Format](#entry-format)
- [Agent Roles](#agent-roles)
  - [planner](#planner)
  - [critic](#critic)
  - [implementer](#implementer)
  - [tester](#tester)
  - [pm](#pm)
  - [scribe](#scribe)
- [Entry Types](#entry-types)
  - [Note](#note)
  - [Plan](#plan)
  - [Decision](#decision)
  - [PR](#pr)
  - [Closure](#closure)
- [CLI Commands with Structured Entries](#cli-commands-with-structured-entries)
- [Agent Format](#agent-format)
- [Ball Auto-Flip Behavior](#ball-auto-flip-behavior)
- [Best Practices](#best-practices)
- [Examples](#examples)
- [See Also](#see-also)

## Overview

Each entry in a watercooler thread includes:
- **Agent**: Who created the entry (with user tag)
- **Role**: The agent's role in this context
- **Type**: The kind of entry being made
- **Title**: Brief summary of the entry
- **Timestamp**: UTC timestamp
- **Body**: The actual content

## Identity Pre-Flight

Before issuing write operations (`say`, `ack`, `handoff`, `set_status`), agents must declare identity:

- Call `watercooler_set_agent(base="Claude Code", spec="implementer-code")` once per session **or** supply `agent_func="Claude Code:sonnet-4:implementer"` on each write (format: `<platform>:<model>:<role>`).
- Align `spec` with the role recorded in the entry (`planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`).
- Include a visible `Spec: <value>` line in the entry body. This keeps the Watercooler audit trail self-describing when rendered outside MCP tools.
- Enable strict enforcement by setting `WATERCOOLER_REQUIRE_IDENTITY=1`. When active, writes using the default `Agent` identity are rejected with guidance.

Identity impacts counterpart lookups, commit footers, and structured entry headers. Treat it as mandatory hygiene for multi-agent coordination.

## Entry Format

Entries are formatted as:

```markdown
---
Entry: Agent (user) 2025-10-06T12:34:56Z
Role: critic
Type: Decision
Title: Security Review Complete

Detailed entry content goes here.
Can span multiple lines and include markdown formatting.
```

## Agent Roles

Watercooler-cloud supports six distinct agent roles:

### `planner`
**Purpose**: Architecture and design decisions

Use when:
- Proposing system architecture
- Designing new features
- Planning implementation approach
- Creating technical roadmaps

**Example**:
```bash
watercooler say feature-auth \
  --agent Claude \
  --role planner \
  --title "Auth System Design" \
  --body "Propose JWT-based auth with refresh tokens"
```

### `critic`
**Purpose**: Code review and quality assessment

Use when:
- Reviewing code changes
- Identifying potential issues
- Suggesting improvements
- Assessing security or performance

**Example**:
```bash
watercooler say feature-auth \
  --agent Claude \
  --role critic \
  --title "Security Review" \
  --body "Token expiry should be configurable per environment"
```

### `implementer`
**Purpose**: Feature implementation and coding

Use when:
- Writing code
- Implementing features
- Making code changes
- Fixing bugs

**Example**:
```bash
watercooler say feature-auth \
  --agent Codex \
  --role implementer \
  --title "JWT Implementation Complete" \
  --body "Added token generation and validation middleware"
```

### `tester`
**Purpose**: Test coverage and validation

Use when:
- Writing tests
- Running test suites
- Validating behavior
- Reporting test results

**Example**:
```bash
watercooler say feature-auth \
  --agent Claude \
  --role tester \
  --title "Test Coverage Report" \
  --body "Added 15 unit tests, coverage at 92%"
```

### `pm`
**Purpose**: Project management and coordination

Use when:
- Managing project timeline
- Coordinating between agents
- Tracking progress
- Making handoffs
- Setting priorities

**Example**:
```bash
watercooler handoff feature-auth \
  --agent Team \
  --role pm \
  --note "Ready for implementation phase"
```

### `scribe`
**Purpose**: Documentation and notes

Use when:
- Writing documentation
- Taking meeting notes
- Recording decisions
- Creating summaries

**Example**:
```bash
watercooler say feature-auth \
  --agent Team \
  --role scribe \
  --title "Decision Record" \
  --body "Agreed to use JWT with 15-minute expiry"
```

## Entry Types

Five entry types provide semantic meaning:

### `Note`
**Default type** for general observations and updates.

Use for:
- Status updates
- General observations
- Quick notes
- Acknowledgments

```bash
watercooler say topic --title "Progress Update" --type Note --body "50% complete"
```

### `Plan`
Design proposals and roadmaps.

Use for:
- Feature proposals
- Implementation plans
- Architecture designs
- Technical roadmaps

```bash
watercooler append-entry topic \
  --agent Claude \
  --role planner \
  --title "Migration Plan" \
  --type Plan \
  --body "Three-phase migration: 1) Prep, 2) Migrate, 3) Validate"
```

### `Decision`
Architectural or technical decisions.

Use for:
- Technical choices
- Architecture decisions
- Tool selections
- Approach agreements

```bash
watercooler append-entry topic \
  --agent Team \
  --role pm \
  --title "Database Selection" \
  --type Decision \
  --body "Going with PostgreSQL for ACID compliance"
```

### `PR`
Pull request related entries.

Use for:
- PR announcements
- Code review requests
- Merge notifications
- PR status updates

```bash
watercooler append-entry topic \
  --agent Codex \
  --role implementer \
  --title "PR #42 Ready" \
  --type PR \
  --body "https://github.com/org/repo/pull/42 - Auth implementation"
```

### `Closure`
Thread conclusion and summary.

Use for:
- Closing completed work
- Project summaries
- Final status reports
- Archive notes

```bash
watercooler append-entry topic \
  --agent Team \
  --role scribe \
  --title "Feature Complete" \
  --type Closure \
  --body "Auth system deployed to production. All tests passing."
```

#### Closure Entry Body Convention

When closing a thread, use this structured template to maximize retrieval value and compounding:

```
## Decisions Made
- [Decision 1]: [rationale] (ref: entry [N] or external link)
- [Decision 2]: [rationale]

## Open Questions
- [Question]: [current understanding] → [suggested next thread or owner]

## Lessons Learned
- **[lesson-slug]**
  - Problem pattern: [what went wrong or what was discovered]
  - Insight: [the transferable knowledge]
  - Applicability: [repo-wide | directory-scoped | pattern-specific]
  - Evidence: [entry IDs, commit SHAs, or PR numbers]

## Policy Proposals
- [Proposed change]: [scope] → [target file, e.g. CLAUDE.md section]
  - Rationale: [why]
  - Promotion gate: [e.g., "ship after 2 successful uses"]

## Retrieval Smoke Test
- Query: "[natural language question this thread should answer]"
- Expected: [which lessons/decisions should surface]
```

This template is a **convention, not enforcement** — use the sections that apply, skip those that don't. The structured fields (especially Lessons) enable `watercooler_smart_query` to surface thread conclusions in future planning sessions.

## CLI Commands with Structured Entries

### append-entry
Full control over all metadata:

```bash
watercooler append-entry topic \
  --threads-dir ./watercooler \
  --agent Claude \
  --role critic \
  --title "Review Complete" \
  --type Decision \
  --body "Architecture approved" \
  --status in-review \
  --ball codex
```

### say
Quick entry with auto-ball-flip:

```bash
# Flips ball to counterpart automatically
watercooler say topic \
  --agent Team \
  --role pm \
  --title "Timeline Update" \
  --body "Sprint extended by 2 days"
```

Agent and role default to Team/pm if not specified:

```bash
# Uses defaults: agent=Team, role=pm
watercooler say topic --title "Quick Update" --body "On track"
```

### ack
Acknowledge without changing ball:

```bash
# Ball stays with current owner
watercooler ack topic
```

With custom message:

```bash
watercooler ack topic \
  --title "Acknowledged" \
  --body "Will review by EOD"
```

### handoff
Explicit ball transfer:

```bash
watercooler handoff topic \
  --agent Codex \
  --note "Implementation ready to start"
```

## Agent Format

Agents are identified in the format: `Agent (user)`

Examples:
- `Claude (agent)` - Claude agent used by agent
- `Codex (agent)` - Codex agent used by agent
- `Team (agent)` - Generic team entry by agent

The user tag is automatically added based on git configuration or can be specified via agent registry.

## Ball Auto-Flip Behavior

Understanding ball auto-flip is crucial:

### `say()` - Auto-flips ball
```bash
# If ball is currently "codex", say() will flip to counterpart
watercooler say topic --agent Codex --title "Done" --body "Implemented"
# Ball now belongs to counterpart (e.g., "claude")
```

### `ack()` - Preserves ball
```bash
# Ball stays with current owner
watercooler ack topic --body "Acknowledged"
# Ball unchanged
```

### `append-entry()` - Explicit control
```bash
# Auto-flip if --ball not specified
watercooler append-entry topic --agent Claude --title "Review" --body "OK"

# Explicit ball prevents auto-flip
watercooler append-entry topic --agent Claude --title "Review" --body "OK" --ball claude
```

### `handoff()` - Explicit flip
```bash
# Always flips to counterpart
watercooler handoff topic --agent Codex --note "Your turn"
```

## Best Practices

### 1. Use Appropriate Roles
Match the role to the activity:
- Planning -> `planner`
- Code review -> `critic`
- Implementation -> `implementer`
- Testing -> `tester`
- Coordination -> `pm`
- Documentation -> `scribe`

### 2. Meaningful Titles
Titles should be concise but descriptive:

Good:
- "JWT Implementation Complete"
- "Security Review Passed"
- "Migration Plan Approved"

Bad:
- "Update"
- "Note"
- "Done"

### 3. Choose Correct Entry Types
Use types for semantic meaning:
- Decisions -> `Decision`
- Plans -> `Plan`
- PRs -> `PR`
- Closures -> `Closure`
- Everything else -> `Note`

### 4. Ball Management
- Use `say` for normal back-and-forth
- Use `ack` when you need to respond but keep the ball
- Use `handoff` for explicit coordination

### 5. Structured Bodies
Use markdown in entry bodies:

```bash
watercooler say topic \
  --title "Test Results" \
  --body "## Summary

**Passed**: 42/45 tests
**Failed**: 3 tests

## Next Steps
- Fix timeout issues
- Add retry logic"
```

### 6. Thread Content Arc
In general, threads follow a natural progression that helps maintain context and document the journey:

- **Start**: Persist the state of the project at the start of the thread. Describe why the thread exists and lay out the desired state change for the code/project. This establishes the baseline and goal.

- **Middle**: Reason towards the appropriate solution. Document the exploration, trade-offs considered, and decision-making process.

- **End**: Describe the effective solution reached. Summarize what was implemented, tested, or decided.

- **Often**: Recap that arc in a closing message to the thread. This helps future readers quickly understand the thread's purpose, journey, and outcome.

This arc ensures threads remain self-documenting and provide valuable context for future reference.

### 7. File References
Thread entries should **explicitly reference any files changed** in the entry body. Use file paths (e.g., `src/watercooler_mcp/server.py`, `docs/README.md`) to maintain clear traceability of what was modified. This helps readers quickly locate relevant code or documentation.

## Examples

### Code Review Workflow

```bash
# Implementer completes work
watercooler say feature-auth \
  --agent Codex \
  --role implementer \
  --title "Implementation Complete" \
  --body "JWT auth with refresh tokens implemented"

# Reviewer provides feedback (ball now with reviewer)
watercooler say feature-auth \
  --agent Claude \
  --role critic \
  --title "Review Feedback" \
  --body "Looks good, one suggestion: add rate limiting"

# Implementer acknowledges but keeps ball for fixes
watercooler ack feature-auth \
  --agent Codex \
  --body "Good point, will add rate limiting"

# After fixes, hand off for final review
watercooler handoff feature-auth \
  --agent Codex \
  --note "Rate limiting added, ready for final review"
```

### Decision Making

```bash
# Planner proposes approach
watercooler append-entry feature-cache \
  --agent Claude \
  --role planner \
  --title "Caching Strategy" \
  --type Plan \
  --body "Propose Redis for session cache"

# PM makes decision
watercooler append-entry feature-cache \
  --agent Team \
  --role pm \
  --title "Cache Decision" \
  --type Decision \
  --body "Approved: Redis for sessions, in-memory for config"
```

### Testing Workflow

```bash
# Tester reports results
watercooler say feature-auth \
  --agent Claude \
  --role tester \
  --title "Test Results" \
  --type Note \
  --body "45 tests passing, coverage at 94%"

# Implementer acknowledges
watercooler ack feature-auth \
  --agent Codex \
  --body "Excellent coverage"
```

## See Also

- [TEMPLATES.md](archive/TEMPLATES.md) - Customizing entry templates
- [AGENT_REGISTRY.md](archive/AGENT_REGISTRY.md) - Configuring agents and counterparts
- [README.md](../README.md) - General usage guide
