"""Role discovery tool for the watercooler MCP server.

Tool:
- watercooler_roles: the project's role catalog, or — when ``role`` is given
  — the full behavioral spec for that single role.

PR3b consolidation: ``watercooler_role_details`` was folded into
``watercooler_roles(role=...)``; the old name forwards via a deprecation
alias (see ``watercooler_mcp/aliases.py``).
"""

from __future__ import annotations

import json
from dataclasses import asdict

from watercooler.role_loader import load_roles


# Module-level reference to the registered tool (populated by register_role_tools)
roles = None


def _roles_impl(code_path: str = "", role: str = "") -> str:
    """Return the project's role catalog, or one role's full behavioral spec.

    With no ``role``, returns the compact catalog — name, description,
    canonical_role, produces, boundary, when_to_use, handoff_to for every
    role. With a ``role`` name, returns that role's full spec: the compact
    fields plus instructions, entry_style, and collaborate_with.

    Args:
        code_path: Path to the project repository root. Uses bundled
            defaults when empty.
        role: Optional role name (e.g. "critic", "implementer"). Empty
            returns the full catalog.

    Returns:
        JSON — the catalog object, the single-role spec, or an error with a
        ``valid_roles`` list.
    """
    try:
        loaded = load_roles(code_path or None)
    except Exception as exc:
        return json.dumps({"error": "load_failed", "detail": str(exc)})

    if role:
        normalized = role.strip().lower()
        if normalized not in loaded:
            return json.dumps({
                "error": "unknown_role",
                "role": role,
                "valid_roles": sorted(loaded.keys()),
            })
        return json.dumps(asdict(loaded[normalized]), indent=2)

    result = {}
    for name, rd in loaded.items():
        result[name] = {
            "description": rd.description,
            "canonical_role": rd.canonical_role,
            "produces": rd.produces,
            "boundary": rd.boundary,
            "when_to_use": rd.when_to_use,
            "handoff_to": rd.handoff_to,
        }

    return json.dumps(result, indent=2)


def register_role_tools(mcp):
    """Register the role discovery tool with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global roles

    roles = mcp.tool(name="watercooler_roles")(_roles_impl)
