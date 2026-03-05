"""Watercooler MCP Server - Phase 1B

FastMCP server that exposes watercooler-cloud tools to AI agents.
Tools are namespaced as watercooler_* for provider compatibility.

Phase 1B features:
- Upward directory search for .watercooler/ (stops at git root or HOME)
- Comprehensive documentation (QUICKSTART.md, TROUBLESHOOTING.md)
- Codex TOML configuration support
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("watercooler")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"  # Fallback for editable installs without metadata

from .server import mcp

__all__ = ["mcp"]
