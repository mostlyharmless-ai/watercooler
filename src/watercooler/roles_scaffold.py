"""Shared scaffolder for the project roles override file.

Both the CLI ``watercooler roles init`` (:func:`watercooler.commands.roles_init`)
and the MCP ``watercooler_init`` tool write the SAME bytes through this one
function, so a human and an agent re-initialize ``.watercooler/roles.toml`` to
identical content. Keeping a single scaffolder is what guarantees the
human/agent symmetry the new-repo-init work exists to provide.

The scaffolded file is a fully *commented* stub (``templates/roles.project-stub.toml``):
it overrides nothing as written, so an untouched repo always tracks the current
bundled role defaults, and customization is an explicit uncomment-and-edit. An
*active* copy would silently pin the repo to stale roles across upgrades.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Optional

# Status values for :class:`RolesScaffoldResult.status`.
STATUS_CREATED = "created"
STATUS_EXISTS = "exists"
STATUS_SKIPPED_READONLY = "skipped_readonly"


@dataclass(frozen=True)
class RolesScaffoldResult:
    """Outcome of a roles-file scaffold attempt.

    Attributes:
        status: ``"created"`` (wrote the stub), ``"exists"`` (present, left
            untouched because ``force`` was False), or ``"skipped_readonly"``
            (could not write — unwritable tree or missing bundled stub).
        target_path: The ``.watercooler/roles.toml`` path acted on.
        backup_path: When ``force`` overwrote an existing file, the path the
            previous contents were moved to (so customizations aren't lost).
        error: Human-readable reason when ``status`` is ``"skipped_readonly"``.
    """

    status: str
    target_path: Path
    backup_path: Optional[Path] = None
    error: Optional[str] = None


def _load_stub_bytes() -> bytes:
    """Read the bundled commented roles stub from package data."""
    resource = files("watercooler") / "templates" / "roles.project-stub.toml"
    return resource.read_bytes()


def _backup_suffix() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f".bak-{stamp}"


def scaffold_roles_file(
    project_path: Path, *, force: bool = False
) -> RolesScaffoldResult:
    """Create ``<project_path>/.watercooler/roles.toml`` from the commented stub.

    Create-only by default: an existing file is left untouched (``status``
    ``"exists"``) so an edited override is never clobbered. With ``force=True``,
    an existing file is first moved aside to a timestamped ``.bak-*`` backup,
    then replaced — so a re-scaffold never silently destroys customization.

    Args:
        project_path: Project (code) repo root; ``.watercooler/`` is created
            under it.
        force: Re-scaffold even if the file exists, backing the old one up.

    Returns:
        A :class:`RolesScaffoldResult` describing what happened.
    """
    target_dir = Path(project_path) / ".watercooler"
    target_path = target_dir / "roles.toml"

    if target_path.exists() and not force:
        return RolesScaffoldResult(status=STATUS_EXISTS, target_path=target_path)

    try:
        content = _load_stub_bytes()
    except Exception as exc:  # pragma: no cover - packaging failure
        return RolesScaffoldResult(
            status=STATUS_SKIPPED_READONLY,
            target_path=target_path,
            error=f"could not read bundled roles stub: {exc}",
        )

    backup_path: Optional[Path] = None
    tmp_path_str: Optional[str] = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # Write the replacement to a temp file FIRST, so the realistic failure
        # points (disk full, permissions) occur before the existing file is
        # touched. Only then move the original aside and swap the new one in —
        # a failed force re-scaffold must never disable an existing roles.toml.
        fd, tmp_path_str = tempfile.mkstemp(
            dir=target_dir, suffix=".tmp", prefix="roles_"
        )
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        if target_path.exists() and force:
            backup_path = target_path.with_name(target_path.name + _backup_suffix())
            os.replace(target_path, backup_path)
        os.replace(tmp_path_str, target_path)
    except OSError as exc:
        if tmp_path_str:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
        # If the original was already moved to backup but the final swap failed,
        # restore it so the repo is left exactly as we found it.
        if backup_path is not None and not target_path.exists():
            try:
                os.replace(backup_path, target_path)
                backup_path = None
            except OSError:
                pass
        return RolesScaffoldResult(
            status=STATUS_SKIPPED_READONLY,
            target_path=target_path,
            backup_path=backup_path,
            error=str(exc),
        )

    return RolesScaffoldResult(
        status=STATUS_CREATED, target_path=target_path, backup_path=backup_path
    )
