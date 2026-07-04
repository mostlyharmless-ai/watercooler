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


def test_roles_compact_includes_project_salience():
    """Catalog response surfaces project_salience (empty for bundled defaults)."""
    result = json.loads(_roles_impl())
    for role_data in result.values():
        assert "project_salience" in role_data
        assert role_data["project_salience"] == []


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


def test_roles_full_spec_includes_project_salience(tmp_path):
    """Single-role full spec surfaces a project's project_salience bullets."""
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir()
    (wc_dir / "roles.toml").write_text(
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["watch for hidden authority expansion"]\n'
    )
    result = json.loads(_roles_impl(code_path=str(tmp_path), role="critic"))
    assert result["project_salience"] == ["watch for hidden authority expansion"]


class TestLedgerAction:
    def test_ledger_action_requires_role(self, tmp_path):
        result = json.loads(_roles_impl(code_path=str(tmp_path), action="ledger"))
        assert result["error"] == "role_required_for_ledger_action"

    def test_ledger_action_unknown_role(self, tmp_path):
        result = json.loads(
            _roles_impl(code_path=str(tmp_path), role="not-a-role", action="ledger")
        )
        assert result["error"] == "unknown_role"
        assert set(result["valid_roles"]) >= CANONICAL_ROLES

    def test_ledger_action_returns_ledgered_and_unledgered(self, tmp_path, monkeypatch):
        from watercooler.role_salience_lib import ledger_entry_for, append_ledger_entries, SalienceBullet

        wc_dir = tmp_path / ".watercooler"
        wc_dir.mkdir()
        (wc_dir / "roles.toml").write_text(
            '[roles.critic]\n'
            'description = "Critic"\n'
            'canonical_role = "critic"\n'
            'project_salience = ["watch for X", "hand-authored bullet"]\n'
        )
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])

        import watercooler_mcp.tools.roles as roles_mod

        monkeypatch.setattr(roles_mod, "_LEDGER_PATH", ledger_path)

        result = json.loads(
            _roles_impl(code_path=str(tmp_path), role="critic", action="ledger")
        )
        assert result["role"] == "critic"
        assert result["ledgered"] == ["watch for X"]
        assert result["unledgered"] == ["hand-authored bullet"]
        assert result["review_due"] == []

    def test_ledger_action_returns_review_due_bullets(self, tmp_path, monkeypatch):
        from watercooler.role_salience_lib import (
            ledger_entry_for,
            append_ledger_entries,
            SalienceBullet,
        )

        wc_dir = tmp_path / ".watercooler"
        wc_dir.mkdir()
        (wc_dir / "roles.toml").write_text(
            '[roles.critic]\n'
            'description = "Critic"\n'
            'canonical_role = "critic"\n'
            'project_salience = ["stale bullet"]\n'
        )
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(
            text="stale bullet", source_lesson_ulid="L1", review_after="2020-01-01"
        )
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])

        import watercooler_mcp.tools.roles as roles_mod

        monkeypatch.setattr(roles_mod, "_LEDGER_PATH", ledger_path)

        result = json.loads(
            _roles_impl(code_path=str(tmp_path), role="critic", action="ledger")
        )
        assert len(result["review_due"]) == 1
        assert result["review_due"][0]["text"] == "stale bullet"
        assert result["review_due"][0]["reason"] == "review_after_passed"

    def test_ledger_action_no_ledger_file_all_unledgered(self, tmp_path, monkeypatch):
        wc_dir = tmp_path / ".watercooler"
        wc_dir.mkdir()
        (wc_dir / "roles.toml").write_text(
            '[roles.critic]\n'
            'description = "Critic"\n'
            'canonical_role = "critic"\n'
            'project_salience = ["hand-authored"]\n'
        )
        import watercooler_mcp.tools.roles as roles_mod

        monkeypatch.setattr(roles_mod, "_LEDGER_PATH", tmp_path / "nonexistent.jsonl")

        result = json.loads(
            _roles_impl(code_path=str(tmp_path), role="critic", action="ledger")
        )
        assert result["ledgered"] == []
        assert result["unledgered"] == ["hand-authored"]

    def test_default_action_unaffected_by_ledger_addition(self, tmp_path):
        """action="" (default) still returns the normal catalog, not a
        ledger response."""
        result = json.loads(_roles_impl())
        assert set(result.keys()) == CANONICAL_ROLES


class TestCompileAction:
    """action="compile" — a dry-run preview of compiling candidate bullets
    into a role's salience, agent-reachable without local Python execution."""

    def test_compile_requires_role(self):
        out = json.loads(_roles_impl(action="compile", bullets=["x"]))
        assert out["error"] == "role_required_for_compile_action"

    def test_compile_unknown_role(self):
        out = json.loads(_roles_impl(role="nope", action="compile", bullets=["x"]))
        assert out["error"] == "unknown_role"
        assert "valid_roles" in out

    def test_compile_previews_accepted_and_dropped(self):
        out = json.loads(
            _roles_impl(
                role="critic",
                action="compile",
                bullets=["watch for stalled review loops", "x" * 200],
            )
        )
        assert out["role"] == "critic"
        assert "watch for stalled review loops" in out["accepted"]
        # The overlength bullet is dropped with a reason, not accepted.
        assert any("too_long" in d["reason"] for d in out["dropped"])
        assert isinstance(out["has_changes"], bool)

    def test_compile_flags_needs_rewrite(self):
        out = json.loads(
            _roles_impl(
                role="critic",
                action="compile",
                bullets=["critic must reject hidden authority"],
            )
        )
        assert out["needs_rewrite"]  # authority vocabulary → needs_rewrite
        assert not out["accepted"]

    def test_compile_writes_nothing(self, tmp_path, monkeypatch):
        """The preview must not persist anything (stays L1/L2)."""
        import watercooler_mcp.tools.roles as roles_mod

        ledger = tmp_path / "role_salience_ledger.jsonl"
        monkeypatch.setattr(roles_mod, "_LEDGER_PATH", ledger)
        _roles_impl(role="critic", action="compile", bullets=["watch for X"])
        assert not ledger.exists()

    def test_compile_no_bullets_is_noop_preview(self):
        out = json.loads(_roles_impl(role="critic", action="compile", bullets=[]))
        assert out["has_changes"] is False
