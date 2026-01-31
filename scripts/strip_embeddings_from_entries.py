#!/usr/bin/env python3
"""Strip embedding fields from entries.jsonl files.

After migrating embeddings to FalkorDB, this script removes the legacy
embedding fields from entries.jsonl files to reduce file size.

Usage:
    # Dry run (show what would be changed)
    python scripts/strip_embeddings_from_entries.py --code-path /path/to/repo --dry-run

    # Execute cleanup
    python scripts/strip_embeddings_from_entries.py --code-path /path/to/repo

    # Keep backup files (.bak)
    python scripts/strip_embeddings_from_entries.py --code-path /path/to/repo --backup
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CleanupStats:
    """Cleanup statistics."""
    files_processed: int = 0
    entries_processed: int = 0
    entries_with_embedding: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    files_unchanged: int = 0


def setup_logging(verbose: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_threads_dir(code_path: Path) -> Path:
    """Get threads directory from code path."""
    for threads_name in ["threads", ".threads"]:
        threads_dir = code_path / threads_name
        if threads_dir.exists():
            return threads_dir
    return code_path


def get_graph_dir(threads_dir: Path) -> Path:
    """Get graph directory from threads directory."""
    return threads_dir / "graph" / "baseline"


def find_entries_files(graph_dir: Path) -> list[Path]:
    """Find all entries.jsonl files in per-thread format."""
    threads_dir = graph_dir / "threads"
    if not threads_dir.exists():
        return []

    entries_files = []
    for topic_dir in threads_dir.iterdir():
        if topic_dir.is_dir():
            entries_file = topic_dir / "entries.jsonl"
            if entries_file.exists():
                entries_files.append(entries_file)

    return sorted(entries_files)


def strip_embeddings_from_file(
    entries_file: Path,
    dry_run: bool = False,
    backup: bool = False,
) -> tuple[int, int, int, int]:
    """Strip embedding fields from a single entries.jsonl file.

    Returns:
        Tuple of (entries_processed, entries_with_embedding, bytes_before, bytes_after)
    """
    bytes_before = entries_file.stat().st_size

    # Read all entries
    entries = []
    entries_with_embedding = 0

    with open(entries_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    if "embedding" in entry:
                        entries_with_embedding += 1
                        if not dry_run:
                            del entry["embedding"]
                    entries.append(entry)
                except json.JSONDecodeError:
                    # Keep malformed lines as-is
                    entries.append(line.rstrip("\n"))

    if entries_with_embedding == 0:
        # No changes needed
        return len(entries), 0, bytes_before, bytes_before

    if dry_run:
        # Estimate size reduction (embedding is ~21KB per entry)
        estimated_after = bytes_before - (entries_with_embedding * 21000)
        return len(entries), entries_with_embedding, bytes_before, max(estimated_after, 1000)

    # Create backup if requested
    if backup:
        backup_file = entries_file.with_suffix(".jsonl.bak")
        entries_file.rename(backup_file)
        logging.debug(f"Created backup: {backup_file}")

    # Write cleaned entries
    with open(entries_file, "w", encoding="utf-8") as f:
        for entry in entries:
            if isinstance(entry, dict):
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            else:
                f.write(entry + "\n")

    bytes_after = entries_file.stat().st_size

    return len(entries), entries_with_embedding, bytes_before, bytes_after


def format_size(bytes_val: int) -> str:
    """Format bytes as human-readable size."""
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    else:
        return f"{bytes_val / (1024 * 1024):.1f} MB"


def run_cleanup(
    threads_dir: Path,
    dry_run: bool = False,
    backup: bool = False,
) -> CleanupStats:
    """Run the cleanup.

    Args:
        threads_dir: Path to threads directory
        dry_run: If True, don't actually modify files
        backup: If True, create .bak files before modifying

    Returns:
        Cleanup statistics
    """
    stats = CleanupStats()
    graph_dir = get_graph_dir(threads_dir)

    if not graph_dir.exists():
        logging.error(f"Graph directory not found: {graph_dir}")
        return stats

    entries_files = find_entries_files(graph_dir)

    if not entries_files:
        logging.warning("No entries.jsonl files found")
        return stats

    logging.info(f"Found {len(entries_files)} entries.jsonl files")

    if dry_run:
        logging.info("DRY RUN - no files will be modified")

    for entries_file in entries_files:
        topic = entries_file.parent.name

        entries_count, with_embedding, before, after = strip_embeddings_from_file(
            entries_file, dry_run, backup
        )

        stats.files_processed += 1
        stats.entries_processed += entries_count
        stats.entries_with_embedding += with_embedding
        stats.bytes_before += before
        stats.bytes_after += after

        if with_embedding > 0:
            saved = before - after
            logging.info(
                f"{topic}: {with_embedding}/{entries_count} entries cleaned, "
                f"saved {format_size(saved)}"
            )
        else:
            stats.files_unchanged += 1
            logging.debug(f"{topic}: no embeddings found")

    return stats


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Strip embedding fields from entries.jsonl files"
    )
    parser.add_argument(
        "--code-path",
        type=Path,
        required=True,
        help="Path to the code/threads repository",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying files",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create .bak backup files before modifying",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Resolve paths
    code_path = args.code_path.resolve()
    if not code_path.exists():
        logging.error(f"Path does not exist: {code_path}")
        sys.exit(1)

    threads_dir = get_threads_dir(code_path)
    if not threads_dir.exists():
        logging.error(f"Threads directory not found: {threads_dir}")
        sys.exit(1)

    logging.info(f"Threads dir: {threads_dir}")

    # Run cleanup
    stats = run_cleanup(threads_dir, args.dry_run, args.backup)

    # Print summary
    saved = stats.bytes_before - stats.bytes_after
    pct = (saved / stats.bytes_before * 100) if stats.bytes_before > 0 else 0

    print("\n" + "=" * 50)
    print("Cleanup Summary")
    print("=" * 50)
    print(f"Files processed:        {stats.files_processed}")
    print(f"Files unchanged:        {stats.files_unchanged}")
    print(f"Entries processed:      {stats.entries_processed}")
    print(f"Entries with embedding: {stats.entries_with_embedding}")
    print(f"Size before:            {format_size(stats.bytes_before)}")
    print(f"Size after:             {format_size(stats.bytes_after)}")
    print(f"Space saved:            {format_size(saved)} ({pct:.1f}%)")
    print("=" * 50)


if __name__ == "__main__":
    main()
