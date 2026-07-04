"""Sync repair command for diagnosing and fixing orphan branch sync issues.

Capabilities:
- Diagnose: report branch state, ahead/behind, stuck rebase, dirty files,
  stale locks, recovery log, global artifact status
- Fix: abort stuck rebase/merge, break stale locks, recover local-only
  commits by pushing/rebasing them onto the remote (preserve-first — the
  default path never discards committed work), regenerate derived files
- Discard: opt-in (``discard_local_commits``) destructive reset that drops
  local-only commits after logging them to the recovery log
- Migrate: one-time cleanup of globally-committed derived files
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from watercooler.baseline_graph.storage import (
    get_graph_dir,
    load_manifest,
    rebuild_manifest_from_scan,
)
from watercooler.sync_common import (
    WORKTREE_LOCK_DIR,
    WORKTREE_LOCK_TTL_SECONDS,
    acquire_worktree_lock,
    is_safe_to_auto_recover,
    load_recovery_log,
    log_local_only_commits,
)

# Derived file patterns that can be safely discarded from a dirty worktree.
# These files are caches/derived state rebuilt on demand — they should never
# block sync operations or prevent reset.
DERIVED_FILE_PATTERNS = frozenset({
    "annotation_state.json",
})

# Derived directory prefixes (repo-relative, forward-slash separators). Any
# tracked file under one of these paths is a regenerable projection — treated
# the same as DERIVED_FILE_PATTERNS but matched by path prefix rather than
# basename, because these are directories of per-topic files whose basenames
# vary (e.g. Slack channel mappings written by the Slack integration).
DERIVED_PATH_PREFIXES = frozenset({
    ".watercooler/slack-mappings/",
})


def is_derived_file(rel_path: str) -> bool:
    """Return True if a repo-relative path is a derived/regenerable cache.

    Matches either a basename in :data:`DERIVED_FILE_PATTERNS` or a path under
    any prefix in :data:`DERIVED_PATH_PREFIXES`. Such files can be safely
    discarded from a dirty worktree to unblock sync — they are rebuilt on
    demand and are never the canonical record.

    Args:
        rel_path: Repo-relative path (forward-slash or OS separators).
    """
    if not rel_path:
        return False
    normalized = rel_path.replace("\\", "/")
    if Path(normalized).name in DERIVED_FILE_PATTERNS:
        return True
    return any(normalized.startswith(prefix) for prefix in DERIVED_PATH_PREFIXES)


def is_untracked_write_once_projection(status: str, rel_path: str) -> bool:
    """Return True if a dirty entry is an **untracked** write-once projection.

    These are files matched only by a :data:`DERIVED_PATH_PREFIXES` prefix (e.g.
    the Slack ``slack-mappings`` files, written by the integration *after* the
    entry commit and committed lazily by the next write / committer pass) that
    are not yet tracked in git. While untracked, such a file is the *sole* copy
    of state not yet pushed to origin — the sync heal must not discard it blindly,
    because the clean path can only restore *tracked* files from the index.

    A tracked-modified projection is always restorable from origin (that churn is
    exactly what the parity heal targets), and basename caches
    (:data:`DERIVED_FILE_PATTERNS`, e.g. ``annotation_state.json``) are locally
    regenerable — so neither is an untracked write-once projection here.

    Args:
        status: The two-char ``XY`` field from ``git status --porcelain``
            (``"??"`` marks an untracked file).
        rel_path: Repo-relative path of the entry.
    """
    if status.strip() != "??":
        return False
    normalized = rel_path.replace("\\", "/")
    matched_by_basename = Path(normalized).name in DERIVED_FILE_PATTERNS
    matched_by_prefix = any(
        normalized.startswith(prefix) for prefix in DERIVED_PATH_PREFIXES
    )
    return matched_by_prefix and not matched_by_basename


def _parse_porcelain_filename(entry: str) -> str:
    """Extract the filename from a git status --porcelain line.

    Handles both raw porcelain (``XY filename``, 3-char prefix with space
    at index 2) and pre-stripped lines from ``diagnose()`` where leading
    spaces have been removed. Also handles renames (``XY old -> new``).

    Returns the (new) filename, or empty string on parse failure.
    """
    if len(entry) < 3:
        return ""
    # Raw porcelain: "XY filename" — X is index/staging, Y is worktree,
    # space at index 2, filename at index 3+.
    # Pre-stripped (" M file" → "M file"): status chars then space then file.
    # Detect raw porcelain by checking if position 2 is a space.
    if len(entry) > 3 and entry[2] == " ":
        filename = entry[3:].strip()
    else:
        # Pre-stripped: find first space separating status from filename
        idx = entry.find(" ")
        if idx < 0:
            return ""
        filename = entry[idx + 1:].strip()
    # Handle renames: "old -> new"
    if " -> " in filename:
        filename = filename.split(" -> ")[-1].strip()
    return filename


@dataclass
class DiagnosticReport:
    """Report from sync-repair --diagnose."""
    branch: Optional[str] = None
    tracking: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    stuck_rebase: bool = False
    stuck_merge: bool = False
    dirty_files: List[str] = field(default_factory=list)
    stale_locks: List[str] = field(default_factory=list)
    recovery_log_entries: int = 0
    recoverable_entries: int = 0
    has_global_manifest: bool = False
    has_global_search_index: bool = False
    has_global_sync_state: bool = False
    remotes: List[str] = field(default_factory=list)
    published_remotes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "branch": self.branch,
            "tracking": self.tracking,
            "ahead": self.ahead,
            "behind": self.behind,
            "stuck_rebase": self.stuck_rebase,
            "stuck_merge": self.stuck_merge,
            "dirty_files": self.dirty_files,
            "stale_locks": self.stale_locks,
            "recovery_log_entries": self.recovery_log_entries,
            "recoverable_entries": self.recoverable_entries,
            "has_global_manifest": self.has_global_manifest,
            "has_global_search_index": self.has_global_search_index,
            "has_global_sync_state": self.has_global_sync_state,
            "remotes": self.remotes,
            "published_remotes": self.published_remotes,
            "errors": self.errors,
        }

    @property
    def dirty_derived_only(self) -> bool:
        """True when all dirty files are derived caches (safe to auto-clean)."""
        if not self.dirty_files:
            return False
        for entry in self.dirty_files:
            filename = _parse_porcelain_filename(entry)
            if not is_derived_file(filename):
                return False
        return True

    @property
    def parity_state(self) -> str:
        """Map diagnostic fields to the canonical parity vocabulary.

        Returns the same string literals as
        ``watercooler_mcp.sync.primitives.get_parity_state()``.
        """
        if self.stuck_rebase or self.stuck_merge:
            return "stuck_rebase_or_merge"
        if self.errors:
            # Errors from diagnose() are git subprocess failures — typically
            # auth or network.  Matches get_parity_state() vocabulary even
            # though rare local failures (permissions, corrupt objects) would
            # also land here.
            return "auth_or_network_error"
        if not self.tracking:
            return "no_upstream"
        if self.ahead > 0 and self.behind > 0:
            return "diverged"
        if self.dirty_files:
            return "dirty_derived_only" if self.dirty_derived_only else "dirty_mixed"
        if self.behind > 0:
            return "behind_only"
        if self.ahead > 0:
            return "ahead_only"
        return "clean"

    @property
    def needs_repair(self) -> bool:
        return (
            self.stuck_rebase
            or self.stuck_merge
            or bool(self.stale_locks)
            or self.ahead > 0
            or self.behind > 0
            or bool(self.dirty_files)
            or self.parity_state == "no_upstream"
        )


def diagnose(threads_dir: Path) -> DiagnosticReport:
    """Diagnose the state of the orphan branch worktree.

    Args:
        threads_dir: Threads repository directory

    Returns:
        DiagnosticReport with findings
    """
    report = DiagnosticReport()
    td = str(threads_dir)

    if not (threads_dir / ".git").exists():
        report.errors.append("Not a git repository")
        return report

    # Branch info
    try:
        result = subprocess.run(
            ["git", "-C", td, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            report.branch = result.stdout.strip()
    except Exception as e:
        report.errors.append(f"Branch check failed: {e}")

    # Tracking branch
    try:
        result = subprocess.run(
            ["git", "-C", td, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            report.tracking = result.stdout.strip()
    except Exception:
        pass

    # Ahead/behind
    if report.tracking:
        try:
            result = subprocess.run(
                ["git", "-C", td, "rev-list", "--count", "--left-right", f"{report.tracking}...HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                if len(parts) == 2:
                    report.behind = int(parts[0])
                    report.ahead = int(parts[1])
        except Exception:
            pass

    # Stuck rebase/merge
    from watercooler.cli import _resolve_git_dir
    git_dir = _resolve_git_dir(threads_dir)
    report.stuck_rebase = (
        (git_dir / "rebase-merge").exists()
        or (git_dir / "rebase-apply").exists()
    )
    report.stuck_merge = (git_dir / "MERGE_HEAD").exists()

    # Dirty files
    try:
        result = subprocess.run(
            ["git", "-C", td, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            report.dirty_files = [
                line.strip() for line in result.stdout.strip().split("\n")
                if line.strip()
            ]
    except Exception:
        pass

    # Stale locks
    lock_dir = threads_dir / WORKTREE_LOCK_DIR / "locks"
    if lock_dir.exists():
        now = time.time()
        for lock_file in lock_dir.iterdir():
            if lock_file.suffix == ".lock":
                try:
                    age = now - lock_file.stat().st_mtime
                    # Both topic locks (sync/__init__.py LOCK_TTL_SECONDS)
                    # and worktree locks use 120s TTL.
                    if age > WORKTREE_LOCK_TTL_SECONDS:
                        report.stale_locks.append(f"{lock_file.name} (age: {age:.0f}s)")
                except OSError:
                    pass

    # Recovery log
    recovery_entries = load_recovery_log(threads_dir)
    report.recovery_log_entries = len(recovery_entries)
    report.recoverable_entries = sum(
        1 for e in recovery_entries if is_safe_to_auto_recover(e)
    )

    # Global artifact status
    graph_dir = get_graph_dir(threads_dir)
    report.has_global_manifest = (graph_dir / "manifest.json").exists()
    report.has_global_search_index = (graph_dir / "search-index.jsonl").exists()
    report.has_global_sync_state = (graph_dir / "sync_state.json").exists()

    # Remote context — which remotes exist, and which already carry the
    # thread branch. Uses local refs only (no ls-remote) so diagnose()
    # stays network-free; the published-remotes view may be as stale as
    # the last fetch, which is acceptable for a diagnostic.
    try:
        result = subprocess.run(
            ["git", "-C", td, "remote"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            report.remotes = [
                r.strip() for r in result.stdout.splitlines() if r.strip()
            ]
    except Exception:
        pass
    if report.branch:
        suffix = "/" + report.branch
        try:
            result = subprocess.run(
                ["git", "-C", td, "branch", "-r", "--list", f"*{suffix}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                pub = set()
                for line in result.stdout.splitlines():
                    ref = line.strip()
                    if ref and " -> " not in ref and ref.endswith(suffix):
                        pub.add(ref[: -len(suffix)])
                report.published_remotes = sorted(pub)
        except Exception:
            pass

    return report


def _recover_local_only(threads_dir: Path, report: DiagnosticReport) -> List[str]:
    """Preserve local-only commits by pushing them to the remote.

    Mirrors the write path (``middleware.run_with_sync``): reuse
    ``sync.primitives.push_with_retry``, which rebases onto the remote on a
    non-fast-forward rejection and retries. That recovers both ``ahead_only``
    (clean push) and ``diverged`` (rebase-then-push) without ever discarding
    committed work. On failure the commits stay intact in the local worktree.
    """
    n = report.ahead
    try:
        from git import Repo

        from watercooler_mcp.sync.primitives import push_with_retry
    except ImportError as e:  # pragma: no cover - defensive
        return [f"FAILED: Recover {n} local-only commit(s): {e}"]

    err = ""
    try:
        repo = Repo(threads_dir)
        pushed = push_with_retry(repo, branch=report.branch)
    except Exception as e:
        pushed = False
        err = str(e)[:200]

    if pushed:
        return [f"Recover {n} local-only commit(s): pushed to {report.tracking}"]
    detail = f": {err}" if err else ""
    return [
        f"FAILED: Recover {n} local-only commit(s){detail}. Commits are intact "
        f"locally — fix the push side (auth/network) and re-run, or pass "
        f"discard_local_commits=True to drop them."
    ]


def _discard_local_only(threads_dir: Path, report: DiagnosticReport) -> List[str]:
    """Destructively reset to the tracking ref, discarding local-only commits.

    Opt-in only (``discard_local_commits=True``). Every discarded commit is
    written to ``.watercooler/recovery.jsonl`` first; the reset is aborted if
    that recovery log cannot be completed.
    """
    td = str(threads_dir)
    action = f"Discard {report.ahead} local-only commit(s) (reset to {report.tracking})"
    if not log_local_only_commits(threads_dir, report.tracking):
        return [f"FAILED: {action}: recovery log incomplete, aborting reset"]
    result = subprocess.run(
        ["git", "-C", td, "reset", "--hard", report.tracking],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return [f"{action} — discarded commits logged to .watercooler/recovery.jsonl"]
    return [f"FAILED: {action}: {result.stderr.strip()[:200]}"]


def _publish_orphan_branch(
    threads_dir: Path, report: DiagnosticReport, remote: str
) -> List[str]:
    """Publish the thread branch to a named remote and set its upstream.

    The opt-in repair for an orphan branch that has no usable remote
    (issue #689): pushes the current branch to ``remote`` with ``-u`` so
    subsequent pushes have a tracking target.
    """
    td = str(threads_dir)
    branch = report.branch
    if not branch:
        return [f"FAILED: publish to '{remote}': current branch is unknown"]
    if remote not in report.remotes:
        avail = ", ".join(report.remotes) or "(none configured)"
        return [
            f"FAILED: publish '{branch}' to '{remote}': no such remote "
            f"(configured: {avail})"
        ]
    result = subprocess.run(
        ["git", "-C", td, "push", "-u", remote, branch],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        return [f"Published '{branch}' to '{remote}' and set upstream tracking"]
    return [
        f"FAILED: publish '{branch}' to '{remote}': "
        f"{result.stderr.strip()[:200]}"
    ]


def suggest_publish_remote(
    remotes: List[str], published_remotes: List[str]
) -> Optional[str]:
    """Pick the best remote to (re)publish the thread branch to.

    Prefers the sole remote that already carries the branch — republishing
    there repairs tracking instead of forking thread history onto a second
    remote. When the branch is already on *several* remotes the target is
    genuinely ambiguous and None is returned. Only when the branch is on no
    remote does it fall back to ``origin``, then a lone remote; None when
    that too is ambiguous (operator must choose).
    """
    if len(published_remotes) == 1:
        return published_remotes[0]
    if published_remotes:
        # Already on multiple remotes — ambiguous; do not nudge the
        # operator to fork yet another copy onto origin.
        return None
    if "origin" in remotes:
        return "origin"
    if len(remotes) == 1:
        return remotes[0]
    return None


def repair(
    threads_dir: Path,
    dry_run: bool = False,
    regenerate_cache: bool = False,
    migrate: bool = False,
    discard_local_commits: bool = False,
    publish_remote: str = "",
) -> List[str]:
    """Fix common sync issues.

    Args:
        threads_dir: Threads repository directory
        dry_run: If True, only report what would be done
        regenerate_cache: If True, rebuild manifest + search-index from per-thread data
        migrate: If True, perform one-time cleanup of globally-committed derived files
        discard_local_commits: If True, local-only commits are destructively
            reset away (after recovery-log capture) instead of recovered by
            push/rebase. Default False — the safe, preserve-first path.
        publish_remote: If set, publish the thread branch to this remote and
            set upstream tracking — the opt-in repair for an orphan branch
            with no usable remote (issue #689).

    Returns:
        List of actions taken (or would-be-taken if dry_run)
    """
    actions: List[str] = []
    td = str(threads_dir)

    if not (threads_dir / ".git").exists():
        return ["ERROR: Not a git repository"]

    report = diagnose(threads_dir)

    # Publish the thread branch to a named remote — the opt-in repair for
    # an orphan branch with no usable upstream (issue #689). Explicit: the
    # caller chooses the remote.
    if publish_remote:
        if dry_run:
            actions.append(
                f"[DRY RUN] Publish '{report.branch}' to remote '{publish_remote}'"
            )
        else:
            actions.extend(
                _publish_orphan_branch(threads_dir, report, publish_remote)
            )

    # Fix stuck rebase/merge (requires worktree lock — mutating git state)
    if (report.stuck_rebase or report.stuck_merge) and not dry_run:
        try:
            abort_wt_lock = acquire_worktree_lock(threads_dir)
        except TimeoutError:
            actions.append("FAILED: Abort stuck rebase/merge: worktree lock timeout")
            abort_wt_lock = None

        if abort_wt_lock:
            try:
                if report.stuck_rebase:
                    result = subprocess.run(
                        ["git", "-C", td, "rebase", "--abort"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.returncode == 0:
                        actions.append("Abort stuck rebase")
                    else:
                        actions.append(f"FAILED: Abort stuck rebase: {result.stderr.strip()[:200]}")
                if report.stuck_merge:
                    result = subprocess.run(
                        ["git", "-C", td, "merge", "--abort"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.returncode == 0:
                        actions.append("Abort stuck merge")
                    else:
                        actions.append(f"FAILED: Abort stuck merge: {result.stderr.strip()[:200]}")
            finally:
                abort_wt_lock.release()
    else:
        if report.stuck_rebase and dry_run:
            actions.append("[DRY RUN] Abort stuck rebase")
        if report.stuck_merge and dry_run:
            actions.append("[DRY RUN] Abort stuck merge")

    # Break stale locks
    if report.stale_locks:
        lock_dir = threads_dir / WORKTREE_LOCK_DIR / "locks"
        for lock_info in report.stale_locks:
            lock_name = lock_info.split(" ")[0]
            lock_path = lock_dir / lock_name
            action = f"Break stale lock: {lock_name}"
            if dry_run:
                actions.append(f"[DRY RUN] {action}")
            else:
                try:
                    lock_path.unlink(missing_ok=True)
                    actions.append(action)
                except OSError as e:
                    actions.append(f"FAILED: {action}: {e}")

    # Auto-clean derived-only dirt (annotation_state.json etc.)
    # These are disposable caches that can be safely deleted — they block
    # pulls and prevent reset, causing chronic sync divergence.
    if report.dirty_files and report.dirty_derived_only:
        action_desc = f"Auto-clean {len(report.dirty_files)} derived cache file(s)"
        if dry_run:
            actions.append(f"[DRY RUN] {action_desc}")
        else:
            cleaned = 0
            for entry in report.dirty_files:
                filename = _parse_porcelain_filename(entry)
                if not filename:
                    continue
                filepath = threads_dir / filename
                try:
                    if filepath.exists():
                        filepath.unlink()
                        cleaned += 1
                    # Also remove from git index if tracked
                    subprocess.run(
                        ["git", "-C", td, "checkout", "--", filename],
                        capture_output=True, text=True, timeout=5,
                    )
                except Exception as e:
                    actions.append(f"WARNING: Could not clean {filename}: {e}")
            if cleaned > 0:
                actions.append(f"{action_desc} ({cleaned} removed)")
            # Re-diagnose after cleaning to get updated state
            report = diagnose(threads_dir)

    # Fetch so the tracking ref is current before recovering local commits
    if report.ahead > 0 and report.tracking and not dry_run:
        subprocess.run(
            ["git", "-C", td, "fetch", "origin"],
            capture_output=True, text=True, timeout=30,
        )

    # Handle local-only commits — preserve-first, mirroring the write path.
    # Default: push them to the remote (push_with_retry rebases onto remote
    # when diverged). Destructive reset is opt-in only via discard_local_commits.
    if report.ahead > 0 and report.tracking:
        n = report.ahead
        verb = (
            f"Discard {n} local-only commit(s) (reset --hard to {report.tracking})"
            if discard_local_commits
            else f"Recover {n} local-only commit(s) by pushing to {report.tracking}"
        )
        if dry_run:
            actions.append(f"[DRY RUN] {verb}")
        elif report.dirty_files:
            # Match cli.py and middleware.py: don't touch HEAD or push when the
            # worktree has uncommitted changes — resolve those first.
            actions.append(
                f"SKIPPED: {verb}: worktree has {len(report.dirty_files)} "
                f"uncommitted file(s) — resolve those first"
            )
        else:
            try:
                wt_lock = acquire_worktree_lock(threads_dir)
            except TimeoutError:
                actions.append(f"FAILED: {verb}: worktree lock timeout")
                return actions

            try:
                if discard_local_commits:
                    actions.extend(_discard_local_only(threads_dir, report))
                else:
                    actions.extend(_recover_local_only(threads_dir, report))
            finally:
                wt_lock.release()

    # Fast-forward a behind-only worktree. This is the routine drift that
    # sync_guard and the read path already heal — but the MANUAL tool did not,
    # so an operator who reached for sync_repair on a silently-behind worktree
    # got "No issues found" instead of a fix (bug-sync-worktree-poisoning).
    # Fetch first (read-only, outside the lock — like the ahead path) so the
    # behind count reflects the true remote even on a worktree that hasn't
    # fetched recently; re-read state (earlier steps may have broken locks,
    # cleaned derived dirt, or recovered local commits); then ff-merge only when
    # cleanly fast-forwardable: behind, not ahead (diverged ahead is handled
    # above), no dirty files, not mid rebase/merge.
    if not dry_run and report.tracking:
        subprocess.run(
            ["git", "-C", td, "fetch", "origin"],
            capture_output=True, text=True, timeout=30,
        )
    ff_report = diagnose(threads_dir) if not dry_run else report
    if (
        ff_report.behind > 0
        and ff_report.ahead == 0
        and not ff_report.dirty_files
        and not (ff_report.stuck_rebase or ff_report.stuck_merge)
        and ff_report.tracking
    ):
        verb = f"Fast-forward pull {ff_report.behind} commit(s) from {ff_report.tracking}"
        if dry_run:
            actions.append(f"[DRY RUN] {verb}")
        else:
            try:
                wt_lock = acquire_worktree_lock(threads_dir)
            except TimeoutError:
                actions.append(f"FAILED: {verb}: worktree lock timeout")
                return actions
            try:
                result = subprocess.run(
                    ["git", "-C", td, "merge", "--ff-only", ff_report.tracking],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    actions.append(verb)
                else:
                    actions.append(
                        f"FAILED: {verb}: {result.stderr.strip()[:200]}"
                    )
            finally:
                wt_lock.release()

    # Verify manifest scan (manifest is always derived on-demand now)
    if regenerate_cache:
        graph_dir = get_graph_dir(threads_dir)
        manifest = rebuild_manifest_from_scan(graph_dir)
        topic_count = len(manifest.get("topics", {}))
        actions.append(
            f"Manifest scan OK: {topic_count} topic(s) found "
            f"(manifest is now derived on-demand, no cache file written)"
        )

    # Migration: remove globally-committed derived files
    if migrate:
        graph_dir = get_graph_dir(threads_dir)

        # Collect dry-run actions for file removal
        files_to_remove = []
        for filename, desc in [
            ("manifest.json", "manifest"),
            ("sync_state.json", "sync state"),
            ("search-index.jsonl", "global search index"),
        ]:
            filepath = graph_dir / filename
            if filepath.exists():
                action = f"Remove globally-committed {desc} ({filename})"
                if dry_run:
                    actions.append(f"[DRY RUN] {action}")
                else:
                    files_to_remove.append((filepath, action))

        # Remove per-thread annotation_state.json files from tracking.
        # These are derived caches that should never have been committed —
        # they cause chronic dirty_derived_only churn for sync_guard.
        ann_pattern = "graph/baseline/threads/*/annotation_state.json"
        ann_tracked = subprocess.run(
            ["git", "-C", td, "ls-files", ann_pattern],
            capture_output=True, text=True, timeout=10,
        )
        ann_files = [f for f in ann_tracked.stdout.strip().split("\n") if f.strip()]
        if ann_files:
            action = f"Remove {len(ann_files)} tracked annotation_state.json caches"
            if dry_run:
                actions.append(f"[DRY RUN] {action}")
            else:
                rm_result = subprocess.run(
                    ["git", "-C", td, "rm", "--cached", "-f", "--"] + ann_files,
                    capture_output=True, text=True, timeout=30,
                )
                if rm_result.returncode == 0:
                    # Delete from working tree too (they're derived, will rebuild on demand)
                    for af in ann_files:
                        fp = threads_dir / af
                        fp.unlink(missing_ok=True)
                    actions.append(action)
                else:
                    actions.append(f"FAILED: {action}: {rm_result.stderr.strip()[:200]}")

        # Collect dry-run actions for .gitignore
        gitignore_path = threads_dir / ".gitignore"
        gitignore_entries = [
            "graph/baseline/manifest.json",
            "graph/baseline/sync_state.json",
            "graph/baseline/search-index.jsonl",
            "**/annotation_state.json",
            ".watercooler/",
        ]
        gitignore_action = "Update .gitignore with derived file patterns"
        if dry_run:
            actions.append(f"[DRY RUN] {gitignore_action}")

        # Execute migration under worktree lock (git rm, add, commit, push)
        if not dry_run:
            try:
                wt_lock = acquire_worktree_lock(threads_dir)
            except TimeoutError:
                actions.append("FAILED: Migration aborted — worktree lock timeout")
                return actions

            try:
                for filepath, action in files_to_remove:
                    try:
                        # git rm --cached first so failure leaves file intact
                        rm_result = subprocess.run(
                            ["git", "-C", td, "rm", "--cached", "-f",
                             str(filepath.relative_to(threads_dir))],
                            capture_output=True, text=True, timeout=5,
                        )
                        if rm_result.returncode != 0:
                            actions.append(f"FAILED: {action}: git rm failed: {rm_result.stderr.strip()[:200]}")
                            continue
                        filepath.unlink(missing_ok=True)
                        actions.append(action)
                    except Exception as e:
                        actions.append(f"FAILED: {action}: {e}")

                # Update .gitignore
                existing_lines: set = set()
                existing = ""
                if gitignore_path.exists():
                    existing = gitignore_path.read_text(encoding="utf-8")
                    existing_lines = {line.strip() for line in existing.splitlines()}
                new_entries = [e for e in gitignore_entries if e not in existing_lines]
                if new_entries:
                    with open(gitignore_path, "a", encoding="utf-8") as f:
                        if existing and not existing.endswith("\n"):
                            f.write("\n")
                        f.write("\n# Derived cache files (not committed)\n")
                        for entry in new_entries:
                            f.write(f"{entry}\n")
                    actions.append(f"{gitignore_action} ({len(new_entries)} entries)")
                else:
                    actions.append(f"{gitignore_action} (already present)")

                # Invalidate gitignore cache so next lock acquisition re-verifies
                from watercooler.sync_common import invalidate_gitignore_cache
                invalidate_gitignore_cache(threads_dir)

                # Stage, commit, push
                add_gi = subprocess.run(
                    ["git", "-C", td, "add", ".gitignore"],
                    capture_output=True, text=True, timeout=5,
                )
                if add_gi.returncode != 0:
                    actions.append(f"WARNING: git add .gitignore failed: {add_gi.stderr.strip()[:200]}")
                status = subprocess.run(
                    ["git", "-C", td, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5,
                )
                if status.returncode == 0 and status.stdout.strip():
                    result = subprocess.run(
                        ["git", "-C", td, "commit", "-m",
                         "chore: migrate derived files out of git tracking"],
                        capture_output=True, text=True, timeout=15,
                    )
                    if result.returncode == 0:
                        # Push with retry on rejection (matching write path)
                        pushed = False
                        rebase_failed = False
                        for attempt in range(3):
                            push = subprocess.run(
                                ["git", "-C", td, "push"],
                                capture_output=True, text=True, timeout=30,
                            )
                            if push.returncode == 0:
                                pushed = True
                                break
                            err = push.stderr.lower()
                            if "rejected" in err or "non-fast-forward" in err:
                                rebase = subprocess.run(
                                    ["git", "-C", td, "pull", "--rebase"],
                                    capture_output=True, text=True, timeout=30,
                                )
                                if rebase.returncode != 0:
                                    subprocess.run(
                                        ["git", "-C", td, "rebase", "--abort"],
                                        capture_output=True, text=True, timeout=10,
                                    )
                                    actions.append(f"Migration push failed: rebase conflict on attempt {attempt + 1}")
                                    rebase_failed = True
                                    break
                                continue
                            break
                        if pushed:
                            actions.append("Committed and pushed migration changes")
                        elif not rebase_failed:
                            actions.append(f"Migration committed locally but push failed: {push.stderr.strip()[:200]}")
                    else:
                        actions.append(f"Migration commit failed: {result.stderr.strip()[:200]}")
            finally:
                wt_lock.release()

    if not actions:
        actions.append("No issues found")

    return actions


def format_report(report: DiagnosticReport) -> str:
    """Format a diagnostic report for human consumption."""
    lines = ["Sync Repair Diagnosis", "=" * 40]

    lines.append(f"Branch: {report.branch or '(unknown)'}")
    lines.append(f"Tracking: {report.tracking or '(none)'}")
    lines.append(f"Ahead: {report.ahead}  Behind: {report.behind}")
    if report.ahead > 0:
        lines.append(
            f"  -> {report.ahead} local-only commit(s): `watercooler sync-repair` "
            f"recovers these by push/rebase (preserved by default; reset away "
            f"only with discard_local_commits)"
        )
    if report.behind > 0 and report.ahead == 0:
        lines.append(
            f"  -> {report.behind} commit(s) behind: `watercooler sync-repair` "
            f"fast-forwards these (the routine drift sync_guard/reads also heal)"
        )

    if report.parity_state == "no_upstream":
        lines.append("")
        lines.append("!! NO REMOTE UPSTREAM for the thread branch")
        if report.remotes:
            lines.append(f"  Configured remotes: {', '.join(report.remotes)}")
        else:
            lines.append("  No git remotes are configured.")
        if report.published_remotes:
            lines.append(
                f"  '{report.branch}' exists on: "
                f"{', '.join(report.published_remotes)}"
            )
        else:
            lines.append(f"  '{report.branch}' is not published to any remote.")
        _target = suggest_publish_remote(report.remotes, report.published_remotes)
        if _target:
            lines.append(
                f"  -> Publish it: "
                f"watercooler_sync_repair(publish_remote='{_target}')"
            )
        elif report.remotes:
            lines.append(
                "  -> Choose a remote, then: "
                "watercooler_sync_repair(publish_remote='<remote>')"
            )

    if report.stuck_rebase:
        lines.append("!! STUCK REBASE detected")
    if report.stuck_merge:
        lines.append("!! STUCK MERGE detected")

    if report.dirty_files:
        lines.append(f"Dirty files: {len(report.dirty_files)}")
        for f in report.dirty_files[:10]:
            lines.append(f"  {f}")
        if len(report.dirty_files) > 10:
            lines.append(f"  ... and {len(report.dirty_files) - 10} more")

    if report.stale_locks:
        lines.append(f"Stale locks: {len(report.stale_locks)}")
        for lock in report.stale_locks:
            lines.append(f"  {lock}")

    lines.append(f"Recovery log: {report.recovery_log_entries} entries "
                 f"({report.recoverable_entries} auto-recoverable)")

    lines.append("")
    lines.append("Global artifacts:")
    lines.append(f"  manifest.json: {'present' if report.has_global_manifest else 'absent'}")
    lines.append(f"  search-index.jsonl: {'present' if report.has_global_search_index else 'absent'}")
    lines.append(f"  sync_state.json: {'present' if report.has_global_sync_state else 'absent'}")

    if report.errors:
        lines.append("")
        lines.append("Errors:")
        for err in report.errors:
            lines.append(f"  {err}")

    lines.append("")
    if report.needs_repair:
        lines.append("Status: NEEDS REPAIR - run `watercooler sync-repair` to fix")
    else:
        lines.append("Status: OK")

    return "\n".join(lines)
