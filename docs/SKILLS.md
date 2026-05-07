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

The open-source Watercooler repository ships seven skills under a top-level
`skills/` directory:

- `find-related/`
- `recall/`
- `search-threads/`
- `threads/`
- `update-agent-context/`
- `watercooler-health/`
- `watercooler-onboarding/`

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
update-agent-context/
watercooler-health/
watercooler-onboarding/
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
update-agent-context/
watercooler-health/
watercooler-onboarding/
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
.cursor/commands/update-agent-context.md
.cursor/commands/watercooler-health.md
.cursor/commands/watercooler-onboarding.md
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
/watercooler-onboarding
/update-agent-context --phase2
```

## Available skills

| Skill | When to use it | Example |
|---|---|---|
| `watercooler-onboarding` | First-run setup: seed a repo with structured `onboarding-*` threads (overview, architecture, risk register, etc.) | `/watercooler-onboarding` |
| `update-agent-context` | Refresh `CLAUDE.md` and `AGENTS.md` from the repo + onboarding seeds + recent Decisions | `/update-agent-context --phase2` |
| `threads` | You want an overview of current threads, or you want to inspect one thread | `/threads open` |
| `search-threads` | You know roughly what you want and need to search by topic, role, type, date, or agent | `/search-threads type:Decision auth` |
| `find-related` | You found one useful thread or entry and want connected discussions | `/find-related branch parity` |
| `recall` | You want a direct answer to "what was decided?" or "why did we do this?" | `/recall Why did we choose markdown for threads?` |
| `watercooler-health` | Something feels broken and you want a broad system check | `/watercooler-health` |

## Bootstrapping a repo: setup and ongoing maintenance

The two skills `/watercooler-onboarding` and `/update-agent-context` work
together to seed and maintain the agent-context layer of a repo. They are
independent — you can use either alone — but they compose well.

### What CLAUDE.md and AGENTS.md are

`CLAUDE.md` and `AGENTS.md` are top-level Markdown files that AI coding tools
read on startup to learn the working conventions of a repo. `CLAUDE.md` is the
Claude-specific source of truth in projects that have one; `AGENTS.md` is
derived from it with Claude-specific sections stripped, so other agents
(Codex, Cursor, etc.) can read the same project conventions through their own
front door.

These files are an ecosystem convention, not Watercooler-specific — many repos
use them independently. Watercooler's role is to keep their *content* grounded
in the project's actual decisions and seed context.

**Two phases at a glance.** `/update-agent-context` has two modes that do
very different things:

- **`--phase1`** (full rewrite) — replaces `CLAUDE.md` from scratch using a
  canonical structure, then re-derives `AGENTS.md`. Use on first setup, or
  after a major tooling/role refactor.
- **`--phase2`** (incremental patch) — touches only the
  `## Project Conventions` block of `CLAUDE.md`, folding in new
  `Decision`-typed entries since the last refresh. Use periodically.

The bootstrap flow below uses Phase 1 (via the chain); the maintenance flow
uses Phase 2.

### Bootstrap flow (run once on a new repo)

```text
/watercooler-onboarding
```

This inspects the repo (README, CI, package manifests, git history, existing
threads) and writes a small set of provenance-backed `onboarding-*` seed
threads — overview, product charter, architecture, working map, risk register,
test surface, docs/contracts, team map, and an entry path for new
contributors. The seeds are tagged `onboarding` so other skills can find them.

To also refresh `CLAUDE.md` and `AGENTS.md` in the same run:

```text
/watercooler-onboarding --update-agent-context
```

This chains into `/update-agent-context --phase1` after the seeds are written
and verified, so the new agent-context files reflect the freshly-seeded
threads.

**Backups are taken first.** Existing `CLAUDE.md` and `AGENTS.md` are copied
to `<file>.pre-onboarding.<UTC-timestamp>.bak` before the rewrite. If the
result isn't what you wanted, restore with:

```bash
mv CLAUDE.md.pre-onboarding.<TS>.bak CLAUDE.md
mv AGENTS.md.pre-onboarding.<TS>.bak AGENTS.md
```

The chain is opt-in. Without the flag, `/watercooler-onboarding` finishes
after writing the seeds and prints a recommendation to run
`/update-agent-context --phase1` separately.

### Maintenance flow (run periodically)

```text
/update-agent-context --phase2
```

Run this monthly, or after a batch of `Decision` entries lands in threads.
Phase 2 patches the `## Project Conventions` block of `CLAUDE.md` with new
evidence-backed conventions, then re-derives `AGENTS.md`. It does not touch
the rest of the file.

If `/update-agent-context --phase2` reports no candidates, that's expected on
a young repo — Phase 2 needs Decision entries to fold in. Run
`/watercooler-onboarding` first (Phase 2 also reads onboarding seeds) or wait
until Decisions accumulate.

## Which skill should I start with?

**For setup:** if Watercooler is new to your repo, run `/watercooler-onboarding`
(optionally with `--update-agent-context`) before everything else. See
[Bootstrapping a repo](#bootstrapping-a-repo-setup-and-ongoing-maintenance)
above.

**For everyday use:**

1. Start with `threads` if you need orientation
2. Use `search-threads` if you already know the topic
3. Use `recall` if you want an answer, not just search results
4. Use `find-related` when you want to widen the context
5. Use `watercooler-health` when the system itself may be the problem

## A simple workflow

One practical way to use the skills:

0. **First time only:** run `/watercooler-onboarding` to seed the repo with
   structured `onboarding-*` threads. Add `--update-agent-context` if you also
   want `CLAUDE.md` / `AGENTS.md` populated immediately.
1. Run `/threads` to see what is active
2. Run `/search-threads` or `/recall` to get the context you need
3. Run `/find-related` if you discover a thread worth digging into
4. Run `/watercooler-health` if results look wrong or tools stop behaving

Run `/update-agent-context --phase2` periodically (monthly, or after a batch of
`Decision` entries lands) to keep `CLAUDE.md` / `AGENTS.md` current.
