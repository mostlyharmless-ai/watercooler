"""Daemon management MCP tools.

Tools:
- watercooler_daemon_status: View daemon health and status
- watercooler_daemon_findings: Query daemon findings with filters
"""

from __future__ import annotations

import json
from typing import Optional

from fastmcp import Context

# Module-level references to registered tools (populated by register_daemon_tools)
daemon_status = None
daemon_findings = None


def _daemon_status_impl(
    ctx: Context,
    daemon: str = "",
) -> str:
    """Check daemon status and health.

    Returns status, last run time, interval, and error/findings counts
    for all registered daemons (or a specific one).

    Args:
        daemon: Optional daemon name. Empty returns all daemons.
    """
    from ..daemons import get_daemon_manager

    manager = get_daemon_manager()
    if manager is None:
        return json.dumps({
            "status": "not_initialized",
            "message": "Daemon manager not initialized",
        }, indent=2)

    if daemon:
        d = manager.get_daemon(daemon)
        if d is None:
            return json.dumps({
                "status": "error",
                "message": f"Daemon '{daemon}' not found",
                "available": manager.daemon_names,
            }, indent=2)
        return json.dumps(d.status_summary(), indent=2)

    return json.dumps(manager.status_all(), indent=2)


def _daemon_findings_impl(
    ctx: Context,
    daemon: str = "",
    severity: str = "",
    category: str = "",
    topic: str = "",
    limit: int = 50,
    unacknowledged_only: bool = False,
) -> str:
    """Query daemon findings with optional filters.

    Returns findings in reverse chronological order (newest first).

    Args:
        daemon: Filter by daemon name (empty = all daemons).
        severity: Filter by severity ("info", "warning", "error").
        category: Filter by category (e.g., "missing_status", "stale_thread").
        topic: Filter by thread topic.
        limit: Maximum findings to return (default 50).
        unacknowledged_only: Only return unacknowledged findings.
    """
    from ..daemons import get_daemon_manager

    manager = get_daemon_manager()
    if manager is None:
        return json.dumps({
            "status": "not_initialized",
            "message": "Daemon manager not initialized",
            "findings": [],
        }, indent=2)

    # Clamp limit
    limit = max(1, min(limit, 500))

    try:
        findings = manager.get_all_findings(
            limit=limit,
            daemon=daemon or None,
            severity=severity or None,
            category=category or None,
            topic=topic or None,
            unacknowledged_only=unacknowledged_only,
        )
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": str(exc),
            "findings": [],
        }, indent=2)

    return json.dumps({
        "count": len(findings),
        "findings": [f.to_dict() for f in findings],
    }, indent=2)


def register_daemon_tools(mcp) -> None:
    """Register daemon tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global daemon_status, daemon_findings

    daemon_status = mcp.tool(name="watercooler_daemon_status")(_daemon_status_impl)
    daemon_findings = mcp.tool(name="watercooler_daemon_findings")(_daemon_findings_impl)
