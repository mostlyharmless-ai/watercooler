# Architecture

Watercooler is a thin layer on top of git. This document explains where
thread data lives, how reads and writes differ, and how Watercooler stays
out of the way of your code.

Read this once early — most questions about configuration, federation,
daemons, and troubleshooting become easier once the storage picture is
clear.

## The storage picture

```text
                 +------------------------------------------+
your-repo/       |  main / feature branches   (your code)   |
                 |                                          |
                 |  watercooler/threads       (orphan)      |
                 |    ├── graph/baseline/     (graph data)  |
                 |    └── threads/*.md        (projections) |
                 +------------------------------------------+
                                  ▲
                                  │ git fetch / pull / push
                                  ▼
~/.watercooler/worktrees/<repo>/  (checkout of the orphan branch)
        │
        ├── threads/*.md                   ← markdown projections (write-only)
        └── graph/baseline/                ← baseline graph (source of truth)
              ├── nodes.jsonl, edges.jsonl   (global nodes + edges)
              └── threads/<topic>/             (per-topic JSONL: entries,
                                                edges, meta)

~/.watercooler/                   (shared across all watercooler-backed repos)
        ├── daemons/                   ← per-daemon finding logs
        └── cache/embeddings/          ← optional semantic search vectors
```

Every Watercooler install has three pieces:

1. **Your code repo.** Untouched by Watercooler except for one extra
   branch — the orphan branch — that has no shared history with your
   code branches.
2. **The orphan branch `watercooler/threads`.** This is where thread
   data is committed and pushed. It lives inside your existing repo, so
   there is no second repository to manage.
3. **The worktree at `~/.watercooler/worktrees/<repo>/`.** A local
   checkout of the orphan branch. Reads and writes go through this
   worktree; the MCP server never touches your code checkout directly.

## The baseline graph is the source of truth

Inside the worktree, two artifacts coexist:

- **`graph/baseline/`** — the baseline graph. A directory of JSON Lines
  files (global `nodes.jsonl` + `edges.jsonl`, plus per-topic
  `threads/<topic>/{entries,edges,meta}.jsonl`) with every thread,
  every entry, every role, every link. **Every read goes through this
  graph.**
- **`threads/*.md`** — markdown projections of the same data. These are
  **write-only**: they are produced when an entry is written so that
  git diffs and human review tools have something readable. Reads
  never parse them.

This matters for one reason: if you hand-edit a markdown file under the
worktree, the baseline graph does not change, and the next read will
ignore your edit. All writes must go through MCP tools or the
`watercooler` CLI, which update both artifacts atomically.

## Branch scoping

Every entry is tagged with the code branch it was written from. The
branch label is auto-populated from the currently-checked-out branch of
your repo at write time — you do not have to set it.

Reads filter by `code_branch` by default. This keeps threads scoped to
the branch that produced them. Pass `code_branch="*"` to any read tool
to see entries from all branches.

The three footer fields `Code-Repo`, `Code-Branch`, and `Code-Commit`
are recorded in every orphan-branch commit so you can trace any thread
entry back to the exact state of your code at write time.

## User vs project configuration

Watercooler resolves config from two locations, in order:

| Path | Scope | What it holds |
|---|---|---|
| `~/.watercooler/config.toml` | user — applies to every repo | agent identity, transport, dashboard defaults |
| `<your-repo>/.watercooler/config.toml` | project — repo-local overrides | per-project daemon enablement, custom roles, federation |

Project config wins on keys it sets. Environment variables win over
both. See [CONFIGURATION.md](./CONFIGURATION.md) for the precedence
rules and full key list.

The two paths are independent:

- `~/.watercooler/` — your user-level config, credentials, and the
  worktrees directory
- `<your-repo>/.watercooler/` — repo-local overrides (can be committed)

## Read and write flow

**Reads:**

```text
MCP read tool  →  baseline graph  →  ranked results
                  (~/.watercooler/worktrees/<repo>/graph/baseline/)
```

Reads attempt a quick `git fetch` + fast-forward before returning. If
the remote is unreachable, reads fall back to local data and surface a
sync warning in the response rather than failing. The baseline graph
is the local materialized state of the orphan branch — no remote
round-trip happens during result ranking itself.

**Writes:**

```text
MCP write tool
  → acquire topic lock
  → git pull (fetch + rebase)
  → update baseline graph
  → rewrite markdown projections
  → git commit  (footer: Code-Repo, Code-Branch, Code-Commit,
                         Watercooler-Entry-ID, Watercooler-Topic)
  → git push (retry with rebase on conflict)
  → release lock
```

Writes are synchronous. If push fails after retries, the entry is
preserved locally (your graph and worktree are consistent) and the
response surfaces a warning — no data is lost. See
[TROUBLESHOOTING — Sync push failure](./TROUBLESHOOTING.md#sync-push-failure)
for recovery.

## Federation in one paragraph

Federation lets one MCP server search across multiple repositories in a
single call. Each repo is a **namespace**. The namespace you pass as
`code_path` is the **primary**; other configured repos are
**secondaries**. A federated search fans out to every configured
namespace, merges the ranked results, and returns them weighted toward
the primary. Federation does not merge data — each namespace keeps its
own orphan branch, its own worktree, and its own baseline graph. See
[FEDERATION.md](./FEDERATION.md).

## Daemons in one paragraph

Daemons are opt-in background workers inside the MCP server that scan
your threads on an interval and emit findings — structured
observations like "this thread has been stalled for 10 days" or "this
entry looks like a Decision candidate." Findings go to a per-daemon
log under `~/.watercooler/daemons/` and are queryable via MCP tools.
Daemons do not modify threads unless that is their explicit documented
purpose. Four daemons ship in the open-source build; premium and hosted
builds add several more. See [DAEMONS.md](./DAEMONS.md).

## Further reading

- [QUICKSTART.md](./QUICKSTART.md) — zero to first entry
- [CONFIGURATION.md](./CONFIGURATION.md) — full config reference
- [DAEMONS.md](./DAEMONS.md) — daemon catalog and finding tools
- [FEDERATION.md](./FEDERATION.md) — cross-namespace search
- [GLOSSARY.md](./GLOSSARY.md) — term definitions
