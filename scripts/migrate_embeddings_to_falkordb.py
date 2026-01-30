#!/usr/bin/env python3
"""Migration script: Move entry embeddings from search-index.jsonl to FalkorDB.

This one-time migration script transfers embeddings from the file-based
search-index.jsonl format to FalkorDB vector storage.

Usage:
    # Dry run (verify what would be migrated)
    python scripts/migrate_embeddings_to_falkordb.py --code-path /path/to/repo --dry-run

    # Execute migration
    python scripts/migrate_embeddings_to_falkordb.py --code-path /path/to/repo

    # Execute with verbose logging
    python scripts/migrate_embeddings_to_falkordb.py --code-path /path/to/repo -v

    # Skip verification (faster)
    python scripts/migrate_embeddings_to_falkordb.py --code-path /path/to/repo --skip-verify

Prerequisites:
    - FalkorDB must be running (docker-compose up -d falkordb)
    - The search-index.jsonl file must exist in the graph directory

After migration:
    - Embeddings are stored in FalkorDB with HNSW vector index
    - File-based search-index.jsonl is preserved as backup
    - Semantic search uses FalkorDB (with file-based fallback)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class MigrationStats:
    """Migration statistics."""

    total_entries: int = 0
    migrated: int = 0
    skipped_no_embedding: int = 0
    skipped_no_topic: int = 0
    failed: int = 0
    already_exists: int = 0


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
    # Try common locations
    for threads_name in ["threads", ".threads"]:
        threads_dir = code_path / threads_name
        if threads_dir.exists():
            return threads_dir

    # Fall back to assuming code_path is the threads dir
    return code_path


def get_graph_dir(threads_dir: Path) -> Path:
    """Get graph directory from threads directory."""
    return threads_dir / "graph" / "baseline"


def get_group_id(threads_dir: Path) -> str:
    """Derive group_id from threads directory.

    Handles two layouts:
    1. Paired repos: /path/to/watercooler-site-threads → watercooler_site
    2. Embedded dirs: /path/to/repo/threads/ → repo
    """
    dir_name = threads_dir.name
    if dir_name.endswith("-threads"):
        name = dir_name[:-8]  # Strip "-threads" suffix
    else:
        name = threads_dir.parent.name
    return name.replace("-", "_").lower() or "watercooler"


def load_search_index(graph_dir: Path) -> Iterator[dict]:
    """Load search index entries from file."""
    search_index_file = graph_dir / "search-index.jsonl"
    if not search_index_file.exists():
        logging.error(f"Search index file not found: {search_index_file}")
        return

    with open(search_index_file, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    logging.warning(f"Invalid JSON on line {line_num}: {e}")
                    continue


async def migrate_entry(
    store,
    entry_id: str,
    thread_topic: str,
    embedding: list[float],
    dry_run: bool = False,
) -> bool:
    """Migrate a single entry embedding to FalkorDB.

    Returns:
        True if migrated successfully, False otherwise
    """
    if dry_run:
        logging.debug(f"Would migrate: {entry_id} (topic: {thread_topic})")
        return True

    try:
        await store.store_embedding(entry_id, thread_topic, embedding)
        logging.debug(f"Migrated: {entry_id}")
        return True
    except Exception as e:
        logging.error(f"Failed to migrate {entry_id}: {e}")
        return False


async def verify_migration(
    store,
    graph_dir: Path,
    stats: MigrationStats,
) -> bool:
    """Verify migration by comparing counts.

    Returns:
        True if counts match, False otherwise
    """
    logging.info("Verifying migration...")

    # Count entries in FalkorDB
    try:
        falkordb_count = await store.count_entries()
    except Exception as e:
        logging.error(f"Failed to count FalkorDB entries: {e}")
        return False

    # Count entries in search-index.jsonl (with embeddings)
    file_count = 0
    for entry in load_search_index(graph_dir):
        if entry.get("embedding"):
            file_count += 1

    logging.info(f"FalkorDB entries: {falkordb_count}")
    logging.info(f"File entries with embeddings: {file_count}")
    logging.info(f"Migration stats - migrated: {stats.migrated}, failed: {stats.failed}")

    if falkordb_count >= stats.migrated:
        logging.info("Verification PASSED: FalkorDB has expected number of entries")
        return True
    else:
        logging.error(
            f"Verification FAILED: FalkorDB has {falkordb_count} entries, "
            f"expected at least {stats.migrated}"
        )
        return False


async def run_migration(
    threads_dir: Path,
    dry_run: bool = False,
    skip_verify: bool = False,
) -> MigrationStats:
    """Run the migration.

    Args:
        threads_dir: Path to threads directory
        dry_run: If True, don't actually migrate
        skip_verify: If True, skip verification step

    Returns:
        Migration statistics
    """
    stats = MigrationStats()
    graph_dir = get_graph_dir(threads_dir)
    group_id = get_group_id(threads_dir)

    if not graph_dir.exists():
        logging.error(f"Graph directory not found: {graph_dir}")
        return stats

    search_index_file = graph_dir / "search-index.jsonl"
    if not search_index_file.exists():
        logging.error(f"Search index file not found: {search_index_file}")
        return stats

    logging.info(f"Migrating embeddings from: {search_index_file}")
    logging.info(f"Target group_id: {group_id}")

    if dry_run:
        logging.info("DRY RUN - no changes will be made")

    # Import and create FalkorDB store
    from watercooler.baseline_graph.falkordb_entries import FalkorDBEntryStore

    store = FalkorDBEntryStore.from_config(group_id)

    try:
        if not dry_run:
            # Connect to FalkorDB
            logging.info("Connecting to FalkorDB...")
            await store.connect()
            logging.info("Connected. Ensuring vector index exists...")
            await store.ensure_index()
            logging.info("Index ready.")

        # Process each entry in search index
        for entry in load_search_index(graph_dir):
            stats.total_entries += 1

            entry_id = entry.get("entry_id")
            thread_topic = entry.get("thread_topic")
            embedding = entry.get("embedding")

            if not entry_id:
                logging.warning(f"Entry missing entry_id: {entry}")
                stats.failed += 1
                continue

            if not thread_topic:
                logging.debug(f"Entry {entry_id} has no thread_topic, skipping")
                stats.skipped_no_topic += 1
                continue

            if not embedding:
                logging.debug(f"Entry {entry_id} has no embedding, skipping")
                stats.skipped_no_embedding += 1
                continue

            # Migrate the entry
            if await migrate_entry(store, entry_id, thread_topic, embedding, dry_run):
                stats.migrated += 1
            else:
                stats.failed += 1

            # Progress logging
            if stats.total_entries % 100 == 0:
                logging.info(f"Progress: {stats.total_entries} entries processed...")

        logging.info(f"Migration complete. {stats.migrated} entries migrated.")

        # Verify
        if not dry_run and not skip_verify:
            await verify_migration(store, graph_dir, stats)

    finally:
        if not dry_run:
            await store.close()

    return stats


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate entry embeddings from search-index.jsonl to FalkorDB"
    )
    parser.add_argument(
        "--code-path",
        type=Path,
        required=True,
        help="Path to the code repository (containing threads/ directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify what would be migrated without making changes",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip verification step after migration",
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
        logging.error(f"Code path does not exist: {code_path}")
        sys.exit(1)

    threads_dir = get_threads_dir(code_path)
    if not threads_dir.exists():
        logging.error(f"Threads directory not found: {threads_dir}")
        sys.exit(1)

    logging.info(f"Code path: {code_path}")
    logging.info(f"Threads dir: {threads_dir}")

    # Run migration
    stats = asyncio.run(run_migration(threads_dir, args.dry_run, args.skip_verify))

    # Print summary
    print("\n" + "=" * 50)
    print("Migration Summary")
    print("=" * 50)
    print(f"Total entries processed: {stats.total_entries}")
    print(f"Successfully migrated:   {stats.migrated}")
    print(f"Skipped (no embedding):  {stats.skipped_no_embedding}")
    print(f"Skipped (no topic):      {stats.skipped_no_topic}")
    print(f"Failed:                  {stats.failed}")
    print("=" * 50)

    if stats.failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
