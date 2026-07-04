"""Role definition loader for the Watercooler protocol.

Loads role definitions by merging bundled defaults with an optional project-level
.watercooler/roles.toml override. Provides validation for role values at write time.

This module has no MCP dependencies — it is safe to import from the core library,
CLI, and MCP server alike.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Optional

# TOML loading: tomllib (3.11+) with tomli fallback (same pattern as config_loader)
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            tomllib = None  # type: ignore

PROJECT_ROLES_FILE = "roles.toml"
PROJECT_CONFIG_DIR = ".watercooler"


@dataclass
class RoleDefinition:
    """Full behavioral specification for a watercooler role."""

    name: str
    description: str
    canonical_role: str
    produces: list[str] = field(default_factory=list)
    boundary: str = ""
    handoff_to: list[str] = field(default_factory=list)
    instructions: str = ""
    entry_style: str = ""
    when_to_use: str = ""
    collaborate_with: str = ""
    project_salience: list[str] = field(default_factory=list)


def _load_toml_bytes(data: bytes) -> dict:
    if tomllib is None:
        raise RuntimeError(
            "TOML support requires Python 3.11+ or the 'tomli' package. "
            "Install with: pip install tomli"
        )
    return tomllib.loads(data.decode())


def _validate_project_salience(name: str, value: object) -> list[str]:
    """Type-guard ``project_salience``: list[str] of non-empty trimmed strings.

    Raises ValueError on malformed input (fail-loud catalog contract, PR #685).
    Length/count caps are enforced by the compiler, not here.

    Also rejects entries containing C0/C1 control or escape bytes: a bullet
    persists into the committed ``roles.toml`` and is later echoed to the
    terminal via the Stop hook, so a control byte would become a
    version-controlled terminal-escape-injection payload. This is a *safety*
    check (fail-loud at the boundary), not a policy cap — it stays here even
    though length/count caps deliberately live in the compiler.
    """
    if not isinstance(value, list):
        raise ValueError(
            f"Invalid project_salience for role {name!r}: expected a list of strings, "
            f"got {type(value).__name__}."
        )
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"Invalid project_salience for role {name!r}: every entry must be a "
                f"non-empty string, got {item!r}."
            )
        if any((ord(c) < 0x20 and c != "\t") or 0x7F <= ord(c) <= 0x9F for c in item):
            raise ValueError(
                f"Invalid project_salience for role {name!r}: entry contains control "
                f"or escape characters, got {item!r}."
            )
    return [item.strip() for item in value]


def _parse_roles(data: dict) -> dict[str, RoleDefinition]:
    """Parse a roles TOML dict into RoleDefinition objects."""
    roles: dict[str, RoleDefinition] = {}
    for name, spec in data.get("roles", {}).items():
        roles[name] = RoleDefinition(
            name=name,
            description=spec.get("description", ""),
            canonical_role=spec.get("canonical_role", name),
            produces=spec.get("produces", []),
            boundary=spec.get("boundary", ""),
            handoff_to=spec.get("handoff_to", []),
            instructions=spec.get("instructions", ""),
            entry_style=spec.get("entry_style", ""),
            when_to_use=spec.get("when_to_use", ""),
            collaborate_with=spec.get("collaborate_with", ""),
            project_salience=_validate_project_salience(
                name, spec.get("project_salience", [])
            ),
        )
    return roles


def _load_bundled_defaults() -> dict[str, RoleDefinition]:
    """Load the canonical 6-role defaults bundled with the package."""
    data_pkg = files("watercooler") / "data" / "roles.toml"
    raw = data_pkg.read_bytes()
    return _parse_roles(_load_toml_bytes(raw))


def _find_project_roles_file(code_path: Path) -> Optional[Path]:
    """Walk up from code_path to find .watercooler/roles.toml.

    Returns None if not found. Does NOT fall back to cwd.
    """
    current = code_path.resolve() if not code_path.is_absolute() else code_path
    while current != current.parent:
        candidate = current / PROJECT_CONFIG_DIR / PROJECT_ROLES_FILE
        if candidate.is_file():
            return candidate
        current = current.parent
    return None


def load_roles(code_path: Optional[str | Path] = None) -> dict[str, RoleDefinition]:
    """Load role definitions, merging bundled defaults with project overrides.

    Args:
        code_path: Root of the project repo. When None, returns bundled defaults only.
            Never implicitly reads from the process working directory — callers that want
            project-scoped roles must supply this explicitly.

    Returns:
        Dict of role name → RoleDefinition, with project entries overriding bundled ones.

    Raises:
        ValueError: If the project's ``.watercooler/roles.toml`` is present but cannot
            be parsed (syntax error, permission error, etc.). The message names the
            file and the underlying error so the user can fix and retry.
    """
    defaults = _load_bundled_defaults()

    if code_path is None:
        return defaults

    project_file = _find_project_roles_file(Path(code_path))
    if project_file is None:
        return defaults

    try:
        raw = project_file.read_bytes()
        overrides = _parse_roles(_load_toml_bytes(raw))
    except Exception as exc:
        raise ValueError(
            f"Could not load project roles from {project_file}: {exc}. "
            f"Fix the file and retry, or remove it to use bundled defaults."
        ) from exc

    merged = dict(defaults)
    merged.update(overrides)
    return merged


def validate_role(
    role: Optional[str],
    code_path: Optional[str | Path] = None,
) -> Optional[str]:
    """Validate a role value against the active role set.

    Args:
        role: The role string to validate. None and empty string are returned unchanged
            without validation — callers using default role values must not be rejected.
        code_path: Project repo root for project-scoped validation. None means bundled
            defaults only.

    Returns:
        The normalized (lowercase, stripped) role name if valid, or None/"" unchanged.

    Raises:
        ValueError: If role is non-empty and not in the active role set. The message
            lists all valid role names.
    """
    if not role:
        return role

    normalized = role.strip().lower()
    active_roles = load_roles(code_path)

    if normalized in active_roles:
        return normalized

    valid = sorted(active_roles.keys())
    raise ValueError(
        f"Invalid role {role!r}. Valid roles are: {', '.join(valid)}"
    )
