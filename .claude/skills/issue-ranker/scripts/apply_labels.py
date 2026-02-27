#!/usr/bin/env python3
"""
Apply priority and severity label changes to GitHub issues.

Reads current issue labels from /tmp/wc_issues.json and a label plan,
computes the minimal diff, and applies only actual changes.

Usage:
    python3 apply_labels.py --plan /tmp/wc_label_plan.json
    python3 apply_labels.py --plan /tmp/wc_label_plan.json --dry-run
    python3 apply_labels.py --plan /tmp/wc_label_plan.json --issues /tmp/wc_issues.json

Plan file format (JSON array):
    [
      {"number": 123, "priority": "priority:now", "sev": "sev:critical"},
      {"number": 124, "priority": "priority:next", "sev": "sev:high"},
      ...
    ]

This script:
  - Only touches priority:* and sev:* labels
  - Never removes domain labels (bug, enhancement, feature-area, etc.)
  - Skips issues where proposed labels already match current labels exactly
  - Shows a precise change log: "priority:backlog → priority:now | (none) → sev:high"
  - Reports totals: N updated, M already correct, P errors
"""

import argparse
import json
import subprocess
import sys

PRIORITY_LABELS = {"priority:now", "priority:next", "priority:soon", "priority:backlog"}
SEV_LABELS = {"sev:critical", "sev:high", "sev:medium", "sev:low"}


def _validate_plan(plan: list[dict]) -> None:
  """Validate plan entries before any GitHub API calls.

  Raises ValueError with a descriptive message if any entry is invalid.
  Validation must pass completely before any gh subprocess call is made.
  """
  seen_numbers: set[int] = set()
  for i, entry in enumerate(plan):
    number = entry.get("number")
    priority = entry.get("priority")
    sev = entry.get("sev")

    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
      raise ValueError(f"Plan entry {i}: invalid number {number!r} (must be a positive integer)")
    if number in seen_numbers:
      raise ValueError(f"Plan entry {i}: duplicate issue number #{number}")
    seen_numbers.add(number)
    if priority not in PRIORITY_LABELS:
      raise ValueError(
        f"Plan entry {i} (#{number}): invalid priority {priority!r} "
        f"(must be one of: {sorted(PRIORITY_LABELS)})"
      )
    if sev not in SEV_LABELS:
      raise ValueError(
        f"Plan entry {i} (#{number}): invalid sev {sev!r} "
        f"(must be one of: {sorted(SEV_LABELS)})"
      )


def load_current_labels(issues_path: str) -> dict[int, set[str]]:
  """Load current labels for each issue from the issues JSON."""
  with open(issues_path) as f:
    issues = json.load(f)
  return {issue["number"]: set(issue.get("labels", [])) for issue in issues}


def compute_diff(
  current: set[str],
  proposed_priority: str,
  proposed_sev: str,
) -> tuple[list[str], list[str]]:
  """
  Compute the minimal label change needed.

  Returns:
      (add_labels, remove_labels) — labels to add and labels to remove.
      Both lists are empty if current labels already match proposed.
  """
  add_labels: list[str] = []
  remove_labels: list[str] = []

  # Priority: add proposed if not present; remove all other priority labels
  if proposed_priority not in current:
    add_labels.append(proposed_priority)
  for lbl in sorted(PRIORITY_LABELS - {proposed_priority}):
    if lbl in current:
      remove_labels.append(lbl)

  # Sev: same pattern
  if proposed_sev not in current:
    add_labels.append(proposed_sev)
  for lbl in sorted(SEV_LABELS - {proposed_sev}):
    if lbl in current:
      remove_labels.append(lbl)

  return add_labels, remove_labels


def format_change(current: set[str], proposed_priority: str, proposed_sev: str) -> str:
  """Format a human-readable delta string for an issue."""
  parts = []

  old_priority = sorted(current & PRIORITY_LABELS)
  old_priority_str = old_priority[0] if old_priority else "(none)"
  if old_priority_str != proposed_priority:
    parts.append(f"{old_priority_str} → {proposed_priority}")
  else:
    parts.append(proposed_priority)

  old_sev = sorted(current & SEV_LABELS)
  old_sev_str = old_sev[0] if old_sev else "(none)"
  if old_sev_str != proposed_sev:
    parts.append(f"{old_sev_str} → {proposed_sev}")
  else:
    parts.append(proposed_sev)

  return " | ".join(parts)


def apply_issue_labels(
  number: int,
  add_labels: list[str],
  remove_labels: list[str],
  dry_run: bool = False,
) -> bool:
  """
  Apply label changes to a single issue via gh CLI.
  Returns True on success (or in dry-run mode).
  """
  if dry_run:
    return True  # Caller already printed the change line

  args = ["gh", "issue", "edit", str(number)]
  if add_labels:
    args += ["--add-label", ",".join(add_labels)]
  if remove_labels:
    args += ["--remove-label", ",".join(remove_labels)]

  result = subprocess.run(args, capture_output=True, text=True)
  if result.returncode != 0:
    print(f"  ✗ #{number}: {result.stderr.strip()}", file=sys.stderr)
    return False
  return True


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Apply priority/severity label changes to GitHub issues"
  )
  parser.add_argument("--plan", required=True, help="Path to label plan JSON")
  parser.add_argument(
    "--issues",
    default="/tmp/wc_issues.json",
    help="Path to current issues JSON (default: /tmp/wc_issues.json)",
  )
  parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Show what would change without applying anything",
  )
  args = parser.parse_args()

  # Load inputs
  try:
    current_labels = load_current_labels(args.issues)
  except FileNotFoundError:
    print(f"Error: issues file not found: {args.issues}", file=sys.stderr)
    sys.exit(1)
  except json.JSONDecodeError as e:
    print(f"Error: invalid JSON in {args.issues}: {e}", file=sys.stderr)
    sys.exit(1)

  try:
    with open(args.plan) as f:
      plan = json.load(f)
  except FileNotFoundError:
    print(f"Error: plan file not found: {args.plan}", file=sys.stderr)
    sys.exit(1)
  except json.JSONDecodeError as e:
    print(f"Error: invalid JSON in {args.plan}: {e}", file=sys.stderr)
    sys.exit(1)

  try:
    _validate_plan(plan)
  except ValueError as e:
    print(f"Error: invalid plan — {e}", file=sys.stderr)
    print("No GitHub issues were modified.", file=sys.stderr)
    sys.exit(1)

  prefix = "[dry-run] " if args.dry_run else ""
  print(f"{prefix}Processing {len(plan)} issues...")
  print()

  changed = 0
  skipped = 0
  errors = 0

  for i, entry in enumerate(plan, 1):
    number = entry["number"]
    proposed_priority = entry["priority"]
    proposed_sev = entry["sev"]
    current = current_labels.get(number, set())

    add_labels, remove_labels = compute_diff(current, proposed_priority, proposed_sev)

    if not add_labels and not remove_labels:
      skipped += 1
      continue

    change_str = format_change(current, proposed_priority, proposed_sev)
    line_prefix = "[dry-run] " if args.dry_run else ""
    print(f"  {line_prefix}#{number}: {change_str}")

    success = apply_issue_labels(number, add_labels, remove_labels, dry_run=args.dry_run)
    if success:
      changed += 1
    else:
      errors += 1

    # Batch progress every 10 issues
    if i % 10 == 0:
      print(f"  ... {i}/{len(plan)} processed")

  print()
  action_word = "would be " if args.dry_run else ""
  print(
    f"Done: {changed} issues {action_word}updated, "
    f"{skipped} already correct (no change), "
    f"{errors} errors"
  )


if __name__ == "__main__":
  main()
