#!/usr/bin/env python3
"""Migrate legacy watercooler threads to per-thread graph format.

Handles migration from:
  - Legacy .md thread files
  - Monolithic graph format (nodes.jsonl + edges.jsonl)

To per-thread format:
  graph/baseline/threads/{topic}/
      meta.json       # Thread metadata
      entries.jsonl   # Entry nodes
      edges.jsonl     # Thread-local edges

Usage:
    # Migrate a threads repo
    ./scripts/migrate_to_per_thread.py /path/to/project-threads

    # Migrate with options
    ./scripts/migrate_to_per_thread.py /path/to/project-threads --keep-monolithic
    ./scripts/migrate_to_per_thread.py /path/to/project-threads --dry-run

    # Migrate threads in a code repo's .watercooler directory
    ./scripts/migrate_to_per_thread.py /path/to/project
"""

import argparse
import sys
from pathlib import Path
from typing import Any, TypedDict

# Add src to path for local dev
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class ThreadsState(TypedDict):
    """State of a threads directory."""

    has_md_files: bool
    md_count: int
    has_monolithic: bool
    has_per_thread: bool
    per_thread_count: int
    graph_dir: Path


def find_threads_dir(path: Path) -> Path | None:
    """Resolve threads directory from various input paths.

    Accepts:
      - Direct path to threads directory (contains .md files)
      - Path to repo with .watercooler/ subdirectory
      - Path to -threads repo root

    Returns:
        Resolved threads directory path, or None if not found
    """
    # Direct path with .md files
    if path.is_dir() and list(path.glob("*.md")):
        return path

    # Check for .watercooler subdirectory
    watercooler_dir = path / ".watercooler"
    if watercooler_dir.is_dir() and list(watercooler_dir.glob("*.md")):
        return watercooler_dir

    # Check if it's a -threads repo with threads at root
    if path.is_dir():
        # Look for graph directory (already has some graph data)
        graph_dir = path / "graph" / "baseline"
        if graph_dir.exists():
            return path
        # Look for any .md files
        if list(path.glob("*.md")):
            return path

    return None


def check_current_state(threads_dir: Path) -> ThreadsState:
    """Check the current state of the threads directory.

    Args:
        threads_dir: Path to the threads directory

    Returns:
        ThreadsState with migration status info
    """
    graph_dir = threads_dir / "graph" / "baseline"
    nodes_file = graph_dir / "nodes.jsonl"
    threads_base = graph_dir / "threads"

    md_files = list(threads_dir.glob("*.md"))

    # Check for per-thread format
    has_per_thread = False
    per_thread_count = 0
    if threads_base.exists():
        for topic_dir in threads_base.iterdir():
            if topic_dir.is_dir() and (topic_dir / "meta.json").exists():
                has_per_thread = True
                per_thread_count += 1

    return {
        "has_md_files": len(md_files) > 0,
        "md_count": len(md_files),
        "has_monolithic": nodes_file.exists(),
        "has_per_thread": has_per_thread,
        "per_thread_count": per_thread_count,
        "graph_dir": graph_dir,
    }


def build_monolithic_from_md(threads_dir: Path, extractive_only: bool = True) -> dict[str, Any]:
    """Build monolithic graph from .md files.

    Args:
        threads_dir: Threads directory containing .md files
        extractive_only: Use extractive summaries (no LLM required)

    Returns:
        Export manifest with statistics (threads_exported, nodes_written, edges_written)
    """
    from watercooler.baseline_graph import export_all_threads, SummarizerConfig

    output_dir = threads_dir / "graph" / "baseline"
    config = SummarizerConfig(prefer_extractive=extractive_only)

    return export_all_threads(
        threads_dir,
        output_dir,
        config,
        skip_closed=False,
        generate_embeddings=False,
    )


def migrate_monolithic_to_per_thread(
    threads_dir: Path,
    delete_monolithic: bool = True,
) -> "MigrationResult":
    """Migrate from monolithic to per-thread format.

    Args:
        threads_dir: Threads directory
        delete_monolithic: Delete old files after migration

    Returns:
        MigrationResult with counts and errors
    """
    from watercooler.baseline_graph.sync import migrate_to_per_thread_format

    return migrate_to_per_thread_format(
        threads_dir,
        delete_monolithic=delete_monolithic,
        build_search_index=True,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Migrate legacy watercooler threads to per-thread graph format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Basic migration
    %(prog)s /path/to/myproject-threads

    # Preview what would happen
    %(prog)s /path/to/myproject-threads --dry-run

    # Keep old monolithic files as backup
    %(prog)s /path/to/myproject-threads --keep-monolithic

    # Migrate threads in a code repo
    %(prog)s /path/to/myproject  # looks for .watercooler/
        """,
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to threads directory, -threads repo, or code repo with .watercooler/",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--keep-monolithic",
        action="store_true",
        help="Keep monolithic files (nodes.jsonl, edges.jsonl) after migration",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Use LLM for summaries (requires local llama-server or compatible API)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output",
    )
    args = parser.parse_args()

    # Resolve threads directory
    if not args.path.exists():
        print(f"Error: Path not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    threads_dir = find_threads_dir(args.path)
    if not threads_dir:
        print(f"Error: Could not find threads directory in {args.path}", file=sys.stderr)
        print("Expected: directory with .md files, or repo with .watercooler/", file=sys.stderr)
        sys.exit(1)

    print(f"Threads directory: {threads_dir}")
    print()

    # Check current state
    state = check_current_state(threads_dir)

    print("Current state:")
    print(f"  .md thread files: {state['md_count']}")
    print(f"  Monolithic graph: {'yes' if state['has_monolithic'] else 'no'}")
    print(f"  Per-thread format: {'yes' if state['has_per_thread'] else 'no'} ({state['per_thread_count']} threads)")
    print()

    # Determine what needs to be done
    needs_md_build = state["has_md_files"] and not state["has_monolithic"] and not state["has_per_thread"]
    needs_migration = state["has_monolithic"] and not state["has_per_thread"]

    if state["has_per_thread"] and not state["has_monolithic"] and not needs_md_build:
        print("Already migrated to per-thread format. Nothing to do.")
        sys.exit(0)

    if not state["has_md_files"] and not state["has_monolithic"]:
        print("Error: No .md files or monolithic graph found. Nothing to migrate.", file=sys.stderr)
        sys.exit(1)

    # Plan
    print("Migration plan:")
    if needs_md_build:
        print(f"  1. Build monolithic graph from {state['md_count']} .md files")
        print("  2. Migrate to per-thread format")
    elif needs_migration:
        print("  1. Migrate monolithic graph to per-thread format")
    else:
        print("  (no migration needed)")
        sys.exit(0)

    if not args.keep_monolithic:
        print("  3. Delete monolithic files (use --keep-monolithic to preserve)")
    print()

    if args.dry_run:
        print("Dry run - no changes made.")
        sys.exit(0)

    # Execute migration
    try:
        # Step 1: Build from .md if needed
        if needs_md_build:
            print("Building monolithic graph from .md files...")
            manifest = build_monolithic_from_md(
                threads_dir,
                extractive_only=not args.use_llm,
            )
            print(f"  Exported {manifest.get('threads_exported', 0)} threads")
            print(f"  Generated {manifest.get('nodes_written', 0)} nodes, {manifest.get('edges_written', 0)} edges")
            print()

        # Step 2: Migrate to per-thread
        print("Migrating to per-thread format...")
        result = migrate_monolithic_to_per_thread(
            threads_dir,
            delete_monolithic=not args.keep_monolithic,
        )

        print(f"  Threads migrated: {result.threads_migrated}")
        print(f"  Entries migrated: {result.entries_migrated}")
        print(f"  Edges migrated: {result.edges_migrated}")
        if result.search_index_entries:
            print(f"  Search index entries: {result.search_index_entries}")
        if result.monolithic_deleted:
            print("  Monolithic files deleted")
        print()

        if result.errors:
            print("Warnings/Errors:", file=sys.stderr)
            for err in result.errors:
                print(f"  - {err}", file=sys.stderr)
            print()

        # Final state
        final_state = check_current_state(threads_dir)
        print("Final state:")
        print(f"  Per-thread format: {final_state['per_thread_count']} threads")
        print(f"  Location: {final_state['graph_dir'] / 'threads'}")
        print()
        print("Migration complete!")

    except Exception as e:
        print(f"Error during migration: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
