"""Shared server factory for all MCP surfaces.

Centralises tool selection and FastMCP assembly so that ``server.py``
(CLI / stdio / proxy), ``server_http.py`` (hosted HTTP), and tests
all share one authoritative code path.

Surface ↔ config.toml `transport` mapping:
  - transport="stdio"  → surface="local_full"   (``build_default_local_server``)
  - transport="hybrid" → surface="local_hybrid" (``_run_hybrid`` in server.py)
  - transport="proxy"  → no surface; server.py uses ``create_proxy()`` directly
  - transport="http"   → bifurcates on ``is_hosted_mode()``:
      - ``is_hosted_mode()=True``  → surface="hosted_full" / "hosted_premium"
                                     (``build_http_surfaces``, Railway deployment)
      - ``is_hosted_mode()=False`` → surface="local_full" (self-hosted HTTP:
                                     the module-level ``mcp = build_default_local_server()``
                                     served over HTTP instead of stdio — server.py:528-542)

The config.toml key is called ``transport`` for historical reasons but
actually selects an execution-routing mode, not the agent↔mcp pipe (which
is always stdio). Consolidating ``local_full`` + ``local_hybrid`` into a
single surface is tracked as a future-unification proposal — see the
repository issues labeled ``terminology`` / ``refactor``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastmcp import FastMCP

from .capabilities import (
    DAEMON_TOOL_NAMES,
    GRAPH_TOOL_NAMES,
    MIXED_TOOL_NAMES,
    PREMIUM_GRAPH_TOOL_NAMES,
    REMOTE_CAPABLE_MEMORY_TOOL_NAMES,
    CapabilityProfile,
)
from .tool_runtime import ToolRuntime

if TYPE_CHECKING:
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

    Fail-closed behaviour: any tool registered with FastMCP but missing
    from ``_TOOL_CAPABILITY_MAP`` is refused at runtime rather than
    silently bypassing the grant check. The capability enumeration test
    catches this drift at build time; this middleware is runtime
    defense-in-depth for the same failure mode.
    """
    import json as _json

    from fastmcp.server.middleware import Middleware
    from mcp.types import TextContent
    from fastmcp.tools.tool import ToolResult

    from .capabilities import tool_capability
    from .context import get_effective_context

    class _CapabilityAuthMiddleware(Middleware):
        async def on_call_tool(self, context, call_next):
            params = context.message
            tool_name = params.name
            arguments = params.arguments or {}
            logger.warning("CAPABILITY_MW: entered on_call_tool for %s", tool_name)

            # Resolve which capability this tool call requires.
            try:
                cap = tool_capability(tool_name, arguments)
            except ValueError:
                # ``tool_capability`` raises ValueError for both "tool not in
                # _TOOL_CAPABILITY_MAP" and "tool name does not exist". The
                # first is a server config bug that must fail-closed; the
                # second is a client typo that must surface as FastMCP's
                # normal unknown-tool error (preserving protocol semantics).
                # Disambiguate by asking the server whether the tool is
                # registered.
                try:
                    registered = await mcp.get_tool(tool_name) is not None
                except Exception:  # pragma: no cover - defensive
                    registered = False

                if not registered:
                    logger.debug(
                        "CAPABILITY_MW: %s not registered — delegating to "
                        "FastMCP for unknown-tool response",
                        tool_name,
                    )
                    return await call_next(context)

                logger.error(
                    "CAPABILITY_MW: denying %s — registered tool is missing "
                    "from _TOOL_CAPABILITY_MAP (hosted fail-closed)",
                    tool_name,
                )
                return ToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=_json.dumps(
                                {
                                    "error": "capability_not_registered",
                                    "tool": tool_name,
                                    "message": (
                                        f"Tool {tool_name!r} has no capability "
                                        f"mapping and cannot be authorized on "
                                        f"hosted surfaces. This is a server "
                                        f"configuration error; add the tool to "
                                        f"_TOOL_CAPABILITY_MAP."
                                    ),
                                }
                            ),
                        )
                    ]
                )

            # Resolve user identity and preloaded capabilities from context.
            eff_ctx = get_effective_context()
            user_id = eff_ctx.user_id if eff_ctx else ""
            preloaded_caps = eff_ctx.capabilities if eff_ctx else None
            logger.warning(
                "CAPABILITY_MW: tool=%s cap=%s user=%s context=%s caps=%s",
                tool_name,
                cap,
                user_id or "NONE",
                "present" if eff_ctx else "MISSING",
                len(preloaded_caps) if preloaded_caps else "NONE",
            )

            # Check authorization (uses preloaded caps from credentials
            # response to avoid a second control-plane round-trip).
            # Use the async variant so any cache-miss fallback fetch
            # runs in a worker thread instead of blocking the event
            # loop on a 10s urlopen timeout.  See issue #521.
            denial = await authorizer.ensure_async(
                cap, user_id, preloaded_capabilities=preloaded_caps
            )
            logger.debug(
                "CAPABILITY_MW: ensure() returned denial=%s", denial is not None
            )
            if denial is not None:
                # Return the denial as a text tool result.
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


def _premium_daemon_pinned_local() -> bool:
    """True iff a *premium* daemon has an explicit ``route="local"``.

    Non-premium daemons (``thread_auditor``, ``sync_guard``,
    ``decision_detector``, ``decision_extractor``, ``content_scout``,
    ``content_refiner``) are irrelevant here — they always run local in
    hybrid mode and are not mirrored as hosted tools, so they don't
    drive daemon-tool mounting.  Only premium daemons (which the
    proxy also mounts) create a shadowing risk that requires overriding
    the default ``daemon_observe`` route.

    Scoped to ``_PREMIUM_DAEMONS`` rather than iterating every
    DaemonsConfig field so nearby fields like ``compound`` (a callable
    artifact hook, not a daemon) can't accidentally satisfy the
    predicate.
    """
    try:
        from watercooler.config_facade import config

        from .daemons import _PREMIUM_DAEMONS, daemon_execution_policy

        full = config.full()
        daemons_cfg = full.mcp.daemons
        if not getattr(daemons_cfg, "enabled", False):
            return False
        transport = getattr(full.mcp, "transport", "stdio")
        for name in _PREMIUM_DAEMONS:
            sub = getattr(daemons_cfg, name, None)
            if sub is None:
                continue
            decision = daemon_execution_policy(
                name, sub, transport, in_hosted_coordinator=False
            )
            if decision == "local":
                return True
    except Exception:  # noqa: BLE001 — facade unavailable is a non-fatal hint
        return False
    return False


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
_HOSTED_EXCLUDED_GRAPH_TOOLS: frozenset[str] = frozenset(
    {
        "watercooler_graph_enrich",
        "watercooler_graph_project",
        "watercooler_graph_recover",
        "watercooler_sync_repair",
        "watercooler_reindex",
    }
)


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
    migration_tools = {
        "watercooler_migration_preflight",
        "watercooler_migrate_to_memory_backend",
    }

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
        for name in REMOTE_CAPABLE_MEMORY_TOOL_NAMES - migration_tools:
            cap = tool_capability(name)
            target = profile.resolve_execution_target(
                cap,
                local_available=True,
                remote_available=runtime.premium_client is not None,
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
            return {
                "watercooler_migration_preflight",
                "watercooler_migrate_to_memory_backend",
            }
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
    all_candidates = (
        REMOTE_CAPABLE_MEMORY_TOOL_NAMES | DAEMON_TOOL_NAMES
    ) - MIXED_TOOL_NAMES
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
    from .tools.diagnostic import register_diagnostic_tools, set_runtime as _set_diagnostic_runtime

    register_diagnostic_tools(mcp)
    _set_diagnostic_runtime(runtime)

    # Memory-sync runtime hook (Plan v20 Phase 1 scaffolding; used from Phase 5)
    # Round 18 (MEDIUM): set_runtime now flips both the T2 handoff flag
    # AND installs the T1 hybrid callbacks from the same snapshot of
    # runtime state, so the two guards can't end up mismatched.
    from .memory_sync import set_runtime as _set_memory_sync_runtime

    _set_memory_sync_runtime(runtime)

    # Thread tools (all surfaces except hosted_premium)
    if surface != "hosted_premium":
        from .tools.thread_query import register_thread_query_tools
        from .tools.thread_write import register_thread_write_tools
        from .tools.decisions import register_decisions_tools
        from .tools.annotations_xref import register_annotations_xref_tools

        register_thread_query_tools(mcp)
        register_thread_write_tools(mcp)
        register_decisions_tools(mcp)
        register_annotations_xref_tools(mcp)

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
    # Exception: in hybrid mode a user can pin an individual premium
    # daemon local via ``[mcp.daemons.<name>] route = "local"``.  When
    # that happens the local daemon has no tools to drive it if we skip
    # ``register_daemon_tools``, so mount them here regardless of the
    # capability-route resolution — AND suppress the proxy mount of
    # daemon tools further down (otherwise the local tool
    # implementations, which query ``get_daemon_runtime()``, would
    # silently shadow the remote premium daemons).  Operators wanting
    # visibility of hosted daemons alongside a locally-pinned one must
    # explicitly set ``[mcp.capability_routes] daemon_observe = "local"``
    # and accept that the local daemon tools do not surface hosted
    # daemons — merging both surfaces is a larger refactor.
    _register_daemons_locally = True
    _suppress_proxy_daemon_tools = False
    if surface == "local_hybrid":
        from .capabilities import tool_capability

        cap = tool_capability("watercooler_daemon_status")
        target = runtime.capability_profile.resolve_execution_target(
            cap,
            local_available=True,
            remote_available=runtime.premium_client is not None,
        )
        if target == "remote":
            if _premium_daemon_pinned_local():
                # Route stays "remote" for the capability, but a
                # specific daemon is pinned local — we must mount the
                # local tool surface for it and prevent the proxy from
                # mirroring the same names.
                _suppress_proxy_daemon_tools = True
            else:
                _register_daemons_locally = False
    if _register_daemons_locally:
        from .tools.daemon import register_daemon_tools

        register_daemon_tools(mcp)

    # Decision extractor admin tool (local-only — the extractor daemon
    # never runs in hosted scopes). Gated independently of
    # _register_daemons_locally so the tool remains available on
    # local_hybrid even when the observability daemon tools route to Railway.
    if surface in ("local_full", "local_hybrid"):
        from .tools.daemon import register_decision_extractor_admin_tools

        register_decision_extractor_admin_tools(mcp)

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

    # Semantic T1 tools — hosted surfaces only (Plan v20 Phase 8).
    if surface in ("hosted_full", "hosted_premium"):
        from .tools.semantic import register_semantic_tools

        register_semantic_tools(mcp)

    # Mount remote tools for hybrid surface
    if surface == "local_hybrid" and runtime.premium_client is not None:
        mount_names = mountable_remote_tools_for_hybrid(runtime)
        if _suppress_proxy_daemon_tools:
            # A premium daemon is pinned local; the local daemon tools
            # (registered above) would win FastMCP name resolution and
            # silently shadow the remote daemon observations.  Skip the
            # proxy mount of daemon tools instead.  Memory / migration
            # tools are unaffected.
            mount_names = mount_names - DAEMON_TOOL_NAMES
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
