"""Verify the bundled assets a fresh (e.g. uvx) install needs are resolvable.

A user who installs only the MCP server gets no dev checkout, so anything read
from the source tree via ``__file__`` rather than packaged via
``importlib.resources`` is invisible to them. This check resolves each bundled
asset the way runtime code does and reports any that are missing — the
``packaged_assets_ok`` signal in the setup-readiness report.
"""

from __future__ import annotations

from importlib.resources import files
from typing import List, Tuple

# (relative-path-tuple, human label) for each bundled asset runtime code reads.
_REQUIRED_ASSETS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("data", "roles.toml"), "bundled role defaults"),
    (("templates", "roles.project-stub.toml"), "project roles stub"),
    (("templates", "config.example.toml"), "config example"),
    (("schemas", "thread_entry.schema.json"), "thread-entry schema"),
    (("schemas", "watercooler_thread.schema.json"), "thread schema"),
)


def check_packaged_assets() -> Tuple[bool, List[str]]:
    """Return ``(ok, missing)`` for the bundled assets a fresh install needs.

    ``missing`` is a list of human labels for assets that did not resolve as
    packaged files; ``ok`` is True only when the list is empty.
    """
    missing: List[str] = []
    for parts, label in _REQUIRED_ASSETS:
        resource = files("watercooler")
        for part in parts:
            resource = resource / part
        try:
            if not resource.is_file():
                missing.append(label)
        except (OSError, ModuleNotFoundError):  # pragma: no cover - packaging failure
            missing.append(label)
    return (not missing, missing)
