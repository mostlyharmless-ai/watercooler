"""Railway worktree — shallow clone of the orphan branch for daemon reads.

Replaces the GitHub Contents API path (hosted_data → hosted_ops → REST)
with a local filesystem clone that daemons read identically to local mode.

Design:
- One shallow clone per scope in ``/tmp/wc-worktree/<scope_id>/``
- Refresh via ``git fetch && git reset --hard`` (one git-protocol call,
  does NOT count against GitHub REST API rate limit)
- TTL-gated: configurable refresh interval (default 120s)
- Thread-safe: lock around refresh
- Auth: ``GITHUB_TOKEN`` via ``GIT_ASKPASS`` helper
- Fallback: if clone/fetch fails, callers should fall back to hosted_data
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Orphan branch that stores thread data.
_ORPHAN_BRANCH = "watercooler/threads"

# Default seconds between git-fetch refreshes.
DEFAULT_REFRESH_INTERVAL = 120.0


class HostedWorktree:
    """Manages a shallow clone of the orphan branch on Railway.

    Args:
        repo: GitHub repo in ``org/name`` format.
        github_token: Personal access token or app token for git auth.
        scope_id: Unique scope key (``user:repo``) for directory isolation.
        refresh_interval: Seconds between git fetch refreshes.
    """

    def __init__(
        self,
        *,
        repo: str,
        github_token: str,
        scope_id: str,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        self._repo = repo
        self._github_token = github_token
        self._scope_id = scope_id
        self._refresh_interval = refresh_interval

        self._lock = threading.Lock()
        self._last_refresh: float = 0.0
        self._clone_dir: Optional[Path] = None
        self._ready = False
        self._failed = False
        self._askpass_path: Optional[Path] = None

    @property
    def path(self) -> Optional[Path]:
        """Path to the worktree root, or None if not yet initialized."""
        return self._clone_dir if self._ready else None

    @property
    def failed(self) -> bool:
        """True if the initial clone failed (fallback to hosted_data recommended)."""
        return self._failed

    def update_token(self, github_token: str) -> None:
        """Swap the auth token used for subsequent git operations.

        Fleet-scheduler bridge identity (Design (hosted) v4 D3): the
        most-recently-validated tenant token wins. ``_git_env`` reads the
        attribute on every git call, so a swap takes effect on the next
        fetch/push with no re-clone.
        """
        with self._lock:
            self._github_token = github_token

    def _git_env(self) -> dict[str, str]:
        """Env dict with GIT_ASKPASS helper for token auth.

        Creates a per-instance temp script that prints the token via an
        env var (never embedded in the script text).  The file is opened
        with 0o700 permissions from the start to avoid a world-readable
        window.
        """
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Pass token via env so the script never contains it literally.
        # Avoids issues with special chars (single quotes, etc.) in tokens.
        # Note: this env var is visible to git hooks during clone/fetch.
        # The orphan branch is system-controlled (no user hooks), so risk
        # is low. The env is a subprocess copy — not set in the parent.
        env["_WC_GIT_TOKEN"] = self._github_token

        # Create (or reuse) the per-instance ASKPASS helper script
        if self._askpass_path is None or not self._askpass_path.exists():
            # Use per-instance temp file to prevent cross-scope contamination
            # when multiple HostedWorktree instances refresh concurrently.
            fd = tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", prefix="wc-askpass-",
                delete=False,
            )
            fd.write('#!/bin/sh\nprintf "%s\\n" "$_WC_GIT_TOKEN"\n')
            fd.close()
            askpass = Path(fd.name)
            askpass.chmod(0o700)
            self._askpass_path = askpass

        env["GIT_ASKPASS"] = str(self._askpass_path)
        return env

    def _public_url(self) -> str:
        """Remote URL without embedded credentials (stored in .git/config)."""
        return f"https://github.com/{self._repo}.git"

    # _clone_url() removed: clone now uses _public_url() + GIT_ASKPASS
    # so the token never appears in CLI args or .git/config.

    def initialize(self) -> bool:
        """Perform initial shallow clone of the orphan branch.

        Returns True on success, False on failure.
        """
        with self._lock:
            if self._ready:
                return True
            if self._failed:
                return False

            base = Path(tempfile.gettempdir()) / "wc-worktree"
            base.mkdir(parents=True, exist_ok=True)
            # Sanitize scope_id for filesystem (replace : and / with _)
            safe_scope = self._scope_id.replace(":", "_").replace("/", "_")
            clone_dir = base / safe_scope

            if clone_dir.exists() and (clone_dir / ".git").exists():
                # Reuse existing clone from a previous process lifecycle
                self._clone_dir = clone_dir
                self._ready = True
                self._last_refresh = 0.0  # Force refresh on next access
                logger.info("WORKTREE: reusing existing clone at %s", clone_dir)
                return True

            # Remove stale partial clone
            if clone_dir.exists():
                import shutil
                shutil.rmtree(clone_dir, ignore_errors=True)

            try:
                # Use public URL + GIT_ASKPASS for auth so the token
                # never appears in command-line args (/proc/pid/cmdline).
                cmd = [
                    "git", "clone",
                    "--depth", "1",
                    "--single-branch",
                    "--branch", _ORPHAN_BRANCH,
                    self._public_url(),
                    str(clone_dir),
                ]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=self._git_env(),
                )
                if result.returncode != 0:
                    logger.error(
                        "WORKTREE: clone failed (rc=%d): %s",
                        result.returncode, result.stderr[:500],
                    )
                    self._failed = True
                    return False

                self._clone_dir = clone_dir
                self._ready = True
                self._last_refresh = time.monotonic()
                logger.info(
                    "WORKTREE: cloned %s branch=%s to %s",
                    self._repo, _ORPHAN_BRANCH, clone_dir,
                )
                return True

            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.error("WORKTREE: clone error: %s", exc)
                self._failed = True
                return False

    def refresh_if_stale(self) -> bool:
        """Fetch latest changes if the refresh interval has elapsed.

        Returns True if the worktree is up-to-date, False on error.
        Thread-safe: concurrent calls are serialized via lock; if a
        refresh is already in progress, the second caller waits.
        """
        if not self._ready:
            return False

        now = time.monotonic()
        if (now - self._last_refresh) < self._refresh_interval:
            return True  # Still fresh

        with self._lock:
            # Double-check after acquiring lock (another thread may have refreshed)
            if (time.monotonic() - self._last_refresh) < self._refresh_interval:
                return True

            try:
                # Fetch and hard-reset to remote HEAD.  One git-protocol
                # round trip — does NOT count against REST API rate limit.
                fetch_cmd = [
                    "git", "-C", str(self._clone_dir),
                    "fetch", "origin", _ORPHAN_BRANCH,
                    "--depth", "1",
                ]
                result = subprocess.run(
                    fetch_cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=self._git_env(),
                )
                if result.returncode != 0:
                    logger.warning("WORKTREE: fetch failed: %s", result.stderr[:300])
                    return False

                reset_cmd = [
                    "git", "-C", str(self._clone_dir),
                    "reset", "--hard", f"origin/{_ORPHAN_BRANCH}",
                ]
                result = subprocess.run(
                    reset_cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    logger.warning("WORKTREE: reset failed: %s", result.stderr[:300])
                    return False

                self._last_refresh = time.monotonic()

                # Record telemetry
                try:
                    from .telemetry import SVC_GIT_FETCH, record_call
                    record_call(SVC_GIT_FETCH)
                except ImportError:
                    pass

                return True

            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("WORKTREE: refresh error: %s", exc)
                return False

    def cleanup(self) -> None:
        """Remove the clone directory and ASKPASS helper.

        Thread-safe: acquires the lock to prevent races with
        ``refresh_if_stale()`` which checks ``_ready`` and ``_clone_dir``.
        """
        with self._lock:
            if self._clone_dir and self._clone_dir.exists():
                import shutil
                shutil.rmtree(self._clone_dir, ignore_errors=True)
                logger.debug("WORKTREE: removed %s", self._clone_dir)
            if self._askpass_path and self._askpass_path.exists():
                try:
                    self._askpass_path.unlink()
                except OSError:
                    pass
            self._ready = False
            self._clone_dir = None
            self._askpass_path = None
