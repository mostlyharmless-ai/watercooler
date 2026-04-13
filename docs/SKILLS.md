# Skills

Watercooler skills are shortcuts for the most common thread and memory tasks.
They help you ask better questions and get useful answers without remembering
every underlying tool call.

## What a skill is

A skill is a reusable workflow your agent can run for you.

Instead of explaining every step manually, you can use a skill like:

- `/threads` to list or inspect threads
- `/recall` to ask what was previously decided
- `/search-threads` to search with filters

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

In clients that expose Watercooler skills as slash commands, invoke the skill by
name and then add your question or topic.

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
