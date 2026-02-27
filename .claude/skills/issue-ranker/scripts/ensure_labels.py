#!/usr/bin/env python3
"""
Creates missing priority and severity labels on the current GitHub repo.

Usage:
    python3 ensure_labels.py           # Create missing labels
    python3 ensure_labels.py --dry-run # Show what would be created

Labels managed:
    priority:now     - Critical path, address this sprint
    priority:next    - Address next sprint
    priority:soon    - Address this quarter, not this sprint
    priority:backlog - Tracked but deferred
    sev:critical     - System broken / data loss risk
    sev:high         - Major feature broken
    sev:medium       - Partial impact, workaround available
    sev:low          - Minor / cosmetic impact
"""

import argparse
import json
import subprocess
import sys

LABELS = [
  {
    "name": "priority:now",
    "color": "d93f0b",
    "description": "Critical path — address this sprint",
  },
  {
    "name": "priority:next",
    "color": "e4e669",
    "description": "Address next sprint",
  },
  {
    "name": "priority:soon",
    "color": "fbca04",
    "description": "Should address this quarter, not this sprint",
  },
  {
    "name": "priority:backlog",
    "color": "c2e0c6",
    "description": "Tracked but deferred",
  },
  {
    "name": "sev:critical",
    "color": "b60205",
    "description": "System broken / data loss risk",
  },
  {
    "name": "sev:high",
    "color": "e11d48",
    "description": "Major feature broken",
  },
  {
    "name": "sev:medium",
    "color": "f97316",
    "description": "Partial impact, workaround available",
  },
  {
    "name": "sev:low",
    "color": "fde68a",
    "description": "Minor / cosmetic impact",
  },
]


def get_existing_labels() -> set[str]:
  """Return the set of label names that already exist on the repo."""
  result = subprocess.run(
    ["gh", "label", "list", "--limit", "200", "--json", "name"],
    capture_output=True,
    text=True,
    check=True,
  )
  labels = json.loads(result.stdout)
  return {lbl["name"] for lbl in labels}


def create_label(
  name: str, color: str, description: str, dry_run: bool = False
) -> bool:
  """Create a single label. Returns True on success."""
  if dry_run:
    print(f"  [dry-run] would create: {name} (#{color}) — {description}")
    return True

  result = subprocess.run(
    [
      "gh", "label", "create", name,
      "--color", color,
      "--description", description,
    ],
    capture_output=True,
    text=True,
  )
  if result.returncode == 0:
    print(f"  ✓ created: {name}")
    return True
  else:
    print(f"  ✗ failed to create {name}: {result.stderr.strip()}", file=sys.stderr)
    return False


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Ensure priority/severity labels exist on the GitHub repo"
  )
  parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Show what would be created without doing it",
  )
  args = parser.parse_args()

  try:
    existing = get_existing_labels()
  except subprocess.CalledProcessError as e:
    print(f"Error fetching existing labels: {e.stderr}", file=sys.stderr)
    sys.exit(1)

  print(f"Repo has {len(existing)} existing labels.")
  print()

  created = 0
  skipped = 0

  for label in LABELS:
    if label["name"] in existing:
      print(f"  - skip (exists): {label['name']}")
      skipped += 1
    else:
      success = create_label(
        label["name"], label["color"], label["description"], dry_run=args.dry_run
      )
      if success:
        created += 1

  print()
  prefix = "would be " if args.dry_run else ""
  print(f"Done: {created} {prefix}created, {skipped} already exist.")


if __name__ == "__main__":
  main()
