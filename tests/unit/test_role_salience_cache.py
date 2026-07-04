"""Tests for watercooler_mcp.daemons.role_salience.RoleSalienceCache."""

from __future__ import annotations

import time

from watercooler_mcp.daemons.role_salience import RoleSalienceCache


def _write_roles(tmp_path, body: str) -> None:
    wc_dir = tmp_path / ".watercooler"
    wc_dir.mkdir(exist_ok=True)
    (wc_dir / "roles.toml").write_text(body)


def test_no_code_root_returns_empty_no_diagnostic():
    cache = RoleSalienceCache()
    salience, diag = cache.resolve(None, daemon_name="d", scope_id="s")
    assert salience == {}
    assert diag is None


def test_no_project_roles_file_returns_empty(tmp_path):
    cache = RoleSalienceCache()
    salience, diag = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert salience == {}
    assert diag is None


def test_loads_project_salience_for_stance_roles(tmp_path):
    _write_roles(
        tmp_path,
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["watch for X"]\n',
    )
    cache = RoleSalienceCache()
    salience, diag = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert salience == {"critic": ("watch for X",)}
    assert diag is None


def test_role_with_no_salience_is_omitted(tmp_path):
    _write_roles(
        tmp_path,
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n',
    )
    cache = RoleSalienceCache()
    salience, _ = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert "critic" not in salience


def test_cache_hit_without_mtime_change(tmp_path):
    _write_roles(
        tmp_path,
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["watch for X"]\n',
    )
    cache = RoleSalienceCache()
    first, _ = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    second, _ = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert first == second == {"critic": ("watch for X",)}


def test_reloads_on_mtime_change(tmp_path):
    _write_roles(
        tmp_path,
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["first"]\n',
    )
    cache = RoleSalienceCache()
    first, _ = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert first == {"critic": ("first",)}

    time.sleep(0.01)
    _write_roles(
        tmp_path,
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["second"]\n',
    )
    second, _ = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert second == {"critic": ("second",)}


def test_malformed_roles_toml_falls_back_with_diagnostic(tmp_path):
    _write_roles(tmp_path, "not valid toml [[[")
    cache = RoleSalienceCache()
    salience, diag = cache.resolve(tmp_path, daemon_name="decision_stance", scope_id="s")
    assert salience == {}
    assert diag is not None
    assert diag.category == "role_salience_diagnostic"
    assert diag.severity == "warning"
    assert diag.details["effect"] == "stance_salience_disabled"
    assert diag.details["error_type"] == "ValueError"
    assert diag.repo == str(tmp_path)


def test_malformed_roles_toml_diagnostic_deduped_when_identical_message(tmp_path):
    """Repeated calls with the exact same unfixed error must not re-emit."""
    _write_roles(tmp_path, "not valid toml [[[")
    cache = RoleSalienceCache()
    _, diag1 = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert diag1 is not None

    # Touch the file with byte-identical content so the mtime changes (forcing
    # a reload) but the resulting parse error message is unchanged.
    time.sleep(0.01)
    _write_roles(tmp_path, "not valid toml [[[")
    _, diag2 = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert diag2 is None


def test_malformed_roles_toml_diagnostic_re_emits_on_distinct_error_message(
    tmp_path,
):
    """Two distinct malformed-file reasons raising the same exception class
    (ValueError) must each surface their own diagnostic — dedup-by-class-name
    alone would silently mask the second, different failure."""
    _write_roles(tmp_path, "not valid toml [[[")
    cache = RoleSalienceCache()
    _, diag1 = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert diag1 is not None

    time.sleep(0.01)
    _write_roles(tmp_path, "[roles.critic]\nproject_salience = \"not a list\"\n")
    salience, diag2 = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert salience == {}
    assert diag2 is not None
    assert diag2.details["error_type"] == "ValueError"


def test_diagnostic_re_emits_when_error_type_changes(tmp_path):
    _write_roles(tmp_path, "not valid toml [[[")
    cache = RoleSalienceCache()
    _, diag1 = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert diag1 is not None
    assert diag1.details["error_type"] == "ValueError"

    # Recovery: fix the file — next resolve should succeed with no diagnostic.
    time.sleep(0.01)
    _write_roles(
        tmp_path,
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["fixed"]\n',
    )
    salience, diag2 = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert diag2 is None
    assert salience == {"critic": ("fixed",)}


def test_roles_toml_deleted_after_successful_load_falls_back(tmp_path):
    """If roles.toml is removed after a successful load, the next resolve
    must fall back to bundled defaults (no project_salience) rather than
    keep serving stale cached salience."""
    _write_roles(
        tmp_path,
        '[roles.critic]\n'
        'description = "Critic"\n'
        'canonical_role = "critic"\n'
        'project_salience = ["watch for X"]\n',
    )
    cache = RoleSalienceCache()
    first, diag1 = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert first == {"critic": ("watch for X",)}
    assert diag1 is None

    (tmp_path / ".watercooler" / "roles.toml").unlink()
    second, diag2 = cache.resolve(tmp_path, daemon_name="d", scope_id="s")
    assert second == {}
    assert diag2 is None  # missing file falls back to bundled defaults, not an error
