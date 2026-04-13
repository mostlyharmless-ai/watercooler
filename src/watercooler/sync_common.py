"""Shared sync utilities for CLI and MCP write paths.

Provides:
- Topic-scoped staging: resolves actual file paths for a topic
- Worktree-level locking: serializes concurrent git operations
- Lock ordering: topic lock → worktree lock (prevents deadlock)
- Recovery log: preserves local-only commit metadata before reset
"""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir
from watercooler.fs import thread_path
from watercooler.lock import AdvisoryLock


# ============================================================================
# Constants
# ============================================================================

WORKTREE_LOCK_TTL_SECONDS = 120
WORKTREE_LOCK_TIMEOUT_SECONDS = 60
WORKTREE_LOCK_DIR = ".watercooler"
WORKTREE_LOCK_NAME = "_worktree.lock"
RECOVERY_LOG_DIR = ".watercooler"
RECOVERY_LOG_NAME = "recovery.jsonl"
# Must match the relative path from get_graph_dir() + get_thread_graph_dir()
_GRAPH_THREADS_PREFIX = "graph/baseline/threads/"


# ============================================================================
# Gitignore Management
# ============================================================================

_gitignore_lock = threading.Lock()
_gitignore_done: set = set()  # worktrees where .gitignore has been verified


def _ensure_watercooler_gitignored(threads_dir: Path) -> bool:
    """Ensure .watercooler/ is in .gitignore. Runs at most once per process.

    Returns True if .gitignore was modified (caller should stage it).
    """
    td_str = str(threads_dir)
    gitignore = threads_dir / ".gitignore"
    pattern = ".watercooler/"
    with _gitignore_lock:
        if td_str in _gitignore_done:
            return False
        try:
            if gitignore.exists():
                content = gitignore.read_text(encoding="utf-8")
                if any(line.strip() == pattern for line in content.splitlines()):
                    _gitignore_done.add(td_str)
                    return False
                with open(gitignore, "a", encoding="utf-8") as f:
                    if content and not content.endswith("\n"):
                        f.write("\n")
                    f.write(f"{pattern}\n")
            else:
                gitignore.write_text(f"{pattern}\n", encoding="utf-8")
            _gitignore_done.add(td_str)
            return True
        except OSError:
            return False


def invalidate_gitignore_cache(threads_dir: Path) -> None:
    """Clear gitignore check cache after external modification (e.g. --migrate)."""
    with _gitignore_lock:
        _gitignore_done.discard(str(threads_dir))


# ============================================================================
# Worktree Lock
# ============================================================================


def _worktree_lock_path(threads_dir: Path) -> Path:
    """Get the worktree-level lock path."""
    lock_dir = threads_dir / WORKTREE_LOCK_DIR / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / WORKTREE_LOCK_NAME


def acquire_worktree_lock(
    threads_dir: Path,
    timeout: int = WORKTREE_LOCK_TIMEOUT_SECONDS,
) -> AdvisoryLock:
    """Acquire worktree-level lock for mutating git operations.

    Serializes reset, pull, rebase, add, commit, push across all topics.
    Fetch can stay outside the lock (read-only).

    Lock ordering: topic lock → worktree lock (when both needed).

    Returns:
        AdvisoryLock (caller must release in finally block)

    Raises:
        TimeoutError: If lock cannot be acquired within timeout
    """
    lock_path = _worktree_lock_path(threads_dir)
    lock = AdvisoryLock(lock_path, ttl=WORKTREE_LOCK_TTL_SECONDS, timeout=timeout)
    if not lock.acquire():
        raise TimeoutError(
            f"Failed to acquire worktree lock within {timeout}s. "
            f"Another writer may be in progress."
        )
    return lock


# ============================================================================
# Topic-Scoped Staging
# ============================================================================


def paths_to_stage_for_topic(
    threads_dir: Path, topic: str, *, include_missing: bool = False,
) -> List[str]:
    """Resolve file paths that should be staged for a topic write.

    Returns paths relative to threads_dir for ``git add --all -- <paths>``.

    Includes:
    - Per-thread graph directory (meta.json, entries.jsonl, edges.jsonl, search-index.jsonl)
    - Markdown projection (threads/<topic>.md or <topic>.md)
    - .gitignore (only if modified by _ensure_watercooler_gitignored this process)

    Design invariant: all per-topic writes MUST land inside these paths.
    """
    paths: List[str] = []

    graph_dir = get_graph_dir(threads_dir)
    topic_graph_dir = get_thread_graph_dir(graph_dir, topic)
    rel = topic_graph_dir.relative_to(threads_dir)
    if topic_graph_dir.exists() or include_missing:
        paths.append(str(rel))

    md_path = thread_path(topic, threads_dir)
    if md_path.exists() or include_missing:
        rel = md_path.relative_to(threads_dir)
        paths.append(str(rel))

    # Stage .gitignore if we modified it (ensures it's committed with the topic write)
    gitignore_modified = _ensure_watercooler_gitignored(threads_dir)
    if gitignore_modified:
        paths.append(".gitignore")

    return paths


# ============================================================================
# Recovery Log
# ============================================================================


def _recovery_log_path(threads_dir: Path, *, create: bool = False) -> Path:
    """Get path to the recovery log.

    Args:
        create: If True, create the parent directory. Only True for writes.
    """
    log_dir = threads_dir / RECOVERY_LOG_DIR
    if create:
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / RECOVERY_LOG_NAME


def _classify_commit(summary: str, changed_paths: List[str]) -> str:
    """Classify whether a commit was made under topic-scoped staging.

    A commit is ``topic_scoped`` when all its paths belong to exactly one
    topic's graph directory (with optional markdown projection). Any path
    outside that scope → ``legacy_global``.
    """
    if not changed_paths:
        return "legacy_global"

    topic_dirs = set()
    for p in changed_paths:
        if p.startswith(_GRAPH_THREADS_PREFIX):
            # Topic is the directory name immediately after the prefix.
            # Topics are flat slugs (no slashes) per CLAUDE.md convention.
            remainder = p[len(_GRAPH_THREADS_PREFIX):]
            topic_name = remainder.split("/")[0] if remainder else ""
            if topic_name:
                topic_dirs.add(topic_name)
        elif p.endswith(".md") and p.startswith("threads/"):
            # Thread projection: threads/<topic>.md
            stem = Path(p).stem
            topic_dirs.add(stem)
        elif p == ".gitignore":
            pass  # Infrastructure — consistent with topic-scoped
        else:
            return "legacy_global"

    return "topic_scoped" if len(topic_dirs) == 1 else "legacy_global"


def _extract_entry_ids_from_commit(message: str) -> List[str]:
    """Extract entry IDs from commit footer metadata."""
    ids = []
    for line in message.split("\n"):
        if line.startswith("Watercooler-Entry-ID:"):
            entry_id = line.split(":", 1)[1].strip()
            if entry_id:
                ids.append(entry_id)
    return ids


def _extract_topic_from_commit(message: str) -> Optional[str]:
    """Extract topic from commit footer metadata."""
    for line in message.split("\n"):
        if line.startswith("Watercooler-Topic:"):
            topic = line.split(":", 1)[1].strip()
            if topic:
                return topic
    return None


def log_recovery_entry(
    threads_dir: Path,
    sha: str,
    summary: str,
    full_message: str,
    changed_paths: List[str],
    status: str = "recovered",
) -> Optional[Dict[str, Any]]:
    """Append an entry to the recovery log for a local-only commit.

    Returns the entry dict if written, None if skipped (lock timeout / I/O error).
    """
    commit_type = _classify_commit(summary, changed_paths)
    entry_ids = _extract_entry_ids_from_commit(full_message)
    topic = _extract_topic_from_commit(full_message)

    entry: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sha": sha,
        "summary": summary,
        "topic": topic,
        "changed_paths": changed_paths,
        "entry_ids": entry_ids,
        "commit_type": commit_type,
        "status": status,
    }

    log_path = _recovery_log_path(threads_dir, create=True)
    lock_path = log_path.with_suffix(".lock")
    try:
        lock = AdvisoryLock(lock_path, ttl=10, timeout=5)
        if not lock.acquire():
            return None
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        finally:
            lock.release()
    except OSError:
        return None

    return entry


def load_recovery_log(threads_dir: Path) -> List[Dict[str, Any]]:
    """Load all entries from the recovery log (oldest first)."""
    log_path = _recovery_log_path(threads_dir)
    if not log_path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return entries


def is_safe_to_auto_recover(entry: Dict[str, Any]) -> bool:
    """Check if a recovery log entry is safe to auto-cherry-pick."""
    return entry.get("commit_type") == "topic_scoped"


# ============================================================================
# Shared Recovery Logging (used by CLI, MCP middleware, sync_repair)
# ============================================================================


def log_local_only_commits(
    threads_dir: Path,
    remote_ref: str,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
) -> bool:
    """Log all local-only commits to the recovery log before a hard reset.

    Args:
        threads_dir: Threads repository directory
        remote_ref: Remote tracking ref (e.g. "origin/watercooler/threads")
        log_fn: Optional callback for per-commit logging (receives message string)

    Returns:
        True if all commits were logged (safe to proceed with reset).
        False if any write failed (caller should abort the reset).
    """
    td = str(threads_dir)
    # Ensure .watercooler/ is gitignored before writing recovery log
    _ensure_watercooler_gitignored(threads_dir)
    try:
        result = subprocess.run(
            ["git", "-C", td, "log", "--format=%H%n%s%n%b%x00", f"{remote_ref}..HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            if log_fn:
                log_fn(f"git log failed (rc={result.returncode}), aborting reset")
            return False  # Git error — abort to avoid unlogged data loss
        if not result.stdout.strip():
            return True  # No local-only commits — safe to proceed

        for chunk in result.stdout.split("\x00"):
            chunk = chunk.strip()
            if not chunk:
                continue
            lines = chunk.split("\n", 2)
            sha = lines[0] if len(lines) > 0 else ""
            summary = lines[1] if len(lines) > 1 else ""
            full_msg = lines[2] if len(lines) > 2 else ""
            if not sha:
                continue

            diff_result = subprocess.run(
                ["git", "-C", td, "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
                capture_output=True, text=True, timeout=5,
            )
            changed = [p for p in diff_result.stdout.strip().split("\n") if p] if diff_result.returncode == 0 else []

            if log_fn:
                log_fn(f"Discarding local-only: {sha[:12]} {summary}")

            logged = log_recovery_entry(
                threads_dir, sha=sha, summary=summary,
                full_message=full_msg, changed_paths=changed,
                status="discarded",
            )
            if not logged:
                if log_fn:
                    log_fn(f"Recovery log write failed for {sha[:12]}, aborting reset")
                return False

    except Exception as e:
        if log_fn:
            log_fn(f"Recovery logging failed: {e}, aborting reset")
        return False

    return True
