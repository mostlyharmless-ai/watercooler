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
    MAX_PUSH_RETRIES,
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
class BranchMismatch:
    """Represents a branch pairing mismatch.

    Compatible with the legacy git_sync.py API for backward compatibility.

    Attributes:
        type: Mismatch type - "branch_name_mismatch", "code_branch_missing", etc.
        code: Code branch name (if available)
        threads: Threads branch name (if available)
        severity: "error" or "warning"
        recovery: Suggested recovery command or action
        needs_merge_to_main: True if threads branch should be merged to main
    """

    type: str
    code: Optional[str]
    threads: Optional[str]
    severity: str
    recovery: str
    needs_merge_to_main: bool = False


@dataclass
class BranchSyncResult:
    """Result of branch history synchronization.

    Attributes:
        success: Whether the sync operation succeeded
        action_taken: Action performed - "rebased", "reset", "fast_forward", "no_action", "error"
        commits_preserved: Number of local commits preserved after rebase
        commits_lost: Number of commits that couldn't be rebased (conflicts)
        details: Human-readable description of what happened
        needs_manual_resolution: True if manual intervention required
    """

    success: bool
    action_taken: str
    commits_preserved: int = 0
    commits_lost: int = 0
    details: str = ""
    needs_manual_resolution: bool = False


@dataclass
class BranchDivergenceInfo:
    """Information about branch history divergence between repos.

    Attributes:
        diverged: Whether branches have diverged
        commits_ahead: Threads branch commits ahead of common ancestor
        commits_behind: Threads branch commits behind code branch
        common_ancestor: Common merge-base commit SHA (if any)
        needs_rebase: True if threads branch needs to be rebased
        needs_fetch: True if remote fetch might help
        details: Human-readable explanation
        needs_merge_to_main: True if threads branch should be merged to main
    """

    diverged: bool
    commits_ahead: int
    commits_behind: int
    common_ancestor: Optional[str]
    needs_rebase: bool
    needs_fetch: bool
    details: str
    needs_merge_to_main: bool = False


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
        mismatches: List of BranchMismatch objects
        warnings: List of non-blocking warnings
    """

    valid: bool
    code_branch: Optional[str] = None
    threads_branch: Optional[str] = None
    state_class: Optional[StateClass] = None
    mismatches: List[BranchMismatch] = field(default_factory=list)
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

                # Detect MAIN_PROTECTION case: threads on main, code on feature branch
                main_branch = _find_main_branch(self.threads_repo)
                is_main_protection = (
                    main_branch is not None
                    and threads_branch == main_branch
                    and code_branch != main_branch
                )

                if is_main_protection:
                    state_class = StateClass.MAIN_PROTECTION
                    mismatches.append(
                        f"Threads on '{main_branch}' but code on '{code_branch}'. "
                        f"Threads branch needs to be switched to match code."
                    )
                else:
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
                state.last_error = "Code repository has unresolved merge conflicts. Resolve manually, then git add and commit"
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
                    state.last_error = "Threads repository has unresolved merge conflicts. Resolve manually"
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

            # Detect MAIN_PROTECTION case: threads on main, code on feature branch
            # This is a special case that needs specific handling
            main_branch = _find_main_branch(self.threads_repo)
            is_main_protection = (
                main_branch is not None
                and threads_branch == main_branch
                and code_branch != main_branch
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

                    checkout_success = False

                    if is_main_protection:
                        # MAIN_PROTECTION: threads on main, code on feature branch
                        # Try in order: local branch exists, origin has branch, create new
                        log_debug(
                            f"[PARITY] MAIN_PROTECTION: threads on '{main_branch}', "
                            f"code on '{code_branch}'"
                        )

                        # 1. Check if branch exists locally
                        local_branches = [h.name for h in self.threads_repo.heads]
                        if code_branch in local_branches:
                            checkout_success = checkout_branch(
                                self.threads_repo, code_branch, create=False
                            )
                            if checkout_success:
                                actions_taken.append(
                                    f"MAIN_PROTECTION: Switched to existing local branch {code_branch}"
                                )

                        # 2. Check if origin has the branch
                        if not checkout_success:
                            if branch_exists_on_origin(self.threads_repo, code_branch):
                                # Fetch and checkout
                                try:
                                    fetch_with_timeout(self.threads_repo)
                                    checkout_success = checkout_branch(
                                        self.threads_repo, code_branch, create=False
                                    )
                                    if checkout_success:
                                        actions_taken.append(
                                            f"MAIN_PROTECTION: Fetched and switched to origin/{code_branch}"
                                        )
                                except Exception as e:
                                    log_debug(f"[PARITY] Fetch for MAIN_PROTECTION failed: {e}")

                        # 3. Create new branch from main
                        if not checkout_success:
                            checkout_success = checkout_branch(
                                self.threads_repo, code_branch, create=True
                            )
                            if checkout_success:
                                actions_taken.append(
                                    f"MAIN_PROTECTION: Created new branch {code_branch} from {main_branch}"
                                )
                    else:
                        # Generic branch mismatch: checkout to match code branch
                        # First try without create (if branch exists), then with create
                        checkout_success = checkout_branch(
                            self.threads_repo, code_branch, create=False
                        )
                        if not checkout_success:
                            checkout_success = checkout_branch(
                                self.threads_repo, code_branch, create=True
                            )

                        if checkout_success:
                            actions_taken.append(f"Checked out threads to {code_branch}")

                    if checkout_success:
                        threads_branch = code_branch
                        state.threads_branch = threads_branch
                    else:
                        # Checkout failed - use appropriate status
                        if is_main_protection:
                            state.status = ParityStatus.MAIN_PROTECTION.value
                            state.last_error = (
                                f"Threads on '{main_branch}' but code on '{code_branch}'. "
                                f"Failed to switch threads branch. "
                                f"Use watercooler_sync_branch_state to manually fix."
                            )
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
                    # Not auto-fix: report appropriate status
                    if is_main_protection:
                        state.status = ParityStatus.MAIN_PROTECTION.value
                        state.last_error = (
                            f"Threads on '{main_branch}' but code on '{code_branch}'. "
                            f"Use watercooler_sync_branch_state to fix."
                        )
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

            # Check for orphan branch: threads on origin but code not on origin
            code_on_origin = branch_exists_on_origin(self.code_repo, code_branch)
            threads_on_origin = branch_exists_on_origin(self.threads_repo, threads_branch)
            if threads_on_origin and not code_on_origin:
                state.status = ParityStatus.ORPHAN_BRANCH.value
                state.last_error = (
                    f"Orphan threads branch: '{threads_branch}' exists on origin "
                    f"but code branch was deleted or never pushed"
                )
                write_parity_state(self.threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            # Check and set upstream tracking for threads branch if missing
            if auto_fix:
                try:
                    tracking = self.threads_repo.active_branch.tracking_branch()
                    remote_ref = f"origin/{threads_branch}"
                    has_remote = False
                    try:
                        has_remote = remote_ref in [
                            r.name for r in self.threads_repo.remotes.origin.refs
                        ]
                    except Exception:
                        pass
                    if tracking is None and has_remote:
                        self.threads_repo.git.branch("--set-upstream-to", remote_ref)
                        actions_taken.append(f"Set upstream tracking: {threads_branch} -> {remote_ref}")
                except Exception as e:
                    log_debug(f"[PARITY] Could not check/set upstream (non-fatal): {e}")

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
                state.last_error = f"Code is {code_behind} commits behind origin. Run 'git pull' in code repo"
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

                # Check for conflicts after pull (rebase may leave conflicts)
                # Auto-resolve if only graph files or only thread files are in conflict
                if has_conflicts(self.threads_repo):
                    log_debug("[PARITY] Conflicts detected after pull, attempting auto-resolution")
                    resolver = ConflictResolver(self.threads_repo)

                    if has_graph_conflicts_only(self.threads_repo):
                        if resolver.resolve_graph_conflicts():
                            try:
                                self.threads_repo.git.rebase("--continue")
                                actions_taken.append("Auto-resolved graph conflicts after pull")
                            except GitCommandError as e:
                                log_debug(f"[PARITY] Rebase continue failed: {e}")
                                state.status = ParityStatus.DIVERGED.value
                                state.last_error = (
                                    "Graph conflicts resolved but rebase continue failed. "
                                    "Manual intervention required."
                                )
                                write_parity_state(self.threads_repo_path, state)
                                return PreflightResult(
                                    success=False,
                                    state=state,
                                    can_proceed=False,
                                    blocking_reason=state.last_error,
                                )
                        else:
                            state.status = ParityStatus.DIVERGED.value
                            state.last_error = "Graph conflicts after pull could not be auto-resolved"
                            write_parity_state(self.threads_repo_path, state)
                            return PreflightResult(
                                success=False,
                                state=state,
                                can_proceed=False,
                                blocking_reason=state.last_error,
                            )
                    elif has_thread_conflicts_only(self.threads_repo):
                        if resolver.resolve_thread_conflicts():
                            try:
                                self.threads_repo.git.rebase("--continue")
                                actions_taken.append("Auto-resolved thread conflicts after pull")
                            except GitCommandError as e:
                                log_debug(f"[PARITY] Rebase continue failed: {e}")
                                state.status = ParityStatus.DIVERGED.value
                                state.last_error = (
                                    "Thread conflicts resolved but rebase continue failed. "
                                    "Manual intervention required."
                                )
                                write_parity_state(self.threads_repo_path, state)
                                return PreflightResult(
                                    success=False,
                                    state=state,
                                    can_proceed=False,
                                    blocking_reason=state.last_error,
                                )
                        else:
                            state.status = ParityStatus.DIVERGED.value
                            state.last_error = "Thread conflicts after pull could not be auto-resolved"
                            write_parity_state(self.threads_repo_path, state)
                            return PreflightResult(
                                success=False,
                                state=state,
                                can_proceed=False,
                                blocking_reason=state.last_error,
                            )
                    else:
                        # Mixed conflicts (both graph and thread files) - require manual resolution
                        state.status = ParityStatus.DIVERGED.value
                        state.last_error = (
                            "Mixed conflicts after pull (both graph and thread files). "
                            "Manual resolution required."
                        )
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

            # Threads ahead of origin: AUTO-PUSH
            if threads_ahead > 0 and auto_fix:
                pushed = push_with_retry(
                    self.threads_repo, threads_branch, max_retries=MAX_PUSH_RETRIES
                )
                if pushed:
                    actions_taken.append(f"Pushed threads to origin ({threads_ahead} commits)")
                    # Update ahead/behind
                    threads_ahead, threads_behind = get_ahead_behind(
                        self.threads_repo, threads_branch
                    )
                    state.threads_ahead_origin = threads_ahead
                    state.threads_behind_origin = threads_behind
                else:
                    # Push failed - still allow proceeding but mark pending
                    state.pending_push = True
                    actions_taken.append("Push failed (marked pending)")

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


# =============================================================================
# Helper Functions
# =============================================================================


def _find_main_branch(repo: Repo) -> Optional[str]:
    """Find the main/master branch name in a repository.

    Args:
        repo: GitPython Repo object

    Returns:
        Branch name ('main' or 'master') if found, None otherwise
    """
    for name in ["main", "master"]:
        try:
            repo.commit(name)
            return name
        except Exception as e:
            log_debug(f"Branch '{name}' not found: {e}")
            continue
    return None


def _fuzzy_match_branches(branch1: str, branch2: str) -> float:
    """Calculate similarity score between two branch names.

    Uses simple character-based similarity (ratio of common characters).
    Returns a score between 0.0 and 1.0, where 1.0 is exact match.

    Args:
        branch1: First branch name
        branch2: Second branch name

    Returns:
        Similarity score (0.0 to 1.0)
    """
    def normalize(name: str) -> str:
        name = name.lower()
        for prefix in ['feature/', 'feat/', 'fix/', 'bugfix/', 'hotfix/', 'release/']:
            if name.startswith(prefix):
                name = name[len(prefix):]
        return name

    norm1 = normalize(branch1)
    norm2 = normalize(branch2)

    if norm1 == norm2:
        return 1.0

    set1 = set(norm1)
    set2 = set(norm2)
    intersection = len(set1 & set2)
    union = len(set1 | set2)

    if union == 0:
        return 0.0

    return intersection / union


def _detect_branch_rename(
    code_repo_obj: Repo,
    threads_repo_obj: Repo,
    code_branch: Optional[str],
    threads_branch: Optional[str],
) -> Tuple[bool, Optional[str], float]:
    """Detect if a branch was renamed by finding similar branch names.

    Args:
        code_repo_obj: GitPython Repo object for code repository
        threads_repo_obj: GitPython Repo object for threads repository
        code_branch: Current code branch name (or None)
        threads_branch: Current threads branch name (or None)

    Returns:
        Tuple of (is_rename, suggested_branch, similarity_score)
    """
    if not code_branch or not threads_branch:
        return (False, None, 0.0)

    try:
        code_branches = {b.name for b in code_repo_obj.heads}
        threads_branches = {b.name for b in threads_repo_obj.heads}

        if code_branch in threads_branches and threads_branch in code_branches:
            return (False, None, 0.0)

        if code_branch not in threads_branches:
            best_match = None
            best_score = 0.0
            for thread_branch in threads_branches:
                score = _fuzzy_match_branches(code_branch, thread_branch)
                if score > best_score and score > 0.6:
                    best_score = score
                    best_match = thread_branch

            if best_match:
                return (True, best_match, best_score)

        if threads_branch not in code_branches:
            best_match = None
            best_score = 0.0
            for code_branch_candidate in code_branches:
                score = _fuzzy_match_branches(threads_branch, code_branch_candidate)
                if score > best_score and score > 0.6:
                    best_score = score
                    best_match = code_branch_candidate

            if best_match:
                return (True, best_match, best_score)

        return (False, None, 0.0)
    except Exception:
        return (False, None, 0.0)


def _detect_branch_divergence(
    code_repo_obj: Repo,
    threads_repo_obj: Repo,
    code_branch: str,
    threads_branch: str,
) -> BranchDivergenceInfo:
    """Detect if branches have diverged in commit history.

    Args:
        code_repo_obj: GitPython Repo object for code repository
        threads_repo_obj: GitPython Repo object for threads repository
        code_branch: Name of the code branch
        threads_branch: Name of the threads branch

    Returns:
        BranchDivergenceInfo with divergence status and remediation info
    """
    try:
        try:
            code_head = code_repo_obj.commit(code_branch)
        except Exception:
            return BranchDivergenceInfo(
                diverged=False,
                commits_ahead=0,
                commits_behind=0,
                common_ancestor=None,
                needs_rebase=False,
                needs_fetch=False,
                details=f"Could not find code branch '{code_branch}'"
            )

        try:
            threads_head = threads_repo_obj.commit(threads_branch)
        except Exception:
            return BranchDivergenceInfo(
                diverged=False,
                commits_ahead=0,
                commits_behind=0,
                common_ancestor=None,
                needs_rebase=False,
                needs_fetch=False,
                details=f"Could not find threads branch '{threads_branch}'"
            )

        threads_origin_ref = None
        needs_fetch = False
        try:
            threads_origin_ref = threads_repo_obj.commit(f"origin/{threads_branch}")
        except Exception:
            needs_fetch = True

        commits_ahead = 0
        commits_behind = 0
        common_ancestor_sha: Optional[str] = None

        if threads_origin_ref:
            try:
                merge_base = threads_repo_obj.merge_base(threads_head, threads_origin_ref)
                if merge_base:
                    common_ancestor_sha = merge_base[0].hexsha[:8]

                    ahead_commits = list(threads_repo_obj.iter_commits(
                        f"origin/{threads_branch}..{threads_branch}"
                    ))
                    commits_ahead = len(ahead_commits)

                    behind_commits = list(threads_repo_obj.iter_commits(
                        f"{threads_branch}..origin/{threads_branch}"
                    ))
                    commits_behind = len(behind_commits)
            except Exception:
                pass

        diverged = commits_ahead > 0 and commits_behind > 0
        needs_rebase = diverged or commits_behind > 0

        if diverged:
            details = (
                f"Threads branch '{threads_branch}' has diverged from origin: "
                f"{commits_ahead} commits ahead, {commits_behind} behind. "
                f"Common ancestor: {common_ancestor_sha or 'unknown'}. "
                f"Recommended: rebase threads branch onto origin/{threads_branch}"
            )
        elif commits_behind > 0:
            details = (
                f"Threads branch '{threads_branch}' is {commits_behind} commits behind origin. "
                f"Recommended: pull or rebase to sync with remote."
            )
        elif commits_ahead > 0:
            details = (
                f"Threads branch '{threads_branch}' is {commits_ahead} commits ahead of origin. "
                f"This is normal for unpushed local changes."
            )
        elif needs_fetch:
            details = (
                f"No remote tracking info for threads branch '{threads_branch}'. "
                f"Consider fetching from origin to check for updates."
            )
        else:
            details = f"Threads branch '{threads_branch}' is in sync with origin."

        return BranchDivergenceInfo(
            diverged=diverged,
            commits_ahead=commits_ahead,
            commits_behind=commits_behind,
            common_ancestor=common_ancestor_sha,
            needs_rebase=needs_rebase,
            needs_fetch=needs_fetch,
            details=details,
        )

    except Exception as e:
        return BranchDivergenceInfo(
            diverged=False,
            commits_ahead=0,
            commits_behind=0,
            common_ancestor=None,
            needs_rebase=False,
            needs_fetch=True,
            details=f"Error detecting divergence: {str(e)}"
        )


def _detect_behind_main_divergence(
    code_repo_obj: Repo,
    threads_repo_obj: Repo,
    code_branch: str,
    threads_branch: str,
) -> Optional[BranchDivergenceInfo]:
    """Detect if threads branch is behind main while code branch is not.

    Args:
        code_repo_obj: GitPython Repo object for code repository
        threads_repo_obj: GitPython Repo object for threads repository
        code_branch: Name of the code branch
        threads_branch: Name of the threads branch

    Returns:
        BranchDivergenceInfo if divergence detected, None otherwise
    """
    code_main = _find_main_branch(code_repo_obj)
    threads_main = _find_main_branch(threads_repo_obj)

    if not code_main or not threads_main:
        log_debug(f"[PARITY] Early exit: main branch not found")
        return None

    if code_branch == code_main or threads_branch == threads_main:
        log_debug(f"[PARITY] Early exit: already on main")
        return None

    try:
        def _get_main_ref(repo: Repo, main_branch: str, repo_name: str) -> str:
            origin_ref = f"origin/{main_branch}"
            try:
                repo.commit(origin_ref)
                log_debug(f"[PARITY] {repo_name}: using remote ref {origin_ref}")
                return origin_ref
            except Exception:
                log_debug(f"[PARITY] {repo_name}: using local {main_branch}")
                return main_branch

        code_main_ref = _get_main_ref(code_repo_obj, code_main, "code")
        threads_main_ref = _get_main_ref(threads_repo_obj, threads_main, "threads")

        code_behind_main = list(code_repo_obj.iter_commits(
            f"{code_branch}..{code_main_ref}"
        ))
        code_ahead_main = list(code_repo_obj.iter_commits(
            f"{code_main_ref}..{code_branch}"
        ))

        threads_behind_main = list(threads_repo_obj.iter_commits(
            f"{threads_branch}..{threads_main_ref}"
        ))
        threads_ahead_main = list(threads_repo_obj.iter_commits(
            f"{threads_main_ref}..{threads_branch}"
        ))

        code_tree_main = code_repo_obj.commit(code_main_ref).tree.hexsha
        code_tree_branch = code_repo_obj.commit(code_branch).tree.hexsha
        code_content_synced = (code_tree_main == code_tree_branch)
        code_commit_synced = len(code_behind_main) == 0 and len(code_ahead_main) == 0
        code_synced = code_content_synced or code_commit_synced

        if code_synced and len(threads_behind_main) > 0:
            sync_reason = "content-equivalent" if code_content_synced else "0 commits behind"
            return BranchDivergenceInfo(
                diverged=True,
                commits_ahead=0,
                commits_behind=len(threads_behind_main),
                common_ancestor=None,
                needs_rebase=True,
                needs_fetch=False,
                details=(
                    f"Threads branch '{threads_branch}' is {len(threads_behind_main)} commits behind "
                    f"'{threads_main}', but code branch '{code_branch}' is synced with "
                    f"'{code_main}' ({sync_reason}). Recommended: rebase threads/{threads_branch} "
                    f"onto threads/{threads_main}"
                )
            )

        if code_synced and len(threads_ahead_main) > 0 and len(threads_behind_main) == 0:
            sync_reason = "content-equivalent" if code_content_synced else "0 commits behind"
            return BranchDivergenceInfo(
                diverged=True,
                commits_ahead=len(threads_ahead_main),
                commits_behind=0,
                common_ancestor=None,
                needs_rebase=False,
                needs_fetch=False,
                details=(
                    f"Threads branch '{threads_branch}' is {len(threads_ahead_main)} commits ahead of "
                    f"'{threads_main}', but code branch '{code_branch}' is synced with "
                    f"'{code_main}' ({sync_reason}). Code PR was merged; threads branch will be "
                    f"merged to main to maintain parity."
                ),
                needs_merge_to_main=True,
            )

        return None

    except Exception as e:
        log_debug(f"[PARITY] Error checking behind-main divergence: {e}")
        return None


def _rebase_branch_onto(
    repo: Repo,
    branch: str,
    onto: str,
    force: bool,
) -> BranchSyncResult:
    """Rebase a branch onto another branch.

    Args:
        repo: GitPython Repo object
        branch: Branch to rebase
        onto: Target branch to rebase onto
        force: If True, force-push after rebase

    Returns:
        BranchSyncResult with outcome details
    """
    original_branch: Optional[str] = None
    stash_created = False

    try:
        try:
            log_debug(f"Fetching origin before rebase onto {onto}")
            repo.git.fetch('origin')
        except Exception as e:
            log_debug(f"Warning: Could not fetch origin: {e}")

        rebase_target = f"origin/{onto}"
        try:
            repo.commit(rebase_target)
        except Exception:
            log_debug(f"Remote ref '{rebase_target}' not found, using local '{onto}'")
            rebase_target = onto

        commits_ahead = sum(1 for _ in repo.iter_commits(f"{rebase_target}..{branch}"))
        commits_behind = sum(1 for _ in repo.iter_commits(f"{branch}..{rebase_target}"))

        if commits_behind == 0:
            return BranchSyncResult(
                success=True,
                action_taken="no_action",
                commits_preserved=commits_ahead,
                commits_lost=0,
                details=f"Branch '{branch}' is already up-to-date with '{rebase_target}'.",
                needs_manual_resolution=False,
            )

        stash_needed = repo.is_dirty()
        if stash_needed:
            try:
                repo.git.stash('push', '-m', 'Auto-stash for rebase onto main')
                stash_created = True
            except Exception as e:
                log_debug(f"Warning: Could not stash changes: {e}")

        try:
            original_branch = repo.active_branch.name
        except TypeError:
            original_branch = None

        if original_branch != branch:
            log_debug(f"Switching from '{original_branch}' to '{branch}' for rebase")
            try:
                repo.git.checkout(branch)
            except Exception as e:
                if stash_created:
                    try:
                        repo.git.stash('pop')
                    except Exception:
                        pass
                return BranchSyncResult(
                    success=False,
                    action_taken="error",
                    commits_preserved=0,
                    commits_lost=0,
                    details=f"Failed to checkout branch '{branch}': {e}",
                    needs_manual_resolution=True,
                )

        try:
            log_debug(f"GIT_OP_START: rebase {branch} onto {rebase_target}")
            repo.git.rebase(rebase_target)
            log_debug(f"GIT_OP_END: rebase {branch} onto {rebase_target}")

            if stash_created:
                try:
                    repo.git.stash('pop')
                except Exception as e:
                    log_debug(f"Warning: Could not pop stash after rebase: {e}")

            if force:
                repo.git.push('origin', branch, '--force-with-lease')
                push_msg = "Force-pushed rebased branch to origin."
            else:
                push_msg = "Rebase complete. Run with force=True to push, or push manually."

            return BranchSyncResult(
                success=True,
                action_taken="rebased",
                commits_preserved=commits_ahead,
                commits_lost=0,
                details=(
                    f"Rebased {commits_ahead} commits from '{branch}' onto '{rebase_target}' "
                    f"(was {commits_behind} behind). {push_msg}"
                ),
                needs_manual_resolution=not force,
            )
        except Exception as e:
            try:
                repo.git.rebase('--abort')
            except Exception:
                pass

            if stash_created:
                try:
                    repo.git.stash('pop')
                except Exception:
                    pass

            return BranchSyncResult(
                success=False,
                action_taken="error",
                commits_preserved=0,
                commits_lost=commits_ahead,
                details=f"Rebase of '{branch}' onto '{rebase_target}' failed (likely conflicts): {str(e)}",
                needs_manual_resolution=True,
            )

    except Exception as e:
        return BranchSyncResult(
            success=False,
            action_taken="error",
            commits_preserved=0,
            commits_lost=0,
            details=f"Error during rebase: {str(e)}",
            needs_manual_resolution=True,
        )


# =============================================================================
# Standalone Functions (Public API)
# =============================================================================


def validate_branch_pairing(
    code_repo: Path,
    threads_repo: Path,
    strict: bool = True,
    check_history: bool = False,
) -> BranchPairingResult:
    """Validate that code and threads repos are on matching branches.

    Args:
        code_repo: Path to code repository root
        threads_repo: Path to threads repository root
        strict: If True, return valid=False on any mismatch
        check_history: If True, also check for commit history divergence

    Returns:
        BranchPairingResult with validation status
    """
    mismatches: List[BranchMismatch] = []
    warnings: List[str] = []

    # Get code repo branch
    code_branch: Optional[str] = None
    code_repo_obj: Optional[Repo] = None
    try:
        code_repo_obj = Repo(code_repo, search_parent_directories=True)
        if code_repo_obj.head.is_detached:
            warnings.append("Code repo is in detached HEAD state")
        else:
            code_branch = code_repo_obj.active_branch.name
    except InvalidGitRepositoryError:
        mismatches.append(BranchMismatch(
            type="code_repo_not_git",
            code=None,
            threads=None,
            severity="error",
            recovery=f"Code path {code_repo} is not a git repository"
        ))
        return BranchPairingResult(
            valid=False,
            code_branch=None,
            threads_branch=None,
            mismatches=mismatches,
            warnings=warnings,
        )
    except Exception as e:
        mismatches.append(BranchMismatch(
            type="code_repo_error",
            code=None,
            threads=None,
            severity="error",
            recovery=f"Failed to read code repo: {str(e)}"
        ))
        return BranchPairingResult(
            valid=False,
            code_branch=None,
            threads_branch=None,
            mismatches=mismatches,
            warnings=warnings,
        )

    # Get threads repo branch
    threads_branch: Optional[str] = None
    threads_repo_obj: Optional[Repo] = None
    try:
        threads_repo_obj = Repo(threads_repo, search_parent_directories=True)
        if threads_repo_obj.head.is_detached:
            warnings.append("Threads repo is in detached HEAD state")
        else:
            threads_branch = threads_repo_obj.active_branch.name
    except InvalidGitRepositoryError:
        mismatches.append(BranchMismatch(
            type="threads_repo_not_git",
            code=code_branch,
            threads=None,
            severity="error",
            recovery=f"Threads path {threads_repo} is not a git repository"
        ))
        return BranchPairingResult(
            valid=False,
            code_branch=code_branch,
            threads_branch=None,
            mismatches=mismatches,
            warnings=warnings,
        )
    except Exception as e:
        mismatches.append(BranchMismatch(
            type="threads_repo_error",
            code=code_branch,
            threads=None,
            severity="error",
            recovery=f"Failed to read threads repo: {str(e)}"
        ))
        return BranchPairingResult(
            valid=False,
            code_branch=code_branch,
            threads_branch=None,
            mismatches=mismatches,
            warnings=warnings,
        )

    # Compare branches
    if code_branch is None and threads_branch is None:
        warnings.append("Both repos in detached HEAD state")
        return BranchPairingResult(
            valid=not strict,
            code_branch=None,
            threads_branch=None,
            mismatches=mismatches,
            warnings=warnings,
        )

    if code_branch is None:
        mismatches.append(BranchMismatch(
            type="code_branch_detached",
            code=None,
            threads=threads_branch,
            severity="error",
            recovery="Checkout a branch in code repo or create one"
        ))
    elif threads_branch is None:
        mismatches.append(BranchMismatch(
            type="threads_branch_detached",
            code=code_branch,
            threads=None,
            severity="error",
            recovery=f"Checkout branch '{code_branch}' in threads repo"
        ))
    elif code_branch != threads_branch:
        is_rename, suggested_branch, similarity = _detect_branch_rename(
            code_repo_obj, threads_repo_obj, code_branch, threads_branch
        )

        if is_rename and suggested_branch:
            recovery_msg = (
                f"Possible branch rename detected (similarity: {similarity:.0%}). "
                f"Suggested branch: '{suggested_branch}'. "
                f"Run: watercooler_sync_branch_state with operation='checkout' and branch='{suggested_branch}'"
            )
            warnings.append(
                f"Branch name mismatch may be due to rename: '{code_branch}' vs '{threads_branch}' "
                f"(suggested: '{suggested_branch}')"
            )
        else:
            recovery_msg = (
                f"Run: watercooler_sync_branch_state with operation='checkout' to sync branches"
            )

        mismatches.append(BranchMismatch(
            type="branch_name_mismatch",
            code=code_branch,
            threads=threads_branch,
            severity="error",
            recovery=recovery_msg
        ))
    elif check_history and code_repo_obj and threads_repo_obj:
        divergence = _detect_branch_divergence(
            code_repo_obj, threads_repo_obj, code_branch, threads_branch
        )

        if divergence.diverged:
            mismatches.append(BranchMismatch(
                type="branch_history_diverged",
                code=code_branch,
                threads=threads_branch,
                severity="error",
                recovery=(
                    f"Branch histories have diverged: {divergence.commits_ahead} ahead, "
                    f"{divergence.commits_behind} behind. "
                    f"Run: watercooler_sync_branch_state with operation='recover' to attempt auto-fix, "
                    f"or manually rebase the threads branch."
                )
            ))
            warnings.append(divergence.details)
        elif divergence.needs_rebase:
            warnings.append(
                f"Threads branch is {divergence.commits_behind} commits behind origin. "
                f"Consider pulling or rebasing."
            )
        elif divergence.needs_fetch:
            warnings.append(divergence.details)

        behind_main = _detect_behind_main_divergence(
            code_repo_obj, threads_repo_obj, code_branch, threads_branch
        )
        if behind_main:
            if behind_main.needs_merge_to_main:
                mismatches.append(BranchMismatch(
                    type="branch_history_diverged",
                    code=code_branch,
                    threads=threads_branch,
                    severity="error",
                    recovery=(
                        f"Threads branch is {behind_main.commits_ahead} commits ahead of main "
                        f"but code branch is synced with main (PR merged). "
                        f"Will auto-merge threads branch to main."
                    ),
                    needs_merge_to_main=True,
                ))
            else:
                mismatches.append(BranchMismatch(
                    type="branch_history_diverged",
                    code=code_branch,
                    threads=threads_branch,
                    severity="error",
                    recovery=(
                        f"Threads branch is {behind_main.commits_behind} commits behind main "
                        f"but code branch is up-to-date. "
                        f"Run: watercooler_sync_branch_state with operation='recover' to rebase "
                        f"threads branch onto main."
                    )
                ))
            warnings.append(behind_main.details)

    # Determine validity
    has_errors = any(m.severity == "error" for m in mismatches)
    valid = not has_errors if strict else len(mismatches) == 0

    return BranchPairingResult(
        valid=valid,
        code_branch=code_branch,
        threads_branch=threads_branch,
        mismatches=mismatches,
        warnings=warnings,
    )


def sync_branch_history(
    threads_repo_path: Path,
    branch: str,
    strategy: str = "rebase",
    force: bool = False,
    onto: Optional[str] = None,
) -> BranchSyncResult:
    """Synchronize threads branch history with a target branch.

    Args:
        threads_repo_path: Path to threads repository
        branch: Branch name to sync
        strategy: Sync strategy - "rebase", "reset", or "merge"
        force: If True, use force push after rebase/reset
        onto: Target branch to rebase onto

    Returns:
        BranchSyncResult with outcome details
    """
    try:
        repo = Repo(threads_repo_path, search_parent_directories=True)

        if repo.active_branch.name != branch:
            try:
                repo.git.checkout(branch)
            except Exception as e:
                return BranchSyncResult(
                    success=False,
                    action_taken="error",
                    commits_preserved=0,
                    commits_lost=0,
                    details=f"Failed to checkout branch '{branch}': {str(e)}",
                    needs_manual_resolution=True,
                )

        if onto and onto != f"origin/{branch}":
            return _rebase_branch_onto(repo, branch, onto, force)

        try:
            repo.git.fetch('origin', branch)
        except Exception as e:
            return BranchSyncResult(
                success=False,
                action_taken="error",
                commits_preserved=0,
                commits_lost=0,
                details=f"Failed to fetch from origin: {str(e)}",
                needs_manual_resolution=True,
            )

        local_head = repo.commit(branch)
        try:
            remote_head = repo.commit(f"origin/{branch}")
        except Exception:
            try:
                repo.git.push('origin', branch, '--set-upstream')
                return BranchSyncResult(
                    success=True,
                    action_taken="push_new",
                    commits_preserved=0,
                    commits_lost=0,
                    details=f"Pushed new branch '{branch}' to origin.",
                    needs_manual_resolution=False,
                )
            except Exception as push_e:
                return BranchSyncResult(
                    success=False,
                    action_taken="error",
                    commits_preserved=0,
                    commits_lost=0,
                    details=f"Failed to push new branch: {str(push_e)}",
                    needs_manual_resolution=True,
                )

        try:
            merge_base_list = repo.merge_base(local_head, remote_head)
            if not merge_base_list:
                return BranchSyncResult(
                    success=False,
                    action_taken="error",
                    commits_preserved=0,
                    commits_lost=0,
                    details="No common ancestor found. Branches have divergent histories.",
                    needs_manual_resolution=True,
                )
            merge_base = merge_base_list[0]
        except Exception as e:
            return BranchSyncResult(
                success=False,
                action_taken="error",
                commits_preserved=0,
                commits_lost=0,
                details=f"Failed to find merge base: {str(e)}",
                needs_manual_resolution=True,
            )

        commits_ahead = len(list(repo.iter_commits(f"origin/{branch}..{branch}")))
        commits_behind = len(list(repo.iter_commits(f"{branch}..origin/{branch}")))

        if commits_ahead == 0 and commits_behind == 0:
            return BranchSyncResult(
                success=True,
                action_taken="no_action",
                commits_preserved=0,
                commits_lost=0,
                details="Branch is already in sync with origin.",
                needs_manual_resolution=False,
            )

        if commits_ahead == 0 and commits_behind > 0:
            try:
                repo.git.pull('origin', branch, '--ff-only')
                return BranchSyncResult(
                    success=True,
                    action_taken="fast_forward",
                    commits_preserved=0,
                    commits_lost=0,
                    details=f"Fast-forwarded {commits_behind} commits from origin.",
                    needs_manual_resolution=False,
                )
            except Exception:
                pass

        if commits_ahead > 0 and commits_behind == 0:
            try:
                repo.git.push('origin', branch)
                return BranchSyncResult(
                    success=True,
                    action_taken="push",
                    commits_preserved=commits_ahead,
                    commits_lost=0,
                    details=f"Pushed {commits_ahead} local commits to origin.",
                    needs_manual_resolution=False,
                )
            except Exception as e:
                return BranchSyncResult(
                    success=False,
                    action_taken="error",
                    commits_preserved=0,
                    commits_lost=0,
                    details=f"Failed to push: {str(e)}",
                    needs_manual_resolution=True,
                )

        # Diverged - need to reconcile
        if strategy == "rebase":
            try:
                stash_needed = repo.is_dirty()
                if stash_needed:
                    repo.git.stash()

                repo.git.rebase(f"origin/{branch}")

                if stash_needed:
                    try:
                        repo.git.stash('pop')
                    except Exception:
                        pass

                if force:
                    repo.git.push('origin', branch, '--force-with-lease')
                    push_msg = "Force-pushed rebased branch to origin."
                else:
                    push_msg = "Rebase complete. Run with force=True to push."

                return BranchSyncResult(
                    success=True,
                    action_taken="rebased",
                    commits_preserved=commits_ahead,
                    commits_lost=0,
                    details=f"Rebased {commits_ahead} local commits onto origin/{branch}. {push_msg}",
                    needs_manual_resolution=not force,
                )
            except Exception as e:
                try:
                    repo.git.rebase('--abort')
                except Exception:
                    pass

                return BranchSyncResult(
                    success=False,
                    action_taken="error",
                    commits_preserved=0,
                    commits_lost=commits_ahead,
                    details=f"Rebase failed (likely conflicts): {str(e)}. Manual resolution required.",
                    needs_manual_resolution=True,
                )

        elif strategy == "reset":
            if not force:
                return BranchSyncResult(
                    success=False,
                    action_taken="error",
                    commits_preserved=0,
                    commits_lost=0,
                    details=f"Reset strategy requires force=True. This will discard {commits_ahead} local commits.",
                    needs_manual_resolution=False,
                )

            try:
                repo.git.reset('--hard', f"origin/{branch}")
                return BranchSyncResult(
                    success=True,
                    action_taken="reset",
                    commits_preserved=0,
                    commits_lost=commits_ahead,
                    details=f"Reset to origin/{branch}. Lost {commits_ahead} local commits.",
                    needs_manual_resolution=False,
                )
            except Exception as e:
                return BranchSyncResult(
                    success=False,
                    action_taken="error",
                    commits_preserved=0,
                    commits_lost=0,
                    details=f"Reset failed: {str(e)}",
                    needs_manual_resolution=True,
                )

        elif strategy == "merge":
            try:
                repo.git.merge(f"origin/{branch}", '--no-edit')
                repo.git.push('origin', branch)
                return BranchSyncResult(
                    success=True,
                    action_taken="merged",
                    commits_preserved=commits_ahead,
                    commits_lost=0,
                    details=f"Merged origin/{branch} into local and pushed.",
                    needs_manual_resolution=False,
                )
            except Exception as e:
                try:
                    repo.git.merge('--abort')
                except Exception:
                    pass
                return BranchSyncResult(
                    success=False,
                    action_taken="error",
                    commits_preserved=0,
                    commits_lost=0,
                    details=f"Merge failed: {str(e)}",
                    needs_manual_resolution=True,
                )

        else:
            return BranchSyncResult(
                success=False,
                action_taken="error",
                commits_preserved=0,
                commits_lost=0,
                details=f"Unknown strategy: {strategy}. Use 'rebase', 'reset', or 'merge'.",
                needs_manual_resolution=False,
            )

    except InvalidGitRepositoryError:
        return BranchSyncResult(
            success=False,
            action_taken="error",
            commits_preserved=0,
            commits_lost=0,
            details=f"Not a git repository: {threads_repo_path}",
            needs_manual_resolution=True,
        )
    except Exception as e:
        return BranchSyncResult(
            success=False,
            action_taken="error",
            commits_preserved=0,
            commits_lost=0,
            details=f"Unexpected error: {str(e)}",
            needs_manual_resolution=True,
        )


# =============================================================================
# Locking utilities (moved from branch_parity.py)
# =============================================================================

LOCK_TIMEOUT_SECONDS = 30  # How long to wait for lock acquisition
LOCK_TTL_SECONDS = 60  # How long before a lock is considered stale
LOCK_QUICK_RETRIES = 3  # Quick retries before full timeout (handles transient contention)
LOCK_QUICK_RETRY_DELAY = 0.1  # Delay between quick retries in seconds
LOCKS_DIR_NAME = ".watercooler/locks"  # Subdirectory for lock files

# Topic validation constants
MAX_TOPIC_LENGTH = 200
UNSAFE_TOPIC_CHARS_PATTERN = r'[^\w\-.]'  # Characters not allowed in topic names


def _lock_dir(threads_dir: Path) -> Path:
    """Get the directory for lock files."""
    return threads_dir / ".watercooler" / "locks"


def _sanitize_topic_for_filename(topic: str) -> str:
    """Sanitize topic name for use as filename.

    Security: Neutralizes path traversal attempts (../, ..) and
    replaces dangerous filesystem characters. Also strips leading dots
    to avoid hidden files on Unix systems.

    Long names are truncated to MAX_TOPIC_LENGTH with a hash suffix
    for uniqueness.
    """
    import hashlib
    import re
    # First, neutralize path traversal attempts by replacing .. with _
    safe = re.sub(r'\.\.', '_', topic)
    # Replace path separators and other problematic chars
    safe = re.sub(r'[<>:"/\\|?*]', '_', safe)
    # Collapse multiple underscores
    safe = re.sub(r'_+', '_', safe)
    # Trim underscores and leading dots from edges
    safe = safe.strip('_').lstrip('.')
    # Handle empty result (from empty input or all-special-char input)
    if not safe:
        return '_empty_'
    # Truncate long names with hash suffix for uniqueness
    if len(safe) > MAX_TOPIC_LENGTH:
        hash_suffix = hashlib.sha256(topic.encode()).hexdigest()[:8]
        # Leave room for underscore + hash suffix
        truncate_at = MAX_TOPIC_LENGTH - len(hash_suffix) - 1
        safe = f"{safe[:truncate_at]}_{hash_suffix}"
    return safe


def _topic_lock_path(threads_dir: Path, topic: str) -> Path:
    """Get path to per-topic lock file."""
    lock_dir = _lock_dir(threads_dir)
    safe_topic = _sanitize_topic_for_filename(topic)
    return lock_dir / f"{safe_topic}.lock"


def acquire_topic_lock(
    threads_dir: Path, topic: str, timeout: int = LOCK_TIMEOUT_SECONDS
) -> "AdvisoryLock":
    """Acquire lock for a specific topic. Returns lock (caller must release).

    Args:
        threads_dir: Path to threads repository
        topic: Topic name to lock (will be sanitized for filename safety)
        timeout: Seconds to wait for lock acquisition

    Returns:
        AdvisoryLock instance (caller must call release() or use as context manager)

    Raises:
        TimeoutError: If lock cannot be acquired within timeout period
    """
    import time
    from watercooler.lock import AdvisoryLock

    lock_path = _topic_lock_path(threads_dir, topic)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Quick retries for transient contention
    for attempt in range(LOCK_QUICK_RETRIES):
        lock = AdvisoryLock(lock_path, ttl=LOCK_TTL_SECONDS, timeout=0)
        if lock.acquire():
            return lock
        time.sleep(LOCK_QUICK_RETRY_DELAY)

    # Quick retries failed, fall back to full timeout
    lock = AdvisoryLock(lock_path, ttl=LOCK_TTL_SECONDS, timeout=timeout)
    if not lock.acquire():
        lock_info = lock.get_lock_info()
        if lock_info:
            holder_pid = lock_info.get("pid", "unknown")
            holder_time = lock_info.get("time", "unknown")
            holder_user = lock_info.get("user", "unknown")
            raise TimeoutError(
                f"Failed to acquire lock for topic '{topic}' within {timeout}s. "
                f"Lock held by: pid={holder_pid}, user={holder_user}, since={holder_time}. "
                f"If stale, it will auto-expire after TTL ({LOCK_TTL_SECONDS}s)."
            )
        raise TimeoutError(
            f"Failed to acquire lock for topic '{topic}' within {timeout}s."
        )
    return lock


# =============================================================================
# Timestamp utility
# =============================================================================

def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# Standalone function wrappers for backward compatibility
# =============================================================================

def run_preflight(
    code_repo_path: Path,
    threads_repo_path: Path,
    auto_fix: bool = True,
    fetch_first: bool = True,
) -> PreflightResult:
    """Run preflight checks for branch parity.

    Standalone wrapper around BranchParityManager.run_preflight().

    Args:
        code_repo_path: Path to code repository
        threads_repo_path: Path to threads repository
        auto_fix: Whether to auto-fix issues
        fetch_first: Whether to fetch from origin before checking

    Returns:
        PreflightResult with can_proceed, blocking_reason, etc.
    """
    manager = BranchParityManager(code_repo_path, threads_repo_path)
    return manager.run_preflight(auto_fix=auto_fix, fetch_first=fetch_first)


def ensure_readable(threads_repo_path: Path, code_repo_path: Optional[Path] = None) -> Tuple[bool, List[str]]:
    """Ensure threads repo is readable (lightweight sync for reads).

    Standalone wrapper around BranchParityManager.ensure_readable().

    Args:
        threads_repo_path: Path to threads repository
        code_repo_path: Optional path to code repository (for context)

    Returns:
        Tuple of (success, list of actions taken)
    """
    # For ensure_readable, we only need the threads repo
    # Use threads path for both if code path not provided
    code_path = code_repo_path if code_repo_path else threads_repo_path.parent / threads_repo_path.name.replace("-threads", "")
    manager = BranchParityManager(code_path, threads_repo_path)
    return manager.ensure_readable()


def get_branch_health(
    code_repo_path: Path,
    threads_repo_path: Path,
    fetch_first: bool = True,
) -> dict:
    """Get health status of branch parity.

    Standalone wrapper around BranchParityManager.get_health().

    Args:
        code_repo_path: Path to code repository
        threads_repo_path: Path to threads repository
        fetch_first: Whether to fetch from origin before checking (note: get_health
            always performs live checks with non-blocking fetch)

    Returns:
        Dict with health status information
    """
    manager = BranchParityManager(code_repo_path, threads_repo_path)
    # Note: get_health() always does live checks with non-blocking fetch internally
    # The fetch_first parameter is kept for backward compatibility but is effectively ignored
    return manager.get_health()


def push_after_commit(
    threads_repo_path: Path,
    branch_name: Optional[str] = None,  # Backward compat - ignored, branch auto-detected
    *,
    max_retries: int = 5,
    code_repo_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Push threads repo after a commit.

    Standalone wrapper around BranchParityManager.push_after_commit().

    Args:
        threads_repo_path: Path to threads repository
        branch_name: Deprecated - branch is auto-detected from repo
        max_retries: Maximum push retry attempts
        code_repo_path: Optional path to code repository

    Returns:
        Tuple of (success, message)
    """
    code_path = code_repo_path if code_repo_path else threads_repo_path.parent / threads_repo_path.name.replace("-threads", "")
    manager = BranchParityManager(code_path, threads_repo_path)
    return manager.push_after_commit(max_retries=max_retries)


def auto_merge_to_main(
    threads_repo: Repo,
    feature_branch: str,
    main_branch: str,
) -> Tuple[bool, str]:
    """Merge threads feature branch to main after code PR merge.

    This is called when the code branch has been merged to main but the
    threads branch hasn't. Per the branch pairing contract, threads follows
    code, so we auto-merge the threads branch to main.

    Args:
        threads_repo: GitPython Repo object for threads repository
        feature_branch: Name of the feature branch to merge
        main_branch: Name of the main branch

    Returns:
        Tuple of (success: bool, message: str describing what happened)
    """
    from .conflict import has_thread_conflicts_only, has_graph_conflicts_only

    try:
        validate_branch_name(feature_branch)
        validate_branch_name(main_branch)
    except ValueError as e:
        return (False, f"Invalid branch name: {e}")

    original_branch = get_branch_name(threads_repo)
    actions: List[str] = []
    stash_ref = None

    try:
        # 0. Stash any uncommitted changes
        stash_ref = stash_changes(threads_repo, prefix="watercooler-merge")
        if stash_ref:
            actions.append("Stashed uncommitted changes")

        # 1. Fetch latest from origin
        if not fetch_with_timeout(threads_repo):
            log_debug("[PARITY] auto_merge_to_main: fetch failed, proceeding anyway")

        # 2. Checkout main branch
        if not checkout_branch(threads_repo, main_branch):
            if stash_ref:
                restore_stash(threads_repo, stash_ref)
            return (False, f"Failed to checkout {main_branch}")
        actions.append(f"Checked out {main_branch}")

        # 3. Pull latest main
        if pull_ff_only(threads_repo, main_branch):
            actions.append(f"Pulled latest {main_branch}")
        elif pull_rebase(threads_repo, main_branch):
            actions.append(f"Pulled {main_branch} (rebase)")
        else:
            log_debug("[PARITY] auto_merge_to_main: pull main failed, proceeding anyway")

        # 4. Merge the feature branch into main
        try:
            threads_repo.git.merge(feature_branch, "--no-edit")
            actions.append(f"Merged {feature_branch} into {main_branch}")
        except GitCommandError as e:
            if has_conflicts(threads_repo):
                # Try auto-resolve for thread-only or graph-only conflicts
                if has_thread_conflicts_only(threads_repo):
                    try:
                        # Accept ours strategy for thread conflicts (append-only semantics)
                        threads_repo.git.checkout("--ours", ".")
                        threads_repo.git.add("-A")
                        threads_repo.git.commit("--no-edit")
                        actions.append("Auto-resolved thread conflicts during merge")
                    except Exception:
                        try:
                            threads_repo.git.merge("--abort")
                        except Exception:
                            pass
                        if original_branch:
                            checkout_branch(threads_repo, original_branch)
                        if stash_ref:
                            restore_stash(threads_repo, stash_ref)
                        return (False, f"Merge conflict could not be auto-resolved: {e}")
                elif has_graph_conflicts_only(threads_repo):
                    try:
                        # Accept theirs for graph (regeneratable)
                        threads_repo.git.checkout("--theirs", ".")
                        threads_repo.git.add("-A")
                        threads_repo.git.commit("--no-edit")
                        actions.append("Auto-resolved graph conflicts during merge")
                    except Exception:
                        try:
                            threads_repo.git.merge("--abort")
                        except Exception:
                            pass
                        if original_branch:
                            checkout_branch(threads_repo, original_branch)
                        if stash_ref:
                            restore_stash(threads_repo, stash_ref)
                        return (False, f"Graph conflict could not be auto-resolved: {e}")
                else:
                    # Mixed conflicts - abort
                    try:
                        threads_repo.git.merge("--abort")
                    except Exception:
                        pass
                    if original_branch:
                        checkout_branch(threads_repo, original_branch)
                    if stash_ref:
                        restore_stash(threads_repo, stash_ref)
                    return (False, f"Merge conflict requiring manual resolution: {e}")
            else:
                if stash_ref:
                    restore_stash(threads_repo, stash_ref)
                return (False, f"Merge failed: {e}")

        # 5. Push main to origin
        if push_with_retry(threads_repo, main_branch):
            actions.append(f"Pushed {main_branch} to origin")
        else:
            if stash_ref:
                restore_stash(threads_repo, stash_ref)
            return (False, f"Merge succeeded locally but push failed. Run: git push origin {main_branch}")

        # 6. Restore stash if we created one
        if stash_ref:
            if restore_stash(threads_repo, stash_ref):
                actions.append("Restored stashed changes")
            else:
                actions.append("Warning: stash restore failed, run 'git stash pop' manually")

        message = f"Auto-merged threads branch to main: {'; '.join(actions)}"
        log_debug(f"[PARITY] {message}")
        return (True, message)

    except Exception as e:
        log_debug(f"[PARITY] auto_merge_to_main failed: {e}")
        if original_branch:
            try:
                checkout_branch(threads_repo, original_branch)
            except Exception:
                pass
        if stash_ref:
            try:
                restore_stash(threads_repo, stash_ref)
            except Exception:
                pass
        return (False, f"Auto-merge failed: {e}")


def _detect_squash_merge(code_repo_obj: Repo, branch: str) -> Tuple[bool, Optional[str]]:
    """Detect if a code branch was squash-merged to main.

    Args:
        code_repo_obj: GitPython Repo object for code repository
        branch: Branch name to check

    Returns:
        Tuple of (is_squash_merged, squash_commit_sha)
        Returns (False, None) if branch doesn't exist or wasn't merged
    """
    try:
        if branch not in [b.name for b in code_repo_obj.heads]:
            return (False, None)

        if "main" not in [b.name for b in code_repo_obj.heads]:
            return (False, None)

        # Get commits on feature branch that aren't on main
        feature_commits = list(code_repo_obj.iter_commits(f"main..{branch}"))
        if not feature_commits:
            return (False, None)

        # Check if any of these commits exist on main (by message or content)
        main_commits = list(code_repo_obj.iter_commits("main", max_count=50))
        main_commit_shas = {c.hexsha for c in main_commits}
        main_commit_messages = {c.message.strip() for c in main_commits}

        # Check if feature branch commits exist on main
        feature_commits_on_main = 0
        for commit in feature_commits[:10]:
            if commit.hexsha in main_commit_shas:
                feature_commits_on_main += 1
            elif commit.message.strip() in main_commit_messages:
                feature_commits_on_main += 1

        # If no feature commits exist on main, likely squash merge
        if feature_commits_on_main == 0 and len(feature_commits) > 1:
            for commit in main_commits[:20]:
                if branch.lower() in commit.message.lower() or "squash" in commit.message.lower():
                    return (True, commit.hexsha[:7])
            return (True, None)

        return (False, None)
    except Exception:
        return (False, None)
