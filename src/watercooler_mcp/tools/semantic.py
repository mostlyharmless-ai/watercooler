"""Plan v20 Phase 8: hosted semantic T1 tool.

Registers one tool on the hosted FastMCP surface:

- ``watercooler_semantic`` — hosted T1 FalkorDB embedding operations,
  selected by ``action``: ``upsert``, ``list``, or ``delete``.

PR3b consolidation: the former ``watercooler_semantic_upsert_embedding`` /
``watercooler_semantic_list_embeddings`` / ``watercooler_semantic_delete_embedding``
tools were folded into ``watercooler_semantic(action=...)``; the old names
forward via deprecation aliases (see ``watercooler_mcp/aliases.py``).

All actions run synchronously against the hosted T1 FalkorDB; there is no
queue between the tool and the graph.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from fastmcp import FastMCP
from mcp.types import TextContent
from fastmcp.tools.tool import ToolResult

from ..hosted_semantic import delete_embedding, list_embeddings_t1, upsert_embedding

logger = logging.getLogger(__name__)


def _result(payload: dict) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text=json.dumps(payload))])


def register_semantic_tools(mcp: FastMCP) -> None:
    """Register the hosted semantic T1 tool on *mcp*."""

    @mcp.tool(
        name="watercooler_semantic",
        description=(
            "Hosted T1 FalkorDB (<org>_<repo>_t1) embedding operations, "
            "selected by `action`:\n"
            "- action='upsert': upsert an entry embedding. Requires entry_id, "
            "topic, group_id, embedding; optional role/entry_type/agent/"
            "timestamp are materialised on the Entry node for hosted "
            "semantic-search filtering.\n"
            "- action='list': paginated dump of every Entry node — used by "
            "`watercooler migrate t1 --to stdio`. Requires group_id; "
            "cursor/limit page the results (`next_cursor` is empty when "
            "exhausted).\n"
            "- action='delete': delete an entry embedding. Requires "
            "entry_id, group_id.\n"
            "Intended for hybrid-mode clients routing T1 embedding work to "
            "the hosted side. Cross-tenant isolation: the caller-supplied "
            "group_id is advisory — the authoritative scope is derived from "
            "the hosted X-Repo request context."
        ),
    )
    async def semantic(
        action: str,
        group_id: str = "",
        entry_id: str = "",
        topic: str = "",
        embedding: Optional[List[float]] = None,
        role: str = "",
        entry_type: str = "",
        agent: str = "",
        timestamp: str = "",
        cursor: str = "",
        limit: int = 200,
    ) -> ToolResult:
        """Hosted T1 embedding upsert / list / delete (Plan v20 Phase 8)."""
        normalized = (action or "").strip().lower()
        if normalized not in ("upsert", "list", "delete"):
            return _result({
                "success": False,
                "error": (
                    f"unknown action: {action!r}; expected "
                    f"'upsert', 'list', or 'delete'"
                ),
            })

        # Cross-tenant write guard — the caller-supplied group_id is advisory;
        # the authoritative target is derived from the hosted request context
        # (X-Repo header via premium auth middleware). PR #654.
        project_group_id, scope_err = _scope_group_id_to_http_ctx(group_id)
        if scope_err is not None:
            return _result(scope_err)
        database = _derive_database(project_group_id)

        if normalized == "upsert":
            if embedding is None:
                return _result({
                    "success": False,
                    "error": "embedding is required for action='upsert'",
                })
            result = upsert_embedding(
                database=database,
                entry_id=entry_id,
                topic=topic,
                embedding=embedding,
                group_id=project_group_id,
                role=role,
                entry_type=entry_type,
                agent=agent,
                timestamp=timestamp,
            )
        elif normalized == "list":
            result = list_embeddings_t1(
                database=database,
                group_id=project_group_id,
                cursor=cursor,
                limit=limit,
            )
        else:  # delete
            result = delete_embedding(
                database=database,
                entry_id=entry_id,
                group_id=project_group_id,
                topic=topic,
            )
        return _result(result)


def _scope_group_id_to_http_ctx(
    caller_group_id: str,
) -> tuple[str, dict | None]:
    """Resolve the authoritative project_group_id for a hosted T1 write.

    Returns ``(scoped_group_id, error_dict_or_None)``.

    Move 1 of the security consolidation plan: this function delegates
    to :func:`watercooler_mcp.auth.scope.resolve_scope_or_off_hosted`,
    preserving the existing tuple return shape so downstream callers
    work unchanged. Off-hosted (stdio / dev) still falls through to
    the caller's value to keep local development frictionless.

    Atomic single-lookup discipline: ``resolve_scope_or_off_hosted``
    reads each context-var at most once internally. Three outcomes:

    - returns ``None`` → off-hosted → use caller's value;
    - returns ``ResolvedScope`` → use auth-derived scope;
    - raises ``ScopeResolutionError`` → fail closed.

    No outer "is context absent?" probe is performed here, eliminating
    the asymmetric TOCTOU where the outer probe could observe absence
    while a context set in between was missed by a separate inner
    check. The new path resolves the off-hosted decision and the
    auth-derived scope from the same atomic context read.

    PR #654 background: prevents a premium user from authenticating
    with token-for-tenant-A and passing ``group_id=tenant_B`` to write
    into tenant B's T1 graph.
    """
    from ..auth.scope import (
        ScopeResolutionError,
        resolve_scope_or_off_hosted,
        warn_caller_hint_mismatch,
    )

    try:
        scope = resolve_scope_or_off_hosted()
    except ScopeResolutionError as e:
        # Hosted but incomplete context — fail closed.
        return "", {"success": False, "error": f"scope_resolution_failed: {e}"}

    if scope is None:
        # Off-hosted (no HTTP nor worker context at the moment of
        # the atomic lookup). Caller's value wins; preserves
        # stdio/dev-mode fallback from the original implementation.
        return _strip_t1_suffix(caller_group_id), None

    caller_stripped = _strip_t1_suffix(caller_group_id)
    try:
        warn_caller_hint_mismatch(
            scope=scope, caller_supplied=caller_stripped, field="group_id"
        )
    except ScopeResolutionError as e:
        # Strict mode (``WATERCOOLER_STRICT_SCOPE=1``) escalates
        # caller-hint mismatches from a WARN log to a raised
        # ``ScopeResolutionError``. The wrapper converts that into the
        # established error-tuple shape so the MCP client receives a
        # consistent ``scope_resolution_failed`` response in both
        # warn-mode and strict-mode rather than an uncaught exception
        # in strict mode only.
        return "", {"success": False, "error": f"scope_resolution_failed: {e}"}
    return scope.project_group_id, None


def _derive_database(group_id: str) -> str:
    """Map a caller-supplied project_group_id to the canonical T1 DB name.

    Accepts either ``<org>_<repo>`` or the raw ``<org>_<repo>_t1`` name (in
    which case it passes through unchanged).
    """
    if not group_id:
        return ""
    if group_id.endswith("_t1"):
        return group_id
    return f"{group_id}_t1"


def _strip_t1_suffix(group_id: str) -> str:
    """Return the project_group_id form (no ``_t1`` suffix) for node properties."""
    if not group_id:
        return ""
    if group_id.endswith("_t1"):
        return group_id[:-3]
    return group_id
