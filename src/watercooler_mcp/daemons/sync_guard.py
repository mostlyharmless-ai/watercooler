"""Sync Guard Daemon — proactive worktree parity checker.

Periodically checks sync parity between local threads worktrees and the
remote, auto-healing bounded cases (behind-only, dirty-derived). Emits
warning findings for states that require manual intervention.

This complements the reactive ``ensure_readable()`` path: if no reads
occur for a while, divergence compounds silently. The sync guard detects
and resolves drift before it becomes a problem.

A single MCP process serves multiple repos' threads as separate git
worktrees under ``~/.watercooler/worktrees/<repo>/``. Each tick sweeps
*every* served worktree (not just the server's primary one), so a worktree
that is read-only — or never read at all — still stays in parity.
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
    worktree: str = "",
) -> Finding:
    """Create a sync_guard finding."""
    import time

    from ulid import ULID

    details: dict = {}
    if parity_state:
        details["parity_state"] = parity_state
    if worktree:
        details["worktree"] = worktree

    return Finding(
        finding_id=str(ULID()),
        daemon_name="sync_guard",
        category=category,
        topic="",
        message=message,
        severity=severity,
        details=details,
        created_at=time.time(),
    )


class SyncGuardDaemon(BaseDaemon):
    """Proactive worktree parity checker.

    Runs every ``interval`` seconds and, for each served threads worktree,
    checks parity and auto-heals safe cases (behind-only, dirty-derived-only).
    Emits warning findings for states that need manual repair.

    Args:
        interval: Seconds between parity checks.
        threads_dir: Override threads directory. When set, the daemon tends
            only that single worktree (back-compat / tests) and skips the
            multi-worktree sweep.
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
        """Resolve the server's primary threads directory.

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

    def _discover_worktrees(self) -> List[Path]:
        """Return every served threads worktree to keep in parity.

        With an explicit ``threads_dir`` override, returns only that one (the
        daemon was scoped deliberately). Otherwise unions the CWD-resolved
        primary worktree with every directory under ``WORKTREE_BASE``
        (``~/.watercooler/worktrees/*``), deduplicated by resolved path.
        Per-worktree git/branch validity is checked in ``_heal_worktree``.
        """
        if self._threads_dir_override is not None:
            return [self._threads_dir_override]

        candidates: List[Path] = []
        primary = self._resolve_threads_dir()
        if primary is not None:
            candidates.append(primary)

        try:
            from watercooler_mcp.config import WORKTREE_BASE

            if WORKTREE_BASE.exists():
                for child in sorted(WORKTREE_BASE.iterdir()):
                    if child.is_dir():
                        candidates.append(child)
        except Exception as exc:
            logger.debug("DAEMON[sync_guard]: worktree enumeration failed: %s", exc)

        seen: set = set()
        result: List[Path] = []
        for path in candidates:
            try:
                key = path.resolve()
            except Exception:
                key = path
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result

    def tick(self) -> List[Finding]:
        """Run one parity-check cycle across all served worktrees."""
        from .hosted_data import is_daemon_hosted_mode

        if is_daemon_hosted_mode() and self._threads_dir_override is None:
            return []

        findings: List[Finding] = []
        for threads_dir in self._discover_worktrees():
            try:
                findings.extend(self._heal_worktree(threads_dir))
            except Exception as exc:
                logger.debug(
                    "DAEMON[sync_guard]: heal failed for %s: %s", threads_dir, exc
                )
        return findings

    def _heal_worktree(self, threads_dir: Optional[Path]) -> List[Finding]:
        """Check + auto-heal a single worktree's parity. Never raises."""
        if threads_dir is None or not threads_dir.exists():
            return []

        # Check for .git dir / worktree marker
        git_dir = threads_dir / ".git"
        if not git_dir.exists() and not (threads_dir / "HEAD").exists():
            return []

        label = threads_dir.name

        try:
            from git import Repo

            from watercooler_mcp.sync.primitives import (
                fetch_with_timeout,
                get_parity_state,
                pull_ff_only,
            )

            repo = Repo(threads_dir)
        except Exception as exc:
            logger.debug("DAEMON[sync_guard]: could not open repo %s: %s", threads_dir, exc)
            return []

        # Fetch first for accurate parity
        try:
            fetch_with_timeout(repo, timeout=15)
        except Exception as exc:
            logger.debug("DAEMON[sync_guard]: fetch failed for %s: %s", threads_dir, exc)
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
                            f"[{label}] Worktree was behind remote; fast-forward pull succeeded.",
                            parity_state="behind_only",
                            worktree=label,
                        )
                    ]
            except Exception as exc:
                logger.debug("DAEMON[sync_guard]: pull failed for %s: %s", threads_dir, exc)
                return [
                    _make_finding(
                        "sync_guard_warning",
                        f"[{label}] Worktree behind remote but pull failed: {exc}",
                        severity="warning",
                        parity_state="behind_only",
                        worktree=label,
                    )
                ]

        # Auto-heal: dirty derived files — clean + pull if needed
        if parity == "dirty_derived_only":
            try:
                from watercooler_mcp.sync.primitives import should_discard_dirty_entry

                status_out = repo.git.status("--porcelain")
                for line in status_out.strip().split("\n"):
                    if not line.strip():
                        continue
                    filename = line[3:].split(" -> ")[-1].strip()
                    # Skip non-derived files and untracked write-once projections
                    # whose sole copy isn't on origin yet (preserve for the
                    # committer); discard the rest, incl. an untracked projection
                    # origin already tracks (#924 review).
                    if not should_discard_dirty_entry(repo, line[:2], filename):
                        continue
                    filepath = threads_dir / filename
                    if filepath.exists():
                        filepath.unlink()
                    try:
                        repo.git.checkout("--", filename)
                    except Exception:
                        pass  # Untracked derived files won't have index entry

                # Re-check and pull if needed. A remaining dirty_derived_only here
                # means only a preserved untracked projection is left, which never
                # blocks a fast-forward — so still pull to clear any behind state.
                new_parity = get_parity_state(repo)
                if new_parity in ("behind_only", "dirty_derived_only"):
                    if not pull_ff_only(repo):
                        return [
                            _make_finding(
                                "sync_guard_warning",
                                f"[{label}] Cleaned derived caches but pull failed — worktree still behind.",
                                severity="warning",
                                parity_state="dirty_derived_only",
                                worktree=label,
                            )
                        ]

                return [
                    _make_finding(
                        "sync_guard_healed",
                        f"[{label}] Cleaned derived caches and synced worktree.",
                        parity_state="dirty_derived_only",
                        worktree=label,
                    )
                ]
            except Exception as exc:
                logger.debug("DAEMON[sync_guard]: derived cleanup failed for %s: %s", threads_dir, exc)
                return [
                    _make_finding(
                        "sync_guard_warning",
                        f"[{label}] Derived cache cleanup failed: {exc}",
                        severity="warning",
                        parity_state="dirty_derived_only",
                        worktree=label,
                    )
                ]

        # Warning-only states — don't attempt risky repair
        if parity in ("diverged", "dirty_mixed"):
            return [
                _make_finding(
                    "sync_guard_warning",
                    f"[{label}] Worktree in {parity} state — manual repair may be needed.",
                    severity="warning",
                    parity_state=parity,
                    worktree=label,
                )
            ]

        if parity == "stuck_rebase_or_merge":
            return [
                _make_finding(
                    "sync_guard_warning",
                    f"[{label}] Worktree has a stuck rebase or merge — manual resolution needed.",
                    severity="warning",
                    parity_state=parity,
                    worktree=label,
                )
            ]

        if parity == "auth_or_network_error":
            return [
                _make_finding(
                    "sync_guard_warning",
                    f"[{label}] Cannot reach remote — check network or credentials.",
                    severity="warning",
                    parity_state=parity,
                    worktree=label,
                )
            ]

        return []
