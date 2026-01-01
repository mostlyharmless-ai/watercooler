"""Branch parity state machine with auto-remediation.

.. deprecated:: 0.5.0
   This module is deprecated and will be removed in a future version.
   Use the new modular sync package instead:

   - For status enums: ``from watercooler_mcp.sync import ParityStatus, StateClass``
   - For state: ``from watercooler_mcp.sync import ParityState, StateManager``
   - For results: ``from watercooler_mcp.sync import PreflightResult, BranchPairingResult``
   - For manager: ``from watercooler_mcp.sync import BranchParityManager``
   - For errors: ``from watercooler_mcp.sync import BranchPairingError``

This module is preserved as a facade for backward compatibility.
New code should import from watercooler_mcp.sync directly.

Legacy documentation:
This module provides preflight validation and auto-remediation for branch
parity between code and threads repositories. It enforces:

1. Branch name parity: code.active_branch == threads.active_branch
2. Remote push parity: if code branch on origin, threads branch must be too
3. Main protection: no writes when threads=main but code=feature
4. History coherence: threads has all commits that main has (if code does)

Key principle: neutral origin - no force-push, no history rewrites, no auto-merge to main.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import git
from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError

from watercooler.lock import AdvisoryLock

from .observability import log_debug


class ParityStatus(str, Enum):
    """Branch parity status values."""

    CLEAN = "clean"  # All checks pass, ready for operations
    PENDING_PUSH = "pending_push"  # Threads has unpushed commits
    BRANCH_MISMATCH = "branch_mismatch"  # Branch names don't match
    MAIN_PROTECTION = "main_protection"  # Threads on main, code on feature
    CODE_BEHIND_ORIGIN = "code_behind_origin"  # Code needs pull (can't auto-fix)
    REMOTE_UNREACHABLE = "remote_unreachable"  # Can't reach origin
    REBASE_IN_PROGRESS = "rebase_in_progress"  # Rebase/merge in progress
    DETACHED_HEAD = "detached_head"  # One or both repos in detached HEAD
    DIVERGED = "diverged"  # Branches have diverged, needs manual fix
    NEEDS_MANUAL_RECOVER = "needs_manual_recover"  # Force-push detected
    ORPHAN_BRANCH = "orphan_branch"  # Threads branch exists without code branch
    ERROR = "error"  # Unexpected error during check


class StateClass(str, Enum):
    """Detailed state classification for deterministic remediation.

    Maps the 3 orthogonal dimensions (Branch Alignment, Origin Sync, Working Tree)
    to actionable state classes. See plan entry 01KCZGH3BDZPACW5YC746R6S54.
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


@dataclass
class ParityError:
    """Structured error for blocking states with recovery guidance."""

    state_class: str
    message: str
    requires_human: bool
    suggested_commands: List[str] = field(default_factory=list)
    recovery_refs: dict = field(default_factory=dict)  # e.g., {"stash": "abc123"}


@dataclass
class ParityState:
    """Parity state persisted to branch_parity_state.json."""

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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ParityState":
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
        )


@dataclass
class PreflightResult:
    """Result of preflight check."""

    success: bool
    state: ParityState
    can_proceed: bool  # Whether operation can proceed
    blocking_reason: Optional[str] = None  # Human-readable reason if blocked
    auto_fixed: bool = False  # Whether auto-remediation was applied


# State file and lock paths
STATE_FILE_NAME = "branch_parity_state.json"
LOCKS_DIR_NAME = ".wc-locks"

# Lock configuration constants
LOCK_TIMEOUT_SECONDS = 30  # How long to wait for lock acquisition
LOCK_TTL_SECONDS = 60  # How long before a lock is considered stale
LOCK_QUICK_RETRIES = 3  # Quick retries before full timeout (handles transient contention)
LOCK_QUICK_RETRY_DELAY = 0.1  # Delay between quick retries in seconds

# Push retry configuration
MAX_PUSH_RETRIES = 3  # Number of rebase+retry attempts for push operations

# Topic name constraints
MAX_TOPIC_LENGTH = 200  # Maximum length for sanitized topic names
# Characters that are invalid in filenames on Windows or could cause issues
UNSAFE_TOPIC_CHARS_PATTERN = r'[<>:"/\\|?*\x00-\x1f]'

# Branch name constraints (git refname rules)
MAX_BRANCH_LENGTH = 255  # Git branch name length limit
# Pattern for invalid branch name characters/sequences per git-check-ref-format
# Each tuple is (compiled_pattern, raw_pattern, human_readable_message)
_BRANCH_VALIDATION_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r'\.\.+'), r'\.\.+', 'contains consecutive dots (..)'),
    (re.compile(r'^-'), r'^-', 'starts with hyphen (potential flag injection)'),
    (re.compile(r'-$'), r'-$', 'ends with hyphen'),
    (re.compile(r'^\.|\.$'), r'^\.|\.$', 'starts or ends with dot'),
    (re.compile(r'\.lock$'), r'\.lock$', 'ends with .lock (reserved suffix)'),
    (re.compile(r'@\{'), r'@\{', 'contains reflog syntax (@{)'),
    (re.compile(r'[\x00-\x1f\x7f]'), r'[\x00-\x1f\x7f]', 'contains control characters'),
    (re.compile(r'[~^:?*\[\]\\]'), r'[~^:?*\[\]\\]', 'contains invalid git characters (~^:?*[]\\)'),
    (re.compile(r'\s'), r'\s', 'contains whitespace'),
]
# Keep raw patterns for backwards compatibility in tests
INVALID_BRANCH_PATTERNS = [rule[1] for rule in _BRANCH_VALIDATION_RULES]


def _sanitize_topic_for_filename(topic: str) -> str:
    """Sanitize topic name for safe use as a filename.

    Security considerations:
    - Prevents path traversal (../../etc/passwd)
    - Removes characters invalid on Windows (<>:"/\\|?*)
    - Removes control characters
    - Handles collisions via hash suffix for long names
    - Normalizes slashes to underscores
    - Normalizes Unicode to NFC form for consistent handling

    Args:
        topic: Raw topic name (may contain unsafe characters)

    Returns:
        Safe filename string (without extension)
    """
    if not topic:
        return "_empty_"

    # Step 0: Normalize Unicode to NFC (composed form) for consistent handling
    # This ensures that accented characters like "café" are always stored the same way
    # regardless of how the input was encoded (é vs e + combining acute accent)
    safe = unicodedata.normalize("NFC", topic)

    # Step 1: Normalize path separators to underscores
    safe = safe.replace("/", "_").replace("\\", "_")

    # Step 2: Remove path traversal attempts (.. sequences)
    safe = re.sub(r"\.\.+", "_", safe)

    # Step 3: Remove unsafe characters for cross-platform compatibility
    safe = re.sub(UNSAFE_TOPIC_CHARS_PATTERN, "_", safe)

    # Step 4: Collapse multiple underscores
    safe = re.sub(r"_+", "_", safe)

    # Step 5: Strip leading/trailing underscores and dots (Windows reserved)
    safe = safe.strip("_.")

    # Step 6: Handle empty result after sanitization
    if not safe:
        safe = "_sanitized_"

    # Step 7: Handle length limits with hash suffix for uniqueness
    if len(safe) > MAX_TOPIC_LENGTH:
        # Use first 180 chars + hash of full topic for uniqueness
        topic_hash = hashlib.sha256(topic.encode()).hexdigest()[:16]
        safe = f"{safe[:180]}_{topic_hash}"

    return safe


def _validate_branch_name(branch: str) -> None:
    """Validate branch name to prevent injection and ensure git compatibility.

    Security considerations:
    - Prevents flag injection (branch names starting with -)
    - Enforces git-check-ref-format rules
    - Prevents control character injection
    - Limits length to prevent filesystem issues

    Uses pre-compiled regex patterns for performance and provides
    human-readable error messages.

    Args:
        branch: Branch name to validate

    Raises:
        ValueError: If branch name is invalid or potentially dangerous
    """
    if not branch:
        raise ValueError("Branch name cannot be empty")

    if len(branch) > MAX_BRANCH_LENGTH:
        raise ValueError(
            f"Branch name too long: {len(branch)} chars (max {MAX_BRANCH_LENGTH})"
        )

    # Check against all invalid patterns using pre-compiled regexes
    for compiled_pattern, _raw_pattern, message in _BRANCH_VALIDATION_RULES:
        if compiled_pattern.search(branch):
            raise ValueError(f"Branch name '{branch}' {message}")

    # Additional safety: no consecutive slashes (path component issues)
    if "//" in branch:
        raise ValueError(f"Branch name '{branch}' contains consecutive slashes")

    # No trailing slash
    if branch.endswith("/"):
        raise ValueError(f"Branch name '{branch}' cannot end with slash")


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _state_file_path(threads_dir: Path) -> Path:
    """Get path to parity state file."""
    return threads_dir / STATE_FILE_NAME


def _lock_dir(threads_dir: Path) -> Path:
    """Get path to locks directory."""
    return threads_dir / LOCKS_DIR_NAME


def _topic_lock_path(threads_dir: Path, topic: str) -> Path:
    """Get path to per-topic lock file.

    Uses robust sanitization to prevent path traversal and handle
    special characters that are invalid on various filesystems.
    """
    lock_dir = _lock_dir(threads_dir)
    safe_topic = _sanitize_topic_for_filename(topic)
    return lock_dir / f"{safe_topic}.lock"


def read_parity_state(threads_dir: Path) -> ParityState:
    """Read parity state from file, return empty state if not found.

    Note:
        If the state file is corrupted (malformed JSON or invalid structure),
        this function logs a warning and returns a clean state. The corrupted
        content is not automatically backed up to avoid accumulating files,
        but the warning message includes the error details for debugging.
    """
    state_file = _state_file_path(threads_dir)
    try:
        if state_file.exists():
            content = state_file.read_text(encoding="utf-8")
            data = json.loads(content)
            return ParityState.from_dict(data)
    except json.JSONDecodeError as e:
        # Corrupted JSON - log warning with details for debugging
        log_debug(
            f"[PARITY] WARNING: Corrupted state file at {state_file}, resetting to clean state. "
            f"JSON error: {e}"
        )
    except (KeyError, TypeError) as e:
        # Invalid structure - missing required fields or wrong types
        log_debug(
            f"[PARITY] WARNING: Invalid state file structure at {state_file}, resetting to clean state. "
            f"Structure error: {e}"
        )
    except Exception as e:
        # Other errors (permissions, IO, etc.)
        log_debug(f"[PARITY] Failed to read state file: {e}")
    return ParityState()


def write_parity_state(threads_dir: Path, state: ParityState) -> bool:
    """Write parity state to file atomically (temp + rename)."""
    state_file = _state_file_path(threads_dir)
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        # Write to temp file then rename for atomicity
        fd, temp_path = tempfile.mkstemp(
            dir=state_file.parent, prefix=".parity_state_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, indent=2)
            os.replace(temp_path, state_file)
            return True
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(temp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        log_debug(f"[PARITY] Failed to write state file: {e}")
        return False


def acquire_topic_lock(
    threads_dir: Path, topic: str, timeout: int = LOCK_TIMEOUT_SECONDS
) -> AdvisoryLock:
    """Acquire lock for a specific topic. Returns lock (caller must release).

    Args:
        threads_dir: Path to threads repository
        topic: Topic name to lock (will be sanitized for filename safety)
        timeout: Seconds to wait for lock acquisition (default: LOCK_TIMEOUT_SECONDS)

    Returns:
        AdvisoryLock instance (caller must call release() or use as context manager)

    Raises:
        TimeoutError: If lock cannot be acquired within timeout period

    Note:
        The lock has a TTL of LOCK_TTL_SECONDS (60s). If a process crashes while
        holding the lock, it will be automatically cleaned up after the TTL expires.
        This ensures stale locks from crashed processes don't block agents indefinitely.

        For transient contention (two agents trying to write simultaneously), the
        function first attempts LOCK_QUICK_RETRIES quick retries with zero timeout
        before falling back to the full timeout. This handles the common case where
        one agent just needs to wait a fraction of a second.
    """
    lock_path = _topic_lock_path(threads_dir, topic)
    # Ensure locks directory exists
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Quick retries for transient contention (two agents hitting simultaneously)
    # This avoids the full 30s timeout in the common case where the lock is
    # held for only a few hundred milliseconds
    for attempt in range(LOCK_QUICK_RETRIES):
        lock = AdvisoryLock(lock_path, ttl=LOCK_TTL_SECONDS, timeout=0)
        if lock.acquire():
            return lock
        # Brief delay before retry (100ms default)
        time.sleep(LOCK_QUICK_RETRY_DELAY)

    # Quick retries failed, fall back to full timeout
    lock = AdvisoryLock(lock_path, ttl=LOCK_TTL_SECONDS, timeout=timeout)
    if not lock.acquire():
        # Get lock holder info for better error message
        lock_info = lock.get_lock_info()
        if lock_info:
            holder_pid = lock_info.get("pid", "unknown")
            holder_time = lock_info.get("time", "unknown")
            holder_user = lock_info.get("user", "unknown")
            raise TimeoutError(
                f"Failed to acquire lock for topic '{topic}' within {timeout}s. "
                f"Lock held by: pid={holder_pid}, user={holder_user}, since={holder_time}. "
                f"If this lock is stale (holder crashed), it will auto-expire after TTL ({LOCK_TTL_SECONDS}s). "
                f"To force unlock: rm {lock_path}"
            )
        raise TimeoutError(
            f"Failed to acquire lock for topic '{topic}' within {timeout}s. "
            f"Lock file exists but metadata unavailable. To force unlock: rm {lock_path}"
        )
    return lock


def _get_branch_name(repo: Repo) -> Optional[str]:
    """Get active branch name, or None if detached HEAD."""
    try:
        if repo.head.is_detached:
            return None
        return repo.active_branch.name
    except Exception:
        return None


def _is_rebase_in_progress(repo: Repo) -> bool:
    """Check if a rebase or merge is in progress."""
    git_dir = Path(repo.git_dir)
    return (
        (git_dir / "rebase-merge").exists()
        or (git_dir / "rebase-apply").exists()
        or (git_dir / "MERGE_HEAD").exists()
    )


def _branch_exists_on_origin(repo: Repo, branch: str) -> bool:
    """Check if branch exists on origin.

    Note:
        Validates branch name before constructing git ref strings for defense-in-depth.
    """
    try:
        _validate_branch_name(branch)
    except ValueError as e:
        log_debug(f"[PARITY] Invalid branch name in _branch_exists_on_origin: {e}")
        return False

    try:
        origin = repo.remote("origin")
        return f"origin/{branch}" in [ref.name for ref in origin.refs]
    except Exception:
        return False


def _get_ahead_behind(repo: Repo, branch: str) -> tuple[int, int]:
    """Get commits ahead/behind origin for a branch. Returns (ahead, behind).

    Note:
        Validates branch name before constructing git ref strings for defense-in-depth.
    """
    try:
        _validate_branch_name(branch)
    except ValueError as e:
        log_debug(f"[PARITY] Invalid branch name in _get_ahead_behind: {e}")
        return (0, 0)

    try:
        remote_ref = f"origin/{branch}"
        # Check if remote ref exists
        try:
            repo.commit(remote_ref)
        except Exception:
            return (0, 0)  # No remote tracking

        ahead = len(list(repo.iter_commits(f"{remote_ref}..{branch}")))
        behind = len(list(repo.iter_commits(f"{branch}..{remote_ref}")))
        return (ahead, behind)
    except Exception as e:
        log_debug(f"[PARITY] Error getting ahead/behind: {e}")
        return (0, 0)


def _find_main_branch(repo: Repo) -> Optional[str]:
    """Find the main branch name (main or master)."""
    for name in ("main", "master"):
        try:
            repo.commit(name)
            return name
        except Exception:
            continue
    return None


def _fetch_with_timeout(repo: Repo, timeout: int = 30) -> bool:
    """Fetch from origin with timeout. Returns True on success."""
    try:
        # Use git command with timeout
        repo.git.fetch("origin", kill_after_timeout=timeout)
        return True
    except Exception as e:
        log_debug(f"[PARITY] Fetch failed: {e}")
        return False


def _checkout_branch(repo: Repo, branch: str, create: bool = False, set_upstream: bool = True) -> bool:
    """Checkout a branch, optionally creating it with upstream tracking.

    Args:
        repo: Git repository
        branch: Branch name (validated for safety)
        create: If True, create the branch with -b flag
        set_upstream: If True and remote branch exists, configure upstream tracking

    Returns:
        True on success, False on failure

    Note:
        Branch name is validated to prevent flag injection and ensure
        git compatibility before any git operations are performed.

        When create=True and origin/branch exists, the new branch will track the remote.
        When create=False, upstream tracking will be set if not already configured.
    """
    try:
        _validate_branch_name(branch)
        remote_ref = f"origin/{branch}"

        # Check if remote branch exists
        has_remote = False
        try:
            has_remote = remote_ref in [r.name for r in repo.remotes.origin.refs]
        except Exception:
            pass  # No remote or refs unavailable

        if create:
            if has_remote and set_upstream:
                # Create branch tracking the remote
                repo.git.checkout("-b", branch, "--track", remote_ref)
                log_debug(f"[PARITY] Created branch {branch} tracking {remote_ref}")
            else:
                repo.git.checkout("-b", branch)
                log_debug(f"[PARITY] Created branch {branch} (no remote to track)")
        else:
            repo.git.checkout(branch)
            # Set upstream if exists and not already set
            if set_upstream and has_remote:
                try:
                    tracking = repo.active_branch.tracking_branch()
                    if tracking is None:
                        repo.git.branch("--set-upstream-to", remote_ref)
                        log_debug(f"[PARITY] Set upstream tracking for {branch} -> {remote_ref}")
                except Exception as e:
                    log_debug(f"[PARITY] Could not set upstream (non-fatal): {e}")
        return True
    except ValueError as e:
        log_debug(f"[PARITY] Invalid branch name: {e}")
        return False
    except Exception as e:
        log_debug(f"[PARITY] Checkout failed: {e}")
        return False


def _create_and_push_branch(repo: Repo, branch: str) -> bool:
    """Create a branch and push to origin. Returns True on success.

    Args:
        repo: Git repository
        branch: Branch name (validated for safety)

    Returns:
        True on success, False on failure

    Note:
        Branch name is validated to prevent flag injection and ensure
        git compatibility before any git operations are performed.
    """
    try:
        _validate_branch_name(branch)
        repo.git.checkout("-b", branch)
        repo.git.push("-u", "origin", branch)
        return True
    except ValueError as e:
        log_debug(f"[PARITY] Invalid branch name: {e}")
        return False
    except Exception as e:
        log_debug(f"[PARITY] Create and push branch failed: {e}")
        return False


def _pull_ff_only(repo: Repo, branch: Optional[str] = None) -> bool:
    """Pull with --ff-only. Returns True on success.

    Args:
        repo: Git repository
        branch: Optional branch name. If provided, pulls from origin/branch explicitly.
                This allows pull to work even without upstream tracking configured.
    """
    try:
        if branch:
            # Note: --ff-only must come before origin/branch for git pull
            repo.git.pull("--ff-only", "origin", branch)
        else:
            repo.git.pull("--ff-only")
        return True
    except GitCommandError as e:
        log_debug(f"[PARITY] FF-only pull failed: {e}")
        return False


def _pull_rebase(repo: Repo, branch: Optional[str] = None) -> bool:
    """Pull with --rebase. Returns True on success.

    Args:
        repo: Git repository
        branch: Optional branch name. If provided, pulls from origin/branch explicitly.
                This allows pull to work even without upstream tracking configured.
    """
    try:
        if branch:
            # Note: --rebase must come before origin/branch for git pull
            repo.git.pull("--rebase", "origin", branch)
        else:
            repo.git.pull("--rebase")
        return True
    except GitCommandError as e:
        log_debug(f"[PARITY] Rebase pull failed: {e}")
        return False


def _detect_stash(repo: Repo) -> bool:
    """Check if repo has any stashed changes."""
    try:
        stash_list = repo.git.stash("list")
        return bool(stash_list.strip())
    except GitCommandError:
        return False


def _preserve_stash(repo: Repo, prefix: str = "watercooler-auto") -> Optional[str]:
    """Stash changes with timestamped message. Returns stash ref or None if nothing to stash.

    Data-safety invariant: Never drops stash on conflict. Stash ref preserved in error messages.
    """
    from datetime import datetime

    if not repo.is_dirty(untracked_files=True):
        return None

    stash_msg = f"{prefix}-{datetime.utcnow().isoformat()}"
    try:
        result = repo.git.stash("push", "-m", stash_msg, "--include-untracked")
        if "No local changes" in result:
            return None
        log_debug(f"[PARITY] Stashed changes: {stash_msg}")
        return stash_msg
    except GitCommandError as e:
        log_debug(f"[PARITY] Failed to stash: {e}")
        raise


def _restore_stash(repo: Repo, stash_ref: Optional[str] = None) -> bool:
    """Pop the most recent stash. Returns True on success.

    Data-safety invariant: On conflict, stash is preserved (not dropped).
    """
    if stash_ref is None:
        return True  # Nothing to restore

    try:
        repo.git.stash("pop")
        log_debug(f"[PARITY] Restored stash: {stash_ref}")
        return True
    except GitCommandError as e:
        # Stash pop failed (likely conflict) - stash is preserved
        log_debug(f"[PARITY] Stash pop failed (stash preserved): {e}")
        return False


def _has_conflicts(repo: Repo) -> bool:
    """Check if repo has unresolved merge/rebase conflicts."""
    try:
        status = repo.git.status("--porcelain")
        # Check for conflict markers (UU, AA, DD, etc.)
        for line in status.split("\n"):
            if line and len(line) >= 2:
                xy = line[:2]
                if "U" in xy or xy == "AA" or xy == "DD":
                    return True
        return False
    except GitCommandError:
        return False


def _has_graph_conflicts_only(repo: Repo) -> bool:
    """Check if all conflicts are confined to graph/baseline/ files.

    Returns True if:
    - There are conflicts (UU, AA, DD status markers)
    - ALL conflicted files are under graph/baseline/ directory

    Returns False if:
    - No conflicts exist
    - Any conflicts exist outside graph/baseline/
    """
    try:
        status = repo.git.status("--porcelain")
        conflicted_files = []

        for line in status.split("\n"):
            if line and len(line) >= 2:
                xy = line[:2]
                if "U" in xy or xy == "AA" or xy == "DD":
                    # Extract file path (skip XY status)
                    file_path = line[3:].strip()
                    conflicted_files.append(file_path)

        if not conflicted_files:
            return False

        # Check if ALL conflicts are in graph/baseline/
        return all(
            f.startswith("graph/baseline/")
            for f in conflicted_files
        )
    except GitCommandError:
        return False


def _has_thread_conflicts_only(repo: Repo) -> bool:
    """Check if all conflicts are in thread markdown files (not in graph/).

    Returns True if:
    - There are conflicts (UU, AA, DD status markers)
    - ALL conflicted files are .md files in root (not in graph/ or subdirs)

    Returns False if:
    - No conflicts exist
    - Any conflicts exist in graph/ or non-.md files
    """
    try:
        status = repo.git.status("--porcelain")
        conflicted_files = []

        for line in status.split("\n"):
            if line and len(line) >= 2:
                xy = line[:2]
                if "U" in xy or xy == "AA" or xy == "DD":
                    file_path = line[3:].strip()
                    conflicted_files.append(file_path)

        if not conflicted_files:
            return False

        # Check if ALL conflicts are .md files not in graph/
        return all(
            f.endswith(".md") and not f.startswith("graph/")
            for f in conflicted_files
        )
    except GitCommandError:
        return False


def merge_manifest_content(ours_content: str, theirs_content: str) -> str:
    """Pure function to merge manifest.json content.

    Merge strategy:
    - version: Take from ours
    - last_updated: Take max (newer timestamp)
    - topics_synced: Merge both dicts (theirs overwrites ours for same keys)
    - Other fields: Take from ours (generated_at, source_dir, etc.)

    Args:
        ours_content: JSON string of our version
        theirs_content: JSON string of their version

    Returns:
        Merged JSON string with pretty formatting
    """
    import json

    ours_json = json.loads(ours_content)
    theirs_json = json.loads(theirs_content)

    # Merge strategy: take base structure from ours, newer timestamp, merged topics
    merged = {
        **ours_json,  # Start with ours (includes all base fields)
        "last_updated": max(
            ours_json.get("last_updated", ""),
            theirs_json.get("last_updated", "")
        ),
        "topics_synced": {
            **ours_json.get("topics_synced", {}),
            **theirs_json.get("topics_synced", {})
        }
    }

    return json.dumps(merged, indent=2) + "\n"


def merge_jsonl_content(ours_content: str, theirs_content: str) -> str:
    """Pure function to merge JSONL content by deduplicating entries by UUID.

    Both nodes.jsonl and edges.jsonl are additive - entries from both
    sides can coexist. We deduplicate by UUID to handle any duplicates.

    Args:
        ours_content: JSONL string of our version
        theirs_content: JSONL string of their version

    Returns:
        Merged JSONL string with deduplicated entries
    """
    import json

    seen_uuids: set[str] = set()
    merged_lines = []

    # Process ours first, then theirs
    for content in [ours_content, theirs_content]:
        for line in content.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                uuid = entry.get("uuid") or entry.get("id")
                if uuid and uuid not in seen_uuids:
                    seen_uuids.add(uuid)
                    merged_lines.append(json.dumps(entry))
            except json.JSONDecodeError:
                continue

    return "\n".join(merged_lines) + "\n"


def merge_thread_content(ours_content: str, theirs_content: str) -> Tuple[str, bool]:
    """Pure function to merge two thread file versions at entry level.

    Entries are identified by their Entry-ID (ULID in HTML comment).
    This allows merging non-overlapping entries without conflict.

    Strategy:
    - Parse both versions into header + entries
    - Header: take theirs (metadata is more recent)
    - Entries identified by Entry-ID
    - Non-overlapping entries: merge (union of unique entries)
    - Same Entry-ID with different content: TRUE CONFLICT

    Args:
        ours_content: Our version of the thread file
        theirs_content: Their version of the thread file

    Returns:
        Tuple of (merged_content, had_true_conflicts)
        - merged_content: The merged thread file if successful, empty if conflicts
        - had_true_conflicts: True if same Entry-ID has different content
    """
    from watercooler.thread_entries import parse_thread_entries

    # Parse both versions
    ours_entries = parse_thread_entries(ours_content)
    theirs_entries = parse_thread_entries(theirs_content)

    # Build maps by Entry-ID
    ours_by_id: Dict[str, Any] = {}
    theirs_by_id: Dict[str, Any] = {}

    for entry in ours_entries:
        if entry.entry_id:
            ours_by_id[entry.entry_id] = entry

    for entry in theirs_entries:
        if entry.entry_id:
            theirs_by_id[entry.entry_id] = entry

    # Check for true conflicts (same ID, different content)
    # Compare all meaningful fields: body, title, entry_type, role
    for entry_id, ours_entry in ours_by_id.items():
        if entry_id in theirs_by_id:
            theirs_entry = theirs_by_id[entry_id]
            # Compare body content (normalized)
            if ours_entry.body.strip() != theirs_entry.body.strip():
                log_debug(f"[THREAD-MERGE] True conflict: Entry-ID {entry_id} has different body")
                return "", True
            # Compare title
            if (ours_entry.title or "").strip() != (theirs_entry.title or "").strip():
                log_debug(f"[THREAD-MERGE] True conflict: Entry-ID {entry_id} has different title")
                return "", True
            # Compare entry type
            if (ours_entry.entry_type or "").strip() != (theirs_entry.entry_type or "").strip():
                log_debug(f"[THREAD-MERGE] True conflict: Entry-ID {entry_id} has different entry_type")
                return "", True
            # Compare role
            if (ours_entry.role or "").strip() != (theirs_entry.role or "").strip():
                log_debug(f"[THREAD-MERGE] True conflict: Entry-ID {entry_id} has different role")
                return "", True

    # No true conflicts - merge entries
    # Use theirs' header (more recent metadata)
    header_end = _find_header_end(theirs_content)
    merged_header = theirs_content[:header_end] if header_end > 0 else ""

    # Collect all unique entries by ID
    all_entries: Dict[str, Any] = {}

    # Add ours first
    for entry in ours_entries:
        key = entry.entry_id or f"_no_id_{entry.index}"
        all_entries[key] = entry

    # Add theirs (overwriting duplicates is fine since content matches)
    for entry in theirs_entries:
        key = entry.entry_id or f"_no_id_{entry.index}"
        all_entries[key] = entry

    # Sort entries by timestamp
    def sort_key(entry: Any) -> Tuple[int, str]:
        has_timestamp = 0 if entry.timestamp else 1
        timestamp = entry.timestamp or ""
        return (has_timestamp, timestamp)

    sorted_entries = sorted(all_entries.values(), key=sort_key)

    # Reconstruct thread file
    lines = [merged_header] if merged_header else []

    for entry in sorted_entries:
        # Add separator
        lines.append("\n---\n")

        # Reconstruct entry header
        entry_line = f"Entry: {entry.agent}"
        if entry.timestamp:
            entry_line += f" {entry.timestamp}"
        lines.append(entry_line + "\n")

        if entry.role:
            lines.append(f"Role: {entry.role}\n")
        if entry.entry_type:
            lines.append(f"Type: {entry.entry_type}\n")
        if entry.title:
            lines.append(f"Title: {entry.title}\n")

        # Add body
        if entry.body:
            lines.append("\n" + entry.body.strip() + "\n")

    merged = "".join(lines)
    log_debug(f"[THREAD-MERGE] Successfully merged {len(sorted_entries)} entries")
    return merged, False


def _find_header_end(content: str) -> int:
    """Find the byte offset where the thread header ends (first ---)."""
    lines = content.split("\n")
    offset = 0
    for line in lines:
        if line.strip() == "---":
            return offset
        offset += len(line) + 1  # +1 for newline
    return 0


def _merge_manifest(file_path: Path) -> bool:
    """Merge manifest.json by taking newer timestamp and merging topics.

    Uses git show :2: and :3: to get clean ours/theirs versions.

    Returns True on success, False on failure.
    """
    try:
        from git import Repo
        repo = Repo(file_path.parent.parent.parent, search_parent_directories=False)

        # Get relative path from repo root
        repo_path = Path(repo.working_dir)
        rel_path = file_path.relative_to(repo_path)

        # Get ours (:2:) and theirs (:3:) versions
        try:
            ours_content = repo.git.show(f":2:{rel_path}")
            theirs_content = repo.git.show(f":3:{rel_path}")
        except GitCommandError as e:
            log_debug(f"[PARITY] Git error getting ours/theirs for {file_path.name}: {e}")
            return False

        # Use pure merge function
        merged_content = merge_manifest_content(ours_content, theirs_content)
        file_path.write_text(merged_content)
        log_debug(f"[PARITY] Auto-merged {file_path.name}: newer timestamp, merged topics")
        return True

    except GitCommandError as e:
        log_debug(f"[PARITY] Git error in manifest merge: {e}")
        return False
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log_debug(f"[PARITY] Parse error in manifest merge: {e}")
        return False
    except OSError as e:
        log_debug(f"[PARITY] IO error in manifest merge: {e}")
        return False


def _merge_jsonl(file_path: Path) -> bool:
    """Merge .jsonl files by deduplicating entries by UUID.

    Uses git show :2: and :3: to get clean ours/theirs versions.

    Returns True on success, False on failure.
    """
    try:
        from git import Repo
        repo = Repo(file_path.parent.parent.parent, search_parent_directories=False)

        # Get relative path from repo root
        repo_path = Path(repo.working_dir)
        rel_path = file_path.relative_to(repo_path)

        # Get ours (:2:) and theirs (:3:) versions
        try:
            ours_content = repo.git.show(f":2:{rel_path}")
            theirs_content = repo.git.show(f":3:{rel_path}")
        except GitCommandError as e:
            log_debug(f"[PARITY] Git error getting ours/theirs for {file_path.name}: {e}")
            return False

        # Use pure merge function
        merged_content = merge_jsonl_content(ours_content, theirs_content)
        file_path.write_text(merged_content)
        log_debug(f"[PARITY] Auto-merged {file_path.name}: deduplicated entries")
        return True

    except GitCommandError as e:
        log_debug(f"[PARITY] Git error in JSONL merge: {e}")
        return False
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log_debug(f"[PARITY] Parse error in JSONL merge: {e}")
        return False
    except OSError as e:
        log_debug(f"[PARITY] IO error in JSONL merge: {e}")
        return False


def _auto_resolve_graph_conflicts(repo: Repo) -> bool:
    """Auto-resolve conflicts in graph files using smart merge strategy.

    Returns True if all conflicts successfully resolved, False otherwise.

    This function should only be called when _has_graph_conflicts_only()
    returns True, ensuring all conflicts are in graph/baseline/ files.

    After resolving conflicts, completes the merge by committing.
    """
    try:
        repo_path = Path(repo.working_dir)

        # Get conflicted files
        status = repo.git.status("--porcelain")
        conflicted = []

        for line in status.split("\n"):
            if line and len(line) >= 2:
                xy = line[:2]
                if "U" in xy or xy == "AA" or xy == "DD":
                    file_rel = line[3:].strip()
                    conflicted.append(file_rel)

        if not conflicted:
            return True  # No conflicts to resolve

        # Resolve each conflicted file
        for file_rel in conflicted:
            file_path = repo_path / file_rel

            if not file_path.exists():
                log_debug(f"[PARITY] Conflicted file doesn't exist: {file_rel}")
                return False

            if file_path.name == "manifest.json":
                if not _merge_manifest(file_path):
                    return False
            elif file_path.suffix == ".jsonl":
                if not _merge_jsonl(file_path):
                    return False
            else:
                log_debug(f"[PARITY] Unknown graph file type: {file_path.name}")
                return False

            # Stage resolved file - must clear conflict stages first
            # During rebase, index has 3 stages (base/ours/theirs) that prevent normal add
            try:
                repo.git.rm("--cached", file_rel)
            except Exception:
                pass  # May fail if not in conflict state, that's OK
            repo.index.add([file_rel])
            log_debug(f"[PARITY] Staged resolved file: {file_rel}")

        # Complete the merge/rebase
        git_dir = Path(repo.git_dir)
        is_rebase = (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

        try:
            if is_rebase:
                # During rebase, continue instead of commit
                repo.git.rebase("--continue")
                log_debug("[PARITY] Continued rebase after resolving graph conflicts")
            else:
                # Regular merge conflict - use git.commit to properly finalize merge
                # repo.index.commit() doesn't remove MERGE_HEAD, but git commit does
                repo.git.commit("-m", "Auto-merge graph conflicts")
                log_debug("[PARITY] Committed merged graph files")
        except Exception as e:
            log_debug(f"[PARITY] Failed to complete merge/rebase: {e}")
            return False

        return True

    except Exception as e:
        log_debug(f"[PARITY] Failed to auto-resolve graph conflicts: {e}")
        return False


def _merge_thread(file_path: Path) -> bool:
    """Merge a thread markdown file by parsing entries and merging.

    Uses git show :2: and :3: to get clean ours/theirs versions.

    Returns True on success, False on failure or true conflict.
    """
    try:
        from git import Repo
        repo = Repo(file_path.parent, search_parent_directories=True)

        # Get relative path from repo root
        repo_path = Path(repo.working_dir)
        rel_path = file_path.relative_to(repo_path)

        # Get ours (:2:) and theirs (:3:) versions
        try:
            ours_content = repo.git.show(f":2:{rel_path}")
            theirs_content = repo.git.show(f":3:{rel_path}")
        except GitCommandError as e:
            log_debug(f"[PARITY] Git error getting ours/theirs for {file_path.name}: {e}")
            return False

        # Use pure merge function
        merged_content, had_conflicts = merge_thread_content(ours_content, theirs_content)

        if had_conflicts:
            log_debug(f"[PARITY] Thread {file_path.name} has true conflicts (same Entry-ID, different content)")
            return False

        file_path.write_text(merged_content)
        log_debug(f"[PARITY] Auto-merged thread {file_path.name}")
        return True

    except GitCommandError as e:
        log_debug(f"[PARITY] Git error in thread merge: {e}")
        return False
    except (ValueError, AttributeError) as e:
        log_debug(f"[PARITY] Parse error in thread merge: {e}")
        return False
    except OSError as e:
        log_debug(f"[PARITY] IO error in thread merge: {e}")
        return False


def _auto_resolve_thread_conflicts(repo: Repo) -> bool:
    """Auto-resolve conflicts in thread markdown files using entry-level merge.

    Returns True if all conflicts successfully resolved, False otherwise.

    This function should only be called when _has_thread_conflicts_only()
    returns True, ensuring all conflicts are in .md thread files.

    After resolving conflicts, completes the merge by committing.
    """
    try:
        repo_path = Path(repo.working_dir)

        # Get conflicted files
        status = repo.git.status("--porcelain")
        conflicted = []

        for line in status.split("\n"):
            if line and len(line) >= 2:
                xy = line[:2]
                if "U" in xy or xy == "AA" or xy == "DD":
                    file_rel = line[3:].strip()
                    conflicted.append(file_rel)

        if not conflicted:
            return True  # No conflicts to resolve

        # Resolve each conflicted file
        for file_rel in conflicted:
            file_path = repo_path / file_rel

            if not file_path.exists():
                log_debug(f"[PARITY] Conflicted file doesn't exist: {file_rel}")
                return False

            if not file_rel.endswith(".md"):
                log_debug(f"[PARITY] Non-.md file in thread conflicts: {file_rel}")
                return False

            if not _merge_thread(file_path):
                return False

            # Stage resolved file - must clear conflict stages first
            try:
                repo.git.rm("--cached", file_rel)
            except Exception:
                pass  # May fail if not in conflict state
            repo.index.add([file_rel])
            log_debug(f"[PARITY] Staged resolved thread file: {file_rel}")

        # Complete the merge/rebase
        git_dir = Path(repo.git_dir)
        is_rebase = (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

        try:
            if is_rebase:
                repo.git.rebase("--continue")
                log_debug("[PARITY] Continued rebase after resolving thread conflicts")
            else:
                repo.git.commit("-m", "Auto-merge thread conflicts")
                log_debug("[PARITY] Committed merged thread files")
        except Exception as e:
            log_debug(f"[PARITY] Failed to complete merge/rebase: {e}")
            return False

        return True

    except Exception as e:
        log_debug(f"[PARITY] Failed to auto-resolve thread conflicts: {e}")
        return False


def _push_with_retry(repo: Repo, branch: str, max_retries: int = MAX_PUSH_RETRIES, set_upstream: bool = False) -> bool:
    """Push to origin with retry. Returns True on success.

    Args:
        repo: Git repository
        branch: Branch name to push (validated for safety)
        max_retries: Maximum retry attempts
        set_upstream: If True, use -u flag to set upstream tracking (for first push)

    Note:
        Branch name is validated to prevent flag injection before git operations.
    """
    try:
        _validate_branch_name(branch)
    except ValueError as e:
        log_debug(f"[PARITY] Invalid branch name for push: {e}")
        return False

    for attempt in range(max_retries):
        try:
            if set_upstream:
                repo.git.push("-u", "origin", branch)
            else:
                repo.git.push("origin", branch)
            return True
        except GitCommandError as e:
            error_text = str(e).lower()
            if "rejected" in error_text or "non-fast-forward" in error_text:
                # Try pull --rebase then retry
                log_debug(f"[PARITY] Push rejected, attempting pull --rebase (attempt {attempt + 1})")
                if _pull_rebase(repo, branch):
                    continue  # Retry push
                else:
                    return False
            else:
                log_debug(f"[PARITY] Push failed: {e}")
                return False
    return False


def auto_merge_to_main(
    threads_repo: Repo,
    feature_branch: str,
    main_branch: str,
) -> tuple[bool, str]:
    """Merge threads feature branch to main after code PR merge.

    This is called when the code branch has been merged to main but the
    threads branch hasn't. Per the branch pairing contract, threads follows
    code, so we auto-merge the threads branch to main.

    Args:
        threads_repo: GitPython Repo object for threads repository
        feature_branch: Name of the feature branch to merge (e.g., 'feature/add-blog')
        main_branch: Name of the main branch (e.g., 'main')

    Returns:
        Tuple of (success: bool, message: str describing what happened)
    """
    try:
        _validate_branch_name(feature_branch)
        _validate_branch_name(main_branch)
    except ValueError as e:
        return (False, f"Invalid branch name: {e}")

    original_branch = _get_branch_name(threads_repo)
    actions = []
    stash_ref = None

    try:
        # 0. Stash any uncommitted changes to allow checkout
        stash_ref = _preserve_stash(threads_repo, prefix="watercooler-merge")
        if stash_ref:
            actions.append("Stashed uncommitted changes")

        # 1. Fetch latest from origin
        if not _fetch_with_timeout(threads_repo):
            log_debug("[PARITY] auto_merge_to_main: fetch failed, proceeding anyway")

        # 2. Checkout main branch
        if not _checkout_branch(threads_repo, main_branch):
            # Restore stash before failing
            if stash_ref:
                _restore_stash(threads_repo, stash_ref)
            return (False, f"Failed to checkout {main_branch}")
        actions.append(f"Checked out {main_branch}")

        # 3. Pull latest main (ff-only to ensure we're up to date)
        if _pull_ff_only(threads_repo, main_branch):
            actions.append(f"Pulled latest {main_branch}")
        else:
            # Try rebase if ff-only fails
            if _pull_rebase(threads_repo, main_branch):
                actions.append(f"Pulled {main_branch} (rebase)")
            else:
                log_debug("[PARITY] auto_merge_to_main: pull main failed, proceeding anyway")

        # 4. Merge the feature branch into main
        try:
            threads_repo.git.merge(feature_branch, "--no-edit")
            actions.append(f"Merged {feature_branch} into {main_branch}")
        except GitCommandError as e:
            # Check if it's a conflict
            if _has_conflicts(threads_repo):
                # Try auto-resolve if it's thread-only conflicts
                if _has_thread_conflicts_only(threads_repo):
                    if _auto_resolve_thread_conflicts(threads_repo):
                        actions.append("Auto-resolved thread conflicts during merge")
                    else:
                        # Abort and return to original branch
                        try:
                            threads_repo.git.merge("--abort")
                        except Exception:
                            pass
                        if original_branch:
                            _checkout_branch(threads_repo, original_branch)
                        if stash_ref:
                            _restore_stash(threads_repo, stash_ref)
                        return (False, f"Merge conflict in thread files that could not be auto-resolved: {e}")
                elif _has_graph_conflicts_only(threads_repo):
                    if _auto_resolve_graph_conflicts(threads_repo):
                        actions.append("Auto-resolved graph conflicts during merge")
                    else:
                        try:
                            threads_repo.git.merge("--abort")
                        except Exception:
                            pass
                        if original_branch:
                            _checkout_branch(threads_repo, original_branch)
                        if stash_ref:
                            _restore_stash(threads_repo, stash_ref)
                        return (False, f"Merge conflict in graph files that could not be auto-resolved: {e}")
                else:
                    # Mixed conflicts - abort
                    try:
                        threads_repo.git.merge("--abort")
                    except Exception:
                        pass
                    if original_branch:
                        _checkout_branch(threads_repo, original_branch)
                    if stash_ref:
                        _restore_stash(threads_repo, stash_ref)
                    return (False, f"Merge conflict requiring manual resolution: {e}")
            else:
                if stash_ref:
                    _restore_stash(threads_repo, stash_ref)
                return (False, f"Merge failed: {e}")

        # 5. Push main to origin
        if _push_with_retry(threads_repo, main_branch):
            actions.append(f"Pushed {main_branch} to origin")
        else:
            if stash_ref:
                _restore_stash(threads_repo, stash_ref)
            return (False, f"Merge succeeded locally but push failed. Run: git push origin {main_branch}")

        # 6. Optionally delete the feature branch (local and remote)
        # For now, we leave the feature branch - user can clean up manually
        # This avoids data loss if they want to reference it

        # 7. Stay on main (don't checkout back to feature branch)
        # The feature branch work is complete since code was merged

        # 8. Restore any stashed changes (now on main branch)
        if stash_ref:
            if _restore_stash(threads_repo, stash_ref):
                actions.append("Restored stashed changes")
            else:
                actions.append("Warning: stash restore failed, run 'git stash pop' manually")

        message = f"Auto-merged threads branch to main: {'; '.join(actions)}"
        log_debug(f"[PARITY] {message}")
        return (True, message)

    except Exception as e:
        log_debug(f"[PARITY] auto_merge_to_main failed: {e}")
        # Try to return to original branch
        if original_branch:
            try:
                _checkout_branch(threads_repo, original_branch)
            except Exception:
                pass
        # Restore stash if we created one
        if stash_ref:
            try:
                _restore_stash(threads_repo, stash_ref)
            except Exception:
                pass
        return (False, f"Auto-merge failed: {e}")


def run_preflight(
    code_repo_path: Path,
    threads_repo_path: Path,
    auto_fix: bool = True,
    fetch_first: bool = True,
) -> PreflightResult:
    """Run preflight parity checks with optional auto-remediation.

    Args:
        code_repo_path: Path to code repository
        threads_repo_path: Path to threads repository
        auto_fix: If True, attempt to auto-fix issues (default True)
        fetch_first: If True, fetch from origin before checks (default True)

    Returns:
        PreflightResult with success status, state, and whether operation can proceed
    """
    state = ParityState(last_check_at=_now_iso())
    actions_taken: List[str] = []

    try:
        # Open repos
        try:
            code_repo = Repo(code_repo_path, search_parent_directories=True)
        except InvalidGitRepositoryError:
            state.status = ParityStatus.ERROR.value
            state.last_error = f"Code path is not a git repository: {code_repo_path}"
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        try:
            threads_repo = Repo(threads_repo_path, search_parent_directories=True)
        except InvalidGitRepositoryError:
            state.status = ParityStatus.ERROR.value
            state.last_error = f"Threads path is not a git repository: {threads_repo_path}"
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        # Check for conflicts FIRST - if a merge/rebase has conflicts, that's the primary issue
        # This must come before _is_rebase_in_progress because MERGE_HEAD exists during conflicts
        if _has_conflicts(code_repo):
            state.status = ParityStatus.DIVERGED.value
            state.last_error = (
                f"Code repository has unresolved merge conflicts. "
                f"Resolve conflicts manually and commit, then retry.\n\n"
                f"To resolve:\n"
                f"  cd {code_repo_path}\n"
                f"  git status  # See conflicted files\n"
                f"  # Edit files to resolve <<<< ==== >>>> markers\n"
                f"  git add <resolved-files>\n"
                f"  git commit -m 'Resolve merge conflict'\n"
                f"  # Then retry your watercooler operation"
            )
            write_parity_state(threads_repo_path, state)
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        if _has_conflicts(threads_repo):
            # Check if conflicts are graph-only and auto-resolvable
            if _has_graph_conflicts_only(threads_repo):
                log_debug("[PARITY] Detected graph-only conflicts, attempting auto-resolution...")
                if _auto_resolve_graph_conflicts(threads_repo):
                    log_debug("[PARITY] Successfully auto-resolved graph conflicts")
                    actions_taken.append("Auto-resolved graph file conflicts")
                    # Continue with normal preflight flow
                else:
                    # Auto-resolution failed
                    state.status = ParityStatus.DIVERGED.value
                    state.last_error = (
                        f"Threads repository has unresolved conflicts in graph files "
                        f"that could not be auto-merged. Manual resolution required.\n\n"
                        f"To resolve:\n"
                        f"  cd {threads_repo_path}\n"
                        f"  git status  # See conflicted files\n"
                        f"  # Resolve conflicts and commit\n"
                        f"  git commit -m 'Resolve graph conflicts'\n"
                        f"  # Then retry your watercooler operation"
                    )
                    write_parity_state(threads_repo_path, state)
                    return PreflightResult(
                        success=False,
                        state=state,
                        can_proceed=False,
                        blocking_reason=state.last_error,
                    )
            elif _has_thread_conflicts_only(threads_repo):
                # Thread-only conflicts - try entry-level auto-merge
                log_debug("[PARITY] Detected thread-only conflicts, attempting auto-resolution...")
                if _auto_resolve_thread_conflicts(threads_repo):
                    log_debug("[PARITY] Successfully auto-resolved thread conflicts")
                    actions_taken.append("Auto-resolved thread file conflicts")
                    # Continue with normal preflight flow
                else:
                    # Auto-resolution failed (true conflict - same Entry-ID, different content)
                    state.status = ParityStatus.DIVERGED.value
                    state.last_error = (
                        f"Threads repository has conflicting entries (same Entry-ID, different content). "
                        f"Manual resolution required.\n\n"
                        f"To resolve:\n"
                        f"  cd {threads_repo_path}\n"
                        f"  git status  # See conflicted files\n"
                        f"  # Edit files to resolve conflicts\n"
                        f"  git add <resolved-files>\n"
                        f"  git commit -m 'Resolve thread conflicts'\n"
                        f"  # Then retry your watercooler operation"
                    )
                    write_parity_state(threads_repo_path, state)
                    return PreflightResult(
                        success=False,
                        state=state,
                        can_proceed=False,
                        blocking_reason=state.last_error,
                    )
            else:
                # Mixed or other conflicts - require manual resolution
                state.status = ParityStatus.DIVERGED.value
                state.last_error = (
                    f"Threads repository has unresolved merge conflicts from a previous operation. "
                    f"Resolve conflicts manually and commit, then retry.\n\n"
                    f"To resolve:\n"
                    f"  cd {threads_repo_path}\n"
                    f"  git status  # See conflicted files\n"
                    f"  # Edit files to resolve <<<< ==== >>>> markers\n"
                    f"  git add <resolved-files>\n"
                    f"  git commit -m 'Resolve merge conflict'\n"
                    f"  # Then retry your watercooler operation"
                )
                write_parity_state(threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

        # Check for clean rebase/merge in progress (no conflicts)
        # If we get here, any MERGE_HEAD/rebase state is conflict-free and just needs completion
        if _is_rebase_in_progress(code_repo):
            state.status = ParityStatus.REBASE_IN_PROGRESS.value
            state.last_error = "Code repo has rebase/merge in progress (no conflicts detected)"
            write_parity_state(threads_repo_path, state)
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        if _is_rebase_in_progress(threads_repo):
            state.status = ParityStatus.REBASE_IN_PROGRESS.value
            state.last_error = "Threads repo has rebase/merge in progress (no conflicts detected)"
            write_parity_state(threads_repo_path, state)
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        # Fetch from origin (both repos)
        if fetch_first:
            code_fetch_ok = _fetch_with_timeout(code_repo)
            threads_fetch_ok = _fetch_with_timeout(threads_repo)
            if not code_fetch_ok and not threads_fetch_ok:
                state.status = ParityStatus.REMOTE_UNREACHABLE.value
                state.last_error = "Cannot reach origin for either repository"
                write_parity_state(threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

        # Get branch names
        code_branch = _get_branch_name(code_repo)
        threads_branch = _get_branch_name(threads_repo)
        state.code_branch = code_branch
        state.threads_branch = threads_branch

        # Check for detached HEAD
        if code_branch is None:
            state.status = ParityStatus.DETACHED_HEAD.value
            state.last_error = "Code repo is in detached HEAD state"
            write_parity_state(threads_repo_path, state)
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        if threads_branch is None:
            state.status = ParityStatus.DETACHED_HEAD.value
            state.last_error = "Threads repo is in detached HEAD state"
            write_parity_state(threads_repo_path, state)
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        # Main protection: block if threads=main and code=feature
        main_branch = _find_main_branch(code_repo)
        if main_branch:
            if threads_branch == main_branch and code_branch != main_branch:
                # Try to auto-fix by checking out threads to code branch
                if auto_fix:
                    log_debug(f"[PARITY] Main protection: threads on {main_branch}, code on {code_branch}")

                    # Stash dirty tree before checkout (main protection path)
                    mp_is_dirty = threads_repo.is_dirty(untracked_files=True)
                    mp_stash_ref: Optional[str] = None
                    if mp_is_dirty:
                        try:
                            mp_stash_ref = _preserve_stash(
                                threads_repo, prefix="watercooler-main-protect"
                            )
                            if mp_stash_ref:
                                actions_taken.append(f"Stashed before checkout: {mp_stash_ref}")
                                log_debug(f"[PARITY] Stashed dirty tree: {mp_stash_ref}")
                        except GitCommandError as e:
                            state.status = ParityStatus.MAIN_PROTECTION.value
                            state.last_error = (
                                f"Cannot stash changes before checkout: {e}. "
                                f"Please commit or stash manually, then retry."
                            )
                            write_parity_state(threads_repo_path, state)
                            return PreflightResult(
                                success=False,
                                state=state,
                                can_proceed=False,
                                blocking_reason=state.last_error,
                            )

                    # Check if threads branch exists locally or on origin
                    threads_has_branch = code_branch in [ref.name for ref in threads_repo.heads]
                    threads_origin_has_branch = _branch_exists_on_origin(threads_repo, code_branch)
                    mp_checkout_succeeded = False

                    if threads_has_branch:
                        if _checkout_branch(threads_repo, code_branch):
                            actions_taken.append(f"Checked out threads to {code_branch}")
                            threads_branch = code_branch
                            state.threads_branch = threads_branch
                            mp_checkout_succeeded = True
                        else:
                            state.status = ParityStatus.MAIN_PROTECTION.value
                            state.last_error = (
                                f"Threads repo is on '{main_branch}' but code is on '{code_branch}'. "
                                f"Auto-checkout to {code_branch} failed."
                            )
                            write_parity_state(threads_repo_path, state)
                            return PreflightResult(
                                success=False,
                                state=state,
                                can_proceed=False,
                                blocking_reason=state.last_error,
                            )
                    elif threads_origin_has_branch:
                        # Fetch and checkout
                        try:
                            threads_repo.git.fetch("origin", f"{code_branch}:refs/heads/{code_branch}")
                            if _checkout_branch(threads_repo, code_branch):
                                actions_taken.append(f"Fetched and checked out threads to {code_branch}")
                                threads_branch = code_branch
                                state.threads_branch = threads_branch
                                mp_checkout_succeeded = True
                            else:
                                raise Exception("Checkout failed")
                        except Exception as e:
                            state.status = ParityStatus.MAIN_PROTECTION.value
                            state.last_error = (
                                f"Threads repo is on '{main_branch}' but code is on '{code_branch}'. "
                                f"Auto-fetch and checkout failed: {e}"
                            )
                            write_parity_state(threads_repo_path, state)
                            return PreflightResult(
                                success=False,
                                state=state,
                                can_proceed=False,
                                blocking_reason=state.last_error,
                            )
                    else:
                        # Branch doesn't exist anywhere, create it
                        if _checkout_branch(threads_repo, code_branch, create=True):
                            actions_taken.append(f"Created and checked out threads branch {code_branch}")
                            threads_branch = code_branch
                            state.threads_branch = threads_branch
                            mp_checkout_succeeded = True
                        else:
                            state.status = ParityStatus.MAIN_PROTECTION.value
                            state.last_error = (
                                f"Threads repo is on '{main_branch}' but code is on '{code_branch}'. "
                                f"Failed to create branch {code_branch}."
                            )
                            write_parity_state(threads_repo_path, state)
                            return PreflightResult(
                                success=False,
                                state=state,
                                can_proceed=False,
                                blocking_reason=state.last_error,
                            )

                    # Restore stash after successful checkout
                    if mp_stash_ref and mp_checkout_succeeded:
                        if _restore_stash(threads_repo, mp_stash_ref):
                            actions_taken.append("Restored stashed changes after checkout")
                            log_debug(f"[PARITY] Restored stash: {mp_stash_ref}")
                        else:
                            log_debug(
                                f"[PARITY] Could not restore stash {mp_stash_ref} "
                                "(may have conflicts). Stash preserved."
                            )
                            actions_taken.append(
                                f"Warning: stash {mp_stash_ref} not restored (conflicts?)"
                            )
                else:
                    state.status = ParityStatus.MAIN_PROTECTION.value
                    state.last_error = (
                        f"Threads repo is on '{main_branch}' but code is on '{code_branch}'. "
                        f"Use watercooler_sync_branch_state with operation='checkout' to fix."
                    )
                    write_parity_state(threads_repo_path, state)
                    return PreflightResult(
                        success=False,
                        state=state,
                        can_proceed=False,
                        blocking_reason=state.last_error,
                    )

            # Inverse main protection: code=main and threads=feature
            # Threads ALWAYS follows code. Auto-checkout threads to main.
            # (Merge only happens on explicit PR merge event, not during preflight.)
            if code_branch == main_branch and threads_branch != main_branch:
                log_debug(
                    f"[PARITY] Code on {main_branch}, threads on {threads_branch}. "
                    f"Auto-checkout threads to follow code."
                )
                # Fall through to general branch mismatch handler below,
                # which will auto-checkout threads to code_branch (main)

        # Branch name parity check
        if code_branch != threads_branch:
            if auto_fix:
                # Try to checkout threads to code branch
                log_debug(f"[PARITY] Branch mismatch: code={code_branch}, threads={threads_branch}")

                # BRANCH_MISMATCH_DIRTY: stash before checkout if tree is dirty
                is_dirty = threads_repo.is_dirty(untracked_files=True)
                checkout_stash_ref: Optional[str] = None
                if is_dirty:
                    try:
                        checkout_stash_ref = _preserve_stash(
                            threads_repo, prefix="watercooler-checkout"
                        )
                        if checkout_stash_ref:
                            actions_taken.append(f"Stashed before checkout: {checkout_stash_ref}")
                            log_debug(f"[PARITY] Stashed dirty tree: {checkout_stash_ref}")
                    except GitCommandError as e:
                        # Cannot stash = cannot checkout safely
                        state.status = ParityStatus.BRANCH_MISMATCH.value
                        state.last_error = (
                            f"Cannot stash changes before checkout: {e}. "
                            f"Please commit or stash manually, then retry."
                        )
                        write_parity_state(threads_repo_path, state)
                        return PreflightResult(
                            success=False,
                            state=state,
                            can_proceed=False,
                            blocking_reason=state.last_error,
                        )

                threads_has_branch = code_branch in [ref.name for ref in threads_repo.heads]
                threads_origin_has_branch = _branch_exists_on_origin(threads_repo, code_branch)
                checkout_succeeded = False

                if threads_has_branch:
                    if _checkout_branch(threads_repo, code_branch):
                        actions_taken.append(f"Checked out threads to {code_branch}")
                        threads_branch = code_branch
                        state.threads_branch = threads_branch
                        checkout_succeeded = True
                elif threads_origin_has_branch:
                    try:
                        threads_repo.git.fetch("origin", f"{code_branch}:refs/heads/{code_branch}")
                        if _checkout_branch(threads_repo, code_branch):
                            actions_taken.append(f"Fetched and checked out threads to {code_branch}")
                            threads_branch = code_branch
                            state.threads_branch = threads_branch
                            checkout_succeeded = True
                    except Exception as e:
                        log_debug(f"[PARITY] Fetch and checkout failed: {e}")
                else:
                    # Create branch
                    if _checkout_branch(threads_repo, code_branch, create=True):
                        actions_taken.append(f"Created threads branch {code_branch}")
                        threads_branch = code_branch
                        state.threads_branch = threads_branch
                        checkout_succeeded = True

                # Restore stash after successful checkout
                if checkout_stash_ref and checkout_succeeded:
                    if _restore_stash(threads_repo, checkout_stash_ref):
                        actions_taken.append("Restored stashed changes after checkout")
                        log_debug(f"[PARITY] Restored stash after checkout: {checkout_stash_ref}")
                    else:
                        # Stash restore failed (conflict) - leave stash for user
                        log_debug(
                            f"[PARITY] Could not restore stash {checkout_stash_ref} "
                            "(may have conflicts). Stash preserved for manual recovery."
                        )
                        actions_taken.append(
                            f"Warning: stash {checkout_stash_ref} not restored (conflicts?)"
                        )

            # Re-check after auto-fix attempt
            if code_branch != threads_branch:
                state.status = ParityStatus.BRANCH_MISMATCH.value
                state.last_error = (
                    f"Branch mismatch: code is on '{code_branch}', threads is on '{threads_branch}'. "
                    f"Use watercooler_sync_branch_state with operation='checkout' to fix."
                )
                write_parity_state(threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

        # Remote existence check: if code on origin, threads should be too
        code_on_origin = _branch_exists_on_origin(code_repo, code_branch)
        threads_on_origin = _branch_exists_on_origin(threads_repo, code_branch)

        if code_on_origin and not threads_on_origin:
            if auto_fix:
                # Push threads branch to origin (with -u to set upstream tracking)
                log_debug(f"[PARITY] Threads branch {code_branch} not on origin, pushing with upstream")
                if _push_with_retry(threads_repo, code_branch, set_upstream=True):
                    actions_taken.append(f"Pushed threads branch {code_branch} to origin (upstream set)")
                    threads_on_origin = True
                else:
                    state.status = ParityStatus.PENDING_PUSH.value
                    state.pending_push = True
                    state.last_error = f"Failed to push threads branch {code_branch} to origin"
                    # This is a warning, not a blocker - we can proceed with local commit
                    log_debug(f"[PARITY] {state.last_error}")

        # ORPHANED_BRANCH: threads branch on origin but code branch isn't
        # This indicates the code branch was deleted or never pushed, but threads still exist
        if threads_on_origin and not code_on_origin:
            state.status = ParityStatus.ORPHAN_BRANCH.value
            state.last_error = (
                f"Threads branch '{code_branch}' exists on origin but code branch does not. "
                f"Either push the code branch to origin, or delete the orphaned threads branch."
            )
            log_debug(f"[PARITY] {state.last_error}")
            write_parity_state(threads_repo_path, state)
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        # Check upstream tracking and auto-set if missing
        # This ensures pull operations work correctly even after branch creation
        try:
            upstream = threads_repo.active_branch.tracking_branch()
        except Exception:
            upstream = None

        if upstream is None and threads_on_origin:
            if auto_fix:
                try:
                    threads_repo.git.branch("--set-upstream-to", f"origin/{code_branch}")
                    actions_taken.append(f"Set upstream tracking for {code_branch}")
                    log_debug(f"[PARITY] Set upstream tracking: {code_branch} -> origin/{code_branch}")
                except GitCommandError as e:
                    # Non-blocking - we can still work with explicit origin/branch
                    log_debug(f"[PARITY] Failed to set upstream (non-fatal): {e}")
            else:
                log_debug(f"[PARITY] Warning: No upstream tracking for {code_branch}")

        # Get ahead/behind status
        code_ahead, code_behind = _get_ahead_behind(code_repo, code_branch)
        threads_ahead, threads_behind = _get_ahead_behind(threads_repo, code_branch)
        state.code_ahead_origin = code_ahead
        state.code_behind_origin = code_behind
        state.threads_ahead_origin = threads_ahead
        state.threads_behind_origin = threads_behind

        # Code behind origin: block (we don't mutate code repo)
        if code_behind > 0:
            state.status = ParityStatus.CODE_BEHIND_ORIGIN.value
            state.last_error = (
                f"Code branch is {code_behind} commits behind origin. "
                f"Please pull the code repo before proceeding."
            )
            # Write state before returning so health reports this correctly
            write_parity_state(threads_repo_path, state)
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        # Threads behind origin: AUTO-REMEDIATE with stash safety
        # State classification:
        #   - behind only (threads_behind > 0, threads_ahead == 0): BEHIND_CLEAN or BEHIND_DIRTY
        #   - diverged (threads_behind > 0, threads_ahead > 0): DIVERGED_CLEAN or DIVERGED_DIRTY
        if threads_behind > 0 and auto_fix:
            is_diverged = threads_ahead > 0
            is_dirty = threads_repo.is_dirty(untracked_files=True)
            stash_ref = None

            log_debug(
                f"[PARITY] Threads behind by {threads_behind} commits"
                f"{f', ahead by {threads_ahead}' if is_diverged else ''}"
                f"{', dirty tree' if is_dirty else ''}, auto-remediating"
            )

            # Step 1: Stash if dirty (with timestamp for identification)
            if is_dirty:
                try:
                    stash_ref = _preserve_stash(threads_repo, prefix="watercooler-parity")
                    if stash_ref:
                        actions_taken.append(f"Stashed: {stash_ref}")
                except GitCommandError as e:
                    # Cannot stash → BLOCK
                    state.status = ParityStatus.DIVERGED.value
                    state.last_error = f"Cannot stash changes before pull: {e}"
                    write_parity_state(threads_repo_path, state)
                    return PreflightResult(
                        success=False,
                        state=state,
                        can_proceed=False,
                        blocking_reason=state.last_error,
                    )

            # Step 2: Pull (ff-only first for behind-only, rebase for diverged)
            # Pass code_branch explicitly to work without upstream tracking
            pulled = False
            if is_diverged:
                # Diverged: must use rebase to reconcile
                if _pull_rebase(threads_repo, code_branch):
                    pulled = True
                    actions_taken.append(f"Pulled with rebase ({threads_behind} commits)")
            else:
                # Behind only: try ff-only first, fallback to rebase
                if _pull_ff_only(threads_repo, code_branch):
                    pulled = True
                    actions_taken.append(f"Pulled (ff-only, {threads_behind} commits)")
                elif _pull_rebase(threads_repo, code_branch):
                    pulled = True
                    actions_taken.append(f"Pulled (rebase fallback, {threads_behind} commits)")

            if not pulled:
                # Pull failed - restore stash before blocking
                if stash_ref:
                    if _restore_stash(threads_repo, stash_ref):
                        actions_taken.append("Restored stashed changes after pull failure")
                    # If restore fails, stash is preserved (not dropped)

                state.status = ParityStatus.DIVERGED.value
                state.last_error = (
                    f"Pull failed for threads branch (was {threads_behind} commits behind). "
                    f"{f'Stash preserved: {stash_ref}. ' if stash_ref else ''}"
                    f"Use watercooler_reconcile_parity with force option to recover."
                )
                write_parity_state(threads_repo_path, state)
                return PreflightResult(
                    success=False,
                    state=state,
                    can_proceed=False,
                    blocking_reason=state.last_error,
                )

            # Step 3: Check for conflicts after pull
            if _has_conflicts(threads_repo):
                # Check if conflicts are graph-only and auto-resolvable
                if _has_graph_conflicts_only(threads_repo):
                    log_debug("[PARITY] Detected graph-only conflicts after pull, attempting auto-resolution...")
                    if _auto_resolve_graph_conflicts(threads_repo):
                        log_debug("[PARITY] Successfully auto-resolved graph conflicts after pull")
                        actions_taken.append("Auto-resolved graph file conflicts after pull")
                        # Continue rebase after resolving conflicts
                        try:
                            threads_repo.git.rebase("--continue")
                            log_debug("[PARITY] Rebase continued successfully after resolving conflicts")
                            actions_taken.append("Continued rebase after resolving conflicts")
                        except GitCommandError as e:
                            # Rebase continue failed - this shouldn't happen if we resolved all conflicts
                            log_debug(f"[PARITY] Rebase continue failed after resolving conflicts: {e}")
                            state.status = ParityStatus.DIVERGED.value
                            state.last_error = (
                                f"Rebase continue failed after auto-resolving graph conflicts. "
                                f"{f'Stash preserved: {stash_ref}. ' if stash_ref else ''}"
                                f"Manual intervention required."
                            )
                            write_parity_state(threads_repo_path, state)
                            return PreflightResult(
                                success=False,
                                state=state,
                                can_proceed=False,
                                blocking_reason=state.last_error,
                            )
                    else:
                        # Auto-resolution failed
                        state.status = ParityStatus.DIVERGED.value
                        state.last_error = (
                            f"Graph conflicts after pull could not be auto-merged. "
                            f"{f'Stash preserved: {stash_ref}. ' if stash_ref else ''}"
                            f"Manual resolution required."
                        )
                        write_parity_state(threads_repo_path, state)
                        return PreflightResult(
                            success=False,
                            state=state,
                            can_proceed=False,
                            blocking_reason=state.last_error,
                        )
                elif _has_thread_conflicts_only(threads_repo):
                    # Thread-only conflicts after pull - try entry-level auto-merge
                    log_debug("[PARITY] Detected thread-only conflicts after pull, attempting auto-resolution...")
                    if _auto_resolve_thread_conflicts(threads_repo):
                        log_debug("[PARITY] Successfully auto-resolved thread conflicts after pull")
                        actions_taken.append("Auto-resolved thread file conflicts after pull")
                        # Continue rebase after resolving conflicts
                        try:
                            threads_repo.git.rebase("--continue")
                            log_debug("[PARITY] Rebase continued successfully after resolving thread conflicts")
                            actions_taken.append("Continued rebase after resolving thread conflicts")
                        except GitCommandError as e:
                            log_debug(f"[PARITY] Rebase continue failed after resolving thread conflicts: {e}")
                            state.status = ParityStatus.DIVERGED.value
                            state.last_error = (
                                f"Rebase continue failed after auto-resolving thread conflicts. "
                                f"{f'Stash preserved: {stash_ref}. ' if stash_ref else ''}"
                                f"Manual intervention required."
                            )
                            write_parity_state(threads_repo_path, state)
                            return PreflightResult(
                                success=False,
                                state=state,
                                can_proceed=False,
                                blocking_reason=state.last_error,
                            )
                    else:
                        # Auto-resolution failed (true conflict)
                        state.status = ParityStatus.DIVERGED.value
                        state.last_error = (
                            f"Thread conflicts after pull could not be auto-merged (same Entry-ID, different content). "
                            f"{f'Stash preserved: {stash_ref}. ' if stash_ref else ''}"
                            f"Manual resolution required."
                        )
                        write_parity_state(threads_repo_path, state)
                        return PreflightResult(
                            success=False,
                            state=state,
                            can_proceed=False,
                            blocking_reason=state.last_error,
                        )
                else:
                    # Mixed or other conflicts → BLOCK (stash preserved)
                    state.status = ParityStatus.DIVERGED.value
                    state.last_error = (
                        f"Merge conflict after pull. "
                        f"{f'Stash preserved: {stash_ref}. ' if stash_ref else ''}"
                        f"Resolve conflicts manually, then run watercooler_reconcile_parity."
                    )
                    write_parity_state(threads_repo_path, state)
                    return PreflightResult(
                        success=False,
                        state=state,
                        can_proceed=False,
                        blocking_reason=state.last_error,
                    )

            # Step 4: Pop stash if we stashed earlier
            if stash_ref:
                if _restore_stash(threads_repo, stash_ref):
                    actions_taken.append("Restored stashed changes")
                else:
                    # Stash pop conflict → BLOCK (stash preserved)
                    state.status = ParityStatus.DIVERGED.value
                    state.last_error = (
                        f"Stash pop conflict after pull. Stash preserved: {stash_ref}. "
                        f"Resolve conflicts manually: cd <threads-repo> && git stash pop"
                    )
                    write_parity_state(threads_repo_path, state)
                    return PreflightResult(
                        success=False,
                        state=state,
                        can_proceed=False,
                        blocking_reason=state.last_error,
                    )

            # Step 5: Re-check ahead/behind counts after pull
            threads_ahead, threads_behind = _get_ahead_behind(threads_repo, code_branch)
            state.threads_ahead_origin = threads_ahead
            state.threads_behind_origin = threads_behind

        elif threads_behind > 0 and not auto_fix:
            # Auto-fix disabled: BLOCK with guidance
            state.status = ParityStatus.DIVERGED.value
            state.last_error = (
                f"Threads branch is {threads_behind} commits behind origin. "
                f"Enable auto_fix or use watercooler_reconcile_parity to sync."
            )
            write_parity_state(threads_repo_path, state)
            return PreflightResult(
                success=False,
                state=state,
                can_proceed=False,
                blocking_reason=state.last_error,
            )

        # Threads ahead of origin: auto-push when code is synced
        # This is the key parity check: if code is pushed, threads should be too
        #
        # Race condition note: Between checking threads_ahead and calling _push_with_retry,
        # another agent could push to origin. This is handled by _push_with_retry which
        # does pull --rebase and retries on rejection. The commit count in actions_taken
        # reflects the pre-push state; the actual pushed count may differ after rebase.
        if threads_ahead > 0 and code_ahead == 0:
            if auto_fix:
                original_ahead = threads_ahead
                log_debug(f"[PARITY] Threads ahead of origin by {threads_ahead} commits, pushing")
                if _push_with_retry(threads_repo, code_branch):
                    # Re-check ahead count after push (may differ if rebase occurred)
                    new_ahead, _ = _get_ahead_behind(threads_repo, code_branch)
                    if new_ahead == 0:
                        actions_taken.append(f"Pushed threads (was {original_ahead} commits ahead)")
                    else:
                        # Partial push - some commits still unpushed after rebase
                        # This can happen if rebase created new merge commits
                        actions_taken.append(
                            f"Pushed threads (was {original_ahead} ahead, now {new_ahead} ahead)"
                        )
                        state.pending_push = True
                        log_debug(
                            f"[PARITY] Push succeeded but {new_ahead} commits remain ahead "
                            "(rebase may have created new commits)"
                        )
                    threads_ahead = new_ahead
                    state.threads_ahead_origin = new_ahead
                else:
                    # Push failed - mark as pending but allow operation to proceed
                    # The write will add more commits; we'll try again next time
                    state.status = ParityStatus.PENDING_PUSH.value
                    state.pending_push = True
                    state.last_error = f"Failed to push threads branch {code_branch} to origin"
                    log_debug(f"[PARITY] {state.last_error}")
            else:
                # No auto-fix: warn but don't block
                log_debug(
                    f"[PARITY] Threads ahead of origin by {threads_ahead} commits "
                    f"(auto_fix disabled, not pushing)"
                )

        # All checks passed
        state.status = ParityStatus.CLEAN.value
        state.actions_taken = actions_taken

        # Write state file
        write_parity_state(threads_repo_path, state)

        return PreflightResult(
            success=True,
            state=state,
            can_proceed=True,
            auto_fixed=len(actions_taken) > 0,
        )

    except Exception as e:
        state.status = ParityStatus.ERROR.value
        state.last_error = f"Unexpected error during preflight: {e}"
        # Try to persist state; may fail if threads_repo_path is invalid
        try:
            write_parity_state(threads_repo_path, state)
        except Exception:
            pass  # Best effort - don't mask the original error
        return PreflightResult(
            success=False,
            state=state,
            can_proceed=False,
            blocking_reason=state.last_error,
        )


def ensure_readable(
    threads_repo_path: Path,
    code_repo_path: Optional[Path] = None,
) -> tuple[bool, List[str]]:
    """Lightweight sync for read operations. Never blocks - worst case is stale data.

    This function is designed to be called before read operations (list_threads,
    read_thread, get_thread_entry, etc.) to ensure the local threads repo is
    up-to-date with origin. Unlike run_preflight(), this function:

    - Never blocks: Always returns (True, actions) even on failure
    - No stashing: Only pulls if the tree is clean (safe operation)
    - No pushing: Read operations don't require push parity
    - Logs warnings: Issues are logged but don't prevent the read

    Args:
        threads_repo_path: Path to threads repository
        code_repo_path: Optional path to code repository (for context logging)

    Returns:
        Tuple of (success, list of actions taken)
    """
    actions: List[str] = []

    try:
        repo = Repo(threads_repo_path, search_parent_directories=True)

        # Early conflict detection - skip sync but allow stale reads
        # This prevents attempting pulls on a conflicted repo
        if _has_conflicts(repo):
            log_debug(
                f"[PARITY] ensure_readable: Threads repo has unresolved conflicts, "
                f"skipping sync (may return stale data)"
            )
            return (True, ["Skipped sync due to unresolved conflicts - reading potentially stale data"])

        # Fetch from origin (with timeout)
        if not _fetch_with_timeout(repo):
            log_debug("[PARITY] ensure_readable: fetch failed (proceeding with cached data)")
            return (True, actions)

        # Get current branch
        branch = _get_branch_name(repo)
        if not branch:
            log_debug("[PARITY] ensure_readable: detached HEAD (proceeding anyway)")
            return (True, actions)

        # Get ahead/behind status
        ahead, behind = _get_ahead_behind(repo, branch)

        # Only auto-pull if:
        # 1. Behind origin (need to catch up)
        # 2. NOT ahead (no local commits to lose)
        # 3. Tree is clean (safe to pull without stash)
        if behind > 0 and ahead == 0 and not repo.is_dirty(untracked_files=True):
            log_debug(f"[PARITY] ensure_readable: behind by {behind} commits, auto-pulling")

            # Try ff-only first, then rebase
            # Pass branch explicitly to work without upstream tracking
            if _pull_ff_only(repo, branch):
                actions.append(f"Pulled (ff-only, {behind} commits)")
            elif _pull_rebase(repo, branch):
                actions.append(f"Pulled (rebase, {behind} commits)")
            else:
                log_debug("[PARITY] ensure_readable: pull failed (proceeding with stale data)")

        elif behind > 0:
            # Behind but can't safely pull - log warning
            if ahead > 0:
                log_debug(
                    f"[PARITY] ensure_readable: diverged (ahead={ahead}, behind={behind}), "
                    "using local data"
                )
            elif repo.is_dirty(untracked_files=True):
                log_debug(
                    f"[PARITY] ensure_readable: behind by {behind} but tree is dirty, "
                    "using local data"
                )

        return (True, actions)

    except InvalidGitRepositoryError:
        log_debug(f"[PARITY] ensure_readable: not a git repo: {threads_repo_path}")
        return (True, actions)
    except Exception as e:
        log_debug(f"[PARITY] ensure_readable: error (proceeding anyway): {e}")
        return (True, actions)


def push_after_commit(
    threads_repo_path: Path,
    branch: str,
    max_retries: int = MAX_PUSH_RETRIES,
) -> tuple[bool, Optional[str]]:
    """Push threads repo after commit. Returns (success, error_message).

    Note:
        Branch name is validated to prevent flag injection before git operations.
    """
    try:
        _validate_branch_name(branch)
    except ValueError as e:
        return (False, f"Invalid branch name: {e}")

    try:
        threads_repo = Repo(threads_repo_path, search_parent_directories=True)

        for attempt in range(max_retries):
            try:
                threads_repo.git.push("origin", branch)
                return (True, None)
            except GitCommandError as e:
                error_text = str(e).lower()
                if "rejected" in error_text or "non-fast-forward" in error_text:
                    log_debug(f"[PARITY] Push rejected, pulling with rebase (attempt {attempt + 1})")
                    try:
                        threads_repo.git.pull("--rebase")
                    except GitCommandError as pull_e:
                        # Rebase failed, abort and report
                        try:
                            threads_repo.git.rebase("--abort")
                        except Exception:
                            pass
                        return (False, f"Push rejected and rebase failed: {pull_e}")
                    continue  # Retry push
                else:
                    return (False, f"Push failed: {e}")

        return (False, f"Push failed after {max_retries} attempts")

    except Exception as e:
        return (False, f"Unexpected error during push: {e}")


def get_branch_health(
    code_repo_path: Path,
    threads_repo_path: Path,
) -> dict:
    """Get branch health status with LIVE git checks.

    This function performs live git operations to get current state,
    rather than relying on potentially stale cached state from
    branch_parity_state.json.

    Note:
        This is a best-effort snapshot for health reporting. There is no
        locking mechanism, so external git operations could modify state
        between reading and returning. For operations requiring strict
        consistency, use BranchParityManager from the sync package which
        provides proper locking via run_with_sync().

    Returns dict with:
    - status: Current parity status (computed LIVE)
    - code_branch, threads_branch: Branch names (LIVE)
    - code_ahead/behind, threads_ahead/behind: Commit counts (LIVE)
    - pending_push: Whether threads has unpushed commits
    - last_check_at: Timestamp of this check (fresh)
    - lock_holder: PID of current lock holder (if any)
    """
    # Read cached state for metadata only (pending_push, last_error, actions_taken)
    state = read_parity_state(threads_repo_path)

    # Initialize with fallback values from state
    code_branch = state.code_branch
    threads_branch = state.threads_branch
    code_ahead, code_behind = state.code_ahead_origin, state.code_behind_origin
    threads_ahead, threads_behind = state.threads_ahead_origin, state.threads_behind_origin
    live_status = state.status

    # Perform LIVE git checks
    try:
        code_repo = Repo(code_repo_path, search_parent_directories=True)
        threads_repo = Repo(threads_repo_path, search_parent_directories=True)

        # Live branch names
        code_branch = _get_branch_name(code_repo)
        threads_branch = _get_branch_name(threads_repo)

        # Check for detached HEAD
        if code_branch is None or threads_branch is None:
            live_status = ParityStatus.DETACHED_HEAD.value
        else:
            # Live ahead/behind counts (fetch first for accuracy)
            try:
                code_repo.remotes.origin.fetch(prune=True)
            except Exception as e:
                log_debug(f"[PARITY] Fetch failed for code repo (non-fatal): {e}")

            try:
                threads_repo.remotes.origin.fetch(prune=True)
            except Exception as e:
                log_debug(f"[PARITY] Fetch failed for threads repo (non-fatal): {e}")

            code_ahead, code_behind = _get_ahead_behind(code_repo, code_branch)
            threads_ahead, threads_behind = _get_ahead_behind(threads_repo, threads_branch)

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
        log_debug(f"[PARITY] Invalid git repository in get_branch_health: {e}")
        # Fall back to cached state
    except Exception as e:
        log_debug(f"[PARITY] Error in get_branch_health: {e}")
        # Fall back to cached state

    # Check for active lock
    lock_holder = None
    lock_dir = _lock_dir(threads_repo_path)
    if lock_dir.exists():
        for lock_file in lock_dir.glob("*.lock"):
            try:
                content = lock_file.read_text(encoding="utf-8")
                if content.startswith("pid="):
                    lock_holder = content.split()[0].split("=")[1]
                    break
            except Exception:
                pass

    return {
        "status": live_status,  # NOW LIVE
        "code_branch": code_branch,  # NOW LIVE
        "threads_branch": threads_branch,  # NOW LIVE
        "code_ahead_origin": code_ahead,  # NOW LIVE
        "code_behind_origin": code_behind,  # NOW LIVE
        "threads_ahead_origin": threads_ahead,  # NOW LIVE
        "threads_behind_origin": threads_behind,  # NOW LIVE
        "pending_push": state.pending_push,  # From state (async queue status)
        "last_check_at": _now_iso(),  # Fresh timestamp
        "last_error": state.last_error,  # From state (historical)
        "actions_taken": state.actions_taken,  # From state (historical)
        "lock_holder": lock_holder,  # Live check
    }
