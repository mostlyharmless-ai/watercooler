"""Entry point for FastMCP Cloud deployment.

This file sits at the repository root and imports the watercooler MCP server.
FastMCP Cloud automatically installs the package via pip, so we can import directly.
"""

# Import and re-export the MCP server
# This works because FastMCP Cloud runs `pip install .` which installs src/watercooler_mcp
from watercooler_mcp.server import mcp

__all__ = ["mcp"]
