#!/usr/bin/env python3
"""
Analyzes relationships between open GitHub issues across four dimensions:
  1. Dependencies  — A must be done before B
  2. Synergies     — A and B benefit from being tackled together
  3. Conflicts     — A and B contradict or duplicate each other
  4. Opportunities — A is a cheap win given what else is in flight

Reads issues from /tmp/wc_issues.json (produced by fetch_issues.py).
Outputs a JSON relationship map to stdout.

Usage:
    python3 analyze_relationships.py > /tmp/wc_relationships.json
    python3 analyze_relationships.py --input /tmp/wc_issues.json

Output schema:
    {
      "dependencies": {
        "<issue_number>": {
          "blocks": [<number>, ...],     # this issue must be done first
          "blocked_by": [<number>, ...]  # these must be done first
        }
      },
      "synergies": [
        {
          "issues": [<number>, ...],
          "reason": "Shared area: memory-tiers",
          "type": "label_cluster",
          "label": "memory-tiers"
        }
      ],
      "conflicts": [
        {
          "issues": [<number>, <number>],
          "reason": "Both reference #NNN with fix/close intent",
          "type": "potential_duplicate",
          "confidence": "low"
        }
      ],
      "opportunities": [
        {
          "issue": <number>,
          "reason": "In memory-tiers cluster with 2 priority:now issues",
          "type": "synergy_window | blocker_removal | cheap_win",
          "adjacent_high_priority": [<number>, ...]
        }
      ],
      "cross_references": {
        "<number>": [<number>, ...]   # all other open issues mentioned in body
      }
    }

Dimension heuristics:

  Dependencies (high confidence — automatic):
    - "blocked by #N", "depends on #N", "requires #N", "after #N is merged" → blocked_by
    - "follow-up to #N" → blocked_by (current issue depends on N being done first)
    - "blocks #N" → blocks
    Score modifier: blocker of priority:now → +4; blocker of priority:next/soon → +2 (cap +6)

  Synergies (medium confidence — label-based):
    - Issues sharing the same feature-area label (memory-tiers, federation, etc.)
    - Only clusters of 2+ issues reported

  Conflicts (low confidence — always flagged for human review):
    - Two issues both referencing the same issue with "fixes/closes/resolves #N"

  Opportunities (derived from synergies + dependencies + tier):
    - synergy_window: lower-tier issue shares area label with ≥1 priority:now issue
    - blocker_removal: issue unblocks ≥2 other issues (high cascade leverage)
    - cheap_win: issue is sev:low/medium in the same cluster as active sprint work
    No score modifier — presented as tactical recommendations in the report.
"""

import argparse
import json
import re
import sys
from collections import defaultdict


# In body of issue A: "blocked by #B" → A blocked_by B, B blocks A
BLOCKED_BY_PATTERNS = [
    r"blocked?\s+by\s+#(\d+)",
    r"depends?\s+on\s+#(\d+)",
    r"requires?\s+#(\d+)\b",
    r"after\s+#(\d+)\s+(?:is\s+)?(?:merged|closed|done|fixed|resolved|landed)",
    r"needs?\s+#(\d+)\s+(?:to\s+be\s+)?(?:merged|closed|done|fixed|resolved)",
    r"prerequisite[:\s]+#(\d+)",
    r"follow[- ]?up\s+(?:to|from|on)\s+#(\d+)",
]

# In body of issue A: "blocks #B" → A blocks B
BLOCKS_PATTERNS = [
    r"\bblocks?\s+#(\d+)",
    r"\bblocking\s+#(\d+)",
]

# In body of issue A: "fixes/closes #N" — used for conflict detection
FIXES_CLOSES_PATTERNS = [
    r"(?:fixes|closes|resolves)\s+#(\d+)",
]

# Any bare #NNN reference (for cross-reference map — informational only)
ANY_ISSUE_REF = r"#(\d+)"

# Feature-area labels whose issues benefit from batching
# Must mirror Feature Area Labels in references/label_taxonomy.md
SYNERGY_LABELS = [
    "memory-tiers",
    "federation",
    "leanrag",
    "daemon",
    "graph-first",
    "testing",
    "documentation",
]


def _issue_text(issue: dict) -> str:
  """Concatenate title and body for pattern matching."""
  return f"{issue.get('title', '') or ''}\n{issue.get('body', '') or ''}"


def _extract_refs(text: str, patterns: list[str]) -> list[int]:
  """Extract all issue numbers matching any of the given patterns."""
  refs: list[int] = []
  for pattern in patterns:
    for match in re.finditer(pattern, text, re.IGNORECASE):
      refs.append(int(match.group(1)))
  return refs


def analyze_dependencies(
  issues: list[dict],
) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
  """
  Returns:
      blocks    — {issue_num: set of issue_nums it must precede}
      blocked_by — {issue_num: set of issue_nums that must precede it}
  """
  issue_numbers = {issue["number"] for issue in issues}
  blocks: dict[int, set[int]] = defaultdict(set)
  blocked_by: dict[int, set[int]] = defaultdict(set)

  for issue in issues:
    a = issue["number"]

    # Prefer pre-extracted structured fields from fetch_issues.py (full body, pre-truncation).
    # Fall back to regex over the truncated body for backward compatibility.
    if "blocked_by_refs" in issue:
      bb_refs = [b for b in issue["blocked_by_refs"] if b in issue_numbers and b != a]
    else:
      bb_refs = [b for b in _extract_refs(_issue_text(issue), BLOCKED_BY_PATTERNS)
                 if b in issue_numbers and b != a]

    if "blocks_refs" in issue:
      bl_refs = [b for b in issue["blocks_refs"] if b in issue_numbers and b != a]
    else:
      bl_refs = [b for b in _extract_refs(_issue_text(issue), BLOCKS_PATTERNS)
                 if b in issue_numbers and b != a]

    for b in bb_refs:
      blocked_by[a].add(b)
      blocks[b].add(a)

    for b in bl_refs:
      blocks[a].add(b)
      blocked_by[b].add(a)

  return blocks, blocked_by


def analyze_conflicts(issues: list[dict]) -> list[dict]:
  """
  Detect potential conflicts/duplicates: two issues both claiming to fix the same issue.
  Always flagged as low confidence — requires human verification.
  """
  issue_numbers = {issue["number"] for issue in issues}
  fixes_map: dict[int, list[int]] = defaultdict(list)

  for issue in issues:
    a = issue["number"]
    # Prefer pre-extracted structured field (full body, pre-truncation).
    # Fall back to regex over the truncated body for backward compatibility.
    # Uses body-only (not title+body) — fix/close intent is only meaningful in body.
    if "fixes_refs" in issue:
      targets = [t for t in issue["fixes_refs"] if t in issue_numbers and t != a]
    else:
      body = issue.get("body", "") or ""
      # dict.fromkeys deduplicates while preserving order — guards against a body
      # like "Fixes #10 and closes #10" producing two appends for the same ref,
      # which would create a spurious conflict with a single unique participant.
      targets = list(dict.fromkeys(
        t for t in _extract_refs(body, FIXES_CLOSES_PATTERNS)
        if t in issue_numbers and t != a
      ))
    for target in targets:
      fixes_map[target].append(a)

  conflicts = []
  for target, fixers in fixes_map.items():
    if len(fixers) >= 2:
      conflicts.append({
        "issues": sorted(set(fixers)),
        "reason": f"Both reference #{target} with fix/close intent — potential duplicates",
        "type": "potential_duplicate",
        "confidence": "low",
      })

  return conflicts


def analyze_synergies(issues: list[dict]) -> list[dict]:
  """
  Group issues into synergy clusters by shared feature-area label.
  Only clusters with 2+ issues are included.
  """
  label_to_issues: dict[str, list[int]] = defaultdict(list)
  for issue in issues:
    for label in issue.get("labels", []):
      if label in SYNERGY_LABELS:
        label_to_issues[label].append(issue["number"])

  synergies = []
  for label, nums in sorted(label_to_issues.items()):
    if len(nums) >= 2:
      synergies.append({
        "issues": sorted(nums),
        "reason": f"Shared area: {label}",
        "type": "label_cluster",
        "label": label,
      })

  return synergies


def analyze_cross_references(issues: list[dict]) -> dict[int, list[int]]:
  """
  For each issue, list all other open issue numbers mentioned anywhere in its body/title.
  Informational only — captures informal references not caught by explicit patterns.
  """
  issue_numbers = {issue["number"] for issue in issues}
  cross_refs: dict[int, list[int]] = {}

  for issue in issues:
    a = issue["number"]
    text = _issue_text(issue)
    refs: set[int] = set()
    for m in re.finditer(ANY_ISSUE_REF, text):
      num = int(m.group(1))
      if num in issue_numbers and num != a:
        refs.add(num)
    if refs:
      cross_refs[a] = sorted(refs)

  return cross_refs


def analyze_opportunities(
  issues: list[dict],
  synergies: list[dict],
  blocks: dict[int, set[int]],
) -> list[dict]:
  """
  Identify tactical opportunity windows — issues that are cheap wins given what else
  is in flight.  Three types:

    synergy_window   — lower-priority issue shares area label with ≥1 priority:now issue.
                       Rationale: the team is already holding context for this area.

    blocker_removal  — issue unblocks ≥2 other issues.
                       Rationale: one action enables cascading progress.

    cheap_win        — sev:low or no-sev issue that is in a synergy cluster
                       containing active sprint work.
                       Rationale: low investment, high contextual leverage.

  No score modifier is applied — opportunities are surfaced as tactical recommendations.
  """
  # Index current label state of each issue
  num_to_issue = {issue["number"]: issue for issue in issues}
  now_issues = {
    issue["number"]
    for issue in issues
    if "priority:now" in issue.get("labels", [])
  }
  next_issues = {
    issue["number"]
    for issue in issues
    if "priority:next" in issue.get("labels", [])
  }

  opportunities: list[dict] = []
  seen: set[int] = set()  # synergy_window takes precedence over blocker_removal for the same issue

  # --- synergy_window ---
  for cluster in synergies:
    cluster_nums = set(cluster["issues"])
    active_in_cluster = cluster_nums & now_issues
    if not active_in_cluster:
      continue
    # Lower-priority issues in the same cluster are opportunity picks
    for num in sorted(cluster_nums - now_issues):
      if num in seen:
        continue
      issue = num_to_issue.get(num, {})
      labels = issue.get("labels", [])
      is_cheap = any(l in ("sev:low", "sev:medium") for l in labels) or not any(
        l.startswith("sev:") for l in labels
      )
      opp_type = "cheap_win" if is_cheap else "synergy_window"
      opportunities.append({
        "issue": num,
        "reason": (
          f"Shares '{cluster['label']}' area with {len(active_in_cluster)} priority:now "
          f"issue(s) — team already holds context"
        ),
        "type": opp_type,
        "adjacent_high_priority": sorted(active_in_cluster),
      })
      seen.add(num)

  # --- blocker_removal ---
  for num, blocked_set in sorted(blocks.items()):
    if len(blocked_set) < 2:
      continue
    if num in seen:
      continue
    # Count how many of the blocked issues are in now/next tier
    high_value_unblocked = [b for b in blocked_set if b in now_issues | next_issues]
    if not high_value_unblocked:
      continue
    opportunities.append({
      "issue": num,
      "reason": (
        f"Unblocks {len(blocked_set)} other issues "
        f"({len(high_value_unblocked)} in priority:now/next) — high cascade leverage"
      ),
      "type": "blocker_removal",
      "adjacent_high_priority": sorted(high_value_unblocked),
    })
    seen.add(num)

  # Sort by adjacency count descending (most leverage first)
  opportunities.sort(key=lambda o: len(o["adjacent_high_priority"]), reverse=True)
  return opportunities


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Analyze relationships between GitHub issues"
  )
  parser.add_argument(
    "--input",
    default=".sprint/tmp/ps_issues.json",
    help="Path to issues JSON (default: .sprint/tmp/ps_issues.json)",
  )
  args = parser.parse_args()

  try:
    with open(args.input) as f:
      issues = json.load(f)
  except FileNotFoundError:
    print(f"Error: {args.input} not found. Run fetch_issues.py first.", file=sys.stderr)
    sys.exit(1)
  except json.JSONDecodeError as e:
    print(f"Error: invalid JSON in {args.input}: {e}", file=sys.stderr)
    sys.exit(1)

  blocks, blocked_by = analyze_dependencies(issues)
  synergies = analyze_synergies(issues)
  conflicts = analyze_conflicts(issues)
  cross_refs = analyze_cross_references(issues)
  opportunities = analyze_opportunities(issues, synergies, blocks)

  # Build unified dependency map (only issues with at least one relationship)
  all_nums = {issue["number"] for issue in issues}
  dependencies: dict[str, dict] = {}
  for num in sorted(all_nums):
    b = sorted(blocks.get(num, set()))
    bb = sorted(blocked_by.get(num, set()))
    if b or bb:
      dependencies[str(num)] = {"blocks": b, "blocked_by": bb}

  output = {
    "dependencies": dependencies,
    "synergies": synergies,
    "conflicts": conflicts,
    "opportunities": opportunities,
    "cross_references": {str(k): v for k, v in sorted(cross_refs.items())},
  }

  print(json.dumps(output, indent=2))

  # Summary stats to stderr
  syn_issue_count = sum(len(s["issues"]) for s in synergies)
  print(
    f"# Relationships: {len(dependencies)} issues with deps, "
    f"{len(synergies)} synergy clusters ({syn_issue_count} issues), "
    f"{len(conflicts)} potential conflicts, "
    f"{len(opportunities)} opportunity picks, "
    f"{sum(len(v) for v in cross_refs.values())} cross-references",
    file=sys.stderr,
  )


if __name__ == "__main__":
  main()
