# Configuration

Configuration reference for local/open-source Watercooler.

## Minimum viable config

Most users only need these settings. Create `~/.watercooler/config.toml` with:

```toml
# ~/.watercooler/config.toml
version = 1

[mcp]
default_agent = "Claude Code"
agent_tag = "(jay)"
```

Generate an annotated version with:

```bash
watercooler config init --user
```

## Team identity convention

When multiple people use the same client type, set a unique lowercase
`agent_tag` so entries remain attributable.

Examples:

```toml
[mcp]
default_agent = "Codex"
agent_tag = "(jay)"
```

```toml
[mcp]
default_agent = "Codex"
agent_tag = "(caleb)"
```

## Local mode

This guide covers local/open-source Watercooler:

- local git-backed threads
- local MCP transport (`stdio` by default)
- baseline graph and thread operations
- local config and credentials files

Most users do not need any additional infrastructure to get started.

## Config vs credentials

| File | What it stores | Safe to commit? |
|---|---|---|
| `~/.watercooler/config.toml` | Behavior and preferences | Yes |
| `~/.watercooler/credentials.toml` | Secrets (tokens, API keys) | Never |

Project-level overrides are also supported at:

```text
.watercooler/config.toml
```

## Config commands

**Initialize config from template:**

```bash
watercooler config init --user
watercooler config init --project
```

Pass `--force` to overwrite an existing file.

**Show resolved config** (merged user + project + env vars):

```bash
watercooler config show
watercooler config show --json
watercooler config show --sources
watercooler config show --project-path /path/to/repo
```

**Validate config:**

```bash
watercooler config validate
watercooler config validate --strict
```

## Key settings by category

### `[common]` — thread location

| Key | Default | Description |
|---|---|---|
| `templates_dir` | (bundled) | Custom templates directory |
| `threads_suffix` | `"-threads"` | Legacy separate-threads-repo suffix. Ignored in the default orphan-branch setup. |
| `threads_pattern` | (derived) | Legacy full URL pattern for a separate threads repo. Ignored unless migrating from the old model. |

### `[mcp]` — server and identity

| Key | Default | Description |
|---|---|---|
| `default_agent` | `"Agent"` | Agent name shown in thread entries |
| `agent_tag` | `""` | Short lowercase tag appended to agent name |
| `threads_dir` | (auto) | Explicit threads directory; leave empty for auto-discovery |
| `transport` | `"stdio"` | MCP transport for local use: `stdio` or `http` |
| `auto_branch` | `true` | Auto-create threads branches for new code branches |

### `[mcp.git]` — commit identity

| Key | Default | Description |
|---|---|---|
| `author` | `""` (uses agent name) | Git commit author name |
| `email` | `"mcp@watercooler.dev"` | Git commit email |
| `ssh_key` | `""` | Path to SSH private key (empty = use default ssh-agent) |

```toml
[mcp.git]
author = "Claude Code"
email = "claude@example.com"
# ssh_key = "~/.ssh/id_ed25519"
```

### `[dashboard]` — dashboard UI defaults

These settings control optional dashboard UI defaults.

| Key | Default | Description |
|---|---|---|
| `default_repo` | `""` | Repo pre-selected on dashboard load |
| `default_branch` | `"main"` | Default branch for new selections |
| `poll_interval_active` | `15` | Refresh interval in seconds when tab is focused |
| `poll_interval_moderate` | `30` | Refresh interval in seconds when tab is visible but inactive |
| `poll_interval_idle` | `60` | Refresh interval in seconds when tab is hidden |
| `expand_threads_by_default` | `false` | Expand thread rows on load |
| `show_closed_threads` | `false` | Show closed threads by default |

## Environment variable reference

Environment variables override TOML settings.

### Thread and agent settings

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_AGENT` | `mcp.default_agent` | `"Agent"` | Agent name in thread entries |
| `WATERCOOLER_AGENT_TAG` | `mcp.agent_tag` | `""` | Tag appended to agent name |
| `WATERCOOLER_DIR` | `mcp.threads_dir` | (auto) | Explicit threads directory path |
| `WATERCOOLER_THREADS_BASE` | `mcp.threads_base` | (auto) | Base directory for threads repos |
| `WATERCOOLER_THREADS_PATTERN` | `common.threads_pattern` | (derived) | Full URL pattern for a legacy threads repo |
| `WATERCOOLER_AUTO_BRANCH` | `mcp.auto_branch` | `true` | Auto-create threads branches |
| `WATERCOOLER_AUTO_PROVISION` | `mcp.auto_provision` | `true` | Auto-create threads repos |
| `WATERCOOLER_MCP_TRANSPORT` | `mcp.transport` | `"stdio"` | MCP transport: `stdio`, `http`, `proxy`, or `hybrid` |
| `WATERCOOLER_MCP_URL` | `mcp.url` | `""` | Remote MCP endpoint URL for `proxy` or `hybrid` transport |
| `WATERCOOLER_CODE_REPO` | `mcp.proxy_repo` | `""` | Override repo sent to remote MCP in `proxy` or `hybrid` mode |
| `WATERCOOLER_CODE_BRANCH` | `mcp.proxy_branch` | `""` | Override branch sent to remote MCP in `proxy` or `hybrid` mode |
| `WATERCOOLER_ALLOW_LOCAL_ONLY` | _(no TOML)_ | `""` | Set to `1` to explicitly allow thread writes into a directory that is not backed by a GitHub repository. Default behavior refuses such writes with an actionable error. Threads written in local-only mode are **not pushed to any remote**. See [TROUBLESHOOTING.md#local-only-mode](./TROUBLESHOOTING.md#local-only-mode). |

### Git commit identity

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_GIT_AUTHOR` | `mcp.git.author` | `""` | Git commit author name |
| `WATERCOOLER_GIT_EMAIL` | `mcp.git.email` | `"mcp@watercooler.dev"` | Git commit email |
| `WATERCOOLER_GIT_SSH_KEY` | `mcp.git.ssh_key` | `""` | Path to SSH private key |

### Authentication

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `GITHUB_TOKEN` | — | — | GitHub token for git operations |
| `GH_TOKEN` | — | — | Alternative to `GITHUB_TOKEN` |

### MCP server

| Env var | TOML equivalent | Default | Description |
|---|---|---|---|
| `WATERCOOLER_MCP_HOST` | `mcp.host` | `"127.0.0.1"` | HTTP mode bind address |
| `WATERCOOLER_MCP_PORT` | `mcp.port` | `3000` | HTTP mode port |

### Logging

| Env var | Default | Description |
|---|---|---|
| `WATERCOOLER_LOG_LEVEL` | `"INFO"` | Log level |
| `WATERCOOLER_LOG_DIR` | `~/.watercooler/logs/` | Log file directory |
| `WATERCOOLER_LOG_DISABLE_FILE` | `false` | Set to `1` to disable file logging |

### Daemons

Watercooler includes a small set of optional local daemons in open-core mode.
They are disabled by default unless you enable them in config.

| Daemon | What it does |
|---|---|
| `thread_auditor` | Scans threads for hygiene issues and missing structure |
| `sync_guard` | Detects and reports sync problems in the threads worktree |
| `decision_detector` | Scans thread activity for likely decision candidates |
| `decision_extractor` | Turns high-signal decision candidates into structured Decision entries |

Enable daemons globally, then opt in per daemon:

```toml
[mcp.daemons]
enabled = true

[mcp.daemons.thread_auditor]
enabled = true

[mcp.daemons.sync_guard]
enabled = true

[mcp.daemons.decision_detector]
enabled = true

[mcp.daemons.decision_extractor]
enabled = true
```

Inspect daemon state and findings with:

```python
watercooler_daemon_status()
watercooler_daemon_findings(daemon="thread_auditor")
watercooler_daemon_findings(daemon="decision_detector")
```

## Precedence rules

Later sources override earlier ones, on a per-key basis:

1. Built-in defaults
2. User config: `~/.watercooler/config.toml`
3. Project config: `<project>/.watercooler/config.toml`
4. Environment variables

To see which config files are active and in what order, run:

```bash
watercooler config show --sources
```

## Custom roles (`.watercooler/roles.toml`)

Watercooler ships six canonical roles (`planner`, `critic`, `implementer`,
`tester`, `pm`, `scribe`). Projects can extend or override these by creating
`.watercooler/roles.toml` in the repository root.

### Field reference

| Field | Recommended | Type | Description |
|---|---|---|---|
| `description` | strongly recommended | string | One-line summary of what this role does; defaults to `""` if omitted |
| `canonical_role` | strongly recommended | string | Should be one of: `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`. Documents the canonical mapping; defaults to the role name if omitted |
| `produces` | strongly recommended | list | Entry types this role typically writes; defaults to `[]` if omitted |
| `boundary` | recommended | string | What this role explicitly does not do |
| `handoff_to` | recommended | list | Role names this role commonly passes work to |
| `instructions` | recommended | string | Behavioral guidance for agents wearing this role |
| `entry_style` | optional | string | Style guidance for entry body format |
| `when_to_use` | optional | string | Conditions under which to choose this role |
| `collaborate_with` | optional | string | Which other roles this role works alongside |

The only enforced constraint is that the role name exists in the active role set.
Missing fields default to empty strings or lists, so include `description`,
`canonical_role`, and `produces` to make the role useful and discoverable.

### Minimal example

```toml
# .watercooler/roles.toml

[roles.security-audit]
description    = "Review code and configs for security vulnerabilities"
canonical_role = "critic"
produces       = ["Note", "Decision"]
boundary       = "Does not implement fixes — hands off to implementer."
handoff_to     = ["implementer", "pm"]
instructions   = """
Focus on input validation, authentication, authorization, secrets handling,
and dependency risks. Cite exact file paths and line numbers.
"""
```

### Verifying custom roles

```python
watercooler_roles(code_path=".")
watercooler_role_details(code_path=".", role="security-audit")
```

Or via CLI:

```bash
watercooler config validate
```
