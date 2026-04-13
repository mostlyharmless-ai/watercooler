"""Sync Guard Daemon — proactive worktree parity checker.

Periodically checks sync parity between the local threads worktree and
the remote, auto-healing bounded cases (behind-only, dirty-derived).
Emits warning findings for states that require manual intervention.

This complements the reactive ``ensure_readable()`` path: if no reads
occur for a while, divergence compounds silently. The sync guard
detects and resolves drift before it becomes a problem.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from .base import BaseDaemon
from .state import Finding

logger = logging.getLogger(__name__)


def _make_finding(
    category: str,
    message: str,
    *,
    severity: str = "info",
    parity_state: str = "",
) -> Finding:
    """Create a sync_guard finding."""
    import time

    from ulid import ULID

    return Finding(
        finding_id=str(ULID()),
        daemon_name="sync_guard",
        category=category,
        topic="",
        message=message,
        severity=severity,
        details={"parity_state": parity_state} if parity_state else {},
        created_at=time.time(),
    )


class SyncGuardDaemon(BaseDaemon):
    """Proactive worktree parity checker.

    Runs every ``interval`` seconds, checks the threads worktree parity
    state, and auto-heals safe cases (behind-only, dirty-derived-only).
    Emits warning findings for states that need manual repair.

    Args:
        interval: Seconds between parity checks.
        threads_dir: Override threads directory (None = resolve at tick time).
    """

    def __init__(
        self,
        *,
        interval: float = 180.0,
        threads_dir: Optional[Path] = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            name="sync_guard",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._threads_dir_override = threads_dir
        self._resolved_threads_dir: Optional[Path] = None

    def _resolve_threads_dir(self) -> Optional[Path]:
        """Resolve the threads directory.

        Cached after first successful resolution to avoid CWD drift
        in a long-running background thread.
        """
        if self._threads_dir_override is not None:
            return self._threads_dir_override

        if self._resolved_threads_dir is not None:
            return self._resolved_threads_dir

        try:
            from watercooler_mcp.config import resolve_thread_context

            ctx = resolve_thread_context(Path.cwd())
            self._resolved_threads_dir = ctx.threads_dir
            return self._resolved_threads_dir
        except Exception as exc:
            logger.debug("DAEMON[sync_guard]: could not resolve threads_dir: %s", exc)
            return None

    def tick(self) -> List[Finding]:
        """Run one parity check cycle."""
        from .hosted_data import is_daemon_hosted_mode

        if is_daemon_hosted_mode() and self._threads_dir_override is None:
            return []

        threads_dir = self._resolve_threads_dir()
        if threads_dir is None or not threads_dir.exists():
            return []

        # Check for .git dir / worktree marker
        git_dir = threads_dir / ".git"
        if not git_dir.exists() and not (threads_dir / "HEAD").exists():
            return []

        try:
            from git import Repo

            from watercooler_mcp.sync.primitives import (
                fetch_with_timeout,
                get_parity_state,
                pull_ff_only,
            )

            repo = Repo(threads_dir)
        except Exception as exc:
            logger.debug("DAEMON[sync_guard]: could not open repo: %s", exc)
            return []

        # Fetch first for accurate parity
        try:
            fetch_with_timeout(repo, timeout=15)
        except Exception as exc:
            logger.debug("DAEMON[sync_guard]: fetch failed: %s", exc)
            # Continue — parity check may still be useful with stale refs

        parity = get_parity_state(repo)

        # No-op states
        if parity in ("clean", "ahead_only", "no_upstream"):
            return []

        # Auto-heal: behind only — fast-forward pull
        if parity == "behind_only":
            try:
                if pull_ff_only(repo):
                    return [
                        _make_finding(
                            "sync_guard_healed",
                            "Worktree was behind remote; fast-forward pull succeeded.",
                            parity_state="behind_only",
                        )
                    ]
            except Exception as exc:
                logger.debug("DAEMON[sync_guard]: pull failed: %s", exc)
                return [
                    _make_finding(
                        "sync_guard_warning",
                        f"Worktree behind remote but pull failed: {exc}",
                        severity="warning",
                        parity_state="behind_only",
                    )
                ]

        # Auto-heal: dirty derived files — clean + pull if needed
        if parity == "dirty_derived_only":
            try:
                from watercooler.sync_repair import DERIVED_FILE_PATTERNS

                status_out = repo.git.status("--porcelain")
                for line in status_out.strip().split("\n"):
                    if not line.strip():
                        continue
                    filename = line[3:].split(" -> ")[-1].strip()
                    if Path(filename).name not in DERIVED_FILE_PATTERNS:
                        continue
                    filepath = threads_dir / filename
                    if filepath.exists():
                        filepath.unlink()
                    try:
                        repo.git.checkout("--", filename)
                    except Exception:
                        pass  # Untracked derived files won't have index entry

                # Re-check and pull if needed
                new_parity = get_parity_state(repo)
                if new_parity == "behind_only":
                    if not pull_ff_only(repo):
                        return [
                            _make_finding(
                                "sync_guard_warning",
                                "Cleaned derived caches but pull failed — worktree still behind.",
                                severity="warning",
                                parity_state="dirty_derived_only",
                            )
                        ]

                return [
                    _make_finding(
                        "sync_guard_healed",
                        "Cleaned derived caches and synced worktree.",
                        parity_state="dirty_derived_only",
                    )
                ]
            except Exception as exc:
                logger.debug("DAEMON[sync_guard]: derived cleanup failed: %s", exc)
                return [
                    _make_finding(
                        "sync_guard_warning",
                        f"Derived cache cleanup failed: {exc}",
                        severity="warning",
                        parity_state="dirty_derived_only",
                    )
                ]

        # Warning-only states — don't attempt risky repair
        if parity in ("diverged", "dirty_mixed"):
            return [
                _make_finding(
                    "sync_guard_warning",
                    f"Worktree in {parity} state — manual repair may be needed.",
                    severity="warning",
                    parity_state=parity,
                )
            ]

        if parity == "stuck_rebase_or_merge":
            return [
                _make_finding(
                    "sync_guard_warning",
                    "Worktree has a stuck rebase or merge — manual resolution needed.",
                    severity="warning",
                    parity_state=parity,
                )
            ]

        if parity == "auth_or_network_error":
            return [
                _make_finding(
                    "sync_guard_warning",
                    "Cannot reach remote — check network or credentials.",
                    severity="warning",
                    parity_state=parity,
                )
            ]

        return []
