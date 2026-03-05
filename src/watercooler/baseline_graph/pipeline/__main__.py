#!/usr/bin/env python3
"""CLI for baseline graph pipeline.

Usage:
    python -m watercooler.baseline_graph.pipeline run --threads /path/to/threads
    python -m watercooler.baseline_graph.pipeline run --threads /path/to/threads --test-limit 3 -y
    python -m watercooler.baseline_graph.pipeline run --threads /path/to/threads --fresh --extractive
"""

import argparse
import sys
from pathlib import Path

from .runner import run_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baseline graph construction pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build graph from threads with LLM summarization and embeddings
  python -m watercooler.baseline_graph.pipeline run --threads /path/to/threads

  # Test with 3 threads, auto-approve server prompts
  python -m watercooler.baseline_graph.pipeline run --threads /path/to/threads --test-limit 3 -y

  # Fresh build with extractive summaries only (no LLM)
  python -m watercooler.baseline_graph.pipeline run --threads /path/to/threads --fresh --extractive

  # Incremental build (only process changed threads)
  python -m watercooler.baseline_graph.pipeline run --threads /path/to/threads --incremental

  # Skip embeddings (summaries only)
  python -m watercooler.baseline_graph.pipeline run --threads /path/to/threads --skip-embeddings
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run pipeline")
    run_parser.add_argument("--threads", "-t", required=True, help="Path to threads directory")
    run_parser.add_argument("--output", "-o", help="Output directory (default: {threads}/graph/baseline)")
    run_parser.add_argument("--test-limit", type=int, help="Limit to N threads (test mode)")
    run_parser.add_argument("--fresh", action="store_true", help="Clear cached results before running")
    run_parser.add_argument("--incremental", "-i", action="store_true", help="Only process changed threads")
    run_parser.add_argument("--extractive", action="store_true", help="Use extractive summarization (no LLM)")
    run_parser.add_argument("--skip-embeddings", action="store_true", help="Skip embedding generation")
    run_parser.add_argument("--skip-closed", action="store_true", help="Skip closed threads")
    run_parser.add_argument("--no-auto-server", action="store_true", help="Don't auto-start servers")
    run_parser.add_argument("--stop-servers", action="store_true", help="Stop servers when complete")
    run_parser.add_argument("-y", "--yes", action="store_true", help="Auto-approve all prompts")
    run_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "run":
        threads_dir = Path(args.threads)
        if not threads_dir.exists():
            print(f"Error: Threads directory not found: {threads_dir}", file=sys.stderr)
            return 1

        output_dir = Path(args.output) if args.output else None

        print(f"Baseline Graph Pipeline")
        print(f"{'=' * 40}")
        print(f"Threads: {threads_dir}")
        if args.test_limit:
            print(f"Test mode: {args.test_limit} threads")
        if args.fresh:
            print(f"Fresh: clearing cached results")
        if args.incremental:
            print(f"Incremental: only processing changed threads")
        print()

        result = run_pipeline(
            threads_dir=threads_dir,
            output_dir=output_dir,
            test_limit=args.test_limit,
            fresh=args.fresh,
            incremental=args.incremental,
            extractive_only=args.extractive,
            skip_embeddings=args.skip_embeddings,
            skip_closed=args.skip_closed,
            verbose=args.verbose,
            auto_server=not args.no_auto_server,
            stop_servers=args.stop_servers,
            auto_approve=args.yes,
        )

        print()
        print(f"{'=' * 40}")
        print(f"Results:")
        print(f"  Threads processed:    {result.threads_processed}")
        print(f"  Entries processed:    {result.entries_processed}")
        print(f"  Nodes created:        {result.nodes_created}")
        print(f"  Edges created:        {result.edges_created}")
        print(f"  Embeddings generated: {result.embeddings_generated}")
        print(f"  Duration:             {result.duration_seconds:.1f}s")
        print(f"  Output:               {result.output_dir}")

        if result.error:
            print(f"\nError: {result.error}", file=sys.stderr)
            return 1

        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
