"""Unit tests for the shared roles scaffolder (watercooler.roles_scaffold).

The CLI ``roles init`` and the MCP ``watercooler_init`` tool both depend on the
structured result this returns, so the status enum + backup_path contract is
tested directly here.
"""

from __future__ import annotations

from importlib.resources import files

import watercooler.roles_scaffold as roles_scaffold
from watercooler.roles_scaffold import (
    STATUS_CREATED,
    STATUS_EXISTS,
    STATUS_SKIPPED_READONLY,
    scaffold_roles_file,
)

STUB = (files("watercooler") / "templates" / "roles.project-stub.toml").read_bytes()


def test_created_writes_stub_bytes(tmp_path):
    result = scaffold_roles_file(tmp_path)
    assert result.status == STATUS_CREATED
    assert result.target_path == tmp_path / ".watercooler" / "roles.toml"
    assert result.backup_path is None
    assert result.target_path.read_bytes() == STUB


def test_exists_is_create_only(tmp_path):
    target = tmp_path / ".watercooler" / "roles.toml"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# edited\n")

    result = scaffold_roles_file(tmp_path)
    assert result.status == STATUS_EXISTS
    assert result.backup_path is None
    assert target.read_bytes() == b"# edited\n"  # untouched


def test_force_backs_up_then_replaces(tmp_path):
    target = tmp_path / ".watercooler" / "roles.toml"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# edited\n")

    result = scaffold_roles_file(tmp_path, force=True)
    assert result.status == STATUS_CREATED
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == b"# edited\n"
    assert target.read_bytes() == STUB


def test_unwritable_tree_reports_skipped_readonly(tmp_path):
    # A regular file where .watercooler/ should be → mkdir/write fails cleanly.
    (tmp_path / ".watercooler").write_bytes(b"not a dir")
    result = scaffold_roles_file(tmp_path)
    assert result.status == STATUS_SKIPPED_READONLY
    assert result.error


def test_force_failure_preserves_original(tmp_path, monkeypatch):
    """A failed force re-scaffold must leave the existing roles.toml in place (#021)."""
    target = tmp_path / ".watercooler" / "roles.toml"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# precious user edits\n")

    # Fail ONLY the temp → target swap (the original has by then been moved to
    # backup). The except path must restore the backup, so fail on the ".tmp"
    # source only and let the backup-restore replace succeed.
    real_replace = roles_scaffold.os.replace

    def flaky_replace(src, dst):
        if str(src).endswith(".tmp"):
            raise OSError("simulated disk full on final swap")
        return real_replace(src, dst)

    monkeypatch.setattr(roles_scaffold.os, "replace", flaky_replace)

    result = scaffold_roles_file(tmp_path, force=True)
    assert result.status == STATUS_SKIPPED_READONLY
    # The repo is left exactly as we found it — original content intact.
    assert target.is_file()
    assert target.read_bytes() == b"# precious user edits\n"
    # No orphan backup left dangling once the original is restored.
    assert not list(target.parent.glob("roles.toml.bak-*"))
