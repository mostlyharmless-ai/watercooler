> **📦 Archived Documentation**: This document may contain outdated installation URLs. For current setup instructions, see [INSTALLATION.md](../INSTALLATION.md). Production users should install from the `@stable` branch.

# Integration Guide

Complete guide for integrating watercooler-cloud into your project.

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
  - [CLI Usage](#cli-usage)
  - [Python Library Usage](#python-library-usage)
- [Configuration](#configuration)
  - [Threads Directory](#threads-directory)
  - [Templates Directory](#templates-directory)
  - [Environment Variables](#environment-variables)
- [Template Customization](#template-customization)
- [Agent Registry](#agent-registry)
- [Git Configuration](#git-configuration)
- [Integration Patterns](#integration-patterns)
- [Troubleshooting](#troubleshooting)

## Installation

### Development Installation

Currently watercooler-cloud is available for development installation:

```bash
git clone https://github.com/mostlyharmless-ai/watercooler-cloud.git
cd watercooler-cloud
pip install -e .
```

### Future: PyPI Installation

```bash
# Not yet published
pip install watercooler-cloud
```

### Verify Installation

```bash
# Check CLI is available
watercooler --help

# Check version
python3 -c "import watercooler; print(watercooler.__version__)"
```

---

## Quick Start

Watercooler-cloud can be used three ways:
1. **MCP Server** - Automated integration with AI agents (Claude, Codex) - **Recommended**
2. **CLI** - Command-line tool for interactive use and scripts
3. **Python Library** - Programmatic API for custom automation

### MCP Server Integration (Recommended)

The **Model Context Protocol (MCP) server** allows AI agents to automatically use watercooler tools without manual CLI commands.

**Setup guides by client:**
- [Claude Code Setup](./CLAUDE_CODE_SETUP.md) - Configure Claude Code CLI
- [Claude Desktop Setup](./CLAUDE_DESKTOP_SETUP.md) - Configure Claude Desktop app
- [Codex Setup](./QUICKSTART.md#for-codex) - Configure Codex

**Benefits:**
- ✅ AI agents automatically discover and use watercooler tools
- ✅ Natural language interface ("Check my threads", "Respond to feature-x")
- ✅ No manual CLI commands needed
- ✅ Supports cloud sync for team collaboration

**Example workflow:**

```
User: "Claude, what watercooler threads do I have?"
Claude: [calls watercooler_list_threads]
        "You have 3 threads where you have the ball..."

User: "Read the feature-auth thread and respond that it looks good"
Claude: [calls watercooler_read_thread, then watercooler_say]
        "✅ Entry added to 'feature-auth'"
```

See [MCP Server Guide](./mcp-server.md) for complete tool reference.

### CLI Usage

#### Initialize a Thread

Set a threads directory that lives alongside your code repo (matching the MCP layout):

```bash
THREADS_DIR="../demo-project-threads"

watercooler init-thread feature-auth \
  --threads-dir "$THREADS_DIR" \
  --owner agent \
  --participants "agent, Claude, Codex" \
  --ball codex
```

This creates `$THREADS_DIR/feature-auth.md`:

```markdown
Title: Feature auth
Status: open
Ball: codex
Updated: 2025-10-07T10:00:00Z
Owner: agent
Participants: agent, Claude, Codex

# Feature auth
```

#### Add an Entry

```bash
watercooler say feature-auth \
  --threads-dir "$THREADS_DIR" \
  --agent Claude \
  --role critic \
  --title "Design Review" \
  --body "Authentication approach looks solid"
```

#### Update Status

```bash
watercooler set-status feature-auth in-review --threads-dir "$THREADS_DIR"
```

#### List Threads

```bash
watercooler list --threads-dir "$THREADS_DIR"
# Output:
# 2025-10-07T10:00:00Z    open    codex        feature-auth    $THREADS_DIR/feature-auth.md
```

See [README.md](../../README.md) for complete CLI reference.

---

### Python Library Usage

#### Basic Operations

```python
from pathlib import Path
from watercooler import read, write, thread_path, bump_header, AdvisoryLock

# Configuration (matches CLI/MCP sibling repo pattern)
repo_root = Path("/workspace/demo-project").resolve()
threads_dir = repo_root.parent / f"{repo_root.name}-threads"
topic = "feature-auth"

# Get thread path
t_path = thread_path(topic, threads_dir)
lock_path = threads_dir / f".{topic}.lock"

# Read thread with locking
with AdvisoryLock(lock_path, timeout=5):
    content = read(t_path)
    print(content)

# Update thread
with AdvisoryLock(lock_path, timeout=5):
    content = read(t_path)
    content = bump_header(content, status="in-review", ball="Claude")
    write(t_path, content)
```

#### Using Commands Module

For higher-level operations:

```python
from pathlib import Path
from watercooler.commands import init_thread, say, set_status

repo_root = Path("/workspace/demo-project").resolve()
threads_dir = repo_root.parent / f"{repo_root.name}-threads"

# Initialize thread
init_thread(
    "feature-auth",
    threads_dir=threads_dir,
    title="Feature Authentication",
    status="open",
    ball="codex"
)

# Add entry
say(
    "feature-auth",
    threads_dir=threads_dir,
    agent="Claude",
    role="critic",
    title="Review Complete",
    body="All good!"
)

# Update status
set_status("feature-auth", threads_dir=threads_dir, status="closed")
```

#### Python API Reference

Import the core helpers directly from the package:

```python
from watercooler import read, write, thread_path, bump_header, AdvisoryLock
```

- `read(path: Path) -> str` – load a thread file
- `write(path: Path, text: str)` – write text (parents auto-created)
- `thread_path(topic: str, threads_dir: Path) -> Path` – resolve topic → file path
- `bump_header(text: str, *, status=None, ball=None) -> str` – update header metadata
- `AdvisoryLock(path: Path, timeout=5, ttl=30)` – PID-aware lock used in CLI and MCP flows

For higher-level operations use the `commands` module:

```python
from watercooler.commands import init_thread, say, set_status, append_entry
```

- `init_thread(topic, threads_dir, **kwargs)` – create a thread using templates
- `say(...)` / `append_entry(...)` – append structured entries, handling templates, agent registry, and idempotency markers
- `set_status(topic, threads_dir, status)` – adjust Status header
- `set_ball(topic, threads_dir, ball)` – change Ball owner explicitly

Most applications only need these helpers; the CLI and MCP server are built on top of them.

##### Locking example

```python
from pathlib import Path
from watercooler import read, write, AdvisoryLock

repo_root = Path("/workspace/demo-project").resolve()
threads_dir = repo_root.parent / f"{repo_root.name}-threads"
thread = threads_dir / "feature-auth.md"
lock = threads_dir / ".feature-auth.lock"

with AdvisoryLock(lock, timeout=5):
    text = read(thread)
    text = bump_header(text, status="IN_REVIEW")
    write(thread, text)
```

---

## Configuration

### Threads Directory

The CLI stores threads wherever you point `--threads-dir`. To stay consistent
with the MCP server, use the sibling repo layout:

```bash
THREADS_DIR="../<repo>-threads"
watercooler list --threads-dir "$THREADS_DIR"
```

Configuration precedence:
1. CLI argument: `--threads-dir <path>` (recommended)
2. Environment variable: `WATERCOOLER_DIR` (manual override for a fixed location)
3. If neither is set, the current prerelease resolves the sibling `<repo>-threads`
   directory that lives beside your code repository. If you still observe a
   repo-local fallback such as `./threads-local`, upgrade the package and clean
   up the stray directory—it’s treated as a misconfiguration going forward.

**Python usage:**

```python
from pathlib import Path
from watercooler.config import resolve_threads_dir

repo_root = Path("/workspace/demo-project").resolve()
recommended = repo_root.parent / f"{repo_root.name}-threads"
threads_dir = resolve_threads_dir(cli_value=str(recommended))
```

---

### Templates Directory

Templates customize thread and entry formatting.

**Default:** Package bundled templates

**Configuration precedence:**
1. CLI argument: `--templates-dir <path>`
2. Environment variable: `WATERCOOLER_TEMPLATES`
3. Project-local: `$THREADS_DIR/templates/` (if exists)
4. Package bundled templates (fallback)

**Template files:**
- `_TEMPLATE_topic_thread.md` - Thread initialization template
- `_TEMPLATE_entry_block.md` - Entry format template

**Example - Project-local templates:**

```bash
# Create project-local templates
mkdir -p "$THREADS_DIR"/templates

TEMPLATE_DIR=$(python3 -c "from pathlib import Path; import watercooler; print(Path(watercooler.__file__).parent / 'templates')")
cp "$TEMPLATE_DIR"/_TEMPLATE_*.md "$THREADS_DIR"/templates/

# Edit template
vim "$THREADS_DIR"/templates/_TEMPLATE_topic_thread.md

# Will be used automatically
watercooler init-thread new-topic --threads-dir "$THREADS_DIR"
```

See [Templates Guide](TEMPLATES.md) for complete customization reference.

---

### Environment Variables

Watercooler supports multiple environment variables for configuration. For complete documentation, see **[ENVIRONMENT_VARS.md](./ENVIRONMENT_VARS.md)**.

#### Key Variables

**Core Configuration:**
- `WATERCOOLER_AGENT` - Agent identity (required for MCP, optional for CLI)
- `WATERCOOLER_THREADS_BASE` - Optional root for local thread clones (otherwise the sibling `<repo>-threads` directory is used)
- `WATERCOOLER_THREADS_PATTERN` - Remote repo pattern (`https://github.com/{org}/{repo}-threads.git` default)
- `WATERCOOLER_AUTO_BRANCH` - Enable/disable automatic branch creation (`1` by default)
- `WATERCOOLER_TEMPLATES` - Custom templates directory
- `WATERCOOLER_DIR` - Manual override for a fixed threads directory (disables universal discovery)

**Cloud Sync (Optional):**
- `WATERCOOLER_GIT_REPO` - Git repository URL (enables cloud mode)
- `WATERCOOLER_GIT_SSH_KEY` - SSH private key path
- `WATERCOOLER_GIT_AUTHOR` - Git commit author name
- `WATERCOOLER_GIT_EMAIL` - Git commit author email

**Advanced:**
- `WATERCOOLER_USER` - Username override for lock files (low-level, rarely needed)

**Locking Configuration:**
- `WCOOLER_LOCK_TTL` - Lock time-to-live in seconds (default: 30)
- `WCOOLER_LOCK_POLL` - Lock polling interval in seconds (default: 0.1)

**Example `.env` file:**

```bash
# MCP configuration
WATERCOOLER_AGENT="Claude@Code"
# Optional central cache for threads repos
# WATERCOOLER_THREADS_BASE="/srv/watercooler-threads"
WATERCOOLER_THREADS_PATTERN="https://github.com/{org}/{repo}-threads.git"
WATERCOOLER_AUTO_BRANCH=1

# Optional: Cloud sync
WATERCOOLER_GIT_REPO=https://github.com/org/watercooler-threads.git
WATERCOOLER_GIT_SSH_KEY=/path/to/deploy/key
WATERCOOLER_GIT_AUTHOR="Agent Name"
WATERCOOLER_GIT_EMAIL=agent@example.com

# Optional: Custom templates
WATERCOOLER_TEMPLATES="../demo-project-threads/templates"

# Optional: Lock tuning
WCOOLER_LOCK_TTL=60
WCOOLER_LOCK_POLL=0.2
```

See **[ENVIRONMENT_VARS.md](./ENVIRONMENT_VARS.md)** for detailed documentation and troubleshooting.

---

## Template Customization

### Quick Template Override

Copy bundled templates and customize:

```bash
# Find bundled templates
python3 -c "from pathlib import Path; import watercooler; print(Path(watercooler.__file__).parent / 'templates')"

# Copy to project
mkdir -p "$THREADS_DIR"/templates
cp <bundled-path>/_TEMPLATE_topic_thread.md "$THREADS_DIR"/templates/
cp <bundled-path>/_TEMPLATE_entry_block.md "$THREADS_DIR"/templates/

# Customize
vim "$THREADS_DIR"/templates/_TEMPLATE_topic_thread.md
```

### Thread Template

`$THREADS_DIR/templates/_TEMPLATE_topic_thread.md`:

```markdown
Title: {{Short title}}
Status: {{STATUS}}
Ball: {{BALL}}
Updated: {{UTC}}
Owner: {{OWNER}}
Participants: {{PARTICIPANTS}}

# {{Short title}}

Initial body here...
```

**Placeholders:**
- `{{TOPIC}}` or `<TOPIC>` - Thread topic
- `{{Short title}}` - Human-readable title
- `{{STATUS}}` - Initial status
- `{{BALL}}` - Initial ball owner
- `{{UTC}}` or `{{NOWUTC}}` - Current timestamp
- `{{OWNER}}` - Thread owner
- `{{PARTICIPANTS}}` - Comma-separated participant list

### Entry Template

`$THREADS_DIR/templates/_TEMPLATE_entry_block.md`:

```markdown
---
Entry: {{AGENT}} {{UTC}}
Role: {{ROLE}}
Type: {{TYPE}}
Title: {{TITLE}}

{{BODY}}
```

**Placeholders:**
- `{{AGENT}}` - Agent name with user tag (e.g., "Claude (agent)")
- `{{UTC}}` - Entry timestamp
- `{{ROLE}}` - Agent role (planner, critic, implementer, tester, pm, scribe)
- `{{TYPE}}` - Entry type (Note, Plan, Decision, PR, Closure)
- `{{TITLE}}` - Entry title
- `{{BODY}}` - Entry body text

See [Templates Guide](TEMPLATES.md) for advanced customization.

---

## Agent Registry

Configure agent behavior with a JSON registry.

### Default Agents

Built-in agents with counterpart mappings:
- **Codex** ↔ **Claude** (default pair)
- **Team** (neutral, no auto-flip)

### Custom Agent Registry

Create `agents.json`:

```json
{
  "canonical": {
    "gpt": "GPT",
    "claude": "Claude",
    "codex": "Codex",
    "team": "Team"
  },
  "counterpart": {
    "GPT": "Claude",
    "Claude": "Codex",
    "Codex": "Claude"
  },
  "default_agent": "Team",
  "default_role": "pm",
  "default_ball": "Team"
}
```

**Usage:**

```bash
watercooler say feature-auth \
  --agents-file ./agents.json \
  --agent gpt \
  --title "Review" \
  --body "Looks good"
```

See [Agent Registry Guide](AGENT_REGISTRY.md) for complete reference.

---

## Git Configuration

For multi-user collaboration, configure git merge strategies.

### Required: Enable "ours" merge driver (for generated indexes)

```bash
git config merge.ours.driver true
```

### Recommended: Pre-commit Hook

```bash
# Enable project hooks
git config core.hooksPath .githooks
```

### `.gitattributes` template

```
# Append-only thread files
*.md merge=union

# Generated indexes
index.md merge=ours
```

This keeps thread entries from both sides during merges while allowing the index to regenerate cleanly after branch joins.

See [Git Setup Guide](../../.github/WATERCOOLER_SETUP.md) for detailed configuration.

---

## Integration Patterns

### Pattern 1: Automation Script

```python
#!/usr/bin/env python3
"""Automated thread status updater."""
from pathlib import Path
from watercooler.commands import set_status
from watercooler.metadata import thread_meta, is_closed

repo_root = Path("/workspace/demo-project").resolve()
threads_dir = repo_root.parent / f"{repo_root.name}-threads"

# Find all open threads
for thread_file in threads_dir.glob("*.md"):
    title, status, ball, updated = thread_meta(thread_file)

    if not is_closed(status):
        # Check some condition
        if should_close_thread(title):
            topic = thread_file.stem
            set_status(topic, threads_dir=threads_dir, status="closed")
            print(f"Closed: {title}")

def should_close_thread(title: str) -> bool:
    # Your logic here
    return "deprecated" in title.lower()
```

### Pattern 2: CI/CD Integration

```yaml
# .github/workflows/watercooler.yml
name: Watercooler Index
on:
  push:
    paths:
      - 'threads/*.md'

jobs:
  update-index:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code repo
        uses: actions/checkout@v3
        with:
          path: repo
      - name: Checkout threads repo
        uses: actions/checkout@v3
        with:
          repository: ${{ github.repository }}-threads
          path: threads
      - name: Setup Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - name: Install watercooler
        run: pip install git+https://github.com/mostlyharmless-ai/watercooler-cloud.git
      - name: Generate index
        run: |
          watercooler reindex --threads-dir threads
          watercooler web-export --threads-dir threads
      - name: Commit updates
        working-directory: threads
        run: |
          git config user.name "Bot"
          git config user.email "bot@example.com"
          git add index.{md,html}
          git commit -m "chore: update watercooler index" || true
          git push
```

### Pattern 3: Pre-commit Hook

`.githooks/pre-commit`:

```bash
#!/bin/bash
# Validate watercooler threads before commit

set -e

echo "Validating watercooler threads..."

# Path to threads repo (adjust as needed)
THREADS_DIR="../<repo>-threads"

# Check for merge conflicts in threads
if git diff --cached "$THREADS_DIR"/*.md | grep -q '<<<<<<<'; then
    echo "Error: Merge conflict markers found in threads"
    exit 1
fi

# Regenerate index
watercooler reindex --threads-dir "$THREADS_DIR"

# Stage index if changed
git add "$THREADS_DIR"/index.md

echo "✓ Watercooler validation passed"
```

Make executable:

```bash
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks
```

### Pattern 4: Wrapper Script

`tools/wc-say`:

```bash
#!/bin/bash
# Convenience wrapper for common watercooler operations

set -e

THREADS_DIR="${THREADS_DIR:-../<repo>-threads}"

if [ ! -d "$THREADS_DIR" ]; then
    echo "Threads directory '$THREADS_DIR' does not exist. Set THREADS_DIR before running." >&2
    exit 1
fi

if [ -z "$1" ]; then
    echo "Usage: wc-say <topic> <message>"
    exit 1
fi

TOPIC="$1"
shift
MESSAGE="$*"

watercooler say "$TOPIC" \
    --threads-dir "$THREADS_DIR" \
    --agent Team \
    --role pm \
    --title "Quick Note" \
    --body "$MESSAGE"

echo "✓ Added note to $TOPIC"
```

Usage:

```bash
./tools/wc-say feature-auth "Deployment complete"
```

---

## Troubleshooting

### Issue: Command not found

```bash
watercooler: command not found
```

**Solution:**
- Ensure installation completed: `pip install -e .`
- Check PATH includes pip bin directory: `python3 -m site --user-base`
- Try: `python3 -m watercooler.cli --help`

### Issue: Lock timeout

```
AdvisoryLock timeout after 5 seconds
```

**Solutions:**
- Increase timeout: `WCOOLER_LOCK_TTL=60 watercooler say ...`
- Remove stuck lock: `watercooler unlock <topic>`
- Check for hung processes: `ps aux | grep watercooler`

### Issue: Template not found

```
FileNotFoundError: Template '_TEMPLATE_topic_thread.md' not found
```

**Solutions:**
- Check template location: `ls "$THREADS_DIR"/templates/`
- Verify `WATERCOOLER_TEMPLATES` if set
- Reset to bundled: `unset WATERCOOLER_TEMPLATES`

### Issue: Permission denied

```
PermissionError: [Errno 13] Permission denied: '$THREADS_DIR/feature-auth.md'
```

**Solutions:**
- Check file permissions: `ls -la "$THREADS_DIR"`
- Ensure directory is writable: `chmod u+w "$THREADS_DIR"`
- Check lock file isn't orphaned: `watercooler unlock <topic>`

### Issue: Git merge conflicts

```
CONFLICT (content): Merge conflict in threads/feature-auth.md
```

**Solutions:**
- Configure merge driver: `git config merge.ours.driver true`
- Create `.gitattributes`: `*.md merge=union` and `index.md merge=ours`
- See [Git Setup Guide](../../.github/WATERCOOLER_SETUP.md)

---

## See Also

### Getting Started
- [Quickstart Guide](./QUICKSTART.md) - 5-minute setup for MCP server
- [Claude Code Setup](./CLAUDE_CODE_SETUP.md) - Configure Claude Code
- [Claude Desktop Setup](./CLAUDE_DESKTOP_SETUP.md) - Configure Claude Desktop

### Configuration
- [Environment Variables](./ENVIRONMENT_VARS.md) - Complete configuration reference
- [Agent Registry](AGENT_REGISTRY.md) - Agent configuration guide
- [Templates Guide](TEMPLATES.md) - Template customization reference

### Reference
- [MCP Server Guide](./mcp-server.md) - MCP tool documentation
- [Python API Reference](#python-api-reference) - Library usage summary
- [Troubleshooting](./TROUBLESHOOTING.md) - Common issues and solutions
- [Roadmap](../../ROADMAP.md) - Project status and future plans

### Community
- [Use Cases Guide](USE_CASES.md) - Real-world examples
- [FAQ](FAQ.md) - Frequently asked questions
- [GitHub Repository](https://github.com/mostlyharmless-ai/watercooler-cloud)
