"""Hosted JSON-RPC adapter for MCP protocol.

Replaces mounted FastMCP http_app() surfaces with direct request handling.
Calls into FastMCP surfaces via their public API (list_tools, call_tool, etc.)
while the parent FastAPI app owns auth, context, and timeouts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from fastapi.responses import JSONResponse
from starlette.requests import Request as StarletteRequest

from .request_trace import get_request_trace, trace_stage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP protocol version negotiation
# ---------------------------------------------------------------------------

# Supported MCP protocol versions for initialize handshake.
# Keep in sync with the mcp SDK; if the import fails we fall back to a
# known-good default so the adapter is still functional.
try:
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
except ImportError:
    SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05",)

_SUPPORTED_VERSIONS: frozenset[str] = frozenset(
    SUPPORTED_PROTOCOL_VERSIONS
    if isinstance(SUPPORTED_PROTOCOL_VERSIONS, (list, tuple, set, frozenset))
    else (SUPPORTED_PROTOCOL_VERSIONS,)
)
_LATEST_VERSION: str = sorted(_SUPPORTED_VERSIONS)[-1] if _SUPPORTED_VERSIONS else "2024-11-05"


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def jsonrpc_ok(id: Any, result: Any) -> dict:
    """Build a JSON-RPC 2.0 success response envelope."""
    return {"jsonrpc": "2.0", "id": id, "result": result}


def jsonrpc_error(id: Any, code: int, message: str, data: Any = None) -> dict:
    """Build a JSON-RPC 2.0 error response envelope."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id if id is not None else "server-error", "error": err}


# Standard JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# HostedSurfaceSpec
# ---------------------------------------------------------------------------


@dataclass
class HostedSurfaceSpec:
    """Descriptor binding a FastMCP surface to a hosted route.

    Attributes:
        name: Human-readable surface label (e.g. ``"dashboard"`` or ``"premium"``).
        surface: The FastMCP instance that owns tools, resources, and prompts.
        path: URL path prefix where this surface is served
            (e.g. ``"/mcp"`` or ``"/mcp/premium"``).
    """

    name: str
    surface: Any  # FastMCP instance
    path: str


# ---------------------------------------------------------------------------
# Method dispatch table
# ---------------------------------------------------------------------------

# Methods that are JSON-RPC notifications (no id, no response expected).
_NOTIFICATION_METHODS: frozenset[str] = frozenset({
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
})


def _rebind_session_context(request: StarletteRequest) -> None:
    """Rebind session identity into the active HttpRequestContext.

    Loads the session from the ``mcp-session-id`` header, and if found,
    updates the current ``HttpRequestContext`` with ``session_id`` and
    ``client_id`` from the session store.  This ensures that
    ``get_effective_client_id()`` returns the correct identity for tools
    like ``watercooler_whoami``.
    """
    session_id = (
        request.headers.get("mcp-session-id")
        or request.headers.get("x-session-id")
    )
    if not session_id:
        return

    from .context import get_http_context
    from .hosted_session import get_hosted_session_store

    store = get_hosted_session_store()
    session_info = store.get(session_id)
    if session_info is None:
        return

    ctx = get_http_context()
    if ctx is not None:
        # Validate session ownership before rebinding any fields.
        # Without this, a valid Bearer token + forged mcp-session-id
        # header could replace the session identity in audit trails.
        if session_info.user_id and ctx.user_id:
            if session_info.user_id != ctx.user_id:
                return  # silently ignore — session belongs to another user

        ctx.session_id = session_id
        # Only rebind client_id if it hasn't already been set by the auth
        # layer.  Blindly overwriting would let a forged mcp-session-id
        # header replace the authenticated identity.
        if session_info.client_id and not ctx.client_id:
            ctx.client_id = session_info.client_id


async def dispatch_hosted_request(
    spec: HostedSurfaceSpec,
    request: StarletteRequest,
) -> JSONResponse:
    """Dispatch an incoming HTTP request as a JSON-RPC 2.0 MCP call.

    Reads the request body, validates JSON-RPC structure, and routes
    to the appropriate handler based on the ``method`` field.

    Args:
        spec: The surface spec describing which FastMCP instance to call.
        request: The incoming FastAPI/Starlette request.

    Returns:
        A ``JSONResponse`` containing the JSON-RPC 2.0 result or error.
    """
    # -- Parse body ----------------------------------------------------------
    with trace_stage("rpc.parse"):
        try:
            body = await request.body()
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return JSONResponse(
                status_code=200,
                content=jsonrpc_error(None, _PARSE_ERROR, f"Parse error: {exc}"),
            )

    # -- Reject batches (arrays) ---------------------------------------------
    if isinstance(payload, list):
        return JSONResponse(
            status_code=200,
            content=jsonrpc_error(
                None,
                _INVALID_REQUEST,
                "Batch requests are not supported",
            ),
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=200,
            content=jsonrpc_error(None, _INVALID_REQUEST, "Request must be a JSON object"),
        )

    # -- Extract fields ------------------------------------------------------
    jsonrpc_version = payload.get("jsonrpc")
    if jsonrpc_version != "2.0":
        return JSONResponse(
            status_code=200,
            content=jsonrpc_error(
                payload.get("id"),
                _INVALID_REQUEST,
                f"Unsupported jsonrpc version: {jsonrpc_version!r}",
            ),
        )

    method: str = payload.get("method", "")
    rpc_id: Any = payload.get("id")
    params: dict = payload.get("params") or {}

    if not method:
        return JSONResponse(
            status_code=200,
            content=jsonrpc_error(rpc_id, _INVALID_REQUEST, "Missing 'method' field"),
        )

    # Set tool_name on the active request trace for tools/call
    trace = get_request_trace()
    if trace and method == "tools/call":
        trace.tool_name = params.get("name", "")

    # -- Session context rebinding (Step 5) ----------------------------------
    # Before dispatching, load session from header and rebind client_id
    # into the HttpRequestContext so get_effective_client_id() works.
    with trace_stage("rpc.session.lookup"):
        _rebind_session_context(request)

    # -- Dispatch to handler -------------------------------------------------
    with trace_stage("rpc.route", route=method, surface=spec.name):
        try:
            result, session_id = await _dispatch_method(spec, method, params, rpc_id, request)
        except Exception:
            logger.exception("Unhandled error in dispatch for method=%s surface=%s", method, spec.name)
            return JSONResponse(
                status_code=200,
                content=jsonrpc_error(rpc_id, _INTERNAL_ERROR, "Internal server error"),
            )

    # -- Build response ------------------------------------------------------
    with trace_stage("rpc.serialize"):
        headers: dict[str, str] = {}
        if session_id:
            headers["mcp-session-id"] = session_id

        # Notifications have no id and no response body
        if method in _NOTIFICATION_METHODS:
            return JSONResponse(status_code=202, content={}, headers=headers)

        return JSONResponse(status_code=200, content=result, headers=headers)


# ---------------------------------------------------------------------------
# Internal dispatch router
# ---------------------------------------------------------------------------


async def _dispatch_method(
    spec: HostedSurfaceSpec,
    method: str,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Route a JSON-RPC method to the appropriate handler.

    Returns:
        Tuple of (response_dict, optional_session_id).
    """
    handler_map: dict[str, Any] = {
        "initialize": _handle_initialize,
        "ping": _handle_ping,
        "tools/list": _handle_tools_list,
        "tools/call": _handle_tools_call,
        "resources/list": _handle_resources_list,
        "resources/read": _handle_resources_read,
        "resources/templates/list": _handle_resource_templates_list,
        "prompts/list": _handle_prompts_list,
        "prompts/get": _handle_prompts_get,
        "notifications/initialized": _handle_notifications_initialized,
    }

    handler = handler_map.get(method)
    if handler is None:
        return jsonrpc_error(rpc_id, _METHOD_NOT_FOUND, f"Unknown method: {method}"), None

    return await handler(spec, params, rpc_id, request)


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------


async def _handle_initialize(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``initialize`` — negotiate protocol version and capabilities.

    Generates a new session, stores it in the HostedSessionStore, and
    returns server capabilities derived from the FastMCP surface.
    """
    from .hosted_session import get_hosted_session_store

    # -- Protocol version negotiation ----------------------------------------
    client_version = params.get("protocolVersion", "")
    if client_version and client_version not in _SUPPORTED_VERSIONS:
        return (
            jsonrpc_error(
                rpc_id,
                _INVALID_PARAMS,
                f"Unsupported protocol version: {client_version}. "
                f"Supported: {sorted(_SUPPORTED_VERSIONS)}",
            ),
            None,
        )
    negotiated_version = client_version if client_version in _SUPPORTED_VERSIONS else _LATEST_VERSION

    # -- Session creation ----------------------------------------------------
    session_id = str(uuid.uuid4())
    client_info = params.get("clientInfo") or {}
    client_id = client_info.get("name", "")

    # Capture the authenticated user_id for session ownership validation.
    from .context import get_http_context
    _ctx = get_http_context()
    _session_user_id = _ctx.user_id if _ctx else None

    store = get_hosted_session_store()
    store.create_or_replace(
        session_id=session_id,
        surface_name=spec.name,
        client_id=client_id,
        client_info=client_info,
        protocol_version=negotiated_version,
        user_id=_session_user_id,
    )

    # -- Build capabilities from the FastMCP surface -------------------------
    capabilities = _build_server_capabilities(spec)

    # -- Server info ---------------------------------------------------------
    server_info = {
        "name": f"Watercooler Cloud ({spec.name})",
        "version": _get_server_version(),
    }

    result = {
        "protocolVersion": negotiated_version,
        "capabilities": capabilities,
        "serverInfo": server_info,
    }

    return jsonrpc_ok(rpc_id, result), session_id


async def _handle_ping(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``ping`` — return empty result."""
    session_id = _touch_session(request)
    return jsonrpc_ok(rpc_id, {}), session_id


async def _handle_tools_list(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``tools/list`` — enumerate available tools."""
    session_id = _touch_session(request)

    tools = await spec.surface.list_tools()
    tools_json = []
    for t in tools:
        tool_entry: dict[str, Any] = {"name": t.name}
        # FastMCP Tool objects expose .description and .inputSchema (mcp.types.Tool)
        # or .parameters (fastmcp internal). Try both for compatibility.
        if hasattr(t, "description") and t.description:
            tool_entry["description"] = t.description
        if hasattr(t, "inputSchema"):
            tool_entry["inputSchema"] = t.inputSchema
        elif hasattr(t, "parameters"):
            tool_entry["inputSchema"] = t.parameters
        tools_json.append(tool_entry)

    return jsonrpc_ok(rpc_id, {"tools": tools_json}), session_id


async def _handle_tools_call(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``tools/call`` — invoke a tool on the FastMCP surface.

    Applies per-tool timeouts from the middleware configuration.
    Rejects requests that include a ``task`` key (unsupported).
    """
    session_id = _touch_session(request)

    name = params.get("name", "")
    arguments = params.get("arguments") or {}

    if not name:
        return jsonrpc_error(rpc_id, _INVALID_PARAMS, "Missing 'name' in tools/call params"), session_id

    # Reject unsupported task-based invocations.
    if "task" in params:
        return (
            jsonrpc_error(rpc_id, _INVALID_PARAMS, "Task-based tool calls are not supported"),
            session_id,
        )

    # Resolve per-tool timeout.
    from .middleware import get_tool_timeout

    timeout = get_tool_timeout(name)

    try:
        tool_result = await asyncio.wait_for(
            spec.surface.call_tool(name, arguments),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return (
            jsonrpc_error(rpc_id, _INTERNAL_ERROR, f"Tool '{name}' timed out after {timeout}s"),
            session_id,
        )
    except Exception as exc:
        logger.exception("Tool call failed: %s(%s)", name, arguments)
        return (
            jsonrpc_error(rpc_id, _INTERNAL_ERROR, f"Tool call failed: {exc}"),
            session_id,
        )

    # Convert ToolResult to MCP JSON-RPC format.
    result = _convert_tool_result(tool_result)
    return jsonrpc_ok(rpc_id, result), session_id


async def _handle_resources_list(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``resources/list`` — enumerate available resources."""
    session_id = _touch_session(request)

    resources = await spec.surface.list_resources()
    resources_json = []
    for r in resources:
        entry: dict[str, Any] = {"uri": str(r.uri), "name": r.name}
        if hasattr(r, "description") and r.description:
            entry["description"] = r.description
        if hasattr(r, "mimeType") and r.mimeType:
            entry["mimeType"] = r.mimeType
        resources_json.append(entry)

    return jsonrpc_ok(rpc_id, {"resources": resources_json}), session_id


async def _handle_resources_read(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``resources/read`` — read a specific resource by URI."""
    session_id = _touch_session(request)

    uri = params.get("uri", "")
    if not uri:
        return jsonrpc_error(rpc_id, _INVALID_PARAMS, "Missing 'uri' in resources/read params"), session_id

    try:
        result = await spec.surface.read_resource(uri)
    except Exception as exc:
        logger.exception("Resource read failed: %s", uri)
        return (
            jsonrpc_error(rpc_id, _INTERNAL_ERROR, f"Resource read failed: {exc}"),
            session_id,
        )

    # read_resource returns content — convert to MCP format.
    contents = _convert_resource_contents(uri, result)
    return jsonrpc_ok(rpc_id, {"contents": contents}), session_id


async def _handle_resource_templates_list(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``resources/templates/list`` — enumerate resource templates."""
    session_id = _touch_session(request)

    templates = await spec.surface.list_resource_templates()
    templates_json = []
    for t in templates:
        entry: dict[str, Any] = {"uriTemplate": str(t.uriTemplate), "name": t.name}
        if hasattr(t, "description") and t.description:
            entry["description"] = t.description
        if hasattr(t, "mimeType") and t.mimeType:
            entry["mimeType"] = t.mimeType
        templates_json.append(entry)

    return jsonrpc_ok(rpc_id, {"resourceTemplates": templates_json}), session_id


async def _handle_prompts_list(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``prompts/list`` — enumerate available prompts."""
    session_id = _touch_session(request)

    prompts = await spec.surface.list_prompts()
    prompts_json = []
    for p in prompts:
        entry: dict[str, Any] = {"name": p.name}
        if hasattr(p, "description") and p.description:
            entry["description"] = p.description
        if hasattr(p, "arguments") and p.arguments:
            entry["arguments"] = [
                _convert_prompt_argument(a) for a in p.arguments
            ]
        prompts_json.append(entry)

    return jsonrpc_ok(rpc_id, {"prompts": prompts_json}), session_id


async def _handle_prompts_get(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``prompts/get`` — retrieve a specific prompt."""
    session_id = _touch_session(request)

    name = params.get("name", "")
    if not name:
        return jsonrpc_error(rpc_id, _INVALID_PARAMS, "Missing 'name' in prompts/get params"), session_id

    arguments = params.get("arguments") or {}

    try:
        result = await spec.surface.get_prompt(name, arguments)
    except Exception as exc:
        logger.exception("Prompt get failed: %s", name)
        return (
            jsonrpc_error(rpc_id, _INTERNAL_ERROR, f"Prompt get failed: {exc}"),
            session_id,
        )

    # Convert prompt result to MCP format.
    prompt_result = _convert_prompt_result(result)
    return jsonrpc_ok(rpc_id, prompt_result), session_id


async def _handle_notifications_initialized(
    spec: HostedSurfaceSpec,
    params: dict,
    rpc_id: Any,
    request: StarletteRequest,
) -> tuple[dict, Optional[str]]:
    """Handle ``notifications/initialized`` — mark session as ready.

    This is a notification (no id), so the caller should return 202
    with an empty body.
    """
    session_id = request.headers.get("mcp-session-id") or request.headers.get("x-session-id")
    if session_id:
        from .hosted_session import get_hosted_session_store

        store = get_hosted_session_store()
        store.mark_initialized(session_id)

    return jsonrpc_ok(rpc_id, {}), session_id


# ---------------------------------------------------------------------------
# DELETE handler (session teardown)
# ---------------------------------------------------------------------------


async def handle_hosted_delete(spec: HostedSurfaceSpec, request: StarletteRequest) -> JSONResponse:
    """Handle HTTP DELETE — tear down a hosted MCP session.

    Removes the session from the HostedSessionStore if present.

    Args:
        spec: The surface spec (used for logging context).
        request: The incoming request with ``mcp-session-id`` header.

    Returns:
        A 200 JSONResponse with empty body.
    """
    session_id = request.headers.get("mcp-session-id") or request.headers.get("x-session-id")
    if session_id:
        from .context import get_http_context
        from .hosted_session import get_hosted_session_store

        store = get_hosted_session_store()
        # Validate session ownership before deletion (fail-closed).
        session_info = store.get(session_id)
        if session_info:
            ctx = get_http_context()
            requesting_user = ctx.user_id if ctx else None
            # Deny if: no requester identity, session has no owner, or
            # requester doesn't match owner.
            if not requesting_user or not session_info.user_id or (
                requesting_user != session_info.user_id
            ):
                logger.warning(
                    "Session %s delete denied: owner=%s requester=%s",
                    session_id, session_info.user_id, requesting_user,
                )
                return JSONResponse(status_code=403, content={"error": "Forbidden"})
        store.delete(session_id)
        logger.info("Session %s deleted on surface %s", session_id, spec.name)

    return JSONResponse(status_code=200, content={})


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _convert_tool_result(tool_result: Any) -> dict[str, Any]:
    """Convert a FastMCP ToolResult to MCP JSON-RPC response format.

    FastMCP's ``call_tool()`` returns a ToolResult (or CallToolResult)
    with a ``.content`` list of TextContent/ImageContent/etc. objects
    and an optional ``.isError`` flag.

    Returns:
        Dict with ``content`` (list of content dicts) and ``isError`` (bool).
    """
    # Extract content list — handle both attribute styles.
    if hasattr(tool_result, "content"):
        raw_contents = tool_result.content
    elif isinstance(tool_result, (list, tuple)):
        raw_contents = tool_result
    else:
        raw_contents = [tool_result]

    content_items = []
    for item in (raw_contents or []):
        content_items.append(_convert_content_item(item))

    is_error = getattr(tool_result, "isError", False) or getattr(tool_result, "is_error", False)

    return {"content": content_items, "isError": bool(is_error)}


def _convert_content_item(item: Any) -> dict[str, Any]:
    """Convert a single MCP content item to a serializable dict.

    Handles TextContent, ImageContent, and EmbeddedResource objects.
    Falls back to a text representation for unknown types.
    """
    item_type = getattr(item, "type", "text")

    if item_type == "text":
        return {"type": "text", "text": getattr(item, "text", str(item))}
    elif item_type == "image":
        return {
            "type": "image",
            "data": getattr(item, "data", ""),
            "mimeType": getattr(item, "mimeType", "image/png"),
        }
    elif item_type == "resource":
        resource = getattr(item, "resource", None)
        if resource:
            return {
                "type": "resource",
                "resource": {
                    "uri": str(getattr(resource, "uri", "")),
                    "text": getattr(resource, "text", ""),
                    "mimeType": getattr(resource, "mimeType", "text/plain"),
                },
            }
    # Fallback: coerce to text
    return {"type": "text", "text": str(item)}


def _convert_resource_contents(uri: str, result: Any) -> list[dict[str, Any]]:
    """Convert FastMCP read_resource result to MCP contents format.

    FastMCP's ``read_resource()`` may return a string, bytes, or a
    structured object with content items.

    Args:
        uri: The resource URI that was read.
        result: The return value from ``surface.read_resource()``.

    Returns:
        List of content dicts suitable for a ``resources/read`` response.
    """
    if isinstance(result, str):
        return [{"uri": uri, "text": result, "mimeType": "text/plain"}]
    if isinstance(result, bytes):
        import base64

        return [{"uri": uri, "blob": base64.b64encode(result).decode("ascii"), "mimeType": "application/octet-stream"}]

    # Structured result — may be a list of content items or a single item.
    if isinstance(result, (list, tuple)):
        contents = []
        for item in result:
            contents.append(_convert_resource_content_item(uri, item))
        return contents

    # Single content item with .text or .content attribute.
    return [_convert_resource_content_item(uri, result)]


def _convert_resource_content_item(uri: str, item: Any) -> dict[str, Any]:
    """Convert a single resource content item to a serializable dict."""
    if isinstance(item, str):
        return {"uri": uri, "text": item, "mimeType": "text/plain"}

    entry: dict[str, Any] = {"uri": str(getattr(item, "uri", uri))}
    if hasattr(item, "text"):
        entry["text"] = item.text
        entry["mimeType"] = getattr(item, "mimeType", "text/plain")
    elif hasattr(item, "blob"):
        entry["blob"] = item.blob
        entry["mimeType"] = getattr(item, "mimeType", "application/octet-stream")
    else:
        entry["text"] = str(item)
        entry["mimeType"] = "text/plain"
    return entry


def _convert_prompt_argument(arg: Any) -> dict[str, Any]:
    """Convert a prompt argument descriptor to a serializable dict."""
    entry: dict[str, Any] = {"name": getattr(arg, "name", "")}
    if hasattr(arg, "description") and arg.description:
        entry["description"] = arg.description
    if hasattr(arg, "required"):
        entry["required"] = arg.required
    return entry


def _convert_prompt_result(result: Any) -> dict[str, Any]:
    """Convert a FastMCP prompt result to MCP JSON-RPC format.

    Prompt results contain ``messages`` (list of PromptMessage) and
    optionally a ``description``.
    """
    output: dict[str, Any] = {}
    if hasattr(result, "description") and result.description:
        output["description"] = result.description

    messages = []
    raw_messages = getattr(result, "messages", []) or []
    for msg in raw_messages:
        message_entry: dict[str, Any] = {
            "role": getattr(msg, "role", "assistant"),
        }
        content = getattr(msg, "content", None)
        if content:
            message_entry["content"] = _convert_content_item(content)
        messages.append(message_entry)

    output["messages"] = messages
    return output


# ---------------------------------------------------------------------------
# Server capabilities builder
# ---------------------------------------------------------------------------


def _build_server_capabilities(spec: HostedSurfaceSpec) -> dict[str, Any]:
    """Derive MCP server capabilities from the FastMCP surface.

    Introspects the surface to determine which capability groups
    are available (tools, resources, prompts). Strips unsupported
    capabilities (e.g. ``tasks``) from the result.

    Args:
        spec: The surface spec to introspect.

    Returns:
        A capabilities dict for the ``InitializeResult``.
    """
    capabilities: dict[str, Any] = {}

    # Tools — always present on our surfaces.
    capabilities["tools"] = {}

    # Resources — present if the surface has registered resources.
    capabilities["resources"] = {}

    # Prompts — present if the surface has registered prompts.
    capabilities["prompts"] = {}

    # Logging support.
    capabilities["logging"] = {}

    # Explicitly exclude unsupported capability groups.
    capabilities.pop("tasks", None)

    return capabilities


def _get_server_version() -> str:
    """Return the server version string."""
    try:
        from .config import get_version

        return get_version()
    except Exception:
        return "0.0.0"


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _touch_session(request: StarletteRequest) -> Optional[str]:
    """Extract and touch the session from request headers.

    Updates ``last_seen_at`` on the session to prevent idle eviction.

    Args:
        request: The incoming request.

    Returns:
        The session ID if present, ``None`` otherwise.
    """
    session_id = request.headers.get("mcp-session-id") or request.headers.get("x-session-id")
    if session_id:
        from .hosted_session import get_hosted_session_store

        store = get_hosted_session_store()
        store.touch(session_id)
    return session_id
