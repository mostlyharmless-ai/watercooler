# Workflow examples

These are canonical patterns from real Watercooler usage. They are intentionally brief.

## How write actions work

You don't need to specify write actions explicitly. Tell your agent what to capture
("document the plan and hand off to review"), and it selects the appropriate action
(`say`, `ack`, `handoff`, `set-status`) based on the intent. The write patterns below
show what the agent chooses internally — they're descriptive, not instructions you have
to issue.

If you want fine-grained control, you can specify actions directly:
"use `ack` here — I want the ball to stay with you." But most of the time, the intent
is enough.

## Ground rules

- Thread state changes only through explicit write actions: `say`, `ack`, `handoff`,
  and `set-status`.
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
- `set-status` to `IN_REVIEW` or `OPEN` based on next step

## 3. Multi-agent implementation and review

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

## 4. Blocked or waiting

When to use: Progress pauses on dependency, credentials, CI, or external input.

Capture:
- What is already confirmed
- Exact blocker and impact
- Recommended next action and owner

Write pattern:
- `say` for a complete blocker note
- `ack` for heartbeat updates while ownership stays put
- `handoff` only when next action must move to another person/agent

## 5. Cross-tool or cross-person continuity

When to use: Work switches between clients, teammates, or time zones.

Capture:
- Current state checkpoint before switching
- Branch/environment details needed to continue
- Exact next step so the next contributor can start immediately

Write pattern:
- `say` checkpoint before leaving tool/person A
- `handoff` to tool/person B when action should transfer
- `ack` from new owner to confirm pickup without changing ball again

## 6. Decision and closure hygiene

When to use: Scope ships or a thread reaches a meaningful stopping point.

Capture:
- Final decision and rationale
- What shipped and what did not
- Follow-ups, risks, and references (PR/issues)

Write pattern:
- `say` with `entry_type="Decision"` for final technical call
- `say` with `entry_type="Closure"` for end recap
- `set-status` to `CLOSED`

## Quick chooser

- Use `say` for substantive, durable updates.
- Use `ack` to acknowledge or checkpoint without default ball transfer.
- Use `handoff` when the next action should clearly move to someone else.
- Use `set-status` to mark lifecycle state (`OPEN`, `IN_REVIEW`, `BLOCKED`, `CLOSED`).
