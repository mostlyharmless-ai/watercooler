#!/usr/bin/env python3
"""
Fetches eligible GitHub issues for parallel-sprint and builds the active-PR map.

Two modes:

  Default mode (issue fetch):
    python3 fetch_issues.py [--limit 200] [--label <label>]
        [--pr-map .sprint/tmp/ps_pr_map.json]

    Fetches open, unassigned issues with no active PR and no 'in-progress' label.
    Outputs JSON array to stdout.  Write to .sprint/tmp/ps_issues.json.

  PR-map mode:
    python3 fetch_issues.py --build-pr-map
        --graphql-input .sprint/tmp/ps_pr_map_raw.json
        --pr-map-output .sprint/tmp/ps_pr_map.json

    Transforms raw GraphQL closingIssuesReferences output into a flat
    {issue_number: pr_number} map and writes it to --pr-map-output.

Output fields (default mode):
    number, title, body (truncated to 2000 chars), labels, assignees,
    comment_count, url, milestone, created_at, updated_at,
    blocked_by_refs, blocks_refs, fixes_refs, flagged_injection
"""

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

# Shared dependency patterns — imported from _patterns.py to stay in sync with
# analyze_relationships.py. Each list has one capturing group per pattern.
# See _patterns.py for the rationale against combined alternation regexes.
from _patterns import BLOCKED_BY_PATTERNS, BLOCKS_PATTERNS, FIXES_CLOSES_PATTERNS

# ---------------------------------------------------------------------------
# Prompt injection markers — flag for LLM awareness
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS = re.compile(
  r"SYSTEM\s*:"
  r"|\[INST\]"
  r"|ignore\s+previous"
  r"|override\s+(?:scoring|instructions|rules)"
  r"|<!--\s*AI"
  r"|</?\s*ISSUE_DATA",
  re.IGNORECASE,
)

INPROGRESS_LABEL = "in-progress"


def _extract_dep_refs(text: str) -> dict[str, list[int]]:
  """Extract issue references from all dependency patterns.

  Each pattern list is iterated independently — adding, removing, or reordering
  patterns does not affect extraction logic (no positional group indexing).
  """
  def _extract(patterns: list[str]) -> list[int]:
    refs: set[int] = set()
    for pattern in patterns:
      for m in re.finditer(pattern, text, re.IGNORECASE):
        refs.add(int(m.group(1)))
    return sorted(refs)

  return {
    "blocked_by_refs": _extract(BLOCKED_BY_PATTERNS),
    "blocks_refs": _extract(BLOCKS_PATTERNS),
    "fixes_refs": _extract(FIXES_CLOSES_PATTERNS),
  }


def _scan_injection(text: str) -> bool:
  return bool(_INJECTION_PATTERNS.search(text))


def fetch_issues(
  limit: int = 200,
  label: str | None = None,
  pr_map_path: Path | None = None,
) -> tuple[list[dict], int]:
  """Fetch open, unassigned issues eligible for parallel-sprint.

  Returns:
      Tuple of (normalized_issues, raw_count) where raw_count is the number
      of issues returned by gh before any filtering.  Use raw_count (not
      len(normalized_issues)) to detect when the fetch limit was hit.
  """
  cmd = [
    "gh", "issue", "list",
    "--state", "open",
    "--limit", str(limit),
    "--search", "no:assignee",
    "--json", (
      "number,title,body,labels,assignees,comments,"
      "url,milestone,createdAt,updatedAt"
    ),
  ]
  if label:
    cmd += ["--label", label]

  result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
  raw_issues = json.loads(result.stdout)
  raw_count = len(raw_issues)

  # Load active-PR map if provided (built from GraphQL closingIssuesReferences)
  pr_issue_set: set[int] = set()
  if pr_map_path and pr_map_path.exists():
    pr_map = json.loads(pr_map_path.read_text())
    pr_issue_set = {int(k) for k in pr_map}

  normalized = []
  for issue in raw_issues:
    labels = [lbl["name"] for lbl in issue.get("labels", [])]

    # Skip issues with an active PR or the in-progress label
    if issue["number"] in pr_issue_set:
      continue
    if INPROGRESS_LABEL in labels:
      continue

    # Skip assigned issues (belt-and-suspenders — gh --search no:assignee is primary)
    if issue.get("assignees"):
      continue

    full_body = issue.get("body") or ""
    dep_refs = _extract_dep_refs(full_body)
    flagged = _scan_injection(full_body) or _scan_injection(issue.get("title", ""))

    normalized.append({
      "number": issue["number"],
      "title": issue["title"],
      "body": html.escape(full_body[:2000]),  # pre-escaped for safe embedding in XML-like delimiters
      "labels": labels,
      "assignees": [a["login"] for a in issue.get("assignees", [])],
      "comment_count": len(issue.get("comments", [])),
      "url": issue["url"],
      "milestone": (
        issue["milestone"]["title"] if issue.get("milestone") else None
      ),
      "created_at": issue.get("createdAt"),
      "updated_at": issue.get("updatedAt"),
      **dep_refs,
      "flagged_injection": flagged,
    })

  return normalized, raw_count


def build_pr_map(graphql_input: Path, pr_map_output: Path) -> None:
  """
  Transform raw GraphQL closingIssuesReferences JSON into a flat map:
    { "<issue_number>": <pr_number>, ... }

  GraphQL input structure (from closingIssuesReferences query):
    {
      "data": {
        "repository": {
          "pullRequests": {
            "nodes": [
              {
                "number": <pr_number>,
                "closingIssuesReferences": {
                  "nodes": [{"number": <issue_number>}, ...]
                }
              },
              ...
            ]
          }
        }
      }
    }
  """
  raw = json.loads(graphql_input.read_text())
  pr_nodes = (
    raw
    .get("data", {})
    .get("repository", {})
    .get("pullRequests", {})
    .get("nodes", [])
  )

  pr_map: dict[str, int] = {}
  for pr in pr_nodes:
    pr_num = pr.get("number")
    closing_data = pr.get("closingIssuesReferences", {})
    closing = closing_data.get("nodes", [])
    if closing_data.get("pageInfo", {}).get("hasNextPage"):
      print(
        f"Warning: PR #{pr_num} links >20 issues; some may be missing from the "
        "active-PR exclusion map. Check manually.",
        file=sys.stderr,
      )
    for issue_ref in closing:
      issue_num = issue_ref.get("number")
      if issue_num is not None and pr_num is not None:
        pr_map[str(issue_num)] = pr_num

  pr_map_output.write_text(json.dumps(pr_map, indent=2))
  print(
    f"# PR map: {len(pr_map)} issues with active PRs written to {pr_map_output}",
    file=sys.stderr,
  )
  if len(pr_nodes) >= 200:
    print(
      "Warning: fetched exactly 200 open PRs — repo may have more. "
      "Issues linked to PRs beyond the first 200 are not excluded from sprint candidates. "
      "Consider closing stale PRs or raising the pullRequests(first:...) limit in SKILL.md.",
      file=sys.stderr,
    )


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Fetch parallel-sprint eligible issues or build the active-PR map"
  )
  parser.add_argument(
    "--limit", type=int, default=200, help="Max issues to fetch (default: 200)"
  )
  parser.add_argument(
    "--label", type=str, default=None,
    help="Filter issues by this label (optional)",
  )
  parser.add_argument(
    "--pr-map", type=Path, default=None,
    metavar="PATH",
    help="Path to ps_pr_map.json; issues with active PRs are excluded",
  )

  # PR-map build mode
  parser.add_argument(
    "--build-pr-map", action="store_true",
    help="Transform GraphQL closingIssuesReferences output into issue→PR map",
  )
  parser.add_argument(
    "--graphql-input", type=Path, default=None, metavar="PATH",
    help="Path to raw GraphQL response (required with --build-pr-map)",
  )
  parser.add_argument(
    "--pr-map-output", type=Path, default=None, metavar="PATH",
    help="Where to write the built PR map (required with --build-pr-map)",
  )

  args = parser.parse_args()

  try:
    if args.build_pr_map:
      if not args.graphql_input or not args.pr_map_output:
        print(
          "Error: --build-pr-map requires --graphql-input and --pr-map-output",
          file=sys.stderr,
        )
        sys.exit(1)
      build_pr_map(args.graphql_input, args.pr_map_output)
      return

    issues, raw_count = fetch_issues(
      limit=args.limit,
      label=args.label,
      pr_map_path=args.pr_map,
    )
    print(json.dumps(issues, indent=2))
    flagged_count = sum(1 for i in issues if i.get("flagged_injection"))
    print(
      f"# Fetched {len(issues)} eligible issues"
      + (f" ({flagged_count} with injection flags)" if flagged_count else ""),
      file=sys.stderr,
    )
    if raw_count >= args.limit:
      print(
        f"Warning: gh returned {raw_count} issues (the fetch limit). "
        f"The repo may have more eligible issues beyond this cap. "
        f"Re-run with --limit <higher number> to raise the cap.",
        file=sys.stderr,
      )

  except subprocess.TimeoutExpired:
    print(
      "Error: gh CLI timed out after 60s. Check your network and GitHub auth.",
      file=sys.stderr,
    )
    sys.exit(1)
  except subprocess.CalledProcessError as e:
    print(f"Error: gh CLI failed:\n{e.stderr}", file=sys.stderr)
    sys.exit(1)
  except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
  main()
