"""Premium tool client for hybrid and hosted surfaces.

Wraps a FastMCP ``Client`` that connects to the hosted premium MCP
endpoint (``/mcp/premium``).  Used by:

- **Hybrid mode**: mixed-tool wrappers call ``call_tool_text()`` for
  capabilities routed to ``remote``.
- **Hybrid mode**: ``proxy_server()`` creates a mountable FastMCP proxy
  for pure remote-only tools.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy

logger = logging.getLogger(__name__)


def build_premium_headers(
    transport_config: dict[str, Any],
    *,
    boot_cwd: Path | None = None,
) -> dict[str, str]:
    """Build hosted context headers for hybrid/proxy transports.

    Args:
        transport_config: Dict with ``proxy_repo`` and ``proxy_branch`` keys.
        boot_cwd: Process cwd captured at MCP startup by the caller.

    Returns:
        Headers for the hosted premium transport.

    Raises:
        ValueError: If repo or branch context cannot be resolved.
    """
    from watercooler.config_facade import config as wc_config

    headers: dict[str, str] = {}
    repo = transport_config.get("proxy_repo", "")
    branch = transport_config.get("proxy_branch", "")

    if not repo or not branch:
        try:
            ctx = wc_config.context(boot_cwd)
            if not repo and ctx.code_repo:
                repo = ctx.code_repo
            if not branch and ctx.code_branch:
                branch = ctx.code_branch
        except Exception as exc:
            if not repo and not branch:
                raise ValueError(
                    "Hybrid/proxy mode requires a resolvable code repo and "
                    "code branch. "
                    f"Detected boot cwd {boot_cwd or '<not provided>'!s}, but "
                    "git context resolution failed. Set [mcp].proxy_repo = "
                    "'<owner>/<repo>' and [mcp].proxy_branch in "
                    "~/.watercooler/config.toml or run the MCP server from "
                    "inside a git repo."
                ) from exc
            elif not repo:
                raise ValueError(
                    "Hybrid/proxy mode requires a resolvable code repo. "
                    f"Detected boot cwd {boot_cwd or '<not provided>'!s}, but "
                    "git context resolution failed. Set [mcp].proxy_repo = "
                    "'<owner>/<repo>' in ~/.watercooler/config.toml or run "
                    "the MCP server from inside a git repo."
                ) from exc
            elif not branch:
                raise ValueError(
                    "Hybrid/proxy mode could not resolve a code branch. "
                    f"Detected boot cwd {boot_cwd or '<not provided>'!s}, but "
                    "git context resolution failed. Set [mcp].proxy_branch "
                    "in ~/.watercooler/config.toml or run the MCP server "
                    "from inside a git repo."
                ) from exc

    if repo:
        headers["X-Repo"] = repo
    else:
        raise ValueError(
            "Hybrid/proxy mode requires a resolvable code repo. "
            f"Detected boot cwd {boot_cwd or '<not provided>'!s}, but no "
            "repository slug could be inferred. Set [mcp].proxy_repo = "
            "'<owner>/<repo>' in ~/.watercooler/config.toml or run the MCP "
            "server from inside a git repo."
        )

    if branch:
        headers["X-Branch"] = branch
    else:
        raise ValueError(
            "Hybrid/proxy mode could not resolve a code branch. "
            f"Detected boot cwd {boot_cwd or '<not provided>'!s}, but no "
            "branch could be inferred. Set [mcp].proxy_branch in "
            "~/.watercooler/config.toml or run the MCP server from inside a "
            "git repo with a checked-out branch."
        )

    # Send the user's daemon config overrides (non-default values only)
    # so Railway merges them onto hosted defaults (all daemons enabled).
    try:
        import json as _json

        full_config = wc_config.full()
        daemon_cfg = full_config.mcp.daemons
        overrides = daemon_cfg.model_dump(exclude_unset=True)
        if overrides:
            headers["X-Daemon-Config"] = _json.dumps(overrides)
    except Exception:
        pass  # Non-fatal: Railway falls back to hosted defaults

    return headers


class PremiumToolClient:
    """Client wrapper for the hosted premium MCP endpoint."""

    def __init__(self, client: Client, *, name: str = "Watercooler Premium") -> None:
        self._client = client
        self._name = name

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_transport_config(
        cls,
        transport_config: dict[str, Any],
        *,
        boot_cwd: Path | None = None,
    ) -> PremiumToolClient:
        """Build a ``PremiumToolClient`` from the MCP transport config dict.

        Reuses the same repo/branch header resolution logic that
        ``_run_proxy()`` uses, so the hosted endpoint receives context
        headers on every request.

        Args:
            transport_config: Dict with ``url``, ``proxy_repo``,
                ``proxy_branch`` keys (as returned by
                ``get_mcp_transport_config()``).
            boot_cwd: Process cwd captured at MCP startup. The caller, not
                this helper, decides when cwd inference is safe.

        Returns:
            A configured ``PremiumToolClient``.

        Raises:
            ValueError: If ``url`` is missing from the transport config.
        """
        url = transport_config.get("url", "")
        if not url:
            raise ValueError(
                "Premium client requires a remote URL. "
                "Set [mcp].url in config.toml or WATERCOOLER_MCP_URL env var."
            )

        from watercooler.config_facade import config as wc_config

        api_key = wc_config.get_hosted_api_key()

        headers = build_premium_headers(transport_config, boot_cwd=boot_cwd)

        transport = StreamableHttpTransport(url, headers=headers, auth=api_key)
        client = Client(transport)
        return cls(client)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[str]:
        """Return the tool names exposed by the remote premium endpoint."""
        async with self._client:
            tools = await self._client.list_tools()
            return [t.name for t in tools]

    def proxy_server(self, name: str = "Watercooler Premium Proxy") -> FastMCP:
        """Create a FastMCP proxy server for mounting remote tools.

        The returned server can be mounted onto the local hybrid server
        with ``server.mount(proxy, tool_names=...)``.
        """
        return create_proxy(self._client, name=name)

    async def call_tool_text(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a remote tool and return the text result.

        Uses ``Client.call_tool()``; does not hand-roll JSON-RPC.

        Returns:
            The first ``TextContent.text`` from the result when the
            result is the normal single-text payload.  Returns a
            JSON-formatted error string if the remote call reports an
            error or returns an unexpected shape.
        """
        try:
            async with self._client:
                result = await self._client.call_tool(name, arguments)

            # FastMCP call_tool returns a CallToolResult with .content list.
            if hasattr(result, "content"):
                contents = result.content
            else:
                # Some versions return the content list directly.
                contents = result

            if not contents:
                return json.dumps({"error": "empty_response", "tool": name})

            first = contents[0]
            if hasattr(first, "text"):
                return first.text

            return json.dumps({"error": "unexpected_content_type", "tool": name})

        except Exception as exc:
            logger.warning("Premium client call_tool(%s) failed: %s", name, exc)
            return json.dumps({
                "error": "remote_call_failed",
                "tool": name,
                "message": str(exc),
            })
