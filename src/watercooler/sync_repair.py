"""Sync repair command for diagnosing and fixing orphan branch sync issues.

Capabilities:
- Diagnose: report branch state, ahead/behind, stuck rebase, dirty files,
  stale locks, recovery log, global artifact status
- Fix: abort stuck rebase/merge, break stale locks, cherry-pick or discard
  local-only commits, regenerate derived files
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
    is_safe_to_auto_recover,
    load_recovery_log,
)

# Derived file patterns that can be safely discarded from a dirty worktree.
# These files are caches/derived state rebuilt on demand — they should never
# block sync operations or prevent reset.
DERIVED_FILE_PATTERNS = frozenset({
    "annotation_state.json",
})


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
            "errors": self.errors,
        }

    @property
    def dirty_derived_only(self) -> bool:
        """True when all dirty files are derived caches (safe to auto-clean)."""
        if not self.dirty_files:
            return False
        for entry in self.dirty_files:
            filename = _parse_porcelain_filename(entry)
            basename = Path(filename).name if filename else ""
            if basename not in DERIVED_FILE_PATTERNS:
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
            or bool(self.dirty_files)
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

    return report


def repair(
    threads_dir: Path,
    dry_run: bool = False,
    regenerate_cache: bool = False,
    migrate: bool = False,
) -> List[str]:
    """Fix common sync issues.

    Args:
        threads_dir: Threads repository directory
        dry_run: If True, only report what would be done
        regenerate_cache: If True, rebuild manifest + search-index from per-thread data
        migrate: If True, perform one-time cleanup of globally-committed derived files

    Returns:
        List of actions taken (or would-be-taken if dry_run)
    """
    actions: List[str] = []
    td = str(threads_dir)

    if not (threads_dir / ".git").exists():
        return ["ERROR: Not a git repository"]

    report = diagnose(threads_dir)

    # Fix stuck rebase/merge (requires worktree lock — mutating git state)
    if (report.stuck_rebase or report.stuck_merge) and not dry_run:
        from watercooler.sync_common import acquire_worktree_lock
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

    # Fetch before reset so the tracking ref is current
    if report.ahead > 0 and report.tracking and not dry_run:
        subprocess.run(
            ["git", "-C", td, "fetch", "origin"],
            capture_output=True, text=True, timeout=30,
        )

    # Handle local-only commits
    if report.ahead > 0 and report.tracking:
        # Log local-only commits to recovery before discarding
        action = f"Discard {report.ahead} local-only commit(s) (reset to {report.tracking})"
        if dry_run:
            actions.append(f"[DRY RUN] {action}")
        elif report.dirty_files:
            # Match cli.py and middleware.py: skip reset when worktree has
            # uncommitted changes to avoid destroying concurrent writes.
            actions.append(
                f"SKIPPED: {action}: worktree has {len(report.dirty_files)} "
                f"uncommitted file(s) — reset would destroy them"
            )
        else:
            from watercooler.sync_common import acquire_worktree_lock, log_local_only_commits
            try:
                wt_lock = acquire_worktree_lock(threads_dir)
            except TimeoutError:
                actions.append(f"FAILED: {action}: worktree lock timeout")
                return actions

            try:
                recovery_ok = log_local_only_commits(threads_dir, report.tracking)
                if recovery_ok:
                    result = subprocess.run(
                        ["git", "-C", td, "reset", "--hard", report.tracking],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.returncode == 0:
                        actions.append(action)
                    else:
                        actions.append(f"FAILED: {action}: {result.stderr.strip()[:200]}")
                else:
                    actions.append(f"FAILED: {action}: recovery log incomplete, aborting reset")
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
            from watercooler.sync_common import acquire_worktree_lock
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
