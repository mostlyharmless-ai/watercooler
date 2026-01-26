#!/usr/bin/env python3
"""Enrich baseline graph with summaries and embeddings.

Generate or regenerate LLM summaries and embedding vectors for graph entries.
Supports mode-based control for targeted or full enrichment.

Usage:
    # Fill missing embeddings only (safe default)
    ./scripts/enrich_graph.py /path/to/threads --mode missing --embeddings

    # Regenerate embeddings for specific topics
    ./scripts/enrich_graph.py /path/to/threads --mode selective --topics topic-a,topic-b --embeddings

    # Full refresh of all embeddings (use with caution)
    ./scripts/enrich_graph.py /path/to/threads --mode all --embeddings

    # Preview what would be processed (dry run)
    ./scripts/enrich_graph.py /path/to/threads --mode all --embeddings --dry-run

Requirements:
    - For summaries: Ollama or compatible LLM server at localhost:11434
    - For embeddings: llama.cpp server or compatible at localhost:8080
"""

import argparse
import sys
import time
from pathlib import Path

# Add src to path for local dev
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def find_threads_dir(path: Path) -> Path | None:
    """Resolve threads directory from various input paths."""
    # Direct path with .md files or graph directory
    if path.is_dir():
        # Check for graph/baseline/threads (per-thread format)
        graph_threads = path / "graph" / "baseline" / "threads"
        if graph_threads.exists():
            return path
        # Check for .md files
        if list(path.glob("*.md")):
            return path

    # Check for .watercooler subdirectory
    watercooler_dir = path / ".watercooler"
    if watercooler_dir.is_dir():
        return watercooler_dir

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Enrich baseline graph with summaries and embeddings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
    missing     Only fill entries with missing values (default, safe)
    selective   Process only specified topics (force regenerate)
    all         Regenerate everything (global refresh, use with caution)

Examples:
    # Fill missing embeddings
    %(prog)s /path/to/threads --mode missing --embeddings

    # Regenerate specific topics
    %(prog)s /path/to/threads --mode selective --topics auth,login --embeddings

    # Full refresh (preview first)
    %(prog)s /path/to/threads --mode all --embeddings --dry-run
    %(prog)s /path/to/threads --mode all --embeddings
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
        help="Processing mode (default: missing)",
    )
    parser.add_argument(
        "--topics",
        help="Comma-separated list of topics (required for selective mode)",
    )
    parser.add_argument(
        "--summaries",
        action="store_true",
        help="Generate/regenerate LLM summaries",
    )
    parser.add_argument(
        "--embeddings",
        action="store_true",
        help="Generate/regenerate embedding vectors",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Batch size for processing (default: 10)",
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
    args = parser.parse_args()

    # Validate arguments
    if not args.summaries and not args.embeddings:
        parser.error("At least one of --summaries or --embeddings is required")

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

    # Parse topics
    topic_list = None
    if args.topics:
        topic_list = [t.strip() for t in args.topics.split(",") if t.strip()]
        print(f"Topics: {', '.join(topic_list)}")

    print(f"Mode: {args.mode}")
    print(f"Summaries: {'yes' if args.summaries else 'no'}")
    print(f"Embeddings: {'yes' if args.embeddings else 'no'}")
    if args.dry_run:
        print("DRY RUN - no changes will be made")
    print()

    # Import after path setup
    from watercooler.baseline_graph.sync import enrich_graph

    # Run enrichment
    print("Starting enrichment...")
    start_time = time.time()

    result = enrich_graph(
        threads_dir=threads_dir,
        summaries=args.summaries,
        embeddings=args.embeddings,
        mode=args.mode,
        topics=topic_list,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )

    elapsed = time.time() - start_time

    # Print results
    print()
    if args.dry_run:
        print("=== DRY RUN RESULTS ===")
        print(f"Would process {result.threads_processed} threads, {result.entries_processed} entries")
        if args.summaries:
            print(f"Would generate {result.summaries_generated} summaries")
        if args.embeddings:
            print(f"Would generate {result.embeddings_generated} embeddings")
        print(f"Would skip {result.skipped} entries (already have values)")
    else:
        print("=== RESULTS ===")
        print(f"Processed {result.threads_processed} threads, {result.entries_processed} entries")
        if args.summaries:
            print(f"Generated {result.summaries_generated} summaries")
        if args.embeddings:
            print(f"Generated {result.embeddings_generated} embeddings")
        print(f"Skipped {result.skipped} entries")
        print(f"Elapsed: {elapsed:.1f} seconds ({elapsed/60:.1f} minutes)")

    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors[:10]:
            print(f"  - {err}")
        if len(result.errors) > 10:
            print(f"  ... and {len(result.errors) - 10} more")
        sys.exit(1)

    print("\nEnrichment complete!")


if __name__ == "__main__":
    main()
