---
status: completed
priority: p2
issue_id: "023"
tags: [code-review, agent-native, discoverability, federation]
dependencies: []
---

# Add `watercooler_federated_search` to instructions resource

## Problem Statement

The `watercooler://instructions` MCP resource lists all available tools for agent self-discovery. The new `watercooler_federated_search` tool is not listed there. Agents reading this resource won't discover the federation search capability.

Flagged by agent-native-reviewer.

## Findings

- **Location**: `src/watercooler_mcp/resources.py`
- **Impact**: Agents that read `watercooler://instructions` for tool discovery will miss the federation tool
- **Pattern**: Every other tool is listed in this resource

## Proposed Solutions

### Solution A: Add tool entry to instructions resource (Recommended)

Add `watercooler_federated_search` with description, parameters, and usage example to the instructions resource template.

**Pros:** Follows existing pattern, enables agent self-discovery
**Cons:** None
**Effort:** Small
**Risk:** Low

## Acceptance Criteria

- [ ] `watercooler_federated_search` appears in `watercooler://instructions` resource
- [ ] Description and parameter list are accurate

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
