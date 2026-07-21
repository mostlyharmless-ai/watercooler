"""Proxy-transport repo scope: routing (multi-repo, #1082) with guard fallback.

Proxy mode pins one ``X-Repo`` at construction for the whole session
(the ``PremiumToolClient`` headers built in ``server._run_proxy``).
Amendment A2 of completion plan v3 (thread
``audit-transport-modes-hosted-db-2026-07``, ratified Decision
01KWY4MNN66A…) made that boundary deterministic with a
``proxy_repo_mismatch`` refusal. Wave 2 of the completion-sequence plan
(:36, 01KX0AMDMN0R0VX335PQYDW4M3; Codex-approved at :37) upgrades the
refusal point into a ROUTER: a tool call whose ``code_path`` positively
derives a *different* repo is forwarded to that repo's pooled premium
client — reads AND writes — instead of being refused. The hosted
ownership check remains the authority: an unclaimed derived repo is
refused server-side per request, never silently served from the pinned
repo's graphs.

Pass-through semantics (pinned-repo behavior preserved for single-repo
use): calls with no ``code_path`` argument, or one that does not
resolve to a git repo with an ``origin`` remote, are forwarded
unchanged on the pinned session. Only a *positively derived* foreign
repo routes — the router never guesses, and per the Codex review it
deliberately does NOT fail closed on underivable writes (ordinary
pinned single-repo proxy writes without a derivable ``code_path`` are a
valid, common shape).

No-silent-fallback contract (Codex review :37 constraint 1): the routed
leg is built on ``PremiumClientPool.client_for_repo`` with the derived
slug — NOT on ``select_pool_client``, whose boot-client fallback is a
read-only convenience by documented contract. Any failure on the routed
leg surfaces as a structured ``proxy_route_error`` result; it never
falls back to the pinned client.

Header parity note (Codex review :37 constraint 2): ``create_proxy``'s
incoming-header forwarding applies to HTTP-served proxies. This proxy
is stdio-served (``server._run_proxy`` → ``proxy.run()``): there are no
incoming HTTP headers to forward on EITHER leg, and both legs build the
identical hosted header set (auth + ``X-Repo``/``X-Branch``) through the
same machinery — ``build_premium_headers`` via
``PremiumToolClient.from_transport_config`` (the pool constructs its
per-repo clients exactly as ``_run_proxy`` builds the boot client).

Constructed without a pool, the middleware degrades to the original A2
guard behavior (structured ``proxy_repo_mismatch`` refusal).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from .premium_client import summarize_http_error

logger = logging.getLogger(__name__)


class ProxyRepoScopeMiddleware(Middleware):
    """Route (with a pool) or reject (without) cross-repo proxy calls."""

    def __init__(self, pinned_repo: str, pool: Optional[Any] = None) -> None:
        self._pinned = (pinned_repo or "").strip()
        self._pool = pool

    async def on_call_tool(self, context, call_next):
        arguments = getattr(context.message, "arguments", None) or {}
        code_path = arguments.get("code_path")
        requested = _derive_repo(code_path)
        tool_name = getattr(context.message, "name", "<unknown>")
        if not (
            requested
            and self._pinned
            and requested.lower() != self._pinned.lower()
        ):
            try:
                return await call_next(context)
            except Exception as exc:
                # Keep the hosted endpoint's error body visible (#1117):
                # an HTTP-layer rejection on the forwarded leg (e.g. a 403
                # ``repo_claim_mismatch`` when the token's claim changed
                # mid-session) would otherwise be flattened by FastMCP's
                # generic exception mapping to a bare -32603 status line.
                status, detail = summarize_http_error(exc)
                if detail is None:
                    raise
                logger.warning(
                    "PROXY_GUARD: forwarded call %s failed with HTTP %s: %s",
                    tool_name,
                    status,
                    detail,
                )
                return ToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "hosted_http_error",
                                    "status_code": status,
                                    "tool": tool_name,
                                    "message": detail,
                                },
                                indent=2,
                            ),
                        )
                    ],
                    is_error=True,
                )

        if self._pool is None:
            logger.warning(
                "PROXY_GUARD: rejecting %s — code_path derives repo %s but "
                "this proxy session is pinned to %s",
                tool_name,
                requested,
                self._pinned,
            )
            return _structured_error(
                error="proxy_repo_mismatch",
                pinned=self._pinned,
                requested=requested,
                tool=tool_name,
                message=(
                    "proxy transport is single-repo per "
                    f"session: this session is pinned to {self._pinned!r} "
                    f"but the call's code_path resolves to {requested!r}. "
                    "Restart the proxy with [mcp].proxy_repo set to that "
                    "repo, or use the hybrid transport for multi-repo "
                    "sessions."
                ),
            )

        # Routed leg. Failures surface — never a silent fallback to the
        # pinned client (a derivation or pool bug must not write to the
        # boot repo).
        try:
            client = self._pool.client_for_repo(
                requested, repo_root=Path(code_path)
            )
            logger.info(
                "PROXY_ROUTE: %s — code_path derives repo %s; routing on "
                "the pooled client (session pinned to %s)",
                tool_name,
                requested,
                self._pinned,
            )
            return await client.call_tool_result(tool_name, arguments)
        except Exception as exc:
            logger.warning(
                "PROXY_ROUTE: routed call for %s to repo %s failed: %s",
                tool_name,
                requested,
                exc,
            )
            # An HTTP-layer rejection carries the hosted endpoint's own
            # error body — include it rather than only str(exc) (#1117).
            status, detail = summarize_http_error(exc)
            hosted = (
                f" Hosted response (HTTP {status}): {detail}" if detail else ""
            )
            return _structured_error(
                error="proxy_route_error",
                pinned=self._pinned,
                requested=requested,
                tool=tool_name,
                message=(
                    f"routing to derived repo {requested!r} failed "
                    f"({type(exc).__name__}: {exc}). The call was NOT "
                    f"served from the pinned repo {self._pinned!r}."
                    + hosted
                ),
            )


def _structured_error(
    *, error: str, pinned: str, requested: str, tool: str, message: str
) -> ToolResult:
    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": error,
                        "pinned_repo": pinned,
                        "requested_repo": requested,
                        "tool": tool,
                        "message": message,
                    },
                    indent=2,
                ),
            )
        ],
        is_error=True,
    )


def _derive_repo(code_path: object) -> str:
    """Derive ``<org>/<repo>`` from a call's ``code_path``, or ``""``.

    Any failure (no path, not a git repo, no origin remote, git missing)
    returns ``""`` so the caller passes the call through — the router
    only acts on a positively derived repo.
    """
    if not code_path or not isinstance(code_path, str):
        return ""
    try:
        from watercooler.path_resolver import derive_repo_slug

        return (derive_repo_slug(code_path=Path(code_path)) or "").strip()
    except Exception:
        return ""
