---
status: completed
priority: p2
issue_id: "024"
tags: [code-review, documentation, agent-native, federation]
dependencies: []
---

# Document `watercooler_federated_search` in mcp-server.md

## Problem Statement

The new `watercooler_federated_search` tool is not documented in `docs/mcp-server.md`, which is the human-facing tool reference. Users and integrators won't know the tool exists or how to use it.

Flagged by agent-native-reviewer.

## Findings

- **Location**: `docs/mcp-server.md`
- **Impact**: Missing from the canonical tool documentation
- **Pattern**: All other tools are documented there with parameters, examples, and notes

## Proposed Solutions

### Solution A: Add tool documentation section (Recommended)

Add a section following the existing pattern:
- Tool name, description
- Parameters with types and defaults
- Example usage
- Notes on federation configuration required

**Pros:** Follows existing pattern, comprehensive
**Cons:** None
**Effort:** Small
**Risk:** Low

## Acceptance Criteria

- [ ] `watercooler_federated_search` documented in `docs/mcp-server.md`
- [ ] Parameters, return format, and usage examples included

## Work Log

| Date | Action | Notes |
|------|--------|-------|
| 2026-02-20 | Created | From review round 3 |

## Resources

- PR #190 on branch `feat/federated-search-phase1`
