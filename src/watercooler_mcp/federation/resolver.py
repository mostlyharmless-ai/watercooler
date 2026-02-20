"""Read-only worktree discovery for federated namespaces.

Pure filesystem checks — no git operations. Uses _worktree_path_for() and
WORKTREE_BASE from watercooler_mcp.config for DRY worktree path resolution.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from watercooler.config_schema import FederationConfig, FederationNamespaceConfig
from watercooler_mcp.config import WORKTREE_BASE, ThreadContext, _worktree_path_for

__all__ = [
    "NamespaceResolution",
    "WorktreeStatus",
    "discover_namespace_worktree",
    "resolve_all_namespaces",
]

logger = logging.getLogger(__name__)


class WorktreeStatus(enum.Enum):
    """Non-path outcomes from worktree discovery."""

    SECURITY_REJECTED = "security_rejected"
    NOT_INITIALIZED = "not_initialized"


@dataclass(frozen=True)
class NamespaceResolution:
    """Result of resolving a single namespace."""

    namespace_id: str
    threads_dir: Path | None
    code_path: Path
    status: Literal["ok", "not_initialized", "security_rejected", "error"]
    is_primary: bool = False
    error_message: str = ""
    action_hint: str = ""


def discover_namespace_worktree(
    namespace_id: str,
    namespace_config: FederationNamespaceConfig,
) -> Path | WorktreeStatus:
    """Discover existing worktree via filesystem check.

    Security: rejects symlinked worktree paths and paths escaping WORKTREE_BASE.
    No git operations. Pure Path.exists() + Path.is_dir() + symlink check.

    IMPORTANT: Does NOT call resolve_thread_context() — that would trigger
    git operations and env var overrides (WATERCOOLER_DIR) that break
    config isolation.

    Returns:
        Resolved worktree path if exists, ``WorktreeStatus.SECURITY_REJECTED``
        if path fails security checks, ``WorktreeStatus.NOT_INITIALIZED``
        if worktree directory doesn't exist yet.
    """
    code_root = Path(namespace_config.code_path)
    worktree_path = _worktree_path_for(code_root)

    # Reject symlinks (best-effort check — TOCTOU gap exists but is acceptable for read-only search)
    if worktree_path.is_symlink():
        logger.warning(
            "Federation: worktree path is a symlink, rejecting: %s (namespace=%s)",
            worktree_path, namespace_id,
        )
        return WorktreeStatus.SECURITY_REJECTED

    # Verify resolved path stays under WORKTREE_BASE
    try:
        resolved = worktree_path.resolve()
        worktree_base_resolved = WORKTREE_BASE.resolve()
        resolved.relative_to(worktree_base_resolved)
    except ValueError:
        logger.warning(
            "Federation: worktree path escapes WORKTREE_BASE, rejecting: %s (namespace=%s)",
            resolved, namespace_id,
        )
        return WorktreeStatus.SECURITY_REJECTED
    except OSError:
        return WorktreeStatus.NOT_INITIALIZED

    if resolved.exists() and resolved.is_dir():
        return resolved

    return WorktreeStatus.NOT_INITIALIZED


def resolve_all_namespaces(
    primary_context: ThreadContext,
    federation_config: FederationConfig,
    namespace_override: list[str] | None = None,
) -> dict[str, NamespaceResolution]:
    """Resolve all configured (or overridden) namespaces.

    Primary: uses existing ThreadContext.threads_dir (already resolved).
    Secondaries: filesystem worktree discovery via discover_namespace_worktree().

    Args:
        primary_context: Resolved primary thread context.
        federation_config: Federation configuration.
        namespace_override: Optional list of namespace IDs to search
            (replaces configured namespaces).

    Returns:
        Dict of namespace_id -> NamespaceResolution.
    """
    results: dict[str, NamespaceResolution] = {}

    # Derive primary namespace ID from code_root basename.
    # Convention: the directory basename of the checkout IS the namespace ID.
    # This means renaming the checkout directory changes the ID, and the same
    # repo at different paths on different machines produces different IDs.
    # Callers should configure allowlists using the basename that matches
    # their checkout directory (e.g., "watercooler-cloud" for ~/projects/watercooler-cloud).
    primary_ns_id = primary_context.code_root.name if primary_context.code_root else "primary"

    # Guard: primary ID collides with a configured secondary namespace
    if primary_ns_id in federation_config.namespaces:
        logger.warning(
            "Secondary namespace '%s' collides with primary — skipping secondary",
            primary_ns_id,
        )

    # Add primary namespace
    results[primary_ns_id] = NamespaceResolution(
        namespace_id=primary_ns_id,
        threads_dir=primary_context.threads_dir,
        code_path=primary_context.code_root or Path("."),
        status="ok",
        is_primary=True,
    )

    # Determine which secondary namespaces to resolve
    if namespace_override is not None:
        ns_ids = [ns for ns in namespace_override if ns != primary_ns_id]
    else:
        ns_ids = [ns for ns in federation_config.namespaces if ns != primary_ns_id]

    for ns_id in ns_ids:
        ns_config = federation_config.namespaces.get(ns_id)
        if ns_config is None:
            results[ns_id] = NamespaceResolution(
                namespace_id=ns_id,
                threads_dir=None,
                code_path=Path("."),
                status="error",
                error_message=f"Namespace '{ns_id}' not found in federation config",
            )
            continue

        worktree = discover_namespace_worktree(ns_id, ns_config)
        if isinstance(worktree, Path):
            results[ns_id] = NamespaceResolution(
                namespace_id=ns_id,
                threads_dir=worktree,
                code_path=Path(ns_config.code_path),
                status="ok",
            )
        elif worktree is WorktreeStatus.SECURITY_REJECTED:
            results[ns_id] = NamespaceResolution(
                namespace_id=ns_id,
                threads_dir=None,
                code_path=Path(ns_config.code_path),
                status="security_rejected",
                error_message=(
                    f"Worktree path for namespace '{ns_id}' failed security checks "
                    f"(symlink or path escape)"
                ),
            )
        elif worktree is WorktreeStatus.NOT_INITIALIZED:
            results[ns_id] = NamespaceResolution(
                namespace_id=ns_id,
                threads_dir=None,
                code_path=Path(ns_config.code_path),
                status="not_initialized",
                action_hint=(
                    f"Run watercooler_health(code_path='{ns_config.code_path}') "
                    f"to bootstrap the worktree for namespace '{ns_id}'"
                ),
            )

    return results
