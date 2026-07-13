# GitHub history layer — implementation detail

Load this reference when executing **Step 2.2** of `SKILL.md`. It defines the source-repo resolver, the `gh` command set, the per-category fallback rules, and the body template for the conditional `recent-activity` seed thread.

---

## When to load this reference

Load only when Step 2.2 of `SKILL.md` is being executed and not skipped. The skip conditions in `SKILL.md` (skill arguments `no-github`/`skip-github`/`local-only`/`offline`, missing `gh`, no resolvable GitHub source repo) take precedence and short-circuit before this reference is needed.

Treat every `gh` invocation as independently failable. Capture stderr, record per-query outcomes (`landed` / `skipped` / `failed: <reason>` / `empty`), and continue.

---

## Source-repo resolver

Build an ordered list of GitHub source repositories ONCE at the top of Step 2.2. The list determines which repo each subsequent `gh` query targets via `--repo <owner>/<repo>`. Order matters: the active remote stays first; non-active sources are consulted only when an earlier source returned no signal for that category.

### Resolver inputs (in order of trust)

1. **Active remote.** Parse `git remote get-url origin`. If it points to GitHub (`github.com:<owner>/<repo>` or `https://github.com/<owner>/<repo>(.git)?`), include `<owner>/<repo>` as the first entry.

2. **GitHub-tracked parent.** Run `gh repo view --json nameWithOwner,parent,url` against the active remote. If `parent.nameWithOwner` is non-null, include it as the second entry. Note that `parent` is null for repos that are not modelled as GitHub forks (mirrors, manually-cloned forks, renamed remotes) — do not assume parent absence means there is no upstream.

3. **High-confidence canonical repo from local metadata.** Inspect package / build manifests for explicit GitHub URLs that point to a different `<owner>/<repo>` than the active remote:
   - `package.json`: `repository.url`, `bugs.url`, `homepage` fields.
   - `pyproject.toml`: `[project.urls]` keys (`Homepage`, `Repository`, `Source`, `Bug Tracker`, etc.).
   - `Cargo.toml`: `package.repository`, `package.homepage`.
   - `setup.cfg` / `setup.py`: `url`, `project_urls`.
   - `.mcpb` / `manifest.json` / `server.json`: any `repository.url` or `homepage` field.

   Treat a metadata URL as "high-confidence canonical" when it appears in two or more of the fields above and points to a public GitHub repo. Add the resolved `<owner>/<repo>` as a subsequent entry in the source list.

### Resolver output

After deduping and preserving order, the source list is one of:

- `[active]` (single entry — typical non-fork case),
- `[active, parent]` (GitHub-tracked fork),
- `[active, canonical]` (mirror or manual fork; no GitHub-tracked parent),
- `[active, parent, canonical]` (rare).

Cache the list for the rest of Step 2.2 — do not recompute per category.

If the resolver produces an empty list (no GitHub source can be inferred), Step 2.2 short-circuits per the skip conditions in `SKILL.md`. Do not run any `gh` queries.

---

## PR fetch (Decision 1)

Goal: gather rationale for up to 20 unique recent PRs. Discover candidates first (cheap, no body), dedupe and cap at 20, THEN fetch body excerpts for the selected PRs only. Without the discovery-then-body split, the budget is aspirational rather than enforced.

### Step 1 — collect commit SHAs

```bash
git log --format=%H -20
```

Use full commit SHAs. Abbreviated SHAs from `git log --oneline` cause `gh pr list --search` to over-match.

### Step 2 — discover commit-associated PRs (active source first)

For each SHA, against the FIRST source in the resolver list:

```bash
gh pr list --repo <source> --search "<full-sha>" --state merged --limit 1 \
  --json number,title,mergedAt,url,labels,author
```

Collect results into a candidate set keyed by `(source, number)`. Drop bodies — they're fetched in Step 6.

If the active source yields zero PRs across all 20 SHAs, retry the same SHA loop against each remaining source in order until one source produces signal. Stop at the first source that yields ≥1 PR; do NOT continue to later sources after success (per-category fallback semantics).

### Step 3 — validate candidate `(#NNNN)` refs

Parse `(#NNNN)` patterns from commit subjects (`git log --oneline -20`). For each candidate ref, validate against the first source that produced PR signal in Step 2:

```bash
gh pr view <num> --repo <selected-source> \
  --json number,title,mergedAt,url,labels,author
```

Refs that fail validation against the selected source AND succeed against another source in the resolver list are valid (record the resolved source). Refs that fail against every source are recorded as **unresolved** for the thread body — do not treat them as missing rationale.

If Step 2 produced no signal in any source, run Step 3 across the resolver list until one source resolves at least one candidate; that becomes the selected source.

### Step 4 — fill remaining budget with merged-PR fallback

If Steps 2-3 yielded fewer than 5 unique PRs from the selected source, fill the remainder against the same selected source:

```bash
gh pr list --repo <selected-source> --state merged --limit <remaining> \
  --json number,title,mergedAt,url,labels,author
```

`<remaining>` is `20 - len(unique candidates so far)`. Do not exceed 20 total.

If no source has produced signal yet, run the merged-PR fallback across the resolver list in order; the first source returning ≥1 result becomes the selected source.

### Step 5 — dedupe and cap

Dedupe the union of Steps 2-4 by `(source, number)`. Then cap the deduped list at 20 entries. Selection priority: commit-associated > validated candidate refs > merged-PR fallback. Within a tier, prefer more recent `mergedAt`.

### Step 6 — fetch bodies for the capped set

Only now fetch full body content for the ≤20 selected PRs:

```bash
gh pr view <num> --repo <source> \
  --json number,title,body,labels,mergedAt,author,url
```

Excerpt each body to its first paragraph or ~200 chars (whichever is shorter), as required by the body-inclusion policy. Do NOT store the full body in the thread; the URL is the lookup path.

---

## Closed-issue fetch (Decision 2)

Goal: top ~15 issues closed in the last 14 days from the selected source repo, with first-paragraph excerpts.

### Step 1 — compute portable date

Compute `YYYY-MM-DD` for "14 days ago" without using `date -d` (GNU-only). Acceptable approaches in order of preference:

1. Use the run's known current date from session/environment context to derive the cutoff explicitly. The skill's invocation context already carries today's date; subtract 14 days arithmetically.
2. If a portable shell computation is available, use Python: `python3 -c "from datetime import date,timedelta;print(date.today()-timedelta(days=14))"`.
3. As a last resort, run `date -v -14d +%Y-%m-%d` (BSD/macOS) or `date -d '14 days ago' +%Y-%m-%d` (GNU) — but only after detecting which is supported. Do not hard-code either.

Record the computed cutoff in the `recent-activity` thread's Provenance section so future readers can interpret the window.

### Step 2 — query against active source first

```bash
gh issue list --state closed --repo <active-source> \
  --search "closed:>=YYYY-MM-DD sort:updated-desc" --limit 15 \
  --json number,title,labels,closedAt,body,url
```

If the active source returns zero issues AND the resolver list has additional sources, retry against each remaining source in order until one returns issue signal or the list is exhausted. Items sourced from a non-active repo are labelled `[github/<owner>/<repo>]` in the thread body.

### Step 3 — excerpt bodies

For each returned issue, take the first paragraph or ~200 chars of `body` for the thread. The full body is never stored in the thread; the URL is the lookup path.

---

## Release fetch (Decision 3)

Goal: the latest GitHub Release for the selected source repo, with a body excerpt.

```bash
gh release view --repo <active-source> \
  --json tagName,publishedAt,body,url
```

If the active source has no releases, retry against each remaining source in order until one returns a release or the list is exhausted. Label non-active releases as `[github/<owner>/<repo>]`.

If no source has any release, record the category as **empty** (not failed). Do not invent a release.

Excerpt the body to its first paragraph or ~200 chars.

---

## Per-category fallback summary

Each of PRs, releases, and closed issues independently runs the source-fallback chain. A fork that has its own PRs but no releases will use its own data for PRs and the parent / canonical for releases. Active divergent forks therefore retain primacy of their own activity per category.

Record per-category outcomes in the thread body and the Step 6 final-response report:

- `landed: <count> from <source>` — at least one item came from this source.
- `skipped: <reason>` — a soft-gate failure (auth, network, rate limit).
- `failed: <reason>` — non-recoverable error.
- `empty across [<sources>]` — every source returned zero items in the relevant window.

---

## `recent-activity` thread body template

Write the `recent-activity` Watercooler entry per the strict template in `SKILL.md` Step 4 (Spec / Purpose / Observed / Inferred / Drift findings (omit) / Next query / Provenance). The Drift findings section is omitted for `recent-activity`. The body content for the Observed section is the GitHub layer's findings, formatted as below.

```
Spec: docs

Purpose: snapshot of recent GitHub activity to give future contributors the rationale and shipping context that local commit history alone does not carry.

Observed:

- Latest release: <tag> on <publishedAt> [`<source>`] — <first-paragraph excerpt, ~200 chars> [`<url>`]

- Recent PRs:
  - #<num> <title> [`<source>`] — <excerpt, ~200 chars> [`<url>`]
  - #<num> <title> [`<source>`] — <excerpt, ~200 chars> [`<url>`]
  - ...

- Unresolved candidate refs (if any): #<num>, #<num>  — could not be validated against [<sources tried>].

- Recently closed issues (closed since <YYYY-MM-DD>):
  - #<num> <title> · labels: <labels> · closed <closedAt> [`<source>`] — <excerpt, ~200 chars> [`<url>`]
  - ...

- GitHub layer status:
  - Source list: <ordered list of resolved sources>
  - PRs: <landed N from source / skipped reason / failed reason / empty>
  - Release: <landed from source / skipped / failed / empty>
  - Closed issues: <landed N from source / skipped / failed / empty>
  - Cutoff date for closed issues: <YYYY-MM-DD>

Inferred:

- <claim about active workstream, only when supported by ≥2 PR/issue/release excerpts> — confidence: medium — basis: <which surfaces>

Next query: `watercooler_search(query="recent activity", thread_topic="recent-activity", code_path=".")`

Provenance:

- Source repos consulted: <ordered list>
- gh commands run: <bullet list of the gh invocations>
- Cutoff date computation: <method used, e.g. "session-context arithmetic">
- Items by source: [active] N, [github/<owner>/<repo>] M
```

Keep excerpts terse. The thread is queryable; long entries hurt that. Future readers fetch full bodies via the URLs when needed.

---

## Skip flags

The skill arguments `no-github`, `skip-github`, `local-only`, `offline` each cause Step 2.2 to short-circuit before the resolver runs. Do not introduce additional aliases unless the brainstorm doc is updated. Do not advise users to set `GH_TOKEN=""` as a skip mechanism — `GH_TOKEN` precedence is real but empty-value handling is shell- and client-dependent, and stored credentials may persist regardless.

---

## Failure mode summary

- Resolver produces empty source list → skip Step 2.2; report "GitHub layer unavailable: no GitHub source repo resolvable."
- All `gh` queries fail → do not write `recent-activity`; report "GitHub layer unavailable: gh queries failed (auth / network / rate limit)."
- All categories return empty across all sources → do not write `recent-activity`; report "GitHub reachable but no recent activity surfaced."
- At least one category returns non-empty in any source → write `recent-activity` with that data and explicit category-by-category status.
