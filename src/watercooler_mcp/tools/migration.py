"""Migration tools for watercooler MCP server.

Tools:
- watercooler_migration_preflight: Check migration prerequisites
- watercooler_migrate_to_memory_backend: Migrate entries to memory backend
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import Context

from watercooler.thread_entries import parse_thread_entries, ThreadEntry
from watercooler_memory.backends import TransientError, BackendError
from watercooler_memory.backends.graphiti import _derive_database_name
from watercooler_memory.chunker import chunk_entry, ChunkerConfig, ChunkNode
from watercooler_memory.schema import EntryNode

logger = logging.getLogger(__name__)

# Valid backend names for migration
VALID_BACKENDS = frozenset({"graphiti", "leanrag"})

# =============================================================================
# Chunking Configuration
# =============================================================================
#
# These defaults are tuned for Graphiti's episode processing pipeline:
#
# DEFAULT_CHUNK_MAX_TOKENS = 768
#   - Balances context retention with manageable LLM processing
#   - ~2-3 paragraphs of typical prose
#   - Large enough for meaningful entity extraction per chunk
#   - Small enough to avoid Graphiti's context window constraints
#   - Aligns with typical RAG chunk sizes (512-1024 range)
#
# DEFAULT_CHUNK_OVERLAP = 64
#   - ~8% overlap ratio (64/768)
#   - Ensures entity/relationship continuity across chunk boundaries
#   - Prevents "lost" entities that span chunk breaks
#   - Low enough to avoid excessive redundancy
#
DEFAULT_CHUNK_MAX_TOKENS = 768
DEFAULT_CHUNK_OVERLAP = 64


# Module-level references to registered tools
migrate_to_memory_backend = None
migration_preflight = None


# --- Checkpoint v2 Data Structures ---

@dataclass
class ChunkProgress:
    """Progress tracking for a single chunk."""
    chunk_index: int
    chunk_id: str
    episode_uuid: str


@dataclass
class EntryProgress:
    """Progress tracking for a single entry migration.

    Tracks chunk-by-chunk progress for resumable migration.
    """
    thread_id: str
    status: str  # "in_progress" | "complete"
    total_chunks: int
    last_completed_chunk_index: int  # -1 when none completed
    chunks: List[ChunkProgress] = field(default_factory=list)
    last_updated_at: str = ""
    run_id: str = ""
    mode: str = "chunked"  # "chunked" | "single"

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
            "mode": self.mode,
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
            mode=d.get("mode", "single"),
        )


@dataclass
class CheckpointV2:
    """Checkpoint v2 format with entry-centric structure for chunk resumption."""
    version: int = 2
    backend: str = ""
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
    def from_dict(cls, d: Dict[str, Any]) -> "CheckpointV2":
        entries = {
            eid: EntryProgress.from_dict(ep_dict)
            for eid, ep_dict in d.get("entries", {}).items()
        }
        return cls(
            version=d.get("version", 2),
            backend=d.get("backend", ""),
            entries=entries,
            run_id=d.get("run_id", ""),
        )

    @classmethod
    def from_v1(cls, v1_data: Dict[str, Any]) -> "CheckpointV2":
        """Convert v1 checkpoint to v2 format.

        V1 entries are treated as complete single-episode migrations.
        """
        checkpoint = cls(
            version=2,
            backend=v1_data.get("backend", ""),
        )
        for entry_id in v1_data.get("migrated_entries", []):
            checkpoint.entries[entry_id] = EntryProgress(
                thread_id="",  # Unknown in v1
                status="complete",
                total_chunks=1,
                last_completed_chunk_index=0,
                chunks=[],
                mode="single",
            )
        return checkpoint

    def is_entry_complete(self, entry_id: str) -> bool:
        """Check if an entry is fully migrated."""
        ep = self.entries.get(entry_id)
        return ep is not None and ep.status == "complete"

    def get_resume_chunk_index(self, entry_id: str) -> int:
        """Get the chunk index to resume from for an entry.

        Returns 0 for new entries, or last_completed_chunk_index + 1 for resuming.
        """
        ep = self.entries.get(entry_id)
        if ep is None:
            return 0
        return ep.last_completed_chunk_index + 1

    def get_previous_episode_uuids(self, entry_id: str) -> List[str]:
        """Get episode UUIDs for previous chunks of an entry.

        Used to build the previous_episode_uuids chain for Graphiti.
        """
        ep = self.entries.get(entry_id)
        if ep is None or not ep.chunks:
            return []
        return [c.episode_uuid for c in ep.chunks]


def _entry_dict_to_node(entry: Dict[str, Any], index: int = 0) -> EntryNode:
    """Convert a parsed entry dict to an EntryNode.

    Args:
        entry: Parsed entry dict from _parse_thread_entries_from_file
        index: Entry index within thread

    Returns:
        EntryNode suitable for chunking
    """
    return EntryNode(
        entry_id=entry.get("id", f"unknown-{index}"),
        thread_id=entry.get("topic", ""),
        index=index,
        agent=entry.get("agent"),
        role=entry.get("role"),
        entry_type=entry.get("entry_type"),
        title=entry.get("title"),
        timestamp=entry.get("timestamp"),
        body=entry.get("body", ""),
    )


def _chunk_entry_for_migration(
    entry: Dict[str, Any],
    index: int = 0,
    max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[ChunkNode]:
    """Chunk an entry for migration using watercooler_preset.

    Uses the same chunking approach as index_graphiti.py:
    - ChunkerConfig.watercooler_preset() with include_header=True
    - Creates header chunk with metadata followed by body chunks
    - Returns ChunkNode objects with full metadata

    Args:
        entry: Parsed entry dict from _parse_thread_entries_from_file
        index: Entry index within thread
        max_tokens: Maximum tokens per chunk
        overlap: Overlap tokens between chunks

    Returns:
        List of ChunkNode objects (header chunk first, then body chunks)
    """
    # Convert to EntryNode for chunk_entry()
    entry_node = _entry_dict_to_node(entry, index)

    # Use watercooler_preset for consistent chunking with MemoryGraph/LeanRAG
    config = ChunkerConfig.watercooler_preset(
        max_tokens=max_tokens,
        overlap=overlap,
    )

    # chunk_entry returns ChunkNode objects with header chunk first
    return chunk_entry(entry_node, config)


def _validate_backend(backend: str) -> Optional[str]:
    """Validate the backend parameter.

    Args:
        backend: Backend name to validate

    Returns:
        Error message if invalid, None if valid
    """
    if not backend:
        return "Backend parameter is required"
    if backend not in VALID_BACKENDS:
        return f"Invalid backend '{backend}'. Valid options: {', '.join(sorted(VALID_BACKENDS))}"
    return None


def _check_backend_availability(backend: str, code_path: str = "") -> Dict[str, Any]:
    """Check if the target memory backend is available.

    Args:
        backend: Backend name ("graphiti" or "leanrag")
        code_path: Path to code repository (for database name derivation)

    Returns:
        Dict with "available" bool and optional version/error info
    """
    # Validate backend first
    validation_error = _validate_backend(backend)
    if validation_error:
        return {"available": False, "error": validation_error}

    if backend == "graphiti":
        try:
            from .. import memory as mem

            config = mem.load_graphiti_config(code_path=code_path)
            if not config:
                return {"available": False, "error": "Graphiti not enabled"}

            # Don't create a full backend here - just check config is valid
            # Creating a backend opens FalkorDB connections that can interfere
            # with the actual migration backend created later
            return {"available": True, "version": "1.0.0"}
        except Exception as e:
            return {"available": False, "error": str(e)}

    elif backend == "leanrag":
        # LeanRAG availability check
        return {"available": False, "error": "LeanRAG migration not yet implemented"}

    # Unreachable after validation, but kept for safety
    return {"available": False, "error": f"Unknown backend: {backend}"}


def _get_migration_backend(backend: str, code_path: str = ""):
    """Get the migration backend instance.

    Args:
        backend: Backend name
        code_path: Path to code repository (for database name derivation)

    Returns:
        Backend instance or None
    """
    if backend == "graphiti":
        from .. import memory as mem

        config = mem.load_graphiti_config(code_path=code_path)
        if not config:
            return None
        return mem.get_graphiti_backend(config)

    return None


def _parse_thread_entries_from_file(thread_path: Path) -> List[Dict[str, Any]]:
    """Parse entries from a thread markdown file using the robust parser.

    Uses the well-tested parse_thread_entries() from watercooler.thread_entries
    which properly handles code blocks, deduplication, and edge cases.

    Args:
        thread_path: Path to thread file

    Returns:
        List of entry dicts with id, content, timestamp, etc.
    """
    try:
        content = thread_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, IOError, UnicodeDecodeError) as e:
        logger.warning(f"Failed to read thread file {thread_path}: {e}")
        return []

    # Use the robust parser from thread_entries module
    parsed_entries: List[ThreadEntry] = parse_thread_entries(content)
    entries: List[Dict[str, Any]] = []

    for entry in parsed_entries:
        # Skip entries without body content
        if not entry.body or not entry.body.strip():
            continue

        entry_dict: Dict[str, Any] = {
            "index": entry.index,
            "topic": thread_path.stem,
            "id": entry.entry_id or f"{thread_path.stem}-{entry.index}",
            "body": entry.body.strip(),
        }

        # Add optional fields if present
        if entry.timestamp:
            entry_dict["timestamp"] = entry.timestamp
        if entry.agent:
            entry_dict["agent"] = entry.agent
        if entry.role:
            entry_dict["role"] = entry.role
        if entry.entry_type:
            entry_dict["entry_type"] = entry.entry_type
        if entry.title:
            entry_dict["title"] = entry.title

        entries.append(entry_dict)

    return entries


def _get_thread_status(thread_path: Path) -> str:
    """Get thread status from file.

    Args:
        thread_path: Path to thread file

    Returns:
        Status string or "UNKNOWN"
    """
    try:
        content = thread_path.read_text(encoding="utf-8", errors="replace")
        status_match = re.search(r"^Status:\s*(\w+)", content, re.MULTILINE)
        if status_match:
            return status_match.group(1).upper()
    except (OSError, IOError, UnicodeDecodeError):
        pass
    return "UNKNOWN"


def _load_checkpoint(threads_dir: Path) -> CheckpointV2:
    """Load migration checkpoint if exists.

    Handles both v1 and v2 formats, auto-upgrading v1 to v2.

    Args:
        threads_dir: Threads directory

    Returns:
        CheckpointV2 instance (empty if no checkpoint exists)
    """
    checkpoint_file = threads_dir / ".migration_checkpoint.json"
    if checkpoint_file.exists():
        try:
            data = json.loads(checkpoint_file.read_text())
            version = data.get("version", 1)
            if version >= 2:
                return CheckpointV2.from_dict(data)
            else:
                # Convert v1 to v2
                return CheckpointV2.from_v1(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load checkpoint: {e}")
    return CheckpointV2()


def _save_checkpoint_v2(
    threads_dir: Path,
    checkpoint: CheckpointV2,
) -> None:
    """Save migration checkpoint atomically with fsync.

    Uses atomic write (temp file + rename) for durability.
    Flushes and fsyncs for crash safety.

    Args:
        threads_dir: Threads directory
        checkpoint: CheckpointV2 instance to save
    """
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
        # Set restrictive permissions (owner read/write only)
        checkpoint_file.chmod(0o600)
    except Exception:
        # Clean up temp file on failure
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def _save_checkpoint(
    threads_dir: Path,
    migrated_entries: List[str],
    backend: str,
) -> None:
    """Save migration checkpoint (v1 compatibility wrapper).

    DEPRECATED: Use _save_checkpoint_v2 for new code.

    Args:
        threads_dir: Threads directory
        migrated_entries: List of migrated entry IDs
        backend: Backend name
    """
    # Convert to v2 format
    checkpoint = CheckpointV2(backend=backend)
    for entry_id in migrated_entries:
        checkpoint.entries[entry_id] = EntryProgress(
            thread_id="",
            status="complete",
            total_chunks=1,
            last_completed_chunk_index=0,
            chunks=[],
            mode="single",
        )
    _save_checkpoint_v2(threads_dir, checkpoint)


async def _migration_preflight_impl(
    threads_dir: Path,
    backend: str,
    ctx: Context,
    code_path: str = "",
) -> str:
    """Check migration prerequisites.

    Args:
        threads_dir: Path to threads directory
        backend: Target backend ("graphiti" or "leanrag")
        ctx: MCP context
        code_path: Path to code repository (for database name derivation)

    Returns:
        JSON with preflight check results
    """
    result: Dict[str, Any] = {
        "threads_dir_exists": False,
        "thread_count": 0,
        "estimated_entries": 0,
        "backend_available": False,
        "ready": False,
        "issues": [],
    }

    # Check threads directory
    if threads_dir.exists():
        result["threads_dir_exists"] = True

        # Count threads and estimate entries
        thread_files = list(threads_dir.glob("*.md"))
        result["thread_count"] = len(thread_files)

        total_entries = 0
        for thread_file in thread_files:
            try:
                entries = _parse_thread_entries_from_file(thread_file)
                total_entries += len(entries)
            except Exception as e:
                result["issues"].append(f"Error parsing {thread_file.name}: {e}")

        result["estimated_entries"] = total_entries
    else:
        result["issues"].append(f"Threads directory not found: {threads_dir}")

    # Check backend availability
    backend_check = _check_backend_availability(backend, code_path=code_path)
    result["backend_available"] = backend_check.get("available", False)
    if not result["backend_available"]:
        result["issues"].append(
            f"Backend {backend} unavailable: {backend_check.get('error', 'unknown')}"
        )
    else:
        result["backend_version"] = backend_check.get("version")

    # Check for existing checkpoint (v2 format)
    checkpoint = _load_checkpoint(threads_dir)
    if checkpoint.backend:
        result["has_checkpoint"] = True
        result["checkpoint_entries"] = len(checkpoint.entries)
        result["checkpoint_version"] = checkpoint.version
        # Count entries by status
        complete_count = sum(1 for e in checkpoint.entries.values() if e.status == "complete")
        in_progress_count = sum(1 for e in checkpoint.entries.values() if e.status == "in_progress")
        result["checkpoint_complete"] = complete_count
        result["checkpoint_in_progress"] = in_progress_count
    else:
        result["has_checkpoint"] = False

    # Determine if ready
    result["ready"] = (
        result["threads_dir_exists"]
        and result["backend_available"]
        and result["thread_count"] > 0
    )

    return json.dumps(result, indent=2)


async def _migrate_to_memory_backend_impl(
    threads_dir: Path,
    backend: str,
    ctx: Context,
    code_path: str = "",
    dry_run: bool = True,
    topics: str = "",
    skip_closed: bool = False,
    resume: bool = True,
    force_new_migration: bool = False,
    chunk_max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    rechunk: bool = False,
) -> str:
    """Migrate thread entries to memory backend with chunking support.

    Entries are chunked using watercooler's chunker for aligned chunk boundaries
    across MemoryGraph, LeanRAG, and Graphiti. Episodes within an entry are
    linked using Graphiti's previous_episode_uuids for explicit temporal ordering.

    Args:
        threads_dir: Path to threads directory
        backend: Target backend ("graphiti" or "leanrag")
        ctx: MCP context
        code_path: Path to code repository (for database name derivation)
        dry_run: If True, show what would be migrated without executing
        topics: Comma-separated list of topics to migrate (empty = all)
        skip_closed: Skip closed threads
        resume: Resume from checkpoint if available
        force_new_migration: If True, ignore checkpoint backend mismatch
        chunk_max_tokens: Maximum tokens per chunk (default: 768)
        chunk_overlap: Overlap tokens between chunks (default: 64)
        rechunk: If True, re-migrate entries that were previously migrated as single episodes

    Returns:
        JSON with migration results
    """
    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "backend": backend,
        "entries_migrated": 0,
        "entries_failed": 0,
        "entries_skipped": 0,
        "chunks_migrated": 0,
        "chunks_deduplicated": 0,
        "threads_processed": 0,
        "errors": [],
    }

    # Check backend availability (config-only check, no backend creation)
    import sys
    backend_check = _check_backend_availability(backend, code_path=code_path)
    if not backend_check.get("available"):
        result["success"] = False
        result["error"] = f"Backend unavailable: {backend_check.get('error')}"
        return json.dumps(result, indent=2)

    # Derive unified group_id from code_path (all threads share the same project database)
    # This allows entities to be naturally shared across threads within the same project
    unified_group_id = _derive_database_name(code_path)
    result["unified_group_id"] = unified_group_id

    # Get thread files
    thread_files = list(threads_dir.glob("*.md"))

    # Filter by topics if specified
    if topics:
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        thread_files = [f for f in thread_files if f.stem in topic_list]

    # Load checkpoint for resume (now returns CheckpointV2)
    checkpoint = _load_checkpoint(threads_dir)
    if resume and checkpoint.backend:
        if checkpoint.backend == backend:
            result["resumed_from_checkpoint"] = True
            result["checkpoint_entries"] = len(checkpoint.entries)
        elif force_new_migration:
            # User explicitly wants to start fresh
            logger.info(
                f"Ignoring checkpoint for backend '{checkpoint.backend}' - "
                f"starting fresh migration to '{backend}' (force_new_migration=True)"
            )
            result["checkpoint_ignored"] = True
            result["checkpoint_backend"] = checkpoint.backend
            checkpoint = CheckpointV2(backend=backend)
        else:
            # Fail to prevent accidental duplicate migrations
            result["success"] = False
            result["error"] = (
                f"Checkpoint exists for backend '{checkpoint.backend}' but targeting '{backend}'. "
                f"Either delete .migration_checkpoint.json or use force_new_migration=True to override."
            )
            return json.dumps(result, indent=2)
    else:
        # No checkpoint or not resuming - start fresh
        checkpoint = CheckpointV2(backend=backend)

    # Collect entries to migrate
    would_migrate: List[Dict[str, Any]] = []

    for thread_file in thread_files:
        # Check if thread is closed
        if skip_closed and _get_thread_status(thread_file) == "CLOSED":
            continue

        try:
            entries = _parse_thread_entries_from_file(thread_file)
            for entry_idx, entry in enumerate(entries):
                entry_id = entry.get("id", "")
                body = entry.get("body", "").strip()

                # Check if already migrated
                if checkpoint.is_entry_complete(entry_id):
                    entry_progress = checkpoint.entries.get(entry_id)
                    # Skip unless rechunk requested and entry was single-episode
                    if not rechunk or (entry_progress and entry_progress.mode != "single"):
                        result["entries_skipped"] += 1
                        continue

                # Estimate chunks for dry run (uses watercooler_preset with header)
                chunks = _chunk_entry_for_migration(entry, entry_idx, chunk_max_tokens, chunk_overlap)

                would_migrate.append({
                    "topic": entry.get("topic"),
                    "entry_id": entry_id,
                    "timestamp": entry.get("timestamp"),
                    "agent": entry.get("agent"),
                    "body_preview": body[:100] if body else "",
                    "estimated_chunks": len(chunks),
                    "has_header_chunk": len(chunks) > 0 and chunks[0].text.startswith("agent:"),
                })

            result["threads_processed"] += 1

        except Exception as e:
            result["errors"].append(f"Error processing {thread_file.name}: {e}")

    result["would_migrate"] = would_migrate
    result["estimated_total_chunks"] = sum(e.get("estimated_chunks", 1) for e in would_migrate)

    # If dry run, return without executing
    if dry_run:
        result["success"] = True
        return json.dumps(result, indent=2)

    # Execute actual migration
    migration_backend = _get_migration_backend(backend, code_path=code_path)
    if not migration_backend:
        result["success"] = False
        result["error"] = "Failed to get migration backend"
        return json.dumps(result, indent=2)

    # Process entries with chunking
    for thread_file in thread_files:
        if skip_closed and _get_thread_status(thread_file) == "CLOSED":
            continue

        try:
            entries = _parse_thread_entries_from_file(thread_file)
            for entry_idx, entry in enumerate(entries):
                entry_id = entry.get("id", "")

                # Check if already complete
                if checkpoint.is_entry_complete(entry_id):
                    entry_progress = checkpoint.entries.get(entry_id)
                    if not rechunk or (entry_progress and entry_progress.mode != "single"):
                        continue

                # Validate required fields before calling backend
                body = entry.get("body", "").strip()
                topic = entry.get("topic", "").strip()

                if not body or not topic:
                    logger.warning(f"Skipping entry {entry_id}: missing required fields (body or topic)")
                    result["entries_skipped"] += 1
                    continue

                # Parse timestamp for reference_time
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
                if title and title.strip():
                    base_name = title.strip()
                elif body:
                    body_snippet = body[:50].replace('\n', ' ').strip()
                    base_name = body_snippet + ("..." if len(body) > 50 else "")
                else:
                    base_name = f"Entry {entry_id}" if entry_id else "Untitled Entry"

                # Build source description from entry metadata
                # Include thread topic for traceability (group_id is now unified project-level)
                agent = entry.get("agent", "Unknown")
                role = entry.get("role", "")
                entry_type = entry.get("entry_type", "Note")
                base_source_desc = f"thread:{topic} | Migration: {agent}"
                if role:
                    base_source_desc += f" ({role})"

                # Chunk entry using watercooler_preset (header chunk + body chunks)
                chunks = _chunk_entry_for_migration(entry, entry_idx, chunk_max_tokens, chunk_overlap)
                total_chunks = len(chunks)

                # Get or create entry progress
                if entry_id not in checkpoint.entries:
                    checkpoint.entries[entry_id] = EntryProgress(
                        thread_id=topic,
                        status="in_progress",
                        total_chunks=total_chunks,
                        last_completed_chunk_index=-1,
                        chunks=[],
                        run_id=checkpoint.run_id,
                        mode="chunked" if total_chunks > 1 else "single",
                    )

                entry_progress = checkpoint.entries[entry_id]
                start_chunk_index = checkpoint.get_resume_chunk_index(entry_id)

                # Process chunks sequentially (critical for episode linking)
                entry_failed = False
                for i, chunk in enumerate(chunks):
                    # Skip already completed chunks
                    if i < start_chunk_index:
                        continue

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
                        if i > 0 and entry_progress.chunks:
                            # Link to the previous chunk's episode
                            previous_uuids = [entry_progress.chunks[-1].episode_uuid]

                        # DEDUPLICATION: Check if this chunk already exists in the graph
                        # This prevents duplicates if checkpoint is lost/deleted or migration is re-run
                        # Use async version to avoid event loop conflicts in MCP context
                        existing_episode = await migration_backend.find_episode_by_chunk_id_async(
                            chunk_id=chunk.chunk_id,
                            group_id=unified_group_id,
                        )

                        if existing_episode:
                            # Episode already exists - use existing UUID
                            episode_uuid = existing_episode["uuid"]
                            logger.info(
                                f"Dedup: Entry {entry_id} chunk {i + 1}/{total_chunks} "
                                f"already exists as episode {episode_uuid}"
                            )
                            result["chunks_deduplicated"] += 1
                        else:
                            # Call backend to add episode (SEQUENTIAL await - critical!)
                            ep_result = await migration_backend.add_episode_direct(
                                name=episode_name,
                                episode_body=chunk.text,
                                source_description=source_desc,
                                reference_time=ref_time,
                                group_id=unified_group_id,
                                previous_episode_uuids=previous_uuids,
                            )

                            episode_uuid = ep_result.get("episode_uuid", "unknown")

                        # Update progress
                        entry_progress.chunks.append(ChunkProgress(
                            chunk_index=i,
                            chunk_id=chunk.chunk_id,
                            episode_uuid=episode_uuid,
                        ))
                        entry_progress.last_completed_chunk_index = i
                        entry_progress.last_updated_at = datetime.now(timezone.utc).isoformat()

                        # Save checkpoint after each chunk for durability
                        _save_checkpoint_v2(threads_dir, checkpoint)

                        result["chunks_migrated"] += 1

                        logger.debug(
                            f"Migrated entry {entry_id} chunk {i + 1}/{total_chunks} -> "
                            f"episode {episode_uuid}"
                        )

                    except (TransientError, BackendError, ConnectionError, TimeoutError, OSError) as e:
                        # On failure, stop processing this entry (atomic entry migration)
                        logger.warning(f"Error migrating entry {entry_id} chunk {i + 1}/{total_chunks}: {e}")
                        result["errors"].append(f"Entry {entry_id} chunk {i + 1}: {type(e).__name__}: {e}")
                        entry_failed = True
                        break
                    except Exception as e:
                        logger.exception(f"Failed to migrate entry {entry_id} chunk {i + 1}/{total_chunks}")
                        result["errors"].append(f"Entry {entry_id} chunk {i + 1}: {type(e).__name__}: {e}")
                        entry_failed = True
                        break

                # Mark entry complete or failed
                if entry_failed:
                    result["entries_failed"] += 1
                else:
                    entry_progress.status = "complete"
                    _save_checkpoint_v2(threads_dir, checkpoint)
                    result["entries_migrated"] += 1

        except (OSError, IOError) as e:
            # File errors
            logger.warning(f"File error processing thread {thread_file.name}: {e}")
            result["errors"].append(f"Thread {thread_file.name}: {type(e).__name__}: {e}")
        except Exception as e:
            # Unexpected error
            logger.exception(f"Failed to process thread {thread_file.name}")
            result["errors"].append(f"Thread {thread_file.name}: {type(e).__name__}: {e}")

    result["success"] = result["entries_failed"] == 0
    # Remove would_migrate from actual run (already processed)
    result.pop("would_migrate", None)

    return json.dumps(result, indent=2)


def register_migration_tools(mcp):
    """Register migration tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global migrate_to_memory_backend, migration_preflight

    from .. import validation

    async def preflight_wrapper(
        ctx: Context,
        code_path: str = "",
        backend: str = "graphiti",
    ) -> str:
        """Check migration prerequisites.

        Args:
            code_path: Path to code repository
            backend: Target backend ("graphiti" or "leanrag")

        Returns:
            JSON with preflight check results
        """
        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return json.dumps({
                "success": False,
                "error": "Unable to resolve threads directory",
            })

        return await _migration_preflight_impl(
            threads_dir=context.threads_dir,
            backend=backend,
            ctx=ctx,
            code_path=code_path,
        )

    async def migrate_wrapper(
        ctx: Context,
        code_path: str = "",
        backend: str = "graphiti",
        dry_run: bool = True,
        topics: str = "",
        skip_closed: bool = False,
        resume: bool = True,
        force_new_migration: bool = False,
        chunk_max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        rechunk: bool = False,
    ) -> str:
        """Migrate thread entries to memory backend with chunking support.

        Entries are chunked for aligned boundaries with MemoryGraph and LeanRAG.
        Episodes within an entry are linked for proper temporal ordering.

        Args:
            code_path: Path to code repository
            backend: Target backend ("graphiti" or "leanrag")
            dry_run: If True, show what would be migrated without executing
            topics: Comma-separated list of topics to migrate (empty = all)
            skip_closed: Skip closed threads
            resume: Resume from checkpoint if available
            force_new_migration: If True, ignore checkpoint backend mismatch
            chunk_max_tokens: Maximum tokens per chunk (default: 768)
            chunk_overlap: Overlap tokens between chunks (default: 64)
            rechunk: If True, re-migrate entries previously migrated as single episodes

        Returns:
            JSON with migration results
        """
        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return json.dumps({
                "success": False,
                "error": "Unable to resolve threads directory",
            })

        return await _migrate_to_memory_backend_impl(
            threads_dir=context.threads_dir,
            backend=backend,
            ctx=ctx,
            code_path=code_path,
            dry_run=dry_run,
            topics=topics,
            skip_closed=skip_closed,
            resume=resume,
            force_new_migration=force_new_migration,
            chunk_max_tokens=chunk_max_tokens,
            chunk_overlap=chunk_overlap,
            rechunk=rechunk,
        )

    # Register tools
    migration_preflight = mcp.tool(name="watercooler_migration_preflight")(preflight_wrapper)
    migrate_to_memory_backend = mcp.tool(name="watercooler_migrate_to_memory_backend")(migrate_wrapper)
