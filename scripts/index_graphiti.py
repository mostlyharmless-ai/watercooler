#!/usr/bin/env python3
"""Index watercooler threads into Graphiti backend using chunked episodes.

This script uses the same chunked approach as the migration tool:
- Entries are chunked using watercooler_preset (header chunk + body chunks)
- Each chunk is added as a separate episode via add_episode_direct()
- Chunks within an entry are linked via previous_episode_uuids
- First chunk of each entry passes [] to prevent unbounded context growth

Usage:
    python3 scripts/index_graphiti.py --thread-list /path/to/threads-to-index.txt
    python3 scripts/index_graphiti.py --threads graphiti-mcp-integration memory-backend
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from watercooler_memory.backends.graphiti import GraphitiBackend, GraphitiConfig
from watercooler_memory.graph import MemoryGraph
from watercooler_memory.chunker import ChunkerConfig, chunk_entry
from watercooler_memory.graph import GraphConfig, EntryNode


def load_thread_list(list_file: Path) -> list[str]:
    """Load thread filenames from list file."""
    threads = []
    with open(list_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                # Ensure .md extension
                if not line.endswith(".md"):
                    line = f"{line}.md"
                threads.append(line)
    return threads


def build_entries_with_chunks(
    threads_dir: Path,
    thread_files: list[str],
    max_tokens: int = 768,
    overlap: int = 64,
) -> list[dict]:
    """Build entries with chunks from watercooler threads.

    Returns list of entry dicts, each containing:
    - Entry metadata (id, thread_id, agent, role, etc.)
    - chunks: list of ChunkNode objects for this entry
    """
    print(f"Building entries from {len(thread_files)} threads...")

    # Build memory graph with watercooler preset for headers
    config = GraphConfig(chunker=ChunkerConfig.watercooler_preset(
        max_tokens=max_tokens,
        overlap=overlap,
    ))
    graph = MemoryGraph(config=config)

    for thread_file in thread_files:
        thread_path = threads_dir / thread_file
        if thread_path.exists():
            print(f"  Loading {thread_file}...")
            graph.add_thread(thread_path)
        else:
            print(f"  Warning: {thread_file} not found, skipping")

    # Chunk all entries
    print("Chunking entries...")
    chunk_nodes = graph.chunk_all_entries()

    # Group chunks by entry_id
    chunks_by_entry: dict[str, list] = {}
    for chunk in chunk_nodes:
        if chunk.entry_id not in chunks_by_entry:
            chunks_by_entry[chunk.entry_id] = []
        chunks_by_entry[chunk.entry_id].append(chunk)

    # Build entry list with chunks
    entries = []
    for entry in graph.entries.values():
        entry_chunks = chunks_by_entry.get(entry.entry_id, [])
        entries.append({
            "id": entry.entry_id,
            "thread_id": entry.thread_id,
            "agent": entry.agent,
            "role": entry.role,
            "entry_type": entry.entry_type,
            "title": entry.title,
            "body": entry.body,
            "timestamp": entry.timestamp,
            "chunks": entry_chunks,
        })

    total_chunks = sum(len(e["chunks"]) for e in entries)
    print(f"Created {total_chunks} chunks from {len(entries)} entries")

    return entries


async def index_entries_chunked(
    backend: GraphitiBackend,
    entries: list[dict],
) -> dict:
    """Index entries into Graphiti using chunked episodes.

    Each chunk becomes a separate episode. Chunks within an entry
    are linked via previous_episode_uuids for temporal ordering.
    First chunk of each entry passes [] to prevent context overflow.

    Returns:
        Dict with indexing statistics
    """
    stats = {
        "entries_processed": 0,
        "entries_failed": 0,
        "chunks_indexed": 0,
        "errors": [],
    }

    for entry_idx, entry in enumerate(entries):
        entry_id = entry["id"]
        thread_id = entry["thread_id"]
        chunks = entry["chunks"]

        if not chunks:
            print(f"  Skipping entry {entry_id}: no chunks")
            continue

        # Parse timestamp
        timestamp_str = entry.get("timestamp")
        if timestamp_str:
            try:
                ref_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            except ValueError:
                ref_time = datetime.now(timezone.utc)
        else:
            ref_time = datetime.now(timezone.utc)

        # Build base episode name from title
        title = entry.get("title", "")
        body = entry.get("body", "")
        if title and title.strip():
            base_name = title.strip()
        elif body:
            body_snippet = body[:50].replace('\n', ' ').strip()
            base_name = body_snippet + ("..." if len(body) > 50 else "")
        else:
            base_name = f"Entry {entry_id}"

        # Build source description
        agent = entry.get("agent", "Unknown")
        role = entry.get("role", "")
        entry_type = entry.get("entry_type", "Note")
        base_source_desc = f"Index: {agent}"
        if role:
            base_source_desc += f" ({role})"

        total_chunks = len(chunks)
        entry_failed = False
        previous_episode_uuid: str | None = None

        print(f"  [{entry_idx + 1}/{len(entries)}] {entry_id}: {total_chunks} chunks")

        for i, chunk in enumerate(chunks):
            try:
                # Build episode name with chunk suffix
                if total_chunks > 1:
                    episode_name = f"{base_name} [{i + 1}/{total_chunks}]"
                    source_desc = f"{base_source_desc} - chunk:{chunk.chunk_id[:12]} [{i + 1}/{total_chunks}]"
                else:
                    episode_name = base_name
                    source_desc = base_source_desc
                    if entry_type:
                        source_desc += f" - {entry_type}"

                # Get previous episode UUIDs for linking
                # First chunk: [] (no previous context - prevents unbounded context growth)
                # Subsequent chunks: link to previous chunk's episode
                # Note: Using [] instead of None prevents Graphiti from retrieving
                # RELEVANT_SCHEMA_LIMIT (10) previous episodes, which can exceed
                # LLM context limits. Cross-entry dedup still works via graph merges.
                previous_uuids: list[str] = []
                if i > 0 and previous_episode_uuid:
                    previous_uuids = [previous_episode_uuid]

                # Add episode directly (bypasses prepare/index workflow)
                result = await backend.add_episode_direct(
                    name=episode_name,
                    episode_body=chunk.text,
                    source_description=source_desc,
                    reference_time=ref_time,
                    group_id=thread_id,
                    previous_episode_uuids=previous_uuids,
                )

                episode_uuid = result.get("episode_uuid", "")
                previous_episode_uuid = episode_uuid
                stats["chunks_indexed"] += 1

            except Exception as e:
                print(f"    Error on chunk {i + 1}/{total_chunks}: {e}")
                stats["errors"].append(f"{entry_id} chunk {i + 1}: {e}")
                entry_failed = True
                break

        if entry_failed:
            stats["entries_failed"] += 1
        else:
            stats["entries_processed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description="Index watercooler threads into Graphiti")
    parser.add_argument("--threads-dir", default="/home/jay/projects/watercooler-cloud-threads",
                        help="Path to threads directory")
    parser.add_argument("--thread-list", help="Path to file with thread list (one per line)")
    parser.add_argument("--threads", nargs="+", help="List of thread topics (without .md)")
    parser.add_argument("--work-dir", help="Work directory for Graphiti (default: ~/.watercooler/graphiti)")
    parser.add_argument("--chunk-max-tokens", type=int, default=768,
                        help="Maximum tokens per chunk (default: 768)")
    parser.add_argument("--chunk-overlap", type=int, default=64,
                        help="Overlap tokens between chunks (default: 64)")

    args = parser.parse_args()

    # Check for LLM API key (supports local LLM servers)
    llm_api_key = os.environ.get("LLM_API_KEY")
    if not llm_api_key:
        print("Error: LLM_API_KEY environment variable not set", file=sys.stderr)
        print("For local LLM: export LLM_API_KEY=local", file=sys.stderr)
        print("For OpenAI: export LLM_API_KEY=sk-...", file=sys.stderr)
        return 1

    # Check for embedding API key
    embedding_api_key = os.environ.get("EMBEDDING_API_KEY")
    if not embedding_api_key:
        print("Error: EMBEDDING_API_KEY environment variable not set", file=sys.stderr)
        print("For local embeddings: export EMBEDDING_API_KEY=local", file=sys.stderr)
        return 1

    # Determine thread list
    if args.thread_list:
        thread_list_path = Path(args.thread_list)
        if not thread_list_path.exists():
            print(f"Error: Thread list file not found: {thread_list_path}", file=sys.stderr)
            return 1
        thread_files = load_thread_list(thread_list_path)
    elif args.threads:
        thread_files = [f"{t}.md" if not t.endswith(".md") else t for t in args.threads]
    else:
        print("Error: Specify either --thread-list or --threads", file=sys.stderr)
        parser.print_help()
        return 1

    threads_dir = Path(args.threads_dir)
    if not threads_dir.exists():
        print(f"Error: Threads directory not found: {threads_dir}", file=sys.stderr)
        return 1

    # Set up Graphiti backend with LLM/embedding configuration
    work_dir = Path(args.work_dir) if args.work_dir else Path.home() / ".watercooler" / "graphiti"
    config = GraphitiConfig(
        work_dir=work_dir,
        test_mode=False,
        # LLM configuration (from environment)
        llm_api_key=llm_api_key,
        llm_api_base=os.environ.get("LLM_API_BASE"),
        llm_model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        # Embedding configuration (from environment)
        embedding_api_key=embedding_api_key,
        embedding_api_base=os.environ.get("EMBEDDING_API_BASE"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    backend = GraphitiBackend(config)

    # Check health
    print("Checking Graphiti backend health...")
    health = backend.healthcheck()
    if not health.ok:
        print(f"Error: Backend health check failed: {health.details}", file=sys.stderr)
        print("\nMake sure FalkorDB is running: docker run -d -p 6379:6379 falkordb/falkordb:latest", file=sys.stderr)
        return 1
    print(f"✓ Backend healthy: {health.details}")

    # Build entries with chunks
    entries = build_entries_with_chunks(
        threads_dir,
        thread_files,
        max_tokens=args.chunk_max_tokens,
        overlap=args.chunk_overlap,
    )
    print(f"\n✓ Built {len(entries)} entries")

    # Index using chunked episodes
    print("\nIndexing into Graphiti (this may take several minutes)...")
    print("  Each chunk is added as a separate episode with LLM entity extraction.")
    print("  Chunks within an entry are linked for temporal ordering.")

    stats = asyncio.run(index_entries_chunked(backend, entries))

    print(f"\n✅ Indexing complete!")
    print(f"  Entries processed: {stats['entries_processed']}")
    print(f"  Entries failed: {stats['entries_failed']}")
    print(f"  Chunks indexed: {stats['chunks_indexed']}")
    if stats["errors"]:
        print(f"  Errors: {len(stats['errors'])}")
        for err in stats["errors"][:5]:
            print(f"    - {err}")
        if len(stats["errors"]) > 5:
            print(f"    ... and {len(stats['errors']) - 5} more")

    print(f"\nWork directory: {work_dir}")
    print("\nYou can now query via MCP:")
    print('  watercooler_query_memory(query="your question", code_path=".", limit=10)')

    return 0 if stats["entries_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
