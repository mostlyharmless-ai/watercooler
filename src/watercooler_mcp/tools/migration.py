"""Migration tools for watercooler MCP server.

Tools:
- watercooler_migration_preflight: Check migration prerequisites
- watercooler_migrate_to_memory_backend: Migrate entries to memory backend
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import Context

from watercooler.thread_entries import parse_thread_entries, ThreadEntry

logger = logging.getLogger(__name__)

# Valid backend names for migration
VALID_BACKENDS = frozenset({"graphiti", "leanrag"})


# Module-level references to registered tools
migrate_to_memory_backend = None
migration_preflight = None


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


def _check_backend_availability(backend: str) -> Dict[str, Any]:
    """Check if the target memory backend is available.

    Args:
        backend: Backend name ("graphiti" or "leanrag")

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

            config = mem.load_graphiti_config()
            if not config:
                return {"available": False, "error": "Graphiti not enabled"}

            graphiti_backend = mem.get_graphiti_backend()
            if not graphiti_backend:
                return {"available": False, "error": "Graphiti backend unavailable"}

            return {"available": True, "version": "1.0.0"}
        except Exception as e:
            return {"available": False, "error": str(e)}

    elif backend == "leanrag":
        # LeanRAG availability check
        return {"available": False, "error": "LeanRAG migration not yet implemented"}

    # Unreachable after validation, but kept for safety
    return {"available": False, "error": f"Unknown backend: {backend}"}


def _get_migration_backend(backend: str):
    """Get the migration backend instance.

    Args:
        backend: Backend name

    Returns:
        Backend instance or None
    """
    if backend == "graphiti":
        from .. import memory as mem

        return mem.get_graphiti_backend()

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


def _load_checkpoint(threads_dir: Path) -> Dict[str, Any]:
    """Load migration checkpoint if exists.

    Args:
        threads_dir: Threads directory

    Returns:
        Checkpoint data or empty dict
    """
    checkpoint_file = threads_dir / ".migration_checkpoint.json"
    if checkpoint_file.exists():
        try:
            return json.loads(checkpoint_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_checkpoint(
    threads_dir: Path,
    migrated_entries: List[str],
    backend: str,
) -> None:
    """Save migration checkpoint.

    Args:
        threads_dir: Threads directory
        migrated_entries: List of migrated entry IDs
        backend: Backend name
    """
    checkpoint_file = threads_dir / ".migration_checkpoint.json"
    checkpoint = {
        "migrated_entries": migrated_entries,
        "backend": backend,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    checkpoint_file.write_text(json.dumps(checkpoint, indent=2))
    # Set restrictive permissions (owner read/write only)
    checkpoint_file.chmod(0o600)


async def _migration_preflight_impl(
    threads_dir: Path,
    backend: str,
    ctx: Context,
) -> str:
    """Check migration prerequisites.

    Args:
        threads_dir: Path to threads directory
        backend: Target backend ("graphiti" or "leanrag")
        ctx: MCP context

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
    backend_check = _check_backend_availability(backend)
    result["backend_available"] = backend_check.get("available", False)
    if not result["backend_available"]:
        result["issues"].append(
            f"Backend {backend} unavailable: {backend_check.get('error', 'unknown')}"
        )
    else:
        result["backend_version"] = backend_check.get("version")

    # Check for existing checkpoint
    checkpoint = _load_checkpoint(threads_dir)
    if checkpoint:
        result["has_checkpoint"] = True
        result["checkpoint_entries"] = len(checkpoint.get("migrated_entries", []))
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
    dry_run: bool = True,
    topics: str = "",
    skip_closed: bool = False,
    resume: bool = True,
) -> str:
    """Migrate thread entries to memory backend.

    Args:
        threads_dir: Path to threads directory
        backend: Target backend ("graphiti" or "leanrag")
        ctx: MCP context
        dry_run: If True, show what would be migrated without executing
        topics: Comma-separated list of topics to migrate (empty = all)
        skip_closed: Skip closed threads
        resume: Resume from checkpoint if available

    Returns:
        JSON with migration results
    """
    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "backend": backend,
        "entries_migrated": 0,
        "entries_failed": 0,
        "entries_skipped": 0,
        "threads_processed": 0,
        "errors": [],
    }

    # Check backend availability
    backend_check = _check_backend_availability(backend)
    if not backend_check.get("available"):
        result["success"] = False
        result["error"] = f"Backend unavailable: {backend_check.get('error')}"
        return json.dumps(result, indent=2)

    # Get thread files
    thread_files = list(threads_dir.glob("*.md"))

    # Filter by topics if specified
    if topics:
        topic_list = [t.strip() for t in topics.split(",") if t.strip()]
        thread_files = [f for f in thread_files if f.stem in topic_list]

    # Load checkpoint for resume
    migrated_entries: List[str] = []
    if resume:
        checkpoint = _load_checkpoint(threads_dir)
        checkpoint_backend = checkpoint.get("backend")
        if checkpoint_backend == backend:
            migrated_entries = checkpoint.get("migrated_entries", [])
            result["resumed_from_checkpoint"] = True
            result["checkpoint_entries"] = len(migrated_entries)
        elif checkpoint_backend:
            # Checkpoint exists but for different backend - warn user
            logger.warning(
                f"Checkpoint found for backend '{checkpoint_backend}' but targeting '{backend}'. "
                f"Starting fresh migration. Delete .migration_checkpoint.json to suppress this warning."
            )
            result["checkpoint_backend_mismatch"] = True
            result["checkpoint_backend"] = checkpoint_backend

    # Collect entries to migrate
    would_migrate: List[Dict[str, Any]] = []

    for thread_file in thread_files:
        # Check if thread is closed
        if skip_closed and _get_thread_status(thread_file) == "CLOSED":
            continue

        try:
            entries = _parse_thread_entries_from_file(thread_file)
            for entry in entries:
                entry_id = entry.get("id", "")

                # Skip already migrated entries
                if entry_id in migrated_entries:
                    result["entries_skipped"] += 1
                    continue

                would_migrate.append({
                    "topic": entry.get("topic"),
                    "entry_id": entry_id,
                    "timestamp": entry.get("timestamp"),
                    "agent": entry.get("agent"),
                    "body_preview": entry.get("body", "")[:100],
                })

            result["threads_processed"] += 1

        except Exception as e:
            result["errors"].append(f"Error processing {thread_file.name}: {e}")

    result["would_migrate"] = would_migrate

    # If dry run, return without executing
    if dry_run:
        result["success"] = True
        return json.dumps(result, indent=2)

    # Execute actual migration
    migration_backend = _get_migration_backend(backend)
    if not migration_backend:
        result["success"] = False
        result["error"] = "Failed to get migration backend"
        return json.dumps(result, indent=2)

    # Process entries
    for thread_file in thread_files:
        if skip_closed and _get_thread_status(thread_file) == "CLOSED":
            continue

        try:
            entries = _parse_thread_entries_from_file(thread_file)
            for entry in entries:
                entry_id = entry.get("id", "")

                if entry_id in migrated_entries:
                    continue

                try:
                    # Call backend to add episode
                    await migration_backend.add_episode_direct(
                        content=entry.get("body", ""),
                        group_id=entry.get("topic", ""),
                        source_id=entry_id,
                        timestamp=entry.get("timestamp"),
                    )

                    migrated_entries.append(entry_id)
                    result["entries_migrated"] += 1

                except Exception as e:
                    result["entries_failed"] += 1
                    result["errors"].append(f"Entry {entry_id}: {e}")

        except Exception as e:
            result["errors"].append(f"Thread {thread_file.name}: {e}")

    # Save checkpoint
    _save_checkpoint(threads_dir, migrated_entries, backend)

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
        )

    async def migrate_wrapper(
        ctx: Context,
        code_path: str = "",
        backend: str = "graphiti",
        dry_run: bool = True,
        topics: str = "",
        skip_closed: bool = False,
        resume: bool = True,
    ) -> str:
        """Migrate thread entries to memory backend.

        Args:
            code_path: Path to code repository
            backend: Target backend ("graphiti" or "leanrag")
            dry_run: If True, show what would be migrated without executing
            topics: Comma-separated list of topics to migrate (empty = all)
            skip_closed: Skip closed threads
            resume: Resume from checkpoint if available

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
            dry_run=dry_run,
            topics=topics,
            skip_closed=skip_closed,
            resume=resume,
        )

    # Register tools
    migration_preflight = mcp.tool(name="watercooler_migration_preflight")(preflight_wrapper)
    migrate_to_memory_backend = mcp.tool(name="watercooler_migrate_to_memory_backend")(migrate_wrapper)
