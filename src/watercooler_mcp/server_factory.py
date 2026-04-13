"""Shared server factory for all MCP surfaces.

Centralises tool selection and FastMCP assembly so that ``server.py``
(CLI / stdio / proxy), ``server_http.py`` (hosted HTTP), and tests
all share one authoritative code path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastmcp import FastMCP

from .capabilities import (
    DAEMON_TOOL_NAMES,
    GRAPH_TOOL_NAMES,
    HYBRID_DEFAULT_ROUTES,
    HYBRID_DISABLED_TOOL_NAMES,
    HYBRID_REMOTE_MOUNT_TOOLS,
    MIXED_TOOL_NAMES,
    PREMIUM_GRAPH_TOOL_NAMES,
    REMOTE_CAPABLE_MEMORY_TOOL_NAMES,
    CapabilityProfile,
    validate_capability_routes,
)
from .tool_runtime import SurfaceName, ToolRuntime

if TYPE_CHECKING:
    from .premium_client import PremiumToolClient
    from .capability_auth import CapabilityAuthorizer
    from .deployment_profile import DeploymentAvailability

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hosted capability authorization middleware
# ---------------------------------------------------------------------------


def _apply_hosted_auth_transforms(mcp: FastMCP, authorizer) -> None:
    """Add a middleware that enforces capability grants on hosted surfaces.

    Before every ``tools/call``, the middleware resolves the required
    capability and calls ``authorizer.ensure(capability, user_id)``.
    If denied, the tool call short-circuits with a JSON error response.
    """
    import json
    from fastmcp.server.middleware import Middleware
    from .capabilities import tool_capability
    from .context import get_effective_context

    class _CapabilityAuthMiddleware(Middleware):
        async def on_call_tool(self, context, call_next):
            params = context.message
            tool_name = params.name
            arguments = params.arguments or {}
            logger.warning(
                "CAPABILITY_MW: entered on_call_tool for %s", tool_name
            )

            # Resolve which capability this tool call requires.
            try:
                cap = tool_capability(tool_name, arguments)
            except ValueError:
                # Unknown tool — let FastMCP handle the 404.
                return await call_next(context)

            # Resolve user identity and preloaded capabilities from context.
            eff_ctx = get_effective_context()
            user_id = eff_ctx.user_id if eff_ctx else ""
            preloaded_caps = eff_ctx.capabilities if eff_ctx else None
            logger.warning(
                "CAPABILITY_MW: tool=%s cap=%s user=%s context=%s caps=%s",
                tool_name, cap, user_id or "NONE",
                "present" if eff_ctx else "MISSING",
                len(preloaded_caps) if preloaded_caps else "NONE",
            )

            # Check authorization (uses preloaded caps from credentials
            # response to avoid a second control-plane round-trip).
            denial = authorizer.ensure(cap, user_id, preloaded_capabilities=preloaded_caps)
            logger.debug("CAPABILITY_MW: ensure() returned denial=%s", denial is not None)
            if denial is not None:
                # Return the denial as a text tool result.
                from mcp.types import TextContent
                from fastmcp.tools.tool import ToolResult
                logger.debug("CAPABILITY_MW: returning denial for %s", tool_name)
                return ToolResult(content=[TextContent(type="text", text=denial)])

            logger.debug("CAPABILITY_MW: calling call_next for %s", tool_name)
            result = await call_next(context)
            logger.debug("CAPABILITY_MW: call_next returned for %s", tool_name)
            return result

    mcp.add_middleware(_CapabilityAuthMiddleware())

# ---------------------------------------------------------------------------
# Profile-aware availability helpers
# ---------------------------------------------------------------------------


def _memory_tools_available(runtime: ToolRuntime) -> bool:
    """Return True if memory tools should be offered on this surface.

    Local surfaces (``deployment_availability is None``) always get memory
    tools.  Hosted surfaces need at least a ``t2`` effective profile.
    """
    if runtime.deployment_availability is None:
        return True
    return runtime.effective_hosted_profile in ("t2", "t2t3")


def _leanrag_tools_available(runtime: ToolRuntime) -> bool:
    """Return True if LeanRAG tools should be offered on this surface.

    Local surfaces always get LeanRAG tools.  Hosted surfaces need a
    ``t2t3`` effective profile.
    """
    if runtime.deployment_availability is None:
        return True
    return runtime.effective_hosted_profile == "t2t3"


# ---------------------------------------------------------------------------
# Tool-set selection helpers
# ---------------------------------------------------------------------------

# Local-only maintenance tools that must NOT appear on hosted surfaces.
_HOSTED_EXCLUDED_GRAPH_TOOLS: frozenset[str] = frozenset({
    "watercooler_graph_enrich",
    "watercooler_graph_project",
    "watercooler_graph_recover",
    "watercooler_sync_repair",
    "watercooler_reindex",
})


def graph_tools_for_surface(runtime: ToolRuntime) -> set[str]:
    """Return the graph tool names that should be registered for *runtime*."""
    if runtime.surface == "hosted_premium":
        return set(PREMIUM_GRAPH_TOOL_NAMES) - _HOSTED_EXCLUDED_GRAPH_TOOLS
    if runtime.surface == "hosted_full":
        return set(GRAPH_TOOL_NAMES) - _HOSTED_EXCLUDED_GRAPH_TOOLS
    # Local surfaces get the full graph tool set.
    return set(GRAPH_TOOL_NAMES)


def memory_tools_for_surface(runtime: ToolRuntime) -> set[str]:
    """Return the memory tool names that should be registered locally."""
    migration_tools = {"watercooler_migration_preflight", "watercooler_migrate_to_memory_backend"}

    # Profile gate: if memory tools are not available, return empty set.
    if not _memory_tools_available(runtime):
        return set()

    if runtime.surface == "hosted_full":
        # Hosted full gets all memory tools (authorizer controls access).
        return set(REMOTE_CAPABLE_MEMORY_TOOL_NAMES) - migration_tools

    if runtime.surface == "hosted_premium":
        result = set(REMOTE_CAPABLE_MEMORY_TOOL_NAMES) - migration_tools
        # Exclude LeanRAG tool unless the profile supports it.
        if not _leanrag_tools_available(runtime):
            result.discard("watercooler_leanrag_run_pipeline")
        return result

    if runtime.surface == "local_hybrid":
        # Hybrid: derive everything from capability resolution.
        # Tools whose capability resolves to "local" are registered locally.
        # Tools whose capability resolves to "remote" are NOT registered
        # locally (they'll be mounted from the premium proxy instead).
        # Tools whose capability resolves to "disabled" are excluded entirely.
        profile = runtime.capability_profile
        from .capabilities import tool_capability
        result: set[str] = set()
        for name in (REMOTE_CAPABLE_MEMORY_TOOL_NAMES - migration_tools):
            cap = tool_capability(name)
            target = profile.resolve_execution_target(
                cap, local_available=True, remote_available=runtime.premium_client is not None
            )
            if target == "local":
                result.add(name)
            # "remote" and "disabled" are excluded from local registration
        return result

    # local_full: register all memory tools.
    return set(REMOTE_CAPABLE_MEMORY_TOOL_NAMES) - migration_tools


def migration_tools_for_surface(runtime: ToolRuntime) -> set[str]:
    """Return the migration tool names that should be registered locally.

    Migration tools are local-only maintenance operations and must NOT
    appear on any hosted surface (full or premium).
    """
    if runtime.surface in ("hosted_premium", "hosted_full"):
        return set()

    if runtime.surface == "local_hybrid":
        # Hybrid: migration tools are disabled by default.
        profile = runtime.capability_profile
        target = profile.resolve_execution_target(
            "memory_migration",
            local_available=True,
            remote_available=runtime.premium_client is not None,
        )
        if target == "local":
            return {"watercooler_migration_preflight", "watercooler_migrate_to_memory_backend"}
        return set()

    # local_full
    return {"watercooler_migration_preflight", "watercooler_migrate_to_memory_backend"}


def mountable_remote_tools_for_hybrid(runtime: ToolRuntime) -> set[str]:
    """Return the tool names that should be mounted from the premium proxy.

    Only applies to ``local_hybrid`` surfaces.  Derives eligibility from
    capability resolution: any memory/migration tool whose capability
    resolves to ``remote`` and is NOT a mixed tool (mixed tools use local
    wrappers with per-call routing).
    """
    if runtime.surface != "local_hybrid":
        return set()

    profile = runtime.capability_profile
    result: set[str] = set()
    from .capabilities import tool_capability

    # All remote-capable memory + migration + daemon tools are candidates.
    # Mixed tools are excluded (they handle routing via wrappers).
    all_candidates = (REMOTE_CAPABLE_MEMORY_TOOL_NAMES | DAEMON_TOOL_NAMES) - MIXED_TOOL_NAMES
    for name in all_candidates:
        cap = tool_capability(name)
        target = profile.resolve_execution_target(
            cap, local_available=False, remote_available=True
        )
        if target == "remote":
            result.add(name)
    return result


# ---------------------------------------------------------------------------
# Server builder
# ---------------------------------------------------------------------------


def build_mcp_server(runtime: ToolRuntime) -> FastMCP:
    """Build a FastMCP server for the given runtime surface.

    Registers the appropriate subset of tools based on the surface type.
    """
    surface = runtime.surface
    name_suffix = {
        "local_full": "",
        "local_hybrid": " (Hybrid)",
        "hosted_full": " (Hosted)",
        "hosted_premium": " (Premium)",
    }.get(surface, "")
    mcp = FastMCP(name=f"Watercooler Cloud{name_suffix}")

    # Resources (all surfaces)
    from .resources import register_resources
    register_resources(mcp)

    # Diagnostics (all surfaces)
    from .tools.diagnostic import register_diagnostic_tools
    register_diagnostic_tools(mcp)

    # Thread tools (all surfaces except hosted_premium)
    if surface != "hosted_premium":
        from .tools.thread_query import register_thread_query_tools
        from .tools.thread_write import register_thread_write_tools
        register_thread_query_tools(mcp)
        register_thread_write_tools(mcp)

    # Sync tools (local surfaces only — reindex must not leak to hosted)
    if surface in ("local_full", "local_hybrid"):
        from .tools.sync import register_sync_tools
        register_sync_tools(mcp)

    # Federation tools (all surfaces except hosted_premium)
    if surface != "hosted_premium":
        from .tools.federation import register_federation_tools
        register_federation_tools(mcp)

    # Role tools (all surfaces except hosted_premium)
    if surface != "hosted_premium":
        from .tools.roles import register_role_tools
        register_role_tools(mcp)

    # Daemon tools — registered on all surfaces EXCEPT local_hybrid when
    # daemon capabilities route to remote (they'll be mounted from proxy).
    _register_daemons_locally = True
    if surface == "local_hybrid":
        from .capabilities import tool_capability
        cap = tool_capability("watercooler_daemon_status")
        target = runtime.capability_profile.resolve_execution_target(
            cap, local_available=True, remote_available=runtime.premium_client is not None,
        )
        if target == "remote":
            _register_daemons_locally = False
    if _register_daemons_locally:
        from .tools.daemon import register_daemon_tools
        register_daemon_tools(mcp)

    # Graph tools (selected per surface)
    from .tools.graph import register_graph_tools
    graph_selected = graph_tools_for_surface(runtime)
    if graph_selected:
        register_graph_tools(mcp, selected=graph_selected, runtime=runtime)

    # Memory tools (selected per surface)
    from .tools.memory import register_memory_tools
    memory_selected = memory_tools_for_surface(runtime)
    if memory_selected:
        register_memory_tools(mcp, selected=memory_selected, runtime=runtime)

    # Migration tools (selected per surface)
    from .tools.migration import register_migration_tools
    migration_selected = migration_tools_for_surface(runtime)
    if migration_selected:
        register_migration_tools(mcp, selected=migration_selected, runtime=runtime)

    # Mount remote tools for hybrid surface
    if surface == "local_hybrid" and runtime.premium_client is not None:
        mount_names = mountable_remote_tools_for_hybrid(runtime)
        if mount_names:
            premium_proxy = runtime.premium_client.proxy_server()
            mcp.mount(premium_proxy, tool_names={n: n for n in mount_names})

    # Apply hosted capability authorization as a tool transformation.
    # This wraps every tool on hosted surfaces so that
    # authorizer.ensure(capability, user_id) runs before execution.
    if surface in ("hosted_full", "hosted_premium") and runtime.authorizer is not None:
        _apply_hosted_auth_transforms(mcp, runtime.authorizer)

    return mcp


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def build_default_local_runtime() -> ToolRuntime:
    """Build a ``local_full`` ToolRuntime with default capability profile."""
    return ToolRuntime(
        surface="local_full",
        capability_profile=CapabilityProfile(),
    )


def build_default_local_server() -> FastMCP:
    """Build a fully-featured local MCP server (drop-in for ``server.py``)."""
    return build_mcp_server(build_default_local_runtime())


def build_http_surfaces(
    *,
    authorizer: CapabilityAuthorizer | None = None,
    deployment_availability: DeploymentAvailability | None = None,
) -> tuple[FastMCP, FastMCP]:
    """Build the two hosted HTTP surfaces.

    Args:
        authorizer: Optional capability authorizer for hosted surfaces.
        deployment_availability: Optional deployment profile resolved at
            startup.  When provided, tool selection is gated by the
            effective profile (e.g. memory tools require t2+).

    Returns:
        (hosted_full_mcp, hosted_premium_mcp) tuple.
    """
    hosted_full_rt = ToolRuntime(
        surface="hosted_full",
        capability_profile=CapabilityProfile(),
        authorizer=authorizer,
        deployment_availability=deployment_availability,
    )
    hosted_premium_rt = ToolRuntime(
        surface="hosted_premium",
        capability_profile=CapabilityProfile(),
        authorizer=authorizer,
        deployment_availability=deployment_availability,
    )
    return build_mcp_server(hosted_full_rt), build_mcp_server(hosted_premium_rt)
