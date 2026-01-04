"""Unified state management for sync operations.

This module provides:
- ParityStatus enum: All possible parity states
- ParityState dataclass: State persisted to branch_parity_state.json
- StateManager class: Unified read/write with live status checks

Key principle: get_live_status() always performs fresh git checks,
never relying on potentially stale cached state.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from git import Repo
from git.exc import InvalidGitRepositoryError

from ..observability import log_debug
from .primitives import (
    get_branch_name,
    get_ahead_behind,
    is_rebase_in_progress,
    has_conflicts,
)


# =============================================================================
# Constants
# =============================================================================

STATE_FILE_NAME = "branch_parity_state.json"
STATE_FILE_VERSION = 1  # For future migrations
# Prefer storing parity state under .watercooler/state (ignored/local), fallback to legacy root file for compatibility.
STATE_DIR = ".watercooler/state"


def _default_state_path(threads_dir: Path) -> Path:
    """Preferred location for parity state (ignored/local)."""
    return Path(threads_dir) / STATE_DIR / STATE_FILE_NAME


# =============================================================================
# Enums
# =============================================================================


class ParityStatus(str, Enum):
    """Branch parity status values.

    These represent the current state of branch parity between code and threads repos.

    Blocking states (require human intervention):
    - CODE_BEHIND_ORIGIN: User must pull code repo
    - DETACHED_HEAD: Resolve detached HEAD state
    - REBASE_IN_PROGRESS: Complete or abort rebase/merge
    - DIVERGED: Manual merge/rebase needed
    - NEEDS_MANUAL_RECOVER: Force-push detected, history corrupted
    - ERROR: Unexpected error during check

    Auto-fixable states:
    - BRANCH_MISMATCH: Auto-checkout to match code branch
    - PENDING_PUSH: Auto-push on next write
    - MAIN_PROTECTION: Block write, suggest feature branch
    - REMOTE_UNREACHABLE: Retry with backoff
    - ORPHAN_BRANCH: Threads branch exists but code branch was deleted
                     (auto-merge to main and delete orphan)

    Clean state:
    - CLEAN: Ready for operations
    """

    CLEAN = "clean"
    PENDING_PUSH = "pending_push"
    BRANCH_MISMATCH = "branch_mismatch"
    MAIN_PROTECTION = "main_protection"
    CODE_BEHIND_ORIGIN = "code_behind_origin"
    REMOTE_UNREACHABLE = "remote_unreachable"
    REBASE_IN_PROGRESS = "rebase_in_progress"
    DETACHED_HEAD = "detached_head"
    DIVERGED = "diverged"
    NEEDS_MANUAL_RECOVER = "needs_manual_recover"
    ORPHAN_BRANCH = "orphan_branch"
    ERROR = "error"

    @classmethod
    def is_blocking(cls, status: "ParityStatus") -> bool:
        """Check if status blocks all operations (requires human intervention)."""
        blocking = {
            cls.CODE_BEHIND_ORIGIN,
            cls.DETACHED_HEAD,
            cls.REBASE_IN_PROGRESS,
            cls.DIVERGED,
            cls.NEEDS_MANUAL_RECOVER,
            cls.ERROR,
        }
        return status in blocking

    @classmethod
    def is_auto_fixable(cls, status: "ParityStatus") -> bool:
        """Check if status can be auto-fixed."""
        auto_fixable = {
            cls.BRANCH_MISMATCH,
            cls.PENDING_PUSH,
            cls.REMOTE_UNREACHABLE,
            cls.ORPHAN_BRANCH,
        }
        return status in auto_fixable


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ParityError:
    """Represents an error in parity operations.

    Attributes:
        state_class: The StateClass value (e.g., 'detached_head', 'behind_dirty')
        message: Human-readable error message
        requires_human: Whether human intervention is required
        suggested_commands: List of git commands to run for recovery
        recovery_refs: Dict of refs needed for recovery (e.g., stash refs)
    """

    state_class: str
    message: str
    requires_human: bool = False
    suggested_commands: List[str] = field(default_factory=list)
    recovery_refs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParityState:
    """Parity state persisted to branch_parity_state.json.

    This represents the cached state from the last preflight check.
    Note: Use StateManager.get_live_status() for current state,
    as this cached state may be stale after external git operations.
    """

    status: str = ParityStatus.CLEAN.value
    last_check_at: str = ""
    code_branch: Optional[str] = None
    threads_branch: Optional[str] = None
    actions_taken: List[str] = field(default_factory=list)
    pending_push: bool = False
    last_error: Optional[str] = None
    code_ahead_origin: int = 0
    code_behind_origin: int = 0
    threads_ahead_origin: int = 0
    threads_behind_origin: int = 0
    # Version field for future migrations
    version: int = STATE_FILE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParityState":
        """Create from dictionary, handling missing fields gracefully."""
        return cls(
            status=data.get("status", ParityStatus.CLEAN.value),
            last_check_at=data.get("last_check_at", ""),
            code_branch=data.get("code_branch"),
            threads_branch=data.get("threads_branch"),
            actions_taken=data.get("actions_taken", []),
            pending_push=data.get("pending_push", False),
            last_error=data.get("last_error"),
            code_ahead_origin=data.get("code_ahead_origin", 0),
            code_behind_origin=data.get("code_behind_origin", 0),
            threads_ahead_origin=data.get("threads_ahead_origin", 0),
            threads_behind_origin=data.get("threads_behind_origin", 0),
            version=data.get("version", 1),
        )


# =============================================================================
# State Manager
# =============================================================================


class StateManager:
    """Unified state management with live git checks.

    This class provides:
    - read(): Read cached state from file
    - write(): Write state to file atomically
    - get_live_status(): Get current status with LIVE git checks
    - invalidate(): Mark state as needing refresh

    Usage:
        manager = StateManager(threads_dir, code_repo_path)
        live_status = manager.get_live_status()  # Always fresh
        cached_state = manager.read()  # May be stale
    """

    def __init__(
        self,
        threads_dir: Path,
        code_repo_path: Optional[Path] = None,
    ):
        """Initialize state manager.

        Args:
            threads_dir: Path to threads repository directory
            code_repo_path: Optional path to code repository (for live status)
        """
        self.threads_dir = Path(threads_dir)
        self.code_repo_path = Path(code_repo_path) if code_repo_path else None
        # Prefer the ignored/local path; keep legacy path for backward compatibility reads.
        self._state_file = _default_state_path(self.threads_dir)
        self._legacy_state_file = self.threads_dir / STATE_FILE_NAME
        self._cached_state: Optional[ParityState] = None
        self._cache_valid = False

    def read(self, use_cache: bool = True) -> ParityState:
        """Read parity state from file.

        Args:
            use_cache: If True, return cached state if available

        Returns:
            ParityState from file, or default clean state if not found/corrupted

        Note:
            If the state file is corrupted (malformed JSON or invalid structure),
            logs a warning and returns a clean state.
        """
        if use_cache and self._cache_valid and self._cached_state is not None:
            return self._cached_state

        state = self._read_from_file()
        self._cached_state = state
        self._cache_valid = True
        return state

    def _read_from_file(self) -> ParityState:
        """Read state directly from file, bypassing cache."""
        # Try preferred then legacy path for compatibility; migrate forward on next write.
        for candidate in (self._state_file, self._legacy_state_file):
            try:
                if candidate.exists():
                    content = candidate.read_text(encoding="utf-8")
                    data = json.loads(content)

                    # Handle version migration if needed
                    file_version = data.get("version", 1)
                    if file_version < STATE_FILE_VERSION:
                        data = self._migrate_state(data, file_version)

                    # If we read from legacy, keep writing to preferred path going forward.
                    if candidate != self._state_file:
                        self._state_file = _default_state_path(self.threads_dir)
                    return ParityState.from_dict(data)
            except json.JSONDecodeError as e:
                log_debug(
                    f"[STATE] WARNING: Corrupted state file at {candidate}, "
                    f"resetting to clean state. JSON error: {e}"
                )
            except (KeyError, TypeError) as e:
                log_debug(
                    f"[STATE] WARNING: Invalid state file structure at {candidate}, "
                    f"resetting to clean state. Structure error: {e}"
                )
            except Exception as e:
                log_debug(f"[STATE] Failed to read state file {candidate}: {e}")

        return ParityState()

    def write(self, state: ParityState) -> bool:
        """Write parity state to file atomically.

        Uses temp file + rename for atomicity to prevent corruption
        on crash or power failure.

        Args:
            state: ParityState to write

        Returns:
            True on success, False on failure
        """
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)

            # Ensure version is set
            state.version = STATE_FILE_VERSION

            # Write to temp file then rename for atomicity
            fd, temp_path = tempfile.mkstemp(
                dir=self._state_file.parent,
                prefix=".parity_state_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(state.to_dict(), f, indent=2)
                os.replace(temp_path, self._state_file)

                # Update cache
                self._cached_state = state
                self._cache_valid = True
                return True
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                raise
        except Exception as e:
            log_debug(f"[STATE] Failed to write state file: {e}")
            return False

    def invalidate(self) -> None:
        """Mark cached state as invalid, forcing refresh on next read."""
        self._cache_valid = False
        self._cached_state = None

    def get_live_status(self) -> Dict[str, Any]:
        """Get current branch health with LIVE git checks.

        This method ALWAYS performs fresh git operations to get current state,
        rather than relying on potentially stale cached state.

        Returns:
            Dict with live status information:
            - status: Current parity status (computed LIVE)
            - code_branch, threads_branch: Branch names (LIVE)
            - code_ahead/behind, threads_ahead/behind: Commit counts (LIVE)
            - pending_push: Whether threads has unpushed commits
            - last_check_at: Timestamp of this check (fresh)
            - lock_holder: PID of current lock holder (if any)
        """
        # Read cached state for metadata only
        state = self.read(use_cache=False)

        # Initialize with fallback values from state
        code_branch = state.code_branch
        threads_branch = state.threads_branch
        code_ahead, code_behind = state.code_ahead_origin, state.code_behind_origin
        threads_ahead, threads_behind = (
            state.threads_ahead_origin,
            state.threads_behind_origin,
        )
        live_status = state.status

        # Perform LIVE git checks
        if self.code_repo_path:
            try:
                code_repo = Repo(self.code_repo_path, search_parent_directories=True)
                threads_repo = Repo(self.threads_dir, search_parent_directories=True)

                # Live branch names
                code_branch = get_branch_name(code_repo)
                threads_branch = get_branch_name(threads_repo)

                # Check for detached HEAD
                if code_branch is None or threads_branch is None:
                    live_status = ParityStatus.DETACHED_HEAD.value
                else:
                    # Check for rebase in progress
                    if is_rebase_in_progress(code_repo) or is_rebase_in_progress(
                        threads_repo
                    ):
                        live_status = ParityStatus.REBASE_IN_PROGRESS.value
                    elif has_conflicts(threads_repo):
                        live_status = ParityStatus.DIVERGED.value
                    else:
                        # Fetch for accurate ahead/behind (non-blocking on failure)
                        try:
                            code_repo.remotes.origin.fetch(prune=True)
                        except Exception as e:
                            log_debug(
                                f"[STATE] Fetch failed for code repo (non-fatal): {e}"
                            )

                        try:
                            threads_repo.remotes.origin.fetch(prune=True)
                        except Exception as e:
                            log_debug(
                                f"[STATE] Fetch failed for threads repo (non-fatal): {e}"
                            )

                        # Live ahead/behind counts
                        code_ahead, code_behind = get_ahead_behind(
                            code_repo, code_branch
                        )
                        threads_ahead, threads_behind = get_ahead_behind(
                            threads_repo, threads_branch
                        )

                        # Recompute status based on live data
                        if code_behind > 0:
                            live_status = ParityStatus.CODE_BEHIND_ORIGIN.value
                        elif code_branch != threads_branch:
                            live_status = ParityStatus.BRANCH_MISMATCH.value
                        elif threads_ahead > 0:
                            live_status = ParityStatus.PENDING_PUSH.value
                        else:
                            live_status = ParityStatus.CLEAN.value

            except InvalidGitRepositoryError as e:
                log_debug(f"[STATE] Invalid git repository: {e}")
                # Fall back to cached state
            except Exception as e:
                log_debug(f"[STATE] Error in get_live_status: {e}")
                # Fall back to cached state

        # Check for active lock
        lock_holder = self._get_active_lock_holder()

        return {
            "status": live_status,
            "code_branch": code_branch,
            "threads_branch": threads_branch,
            "code_ahead_origin": code_ahead,
            "code_behind_origin": code_behind,
            "threads_ahead_origin": threads_ahead,
            "threads_behind_origin": threads_behind,
            "pending_push": state.pending_push,
            "last_check_at": datetime.now(timezone.utc).isoformat(),
            "last_error": state.last_error,
            "actions_taken": state.actions_taken,
            "lock_holder": lock_holder,
        }

    def _get_active_lock_holder(self) -> Optional[str]:
        """Check for active lock and return holder PID if any."""
        lock_dir = self.threads_dir / ".locks"
        if not lock_dir.exists():
            return None

        for lock_file in lock_dir.glob("*.lock"):
            try:
                content = lock_file.read_text(encoding="utf-8")
                if content.startswith("pid="):
                    return content.split()[0].split("=")[1]
            except Exception:
                pass
        return None

    def _migrate_state(
        self, data: Dict[str, Any], from_version: int
    ) -> Dict[str, Any]:
        """Migrate state from older version to current.

        Args:
            data: State data from file
            from_version: Version of the state file

        Returns:
            Migrated state data
        """
        # Currently no migrations needed (version 1 is current)
        # Future migrations would go here:
        # if from_version < 2:
        #     data = self._migrate_v1_to_v2(data)
        # if from_version < 3:
        #     data = self._migrate_v2_to_v3(data)

        log_debug(f"[STATE] Migrated state from version {from_version} to {STATE_FILE_VERSION}")
        data["version"] = STATE_FILE_VERSION
        return data


# =============================================================================
# Convenience Functions (for backward compatibility)
# =============================================================================


def read_parity_state(threads_dir: Path) -> ParityState:
    """Read parity state from file.

    This is a convenience function for backward compatibility.
    For new code, prefer using StateManager directly.
    """
    manager = StateManager(threads_dir)
    return manager.read(use_cache=False)


def write_parity_state(threads_dir: Path, state: ParityState) -> bool:
    """Write parity state to file.

    This is a convenience function for backward compatibility.
    For new code, prefer using StateManager directly.
    """
    manager = StateManager(threads_dir)
    return manager.write(state)


def get_state_file_path(threads_dir: Path) -> Path:
    """Get path to parity state file (preferred ignored path)."""
    preferred = _default_state_path(threads_dir)
    legacy = threads_dir / STATE_FILE_NAME
    return preferred if preferred.exists() or not legacy.exists() else legacy
