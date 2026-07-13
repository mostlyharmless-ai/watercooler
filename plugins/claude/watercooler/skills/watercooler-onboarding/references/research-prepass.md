# Research pre-pass — external references → `onboarding-biblio`

Detail reference for SKILL.md **Step 2.3** (default-on; auto-skips offline / `no-github` /
`local-only` / `offline` / `no-biblio` / dry-run). Runs **before** deep-history (Step 2.4) and
the analytical seeds (Step 4/5) so the discerned intent and history threads can be framed
against the repo's external scholarship.

**Goal.** Many repos implement or extend external work (a founding paper, a method, prior art)
that never surfaces from code/git alone. This step harvests those references from the README and
docs into a single `onboarding-biblio` thread and — when a **principal source paper** exists —
fetches it, parses its bibliography, and records the **salient secondary references** too. The
biblio is a *context substrate*, not authority: every entry is a `Note`, never a `Decision`.

This step is **skill composition** — reuse, do not reinvent:
- **`fetch-papers` skill** — reference-pattern extraction (§1) and `curl` download safety (§3).
- **`pdf-to-md` skill** — PDF→markdown via Claude's native multimodal reader, one **Task
  subagent per PDF** for context isolation (§3/§5).
- **`whitepaper_parser`** (`src/watercooler_memory/whitepaper_parser.py`) —
  `parse_whitepaper_structure()` + the References-section detector to split a parsed paper's
  bibliography (§3).

---

## §0. Gating

Run the **harvest** (§1–§2) whenever the README/docs are readable — it is cheap and offline-safe.
**Skip the network steps (§3–§4)** and record the reason when any of:
- args include `no-biblio`, `no-github`, `skip-github`, `local-only`, or `offline`;
- there is no network / no usable fetch tool (`WebSearch`/`WebFetch`/`curl`);
- dry-run (`dry-run`/`preview`/`read-only`/`orient`) — instead print the planned `onboarding-biblio`
  entries (index + per-paper) and write nothing.

Soft-gate posture: treat each fetch/parse as independently failable. A failed download or parse is
caught, recorded in the index entry, and the step continues with whatever succeeded. A pre-pass
failure never aborts onboarding.

---

## §1. Harvest external references (always)

Scan, in priority order: `README*`, `CITATION.cff` / `CITATION*`, `docs/`, `REFERENCES.md`,
`paper/` or `papers/`, `*.bib`, and positioning artifacts (`*THESIS*`, `WHITEPAPER*`, `docs/PHILOSOPHY*`).
Extract references using the fetch-papers Phase-2 patterns:

| Pattern | Example |
|---|---|
| arXiv URL / ID | `arxiv.org/abs/2404.17490`, `arXiv:2106.13898` |
| DOI | `doi.org/10.1109/...` |
| Direct PDF URL | `https://…/paper.pdf` |
| `Author (Year)` citation | `Lyon (2024)`, `Vaswani et al. (2017)` |
| Named work / venue | "Attention Is All You Need", "the CARFAC model" |

For each, capture: citation text, resolved source link (if any), and a **resolvable vs
paywalled/unresolved** classification (paywalled hosts — IEEE Xplore, ACM DL, Springer,
ScienceDirect — cannot be auto-fetched; record the link only). Note: "resolvable" here means a
link exists; whether it is *fetchable* is decided at §3.1 by the trusted-domain allowlist — a
resolvable link on a non-allowlisted domain is recorded and becomes a stub (§4/§5), never dropped.
This catalogue is the index entry (§5) and is written even when §3–§4 are skipped.

---

## §2. Identify the principal source paper(s)

Heuristic — **record as `Inferred` with a confidence label and basis**, never as fact:
- Named in `CITATION.cff` / a "cite this" / "based on" / "implements" section of the README, **or**
- the most prominently and repeatedly cited foundational source (title in the project tagline,
  repeated across README + docs), **or**
- the paper a `paper/`-style directory or the project name is derived from.

There may be **zero** (skip §3–§4; index entry only), **one**, or a small few. Do not invent one;
"no principal paper identified" is a valid, recorded outcome.

---

## §3. Fetch + parse the principal paper(s) (network-gated)

For each principal paper that is open-access:
1. **Fetch to a persistent reuse cache** — `~/.watercooler/cache/biblio/<repo-name>/` (create with
   `mkdir -p`). **Never** download into the subject repo or the threads branch. This cache is
   **persistent and reused across runs**: before fetching, if the target PDF (or its `.md`
   extraction) is already cached, **skip the download and reuse it** — so re-running onboarding
   does not re-fetch or re-parse. (Lifecycle: the cache is not auto-cleaned; it is a deliberate
   reuse store the operator can delete to force a refresh.) Use fetch-papers' curl safety verbatim:
   `curl -L --max-filesize 50000000 --max-redirs 3 -o "<cache>/<author>-<year>-<short>.pdf" "<url>"`;
   arXiv abs → `https://arxiv.org/pdf/<id>.pdf`; **only trusted domains** (arxiv.org, openreview.net,
   aclanthology.org, proceedings.mlr.press); sanitize filenames (reject `..`/absolute). A paper
   whose only link is on a **non-allowlisted domain** (and is not a known paywall host) is **not**
   fetched — record it as a **reference stub** (§4 / §5) with reason "domain not in fetch
   allowlist", never silently dropped.
2. **Extract to markdown** via the pdf-to-md pattern: spawn a **Task subagent per PDF**
   (`subagent_type="general-purpose"`) that reads the cached PDF — context-isolated so large
   papers do not accumulate in the onboarding agent's context. **Write path (load-bearing):** the
   subagent should write the entry itself (§5) via `watercooler_say`, so the markdown never
   returns to the parent. But MCP write tools are **not guaranteed** in a subagent (interactively-
   authenticated MCP servers can be absent in headless/cron runs — same caveat as
   `deep-history.md`). So instruct the subagent: *attempt* the `watercooler_say` write; **if the
   write tool is unavailable or fails, return the markdown to the parent**, which writes it
   (accepting the one-time context cost). **Never drop the paper entry.**
3. **Isolate the bibliography** — run `parse_whitepaper_structure()` (or read the extraction's
   `## References` section) to get the reference list.
4. **Rank** the bibliography (§6) and take the top **~8–12** as the high-relevance secondary set;
   record a `+N more folded` line in the index entry (no silent caps).

---

## §4. Fetch + parse the high-relevance secondaries

For each ranked secondary reference (capped per §3.4):
- If open-access **and on the §3.1 fetch allowlist**: fetch to the cache + pdf-to-md subagent
  (same as §3.1–§3.2) → a full-markdown entry (§5).
- If paywalled, unresolvable, **or on a non-allowlisted domain**: a **reference stub** entry
  (citation + link + one-line why-salient + the reason it wasn't fetched).

---

## §5. Write `onboarding-biblio`

Thread topic: `onboarding-biblio`. First write creates it (`create_if_missing=true`); then apply
the thread-level `onboarding` tag once (per SKILL.md Step 5 tag rule) plus a `biblio` tag. Every
entry title begins with `Onboarding: ` (Step 5 title rule). Honors dry-run (print, don't write).

**Index entry** — write first. Title `Onboarding: bibliography & external sources`,
`entry_type="Note"`, `role="scribe"`, `agent_func="<platform>:<model>:scribe"`. Body (strict
template — Spec/Purpose/Observed/Inferred/Provenance):
- `Observed:` the full harvested catalogue (every reference + source link), grouped
  resolvable vs paywalled; the principal-paper identification; an index of which references became
  their own entries (`entry_id` once known); the `+N more folded` count.
- `Inferred:` principal-paper choice + confidence + basis; salience-ranking basis (state it, §6).
- `overview` linkage: this entry is added to the `onboarding-overview` sibling index + reading
  order as the scholarly-context seed.

**One entry per principal paper** — Title `Onboarding: source paper — <title>`, `Note`, `scribe`.
Body = the pdf-to-md markdown extraction. The per-paper Task subagent attempts the
`watercooler_say` write itself (so the large content never returns to the onboarding agent); on
the §3.2 fallback (no MCP write tool in the subagent) it returns the markdown and the parent
writes the entry. Prepend a short `Spec: docs` + `Provenance:` (source URL, cache path,
"extracted via pdf-to-md").

**One entry per high-relevance secondary** — Title `Onboarding: reference — <title>`, `Note`,
`scribe`. Full markdown when open-access; else a stub: citation, link, and `why-salient` (which
principal paper cited it + the ranking signal). Link back to the principal paper's `entry_id`.

Enrichment (summary + embedding) on these Notes is desirable — it makes the corpus searchable via
`watercooler_search`/`smart_query`. Writes go through the committer (accepted durably, committed
eventually); no per-worker `sync_repair` self-flush.

---

## §6. Salience ranking (for the secondary set)

Rank a principal paper's bibliography by, in descending weight:
1. **Domain-relevance** — overlaps the repo's own vocabulary (subsystem names, README keywords,
   method names the code implements).
2. **Foundational/seminal signal** — surveys, "introducing X" papers, works the principal paper
   leans on most (cited repeatedly in its own text, or in its Related-Work/Methods sections).
3. **Recency** — recent advances that situate the repo's current state.
State the basis in the index entry (transparent, auditable). Ranking informs **inclusion only**,
never authority.

---

## §7. Safety, no-pollution, cost

- **No subject-repo or threads-branch file pollution.** PDFs live only in
  `~/.watercooler/cache/biblio/<repo>/`; the markdown lives as thread entries. (This departs from
  fetch-papers/pdf-to-md, which write to `docs/`/`refs/` — do not do that here.)
- Reuse fetch-papers' download caps, trusted-domain allowlist, filename sanitization, and the
  WebSearch budget (≤8 if you search at all; prefer resolving explicit links over searching).
- **Cost guard.** The deep parse (§3–§4) is **default-on**: on the first online run it downloads
  and pdf-to-md-parses up to ~8–12 papers (LLM-heavy). That cost is paid **once per repo** — the
  persistent reuse cache (§3.1) makes every subsequent run skip already-fetched/parsed papers, so
  re-running onboarding is cheap. The ~8–12 cap + the §0 network-gating bound the first-run cost.
  If one extraction is enormous, record abstract + key sections + the reference list rather than
  silently truncating — and say so in that entry.

---

## §8. Self-checks

- Every principal-paper claim is `Inferred` with confidence + basis (never stated as fact).
- The index entry's catalogue is complete (resolvable + paywalled) even when §3–§4 were skipped.
- The `+N more folded` count is present whenever the secondary set was capped.
- No PDF was written into the subject repo or the threads branch (cache only).
- In dry-run, nothing was written — only the planned entries were printed.
