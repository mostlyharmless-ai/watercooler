#!/usr/bin/env python3
"""
Clusters GitHub issues into candidate groups using structural signals only.
No LLM calls — the orchestrator LLM in SKILL.md interprets these signals.

Reads ps_issues.json and ps_relationships.json (produced by fetch_issues.py
and analyze_relationships.py respectively).

Outputs ps_candidates.json: a list of candidate groups with structural
signals (label clusters, dependency edges, cross-group edge markers,
structural scope hints, hint overlap map) for the LLM to interpret.

Usage:
    python3 cluster_issues.py \\
        --issues .sprint/tmp/ps_issues.json \\
        --relationships .sprint/tmp/ps_relationships.json \\
        [--top-n 5]

Output schema:
    {
      "candidates": [
        {
          "label": "memory-tiers",           # shared label that defines this group
          "issues": [                         # issues in this candidate group
            {
              "number": 210,
              "title": "Fix N+1 in memory lookup",
              "labels": ["memory-tiers", "sev:medium"],
              "effort_label": null,           # size:S/M/L/XL from labels, or null
              "scope_hints": ["src/watercooler/memory.py", "memory"],
              "flagged_injection": false
            }
          ],
          "dependency_edges": [              # edges WITHIN this group
            {"from": 210, "to": 215, "type": "blocked_by"}
          ],
          "cross_group_edges": [             # edges that cross group boundaries
            {"from": 210, "to": 197, "type": "blocked_by", "other_group": "leanrag"}
          ],
          "has_cycle": false,                # true if any cycle detected in this group
          "hint_overlap_pairs": [            # pairs with overlapping scope hints
            {"issues": [210, 215], "shared_hints": ["memory.py"]}
          ]
        }
      ],
      "ungrouped": [                         # issues not in any label cluster
        {"number": 220, "title": "...", ...}
      ],
      "cycle_warnings": ["Cycle detected: 210 -> 215 -> 210"],
      "stats": {
        "total_issues": 42,
        "grouped": 30,
        "ungrouped": 12,
        "candidate_count": 5
      }
    }
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Structural scope hint extraction (no LLM)
# Looks for path-like strings and module name mentions in title + body.
# ---------------------------------------------------------------------------

# Match path-like tokens: foo/bar.py, src/watercooler/x.py, x.py, etc.
_PATH_RE = re.compile(r"(?:[\w.-]+/)+[\w.-]+\.py\b|[\w.-]+\.py\b")

# Effort labels (size:S/M/L/XL, effort:S/M/L/XL, complexity:S/M/L/XL)
_EFFORT_RE = re.compile(
  r"\b(?:size|effort|complexity):([SsMmLlXx]+)\b",
)


def _extract_scope_hints(issue: dict) -> list[str]:
  text = f"{issue.get('title', '')} {issue.get('body', '')} "
  hints: list[str] = []
  for m in _PATH_RE.finditer(text):
    path = m.group(0).strip()
    if path and path not in hints:
      hints.append(path)
  return hints[:20]  # cap per issue


def _extract_effort_label(labels: list[str]) -> str | None:
  for label in labels:
    m = _EFFORT_RE.match(label)
    if m:
      return m.group(1).upper()
  return None


# ---------------------------------------------------------------------------
# Cycle detection (DFS)
# ---------------------------------------------------------------------------

def _detect_cycles(
  group_numbers: set[int],
  dep_map: dict[int, list[int]],
) -> tuple[bool, list[str]]:
  """
  DFS cycle detection over the dependency subgraph within the group.
  Returns (has_cycle, list_of_warning_strings).
  """
  warnings: list[str] = []
  visited: set[int] = set()
  in_stack: set[int] = set()

  def dfs(node: int, path: list[int]) -> None:
    if node in in_stack:
      cycle_start = path.index(node)
      cycle = path[cycle_start:] + [node]
      warnings.append("Cycle detected: " + " -> ".join(str(n) for n in cycle))
      return
    if node in visited:
      return
    visited.add(node)
    in_stack.add(node)
    for neighbour in dep_map.get(node, []):
      if neighbour in group_numbers:
        dfs(neighbour, path + [node])
    in_stack.discard(node)

  for num in sorted(group_numbers):
    if num not in visited:
      dfs(num, [])

  return bool(warnings), warnings


# ---------------------------------------------------------------------------
# Main clustering logic
# ---------------------------------------------------------------------------

def cluster_issues(
  issues: list[dict],
  relationships: dict,
  top_n: int = 5,
) -> dict:
  """
  Build candidate clusters from structural signals.
  Pure data — no LLM calls.
  """
  # Build lookup
  num_to_issue = {i["number"]: i for i in issues}
  all_nums = set(num_to_issue.keys())

  # Enrich issues with scope hints and effort labels
  for issue in issues:
    issue["scope_hints"] = _extract_scope_hints(issue)
    issue["effort_label"] = _extract_effort_label(issue.get("labels", []))

  # ---- Label clusters ----
  # Group by any shared feature-area-ish label (exclude priority:*, sev:*, size:*, etc.)
  label_to_nums: dict[str, list[int]] = defaultdict(list)
  for issue in issues:
    for label in issue.get("labels", []):
      if not any(label.startswith(prefix) for prefix in (
        "priority:", "sev:", "size:", "effort:", "complexity:", "in-progress",
      )):
        label_to_nums[label].append(issue["number"])

  # Only keep labels with 2+ issues
  label_clusters: dict[str, list[int]] = {
    label: sorted(nums)
    for label, nums in sorted(label_to_nums.items())
    if len(nums) >= 2
  }

  # ---- Build dependency edge map ----
  deps = relationships.get("dependencies", {})
  # blocks[A] = [B, ...] means A must precede B
  blocks_map: dict[int, list[int]] = {}
  blocked_by_map: dict[int, list[int]] = {}
  for str_num, dep in deps.items():
    num = int(str_num)
    blocks_map[num] = [int(x) for x in dep.get("blocks", [])]
    blocked_by_map[num] = [int(x) for x in dep.get("blocked_by", [])]

  # ---- Cycle detection across ALL issues ----
  all_cycle_warnings: list[str] = []
  _, all_cycle_warnings = _detect_cycles(all_nums, blocked_by_map)

  # ---- Build candidate groups ----
  candidates = []
  already_grouped: set[int] = set()

  for label, nums in sorted(label_clusters.items(), key=lambda kv: -len(kv[1])):
    group_set = set(nums)

    # Intra-group dependency edges
    intra_edges: list[dict] = []
    for n in sorted(group_set):
      for blocked in blocked_by_map.get(n, []):
        if blocked in group_set:
          intra_edges.append({"from": n, "to": blocked, "type": "blocked_by"})
      for blocks in blocks_map.get(n, []):
        if blocks in group_set:
          intra_edges.append({"from": n, "to": blocks, "type": "blocks"})

    # Cross-group edges (edges leaving this group to other known issues)
    cross_edges: list[dict] = []
    for n in sorted(group_set):
      for blocked in blocked_by_map.get(n, []):
        if blocked not in group_set and blocked in all_nums:
          other_group = next(
            (lbl for lbl, lnums in label_clusters.items() if blocked in lnums),
            "ungrouped",
          )
          cross_edges.append({
            "from": n, "to": blocked,
            "type": "blocked_by", "other_group": other_group,
          })
      for blocks in blocks_map.get(n, []):
        if blocks not in group_set and blocks in all_nums:
          other_group = next(
            (lbl for lbl, lnums in label_clusters.items() if blocks in lnums),
            "ungrouped",
          )
          cross_edges.append({
            "from": n, "to": blocks,
            "type": "blocks", "other_group": other_group,
          })

    # Cycle detection within this group
    has_cycle, group_cycle_warnings = _detect_cycles(group_set, blocked_by_map)

    # Pairwise hint overlap
    overlap_pairs: list[dict] = []
    num_list = sorted(group_set)
    for i, na in enumerate(num_list):
      for nb in num_list[i + 1:]:
        hints_a = set(num_to_issue[na].get("scope_hints", []))
        hints_b = set(num_to_issue[nb].get("scope_hints", []))
        shared = sorted(hints_a & hints_b)
        if shared:
          overlap_pairs.append({"issues": [na, nb], "shared_hints": shared})

    # Build issue list for this candidate
    candidate_issues = []
    for n in sorted(group_set):
      issue = num_to_issue[n]
      candidate_issues.append({
        "number": n,
        "title": issue["title"],
        "labels": issue.get("labels", []),
        "effort_label": issue.get("effort_label"),
        "scope_hints": issue.get("scope_hints", []),
        "flagged_injection": issue.get("flagged_injection", False),
      })

    candidates.append({
      "label": label,
      "issues": candidate_issues,
      "dependency_edges": intra_edges,
      "cross_group_edges": cross_edges,
      "has_cycle": has_cycle,
      "cycle_warnings": group_cycle_warnings,
      "hint_overlap_pairs": overlap_pairs,
    })

    already_grouped.update(group_set)

  # ---- Cap at top_n ----
  # Sort by (has_cycle ascending, cross_group_edges count ascending, group_size descending)
  candidates.sort(key=lambda c: (
    c["has_cycle"],
    len(c["cross_group_edges"]),
    -len(c["issues"]),
  ))
  candidates = candidates[:top_n]

  # ---- Ungrouped issues ----
  grouped_in_result = {n for c in candidates for issue in c["issues"] for n in [issue["number"]]}
  ungrouped = [
    {
      "number": num_to_issue[n]["number"],
      "title": num_to_issue[n]["title"],
      "labels": num_to_issue[n].get("labels", []),
      "effort_label": num_to_issue[n].get("effort_label"),
      "scope_hints": num_to_issue[n].get("scope_hints", []),
      "flagged_injection": num_to_issue[n].get("flagged_injection", False),
    }
    for n in sorted(all_nums - grouped_in_result)
  ]

  return {
    "candidates": candidates,
    "ungrouped": ungrouped,
    "cycle_warnings": all_cycle_warnings,
    "stats": {
      "total_issues": len(issues),
      "grouped": len(grouped_in_result),
      "ungrouped": len(ungrouped),
      "candidate_count": len(candidates),
    },
  }


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Build structural candidate clusters for parallel-sprint (no LLM)"
  )
  parser.add_argument(
    "--issues",
    default=".sprint/tmp/ps_issues.json",
    help="Path to ps_issues.json (default: .sprint/tmp/ps_issues.json)",
  )
  parser.add_argument(
    "--relationships",
    default=".sprint/tmp/ps_relationships.json",
    help="Path to ps_relationships.json (default: .sprint/tmp/ps_relationships.json)",
  )
  parser.add_argument(
    "--top-n", type=int, default=5,
    help="Max number of candidate clusters to emit (default: 5)",
  )
  args = parser.parse_args()

  try:
    with open(args.issues) as f:
      issues = json.load(f)
  except FileNotFoundError:
    print(f"Error: {args.issues} not found. Run fetch_issues.py first.", file=sys.stderr)
    sys.exit(1)
  except json.JSONDecodeError as e:
    print(f"Error: invalid JSON in {args.issues}: {e}", file=sys.stderr)
    sys.exit(1)

  try:
    with open(args.relationships) as f:
      relationships = json.load(f)
  except FileNotFoundError:
    print(
      f"Error: {args.relationships} not found. Run analyze_relationships.py first.",
      file=sys.stderr,
    )
    sys.exit(1)
  except json.JSONDecodeError as e:
    print(f"Error: invalid JSON in {args.relationships}: {e}", file=sys.stderr)
    sys.exit(1)

  result = cluster_issues(issues, relationships, top_n=args.top_n)
  print(json.dumps(result, indent=2))

  s = result["stats"]
  print(
    f"# {s['candidate_count']} candidate clusters "
    f"({s['grouped']} issues grouped, {s['ungrouped']} ungrouped of {s['total_issues']} total)",
    file=sys.stderr,
  )
  if result["cycle_warnings"]:
    for w in result["cycle_warnings"]:
      print(f"Warning: {w}", file=sys.stderr)


if __name__ == "__main__":
  main()
