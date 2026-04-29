"""Memory backend sync implementations.

This module contains the sync callbacks for memory backends (Graphiti, LeanRAG).
Callbacks are registered at MCP startup via init_memory_sync_callbacks().

Architecture:
    - Callbacks follow the signature defined in baseline_graph.sync.register_memory_sync_callback
    - Each callback handles syncing a single entry to its respective backend
    - Callbacks run in a ThreadPoolExecutor (fire-and-forget)
    - Errors are logged but don't block the main sync flow

Issue #83: This module extracts Graphiti-specific code from baseline_graph/sync.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone as tz
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Lock for thread-safe queue file writes
_queue_lock = threading.Lock()


# =============================================================================
# Runtime context (Plan v20 Phase 1 scaffolding; used from Phase 5 onward)
# =============================================================================
#
# Mirrors the ``_runtime`` module-level pattern in
# :mod:`watercooler_mcp.tools.memory`. Phase 1 introduces the hook and its
# setter; Phase 5 is where the sync callback and executor actually consult
# ``_runtime.surface`` / ``_runtime.premium_client`` to route T2 work to the
# hosted ``/mcp/premium/`` endpoint in ``local_hybrid``.

_runtime: "ToolRuntime | None" = None  # type: ignore[name-defined]  # noqa: F821 — forward ref resolved lazily


def set_runtime(runtime: "ToolRuntime | None") -> None:  # type: ignore[name-defined]  # noqa: F821
    """Set the module-level runtime context.

    Called by :func:`watercooler_mcp.server_factory.build_mcp_server` during
    server construction. Accepting ``None`` is supported for tests that do
    not construct a full ``ToolRuntime``.

    Side effect: flips the core hybrid-T2-handoff signal on
    :mod:`watercooler.baseline_graph.sync` so ``sync_to_memory_backend``
    bypasses the local memory queue in hybrid (the queue's worker cannot
    execute T2 locally). Clearing the runtime resets the flag.
    """
    global _runtime
    _runtime = runtime
    try:
        from watercooler.baseline_graph.sync import set_hybrid_t2_handoff_active
    except ImportError:
        return
    hybrid = (
        runtime is not None
        and getattr(runtime, "surface", None) == "local_hybrid"
        and getattr(runtime, "premium_client", None) is not None
    )
    set_hybrid_t2_handoff_active(hybrid)

    # Round 18/21/25/26: keep the T1 callbacks and T2 handoff flag
    # decided from the same snapshot so they can't end up mismatched.
    # If ANY step of the T1 install fails (import error OR runtime
    # exception), roll back the T2 handoff flag AND log — but don't
    # re-raise. ``set_runtime`` is called from ``build_mcp_server``
    # with no surrounding try/except; a raise here would kill the
    # whole MCP server at startup on a recoverable issue (missing T1
    # callbacks degrade the server to non-hybrid, they don't break it).
    try:
        from .t1_hybrid import install_hybrid_callbacks
        install_hybrid_callbacks(runtime)
    except Exception as e:
        set_hybrid_t2_handoff_active(False)
        logger.warning(
            "MEMORY_SYNC: T1 hybrid install failed (%s); T2 handoff "
            "flag rolled back. Server continues in non-hybrid mode; "
            "T1 semantic writes will use the local path.",
            e,
        )


def get_runtime() -> "ToolRuntime | None":  # type: ignore[name-defined]  # noqa: F821
    """Return the module-level runtime context (may be ``None``)."""
    return _runtime


# Round 17 (MEDIUM): fresh-loop helper moved to _async_utils so the
# t1_hybrid and memory_sync callers share one implementation — future
# fixes (like the round-11 drain) can't drift between copies.
from ._async_utils import run_coro_in_fresh_loop as _run_coro_in_fresh_loop

# TransientError is part of the core watercooler_memory package and must be
# importable whenever the memory system is active.  Import at module level so
# _transport_error_types() always includes it — a deferred ImportError here is
# preferable to silently omitting it and losing backend eviction on transient
# failures.
try:
    from watercooler_memory.backends import TransientError as _TransientError
except ImportError:  # pragma: no cover — package not installed
    _TransientError = None  # type: ignore[assignment,misc]


def _transport_error_types() -> tuple[type, ...]:
    """Return redis-py + project transport exception types for except clauses.

    Returns a tuple containing whichever of the optional error types could be
    imported.  Always includes TransientError when watercooler_memory is
    installed (the normal case).
    """
    types: list[type] = []
    try:
        from redis.exceptions import (
            ConnectionError as _RedisConnErr,
            TimeoutError as _RedisTimeoutErr,
        )
        types.extend([_RedisConnErr, _RedisTimeoutErr])
    except ImportError:
        pass
    if _TransientError is not None:
        types.append(_TransientError)
    return tuple(types)


# ============================================================================
# Graphiti Sync Callback
# ============================================================================


async def _call_graphiti_add_episode(
    content: str,
    topic: str,
    entry_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    title: Optional[str] = None,
    code_path: str = "",
    backend: Optional[Any] = None,
    xrefs: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
    vote_score: int = 0,
    pinned: bool = False,
) -> Dict[str, Any]:
    """Call graphiti_add_episode to sync entry to Graphiti.

    This is the internal async implementation that interfaces with
    the Graphiti backend. Uses unified project group_id (config.database)
    instead of per-thread group_ids, allowing entities to be shared across
    threads within the same project.

    Args:
        content: Entry body text
        topic: Thread topic (included in source_description for traceability)
        entry_id: Entry ID for provenance tracking
        timestamp: Entry timestamp (ISO 8601)
        title: Entry title
        code_path: Path to code repository (for database name derivation)
        backend: Optional pre-initialised GraphitiBackend. When provided
            (the queued-task path), transport errors are re-raised so the
            caller's eviction logic can fire. When None, creates a backend
            from config and returns error dicts for all failures.
        xrefs: Cross-referenced entry IDs from annotations
        tags: Tags from annotations
        vote_score: Net upvote score from annotations
        pinned: Whether the entry is pinned

    Returns:
        Result dict with success status and episode_uuid
    """
    # Capture whether a backend was injected before any fallback creation.
    # Transport errors must re-raise on the injected path so the queue
    # executor can evict the thread-local backend; on the non-injected path
    # (fire-and-forget sync callbacks) we return error dicts instead.
    backend_injected = backend is not None
    try:
        if backend is None:
            from watercooler_mcp import memory as mem

            config = mem.load_graphiti_config(code_path=code_path)
            if config is None:
                return {"success": False, "error": "Graphiti not enabled"}

            backend = mem.get_graphiti_backend(config)
            if backend is None or isinstance(backend, dict):
                error_msg = "Graphiti backend unavailable"
                if isinstance(backend, dict):
                    error_msg = backend.get("message", error_msg)
                return {"success": False, "error": error_msg}

        # Use the group_id already baked into the backend's config (the
        # executor sets config.database = task.group_id before creating it).
        unified_group_id = getattr(getattr(backend, "config", None), "database", None) or ""

        # Pre-flight dedup: skip if already indexed
        if entry_id and getattr(backend, "entry_episode_index", None) is not None:
            if backend.entry_episode_index.has_any_mapping(entry_id):
                logger.debug("MEMORY: Skipping already-indexed entry %s", entry_id)
                return {"success": True, "skipped": True, "skip_reason": "already_indexed"}

        # Parse timestamp
        if timestamp:
            try:
                ref_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                ref_time = datetime.now(tz.utc)
        else:
            ref_time = datetime.now(tz.utc)

        # Create episode title
        episode_title = title if title else content[:50] + ("..." if len(content) > 50 else "")

        # Include thread topic and annotation signals in source_description
        source_parts = [f"thread:{topic}", "Sync from baseline graph"]
        if xrefs:
            source_parts.append(f"xrefs:{','.join(xrefs)}")
        if tags:
            source_parts.append(f"tags:{','.join(tags)}")
        if vote_score != 0:
            source_parts.append(f"vote_score:{vote_score}")
        if pinned:
            source_parts.append("pinned:true")
        source_desc = " | ".join(source_parts)

        # Add episode directly to Graphiti
        result = await backend.add_episode_direct(
            name=episode_title,
            episode_body=content,
            source_description=source_desc,
            reference_time=ref_time,
            group_id=unified_group_id,
        )

        episode_uuid = result.get("episode_uuid", "unknown")

        # Track entry-episode mapping if entry_id provided
        if entry_id and episode_uuid != "unknown":
            backend.index_entry_as_episode(entry_id, episode_uuid, unified_group_id)

        logger.debug(f"MEMORY: Synced entry {entry_id} as episode {episode_uuid}")

        return {
            "success": True,
            "episode_uuid": episode_uuid,
            "entities_extracted": result.get("entities_extracted", []),
        }

    except ImportError as e:
        return {"success": False, "error": f"Memory module unavailable: {e}"}
    except OSError as e:
        if backend_injected:
            raise
        return {"success": False, "error": str(e)}
    except Exception as e:
        if backend_injected and isinstance(e, _transport_error_types()):
            raise
        return {"success": False, "error": str(e)}


async def _call_graphiti_add_episode_chunked(
    content: str,
    topic: str,
    entry_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    title: Optional[str] = None,
    code_path: str = "",
    max_tokens: int = 768,
    overlap: int = 64,
) -> Dict[str, Any]:
    """Call graphiti_add_episode with chunking for large entries.

    Splits the entry body into chunks and creates separate episodes for each,
    linking them via previous_episode_uuids for temporal ordering.

    Args:
        content: Entry body text
        topic: Thread topic (included in source_description for traceability)
        entry_id: Entry ID for provenance tracking
        timestamp: Entry timestamp (ISO 8601)
        title: Entry title
        code_path: Path to code repository (for database name derivation)
        max_tokens: Maximum tokens per chunk
        overlap: Token overlap between chunks

    Returns:
        Result dict with success status, episode_uuids list, and chunk_count
    """
    try:
        from watercooler_memory.chunker import ChunkerConfig, chunk_text
        from watercooler_mcp import memory as mem

        config = mem.load_graphiti_config(code_path=code_path)
        if config is None:
            return {"success": False, "error": "Graphiti not enabled"}

        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            error_msg = "Graphiti backend unavailable"
            if isinstance(backend, dict):
                error_msg = backend.get("message", error_msg)
            return {"success": False, "error": error_msg}

        # Pre-flight dedup: skip entire entry if any chunk already indexed.
        # All-or-nothing semantics: if a crash left partial chunks (e.g. only
        # chunk 0 committed), remaining chunks are NOT retried on the next call.
        # Clear the entry from entry_episode_index manually to force re-indexing.
        if entry_id and backend.entry_episode_index is not None:
            if backend.entry_episode_index.has_any_mapping(entry_id):
                if not backend.entry_episode_index.has_entry(entry_id):
                    # Chunk mapping exists but no full-entry mapping — possible
                    # partial indexing from a prior crash.
                    logger.warning(
                        "MEMORY: Entry %s has partial chunk mapping (crash recovery path). "
                        "Remaining chunks will NOT be re-indexed. Clear index to retry.",
                        entry_id,
                    )
                else:
                    logger.debug("MEMORY: Skipping already-indexed entry %s (chunked)", entry_id)
                return {
                    "success": True, "chunk_count": 0, "total_chunks": 0,
                    "episode_uuids": [], "skipped": True, "skip_reason": "already_indexed",
                }

        # Configure chunking
        chunker_config = ChunkerConfig(
            max_tokens=max_tokens,
            overlap=overlap,
        )

        # Chunk the content
        chunks = chunk_text(content, chunker_config)

        # If single chunk or no chunking needed, fall back to simple sync
        if len(chunks) <= 1:
            return await _call_graphiti_add_episode(
                content=content,
                topic=topic,
                entry_id=entry_id,
                timestamp=timestamp,
                title=title,
                code_path=code_path,
            )

        # Use unified project group_id
        unified_group_id = config.database

        # Parse timestamp
        if timestamp:
            try:
                ref_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                ref_time = datetime.now(tz.utc)
        else:
            ref_time = datetime.now(tz.utc)

        total_chunks = len(chunks)
        episode_uuids: list[str] = []
        entities_extracted: list[str] = []
        previous_episode_uuids: list[str] = []
        failed_chunks: list[int] = []

        for i, (chunk_text_content, token_count) in enumerate(chunks):
            chunk_num = i + 1

            # Create chunk-specific title
            chunk_title = f"{title} [chunk {chunk_num}/{total_chunks}]" if title else f"Entry chunk {chunk_num}/{total_chunks}"

            # Include chunk info in source_description
            source_desc = f"thread:{topic} | entry:{entry_id} | chunk:{chunk_num}/{total_chunks}"

            try:
                # Add episode with link to previous chunks
                result = await backend.add_episode_direct(
                    name=chunk_title,
                    episode_body=chunk_text_content,
                    source_description=source_desc,
                    reference_time=ref_time,
                    group_id=unified_group_id,
                    previous_episode_uuids=previous_episode_uuids.copy() if previous_episode_uuids else None,
                )

                episode_uuid = result.get("episode_uuid", "unknown")
                if episode_uuid != "unknown":
                    episode_uuids.append(episode_uuid)
                    # Link next chunk to this one
                    previous_episode_uuids = [episode_uuid]

                    # Track chunk mapping if entry_id provided and index available
                    if entry_id and backend.entry_episode_index is not None:
                        # Generate a chunk_id based on entry_id and chunk content
                        chunk_id = hashlib.sha256(
                            f"{entry_id}:{i}:{chunk_text_content[:100]}".encode()
                        ).hexdigest()[:16]

                        backend.entry_episode_index.add_chunk_mapping(
                            chunk_id=chunk_id,
                            episode_uuid=episode_uuid,
                            entry_id=entry_id,
                            thread_id=topic,
                            chunk_index=i,
                            total_chunks=total_chunks,
                        )

                entities = result.get("entities_extracted", [])
                if entities:
                    entities_extracted.extend(entities)

            except Exception as e:
                logger.warning(
                    f"MEMORY: Failed to sync chunk {chunk_num}/{total_chunks} "
                    f"for {topic}/{entry_id}: {e}"
                )
                failed_chunks.append(chunk_num)
                continue

        # Save index after all chunks (if any were successful)
        if episode_uuids and entry_id and backend.entry_episode_index is not None:
            try:
                backend.entry_episode_index.save()
            except Exception as e:
                logger.warning(f"MEMORY: Failed to save entry_episode_index: {e}")

        # Consider success if at least one chunk was indexed
        if episode_uuids:
            logger.debug(
                f"MEMORY: Synced entry {entry_id} as {len(episode_uuids)} "
                f"linked episodes (chunks)"
            )
            return {
                "success": True,
                "episode_uuids": episode_uuids,
                "chunk_count": len(episode_uuids),
                "total_chunks": total_chunks,
                "failed_chunks": failed_chunks,
                "entities_extracted": entities_extracted,
            }
        else:
            return {
                "success": False,
                "error": f"All {total_chunks} chunks failed to sync",
                "failed_chunks": failed_chunks,
            }

    except ImportError as e:
        return {"success": False, "error": f"Chunking module unavailable: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _submit_graphiti_to_hosted(
    *,
    threads_dir: Path,
    topic: str,
    entry_id: str,
    entry_body: str,
    entry_title: Optional[str],
    timestamp: Optional[str],
    entry_summary: str,
    runtime: Any,
    log: logging.Logger,
) -> bool:
    """Submit a Graphiti episode to the hosted premium endpoint (hybrid T2).

    Plan v20 Phase 5: the hybrid T2 hot-path. Calls hosted
    ``watercooler_graphiti_add_episode`` via ``premium_client``, writes a
    Stage-A handoff receipt, and returns ``True`` on successful submission.
    Submission failure is a sync failure — we do NOT fall back to local
    execution because hybrid must never live-write T2 locally.
    """
    from .handoff_receipts import append_handoff_receipt

    try:
        from watercooler.path_resolver import (
            derive_repo_slug,
            derive_t2_database_name,
        )
        from watercooler.memory_config import get_graphiti_use_summary
    except ImportError as e:
        log.warning(f"MEMORY: hybrid submit import failed for {topic}/{entry_id}: {e}")
        append_handoff_receipt(
            backend="graphiti",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            error=f"import_failed: {e}",
        )
        return False

    threads_dir_str = str(threads_dir)
    if threads_dir_str.endswith("-threads"):
        code_path = threads_dir_str.removesuffix("-threads")
    else:
        code_path = threads_dir_str

    # Plan v20 Phase 5 + Codex review: drive the canonical <org>_<repo>
    # identity off the git remote slug so Phase 6 migration targets and
    # the live submit target stay aligned. If the remote is absent we log
    # a warning (PR #654 code-review §5: the downgrade to repo-only was
    # previously silent, so submissions on a dev box without an origin
    # could land in the wrong hosted namespace unnoticed).
    try:
        repo_slug = derive_repo_slug(code_path=code_path, threads_dir=threads_dir)
    except Exception:
        repo_slug = None
    if not repo_slug:
        # Round 18 (LOW): fail closed rather than submit under a
        # non-canonical repo-only namespace. Mirrors the stricter
        # stance ``t1_hybrid._derive_group_id`` took in round 14.
        log.warning(
            f"MEMORY: hybrid submit for {topic}/{entry_id} could not "
            "resolve <org>/<repo> from the git remote; refusing rather "
            "than submitting to a non-canonical namespace. Configure a "
            "git remote or use the stdio surface explicitly."
        )
        append_handoff_receipt(
            backend="graphiti",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            error="repo_slug_unresolved",
        )
        return False

    try:
        # Plan v20 defect #34 fix (PR #660 review): derive the canonical
        # ``<org>_<repo>_t2`` form here, not the bare ``<org>_<repo>``.
        # The hosted-side ``_canonicalize_t2_group_id`` mismatch guard
        # compares the submitted ``group_id`` against its http_ctx-derived
        # canonical name. Submitting the bare form would always trigger
        # the mismatch warning (masking real cross-tenant errors) and
        # under concurrent multi-tenant load could cause the receiver
        # to silently route the episode to the wrong tenant's database.
        group_id = derive_t2_database_name(
            repo_slug=repo_slug,
            code_path=code_path,
            threads_dir=threads_dir,
        )
    except Exception as e:
        log.warning(f"MEMORY: could not derive group_id for {topic}/{entry_id}: {e}")
        append_handoff_receipt(
            backend="graphiti",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            error=f"group_id_derivation: {e}",
        )
        return False

    content = entry_body
    try:
        if get_graphiti_use_summary() and entry_summary:
            content = entry_summary
    except Exception:
        pass  # Fall back to raw body; not a hard failure for handoff.

    episode_title = (
        entry_title if entry_title else content[:50] + ("..." if len(content) > 50 else "")
    )
    source_desc = f"thread:{topic} | hybrid_handoff | entry:{entry_id}"

    arguments = {
        "content": content,
        "group_id": group_id,
        "code_path": code_path,
        "entry_id": entry_id,
        "timestamp": timestamp or "",
        "title": episode_title,
        "source_description": source_desc,
    }

    try:
        text = _run_coro_in_fresh_loop(
            runtime.premium_client.call_tool_text(
                "watercooler_graphiti_add_episode", arguments
            )
        )
    except Exception as e:
        log.warning(
            f"MEMORY: hybrid submit RPC failed for {topic}/{entry_id}: {e}"
        )
        append_handoff_receipt(
            backend="graphiti",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            group_id=group_id,
            error=f"rpc_failed: {e}",
        )
        return False

    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        log.warning(
            f"MEMORY: hybrid submit returned non-JSON for {topic}/{entry_id}: {text[:200]}"
        )
        append_handoff_receipt(
            backend="graphiti",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            group_id=group_id,
            error="non_json_response",
        )
        return False

    if not payload.get("success", False):
        log.warning(
            f"MEMORY: hybrid submit rejected for {topic}/{entry_id}: "
            f"{payload.get('error', 'unknown')}"
        )
        append_handoff_receipt(
            backend="graphiti",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            group_id=group_id,
            error=str(payload.get("error", "rejected")),
        )
        return False

    remote_task_id = str(payload.get("remote_task_id") or payload.get("task_id") or "")
    submission_status = str(payload.get("status") or "")

    # Round 18 (LOW): identity_downgrade receipt is no longer possible
    # — an unresolved ``repo_slug`` now triggers submit_failed above
    # rather than falling through to a downgraded submit.
    append_handoff_receipt(
        backend="graphiti",
        stage="submitted",
        entry_id=entry_id,
        topic=topic,
        group_id=group_id,
        remote_task_id=remote_task_id,
        submission_status=submission_status,
    )

    log.debug(
        f"MEMORY: hybrid handoff {topic}/{entry_id} -> "
        f"remote_task_id={remote_task_id or '(unknown)'} status={submission_status}"
    )
    return True


def _graphiti_sync_callback(
    threads_dir: Path,
    topic: str,
    entry_id: str,
    entry_body: str,
    entry_title: Optional[str],
    timestamp: Optional[str],
    agent: Optional[str],
    role: Optional[str],
    entry_type: Optional[str],
    backend_config: Dict[str, Any],
    log: logging.Logger,
    dry_run: bool = False,
    entry_summary: str = "",
) -> bool:
    """Sync entry to Graphiti backend.

    This callback is registered with baseline_graph.sync and invoked
    for each entry when WATERCOOLER_MEMORY_BACKEND=graphiti.

    Uses unified project group_id (derived from code_path) instead of
    per-thread group_ids, allowing entities to be shared across threads.

    Args:
        threads_dir: Threads directory (used to derive code_path)
        topic: Thread topic (included in source_description for traceability)
        entry_id: Entry ID for provenance tracking
        entry_body: Entry content to sync
        entry_title: Optional entry title
        timestamp: Entry timestamp (ISO 8601)
        agent: Agent name (unused by Graphiti)
        role: Agent role (unused by Graphiti)
        entry_type: Entry type (unused by Graphiti)
        backend_config: Backend configuration dict
        log: Logger instance
        dry_run: If True, simulate without actual sync
        entry_summary: Enriched summary from graph enrichment. Used as
            episode content instead of entry_body when use_summary is
            configured and summary is non-empty.

    Returns:
        True on success, False on failure
    """
    if dry_run:
        log.debug(f"MEMORY: [DRY RUN] Would sync {topic}/{entry_id} to Graphiti")
        return True

    # Plan v20 Phase 5: in ``local_hybrid`` the local side must NEVER execute
    # the Graphiti pipeline locally. Route the episode to the hosted
    # ``watercooler_graphiti_add_episode`` via ``premium_client`` and record a
    # local handoff receipt. Any exception here is logged but does not fall
    # back to local execution — principle 9 of Plan v20.
    runtime = get_runtime()
    if (
        runtime is not None
        and getattr(runtime, "surface", None) == "local_hybrid"
        and getattr(runtime, "premium_client", None) is not None
    ):
        return _submit_graphiti_to_hosted(
            threads_dir=threads_dir,
            topic=topic,
            entry_id=entry_id,
            entry_body=entry_body,
            entry_title=entry_title,
            timestamp=timestamp,
            entry_summary=entry_summary,
            runtime=runtime,
            log=log,
        )

    try:
        # Import config helpers
        from watercooler.memory_config import (
            get_graphiti_chunk_config,
            get_graphiti_chunk_on_sync,
            get_graphiti_use_summary,
        )

        # Resolve content: use enriched summary if configured and available
        content = entry_body
        if get_graphiti_use_summary() and entry_summary:
            content = entry_summary
            log.debug(
                f"MEMORY: Using enriched summary for {topic}/{entry_id} "
                f"({len(entry_summary)} chars vs {len(entry_body)} raw)"
            )

        # Derive code_path from threads_dir
        # threads_dir: /path/to/project-threads -> code_path: /path/to/project
        threads_dir_str = str(threads_dir)
        if threads_dir_str.endswith("-threads"):
            code_path = threads_dir_str.removesuffix("-threads")
        else:
            # Warn about non-standard naming, use threads_dir as fallback
            log.warning(
                f"MEMORY: threads_dir '{threads_dir}' doesn't end with '-threads'. "
                f"Using it directly for code_path derivation."
            )
            code_path = threads_dir_str

        # Check if chunking is enabled
        chunk_on_sync = get_graphiti_chunk_on_sync()

        # Callbacks run in ThreadPoolExecutor workers which have no event loop,
        # so asyncio.run() is always safe here.
        if chunk_on_sync:
            max_tokens, overlap = get_graphiti_chunk_config()
            result = asyncio.run(
                _call_graphiti_add_episode_chunked(
                    content=content,
                    topic=topic,
                    entry_id=entry_id,
                    timestamp=timestamp,
                    title=entry_title,
                    code_path=code_path,
                    max_tokens=max_tokens,
                    overlap=overlap,
                )
            )
        else:
            result = asyncio.run(
                _call_graphiti_add_episode(
                    content=content,
                    topic=topic,
                    entry_id=entry_id,
                    timestamp=timestamp,
                    title=entry_title,
                    code_path=code_path,
                )
            )

        if not result.get("success", False):
            log.warning(
                f"MEMORY: Graphiti sync failed for {topic}/{entry_id}: "
                f"{result.get('error', 'unknown')}"
            )
            return False

        # Log chunk count if chunked
        chunk_count = result.get("chunk_count")
        if chunk_count and chunk_count > 1:
            log.debug(
                f"MEMORY: Synced {topic}/{entry_id} to Graphiti "
                f"({chunk_count} chunks)"
            )
        else:
            log.debug(f"MEMORY: Synced {topic}/{entry_id} to Graphiti")
        return True

    except Exception as e:
        log.exception(f"MEMORY: Graphiti sync error for {topic}/{entry_id}")
        return False


# ============================================================================
# LeanRAG Sync Callback
# ============================================================================


def _leanrag_sync_callback(
    threads_dir: Path,
    topic: str,
    entry_id: str,
    entry_body: str,
    entry_title: Optional[str],
    timestamp: Optional[str],
    agent: Optional[str],
    role: Optional[str],
    entry_type: Optional[str],
    backend_config: Dict[str, Any],
    log: logging.Logger,
    dry_run: bool = False,
    entry_summary: str = "",
) -> bool:
    """Sync entry to LeanRAG backend.

    LeanRAG is a batch processing pipeline - individual entry syncs queue
    entries for later batch processing. The actual clustering happens via
    explicit pipeline runs (watercooler_leanrag_run_pipeline MCP tool).

    Entries are appended to a queue file (.leanrag_queue.jsonl) in the
    threads directory. Pipeline runs can check this file to know if there's
    fresh work to process.

    Args:
        threads_dir: Threads directory
        topic: Thread topic (used as group_id)
        entry_id: Entry ID for provenance tracking
        entry_body: Entry content to sync
        entry_title: Optional entry title
        timestamp: Entry timestamp (ISO 8601)
        agent: Agent name
        role: Agent role
        entry_type: Entry type
        backend_config: Backend configuration dict
        log: Logger instance
        dry_run: If True, simulate without actual sync
        entry_summary: Enriched summary (unused by LeanRAG, protocol compliance)

    Returns:
        True on success, False on failure
    """
    if dry_run:
        log.debug(f"MEMORY: [DRY RUN] Would queue {topic}/{entry_id} for LeanRAG pipeline")
        return True

    try:
        # Build queue entry with all metadata
        queue_entry = {
            "entry_id": entry_id,
            "topic": topic,
            "timestamp": timestamp or datetime.now(tz.utc).isoformat(),
            "queued_at": datetime.now(tz.utc).isoformat(),
            "entry_title": entry_title,
            "entry_body": entry_body,
            "agent": agent,
            "role": role,
            "entry_type": entry_type,
        }

        # Append to queue file (thread-safe)
        queue_file = Path(threads_dir) / ".leanrag_queue.jsonl"
        with _queue_lock:
            with open(queue_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(queue_entry) + "\n")

        log.debug(f"MEMORY: Entry {topic}/{entry_id} queued for LeanRAG pipeline")
        return True

    except Exception as e:
        log.exception(f"MEMORY: Failed to queue {topic}/{entry_id} for LeanRAG: {e}")
        return False


def get_leanrag_queue_path(threads_dir: Path) -> Path:
    """Get the path to the LeanRAG queue file.

    Args:
        threads_dir: Threads directory

    Returns:
        Path to .leanrag_queue.jsonl
    """
    return Path(threads_dir) / ".leanrag_queue.jsonl"


def read_leanrag_queue(threads_dir: Path) -> list[Dict[str, Any]]:
    """Read all entries from the LeanRAG queue.

    Args:
        threads_dir: Threads directory

    Returns:
        List of queued entry dicts
    """
    queue_file = get_leanrag_queue_path(threads_dir)
    if not queue_file.exists():
        return []

    entries = []
    with open(queue_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(f"MEMORY: Skipping malformed queue entry: {line[:50]}...")
    return entries


def clear_leanrag_queue(threads_dir: Path) -> int:
    """Clear the LeanRAG queue after processing.

    Atomically reads and clears the queue while holding the lock to prevent
    race conditions with concurrent writers.

    Args:
        threads_dir: Threads directory

    Returns:
        Number of entries cleared
    """
    queue_file = get_leanrag_queue_path(threads_dir)

    with _queue_lock:
        if not queue_file.exists():
            return 0

        # Count entries while holding lock
        count = 0
        try:
            with open(queue_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
        except (OSError, IOError):
            return 0

        # Delete while still holding lock
        try:
            queue_file.unlink()
        except FileNotFoundError:
            return 0

    logger.debug(f"MEMORY: Cleared {count} entries from LeanRAG queue")
    return count


# ============================================================================
# Callback Registration
# ============================================================================


_callbacks_initialized = False


def init_memory_sync_callbacks() -> None:
    """Register memory sync callbacks at MCP startup.

    This function is idempotent - safe to call multiple times.
    It registers callbacks for all supported memory backends.

    Should be called during MCP server initialization.
    """
    global _callbacks_initialized

    if _callbacks_initialized:
        logger.debug("MEMORY: Callbacks already initialized, skipping")
        return

    try:
        from watercooler.baseline_graph.sync import register_memory_sync_callback

        # Register Graphiti callback
        register_memory_sync_callback("graphiti", _graphiti_sync_callback)

        # Register LeanRAG callback
        register_memory_sync_callback("leanrag", _leanrag_sync_callback)

        _callbacks_initialized = True
        logger.info("MEMORY: Sync callbacks registered for backends: graphiti, leanrag")

    except ImportError as e:
        logger.warning(f"MEMORY: Could not register sync callbacks: {e}")
    except Exception as e:
        logger.exception(f"MEMORY: Error registering sync callbacks: {e}")


def init_memory_queue_executors() -> None:
    """Register backend executors with the memory task queue worker.

    Called after both init_memory_sync_callbacks() and init_memory_queue()
    have completed. The executor adapts the existing _call_graphiti_add_episode
    async function to the MemoryTask interface expected by the queue worker.
    """
    try:
        from .memory_queue import get_worker, MemoryTask
    except ImportError:
        logger.debug("MEMORY: memory_queue package not available, skipping executor registration")
        return

    worker = get_worker()
    if worker is None:
        logger.debug("MEMORY: queue worker not initialised, skipping executor registration")
        return

    from .memory import _graphiti_importable
    if not _graphiti_importable():
        from .observability import log_warning_once
        log_warning_once(
            "graphiti_executor_skip",
            "MEMORY: watercooler_memory not installed — skipping graphiti executor registration",
        )
    else:
        _register_graphiti_executor(worker)

    try:
        from watercooler_memory.backends.leanrag import LeanRAGBackend  # noqa: F401
        worker.register_executor("leanrag_pipeline", _leanrag_pipeline_executor_fn)
        logger.info("MEMORY: Registered leanrag_pipeline executor with memory task queue")
    except ImportError:
        from .observability import log_warning_once
        log_warning_once(
            "leanrag_executor_skip",
            "MEMORY: watercooler_memory not installed — skipping leanrag_pipeline executor registration",
        )


def _register_graphiti_executor(worker: "MemoryTaskWorker") -> None:
    """Register the graphiti executor with the given worker."""
    async def graphiti_executor(task: "MemoryTask") -> Dict[str, Any]:
        """Execute a Graphiti episode ingestion from a queued task.

        Reuses the current thread's backend when the resolved backend key
        (host:port:group_id) is unchanged. Recreates the backend when routing
        changes or after a transport-level failure so the next retry starts
        with a fresh connection.

        LeanRAG executor is unaffected — LeanRAGBackend creates per-task
        instances but uses no async connections, so there is no connection
        exhaustion risk there.
        """
        from watercooler_mcp import memory as mem

        config = mem.load_graphiti_config(code_path=task.code_path or None)
        if config is None:
            raise RuntimeError("Graphiti config unavailable for queued task")
        if not task.group_id:
            raise RuntimeError("Queued Graphiti task missing group_id")

        # group_id is the canonical database identity for queued work.
        # task.code_path may be a threads worktree path, not a code repo
        # root — do not re-derive the database from it.
        #
        # Plan v20 defect #34 defense-in-depth: REMEDIATE non-canonical
        # database names rather than reject them.
        #
        # Earlier draft of this PR raised PermanentTaskError on any task
        # whose ``group_id`` didn't end in ``_t2``. PR #660 review caught
        # the deploy-window data-loss risk: pre-deploy tasks queued under
        # the legacy bare ``<repo>`` form would all hit the assertion on
        # first executor pass and route directly to the dead-letter queue
        # with no retry path. Post-fix tasks should never trigger this
        # branch (caller-side canonicalization is the primary discipline);
        # if they do, log loudly so the regression is visible without
        # losing data.
        canonical_database = (
            task.group_id if task.group_id.endswith("_t2") else f"{task.group_id}_t2"
        )
        if canonical_database != task.group_id:
            from .observability import log_warning as _log_warn
            _log_warn(
                f"GRAPHITI_EXECUTOR: task {task.task_id} has non-canonical "
                f"group_id={task.group_id!r}; remediating database to "
                f"{canonical_database!r} (Plan v20 defect #34 deploy-window "
                f"guard). If this fires post-deploy for a fresh task, the "
                f"caller bypassed _canonicalize_t2_group_id — investigate."
            )
        config = dataclasses.replace(config, database=canonical_database)
        state = worker._get_thread_state()
        # Use the remediated ``canonical_database`` (matches
        # ``config.database``) rather than ``task.group_id`` so legacy
        # bare-named tasks share the same backend cache slot as their
        # canonical equivalents during the deploy window. Otherwise a
        # legacy-form task and its canonical successor produce two
        # different cache keys for the same physical FalkorDB database,
        # leaking backend connections under sustained load.
        backend_key = (
            f"{config.falkordb_host}:{config.falkordb_port}:{canonical_database}"
        )
        backend = state.graphiti_backend

        if backend is None or state.graphiti_backend_key != backend_key:
            if backend is not None:
                # Close the old backend before clearing state so a failure
                # in aclose() doesn't leave state pointing at a closed backend.
                try:
                    await backend.aclose()
                except Exception as exc:
                    logger.warning("MEMORY_QUEUE: aclose() on key change raised: %s", exc)
                with worker._backend_count_lock:
                    worker.active_backend_count = max(0, worker.active_backend_count - 1)
            # Clear before calling get_graphiti_backend so a raise there
            # doesn't leave state pointing at the already-closed old backend.
            state.graphiti_backend = None
            state.graphiti_backend_key = None
            backend = mem.get_graphiti_backend(config)
            if backend is None or isinstance(backend, dict):
                raise RuntimeError("Graphiti backend unavailable for queued task")
            state.graphiti_backend = backend
            state.graphiti_backend_key = backend_key
            with worker._backend_count_lock:
                worker.active_backend_count += 1
            logger.debug(
                "MEMORY_QUEUE: initialized per-thread graphiti backend "
                "(thread=%s, backend_key=%s)",
                threading.current_thread().name,
                backend_key,
            )

        try:
            result = await _call_graphiti_add_episode(
                content=task.content,
                topic=task.topic,
                entry_id=task.entry_id,
                timestamp=task.timestamp,
                title=task.title,
                code_path=task.code_path or "",
                backend=backend,
                xrefs=task.xrefs or None,
                tags=task.tags or None,
                vote_score=task.vote_score,
                pinned=task.pinned,
            )
        except (OSError,) + _transport_error_types():
            # Transport error: evict the stale backend before re-raising so
            # the worker's _reset_thread_backend is also safe to call after
            # this exception propagates.
            try:
                await backend.aclose()
            except Exception:
                pass
            state.graphiti_backend = None
            state.graphiti_backend_key = None
            with worker._backend_count_lock:
                worker.active_backend_count = max(0, worker.active_backend_count - 1)
            raise

        if not result.get("success", False):
            raise RuntimeError(result.get("error", "Graphiti sync failed"))
        return {
            "episode_uuid": result.get("episode_uuid", ""),
            "entities_extracted": result.get("entities_extracted", []),
            "facts_extracted": result.get("facts_extracted", 0),
        }

    worker.register_executor("graphiti", graphiti_executor)
    logger.info("MEMORY: Registered graphiti executor with memory task queue")


def episodes_to_chunk_payload(
    episodes: list, group_id: str,
) -> "ChunkPayload":
    """Convert Graphiti episodes to a ChunkPayload for LeanRAG pipeline.

    Args:
        episodes: List of EpisodeRecord instances from the Graphiti backend.
        group_id: Project group identifier for metadata tagging.

    Returns:
        A ChunkPayload ready for LeanRAG indexing.
    """
    import hashlib
    from watercooler_memory.backends import ChunkPayload

    chunks = []
    for ep in episodes:
        content = ep.content
        if not content:
            logger.debug("episodes_to_chunk_payload: skipping episode %s with empty content", ep.uuid)
            continue
        chunk_id = ep.uuid or hashlib.md5(
            content.encode(), usedforsecurity=False
        ).hexdigest()
        chunks.append({
            "id": chunk_id,
            "text": content,
            "metadata": {
                "group_id": group_id,
                "source": "graphiti_episode",
            },
        })
    return ChunkPayload(manifest_version="1.0", chunks=chunks)


async def _leanrag_pipeline_executor_fn(task: "MemoryTask") -> dict[str, Any]:
    """Execute a LeanRAG pipeline from a queued task.

    Routing logic:
    - BULK tasks always run the full pipeline (full rebuild).
    - SINGLE tasks use incremental_index() when saved cluster state exists,
      otherwise fall back to full index().
    """
    from .memory_queue.task import TaskType

    # Parse pipeline params from task.content (JSON for BULK, raw text for SINGLE)
    try:
        params = json.loads(task.content) if task.content else {}
        if not isinstance(params, dict):
            params = {}
    except (ValueError, TypeError):
        # SINGLE tasks store raw text content, not JSON
        params = {}

    # Get LeanRAG backend via unified config factory
    code_path = task.code_path or ""
    if not code_path:
        raise RuntimeError(
            "LeanRAG pipeline requires code_path for project-scoped config. "
            "Set code_path on the MemoryTask or provide it to the MCP tool. "
            f"(group_id={task.group_id!r}, task_id={task.task_id!r})"
        )
    try:
        from .memory import load_leanrag_config
        from watercooler_memory.backends.leanrag import LeanRAGBackend

        config = load_leanrag_config(code_path=code_path)
        if config is None:
            raise RuntimeError(
                "LeanRAG config unavailable (disabled or misconfigured). "
                f"code_path={code_path!r}, group_id={task.group_id!r}"
            )
        backend = LeanRAGBackend(config)
    except ImportError as exc:
        raise RuntimeError(
            "LeanRAG backend unavailable. This feature is not available in the open-core edition."
        ) from exc

    # ---------- SINGLE task: incremental path ----------
    if task.task_type == TaskType.SINGLE:
        if not task.content:
            raise RuntimeError("Missing content in LeanRAG SINGLE task")

        import hashlib
        from watercooler_memory.backends import ChunkPayload

        content = task.content
        chunk_id = task.entry_id or hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
        chunk_payload = ChunkPayload(
            manifest_version="1.0",
            chunks=[{
                "id": chunk_id,
                "text": content,
                "metadata": {
                    "group_id": task.group_id or "",
                    "source": "single_entry",
                    "entry_id": task.entry_id or "",
                },
            }],
        )

        # Use incremental path if state exists and not forced full rebuild
        use_incremental = params.get("incremental", True)
        if use_incremental and backend.has_incremental_state():
            result = await asyncio.to_thread(backend.incremental_index, chunk_payload)
            logger.info(
                "MEMORY: LeanRAG incremental index for entry %s: %s",
                task.entry_id, result.message,
            )
        else:
            result = await asyncio.to_thread(backend.index, chunk_payload)
            logger.info(
                "MEMORY: LeanRAG full index (no incremental state) for entry %s",
                task.entry_id,
            )

        return {
            "episode_uuid": task.entry_id or "",
            "entities_extracted": [],
            "facts_extracted": result.indexed_count,
            "message": result.message,
        }

    # ---------- BULK task: full pipeline ----------
    group_id = task.group_id
    if not group_id:
        raise RuntimeError("Missing group_id in LeanRAG BULK task")

    # Fetch episodes from Graphiti for this group via unified config
    try:
        from .memory import load_graphiti_config
        from watercooler_memory.backends.graphiti import GraphitiBackend

        graphiti_config = load_graphiti_config(code_path=code_path)
        if graphiti_config is None:
            raise RuntimeError(
                "Graphiti config unavailable — required for episode retrieval. "
                f"code_path={code_path!r}, group_id={group_id!r}"
            )
        graphiti = GraphitiBackend(graphiti_config)
        start_date = params.get("start_date", "")
        end_date = params.get("end_date", "")
        episodes = await asyncio.to_thread(
            graphiti.get_group_episodes,
            group_id=group_id,
            start_time=start_date,
            end_time=end_date,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Graphiti backend required to fetch episodes for LeanRAG pipeline"
        ) from exc

    if not episodes:
        return {
            "group_id": group_id,
            "clusters_created": 0,
            "chunks_processed": 0,
            "message": f"No episodes found for group '{group_id}'",
        }

    chunk_payload = episodes_to_chunk_payload(episodes, group_id)

    # BULK always runs full index (full rebuild)
    result = await asyncio.to_thread(backend.index, chunk_payload)

    logger.info(
        "MEMORY: LeanRAG pipeline completed for %s: %d chunks -> %d clusters",
        group_id, len(chunk_payload.chunks), result.indexed_count,
    )

    # BULK pipeline tasks do not produce per-entry episode UUIDs;
    # the worker defaults missing keys to ""/None/0.
    return {
        "group_id": group_id,
        "clusters_created": result.indexed_count,
        "chunks_processed": len(chunk_payload.chunks),
        "message": result.message,
    }


def reset_callbacks() -> None:
    """Reset callback registration state (for testing).

    This allows re-registration of callbacks in test scenarios.
    """
    global _callbacks_initialized
    _callbacks_initialized = False
