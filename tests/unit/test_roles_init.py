"""Unit tests for commands.roles_init()."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.commands import roles_init

# ---------------------------------------------------------------------------
# Fresh-state behavior
# ---------------------------------------------------------------------------


def test_creates_dotwatercooler_when_missing(tmp_path, capsys):
    """Fresh project (no .watercooler/) → directory + roles.toml created, exit 0."""
    rc = roles_init(project_path=tmp_path)
    assert rc == 0

    target = tmp_path / ".watercooler" / "roles.toml"
    assert target.exists()
    assert target.is_file()
    assert (tmp_path / ".watercooler").is_dir()

    out = capsys.readouterr().out
    assert "✅ Created project roles" in out
    assert str(target) in out


def test_writes_commented_stub_byte_identical(tmp_path):
    """Scaffolded file must be byte-identical to the bundled commented stub."""
    rc = roles_init(project_path=tmp_path)
    assert rc == 0

    stub = (files("watercooler") / "templates" / "roles.project-stub.toml").read_bytes()
    written = (tmp_path / ".watercooler" / "roles.toml").read_bytes()
    assert written == stub


def test_creates_parent_dir_when_dotwatercooler_does_not_exist(tmp_path):
    """`.watercooler/` is created with parents=True so deep targets work too."""
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    rc = roles_init(project_path=nested)
    assert rc == 0
    assert (nested / ".watercooler" / "roles.toml").exists()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_when_file_exists(tmp_path, capsys):
    """Pre-existing roles.toml + force=False → no-op, exit 0, content unchanged."""
    target_dir = tmp_path / ".watercooler"
    target_dir.mkdir()
    target = target_dir / "roles.toml"
    custom = b"# user's custom roles file\n[roles.foo]\n"
    target.write_bytes(custom)

    rc = roles_init(project_path=tmp_path, force=False)
    assert rc == 0

    # Content must NOT have been overwritten.
    assert target.read_bytes() == custom

    out = capsys.readouterr().out
    assert "already initialized" in out
    assert "--force" in out


def test_force_overwrites_existing_and_backs_up(tmp_path, capsys):
    """force=True → re-scaffold from the stub and back the old file up first."""
    target_dir = tmp_path / ".watercooler"
    target_dir.mkdir()
    target = target_dir / "roles.toml"
    custom = b"# user's custom roles file\n"
    target.write_bytes(custom)

    rc = roles_init(project_path=tmp_path, force=True)
    assert rc == 0

    stub = (files("watercooler") / "templates" / "roles.project-stub.toml").read_bytes()
    assert target.read_bytes() == stub

    # The prior contents were preserved in a timestamped backup.
    backups = [
        p
        for p in target_dir.glob("roles.toml.bak-*")
        if p.read_bytes() == custom
    ]
    assert backups, "expected a backup of the overwritten roles.toml"
    assert "Backed up previous roles" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Atomic write / failure handling
# ---------------------------------------------------------------------------


def test_atomic_write_failure_cleans_up_tempfile(tmp_path, capsys):
    """If os.replace raises, the temp file is cleaned up and exit 1 returned."""
    target = tmp_path / ".watercooler" / "roles.toml"

    # Patch os.replace at the call site (commands module). The function imports
    # os locally, so we patch the os module's `replace`.
    with patch("os.replace", side_effect=OSError("simulated rename failure")):
        rc = roles_init(project_path=tmp_path)

    assert rc == 1
    assert not target.exists()  # final target never written

    # No leftover .tmp files in .watercooler/
    leftovers = list((tmp_path / ".watercooler").glob("roles_*.tmp"))
    assert leftovers == [], f"expected no temp leftovers, found: {leftovers}"

    err = capsys.readouterr().err
    assert "❌" in err
    assert "Failed to initialize roles" in err


def test_mkdir_failure_when_dotwatercooler_is_a_regular_file(tmp_path, capsys):
    """``.watercooler`` already existing as a regular file is reported cleanly.

    Regression test for the Codex review on PR `feat/roles-init-scaffold`:
    ``target_dir.mkdir()`` originally lived outside the OSError handler, so a
    pre-existing regular file at ``.watercooler`` produced an uncaught
    FileExistsError traceback instead of the documented ❌ failure + rc=1.
    """
    blocker = tmp_path / ".watercooler"
    blocker.write_bytes(b"not a directory")

    rc = roles_init(project_path=tmp_path)

    assert rc == 1
    err = capsys.readouterr().err
    assert "❌" in err
    assert "Failed to initialize roles" in err
    # The blocker file is untouched.
    assert blocker.read_bytes() == b"not a directory"


def test_mkdir_permission_failure_returns_error(tmp_path, capsys):
    """If mkdir raises PermissionError, return 1 with stderr message — no traceback."""
    with patch(
        "pathlib.Path.mkdir",
        side_effect=PermissionError("simulated read-only filesystem"),
    ):
        rc = roles_init(project_path=tmp_path)

    assert rc == 1
    assert not (tmp_path / ".watercooler" / "roles.toml").exists()
    err = capsys.readouterr().err
    assert "❌" in err
    assert "Failed to initialize roles" in err


def test_bundled_read_failure_returns_error(tmp_path, capsys):
    """If reading the bundled stub fails, return 1 with stderr message."""
    # The shared scaffolder binds ``files`` at import time, so patch the name
    # in that module (not importlib.resources).
    with patch(
        "watercooler.roles_scaffold.files",
        side_effect=RuntimeError("simulated package-data failure"),
    ):
        rc = roles_init(project_path=tmp_path)

    assert rc == 1
    err = capsys.readouterr().err
    assert "❌" in err
    assert "Failed to initialize roles" in err


# ---------------------------------------------------------------------------
# Project-path argument honored
# ---------------------------------------------------------------------------


def test_project_path_override_respected(tmp_path):
    """When project_path points elsewhere, the file lands there — not in cwd."""
    other = tmp_path / "other-project"
    other.mkdir()
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()

    # Even if cwd is somewhere else, output must follow project_path.
    old_cwd = Path.cwd()
    try:
        os.chdir(cwd_dir)
        rc = roles_init(project_path=other)
    finally:
        os.chdir(old_cwd)

    assert rc == 0
    assert (other / ".watercooler" / "roles.toml").exists()
    assert not (cwd_dir / ".watercooler").exists()


# ---------------------------------------------------------------------------
# Output observability
# ---------------------------------------------------------------------------


def test_success_message_includes_target_path_and_pointer_to_docs(tmp_path, capsys):
    """Success output should give the user the path and where to learn more."""
    rc = roles_init(project_path=tmp_path)
    assert rc == 0

    out = capsys.readouterr().out
    target = tmp_path / ".watercooler" / "roles.toml"
    assert str(target) in out
    assert "ROLES_CREATION.md" in out


@pytest.mark.parametrize("force", [False, True])
def test_returns_zero_on_clean_state_regardless_of_force(tmp_path, force):
    """Clean state succeeds whether or not --force was passed."""
    rc = roles_init(project_path=tmp_path, force=force)
    assert rc == 0
    assert (tmp_path / ".watercooler" / "roles.toml").exists()
