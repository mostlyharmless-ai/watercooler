"""Tests for src/watercooler_mcp/tools/roles.py.

PR3b: watercooler_role_details was folded into watercooler_roles(role=...);
``_roles_impl`` with a ``role`` argument now serves the single-role spec.
"""

from __future__ import annotations

import json

from watercooler_mcp.tools.roles import _roles_impl

CANONICAL_ROLES = {"planner", "critic", "implementer", "tester", "pm", "scribe"}


def test_roles_returns_all_six():
    """Default code_path returns all 6 canonical roles."""
    result = json.loads(_roles_impl())
    assert set(result.keys()) == CANONICAL_ROLES


def test_roles_compact_no_instructions():
    """Catalog response omits instructions, entry_style, collaborate_with."""
    result = json.loads(_roles_impl())
    for role_data in result.values():
        assert "instructions" not in role_data
        assert "entry_style" not in role_data
        assert "collaborate_with" not in role_data


def test_roles_with_role_returns_full_spec():
    """_roles_impl(role='critic') returns the full behavioral spec."""
    result = json.loads(_roles_impl(role="critic"))
    assert "error" not in result
    assert result["name"] == "critic"
    assert "instructions" in result
    assert result["instructions"]  # not empty
    assert "entry_style" in result
    assert "collaborate_with" in result


def test_roles_with_unknown_role():
    """An unknown role returns an error with a valid_roles list."""
    result = json.loads(_roles_impl(role="reviewer"))
    assert result["error"] == "unknown_role"
    assert "valid_roles" in result
    assert set(result["valid_roles"]) >= CANONICAL_ROLES


def test_roles_project_override(tmp_path):
    """Project roles.toml override is reflected in the catalog response."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Custom critic for this project"\n'
        'canonical_role = "critic"\n'
    )
    result = json.loads(_roles_impl(code_path=str(tmp_path)))
    assert result["critic"]["description"] == "Custom critic for this project"
    # Other roles still present
    assert "planner" in result
