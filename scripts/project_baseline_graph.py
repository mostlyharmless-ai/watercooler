#!/usr/bin/env python3
"""Project markdown files from baseline graph.

Generate markdown thread files from graph data (source of truth).
Supports mode-based control for targeted or full projection.

Usage:
    # Create markdown for topics missing .md files (safe default)
    ./scripts/project_graph.py /path/to/threads --mode missing

    # Project specific topics
    ./scripts/project_graph.py /path/to/threads --mode selective --topics topic-a,topic-b

    # Full regeneration of all markdown (requires --overwrite)
    ./scripts/project_graph.py /path/to/threads --mode all --overwrite

    # Preview what would be processed (dry run)
    ./scripts/project_graph.py /path/to/threads --mode all --overwrite --dry-run

Use cases:
    - Initial markdown generation after graph import
    - Regenerating corrupted markdown files
    - Syncing after direct graph edits
"""

import argparse
import sys
import time
from pathlib import Path

# Add src to path for local dev
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def find_threads_dir(path: Path) -> Path | None:
    """Resolve threads directory from various input paths."""
    # Direct path with graph directory
    if path.is_dir():
        # Check for graph/baseline/threads (per-thread format)
        graph_threads = path / "graph" / "baseline" / "threads"
        if graph_threads.exists():
            return path
        # Check for .md files (existing markdown)
        if list(path.glob("*.md")):
            return path

    # Check for .watercooler subdirectory
    watercooler_dir = path / ".watercooler"
    if watercooler_dir.is_dir():
        return watercooler_dir

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Project markdown files from baseline graph",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
    missing     Only create markdown for topics without .md files (default)
    selective   Project specific topics only
    all         Regenerate all markdown (requires --overwrite)

Examples:
    # Create missing markdown files
    %(prog)s /path/to/threads --mode missing

    # Project specific topics
    %(prog)s /path/to/threads --mode selective --topics auth,login

    # Full regeneration (preview first)
    %(prog)s /path/to/threads --mode all --overwrite --dry-run
    %(prog)s /path/to/threads --mode all --overwrite

    # Project and sync to remote
    %(prog)s /path/to/threads --mode missing --sync
        """,
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to threads directory or repo with .watercooler/",
    )
    parser.add_argument(
        "--mode",
        choices=["missing", "selective", "all"],
        default="missing",
        help="Projection mode (default: missing)",
    )
    parser.add_argument(
        "--topics",
        help="Comma-separated list of topics (required for selective mode)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing markdown files (required for 'all' mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be processed without making changes",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Commit and push changes to remote after projection completes",
    )
    args = parser.parse_args()

    # Validate arguments
    if args.mode == "selective" and not args.topics:
        parser.error("--topics is required for selective mode")

    if args.mode == "all" and not args.overwrite and not args.dry_run:
        parser.error("--overwrite is required for 'all' mode (or use --dry-run to preview)")

    # Resolve threads directory
    if not args.path.exists():
        print(f"Error: Path not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    threads_dir = find_threads_dir(args.path)
    if not threads_dir:
        print(f"Error: Could not find threads directory in {args.path}", file=sys.stderr)
        sys.exit(1)

    print(f"Threads directory: {threads_dir}")

    # Check for graph data
    graph_threads = threads_dir / "graph" / "baseline" / "threads"
    if not graph_threads.exists():
        print(f"Error: No graph data found at {graph_threads}", file=sys.stderr)
        sys.exit(1)

    # Count graph topics
    graph_topics = [d.name for d in graph_threads.iterdir() if d.is_dir()]
    print(f"Graph topics found: {len(graph_topics)}")

    # Parse topics
    topic_list = None
    if args.topics:
        topic_list = [t.strip() for t in args.topics.split(",") if t.strip()]
        print(f"Topics: {', '.join(topic_list)}")

    print(f"Mode: {args.mode}")
    print(f"Overwrite: {'yes' if args.overwrite else 'no'}")
    if args.dry_run:
        print("DRY RUN - no changes will be made")
    print()

    if args.mode == "all" and args.overwrite and not args.dry_run:
        print("WARNING: Full projection will overwrite all existing markdown files.")
        response = input("Continue? [y/N] ")
        if response.lower() != "y":
            print("Aborted.")
            sys.exit(0)
        print()

    # Import after path setup
    from watercooler.baseline_graph.projector import project_graph

    # Run projection
    print("Starting projection...")
    start_time = time.time()

    result = project_graph(
        threads_dir=threads_dir,
        mode=args.mode,
        topics=topic_list,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    elapsed = time.time() - start_time

    # Print results
    print()
    if args.dry_run:
        print("=== DRY RUN RESULTS ===")
        print(f"Would create {result.files_created} files")
        print(f"Would update {result.files_updated} files")
        print(f"Would skip {result.files_skipped} files")
    else:
        print("=== RESULTS ===")
        print(f"Created {result.files_created} files")
        print(f"Updated {result.files_updated} files")
        print(f"Skipped {result.files_skipped} files")
        print(f"Elapsed: {elapsed:.1f} seconds")

    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors[:10]:
            print(f"  - {err}")
        if len(result.errors) > 10:
            print(f"  ... and {len(result.errors) - 10} more")
        sys.exit(1)

    print("\nProjection complete!")

    # Sync to remote if requested
    if args.sync and not args.dry_run:
        print("\n=== SYNCING TO REMOTE ===")
        try:
            from watercooler_mcp.sync import LocalRemoteSyncManager

            manager = LocalRemoteSyncManager(threads_dir)

            commit_msg = f"chore(baseline): project graph ({result.files_created} created, {result.files_updated} updated)"
            sync_result = manager.commit_and_push(
                message=commit_msg,
                all_changes=True,
            )

            if sync_result.success:
                if sync_result.commit_result and sync_result.commit_result.sha:
                    print(f"Committed: {sync_result.commit_result.sha[:8]}")
                if sync_result.push_result and sync_result.push_result.commits_pushed:
                    print(f"Pushed {sync_result.push_result.commits_pushed} commit(s) to remote")
                else:
                    print("No changes to push (already synced)")
            else:
                error = ""
                if sync_result.commit_result and sync_result.commit_result.error:
                    error = sync_result.commit_result.error
                elif sync_result.push_result and sync_result.push_result.error:
                    error = sync_result.push_result.error
                print(f"Sync failed: {error}", file=sys.stderr)
                sys.exit(1)
        except ImportError as e:
            print(f"Sync unavailable (missing dependency): {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Sync error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
