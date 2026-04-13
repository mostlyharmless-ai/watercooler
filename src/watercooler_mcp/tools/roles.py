"""Role discovery tools for the watercooler MCP server.

Tools:
- watercooler_roles: Compact role catalog for a project
- watercooler_role_details: Full behavioral spec for a single role
"""

from __future__ import annotations

import json
from dataclasses import asdict

from watercooler.role_loader import load_roles


# Module-level references to registered tools (populated by register_role_tools)
roles = None
role_details = None


def _roles_impl(code_path: str = "") -> str:
    """Return the compact role catalog for a project.

    Returns name, description, produces, boundary, and when_to_use for all roles.
    Full behavioral specs (instructions, entry_style, collaborate_with) are available
    via watercooler_role_details.

    Args:
        code_path: Path to the project repository root. Uses bundled defaults when empty.

    Returns:
        JSON object keyed by role name with compact metadata.
    """
    try:
        loaded = load_roles(code_path or None)
    except Exception as exc:
        return json.dumps({"error": "load_failed", "detail": str(exc)})

    result = {}
    for name, role in loaded.items():
        result[name] = {
            "description": role.description,
            "canonical_role": role.canonical_role,
            "produces": role.produces,
            "boundary": role.boundary,
            "when_to_use": role.when_to_use,
            "handoff_to": role.handoff_to,
        }

    return json.dumps(result, indent=2)


def _role_details_impl(code_path: str = "", role: str = "") -> str:
    """Return the full behavioral spec for a single role.

    Includes all compact fields plus instructions, entry_style, and collaborate_with.

    Args:
        code_path: Path to the project repository root. Uses bundled defaults when empty.
        role: Role name to retrieve (e.g. "critic", "implementer").

    Returns:
        JSON object with the full role spec, or an error with valid_roles list.
    """
    if not role:
        return json.dumps({"error": "role_required"})

    try:
        loaded = load_roles(code_path or None)
    except Exception as exc:
        return json.dumps({"error": "load_failed", "detail": str(exc)})

    normalized = role.strip().lower()
    if normalized not in loaded:
        return json.dumps({
            "error": "unknown_role",
            "role": role,
            "valid_roles": sorted(loaded.keys()),
        })

    rd = loaded[normalized]
    return json.dumps(asdict(rd), indent=2)


def register_role_tools(mcp):
    """Register role discovery tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global roles, role_details

    roles = mcp.tool(name="watercooler_roles")(_roles_impl)
    role_details = mcp.tool(name="watercooler_role_details")(_role_details_impl)
