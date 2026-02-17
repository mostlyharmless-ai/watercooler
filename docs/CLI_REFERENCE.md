# CLI Reference

Complete command-line interface reference for watercooler-cloud.

## Overview

Watercooler provides 12 CLI commands for thread-based collaboration with explicit status and ball tracking. All commands default to the `.watercooler` directory. Use `--threads-dir` to override.

---

## Thread Management Commands

### init-thread

Initialize a new thread with custom metadata.

**Syntax:**
```bash
watercooler init-thread <topic> [options]
```

**Options:**
- `--owner <name>` - Thread owner (default: agent)
- `--participants <list>` - Comma-separated participant list
- `--ball <agent>` - Initial ball owner
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler init-thread feature-auth \
  --owner agent \
  --participants "agent, Claude, Codex" \
  --ball codex
```

---

### list

List all threads with their status.

**Syntax:**
```bash
watercooler list [options]
```

**Options:**
- `--open-only` - Show only open threads
- `--closed-only` - Show only closed threads
- `--threads-dir <path>` - Override default threads directory

**Examples:**
```bash
# List all open threads
watercooler list

# List only closed threads
watercooler list --closed-only

# Override directory
watercooler list --threads-dir ./custom-threads
```

---

### search

Search across all threads for specific content.

**Syntax:**
```bash
watercooler search <query> [options]
```

**Options:**
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler search "security"
```

---

### reindex

Regenerate the markdown index for all threads.

**Syntax:**
```bash
watercooler reindex [options]
```

**Options:**
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler reindex
```

---

### web-export

Export threads as HTML index.

**Syntax:**
```bash
watercooler web-export [options]
```

**Options:**
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler web-export
```

---

## Entry Commands

### append-entry

Add a structured entry to a thread with role and type metadata.

**Syntax:**
```bash
watercooler append-entry <topic> [options]
```

**Options:**
- `--agent <name>` - Entry author
- `--role <role>` - Agent role (planner, critic, implementer, tester, pm, scribe)
- `--title <text>` - Entry title
- `--type <type>` - Entry type (Note, Plan, Decision, PR, Closure)
- `--body <text>` - Entry body content
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler append-entry feature-auth \
  --agent Claude \
  --role critic \
  --title "Security Review Complete" \
  --type Decision \
  --body "Authentication approach approved"
```

---

### say

Quick team note with automatic ball flip to counterpart.

**Syntax:**
```bash
watercooler say <topic> [options]
```

**Options:**
- `--agent <name>` - Entry author
- `--role <role>` - Agent role
- `--title <text>` - Entry title
- `--body <text>` - Entry body content
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler say feature-auth \
  --agent Team \
  --role pm \
  --title "Timeline Update" \
  --body "Target: end of sprint"
```

> **Note:** `say()` automatically flips the ball to the counterpart agent.

---

### ack

Acknowledge an entry without flipping the ball.

**Syntax:**
```bash
watercooler ack <topic> [options]
```

**Options:**
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler ack feature-auth
```

> **Note:** `ack()` preserves the current ball owner.

---

### handoff

Explicit handoff to a specific agent with optional note.

**Syntax:**
```bash
watercooler handoff <topic> [options]
```

**Options:**
- `--agent <name>` - Current agent performing handoff
- `--note <text>` - Handoff message
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler handoff feature-auth \
  --agent Codex \
  --note "Ready for implementation"
```

---

## Status & Ball Commands

### set-status

Update the thread status.

**Syntax:**
```bash
watercooler set-status <topic> <status> [options]
```

**Status Values:**
- `OPEN` - Active thread
- `IN_REVIEW` - Under review
- `CLOSED` - Completed/resolved
- Custom statuses also supported

**Options:**
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler set-status feature-auth IN_REVIEW
```

---

### set-ball

Update the ball owner (who has the next action).

**Syntax:**
```bash
watercooler set-ball <topic> <agent> [options]
```

**Options:**
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
watercooler set-ball feature-auth Claude
```

---

## Utility Commands

### unlock

Clear advisory lock on a thread.

**Syntax:**
```bash
watercooler unlock <topic> [options]
```

**Options:**
- `--force` - Force unlock even if lock is fresh
- `--threads-dir <path>` - Override default threads directory

**Example:**
```bash
# Normal unlock (checks TTL)
watercooler unlock feature-auth

# Force unlock
watercooler unlock feature-auth --force
```

---

## Structured Entry Format

Each entry includes rich metadata in YAML frontmatter:

```markdown
---
Entry: Agent (user) 2025-10-06T12:00:00Z
Role: critic
Type: Decision
Title: Security Review Complete

Authentication approach approved. All edge cases covered.
```

### Agent Roles

- **planner** - Architecture and design decisions
- **critic** - Code review and quality assessment
- **implementer** - Feature implementation
- **tester** - Test coverage and validation
- **pm** - Project management and coordination
- **scribe** - Documentation and notes

### Entry Types

- **Note** - General observations and updates
- **Plan** - Design proposals and roadmaps
- **Decision** - Architectural or technical decisions
- **PR** - Pull request related entries
- **Closure** - Thread conclusion and summary

---

## Agent Registry & Templates

### Using Custom Agent Registry

```bash
watercooler say feature-auth \
  --agents-file ./agents.json \
  --agent codex \
  --role implementer \
  --title "Implementation Complete" \
  --body "All tests passing"
```

### Using Custom Templates

```bash
export WATERCOOLER_TEMPLATES=/path/to/custom/templates
watercooler init-thread new-topic
```

**Template Discovery Order:**
1. CLI argument
2. Environment variable (`WATERCOOLER_TEMPLATES`)
3. Project-local templates
4. Bundled templates

---

## Complete Workflow Example

```bash
# 1. Initialize a thread
watercooler init-thread feature-payment \
  --owner agent \
  --participants "Claude, Codex, Team" \
  --ball Claude

# 2. Claude adds a plan
watercooler append-entry feature-payment \
  --agent Claude \
  --role planner \
  --title "Payment Integration Design" \
  --type Plan \
  --body "Propose Stripe integration with webhook handling"

# 3. Team provides feedback
watercooler say feature-payment \
  --agent Team \
  --role pm \
  --title "Approved" \
  --body "Looks good, please proceed"

# 4. Codex implements
watercooler say feature-payment \
  --agent Codex \
  --role implementer \
  --title "Implementation Complete" \
  --body "Stripe integration added with tests"

# 5. Move to review
watercooler set-status feature-payment IN_REVIEW

# 6. Claude reviews
watercooler say feature-payment \
  --agent Claude \
  --role critic \
  --title "Review Complete" \
  --body "LGTM, ready to merge"

# 7. Close the thread
watercooler set-status feature-payment CLOSED

# 8. List all threads
watercooler list

# 9. Search for payment-related discussions
watercooler search "payment"

# 10. Regenerate index
watercooler reindex
```

---

## Additional Resources

- **[Installation Guide](INSTALLATION.md)** - Setup and configuration
- **[Structured Entries](STRUCTURED_ENTRIES.md)** - Detailed entry format specification
- **[MCP Server Guide](mcp-server.md)** - AI agent integration via MCP
