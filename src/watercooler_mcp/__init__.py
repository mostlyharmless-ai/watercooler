# SPDX-License-Identifier: Apache-2.0
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

__all__ = ["mcp"]


def __getattr__(name: str):
    """Lazily expose ``mcp`` (PEP 562).

    Importing ``watercooler_mcp`` must not eagerly pull in the FastMCP server
    stack: lightweight submodules — e.g. ``watercooler_mcp.daemons`` helpers
    driven by operator CLI scripts such as
    ``scripts/reset_decision_extractor.py`` — must be importable without
    ``fastmcp`` installed. ``watercooler_mcp.mcp`` and
    ``from watercooler_mcp import mcp`` still resolve, on first access.

    Load-bearing for issue #810: a module-level ``from .server import mcp`` here
    would make importing any submodule (e.g. ``watercooler_mcp.capabilities``,
    imported by the ``mcp.capability_routes`` config validator) eagerly import
    ``server.py``, whose import-time config load re-enters ``get_config()`` and
    self-deadlocks. Keep server import lazy. Guarded by
    ``tests/unit/test_config_loader.py::TestGetConfigReentrancy``.
    """
    if name == "mcp":
        from .server import mcp

        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
