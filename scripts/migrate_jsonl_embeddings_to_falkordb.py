#!/usr/bin/env python3
"""Migrate embeddings from entries.jsonl files to FalkorDB.

This script reads embeddings from legacy JSONL storage and migrates them
to FalkorDB vector storage, preserving the entry associations.

Usage:
    # Dry run (show what would be migrated)
    python scripts/migrate_jsonl_embeddings_to_falkordb.py --code-path /path/to/repo --dry-run

    # Execute migration
    python scripts/migrate_jsonl_embeddings_to_falkordb.py --code-path /path/to/repo
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from watercooler.path_resolver import derive_group_id as _derive_group_id


@dataclass
class MigrationStats:
    """Migration statistics."""
    files_processed: int = 0
    entries_processed: int = 0
    entries_with_embedding: int = 0
    entries_migrated: int = 0
    entries_skipped: int = 0  # Already in FalkorDB
    entries_failed: int = 0


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
    # Check for paired repo pattern first (code_path IS the threads dir)
    if code_path.name.endswith("-threads"):
        return code_path
    # Then check for embedded threads dirs
    for threads_name in ["threads", ".threads"]:
        threads_dir = code_path / threads_name
        if threads_dir.exists():
            return threads_dir
    return code_path


def get_graph_dir(threads_dir: Path) -> Path:
    """Get graph directory from threads directory."""
    return threads_dir / "graph" / "baseline"


def derive_group_id(threads_dir: Path) -> str:
    """Derive group_id from threads directory path.

    Delegates to unified function in watercooler.path_resolver.
    """
    return _derive_group_id(threads_dir=threads_dir)


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


def run_migration(
    threads_dir: Path,
    dry_run: bool = False,
) -> MigrationStats:
    """Run the migration.

    Args:
        threads_dir: Path to threads directory
        dry_run: If True, don't actually migrate

    Returns:
        Migration statistics
    """
    # Import here to avoid import errors if FalkorDB not available
    from watercooler.baseline_graph.falkordb_entries import get_falkordb_entry_store

    stats = MigrationStats()
    graph_dir = get_graph_dir(threads_dir)
    group_id = derive_group_id(threads_dir)

    logging.info(f"Threads dir: {threads_dir}")
    logging.info(f"Group ID: {group_id}")

    if not graph_dir.exists():
        logging.error(f"Graph directory not found: {graph_dir}")
        return stats

    # Get FalkorDB store
    store = get_falkordb_entry_store(group_id)
    if store is None:
        logging.error("FalkorDB not available")
        return stats

    entries_files = find_entries_files(graph_dir)

    if not entries_files:
        logging.warning("No entries.jsonl files found")
        return stats

    logging.info(f"Found {len(entries_files)} entries.jsonl files")

    if dry_run:
        logging.info("DRY RUN - no data will be migrated")

    for entries_file in entries_files:
        topic = entries_file.parent.name
        topic_migrated = 0
        topic_skipped = 0

        with open(entries_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    stats.entries_processed += 1

                    embedding = entry.get("embedding")
                    if not embedding:
                        continue

                    stats.entries_with_embedding += 1
                    entry_id = entry.get("entry_id", "")

                    if not entry_id:
                        logging.warning(f"Entry without entry_id in {topic}")
                        stats.entries_failed += 1
                        continue

                    # Check if already in FalkorDB
                    existing = store.get_embedding(entry_id)
                    if existing:
                        topic_skipped += 1
                        stats.entries_skipped += 1
                        continue

                    if not dry_run:
                        # Store in FalkorDB
                        store.upsert_entry_embedding(
                            entry_id=entry_id,
                            thread_topic=topic,
                            embedding=embedding,
                        )

                    topic_migrated += 1
                    stats.entries_migrated += 1

                except json.JSONDecodeError as e:
                    logging.warning(f"Invalid JSON in {entries_file}: {e}")
                    stats.entries_failed += 1

        stats.files_processed += 1

        if topic_migrated > 0 or topic_skipped > 0:
            logging.info(
                f"{topic}: {topic_migrated} migrated, {topic_skipped} skipped (already in FalkorDB)"
            )

    return stats


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate embeddings from JSONL to FalkorDB"
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
        help="Show what would be migrated without actually migrating",
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

    # Run migration
    stats = run_migration(threads_dir, args.dry_run)

    # Print summary
    print("\n" + "=" * 50)
    print("Migration Summary")
    print("=" * 50)
    print(f"Files processed:         {stats.files_processed}")
    print(f"Entries processed:       {stats.entries_processed}")
    print(f"Entries with embedding:  {stats.entries_with_embedding}")
    print(f"Entries migrated:        {stats.entries_migrated}")
    print(f"Entries skipped:         {stats.entries_skipped}")
    print(f"Entries failed:          {stats.entries_failed}")
    print("=" * 50)


if __name__ == "__main__":
    main()
