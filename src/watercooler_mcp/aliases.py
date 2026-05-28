"""Non-discoverable alias forwarding for retired MCP tool names.

During the tool-surface consolidation (thread
``mcp-tool-surface-consolidation-2026-05``) tools are renamed and collapsed.
To keep legacy callers — older skills, external clients — working for one
deprecation cycle, every retired tool name is registered here as an *alias*:
a forwarder mapping the legacy name to its canonical replacement.

Aliases are deliberately **non-discoverable**. They are NOT registered as
FastMCP tools, so they never appear in ``tools/list`` or in the output of
``.claude/skills/watercooler-tool-audit/scripts/extract_tools.py``. They are
resolved by :class:`AliasForwardingMiddleware`, which intercepts
``tools/call`` *before* tool dispatch and capability authorization, rewrites
the request to the canonical tool name, and emits a deprecation warning.

PR1a ships this machinery with an **empty registry**. Later consolidation PRs
add entries as they retire names; each alias is removed in the minor release
following the PR that introduced it.

Adding an alias (for future PRs)::

    TOOL_ALIASES["watercooler_annotate"] = ToolAlias(
        canonical="watercooler_annotations",
        inject_args={"action": "add"},
        since="PR3",
    )
"""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolAlias:
    """A forward from a retired tool name to its canonical replacement.

    Attributes:
        canonical: The current tool name the legacy call is forwarded to.
        rename_args: Maps a legacy argument name to its canonical counterpart
            when a collapse renamed parameters (e.g. ``start_index`` →
            ``index``). The legacy key is removed and its value re-bound to
            the canonical key; a caller-supplied canonical key always wins.
            Applied before ``inject_args``.
        inject_args: Arguments merged into the call when the canonical tool
            needs a parameter the legacy tool did not expose (e.g. an
            ``action=`` selector for a collapsed tool). The injected value
            **overrides** any caller-supplied value of the same key: a
            collapsed-tool selector defines which operation the retired name
            maps to, and the retired name (which never exposed that key) must
            keep its original meaning even if a stale call passes it.
        guard: Optional precheck on the call arguments (the *original*,
            pre-rename arguments). Returns an error ``dict`` to short-circuit
            (the middleware responds with that JSON and does NOT forward), or
            ``None`` to proceed. Use it to preserve a stricter contract the
            retired tool had that the canonical tool does not — e.g. a
            required argument that the unified tool treats as optional.
        since: Short note of which PR introduced the alias, surfaced in the
            deprecation message.
    """

    canonical: str
    rename_args: Mapping[str, str] = field(default_factory=dict)
    inject_args: Mapping[str, Any] = field(default_factory=dict)
    guard: Optional[Callable[[Mapping[str, Any]], Optional[dict]]] = None
    since: str = ""


# Populated as consolidation PRs retire names. See the module docstring.
TOOL_ALIASES: dict[str, ToolAlias] = {
    # PR3b — watercooler_role_details folded into watercooler_roles(role=...).
    # The legacy `role` arg maps straight through. role_details had a
    # stricter contract than the catalog tool — a missing `role` was an
    # error, not a successful catalog dump — so the guard preserves that
    # for callers of the retired name (PR3b review follow-up).
    "watercooler_role_details": ToolAlias(
        canonical="watercooler_roles",
        guard=lambda args: (
            {"error": "role_required"}
            if not str(args.get("role", "") or "").strip()
            else None
        ),
        since="PR3b",
    ),
    # PR3b — the three semantic_* tools folded into watercooler_semantic;
    # the action= selector the legacy tools lacked is injected per alias.
    "watercooler_semantic_upsert_embedding": ToolAlias(
        canonical="watercooler_semantic",
        inject_args={"action": "upsert"},
        since="PR3b",
    ),
    "watercooler_semantic_list_embeddings": ToolAlias(
        canonical="watercooler_semantic",
        inject_args={"action": "list"},
        since="PR3b",
    ),
    "watercooler_semantic_delete_embedding": ToolAlias(
        canonical="watercooler_semantic",
        inject_args={"action": "delete"},
        since="PR3b",
    ),
    # PR3b — annotate / get_annotations / remove_annotation folded into
    # watercooler_annotations; the action= selector is injected per alias.
    "watercooler_annotate": ToolAlias(
        canonical="watercooler_annotations",
        inject_args={"action": "add"},
        since="PR3b",
    ),
    "watercooler_get_annotations": ToolAlias(
        canonical="watercooler_annotations",
        inject_args={"action": "get"},
        since="PR3b",
    ),
    "watercooler_remove_annotation": ToolAlias(
        canonical="watercooler_annotations",
        inject_args={"action": "remove"},
        since="PR3b",
    ),
    # PR3c — baseline_graph_stats / baseline_sync_status folded into
    # watercooler_baseline_graph; the scope= selector is injected per alias.
    "watercooler_baseline_graph_stats": ToolAlias(
        canonical="watercooler_baseline_graph",
        inject_args={"scope": "stats"},
        since="PR3c",
    ),
    "watercooler_baseline_sync_status": ToolAlias(
        canonical="watercooler_baseline_graph",
        inject_args={"scope": "sync"},
        since="PR3c",
    ),
    # PR3c — get_entity_edge / get_entry_provenance folded into
    # watercooler_graph_trace, which dispatches on which id is supplied
    # (uuid → edge; entry_id/episode_uuid → provenance). The legacy args
    # map straight through, so no inject_args is needed.
    "watercooler_get_entity_edge": ToolAlias(
        canonical="watercooler_graph_trace", since="PR3c"
    ),
    "watercooler_get_entry_provenance": ToolAlias(
        canonical="watercooler_graph_trace", since="PR3c"
    ),
    # PR3c — get_thread_entry_range folded into watercooler_get_thread_entry,
    # which enters range mode when to_index is set. The legacy range args are
    # renamed (start_index → index, end_index → to_index); the guard rejects
    # open-ended legacy calls (no end_index) — the unified tool, by design,
    # does not express an open-ended range.
    "watercooler_get_thread_entry_range": ToolAlias(
        canonical="watercooler_get_thread_entry",
        rename_args={"start_index": "index", "end_index": "to_index"},
        guard=lambda args: (
            {
                "error": "end_index_required",
                "detail": (
                    "watercooler_get_thread_entry_range is retired; its "
                    "replacement watercooler_get_thread_entry needs an "
                    "explicit to_index for ranges. Pass end_index, or use "
                    "watercooler_read_thread for an open-ended read."
                ),
            }
            if args.get("end_index") is None
            else None
        ),
        since="PR3c",
    ),
    # PR4a — migration_preflight folded into watercooler_bulk_index, which runs
    # a migration prerequisite check when preflight_only=True. The legacy
    # code_path/backend args map straight through.
    "watercooler_migration_preflight": ToolAlias(
        canonical="watercooler_bulk_index",
        inject_args={"preflight_only": True},
        since="PR4a",
    ),
    # PR4a — leanrag_run_pipeline folded into watercooler_bulk_index, which runs
    # the LeanRAG clustering pipeline when run_pipeline=True. The legacy
    # group_id/start_date/end_date/dry_run/incremental args map straight through.
    "watercooler_leanrag_run_pipeline": ToolAlias(
        canonical="watercooler_bulk_index",
        inject_args={"run_pipeline": True},
        since="PR4a",
    ),
    # PR4a — migrate_to_memory_backend retired; watercooler_bulk_index is the
    # canonical, superseding ingest path (idempotent queue with retry). The two
    # are not arg-compatible: bulk_index has no dry-run preview, checkpoint/
    # resume, or chunk-size controls. topics maps to threads; the guard rejects
    # any migrate-specific usage with a clear pointer rather than letting a
    # preview-intent call silently queue real work or fail on an unknown kwarg.
    "watercooler_migrate_to_memory_backend": ToolAlias(
        canonical="watercooler_bulk_index",
        rename_args={"topics": "threads"},
        guard=lambda args: (
            {
                "error": "migrate_to_memory_backend_retired",
                "detail": (
                    "watercooler_migrate_to_memory_backend is retired; "
                    "watercooler_bulk_index supersedes it with an idempotent "
                    "queue (re-running is safe — inspect progress with "
                    "watercooler_memory_task_status). bulk_index has no "
                    "dry-run preview, checkpoint/resume, or chunk-size "
                    "controls. Call watercooler_bulk_index directly "
                    "(dry_run=False, no migrate-only args)."
                ),
            }
            if (
                args.get("dry_run", True)
                or any(
                    key in args
                    for key in (
                        "resume",
                        "force_new_migration",
                        "rechunk",
                        "skip_closed",
                        "chunk_max_tokens",
                        "chunk_overlap",
                    )
                )
            )
            else None
        ),
        since="PR4a",
    ),
    # PR4b — watercooler_reindex (a pre-graph-first markdown thread index)
    # retired; the graph-first watercooler_list_threads supersedes it and
    # already offers format="json"/"markdown". reindex took no arguments, so
    # the bare forward maps straight through.
    "watercooler_reindex": ToolAlias(
        canonical="watercooler_list_threads",
        since="PR4b",
    ),
    # PR4b — watercooler_whoami folded into watercooler_health, which returns
    # the resolved identity + write-readiness when detail="identity".
    "watercooler_whoami": ToolAlias(
        canonical="watercooler_health",
        inject_args={"detail": "identity"},
        since="PR4b",
    ),
    # PR5 D1 — watercooler_acknowledge_finding folded into
    # watercooler_daemon_findings(action="acknowledge"). The legacy daemon_name
    # arg maps to the findings tool's daemon arg; finding_id/finding_ids pass
    # straight through.
    "watercooler_acknowledge_finding": ToolAlias(
        canonical="watercooler_daemon_findings",
        rename_args={"daemon_name": "daemon"},
        inject_args={"action": "acknowledge"},
        since="PR5",
    ),
    # PR6 D4 — watercooler_find_similar folded into
    # watercooler_search(seed_entry_id=). entry_id → seed_entry_id;
    # similarity_threshold → semantic_threshold. use_embeddings passes straight
    # through — search's seeded mode preserves the heuristic fallback.
    "watercooler_find_similar": ToolAlias(
        canonical="watercooler_search",
        rename_args={
            "entry_id": "seed_entry_id",
            "similarity_threshold": "semantic_threshold",
        },
        since="PR6",
    ),
    # PR6 D5 — watercooler_federated_search folded into
    # watercooler_search(federated=True). query/code_path/namespaces/limit map
    # straight through; federated=True forces the federated path even when no
    # namespaces filter is supplied.
    "watercooler_federated_search": ToolAlias(
        canonical="watercooler_search",
        inject_args={"federated": True},
        since="PR6",
    ),
}


def resolve_alias(name: str) -> ToolAlias | None:
    """Return the :class:`ToolAlias` for *name*, or None if it is not aliased."""
    return TOOL_ALIASES.get(name)


def _warn_deprecated(legacy: str, alias: ToolAlias) -> None:
    """Emit a ``DeprecationWarning`` and an observability log for an alias hit."""
    since = f" (retired in {alias.since})" if alias.since else ""
    message = (
        f"MCP tool '{legacy}' is a deprecated alias for '{alias.canonical}'"
        f"{since}; update callers — the alias is removed in the next minor "
        f"release."
    )
    warnings.warn(message, DeprecationWarning, stacklevel=2)
    try:
        from .observability import log_warning

        log_warning(
            message,
            deprecated_tool=legacy,
            canonical_tool=alias.canonical,
        )
    except Exception:  # pragma: no cover - observability is best-effort
        logger.warning(message)


def _guard_error_result(payload: dict) -> ToolResult:
    """Wrap a guard's short-circuit error payload as a tool result."""
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))]
    )


class AliasForwardingMiddleware(Middleware):
    """Rewrites a ``tools/call`` for a retired tool name to its canonical tool.

    Installed ahead of the capability-authorization middleware so that
    downstream stages — capability resolution, routing, dispatch — only ever
    see canonical tool names. A call for a non-aliased name passes straight
    through untouched.

    When an alias carries a ``guard``, the guard runs first: if it returns an
    error payload the call short-circuits with that error and is not
    forwarded — this preserves a stricter contract the retired tool had.
    """

    async def on_call_tool(self, context, call_next):
        params = context.message
        alias = TOOL_ALIASES.get(params.name)
        if alias is not None:
            _warn_deprecated(params.name, alias)
            if alias.guard is not None:
                error = alias.guard(params.arguments or {})
                if error is not None:
                    return _guard_error_result(error)
            params.name = alias.canonical
            if alias.rename_args or alias.inject_args:
                merged = dict(params.arguments or {})
                for legacy, canonical in alias.rename_args.items():
                    if legacy not in merged:
                        continue
                    value = merged.pop(legacy)
                    merged.setdefault(canonical, value)
                # inject_args override caller values — the injected key is the
                # collapsed-tool selector that defines the retired name's
                # operation; a stale call passing it must not redirect it.
                for key, value in alias.inject_args.items():
                    merged[key] = value
                params.arguments = merged
        return await call_next(context)


def apply_alias_forwarding(mcp) -> None:
    """Install :class:`AliasForwardingMiddleware` on *mcp*.

    Call this immediately after the ``FastMCP`` instance is created and before
    any capability/authorization middleware is added — middleware runs in
    registration order, so the alias rewrite must be registered first.
    """
    mcp.add_middleware(AliasForwardingMiddleware())
