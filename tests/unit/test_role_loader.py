"""Tests for src/watercooler/role_loader.py."""

from __future__ import annotations

import pytest

from watercooler.role_loader import RoleDefinition, load_roles, validate_role

CANONICAL_ROLES = {"planner", "critic", "implementer", "tester", "pm", "scribe"}


def test_load_roles_defaults():
    """No code_path → bundled defaults, all 6 canonical roles present."""
    roles = load_roles()
    assert set(roles.keys()) == CANONICAL_ROLES
    for name, role in roles.items():
        assert isinstance(role, RoleDefinition)
        assert role.name == name
        assert role.canonical_role == name  # canonical roles map to themselves
        assert role.description  # not empty


def test_load_roles_project_override(tmp_path):
    """Project roles.toml overrides description of one role."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Overridden critic description"\n'
        'canonical_role = "critic"\n'
    )
    roles = load_roles(tmp_path)
    assert roles["critic"].description == "Overridden critic description"
    # Other roles still come from bundled defaults
    assert roles["planner"].description  # not empty, from defaults
    assert set(roles.keys()) >= CANONICAL_ROLES


def test_load_roles_custom_role(tmp_path):
    """Project file can add a 7th role with canonical_role set."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.reviewer]\n'
        'description = "Detailed review and approval"\n'
        'canonical_role = "critic"\n'
    )
    roles = load_roles(tmp_path)
    assert "reviewer" in roles
    assert roles["reviewer"].canonical_role == "critic"
    # Canonical 6 still present
    assert set(roles.keys()) >= CANONICAL_ROLES


def test_validate_role_valid():
    """validate_role with a canonical role returns the normalized role."""
    assert validate_role("critic") == "critic"
    assert validate_role("IMPLEMENTER") == "implementer"
    assert validate_role("  tester  ") == "tester"


def test_validate_role_invalid():
    """validate_role with an unknown role raises ValueError listing valid roles."""
    with pytest.raises(ValueError) as exc_info:
        validate_role("jay")
    msg = str(exc_info.value)
    assert "jay" in msg
    for role in CANONICAL_ROLES:
        assert role in msg


def test_validate_role_none():
    """validate_role(None) returns None without raising."""
    assert validate_role(None) is None


def test_validate_role_empty():
    """validate_role('') returns '' without raising."""
    assert validate_role("") == ""


def test_validate_role_unknown_path(tmp_path):
    """code_path with no .watercooler/ directory falls back to bundled defaults."""
    # tmp_path has no .watercooler/ — should still succeed using bundled defaults
    assert validate_role("critic", code_path=tmp_path) == "critic"
    with pytest.raises(ValueError):
        validate_role("not-a-role", code_path=tmp_path)
