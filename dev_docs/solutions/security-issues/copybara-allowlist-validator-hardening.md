---
title: "Copybara Allowlist Validator — Multi-Layer Security Hardening"
date: 2026-03-04
category: "security-issues"
tags:
  - copybara
  - sync-pipeline
  - allowlist-validation
  - static-analysis
  - parser-correctness
  - ci-workflow
  - security-gating
problem_type: "Security Control Defects in Validator Design"
components:
  - validate-copybara-allowlist.py
  - copybara-dry-run.yml
  - check-forbidden-paths.yml
  - copy.bara.sky
  - src/watercooler_mcp/tools/diagnostic.py
severity: "high"
status: resolved
related_issues:
  - "PR #290"
related_docs:
  - "dev_docs/solutions/process/automated-pr-review-multi-pass-inefficiency.md"
  - "dev_docs/solutions/logic-errors/federation-phase1-code-review-fixes.md"
---

# Copybara Allowlist Validator — Multi-Layer Security Hardening

## Problem

Designing a Copybara stable-to-public sync pipeline produced a `validate-copybara-allowlist.py`
static validator and associated CI gates. The plan document went through 6+ rounds of automated
code review (Codex and `claude-review`) before all security-relevant defects were addressed. The
bugs spanned three layers: **parser correctness**, **allowlist logic**, and **CI gate coverage**.

No production code was affected at time of discovery (plan document only), but the bugs would
have produced false PASSes or missed bypasses if implemented as written.

---

## Root Causes & Fixes

### 1. Parser: Regex truncation at first `]`

**Bug:** `re.search(r"\[(.*?)\]", ..., re.DOTALL)` stops at the **first** `]` after the
opener — including a `]` inside a Starlark `# line comment`. This silently truncates the
captured include/exclude list and produces a false PASS.

**Fix:** Replace the regex list-extractor with `_extract_bracketed()`, a bracket-depth counter
that uses a state machine to skip comment and string content:

```python
def _extract_bracketed(text: str, pos: int) -> tuple[str, int]:
    """Bracket-depth counter immune to ] inside # comments or string literals."""
    assert text[pos] == "["
    depth, in_single, in_double = 0, False, False
    i = pos
    while i < len(text):
        ch = text[i]
        if in_single:
            if ch == "'": in_single = False
        elif in_double:
            if ch == '"': in_double = False
        elif ch == "#":
            while i < len(text) and text[i] != "\n": i += 1  # skip comment
        elif ch == "'": in_single = True
        elif ch == '"': in_double = True
        elif ch == "[": depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0: return text[pos + 1 : i], i
        i += 1
    raise ValueError("Unbalanced '[' in copy.bara.sky")
```

**Rule:** Any time you count brackets in a DSL, you must model the full lexical grammar
(comments, string literals). State machines > regexes for structured text.

---

### 2. Allowlist: Non-`/**` wildcards bypass all checks

**Bug:** `FORBIDDEN_BROAD` only blocks literal full-repo globs (`**`, `*`, etc.). Patterns
like `.github/workflows/*` or `*.md` contain `*` but aren't in `FORBIDDEN_BROAD` and don't
end in `/**` so they also skip `PERMITTED_SUBTREES`. They pass the validator silently.

**Fix:** Add `PERMITTED_GLOBS` (initially empty) and a guard in `check()`:

```python
PERMITTED_GLOBS: set[str] = set()  # add here after explicit security review

if "*" in p and not p.endswith("/**") and p not in FORBIDDEN_BROAD and p not in PERMITTED_GLOBS:
    print(f"ERROR: Non-subtree wildcard '{p}' not permitted — "
          "allowlist only accepts exact paths or approved /** subtrees")
    ok = False
```

**Rule:** When writing an allowlist validator, enumerate **all wildcard forms** the DSL
supports and ensure each form has an explicit check. Default-deny means all pattern categories
need a gate.

---

### 3. Allowlist: `include_could_reach()` first-component false positives

**Bug:** The original function compared only the first path component:
```python
base = exclude_prefix.split("/")[0] + "/"
return include_pattern.startswith(base)
```
`tests/integration/**` and `tests/benchmarks/**` share `tests/` so the function incorrectly
returns `True` — producing spurious "required exclude missing" errors when the allowlist is
tightened to subdirectory patterns.

**Fix:** Ancestor/descendant logic on full directory paths:

```python
def include_could_reach(include_pattern: str, exclude_prefix: str) -> bool:
    if include_pattern in ("**", "*"):
        return True
    covered = include_pattern[:-3] if include_pattern.endswith("/**") else include_pattern
    excluded = exclude_prefix[:-3] if exclude_prefix.endswith("/**") else exclude_prefix
    return (
        covered == excluded
        or excluded.startswith(covered + "/")   # include broader: tests/** → tests/benchmarks
        or covered.startswith(excluded + "/")   # include narrower but inside excluded subtree
    )
```

**Rule:** Path containment is a partial-order relationship. Use component-boundary string
prefix matching (`startswith(x + "/")`, not just `startswith(x)`), or you'll match
`/foobar` for prefix `/foo`.

---

### 4. Allowlist: `REQUIRED_EXCLUDES` presence-only check

**Bug:** The check only verified that required excludes existed in the exclude list. If the
include list was later narrowed (e.g., `tests/**` → `tests/unit/**`), required excludes
for `tests/fixtures/threads/**` become redundant. A future maintainer removing the "stale"
exclude and then broadening includes again creates an unguarded window.

**Fix:** Add a forward-reachability warning:

```python
for required in REQUIRED_EXCLUDES:
    reachable = any(include_could_reach(inc, required) for inc in includes)
    if required not in exclude_set:
        if reachable:
            print(f"ERROR: '{required}' missing and include list exposes this path")
            ok = False
        else:
            print(f"WARNING: '{required}' missing but currently unreachable")
    else:
        if not reachable:
            print(f"WARNING: '{required}' present but no include reaches it — "
                  "exclude is redundant, or include list is too narrow")
```

**Rule:** Presence checks enforce "the config says so." Reachability checks enforce the actual
security invariant. Both matter for long-lived configs that get maintained.

---

### 5. Allowlist: Sentinel grep bare-substring + unanchored

**Sequence of bugs in the same field:**
1. First version: `grep -q "watercooler-cloud" pyproject.toml` — `"watercooler"` is a prefix
   of `"watercooler-cloud"`, so the sentinel passes in any dir whose `pyproject.toml` mentions
   the word.
2. Fix attempt: `grep -q 'name = "watercooler"'` — still matches a comment like
   `# name = "watercooler" is the public package name`.

**Correct fix:** `grep -q '^name = "watercooler"'`

The `^` anchor binds to the actual TOML key line. A comment containing the same string
starts with `#`, not the field name.

**Rule:** When a guard grep relies on a config file format, anchor to the format's structural
markers (TOML field assignment starts at column 0; YAML keys can be indented; JSON has no
comments). Bare substring matching is almost always wrong for structured formats.

---

### 6. Transform: Truncated multi-line `before` string

**Bug:** The `core.replace` transform for `release.yml` stripped 2 of 3 comment lines:
```python
before = "  # Note: No test job - code is already validated on staging before tagging.\n"
         "  # Running tests here would be redundant. The staging branch CI ensures\n"
# missing: "  # all tests pass before code reaches stable.\n"
```
The orphaned third line appeared verbatim in the public repo, disclosing the internal
staging → stable → tag release flow.

**Fix:** Verify the actual file and extend `before` to cover all lines:
```python
before = "  # Note: No test job - code is already validated on staging before tagging.\n"
         "  # Running tests here would be redundant. The staging branch CI ensures\n"
         "  # all tests pass before code reaches stable.\n"
```

**Rule:** Always verify `core.replace` `before` strings against the actual target file.
Multi-line comments are the most common source of partial matches — count lines explicitly.

---

### 7. CI gate: Direct push bypasses PR-only trigger

**Bug:** `copybara-dry-run.yml` only triggered on `pull_request` events. The publish
workflow fires on every `push` to `stable`. A direct push (no PR) triggers publish
without the validation gate ever running.

**Fix (belt-and-suspenders):** Add `push: branches: [stable]` trigger to the dry-run
workflow. Gate the pyproject diff step with `if: github.event_name == 'pull_request'`
since `github.base_ref` is empty on push events.

**Fix (structural):** Require branch protection on private `stable`:
- Require PR before merging
- `validate-allowlist` as required status check

**Rule:** For security-critical CI gates, enumerate every code-mutation event that can
affect the guarded resource. `pull_request` alone misses direct pushes, force-pushes, and
automated merges. Branch protection is the only mechanism that *blocks* rather than
*observes*.

---

### 8. Destination guard: Incomplete FORBIDDEN_PATHS coverage

**Bug:** `check-forbidden-paths.yml` used `find . -name "$name"` (basename-only) for
`.cli-threads` and `test_artifacts`, but the `REQUIRED_EXCLUDES` list in the private repo
also protects `tests/benchmarks`, `tests/fixtures/threads`, `tests/tmp_threads`, and
`src/watercooler_mcp/scripts`. These paths at their known repo-relative locations were
unchecked.

**Fix:** Add a `FORBIDDEN_PATHS` array checked by `[ -e "$fp" ]`:

```bash
FORBIDDEN_PATHS=(
    "tests/benchmarks"
    "tests/fixtures/threads"
    "tests/tmp_threads"
    "src/watercooler_mcp/scripts"
)
for fp in "${FORBIDDEN_PATHS[@]}"; do
    if [ -e "$fp" ]; then
        echo "ERROR: Forbidden repo-relative path found: $fp"
        FOUND=1
    fi
done
```

**Rule:** Defense-in-depth post-push guards should mirror the private-side `REQUIRED_EXCLUDES`
exactly. Any divergence means the public-side detection layer doesn't cover what the private
prevention layer was designed to protect.

---

### 9. Source file: Private URL with wrong format and wrong repo

**Bug:** `diagnostic.py:106` contained `https://github.com/MostlyHarmless-AI/watercooler-cloud/docs/SETUP.md` — two problems:
1. Mixed-case `MostlyHarmless-AI` org name bypasses the lowercase URL transform
2. Missing `/blob/main/` path segment (GitHub 404s without it)

**Fix:** Fix at source with the correct canonical URL:
```python
instructions.append(
    "  For full setup guide: "
    "https://github.com/mostlyharmless-ai/watercooler/blob/main/docs/SETUP.md"
)
```

Add a case-insensitive grep to the dry-run CI to catch future occurrences:
```bash
if grep -ri "github\.com[^\"']*watercooler-cloud" src/; then
    echo "ERROR: Private repo URL found in src/**"
    exit 1
fi
```

**Rule:** Add a CI grep for any string that transforms in `copy.bara.sky` don't cover
(transforms have path filters; `src/**` is commonly excluded from URL rewrites).
Case-insensitive (`-i`) catches authoring inconsistencies.

---

## Prevention Checklist

For future security-adjacent validators:

**Parser**
- [ ] Model the full grammar (comments, string literals) before writing parsers
- [ ] Never use non-greedy `.*?` regexes for structured text — truncation is silent
- [ ] Use bracket-depth counting for balanced-delimiter extraction; add comment-skip

**Allowlist logic**
- [ ] Enumerate all wildcard forms the DSL supports; each needs an explicit gate
- [ ] Use component-boundary prefix matching: `startswith(x + "/")`, not `startswith(x)`
- [ ] Presence checks + reachability checks together enforce the security invariant

**Transforms**
- [ ] Verify `before` strings against the actual target file before committing
- [ ] Count expected vs. actual matching lines for multi-line replacements

**CI gates**
- [ ] Gate on `push` AND `pull_request` for security-critical validators
- [ ] Apply branch protection (required status checks) on promoted branches
- [ ] Post-push destination guards must mirror private-side `REQUIRED_EXCLUDES` exactly

**URLs / string transforms**
- [ ] Add `grep -ri` CI steps for private strings that transforms don't cover
- [ ] Canonical lowercase org/repo names in source to avoid case-bypass

---

## Related

- [`dev_docs/solutions/process/automated-pr-review-multi-pass-inefficiency.md`](../process/automated-pr-review-multi-pass-inefficiency.md) — multi-round review inefficiency pattern (6+ rounds on this PR mirrors 8+ on PR #255)
- [`dev_docs/solutions/logic-errors/federation-phase1-code-review-fixes.md`](../logic-errors/federation-phase1-code-review-fixes.md) — similar iterative hardening (14 review rounds on PR #190)
- [`dev_docs/solutions/process/pr-branch-discipline-push-hygiene.md`](../process/pr-branch-discipline-push-hygiene.md) — batch-commit strategy to reduce review round cost
