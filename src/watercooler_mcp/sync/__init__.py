"""Sync package for watercooler-cloud git operations.

This package provides git synchronization primitives and locking utilities:

- primitives - Pure git operations (validate, fetch, pull, push, stash, checkout)
- errors - Rich exception hierarchy
- Locking utilities - Per-topic advisory locks for concurrent write serialization
"""

import hashlib
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .errors import (
    SyncError,
    PullError,
    PushError,
    ConflictError,
    LockError,
    NetworkError,
    AuthenticationError,
)

from .primitives import (
    # Constants
    MAX_PUSH_RETRIES,
    MAX_BRANCH_LENGTH,
    INVALID_BRANCH_PATTERNS,
    # Validation
    validate_branch_name,
    # Branch operations
    get_branch_name,
    is_detached_head,
    is_dirty,
    is_rebase_in_progress,
    has_conflicts,
    branch_exists_on_origin,
    get_ahead_behind,
    # Fetch/Pull/Push
    fetch_with_timeout,
    pull_ff_only,
    pull_rebase,
    push_with_retry,
    # Checkout
    checkout_branch,
    # Stash
    detect_stash,
    stash_changes,
    restore_stash,
)

# ============================================================================
# Standalone utilities (locking and topic sanitization)
# ============================================================================

# Locking constants
LOCK_TIMEOUT_SECONDS = 30
LOCK_TTL_SECONDS = 120
LOCK_QUICK_RETRIES = 3
LOCK_QUICK_RETRY_DELAY = 0.1
LOCKS_DIR_NAME = ".watercooler"

# Topic validation constants
MAX_TOPIC_LENGTH = 200
UNSAFE_TOPIC_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*]')


def _sanitize_topic_for_filename(topic: str) -> str:
    """Sanitize topic name for use as filename."""
    safe = re.sub(r'\.\.', '_', topic)
    safe = re.sub(r'[<>:"/\\|?*]', '_', safe)
    safe = re.sub(r'_+', '_', safe)
    safe = safe.strip('_').lstrip('.')
    if not safe:
        return '_empty_'
    if len(safe) > MAX_TOPIC_LENGTH:
        hash_suffix = hashlib.sha256(topic.encode()).hexdigest()[:8]
        truncate_at = MAX_TOPIC_LENGTH - len(hash_suffix) - 1
        safe = f"{safe[:truncate_at]}_{hash_suffix}"
    return safe


def _lock_dir(threads_dir: Path) -> Path:
    """Get the directory for lock files."""
    return threads_dir / LOCKS_DIR_NAME / "locks"


def _topic_lock_path(threads_dir: Path, topic: str) -> Path:
    """Get path to per-topic lock file."""
    lock_dir = _lock_dir(threads_dir)
    safe_topic = _sanitize_topic_for_filename(topic)
    return lock_dir / f"{safe_topic}.lock"


def acquire_topic_lock(
    threads_dir: Path, topic: str, timeout: int = LOCK_TIMEOUT_SECONDS
) -> "AdvisoryLock":
    """Acquire lock for a specific topic. Returns lock (caller must release)."""
    from watercooler.lock import AdvisoryLock

    lock_path = _topic_lock_path(threads_dir, topic)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(LOCK_QUICK_RETRIES):
        lock = AdvisoryLock(lock_path, ttl=LOCK_TTL_SECONDS, timeout=0)
        if lock.acquire():
            return lock
        time.sleep(LOCK_QUICK_RETRY_DELAY)

    lock = AdvisoryLock(lock_path, ttl=LOCK_TTL_SECONDS, timeout=timeout)
    if not lock.acquire():
        raise TimeoutError(
            f"Failed to acquire topic lock for '{topic}' within {timeout}s"
        )
    return lock


def ensure_readable(
    threads_repo_path: Path, code_repo_path: Optional[Path] = None
) -> Tuple[bool, List[str]]:
    """Ensure threads dir is readable by doing a fast-forward pull if needed.

    Returns:
        Tuple of (success, list of actions taken)
    """
    actions: List[str] = []
    try:
        if not threads_repo_path.exists():
            return (True, actions)
        git_dir = threads_repo_path / ".git"
        if not git_dir.exists() and not (threads_repo_path / "HEAD").exists():
            # Not a git repo (could be worktree with linked .git file)
            git_file = threads_repo_path / ".git"
            if not git_file.exists():
                return (True, actions)

        from git import Repo
        repo = Repo(threads_repo_path)

        # Fetch with timeout
        try:
            fetch_with_timeout(repo, timeout=15)
            actions.append("fetched")
        except Exception:
            pass

        # Fast-forward if behind
        try:
            pull_ff_only(repo)
            actions.append("pulled")
        except Exception:
            pass

        return (True, actions)
    except Exception as e:
        return (False, [f"error: {e}"])


__all__ = [
    # Errors
    "SyncError",
    "PullError",
    "PushError",
    "ConflictError",
    "LockError",
    "NetworkError",
    "AuthenticationError",
    # Constants
    "MAX_PUSH_RETRIES",
    "MAX_BRANCH_LENGTH",
    "INVALID_BRANCH_PATTERNS",
    # Primitives - Validation
    "validate_branch_name",
    # Primitives - Branch operations
    "get_branch_name",
    "is_detached_head",
    "is_dirty",
    "is_rebase_in_progress",
    "has_conflicts",
    "branch_exists_on_origin",
    "get_ahead_behind",
    # Primitives - Fetch/Pull/Push
    "fetch_with_timeout",
    "pull_ff_only",
    "pull_rebase",
    "push_with_retry",
    # Primitives - Checkout
    "checkout_branch",
    # Primitives - Stash
    "detect_stash",
    "stash_changes",
    "restore_stash",
    # Standalone utilities
    "ensure_readable",
    "acquire_topic_lock",
    "LOCK_TIMEOUT_SECONDS",
    "LOCK_TTL_SECONDS",
    "MAX_TOPIC_LENGTH",
]
