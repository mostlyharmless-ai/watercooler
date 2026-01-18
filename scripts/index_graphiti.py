#!/usr/bin/env python3
"""Index watercooler threads into Graphiti backend using chunked episodes.

This script uses the same chunked approach as the migration tool:
- Entries are chunked using watercooler_preset (header chunk + body chunks)
- Each chunk is added as a separate episode via add_episode_direct()
- Chunks within an entry are linked via previous_episode_uuids
- First chunk of each entry passes [] to prevent unbounded context growth
- Deduplication via chunk_id prevents duplicate episodes on re-runs
- Checkpoint/resume support for interrupted migrations

Usage:
    # Index all threads in a directory
    python3 scripts/index_graphiti.py --threads-dir /path/to/threads --all

    # Index specific threads
    python3 scripts/index_graphiti.py --threads-dir /path/to/threads --threads topic1 topic2

    # Index from a list file
    python3 scripts/index_graphiti.py --threads-dir /path/to/threads --thread-list threads.txt

    # Resume interrupted migration
    python3 scripts/index_graphiti.py --threads-dir /path/to/threads --threads my-thread --resume

    # Force re-index (ignore checkpoint, but still deduplicates)
    python3 scripts/index_graphiti.py --threads-dir /path/to/threads --threads my-thread --force
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from watercooler_memory.backends.graphiti import (
    GraphitiBackend,
    GraphitiConfig,
    _derive_database_name,
)
from watercooler_memory.graph import MemoryGraph
from watercooler_memory.chunker import ChunkerConfig
from watercooler_memory.graph import GraphConfig


# --- Checkpoint Data Structures (aligned with migration.py) ---

@dataclass
class ChunkProgress:
    """Progress tracking for a single chunk."""
    chunk_index: int
    chunk_id: str
    episode_uuid: str


@dataclass
class EntryProgress:
    """Progress tracking for a single entry migration."""
    thread_id: str
    status: str  # "in_progress" | "complete"
    total_chunks: int
    last_completed_chunk_index: int  # -1 when none completed
    chunks: List[ChunkProgress] = field(default_factory=list)
    last_updated_at: str = ""
    run_id: str = ""

    def __post_init__(self):
        if not self.last_updated_at:
            self.last_updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "status": self.status,
            "total_chunks": self.total_chunks,
            "last_completed_chunk_index": self.last_completed_chunk_index,
            "chunks": [
                {"chunk_index": c.chunk_index, "chunk_id": c.chunk_id, "episode_uuid": c.episode_uuid}
                for c in self.chunks
            ],
            "last_updated_at": self.last_updated_at,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EntryProgress":
        chunks = [
            ChunkProgress(
                chunk_index=c["chunk_index"],
                chunk_id=c["chunk_id"],
                episode_uuid=c["episode_uuid"],
            )
            for c in d.get("chunks", [])
        ]
        return cls(
            thread_id=d["thread_id"],
            status=d["status"],
            total_chunks=d["total_chunks"],
            last_completed_chunk_index=d["last_completed_chunk_index"],
            chunks=chunks,
            last_updated_at=d.get("last_updated_at", ""),
            run_id=d.get("run_id", ""),
        )


@dataclass
class Checkpoint:
    """Checkpoint for resumable migration."""
    version: int = 2
    backend: str = "graphiti"
    entries: Dict[str, EntryProgress] = field(default_factory=dict)
    run_id: str = ""

    def __post_init__(self):
        if not self.run_id:
            self.run_id = str(uuid.uuid4())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "backend": self.backend,
            "entries": {eid: ep.to_dict() for eid, ep in self.entries.items()},
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Checkpoint":
        entries = {
            eid: EntryProgress.from_dict(ep_dict)
            for eid, ep_dict in d.get("entries", {}).items()
        }
        return cls(
            version=d.get("version", 2),
            backend=d.get("backend", "graphiti"),
            entries=entries,
            run_id=d.get("run_id", ""),
        )

    def is_entry_complete(self, entry_id: str) -> bool:
        """Check if an entry is fully migrated."""
        ep = self.entries.get(entry_id)
        return ep is not None and ep.status == "complete"

    def get_resume_chunk_index(self, entry_id: str) -> int:
        """Get the chunk index to resume from for an entry."""
        ep = self.entries.get(entry_id)
        if ep is None:
            return 0
        return ep.last_completed_chunk_index + 1


@dataclass
class ChunkTimingStats:
    """Track per-chunk timing with running averages."""
    dedup_times: list[float] = field(default_factory=list)
    index_times: list[float] = field(default_factory=list)
    checkpoint_times: list[float] = field(default_factory=list)
    total_times: list[float] = field(default_factory=list)
    chunks_processed: int = 0
    total_chunks: int = 0

    def add_timing(self, dedup: float, index: float, checkpoint: float) -> None:
        """Record timing for a single chunk."""
        self.dedup_times.append(dedup)
        self.index_times.append(index)
        self.checkpoint_times.append(checkpoint)
        self.total_times.append(dedup + index + checkpoint)
        self.chunks_processed += 1

    def running_avg(self, times: list[float], window: int = 5) -> float:
        """Calculate running average over recent window."""
        if not times:
            return 0.0
        recent = times[-window:] if len(times) >= window else times
        return sum(recent) / len(recent)

    def overall_avg(self) -> float:
        """Calculate overall average chunk time."""
        return sum(self.total_times) / len(self.total_times) if self.total_times else 0.0

    def eta_seconds(self, remaining: int) -> float:
        """Estimate time remaining based on overall average."""
        avg = self.overall_avg()
        return avg * remaining if avg > 0 else 0.0

    def format_eta(self, remaining: int) -> str:
        """Format ETA as human-readable string."""
        eta = self.eta_seconds(remaining)
        if eta >= 60:
            return f"{eta / 60:.1f}m"
        return f"{eta:.1f}s"

    def summary_dict(self) -> dict:
        """Return summary statistics for final report."""
        if not self.total_times:
            return {}
        return {
            "chunks_processed": self.chunks_processed,
            "avg_total": self.overall_avg(),
            "avg_dedup": sum(self.dedup_times) / len(self.dedup_times) if self.dedup_times else 0.0,
            "avg_index": sum(self.index_times) / len(self.index_times) if self.index_times else 0.0,
            "avg_checkpoint": sum(self.checkpoint_times) / len(self.checkpoint_times) if self.checkpoint_times else 0.0,
            "min_total": min(self.total_times),
            "max_total": max(self.total_times),
        }


def load_checkpoint(threads_dir: Path) -> Checkpoint:
    """Load checkpoint if exists."""
    checkpoint_file = threads_dir / ".migration_checkpoint.json"
    if checkpoint_file.exists():
        try:
            data = json.loads(checkpoint_file.read_text())
            return Checkpoint.from_dict(data)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Failed to load checkpoint: {e}")
    return Checkpoint()


def save_checkpoint(threads_dir: Path, checkpoint: Checkpoint) -> None:
    """Save checkpoint atomically with fsync."""
    checkpoint_file = threads_dir / ".migration_checkpoint.json"

    # Atomic write: write to temp, fsync, then rename
    fd, temp_path = tempfile.mkstemp(
        dir=threads_dir,
        prefix=".migration_checkpoint_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(checkpoint.to_dict(), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, checkpoint_file)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


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
    threads_dir: Path,
    checkpoint: Checkpoint,
    database_name: str,
    resume: bool = True,
) -> dict:
    """Index entries into Graphiti using chunked episodes.

    All episodes use the project database_name as group_id (unified graph).
    Thread topic is included in source_description for traceability.

    Features:
    - Deduplication: Checks if chunk already exists before adding
    - Checkpoint: Saves progress after each chunk for resume
    - Episode linking: Chunks within an entry linked via previous_episode_uuids

    Returns:
        Dict with indexing statistics including timing breakdown
    """
    stats = {
        "entries_processed": 0,
        "entries_failed": 0,
        "entries_skipped": 0,
        "chunks_indexed": 0,
        "chunks_deduplicated": 0,
        "errors": [],
    }

    # Initialize timing tracker
    total_chunks_all = sum(len(e["chunks"]) for e in entries)
    timing_stats = ChunkTimingStats(total_chunks=total_chunks_all)
    global_chunk_idx = 0  # Track position across all entries
    progress_interval = 5  # Report progress every N chunks

    for entry_idx, entry in enumerate(entries):
        entry_id = entry["id"]
        thread_id = entry["thread_id"]
        chunks = entry["chunks"]

        if not chunks:
            print(f"  Skipping entry {entry_id}: no chunks")
            continue

        # Check if already complete (checkpoint)
        if resume and checkpoint.is_entry_complete(entry_id):
            stats["entries_skipped"] += 1
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

        # Build source description (includes thread topic for traceability)
        agent = entry.get("agent", "Unknown")
        role = entry.get("role", "")
        entry_type = entry.get("entry_type", "Note")
        base_source_desc = f"thread:{thread_id} | Index: {agent}"
        if role:
            base_source_desc += f" ({role})"

        total_chunks = len(chunks)
        entry_failed = False

        # Get or create entry progress
        if entry_id not in checkpoint.entries:
            checkpoint.entries[entry_id] = EntryProgress(
                thread_id=thread_id,
                status="in_progress",
                total_chunks=total_chunks,
                last_completed_chunk_index=-1,
                chunks=[],
                run_id=checkpoint.run_id,
            )

        entry_progress = checkpoint.entries[entry_id]
        start_chunk_index = checkpoint.get_resume_chunk_index(entry_id) if resume else 0

        # Track previous episode UUID for linking
        previous_episode_uuid: str | None = None
        if entry_progress.chunks:
            previous_episode_uuid = entry_progress.chunks[-1].episode_uuid

        print(f"  [{entry_idx + 1}/{len(entries)}] {entry_id}: {total_chunks} chunks", end="")
        if start_chunk_index > 0:
            print(f" (resuming from chunk {start_chunk_index + 1})")
        else:
            print()

        entry_start_time = time.perf_counter()
        entry_chunks_processed = 0

        for i, chunk in enumerate(chunks):
            # Skip already completed chunks
            if i < start_chunk_index:
                global_chunk_idx += 1
                continue

            try:
                chunk_start = time.perf_counter()
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
                previous_uuids: list[str] = []
                if i > 0 and previous_episode_uuid:
                    previous_uuids = [previous_episode_uuid]

                # DEDUPLICATION: Check if this chunk already exists in the graph
                dedup_start = time.perf_counter()
                existing_episode = await backend.find_episode_by_chunk_id_async(
                    chunk_id=chunk.chunk_id,
                    group_id=database_name,
                )
                dedup_time = time.perf_counter() - dedup_start

                index_start = time.perf_counter()
                if existing_episode:
                    # Episode already exists - use existing UUID
                    episode_uuid = existing_episode["uuid"]
                    stats["chunks_deduplicated"] += 1
                else:
                    # Add episode (unified graph: all threads share database_name as group_id)
                    result = await backend.add_episode_direct(
                        name=episode_name,
                        episode_body=chunk.text,
                        source_description=source_desc,
                        reference_time=ref_time,
                        group_id=database_name,
                        previous_episode_uuids=previous_uuids,
                    )
                    episode_uuid = result.get("episode_uuid", "")
                    stats["chunks_indexed"] += 1
                index_time = time.perf_counter() - index_start

                # Update progress
                previous_episode_uuid = episode_uuid
                entry_progress.chunks.append(ChunkProgress(
                    chunk_index=i,
                    chunk_id=chunk.chunk_id,
                    episode_uuid=episode_uuid,
                ))
                entry_progress.last_completed_chunk_index = i
                entry_progress.last_updated_at = datetime.now(timezone.utc).isoformat()

                # Save checkpoint after each chunk
                ckpt_start = time.perf_counter()
                save_checkpoint(threads_dir, checkpoint)
                ckpt_time = time.perf_counter() - ckpt_start

                # Record timing
                timing_stats.add_timing(dedup_time, index_time, ckpt_time)
                global_chunk_idx += 1
                entry_chunks_processed += 1

                # Progress reporting every N chunks
                if timing_stats.chunks_processed % progress_interval == 0:
                    remaining = total_chunks_all - global_chunk_idx
                    avg_total = timing_stats.running_avg(timing_stats.total_times)
                    avg_dedup = timing_stats.running_avg(timing_stats.dedup_times)
                    avg_index = timing_stats.running_avg(timing_stats.index_times)
                    avg_ckpt = timing_stats.running_avg(timing_stats.checkpoint_times)
                    eta = timing_stats.format_eta(remaining)
                    print(f"    Progress: {global_chunk_idx}/{total_chunks_all} chunks | "
                          f"Last {progress_interval} avg: {avg_total:.2f}s "
                          f"(dedup: {avg_dedup:.2f}s, index: {avg_index:.2f}s, ckpt: {avg_ckpt:.2f}s) | "
                          f"ETA: {eta}")

            except Exception as e:
                print(f"    Error on chunk {i + 1}/{total_chunks}: {e}")
                stats["errors"].append(f"{entry_id} chunk {i + 1}: {e}")
                entry_failed = True
                break

        # Mark entry complete or failed
        if entry_failed:
            stats["entries_failed"] += 1
        else:
            entry_progress.status = "complete"
            save_checkpoint(threads_dir, checkpoint)
            stats["entries_processed"] += 1

            # Entry-level timing summary
            if entry_chunks_processed > 0:
                entry_elapsed = time.perf_counter() - entry_start_time
                entry_avg = entry_elapsed / entry_chunks_processed
                print(f"  ✓ Entry {entry_id[:12]}...: {entry_chunks_processed} chunks in {entry_elapsed:.1f}s (avg {entry_avg:.2f}s/chunk)")

    # Attach timing stats to return dict
    stats["timing"] = timing_stats.summary_dict()
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Index watercooler threads into Graphiti",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Index all threads in a directory
  python3 scripts/index_graphiti.py --threads-dir /path/to/threads --all

  # Index specific threads
  python3 scripts/index_graphiti.py --threads-dir /path/to/threads --threads auth-feature api-design

  # Index from a list file
  python3 scripts/index_graphiti.py --threads-dir /path/to/threads --thread-list threads.txt

  # Resume interrupted migration
  python3 scripts/index_graphiti.py --threads-dir /path/to/threads --threads my-thread --resume

  # Force re-index (still deduplicates, but ignores checkpoint)
  python3 scripts/index_graphiti.py --threads-dir /path/to/threads --threads my-thread --force
""",
    )
    parser.add_argument("--threads-dir",
                        help="Path to threads directory (required, or derive from --code-path)")
    parser.add_argument("--code-path",
                        help="Path to code repository (for database name derivation, defaults to threads-dir without -threads suffix)")
    parser.add_argument("--thread-list", help="Path to file with thread list (one per line)")
    parser.add_argument("--threads", nargs="+", help="List of thread topics (without .md)")
    parser.add_argument("--all", action="store_true",
                        help="Index all .md thread files in threads-dir")
    parser.add_argument("--work-dir", help="Work directory for Graphiti (default: ~/.watercooler/graphiti)")
    parser.add_argument("--chunk-max-tokens", type=int, default=768,
                        help="Maximum tokens per chunk (default: 768)")
    parser.add_argument("--chunk-overlap", type=int, default=64,
                        help="Overlap tokens between chunks (default: 64)")
    parser.add_argument("--resume", action="store_true", default=True,
                        help="Resume from checkpoint if available (default: true)")
    parser.add_argument("--force", action="store_true",
                        help="Ignore checkpoint and re-process all entries (still deduplicates)")

    args = parser.parse_args()

    # Initialize timing
    timings: dict[str, float] = {}
    total_start = time.perf_counter()

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
    if args.all:
        # Auto-discover all .md files in threads_dir
        if not args.threads_dir:
            print("Error: --threads-dir is required when using --all", file=sys.stderr)
            return 1
        threads_dir = Path(args.threads_dir)
        if not threads_dir.exists():
            print(f"Error: Threads directory not found: {threads_dir}", file=sys.stderr)
            return 1
        thread_files = sorted([f.name for f in threads_dir.glob("*.md")])
        if not thread_files:
            print(f"Error: No .md files found in {threads_dir}", file=sys.stderr)
            return 1
        print(f"Discovered {len(thread_files)} thread files")
    elif args.thread_list:
        thread_list_path = Path(args.thread_list)
        if not thread_list_path.exists():
            print(f"Error: Thread list file not found: {thread_list_path}", file=sys.stderr)
            return 1
        thread_files = load_thread_list(thread_list_path)
    elif args.threads:
        thread_files = [f"{t}.md" if not t.endswith(".md") else t for t in args.threads]
    else:
        print("Error: Specify --all, --thread-list, or --threads", file=sys.stderr)
        parser.print_help()
        return 1

    # Resolve threads_dir (required)
    if not args.threads_dir:
        print("Error: --threads-dir is required", file=sys.stderr)
        parser.print_help()
        return 1
    threads_dir = Path(args.threads_dir)
    if not threads_dir.exists():
        print(f"Error: Threads directory not found: {threads_dir}", file=sys.stderr)
        return 1

    # Resolve code_path (derive from threads_dir if not specified)
    if args.code_path:
        code_path = Path(args.code_path)
    else:
        # Derive code_path from threads_dir by removing -threads suffix
        threads_dir_str = str(threads_dir.resolve())
        if threads_dir_str.endswith("-threads"):
            code_path = Path(threads_dir_str.removesuffix("-threads"))
        else:
            print("Warning: Cannot derive --code-path from --threads-dir (no -threads suffix)", file=sys.stderr)
            print("Using threads-dir as code-path for database name derivation", file=sys.stderr)
            code_path = threads_dir
    database_name = _derive_database_name(code_path)
    print(f"Database: {database_name} (derived from {code_path.name})")

    # Set up Graphiti backend with LLM/embedding configuration
    work_dir = Path(args.work_dir) if args.work_dir else Path.home() / ".watercooler" / "graphiti"
    config = GraphitiConfig(
        work_dir=work_dir,
        test_mode=False,
        database=database_name,
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
    step_start = time.perf_counter()
    print("Checking Graphiti backend health...")
    health = backend.healthcheck()
    if not health.ok:
        print(f"Error: Backend health check failed: {health.details}", file=sys.stderr)
        print("\nMake sure FalkorDB is running: docker run -d -p 6379:6379 falkordb/falkordb:latest", file=sys.stderr)
        return 1
    timings["Health check"] = time.perf_counter() - step_start
    print(f"✓ Backend healthy: {health.details}")

    # Load checkpoint
    step_start = time.perf_counter()
    checkpoint = load_checkpoint(threads_dir)
    resume = args.resume and not args.force
    if checkpoint.entries and resume:
        complete = sum(1 for e in checkpoint.entries.values() if e.status == "complete")
        in_progress = sum(1 for e in checkpoint.entries.values() if e.status == "in_progress")
        print(f"✓ Loaded checkpoint: {complete} complete, {in_progress} in-progress")
    elif args.force:
        print("✓ Force mode: ignoring checkpoint (will still deduplicate)")
        checkpoint = Checkpoint()
    timings["Checkpoint load"] = time.perf_counter() - step_start

    # Build entries with chunks
    step_start = time.perf_counter()
    entries = build_entries_with_chunks(
        threads_dir,
        thread_files,
        max_tokens=args.chunk_max_tokens,
        overlap=args.chunk_overlap,
    )
    timings["Build entries"] = time.perf_counter() - step_start
    print(f"\n✓ Built {len(entries)} entries")

    # Index using chunked episodes
    print("\nIndexing into Graphiti (this may take several minutes)...")
    print("  Each chunk is added as a separate episode with LLM entity extraction.")
    print("  Chunks within an entry are linked for temporal ordering.")
    print("  Deduplication prevents duplicate episodes on re-runs.")

    step_start = time.perf_counter()
    stats = asyncio.run(index_entries_chunked(
        backend, entries, threads_dir, checkpoint, database_name, resume=resume
    ))
    timings["Index entries"] = time.perf_counter() - step_start

    print(f"\n✅ Indexing complete!")
    print(f"  Entries processed: {stats['entries_processed']}")
    print(f"  Entries skipped (checkpoint): {stats['entries_skipped']}")
    print(f"  Entries failed: {stats['entries_failed']}")
    print(f"  Chunks indexed: {stats['chunks_indexed']}")
    print(f"  Chunks deduplicated: {stats['chunks_deduplicated']}")
    if stats["errors"]:
        print(f"  Errors: {len(stats['errors'])}")
        for err in stats["errors"][:5]:
            print(f"    - {err}")
        if len(stats["errors"]) > 5:
            print(f"    ... and {len(stats['errors']) - 5} more")

    # Timing report
    total_elapsed = time.perf_counter() - total_start
    print(f"\nTiming:")
    for step_name, elapsed in timings.items():
        print(f"  {step_name + ':':20} {elapsed:.1f}s")
    print(f"  {'─' * 25}")
    print(f"  {'Total:':20} {total_elapsed:.1f}s")

    # Summary stats
    total_entries = len(entries)
    total_chunks = sum(len(e["chunks"]) for e in entries)
    avg_chunks_per_entry = total_chunks / total_entries if total_entries > 0 else 0
    chunks_indexed = stats["chunks_indexed"]
    chunks_deduped = stats["chunks_deduplicated"]
    index_time = timings.get("Index entries", 0)
    chunks_per_sec = chunks_indexed / index_time if index_time > 0 else 0
    entries_processed = stats["entries_processed"]
    entries_per_min = (entries_processed / index_time * 60) if index_time > 0 else 0
    dedup_pct = (chunks_deduped / (chunks_indexed + chunks_deduped) * 100) if (chunks_indexed + chunks_deduped) > 0 else 0

    # Detailed chunk timing breakdown
    timing_breakdown = stats.get("timing", {})

    print(f"\nSummary:")
    print(f"  Input:             {total_entries} entries, {total_chunks} chunks ({avg_chunks_per_entry:.1f} avg/entry)")
    print(f"  Indexed:           {chunks_indexed} chunks at {chunks_per_sec:.1f} chunks/sec")
    if chunks_deduped > 0:
        print(f"  Deduplicated:      {chunks_deduped} chunks ({dedup_pct:.1f}%)")
    print(f"  Throughput:        {entries_per_min:.1f} entries/min")

    # Chunk timing breakdown (if available)
    if timing_breakdown:
        print(f"\nChunk Timing Breakdown:")
        print(f"  Avg per chunk:     {timing_breakdown.get('avg_total', 0):.2f}s")
        print(f"    └─ Dedup check:  {timing_breakdown.get('avg_dedup', 0):.3f}s")
        print(f"    └─ Index (LLM):  {timing_breakdown.get('avg_index', 0):.2f}s")
        print(f"    └─ Checkpoint:   {timing_breakdown.get('avg_checkpoint', 0):.3f}s")
        min_time = timing_breakdown.get('min_total', 0)
        max_time = timing_breakdown.get('max_total', 0)
        if min_time > 0 and max_time > 0:
            print(f"  Range:             {min_time:.2f}s - {max_time:.2f}s")

    print(f"\nWork directory: {work_dir}")
    print(f"Checkpoint: {threads_dir / '.migration_checkpoint.json'}")
    print("\nYou can now query via MCP:")
    print('  watercooler_query_memory(query="your question", code_path=".", limit=10)')

    return 0 if stats["entries_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
