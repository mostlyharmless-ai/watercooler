"""Memory tools for watercooler MCP server (Graphiti backend).

Tools:
- watercooler_get_entity_edge: Get entity/edge details
- watercooler_diagnose_memory: Diagnose memory backend
- watercooler_graphiti_add_episode: Add episode to Graphiti
- watercooler_leanrag_run_pipeline: Run LeanRAG clustering pipeline
- watercooler_clear_graph_group: Clear episodes for a group
- watercooler_smart_query: Multi-tier intelligent query with auto-escalation
- watercooler_memory_task_status: Check queue health, poll task status, recover
- watercooler_bulk_index: Queue bulk thread indexing into memory backend
- watercooler_get_entry_provenance: Bidirectional entry↔episode provenance lookup

Removed (use replacements):
- watercooler_query_memory → watercooler_smart_query
- watercooler_search_nodes → watercooler_search(mode="entities")
- watercooler_search_memory_facts → watercooler_smart_query
- watercooler_get_episodes → watercooler_search(mode="episodes")
"""

import asyncio
import json
import logging
import os
import threading as _threading
from pathlib import Path
from typing import Any

from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent


from ..observability import log_action, log_error, log_warning
from .. import validation  # Import module for runtime access (enables test patching)
from ._boost import boost_decision_items, sanitize_boost

logger = logging.getLogger(__name__)


# Module-level references to registered tools (populated by register_memory_tools)
get_entity_edge = None
diagnose_memory = None

# Write tools (Milestone 5.1, 5.2)
graphiti_add_episode = None
leanrag_run_pipeline = None

# Cleanup tools
clear_graph_group = None

# Multi-tier orchestration
smart_query = None

# Memory task queue tools
memory_task_status = None
bulk_index = None

# Provenance tools
get_entry_provenance = None


def _ensure_t2_suffix(group_id: str) -> str:
    """Append ``_t2`` to ``group_id`` if not already present.

    Empty in stays empty out. Plan v20 canonical T2 graph names take the
    form ``<org>_<repo>_t2``; legacy callers passed ``<org>_<repo>`` (or
    bare ``<repo>``). This helper makes the suffix idempotent so the
    canonicalization helper can accept either form without surprise.
    """
    if not group_id:
        return ""
    if group_id.endswith("_t2"):
        return group_id
    return f"{group_id}_t2"


def _canonicalize_t2_group_id(caller_group_id: str) -> tuple[str, dict | None]:
    """Resolve the authoritative T2 graph database name for a hosted write.

    Returns ``(canonical_database_name, error_dict_or_None)``.

    Plan v20 defect #34 fix: the queued-work executor in
    ``memory_sync.graphiti_executor`` uses ``task.group_id`` directly as
    the FalkorDB graph database name. To make hosted T2 writes land in
    the canonical ``<org>_<repo>_t2`` graph (Plan v20 intent), we
    canonicalize the caller-supplied ``group_id`` here, before
    enqueueing. Mirrors :func:`watercooler_mcp.tools.semantic._scope_group_id_to_http_ctx`
    for T1.

    Behaviour:

    - On a hosted request (``http_ctx`` set with ``repo`` header), the
      canonical T2 database name is derived from ``repo``; caller value
      is advisory; mismatches log a WARNING and the hosted scope wins.
    - With ``http_ctx`` set but ``repo`` missing, return a
      ``scope_resolution_failed`` error rather than fall through.
    - Off-hosted (stdio / dev, ``http_ctx is None``), caller value is
      accepted as-is, with ``_t2`` suffix appended idempotently.
    """
    try:
        from ..context import get_effective_context
    except ImportError:
        get_effective_context = None  # type: ignore[assignment]

    # Wrap the call too: ``get_effective_context()`` can raise at runtime
    # (e.g., contextvar lookup error in unusual threading configs). Without
    # this, the exception would surface as an unhandled error in the
    # caller rather than a structured ``scope_resolution_failed`` response.
    if get_effective_context is None:
        http_ctx = None
    else:
        try:
            http_ctx = get_effective_context()
        except Exception as _ctx_err:
            return "", {
                "success": False,
                "error": (
                    f"scope_resolution_failed: get_effective_context() raised "
                    f"{_ctx_err.__class__.__name__}: {_ctx_err}"
                ),
            }
    if http_ctx is None:
        # Off-hosted (stdio / dev): caller value is authoritative. Reject
        # an empty caller value here rather than returning ``("", None)`` —
        # the executor downstream would otherwise compute
        # ``canonical_database = "_t2"`` and write to a stub database.
        canonical = _ensure_t2_suffix(caller_group_id)
        if not canonical:
            return "", {
                "success": False,
                "error": (
                    "scope_resolution_failed: empty group_id; the off-hosted "
                    "T2 path requires a caller-supplied group_id."
                ),
            }
        return canonical, None
    if not getattr(http_ctx, "repo", None):
        return "", {
            "success": False,
            "error": (
                "scope_resolution_failed: hosted request arrived without "
                "an X-Repo header; hosted T2 writes require a resolved "
                "tenant scope."
            ),
        }
    if "/" not in http_ctx.repo:
        # Reject malformed slugs explicitly under hosted multi-tenancy:
        # ``derive_t2_database_name`` would silently fall through to
        # ``derive_group_id`` and return ``<bare>_t2`` instead of the
        # canonical ``<org>_<repo>_t2`` — different graph from the one a
        # well-formed request lands in. Surface the failure instead.
        return "", {
            "success": False,
            "error": (
                f"scope_resolution_failed: hosted X-Repo header "
                f"{http_ctx.repo!r} is malformed (expected '<org>/<repo>'); "
                f"refusing to fall back to a non-canonical database name."
            ),
        }

    try:
        from watercooler.path_resolver import (
            derive_t2_database_name,
            get_threads_suffix,
        )

        # Strip any threads-repo suffix the X-Repo header may carry, same
        # way _scope_group_id_to_http_ctx does for T1. Use the configured
        # suffix from path_resolver (default "-threads") rather than a
        # hardcoded literal so deployments overriding
        # WATERCOOLER_THREADS_SUFFIX still produce canonical names.
        owner, repo = http_ctx.repo.split("/", 1)
        threads_suffix = get_threads_suffix()
        if threads_suffix and repo.endswith(threads_suffix):
            repo = repo[: -len(threads_suffix)]
        if not owner or not repo:
            return "", {
                "success": False,
                "error": (
                    f"scope_resolution_failed: hosted X-Repo header "
                    f"{http_ctx.repo!r} has empty owner or repo after "
                    f"threads-suffix strip (owner={owner!r}, repo={repo!r}); "
                    f"refusing to fall back to a non-canonical name."
                ),
            }
        scoped = derive_t2_database_name(repo_slug=f"{owner}/{repo}")
    except Exception as e:
        return "", {
            "success": False,
            "error": f"scope_resolution_failed: {e}",
        }

    caller_canonical = _ensure_t2_suffix(caller_group_id)
    if caller_canonical and caller_canonical != scoped:
        logger.warning(
            "HOSTED_T2: caller-supplied group_id %r (canonical %r) does not "
            "match http_ctx-derived %r; using hosted scope (tenant isolation).",
            caller_group_id, caller_canonical, scoped,
        )
    return scoped, None

# Runtime context (set by register_memory_tools)
_runtime: "ToolRuntime | None" = None

def _cap_tiers_by_profile(config: Any) -> None:
    """Disable tiers that exceed the effective hosted profile.

    Mutates *config* in place:
    - ``"core"`` → only T1 (disable T2 and T3)
    - ``"t2"``   → T1 + T2 (disable T3)
    - ``"t2t3"`` → all tiers allowed (no-op)
    """
    if _runtime is None:
        return  # No runtime context — no capping
    profile = _runtime.effective_hosted_profile
    if profile == "core":
        config.t2_enabled = False
        config.t3_enabled = False
    elif profile == "t2":
        config.t3_enabled = False


# Provenance index cache (mtime-based invalidation, thread-safe)
_provenance_cache_lock = _threading.Lock()
_cached_provenance_index: "EntryEpisodeIndex | None" = None
_cached_provenance_mtime: float = 0.0
_cached_provenance_path: str = ""


def _clear_provenance_cache() -> None:
    """Reset the provenance index cache (useful in tests)."""
    global _cached_provenance_index, _cached_provenance_mtime, _cached_provenance_path
    with _provenance_cache_lock:
        _cached_provenance_index = None
        _cached_provenance_mtime = 0.0
        _cached_provenance_path = ""


def _get_cached_provenance_index(index_path: Path) -> "EntryEpisodeIndex":
    """Return cached EntryEpisodeIndex, reloading only when file changes.

    Thread-safe: uses a lock to prevent concurrent cache invalidation races.
    """
    global _cached_provenance_index, _cached_provenance_mtime, _cached_provenance_path
    from watercooler_memory.entry_episode_index import EntryEpisodeIndex, IndexConfig

    with _provenance_cache_lock:
        path_str = str(index_path)
        file_exists = index_path.exists()
        current_mtime = index_path.stat().st_mtime if file_exists else 0.0
        if (
            _cached_provenance_index is None
            or not file_exists  # never serve stale cache for missing files
            or current_mtime > _cached_provenance_mtime
            or path_str != _cached_provenance_path
        ):
            idx = EntryEpisodeIndex(
                IndexConfig(backend="graphiti", index_path=index_path)
            )
            if file_exists:
                idx.load()
                _cached_provenance_index = idx
                _cached_provenance_mtime = current_mtime
                _cached_provenance_path = path_str
            # else: return fresh empty index; callers handle empty results
            # gracefully.  Not cached so a future file appearance is picked up.
            return idx
        assert _cached_provenance_index is not None  # guarded by cache-miss branch above
        return _cached_provenance_index


async def _get_entity_edge_impl(
    uuid: str,
    ctx: Context,
    code_path: str = "",
    group_id: str | None = None,
) -> ToolResult:
    """Get a specific entity edge (relationship) by UUID.

    Retrieves detailed information about a specific relationship between entities
    in the Graphiti knowledge graph.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. Index built: Use watercooler memory CLI to index threads first
        3. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        uuid: Edge UUID to retrieve
        ctx: MCP context
        code_path: Path to code repository (for resolving threads directory)
        group_id: Project group_id (database name) where edge is stored.
                 In the unified model, all threads share one group_id per project
                 (e.g., "watercooler_cloud"). Searches default database if not provided.

    Returns:
        JSON response with edge details containing:
        - uuid: Edge UUID
        - fact: Description of the relationship
        - source_node_uuid: UUID of source entity
        - target_node_uuid: UUID of target entity
        - valid_at: When relationship became valid
        - invalid_at: When relationship became invalid (if applicable)
        - created_at: When edge was created
        - group_id: Thread topic this edge belongs to
        - message: Status message

    Example:
        get_entity_edge(
            uuid="01ABC123...",
            code_path="."
        )

    Response Format:
        {
          "uuid": "01ABC123...",
          "fact": "Claude implemented OAuth2 authentication",
          "source_node_uuid": "01DEF456...",
          "target_node_uuid": "01GHI789...",
          "valid_at": "2025-10-01T10:00:00Z",
          "created_at": "2025-10-01T10:00:00Z",
          "group_id": "auth-feature",
          "message": "Retrieved edge 01ABC123..."
        }
    """
    try:
        from .. import memory as mem

        # Validate UUID parameter (tool-specific validation)
        if not uuid or not uuid.strip():
            return mem.create_error_response(
                "Invalid UUID",
                "UUID parameter is required and must be non-empty",
                "get_entity_edge"
            )

        # Sanitize UUID (limit length and characters)
        if len(uuid) > 100:
            return mem.create_error_response(
                "Invalid UUID",
                "UUID too long (max 100 characters)",
                "get_entity_edge",
                uuid=uuid[:50] + "..."
            )

        # Check for valid characters (alphanumeric, hyphen, underscore)
        if not all(c.isalnum() or c in '-_' for c in uuid):
            return mem.create_error_response(
                "Invalid UUID",
                "UUID contains invalid characters (only alphanumeric, hyphen, underscore allowed)",
                "get_entity_edge"
            )

        # Common validation (replaces ~100 lines of duplicated code)
        backend, error = mem.validate_memory_prerequisites("get_entity_edge", code_path=code_path)
        if error:
            return error

        # Execute query
        log_action("memory.get_entity_edge", uuid=uuid, group_id=group_id)

        try:
            edge = await asyncio.to_thread(backend.get_entity_edge, uuid, group_id=group_id)

            # Handle None return (edge not found)
            if edge is None:
                return mem.create_error_response(
                    "Edge not found",
                    f"No edge found with UUID {uuid}",
                    "get_entity_edge",
                    uuid=uuid
                )

            # Format response
            response = {
                **edge,
                "message": f"Retrieved edge {uuid}",
            }

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: Get entity edge failed: {e}")
            return mem.create_error_response(
                "Edge retrieval failed",
                str(e),
                "get_entity_edge",
                uuid=uuid
            )

    except Exception as e:
        from .. import memory as mem

        log_error(f"MEMORY: Unexpected error in get_entity_edge: {e}")
        return mem.create_error_response(
            "Internal error",
            str(e),
            "get_entity_edge"
        )


def _add_canonical_identity_fields(diagnostics: dict, code_path: str = "") -> None:
    """Augment a diagnostics dict with Plan v20 canonical identity fields.

    Adds ``repo_slug``, ``repo_name``, ``project_group_id``,
    ``t1_database``, ``t2_database`` via the slug-aware helpers in
    :mod:`watercooler.path_resolver`. Also adds a ``phase_indicator``
    field so operators can tell at a glance which split-surface
    state the deployment is in.
    """
    try:
        from watercooler.path_resolver import (
            derive_project_group_id,
            derive_t1_database_name,
            derive_t2_database_name,
        )
    except Exception:
        return

    # Resolve repo context best-effort. Hybrid/hosted callers may not
    # have a filesystem code_path, so we rely on the config + http ctx
    # where available and fall back gracefully.
    repo_slug = None
    repo_name = None
    http_ctx = None
    try:
        from ..context import get_effective_context
        http_ctx = get_effective_context()
        if http_ctx is not None:
            repo_slug = getattr(http_ctx, "repo", None)
    except Exception:
        repo_slug = None

    try:
        from ..auth import is_hosted_mode
    except ImportError as _hosted_err:
        log_error(
            f"MEMORY: failed to import is_hosted_mode: "
            f"{_hosted_err.__class__.__name__}: {_hosted_err}; "
            f"using local diagnostic graph-name derivation and surfacing "
            f"the auth import error."
        )
        diagnostics["hosted_mode_error"] = (
            f"failed to import is_hosted_mode: {_hosted_err}"
        )
        hosted_mode = False
    except Exception as _hosted_err:
        log_error(
            f"MEMORY: failed to import is_hosted_mode: "
            f"{_hosted_err.__class__.__name__}: {_hosted_err}; "
            f"refusing diagnostic graph-name fallback because hosted-mode "
            f"detection could not be loaded safely."
        )
        diagnostics["scope_error"] = (
            "Hosted-mode detection failed while importing auth helpers; "
            "refusing to derive fallback graph names."
        )
        hosted_mode = True
    else:
        try:
            hosted_mode = is_hosted_mode()
        except Exception as _hosted_err:
            log_error(
                f"MEMORY: is_hosted_mode() raised "
                f"{_hosted_err.__class__.__name__}: {_hosted_err}; refusing "
                f"diagnostic graph-name fallback because hosted scope safety "
                f"could not be determined."
            )
            hosted_mode = True

    try:
        from watercooler.path_resolver import derive_code_repo_name
        from pathlib import Path as _Path
        if code_path:
            repo_name = derive_code_repo_name(code_path=_Path(code_path))
    except Exception:
        pass

    diagnostics["repo_slug"] = repo_slug or "n/a"
    diagnostics["repo_name"] = repo_name or "n/a"
    if hosted_mode and not repo_slug:
        diagnostics["project_group_id"] = "unresolved"
        diagnostics["t1_database"] = "unresolved_missing_x_repo"
        diagnostics["t2_database"] = "unresolved_missing_x_repo"
        diagnostics.setdefault(
            "scope_error",
            (
                "Hosted request has no X-Repo header; refusing to derive a "
                "fallback graph name."
            ),
        )
    else:
        diagnostics["project_group_id"] = derive_project_group_id(
            repo_slug=repo_slug,
            code_repo_name=repo_name,
        )
        diagnostics["t1_database"] = derive_t1_database_name(
            repo_slug=repo_slug,
            code_repo_name=repo_name,
        )
        diagnostics["t2_database"] = derive_t2_database_name(
            repo_slug=repo_slug,
            code_repo_name=repo_name,
        )
    # Plan-state indicator. PR #654 in-PR review round 9 (LOW): the prior
    # form was hardcoded to ``"phase_1_to_5_pre_split"`` regardless of
    # runtime state, so operators triaging production issues would always
    # see stale phase info after Phase 5/6/8 landed. Derive it from the
    # runtime signals we actually observe:
    #
    #   - T1 remote callback registered AND T2 handoff active
    #     → "phase_8_hybrid_t1_hosted"
    #   - T2 handoff active (but no T1 remote callback)
    #     → "phase_5_hybrid_t2_handoff"
    #   - neither handoff active → "phase_1_local_only"
    #
    # Phase 6 is a migration-time change (physical DB rename), not
    # runtime-observable without a live FalkorDB probe. Callers that
    # need the split-naming state should query the hosted admin
    # surface instead.
    try:
        from watercooler.baseline_graph.sync import (
            _t1_remote_upsert_enabled,
            is_hybrid_t2_handoff_active,
        )
        t1_hybrid = _t1_remote_upsert_enabled()
        t2_handoff = is_hybrid_t2_handoff_active()
    except Exception:
        t1_hybrid = False
        t2_handoff = False

    if t1_hybrid and t2_handoff:
        phase = "phase_8_hybrid_t1_hosted"
    elif t2_handoff:
        phase = "phase_5_hybrid_t2_handoff"
    else:
        phase = "phase_1_local_only"
    diagnostics["phase_indicator"] = phase


def _diagnose_memory_hosted_impl(ctx: Context, code_path: str = "") -> ToolResult:
    """Memory diagnostics for hosted mode — skips filesystem checks."""
    import sys

    diagnostics: dict[str, Any] = {
        "mode": "hosted",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "threads_source": "GitHub API (hosted)",
    }
    _add_canonical_identity_fields(diagnostics, code_path=code_path)

    # Check watercooler_memory import
    try:
        import watercooler_memory
        diagnostics["watercooler_memory_path"] = watercooler_memory.__file__
        diagnostics["watercooler_memory_version"] = getattr(
            watercooler_memory, "__version__", "unknown"
        )
    except ImportError as e:
        diagnostics["watercooler_memory_import"] = f"Failed: {e}"

    # T2 (Graphiti) config check
    try:
        from .. import memory as mem
        config = mem.load_graphiti_config(code_path=code_path if code_path else None)
        diagnostics["graphiti_enabled"] = config is not None
        if config:
            diagnostics["falkordb_host"] = config.falkordb_host or "localhost"
            diagnostics["falkordb_port"] = (
                config.falkordb_port if config.falkordb_port is not None else 6379
            )
            diagnostics["graph_name"] = config.database or "watercooler"
            diagnostics["llm_api_key_set"] = bool(config.llm_api_key)
            diagnostics["embedding_api_key_set"] = bool(config.embedding_api_key)

            # Try backend init
            backend = mem.get_graphiti_backend(config)
            if isinstance(backend, dict):
                diagnostics["graphiti_backend_init"] = f"Failed: {backend.get('error', 'unknown')}"
            elif backend is None:
                diagnostics["graphiti_backend_init"] = "Failed: Returned None"
            else:
                diagnostics["graphiti_backend_init"] = "Success"
    except Exception as e:
        diagnostics["graphiti_error"] = str(e)

    # T3 (LeanRAG) config check
    t3: dict[str, Any] = {}
    try:
        from .. import memory as mem
        leanrag_cfg = mem.load_leanrag_config(code_path=code_path if code_path else None)
        if leanrag_cfg is None:
            t3["leanrag_enabled"] = False
        else:
            t3["leanrag_enabled"] = True
            t3["falkordb_host"] = leanrag_cfg.falkordb_host or "localhost"
            t3["falkordb_port"] = (
                leanrag_cfg.falkordb_port if leanrag_cfg.falkordb_port is not None else 6379
            )
            t3["llm_api_key_set"] = bool(leanrag_cfg.llm_api_key)
    except Exception as e:
        t3["error"] = str(e)

    diagnostics["t3_leanrag"] = t3

    return ToolResult(content=[TextContent(
        type="text",
        text=json.dumps(diagnostics, indent=2),
    )])


def _diagnose_memory_impl(ctx: Context, code_path: str = "") -> ToolResult:
    """Diagnose memory backend installation and configuration across all tiers.

    Returns diagnostic information about package paths, imports, and configuration
    for T2 (Graphiti) and T3 (LeanRAG). Useful for debugging backend initialization
    issues and misconfiguration.

    Args:
        ctx: MCP context
        code_path: Path to code repository (for resolving database name)

    Returns:
        JSON with diagnostic information including:
        - Python version and executable path
        - watercooler_memory package path and version
        - T2 (Graphiti): GraphitiBackend import status, config, backend_init
        - T3 (LeanRAG): nested under ``t3_leanrag`` — LeanRAGBackend import,
          env vars, path resolution, FalkorDB/LLM/embedding config, backend_init,
          has_incremental_state

    Example:
        diagnose_memory(code_path="/path/to/project")
    """
    # Hosted mode guard — skip filesystem checks
    try:
        from ..auth import is_hosted_mode
    except ImportError as _hosted_err:
        log_error(
            f"MEMORY: failed to import is_hosted_mode: "
            f"{_hosted_err.__class__.__name__}: {_hosted_err}; cannot "
            f"determine whether hosted diagnostics are required."
        )
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(
                {
                    "error": "Hosted mode detection unavailable",
                    "message": (
                        "Failed to import hosted-mode auth helpers while "
                        f"diagnosing memory configuration: {_hosted_err}"
                    ),
                },
                indent=2,
            ),
        )])
    except Exception as _hosted_err:
        log_error(
            f"MEMORY: failed to import is_hosted_mode: "
            f"{_hosted_err.__class__.__name__}: {_hosted_err}; cannot "
            f"determine whether hosted diagnostics are required."
        )
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(
                {
                    "error": "Hosted mode detection unavailable",
                    "message": (
                        "Failed to import hosted-mode auth helpers while "
                        f"diagnosing memory configuration: {_hosted_err}"
                    ),
                },
                indent=2,
            ),
        )])
    else:
        try:
            hosted_mode = is_hosted_mode()
        except Exception as _hosted_err:
            log_error(
                f"MEMORY: is_hosted_mode() raised "
                f"{_hosted_err.__class__.__name__}: {_hosted_err}; using hosted "
                f"diagnostic path so graph-name derivation fails closed."
            )
            hosted_mode = True
    if hosted_mode:
        return _diagnose_memory_hosted_impl(ctx, code_path)

    try:
        # Import memory module (lazy-load)
        try:
            from .. import memory as mem
        except ImportError as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Memory module unavailable",
                        "message": f"Memory module not available in the open-core edition. Details: {e}",
                    },
                    indent=2,
                )
            )])

        import sys
        diagnostics = {
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "python_executable": sys.executable,
        }
        _add_canonical_identity_fields(diagnostics, code_path=code_path)

        # Check watercooler_memory import and path
        try:
            import watercooler_memory
            diagnostics["watercooler_memory_path"] = watercooler_memory.__file__
            diagnostics["watercooler_memory_version"] = getattr(
                watercooler_memory, "__version__", "unknown"
            )
        except ImportError as e:
            diagnostics["watercooler_memory_import"] = f"✗ Failed: {e}"

        # Check GraphitiBackend import
        try:
            from watercooler_memory.backends import GraphitiBackend
            diagnostics["graphiti_backend_import"] = "✓ Success"
            diagnostics["graphiti_backend_in_all"] = "GraphitiBackend" in getattr(
                __import__("watercooler_memory.backends"), "__all__", []
            )
        except ImportError as e:
            diagnostics["graphiti_backend_import"] = f"✗ Failed: {e}"

        # Check config
        config = mem.load_graphiti_config(code_path=code_path if code_path else None)
        diagnostics["graphiti_enabled"] = config is not None
        if config:
            # Check LLM API key
            llm_key = config.llm_api_key
            diagnostics["llm_api_key_set"] = bool(llm_key)
            diagnostics["llm_api_base"] = config.llm_api_base or "https://api.openai.com/v1 (default)"
            diagnostics["llm_model"] = config.llm_model or "gpt-4o-mini (default)"
            # Validate key format (basic check - not a full auth test)
            if llm_key:
                if llm_key.startswith("sk-"):
                    diagnostics["llm_api_key_format"] = "valid (sk-...)"
                else:
                    diagnostics["llm_api_key_format"] = f"unusual format: {llm_key[:10]}..."
            # FalkorDB connection (T2 uses FalkorDB just like T3)
            # "localhost"/6379 are the actual FalkorDB defaults, not "(not set)" sentinels
            diagnostics["falkordb_host"] = config.falkordb_host or "localhost"
            diagnostics["falkordb_port"] = (
                config.falkordb_port if config.falkordb_port is not None else 6379
            )
            # config attribute is "database"; surfaced as "graph_name" for parity with T3
            diagnostics["graph_name"] = config.database or "watercooler"
            # Embedding config (T2 uses embeddings for entity extraction)
            diagnostics["embedding_model"] = config.embedding_model or "(not set)"
            diagnostics["embedding_api_base"] = config.embedding_api_base or "(not set)"
            diagnostics["embedding_api_key_set"] = bool(config.embedding_api_key)
        else:
            diagnostics["config_issue"] = (
                "Graphiti not enabled. Either set WATERCOOLER_GRAPHITI_ENABLED=1, "
                "or configure [memory] backend = 'graphiti' in config.toml. "
                "Also ensure API keys are configured via LLM_API_KEY / EMBEDDING_API_KEY "
                "env vars or ~/.watercooler/credentials.toml (see credentials.example.toml)."
            )

        # Check backend initialization
        if config:
            backend = mem.get_graphiti_backend(config)
            if isinstance(backend, dict):
                diagnostics["backend_init"] = f"✗ Failed: {backend.get('error', 'unknown')}"
                diagnostics["backend_error_details"] = backend
            elif backend is None:
                diagnostics["backend_init"] = "✗ Failed: Returned None"
            else:
                diagnostics["backend_init"] = "✓ Success"

        # Check T3 (LeanRAG) configuration and availability
        t3: dict[str, Any] = {}

        # Import LeanRAGBackend once and cache the class so we can instantiate
        # it directly below without a redundant load_leanrag_config() call.
        _LeanRAGBackend_cls = None
        try:
            from watercooler_memory.backends.leanrag import LeanRAGBackend as _LeanRAGBackend_cls
            t3["leanrag_backend_import"] = "✓ Success"
        except ImportError as _e:
            t3["leanrag_backend_import"] = f"✗ Failed: {_e}"

        t3["leanrag_path_env"] = os.getenv("LEANRAG_PATH") or "(not set)"
        t3["leanrag_enabled_env"] = os.getenv("WATERCOOLER_LEANRAG_ENABLED") or "(not set)"
        t3["leanrag_database_env"] = os.getenv("WATERCOOLER_LEANRAG_DATABASE") or "(not set)"

        try:
            leanrag_cfg = mem.load_leanrag_config(code_path=code_path if code_path else None)
            if leanrag_cfg is None:
                t3["leanrag_enabled"] = False
                t3["config_issue"] = (
                    "LeanRAG not enabled. Set WATERCOOLER_LEANRAG_ENABLED=1 and LEANRAG_PATH, "
                    "or configure [memory.tiers] t3_enabled = true in config.toml."
                )
            else:
                t3["leanrag_enabled"] = True
                t3["leanrag_path"] = str(leanrag_cfg.leanrag_path) if leanrag_cfg.leanrag_path else "(none)"
                t3["leanrag_path_exists"] = (
                    Path(leanrag_cfg.leanrag_path).exists() if leanrag_cfg.leanrag_path else False
                )
                t3["work_dir"] = str(leanrag_cfg.work_dir) if leanrag_cfg.work_dir else "(none)"
                t3["graph_name"] = leanrag_cfg.work_dir.name if leanrag_cfg.work_dir else "(not set)"
                t3["falkordb_host"] = leanrag_cfg.falkordb_host or "localhost"
                t3["falkordb_port"] = (
                    leanrag_cfg.falkordb_port if leanrag_cfg.falkordb_port is not None else 6379
                )
                t3["llm_api_key_set"] = bool(leanrag_cfg.llm_api_key)
                t3["llm_api_base"] = leanrag_cfg.llm_api_base or "(not set)"
                t3["llm_model"] = leanrag_cfg.llm_model or "(not set)"
                t3["embedding_api_base"] = leanrag_cfg.embedding_api_base or "(not set)"
                t3["embedding_model"] = leanrag_cfg.embedding_model or "(not set)"

                if _LeanRAGBackend_cls is not None:
                    try:
                        leanrag_backend = _LeanRAGBackend_cls(leanrag_cfg)
                        t3["backend_init"] = "✓ Success"
                        try:
                            t3["has_incremental_state"] = leanrag_backend.has_incremental_state()
                        except Exception as _he:
                            t3["has_incremental_state"] = f"✗ Failed: {_he}"
                    except Exception as _be:
                        t3["backend_init"] = f"✗ Failed: {_be}"
                else:
                    t3["backend_init"] = "✗ Skipped: LeanRAGBackend import failed"
        except Exception as _e:
            t3["config_error"] = f"load_leanrag_config: {_e}"

        diagnostics["t3_leanrag"] = t3

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(diagnostics, indent=2)
        )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in diagnose_memory: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps(
                {
                    "error": "Diagnostic failed",
                    "message": str(e),
                },
                indent=2,
            )
        )])


async def _graphiti_add_episode_impl(
    content: str,
    group_id: str,
    ctx: Context,
    code_path: str = "",
    entry_id: str = "",
    timestamp: str = "",
    title: str = "",
    source_description: str = "",
    previous_episode_uuids: list[str] | None = None,
) -> ToolResult:
    """Add an episode directly to Graphiti temporal graph.

    This tool allows direct ingestion of content as a Graphiti episode,
    bypassing the normal thread-based workflow. Useful for:
    - Importing external knowledge
    - Adding custom context to the graph
    - Testing and development

    Args:
        content: The episode content/body text (required)
        group_id: Project group_id for graph partitioning (required). In the unified
            model, all threads in a project share the same group_id (e.g., "watercooler_cloud").
            Use the project database name, not individual thread topics.
        code_path: Path to code repository (for database name derivation)
        entry_id: Optional watercooler entry ID for provenance tracking
        timestamp: Optional ISO 8601 timestamp (defaults to now)
        title: Optional episode title (defaults to first 50 chars of content)
        source_description: Optional source metadata. Include thread topic here for traceability
            (e.g., "thread:auth-feature | Migration: Claude").
        previous_episode_uuids: Optional list of episode UUIDs this episode follows.
            Used for explicit temporal ordering when chunks share the same timestamp.

    Returns:
        JSON with episode_uuid, entities_extracted, and success status

    Example:
        graphiti_add_episode(
            content="We decided to use JWT tokens with RS256 signing",
            group_id="watercooler_cloud",
            entry_id="01ABC123",
            timestamp="2025-01-15T10:00:00Z",
            source_description="thread:auth-feature | Migration: Claude"
        )
    """
    try:
        # Validate required fields
        if not content or not content.strip():
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Content is required and cannot be empty",
                    "episode_uuid": None,
                }, indent=2)
            )])

        if not group_id or not group_id.strip():
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "group_id is required and cannot be empty",
                    "episode_uuid": None,
                }, indent=2)
            )])

        # Plan v20 defect #34 fix: canonicalize the caller-supplied
        # ``group_id`` to ``<org>_<repo>_t2`` before enqueueing. The
        # graphiti_executor in ``memory_sync.py`` uses ``task.group_id``
        # directly as the FalkorDB database name; canonicalizing here
        # ensures hosted writes land in the canonical T2 graph.
        canonical_group_id, scope_err = _canonicalize_t2_group_id(group_id)
        if scope_err is not None:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(scope_err, indent=2)
            )])
        group_id = canonical_group_id

        # Import memory module (lazy-load)
        try:
            from .. import memory as mem
        except ImportError as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Memory module unavailable: {e}",
                    "episode_uuid": None,
                }, indent=2)
            )])

        # Load configuration
        config = mem.load_graphiti_config(code_path=code_path)
        if config is None:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Graphiti not enabled. Set WATERCOOLER_GRAPHITI_ENABLED=1",
                    "episode_uuid": None,
                }, indent=2)
            )])

        # Plan v20 defect #34 fix (PR #660 review): align ``config.database``
        # with the canonical ``group_id`` resolved above so the fire-and-forget
        # fallback path lands in the same physical FalkorDB database the
        # queue path would have written to. Without this, the fallback's
        # ``backend.add_episode_direct`` would have used ``config.database``
        # (derived from ``code_path``) while passing the canonical
        # ``group_id`` as partition key — splitting writes across two
        # physical databases for the same logical call.
        # ``GraphitiConfig`` is a non-frozen dataclass and ``load_graphiti_config``
        # returns a fresh instance per call, so direct assignment is safe and
        # also works in tests that pass a ``MagicMock`` config.
        try:
            config.database = group_id
        except (AttributeError, TypeError) as _set_err:
            # Fail closed: under hosted multi-tenancy, falling through with
            # the original ``config.database`` would write the caller's
            # data to a different physical database than intended, with
            # no visibility. Surface a structured error to the caller and
            # log loudly rather than silently miswriting.
            log_error(
                f"MEMORY: cannot align config.database with canonical group_id "
                f"{group_id!r} (set raised {_set_err.__class__.__name__}: "
                f"{_set_err}); refusing to write to a non-canonical database."
            )
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": (
                        "graphiti_config_immutable: cannot route write to canonical "
                        f"database {group_id!r}. Refusing to fall back to a "
                        "non-canonical name (Plan v20 multi-tenant guard)."
                    ),
                    "episode_uuid": None,
                }, indent=2)
            )])

        # Get backend instance
        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            error_msg = "Graphiti backend unavailable"
            if isinstance(backend, dict):
                error_msg = backend.get("message", error_msg)
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": error_msg,
                    "episode_uuid": None,
                }, indent=2)
            )])

        # Prepare episode data
        from datetime import datetime, timezone

        if timestamp:
            try:
                ref_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            except ValueError:
                ref_time = datetime.now(timezone.utc)
        else:
            ref_time = datetime.now(timezone.utc)

        episode_title = title if title else content[:50] + ("..." if len(content) > 50 else "")
        source_desc = source_description if source_description else "Direct episode via MCP tool"

        # Pre-flight dedup: skip if this entry is already indexed.
        # Protects against agent retries creating duplicate episodes.
        if entry_id and backend.entry_episode_index is not None:
            if backend.entry_episode_index.has_any_mapping(entry_id):
                logger.debug("MEMORY: Skipping already-indexed entry %s (direct tool)", entry_id)
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "success": True,
                        "status": "skipped",
                        "skip_reason": "already_indexed",
                        "entry_id": entry_id,
                        "message": "Entry already indexed; skipping to prevent duplicate episodes.",
                    }, indent=2)
                )])

        # Plan v20 Phase 4: durable hosted ingest. When the memory queue is
        # initialised, enqueue so the worker owns the slow Graphiti pipeline
        # (DeepSeek LLM calls + FalkorDB writes, 60–120s) and a terminal
        # receipt is persisted. Callers poll via
        # watercooler_memory_task_status using the returned task_id as
        # their ``remote_task_id``.
        try:
            from ..memory_queue import enqueue_memory_task, get_queue
            queue_available = get_queue() is not None
        except Exception:
            queue_available = False
            enqueue_memory_task = None  # type: ignore[assignment]

        if queue_available and enqueue_memory_task is not None:
            try:
                task_id = enqueue_memory_task(
                    entry_id=entry_id or "",
                    topic="",
                    group_id=group_id,
                    content=content,
                    title=episode_title,
                    timestamp=timestamp,
                    source_description=source_desc,
                    backend="graphiti",
                    code_path=code_path,
                )
            except Exception as e:
                log_error(f"MEMORY: enqueue failed, falling back to fire-and-forget: {e}")
                task_id = None

            if task_id:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "success": True,
                        "status": "queued",
                        "task_id": task_id,
                        "remote_task_id": task_id,
                        "group_id": group_id,
                        "entry_id": entry_id if entry_id else None,
                        "message": (
                            "Episode enqueued for durable processing. Poll "
                            "watercooler_memory_task_status(task_id=...) for "
                            "terminal receipt."
                        ),
                    }, indent=2)
                )])

        # Fire-and-forget fallback: used when the queue is unavailable
        # (e.g., stdio-only deployments without a worker). Matches the
        # original middleware memory-sync pattern.
        async def _do_add_episode():
            try:
                result = await backend.add_episode_direct(
                    name=episode_title,
                    episode_body=content,
                    source_description=source_desc,
                    reference_time=ref_time,
                    group_id=group_id,
                    previous_episode_uuids=previous_episode_uuids,
                )

                episode_uuid = result.get("episode_uuid", "unknown")

                # Track entry-episode mapping if entry_id provided
                if entry_id and episode_uuid != "unknown":
                    backend.index_entry_as_episode(entry_id, episode_uuid, group_id)

                log_action(
                    f"MEMORY: Background episode added {episode_uuid} "
                    f"to group {group_id} "
                    f"(entities={len(result.get('entities_extracted', []))}, "
                    f"facts={result.get('facts_extracted', 0)})"
                )
            except Exception as e:
                log_error(f"MEMORY: Background add_episode failed: {e}")

        asyncio.create_task(_do_add_episode())

        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "success": True,
                "status": "submitted",
                "group_id": group_id,
                "entry_id": entry_id if entry_id else None,
                "message": "Episode submitted for background processing",
            }, indent=2)
        )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in graphiti_add_episode: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Unexpected error: {e}",
                "episode_uuid": None,
            }, indent=2)
        )])


async def _clear_graph_group_impl(
    group_id: str,
    ctx: Context,
    code_path: str = "",
    confirm: bool = False,
) -> ToolResult:
    """Clear all episodes for a specific project group_id.

    This is a destructive operation that removes all Graphiti episodes
    belonging to the specified group. Use for cleanup/testing purposes.

    IMPORTANT: In the unified model, all threads in a project share one group_id
    (e.g., "watercooler_cloud"). Clearing this group will remove ALL episodes
    from ALL threads in the project.

    IMPORTANT: This operation cannot be undone. Data will be permanently deleted.

    Note: Entity nodes and edges created from these episodes may still remain
    in the graph (Graphiti doesn't cascade delete). Only Episodic nodes are removed.

    Prerequisites:
        1. Graphiti backend enabled: WATERCOOLER_GRAPHITI_ENABLED=1
        2. FalkorDB running: localhost:6379 (or configured host/port)

    Args:
        group_id: Project group_id to clear episodes for (required). In the unified
            model, this is the project database name (e.g., "watercooler_cloud"),
            not individual thread topics.
        ctx: MCP context
        code_path: Path to code repository (for database name derivation)
        confirm: Must be True to execute deletion (safety check)

    Returns:
        JSON with operation results:
        - success: True if episodes were cleared
        - removed: Number of episodes deleted
        - group_id: The sanitized group ID used
        - message: Human-readable status message

    Example Response:
        {
          "success": true,
          "removed": 15,
          "group_id": "cursor_greeting",
          "message": "Removed 15 episodes"
        }

    Safety:
        Set confirm=True to actually execute deletion.
        Without confirm=True, returns error message explaining requirement.
    """
    try:
        # Import memory module (lazy-load)
        try:
            from .. import memory as mem
        except ImportError as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Memory module unavailable: {e}",
                    "removed": 0,
                }, indent=2)
            )])

        # Validate group_id
        if not group_id or not group_id.strip():
            return mem.create_error_response(
                "Invalid group_id",
                "group_id parameter is required and must be non-empty",
                "clear_graph_group",
                removed=0,
            )

        # Safety check - require explicit confirmation
        if not confirm:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Confirmation required",
                    "message": (
                        f"This will permanently delete all episodes for group '{group_id}'. "
                        "Set confirm=True to proceed. This operation cannot be undone."
                    ),
                    "group_id": group_id,
                    "removed": 0,
                }, indent=2)
            )])

        # Load configuration and backend
        config = mem.load_graphiti_config(code_path=code_path)
        if config is None:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Graphiti not enabled. Set WATERCOOLER_GRAPHITI_ENABLED=1",
                    "removed": 0,
                }, indent=2)
            )])

        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            error_msg = "Graphiti backend unavailable"
            if isinstance(backend, dict):
                error_msg = backend.get("message", error_msg)
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": error_msg,
                    "removed": 0,
                }, indent=2)
            )])

        # Execute cleanup
        log_action("memory.clear_graph_group", group_id=group_id, confirm=confirm)

        try:
            result = backend.clear_group_episodes(group_id=group_id)

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "removed": result.get("removed", 0),
                    "group_id": result.get("group_id", group_id),
                    "message": result.get("message", "Episodes cleared"),
                }, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: Failed to clear episodes: {e}")
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to clear episodes: {e}",
                    "removed": 0,
                }, indent=2)
            )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in clear_graph_group: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Unexpected error: {e}",
                "removed": 0,
            }, indent=2)
        )])


def _get_leanrag_backend(code_path: str = "") -> Any:
    """Get LeanRAG backend instance via unified config factory.

    Args:
        code_path: Path to the project directory (for database name derivation).

    Returns:
        LeanRAGBackend instance or None if unavailable/disabled.
    """
    try:
        from ..memory import load_leanrag_config
        from watercooler_memory.backends.leanrag import LeanRAGBackend

        config = load_leanrag_config(code_path=code_path)
        if config is None:
            log_warning("MEMORY: LeanRAG config unavailable (disabled or misconfigured)")
            return None

        return LeanRAGBackend(config)
    except ImportError as e:
        log_error(f"MEMORY: LeanRAG backend import failed: {e}")
        return None
    except Exception as e:
        log_error(f"MEMORY: LeanRAG backend init failed: {e}")
        return None


async def _leanrag_run_pipeline_impl(
    group_id: str = "",
    ctx: Context | None = None,  # noqa: ARG001 — reserved for FastMCP context injection
    code_path: str = "",
    start_date: str = "",
    end_date: str = "",
    dry_run: bool = False,
    incremental: bool = True,
) -> ToolResult:
    """Run LeanRAG clustering pipeline on Graphiti episodes.

    Processes episodes from a thread group through the LeanRAG pipeline:
    1. Extract chunks from Graphiti episodes
    2. Generate embeddings (BGE-M3, 1024-d)
    3. Cluster semantically similar content
    4. Store cluster summaries back to graph

    Args:
        group_id: Thread/topic identifier to process. Optional if code_path
            is provided (group_id will be derived from code_path).
        ctx: MCP context
        code_path: Path to the project directory (required for BULK runs).
            Used to derive group_id and project-scoped LeanRAG/Graphiti config.
        start_date: Optional start date filter (ISO 8601)
        end_date: Optional end date filter (ISO 8601)
        dry_run: If True, only report what would be done
        incremental: If True (default), use incremental update when saved
            cluster state exists. If False, force a full rebuild.

    Returns:
        JSON with clusters_created, chunks_processed, and execution stats

    Example:
        leanrag_run_pipeline(
            code_path="/home/user/my-project",
            start_date="2025-01-01",
            dry_run=True
        )
    """
    import asyncio
    import time

    start_time = time.time()

    try:
        # Require code_path for all LeanRAG BULK runs (direct + queued).
        # group_id is optional — derived from code_path if absent.
        if not code_path or not code_path.strip():
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "code_path is required for LeanRAG pipeline. "
                             "Provide the path to the project directory.",
                    "clusters_created": 0,
                }, indent=2)
            )])

        if not group_id or not group_id.strip():
            from watercooler.path_resolver import derive_group_id
            group_id = derive_group_id(code_path=code_path)
            if group_id == "watercooler" and code_path:
                logger.warning(
                    "group_id defaulted to 'watercooler' from code_path=%s; "
                    "verify code_path is in a git repo",
                    code_path,
                )

        # Get backend instance
        backend = _get_leanrag_backend(code_path=code_path)
        if backend is None:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "LeanRAG backend unavailable. This feature is not available in the open-core edition.",
                    "clusters_created": 0,
                }, indent=2)
            )])

        # For dry_run, just report what would be done
        if dry_run:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "group_id": group_id,
                    "dry_run": True,
                    "clusters_created": 0,
                    "chunks_processed": 0,
                    "execution_time_ms": int((time.time() - start_time) * 1000),
                    "message": f"Dry run: Would process episodes from group '{group_id}'",
                }, indent=2)
            )])

        # Try queue-first: enqueue BULK task for async processing
        try:
            from ..memory_queue import get_queue, get_worker, MemoryTask, TaskType, DuplicateTaskError

            queue = get_queue()
            worker = get_worker()
            if queue is not None and worker is not None and worker.has_executor("leanrag_pipeline"):
                content_payload = json.dumps({
                    "start_date": start_date,
                    "end_date": end_date,
                    "incremental": incremental,
                })
                task = MemoryTask(
                    task_type=TaskType.BULK,
                    backend="leanrag_pipeline",
                    group_id=group_id,
                    topic=group_id,
                    content=content_payload,
                    title=f"leanrag_pipeline:{group_id}",
                    source_description=f"leanrag_run_pipeline|{group_id}",
                    code_path=code_path,
                )
                try:
                    task_id = queue.enqueue(task)
                except DuplicateTaskError:
                    return ToolResult(content=[TextContent(
                        type="text",
                        text=json.dumps({
                            "success": True,
                            "group_id": group_id,
                            "queued": True,
                            "message": f"Pipeline already queued for group '{group_id}'",
                        }, indent=2)
                    )])
                worker.wake()
                log_action(f"MEMORY: LeanRAG pipeline queued for {group_id} (task_id={task_id})")
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "success": True,
                        "group_id": group_id,
                        "queued": True,
                        "task_id": task_id,
                        "message": f"Pipeline queued for async processing. Poll with watercooler_memory_task_status.",
                    }, indent=2)
                )])
        except ImportError:
            pass  # Fall through to direct execution

        # Fallback: direct execution (queue unavailable)
        try:
            from ..memory import load_graphiti_config
            from watercooler_memory.backends.graphiti import GraphitiBackend

            graphiti_config = load_graphiti_config(code_path=code_path)
            if graphiti_config is None:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "success": False,
                        "error": "Graphiti config unavailable — required for episode retrieval",
                        "clusters_created": 0,
                    }, indent=2)
                )])
            graphiti = GraphitiBackend(graphiti_config)
            episodes = await asyncio.to_thread(
                graphiti.get_group_episodes,
                group_id=group_id,
                start_time=start_date,
                end_time=end_date,
            )
        except ImportError:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": "Graphiti backend required to fetch episodes. Enable Graphiti first.",
                    "clusters_created": 0,
                }, indent=2)
            )])
        except Exception as e:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Failed to fetch episodes from Graphiti: {e}",
                    "clusters_created": 0,
                }, indent=2)
            )])

        if not episodes:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "group_id": group_id,
                    "dry_run": False,
                    "clusters_created": 0,
                    "chunks_processed": 0,
                    "execution_time_ms": int((time.time() - start_time) * 1000),
                    "message": f"No episodes found for group '{group_id}'",
                }, indent=2)
            )])

        # Convert episodes to ChunkPayload format
        from ..memory_sync import episodes_to_chunk_payload

        chunk_payload = episodes_to_chunk_payload(episodes, group_id)

        # Run LeanRAG index via thread (ADR 0001 Sync Facade pattern)
        try:
            if incremental and backend.has_incremental_state():
                result = await asyncio.to_thread(backend.incremental_index, chunk_payload)
            else:
                result = await asyncio.to_thread(backend.index, chunk_payload)

            execution_time_ms = int((time.time() - start_time) * 1000)
            log_action(f"MEMORY: LeanRAG pipeline completed for {group_id}: {len(chunk_payload.chunks)} chunks")

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": True,
                    "group_id": group_id,
                    "dry_run": False,
                    "clusters_created": result.indexed_count,
                    "chunks_processed": len(chunk_payload.chunks),
                    "execution_time_ms": execution_time_ms,
                    "message": result.message,
                }, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: LeanRAG pipeline failed: {e}")
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "success": False,
                    "error": f"Pipeline failed: {e}",
                    "clusters_created": 0,
                }, indent=2)
            )])

    except Exception as e:
        log_error(f"MEMORY: Unexpected error in leanrag_run_pipeline: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Unexpected error: {e}",
                "clusters_created": 0,
            }, indent=2)
        )])


def _apply_decision_boost_evidence(
    evidence: list[dict[str, Any]],
    boost: float,
) -> bool:
    """Multiply score on Decision evidence items and re-sort in place.

    Returns True if any item was boosted. Only affects T1 items (the only
    tier whose evidence carries ``metadata.entry_type``); T2 facts/entities
    and T3 summaries don't expose entry_type reliably and are left untouched.
    Delegates to :func:`boost_decision_items` so the ranking behaviour stays
    in sync with ``watercooler_search``.
    """
    return boost_decision_items(evidence, boost) > 0


def _resolve_t2_provenance(
    result: Any,
    code_path_resolved: Path | None,
) -> dict[str, int]:
    """Apply 3-hop T2 backtrace, populating ``thread_entry_id`` on T2 evidence.

    Mutates each ``result.evidence[i].provenance`` dict in place. Returns a
    stats dict with ``attempted/succeeded/failed/not_applicable`` counts.
    Shared between hosted and non-hosted ``smart_query`` paths.
    """
    from watercooler_memory.tier_strategy import Tier

    not_applicable_count = 0
    for e in result.evidence:
        if e.tier == Tier.T2 and not e.provenance.get("edge_uuid"):
            e.provenance["provenance_resolution_status"] = "not_applicable"
            not_applicable_count += 1

    t2_evidence = [
        e for e in result.evidence
        if e.tier == Tier.T2 and e.provenance.get("edge_uuid")
    ]
    prov_stats = {
        "attempted": len(t2_evidence),
        "succeeded": 0,
        "failed": 0,
        "not_applicable": not_applicable_count,
    }

    if not t2_evidence:
        return prov_stats

    try:
        from ..memory import load_graphiti_config
        from watercooler_memory.entry_episode_index import IndexConfig

        graphiti_config = load_graphiti_config(code_path=code_path_resolved)
        if graphiti_config is None:
            for ev in t2_evidence:
                ev.provenance["provenance_resolution_status"] = "config_unavailable"
            prov_stats["failed"] = len(t2_evidence)
            return prov_stats

        index_path = (
            graphiti_config.entry_episode_index_path
            or IndexConfig(backend="graphiti").index_path
        )
        prov_index = _get_cached_provenance_index(index_path)
        for ev in t2_evidence:
            episodes = ev.provenance.get("episodes") or []
            if not episodes:
                ev.provenance["provenance_resolution_status"] = "no_episodes"
                prov_stats["failed"] += 1
                continue
            ep_uuid = episodes[0]
            try:
                entry_id = prov_index.get_entry(ep_uuid)
                if entry_id:
                    ev.provenance["thread_entry_id"] = entry_id
                    ev.provenance["episode_uuid"] = ep_uuid
                    ev.provenance["provenance_resolution_status"] = "resolved"
                    prov_stats["succeeded"] += 1
                else:
                    ev.provenance["provenance_resolution_status"] = "index_miss"
                    prov_stats["failed"] += 1
            except Exception as lookup_exc:
                ev.provenance["provenance_resolution_status"] = "lookup_error"
                prov_stats["failed"] += 1
                logger.debug(
                    "resolve_provenance: index lookup failed for %s: %s",
                    ep_uuid,
                    lookup_exc,
                )
    except Exception as setup_exc:
        logger.debug("resolve_provenance: setup failed: %s", setup_exc)
        for ev in t2_evidence:
            if "provenance_resolution_status" not in ev.provenance:
                ev.provenance["provenance_resolution_status"] = "setup_error"
        prov_stats["failed"] = len(t2_evidence) - prov_stats["succeeded"]

    return prov_stats


def _build_smart_query_response(
    result: Any,
    available: list[Any],
    prov_stats: dict[str, int] | None,
    prioritize_decisions: bool,
    decision_boost: float,
) -> dict:
    """Format ``TierResult`` into the smart_query JSON response shape.

    Shared between hosted and non-hosted paths. Applies the decision boost
    in place on the response evidence list.
    """
    response = result.to_dict()
    response["available_tiers"] = [t.value for t in available]
    if prov_stats is not None:
        response["provenance_resolution_stats"] = prov_stats

    safe_boost = sanitize_boost(decision_boost)
    if prioritize_decisions and _apply_decision_boost_evidence(
        response.get("evidence", []), safe_boost
    ):
        response["decisions_prioritized"] = True
        response["decision_boost"] = safe_boost
    return response


async def _smart_query_impl(
    query: str,
    ctx: Context,
    code_path: str = "",
    threads_dir: str = "",
    max_tiers: int = 2,
    force_tier: str | None = None,
    group_ids: list[str] | None = None,
    resolve_provenance: bool = False,
    prioritize_decisions: bool = True,
    decision_boost: float = 1.5,
) -> ToolResult:
    """Auto-escalating multi-tier query for open-ended natural-language questions
    (T1->T2->T3). Use when you don't know which tier has the answer.
    For structured filtered searches, use watercooler_search instead.

    Queries memory across three tiers with automatic escalation when lower tiers
    don't provide sufficient results:

    - **T1 (Baseline)**: JSONL graph with keyword/semantic search (cheapest, no LLM)
    - **T2 (Graphiti)**: FalkorDB temporal graph with hybrid search (medium cost)
    - **T3 (LeanRAG)**: Hierarchical clustering with multi-hop reasoning (expensive)

    The orchestrator follows the principle: "Always choose the cheapest tier that
    can satisfy the query intent." Escalation happens automatically when results
    are insufficient (fewer than min_results or low confidence).

    Prerequisites:
        - T1: threads_dir must exist with per-thread graph data (threads/ or search-index.jsonl)
        - T2: WATERCOOLER_GRAPHITI_ENABLED=1 + FalkorDB running
        - T3: LEANRAG_PATH set + WATERCOOLER_TIER_T3_ENABLED=1

    Environment Variables:
        WATERCOOLER_TIER_T1_ENABLED: "1" to enable T1 (default: "1")
        WATERCOOLER_TIER_T2_ENABLED: "1" to enable T2 (requires Graphiti)
        WATERCOOLER_TIER_T3_ENABLED: "1" to enable T3 (expensive, opt-in)
        WATERCOOLER_TIER_MAX_TIERS: Maximum tiers to query (default: "2")
        WATERCOOLER_TIER_MIN_RESULTS: Min results for sufficiency (default: "3")

    Args:
        query: Search query (e.g., "What authentication method was implemented?")
        code_path: Path to code repository (for T2/T3 database resolution)
        threads_dir: Path to threads directory (for T1 baseline graph). If empty,
            attempts to resolve from code_path.
        max_tiers: Maximum number of tiers to query (default: 2, max: 3)
        force_tier: Force query to specific tier ("T1", "T2", or "T3"). Disables
            escalation when set.
        group_ids: Optional list of project group_ids to filter results.
        resolve_provenance: If True, auto-follow the 3-hop backtrace for T2 evidence
            and populate provenance.thread_entry_id on each fact item. Call
            watercooler_get_thread_entry(entry_id=thread_entry_id) to fetch the full
            entry body. The response includes a provenance_resolution_stats field
            (attempted/succeeded/failed/not_applicable counts) and each T2 evidence
            item receives a provenance_resolution_status field ("resolved",
            "index_miss", "lookup_error", "no_episodes", "config_unavailable",
            "setup_error", or "not_applicable").

    Returns:
        JSON response with search results containing:
        - query: Original query text
        - result_count: Total evidence items found
        - tiers_queried: List of tiers that were queried (e.g., ["T1", "T2"])
        - primary_tier: The tier that provided best results
        - escalation_reason: Why escalation occurred (if applicable)
        - sufficient: Whether results met sufficiency criteria
        - evidence: List of evidence items from all tiers
        - message: Status message

    Note:
        smart_query returns evidence items (entry content, entity summaries) but
        does NOT expose temporal fact edges (valid_at/invalid_at). For questions
        about what changed, what was superseded, or what the state was before a
        certain date, use watercooler_search(mode="facts", active_only=False)
        instead — it returns structured triples with bi-temporal timestamps that
        show both current and superseded knowledge.

    Example:
        smart_query(
            query="What error handling patterns did we use?",
            code_path=".",
            max_tiers=2
        )

    Response Format:
        {
          "query": "What error handling patterns did we use?",
          "result_count": 5,
          "tiers_queried": ["T1", "T2"],
          "primary_tier": "T2",
          "escalation_reason": "Only 2 results (need 3)",
          "sufficient": true,
          "evidence": [
            {
              "tier": "T1",
              "id": "01ABC...",
              "content": "Implemented try-catch patterns...",
              "score": 0.85,
              "name": "Error Handling Discussion",
              "provenance": {...},
              "metadata": {...}
            },
            ...
          ],
          "message": "Found 5 results from T2"
        }
    """
    try:
        from pathlib import Path
        from watercooler_memory.tier_strategy import (
            TierOrchestrator,
            load_tier_config,
            Tier,
        )

        # Resolve paths — use the same _require_context() flow as
        # watercooler_search so that WATERCOOLER_CODE_PATH / WATERCOOLER_DIR
        # env-var fallbacks work when the caller omits code_path.
        code_path_resolved = Path(code_path) if code_path else None
        threads_dir_resolved = Path(threads_dir) if threads_dir else None

        if not threads_dir_resolved:
            error, context = validation._require_context(code_path)
            if error:
                log_error(f"MEMORY: smart_query context resolution failed: {error}")
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "query": query,
                        "result_count": 0,
                        "error": "Context resolution failed",
                        "message": error,
                        "available_tiers": [],
                    }, indent=2)
                )])

            if context and context.threads_dir:
                threads_dir_resolved = context.threads_dir
                if not code_path_resolved:
                    code_path_resolved = context.code_root
                log_action(
                    "memory.smart_query.resolved_context",
                    threads_dir=str(threads_dir_resolved),
                    code_root=str(code_path_resolved) if code_path_resolved else None,
                )

        # Convert force_tier string to Tier enum upfront so the hosted and
        # non-hosted paths share the same validation flow.
        force_tier_enum: Tier | None = None
        if force_tier:
            try:
                force_tier_enum = Tier(force_tier.upper())
            except ValueError:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "query": query,
                        "result_count": 0,
                        "error": f"Invalid tier: {force_tier}",
                        "valid_tiers": ["T1", "T2", "T3"],
                        "message": f"Invalid tier '{force_tier}'. Valid options: T1, T2, T3",
                    }, indent=2)
                )])

        # Hosted mode: try Graphiti T2/T3 first (canonical <org>_<repo>_t2
        # derived from the request's X-Repo header). Fall back to GitHub-API
        # T1 keyword search only when the orchestrator can't service the
        # request *and* the caller did not explicitly pin to T2/T3.
        if threads_dir_resolved == validation.HOSTED_MODE_SENTINEL:
            from ..hosted_ops import search_entries_hosted
            log_action("memory.smart_query.hosted_mode", query=query)

            # T1 in hosted mode means the GitHub keyword endpoint — skip
            # the orchestrator and fall straight through to the fallback.
            if force_tier_enum != Tier.T1:
                try:
                    config = load_tier_config(
                        threads_dir=None,  # disables orchestrator T1
                        code_path=code_path_resolved,
                    )
                    _cap_tiers_by_profile(config)
                    config.max_tiers = min(max(1, max_tiers), 3)
                    orchestrator = TierOrchestrator(config)
                    available = orchestrator.available_tiers

                    if force_tier_enum and force_tier_enum not in available:
                        return ToolResult(content=[TextContent(
                            type="text",
                            text=json.dumps({
                                "query": query,
                                "result_count": 0,
                                "error": f"Tier {force_tier} not available",
                                "available_tiers": [t.value for t in available],
                                "message": (
                                    f"Requested tier {force_tier} is not "
                                    "available in hosted mode"
                                ),
                            }, indent=2)
                        )])

                    if available:
                        log_action(
                            "memory.smart_query.hosted_mode.t2t3",
                            available_tiers=[t.value for t in available],
                            force_tier=force_tier,
                        )
                        result = await asyncio.to_thread(
                            orchestrator.query,
                            query,
                            group_ids=group_ids,
                            force_tier=force_tier_enum,
                        )

                        if result.evidence:
                            prov_stats = (
                                _resolve_t2_provenance(result, code_path_resolved)
                                if resolve_provenance
                                else None
                            )
                            response = _build_smart_query_response(
                                result,
                                available,
                                prov_stats,
                                prioritize_decisions,
                                decision_boost,
                            )
                            return ToolResult(content=[TextContent(
                                type="text",
                                text=json.dumps(response, indent=2),
                            )])

                        # Caller pinned to T2/T3: return the empty result
                        # rather than masking it with a keyword fallback.
                        if force_tier_enum is not None:
                            response = _build_smart_query_response(
                                result, available, None,
                                prioritize_decisions, decision_boost,
                            )
                            return ToolResult(content=[TextContent(
                                type="text",
                                text=json.dumps(response, indent=2),
                            )])
                except Exception as exc:
                    log_action(
                        "memory.smart_query.hosted_mode.t2t3_fallback",
                        error=str(exc),
                    )
                    if force_tier_enum is not None:
                        # Pinned to T2/T3 — surface the failure instead of
                        # silently degrading to GitHub keyword search.
                        return ToolResult(content=[TextContent(
                            type="text",
                            text=json.dumps({
                                "query": query,
                                "result_count": 0,
                                "error": "Tier query failed",
                                "message": str(exc),
                            }, indent=2)
                        )])

            # Fallback: hosted T1 keyword search via GitHub API
            err, result = search_entries_hosted(query=query, limit=20)
            if err:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "query": query,
                        "result_count": 0,
                        "error": f"Hosted search failed: {err}",
                        "tiers_queried": ["T1"],
                        "primary_tier": "T1",
                        "sufficient": False,
                        "evidence": [],
                        "message": f"Hosted T1 search error: {err}",
                    }, indent=2)
                )])
            # Convert search results to smart_query evidence format
            evidence = []
            for item in result.get("results", []):
                evidence.append({
                    "tier": "T1",
                    "id": item.get("entry_id", ""),
                    "content": item.get("summary", ""),
                    "score": 1.0,
                    "name": item.get("title", ""),
                    "provenance": {
                        "thread_topic": item.get("thread_topic", ""),
                        "entry_id": item.get("entry_id", ""),
                    },
                    "metadata": {
                        "agent": item.get("agent", ""),
                        "role": item.get("role", ""),
                        "entry_type": item.get("entry_type", ""),
                        "timestamp": item.get("timestamp", ""),
                    },
                })
            safe_boost = sanitize_boost(decision_boost)
            boosted = False
            if prioritize_decisions:
                boosted = _apply_decision_boost_evidence(evidence, safe_boost)
            payload = {
                "query": query,
                "result_count": len(evidence),
                "tiers_queried": ["T1"],
                "primary_tier": "T1",
                "escalation_reason": None,
                "sufficient": len(evidence) >= 3,
                "evidence": evidence,
                "message": f"Found {len(evidence)} results from hosted T1 search",
                "source": "hosted_github_api",
            }
            if boosted:
                payload["decisions_prioritized"] = True
                payload["decision_boost"] = safe_boost
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(payload, indent=2)
            )])

        # Load configuration
        config = load_tier_config(
            threads_dir=threads_dir_resolved,
            code_path=code_path_resolved,
        )
        _cap_tiers_by_profile(config)

        # Apply max_tiers parameter
        if max_tiers:
            config.max_tiers = min(max(1, max_tiers), 3)

        # Create orchestrator
        orchestrator = TierOrchestrator(config)

        # Check for available tiers
        available = orchestrator.available_tiers
        if not available:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "query": query,
                    "result_count": 0,
                    "tiers_queried": [],
                    "primary_tier": None,
                    "sufficient": False,
                    "evidence": [],
                    "message": "No memory tiers available. Check configuration.",
                    "available_tiers": [],
                }, indent=2)
            )])

        # Validate force_tier_enum (parsed earlier) against available tiers
        if force_tier_enum is not None and force_tier_enum not in available:
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "query": query,
                    "result_count": 0,
                    "error": f"Tier {force_tier} not available",
                    "available_tiers": [t.value for t in available],
                    "message": f"Requested tier {force_tier} is not available",
                }, indent=2)
            )])

        # Execute query
        log_action(
            "memory.smart_query",
            query=query,
            max_tiers=config.max_tiers,
            force_tier=force_tier,
            available_tiers=[t.value for t in available],
        )

        try:
            result = await asyncio.to_thread(
                orchestrator.query,
                query,
                group_ids=group_ids,
                force_tier=force_tier_enum,
            )

            prov_stats = (
                _resolve_t2_provenance(result, code_path_resolved)
                if resolve_provenance
                else None
            )
            response = _build_smart_query_response(
                result,
                available,
                prov_stats,
                prioritize_decisions,
                decision_boost,
            )

            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps(response, indent=2)
            )])

        except Exception as e:
            log_error(f"MEMORY: Smart query failed: {e}")
            return ToolResult(content=[TextContent(
                type="text",
                text=json.dumps({
                    "query": query,
                    "result_count": 0,
                    "error": "Query execution failed",
                    "message": str(e),
                }, indent=2)
            )])

    except ImportError as e:
        log_error(f"MEMORY: tier_strategy module unavailable: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "query": query,
                "result_count": 0,
                "error": "Multi-tier strategy module unavailable",
                "message": f"Import failed: {e}. Memory feature is not available in the open-core edition.",
            }, indent=2)
        )])
    except Exception as e:
        log_error(f"MEMORY: Unexpected error in smart_query: {e}")
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "query": query,
                "result_count": 0,
                "error": "Internal error",
                "message": str(e),
            }, indent=2)
        )])


# ============================================================================
# Memory Task Queue Tools
# ============================================================================


async def _memory_task_status_impl(
    ctx: Context,
    task_id: str = "",
    recover: bool = False,
    retry_dead_letters: bool = False,
) -> ToolResult:
    """Check memory queue health, poll task status, or trigger recovery.

    Args:
        ctx: MCP context
        task_id: Optional task ID to check. Empty = queue summary.
        recover: If True, reset stale "running" tasks to "pending".
        retry_dead_letters: If True, move dead-letter tasks back to queue.

    Returns:
        JSON with queue status or specific task details.
    """
    try:
        from ..memory_queue import get_queue

        queue = get_queue()
        if queue is None:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": "Memory queue not initialised",
                    "hint": "Queue starts automatically with MCP server",
                }),
            )])

        # Recovery actions
        if recover:
            count = queue.recover_stale()
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "action": "recover_stale",
                    "recovered": count,
                }),
            )])

        if retry_dead_letters:
            count = queue.retry_dead_letters()
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "action": "retry_dead_letters",
                    "re_enqueued": count,
                }),
            )])

        # Specific task lookup
        if task_id:
            task = queue.get_task(task_id)
            if task is not None:
                return ToolResult([TextContent(
                    type="text",
                    text=json.dumps(task.to_dict(), indent=2),
                )])

            # Plan v20 Phase 4: task not in active queue — check the receipt
            # sidecar so terminal outcomes (completed / dead_lettered) remain
            # queryable after the active record has been removed. Also accept
            # a hosted ``remote_task_id`` so clients in hybrid mode can poll
            # using either the local stage-A id or the hosted stage-B id.
            receipt = queue.get_receipt(task_id)
            if receipt is None:
                receipt = queue.get_receipt_by_remote_task_id(task_id)
            if receipt is not None:
                return ToolResult([TextContent(
                    type="text",
                    text=json.dumps({
                        "task_id": receipt.get("task_id"),
                        "remote_task_id": receipt.get("remote_task_id"),
                        "status": receipt.get("terminal_state"),
                        "terminal": True,
                        "source": "receipt",
                        "receipt": receipt,
                    }, indent=2),
                )])

            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Task {task_id} not found in active queue or receipt store",
                    "hint": (
                        "Completed and dead-lettered tasks persist as receipts "
                        "for a bounded window; older terminal outcomes fall off"
                    ),
                }),
            )])

        # Queue summary
        summary = queue.status_summary()

        # Surface worker concurrency and observability info
        from ..memory_queue import get_worker
        worker = get_worker()
        if worker is not None:
            summary["worker_count"] = worker.worker_count
            summary["total_timeouts"] = worker.total_timeouts
            summary["active_backend_count"] = worker.active_backend_count

        return ToolResult([TextContent(
            type="text",
            text=json.dumps(summary, indent=2),
        )])

    except Exception as e:
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({"error": str(e)}),
        )])


async def _bulk_index_hosted_impl(
    *,
    ctx: Context,
    code_path: str,
    backend: str,
    threads: str,
    max_entries: int,
    queue: Any,
) -> ToolResult:
    """Bulk index implementation for hosted mode — discovers entries via GitHub API."""
    from ..hosted_ops import list_threads_hosted, load_entries_hosted
    from ..context import get_effective_context
    from ..memory_queue import enqueue_memory_task

    # Discover topics
    if threads:
        all_topics = [t.strip() for t in threads.split(",")]
    else:
        err, thread_list = list_threads_hosted()
        if err:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({"error": f"Failed to list threads: {err}"}),
            )])
        all_topics = [t.topic for t in thread_list]

    # Plan v20 Phase 6: derive the canonical <org>_<repo> group_id via the
    # slug-aware helper instead of reusing the raw HTTP ``repo`` header
    # (which may still carry a ``-threads`` suffix or arbitrary casing).
    #
    # PR #654 in-PR review round 7 (MEDIUM): normalise ``-threads`` suffix
    # on the repo portion before derivation, matching the scoping in
    # tools/graph.py::_resolve_hosted_t1_target and tools/semantic.py::
    # _scope_group_id_to_http_ctx. Without this, X-Repo="org/repo-threads"
    # would produce group_id="org_repo_threads", so bulk-indexed T2
    # episodes would land under a different database than semantic
    # search / find_similar look in — entries permanently invisible to
    # semantic queries with no error signal.
    http_ctx = get_effective_context()
    # Round 15 (HIGH): refuse when there's no effective context at all.
    # Prior form left group_id="unknown" and enqueued into unknown_t2,
    # invisible to any query.
    if http_ctx is None or not getattr(http_ctx, "repo", None):
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({
                "error": "unresolved_repo_scope",
                "message": (
                    "Hosted bulk_index requires an HTTP context with X-Repo "
                    "(authenticated request). No scope resolved → refusing "
                    "rather than writing to an 'unknown' database."
                ),
            }),
        )])
    # Round 18 (MEDIUM): no ``"unknown"`` seed here. The guard above
    # returns before we reach this block unless ``http_ctx`` and
    # ``http_ctx.repo`` are both truthy, so the seed was dead today —
    # but any future refactor that relaxes the early-return could
    # silently fall through with ``group_id="unknown"`` and route
    # every episode to ``unknown_t2``. Keep ``group_id`` unbound until
    # derivation assigns it, so a regression fails noisily rather than
    # silently.
    group_id: str
    raw_slug = http_ctx.repo
    # Round 12 (MEDIUM): X-Repo without an ``<org>/<repo>`` shape is an
    # unscoped request. ``derive_project_group_id`` would silently fall
    # through to its repo-only default and route episodes to the wrong
    # graph. Reject rather than pollute the wrong database.
    if "/" not in raw_slug:
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({
                "error": "invalid_repo_scope",
                "message": (
                    f"X-Repo header {raw_slug!r} has no '<org>/<repo>' "
                    "form; hosted bulk_index requires a scoped repo."
                ),
            }),
        )])
    try:
        # Plan v20 defect #34 fix: derive the canonical T2 database name
        # (``<org>_<repo>_t2``) instead of the bare project_group_id
        # (``<org>_<repo>``). The graphiti_executor uses ``task.group_id``
        # directly as the FalkorDB database, so this MUST be the
        # canonical suffix-included form for hosted writes to land in
        # the canonical T2 graph.
        from watercooler.path_resolver import (
            derive_t2_database_name,
            get_threads_suffix,
        )
        _owner, _repo = raw_slug.split("/", 1)
        _threads_suffix = get_threads_suffix()
        if _threads_suffix and _repo.endswith(_threads_suffix):
            _repo = _repo[: -len(_threads_suffix)]
        if not _owner or not _repo:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": "invalid_repo_scope",
                    "message": (
                        f"X-Repo header {raw_slug!r} has empty owner or "
                        f"repo after threads-suffix strip "
                        f"(owner={_owner!r}, repo={_repo!r}); refusing to "
                        f"fall back to a non-canonical database name."
                    ),
                }),
            )])
        group_id = derive_t2_database_name(
            repo_slug=f"{_owner}/{_repo}",
        )
    except Exception as _e:
        # Round 13 (MEDIUM): fail closed rather than fall back to the
        # raw ``http_ctx.repo`` (which contained a ``/`` and wasn't a
        # valid FalkorDB database name anyway).
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({
                "error": "group_id_derivation_failed",
                "message": f"Could not derive group_id from {raw_slug!r}: {_e}",
            }),
        )])

    # Load entry-episode index for dedup (if graphiti backend)
    already_indexed = 0
    index = None
    if backend == "graphiti":
        try:
            from .. import memory as mem
            graphiti_config = mem.load_graphiti_config(code_path=code_path if code_path else None)
            if graphiti_config:
                graphiti_backend = mem.get_graphiti_backend(graphiti_config)
                if graphiti_backend and hasattr(graphiti_backend, "entry_episode_index"):
                    index = graphiti_backend.entry_episode_index
                    try:
                        index.load()
                    except Exception:
                        pass
        except Exception:
            pass

    entries_queued = 0
    entries_skipped = 0
    errors = []

    for topic in all_topics:
        err, entries = load_entries_hosted(topic)
        if err:
            errors.append({"topic": topic, "error": err})
            if len(errors) >= 10:
                break
            continue

        for entry in entries:
            if max_entries > 0 and entries_queued >= max_entries:
                break

            entry_id = str(entry.get("entry_id", ""))
            if not entry_id:
                entries_skipped += 1
                continue

            body = str(entry.get("body") or "")
            if not body.strip():
                entries_skipped += 1
                continue

            # Dedup against index
            if index and index.has_any_mapping(entry_id):
                already_indexed += 1
                continue

            title = str(entry.get("title") or "")
            task_id = enqueue_memory_task(
                entry_id=entry_id,
                topic=topic,
                group_id=group_id,
                content=body,
                title=title,
                timestamp=str(entry.get("timestamp") or ""),
                source_description=f"{group_id} | thread:{topic} | bulk_index_hosted",
                backend=backend,
                code_path=code_path,
            )
            if task_id is not None:
                entries_queued += 1

        if max_entries > 0 and entries_queued >= max_entries:
            break

    result = {
        "action": "bulk_index",
        "mode": "hosted",
        "topics_scanned": len(all_topics),
        "entries_queued": entries_queued,
        "entries_skipped": entries_skipped,
        "already_indexed": already_indexed,
        "errors": errors,
        "queue": {
            "depth": queue.depth() if queue else 0,
            "max_depth": queue.max_depth if queue else 0,
        },
    }

    return ToolResult([TextContent(type="text", text=json.dumps(result, indent=2))])


async def _bulk_index_impl(
    ctx: Context,
    code_path: str = "",
    backend: str = "graphiti",
    threads: str = "",
    max_entries: int = 0,
) -> ToolResult:
    """Queue bulk indexing of threads into memory backend (paid tier onboarding).

    Discovers threads, builds a manifest of entries, and enqueues them
    as individual tasks for persistent background processing with retry.

    Args:
        ctx: MCP context
        code_path: Repository root path (for group_id derivation).
        backend: Target backend ("graphiti" or "leanrag").
        threads: Comma-separated thread topics to index (empty = all).
        max_entries: Max entries to queue (0 = unlimited, for testing).

    Returns:
        JSON with indexing summary containing:
        - action: "bulk_index"
        - topics_scanned: number of thread topics discovered
        - entries_queued: entries newly enqueued for background indexing
        - entries_skipped: entries skipped due to missing body content
        - already_indexed: entries skipped because they are already committed
          to the entry-episode index (Graphiti backend only). A second run
          returning entries_queued=0 and already_indexed>0 is the expected
          idempotent outcome — not an error. Calling bulk_index again on an
          already-indexed project is safe and has no side effects.
        - errors: per-topic errors (capped at 10)
        - queue: queue status summary
    """
    try:
        from ..memory_queue import get_queue, MemoryTask, enqueue_memory_task, VALID_BACKENDS

        if backend not in VALID_BACKENDS:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": f"Invalid backend {backend!r}",
                    "valid_backends": sorted(VALID_BACKENDS),
                }),
            )])

        queue = get_queue()
        if queue is None:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": "Memory queue not initialised",
                }),
            )])

        # Hosted mode: discover entries from GitHub API
        from ..auth import is_hosted_mode
        if is_hosted_mode():
            return await _bulk_index_hosted_impl(
                ctx=ctx, code_path=code_path, backend=backend,
                threads=threads, max_entries=max_entries, queue=queue,
            )

        # Discover entries via watercooler library
        from watercooler.commands import list_entries
        from watercooler.path_resolver import resolve_threads_dir, derive_group_id

        threads_dir = resolve_threads_dir(code_root=Path(code_path)) if code_path else None
        if threads_dir is None:
            return ToolResult([TextContent(
                type="text",
                text=json.dumps({
                    "error": "Could not resolve threads directory",
                    "hint": "Provide code_path to the repository root",
                }),
            )])

        # Get list of thread topics from graph
        from watercooler.baseline_graph import storage as _graph_storage
        from watercooler.baseline_graph.storage import get_graph_dir as _get_graph_dir
        all_topics = _graph_storage.list_thread_topics(_get_graph_dir(threads_dir))

        if threads:
            selected = [t.strip() for t in threads.split(",")]
            all_topics = [t for t in all_topics if t in selected]

        # Derive canonical T2 group_id (Plan v20 defect #34): for ``backend ==
        # "graphiti"`` the queue task's ``group_id`` IS the FalkorDB database
        # name and MUST end in ``_t2`` or the executor's defense-in-depth
        # assertion (memory_sync.py:graphiti_executor) will reject the task
        # with PermanentTaskError. ``derive_group_id`` returns the bare
        # ``<repo>`` form; ``derive_t2_database_name`` returns ``<org>_<repo>_t2``
        # (or ``<repo>_t2`` when no slug is available) which is canonical-safe.
        # LeanRAG/baseline backends keep the bare form because the executor
        # guard is graphiti-specific.
        from watercooler.path_resolver import derive_t2_database_name
        if backend == "graphiti":
            group_id = derive_t2_database_name(threads_dir=threads_dir, code_path=code_path)
        else:
            group_id = derive_group_id(threads_dir=threads_dir)

        # Load index once for pre-flight dedup (Graphiti only — LeanRAG has no entry-episode index;
        # the LeanRAG queue file is its dedup mechanism).
        # NOTE: _dedup_index is a READ-ONLY disk snapshot, separate from backend.entry_episode_index
        # (the live in-memory object used by Guards 1/2 at execution time). Guard 3 is a best-effort
        # pre-flight filter; Guards 1/2 remain authoritative. Requires auto_save_index=True (default).
        _dedup_index = None
        if backend == "graphiti":
            try:
                from ..memory import load_graphiti_config
                from watercooler_memory.entry_episode_index import IndexConfig
                graphiti_config = load_graphiti_config(code_path=code_path)
                if graphiti_config and graphiti_config.entry_episode_index_path:
                    _dedup_index = _get_cached_provenance_index(graphiti_config.entry_episode_index_path)
                elif graphiti_config:
                    # Config exists but no explicit index path — use default location
                    _dedup_index = _get_cached_provenance_index(IndexConfig(backend="graphiti").index_path)
                # else: graphiti_config is None → Graphiti not configured → _dedup_index stays None
            except FileNotFoundError:
                logger.debug("MEMORY: No entry index found for dedup guard (first run)")
            except Exception as exc:
                logger.warning(
                    "MEMORY: Could not load entry index for dedup guard — dedup skipped: %s", exc
                )

        queued = 0
        skipped = 0
        already_indexed = 0
        errors = []

        for topic in all_topics:
            try:
                entries = list_entries(topic, threads_dir)
            except Exception as e:
                errors.append(f"{topic}: {e}")
                continue

            for entry in entries:
                if max_entries and queued >= max_entries:
                    break

                entry_id = entry.get("entry_id", "")
                content = entry.get("body", "")
                if not content:
                    skipped += 1
                    continue

                # Skip entries already committed to Graphiti
                if _dedup_index is not None and entry_id and _dedup_index.has_any_mapping(entry_id):
                    logger.debug("MEMORY: Skipping already-indexed entry %s", entry_id)
                    already_indexed += 1
                    continue

                task_id = enqueue_memory_task(
                    entry_id=entry_id,
                    topic=topic,
                    group_id=group_id,
                    content=content,
                    backend=backend,
                    title=entry.get("title", ""),
                    timestamp=entry.get("timestamp", ""),
                    source_description=f"{group_id} | thread:{topic} | bulk_index",
                    code_path=code_path,
                )
                if task_id:
                    queued += 1
                else:
                    skipped += 1

            if max_entries and queued >= max_entries:
                break

        summary = queue.status_summary()
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({
                "action": "bulk_index",
                "topics_scanned": len(all_topics),
                "entries_queued": queued,
                "entries_skipped": skipped,
                "already_indexed": already_indexed,
                "errors": errors[:10],
                "queue": summary,
            }, indent=2),
        )])

    except ImportError as e:
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({
                "error": f"Missing dependency: {e}",
                "hint": "Bulk index requires the full watercooler package",
            }),
        )])
    except Exception as e:
        return ToolResult([TextContent(
            type="text",
            text=json.dumps({"error": str(e)}),
        )])


async def _get_entry_provenance_impl(
    ctx: Context,
    entry_id: str = "",
    episode_uuid: str = "",
    code_path: str = "",
) -> ToolResult:
    """Look up reverse provenance between T1 entries and T2 episodes.

    Supports bidirectional lookup:
    - episode_uuid → entry_id (trace T2 results back to T1 source)
    - entry_id → episode list (find episodes for a T1 entry, chunk-aware)

    Provide exactly one of entry_id or episode_uuid.

    Args:
        ctx: MCP context
        entry_id: Watercooler entry ULID (for entry→episodes lookup)
        episode_uuid: Graphiti episode UUID (for episode→entry lookup)
        code_path: Path to project directory (for index file resolution)

    Returns:
        JSON with provenance mapping or {provenance_available: false}
    """
    # Input validation
    entry_id = entry_id.strip()
    episode_uuid = episode_uuid.strip()

    if entry_id and episode_uuid:
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "error": "Provide exactly one of entry_id or episode_uuid, not both",
                "operation": "get_entry_provenance",
            }, indent=2)
        )])

    if not entry_id and not episode_uuid:
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "error": "Provide either entry_id or episode_uuid",
                "operation": "get_entry_provenance",
            }, indent=2)
        )])

    try:
        from watercooler_memory.entry_episode_index import IndexConfig

        # Resolve index file path
        # 1. Try unified config if code_path provided
        # 2. Fall back to default IndexConfig path
        index_path = None
        try:
            from ..memory import load_graphiti_config
            graphiti_config = load_graphiti_config(code_path=code_path)
            if graphiti_config and graphiti_config.entry_episode_index_path:
                index_path = graphiti_config.entry_episode_index_path
        except Exception as exc:
            logger.debug("load_graphiti_config unavailable, using default index path: %s", exc)

        if index_path is None:
            logger.debug("Graphiti config unavailable; using default index path")
            config = IndexConfig(backend="graphiti")
            index_path = config.index_path

        # Load index via mtime-aware cache (avoids disk I/O on repeated calls)
        index = _get_cached_provenance_index(index_path)

        # episode_uuid → entry lookup
        if episode_uuid:
            found_entry_id = index.get_entry(episode_uuid)
            if found_entry_id:
                index_entry = index.get_index_entry(found_entry_id)
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "provenance_available": True,
                        "entry_id": found_entry_id,
                        "thread_id": index_entry.thread_id if index_entry else "",
                        "episode_uuid": episode_uuid,
                        "indexed_at": index_entry.indexed_at if index_entry else "",
                    }, indent=2)
                )])
            else:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "provenance_available": False,
                        "lookup_key": episode_uuid,
                        "message": "No mapping found for this episode UUID",
                        "action_hints": [
                            "Run watercooler_bulk_index to populate the index",
                            "Check watercooler_diagnose_memory for backend status",
                        ],
                    }, indent=2)
                )])

        # entry_id → episodes lookup (chunk-aware)
        if entry_id:
            # Check for chunked mappings first
            chunks = index.get_chunks_for_entry(entry_id)
            if chunks:
                # Informational: detect pre-chunk-era direct mapping (no consumer acts on this flag;
                # useful for debugging migration state)
                direct_uuid = index.get_episode(entry_id)
                episodes_list = [
                    {
                        "episode_uuid": c.episode_uuid,
                        "chunk_index": c.chunk_index,
                        "total_chunks": c.total_chunks,
                    }
                    for c in chunks
                ]
                result: dict[str, Any] = {
                    "provenance_available": True,
                    "entry_id": entry_id,
                    "thread_id": chunks[0].thread_id,
                    "episodes": episodes_list,
                }
                if direct_uuid:
                    result["stale_direct_mapping"] = True
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps(result, indent=2)
                )])

            # Fallback to non-chunked 1:1 mapping
            ep_uuid = index.get_episode(entry_id)
            if ep_uuid:
                index_entry = index.get_index_entry(entry_id)
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "provenance_available": True,
                        "entry_id": entry_id,
                        "thread_id": index_entry.thread_id if index_entry else "",
                        "episodes": [{
                            "episode_uuid": ep_uuid,
                            "chunk_index": 0,
                            "total_chunks": 1,
                        }],
                    }, indent=2)
                )])
            else:
                return ToolResult(content=[TextContent(
                    type="text",
                    text=json.dumps({
                        "provenance_available": False,
                        "lookup_key": entry_id,
                        "message": "No mapping found for this entry ID",
                        "action_hints": [
                            "Run watercooler_bulk_index to populate the index",
                            "Check watercooler_diagnose_memory for backend status",
                        ],
                    }, indent=2)
                )])

    except ImportError as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "error": f"EntryEpisodeIndex not available: {e}",
                "operation": "get_entry_provenance",
                "action_hints": [
                    "Provenance support requires the full watercooler build.",
                    "Check watercooler_diagnose_memory for backend status",
                ],
            }, indent=2)
        )])
    except Exception as e:
        return ToolResult(content=[TextContent(
            type="text",
            text=json.dumps({
                "error": f"Provenance lookup failed: {e}",
                "operation": "get_entry_provenance",
                "action_hints": [
                    "Provenance support requires the full watercooler build.",
                    "Check watercooler_diagnose_memory for backend status",
                ],
            }, indent=2)
        )])


# ---------------------------------------------------------------------------
# TOOL_BUILDERS: map of public tool name → (impl_func, global_name)
# ---------------------------------------------------------------------------

TOOL_BUILDERS: dict[str, tuple] = {
    "watercooler_get_entity_edge": (_get_entity_edge_impl, "get_entity_edge"),
    "watercooler_diagnose_memory": (_diagnose_memory_impl, "diagnose_memory"),
    "watercooler_graphiti_add_episode": (_graphiti_add_episode_impl, "graphiti_add_episode"),
    "watercooler_leanrag_run_pipeline": (_leanrag_run_pipeline_impl, "leanrag_run_pipeline"),
    "watercooler_clear_graph_group": (_clear_graph_group_impl, "clear_graph_group"),
    "watercooler_smart_query": (_smart_query_impl, "smart_query"),
    "watercooler_memory_task_status": (_memory_task_status_impl, "memory_task_status"),
    "watercooler_bulk_index": (_bulk_index_impl, "bulk_index"),
    "watercooler_get_entry_provenance": (_get_entry_provenance_impl, "get_entry_provenance"),
}


def register_memory_tools(mcp, *, selected=None, runtime=None):
    """Register memory tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
        selected: Optional collection of tool names to register.
            ``None`` means register all tools in this module.
        runtime: Optional ToolRuntime for profile-based tier capping.

    Note:
        The following tools have been removed (use replacements):
        - watercooler_query_memory → watercooler_smart_query
        - watercooler_search_nodes → watercooler_search(mode="entities")
        - watercooler_search_memory_facts → watercooler_smart_query
        - watercooler_get_episodes → watercooler_search(mode="episodes")
    """
    global get_entity_edge, diagnose_memory
    global graphiti_add_episode, leanrag_run_pipeline, clear_graph_group
    global smart_query
    global memory_task_status, bulk_index
    global get_entry_provenance
    global _runtime

    _runtime = runtime

    _globals = globals()
    for tool_name, (impl_func, global_name) in TOOL_BUILDERS.items():
        if selected is not None and tool_name not in selected:
            continue
        registered = mcp.tool(name=tool_name)(impl_func)
        _globals[global_name] = registered
