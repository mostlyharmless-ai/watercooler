#!/usr/bin/env python3
"""Validates copy.bara.sky's origin_files against a forbidden list.

Parses the allowlist patterns from copy.bara.sky and fails if any
forbidden path or prefix is explicitly included.

Usage: python3 scripts/validate-copybara-allowlist.py
"""
import re
import sys

# Patterns that match the entire repo — never safe in an allowlist.
FORBIDDEN_BROAD = {"**", "*", ".", "./", "./**", "*/**"}

# The ONLY subtree globs (ending /**) approved for the include list.
# Any new "something/**" pattern must be added here after explicit security review.
# This prevents accidentally broad expansions (e.g. "scripts/**", ".github/workflows/**")
# from slipping past the validator even when they are not in FORBIDDEN_PREFIXES.
PERMITTED_SUBTREES = {
    "src/**",
    "tests/**",
    "docs/**",
    "schemas/**",
    # Tier A public skills — explicitly approved for sync.
    # .claude/skills/ is otherwise blocked by FORBIDDEN_PREFIXES (.claude/).
    ".claude/skills/recall/**",
    ".claude/skills/search-threads/**",
    ".claude/skills/threads/**",
    ".claude/skills/find-related/**",
    ".claude/skills/watercooler-health/**",
}

FORBIDDEN_EXACT = {
    "AGENTS.md", "CLAUDE.md", "Dockerfile", "nixpacks.toml",
    "Procfile", "railway.json", ".gitmodules", "uv.lock",
    "copy.bara.sky", "pyproject.toml",  # raw; sanitized version is pyproject.public.toml
}

FORBIDDEN_PREFIXES = [
    "dev_docs/", "todos/", "watercooler/", "external/",
    ".claude/", ".serena/", ".githooks/",
    "scripts/build_memory_graph", "scripts/index_", "scripts/migrate_",
    "scripts/check-mcp-servers", "scripts/cleanup-mcp-servers",
    "tests/benchmarks/", "tests/integration/.cli-threads/",
]

# The ONLY scripts/ exact paths approved for the include list.
# Any other scripts/xxx exact path would pass FORBIDDEN_PREFIXES (which only catches
# specific name prefixes, not arbitrary operator scripts like enrich_baseline_graph.py).
# A future maintainer adding an internal script as a "convenience" would pass unchallenged
# without this check.
PERMITTED_EXACT_SCRIPTS = {
    "scripts/install-mcp.sh",
    "scripts/install-mcp.ps1",
    "scripts/mcp-server-daemon.sh",
    "scripts/git-credential-watercooler",
}

# The ONLY non-/** wildcard patterns (containing * but not ending /**) that are permitted.
# Currently empty: the allowlist uses only exact paths or PERMITTED_SUBTREES /** globs.
# Patterns like ".github/workflows/*" or "*.md" pass FORBIDDEN_BROAD (which only blocks
# literal full-repo globs) and skip PERMITTED_SUBTREES (which only catches /**), but
# they still expose broad sets of files without explicit per-file review.
# Any such pattern requires explicit addition here after security review.
PERMITTED_GLOBS: set[str] = set()

# These patterns MUST be present in the exclude block — missing any is a hard failure.
# NOTE: This check verifies presence, not reachability. If the include list is restructured
# (e.g., tests/** split into subdirectory patterns), update REQUIRED_EXCLUDES accordingly.
# The security invariant is: these paths must never appear in the public repo.
REQUIRED_EXCLUDES = {
    "tests/benchmarks/**",
    "tests/integration/.cli-threads/**",
    "tests/.cli-threads/**",
    "tests/test_artifacts/**",
    "tests/fixtures/threads/**",
    "tests/tmp_threads/**",
    "src/watercooler_mcp/scripts/**",
    # Private memory backends (graphiti, leanrag implementations).
    # Excluded until the memory backends are ready for open-source release.
    "src/watercooler_memory/**",
}


def _extract_bracketed(text: str, pos: int) -> tuple[str, int]:
    """Return (content, end_pos) of balanced [...] starting at text[pos].

    Skips Starlark line comments (# to end-of-line) and respects string
    literals so a ] inside either does not prematurely decrement bracket depth.

    Without comment-awareness a ] in a comment such as:
        # old pattern: "tests/fixtures/**"]   <- stray ]
    would close the list early, silently truncate the captured patterns, and
    produce a false PASS.

    State machine:
      - inside '...': only ' exits; # and [ ] are inert.
      - inside "...": only " exits; # and [ ] are inert.
      - outside strings: # skips to end-of-line; [ increments depth; ] decrements.
    """
    assert text[pos] == "[", f"Expected '[' at pos {pos}, got {text[pos]!r}"
    depth = 0
    in_single = False   # inside '...'
    in_double = False   # inside "..."
    i = pos
    while i < len(text):
        ch = text[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "#":
            # Line comment — skip to newline; brackets inside don't count.
            while i < len(text) and text[i] != "\n":
                i += 1
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[pos + 1 : i], i
        i += 1
    raise ValueError("Unbalanced '[' in copy.bara.sky; cannot parse origin_files")


def parse_origin_files(sky_content: str) -> tuple[list[str], list[str]]:
    """Extract include and exclude pattern lists from the origin_files glob() call.

    Uses _extract_bracketed (bracket-depth counting) instead of a regex-based list
    extractor to avoid truncation at a stray ] inside a Starlark comment.

    Returns:
        Tuple of (include_patterns, exclude_patterns).
    """
    m = re.search(r"origin_files\s*=\s*glob\s*\(", sky_content)
    if not m:
        print("ERROR: Could not find 'origin_files = glob(' in copy.bara.sky", file=sys.stderr)
        sys.exit(1)
    # Find the '[' that opens the include list immediately after 'glob('
    inc_bracket = sky_content.index("[", m.end())
    include_content, inc_end = _extract_bracketed(sky_content, inc_bracket)
    # Look for exclude=[...] after the include list
    exc_m = re.search(r"exclude\s*=\s*\[", sky_content[inc_end:])
    if not exc_m:
        # No exclude block — include-only
        includes = re.findall(r"""["']([^"']+)["']""", include_content)
        return includes, []
    # exc_m.end() points past the '['; subtract 1 to get the '[' position in the slice,
    # then add inc_end to convert to an index in the full string.
    exc_bracket = inc_end + exc_m.end() - 1
    exclude_content, _ = _extract_bracketed(sky_content, exc_bracket)
    # Match both double-quoted ("pattern") and single-quoted ('pattern') Starlark strings.
    # Starlark allows both forms; matching only double quotes would silently miss single-quoted
    # patterns and produce a false PASS.
    includes = re.findall(r"""["']([^"']+)["']""", include_content)
    excludes = re.findall(r"""["']([^"']+)["']""", exclude_content)
    return includes, excludes


def include_could_reach(include_pattern: str, exclude_prefix: str) -> bool:
    """True if include_pattern could expose files under exclude_prefix.

    Uses ancestor/descendant matching on directory components, not just the first
    path component. The previous implementation — base = exclude_prefix.split("/")[0]
    — returned True for include="tests/integration/**" vs exclude="tests/benchmarks/**"
    because both share "tests/" as the first component. That is incorrect: a
    tests/integration/** include cannot expose tests/benchmarks/.

    Rules (after stripping trailing /** from /** patterns):
      - "**" or "*" reaches everything.
      - covered_dir reaches exclude_dir when:
          * they are equal (exact subtree match), OR
          * exclude_dir is a descendant of covered_dir (include is broader), OR
          * covered_dir is a descendant of exclude_dir (include is narrower but inside
            the excluded subtree — e.g. include="tests/benchmarks/slow/**" would
            still trigger the "tests/benchmarks/**" exclude).

    Used to distinguish "required exclude is genuinely needed" from
    "required exclude is now redundant because no include covers it".
    """
    if include_pattern in ("**", "*"):
        return True
    covered = include_pattern[:-3] if include_pattern.endswith("/**") else include_pattern
    excluded = exclude_prefix[:-3] if exclude_prefix.endswith("/**") else exclude_prefix
    return (
        covered == excluded
        or excluded.startswith(covered + "/")   # include is broader: tests/** → tests/benchmarks
        or covered.startswith(excluded + "/")   # include is narrower but inside: tests/benchmarks/slow/**
    )


def check(includes: list[str], excludes: list[str]) -> bool:
    ok = True
    for p in includes:
        if p in FORBIDDEN_BROAD:
            print(f"ERROR: Overly broad pattern '{p}' in include list — would sync entire repo")
            ok = False
        # Block non-/** wildcards not in PERMITTED_GLOBS.
        # FORBIDDEN_BROAD only catches literal full-repo globs (**, *, etc.).
        # A pattern like ".github/workflows/*" or "*.md" contains * but passes FORBIDDEN_BROAD
        # and is not caught by PERMITTED_SUBTREES (only /**). Such patterns expose broad sets
        # of files without per-file review. Reject unless explicitly added to PERMITTED_GLOBS.
        if "*" in p and not p.endswith("/**") and p not in FORBIDDEN_BROAD and p not in PERMITTED_GLOBS:
            print(f"ERROR: Non-subtree wildcard '{p}' is not permitted — "
                  "allowlist only accepts exact paths or approved /** subtrees; "
                  "add to PERMITTED_GLOBS after security review if intentional")
            ok = False
        if p.endswith("/**") and p not in PERMITTED_SUBTREES:
            print(f"ERROR: Subtree glob '{p}' is not in PERMITTED_SUBTREES — add after security review")
            ok = False
        for forbidden in FORBIDDEN_EXACT:
            if p == forbidden:
                print(f"ERROR: Forbidden exact path '{p}' in include list")
                ok = False
        for prefix in FORBIDDEN_PREFIXES:
            if p == prefix or p.startswith(prefix) or p == prefix.rstrip("/"):
                if p in PERMITTED_SUBTREES:
                    # Explicitly whitelisted in PERMITTED_SUBTREES — skip.
                    continue
                print(f"ERROR: Forbidden prefix '{p}' matches forbidden '{prefix}'")
                ok = False
        # Enforce that only the 4 approved scripts are allowed as exact scripts/ paths.
        # FORBIDDEN_PREFIXES only covers specific name prefixes — an arbitrary operator script
        # added as an exact path (e.g. "scripts/enrich_baseline_graph.py") would slip through.
        if p.startswith("scripts/") and not p.endswith("/**"):
            if p not in PERMITTED_EXACT_SCRIPTS:
                print(f"ERROR: Unpermitted script path '{p}' — add to PERMITTED_EXACT_SCRIPTS after security review")
                ok = False
    exclude_set = set(excludes)
    for required in REQUIRED_EXCLUDES:
        reachable = any(include_could_reach(inc, required) for inc in includes)
        if required not in exclude_set:
            if reachable:
                print(f"ERROR: Required exclusion '{required}' is missing and include list exposes this path")
                ok = False
            else:
                print(f"WARNING: Required exclusion '{required}' is missing but no include pattern reaches it (currently redundant)")
        else:
            # Exclude is present — also verify the include list actually reaches this path.
            # If no include covers it, the exclude is currently redundant. This may be fine,
            # but it can mask a narrowed include list: a maintainer who later removes the
            # exclude (seeing it as unnecessary) would expose the path if includes are
            # subsequently broadened again. Warn so maintainers can verify intent.
            if not reachable:
                print(f"WARNING: Required exclusion '{required}' is present but no current "
                      "include reaches it — exclude is redundant, or the include list is too narrow")
    return ok


if __name__ == "__main__":
    with open("copy.bara.sky") as f:
        content = f.read()
    includes, excludes = parse_origin_files(content)
    print(f"Checking {len(includes)} include patterns, {len(excludes)} exclude patterns ...")
    if check(includes, excludes):
        print("Allowlist validation passed.")
    else:
        print("Allowlist validation FAILED.")
        sys.exit(1)
