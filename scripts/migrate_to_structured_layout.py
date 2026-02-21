#!/usr/bin/env python3
"""Migrate existing watercooler thread repos to the structured directory layout.

Moves root-level .md thread files into the threads/ subdirectory and creates
the full category hierarchy (threads/, reference/, debug/, closed/, etc.).

This is a one-shot script for the 3 existing repos. Delete after migration.

Usage:
    # Dry run — show what would happen
    ./scripts/migrate_to_structured_layout.py ~/.watercooler/worktrees/watercooler-cloud --dry-run

    # Migrate a single worktree
    ./scripts/migrate_to_structured_layout.py ~/.watercooler/worktrees/watercooler-cloud

    # Migrate all worktrees
    ./scripts/migrate_to_structured_layout.py --all
"""

import argparse
import sys
from pathlib import Path

# Add src/ to path so we can import watercooler
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from watercooler.fs import (
    ensure_directory_structure,
    has_structured_layout,
    migrate_to_structured_layout,
    _DIRECTORY_STRUCTURE,
)


WORKTREE_BASE = Path("~/.watercooler/worktrees").expanduser()


def _discover_worktrees() -> list[Path]:
    """Find all watercooler worktree directories."""
    if not WORKTREE_BASE.is_dir():
        return []
    return sorted(
        p for p in WORKTREE_BASE.iterdir()
        if p.is_dir() and (p / ".git").exists()
    )


def _migrate_one(threads_dir: Path, dry_run: bool = False) -> bool:
    """Migrate a single threads directory. Returns True if changes were made."""
    name = threads_dir.name
    already = has_structured_layout(threads_dir)

    if already and not dry_run:
        # Already structured — just ensure full hierarchy
        created = ensure_directory_structure(threads_dir)
        if created:
            print(f"  [{name}] Created {len(created)} missing directories")
        else:
            print(f"  [{name}] Already fully structured, nothing to do")
        return bool(created)

    # Count root .md files that would move
    root_mds = [
        p for p in threads_dir.glob("*.md")
        if p.is_file() and not p.name.startswith((".", "_"))
    ]

    if dry_run:
        print(f"  [{name}] {'Already structured' if already else 'Flat layout'}")
        print(f"    Would create directories: {len(_DIRECTORY_STRUCTURE)}")
        print(f"    Would move {len(root_mds)} root .md files to threads/")
        for p in root_mds:
            print(f"      {p.name} -> threads/{p.name}")
        return False

    # Do the migration
    moved = migrate_to_structured_layout(threads_dir)
    print(f"  [{name}] Migrated: {len(moved)} files moved to threads/")
    for old, new in moved:
        print(f"    {old.name} -> {new.relative_to(threads_dir)}")
    return bool(moved)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate watercooler thread repos to structured directory layout"
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        help="Path to a threads worktree directory",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"Migrate all worktrees in {WORKTREE_BASE}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )
    args = parser.parse_args()

    if not args.path and not args.all:
        parser.error("Provide a path or use --all")

    if args.dry_run:
        print("=== DRY RUN ===\n")

    targets: list[Path] = []
    if args.all:
        targets = _discover_worktrees()
        if not targets:
            print(f"No worktrees found in {WORKTREE_BASE}")
            return 1
        print(f"Found {len(targets)} worktrees:\n")
    else:
        if not args.path.is_dir():
            print(f"Error: {args.path} is not a directory", file=sys.stderr)
            return 1
        targets = [args.path]

    changed = 0
    for t in targets:
        if _migrate_one(t, dry_run=args.dry_run):
            changed += 1

    if not args.dry_run:
        print(f"\nDone. {changed}/{len(targets)} repos modified.")
        if changed:
            print("\nNext steps:")
            print("  1. cd into each worktree and commit:")
            print("     git add -A && git commit -m 'chore: migrate to structured directory layout'")
            print("  2. Push: git push")
            print("  3. Delete this script when all 3 repos are migrated")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
