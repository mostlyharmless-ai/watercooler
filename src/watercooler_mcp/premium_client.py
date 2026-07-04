"""Premium tool client for hybrid and hosted surfaces.

Wraps a FastMCP ``Client`` that connects to the hosted premium MCP
endpoint (``/mcp/premium``).  Used by:

- **Hybrid mode**: mixed-tool wrappers call ``call_tool_text()`` for
  capabilities routed to ``remote``.
- **Hybrid mode**: ``proxy_server()`` creates a mountable FastMCP proxy
  for pure remote-only tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy

logger = logging.getLogger(__name__)

# Bounds that keep a stuck Railway session from wedging the server forever.
# ``call_timeout`` covers a single tool call — well under the 50s tool wall and
# ~30x the healthy ~1s latency, with headroom for heavy T2 queries.
# ``init_timeout`` bounds the connect handshake (its config default is
# ``None`` == infinite, so it must be passed explicitly). Forced teardown of the
# throwaway per-call client is bounded by fastmcp's own ``_disconnect_timeout``
# (5s, shielded), so we do not wrap it in a second, cancellation-prone timeout.
DEFAULT_CALL_TIMEOUT = 30.0
DEFAULT_INIT_TIMEOUT = 10.0


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


def _extract_http_response(exc: BaseException) -> Any:
    """Return the HTTP response carried by *exc*, or ``None``.

    The ``mcp`` SDK raises ``httpx.HTTPStatusError`` (which carries a
    ``.response``) from inside an anyio context, so the error can reach the
    caller directly, chained via ``__cause__`` / ``__context__``, or wrapped in
    an ``ExceptionGroup``. Walk all of those — cycle-guarded by ``id`` — and
    return the first ``.response`` found. Duck-typed on ``.exceptions`` so it
    stays correct on Python 3.10 (no ``BaseExceptionGroup`` builtin there).
    """
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        resp = getattr(cur, "response", None)
        if resp is not None:
            return resp
        members = getattr(cur, "exceptions", None)  # ExceptionGroup (3.11+)
        if isinstance(members, (tuple, list)):
            stack.extend(members)
        stack.append(getattr(cur, "__cause__", None))
        stack.append(getattr(cur, "__context__", None))
    return None


def build_premium_client(
    url: str,
    headers: dict[str, str],
    api_key: str | None,
    *,
    call_timeout: float = DEFAULT_CALL_TIMEOUT,
    init_timeout: float = DEFAULT_INIT_TIMEOUT,
) -> Client:
    """Build a bounded FastMCP ``Client`` for the hosted premium endpoint.

    Single construction recipe shared by both consumers of the hosted endpoint —
    the hybrid direct path (``PremiumToolClient.from_transport_config``) and the
    pure-proxy path (``server._run_proxy``) — so transport, auth, and timeout
    policy stay identical across the two surfaces. ``init_timeout`` and
    ``timeout`` are passed explicitly because their config defaults are
    ``None`` == infinite.
    """
    transport = StreamableHttpTransport(url, headers=headers, auth=api_key)
    return Client(transport, init_timeout=init_timeout, timeout=call_timeout)


class PremiumToolClient:
    """Client wrapper for the hosted premium MCP endpoint."""

    def __init__(
        self,
        client: Client,
        *,
        name: str = "Watercooler Premium",
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self._client = client
        self._name = name
        self._call_timeout = call_timeout

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_transport_config(
        cls,
        transport_config: dict[str, Any],
        *,
        boot_cwd: Path | None = None,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        init_timeout: float = DEFAULT_INIT_TIMEOUT,
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

        # Each remote call additionally builds a fresh session (see
        # _fresh_session) so a wedged session is never reused.
        client = build_premium_client(
            url,
            headers,
            api_key,
            call_timeout=call_timeout,
            init_timeout=init_timeout,
        )
        return cls(client, call_timeout=call_timeout)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _fresh_session(self) -> AsyncIterator[Client]:
        """Yield a fresh, independent session for one remote interaction.

        FastMCP reuses a ``Client``'s background ``session_task`` across calls
        unless it is ``None``/done, with no liveness check — so one wedged
        session (a half-open stream, or a task orphaned on a closed event loop)
        poisons every later call on a shared client. ``Client.new()`` is
        FastMCP's sanctioned per-request idiom: it clones transport/auth config
        but starts clean session state, so a fresh call can never inherit a
        wedge.

        We solely own the throwaway client, so we force teardown in a
        ``finally``. The case this actually closes is a **failed connect
        handshake**: if ``__aenter__`` raises, ``__aexit__`` is never called, so
        the partial session would leak without this. On the normal/error exit
        paths ``__aexit__`` already disconnects (and fastmcp's ``_disconnect``
        cancels the ``session_task`` on overrun), so the explicit call is a
        cheap, idempotent belt-and-suspenders whose ``force=True`` also shields
        teardown from an outer cancellation. ``_disconnect`` is internally
        bounded and shielded (fastmcp ``_disconnect_timeout``, 5s), so we do
        **not** wrap it in our own ``asyncio.wait_for`` — that outer timeout
        could cancel the shielded cleanup mid-flight and re-leak the very
        ``session_task`` this guards against.
        """
        client = self._client.new()
        try:
            async with client:
                yield client
        finally:
            try:
                # Private fastmcp API; signature/behavior verified stable across
                # 3.2.0–3.4.x (pin floor >=3.4.0,<4). Re-check on a <4 bump.
                await client._disconnect(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "Premium fresh-session teardown failed (ignored)",
                    exc_info=True,
                )

    async def list_tools(self) -> list[str]:
        """Return the tool names exposed by the remote premium endpoint."""
        async with self._fresh_session() as client:
            tools = await client.list_tools()
            return [t.name for t in tools]

    def proxy_server(self, name: str = "Watercooler Premium Proxy") -> FastMCP:
        """Create a FastMCP proxy server for mounting remote tools.

        The returned server can be mounted onto the local hybrid server
        with ``server.mount(proxy, tool_names=...)``.

        ``create_proxy`` chooses its client factory once, from the client's
        connection state (verified against fastmcp 3.4.0): a *disconnected*
        ``Client`` yields a fresh session per request (``client.new()``), while
        a *connected* plain ``Client`` is reused for every request — the
        wedge-prone path. ``self._client`` is
        always disconnected here (``__init__`` only stores it; the proxy is
        mounted before any call), so the proxy is permanently fresh-per-request.
        We assert that invariant rather than hand-roll the ``client_factory``,
        because ``create_proxy`` also enables incoming-header forwarding that a
        hand-rolled factory would silently drop.
        """
        if self._client.is_connected():
            raise RuntimeError(
                "proxy_server() requires a disconnected premium client so the "
                "proxy builds a fresh session per request; got a connected "
                "client."
            )
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
            async with self._fresh_session() as client:
                result = await client.call_tool(
                    name, arguments, timeout=self._call_timeout
                )

                # FastMCP call_tool returns a CallToolResult with .content list.
                # Extract inside the session context (the content is valid here);
                # the fresh session is torn down on exit.
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

                return json.dumps(
                    {"error": "unexpected_content_type", "tool": name}
                )

        except Exception as exc:
            logger.warning("Premium client call_tool(%s) failed: %s", name, exc)
            payload: dict[str, Any] = {
                "error": "remote_call_failed",
                "tool": name,
                "message": str(exc),
            }
            # Surface the remote HTTP response body when the failure carries
            # one. The hosted endpoint already returns actionable 4xx bodies
            # (e.g. ``repo_claim_mismatch`` naming the unauthorised X-Repo and
            # the caller's authorised repo set), but ``str(exc)`` on an httpx
            # error is only the bare status line ("Client error '403
            # Forbidden' for url ..."), so without this the caller never sees
            # *why* the call was refused or how to fix it. The error may arrive
            # directly, chained (__cause__/__context__), or wrapped in an
            # ExceptionGroup (the mcp SDK raises raise_for_status() inside an
            # anyio context) — _extract_http_response walks all of those.
            response = _extract_http_response(exc)
            if response is not None:
                status = getattr(response, "status_code", None)
                if status is not None:
                    payload["status_code"] = status
                remote_error = None
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        remote_error = body.get("error") or body.get("message")
                except Exception:
                    pass
                if not remote_error:
                    text = (getattr(response, "text", "") or "").strip()
                    remote_error = text[:2000] or None
                if remote_error:
                    payload["remote_error"] = remote_error
            return json.dumps(payload)
