"""Tests for src/watercooler_mcp/tools/roles.py."""

from __future__ import annotations

import json

import pytest

from watercooler_mcp.tools.roles import _role_details_impl, _roles_impl

CANONICAL_ROLES = {"planner", "critic", "implementer", "tester", "pm", "scribe"}


def test_roles_returns_all_six():
    """Default code_path returns all 6 canonical roles."""
    result = json.loads(_roles_impl())
    assert set(result.keys()) == CANONICAL_ROLES


def test_roles_compact_no_instructions():
    """Compact response does not include instructions, entry_style, or collaborate_with."""
    result = json.loads(_roles_impl())
    for role_data in result.values():
        assert "instructions" not in role_data
        assert "entry_style" not in role_data
        assert "collaborate_with" not in role_data


def test_role_details_full_spec():
    """role_details for 'critic' includes full behavioral spec."""
    result = json.loads(_role_details_impl(role="critic"))
    assert "error" not in result
    assert result["name"] == "critic"
    assert "instructions" in result
    assert result["instructions"]  # not empty
    assert "entry_style" in result
    assert "collaborate_with" in result


def test_role_details_unknown_role():
    """Unknown role returns error with valid_roles list."""
    result = json.loads(_role_details_impl(role="reviewer"))
    assert result["error"] == "unknown_role"
    assert "valid_roles" in result
    assert set(result["valid_roles"]) >= CANONICAL_ROLES


def test_roles_project_override(tmp_path):
    """Project roles.toml override is reflected in _roles_impl response."""
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
