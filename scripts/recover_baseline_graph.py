#!/usr/bin/env python3
"""Recover baseline graph from markdown files.

Rebuild graph nodes from markdown source files. Use for emergency recovery
when graph data is corrupted, lost, or out of sync with markdown.

WARNING: In normal operation, the graph is the source of truth.
This tool is the exception for recovery scenarios.

Usage:
    # Recover only stale/error threads (auto-detected)
    ./scripts/recover_graph.py /path/to/threads --mode stale

    # Recover specific topics
    ./scripts/recover_graph.py /path/to/threads --mode selective --topics topic-a,topic-b

    # Full rebuild from all markdown (slow, destructive)
    ./scripts/recover_graph.py /path/to/threads --mode all

    # Preview what would be recovered (dry run)
    ./scripts/recover_graph.py /path/to/threads --mode all --dry-run

Requirements:
    - Markdown thread files (.md) must exist
    - For summaries: llama-server at localhost:8000 (auto-starts when configured)
    - For embeddings: llama-server at localhost:8080 (auto-starts when configured)
"""

import argparse
import sys
import time
from pathlib import Path

# Add src to path for local dev
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def find_threads_dir(path: Path) -> Path | None:
    """Resolve threads directory from various input paths."""
    # Direct path with .md files
    if path.is_dir():
        if list(path.glob("*.md")):
            return path
        # Check for graph directory (already has some graph data)
        graph_dir = path / "graph" / "baseline"
        if graph_dir.exists():
            return path

    # Check for .watercooler subdirectory
    watercooler_dir = path / ".watercooler"
    if watercooler_dir.is_dir():
        return watercooler_dir

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Recover baseline graph from markdown files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
    stale       Recover only stale/error threads (auto-detected, default)
    selective   Recover specific topics only
    all         Full rebuild from all markdown (slow, destructive)

Examples:
    # Recover stale threads
    %(prog)s /path/to/threads --mode stale

    # Recover specific topics
    %(prog)s /path/to/threads --mode selective --topics auth,login

    # Full rebuild (preview first)
    %(prog)s /path/to/threads --mode all --dry-run
    %(prog)s /path/to/threads --mode all
        """,
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to threads directory or repo with .watercooler/",
    )
    parser.add_argument(
        "--mode",
        choices=["stale", "selective", "all"],
        default="stale",
        help="Recovery mode (default: stale)",
    )
    parser.add_argument(
        "--topics",
        help="Comma-separated list of topics (required for selective mode)",
    )
    parser.add_argument(
        "--summaries",
        action="store_true",
        default=True,
        help="Generate summaries during recovery (default: yes)",
    )
    parser.add_argument(
        "--no-summaries",
        action="store_false",
        dest="summaries",
        help="Skip summary generation",
    )
    parser.add_argument(
        "--embeddings",
        action="store_true",
        default=True,
        help="Generate embeddings during recovery (default: yes)",
    )
    parser.add_argument(
        "--no-embeddings",
        action="store_false",
        dest="embeddings",
        help="Skip embedding generation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be recovered without making changes",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output",
    )
    args = parser.parse_args()

    # Validate arguments
    if args.mode == "selective" and not args.topics:
        parser.error("--topics is required for selective mode")

    # Resolve threads directory
    if not args.path.exists():
        print(f"Error: Path not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    threads_dir = find_threads_dir(args.path)
    if not threads_dir:
        print(f"Error: Could not find threads directory in {args.path}", file=sys.stderr)
        sys.exit(1)

    print(f"Threads directory: {threads_dir}")

    # Count markdown files
    md_files = list(threads_dir.glob("*.md"))
    print(f"Markdown files found: {len(md_files)}")

    # Parse topics
    topic_list = None
    if args.topics:
        topic_list = [t.strip() for t in args.topics.split(",") if t.strip()]
        print(f"Topics: {', '.join(topic_list)}")

    print(f"Mode: {args.mode}")
    print(f"Generate summaries: {'yes' if args.summaries else 'no'}")
    print(f"Generate embeddings: {'yes' if args.embeddings else 'no'}")
    if args.dry_run:
        print("DRY RUN - no changes will be made")
    print()

    if args.mode == "all" and not args.dry_run:
        print("WARNING: Full rebuild will regenerate all graph data from markdown.")
        response = input("Continue? [y/N] ")
        if response.lower() != "y":
            print("Aborted.")
            sys.exit(0)
        print()

    # Import after path setup
    from watercooler.baseline_graph.sync import recover_graph

    # Run recovery
    print("Starting recovery...")
    start_time = time.time()

    result = recover_graph(
        threads_dir=threads_dir,
        mode=args.mode,
        topics=topic_list,
        generate_summaries=args.summaries,
        generate_embeddings=args.embeddings,
        dry_run=args.dry_run,
    )

    elapsed = time.time() - start_time

    # Print results
    print()
    if args.dry_run:
        print("=== DRY RUN RESULTS ===")
        print(f"Would recover {result.threads_recovered} threads")
        print(f"Would parse {result.entries_parsed} entries")
        if args.summaries:
            print(f"Would generate ~{result.summaries_generated} summaries")
        if args.embeddings:
            print(f"Would generate ~{result.embeddings_generated} embeddings")
    else:
        print("=== RESULTS ===")
        print(f"Recovered {result.threads_recovered} threads")
        print(f"Parsed {result.entries_parsed} entries")
        print(f"Elapsed: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)")

    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors[:10]:
            print(f"  - {err}")
        if len(result.errors) > 10:
            print(f"  ... and {len(result.errors) - 10} more")
        sys.exit(1)

    print("\nRecovery complete!")


if __name__ == "__main__":
    main()
