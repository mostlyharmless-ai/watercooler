---
name: mcp-branch
description: Switch the watercooler-cloud MCP server branch in ~/.claude.json to the current git branch (or use --reset to revert to main). Use before restarting a session to test code changes on a feature branch.
allowed-tools:
  - Bash(git branch --show-current)
  - Bash(python3 *)
---

# MCP Branch Switch

Update `~/.claude.json` so the watercooler-cloud MCP server pulls from
the current git branch instead of `main`. After running this skill, exit
and start a new Claude Code session for the change to take effect.

Optional arguments via `$ARGUMENTS`:
- A branch name to switch to (e.g., `/mcp-branch fix/my-feature`)
- `--reset` to switch back to `main`

## Steps

1. **Determine target branch** (first match wins):
   - If `$ARGUMENTS` contains `--reset`, target is `main` (ignores any branch name)
   - If `$ARGUMENTS` contains a branch name, use that
   - Otherwise, detect the current branch:
     ```bash
     git branch --show-current
     ```

2. **Update `~/.claude.json`** — substitute the branch from step 1 for `$BRANCH`:
   ```bash
   BRANCH="<branch from step 1>"
   python3 -c "
   import json, pathlib, re, sys

   target = sys.argv[1]

   # Validate branch name
   if not target:
       print('Error: could not determine target branch (empty — detached HEAD?)', file=sys.stderr)
       sys.exit(1)
   if not re.match(r'^[A-Za-z0-9._/-]+$', target):
       print(f'Error: invalid branch name: {target!r}', file=sys.stderr)
       sys.exit(1)

   path = pathlib.Path.home() / '.claude.json'

   try:
       with open(path) as f:
           config = json.load(f)

       args = config['mcpServers']['watercooler-cloud']['args']

       # Find the git+https URL arg (contains @branch suffix)
       idx = next(i for i, a in enumerate(args) if a.startswith('git+https://') and '@' in a)
       old = args[idx]
       prefix = old.rsplit('@', 1)[0]  # everything before @branch
       args[idx] = f'{prefix}@{target}'

       with open(path, 'w') as f:
           json.dump(config, f, indent=2)
           f.write('\n')

       print(f'Updated: {old}')
       print(f'     ->  {args[idx]}')
   except FileNotFoundError:
       print(f'Error: {path} not found', file=sys.stderr)
       sys.exit(1)
   except (KeyError, StopIteration):
       print('Error: could not find watercooler-cloud git+https URL in mcpServers args', file=sys.stderr)
       sys.exit(1)
   " "$BRANCH"
   ```

3. **Report result**:
   - Show the old and new `git+https` URL
   - Remind the user to exit and start a new session
   - If the branch was set to anything other than `main`, remind them
     to run `/mcp-branch --reset` when done testing
