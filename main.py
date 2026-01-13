"""Entry point for Railway/ASGI deployments.

Railway and other ASGI platforms auto-detect `main:app`.
This file re-exports the FastAPI app from the watercooler_mcp package.
"""

import sys
print(f"Python: {sys.executable}", file=sys.stderr)
print(f"Path: {sys.path}", file=sys.stderr)

try:
    import watercooler_mcp
    print(f"watercooler_mcp location: {watercooler_mcp.__file__}", file=sys.stderr)
except ImportError as e:
    print(f"Failed to import watercooler_mcp: {e}", file=sys.stderr)

try:
    from watercooler_mcp.server_http import app
    print("Successfully imported app", file=sys.stderr)
except ImportError as e:
    print(f"Failed to import server_http: {e}", file=sys.stderr)
    # Fallback: create minimal app for debugging
    from fastapi import FastAPI
    app = FastAPI()

    @app.get("/")
    def root():
        return {"error": "Failed to import watercooler_mcp.server_http", "details": str(e)}

    @app.get("/health")
    def health():
        return {"status": "degraded", "error": str(e)}

__all__ = ["app"]
