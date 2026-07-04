"""Shared project_salience resolution for stance producers.

Both stance producers (``ProjectCoordinatorDaemon`` and
``DecisionStanceDaemon``) need the same thing: per-role ``project_salience``
bullets from ``.watercooler/roles.toml``, reloaded when the file changes, with
a fail-safe fallback so a malformed project roles file degrades stance
production (no salience) rather than crashing it. This module is the single
implementation both producers share.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from watercooler.pulse_stance_lib import STANCE_ROLES
from watercooler.role_loader import load_roles

from .state import Finding, build_finding_id

logger = logging.getLogger(__name__)


class RoleSalienceCache:
    """Per-daemon mtime-aware cache of ``project_salience`` by role.

    Reloads ``.watercooler/roles.toml`` only when its mtime changes (or on
    first call for a given ``code_root``). On a load failure, falls back to
    empty salience for every role and returns a diagnostic ``Finding``,
    deduped on ``(daemon_name, error_type, message_hash)`` so a persistent
    parse error does not re-emit every tick — the message hash is included
    so a *different* error that happens to raise the same exception class
    (e.g. two distinct malformed-TOML reasons both raising ``ValueError``)
    still surfaces its own diagnostic rather than being silently masked by
    the prior one's dedup key.
    """

    def __init__(self) -> None:
        self._code_root: Path | None = None
        self._roles_mtime: float | None = None
        # Roles present in STANCE_ROLES but with no project_salience configured
        # are simply absent from this dict — absent key means empty tuple, by
        # contract with callers, all of which use .get(role, ()).
        self._salience: dict[str, tuple[str, ...]] = {}
        self._last_diagnostic_key: str | None = None

    def resolve(
        self,
        code_root: Path | None,
        *,
        daemon_name: str,
        scope_id: str,
    ) -> tuple[dict[str, tuple[str, ...]], Finding | None]:
        """Return ``(project_salience_by_role, diagnostic_finding_or_none)``.

        Args:
            code_root: Project repo root, or None when unresolved (returns
                empty salience, no diagnostic — this is the normal
                disk-less/hosted case, not an error).
            daemon_name: Calling daemon's name, for the diagnostic Finding
                and dedup scoping.
            scope_id: Repo scope id, for deterministic finding IDs.
        """
        if code_root is None:
            return {}, None

        roles_path = Path(code_root) / ".watercooler" / "roles.toml"
        try:
            mtime = roles_path.stat().st_mtime if roles_path.is_file() else None
        except OSError:
            mtime = None

        if self._code_root == code_root and mtime == self._roles_mtime:
            return self._salience, None

        diagnostic: Finding | None = None
        try:
            loaded = load_roles(code_root)
            self._salience = {
                role: tuple(loaded[role].project_salience)
                for role in STANCE_ROLES
                if role in loaded and loaded[role].project_salience
            }
            self._last_diagnostic_key = None
        except Exception as exc:
            error_type = type(exc).__name__
            message_hash = hashlib.sha256(str(exc).encode("utf-8")).hexdigest()[:12]
            diagnostic_key = f"{error_type}:{message_hash}"
            self._salience = {}
            if diagnostic_key != self._last_diagnostic_key:
                self._last_diagnostic_key = diagnostic_key
                diagnostic = Finding(
                    finding_id=build_finding_id(
                        scope_id=scope_id,
                        daemon_name=daemon_name,
                        topic="role_salience_diagnostic",
                        category="role_salience_diagnostic",
                        entry_id="",
                        dedup_signature=diagnostic_key,
                    ),
                    daemon_name=daemon_name,
                    severity="warning",
                    category="role_salience_diagnostic",
                    topic="role_salience_diagnostic",
                    entry_id="",
                    # Scope to this repo so a malformed roles.toml in one
                    # repo doesn't leak its warning into every other
                    # repo's Stop-hook sessions on the same machine
                    # (Finding.repo="" is treated as "show everywhere" by
                    # stop_hook._repo_matches).
                    repo=str(code_root),
                    message=(
                        f"project_salience disabled: could not load roles.toml "
                        f"({error_type})"
                    ),
                    details={
                        "advisory_only": True,
                        "error_type": error_type,
                        "path": str(roles_path),
                        "effect": "stance_salience_disabled",
                    },
                )
            logger.warning(
                "DAEMON[%s]: could not load project_salience from %s: %s",
                daemon_name,
                roles_path,
                exc,
            )

        self._code_root = code_root
        self._roles_mtime = mtime
        return self._salience, diagnostic
