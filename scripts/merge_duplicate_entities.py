#!/usr/bin/env python3
"""Merge duplicate entity nodes in Graphiti graph.

This script fixes duplicate entity nodes that were created due to the low-entropy
name bug in Graphiti's deduplication logic. For each set of duplicates (same
normalized name, same group_id):

1. Keep the oldest node (canonical) based on created_at timestamp
2. Reassign all edges from duplicates to the canonical node
3. Delete the duplicate nodes

The script preserves all relationships and edges, preventing data loss.

Usage:
    # Dry run (preview what would be merged)
    python3 scripts/merge_duplicate_entities.py --group-id watercooler_cloud --dry-run

    # Execute merge
    python3 scripts/merge_duplicate_entities.py --group-id watercooler_cloud --execute

    # List all databases with duplicates
    python3 scripts/merge_duplicate_entities.py --list-databases

    # Merge across all databases
    python3 scripts/merge_duplicate_entities.py --all --execute
"""

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Add external/graphiti to path for FalkorDB driver
sys.path.insert(0, str(Path(__file__).parent.parent / "external" / "graphiti"))


# --- Cypher Queries for FalkorDB ---

# Find all duplicate entity sets within a group_id
FIND_DUPLICATES_QUERY = """
MATCH (n:Entity)
WHERE n.group_id = $group_id
WITH toLower(n.name) AS norm_name, COLLECT(n) AS nodes
WHERE SIZE(nodes) > 1
RETURN norm_name,
       [node IN nodes | {
           uuid: node.uuid,
           name: node.name,
           created_at: node.created_at,
           summary: node.summary
       }] AS entities
ORDER BY SIZE(nodes) DESC
"""

# Reassign MENTIONS edges (Episode -> Entity)
# These are the primary connections from episodes to entities they mention
REASSIGN_MENTIONS_QUERY = """
MATCH (e:Episodic)-[r:MENTIONS]->(dup:Entity {uuid: $dup_uuid})
MATCH (keep:Entity {uuid: $keep_uuid})
WHERE NOT (e)-[:MENTIONS]->(keep)
CREATE (e)-[:MENTIONS]->(keep)
DELETE r
RETURN COUNT(*) AS moved
"""

# Clean up duplicate MENTIONS edges (if both dup and keep were mentioned)
CLEANUP_DUP_MENTIONS_QUERY = """
MATCH (e:Episodic)-[r:MENTIONS]->(dup:Entity {uuid: $dup_uuid})
DELETE r
RETURN COUNT(*) AS deleted
"""

# Reassign outgoing RELATES_TO edges (Entity -> Entity)
# These represent relationships extracted from episodes
REASSIGN_RELATES_OUT_QUERY = """
MATCH (dup:Entity {uuid: $dup_uuid})-[r:RELATES_TO]->(target)
MATCH (keep:Entity {uuid: $keep_uuid})
WHERE NOT (keep)-[:RELATES_TO]->(target)
CREATE (keep)-[r2:RELATES_TO]->(target)
SET r2 = properties(r)
DELETE r
RETURN COUNT(*) AS moved
"""

# Clean up duplicate outgoing edges
CLEANUP_DUP_RELATES_OUT_QUERY = """
MATCH (dup:Entity {uuid: $dup_uuid})-[r:RELATES_TO]->(target)
DELETE r
RETURN COUNT(*) AS deleted
"""

# Reassign incoming RELATES_TO edges (Entity -> Entity)
REASSIGN_RELATES_IN_QUERY = """
MATCH (source)-[r:RELATES_TO]->(dup:Entity {uuid: $dup_uuid})
MATCH (keep:Entity {uuid: $keep_uuid})
WHERE NOT (source)-[:RELATES_TO]->(keep)
CREATE (source)-[r2:RELATES_TO]->(keep)
SET r2 = properties(r)
DELETE r
RETURN COUNT(*) AS moved
"""

# Clean up duplicate incoming edges
CLEANUP_DUP_RELATES_IN_QUERY = """
MATCH (source)-[r:RELATES_TO]->(dup:Entity {uuid: $dup_uuid})
DELETE r
RETURN COUNT(*) AS deleted
"""

# Reassign HAS_MEMBER edges (Community -> Entity)
REASSIGN_COMMUNITY_QUERY = """
MATCH (c:Community)-[r:HAS_MEMBER]->(dup:Entity {uuid: $dup_uuid})
MATCH (keep:Entity {uuid: $keep_uuid})
WHERE NOT (c)-[:HAS_MEMBER]->(keep)
CREATE (c)-[:HAS_MEMBER]->(keep)
DELETE r
RETURN COUNT(*) AS moved
"""

# Clean up duplicate community membership
CLEANUP_DUP_COMMUNITY_QUERY = """
MATCH (c:Community)-[r:HAS_MEMBER]->(dup:Entity {uuid: $dup_uuid})
DELETE r
RETURN COUNT(*) AS deleted
"""

# Delete orphaned duplicate node (after all edges reassigned)
DELETE_DUPLICATE_QUERY = """
MATCH (n:Entity {uuid: $uuid})
DETACH DELETE n
RETURN COUNT(*) AS deleted
"""

# Count remaining duplicates (for verification)
COUNT_DUPLICATES_QUERY = """
MATCH (n:Entity)
WHERE n.group_id = $group_id
WITH toLower(n.name) AS name, COUNT(*) AS cnt
WHERE cnt > 1
RETURN name, cnt
ORDER BY cnt DESC
"""

# List all databases with entity duplicates
LIST_DATABASES_WITH_DUPLICATES_QUERY = """
MATCH (n:Entity)
WITH n.group_id AS group_id, toLower(n.name) AS name
WITH group_id, name, COUNT(*) AS cnt
WHERE cnt > 1
RETURN group_id, COUNT(DISTINCT name) AS dup_count, SUM(cnt - 1) AS total_duplicates
ORDER BY total_duplicates DESC
"""


@dataclass
class MergeStats:
    """Statistics for merge operation."""
    duplicates_found: int = 0
    entities_merged: int = 0
    mentions_moved: int = 0
    relates_out_moved: int = 0
    relates_in_moved: int = 0
    community_moved: int = 0
    nodes_deleted: int = 0
    errors: list[str] = field(default_factory=list)


async def get_driver(host: str, port: int, database: str):
    """Create FalkorDB driver for the specified database."""
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    driver = FalkorDriver(
        host=host,
        port=port,
        database=database,
    )
    return driver


async def find_duplicates(driver, group_id: str) -> list[dict[str, Any]]:
    """Find all duplicate entity sets in the group."""
    result = await driver.execute_query(FIND_DUPLICATES_QUERY, group_id=group_id)
    if not result:
        return []

    records, _, _ = result
    duplicates = []
    for record in records:
        duplicates.append({
            "name": record["norm_name"],
            "entities": record["entities"],
        })
    return duplicates


async def merge_entity_pair(
    driver,
    keep_uuid: str,
    dup_uuid: str,
    dry_run: bool = True,
) -> dict[str, int]:
    """Merge a single duplicate entity into the canonical one.

    Returns dict with counts of moved edges.
    """
    stats = {
        "mentions": 0,
        "relates_out": 0,
        "relates_in": 0,
        "community": 0,
    }

    if dry_run:
        return stats

    params = {"keep_uuid": keep_uuid, "dup_uuid": dup_uuid}

    # Reassign MENTIONS edges
    result = await driver.execute_query(REASSIGN_MENTIONS_QUERY, **params)
    if result and result[0]:
        stats["mentions"] = result[0][0].get("moved", 0)
    # Clean up any remaining MENTIONS to dup
    await driver.execute_query(CLEANUP_DUP_MENTIONS_QUERY, dup_uuid=dup_uuid)

    # Reassign outgoing RELATES_TO edges
    result = await driver.execute_query(REASSIGN_RELATES_OUT_QUERY, **params)
    if result and result[0]:
        stats["relates_out"] = result[0][0].get("moved", 0)
    await driver.execute_query(CLEANUP_DUP_RELATES_OUT_QUERY, dup_uuid=dup_uuid)

    # Reassign incoming RELATES_TO edges
    result = await driver.execute_query(REASSIGN_RELATES_IN_QUERY, **params)
    if result and result[0]:
        stats["relates_in"] = result[0][0].get("moved", 0)
    await driver.execute_query(CLEANUP_DUP_RELATES_IN_QUERY, dup_uuid=dup_uuid)

    # Reassign community membership
    result = await driver.execute_query(REASSIGN_COMMUNITY_QUERY, **params)
    if result and result[0]:
        stats["community"] = result[0][0].get("moved", 0)
    await driver.execute_query(CLEANUP_DUP_COMMUNITY_QUERY, dup_uuid=dup_uuid)

    # Delete the duplicate node
    await driver.execute_query(DELETE_DUPLICATE_QUERY, uuid=dup_uuid)

    return stats


async def merge_duplicates_in_group(
    driver,
    group_id: str,
    dry_run: bool = True,
) -> MergeStats:
    """Find and merge all duplicate entities in a group."""
    stats = MergeStats()

    # Find all duplicate sets
    duplicates = await find_duplicates(driver, group_id)

    if not duplicates:
        print(f"No duplicates found in group '{group_id}'")
        return stats

    total_dups = sum(len(d["entities"]) - 1 for d in duplicates)
    print(f"\nFound {len(duplicates)} duplicate sets ({total_dups} total duplicates)")

    for dup_set in duplicates:
        name = dup_set["name"]
        entities = dup_set["entities"]
        stats.duplicates_found += len(entities) - 1

        # Sort by created_at to find oldest (canonical) entity
        # Handle both string and datetime created_at values
        def get_created_at(e):
            created = e.get("created_at", "")
            if created is None:
                return ""
            return str(created)

        entities.sort(key=get_created_at)

        keep = entities[0]
        duplicates_to_merge = entities[1:]

        print(f"\n  '{name}': keeping {keep['uuid'][:12]}... ({len(duplicates_to_merge)} duplicates)")

        for dup in duplicates_to_merge:
            if dry_run:
                print(f"    [DRY RUN] Would merge {dup['uuid'][:12]}... -> {keep['uuid'][:12]}...")
                continue

            try:
                merge_stats = await merge_entity_pair(
                    driver,
                    keep_uuid=keep["uuid"],
                    dup_uuid=dup["uuid"],
                    dry_run=False,
                )

                stats.mentions_moved += merge_stats["mentions"]
                stats.relates_out_moved += merge_stats["relates_out"]
                stats.relates_in_moved += merge_stats["relates_in"]
                stats.community_moved += merge_stats["community"]
                stats.entities_merged += 1
                stats.nodes_deleted += 1

                total_edges = sum(merge_stats.values())
                print(f"    Merged {dup['uuid'][:12]}...: {total_edges} edges moved")

            except Exception as e:
                error_msg = f"Error merging {dup['uuid'][:12]}...: {e}"
                print(f"    ERROR: {error_msg}")
                stats.errors.append(error_msg)

    return stats


async def list_databases_with_duplicates(driver) -> list[dict[str, Any]]:
    """List all databases that have duplicate entities."""
    result = await driver.execute_query(LIST_DATABASES_WITH_DUPLICATES_QUERY)
    if not result:
        return []

    records, _, _ = result
    databases = []
    for record in records:
        databases.append({
            "group_id": record["group_id"],
            "dup_count": record["dup_count"],
            "total_duplicates": record["total_duplicates"],
        })
    return databases


async def verify_no_duplicates(driver, group_id: str) -> bool:
    """Verify that no duplicates remain in the group."""
    result = await driver.execute_query(COUNT_DUPLICATES_QUERY, group_id=group_id)
    if not result:
        return True

    records, _, _ = result
    if records:
        print(f"\nWARNING: {len(records)} duplicate sets still remain:")
        for record in records[:5]:
            print(f"  '{record['name']}': {record['cnt']} copies")
        if len(records) > 5:
            print(f"  ... and {len(records) - 5} more")
        return False

    return True


async def main_async(args: argparse.Namespace) -> int:
    """Main async entry point."""
    # Get connection settings from environment or defaults
    host = os.environ.get("FALKORDB_HOST", "localhost")
    port = int(os.environ.get("FALKORDB_PORT", "6379"))

    # Use default_db for listing databases, specific database for merge
    database = args.group_id if args.group_id else "default_db"

    print(f"Connecting to FalkorDB at {host}:{port}")

    try:
        driver = await get_driver(host, port, database)
    except Exception as e:
        print(f"Error connecting to FalkorDB: {e}")
        print("\nMake sure FalkorDB is running:")
        print("  docker run -d -p 6379:6379 falkordb/falkordb:latest")
        return 1

    try:
        # Health check
        await driver.health_check()
        print(f"Connected to FalkorDB")

        if args.list_databases:
            # List all databases with duplicates
            print("\nSearching for databases with duplicate entities...")
            databases = await list_databases_with_duplicates(driver)

            if not databases:
                print("No databases found with duplicate entities.")
                return 0

            print(f"\nDatabases with duplicates:")
            print(f"{'Group ID':<40} {'Dup Sets':>10} {'Total Dups':>12}")
            print("-" * 65)
            for db in databases:
                print(f"{db['group_id']:<40} {db['dup_count']:>10} {db['total_duplicates']:>12}")

            print(f"\nTo merge duplicates, run:")
            print(f"  python3 {sys.argv[0]} --group-id <group_id> --execute")
            return 0

        if args.all:
            # Process all databases
            print("\nFinding all databases with duplicates...")
            databases = await list_databases_with_duplicates(driver)

            if not databases:
                print("No databases found with duplicate entities.")
                return 0

            all_stats = MergeStats()
            for db in databases:
                group_id = db["group_id"]
                print(f"\n{'='*60}")
                print(f"Processing group: {group_id}")
                print(f"{'='*60}")

                # Create driver for this specific database
                db_driver = await get_driver(host, port, group_id)
                stats = await merge_duplicates_in_group(db_driver, group_id, dry_run=args.dry_run)

                all_stats.duplicates_found += stats.duplicates_found
                all_stats.entities_merged += stats.entities_merged
                all_stats.mentions_moved += stats.mentions_moved
                all_stats.relates_out_moved += stats.relates_out_moved
                all_stats.relates_in_moved += stats.relates_in_moved
                all_stats.community_moved += stats.community_moved
                all_stats.nodes_deleted += stats.nodes_deleted
                all_stats.errors.extend(stats.errors)

                await db_driver.close()

            stats = all_stats
        else:
            # Process single group
            if not args.group_id:
                print("Error: --group-id is required (or use --list-databases or --all)")
                return 1

            stats = await merge_duplicates_in_group(driver, args.group_id, dry_run=args.dry_run)

        # Print summary
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")

        if args.dry_run:
            print("[DRY RUN - no changes made]")

        print(f"  Duplicates found:     {stats.duplicates_found}")
        print(f"  Entities merged:      {stats.entities_merged}")
        print(f"  Nodes deleted:        {stats.nodes_deleted}")
        print(f"  Edges moved:")
        print(f"    - MENTIONS:         {stats.mentions_moved}")
        print(f"    - RELATES_TO (out): {stats.relates_out_moved}")
        print(f"    - RELATES_TO (in):  {stats.relates_in_moved}")
        print(f"    - HAS_MEMBER:       {stats.community_moved}")

        if stats.errors:
            print(f"\n  Errors: {len(stats.errors)}")
            for err in stats.errors[:5]:
                print(f"    - {err}")
            if len(stats.errors) > 5:
                print(f"    ... and {len(stats.errors) - 5} more")

        # Verify no duplicates remain (only if not dry run)
        if not args.dry_run and args.group_id and not args.all:
            print(f"\nVerifying no duplicates remain in '{args.group_id}'...")
            if await verify_no_duplicates(driver, args.group_id):
                print("Verification passed - no duplicates remain")
            else:
                return 1

        return 0 if not stats.errors else 1

    finally:
        await driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge duplicate entity nodes in Graphiti graph",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all databases with duplicates
  python3 scripts/merge_duplicate_entities.py --list-databases

  # Dry run (preview what would be merged)
  python3 scripts/merge_duplicate_entities.py --group-id watercooler_cloud --dry-run

  # Execute merge for specific group
  python3 scripts/merge_duplicate_entities.py --group-id watercooler_cloud --execute

  # Merge all databases
  python3 scripts/merge_duplicate_entities.py --all --execute

Environment variables:
  FALKORDB_HOST  - FalkorDB host (default: localhost)
  FALKORDB_PORT  - FalkorDB port (default: 6379)
""",
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--group-id",
        help="The group_id (database name) to process",
    )
    group.add_argument(
        "--list-databases",
        action="store_true",
        help="List all databases that have duplicate entities",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Process all databases with duplicates",
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview changes without executing (default)",
    )
    action.add_argument(
        "--execute",
        action="store_true",
        help="Execute the merge operation",
    )

    args = parser.parse_args()

    # --execute overrides default dry-run
    if args.execute:
        args.dry_run = False

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
