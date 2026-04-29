"""Tests for the fail-loud path in role_loader.load_roles().

A malformed or unreadable project ``.watercooler/roles.toml`` previously triggered
silent fallback to bundled defaults — leaving the user unaware their override was
ignored. Now it raises ``ValueError`` so the user fixes the file before continuing.
"""

from __future__ import annotations

import pytest

from watercooler.role_loader import load_roles


@pytest.mark.parametrize(
    "name,bad_bytes",
    [
        # TOML syntax error (unclosed table header).
        ("malformed_toml", b"[roles.foo\n"),
        # Non-UTF-8 bytes — fails during data.decode() inside _load_toml_bytes.
        ("non_utf8_bytes", b"\xff\xfe\x00\x00not valid utf-8"),
    ],
)
def test_raises_value_error_on_unreadable_project_file(tmp_path, name, bad_bytes):
    """Any failure during project roles.toml load raises ValueError naming the file."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    target = wc_dir / "roles.toml"
    target.write_bytes(bad_bytes)

    with pytest.raises(ValueError) as exc_info:
        load_roles(tmp_path)

    msg = str(exc_info.value)
    assert str(target) in msg
    assert "Fix the file and retry" in msg


def test_value_error_chains_underlying_cause(tmp_path):
    """The raised ValueError chains the original parse exception via ``from exc``."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text("[roles.foo\n")  # unclosed bracket

    with pytest.raises(ValueError) as exc_info:
        load_roles(tmp_path)

    assert exc_info.value.__cause__ is not None


def test_clean_state_does_not_raise(tmp_path):
    """No project roles.toml → bundled defaults returned, no exception."""
    roles = load_roles(tmp_path)
    assert "planner" in roles
    assert "critic" in roles
    assert len(roles) == 6  # six canonical defaults


def test_well_formed_project_file_does_not_raise(tmp_path):
    """A clean project file with a custom role merges with bundled defaults."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        "[roles.security-auditor]\n"
        'description = "Review code and configs for security vulnerabilities"\n'
        'canonical_role = "critic"\n'
    )

    roles = load_roles(tmp_path)
    assert "security-auditor" in roles
    assert roles["security-auditor"].canonical_role == "critic"
    # Six canonical roles still present.
    assert {"planner", "critic", "implementer", "tester", "pm", "scribe"} <= set(roles)
