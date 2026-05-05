"""Graph tools for watercooler MCP server.

Tools:
- watercooler_baseline_graph_stats: Graph statistics
- watercooler_search: Search threads and entries (tier-aware routing)
- watercooler_find_similar: Find similar entries
- watercooler_baseline_sync_status: Baseline graph sync health
- watercooler_access_stats: Access statistics

New Tool Suite (Fresh Suite Design):
- watercooler_graph_enrich: Generate/regenerate summaries and embeddings
- watercooler_graph_recover: Rebuild graph from markdown (emergency recovery)
- watercooler_graph_project: Generate markdown from graph (source of truth)
"""

import asyncio
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastmcp import Context

from ..memory_queue import (
    DuplicateTaskError,
    MemoryTask,
    MemoryTaskQueue,
    MemoryTaskWorker,
    QueueFullError,
)
from ..sync import SyncError
from ..sync.errors import PushError
from ..middleware import run_with_sync, run_with_graph_sync
from .. import validation  # Import module for runtime access (enables test patching)
from ..validation import is_hosted_context
from ..hosted_ops import (
    _validate_topic,
    get_annotations_hosted,
    append_annotation_hosted,
    delete_entry_hosted,
    delete_thread_hosted,
    archive_thread_hosted,
    search_entries_hosted,
    get_baseline_graph_stats_hosted,
    get_baseline_sync_status_hosted,
)
from watercooler.path_resolver import derive_group_id

from ._boost import boost_decision_items, sanitize_boost

logger = logging.getLogger(__name__)


# =============================================================================
# GraphitiBackend singleton cache
# =============================================================================

_graphiti_backends: dict[str, Any] = {}
_graphiti_backends_lock = threading.Lock()


def _get_or_create_graphiti_backend(config: Any) -> Any:
    """Return a cached GraphitiBackend for this config, creating it if needed.

    Keyed by (host, port, database) so config changes (e.g. different group_id)
    still share the connection pool to the same FalkorDB instance.

    Raises:
        RuntimeError: If the backend could not be initialized (import error or
            init failure). get_graphiti_backend() can return a truthy error dict
            instead of raising — this wrapper treats that as a failure so
            error objects are never cached.
    """
    from .. import memory as mem

    key = f"{config.falkordb_host}:{config.falkordb_port}:{config.database}"
    with _graphiti_backends_lock:
        cached = _graphiti_backends.get(key)
        if cached is not None:
            return cached
        result = mem.get_graphiti_backend(config)
        # get_graphiti_backend() returns a dict {"error": ...} on ImportError or init failure.
        # Never cache error dicts — callers get a fresh attempt on the next call.
        if result is None or isinstance(result, dict):
            raise RuntimeError(f"GraphitiBackend initialization failed: {result}")
        _graphiti_backends[key] = result
        return result


def _clear_graphiti_backend_cache() -> None:
    """Reset the GraphitiBackend cache. Use in tests and after explicit close().

    Mirrors the _clear_provenance_cache() pattern in tools/memory.py:68.
    """
    with _graphiti_backends_lock:
        for backend in _graphiti_backends.values():
            try:
                backend.close()
            except Exception:
                pass  # best-effort cleanup; don't block cache reset
        _graphiti_backends.clear()


# =============================================================================
# Input Validation Helpers
# =============================================================================


# Validation bounds
MAX_LIMIT = 100
MAX_BATCH_SIZE = 100
MIN_SIMILARITY_THRESHOLD = 0.0
MAX_SIMILARITY_THRESHOLD = 1.0


def _validate_limit(limit: int, default: int = 10, max_value: int = MAX_LIMIT) -> int:
    """Validate and constrain a limit parameter.

    Args:
        limit: The user-provided limit value
        default: Default value if limit is invalid
        max_value: Maximum allowed value

    Returns:
        Validated limit between 1 and max_value
    """
    if not isinstance(limit, int) or limit < 1:
        return default
    return min(limit, max_value)


def _validate_threshold(threshold: float, default: float = 0.5) -> float:
    """Validate and constrain a similarity threshold.

    Args:
        threshold: The user-provided threshold value
        default: Default value if invalid

    Returns:
        Validated threshold between 0.0 and 1.0
    """
    if not isinstance(threshold, (int, float)):
        return default
    return max(
        MIN_SIMILARITY_THRESHOLD, min(float(threshold), MAX_SIMILARITY_THRESHOLD)
    )


# =============================================================================
# Search Routing Helpers (Milestone 6: Tier-Aware Search Routing)
# =============================================================================


def get_search_backend(backend: str) -> str:
    """Determine which search backend to use.

    Priority (highest first):
        1. Explicit backend parameter ("baseline", "graphiti", "leanrag")
        2. WATERCOOLER_MEMORY_BACKEND env var
        3. TOML config (memory.backend)
        4. Default: "baseline"

    Args:
        backend: Requested backend - "auto", "baseline", "graphiti", or "leanrag"

    Returns:
        Resolved backend name: "baseline", "graphiti", or "leanrag"
    """
    # Explicit backends are respected (except unknown ones)
    if backend in ("baseline", "graphiti", "leanrag"):
        return backend

    # Auto mode: check env var first, then TOML config
    if backend == "auto":
        # Check env var
        memory_backend = (
            os.environ.get("WATERCOOLER_MEMORY_BACKEND", "").lower().strip()
        )
        if memory_backend in ("graphiti", "leanrag"):
            return memory_backend

        # Check TOML config
        try:
            from watercooler.memory_config import get_memory_backend

            toml_backend = get_memory_backend()
            if toml_backend in ("graphiti", "leanrag"):
                return toml_backend
        except (ImportError, ValueError) as exc:
            if isinstance(exc, ValueError):
                logger.warning("search backend config error: %s", exc)

        return "baseline"

    # Unknown backend falls back to baseline
    logger.warning(f"Unknown search backend: {backend}, falling back to baseline")
    return "baseline"


_TEMPORAL_RE = re.compile(
    r"not\s+the\s+(?:old|previous|original)"
    r"|superse(?:d|ssion)"
    r"|(?:what|how\b.*)\s+changed|has\s+changed"
    r"|changed\s+(?:from|to)\b"
    r"|(?:was|were|been)\s+replaced"
    r"|before\s+and\s+after"
    r"|(?:previous|old)\b.{0,40}\b(?:current|new|now)\b"
    r"|(?:current|new)\b.{0,40}\b(?:not|versus|vs\b|old|previous)\b"
    r"|evolve[ds]?\b|evolution"
    r"|actual\s+(?:root\s+)?cause",
    re.IGNORECASE,
)


def _has_temporal_pattern(query: str) -> bool:
    """Detect temporal/supersession intent in a search query.

    High-precision patterns only — false positives (routing a normal query to
    facts mode) are worse than missed detections (staying in entries mode).

    Cross-ref: tier_strategy.detect_intent() has a complementary keyword list
    used by smart_query for tier selection (T1/T2/T3). This function routes
    watercooler_search auto-mode to facts; detect_intent() routes smart_query
    to T2. They overlap but serve distinct code paths.
    """
    return bool(_TEMPORAL_RE.search(query))


def infer_search_mode(mode: str, _query: str, _semantic: bool) -> str:
    """Infer the search mode based on the query and parameters.

    Args:
        mode: Requested mode - "auto", "entries", "entities", "episodes", or "facts"
        _query: Query text for NL heuristics (temporal pattern detection)
        _semantic: Reserved for future mode inference based on search type

    Returns:
        Resolved mode: "entries", "entities", "episodes", or "facts"
    """
    # Explicit modes are respected
    if mode in ("entries", "entities", "episodes", "facts"):
        return mode

    # Auto mode: detect temporal/supersession queries → facts mode
    if _query and _has_temporal_pattern(_query):
        return "facts"

    return "entries"


async def route_search(
    ctx: Context,
    threads_dir: Path,
    query: str,
    backend: str,
    mode: str,
    code_path: str = "",
    active_only: bool = False,
    **kwargs: Any,
) -> str:
    """Route search to the appropriate backend based on tier and mode.

    Args:
        ctx: MCP context
        threads_dir: Path to threads directory
        query: Search query
        backend: Resolved backend ("baseline", "graphiti", "leanrag")
        code_path: Path to code repository (for database name derivation)
        mode: Resolved mode ("entries", "entities", "episodes", "facts")
        active_only: If True (Graphiti only), exclude superseded facts from results
        **kwargs: Additional search parameters

    Returns:
        JSON string with search results
    """
    fallback_used = False
    fallback_reason = None

    # Detect annotation filters that only the baseline backend can apply.
    # Graphiti/LeanRAG results (facts, entities, episodes, answers) don't carry
    # baseline annotation state, so these filters cannot be honoured there.
    _flag_stripped = (kwargs.get("flag") or "").strip()
    _ann_filters_active = (
        bool(kwargs.get("tags"))
        or bool(_flag_stripped)
        or ("pinned" in kwargs and kwargs["pinned"] is not None)
    )

    def _warn_annotation_filters(result_json: str) -> str:
        """Inject warning when annotation filters were requested but not applied."""
        if not _ann_filters_active:
            return result_json
        try:
            data = json.loads(result_json)
            if not isinstance(data, dict):
                raise ValueError("non-dict JSON")
            data["annotation_filters_not_applied"] = True
            data["annotation_filters_warning"] = (
                "tags, flag, and pinned filters only work with the baseline backend. "
                "These filters were ignored for this search. Re-run with backend='baseline' "
                "to apply annotation filters."
            )
            return json.dumps(data, indent=2)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to inject annotation filter warning into non-JSON result")
            return (
                "WARNING: Annotation filters (tags, flag, pinned) were requested "
                "but could not be applied on this backend. Results are unfiltered.\n\n"
                + result_json
            )

    # Detect entry-shape filters (entry_type, role, agent) that the Graphiti
    # backend cannot apply. Graphiti's search returns fact/entity/episode shapes
    # without entry_type/role/agent fields, so post-filtering is impossible.
    # Issue #393: previously these filters were silently dropped, so callers
    # received unfiltered results and could mistake "0 of type X" for
    # "no entries of type X exist". Surface a structured error envelope
    # instead of returning misleading results.
    _entry_type_active = bool((kwargs.get("entry_type") or "").strip())
    _role_active = bool((kwargs.get("role") or "").strip())
    _agent_active = bool((kwargs.get("agent") or "").strip())
    _entry_filters_active = (
        _entry_type_active or _role_active or _agent_active
    )

    def _entry_filter_unsupported_error() -> str:
        """Return structured error when entry filters meet the graphiti backend."""
        unsupported = []
        if _entry_type_active:
            unsupported.append("entry_type")
        if _role_active:
            unsupported.append("role")
        if _agent_active:
            unsupported.append("agent")
        return json.dumps(
            {
                "error": "entry_filters_not_supported_on_graphiti",
                "message": (
                    "The graphiti backend cannot filter by "
                    f"{', '.join(unsupported)}. Graphiti results are facts/"
                    "entities/episodes which do not carry these fields."
                ),
                "hint": (
                    "Re-run with backend='baseline' to apply these filters, "
                    "or omit them to use the graphiti backend."
                ),
                "unsupported_filters": unsupported,
                "results": [],
                "count": 0,
            },
            indent=2,
        )

    # Facts mode — Graphiti temporal fact edges; hard-fails if Graphiti unavailable.
    # Broad except: intentional — MCP callers must always receive structured JSON,
    # not the bare error string returned by the outer handler. Any exception
    # (connection error, missing config, etc.) is logged server-side and surfaced
    # as a parseable error envelope to the agent.
    # The error message is intentionally static (not derived from the exception)
    # so that internal details (host names, paths, stack traces) are never leaked
    # to MCP callers. The full exception is available in server-side logs.
    if mode == "facts":
        if backend == "baseline":
            return json.dumps(
                {
                    "error": "facts_mode_requires_graphiti",
                    "message": "Graphiti backend is not available.",
                    "hint": "Set WATERCOOLER_GRAPHITI_ENABLED=1 and configure WATERCOOLER_LLM_API_KEY.",
                    "results": [],
                    "count": 0,
                }
            )
        # #393: facts mode result shape lacks entry_type/role/agent — the filter
        # can't be honoured here, so refuse rather than return unfiltered facts.
        if _entry_filters_active:
            return _entry_filter_unsupported_error()
        try:
            return _warn_annotation_filters(await _search_graphiti_impl(
                ctx=ctx,
                threads_dir=threads_dir,
                query=query,
                code_path=code_path,
                mode=mode,
                active_only=active_only,
                **kwargs,
            ))
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("facts mode: search timed out")
            return json.dumps(
                {
                    "error": "search_timeout",
                    "message": "Facts search timed out. The query may be too complex or the database is under load.",
                    "hint": "Retry with a simpler query, or omit superseded_start/end filters.",
                    "results": [],
                    "count": 0,
                }
            )
        except Exception as e:
            import concurrent.futures

            from watercooler_memory.backends import BackendError, TransientError

            if isinstance(e, (TransientError, BackendError)):
                logger.warning("facts mode: backend error: %s", e)
                return json.dumps(
                    {
                        "error": "search_backend_error",
                        "message": "Facts search encountered a backend error.",
                        "hint": "FalkorDB may be temporarily unavailable. Retry in a moment.",
                        "results": [],
                        "count": 0,
                    }
                )
            if isinstance(e, concurrent.futures.TimeoutError):
                logger.warning("facts mode: search timed out (concurrent.futures)")
                return json.dumps(
                    {
                        "error": "search_timeout",
                        "message": "Facts search timed out. The query may be too complex or the database is under load.",
                        "hint": "Retry with a simpler query, or omit superseded_start/end filters.",
                        "results": [],
                        "count": 0,
                    }
                )
            logger.warning("facts mode: Graphiti unavailable: %s", e)
            return json.dumps(
                {
                    "error": "facts_mode_requires_graphiti",
                    "message": "Graphiti backend is not available.",
                    "hint": "Set WATERCOOLER_GRAPHITI_ENABLED=1 and configure WATERCOOLER_LLM_API_KEY.",
                    "results": [],
                    "count": 0,
                }
            )

    # Entities/episodes modes require Graphiti
    if mode in ("entities", "episodes"):
        if backend == "baseline":
            # Can't do entities/episodes on baseline - fall back to entries
            original_mode = mode
            logger.info(
                f"Mode {mode} requires Graphiti, but backend is baseline. Falling back to entries mode."
            )
            mode = "entries"
            fallback_used = True
            fallback_reason = f"{original_mode} requires memory backend"
        else:
            # #393: entities/episodes results don't carry entry_type/role/
            # agent fields — refuse rather than return unfiltered results.
            if _entry_filters_active:
                return _entry_filter_unsupported_error()
            # Route to Graphiti entity/episode search
            try:
                if mode == "entities":
                    return _warn_annotation_filters(await _search_graphiti_nodes_impl(
                        ctx=ctx,
                        threads_dir=threads_dir,
                        query=query,
                        code_path=code_path,
                        **kwargs,
                    ))
                else:  # episodes
                    return _warn_annotation_filters(await _search_graphiti_episodes_impl(
                        ctx=ctx,
                        threads_dir=threads_dir,
                        query=query,
                        code_path=code_path,
                        **kwargs,
                    ))
            except Exception as e:
                logger.warning(
                    f"Graphiti {mode} search failed: {e}. Falling back to baseline."
                )
                fallback_used = True
                fallback_reason = str(e)
                backend = "baseline"
                mode = "entries"

    # Entries mode - route based on backend
    if backend == "graphiti":
        # #393: graphiti entries mode returns facts shape (no entry_type/role/
        # agent fields). Silently dropping these filters made callers think
        # there were "0 entries of type X". Refuse with a structured error
        # that points at backend='baseline' instead of returning misleading
        # unfiltered results.
        if _entry_filters_active:
            return _entry_filter_unsupported_error()
        try:
            return _warn_annotation_filters(await _search_graphiti_impl(
                ctx=ctx,
                threads_dir=threads_dir,
                query=query,
                code_path=code_path,
                mode=mode,
                active_only=active_only,
                **kwargs,
            ))
        except Exception as e:
            logger.warning(f"Graphiti search failed: {e}. Falling back to baseline.")
            fallback_used = True
            fallback_reason = str(e)
            backend = "baseline"

    if backend == "leanrag":
        try:
            return _warn_annotation_filters(_search_leanrag_impl(
                ctx=ctx,
                threads_dir=threads_dir,
                query=query,
                code_path=code_path,
                **kwargs,
            ))
        except Exception as e:
            logger.warning(f"LeanRAG search failed: {e}. Falling back to baseline.")
            fallback_used = True
            fallback_reason = str(e)
            backend = "baseline"

    # Baseline search (default fallback)
    result = _search_baseline_impl(
        ctx=ctx,
        threads_dir=threads_dir,
        query=query,
        **kwargs,
    )

    # Add fallback info if we had to fall back
    if fallback_used:
        try:
            result_data = json.loads(result)
            result_data["fallback_used"] = True
            result_data["fallback_reason"] = fallback_reason
            result = json.dumps(result_data, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass  # If result isn't JSON, just return as-is

    return result


def _search_baseline_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    semantic: bool = False,
    semantic_threshold: float = 0.5,
    start_time: str = "",
    end_time: str = "",
    thread_status: str = "",
    thread_topic: str = "",
    role: str = "",
    entry_type: str = "",
    agent: str = "",
    tags: str = "",
    flag: str = "",
    pinned: bool | None = None,
    limit: int = 10,
    query_operator: str = "AND",
    combine: str = "AND",
    include_threads: bool = True,
    include_entries: bool = True,
    **kwargs: Any,
) -> str:
    """Search the baseline graph (free tier).

    This is the core search implementation for baseline graph.
    """
    from watercooler.baseline_graph.search import SearchQuery, search_graph
    from watercooler.baseline_graph.reader import is_graph_available
    from ..sync import ensure_readable, format_parity_warning

    # Sync preflight: ensure worktree is up-to-date before searching
    parity_banner = ""
    try:
        _ok, _actions, parity, _auto_heal_failed = ensure_readable(threads_dir)
        parity_banner = format_parity_warning(parity, auto_heal_failed=_auto_heal_failed)
        if _actions:
            logging.getLogger(__name__).debug(f"search sync preflight: {_actions}")
    except Exception as e:
        logging.getLogger(__name__).debug(f"search sync preflight failed: {e}")

    if not is_graph_available(threads_dir):
        return json.dumps(
            {
                "error": "Graph not available",
                "message": "No baseline graph found. Run watercooler_baseline_graph_build first.",
                "results": [],
                "count": 0,
            }
        )

    # Validate parameters
    limit = _validate_limit(limit, default=10)
    semantic_threshold = _validate_threshold(semantic_threshold, default=0.5)

    # Parse tags string into list; normalize empty to None
    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    # Normalize flag: strip whitespace, empty → None
    flag_clean = flag.strip() if flag else ""

    # Build search query (parameters already validated above)
    search_query = SearchQuery(
        query=query if query else None,
        semantic=semantic,
        semantic_threshold=semantic_threshold,
        start_time=start_time if start_time else None,
        end_time=end_time if end_time else None,
        thread_status=thread_status if thread_status else None,
        thread_topic=thread_topic if thread_topic else None,
        role=role if role else None,
        entry_type=entry_type if entry_type else None,
        agent=agent if agent else None,
        tags=tags_list if tags_list else None,
        flag=flag_clean if flag_clean else None,
        pinned=pinned,
        limit=limit,
        query_operator=(
            query_operator.upper() if query_operator.upper() in ("AND", "OR") else "AND"
        ),
        combine=combine.upper() if combine.upper() in ("AND", "OR") else "AND",
        include_threads=include_threads,
        include_entries=include_entries,
    )

    # Execute search
    results = search_graph(threads_dir, search_query)

    # Format results for JSON output
    output: Dict[str, Any] = {
        "count": results.count,
        "total_scanned": results.total_scanned,
        "scanned_by_source": results.scanned_by_source,
        "backend": "baseline",
        "results": [],
    }

    for result in results.results:
        item: Dict[str, Any] = {
            "type": result.node_type,
            "id": result.node_id,
            "score": result.score,
            "score_type": result.score_type,
            "matched_fields": result.matched_fields,
        }

        if result.thread:
            item["thread"] = {
                "topic": result.thread.topic,
                "title": result.thread.title,
                "status": result.thread.status,
                "ball": result.thread.ball,
                "last_updated": result.thread.last_updated,
                "entry_count": result.thread.entry_count,
                "summary": result.thread.summary,
            }

        if result.entry:
            item["entry"] = {
                "entry_id": result.entry.entry_id,
                "thread_topic": result.entry.thread_topic,
                "index": result.entry.index,
                "agent": result.entry.agent,
                "role": result.entry.role,
                "entry_type": result.entry.entry_type,
                "title": result.entry.title,
                "timestamp": result.entry.timestamp,
                "summary": result.entry.summary,
            }

        output["results"].append(item)

    if parity_banner:
        output["_parity_warning"] = parity_banner.strip()
    return json.dumps(output, indent=2)


async def _search_graphiti_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    code_path: str = "",
    limit: int = 10,
    mode: str = "entries",
    active_only: bool = False,
    **kwargs: Any,
) -> str:
    """Search Graphiti memory backend for temporal facts (entity edges).

    Routes to backend.search_facts(), which queries Graphiti entity edges with
    optional active_only / time-range post-filters.

    Note: ``mode`` is metadata-only here — routing already happened in
    route_search(). Both facts and entries modes follow the same code path
    through this function; ``mode`` is forwarded to the output envelope so
    callers can identify which search type produced the results.
    """
    from .. import memory as mem

    config = mem.load_graphiti_config(code_path=code_path)
    if not config:
        raise RuntimeError("Graphiti backend not enabled")

    try:
        backend = _get_or_create_graphiti_backend(config)
    except RuntimeError as e:
        raise RuntimeError(f"Graphiti backend unavailable: {e}") from e

    # Extract time filters from kwargs; active_only is an explicit parameter
    start_time = kwargs.get("start_time", "")
    end_time = kwargs.get("end_time", "")
    superseded_start = kwargs.get("superseded_start", "")
    superseded_end = kwargs.get("superseded_end", "")
    has_time_filters = bool(start_time or end_time)
    has_superseded_filters = bool(superseded_start or superseded_end)

    # Call search_memory_facts directly to get (results, meta) tuple without
    # going through search_facts, which discards meta.  This avoids the race
    # condition of storing meta on the shared backend instance.
    # Backend methods use asyncio.run() internally, so run in thread.
    def _search_with_meta():
        return backend.search_memory_facts(
            query=query,
            max_facts=limit,
            start_time=start_time,
            end_time=end_time,
            active_only=active_only,
            superseded_start=superseded_start,
            superseded_end=superseded_end,
        )

    results, search_meta = await asyncio.to_thread(_search_with_meta)

    output: Dict[str, Any] = {
        "count": len(results),
        "backend": "graphiti",
        "mode": mode,
        "results": [
            {
                "type": "fact",
                "id": r.get("uuid", ""),
                "score": r.get("score", 0.0),
                "fact": r.get("fact", ""),
                "content": r.get("content", r.get("fact", "")),
                "name": r.get("name", ""),
                "source_node": r.get("source_node_uuid", ""),
                "target_node": r.get("target_node_uuid", ""),
                "valid_at": r.get("valid_at"),
                "invalid_at": r.get("invalid_at"),
            }
            for r in results
        ],
    }

    # Expose total_before_post_filter from backend metadata (#259)
    if (
        isinstance(search_meta, dict)
        and search_meta.get("total_before_post_filter") is not None
    ):
        output["total_before_post_filter"] = search_meta["total_before_post_filter"]
        output["filters_applied_backend"] = search_meta.get("filters_applied", [])

    applied_filters: Dict[str, Any] = {}
    if has_time_filters:
        applied_filters["start_time"] = start_time or None
        applied_filters["end_time"] = end_time or None
    if has_superseded_filters:
        applied_filters["superseded_start"] = superseded_start or None
        applied_filters["superseded_end"] = superseded_end or None
    if active_only:
        applied_filters["active_only"] = True
    if applied_filters:
        output["filters_applied"] = applied_filters

    return json.dumps(output, indent=2)


async def _search_graphiti_nodes_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    code_path: str = "",
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search Graphiti for entity nodes."""
    from .. import memory as mem

    config = mem.load_graphiti_config(code_path=code_path)
    if not config:
        raise RuntimeError("Graphiti backend not enabled")

    backend = mem.get_graphiti_backend(config)
    if not backend:
        raise RuntimeError("Graphiti backend unavailable")

    # Backend methods use asyncio.run() internally, so run in thread to avoid event loop conflict
    results = await asyncio.to_thread(
        backend.search_nodes, query=query, max_results=limit
    )

    output: Dict[str, Any] = {
        "count": len(results),
        "backend": "graphiti",
        "mode": "entities",
        "results": [
            {
                "type": "entity",
                "id": r.get("uuid", ""),
                "name": r.get("name", ""),
                "labels": r.get("labels", []),
                "summary": r.get("summary", ""),
            }
            for r in results
        ],
    }

    return json.dumps(output, indent=2)


async def _search_graphiti_episodes_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    code_path: str = "",
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search Graphiti for episodes."""
    from .. import memory as mem

    config = mem.load_graphiti_config(code_path=code_path)
    if not config:
        raise RuntimeError("Graphiti backend not enabled")

    backend = mem.get_graphiti_backend(config)
    if not backend:
        raise RuntimeError("Graphiti backend unavailable")

    # Extract time filters from kwargs (passed through from route_search)
    start_time = kwargs.get("start_time", "")
    end_time = kwargs.get("end_time", "")
    has_time_filters = bool(start_time or end_time)

    # Over-fetch when time filters are active (post-filter reduces result count).
    # Cap matches GraphitiBackend.MAX_SEARCH_RESULTS so the two stay in sync.
    fetch_limit = (
        min(limit * 3, backend.MAX_SEARCH_RESULTS) if has_time_filters else limit
    )

    # Backend methods use asyncio.run() internally, so run in thread to avoid event loop conflict
    results = await asyncio.to_thread(
        backend.get_episodes,
        query=query,
        max_episodes=fetch_limit,
        start_time=start_time,
        end_time=end_time,
    )

    # Trim to requested limit after post-filtering
    results = results[:limit]

    output: Dict[str, Any] = {
        "count": len(results),
        "backend": "graphiti",
        "mode": "episodes",
        "results": [
            {
                "type": "episode",
                "id": r.get("uuid", ""),
                "name": r.get("name", ""),
                "content": r.get("content", ""),
                "created_at": r.get("created_at", ""),
            }
            for r in results
        ],
    }
    if has_time_filters:
        output["filters_applied"] = {
            "start_time": start_time or None,
            "end_time": end_time or None,
        }

    return json.dumps(output, indent=2)


def _search_leanrag_impl(
    ctx: Context,
    threads_dir: Path,
    query: str,
    code_path: str = "",
    limit: int = 10,
    **kwargs: Any,
) -> str:
    """Search LeanRAG hierarchical clusters.

    Uses the LeanRAG backend to search the hierarchical knowledge graph.
    Falls back to baseline if LeanRAG is not available or not indexed.

    Args:
        ctx: MCP context
        threads_dir: Path to threads directory
        query: Search query string
        code_path: Path to code repository (for config/database derivation)
        limit: Maximum number of results
        **kwargs: Additional search parameters

    Returns:
        JSON string with search results
    """
    try:
        from watercooler_mcp.memory import load_leanrag_config
        from watercooler_memory.backends.leanrag import LeanRAGBackend
        from watercooler_memory.backends import QueryPayload

        config = load_leanrag_config(code_path=code_path)
        if config is None:
            raise RuntimeError("LeanRAG config unavailable (disabled or misconfigured)")

        backend = LeanRAGBackend(config)

        # Build query payload
        query_payload = QueryPayload(
            manifest_version="1.0",
            queries=[{"query": query, "limit": limit}],
        )

        # Execute query
        result = backend.query(query_payload)

        # Format results
        output = {
            "backend": "leanrag",
            "query": query,
            "result_count": len(result.results),
            "results": [],
        }

        for r in result.results:
            output["results"].append(
                {
                    "query": r.get("query", query),
                    "answer": r.get("answer", ""),
                    "context": r.get("context", ""),
                    "topk": r.get("topk", limit),
                }
            )

        return json.dumps(output, indent=2)

    except ImportError as e:
        # LeanRAG not available
        raise RuntimeError(f"LeanRAG backend not available: {e}")
    except Exception as e:
        # Any error triggers fallback to baseline
        raise RuntimeError(f"LeanRAG search failed: {e}")


# Module-level references to registered tools (populated by register_graph_tools)
baseline_graph_stats = None
search_graph_tool = None
find_similar_entries_tool = None
baseline_sync_status_tool = None
access_stats_tool = None


def _baseline_graph_stats_impl(
    ctx: Context,
    code_path: str = "",
) -> str:
    """Get statistics about threads for baseline graph.

    Returns thread counts, entry counts, and status breakdown.
    Useful for understanding the scope before building a baseline graph.

    Args:
        code_path: Path to code repository (for resolving threads dir).

    Returns:
        JSON with thread statistics.
    """
    try:
        from watercooler.baseline_graph import get_thread_stats

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        # Hosted mode: fetch stats via GitHub API
        if is_hosted_context(context):
            err, stats = get_baseline_graph_stats_hosted()
            if err:
                return f"Error getting hosted graph stats: {err}"
            return json.dumps(stats, indent=2)

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        stats = get_thread_stats(threads_dir)
        return json.dumps(stats, indent=2)

    except Exception as e:
        return f"Error getting baseline graph stats: {str(e)}"


def _apply_decision_boost(result_json: str, boost: float) -> str:
    """Post-rank Decision entries by multiplying their score.

    Operates on the JSON envelope returned by ``route_search``. Silently
    no-ops when the payload lacks the expected shape so we never break a
    valid search result over ranking concerns. Shares core logic with the
    ``smart_query`` evidence path via :func:`boost_decision_items`, which
    sanitizes the multiplier (NaN/inf/negative/non-numeric → 1.0) so
    those pathological values cannot corrupt ranking here.
    """
    safe_boost = sanitize_boost(boost)
    if safe_boost == 1.0:
        return result_json
    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return result_json
    if not isinstance(data, dict):
        return result_json
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return result_json

    boosted = boost_decision_items(
        results, safe_boost, type_path=("entry", "entry_type")
    )
    if boosted:
        data["decisions_prioritized"] = True
        data["decision_boost"] = safe_boost

    try:
        return json.dumps(data, indent=2)
    except (TypeError, ValueError):
        # Non-serialisable upstream value — fall back to the un-boosted
        # envelope rather than dropping the whole search result.
        return result_json


async def _search_graph_impl(
    ctx: Context,
    code_path: str = "",
    query: str = "",
    semantic: bool = False,
    semantic_threshold: float = 0.5,
    start_time: str = "",
    end_time: str = "",
    thread_status: str = "",
    thread_topic: str = "",
    role: str = "",
    entry_type: str = "",
    agent: str = "",
    limit: int = 10,
    query_operator: str = "AND",
    combine: str = "AND",
    include_threads: bool = True,
    include_entries: bool = True,
    tags: str = "",
    flag: str = "",
    pinned: bool | None = None,
    mode: str = "auto",
    backend: str = "auto",
    active_only: bool = False,
    superseded_start: str = "",
    superseded_end: str = "",
    prioritize_decisions: bool = True,
    decision_boost: float = 1.5,
) -> str:
    """Structured search with tier-aware routing and filter control (role, time,
    thread_topic, semantic). Use when you know what backend/tier you want.
    For open-ended NL questions, use watercooler_smart_query instead.

    Supports keyword search, semantic search with embeddings, time-based filtering,
    and metadata filters. Routes to the appropriate backend based on configuration.

    Mode Parameter (replaces removed tools):
        - mode="entries" (default): Search thread entries. This is the standard
          search mode for finding content in watercooler threads.
        - mode="entities": Search entity nodes extracted by Graphiti. Replaces
          the removed watercooler_search_nodes tool.
        - mode="episodes": Search episodic content from Graphiti. Replaces the
          removed watercooler_get_episodes tool.
        - mode="facts": Search Graphiti temporal fact edges (bi-temporal edges with
          valid_at/invalid_at). Use active_only=True to return only currently-valid
          facts. Hard-fails (structured error) if Graphiti backend is unavailable.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        query: Search query (keyword or semantic depending on mode).
        semantic: If True, use semantic search with embedding cosine similarity.
            Requires embeddings to be generated. Falls back to keyword if unavailable.
        semantic_threshold: Minimum cosine similarity for semantic matches (0.0-1.0).
            Only used when semantic=True. Default: 0.5. Lower values return more results.
        start_time: Filter results after this ISO timestamp.
        end_time: Filter results before this ISO timestamp.
        thread_status: Filter threads by status (OPEN, CLOSED, etc.).
        thread_topic: Filter entries by specific thread topic.
        role: Filter entries by role (planner, implementer, etc.).
        entry_type: Filter entries by type (Note, Plan, Decision, etc.).
        agent: Filter entries by agent name (partial match).
        tags: Comma-separated tag names to filter by (e.g. "podcast,newsletter").
            All specified tags must be present (AND semantics, case-insensitive).
        flag: Flag value substring to match (case-insensitive). Returns only
            entries/threads with at least one flag whose value contains this string.
        pinned: Filter by pinned status. True=has pinned entries, False=has no
            pinned entries (includes unannotated items which are implicitly not pinned),
            None=no filter (default).
        limit: Maximum results to return (default: 10, max: 100).
        query_operator: How to combine query tokens - "AND" or "OR" (default: AND).
            Only affects baseline keyword search. AND requires every token in
            the query to appear somewhere in the entry; OR requires any token.
        combine: How to combine filters - "AND" or "OR" (default: AND).
        include_threads: Include thread nodes in results (default: True).
        include_entries: Include entry nodes in results (default: True).
        mode: Search mode - "auto", "entries", "entities", "episodes", or "facts".
            - auto: Infer from query (default is entries)
            - entries: Search thread entries (baseline graph or Graphiti facts)
            - entities: Search entity nodes (requires Graphiti backend). Use this
              mode instead of the removed watercooler_search_nodes tool.
            - episodes: Search episodes (requires Graphiti backend). Use this
              mode instead of the removed watercooler_get_episodes tool.
            - facts: Search Graphiti temporal fact edges. Returns uuid, fact text,
              valid_at, invalid_at, score. Both timestamp fields are ISO 8601 strings
              or null. invalid_at=null means the fact is currently active (not
              superseded); valid_at=null means no known start time. Use active_only=True
              to return only currently-active facts instead of filtering manually.
              Unlike entries mode, facts mode does not fall back to the baseline graph
              — returns a structured error if Graphiti is unavailable.
        backend: Search backend - "auto", "baseline", "graphiti", or "leanrag".
            - auto: Use WATERCOOLER_MEMORY_BACKEND env var, fallback to baseline
            - baseline: Free tier - baseline graph only
            - graphiti: Paid tier - Graphiti memory backend
            - leanrag: Paid tier - LeanRAG hierarchical clusters
        active_only: If True (Graphiti facts and entries modes), exclude superseded facts —
            facts whose ``invalid_at`` field is set because a later episode contradicted
            them. Has no effect on baseline or leanrag backends.
        superseded_start: ISO 8601 lower bound for invalid_at. Filters to facts
            superseded *after* this time. Only effective with Graphiti facts mode.
        superseded_end: ISO 8601 upper bound for invalid_at. Filters to facts
            superseded *before* this time. Only effective with Graphiti facts mode.

    Returns:
        JSON with search results including matched nodes and metadata.

    Examples:
        # Search thread entries (default mode)
        watercooler_search(query="authentication", code_path=".")

        # Search entity nodes (replaces watercooler_search_nodes)
        watercooler_search(query="OAuth2", mode="entities", limit=10)

        # Search episodes (replaces watercooler_get_episodes)
        watercooler_search(query="implementation decisions", mode="episodes", limit=10)

        # Search temporal fact edges — currently-active facts only
        watercooler_search(query="API key rotation", mode="facts", active_only=True)

        # Find what changed: get ALL facts (including superseded ones)
        watercooler_search(query="sync approach", mode="facts", active_only=False)
        # Facts with invalid_at=null are current; invalid_at set = superseded

    When to Use Which Mode:
        - **"entries"** (default): General recall — "what was discussed", "what was
          decided", "summarize the thread". Returns entry text from threads.
        - **"facts"**: Temporal reasoning — "what changed", "what was the old
          approach", "what was superseded", "what was the state before X date".
          Returns structured (subject, predicate, object) triples with timestamps.
          Each fact has valid_at (when it became true) and invalid_at (when it was
          superseded, or null if still current). Use active_only=False to see BOTH
          the old and new state; use active_only=True for only current truth.
          **Use this mode when the question involves before/after, evolution,
          supersession, or corrected diagnoses.**
        - **"entities"**: Concept lookup — "what is X", "what components exist".
          Returns entity nodes (people, services, components) from the knowledge graph.
        - **"episodes"**: Event lookup — "what happened on date X", "what events
          relate to Y". Returns episodic records from Graphiti.

    Keyword Search Tips:
        - **Getting 0 results from a multi-word query? Add `query_operator="OR"`.**
          The default is AND — every token must appear in the same entry.
          `query="decided committed resolved"` returns nothing unless all three
          words are present together. Switch to OR when your query is a list of
          synonyms or candidate keywords rather than a phrase:
              # Phrase / co-occurrence — AND is correct (default)
              watercooler_search(query="long polling", mode="entries")

              # Keyword union / broad recall — use OR
              watercooler_search(
                  query="decided resolved committed opted agreed chosen",
                  query_operator="OR", mode="entries"
              )
        - Single-word queries do substring matching: "auth" matches "authentication".
        - Use short tokens (1-3 words each) for best results.
    """
    try:
        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        # Hosted mode
        if is_hosted_context(context):
            limit = _validate_limit(limit, default=10)
            hosted_mode = infer_search_mode(mode, query, semantic)

            # T2 modes — route to the canonical <org>_<repo>_t2 Graphiti
            # graph. PR #660 made load_graphiti_config resolve the database
            # name from http_ctx.repo per request, so the same _search_graphiti_*
            # impls used by the local path work correctly in hosted mode.
            # The threads_dir argument is ignored inside those functions
            # (they only use code_path for config + the backend for the query),
            # so HOSTED_MODE_SENTINEL is safe to pass.
            if hosted_mode in ("entities", "facts", "episodes"):
                try:
                    if hosted_mode == "entities":
                        return await _search_graphiti_nodes_impl(
                            ctx=ctx,
                            threads_dir=validation.HOSTED_MODE_SENTINEL,
                            query=query,
                            code_path=code_path,
                            limit=limit,
                        )
                    if hosted_mode == "episodes":
                        return await _search_graphiti_episodes_impl(
                            ctx=ctx,
                            threads_dir=validation.HOSTED_MODE_SENTINEL,
                            query=query,
                            code_path=code_path,
                            limit=limit,
                            start_time=start_time,
                            end_time=end_time,
                        )
                    # facts
                    return await _search_graphiti_impl(
                        ctx=ctx,
                        threads_dir=validation.HOSTED_MODE_SENTINEL,
                        query=query,
                        code_path=code_path,
                        limit=limit,
                        mode="facts",
                        active_only=active_only,
                        start_time=start_time,
                        end_time=end_time,
                        superseded_start=superseded_start,
                        superseded_end=superseded_end,
                    )
                except RuntimeError as e:
                    # Mirror local-mode discipline: auto-inferred facts
                    # gracefully degrade to entries; explicit T2 modes
                    # surface the failure rather than silently falling back.
                    if mode == "auto" and hosted_mode == "facts":
                        logger.info(
                            "auto-inferred facts mode falling back to "
                            "hosted entries (Graphiti unavailable: %s)", e,
                        )
                        hosted_mode = "entries"
                    else:
                        return json.dumps({
                            "error": "graphiti_unavailable_hosted",
                            "message": str(e),
                            "mode": hosted_mode,
                            "results": [],
                        }, indent=2)

            # Plan v20 Phase 8: semantic entry search runs against hosted
            # T1 FalkorDB HNSW. Mode="entries" + semantic=True goes through
            # _search_entries_hosted_semantic; keyword entries falls through
            # to the GitHub-API path.
            if hosted_mode == "entries" and semantic:
                threshold = _validate_threshold(semantic_threshold, default=0.5)
                # Codex re-review: preserve filter parity with the
                # keyword-hosted path (search_entries_hosted) so
                # semantic=True does not silently drop role / entry_type /
                # agent / start_time / end_time filters.
                return _search_entries_hosted_semantic(
                    context=context,
                    query=query,
                    thread_topic=thread_topic,
                    limit=limit,
                    similarity_threshold=threshold,
                    role=role,
                    entry_type=entry_type,
                    agent=agent,
                    start_time=start_time,
                    end_time=end_time,
                )
            err, result = search_entries_hosted(
                query=query,
                thread_topic=thread_topic,
                role=role,
                entry_type=entry_type,
                agent=agent,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                query_operator=query_operator,
            )
            if err:
                return f"Error searching hosted graph: {err}"
            return json.dumps(result, indent=2)

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Validate parameters early (before any routing/processing)
        limit = _validate_limit(limit, default=10)
        semantic_threshold = _validate_threshold(semantic_threshold, default=0.5)

        # Resolve backend and mode
        resolved_backend = get_search_backend(backend)
        resolved_mode = infer_search_mode(mode, query, semantic)

        # Auto-inferred facts mode gracefully falls back to entries when
        # Graphiti is unavailable. Explicit mode="facts" hard-fails per design.
        if (
            mode == "auto"
            and resolved_mode == "facts"
            and resolved_backend != "graphiti"
        ):
            logger.info(
                "auto-inferred facts mode falling back to entries "
                "(Graphiti unavailable, backend=%s)",
                resolved_backend,
            )
            resolved_mode = "entries"

        # mode="facts" always routes through _search_graphiti_impl regardless of
        # resolved_backend, so active_only is honoured there even for baseline backend.
        if active_only and resolved_backend != "graphiti" and resolved_mode != "facts":
            logger.warning(
                "active_only=True has no effect on %s backend (no bi-temporal supersession); "
                "use backend='graphiti' or omit active_only",
                resolved_backend,
            )

        # Route to appropriate search implementation.
        # active_only is an explicit parameter on route_search and _search_graphiti_impl.
        # It is applied as a post-filter on entity edges (Graphiti only). It is a
        # no-op on baseline and leanrag backends, which have no bi-temporal supersession.
        result = await route_search(
            ctx=ctx,
            threads_dir=threads_dir,
            query=query,
            backend=resolved_backend,
            mode=resolved_mode,
            code_path=code_path,
            semantic=semantic,
            semantic_threshold=semantic_threshold,
            start_time=start_time,
            end_time=end_time,
            thread_status=thread_status,
            thread_topic=thread_topic,
            role=role,
            entry_type=entry_type,
            agent=agent,
            limit=limit,
            query_operator=query_operator,
            combine=combine,
            tags=tags,
            flag=flag,
            pinned=pinned,
            include_threads=include_threads,
            include_entries=include_entries,
            active_only=active_only,
            superseded_start=superseded_start,
            superseded_end=superseded_end,
        )
        # Post-rank Decision entries when the caller asks for it. Skipped for
        # facts/entities/episodes modes — those shapes don't carry `entry_type`.
        if prioritize_decisions and resolved_mode == "entries":
            result = _apply_decision_boost(result, decision_boost)
        return result

    except Exception as e:
        return f"Error searching graph: {str(e)}"


def _resolve_hosted_t1_target(context: Any) -> tuple[str, str]:
    """Derive ``(t1_database, project_group_id)`` from a hosted ThreadContext.

    Codex re-review (01KPZ367CBHGCZZ6JWWM36KFE6): the hosted context
    constructed by ``_require_context_hosted`` carries ``code_repo`` (the
    ``<org>/<repo>`` slug from the X-Repo header) — not ``repo_slug`` or
    ``project_group_id``. Derive both from ``code_repo``; normalise the
    ``-threads`` suffix so a threads-repo X-Repo value still resolves the
    correct canonical ``<org>_<repo>`` group id and ``<org>_<repo>_t1``
    database.

    Returns ``("", "")`` if neither attribute is available.
    """
    slug = (
        getattr(context, "repo_slug", None)
        or getattr(context, "code_repo", None)
        or ""
    )
    if not slug or "/" not in slug:
        return "", ""
    owner, repo = slug.split("/", 1)
    if repo.endswith("-threads"):
        repo = repo.removesuffix("-threads")
    canonical_slug = f"{owner}/{repo}"

    try:
        from watercooler.path_resolver import (
            derive_project_group_id,
            derive_t1_database_name,
        )
    except ImportError:
        return "", ""

    group_id = derive_project_group_id(repo_slug=canonical_slug)
    t1_db = derive_t1_database_name(repo_slug=canonical_slug)
    return t1_db, group_id


def _search_entries_hosted_semantic(
    *,
    context: Any,
    query: str,
    thread_topic: str,
    limit: int,
    similarity_threshold: float,
    role: str = "",
    entry_type: str = "",
    agent: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    """Hosted semantic entry search against `<org>_<repo>_t1` FalkorDB HNSW.

    Plan v20 Phase 8 (c). Called from the hosted `watercooler_search` branch
    when ``mode="entries"`` + ``semantic=True``. Embeds the query via the
    local embedding client (available on the hosted service) and runs an
    HNSW KNN lookup. Supports filter parity with the hosted keyword path
    (``role``, ``entry_type``, ``agent``, ``start_time``, ``end_time``).
    """
    if not query or not query.strip():
        return json.dumps({
            "error": "missing_query",
            "message": "Semantic entry search requires a non-empty query.",
            "results": [],
        }, indent=2)

    t1_db, group_id = _resolve_hosted_t1_target(context)
    if not t1_db or not group_id:
        return json.dumps({
            "error": "hosted_semantic_unresolved_target",
            "message": (
                "Could not derive hosted T1 database name and group_id from "
                "context.code_repo / context.repo_slug."
            ),
            "results": [],
        }, indent=2)

    try:
        from watercooler.baseline_graph.sync import generate_embedding
    except ImportError as e:
        return json.dumps({
            "error": "embedding_module_missing",
            "message": str(e),
            "results": [],
        }, indent=2)

    try:
        query_vec = generate_embedding(query)
        if query_vec is None:
            return json.dumps({
                "error": "embedding_unavailable",
                "message": "Embedding service is not configured on the hosted side.",
                "results": [],
            }, indent=2)
    except Exception as e:
        return json.dumps({
            "error": "embedding_failed",
            "message": str(e),
            "results": [],
        }, indent=2)

    try:
        from watercooler_mcp.hosted_semantic import search_semantic_entries
    except ImportError as e:
        return json.dumps({
            "error": "hosted_semantic_module_missing",
            "message": str(e),
            "results": [],
        }, indent=2)

    result = search_semantic_entries(
        database=t1_db,
        query_embedding=list(query_vec),
        group_id=group_id,
        limit=limit,
        similarity_threshold=similarity_threshold,
        thread_topic=thread_topic or None,
        role=role or None,
        entry_type=entry_type or None,
        agent=agent or None,
        start_time=start_time or None,
        end_time=end_time or None,
    )
    return json.dumps(result, indent=2)


def _find_similar_hosted(
    *,
    context: Any,
    entry_id: str,
    limit: int,
    similarity_threshold: float,
) -> str:
    """Hosted-mode find_similar against the canonical T1 FalkorDB HNSW index.

    Plan v20 Phase 8 (d). Opens a per-call connection to the hosted FalkorDB,
    fetches the source embedding by entry_id, then runs a KNN query via the
    HNSW vector index. The `watercooler_semantic_upsert_embedding` tool is
    responsible for populating both the embedding node and the index.

    Errors are returned as a structured JSON payload rather than raised, so
    the caller (``_find_similar_entries_impl``) can report the failure as a
    tool result.
    """
    t1_db, group_id = _resolve_hosted_t1_target(context)
    if not t1_db or not group_id:
        return json.dumps({
            "error": "hosted_find_similar_unresolved_target",
            "message": (
                "Could not derive hosted T1 database name from "
                "context.code_repo / context.repo_slug."
            ),
            "source_entry_id": entry_id,
            "results": [],
        }, indent=2)

    try:
        from watercooler_mcp.hosted_semantic import find_similar_t1
    except ImportError as e:
        return json.dumps({
            "error": "hosted_find_similar_module_missing",
            "message": str(e),
            "source_entry_id": entry_id,
            "results": [],
        }, indent=2)

    result = find_similar_t1(
        database=t1_db,
        entry_id=entry_id,
        group_id=group_id,
        limit=limit,
        similarity_threshold=similarity_threshold,
    )
    return json.dumps(result, indent=2)


def _find_similar_entries_impl(
    ctx: Context,
    entry_id: str,
    code_path: str = "",
    limit: int = 5,
    similarity_threshold: float = 0.5,
    use_embeddings: bool = True,
) -> str:
    """Find entries similar to a given entry using embedding similarity.

    Uses cosine similarity with embedding vectors when available.
    Falls back to same-thread heuristic if embeddings are not available.

    Args:
        entry_id: The entry ID to find similar entries for.
        code_path: Path to code repository (for resolving threads dir).
        limit: Maximum number of similar entries to return (default: 5).
        similarity_threshold: Minimum cosine similarity (0.0-1.0, default: 0.5).
        use_embeddings: Try to use embedding similarity (default: True).

    Returns:
        JSON with similar entries and their similarity scores.
    """
    try:
        from watercooler.baseline_graph.search import find_similar_entries
        from watercooler.baseline_graph.reader import is_graph_available

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        # Hosted mode: embedding similarity requires the local baseline
        # graph which is not available in hosted deployments.
        if is_hosted_context(context):
            # Plan v20 Phase 8: hosted find_similar is now real. Delegates to
            # the hosted semantic retrieval against the canonical
            # <org>_<repo>_t1 FalkorDB HNSW index. Missing embeddings for
            # ``entry_id`` return a structured "no_embedding" error rather
            # than the prior "not_supported_hosted" stub.
            try:
                return _find_similar_hosted(
                    context=context,
                    entry_id=entry_id,
                    limit=_validate_limit(limit, default=5, max_value=50),
                    similarity_threshold=_validate_threshold(
                        similarity_threshold, default=0.5
                    ),
                )
            except Exception as e:
                return json.dumps({
                    "error": "hosted_find_similar_failed",
                    "message": str(e),
                    "source_entry_id": entry_id,
                    "results": [],
                }, indent=2)

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        if not is_graph_available(threads_dir):
            return json.dumps(
                {
                    "error": "Graph not available",
                    "message": "No baseline graph found. Run watercooler_baseline_graph_build first.",
                    "results": [],
                }
            )

        # Validate parameters
        limit = _validate_limit(limit, default=5, max_value=50)
        similarity_threshold = _validate_threshold(similarity_threshold, default=0.5)

        # Find similar entries
        similar = find_similar_entries(
            threads_dir=threads_dir,
            entry_id=entry_id,
            limit=limit,
            use_embeddings=use_embeddings,
            similarity_threshold=similarity_threshold,
        )

        # Format results
        output = {
            "source_entry_id": entry_id,
            "count": len(similar),
            "method": (
                "embedding_similarity" if use_embeddings else "same_thread_heuristic"
            ),
            "threshold": similarity_threshold,
            "results": [],
        }

        for entry in similar:
            output["results"].append(
                {
                    "entry_id": entry.entry_id,
                    "thread_topic": entry.thread_topic,
                    "title": entry.title,
                    "agent": entry.agent,
                    "role": entry.role,
                    "timestamp": entry.timestamp,
                    "summary": entry.summary,
                }
            )

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error finding similar entries: {str(e)}"


async def _baseline_sync_status_impl(
    ctx: Context,
    code_path: str = "",
) -> str:
    """Check baseline graph sync status for all threads.

    Reports whether each thread's baseline graph (JSON) is up to date
    with the thread data. This does NOT check FalkorDB or memory tier
    health — use watercooler_diagnose_memory for that.

    Status categories:
    - Synced: baseline graph matches thread data
    - Stale: thread has changed since last graph sync
    - Error: last sync attempt failed
    - Pending: sync in progress

    Use this to diagnose baseline graph issues before running reconcile.

    Args:
        code_path: Path to code repository (for resolving threads dir).

    Returns:
        JSON health report with thread statuses and recommendations.
    """
    try:
        from watercooler.baseline_graph.sync import check_graph_health
        from watercooler.baseline_graph.reader import is_graph_available
        from dataclasses import asdict

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        # Hosted mode: GitHub is source of truth, always synced
        if is_hosted_context(context):
            err, status = get_baseline_sync_status_hosted()
            if err:
                return f"Error checking hosted sync status: {err}"
            return json.dumps(status, indent=2)

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Check if graph exists at all
        graph_available = is_graph_available(threads_dir)

        # Get health report.
        # Run in thread to avoid blocking event loop (#128).
        health = await asyncio.to_thread(check_graph_health, threads_dir)

        output = {
            "graph_available": graph_available,
            "healthy": health.healthy,
            "total_threads": health.total_threads,
            "synced_threads": health.synced_threads,
            "stale_threads": health.stale_threads,
            "error_threads": health.error_threads,
            "pending_threads": health.pending_threads,
            "error_details": health.error_details,
            "recommendations": [],
        }

        # Add recommendations
        if not graph_available:
            output["recommendations"].append(
                "Graph not available. Run watercooler_baseline_graph_build to create it."
            )
        if health.stale_threads:
            output["recommendations"].append(
                f"{len(health.stale_threads)} threads lack sync state. "
                "Run watercooler_graph_enrich(mode='missing') to backfill summaries/embeddings."
            )
        if health.error_threads:
            output["recommendations"].append(
                f"{health.error_threads} threads have sync errors. "
                "Check error_details and run watercooler_graph_enrich on affected topics."
            )

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error checking graph health: {str(e)}"


def _access_stats_impl(
    ctx: Context,
    code_path: str = "",
    node_type: str = "",
    limit: int = 10,
) -> str:
    """Get access statistics from the graph odometer.

    Returns the most frequently accessed threads and entries, useful for
    understanding usage patterns and identifying popular content.

    Args:
        code_path: Path to code repository (for resolving threads dir).
        node_type: Filter by "thread" or "entry". Empty string returns both.
        limit: Maximum number of results to return (default 10).

    Returns:
        JSON with most accessed nodes including type, id, and access count.
    """
    try:
        from watercooler.baseline_graph.reader import get_most_accessed

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        # Hosted mode: access tracking is per-process, not available remotely
        if is_hosted_context(context):
            return json.dumps({
                "total_results": 0,
                "filter": node_type or "all",
                "stats": [],
                "source": "hosted_github_api",
                "note": "Access statistics are local-only metrics not available in hosted mode.",
            }, indent=2)

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Validate node_type
        filter_type = None
        if node_type:
            if node_type.lower() not in ("thread", "entry"):
                return f"Invalid node_type: {node_type}. Must be 'thread', 'entry', or empty."
            filter_type = node_type.lower()

        # Get most accessed (validate limit)
        results = get_most_accessed(
            threads_dir=threads_dir,
            node_type=filter_type,
            limit=_validate_limit(limit, default=10),
        )

        # Format output
        output = {
            "total_results": len(results),
            "filter": filter_type or "all",
            "stats": [
                {"type": t, "id": nid, "access_count": count}
                for t, nid, count in results
            ],
        }

        return json.dumps(output, indent=2)

    except Exception as e:
        return f"Error getting access stats: {str(e)}"


# =============================================================================
# New Tool Suite (Fresh Suite Design)
# =============================================================================


async def _graph_enrich_impl(
    ctx: Context,
    code_path: str = "",
    summaries: bool = True,
    embeddings: bool = True,
    thread_summaries: bool = False,
    mode: str = "missing",
    topics: str = "",
    batch_size: int = 10,
    dry_run: bool = False,
) -> str:
    """Generate or regenerate summaries and embeddings.

    This is the unified enrichment tool that replaces backfill_graph with a cleaner,
    more consistent API. Use this for all enrichment operations.

    Modes:
    - "missing": Only fill missing values (default, safe)
    - "selective": Process only specified topics (force regenerate)
    - "all": Regenerate everything (global refresh, use with caution)

    Args:
        code_path: Path to code repository (for resolving threads dir).
        summaries: Whether to generate/regenerate entry summaries. Default: True.
        embeddings: Whether to generate/regenerate embeddings. Default: True.
        thread_summaries: Whether to regenerate thread summaries. When True with
            mode="missing", only generates for threads without summaries. With
            mode="selective" or mode="all", regenerates thread summaries regardless
            of existing values. Use this to force-regenerate summaries when many
            entries have been added, entry summaries have been improved, or you
            want a fresh summary reflecting current state. Default: False.
        mode: Processing mode - "missing", "selective", or "all". Default: "missing".
        topics: Comma-separated list of topics (required for "selective" mode).
        batch_size: Number of items to process before writing. Default: 10.
        dry_run: If True, return what would be processed without making changes.

    Returns:
        JSON with counts: processed, generated, skipped, errors

    Examples:
        # Fill missing embeddings only
        graph_enrich(embeddings=True, summaries=False, mode="missing")

        # Regenerate embeddings for specific topics (e.g., after dimension change)
        graph_enrich(embeddings=True, mode="selective", topics="topic-a,topic-b")

        # Full refresh of all embeddings
        graph_enrich(embeddings=True, summaries=False, mode="all")

        # Force regenerate thread summary for specific topic
        graph_enrich(thread_summaries=True, summaries=False, embeddings=False,
                     mode="selective", topics="my-topic")

        # Regenerate all thread summaries (batch refresh)
        graph_enrich(thread_summaries=True, summaries=False, embeddings=False, mode="all")
    """
    try:
        from watercooler.baseline_graph.sync import enrich_graph

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Parse topics list
        topic_list = None
        if topics:
            topic_list = [t.strip() for t in topics.split(",") if t.strip()]

        # Validate batch_size parameter
        validated_batch_size = _validate_limit(
            batch_size, default=10, max_value=MAX_BATCH_SIZE
        )

        # Define the enrich operation
        def _do_enrich() -> dict:
            result = enrich_graph(
                threads_dir=threads_dir,
                summaries=summaries,
                embeddings=embeddings,
                thread_summaries=thread_summaries,
                mode=mode,
                topics=topic_list,
                batch_size=validated_batch_size,
                dry_run=dry_run,
            )
            return result.to_dict()

        # Run in thread to avoid blocking event loop (#128).
        # Note: asyncio.to_thread() worker threads continue after timeout —
        # the operation completes in the background. This is acceptable since
        # the server survives and the work completes.
        if dry_run:
            output = await asyncio.to_thread(_do_enrich)
        else:
            # Run with full parity protocol (preflight + commit + push)
            output = await asyncio.to_thread(
                run_with_graph_sync,
                context,
                _do_enrich,
                f"graph: enrich mode={mode}",
            )

        return json.dumps(output, indent=2)

    except SyncError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error enriching graph: {str(e)}"


async def _graph_recover_impl(
    ctx: Context,
    code_path: str = "",
) -> str:
    """Graph recovery from markdown (moved to scripts/).

    Graph recovery is an extraordinary operation that reads .md files to
    rebuild graph data. It has been moved out of the MCP runtime to
    scripts/recover_baseline_graph.py.

    Usage:
        ./scripts/recover_baseline_graph.py /path/to/threads --mode stale
        ./scripts/recover_baseline_graph.py /path/to/threads --mode all --dry-run

    In normal operation, the graph is the sole source of truth and .md files
    are write-only projections. If the graph is lost, restore from git history
    (git checkout <commit> -- graph/) or run the recovery script.

    Args:
        code_path: Unused (retained for tool registration compatibility).

    Returns:
        Instructions for using the recovery script.
    """
    return json.dumps(
        {
            "action": "graph_recover",
            "status": "moved_to_script",
            "message": (
                "Graph recovery has been moved out of the MCP runtime. "
                "Use scripts/recover_baseline_graph.py instead. "
                "For routine issues, try: git checkout <commit> -- graph/"
            ),
            "script": "scripts/recover_baseline_graph.py",
            "examples": [
                "./scripts/recover_baseline_graph.py /path/to/threads --mode stale",
                "./scripts/recover_baseline_graph.py /path/to/threads --mode all --dry-run",
            ],
        },
        indent=2,
    )


def _graph_project_impl(
    ctx: Context,
    code_path: str = "",
    mode: str = "missing",
    topics: str = "",
    overwrite: bool = False,
    dry_run: bool = False,
) -> str:
    """Generate markdown files from graph (source of truth).

    Use this to regenerate markdown projections from graph data.
    The graph is the source of truth; this tool creates the derived markdown.

    Modes:
    - "missing": Only create markdown for topics without .md files
    - "selective": Project specific topics
    - "all": Regenerate all markdown (requires overwrite=True)

    Use cases:
    - Initial markdown generation after graph import
    - Regenerating corrupted markdown
    - Syncing after direct graph edits

    Args:
        code_path: Path to code repository (for resolving threads dir).
        mode: Processing mode - "missing", "selective", or "all". Default: "missing".
        topics: Comma-separated list of topics (required for "selective" mode).
        overwrite: Allow overwriting existing files (required for "all" mode).
        dry_run: If True, return what would be created/updated without changes.

    Returns:
        JSON with files created/updated, skipped, errors
    """
    try:
        from watercooler.baseline_graph.projector import project_graph

        error, context = validation._require_context(code_path)
        if error:
            return error
        if context is None or not context.threads_dir:
            return "Error: Unable to resolve threads directory."

        threads_dir = context.threads_dir
        if not threads_dir.exists():
            return f"Threads directory not found: {threads_dir}"

        # Parse topics list
        topic_list = None
        if topics:
            topic_list = [t.strip() for t in topics.split(",") if t.strip()]

        # Define the project operation
        def _do_project() -> dict:
            result = project_graph(
                threads_dir=threads_dir,
                mode=mode,
                topics=topic_list,
                overwrite=overwrite,
                dry_run=dry_run,
            )
            return result.to_dict()

        # For dry_run, don't wrap in git sync
        if dry_run:
            output = _do_project()
        else:
            # Run with full parity protocol (preflight + commit + push)
            output = run_with_graph_sync(
                context,
                _do_project,
                f"graph: project mode={mode}",
            )

        return json.dumps(output, indent=2)

    except SyncError as e:
        return f"Branch parity error: {str(e)}"
    except Exception as e:
        return f"Error projecting graph: {str(e)}"


# ============================================================================
# Annotation & Management Tools
# ============================================================================


def _annotate_impl(
    topic: str,
    target_id: str,
    target_type: str,
    kind: str,
    value: str,
    code_path: str = "",
    actor: str = "",
) -> str:
    """Add an annotation to an entry or thread.

    Supports reactions (emoji), tags, flags, cross-references (xrefs), and pins.

    Args:
        topic: Thread topic identifier
        target_id: Entry ID (for entries) or thread topic (for threads)
        target_type: "entry" or "thread"
        kind: Annotation kind — reaction, tag, flag, xref, or pin
        value: Emoji name (for reaction), tag name, agent/reason (for flag),
            or target entry_id (for xref). Ignored for pin.
        code_path: Path to the code repository
        actor: Who is adding the annotation (defaults to "unknown")

    Returns:
        Confirmation message with updated annotation state
    """
    from watercooler.baseline_graph.annotations import (
        VALID_KINDS,
        VALID_TARGET_TYPES,
        AnnotationEvent,
        append_annotation,
        get_annotation_state,
    )
    from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir

    # Validate inputs
    topic_err = _validate_topic(topic)
    if topic_err:
        return f"Error: {topic_err}"
    add_kinds = {"reaction", "tag", "flag", "xref", "pin"}
    if kind not in add_kinds:
        return f"Error: kind must be one of {sorted(add_kinds)}, got '{kind}'"
    if target_type not in VALID_TARGET_TYPES:
        return f"Error: target_type must be 'entry' or 'thread', got '{target_type}'"
    if kind != "pin" and not value:
        return f"Error: value is required for kind '{kind}'"

    error, context = validation._require_context(code_path)
    if error:
        return error
    if context is None or not context.threads_dir:
        return "Error: Unable to resolve threads directory."

    # Derive actor: explicit param > HTTP context user_id > "unknown"
    effective_actor = actor
    if not effective_actor:
        from ..context import get_effective_context
        http_ctx = get_effective_context()
        if http_ctx and http_ctx.user_id:
            effective_actor = http_ctx.user_id
        else:
            effective_actor = "unknown"

    try:
        from ulid import ULID
        from datetime import datetime, timezone
        from dataclasses import asdict

        event = AnnotationEvent(
            id=str(ULID()),
            target_id=target_id,
            target_type=target_type,
            kind=kind,
            value=value or "",
            actor=effective_actor,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # Hosted mode: use GitHub API
        if is_hosted_context(context):
            write_err, _sha = append_annotation_hosted(topic, asdict(event))
            if write_err:
                return f"Error adding annotation (hosted): {write_err}"
            # Re-read state from GitHub
            read_err, result = get_annotations_hosted(topic, target_id)
            if read_err:
                return f"Error reading annotation state (hosted): {read_err}"
            ann_state = result.get("annotation_state", {})
            return json.dumps({
                "status": "ok",
                "event_id": event.id,
                "annotation_state": ann_state,
            }, indent=2)

        # Local mode: use filesystem + sync to orphan branch
        threads_dir = context.threads_dir
        graph_dir = get_graph_dir(threads_dir)
        thread_dir = get_thread_graph_dir(graph_dir, topic)

        if not thread_dir.exists():
            return f"Error: Thread '{topic}' not found."

        def _do_annotate():
            append_annotation(thread_dir, event)

        push_warning = None
        try:
            run_with_sync(context, f"annotate: {kind} on {topic}", _do_annotate, topic=topic)
        except PushError as e:
            push_warning = e.message

        state = get_annotation_state(thread_dir, target_id)
        result = {
            "status": "ok",
            "event_id": event.id,
            "annotation_state": state.to_dict(),
        }
        if push_warning:
            result["push_warning"] = push_warning
        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error adding annotation: {e}"


def _remove_annotation_impl(
    topic: str,
    target_id: str,
    target_type: str,
    kind: str,
    value: str = "",
    code_path: str = "",
    actor: str = "",
) -> str:
    """Remove an annotation from an entry or thread.

    Supports tag_remove, flag_clear, xref_remove, and unpin.

    Args:
        topic: Thread topic identifier
        target_id: Entry ID (for entries) or thread topic (for threads)
        target_type: "entry" or "thread"
        kind: Removal kind — tag_remove, flag_clear, xref_remove, or unpin
        value: Tag name, flag reason, or xref entry_id to remove. Ignored for unpin.
        code_path: Path to the code repository
        actor: Who is removing the annotation

    Returns:
        Confirmation message with updated annotation state
    """
    from watercooler.baseline_graph.annotations import (
        VALID_TARGET_TYPES,
        AnnotationEvent,
        append_annotation,
        get_annotation_state,
    )
    from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir

    topic_err = _validate_topic(topic)
    if topic_err:
        return f"Error: {topic_err}"
    remove_kinds = {"tag_remove", "flag_clear", "xref_remove", "unpin", "reaction_remove"}
    if kind not in remove_kinds:
        return f"Error: kind must be one of {sorted(remove_kinds)}, got '{kind}'"
    if target_type not in VALID_TARGET_TYPES:
        return f"Error: target_type must be 'entry' or 'thread', got '{target_type}'"

    error, context = validation._require_context(code_path)
    if error:
        return error
    if context is None or not context.threads_dir:
        return "Error: Unable to resolve threads directory."

    # Derive actor: explicit param > HTTP context user_id > "unknown"
    effective_actor = actor
    if not effective_actor:
        from ..context import get_effective_context
        http_ctx = get_effective_context()
        if http_ctx and http_ctx.user_id:
            effective_actor = http_ctx.user_id
        else:
            effective_actor = "unknown"

    try:
        from ulid import ULID
        from datetime import datetime, timezone
        from dataclasses import asdict

        event = AnnotationEvent(
            id=str(ULID()),
            target_id=target_id,
            target_type=target_type,
            kind=kind,
            value=value or "",
            actor=effective_actor,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # Hosted mode: use GitHub API
        if is_hosted_context(context):
            write_err, _sha = append_annotation_hosted(topic, asdict(event))
            if write_err:
                return f"Error removing annotation (hosted): {write_err}"
            read_err, result = get_annotations_hosted(topic, target_id)
            if read_err:
                return f"Error reading annotation state (hosted): {read_err}"
            ann_state = result.get("annotation_state", {})
            return json.dumps({
                "status": "ok",
                "event_id": event.id,
                "annotation_state": ann_state,
            }, indent=2)

        # Local mode: use filesystem + sync to orphan branch
        threads_dir = context.threads_dir
        graph_dir = get_graph_dir(threads_dir)
        thread_dir = get_thread_graph_dir(graph_dir, topic)

        if not thread_dir.exists():
            return f"Error: Thread '{topic}' not found."

        def _do_remove():
            append_annotation(thread_dir, event)

        push_warning = None
        try:
            run_with_sync(context, f"annotate: {kind} on {topic}", _do_remove, topic=topic)
        except PushError as e:
            push_warning = e.message

        state = get_annotation_state(thread_dir, target_id)
        result = {
            "status": "ok",
            "event_id": event.id,
            "annotation_state": state.to_dict(),
        }
        if push_warning:
            result["push_warning"] = push_warning
        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error removing annotation: {e}"


def _get_annotations_impl(
    topic: str,
    code_path: str = "",
    target_id: str = "",
) -> str:
    """Read annotations for an entry or thread.

    If target_id is empty, returns all annotation states for the thread
    (including thread-level and all entry-level annotations).

    Args:
        topic: Thread topic identifier
        code_path: Path to the code repository
        target_id: Entry ID or thread topic. Empty returns all.

    Returns:
        JSON with annotation states
    """
    from watercooler.baseline_graph.annotations import (
        get_annotation_state,
        load_or_rebuild_state,
    )
    from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir

    topic_err = _validate_topic(topic)
    if topic_err:
        return f"Error: {topic_err}"

    error, context = validation._require_context(code_path)
    if error:
        return error
    if context is None or not context.threads_dir:
        return "Error: Unable to resolve threads directory."

    # Hosted mode: use GitHub API
    if is_hosted_context(context):
        read_err, result = get_annotations_hosted(topic, target_id)
        if read_err:
            return f"Error reading annotations (hosted): {read_err}"
        return json.dumps(result, indent=2)

    # Local mode: pull latest from orphan branch, then read from filesystem
    threads_dir = context.threads_dir
    if threads_dir and (threads_dir / ".git").exists():
        try:
            from git import Repo
            from watercooler_mcp.sync.primitives import pull_ff_only, fetch_with_timeout
            threads_repo = Repo(threads_dir)
            if not fetch_with_timeout(threads_repo, timeout=15):
                logging.getLogger(__name__).debug("Fetch before read failed (continuing with local state)")
            if not pull_ff_only(threads_repo):
                logging.getLogger(__name__).debug("Pull before read failed (continuing with local state)")
        except Exception as pull_err:
            logging.getLogger(__name__).warning(f"Pull before read failed: {pull_err}")

    graph_dir = get_graph_dir(threads_dir)
    thread_dir = get_thread_graph_dir(graph_dir, topic)

    if not thread_dir.exists():
        return f"Error: Thread '{topic}' not found."

    try:
        if target_id:
            state = get_annotation_state(thread_dir, target_id, read_only=True)
            return json.dumps({
                "target_id": target_id,
                "annotation_state": state.to_dict(),
            }, indent=2)
        else:
            states = load_or_rebuild_state(thread_dir, read_only=True)
            return json.dumps({
                "topic": topic,
                "annotation_states": {
                    tid: s.to_dict() for tid, s in states.items()
                },
            }, indent=2)

    except Exception as e:
        return f"Error reading annotations: {e}"


def _delete_entry_impl(
    topic: str,
    entry_id: str,
    code_path: str = "",
    confirmation_token: str = "",
) -> str:
    """Delete a specific entry from a thread.

    Requires a confirmation token for safety. Call once without a token to get
    the token, then call again with the token to confirm deletion.

    Args:
        topic: Thread topic identifier
        entry_id: Entry ID (ULID) to delete
        code_path: Path to the code repository
        confirmation_token: Token from first call to confirm deletion

    Returns:
        Confirmation token (first call) or deletion confirmation (second call)
    """
    import hashlib

    from watercooler.baseline_graph.writer import delete_entry_node, get_entry_node_from_graph
    from watercooler.baseline_graph.storage import get_graph_dir

    topic_err = _validate_topic(topic)
    if topic_err:
        return f"Error: {topic_err}"

    error, context = validation._require_context(code_path)
    if error:
        return error
    if context is None or not context.threads_dir:
        return "Error: Unable to resolve threads directory."

    # Generate expected confirmation token
    expected_token = hashlib.sha256(
        f"delete:{topic}:{entry_id}".encode()
    ).hexdigest()[:16]

    if not confirmation_token:
        return json.dumps({
            "action": "confirm_delete",
            "topic": topic,
            "entry_id": entry_id,
            "confirmation_token": expected_token,
            "message": f"To delete entry '{entry_id}' from '{topic}', "
                       f"call again with confirmation_token='{expected_token}'",
        }, indent=2)

    if confirmation_token != expected_token:
        return "Error: Invalid confirmation token. Call without token first to get a valid one."

    # Hosted mode: use GitHub API
    if is_hosted_context(context):
        err, result = delete_entry_hosted(topic, entry_id)
        if err:
            return f"Error deleting entry (hosted): {err}"
        return json.dumps(result, indent=2)

    # Local mode: filesystem + sync
    threads_dir = context.threads_dir
    entry_node = get_entry_node_from_graph(threads_dir, entry_id, topic=topic)
    if not entry_node:
        return f"Error: Entry '{entry_id}' not found in thread '{topic}'."

    try:
        def _do_delete():
            return delete_entry_node(threads_dir, topic, entry_id)

        push_warning = None
        try:
            success = run_with_sync(
                context, f"delete entry {entry_id[:12]} from {topic}", _do_delete, topic=topic
            )
        except PushError as e:
            success = True  # Operation succeeded locally
            push_warning = e.message
        if success:
            result = {
                "status": "deleted",
                "topic": topic,
                "entry_id": entry_id,
            }
            if push_warning:
                result["push_warning"] = push_warning
            return json.dumps(result, indent=2)
        else:
            return f"Error: Failed to delete entry '{entry_id}'."
    except Exception as e:
        return f"Error deleting entry: {e}"


def _delete_thread_impl(
    topic: str,
    code_path: str = "",
    confirmation_token: str = "",
) -> str:
    """Delete an entire thread and all its entries.

    Requires a confirmation token for safety.

    Args:
        topic: Thread topic identifier
        code_path: Path to the code repository
        confirmation_token: Token from first call to confirm deletion

    Returns:
        Confirmation token (first call) or deletion confirmation (second call)
    """
    import hashlib
    import shutil

    from watercooler.baseline_graph import storage as bg_storage

    topic_err = _validate_topic(topic)
    if topic_err:
        return f"Error: {topic_err}"

    error, context = validation._require_context(code_path)
    if error:
        return error
    if context is None or not context.threads_dir:
        return "Error: Unable to resolve threads directory."

    expected_token = hashlib.sha256(
        f"delete_thread:{topic}".encode()
    ).hexdigest()[:16]

    if not confirmation_token:
        return json.dumps({
            "action": "confirm_delete_thread",
            "topic": topic,
            "confirmation_token": expected_token,
            "message": f"To delete thread '{topic}', "
                       f"call again with confirmation_token='{expected_token}'",
        }, indent=2)

    if confirmation_token != expected_token:
        return "Error: Invalid confirmation token. Call without token first to get a valid one."

    # Hosted mode: use GitHub API
    if is_hosted_context(context):
        err, result = delete_thread_hosted(topic)
        if err:
            return f"Error deleting thread (hosted): {err}"
        return json.dumps(result, indent=2)

    # Local mode: filesystem + sync
    threads_dir = context.threads_dir
    graph_dir = bg_storage.get_graph_dir(threads_dir)
    thread_dir = bg_storage.get_thread_graph_dir(graph_dir, topic)

    if not thread_dir.exists():
        return f"Error: Thread '{topic}' not found."

    try:
        def _do_delete_thread():
            if thread_dir.exists():
                shutil.rmtree(thread_dir)
            # Evict from in-memory manifest cache
            from watercooler.baseline_graph.storage import invalidate_manifest_cache
            invalidate_manifest_cache(graph_dir)
            # Delete manifest.json on disk if not git-tracked. If tracked,
            # leave it — load_manifest() will rebuild from scan. The file
            # only if it's not git-tracked (post-migration). If it's
            # tracked, leave it — the stale topic entry is harmless
            # (thread existence is determined by meta.json, not manifest).
            manifest_file = graph_dir / "manifest.json"
            if manifest_file.exists():
                try:
                    import subprocess as _sp
                    tracked = _sp.run(
                        ["git", "-C", str(threads_dir), "ls-files", "--error-unmatch",
                         str(manifest_file.relative_to(threads_dir))],
                        capture_output=True, text=True, timeout=5,
                    )
                    # returncode 0 = tracked, 1 = not tracked.
                    # Only delete if returncode 1 AND stderr contains the
                    # expected "did not match" message (not a git failure).
                    if tracked.returncode == 1 and "did not match" in tracked.stderr:
                        manifest_file.unlink(missing_ok=True)
                except Exception as e:
                    import logging as _logging
                    _logging.getLogger(__name__).debug(
                        f"Could not check manifest tracking status: {e}"
                    )

        push_warning = None
        try:
            run_with_sync(context, f"delete thread {topic}", _do_delete_thread, topic=topic)
        except PushError as e:
            push_warning = e.message

        result = {"status": "deleted", "topic": topic}
        if push_warning:
            result["push_warning"] = push_warning
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error deleting thread: {e}"


def _archive_thread_impl(
    topic: str,
    code_path: str = "",
    reason: str = "",
    unarchive: bool = False,
) -> str:
    """Archive or unarchive a thread (soft-delete).

    Archived threads keep their data but are marked as archived in meta.json.
    Optionally posts a Closure entry explaining the archive reason.

    Args:
        topic: Thread topic identifier
        code_path: Path to the code repository
        reason: Why the thread is being archived (resolved, abandoned, superseded, other)
        unarchive: If True, unarchive instead of archive

    Returns:
        Confirmation message
    """
    from watercooler.baseline_graph.storage import (
        get_graph_dir,
        load_thread_meta,
        write_thread_meta,
    )

    topic_err = _validate_topic(topic)
    if topic_err:
        return f"Error: {topic_err}"

    error, context = validation._require_context(code_path)
    if error:
        return error
    if context is None or not context.threads_dir:
        return "Error: Unable to resolve threads directory."

    # Hosted mode: use GitHub API
    if is_hosted_context(context):
        err, result = archive_thread_hosted(topic, reason=reason, unarchive=unarchive)
        if err:
            return f"Error archiving thread (hosted): {err}"
        return json.dumps(result, indent=2)

    # Local mode: filesystem + sync
    threads_dir = context.threads_dir
    graph_dir = get_graph_dir(threads_dir)

    meta = load_thread_meta(graph_dir, topic)
    if not meta:
        return f"Error: Thread '{topic}' not found."

    try:
        if unarchive:
            def _do_unarchive():
                meta.pop("archived", None)
                meta.pop("archive_reason", None)
                meta["status"] = "OPEN"
                write_thread_meta(graph_dir, topic, meta)
            push_warning = None
            try:
                run_with_sync(context, f"unarchive thread {topic}", _do_unarchive, topic=topic)
            except PushError as e:
                push_warning = e.message
            result = {"status": "unarchived", "topic": topic}
            if push_warning:
                result["push_warning"] = push_warning
            return json.dumps(result, indent=2)
        else:
            def _do_archive():
                meta["archived"] = True
                if reason:
                    meta["archive_reason"] = reason
                meta["status"] = "CLOSED"
                write_thread_meta(graph_dir, topic, meta)
            push_warning = None
            try:
                run_with_sync(context, f"archive thread {topic}", _do_archive, topic=topic)
            except PushError as e:
                push_warning = e.message
            result = {
                "status": "archived",
                "topic": topic,
                "reason": reason or "none provided",
            }
            if push_warning:
                result["push_warning"] = push_warning
            return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error archiving thread: {e}"


def _sync_repair_impl(
    code_path: str = "",
    diagnose_only: bool = False,
    dry_run: bool = False,
    regenerate_cache: bool = False,
    migrate: bool = False,
    confirm_migrate: bool = False,
) -> str:
    """Diagnose and fix orphan branch sync issues.

    Args:
        code_path: Path to the code repository
        diagnose_only: If True, only report state without fixing
        dry_run: If True, show what would be done without doing it
        regenerate_cache: If True, rebuild manifest + search-index from per-thread data
        migrate: If True, one-time cleanup of globally-committed derived files
        confirm_migrate: Must be True to execute migrate without dry_run

    Returns:
        JSON report of findings or actions taken
    """
    # Hosted mode guard — additive early return
    from ..auth import is_hosted_mode
    if is_hosted_mode():
        return json.dumps({
            "status": "not_applicable",
            "message": "sync_repair is not applicable in hosted mode. "
                       "GitHub API is the source of truth — there is no local sync to repair.",
            "source": "hosted_github_api",
        }, indent=2)

    from watercooler.path_resolver import resolve_threads_dir
    from watercooler.sync_repair import diagnose, repair

    threads_dir = resolve_threads_dir(code_root=Path(code_path)) if code_path else None
    if not threads_dir:
        return json.dumps({"error": "Could not resolve threads directory"})

    if diagnose_only:
        report = diagnose(threads_dir)
        return json.dumps(report.to_dict(), indent=2)

    # migrate is destructive (git rm + push) — require confirmation
    if migrate and not dry_run and not confirm_migrate:
        return json.dumps({
            "action": "confirm_migrate",
            "message": "migrate is destructive (git rm + push). "
                       "Run with dry_run=True first to preview, then re-run with "
                       "migrate=True, confirm_migrate=True to execute.",
        }, indent=2)

    actions = repair(
        threads_dir,
        dry_run=dry_run,
        regenerate_cache=regenerate_cache,
        migrate=migrate,
    )
    return json.dumps({"actions": actions}, indent=2)


# Module-level references for annotation tools
annotate_tool = None
remove_annotation_tool = None
get_annotations_tool = None
delete_entry_tool = None
delete_thread_tool = None
archive_thread_tool = None

# Module-level references for new tools
graph_enrich_tool = None
graph_recover_tool = None
graph_project_tool = None


# ---------------------------------------------------------------------------
# TOOL_BUILDERS: map of public tool name → (impl_func, global_name)
# ---------------------------------------------------------------------------

TOOL_BUILDERS: dict[str, tuple] = {
    "watercooler_baseline_graph_stats": (_baseline_graph_stats_impl, "baseline_graph_stats"),
    "watercooler_search": (_search_graph_impl, "search_graph_tool"),
    "watercooler_find_similar": (_find_similar_entries_impl, "find_similar_entries_tool"),
    "watercooler_baseline_sync_status": (_baseline_sync_status_impl, "baseline_sync_status_tool"),
    "watercooler_access_stats": (_access_stats_impl, "access_stats_tool"),
    "watercooler_graph_enrich": (_graph_enrich_impl, "graph_enrich_tool"),
    "watercooler_graph_recover": (_graph_recover_impl, "graph_recover_tool"),
    "watercooler_graph_project": (_graph_project_impl, "graph_project_tool"),
    "watercooler_annotate": (_annotate_impl, "annotate_tool"),
    "watercooler_remove_annotation": (_remove_annotation_impl, "remove_annotation_tool"),
    "watercooler_get_annotations": (_get_annotations_impl, "get_annotations_tool"),
    "watercooler_delete_entry": (_delete_entry_impl, "delete_entry_tool"),
    "watercooler_delete_thread": (_delete_thread_impl, "delete_thread_tool"),
    "watercooler_archive_thread": (_archive_thread_impl, "archive_thread_tool"),
    "watercooler_sync_repair": (_sync_repair_impl, "sync_repair_tool"),
}


def _build_hybrid_search_wrapper(runtime):
    """Build a hybrid wrapper for ``watercooler_search``.

    The wrapper intercepts the call, resolves the capability based on
    the ``mode`` argument, then routes to local, remote, or disabled.
    """
    import functools
    from ..capabilities import resolve_search_capability

    @functools.wraps(_search_graph_impl)
    async def _hybrid_search(ctx, **kwargs):
        mode = kwargs.get("mode", "auto")
        query = kwargs.get("query", "")
        semantic = kwargs.get("semantic", False)
        # Infer actual mode for capability resolution
        resolved_mode = infer_search_mode(mode, query, semantic)
        # Codex review: pass ``semantic`` through so that semantic entry
        # search resolves to ``semantic_similarity`` (remote in hybrid)
        # rather than falling back to ``baseline_search`` (local).
        capability = resolve_search_capability(
            resolved_mode, query=query, semantic=semantic
        )
        target = runtime.capability_profile.resolve_execution_target(
            capability,
            local_available=True,
            remote_available=runtime.premium_client is not None,
        )
        if target == "remote":
            if runtime.premium_client is None:
                return json.dumps({
                    "error": "remote_unavailable",
                    "capability": capability,
                    "message": "Remote premium client is not configured.",
                })
            return await runtime.premium_client.call_tool_text(
                "watercooler_search", kwargs
            )
        if target == "disabled":
            return json.dumps({
                "error": "capability_disabled",
                "capability": capability,
            }, indent=2)
        # In hybrid local mode, force baseline backend. The user's
        # memory.backend config applies to full/hosted surfaces, not
        # hybrid local where T2/T3 routes to Railway.
        kwargs["backend"] = "baseline"
        return await _search_graph_impl(ctx, **kwargs)

    return _hybrid_search


def _build_hybrid_find_similar_wrapper(runtime):
    """Build a hybrid wrapper for ``watercooler_find_similar``."""
    import functools
    from ..capabilities import resolve_find_similar_capability

    @functools.wraps(_find_similar_entries_impl)
    async def _hybrid_find_similar(ctx, **kwargs):
        capability = resolve_find_similar_capability()
        target = runtime.capability_profile.resolve_execution_target(
            capability,
            local_available=True,
            remote_available=runtime.premium_client is not None,
        )
        if target == "remote":
            if runtime.premium_client is None:
                return json.dumps({
                    "error": "remote_unavailable",
                    "capability": capability,
                    "message": "Remote premium client is not configured.",
                })
            return await runtime.premium_client.call_tool_text(
                "watercooler_find_similar", kwargs
            )
        if target == "disabled":
            return json.dumps({
                "error": "capability_disabled",
                "capability": capability,
            }, indent=2)
        # Local execution: _find_similar_entries_impl is sync
        return _find_similar_entries_impl(ctx, **kwargs)

    return _hybrid_find_similar


def register_graph_tools(mcp, *, selected=None, runtime=None):
    """Register graph tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
        selected: Optional collection of tool names to register.
            ``None`` means register all tools in this module.
        runtime: Optional ToolRuntime. When surface is ``local_hybrid``,
            mixed tools get hybrid wrappers that route local/remote/disabled.
    """
    global baseline_graph_stats, search_graph_tool
    global find_similar_entries_tool, baseline_sync_status_tool, access_stats_tool
    global graph_enrich_tool, graph_recover_tool, graph_project_tool
    global annotate_tool, remove_annotation_tool, get_annotations_tool
    global delete_entry_tool, delete_thread_tool, archive_thread_tool

    _globals = globals()
    for tool_name, (impl_func, global_name) in TOOL_BUILDERS.items():
        if selected is not None and tool_name not in selected:
            continue
        # Apply hybrid wrappers for mixed tools when running in hybrid mode
        actual_impl = impl_func
        if runtime is not None and getattr(runtime, "surface", None) == "local_hybrid":
            if tool_name == "watercooler_search":
                actual_impl = _build_hybrid_search_wrapper(runtime)
            elif tool_name == "watercooler_find_similar":
                actual_impl = _build_hybrid_find_similar_wrapper(runtime)
        registered = mcp.tool(name=tool_name)(actual_impl)
        _globals[global_name] = registered
