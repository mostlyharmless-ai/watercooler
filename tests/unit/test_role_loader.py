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
        assert role.project_salience == []  # bundled roles default to empty


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


def test_load_roles_project_salience_round_trips(tmp_path):
    """A project override can set project_salience and it round-trips as a list[str]."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["watch for hidden authority expansion"]\n'
    )
    roles = load_roles(tmp_path)
    assert roles["critic"].project_salience == ["watch for hidden authority expansion"]


def test_load_roles_project_salience_only_override_empties_other_fields(tmp_path):
    """Whole-block-replace semantics: a project_salience-only block does not inherit
    the bundled role's other fields — they come back empty/default."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'project_salience = ["watch for hidden authority expansion"]\n'
    )
    roles = load_roles(tmp_path)
    critic = roles["critic"]
    assert critic.project_salience == ["watch for hidden authority expansion"]
    assert critic.description == ""
    assert critic.boundary == ""
    assert critic.instructions == ""


def test_load_roles_project_salience_malformed_not_list(tmp_path):
    """project_salience must be a list — a bare string raises ValueError."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = "not a list"\n'
    )
    with pytest.raises(ValueError, match="project_salience"):
        load_roles(tmp_path)


def test_load_roles_project_salience_malformed_item_type(tmp_path):
    """project_salience entries must be strings — a non-string item raises ValueError."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["fine", 42]\n'
    )
    with pytest.raises(ValueError, match="project_salience"):
        load_roles(tmp_path)


def test_load_roles_project_salience_trims_whitespace(tmp_path):
    """project_salience entries are trimmed of surrounding whitespace."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["  watch for hidden authority expansion  "]\n'
    )
    roles = load_roles(tmp_path)
    assert roles["critic"].project_salience == ["watch for hidden authority expansion"]


def test_load_roles_project_salience_malformed_empty_string(tmp_path):
    """project_salience entries must be non-empty after trimming."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["   "]\n'
    )
    with pytest.raises(ValueError, match="project_salience"):
        load_roles(tmp_path)


def test_load_roles_project_salience_rejects_control_chars(tmp_path):
    """A bullet with a control/escape byte is rejected at load — it would
    otherwise persist into the committed roles.toml and be echoed to the
    terminal by the Stop hook (a version-controlled escape-injection payload)."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    #  (ESC) embedded in a TOML string literal.
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["watch for \\u001b[31m risk"]\n'
    )
    with pytest.raises(ValueError, match="control"):
        load_roles(tmp_path)


def test_load_roles_project_salience_rejects_c1_control_chars(tmp_path):
    """Single-byte C1 controls (\\x80-\\x9f, e.g. \\u009b CSI) are rejected too."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["watch for \\u009b31m risk"]\n'
    )
    with pytest.raises(ValueError, match="control"):
        load_roles(tmp_path)


def test_load_roles_project_salience_allows_unicode_text(tmp_path):
    """Legitimate non-ASCII text (accents, em dash) is not a control char and
    must load fine."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["café — watch for façade risk"]\n'
    )
    roles = load_roles(tmp_path)
    assert roles["critic"].project_salience == ["café — watch for façade risk"]
