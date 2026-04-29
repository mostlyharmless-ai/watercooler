# Workflow examples

These are canonical patterns from real Watercooler usage. They are intentionally brief.

For the tool-by-tool reference behind each write action, and for concrete session-start
snippets, see
[TOOLS-REFERENCE.md → Common agent workflows](./TOOLS-REFERENCE.md#common-agent-workflows).

## How write actions work

You don't need to specify write actions explicitly. Tell your agent what to capture
("document the plan and hand off to review"), and it selects the appropriate action
(`say`, `ack`, `handoff`, `set_status`) based on the intent. The write patterns below
show what the agent chooses internally — they're descriptive, not instructions you have
to issue.

If you want fine-grained control, you can specify actions directly:
"use `ack` here — I want the ball to stay with you." But most of the time, the intent
is enough.

## Ground rules

- Thread state changes only through explicit MCP write actions. The
  routine set is `say`, `ack`, `handoff`, and `set_status`; `annotate`
  and `remove_annotation` manage durable tags, flags, and pins, which
  are also part of thread state. Administrative actions (`archive_thread`,
  `delete_entry`, `delete_thread`) have their own tools — see
  [TOOLS-REFERENCE.md](./TOOLS-REFERENCE.md).
- Capture only what needs to stay durable: key plans, decisions, handoffs, blockers,
  and closeout context.
- Use team-attributable agent names in entries when people share the same client:
  `Codex (jay)`, `Codex (caleb)`, `Claude (mina)`.
- User-signaled, agent-authored, Git-persisted.

## 1. Ideation to executable plan

When to use: You are exploring options and need a clear starting plan.

Capture:
- Viable options and tradeoffs
- Chosen direction and why
- First actionable implementation plan

Write pattern:
- `say` with `entry_type="Plan"` when direction is chosen
- `ack` while refining details without ownership transfer
- `handoff` to the implementer when execution can start

## 2. Design ambiguity disentangling

When to use: Requirements are fuzzy or constraints conflict.

Capture:
- Confirmed facts vs assumptions
- Open ambiguity requiring decision
- Decision criteria and resolution

Write pattern:
- `say` to record the ambiguity and candidate options
- `say` or `ack` to record clarified constraints
- `set_status` to `IN_REVIEW` or `OPEN` based on next step

## 3. Planner / Critic converging on a design

When to use: Two roles (typically `planner` and `critic`) iterate a
proposal back and forth until they agree on an approach. The thread
captures the full exchange so the decision is defensible later.

Capture:
- Initial proposal with the tradeoffs the planner considered
- Each critique round: what the critic pushed back on and why
- Each revision: how the planner addressed the concern (or rebutted)
- Explicit convergence moment — both roles signal agreement

Write pattern:
- Planner opens with `say` (`role="planner"`, `entry_type="Plan"`)
- Critic responds with `say` (`role="critic"`, `entry_type="Note"`) raising
  concerns or asking for clarification
- Planner revises with `say` (`role="planner"`) — either a new `Plan` or a
  `Note` explaining the adjustment
- Iterate as many rounds as needed; each side's entry should stand alone
  as a full recap rather than a correction of the previous turn
- Closing:
  - If the exchange produces a concrete direction: planner posts
    `entry_type="Decision"` summarising the agreed approach
  - If it ends in a dead-end or deferral: planner posts a `Note` or
    `set_status` to `BLOCKED` with the blocker recorded

Critics do not typically `handoff` — they signal convergence with `ack`
on the planner's Decision entry, then the planner (or a separate
implementer) picks up via `handoff` when work starts.

## 4. Multi-agent implementation and review

When to use: Planning, implementation, and critique happen across different agents.

Capture:
- Shared briefing before handoff
- Review findings that change behavior
- Updated implementation decision after critique

Write pattern:
- Planner posts `Plan` entry (`say`)
- Implementer posts execution updates (`say`)
- Reviewer posts findings (`say` or `ack`)
- Implementer posts resolved decision and handoff (`handoff`)

## 5. Blocked or waiting

When to use: Progress pauses on dependency, credentials, CI, or external input.

Capture:
- What is already confirmed
- Exact blocker and impact
- Recommended next action and owner

Write pattern:
- `say` for a complete blocker note
- `ack` for heartbeat updates while ownership stays put
- `handoff` only when next action must move to another person/agent

## 6. Cross-tool or cross-person continuity

When to use: Work switches between clients, teammates, or time zones.

Capture:
- Current state checkpoint before switching
- Branch/environment details needed to continue
- Exact next step so the next contributor can start immediately

Write pattern:
- `say` checkpoint before leaving tool/person A
- `handoff` to tool/person B when action should transfer
- `ack` from new owner to confirm pickup without changing ball again

## 7. Decision and closure hygiene

When to use: Scope ships or a thread reaches a meaningful stopping point.

Capture:
- Final decision and rationale
- What shipped and what did not
- Follow-ups, risks, and references (PR/issues)

Write pattern:
- `say` with `entry_type="Decision"` for final technical call
- `say` with `entry_type="Closure"` for end recap
- `set_status` to `CLOSED`

## Quick chooser

- Use `say` for substantive, durable updates.
- Use `ack` to acknowledge or checkpoint without default ball transfer.
- Use `handoff` when the next action should clearly move to someone else.
- Use `set_status` to mark lifecycle state (`OPEN`, `IN_REVIEW`, `BLOCKED`, `CLOSED`).
