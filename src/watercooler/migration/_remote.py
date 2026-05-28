"""Hosted-MCP wrapper for migration.

Reuses ``watercooler_mcp.premium_client.PremiumToolClient`` so the
migration uses the same auth + URL + headers as Claude Code's hybrid
mode. No separate creds, no subprocess shenanigans — same plumbing the
production MCP client already trusts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)


class MigrationTransportError(RuntimeError):
    """Raised when a remote enumeration / call fails partway through.

    Signals "the iterator did NOT reach end-of-stream cleanly." Callers
    catch this to record a real error in MigrationSummary instead of
    treating partial truncation as legitimate end-of-stream — which
    would silently checkpoint a partial pull.
    """


@dataclass
class RemoteEntry:
    entry_id: str
    thread_topic: str
    embedding: list[float]
    group_id: str = ""
    role: str = ""
    entry_type: str = ""
    agent: str = ""
    timestamp: str = ""


def build_premium_client():
    """Construct a ``PremiumToolClient`` using the user's existing config.

    Raises a clear error if the user is not configured for hybrid mode.
    """
    try:
        from watercooler_mcp.premium_client import PremiumToolClient
        from watercooler_mcp.config import get_mcp_transport_config
    except ImportError as e:
        raise RuntimeError(
            "watercooler_mcp not available — install with `pip install -e .` "
            "or check the package is on PYTHONPATH."
        ) from e

    transport = get_mcp_transport_config()
    if not transport.get("url"):
        raise RuntimeError(
            "Hybrid migration requires [mcp].url in config.toml or "
            "WATERCOOLER_MCP_URL env var. Currently your config has no "
            "hosted endpoint configured. Either set one or use stdio mode."
        )
    return PremiumToolClient.from_transport_config(transport)


def call_remote_tool(client, tool_name: str, arguments: Dict[str, Any]) -> str:
    """Synchronous wrapper around the async ``call_tool_text``.

    Returns the raw text result (typically JSON). Caller parses.

    Sync-only entry point — ``asyncio.run()`` cannot be invoked inside a
    running event loop. If you call this from async code (e.g. an
    async test, a Jupyter cell, an embedded migration in a webapp),
    you will get a clear error pointing you at the underlying coroutine.
    """
    coro = client.call_tool_text(tool_name, arguments)
    try:
        return asyncio.run(coro)
    except RuntimeError as e:
        # asyncio.run() raises this when invoked inside a running loop.
        # Surface a useful pointer instead of letting the cryptic
        # default propagate.
        if "asyncio.run() cannot" in str(e) or "running event loop" in str(e):
            raise RuntimeError(
                "call_remote_tool was invoked inside a running event loop. "
                "From async code, await client.call_tool_text(name, arguments) "
                "directly instead of going through this sync wrapper."
            ) from e
        raise


def upsert_remote_embedding(
    client,
    *,
    target_group_id: str,
    entry: RemoteEntry,
) -> Dict[str, Any]:
    """Push one entry to hosted T1 via the canonical MCP tool.

    Returns the parsed JSON response. Successful upsert returns
    ``{"success": True, "status": "upserted", ...}``; errors return
    ``{"error": "...", ...}``.
    """
    raw = call_remote_tool(
        client,
        "watercooler_semantic",
        {
            "action": "upsert",
            "entry_id": entry.entry_id,
            "topic": entry.thread_topic,
            "group_id": target_group_id,
            "embedding": entry.embedding,
            "role": entry.role,
            "entry_type": entry.entry_type,
            "agent": entry.agent,
            "timestamp": entry.timestamp,
        },
    )
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"error": "unparseable_response", "raw": raw[:200]}


def list_remote_embeddings(
    client,
    *,
    target_group_id: str,
    page_size: int = 200,
) -> Iterator[RemoteEntry]:
    """Yield every Entry node from hosted T1 via the list-embeddings tool.

    The hosted tool ``watercooler_semantic`` (``action="list"``) is the
    server-side enumeration primitive added for migration. Pagination is
    cursor-style (offset by entry_id) so concurrent writes during the
    pull are handled deterministically.

    The server derives the FalkorDB database name from ``group_id`` via
    ``hosted_semantic._derive_database``; the migration client doesn't
    pass a database name. Cross-tenant isolation is enforced by
    ``_scope_group_id_to_http_ctx``.
    """
    cursor = ""
    while True:
        request_cursor = cursor  # what we're SENDING this iteration
        raw = call_remote_tool(
            client,
            "watercooler_semantic",
            {
                "action": "list",
                "group_id": target_group_id,
                "cursor": request_cursor,
                "limit": page_size,
            },
        )
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            # Generator MUST signal truncation, not silently end. Pre-fix:
            # caller's for-loop just stopped, summary.errored stayed at 0,
            # checkpoint recorded a partial pull, user re-runs thinking
            # they're done. Real data-loss path.
            logger.warning("Unparseable list-embeddings response: %s", raw[:200])
            raise MigrationTransportError(
                f"Unparseable list_embeddings response after cursor={request_cursor!r}: {e}"
            ) from e
        if "error" in payload:
            logger.warning("list-embeddings error: %s", payload["error"])
            raise MigrationTransportError(
                f"list_embeddings server error after cursor={request_cursor!r}: {payload['error']}"
            )
        # Defensive: detect non-advancing cursor BEFORE yielding any rows
        # from this page. If the server returns next_cursor equal to the
        # cursor we just sent (pagination bug, replayed page), we'd loop
        # forever AND re-yield the same page's rows. Bail BEFORE yielding
        # so the caller doesn't see duplicates from the broken page.
        next_cursor = payload.get("next_cursor") or ""
        if next_cursor and next_cursor == request_cursor:
            raise MigrationTransportError(
                f"list_embeddings cursor did not advance "
                f"(server returned cursor={next_cursor!r} unchanged from request); "
                "aborting to avoid infinite loop and duplicate rows."
            )
        items = payload.get("entries") or []
        # Don't early-return on empty `entries`. A page can be all-null-embedding
        # rows (filtered out server-side) yet still carry a non-empty
        # ``next_cursor`` because the SERVER tracks pagination via the raw
        # row count (see hosted_semantic.list_embeddings_t1). Returning here
        # would silently drop everything past the first all-null page.
        for item in items:
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                continue
            entry_id = str(item.get("entry_id") or "")
            # Per-row float guard. Mirrors the local counterpart
            # `list_local_entries`: a single bad embedding (e.g. server
            # returned ["abc", 0.1] from a corrupt row) MUST NOT kill the
            # whole generator — that would silently truncate the migration
            # mid-page on one data-quality issue. Skip the bad row, log,
            # continue iterating.
            try:
                emb_floats = [float(x) for x in embedding]
            except (TypeError, ValueError) as e:
                logger.warning(
                    "Skipping %s: malformed embedding from server (%s)",
                    entry_id or "<no-id>", e,
                )
                continue
            yield RemoteEntry(
                entry_id=entry_id,
                thread_topic=str(item.get("thread_topic") or ""),
                embedding=emb_floats,
                group_id=str(item.get("group_id") or target_group_id),
                role=str(item.get("role") or ""),
                entry_type=str(item.get("entry_type") or ""),
                agent=str(item.get("agent") or ""),
                timestamp=str(item.get("timestamp") or ""),
            )
        # Advance to the next page. The non-advancing-cursor check
        # above already fired before yielding, so reaching here means
        # the cursor genuinely advanced (or terminated).
        cursor = next_cursor
        if not cursor:
            return
