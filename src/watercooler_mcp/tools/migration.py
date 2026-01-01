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

logger = logging.getLogger(__name__)


# Module-level references to registered tools
migrate_to_memory_backend = None
migration_preflight = None


def _check_backend_availability(backend: str) -> Dict[str, Any]:
    """Check if the target memory backend is available.

    Args:
        backend: Backend name ("graphiti" or "leanrag")

    Returns:
        Dict with "available" bool and optional version/error info
    """
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


def _parse_thread_entries(thread_path: Path) -> List[Dict[str, Any]]:
    """Parse entries from a thread markdown file.

    Args:
        thread_path: Path to thread file

    Returns:
        List of entry dicts with id, content, timestamp, etc.
    """
    content = thread_path.read_text()
    entries = []

    # Split by entry separator
    parts = re.split(r"\n---\n", content)

    for i, part in enumerate(parts[1:], 1):  # Skip header
        entry = {"index": i, "topic": thread_path.stem}

        # Parse entry metadata
        id_match = re.search(r"\*\*ID\*\*:\s*(\S+)", part)
        if id_match:
            entry["id"] = id_match.group(1)
        else:
            entry["id"] = f"{thread_path.stem}-{i}"

        timestamp_match = re.search(r"\*\*Timestamp\*\*:\s*(\S+)", part)
        if timestamp_match:
            entry["timestamp"] = timestamp_match.group(1)

        agent_match = re.search(r"\*\*Agent\*\*:\s*(.+)", part)
        if agent_match:
            entry["agent"] = agent_match.group(1).strip()

        role_match = re.search(r"\*\*Role\*\*:\s*(\w+)", part)
        if role_match:
            entry["role"] = role_match.group(1)

        type_match = re.search(r"\*\*Type\*\*:\s*(\w+)", part)
        if type_match:
            entry["entry_type"] = type_match.group(1)

        # Extract body content (everything after metadata block)
        body_match = re.search(r"\n\n(.+)", part, re.DOTALL)
        if body_match:
            entry["body"] = body_match.group(1).strip()
        else:
            entry["body"] = ""

        if entry.get("body"):  # Only include entries with content
            entries.append(entry)

    return entries


def _get_thread_status(thread_path: Path) -> str:
    """Get thread status from file.

    Args:
        thread_path: Path to thread file

    Returns:
        Status string or "UNKNOWN"
    """
    try:
        content = thread_path.read_text()
        status_match = re.search(r"^Status:\s*(\w+)", content, re.MULTILINE)
        if status_match:
            return status_match.group(1).upper()
    except Exception:
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
                entries = _parse_thread_entries(thread_file)
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
        if checkpoint.get("backend") == backend:
            migrated_entries = checkpoint.get("migrated_entries", [])

    # Collect entries to migrate
    would_migrate: List[Dict[str, Any]] = []

    for thread_file in thread_files:
        # Check if thread is closed
        if skip_closed and _get_thread_status(thread_file) == "CLOSED":
            continue

        try:
            entries = _parse_thread_entries(thread_file)
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
            entries = _parse_thread_entries(thread_file)
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
