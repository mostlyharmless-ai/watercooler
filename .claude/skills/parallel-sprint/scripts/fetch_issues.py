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
import json
import re
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Dependency patterns (mirrored from analyze_relationships.py)
# Extracted BEFORE body truncation so signals beyond 2000 chars are preserved.
# ---------------------------------------------------------------------------
_BLOCKED_BY_RE = re.compile(
  r"blocked?\s+by\s+#(\d+)"
  r"|depends?\s+on\s+#(\d+)"
  r"|requires?\s+#(\d+)\b"
  r"|after\s+#(\d+)\s+(?:is\s+)?(?:merged|closed|done|fixed|resolved|landed)"
  r"|needs?\s+#(\d+)\s+(?:to\s+be\s+)?(?:merged|closed|done|fixed|resolved)"
  r"|prerequisite[:\s]+#(\d+)"
  r"|follow[- ]?up\s+(?:to|from|on)\s+#(\d+)",
  re.IGNORECASE,
)
_BLOCKS_RE = re.compile(r"\bblocks?\s+#(\d+)|\bblocking\s+#(\d+)", re.IGNORECASE)
_FIXES_RE = re.compile(r"(?:fixes|closes|resolves)\s+#(\d+)", re.IGNORECASE)

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
  return {
    "blocked_by_refs": sorted({
      int(g) for m in _BLOCKED_BY_RE.finditer(text) for g in m.groups() if g
    }),
    "blocks_refs": sorted({
      int(g) for m in _BLOCKS_RE.finditer(text) for g in m.groups() if g
    }),
    "fixes_refs": sorted({
      int(g) for m in _FIXES_RE.finditer(text) for g in m.groups() if g
    }),
  }


def _scan_injection(text: str) -> bool:
  return bool(_INJECTION_PATTERNS.search(text))


def fetch_issues(
  limit: int = 200,
  label: str | None = None,
  pr_map_path: Path | None = None,
) -> list[dict]:
  """Fetch open, unassigned issues eligible for parallel-sprint."""
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

  result = subprocess.run(cmd, capture_output=True, text=True, check=True)
  issues = json.loads(result.stdout)

  # Load active-PR map if provided (built from GraphQL closingIssuesReferences)
  pr_issue_set: set[int] = set()
  if pr_map_path and pr_map_path.exists():
    pr_map = json.loads(pr_map_path.read_text())
    pr_issue_set = {int(k) for k in pr_map}

  normalized = []
  for issue in issues:
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
      "body": full_body[:2000],
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

  return normalized


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
    closing = pr.get("closingIssuesReferences", {}).get("nodes", [])
    for issue_ref in closing:
      issue_num = issue_ref.get("number")
      if issue_num is not None and pr_num is not None:
        pr_map[str(issue_num)] = pr_num

  pr_map_output.write_text(json.dumps(pr_map, indent=2))
  print(
    f"# PR map: {len(pr_map)} issues with active PRs written to {pr_map_output}",
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

    issues = fetch_issues(
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
    if len(issues) == args.limit:
      print(
        f"Warning: fetched exactly {args.limit} issues — the repo may have more. "
        f"Re-run with --limit <higher number> to raise the cap.",
        file=sys.stderr,
      )

  except subprocess.CalledProcessError as e:
    print(f"Error: gh CLI failed:\n{e.stderr}", file=sys.stderr)
    sys.exit(1)
  except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
  main()
