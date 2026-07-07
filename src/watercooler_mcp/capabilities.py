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
from typing import Any, Callable, Literal

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
    # Grant-style capability (no route): gates the cross-scope graph
    # enumeration branch of watercooler_health(detail="graph"). The health
    # tool itself stays under "diagnostics" so ordinary health is never
    # grant-dependent (incident bug-hybrid-static-x-repo-cross-tenant-
    # t2-scope, PR 4 / review :5).
    "graph_admin",
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
        "graph_admin",
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
    # Grant-style capability: no tool maps to it for routing (it gates the
    # cross-scope branch inside watercooler_health(detail="graph")); the
    # entry exists to keep the route table total over _ALL_CAPABILITY_IDS.
    "graph_admin": "local",
}

# ---------------------------------------------------------------------------
# Tool-name sets used by surface builders
# ---------------------------------------------------------------------------

# Mixed tools: registered locally but a hybrid wrapper decides local vs remote
# vs disabled per call. watercooler_bulk_index is mixed because its modes span
# capabilities — default ingest (memory_ingest, remote) vs preflight_only= /
# run_pipeline= (memory_migration / memory_admin_cluster, disabled by default);
# a bare-name mount would expose the disabled modes (PR4a review). list_decisions
# is mixed because include_supersession=True performs a T2 memory read while
# the default listing remains baseline-only. graphiti_add_episode is mixed
# (was proxy-mounted) so its hybrid wrapper can select a per-repo premium
# client from the pool via code_path — a bare mount asserted the boot repo's
# X-Repo on every ingest (incident
# bug-hybrid-static-x-repo-cross-tenant-t2-scope).
MIXED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_search",
        "watercooler_bulk_index",
        "watercooler_list_decisions",
        "watercooler_graphiti_add_episode",
    }
)

# Pure remote-mount tools: mounted from the premium proxy in hybrid mode.
HYBRID_REMOTE_MOUNT_TOOLS: frozenset[str] = frozenset(
    {
        "watercooler_smart_query",
        "watercooler_diagnose_memory",
        "watercooler_graph_trace",
        "watercooler_memory_task_status",
        # Daemon tools: routed to Railway in hybrid mode
        "watercooler_daemon_status",
        "watercooler_daemon_findings",
        "watercooler_pulse_snapshot",
    }
)

# Daemon tools: observe and control daemons (routed to Railway in hybrid mode).
# acknowledge_finding folded into daemon_findings(action="acknowledge") in PR5
# D1 — the daemon_control capability is now a daemon_findings mode.
DAEMON_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_daemon_status",
        "watercooler_daemon_findings",
        "watercooler_pulse_snapshot",
    }
)

# Disabled in hybrid by default (memory_admin_graph). The memory_admin_cluster
# and memory_migration capabilities are now reached as bulk_index modes
# (run_pipeline= / preflight_only=) and route per-(tool, args), not per name.
HYBRID_DISABLED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_clear_graph_group",
    }
)

# All remote-capable memory tools (superset of hybrid remote-mount + disabled).
# Hosted surfaces may expose all of these.
REMOTE_CAPABLE_MEMORY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_smart_query",
        "watercooler_diagnose_memory",
        "watercooler_graph_trace",
        "watercooler_memory_task_status",
        "watercooler_bulk_index",
        "watercooler_graphiti_add_episode",
        "watercooler_clear_graph_group",
    }
)

# All graph tool names (registered by tools/graph.py).
GRAPH_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "watercooler_baseline_graph",
        "watercooler_search",
        "watercooler_access_stats",
        "watercooler_graph_enrich",
        "watercooler_graph_project",
        "watercooler_annotations",
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
    }
)

# ---------------------------------------------------------------------------
# Tool × capability × authority matrix
# ---------------------------------------------------------------------------
#
# TOOL_MATRIX is the single source of truth for the tool-surface
# consolidation (thread mcp-tool-surface-consolidation-2026-05): every
# registered watercooler MCP tool has exactly one entry. _TOOL_CAPABILITY_MAP
# is *derived* from it — do not edit that map directly. See
# dev_docs/reference/tool-capability-matrix.md for the schema and rationale.

AuthorityLevel = Literal["L1", "L2", "L3"]
# L1  autonomous  — retrieval / analysis / observation / ordinary Note writes;
#                   an agent may invoke without human authorization.
# L2  preparation — mutates durable state but asserts no human judgment
#                   (indexing, graph maintenance, annotation writes).
# L3  authority   — asserts human-domain authority (Decision/Closure writes,
#                   status mutation, archive, delete, daemon control);
#                   requires explicit human authorization.


@dataclass(frozen=True)
class ToolSpec:
    """One row of TOOL_MATRIX: a tool's capability and authority level.

    For a tool whose capability and/or authority depends on call arguments
    (e.g. ``watercooler_search`` by ``mode``, ``watercooler_write`` by
    ``authority_mode``), ``arg_sensitive`` is True and the ``capability`` /
    ``authority`` fields record the *default* (no-argument) resolution; the
    live value comes from the resolver registered in ``_ARG_RESOLVERS`` /
    ``_AUTHORITY_ARG_RESOLVERS``.
    """

    capability: CapabilityId
    authority: AuthorityLevel
    arg_sensitive: bool = False
    note: str = ""


TOOL_MATRIX: dict[str, ToolSpec] = {
    # ── Thread core ──────────────────────────────────────────────────────
    "watercooler_list_threads": ToolSpec("threads_core", "L1"),
    "watercooler_read_thread": ToolSpec("threads_core", "L1"),
    "watercooler_list_thread_entries": ToolSpec("threads_core", "L1"),
    "watercooler_get_thread_entry": ToolSpec("threads_core", "L1"),
    "watercooler_say": ToolSpec("threads_core", "L1"),
    "watercooler_ack": ToolSpec("threads_core", "L1"),
    "watercooler_handoff": ToolSpec("threads_core", "L1"),
    "watercooler_set_status": ToolSpec(
        "threads_core", "L3", note="status mutation is authority-gated"
    ),
    "watercooler_write": ToolSpec(
        "threads_core", "L1", arg_sensitive=True,
        note="authority_mode=ordinary → L1; decision/closure → L3",
    ),
    # Promotion is human-authorized — writes a supported promoted entry and a
    # CandidateDisposition Note. Requires human_authorized_by.
    "watercooler_promote_candidate": ToolSpec(
        "threads_core", "L3",
        note="promotion writes a supported entry + CandidateDisposition; "
        "human_authorized_by required",
    ),
    # ── Thread state admin ───────────────────────────────────────────────
    "watercooler_delete_entry": ToolSpec("thread_state_admin", "L3"),
    "watercooler_delete_thread": ToolSpec("thread_state_admin", "L3"),
    "watercooler_archive_thread": ToolSpec("thread_state_admin", "L3"),
    # ── Annotation ───────────────────────────────────────────────────────
    "watercooler_annotations": ToolSpec(
        "annotation_admin", "L2", arg_sensitive=True,
        note="action=get → L1; action=add/remove → L2",
    ),
    "watercooler_follow_xref": ToolSpec("annotation_admin", "L1"),
    # ── Baseline search ──────────────────────────────────────────────────
    # watercooler_search capability resolves per-(tool, args): by mode/semantic,
    # or the seed_entry_id= (semantic_similarity) / namespaces= (federation_search)
    # mode selectors (PR6). Every mode is an L1 read.
    "watercooler_search": ToolSpec(
        "baseline_search", "L1", arg_sensitive=True,
        note="capability resolves by mode/semantic/seed_entry_id/namespaces; "
        "all modes are L1 reads",
    ),
    "watercooler_baseline_graph": ToolSpec("baseline_search", "L1"),
    "watercooler_access_stats": ToolSpec("baseline_search", "L1"),
    "watercooler_list_decisions": ToolSpec(
        "baseline_search", "L1", arg_sensitive=True,
        note="include_supersession=True → memory_query; default listing → baseline_search",
    ),
    # C1 (candidate-research-backend-support): open-candidates listing — a
    # pure baseline read over entry bodies + disposition markers, no T2 leg.
    "watercooler_list_pending_candidates": ToolSpec("baseline_search", "L1"),
    # ── Semantic similarity ──────────────────────────────────────────────
    # PR3b: hosted T1 embedding upsert/list/delete, action-selected.
    "watercooler_semantic": ToolSpec(
        "semantic_similarity", "L2", arg_sensitive=True,
        note="action=list → L1; action=upsert/delete → L2",
    ),
    # ── Baseline maintenance (local-only, not hosted-safe) ───────────────
    "watercooler_graph_enrich": ToolSpec("baseline_maintenance", "L2"),
    "watercooler_graph_project": ToolSpec("baseline_maintenance", "L2"),
    "watercooler_sync_repair": ToolSpec("baseline_maintenance", "L2"),
    # ── Memory query ─────────────────────────────────────────────────────
    "watercooler_smart_query": ToolSpec("memory_query", "L1"),
    "watercooler_graph_trace": ToolSpec("memory_query", "L1"),
    # ── Memory observe ───────────────────────────────────────────────────
    "watercooler_diagnose_memory": ToolSpec("memory_observe", "L1"),
    "watercooler_memory_task_status": ToolSpec("memory_observe", "L1"),
    # ── Memory ingest ────────────────────────────────────────────────────
    # bulk_index default = memory_ingest/L2; preflight_only= and run_pipeline=
    # modes resolve via _ARG_RESOLVERS / _AUTHORITY_ARG_RESOLVERS (PR4a B1/B2).
    "watercooler_bulk_index": ToolSpec(
        "memory_ingest", "L2", arg_sensitive=True
    ),
    "watercooler_graphiti_add_episode": ToolSpec("memory_ingest", "L2"),
    # ── Memory admin ─────────────────────────────────────────────────────
    "watercooler_clear_graph_group": ToolSpec("memory_admin_graph", "L2"),
    # ── Daemon ───────────────────────────────────────────────────────────
    "watercooler_daemon_status": ToolSpec("daemon_observe", "L1"),
    # daemon_findings default = daemon_observe/L1 (list); action="acknowledge"
    # resolves to daemon_control/L3 via the _ARG_RESOLVERS entries (PR5 D1).
    "watercooler_daemon_findings": ToolSpec(
        "daemon_observe", "L1", arg_sensitive=True
    ),
    "watercooler_pulse_snapshot": ToolSpec("daemon_observe", "L1"),
    # ── Roles / diagnostics ──────────────────────────────────────────────
    "watercooler_roles": ToolSpec("diagnostics", "L1"),
    "watercooler_health": ToolSpec("diagnostics", "L1"),
    "watercooler_health_probe": ToolSpec("diagnostics", "L1"),
    # watercooler_init mutates durable state (scaffolds, binds the worktree,
    # opt-in pushes) — diagnostics capability, L2 authority (never auto-invoked).
    "watercooler_init": ToolSpec("diagnostics", "L2"),
}

# Derived from TOOL_MATRIX — the authoritative source. Do not edit directly.
_TOOL_CAPABILITY_MAP: dict[str, CapabilityId] = {
    name: spec.capability for name, spec in TOOL_MATRIX.items()
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


# ---------------------------------------------------------------------------
# Argument-sensitive resolution
# ---------------------------------------------------------------------------
#
# Most tools have a static (tool → capability) and (tool → authority) mapping
# in TOOL_MATRIX. A few resolve per call argument. Each such tool registers a
# resolver here; later consolidation PRs add resolvers as they collapse tools
# behind an ``action=`` / ``mode=`` selector. This is the generalization of
# the former hard-coded ``watercooler_search`` special case — the safety
# substrate that lets a collapsed tool span an authority/capability boundary
# while still resolving per ``(tool, arguments)``.

_ARG_RESOLVERS: dict[str, Callable[[dict[str, Any]], CapabilityId]] = {
    # watercooler_search (PR6 D4/D5): seed_entry_id= is seeded similarity
    # (semantic_similarity, folded-in find_similar); federated=/namespaces= is
    # federated search (federation_search, folded-in federated_search);
    # otherwise the capability resolves by mode/semantic.
    "watercooler_search": lambda args: (
        "federation_search"
        if (args.get("federated") or args.get("namespaces"))
        else "semantic_similarity"
        if args.get("seed_entry_id")
        else resolve_search_capability(
            args.get("mode", "entries"),
            query=args.get("query", ""),
            semantic=args.get("semantic", False),
        )
    ),
    # watercooler_bulk_index (PR4a B1/B2): preflight_only= is a migration
    # prerequisite check; run_pipeline= runs the LeanRAG clustering pipeline;
    # the default queues ingest tasks. Each mode keeps the capability the
    # former standalone tool resolved to.
    "watercooler_bulk_index": lambda args: (
        "memory_migration"
        if args.get("preflight_only")
        else "memory_admin_cluster"
        if args.get("run_pipeline")
        else "memory_ingest"
    ),
    # watercooler_list_decisions: default listing is a baseline graph read;
    # include_supersession=True enriches from T2/Graphiti and must route as
    # memory_query in hybrid.
    "watercooler_list_decisions": lambda args: (
        "memory_query" if args.get("include_supersession") else "baseline_search"
    ),
    # watercooler_daemon_findings (PR5 D1): action="acknowledge" is the
    # folded-in acknowledge_finding (daemon_control); listing is daemon_observe.
    "watercooler_daemon_findings": lambda args: (
        "daemon_control"
        if str(args.get("action", "")).strip().lower() == "acknowledge"
        else "daemon_observe"
    ),
}

_AUTHORITY_ARG_RESOLVERS: dict[str, Callable[[dict[str, Any]], AuthorityLevel]] = {
    # watercooler_write: ordinary Note → L1; decision/closure → L3.
    "watercooler_write": lambda args: (
        "L3"
        if str(args.get("authority_mode", "ordinary")) in ("decision", "closure")
        else "L1"
    ),
    # watercooler_semantic: action=list is an L1 read; upsert/delete are L2.
    "watercooler_semantic": lambda args: (
        "L1" if str(args.get("action", "")).strip().lower() == "list" else "L2"
    ),
    # watercooler_annotations: action=get is an L1 read; add/remove are L2.
    "watercooler_annotations": lambda args: (
        "L1" if str(args.get("action", "")).strip().lower() == "get" else "L2"
    ),
    # watercooler_bulk_index: preflight_only= is a read-only prerequisite
    # check (L1, folded-in migration_preflight); queuing ingest and running
    # the LeanRAG pipeline are both L2.
    "watercooler_bulk_index": lambda args: (
        "L1" if args.get("preflight_only") else "L2"
    ),
    # watercooler_daemon_findings: listing is an L1 read; action="acknowledge"
    # (folded-in acknowledge_finding) is an L3 authority-gated write.
    "watercooler_daemon_findings": lambda args: (
        "L3"
        if str(args.get("action", "")).strip().lower() == "acknowledge"
        else "L1"
    ),
    # watercooler_health_probe: the probe itself is a read-only diagnostic (L1),
    # but alert=True dispatches a Slack webhook — an externally visible side
    # effect that must not ride read-only authority. Escalate to L2 (control),
    # matching the read-vs-side-effect split used by annotations/semantic/
    # bulk_index. (Lighter than the L3 durable-record mutations.)
    "watercooler_health_probe": lambda args: ("L2" if args.get("alert") else "L1"),
}


def tool_capability(
    tool_name: str, arguments: dict[str, Any] | None = None
) -> CapabilityId:
    """Return the capability required to execute *tool_name*.

    Tools whose capability depends on call arguments (e.g. ``watercooler_search``
    by ``mode``) resolve through ``_ARG_RESOLVERS``; all others use the static
    TOOL_MATRIX projection.
    """
    resolver = _ARG_RESOLVERS.get(tool_name)
    if resolver is not None:
        return resolver(arguments or {})
    cap = _TOOL_CAPABILITY_MAP.get(tool_name)
    if cap is None:
        raise ValueError(f"Unknown tool: {tool_name}")
    return cap


def tool_authority(
    tool_name: str, arguments: dict[str, Any] | None = None
) -> AuthorityLevel:
    """Return the agent-authority-ladder level a call to *tool_name* implies.

    Tools whose authority depends on call arguments (e.g. ``watercooler_write``
    by ``authority_mode``) resolve through ``_AUTHORITY_ARG_RESOLVERS``; all
    others use the static TOOL_MATRIX authority field.
    """
    resolver = _AUTHORITY_ARG_RESOLVERS.get(tool_name)
    if resolver is not None:
        return resolver(arguments or {})
    spec = TOOL_MATRIX.get(tool_name)
    if spec is None:
        raise ValueError(f"Unknown tool: {tool_name}")
    return spec.authority


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
