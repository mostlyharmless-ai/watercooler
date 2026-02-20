"""Read-only worktree discovery for federated namespaces.

Pure filesystem checks — no git operations. Uses _worktree_path_for() and
WORKTREE_BASE from watercooler_mcp.config for DRY worktree path resolution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from watercooler.config_schema import FederationConfig, FederationNamespaceConfig
from watercooler_mcp.config import WORKTREE_BASE, ThreadContext, _worktree_path_for

__all__ = [
    "NamespaceResolution",
    "discover_namespace_worktree",
    "resolve_all_namespaces",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NamespaceResolution:
    """Result of resolving a single namespace."""

    namespace_id: str
    threads_dir: Path | None
    code_path: Path
    status: Literal["ok", "not_initialized", "error"]
    is_primary: bool = False
    error_message: str = ""
    action_hint: str = ""


def discover_namespace_worktree(
    namespace_id: str,
    namespace_config: FederationNamespaceConfig,
) -> Path | None:
    """Discover existing worktree via filesystem check.

    Security: rejects symlinked worktree paths and paths escaping WORKTREE_BASE.
    No git operations. Pure Path.exists() + Path.is_dir() + symlink check.

    IMPORTANT: Does NOT call resolve_thread_context() — that would trigger
    git operations and env var overrides (WATERCOOLER_DIR) that break
    config isolation.

    Returns:
        Resolved worktree path if exists, None if not initialized.
    """
    code_root = Path(namespace_config.code_path)
    worktree_path = _worktree_path_for(code_root)

    # Reject symlinks (TOCTOU mitigation)
    if worktree_path.is_symlink():
        logger.warning(
            "Federation: worktree path is a symlink, rejecting: %s (namespace=%s)",
            worktree_path, namespace_id,
        )
        return None

    # Verify resolved path stays under WORKTREE_BASE
    try:
        resolved = worktree_path.resolve()
        worktree_base_resolved = WORKTREE_BASE.resolve()
        if not str(resolved).startswith(str(worktree_base_resolved) + "/"):
            logger.warning(
                "Federation: worktree path escapes WORKTREE_BASE, rejecting: %s (namespace=%s)",
                resolved, namespace_id,
            )
            return None
    except (OSError, ValueError):
        return None

    if worktree_path.exists() and worktree_path.is_dir():
        return worktree_path

    return None


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

    # Derive primary namespace ID from code_root basename
    primary_ns_id = primary_context.code_root.name if primary_context.code_root else "primary"

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
        ns_ids = list(federation_config.namespaces.keys())

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
        if worktree is not None:
            results[ns_id] = NamespaceResolution(
                namespace_id=ns_id,
                threads_dir=worktree,
                code_path=Path(ns_config.code_path),
                status="ok",
            )
        else:
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
