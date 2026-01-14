"""Entry point for Railway/ASGI deployments.

Railway and other ASGI platforms auto-detect `main:app`.
This file re-exports the FastAPI app from the watercooler_mcp package.
"""

from watercooler_mcp.server_http import app

__all__ = ["app"]
