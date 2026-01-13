"""HTTP server module for hosted MCP deployment.

This module provides an HTTP-based entry point for the Watercooler MCP server,
designed for deployment as:
- Vercel serverless function (Python runtime)
- Standalone HTTP service (Railway, Fly.io, etc.)
- Docker container

The HTTP server integrates:
- FastMCP with HTTP transport
- Token-based authentication (via auth.py)
- Response caching (via cache.py)
- Request context extraction

Environment variables:
- WATERCOOLER_MCP_TRANSPORT: Set to "http" to enable HTTP mode
- WATERCOOLER_MCP_HOST: HTTP host (default: "0.0.0.0")
- WATERCOOLER_MCP_PORT: HTTP port (default: 8080)
- WATERCOOLER_AUTH_MODE: "local" or "hosted"
- See auth.py and cache.py for additional env vars

Deployment Options:

1. Standalone HTTP Server:
   ```bash
   WATERCOOLER_MCP_TRANSPORT=http python -m watercooler_mcp
   ```

2. Vercel Serverless (api/mcp.py):
   ```python
   from watercooler_mcp.server_http import app
   # Vercel auto-discovers FastAPI/Starlette apps
   ```

3. Docker:
   ```dockerfile
   CMD ["python", "-m", "watercooler_mcp.server_http"]
   ```

Usage from clients:
    POST /mcp
    Content-Type: application/json
    X-User-ID: user_123

    {"method": "tools/call", "params": {"name": "watercooler_say", ...}}
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


def check_http_dependencies() -> bool:
    """Check if HTTP dependencies are installed.

    The HTTP server requires the [http] extra:
        pip install watercooler-cloud[http]

    Returns:
        True if dependencies are available
    """
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        return True
    except ImportError:
        return False


def create_http_app():
    """Create FastAPI application wrapping the MCP server.

    This function creates a FastAPI app that:
    1. Exposes the FastMCP server via HTTP
    2. Adds authentication middleware
    3. Adds caching headers
    4. Provides health check endpoints

    Returns:
        FastAPI application instance
    """
    if not check_http_dependencies():
        raise ImportError(
            "HTTP dependencies not installed. "
            "Install with: pip install watercooler-cloud[http]"
        )

    from fastapi import FastAPI, Request, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    from .auth import extract_request_context, is_hosted_mode, get_github_token
    from .cache import cache

    # Import the main MCP server
    from .server import mcp

    # Create FastAPI wrapper
    app = FastAPI(
        title="Watercooler MCP HTTP Server",
        description="HTTP interface for Watercooler MCP tools",
        version="1.0.0",
    )

    # Configure CORS for browser-based clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv("WATERCOOLER_CORS_ORIGINS", "*").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health_check():
        """Health check endpoint for load balancers."""
        return {
            "status": "healthy",
            "mode": "hosted" if is_hosted_mode() else "local",
            "cache": cache.stats() if hasattr(cache, "stats") else {"backend": "unknown"},
        }

    @app.get("/")
    async def root():
        """Root endpoint with API information."""
        return {
            "service": "Watercooler MCP HTTP Server",
            "version": "1.0.0",
            "endpoints": {
                "/health": "Health check",
                "/mcp": "MCP protocol endpoint (POST)",
            },
            "auth_mode": "hosted" if is_hosted_mode() else "local",
        }

    @app.middleware("http")
    async def add_request_context(request: Request, call_next):
        """Middleware to extract and validate request context."""
        # Extract context from headers
        headers = dict(request.headers)
        query_params = dict(request.query_params)
        ctx = extract_request_context(headers, query_params)

        # Store context in request state
        request.state.user_id = ctx.user_id
        request.state.repo = ctx.repo
        request.state.branch = ctx.branch

        # For hosted mode, validate user has token
        if is_hosted_mode() and request.url.path.startswith("/mcp"):
            if not ctx.user_id:
                return JSONResponse(
                    status_code=401,
                    content={"error": "X-User-ID header required in hosted mode"},
                )
            token_info = get_github_token(ctx.user_id)
            if not token_info:
                return JSONResponse(
                    status_code=403,
                    content={"error": "No GitHub token found for user"},
                )
            # Store token in request state for MCP tools to use
            request.state.github_token = token_info.token

        response = await call_next(request)
        return response

    # Mount the FastMCP app at /mcp
    # FastMCP provides its own HTTP handling when run with transport="http"
    # We expose it through FastAPI for additional middleware

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """MCP protocol endpoint.

        Accepts JSON-RPC style MCP requests and forwards to FastMCP.
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid JSON body"},
            )

        # For now, we need to run the MCP server natively
        # This endpoint is a placeholder for direct HTTP->MCP bridging
        # The actual implementation uses FastMCP's built-in HTTP transport

        return JSONResponse(
            status_code=501,
            content={
                "error": "Direct MCP endpoint not yet implemented",
                "hint": "Use FastMCP's native HTTP transport instead",
                "command": "WATERCOOLER_MCP_TRANSPORT=http python -m watercooler_mcp",
            },
        )

    return app


# Create app instance for import
# This allows deployment platforms to auto-discover the app:
#   from watercooler_mcp.server_http import app
try:
    app = create_http_app()
except ImportError:
    app = None


def run_http_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    reload: bool = False,
) -> None:
    """Run the HTTP server.

    Args:
        host: Host to bind to (default: 0.0.0.0)
        port: Port to bind to (default: 8080)
        reload: Enable auto-reload for development
    """
    if not check_http_dependencies():
        print(
            "HTTP dependencies not installed.\n"
            "Install with: pip install watercooler-cloud[http]",
            file=sys.stderr,
        )
        sys.exit(1)

    import uvicorn

    print(f"Starting Watercooler MCP HTTP Server on http://{host}:{port}", file=sys.stderr)
    print(f"Health check: http://{host}:{port}/health", file=sys.stderr)
    print(f"API docs: http://{host}:{port}/docs", file=sys.stderr)

    uvicorn.run(
        "watercooler_mcp.server_http:app",
        host=host,
        port=port,
        reload=reload,
    )


def main():
    """Entry point for running HTTP server directly.

    Usage:
        python -m watercooler_mcp.server_http
    """
    from .config import get_mcp_transport_config

    config = get_mcp_transport_config()
    run_http_server(
        host=config["host"],
        port=config["port"],
    )


if __name__ == "__main__":
    main()
