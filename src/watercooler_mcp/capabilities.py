"""Capability registry for watercooler MCP surfaces.

Defines the capability taxonomy, route resolution logic, tool-to-capability
mapping, and the CapabilityProfile used by all server surfaces to decide
whether a tool executes locally, remotely, or is disabled.

Surface names map to the user-visible `transport` values in config.toml:
  - transport="stdio"  → surface="local_full"   (all tools execute locally)
  - transport="hybrid" → surface="local_hybrid" (routes per capability profile)
  - transport="proxy"  → no factory surface; server.py uses create_proxy()
  - transport="http"   → bifurcates on is_hosted_mode():
      - is_hosted_mode()=True  → surface="hosted_full" / "hosted_premium"
                                 (via build_http_surfaces, Railway deployment)
      - is_hosted_mode()=False → surface="local_full" (self-hosted HTTP —
                                 the module-level mcp = build_default_local_server()
                                 served over HTTP instead of stdio)

Naming caveat: the config.toml `transport` key is an execution-routing mode,
NOT the MCP agent↔mcp stdio pipe (which is always stdio). See
docs/MCP-CLIENTS.md for the full discussion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Route and capability type aliases
# ---------------------------------------------------------------------------

RouteChoice = Literal["auto", "local", "remote", "disabled"]

CapabilityId = Literal[
    "threads_core",
    "thread_state_admin",
    "annotation_admin",
    "baseline_search",
    "semantic_similarity",
    "baseline_maintenance",
    "memory_query",
    "memory_observe",
    "memory_ingest",
    "memory_admin_graph",
    "memory_admin_cluster",
    "memory_migration",
    "daemon_observe",
    "daemon_control",
    "federation_search",
    "diagnostics",
]

_ALL_CAPABILITY_IDS: frozenset[str] = frozenset(
    {
        "threads_core",
        "thread_state_admin",
        "annotation_admin",
        "baseline_search",
        "semantic_similarity",
        "baseline_maintenance",
        "memory_query",
        "memory_observe",
        "memory_ingest",
        "memory_admin_graph",
        "memory_admin_cluster",
        "memory_migration",
        "daemon_observe",
        "daemon_control",
        "federation_search",
        "diagnostics",
    }
)

# ---------------------------------------------------------------------------
# Default hybrid route table
# ---------------------------------------------------------------------------

HYBRID_DEFAULT_ROUTES: dict[str, RouteChoice] = {
    "threads_core": "local",
    "thread_state_admin": "local",
    "annotation_admin": "local",
    "baseline_search": "local",
    # Plan v20 Phase 8 / Codex review: semantic entry search and find_similar
    # run against the hosted T1 FalkorDB HNSW index in hybrid mode. Keeping
    # this "local" would leave hybrid silently using the JSONL fallback
    # (principle 10). Operators who want the prior local path can override
    # it per-capability in config.toml.
    "semantic_similarity": "remote",
    "baseline_maintenance": "local",
    "memory_query": "remote",
    "memory_observe": "remote",
    "memory_ingest": "remote",
    "memory_admin_graph": "disabled",
    "memory_admin_cluster": "disabled",
    "memory_migration": "disabled",
    "daemon_observe": "remote",
    "daemon_control": "remote",
    "federation_search": "local",
    "diagnostics": "local",
}

# ---------------------------------------------------------------------------
# Tool-name sets used by surface builders
# ---------------------------------------------------------------------------

# Mixed tools: registered locally but wrapper decides local vs remote per-call.
MIXED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_search",
        "watercooler_find_similar",
    }
)

# Pure remote-mount tools: mounted from the premium proxy in hybrid mode.
HYBRID_REMOTE_MOUNT_TOOLS: frozenset[str] = frozenset(
    {
        "watercooler_smart_query",
        "watercooler_diagnose_memory",
        "watercooler_get_entity_edge",
        "watercooler_get_entry_provenance",
        "watercooler_memory_task_status",
        "watercooler_bulk_index",
        "watercooler_graphiti_add_episode",
        # Daemon tools: routed to Railway in hybrid mode
        "watercooler_daemon_status",
        "watercooler_daemon_findings",
        "watercooler_pulse_snapshot",
        "watercooler_acknowledge_finding",
    }
)

# Daemon tools: observe and control daemons (routed to Railway in hybrid mode).
DAEMON_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_daemon_status",
        "watercooler_daemon_findings",
        "watercooler_pulse_snapshot",
        "watercooler_acknowledge_finding",
    }
)

# Disabled in hybrid by default (memory_admin_graph + memory_admin_cluster + memory_migration).
HYBRID_DISABLED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_clear_graph_group",
        "watercooler_leanrag_run_pipeline",
        "watercooler_migration_preflight",
        "watercooler_migrate_to_memory_backend",
    }
)

# All remote-capable memory and migration tools (superset of hybrid remote-mount
# + disabled). Hosted surfaces may expose all of these.
REMOTE_CAPABLE_MEMORY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_smart_query",
        "watercooler_diagnose_memory",
        "watercooler_get_entity_edge",
        "watercooler_get_entry_provenance",
        "watercooler_memory_task_status",
        "watercooler_bulk_index",
        "watercooler_graphiti_add_episode",
        "watercooler_clear_graph_group",
        "watercooler_leanrag_run_pipeline",
        "watercooler_migration_preflight",
        "watercooler_migrate_to_memory_backend",
    }
)

# All graph tool names (registered by tools/graph.py).
GRAPH_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_baseline_graph_stats",
        "watercooler_search",
        "watercooler_find_similar",
        "watercooler_baseline_sync_status",
        "watercooler_access_stats",
        "watercooler_graph_enrich",
        "watercooler_graph_recover",
        "watercooler_graph_project",
        "watercooler_annotate",
        "watercooler_remove_annotation",
        "watercooler_get_annotations",
        "watercooler_delete_entry",
        "watercooler_delete_thread",
        "watercooler_archive_thread",
        "watercooler_sync_repair",
    }
)

# Premium graph tools: the subset of graph tools that participate in
# premium hosted surfaces and mixed-tool routing.
PREMIUM_GRAPH_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_search",
        "watercooler_find_similar",
    }
)

# ---------------------------------------------------------------------------
# Tool → capability mapping
# ---------------------------------------------------------------------------

_TOOL_CAPABILITY_MAP: dict[str, CapabilityId] = {
    # Thread core
    "watercooler_list_threads": "threads_core",
    "watercooler_read_thread": "threads_core",
    "watercooler_list_thread_entries": "threads_core",
    "watercooler_get_thread_entry": "threads_core",
    "watercooler_get_thread_entry_range": "threads_core",
    "watercooler_say": "threads_core",
    "watercooler_ack": "threads_core",
    "watercooler_handoff": "threads_core",
    "watercooler_set_status": "threads_core",
    # Thread state admin
    "watercooler_delete_entry": "thread_state_admin",
    "watercooler_delete_thread": "thread_state_admin",
    "watercooler_archive_thread": "thread_state_admin",
    # Annotation admin
    "watercooler_annotate": "annotation_admin",
    "watercooler_remove_annotation": "annotation_admin",
    "watercooler_get_annotations": "annotation_admin",
    # Baseline search — note: watercooler_search is mode-dependent (see resolve_search_capability)
    "watercooler_baseline_graph_stats": "baseline_search",
    "watercooler_baseline_sync_status": "baseline_search",
    "watercooler_access_stats": "baseline_search",
    "watercooler_list_decisions": "baseline_search",
    # Semantic similarity
    "watercooler_find_similar": "semantic_similarity",
    # Plan v20 Phase 8: hosted-side T1 embedding upsert/delete. Registered
    # only on hosted_full / hosted_premium surfaces; hybrid clients reach
    # them via premium_client. Semantic-similarity capability keeps the
    # hosted auth middleware gate honest.
    "watercooler_semantic_upsert_embedding": "semantic_similarity",
    "watercooler_semantic_delete_embedding": "semantic_similarity",
    "watercooler_semantic_list_embeddings": "semantic_similarity",
    # Baseline maintenance (local-only, not hosted-safe)
    "watercooler_graph_enrich": "baseline_maintenance",
    "watercooler_graph_recover": "baseline_maintenance",
    "watercooler_graph_project": "baseline_maintenance",
    "watercooler_sync_repair": "baseline_maintenance",
    "watercooler_reindex": "baseline_maintenance",
    # Memory query
    "watercooler_smart_query": "memory_query",
    "watercooler_get_entity_edge": "memory_query",
    "watercooler_get_entry_provenance": "memory_query",
    # Memory observe
    "watercooler_diagnose_memory": "memory_observe",
    "watercooler_memory_task_status": "memory_observe",
    # Memory ingest
    "watercooler_bulk_index": "memory_ingest",
    "watercooler_graphiti_add_episode": "memory_ingest",
    # Graph memory admin
    "watercooler_clear_graph_group": "memory_admin_graph",
    # Cluster admin
    "watercooler_leanrag_run_pipeline": "memory_admin_cluster",
    # Memory migration
    "watercooler_migration_preflight": "memory_migration",
    "watercooler_migrate_to_memory_backend": "memory_migration",
    # Daemon
    "watercooler_daemon_status": "daemon_observe",
    "watercooler_daemon_findings": "daemon_observe",
    "watercooler_pulse_snapshot": "daemon_observe",
    "watercooler_acknowledge_finding": "daemon_control",
    "watercooler_decision_extractor_reset": "daemon_control",
    # Federation
    "watercooler_federated_search": "federation_search",
    # Roles
    "watercooler_roles": "diagnostics",
    "watercooler_role_details": "diagnostics",
    # Diagnostics
    "watercooler_health": "diagnostics",
    "watercooler_whoami": "diagnostics",
}


def resolve_search_capability(
    mode: str, query: str = "", semantic: bool = False
) -> CapabilityId:
    """Resolve the capability required for a ``watercooler_search`` call.

    ``watercooler_search`` is a *mixed* tool: when ``mode`` is ``entries``
    the baseline graph suffices (``baseline_search``), but ``facts``,
    ``entities``, and ``episodes`` require the memory backend
    (``memory_query``).

    When *mode* is ``auto``, the same ``infer_search_mode()`` logic used
    by the actual search execution path is applied so that temporal queries
    that auto-inflate to ``facts`` mode correctly require ``memory_query``.
    """
    # Resolve "auto" through the same inference the execution path uses.
    if mode == "auto":
        from .tools.graph import infer_search_mode

        mode = infer_search_mode(mode, query, semantic)
    if mode in ("facts", "entities", "episodes"):
        return "memory_query"
    # Plan v20 Phase 8: entries-mode search with semantic=True routes to the
    # T1 FalkorDB HNSW vector index. In hybrid that path must resolve to
    # remote so the semantic read goes against the hosted T1 rather than a
    # silent local JSONL fallback.
    if mode == "entries" and semantic:
        return "semantic_similarity"
    return "baseline_search"


def resolve_find_similar_capability() -> CapabilityId:
    """Return the capability id for ``watercooler_find_similar``."""
    return "semantic_similarity"


def tool_capability(
    tool_name: str, arguments: dict[str, Any] | None = None
) -> CapabilityId:
    """Return the capability required to execute *tool_name*.

    For ``watercooler_search`` the resolved capability depends on the
    ``mode`` argument; all other tools have a static mapping.
    """
    if tool_name == "watercooler_search":
        args = arguments or {}
        mode = args.get("mode", "entries")
        query = args.get("query", "")
        semantic = args.get("semantic", False)
        return resolve_search_capability(mode, query=query, semantic=semantic)
    cap = _TOOL_CAPABILITY_MAP.get(tool_name)
    if cap is None:
        raise ValueError(f"Unknown tool: {tool_name}")
    return cap


# ---------------------------------------------------------------------------
# Route validation
# ---------------------------------------------------------------------------

_VALID_ROUTES: frozenset[str] = frozenset({"auto", "local", "remote", "disabled"})


def validate_capability_routes(raw: dict[str, str]) -> dict[str, RouteChoice]:
    """Validate and normalise a user-supplied capability route override map.

    Raises ``ValueError`` on unknown capability ids or invalid route values.
    """
    validated: dict[str, RouteChoice] = {}
    for key, value in raw.items():
        if key not in _ALL_CAPABILITY_IDS:
            raise ValueError(
                f"Unknown capability id: {key!r}. "
                f"Valid ids: {sorted(_ALL_CAPABILITY_IDS)}"
            )
        if value not in _VALID_ROUTES:
            raise ValueError(
                f"Invalid route {value!r} for capability {key!r}. "
                f"Valid routes: {sorted(_VALID_ROUTES)}"
            )
        validated[key] = value  # type: ignore[assignment]
    return validated


# ---------------------------------------------------------------------------
# CapabilityProfile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityProfile:
    """Immutable capability route table used at runtime.

    Created once during server bootstrap and shared across all tool
    invocations for the lifetime of the process.
    """

    routes: dict[str, RouteChoice] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Freeze the mutable dict so concurrent readers cannot mutate it.
        from types import MappingProxyType

        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))

    def desired_route(self, capability: CapabilityId) -> RouteChoice:
        """Return the configured route for *capability*, defaulting to ``auto``."""
        return self.routes.get(capability, "auto")

    def resolve_execution_target(
        self,
        capability: CapabilityId,
        *,
        local_available: bool,
        remote_available: bool,
    ) -> Literal["local", "remote", "disabled"]:
        """Decide where a capability should execute.

        Resolution rules (deterministic, no fallback guessing):
        - ``disabled`` ⇒ ``disabled``
        - ``local``    ⇒ ``local`` if available, else ``disabled``
        - ``remote``   ⇒ ``remote`` if available, else ``disabled``
        - ``auto``     ⇒ ``local`` if available, else ``remote`` if available,
                         else ``disabled``
        """
        desired = self.desired_route(capability)
        if desired == "disabled":
            return "disabled"
        if desired == "local":
            return "local" if local_available else "disabled"
        if desired == "remote":
            return "remote" if remote_available else "disabled"
        # auto
        if local_available:
            return "local"
        if remote_available:
            return "remote"
        return "disabled"
