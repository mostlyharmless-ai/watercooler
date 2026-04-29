# Skills

Watercooler skills package common thread and memory workflows so coding agents
can discover and apply them more reliably. In some clients they also appear as
user-invokable slash commands, but that is a secondary convenience rather than
their main purpose.

## What a skill is

A skill is a reusable workflow and prompt scaffold your agent can run for you.

Its main job is to improve workflow selection and tool discoverability for the
agent. Instead of re-deriving the same Watercooler pattern each time, the agent
can reuse a skill such as:

- `/threads` to list or inspect threads
- `/recall` to ask what was previously decided
- `/search-threads` to search with filters

A connected agent can still use Watercooler tools without explicit skill
invocation. Skills mainly make common workflows more consistent and easier to
discover.

## Getting the skills

The open-source Watercooler repository ships five skills under a top-level
`skills/` directory:

- `find-related/`
- `recall/`
- `search-threads/`
- `threads/`
- `watercooler-health/`

Each is a self-contained folder with a `SKILL.md` file and any
supporting scripts or reference material it needs.

Clone the public repo (or download a release archive) and copy the
folders into your client-specific skills directory as shown below:

```bash
git clone https://github.com/mostlyharmless-ai/watercooler.git
# then cp -r watercooler/skills/<name>/ <your-client-skills-dir>/
```

The exact destination depends on which MCP client you use — see the
next section.

## Set up the skills

<details>
<summary>Claude Code</summary>

Place these skill folders in `~/.claude/skills/`:

```text
find-related/
recall/
search-threads/
threads/
watercooler-health/
```

Each folder should contain its own `SKILL.md` file. After adding them, restart
Claude Code so the skills are available as slash commands.

</details>

<details>
<summary>Codex</summary>

Place these skill folders in `~/.codex/skills/`.

If you use a custom `CODEX_HOME`, place them in `$CODEX_HOME/skills/` instead.

```text
find-related/
recall/
search-threads/
threads/
watercooler-health/
```

Each folder should contain its own `SKILL.md` file. After adding them, restart
Codex so the skills are available in the app.

</details>

<details>
<summary>Cursor</summary>

Cursor uses custom commands rather than skill folders. The closest equivalent is
to create one markdown command per workflow in `.cursor/commands/`.

Suggested commands:

```text
.cursor/commands/find-related.md
.cursor/commands/recall.md
.cursor/commands/search-threads.md
.cursor/commands/threads.md
.cursor/commands/watercooler-health.md
```

Each command file should describe the workflow you want Cursor to run. After
adding them, open the Agent chat, type `/`, and choose the command from the
dropdown.

</details>

## How to use skills

In clients that expose Watercooler skills as slash commands, you can invoke the
skill by name and then add your question or topic. This is useful when you want
to steer the agent toward a specific workflow explicitly.

Examples:

```text
/threads
/threads open
/recall What was decided about the config system?
/search-threads type:Decision auth
/find-related branch parity
/watercooler-health
```

## Available skills

| Skill | When to use it | Example |
|---|---|---|
| `threads` | You want an overview of current threads, or you want to inspect one thread | `/threads open` |
| `search-threads` | You know roughly what you want and need to search by topic, role, type, date, or agent | `/search-threads type:Decision auth` |
| `find-related` | You found one useful thread or entry and want connected discussions | `/find-related branch parity` |
| `recall` | You want a direct answer to "what was decided?" or "why did we do this?" | `/recall Why did we choose markdown for threads?` |
| `watercooler-health` | Something feels broken and you want a broad system check | `/watercooler-health` |

## Which skill should I start with?

Use this rule of thumb:

1. Start with `threads` if you need orientation
2. Use `search-threads` if you already know the topic
3. Use `recall` if you want an answer, not just search results
4. Use `find-related` when you want to widen the context
5. Use `watercooler-health` when the system itself may be the problem

## A simple workflow

One practical way to use the skills:

1. Run `/threads` to see what is active
2. Run `/search-threads` or `/recall` to get the context you need
3. Run `/find-related` if you discover a thread worth digging into
4. Run `/watercooler-health` if results look wrong or tools stop behaving
