#!/usr/bin/env python3
"""
Fetches all open GitHub issues from the current repo as structured JSON.

Usage:
    python3 fetch_issues.py > /tmp/issues.json
    python3 fetch_issues.py --limit 50 > /tmp/issues.json

Output: JSON array of issues with fields:
    number, title, body (truncated), labels, comment_count, url,
    milestone, created_at, updated_at,
    blocked_by_refs, blocks_refs, fixes_refs
"""

import argparse
import json
import re
import subprocess
import sys

# Dependency patterns — mirrored from analyze_relationships.py.
# Extracted from the full body BEFORE truncation so signals beyond 2000 chars
# are captured and stored as structured fields alongside the truncated body.
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


def _extract_dep_refs(text: str) -> dict[str, list[int]]:
  """Extract dependency references from the full (untruncated) issue body."""
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


def fetch_issues(limit: int = 200) -> list[dict]:
  """Fetch open issues from GitHub via gh CLI."""
  result = subprocess.run(
    [
      "gh", "issue", "list",
      "--state", "open",
      "--limit", str(limit),
      "--json", "number,title,body,labels,comments,url,milestone,createdAt,updatedAt",
    ],
    capture_output=True,
    text=True,
    check=True,
  )
  issues = json.loads(result.stdout)

  normalized = []
  for issue in issues:
    full_body = issue.get("body") or ""
    dep_refs = _extract_dep_refs(full_body)
    normalized.append({
      "number": issue["number"],
      "title": issue["title"],
      "body": full_body[:2000],
      "labels": [lbl["name"] for lbl in issue.get("labels", [])],
      "comment_count": len(issue.get("comments", [])),
      "url": issue["url"],
      "milestone": (
        issue["milestone"]["title"] if issue.get("milestone") else None
      ),
      "created_at": issue.get("createdAt"),
      "updated_at": issue.get("updatedAt"),
      # Pre-extracted dependency refs from the full body (before truncation).
      # analyze_relationships.py reads these instead of re-parsing the truncated body.
      **dep_refs,
    })

  return normalized


PRIORITY_LABELS = {"priority:now", "priority:next", "priority:soon", "priority:backlog"}


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Fetch open GitHub issues as structured JSON"
  )
  parser.add_argument(
    "--limit", type=int, default=200, help="Max issues to fetch (default: 200)"
  )
  parser.add_argument(
    "--only-unlabeled",
    action="store_true",
    help=(
      "Return only issues without any priority:* label. "
      "Used in incremental re-ranking to focus scoring on new issues. "
      "Note: analyze_relationships.py still needs the full list — use the "
      "default fetch (without this flag) for /tmp/wc_issues.json."
    ),
  )
  args = parser.parse_args()

  try:
    issues = fetch_issues(limit=args.limit)
    if args.only_unlabeled:
      before = len(issues)
      issues = [
        issue for issue in issues
        if not any(lbl in PRIORITY_LABELS for lbl in issue.get("labels", []))
      ]
      print(
        f"# --only-unlabeled: {len(issues)} unlabeled issues "
        f"(filtered from {before} total)",
        file=sys.stderr,
      )
    print(json.dumps(issues, indent=2))
    print(f"# Fetched {len(issues)} open issues", file=sys.stderr)
    if len(issues) == args.limit:
      print(
        f"Warning: fetched exactly {args.limit} issues — the repo may have more open issues. "
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
