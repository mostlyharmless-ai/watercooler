"""Pure git primitives for sync operations.

This module provides low-level git operations with no side effects beyond
the git repository itself. These functions:

- Have no state management (no reading/writing state files)
- Have no logging to external services
- Return simple types (bool, Optional[str], tuple)
- Are safe to call in any order
- Validate inputs before git operations

All branch names are validated before use to prevent:
- Flag injection (names starting with -)
- Path traversal
- Invalid git refname characters
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from git import Repo
from git.exc import GitCommandError

from ..observability import log_action, log_debug, log_warning


# =============================================================================
# Constants
# =============================================================================

MAX_PUSH_RETRIES = 3  # Number of rebase+retry attempts for push operations
MAX_BRANCH_LENGTH = 255  # Git branch name length limit

# Pattern for invalid branch name characters/sequences per git-check-ref-format
# Each tuple is (compiled_pattern, raw_pattern, human_readable_message)
_BRANCH_VALIDATION_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\.\.+"), r"\.\.+", "contains consecutive dots (..)"),
    (re.compile(r"^-"), r"^-", "starts with hyphen (potential flag injection)"),
    (re.compile(r"-$"), r"-$", "ends with hyphen"),
    (re.compile(r"^\.|\.$$"), r"^\.|\.$", "starts or ends with dot"),
    (re.compile(r"\.lock$"), r"\.lock$", "ends with .lock (reserved suffix)"),
    (re.compile(r"@\{"), r"@\{", "contains reflog syntax (@{)"),
    (re.compile(r"[\x00-\x1f\x7f]"), r"[\x00-\x1f\x7f]", "contains control characters"),
    (re.compile(r"[~^:?*\[\]\\]"), r"[~^:?*\[\]\\]", "contains invalid git characters (~^:?*[]\\)"),
    (re.compile(r"\s"), r"\s", "contains whitespace"),
]

# Keep raw patterns for backwards compatibility in tests
INVALID_BRANCH_PATTERNS = [rule[1] for rule in _BRANCH_VALIDATION_RULES]


# =============================================================================
# Validation
# =============================================================================


def validate_branch_name(branch: str) -> None:
    """Validate branch name to prevent injection and ensure git compatibility.

    Security considerations:
    - Prevents flag injection (branch names starting with -)
    - Enforces git-check-ref-format rules
    - Prevents control character injection
    - Limits length to prevent filesystem issues

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


# =============================================================================
# Branch Operations
# =============================================================================


def get_branch_name(repo: Repo) -> Optional[str]:
    """Get active branch name, or None if detached HEAD."""
    try:
        if repo.head.is_detached:
            return None
        return repo.active_branch.name
    except Exception as e:
        log_debug(f"[PRIMITIVES] Error getting branch name: {e}")
        return None


def is_detached_head(repo: Repo) -> bool:
    """Check if repo is in detached HEAD state."""
    try:
        return repo.head.is_detached
    except Exception as e:
        log_debug(f"[PRIMITIVES] Error checking detached HEAD: {e}")
        return True  # Assume detached if we can't check


def is_dirty(repo: Repo, untracked: bool = True) -> bool:
    """Check if working tree has uncommitted changes.

    Args:
        repo: Git repository
        untracked: Whether to include untracked files in the check

    Returns:
        True if there are uncommitted changes
    """
    try:
        return repo.is_dirty(untracked_files=untracked)
    except Exception as e:
        log_debug(f"[PRIMITIVES] Error checking dirty state: {e}")
        return False


def _is_rebase_state(repo: Repo) -> bool:
    """Check if a git rebase (not merge) is in progress."""
    git_dir = Path(repo.git_dir)
    return (
        (git_dir / "rebase-merge").exists()
        or (git_dir / "rebase-apply").exists()
    )


def _is_merge_state(repo: Repo) -> bool:
    """Check if a git merge is in progress."""
    return (Path(repo.git_dir) / "MERGE_HEAD").exists()


def is_rebase_in_progress(repo: Repo) -> bool:
    """Check if a rebase or merge is in progress."""
    return _is_rebase_state(repo) or _is_merge_state(repo)


def has_conflicts(repo: Repo) -> bool:
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


def branch_exists_on_origin(repo: Repo, branch: str) -> bool:
    """Check if branch exists on origin.

    Args:
        repo: Git repository
        branch: Branch name (validated for safety)

    Returns:
        True if branch exists on origin, False otherwise
    """
    try:
        validate_branch_name(branch)
    except ValueError as e:
        log_debug(f"[PRIMITIVES] Invalid branch name in branch_exists_on_origin: {e}")
        return False

    try:
        origin = repo.remote("origin")
        return f"origin/{branch}" in [ref.name for ref in origin.refs]
    except Exception:
        return False


def get_ahead_behind(repo: Repo, branch: str) -> tuple[int, int]:
    """Get commits ahead/behind origin for a branch.

    Args:
        repo: Git repository
        branch: Branch name (validated for safety)

    Returns:
        Tuple of (ahead, behind) commit counts
    """
    try:
        validate_branch_name(branch)
    except ValueError as e:
        log_debug(f"[PRIMITIVES] Invalid branch name in get_ahead_behind: {e}")
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
        log_debug(f"[PRIMITIVES] Error getting ahead/behind: {e}")
        return (0, 0)


# =============================================================================
# Fetch/Pull/Push Operations
# =============================================================================


def fetch_with_timeout(repo: Repo, timeout: int = 30) -> bool:
    """Fetch from origin with timeout.

    Args:
        repo: Git repository
        timeout: Timeout in seconds

    Returns:
        True on success, False on failure
    """
    import time as _time
    t0 = _time.perf_counter()
    try:
        repo.git.fetch("origin", kill_after_timeout=timeout)
        log_action("sync.fetch", outcome="ok", duration_ms=(_time.perf_counter() - t0) * 1000)
        return True
    except Exception as e:
        log_action("sync.fetch", outcome="error", duration_ms=(_time.perf_counter() - t0) * 1000, error=str(e)[:200])
        log_debug(f"[PRIMITIVES] Fetch failed: {e}")
        return False


def pull_ff_only(repo: Repo, branch: Optional[str] = None) -> bool:
    """Pull with --ff-only.

    Args:
        repo: Git repository
        branch: Optional branch name. If provided, pulls from origin/branch explicitly.
                This allows pull to work even without upstream tracking configured.

    Returns:
        True on success, False on failure
    """
    import time as _time
    t0 = _time.perf_counter()
    try:
        if branch:
            validate_branch_name(branch)
            # Note: --ff-only must come before origin/branch for git pull
            repo.git.pull("--ff-only", "origin", branch)
        else:
            repo.git.pull("--ff-only")
        log_action("sync.pull_ff", outcome="ok", duration_ms=(_time.perf_counter() - t0) * 1000)
        return True
    except (GitCommandError, ValueError) as e:
        log_action("sync.pull_ff", outcome="diverged", duration_ms=(_time.perf_counter() - t0) * 1000)
        log_debug(f"[PRIMITIVES] FF-only pull failed: {e}")
        return False


def abort_rebase(repo: Repo) -> bool:
    """Abort an in-progress rebase or merge to restore clean worktree state.

    Handles both rebase (rebase-merge / rebase-apply) and merge (MERGE_HEAD)
    states, using the appropriate abort command for each.

    After rebase abort, HEAD returns to ORIG_HEAD (the pre-rebase local
    branch tip). After merge abort, HEAD returns to the pre-merge state.
    All local commits that existed before the operation are preserved.

    Returns:
        True if aborted successfully or nothing was in progress (callers
        can proceed safely in either case). False only on abort failure.
    """
    if _is_rebase_state(repo):
        try:
            repo.git.rebase("--abort")
            log_warning("[PRIMITIVES] Aborted in-progress rebase")
            log_action("sync.abort_rebase", outcome="ok", state="rebase")
            return True
        except GitCommandError as e:
            log_warning(f"[PRIMITIVES] Failed to abort rebase: {e}")
            log_action("sync.abort_rebase", outcome="error", state="rebase", error=str(e)[:200])
            return False

    if _is_merge_state(repo):
        try:
            repo.git.merge("--abort")
            log_warning("[PRIMITIVES] Aborted in-progress merge")
            log_action("sync.abort_rebase", outcome="ok", state="merge")
            return True
        except GitCommandError as e:
            log_warning(f"[PRIMITIVES] Failed to abort merge: {e}")
            log_action("sync.abort_rebase", outcome="error", state="merge", error=str(e)[:200])
            return False

    return True  # Nothing to abort


def pull_rebase(repo: Repo, branch: Optional[str] = None) -> bool:
    """Pull with --rebase.

    If the rebase fails (e.g. due to conflicts), automatically aborts
    to leave the worktree in a clean state. Without the abort, subsequent
    writes succeed locally but all pushes fail silently.

    Args:
        repo: Git repository
        branch: Optional branch name. If provided, pulls from origin/branch explicitly.
                This allows pull to work even without upstream tracking configured.

    Returns:
        True on success, False on failure
    """
    try:
        if branch:
            validate_branch_name(branch)
            # Note: --rebase must come before origin/branch for git pull
            repo.git.pull("--rebase", "origin", branch)
        else:
            repo.git.pull("--rebase")
        return True
    except (GitCommandError, ValueError) as e:
        log_debug(f"[PRIMITIVES] Rebase pull failed: {e}")
        # CRITICAL: abort the rebase to prevent stuck worktree state.
        # Without this, the worktree stays in rebase-in-progress and all
        # subsequent pushes fail silently (commits land locally but never
        # reach the remote).
        if not abort_rebase(repo):
            log_warning("[PRIMITIVES] Failed to abort rebase — worktree may be in broken state")
        return False


def push_with_retry(
    repo: Repo,
    branch: Optional[str] = None,
    max_retries: int = MAX_PUSH_RETRIES,
    set_upstream: bool = False,
) -> bool:
    """Push to origin with retry on rejection.

    On non-fast-forward rejection, attempts pull --rebase then retries push.

    Args:
        repo: Git repository
        branch: Branch name to push (validated for safety).
                If None, uses the active branch.
        max_retries: Maximum retry attempts
        set_upstream: If True, use -u flag to set upstream tracking

    Returns:
        True on success, False on failure
    """
    # Pre-flight: recover from stuck rebase or merge left by a previous
    # failed push. Must run BEFORE active_branch.name — a stuck rebase
    # leaves HEAD detached, which causes active_branch.name to raise TypeError.
    if _is_rebase_state(repo) or _is_merge_state(repo):
        # Warn if HEAD has moved beyond ORIG_HEAD during the stuck state.
        # ORIG_HEAD is set by git before the rebase starts and points to
        # the original local branch tip. After abort, HEAD returns to
        # ORIG_HEAD. Any commits between ORIG_HEAD and the current
        # detached HEAD that are NOT ancestors of the upstream are
        # user-authored entries written during the stuck state.
        # We log the full HEAD SHA so the user can recover via reflog
        # if needed — range-based cherry-pick commands are unreliable
        # here because the rebase topology makes ranges ambiguous.
        if _is_rebase_state(repo):
            try:
                head_sha = repo.git.rev_parse("HEAD")
                log_warning(
                    f"[PRIMITIVES] Pre-abort HEAD: {head_sha} "
                    f"(recoverable via git reflog if commits are orphaned)"
                )
            except GitCommandError:
                pass

        log_warning("[PRIMITIVES] Detected stuck rebase/merge before push, aborting to recover")
        if not abort_rebase(repo):
            log_warning("[PRIMITIVES] Could not abort stuck state, push will fail")
            return False

    if branch is None:
        try:
            branch = repo.active_branch.name
        except TypeError:
            log_debug("[PRIMITIVES] HEAD is detached and no branch specified, cannot push")
            return False
    try:
        validate_branch_name(branch)
    except ValueError as e:
        log_debug(f"[PRIMITIVES] Invalid branch name for push: {e}")
        return False

    import time as _time
    for attempt in range(max_retries):
        t0 = _time.perf_counter()
        try:
            if set_upstream:
                repo.git.push("-u", "origin", branch)
            else:
                repo.git.push("origin", branch)
            log_action("sync.push", outcome="ok", attempt=attempt + 1, duration_ms=(_time.perf_counter() - t0) * 1000)
            return True
        except GitCommandError as e:
            error_text = str(e).lower()
            if "rejected" in error_text or "non-fast-forward" in error_text:
                log_action("sync.push", outcome="rejected", attempt=attempt + 1, duration_ms=(_time.perf_counter() - t0) * 1000)
                # Try pull --rebase then retry
                log_debug(
                    f"[PRIMITIVES] Push rejected, attempting pull --rebase (attempt {attempt + 1})"
                )
                if pull_rebase(repo, branch):
                    continue  # Retry push
                else:
                    return False
            else:
                log_action("sync.push", outcome="error", attempt=attempt + 1, duration_ms=(_time.perf_counter() - t0) * 1000, error=str(e)[:200])
                log_debug(f"[PRIMITIVES] Push failed: {e}")
                return False
    log_action("sync.push", outcome="exhausted", attempts=max_retries)
    return False


# =============================================================================
# Checkout Operations
# =============================================================================


def checkout_branch(
    repo: Repo, branch: str, create: bool = False, set_upstream: bool = True
) -> bool:
    """Checkout a branch, optionally creating it with upstream tracking.

    Args:
        repo: Git repository
        branch: Branch name (validated for safety)
        create: If True, create the branch with -b flag
        set_upstream: If True and remote branch exists, configure upstream tracking

    Returns:
        True on success, False on failure
    """
    try:
        validate_branch_name(branch)
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
                log_debug(f"[PRIMITIVES] Created branch {branch} tracking {remote_ref}")
            else:
                repo.git.checkout("-b", branch)
                log_debug(f"[PRIMITIVES] Created branch {branch} (no remote to track)")
        else:
            repo.git.checkout(branch)
            # Set upstream if exists and not already set
            if set_upstream and has_remote:
                try:
                    tracking = repo.active_branch.tracking_branch()
                    if tracking is None:
                        repo.git.branch("--set-upstream-to", remote_ref)
                        log_debug(
                            f"[PRIMITIVES] Set upstream tracking for {branch} -> {remote_ref}"
                        )
                except Exception as e:
                    log_debug(f"[PRIMITIVES] Could not set upstream (non-fatal): {e}")
        return True
    except ValueError as e:
        log_debug(f"[PRIMITIVES] Invalid branch name: {e}")
        return False
    except Exception as e:
        log_debug(f"[PRIMITIVES] Checkout failed: {e}")
        return False


# =============================================================================
# Stash Operations
# =============================================================================


def detect_stash(repo: Repo) -> bool:
    """Check if repo has any stashed changes."""
    try:
        stash_list = repo.git.stash("list")
        return bool(stash_list.strip())
    except GitCommandError:
        return False


def stash_changes(repo: Repo, prefix: str = "watercooler-auto") -> Optional[str]:
    """Stash changes with timestamped message.

    Data-safety invariant: Never drops stash on conflict. Stash ref preserved
    in error messages for manual recovery.

    Args:
        repo: Git repository
        prefix: Prefix for stash message

    Returns:
        Stash message/ref if changes were stashed, None if nothing to stash

    Raises:
        GitCommandError: If stash operation fails
    """
    if not repo.is_dirty(untracked_files=True):
        return None

    stash_msg = f"{prefix}-{datetime.now(timezone.utc).isoformat()}"
    try:
        result = repo.git.stash("push", "-m", stash_msg, "--include-untracked")
        if "No local changes" in result:
            return None
        log_debug(f"[PRIMITIVES] Stashed changes: {stash_msg}")
        return stash_msg
    except GitCommandError as e:
        log_debug(f"[PRIMITIVES] Failed to stash: {e}")
        raise


def get_parity_state(repo: Repo, branch: Optional[str] = None) -> str:
    """Determine the canonical sync parity state of a worktree.

    Returns one of:
        - ``clean``: up-to-date, no dirty files
        - ``behind_only``: behind remote, worktree clean
        - ``ahead_only``: ahead of remote, worktree clean
        - ``diverged``: both ahead and behind remote
        - ``dirty_derived_only``: dirty files are all derived caches
        - ``dirty_mixed``: dirty files include non-derived content
        - ``stuck_rebase_or_merge``: rebase or merge in progress
        - ``no_upstream``: no remote tracking configured
        - ``auth_or_network_error``: cannot reach remote

    Args:
        repo: Git repository
        branch: Branch name (uses active branch if None)
    """
    from watercooler.sync_repair import DERIVED_FILE_PATTERNS

    # Check stuck rebase/merge first
    if is_rebase_in_progress(repo):
        return "stuck_rebase_or_merge"

    # Check for conflict markers left in the index after a rebase/merge that
    # ended without REBASE_HEAD / MERGE_HEAD (e.g. stash-pop conflict).
    # Must run before the dirty-file classifier, which only inspects filenames
    # and would misclassify a conflicted derived file as dirty_derived_only.
    if has_conflicts(repo):
        return "stuck_rebase_or_merge"

    # Resolve branch
    if branch is None:
        branch = get_branch_name(repo)
    if not branch:
        return "no_upstream"

    # Check dirty state
    dirty = is_dirty(repo, untracked=True)
    dirty_derived = False
    if dirty:
        try:
            status_out = repo.git.status("--porcelain")
            all_derived = True
            for line in status_out.strip().split("\n"):
                if not line.strip():
                    continue
                filename = line[3:].split(" -> ")[-1].strip()
                basename = Path(filename).name if filename else ""
                if basename not in DERIVED_FILE_PATTERNS:
                    all_derived = False
                    break
            dirty_derived = all_derived
        except Exception:
            pass

    # Check ahead/behind
    try:
        validate_branch_name(branch)
        remote_ref = f"origin/{branch}"
        try:
            repo.commit(remote_ref)
        except Exception:
            return "no_upstream"

        ahead = len(list(repo.iter_commits(f"{remote_ref}..{branch}")))
        behind = len(list(repo.iter_commits(f"{branch}..{remote_ref}")))
    except Exception as e:
        err_str = str(e).lower()
        if "auth" in err_str or "permission" in err_str or "network" in err_str:
            return "auth_or_network_error"
        return "no_upstream"

    # Classify — structural divergence takes priority over dirty caches.
    # dirty_derived_only is auto-cleanable and must not mask divergence.
    diverged = ahead > 0 and behind > 0

    if dirty:
        if diverged:
            return "diverged"
        if dirty_derived:
            return "dirty_derived_only"
        return "dirty_mixed"

    if diverged:
        return "diverged"
    if behind > 0:
        return "behind_only"
    if ahead > 0:
        return "ahead_only"

    return "clean"


def restore_stash(repo: Repo, stash_ref: Optional[str] = None) -> bool:
    """Restore the most recent stash using apply-first semantics.

    Data-safety invariants:
    - Use `stash apply` (not pop) so conflicts retain the stash.
    - Drop the stash only after a successful apply.

    Args:
        repo: Git repository
        stash_ref: Optional stash reference (for logging only)

    Returns:
        True on success, False on failure (stash preserved on failure)
    """
    if stash_ref is None:
        return True  # Nothing to restore

    try:
        repo.git.stash("apply")
        try:
            repo.git.stash("drop")
        except GitCommandError as drop_err:
            log_debug(f"[PRIMITIVES] Stash applied but drop failed (stash may remain): {drop_err}")
        log_debug(f"[PRIMITIVES] Restored stash: {stash_ref}")
        return True
    except GitCommandError as e:
        # Stash apply failed (likely conflict); stash is preserved for manual handling
        log_debug(f"[PRIMITIVES] Stash apply failed (stash preserved): {e}")
        return False
