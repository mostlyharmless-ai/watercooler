"""Branch parity management for cross-repo coordination.

This module provides:
- StateClass: Detailed 3D state classification for deterministic remediation
- PreflightResult: Result of preflight parity checks
- BranchPairingResult: Result of branch pairing validation
- BranchParityManager: Cross-repo coordination (threads ↔ code)

This is Layer 5 in the sync architecture, building on primitives, state, and conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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
    branch_exists_on_origin,
    fetch_with_timeout,
    pull_ff_only,
    pull_rebase,
    push_with_retry,
    checkout_branch,
    stash_changes,
    restore_stash,
    validate_branch_name,
)
from .state import (
    ParityStatus,
    ParityState,
    StateManager,
    write_parity_state,
)
from .conflict import (
    ConflictResolver,
    has_graph_conflicts_only,
    has_thread_conflicts_only,
)
from .errors import BranchPairingError


# =============================================================================
# Enums
# =============================================================================


class StateClass(str, Enum):
    """Detailed state classification for deterministic remediation.

    Maps the 3 orthogonal dimensions (Branch Alignment, Origin Sync, Working Tree)
    to actionable state classes.

    Dimensions:
    1. Branch Alignment: MATCHED | MISMATCHED
    2. Origin Sync: SYNCED | AHEAD | BEHIND | DIVERGED
    3. Working Tree: CLEAN | DIRTY
    """

    # Ready states - can proceed
    READY = "ready"  # MATCHED, SYNCED, CLEAN
    READY_DIRTY = "ready_dirty"  # MATCHED, SYNCED, DIRTY (write commits)

    # Behind states - auto-fixable
    BEHIND_CLEAN = "behind_clean"  # MATCHED, BEHIND, CLEAN -> pull --ff-only or --rebase
    BEHIND_DIRTY = "behind_dirty"  # MATCHED, BEHIND, DIRTY -> stash -> pull -> pop

    # Ahead states - auto-fixable
    AHEAD = "ahead"  # MATCHED, AHEAD, CLEAN -> push after write
    AHEAD_DIRTY = "ahead_dirty"  # MATCHED, AHEAD, DIRTY -> proceed, push after commit

    # Diverged states - auto-fixable (rebase)
    DIVERGED_CLEAN = "diverged_clean"  # MATCHED, DIVERGED, CLEAN -> pull --rebase -> push
    DIVERGED_DIRTY = "diverged_dirty"  # MATCHED, DIVERGED, DIRTY -> stash -> rebase -> pop -> push

    # Branch mismatch - auto-fixable (checkout)
    BRANCH_MISMATCH = "branch_mismatch"  # MISMATCHED, *, CLEAN -> checkout <target>
    BRANCH_MISMATCH_DIRTY = "branch_mismatch_dirty"  # MISMATCHED, *, DIRTY -> stash -> checkout -> pop

    # Blocking states - require human intervention
    DETACHED_HEAD = "detached_head"  # BLOCK
    REBASE_IN_PROGRESS = "rebase_in_progress"  # BLOCK
    CONFLICT = "conflict"  # BLOCK (merge/rebase conflict)
    CODE_BEHIND = "code_behind"  # BLOCK (user must pull code)
    ORPHANED_BRANCH = "orphaned_branch"  # BLOCK

    # Auto-fixable edge cases
    NO_UPSTREAM = "no_upstream"  # push -u origin <branch>
    MAIN_PROTECTION = "main_protection"  # Auto-checkout threads to feature

    @classmethod
    def is_blocking(cls, state: "StateClass") -> bool:
        """Check if state requires human intervention."""
        blocking = {
            cls.DETACHED_HEAD,
            cls.REBASE_IN_PROGRESS,
            cls.CONFLICT,
            cls.CODE_BEHIND,
            cls.ORPHANED_BRANCH,
        }
        return state in blocking

    @classmethod
    def is_auto_fixable(cls, state: "StateClass") -> bool:
        """Check if state can be auto-remediated."""
        auto_fixable = {
            cls.BEHIND_CLEAN,
            cls.BEHIND_DIRTY,
            cls.AHEAD,
            cls.AHEAD_DIRTY,
            cls.DIVERGED_CLEAN,
            cls.DIVERGED_DIRTY,
            cls.BRANCH_MISMATCH,
            cls.BRANCH_MISMATCH_DIRTY,
            cls.NO_UPSTREAM,
            cls.MAIN_PROTECTION,
        }
        return state in auto_fixable


# =============================================================================
# Result Data Classes
# =============================================================================


@dataclass
class PreflightResult:
    """Result of preflight parity check.

    Attributes:
        success: Whether preflight completed without errors
        state: Current parity state
        can_proceed: Whether the operation can proceed
        blocking_reason: Human-readable reason if blocked
        auto_fixed: Whether auto-remediation was applied
        actions_taken: List of actions taken during preflight
    """

    success: bool
    state: ParityState
    can_proceed: bool
    blocking_reason: Optional[str] = None
    auto_fixed: bool = False
    actions_taken: List[str] = field(default_factory=list)


@dataclass
class BranchPairingResult:
    """Result of branch pairing validation.

    Attributes:
        valid: Whether branch pairing is valid
        code_branch: Current code branch name
        threads_branch: Current threads branch name
        state_class: Detailed state classification
        mismatches: List of detected mismatches
        warnings: List of non-blocking warnings
    """

    valid: bool
    code_branch: Optional[str] = None
    threads_branch: Optional[str] = None
    state_class: Optional[StateClass] = None
    mismatches: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# Branch Parity Manager
# =============================================================================


class BranchParityManager:
    """Manager for cross-repo branch parity coordination.

    This class provides the high-level interface for keeping code and threads
    repositories in sync. It uses the primitives, state, and conflict modules
    for the low-level operations.

    Usage:
        manager = BranchParityManager(
            code_repo_path=Path("/path/to/code"),
            threads_repo_path=Path("/path/to/threads"),
        )

        # Full preflight with auto-fix
        result = manager.run_preflight(auto_fix=True)

        # Lightweight sync for reads
        ok, actions = manager.ensure_readable()

        # Full sync for writes
        ok, actions = manager.ensure_writable()

        # Push after commit
        ok, error = manager.push_after_commit()

        # Get live health status
        health = manager.get_health()
    """

    def __init__(
        self,
        code_repo_path: Path,
        threads_repo_path: Path,
        *,
        main_branch: Optional[str] = None,
    ):
        """Initialize the branch parity manager.

        Args:
            code_repo_path: Path to the code repository
            threads_repo_path: Path to the threads repository
            main_branch: Override main branch detection (default: auto-detect)
        """
        self.code_repo_path = Path(code_repo_path)
        self.threads_repo_path = Path(threads_repo_path)
        self._main_branch = main_branch
        self._code_repo: Optional[Repo] = None
        self._threads_repo: Optional[Repo] = None
        self._state_manager = StateManager(threads_repo_path, code_repo_path)

    @property
    def code_repo(self) -> Repo:
        """Get the code repository object."""
        if self._code_repo is None:
            try:
                self._code_repo = Repo(
                    self.code_repo_path, search_parent_directories=True
                )
            except InvalidGitRepositoryError as e:
                raise BranchPairingError(
                    f"Code path is not a git repository: {self.code_repo_path}",
                    context={"path": str(self.code_repo_path)},
                ) from e
        return self._code_repo

    @property
    def threads_repo(self) -> Repo:
        """Get the threads repository object."""
        if self._threads_repo is None:
            try:
                self._threads_repo = Repo(
                    self.threads_repo_path, search_parent_directories=True
                )
            except InvalidGitRepositoryError as e:
                raise BranchPairingError(
                    f"Threads path is not a git repository: {self.threads_repo_path}",
                    context={"path": str(self.threads_repo_path)},
                ) from e
        return self._threads_repo

    @property
    def main_branch(self) -> Optional[str]:
        """Get the main branch name (auto-detected if not specified)."""
        if self._main_branch:
            return self._main_branch

        # Auto-detect: check for main, then master
        for name in ["main", "master"]:
            if name in [ref.name for ref in self.code_repo.heads]:
                self._main_branch = name
                return name
        return None

    def validate(self, *, check_history: bool = False) -> BranchPairingResult:
        """Validate branch pairing without auto-fixing.

        Args:
            check_history: If True, also validate commit history alignment

        Returns:
            BranchPairingResult with validation status
        """
        mismatches = []
        warnings = []

        try:
            # Check for detached HEAD
            code_branch = get_branch_name(self.code_repo)
            threads_branch = get_branch_name(self.threads_repo)

            if code_branch is None:
                return BranchPairingResult(
                    valid=False,
                    code_branch=None,
                    threads_branch=threads_branch,
                    state_class=StateClass.DETACHED_HEAD,
                    mismatches=["Code repository is in detached HEAD state"],
                )

            if threads_branch is None:
                return BranchPairingResult(
                    valid=False,
                    code_branch=code_branch,
                    threads_branch=None,
                    state_class=StateClass.DETACHED_HEAD,
                    mismatches=["Threads repository is in detached HEAD state"],
                )

            # Check for conflicts
            if has_conflicts(self.code_repo):
                return BranchPairingResult(
                    valid=False,
                    code_branch=code_branch,
                    threads_branch=threads_branch,
                    state_class=StateClass.CONFLICT,
                    mismatches=["Code repository has unresolved merge conflicts"],
                )

            if has_conflicts(self.threads_repo):
                return BranchPairingResult(
                    valid=False,
                    code_branch=code_branch,
                    threads_branch=threads_branch,
                    state_class=StateClass.CONFLICT,
                    mismatches=["Threads repository has unresolved merge conflicts"],
                )

            # Check for rebase in progress
            if is_rebase_in_progress(self.code_repo):
                return BranchPairingResult(
                    valid=False,
                    code_branch=code_branch,
                    threads_branch=threads_branch,
                    state_class=StateClass.REBASE_IN_PROGRESS,
                    mismatches=["Code repository has rebase in progress"],
                )

            if is_rebase_in_progress(self.threads_repo):
                return BranchPairingResult(
                    valid=False,
                    code_branch=code_branch,
                    threads_branch=threads_branch,
                    state_class=StateClass.REBASE_IN_PROGRESS,
                    mismatches=["Threads repository has rebase in progress"],
                )

            # Check branch name parity
            if code_branch != threads_branch:
                threads_is_dirty = is_dirty(self.threads_repo)
                state_class = (
                    StateClass.BRANCH_MISMATCH_DIRTY
                    if threads_is_dirty
                    else StateClass.BRANCH_MISMATCH
                )
                mismatches.append(
                    f"Branch mismatch: code is on '{code_branch}', "
                    f"threads is on '{threads_branch}'"
                )
                return BranchPairingResult(
                    valid=False,
                    code_branch=code_branch,
                    threads_branch=threads_branch,
                    state_class=state_class,
                    mismatches=mismatches,
                )

            # Check ahead/behind status
            code_ahead, code_behind = get_ahead_behind(self.code_repo, code_branch)
            threads_ahead, threads_behind = get_ahead_behind(
                self.threads_repo, threads_branch
            )

            if code_behind > 0:
                mismatches.append(
                    f"Code is {code_behind} commits behind origin"
                )
                return BranchPairingResult(
                    valid=False,
                    code_branch=code_branch,
                    threads_branch=threads_branch,
                    state_class=StateClass.CODE_BEHIND,
                    mismatches=mismatches,
                )

            # Determine state class
            threads_dirty = is_dirty(self.threads_repo)

            if threads_behind > 0 and threads_ahead > 0:
                state_class = (
                    StateClass.DIVERGED_DIRTY if threads_dirty else StateClass.DIVERGED_CLEAN
                )
                warnings.append(
                    f"Threads diverged: {threads_ahead} ahead, {threads_behind} behind"
                )
            elif threads_behind > 0:
                state_class = (
                    StateClass.BEHIND_DIRTY if threads_dirty else StateClass.BEHIND_CLEAN
                )
                warnings.append(f"Threads {threads_behind} commits behind origin")
            elif threads_ahead > 0:
                state_class = StateClass.AHEAD_DIRTY if threads_dirty else StateClass.AHEAD
                warnings.append(f"Threads {threads_ahead} commits ahead of origin")
            else:
                state_class = StateClass.READY_DIRTY if threads_dirty else StateClass.READY

            return BranchPairingResult(
                valid=True,
                code_branch=code_branch,
                threads_branch=threads_branch,
                state_class=state_class,
                warnings=warnings,
            )

        except Exception as e:
            return BranchPairingResult(
                valid=False,
                mismatches=[f"Validation error: {e}"],
            )

    def classify_state(self) -> StateClass:
        """Classify the current state of both repositories.

        Returns:
            StateClass indicating the current state
        """
        result = self.validate()
        return result.state_class or StateClass.READY

    def ensure_readable(self) -> Tuple[bool, List[str]]:
        """Lightweight sync for read operations.

        Never blocks - worst case is stale data. This function:
        - Never blocks: Always returns (True, actions) even on failure
        - No stashing: Only pulls if the tree is clean
        - No pushing: Read operations don't require push parity
        - Logs warnings: Issues are logged but don't prevent the read

        Returns:
            Tuple of (success, list of actions taken)
        """
        actions: List[str] = []

        try:
            # Early conflict detection - skip sync but allow stale reads
            if has_conflicts(self.threads_repo):
                log_debug(
                    "[PARITY] ensure_readable: Threads repo has conflicts, "
                    "skipping sync (may return stale data)"
                )
                return (True, ["Skipped sync due to conflicts - reading potentially stale data"])

            # Fetch from origin
            if not fetch_with_timeout(self.threads_repo):
                log_debug("[PARITY] ensure_readable: fetch failed (using cached data)")
                return (True, actions)

            # Get current branch
            branch = get_branch_name(self.threads_repo)
            if not branch:
                log_debug("[PARITY] ensure_readable: detached HEAD (proceeding anyway)")
                return (True, actions)

            # Get ahead/behind status
            ahead, behind = get_ahead_behind(self.threads_repo, branch)

            # Only auto-pull if:
            # 1. Behind origin (need to catch up)
            # 2. NOT ahead (no local commits to lose)
            # 3. Tree is clean (safe to pull without stash)
            if behind > 0 and ahead == 0 and not is_dirty(self.threads_repo):
                log_debug(f"[PARITY] ensure_readable: behind by {behind} commits, pulling")

                if pull_ff_only(self.threads_repo, branch):
                    actions.append(f"Pulled (ff-only, {behind} commits)")
                elif pull_rebase(self.threads_repo, branch):
                    actions.append(f"Pulled (rebase, {behind} commits)")
                else:
                    log_debug("[PARITY] ensure_readable: pull failed (using stale data)")

            elif behind > 0:
                if ahead > 0:
                    log_debug(
                        f"[PARITY] ensure_readable: diverged (ahead={ahead}, behind={behind})"
                    )
                elif is_dirty(self.threads_repo):
                    log_debug(
                        f"[PARITY] ensure_readable: behind by {behind} but dirty tree"
                    )

            return (True, actions)

        except Exception as e:
            log_debug(f"[PARITY] ensure_readable: error (proceeding anyway): {e}")
            return (True, actions)

    def ensure_writable(self) -> Tuple[bool, List[str]]:
        """Full preflight sync for write operations.

        This is equivalent to run_preflight(auto_fix=True) but returns
        a simpler tuple format.

        Returns:
            Tuple of (can_proceed, list of actions taken or blocking reason)
        """
        result = self.run_preflight(auto_fix=True)
        if result.can_proceed:
            return (True, result.actions_taken)
        else:
            return (False, [result.blocking_reason or "Unknown error"])

    def run_preflight(
        self,
        *,
        auto_fix: bool = True,
        fetch_first: bool = True,
    ) -> PreflightResult:
        """Run preflight parity checks with optional auto-remediation.

        Args:
            auto_fix: If True, attempt to auto-fix issues
            fetch_first: If True, fetch from origin before checks

        Returns:
            PreflightResult with success status and actions
        """
        state = ParityState(last_check_at=datetime.now(timezone.utc).isoformat())
        actions_taken: List[str] = []

        try:
            # Validate repos exist
            try:
                _ = self.code_repo
            except BranchPairingError as e:
                state.status = ParityStatus.ERROR.value
                state.last_error = str(e)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            try:
                _ = self.threads_repo
            except BranchPairingError as e:
                state.status = ParityStatus.ERROR.value
                state.last_error = str(e)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            # Check for conflicts FIRST
            if has_conflicts(self.code_repo):
                state.status = ParityStatus.DIVERGED.value
                state.last_error = "Code repository has unresolved merge conflicts"
                write_parity_state(self.threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            if has_conflicts(self.threads_repo):
                # Try auto-resolution for graph or thread conflicts
                resolver = ConflictResolver(self.threads_repo)
                if auto_fix and has_graph_conflicts_only(self.threads_repo):
                    if resolver.resolve_graph_conflicts():
                        actions_taken.append("Auto-resolved graph file conflicts")
                    else:
                        state.status = ParityStatus.DIVERGED.value
                        state.last_error = "Graph conflicts could not be auto-resolved"
                        write_parity_state(self.threads_repo_path, state)
                        return PreflightResult(
                            success=False,
                            state=state,
                            can_proceed=False,
                            blocking_reason=state.last_error,
                        )
                elif auto_fix and has_thread_conflicts_only(self.threads_repo):
                    if resolver.resolve_thread_conflicts():
                        actions_taken.append("Auto-resolved thread file conflicts")
                    else:
                        state.status = ParityStatus.DIVERGED.value
                        state.last_error = "Thread conflicts could not be auto-resolved"
                        write_parity_state(self.threads_repo_path, state)
                        return PreflightResult(
                            success=False,
                            state=state,
                            can_proceed=False,
                            blocking_reason=state.last_error,
                        )
                else:
                    state.status = ParityStatus.DIVERGED.value
                    state.last_error = "Threads repository has unresolved merge conflicts"
                    write_parity_state(self.threads_repo_path, state)
                    return PreflightResult(
                        success=False,
                        state=state,
                        can_proceed=False,
                        blocking_reason=state.last_error,
                    )

            # Check for rebase in progress
            if is_rebase_in_progress(self.code_repo):
                state.status = ParityStatus.REBASE_IN_PROGRESS.value
                state.last_error = "Code repository has rebase in progress"
                write_parity_state(self.threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            if is_rebase_in_progress(self.threads_repo):
                state.status = ParityStatus.REBASE_IN_PROGRESS.value
                state.last_error = "Threads repository has rebase in progress"
                write_parity_state(self.threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            # Fetch from origin
            if fetch_first:
                fetch_with_timeout(self.code_repo)
                fetch_with_timeout(self.threads_repo)

            # Get branch names
            code_branch = get_branch_name(self.code_repo)
            threads_branch = get_branch_name(self.threads_repo)
            state.code_branch = code_branch
            state.threads_branch = threads_branch

            # Check for detached HEAD
            if code_branch is None:
                state.status = ParityStatus.DETACHED_HEAD.value
                state.last_error = "Code repository is in detached HEAD state"
                write_parity_state(self.threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            if threads_branch is None:
                state.status = ParityStatus.DETACHED_HEAD.value
                state.last_error = "Threads repository is in detached HEAD state"
                write_parity_state(self.threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            # Handle branch mismatch
            if code_branch != threads_branch:
                if auto_fix:
                    # Stash if dirty
                    stash_ref = None
                    if is_dirty(self.threads_repo):
                        stash_ref = stash_changes(self.threads_repo)
                        if stash_ref:
                            actions_taken.append(f"Stashed changes: {stash_ref}")

                    # Checkout to match code branch
                    if checkout_branch(self.threads_repo, code_branch, create=True):
                        actions_taken.append(f"Checked out threads to {code_branch}")
                        threads_branch = code_branch
                        state.threads_branch = threads_branch
                    else:
                        state.status = ParityStatus.BRANCH_MISMATCH.value
                        state.last_error = f"Failed to checkout threads to {code_branch}"
                        write_parity_state(self.threads_repo_path, state)
                        return PreflightResult(
                            success=False,
                            state=state,
                            can_proceed=False,
                            blocking_reason=state.last_error,
                        )

                    # Restore stash
                    if stash_ref:
                        if restore_stash(self.threads_repo, stash_ref):
                            actions_taken.append("Restored stashed changes")
                        else:
                            actions_taken.append(f"Warning: stash {stash_ref} not restored")
                else:
                    state.status = ParityStatus.BRANCH_MISMATCH.value
                    state.last_error = (
                        f"Branch mismatch: code={code_branch}, threads={threads_branch}"
                    )
                    write_parity_state(self.threads_repo_path, state)
                    return PreflightResult(
                        success=False,
                        state=state,
                        can_proceed=False,
                        blocking_reason=state.last_error,
                    )

            # Get ahead/behind status
            code_ahead, code_behind = get_ahead_behind(self.code_repo, code_branch)
            threads_ahead, threads_behind = get_ahead_behind(
                self.threads_repo, threads_branch
            )
            state.code_ahead_origin = code_ahead
            state.code_behind_origin = code_behind
            state.threads_ahead_origin = threads_ahead
            state.threads_behind_origin = threads_behind

            # Code behind origin: BLOCK
            if code_behind > 0:
                state.status = ParityStatus.CODE_BEHIND_ORIGIN.value
                state.last_error = f"Code is {code_behind} commits behind origin"
                write_parity_state(self.threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            # Threads behind origin: AUTO-FIX
            if threads_behind > 0 and auto_fix:
                stash_ref = None
                if is_dirty(self.threads_repo):
                    stash_ref = stash_changes(self.threads_repo)
                    if stash_ref:
                        actions_taken.append(f"Stashed: {stash_ref}")

                # Pull (rebase if diverged)
                pulled = False
                if threads_ahead > 0:
                    if pull_rebase(self.threads_repo, threads_branch):
                        pulled = True
                        actions_taken.append(f"Pulled with rebase ({threads_behind} commits)")
                else:
                    if pull_ff_only(self.threads_repo, threads_branch):
                        pulled = True
                        actions_taken.append(f"Pulled ff-only ({threads_behind} commits)")
                    elif pull_rebase(self.threads_repo, threads_branch):
                        pulled = True
                        actions_taken.append(f"Pulled with rebase ({threads_behind} commits)")

                if not pulled:
                    state.status = ParityStatus.DIVERGED.value
                    state.last_error = "Pull failed for threads branch"
                    write_parity_state(self.threads_repo_path, state)
                    return PreflightResult(
                        success=False,
                        state=state,
                        can_proceed=False,
                        blocking_reason=state.last_error,
                    )

                # Restore stash
                if stash_ref:
                    if restore_stash(self.threads_repo, stash_ref):
                        actions_taken.append("Restored stashed changes")
                    else:
                        state.status = ParityStatus.DIVERGED.value
                        state.last_error = f"Stash pop conflict after pull. Stash: {stash_ref}"
                        write_parity_state(self.threads_repo_path, state)
                        return PreflightResult(
                            success=False,
                            state=state,
                            can_proceed=False,
                            blocking_reason=state.last_error,
                        )

                # Update ahead/behind
                threads_ahead, threads_behind = get_ahead_behind(
                    self.threads_repo, threads_branch
                )
                state.threads_ahead_origin = threads_ahead
                state.threads_behind_origin = threads_behind

            # All checks passed
            state.status = ParityStatus.CLEAN.value
            state.actions_taken = actions_taken
            write_parity_state(self.threads_repo_path, state)

            return PreflightResult(
                success=True,
                state=state,
                can_proceed=True,
                auto_fixed=len(actions_taken) > 0,
                actions_taken=actions_taken,
            )

        except Exception as e:
            state.status = ParityStatus.ERROR.value
            state.last_error = f"Unexpected error: {e}"
            try:
                write_parity_state(self.threads_repo_path, state)
            except Exception:
                pass
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

    def push_after_commit(
        self,
        *,
        max_retries: int = 5,
    ) -> Tuple[bool, Optional[str]]:
        """Push threads repo after commit.

        Args:
            max_retries: Maximum push retry attempts

        Returns:
            Tuple of (success, error_message)
        """
        try:
            branch = get_branch_name(self.threads_repo)
            if not branch:
                return (False, "Cannot push from detached HEAD state")

            try:
                validate_branch_name(branch)
            except ValueError as e:
                return (False, f"Invalid branch name: {e}")

            if push_with_retry(self.threads_repo, branch, max_retries=max_retries):
                return (True, None)
            else:
                return (False, f"Push failed after {max_retries} attempts")

        except Exception as e:
            return (False, f"Unexpected error during push: {e}")

    def get_health(self) -> Dict[str, Any]:
        """Get current branch health with LIVE git checks.

        Returns:
            Dict with live status information
        """
        return self._state_manager.get_live_status()
