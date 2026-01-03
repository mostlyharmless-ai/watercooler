"""Local-to-remote sync operations for a single git repository.

This module provides:
- SyncResult, PullResult, PushResult, CommitResult: Operation results
- LocalRemoteSyncManager: Clean interface for single-repo sync operations

This is Layer 4 in the sync architecture, building on primitives and state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from git import Repo
from git.exc import InvalidGitRepositoryError, GitCommandError

from ..observability import log_debug
from .primitives import (
    get_branch_name,
    get_ahead_behind,
    is_detached_head,
    is_dirty,
    is_rebase_in_progress,
    has_conflicts,
    fetch_with_timeout,
    pull_ff_only,
    pull_rebase,
    push_with_retry,
    stash_changes,
    restore_stash,
)
from .errors import (
    SyncError,
    PullError,
    PushError,
    ConflictError,
)


# =============================================================================
# Result Data Classes
# =============================================================================


@dataclass
class PullResult:
    """Result of a pull operation.

    Attributes:
        success: Whether the pull succeeded
        strategy: Strategy used (ff-only, rebase)
        commits_pulled: Number of commits pulled
        stash_used: Whether stash was used for dirty worktree
        error: Error message if failed
    """

    success: bool
    strategy: str = "ff-only"
    commits_pulled: int = 0
    stash_used: bool = False
    error: Optional[str] = None


@dataclass
class CommitResult:
    """Result of a commit operation.

    Attributes:
        success: Whether the commit succeeded
        commit_sha: SHA of the new commit (if successful)
        files_committed: List of files included in commit
        error: Error message if failed
    """

    success: bool
    commit_sha: Optional[str] = None
    files_committed: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class PushResult:
    """Result of a push operation.

    Attributes:
        success: Whether the push succeeded
        commits_pushed: Number of commits pushed
        retries: Number of retry attempts
        error: Error message if failed
    """

    success: bool
    commits_pushed: int = 0
    retries: int = 0
    error: Optional[str] = None


@dataclass
class SyncResult:
    """Combined result of a commit-and-push operation.

    Attributes:
        success: Whether the full operation succeeded
        commit_result: Result of the commit phase
        push_result: Result of the push phase (if commit succeeded)
        timestamp: ISO timestamp of the operation
    """

    success: bool
    commit_result: Optional[CommitResult] = None
    push_result: Optional[PushResult] = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class SyncStatus:
    """Current sync status of a repository.

    Attributes:
        branch: Current branch name (None if detached)
        ahead: Commits ahead of origin
        behind: Commits behind origin
        is_clean: No uncommitted changes
        has_conflicts: Has merge conflicts
        is_detached: In detached HEAD state
        is_rebasing: Rebase in progress
        can_push: Safe to push
        can_pull: Safe to pull
    """

    branch: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    is_clean: bool = True
    has_conflicts: bool = False
    is_detached: bool = False
    is_rebasing: bool = False
    can_push: bool = False
    can_pull: bool = False


# =============================================================================
# Local-Remote Sync Manager
# =============================================================================


class LocalRemoteSyncManager:
    """Manager for single-repo local-to-remote sync operations.

    This class provides a clean interface for:
    - Pulling changes (with ff-only or rebase strategies)
    - Committing changes (with automatic staging)
    - Pushing changes (with retry and backoff)
    - Combined commit-and-push operations

    Usage:
        manager = LocalRemoteSyncManager(repo_path="/path/to/repo")

        # Get current status
        status = manager.get_sync_status()

        # Pull latest changes
        pull_result = manager.pull(strategy="rebase")

        # Commit and push
        sync_result = manager.commit_and_push(
            message="Add new feature",
            files=["src/feature.py"],
        )
    """

    def __init__(
        self,
        repo_path: Path,
        *,
        remote: str = "origin",
        auto_stash: bool = True,
        max_push_retries: int = 5,
    ):
        """Initialize the sync manager.

        Args:
            repo_path: Path to the git repository
            remote: Remote name (default: origin)
            auto_stash: Auto-stash dirty worktree on pull
            max_push_retries: Maximum push retry attempts
        """
        self.repo_path = Path(repo_path)
        self.remote = remote
        self.auto_stash = auto_stash
        self.max_push_retries = max_push_retries
        self._repo: Optional[Repo] = None

    @property
    def repo(self) -> Repo:
        """Get the git repository object."""
        if self._repo is None:
            try:
                self._repo = Repo(self.repo_path, search_parent_directories=True)
            except InvalidGitRepositoryError as e:
                raise SyncError(
                    f"Invalid git repository: {self.repo_path}",
                    context={"path": str(self.repo_path)},
                    recovery_hint="Ensure the path is inside a git repository",
                ) from e
        return self._repo

    def get_sync_status(self, fetch_first: bool = True) -> SyncStatus:
        """Get current sync status of the repository.

        Args:
            fetch_first: Whether to fetch from remote before checking

        Returns:
            SyncStatus with current repository state
        """
        try:
            if fetch_first:
                try:
                    fetch_with_timeout(self.repo, timeout=30)
                except Exception as e:
                    log_debug(f"[L2R] Fetch failed (non-fatal): {e}")

            branch = get_branch_name(self.repo)
            is_detached = is_detached_head(self.repo)
            dirty = is_dirty(self.repo)
            rebasing = is_rebase_in_progress(self.repo)
            conflicts = has_conflicts(self.repo)

            ahead, behind = 0, 0
            if branch:
                ahead, behind = get_ahead_behind(self.repo, branch)

            return SyncStatus(
                branch=branch,
                ahead=ahead,
                behind=behind,
                is_clean=not dirty,
                has_conflicts=conflicts,
                is_detached=is_detached,
                is_rebasing=rebasing,
                can_push=not is_detached and not conflicts and not rebasing and ahead > 0,
                can_pull=not is_detached and not conflicts and not rebasing and behind > 0,
            )
        except Exception as e:
            log_debug(f"[L2R] Error getting sync status: {e}")
            return SyncStatus()

    def pull(
        self,
        strategy: str = "ff-only",
        *,
        allow_stash: Optional[bool] = None,
    ) -> PullResult:
        """Pull changes from remote.

        Args:
            strategy: Pull strategy - "ff-only" or "rebase"
            allow_stash: Override auto_stash setting

        Returns:
            PullResult with operation details
        """
        use_stash = allow_stash if allow_stash is not None else self.auto_stash

        try:
            branch = get_branch_name(self.repo)
            if not branch:
                return PullResult(
                    success=False,
                    error="Cannot pull in detached HEAD state",
                )

            # Check current state
            _, behind = get_ahead_behind(self.repo, branch)
            if behind == 0:
                return PullResult(success=True, strategy=strategy, commits_pulled=0)

            # Handle dirty worktree
            stash_ref = None
            if is_dirty(self.repo):
                if use_stash:
                    stash_ref = stash_changes(self.repo)
                    if not stash_ref:
                        return PullResult(
                            success=False,
                            error="Failed to stash changes",
                        )
                else:
                    return PullResult(
                        success=False,
                        error="Cannot pull with uncommitted changes (stash disabled)",
                    )

            try:
                # Execute pull
                if strategy == "rebase":
                    success = pull_rebase(self.repo, branch)
                else:
                    success = pull_ff_only(self.repo, branch)

                if success:
                    return PullResult(
                        success=True,
                        strategy=strategy,
                        commits_pulled=behind,
                        stash_used=stash_ref is not None,
                    )
                else:
                    return PullResult(
                        success=False,
                        strategy=strategy,
                        error=f"Pull {strategy} failed",
                        stash_used=stash_ref is not None,
                    )
            finally:
                # Restore stash if used
                if stash_ref:
                    restore_stash(self.repo, stash_ref)

        except GitCommandError as e:
            return PullResult(success=False, error=str(e))
        except Exception as e:
            log_debug(f"[L2R] Pull error: {e}")
            return PullResult(success=False, error=str(e))

    def commit(
        self,
        message: str,
        *,
        files: Optional[List[str]] = None,
        all_changes: bool = False,
    ) -> CommitResult:
        """Create a commit.

        Args:
            message: Commit message
            files: Specific files to commit (None = staged only)
            all_changes: Stage all changes (like git commit -a)

        Returns:
            CommitResult with operation details
        """
        try:
            # Stage files if specified
            if files:
                self.repo.index.add(files)
            elif all_changes:
                self.repo.git.add("-A")

            # Check if there's anything to commit
            if not self.repo.index.diff("HEAD") and not self.repo.untracked_files:
                return CommitResult(
                    success=False,
                    error="Nothing to commit",
                )

            # Get list of staged files
            staged = [item.a_path for item in self.repo.index.diff("HEAD")]

            # Create commit
            commit = self.repo.index.commit(message)

            return CommitResult(
                success=True,
                commit_sha=commit.hexsha[:12],
                files_committed=staged,
            )

        except GitCommandError as e:
            return CommitResult(success=False, error=str(e))
        except Exception as e:
            log_debug(f"[L2R] Commit error: {e}")
            return CommitResult(success=False, error=str(e))

    def push(
        self,
        *,
        max_retries: Optional[int] = None,
        force: bool = False,
    ) -> PushResult:
        """Push commits to remote.

        Args:
            max_retries: Override max_push_retries setting
            force: Force push (use with caution)

        Returns:
            PushResult with operation details
        """
        retries = max_retries if max_retries is not None else self.max_push_retries

        try:
            branch = get_branch_name(self.repo)
            if not branch:
                return PushResult(
                    success=False,
                    error="Cannot push from detached HEAD state",
                )

            ahead, _ = get_ahead_behind(self.repo, branch)
            if ahead == 0:
                return PushResult(success=True, commits_pushed=0)

            if force:
                # Force push (dangerous!)
                self.repo.git.push(self.remote, branch, force=True)
                return PushResult(success=True, commits_pushed=ahead)
            else:
                success = push_with_retry(
                    self.repo, branch, max_retries=retries
                )
                if success:
                    return PushResult(success=True, commits_pushed=ahead)
                else:
                    return PushResult(
                        success=False,
                        retries=retries,
                        error="Push failed after retries",
                    )

        except GitCommandError as e:
            return PushResult(success=False, error=str(e))
        except Exception as e:
            log_debug(f"[L2R] Push error: {e}")
            return PushResult(success=False, error=str(e))

    def commit_and_push(
        self,
        message: str,
        *,
        files: Optional[List[str]] = None,
        all_changes: bool = False,
        max_retries: Optional[int] = None,
    ) -> SyncResult:
        """Commit and push in one operation.

        Args:
            message: Commit message
            files: Specific files to commit
            all_changes: Stage all changes
            max_retries: Override max_push_retries setting

        Returns:
            SyncResult with both commit and push results
        """
        # First commit
        commit_result = self.commit(
            message,
            files=files,
            all_changes=all_changes,
        )

        if not commit_result.success:
            return SyncResult(
                success=False,
                commit_result=commit_result,
            )

        # Then push
        push_result = self.push(max_retries=max_retries)

        return SyncResult(
            success=push_result.success,
            commit_result=commit_result,
            push_result=push_result,
        )

    def ensure_synced(
        self,
        *,
        pull_strategy: str = "rebase",
        push_if_ahead: bool = True,
    ) -> Tuple[bool, List[str]]:
        """Ensure repository is synced with remote.

        Args:
            pull_strategy: Strategy for pulling (ff-only or rebase)
            push_if_ahead: Push if we have local commits

        Returns:
            Tuple of (success, list of actions taken)
        """
        actions = []

        try:
            status = self.get_sync_status(fetch_first=True)

            if status.is_detached:
                return False, ["Error: Repository is in detached HEAD state"]

            if status.has_conflicts:
                return False, ["Error: Repository has unresolved conflicts"]

            if status.is_rebasing:
                return False, ["Error: Rebase in progress"]

            # Pull if behind
            if status.behind > 0:
                result = self.pull(strategy=pull_strategy)
                if result.success:
                    actions.append(f"Pulled {result.commits_pulled} commits")
                else:
                    return False, [f"Pull failed: {result.error}"]

            # Push if ahead
            if push_if_ahead and status.ahead > 0:
                result = self.push()
                if result.success:
                    actions.append(f"Pushed {result.commits_pushed} commits")
                else:
                    return False, [f"Push failed: {result.error}"]

            if not actions:
                actions.append("Already synced")

            return True, actions

        except Exception as e:
            return False, [f"Sync error: {e}"]
