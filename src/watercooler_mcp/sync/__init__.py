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
    # Parity
    get_parity_state,
    # Fetch/Pull/Push
    fetch_with_timeout,
    pull_ff_only,
    abort_rebase,
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
    from ..observability import log_action

    lock_path = _topic_lock_path(threads_dir, topic)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    for attempt in range(LOCK_QUICK_RETRIES):
        lock = AdvisoryLock(lock_path, ttl=LOCK_TTL_SECONDS, timeout=0)
        if lock.acquire():
            wait_ms = (time.perf_counter() - t0) * 1000
            log_action("sync.lock.acquire", outcome="ok", topic=topic, wait_ms=round(wait_ms, 2), quick_attempt=attempt + 1)
            return lock
        time.sleep(LOCK_QUICK_RETRY_DELAY)

    # Stale lock cleanup is handled by AdvisoryLock's TTL parameter.
    # Manual stale-break was removed — rename-based approaches have an
    # unavoidable TOCTOU on Linux (rename succeeds even when the file
    # has been replaced by a fresh valid lock between stat and rename).
    lock = AdvisoryLock(lock_path, ttl=LOCK_TTL_SECONDS, timeout=timeout)
    if not lock.acquire():
        wait_ms = (time.perf_counter() - t0) * 1000
        log_action("sync.lock.acquire", outcome="timeout", topic=topic, wait_ms=round(wait_ms, 2))
        raise TimeoutError(
            f"Failed to acquire topic lock for '{topic}' within {timeout}s"
        )
    wait_ms = (time.perf_counter() - t0) * 1000
    log_action("sync.lock.acquire", outcome="ok", topic=topic, wait_ms=round(wait_ms, 2), waited=True)
    return lock


def ensure_readable(
    threads_repo_path: Path, code_repo_path: Optional[Path] = None
) -> Tuple[bool, List[str], str, bool]:
    """Ensure threads dir is readable by doing a fast-forward pull if needed.

    Uses the parity engine to honestly report sync state and only attempt
    safe operations. No longer swallows errors — callers get an honest
    ``parity_state`` to decide how to proceed.

    Returns:
        Tuple of ``(ok, actions, parity_state, auto_heal_failed)``.

        - ``parity_state`` is one of the canonical states from
          ``get_parity_state()``. The canonical vocabulary is preserved;
          no synthetic states.
        - ``auto_heal_failed`` is True only when the entry state was
          ``behind_only`` AND the ``pull_ff_only()`` attempt failed, so
          callers (or ``format_parity_warning``) can surface the
          otherwise-silent "behind but couldn't fast-forward" case
          without adding ``behind_only`` to ``_WARN_PARITY_STATES``
          (which would also fire in the common, successful ff-only
          path).
    """
    actions: List[str] = []
    parity = "unknown"
    auto_heal_failed = False
    try:
        if not threads_repo_path.exists():
            return (True, actions, "clean", False)
        git_dir = threads_repo_path / ".git"
        if not git_dir.exists() and not (threads_repo_path / "HEAD").exists():
            git_file = threads_repo_path / ".git"
            if not git_file.exists():
                return (True, actions, "clean", False)

        from git import Repo
        repo = Repo(threads_repo_path)

        # Fetch first (needed for accurate parity)
        fetch_ok = False
        try:
            if fetch_with_timeout(repo, timeout=15):
                actions.append("fetched")
                fetch_ok = True
        except Exception as fetch_err:
            actions.append(f"fetch failed: {fetch_err}")

        # Determine parity state
        parity = get_parity_state(repo)

        if parity == "behind_only":
            # Safe to fast-forward
            try:
                if pull_ff_only(repo):
                    actions.append("pulled")
                    parity = "clean"
                else:
                    # pull_ff_only returned False — can't fast-forward
                    # (typically: worktree has local divergence or
                    # uncommitted changes preventing the ff). Mark the
                    # state as unresolved so the caller emits a banner.
                    auto_heal_failed = True
            except Exception as pull_err:
                actions.append(f"pull failed: {pull_err}")
                auto_heal_failed = True

        elif parity == "dirty_derived_only":
            # Auto-clean derived caches, then retry
            from .primitives import should_discard_dirty_entry
            try:
                status_out = repo.git.status("--porcelain")
                for line in status_out.strip().split("\n"):
                    if not line.strip():
                        continue
                    filename = line[3:].split(" -> ")[-1].strip()
                    # Guard: only delete derived files, and never an untracked
                    # write-once projection whose sole copy isn't on origin yet
                    # (checkout can't restore it — #924 review). Tracked-modified
                    # churn — and an untracked projection origin already tracks —
                    # is still cleaned/restored from the index.
                    if not should_discard_dirty_entry(repo, line[:2], filename):
                        continue
                    filepath = threads_repo_path / filename
                    if filepath.exists():
                        filepath.unlink()
                    # Restore from index if tracked (prevents leftover D status)
                    try:
                        repo.git.checkout("--", filename)
                    except Exception:
                        pass  # Untracked files will fail checkout — that's fine
                actions.append("cleaned derived caches")
                # Re-check parity after cleaning. A remaining dirty_derived_only
                # means only a preserved untracked projection is left, which never
                # blocks a fast-forward — pull anyway to clear the behind state.
                parity = get_parity_state(repo)
                if parity in ("behind_only", "dirty_derived_only"):
                    # Mirror the primary ``behind_only`` branch above —
                    # when the ff-only pull can't resolve the behind
                    # state, flag the caller so the stale-read banner
                    # fires. Without this, ``dirty_derived_only`` →
                    # ``behind_only`` silently fell through with
                    # ``auto_heal_failed = False`` and no banner.
                    try:
                        if pull_ff_only(repo):
                            actions.append("pulled")
                            parity = "clean"
                        else:
                            actions.append("pull failed (ff-only)")
                            auto_heal_failed = True
                    except Exception as pull_err:
                        actions.append(f"pull failed: {pull_err}")
                        auto_heal_failed = True
            except Exception as clean_err:
                actions.append(f"derived cache cleanup failed: {clean_err}")

        elif parity == "auth_or_network_error":
            if not fetch_ok:
                return (False, actions, parity, auto_heal_failed)

        elif parity == "diverged":
            # Reconcile: clean derived caches, stash, rebase, best-effort push.
            # Mirrors the write path in middleware.py:368-385.
            # Skip if fetch failed — remote refs are stale and rebase would fail.
            if not fetch_ok:
                actions.append("warning: diverged but fetch failed, skipping reconciliation")
                return (True, actions, parity, auto_heal_failed)
            try:
                from ..observability import log_action

                # Step 1: Clean derived caches before stash/rebase
                from .primitives import should_discard_dirty_entry
                if is_dirty(repo, untracked=True):
                    try:
                        status_out = repo.git.status("--porcelain")
                        for line in status_out.strip().split("\n"):
                            if not line.strip():
                                continue
                            filename = line[3:].split(" -> ")[-1].strip()
                            # Untracked write-once projections with no origin copy
                            # are preserved here too; the stash below carries them
                            # through the rebase (#924 review).
                            if should_discard_dirty_entry(repo, line[:2], filename):
                                filepath = threads_repo_path / filename
                                if filepath.exists():
                                    filepath.unlink()
                                try:
                                    repo.git.checkout("--", filename)
                                except Exception:
                                    pass
                        actions.append("cleaned derived caches")
                    except Exception:
                        pass

                # Step 2: Stash any remaining dirty files
                stash_ref = stash_changes(repo)
                if stash_ref:
                    actions.append(f"stashed: {stash_ref}")

                # Step 3: Rebase local commits onto remote
                if pull_rebase(repo):
                    actions.append("rebased onto remote")
                    log_action("sync.ensure_readable.rebase", outcome="ok")

                    # Step 4: Best-effort push to clear backlog
                    try:
                        if push_with_retry(repo, max_retries=2):
                            actions.append("pushed")
                            log_action("sync.ensure_readable.push", outcome="ok")
                        else:
                            actions.append("push failed (will retry on next write)")
                    except Exception as push_err:
                        actions.append(f"push skipped: {push_err}")
                else:
                    actions.append("rebase failed, data may be stale")
                    log_action("sync.ensure_readable.rebase", outcome="conflict")

                # Step 5: Restore stash, then re-sample parity
                if stash_ref:
                    if restore_stash(repo, stash_ref):
                        actions.append("restored stash")
                    else:
                        actions.append(f"stash restore failed (preserved as {stash_ref})")

                parity = get_parity_state(repo)

            except Exception as recon_err:
                actions.append(f"reconciliation failed: {recon_err}")

            return (True, actions, parity, auto_heal_failed)

        elif parity in ("dirty_mixed", "stuck_rebase_or_merge"):
            # Data may be stale but still readable — return True with warning
            actions.append(f"warning: worktree in {parity} state, data may be stale")
            return (True, actions, parity, auto_heal_failed)

        # clean, ahead_only, no_upstream are all readable states
        return (True, actions, parity, auto_heal_failed)
    except Exception as e:
        return (False, [f"error: {e}"], parity, auto_heal_failed)


# Parity states that warrant a user-visible warning banner.
_WARN_PARITY_STATES = frozenset({
    "diverged",
    "dirty_mixed",
    "stuck_rebase_or_merge",
    "auth_or_network_error",
})


def format_parity_warning(parity: str, auto_heal_failed: bool = False) -> str:
    """Return a one-line warning banner for non-clean parity states.

    Returns empty string for clean/benign states. Designed to be
    prepended to tool response text so the user knows data may be
    stale.

    The ``auto_heal_failed`` flag is a narrow, targeted signal from
    ``ensure_readable``: True means the worktree was ``behind_only``
    at entry AND the fast-forward pull attempt failed (typically
    because the worktree has local divergence or uncommitted
    non-derived changes). This case is invisible via
    ``_WARN_PARITY_STATES`` alone — adding ``behind_only`` to that
    set would emit a banner in the common, successful ff-only path
    too. Routing through this flag surfaces stale-read risk only
    when auto-heal actually failed.
    """
    if auto_heal_failed and parity == "behind_only":
        return (
            "⚠ Sync: Threads worktree is behind origin and auto-heal "
            "could not fast-forward — data may be stale. "
            "Run watercooler_sync_repair.\n\n"
        )
    if parity not in _WARN_PARITY_STATES:
        return ""
    msgs = {
        "diverged": "Threads worktree diverged from origin — data may be stale. Run watercooler_sync_repair to reconcile.",
        "dirty_mixed": "Threads worktree has uncommitted non-derived changes — data may be stale.",
        "stuck_rebase_or_merge": "Threads worktree has an unfinished rebase/merge — data may be stale. Run watercooler_sync_repair.",
        "auth_or_network_error": "Could not reach threads remote — showing cached data.",
    }
    return f"⚠ Sync: {msgs.get(parity, parity)}\n\n"


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
    # Primitives - Parity
    "get_parity_state",
    # Primitives - Fetch/Pull/Push
    "fetch_with_timeout",
    "pull_ff_only",
    "abort_rebase",
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
    "format_parity_warning",
    "acquire_topic_lock",
    "LOCK_TIMEOUT_SECONDS",
    "LOCK_TTL_SECONDS",
    "MAX_TOPIC_LENGTH",
]
