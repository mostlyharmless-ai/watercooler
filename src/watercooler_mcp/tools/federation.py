"""Federation tools for watercooler MCP server.

Tools:
- watercooler_federated_search: Cross-namespace keyword search
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastmcp import Context

from typing import Any

from watercooler.baseline_graph.search import SearchQuery, search_graph
from watercooler.config_facade import config

from ..auth import is_hosted_mode
from .. import validation
from ..federation.access import filter_allowed_namespaces, is_topic_denied
from ..federation.merger import (
    ScoredResult,
    build_response_envelope,
    merge_results,
)
from ..federation.resolver import resolve_all_namespaces
from ..federation.scoring import (
    compute_ranking_score,
    compute_recency_decay,
    normalize_keyword_score,
    resolve_namespace_weight,
)
from ..observability import log_action

logger = logging.getLogger(__name__)

# Module-level references to registered tools (populated by register_federation_tools)
federated_search_tool = None

# Validation bounds
MAX_QUERY_LENGTH = 500
MAX_LIMIT = 100


def _extract_entry_data(entry: Any) -> dict[str, Any]:
    """Extract display fields from a search result entry."""
    return {
        "topic": getattr(entry, "thread_topic", ""),
        "title": getattr(entry, "title", ""),
        "entry_id": getattr(entry, "entry_id", ""),
        "role": getattr(entry, "role", ""),
        "agent": getattr(entry, "agent", ""),
        "entry_type": getattr(entry, "entry_type", ""),
        "summary": getattr(entry, "summary", ""),
    }


async def _federated_search_impl(
    ctx: Context,
    query: str,
    code_path: str = "",
    code_branch: str = "",
    namespaces: str = "",
    limit: int = 10,
) -> str:
    """Search across federated watercooler namespaces.

    Performs read-only keyword search across configured watercooler repositories.
    Results are normalized, weighted by namespace proximity, and returned with
    full provenance metadata.

    Args:
        query: Search query (required, max 500 chars).
        code_path: Primary repository root path.
        code_branch: Branch hint for PRIMARY namespace only (Phase 2).
            Currently echoed in response envelope as primary_branch_filter
            but does NOT filter search results. Actual branch-scoped search
            will be implemented when SearchQuery supports a code_branch field.
            Secondary namespaces always search all branches.
        namespaces: Comma-separated namespace IDs to search (override).
            Leave empty to search all configured namespaces.
        limit: Max results to return (1-100, default 10).

    Returns:
        JSON response envelope with schema_version, results, and
        per-namespace provenance metadata.
    """
    try:
        return await _federated_search_inner(ctx, query, code_path, code_branch, namespaces, limit)
    except Exception:
        logger.exception("Federation search failed unexpectedly")
        return json.dumps({
            "schema_version": 1,
            "error": "internal_error",
            "message": "Federation search encountered an unexpected error",
            "results": [],
        })


async def _federated_search_inner(
    ctx: Context,
    query: str,
    code_path: str,
    code_branch: str,
    namespaces: str,
    limit: int,
) -> str:
    """Inner implementation of federated search (unwrapped)."""
    # 1. Validate inputs
    if not query or not query.strip():
        return json.dumps({"schema_version": 1, "error": "EMPTY_QUERY", "message": "Query cannot be empty"})
    if len(query) > MAX_QUERY_LENGTH:
        return json.dumps({
            "schema_version": 1,
            "error": "VALIDATION_ERROR",
            "message": f"Query exceeds maximum length ({MAX_QUERY_LENGTH} chars)",
        })
    # Sanitize query for safe logging (strip control chars)
    query = query.replace("\n", " ").replace("\r", " ").replace("\x1b", "")
    limit = max(1, min(limit, MAX_LIMIT))

    # 2. Feature gate check (fail fast before git overhead)
    fed_config = config.full().federation
    if not fed_config.enabled:
        return json.dumps({
            "schema_version": 1,
            "error": "FEDERATION_DISABLED",
            "message": "Federation is not enabled. Set federation.enabled = true in config.toml",
        })

    # 3. Resolve primary context
    error, primary_ctx = validation._require_context(code_path)
    if error:
        return json.dumps({"schema_version": 1, "error": "CONTEXT_ERROR", "message": error})

    # 4. Hosted mode guard
    if is_hosted_mode():
        return json.dumps({
            "schema_version": 1,
            "error": "FEDERATION_NOT_AVAILABLE",
            "message": "Federated search is not available in hosted mode",
        })

    # 5. Parse namespace override
    namespace_override = None
    if namespaces.strip():
        namespace_override = [ns.strip() for ns in namespaces.split(",") if ns.strip()]

    # 6. Resolve namespaces
    resolutions = resolve_all_namespaces(primary_ctx, fed_config, namespace_override)

    # 7. Check max_namespaces cap
    if len(resolutions) > fed_config.max_namespaces:
        return json.dumps({
            "schema_version": 1,
            "error": "TOO_MANY_NAMESPACES",
            "message": (
                f"Query spans {len(resolutions)} namespaces, "
                f"exceeding max_namespaces={fed_config.max_namespaces}"
            ),
        })

    # Find primary namespace ID
    primary_ns_id = None
    for ns_id, res in resolutions.items():
        if res.is_primary:
            primary_ns_id = ns_id
            break

    if primary_ns_id is None:
        return json.dumps({"schema_version": 1, "error": "NO_PRIMARY", "message": "Could not identify primary namespace"})

    # 8. Access control
    all_ns_ids = list(resolutions.keys())
    allowed_ns_ids, denied_map = filter_allowed_namespaces(
        primary_ns_id, all_ns_ids, fed_config.access
    )

    # Initialize namespace_status with denied namespaces
    namespace_status: dict[str, Any] = {k: {"status": v} for k, v in denied_map.items()}

    # Add not_initialized status for unresolved namespaces (with diagnostics)
    for ns_id in allowed_ns_ids:
        res = resolutions[ns_id]
        if res.status != "ok":
            ns_detail: dict[str, Any] = {"status": res.status}
            if res.error_message:
                ns_detail["error_message"] = res.error_message
            if res.action_hint:
                ns_detail["action_hint"] = res.action_hint
            namespace_status[ns_id] = ns_detail

    # Filter to searchable namespaces (ok status and allowed)
    searchable = [ns_id for ns_id in allowed_ns_ids if resolutions[ns_id].status == "ok"]

    if primary_ns_id not in searchable:
        # Primary must be searchable
        return json.dumps({
            "schema_version": 1,
            "error": "PRIMARY_NOT_AVAILABLE",
            "message": f"Primary namespace '{primary_ns_id}' is not available: "
                       f"{resolutions[primary_ns_id].status}",
        })

    # 9. Compute allocation
    primary_limit = limit
    per_secondary_limit = max(limit // 2, 1)

    # 10. Fan out parallel searches
    now = datetime.now(timezone.utc)

    async def search_namespace(ns_id: str) -> tuple[str, list[ScoredResult] | None, str]:
        """Search a single namespace and score results."""
        res = resolutions[ns_id]
        is_primary = res.is_primary
        ns_limit = primary_limit if is_primary else per_secondary_limit

        # Build search query
        sq = SearchQuery(
            query=query,
            limit=ns_limit,
            include_threads=False,
            include_entries=True,
        )

        try:
            search_results = await asyncio.wait_for(
                asyncio.to_thread(search_graph, res.threads_dir, sq),
                timeout=fed_config.namespace_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Federation: namespace '%s' timed out", ns_id)
            return ns_id, None, "timeout"
        except Exception as e:
            logger.error("Federation: namespace '%s' error: %s", ns_id, e)
            return ns_id, None, "error"

        # Resolve namespace weight
        nw = resolve_namespace_weight(ns_id, primary_ns_id, fed_config.scoring)

        # Score results
        scored: list[ScoredResult] = []
        ns_config = fed_config.namespaces.get(ns_id)

        for sr in search_results.results:
            if sr.entry is None:
                continue

            # Check deny_topics for secondary namespaces
            if ns_config and not is_primary:
                topic = getattr(sr.entry, "thread_topic", "") or ""
                if topic and is_topic_denied(topic, ns_config):
                    continue

            # Parse timestamp for recency decay
            entry_ts = now  # default to now if no timestamp
            ts_str = ""
            if hasattr(sr.entry, "timestamp") and sr.entry.timestamp:
                ts_str = sr.entry.timestamp
                try:
                    entry_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass

            recency = compute_recency_decay(
                entry_ts, now,
                floor=fed_config.scoring.recency_floor,
                half_life_days=fed_config.scoring.recency_half_life_days,
            )
            norm_score = normalize_keyword_score(sr.score)
            ranking = compute_ranking_score(sr.score, nw, recency)

            entry_data = _extract_entry_data(sr.entry)

            scored.append(ScoredResult(
                entry_id=sr.node_id,
                origin_namespace=ns_id,
                raw_score=sr.score,
                normalized_score=norm_score,
                namespace_weight=nw,
                recency_decay=recency,
                ranking_score=ranking,
                entry_data=entry_data,
                timestamp=ts_str or now.isoformat(),
            ))

        return ns_id, scored, "ok"

    # Execute searches in parallel with total timeout.
    # Use asyncio.wait() so completed results are preserved when the
    # total timeout fires (gather+wait_for discards everything).
    task_objects = [asyncio.ensure_future(search_namespace(ns_id)) for ns_id in searchable]

    done, pending = await asyncio.wait(
        task_objects, timeout=fed_config.max_total_timeout
    )
    for p in pending:
        p.cancel()
    if pending:
        logger.warning(
            "Federation: total timeout exceeded (%ss), %d namespace(s) cancelled",
            fed_config.max_total_timeout, len(pending),
        )

    # Process results
    namespace_results: dict[str, list[ScoredResult]] = {}
    total_candidates = 0

    for task_obj in done:
        exc = task_obj.exception()
        if exc is not None:
            logger.error("Federation: unexpected error: %s", exc)
            continue
        ns_id, scored, status = task_obj.result()
        namespace_status[ns_id] = {"status": status}
        if scored is not None:
            namespace_results[ns_id] = scored
            total_candidates += len(scored)

    # Mark timed-out namespaces
    completed_ns_ids = {ns_id for ns_id, _, _ in (
        task_obj.result() for task_obj in done if task_obj.exception() is None
    )}
    for ns_id in searchable:
        if ns_id not in completed_ns_ids and ns_id not in namespace_status:
            namespace_status[ns_id] = {"status": "timeout"}

    # 11. Check primary status
    primary_status = namespace_status.get(primary_ns_id, {})
    primary_status_val = primary_status.get("status") if isinstance(primary_status, dict) else primary_status
    if primary_status_val != "ok":
        return json.dumps({
            "schema_version": 1,
            "error": "PRIMARY_SEARCH_FAILED",
            "message": f"Primary namespace '{primary_ns_id}' search failed: "
                       f"{primary_status_val or 'unknown'}",
        })

    # 12. Merge results
    merged = merge_results(namespace_results, primary_ns_id, limit)

    # 13. Build response envelope
    envelope = build_response_envelope(
        results=merged,
        primary_namespace=primary_ns_id,
        namespace_status=namespace_status,
        queried_namespaces=list(resolutions.keys()),
        query=query,
        total_candidates=total_candidates,
        primary_branch_filter=code_branch,
    )

    log_action(
        f"federated_search: {len(merged)} results from {len(searchable)} namespaces "
        f"(query={query[:50]!r})"
    )

    return json.dumps(envelope)


def register_federation_tools(mcp):
    """Register federation tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global federated_search_tool
    federated_search_tool = mcp.tool(name="watercooler_federated_search")(_federated_search_impl)
