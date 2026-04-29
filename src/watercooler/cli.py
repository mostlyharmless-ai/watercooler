#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Watercooler CLI - command-line interface for thread management."""
from __future__ import annotations

import argparse
import importlib
import json as _json
import subprocess
import sys
import time as _time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

# Fail fast on unsupported interpreter version
if sys.version_info < (3, 10):
    print(f"Watercooler CLI requires Python 3.10+; found {sys.version.split()[0]}", file=sys.stderr)
    sys.exit(1)


_MAX_PUSH_RETRIES = 3


def _import_memory(*names: str, module: str = "watercooler_memory") -> tuple:
    """Import symbols from watercooler_memory, exiting cleanly if unavailable.

    Wraps the real import so there is no TOCTOU gap between the guard check
    and the import itself.  The CLI uses argparse, so SystemExit is the correct
    exit mechanism (argparse itself raises SystemExit from parser.error()).
    """
    try:
        mod = importlib.import_module(module)
        return tuple(getattr(mod, name) for name in names)
    except ImportError as exc:
        raise SystemExit(
            "Memory feature requires the full watercooler build.\n"
            "This feature is not available in the open-core edition.\n"
            f"Detail: {exc}"
        ) from exc
    except AttributeError as exc:
        raise SystemExit(f"Internal error: symbol not found in {module}: {exc}") from exc


def _log_sync_action(action: str, *, outcome: str = "ok", **fields) -> None:
    """Emit a structured sync action log line to stderr.

    Mirrors the schema from watercooler_mcp.observability.log_action so
    both CLI and MCP sync actions can be analyzed with the same tooling.
    """
    payload = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "action": action,
        "outcome": outcome,
    }
    payload.update(fields)
    print(_json.dumps(payload, separators=(",", ":")), file=sys.stderr)


def _resolve_git_dir(threads_dir: Path) -> Path:
    """Resolve the actual .git directory (handles worktree .git files)."""
    git_dir = threads_dir / ".git"
    if git_dir.is_file():
        real_dir = Path(git_dir.read_text().strip().removeprefix("gitdir: "))
        if not real_dir.is_absolute():
            real_dir = (threads_dir / real_dir).resolve()
        return real_dir
    return git_dir


def _is_worktree_busy(threads_dir: Path) -> bool:
    """Check if a rebase or merge is in progress."""
    git_dir = _resolve_git_dir(threads_dir)
    return (
        (git_dir / "rebase-merge").exists()
        or (git_dir / "rebase-apply").exists()
        or (git_dir / "MERGE_HEAD").exists()
    )


def _abort_rebase_if_needed(td: str, threads_dir: Path) -> None:
    """Abort a stuck rebase or merge."""
    git_dir = _resolve_git_dir(threads_dir)
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        subprocess.run(
            ["git", "-C", td, "rebase", "--abort"],
            capture_output=True, text=True, timeout=10,
        )
    if (git_dir / "MERGE_HEAD").exists():
        subprocess.run(
            ["git", "-C", td, "merge", "--abort"],
            capture_output=True, text=True, timeout=10,
        )


def _pull_rebase(td: str, threads_dir: Path) -> bool:
    """Pull with rebase; abort on failure. Returns True on success."""
    rebase = subprocess.run(
        ["git", "-C", td, "pull", "--rebase"],
        capture_output=True, text=True, timeout=30,
    )
    if rebase.returncode != 0:
        _abort_rebase_if_needed(td, threads_dir)
        print(
            f"watercooler sync: pull --rebase failed (aborted): "
            f"{rebase.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return False
    return True


def _topic_lock_path(threads_dir: Path, topic: str) -> Path:
    """Compute the advisory lock path for a topic.

    Uses the same convention as watercooler_mcp.sync to ensure the CLI
    and MCP server respect each other's locks.
    """
    import hashlib as _hashlib
    import re as _re
    safe = _re.sub(r'\.\.', '_', topic)
    safe = _re.sub(r'[<>:"/\\|?*]', '_', safe)
    safe = _re.sub(r'_+', '_', safe)
    safe = safe.strip('_').lstrip('.')
    if not safe:
        safe = '_empty_'
    if len(safe) > 200:
        h = _hashlib.sha256(topic.encode()).hexdigest()[:8]
        safe = f"{safe[:200 - len(h) - 1]}_{h}"
    lock_dir = threads_dir / ".watercooler" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"{safe}.lock"


def _ensure_worktree_current(threads_dir: Path) -> "AdvisoryLock | None":
    """Fetch and fast-forward the worktree; reset on divergence.

    Acquires the worktree lock for mutating operations and RETAINS it
    on success. Caller must release the returned lock (or pass it to
    _sync_after_write which releases in its finally).

    Returns:
        Held worktree lock on success, None on failure or if not needed.
    """
    if not (threads_dir / ".git").exists():
        return None

    td = str(threads_dir)
    from .sync_common import acquire_worktree_lock, log_local_only_commits

    # Fetch is read-only — no lock needed
    t0 = _time.perf_counter()
    fetch = subprocess.run(
        ["git", "-C", td, "fetch", "origin"],
        capture_output=True, text=True, timeout=30,
    )
    fetch_ms = (_time.perf_counter() - t0) * 1000
    if fetch.returncode != 0:
        _log_sync_action("sync.fetch", outcome="error", duration_ms=round(fetch_ms, 2), source="cli")
        return None
    _log_sync_action("sync.fetch", outcome="ok", duration_ms=round(fetch_ms, 2), source="cli")

    # Acquire worktree lock for all mutating ops (abort, pull, reset).
    try:
        wt_lock = acquire_worktree_lock(threads_dir)
    except TimeoutError:
        print("watercooler sync: worktree lock timeout", file=sys.stderr)
        return None

    # Single try/finally: on ANY failure, release lock and return None.
    success = False
    try:
        if _is_worktree_busy(threads_dir):
            _abort_rebase_if_needed(td, threads_dir)
            if _is_worktree_busy(threads_dir):
                print("watercooler sync: stuck rebase/merge", file=sys.stderr)
                return None

        t0 = _time.perf_counter()
        ff = subprocess.run(
            ["git", "-C", td, "pull", "--ff-only"],
            capture_output=True, text=True, timeout=30,
        )
        ff_ms = (_time.perf_counter() - t0) * 1000
        if ff.returncode == 0:
            _log_sync_action("sync.pull_ff", outcome="ok", duration_ms=round(ff_ms, 2), source="cli")
            success = True
            return wt_lock

        _log_sync_action("sync.pull_ff", outcome="diverged", duration_ms=round(ff_ms, 2), source="cli")

        # FF failed — local-only commits exist. Rebase them on top of
        # remote instead of resetting (which destroys their content).
        # This is safe now that manifest.json and search-index.jsonl are
        # no longer committed — the rebase conflict source is gone.
        if _pull_rebase(td, threads_dir):
            _log_sync_action("sync.pull_rebase", outcome="ok", source="cli")
            success = True
            return wt_lock

        # Rebase failed (conflict on topic-scoped files). Log and
        # continue — the write will land on the local branch and
        # _commit_and_push will retry the push with rebase.
        _log_sync_action("sync.pull_rebase", outcome="conflict", source="cli")
        success = True
        return wt_lock
    finally:
        if not success:
            wt_lock.release()


def _cli_write_with_sync(
    threads_dir: Path, topic: str, commit_msg: str,
    operation: "Callable[[], object]", *, no_sync: bool = False,
) -> object:
    """Execute a CLI write with full sync protection.

    Acquires topic lock → worktree lock → fetch/reset → write → stage →
    commit → push → release. The worktree lock is held continuously from
    pre-write through commit with no gap.

    Guards against writing into a non-GitHub-backed target (Bug #3,
    plan v4): before any lock acquisition the shared
    ``assert_github_backed_threads`` helper runs and aborts with an
    actionable remediation message unless
    ``WATERCOOLER_ALLOW_LOCAL_ONLY=1`` is set. This covers every CLI
    write command (`init-thread`, `append-entry`, `say`, `ack`,
    `handoff`, `set-status`, `set-ball`, plus any future additions)
    since they all route through this wrapper.
    """
    from .lock import AdvisoryLock
    from .write_guard import WatercoolerWriteError, assert_github_backed_threads

    try:
        assert_github_backed_threads(threads_dir)
    except WatercoolerWriteError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    lock_path = _topic_lock_path(threads_dir, topic)
    topic_lock = AdvisoryLock(lock_path, ttl=120, timeout=30)
    if not topic_lock.acquire():
        print(
            f"watercooler: could not acquire lock for '{topic}' — another writer "
            f"may be in progress. Entry was NOT written. Retry in a few seconds.",
            file=sys.stderr,
        )
        sys.exit(1)

    wt_lock = None
    try:
        if not no_sync:
            wt_lock = _ensure_worktree_current(threads_dir)
        result = operation()
        if not no_sync:
            _wt = wt_lock
            wt_lock = None  # transfer ownership before call
            _commit_and_push(threads_dir, topic, commit_msg, _wt)
            if _is_worktree_busy(threads_dir):
                print(
                    "watercooler: WARNING — entry written locally but commit skipped "
                    "(worktree has stuck rebase/merge). Run: watercooler sync-repair",
                    file=sys.stderr,
                )
        return result
    except ValueError as exc:
        # Surface validation errors (bad role, malformed .watercooler/roles.toml,
        # etc.) cleanly instead of tracebacking out of the CLI.
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if wt_lock:
            wt_lock.release()
        topic_lock.release()


def _commit_and_push(
    threads_dir: Path, topic: str, commit_msg: str,
    wt_lock: "AdvisoryLock | None",
) -> None:
    """Stage topic files, commit, and push. Always releases wt_lock.

    This is the post-write half of the sync cycle. The worktree lock
    is held from _ensure_worktree_current through this function.

    When wt_lock is None (pre-write sync failed), acquires a fresh lock.
    This creates a brief gap where another topic's writer could interleave,
    but topic-scoped staging prevents cross-topic corruption.
    """
    if not (threads_dir / ".git").exists():
        if wt_lock:
            wt_lock.release()
        return

    td = str(threads_dir)
    from .sync_common import acquire_worktree_lock, paths_to_stage_for_topic

    # Acquire worktree lock if not passed from _ensure_worktree_current
    if not wt_lock:
        try:
            wt_lock = acquire_worktree_lock(threads_dir)
        except TimeoutError:
            print("watercooler sync: worktree lock timeout, skipping commit", file=sys.stderr)
            return

    try:
        if _is_worktree_busy(threads_dir):
            print("watercooler sync: rebase/merge in progress, skipping commit", file=sys.stderr)
            return

        stage_paths = paths_to_stage_for_topic(threads_dir, topic, include_missing=True)
        if not stage_paths:
            return
        add = subprocess.run(
            ["git", "-C", td, "add", "--all", "--"] + stage_paths,
            capture_output=True, text=True, timeout=10,
        )
        if add.returncode != 0:
            print(f"watercooler sync: git add failed: {add.stderr.strip()[:200]}", file=sys.stderr)
            return

        status = subprocess.run(
            ["git", "-C", td, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if status.returncode != 0:
            print(f"watercooler sync: git status failed: {status.stderr.strip()[:200]}", file=sys.stderr)
            return
        if not status.stdout.strip():
            return  # nothing to commit

        result = subprocess.run(
            ["git", "-C", td, "commit", "-m", commit_msg],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            print(f"watercooler sync: commit failed: {result.stderr.strip()[:200]}", file=sys.stderr)
            return

        for attempt in range(_MAX_PUSH_RETRIES):
            push = subprocess.run(
                ["git", "-C", td, "push"],
                capture_output=True, text=True, timeout=30,
            )
            if push.returncode == 0:
                _log_sync_action("sync.push", outcome="ok", attempt=attempt + 1, source="cli")
                return
            err = push.stderr.lower()
            if "rejected" in err or "non-fast-forward" in err:
                _log_sync_action("sync.push", outcome="rejected", attempt=attempt + 1, source="cli")
                if not _pull_rebase(td, threads_dir):
                    return
                continue
            else:
                print(f"watercooler sync: push failed: {push.stderr.strip()[:200]}", file=sys.stderr)
                return

        _log_sync_action("sync.push", outcome="exhausted", attempts=_MAX_PUSH_RETRIES, source="cli")
    except Exception as e:
        print(f"watercooler sync: {e}", file=sys.stderr)
    finally:
        if wt_lock:
            wt_lock.release()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="watercooler",
        description="File-based collaboration for agentic coding",
    )

    sub = ap.add_subparsers(dest="cmd")

    # Command stubs
    p_init = sub.add_parser("init-thread", help="Initialize a new thread")
    p_init.add_argument("topic", help="Thread topic identifier")
    p_init.add_argument("--title", help="Optional title override")
    p_init.add_argument("--status", default="OPEN", help="Initial status (default: OPEN)")
    p_init.add_argument("--ball", default="codex", help="Initial ball owner (default: codex)")
    p_init.add_argument("--threads-dir", help="Threads directory (default: ./watercooler or $WATERCOOLER_DIR)")
    p_init.add_argument("--no-sync", action="store_true", help="Skip git commit+push after write")

    p_web = sub.add_parser("web-export", help="Generate HTML index")
    p_web.add_argument("--threads-dir")
    p_web.add_argument("--out", help="Optional output file path")
    p_web.add_argument("--open-only", action="store_true")
    p_web.add_argument("--closed", action="store_true")

    p_say = sub.add_parser("say", help="Quick team note with auto-ball-flip")
    p_say.add_argument("topic")
    p_say.add_argument("--threads-dir")
    p_say.add_argument("--agent", help="Agent name (defaults to Team)")
    p_say.add_argument("--role", help="Agent role — see project's .watercooler/roles.toml or call watercooler_role_details for the active catalog (default: implementer)")
    p_say.add_argument("--title", required=True, help="Entry title")
    p_say.add_argument("--type", dest="entry_type", default="Note", help="Entry type (Note, Plan, Decision, PR, Closure)")
    p_say.add_argument("--body", required=True, help="Entry body text or @file path")
    p_say.add_argument("--status", help="Optional status update")
    p_say.add_argument("--ball", help="Optional ball update (auto-flips if not provided)")

    p_say.add_argument("--agents-file", help="Agent registry JSON file")
    p_say.add_argument("--no-sync", action="store_true", help="Skip git commit+push after write")

    p_ack = sub.add_parser("ack", help="Acknowledge without ball flip")
    p_ack.add_argument("topic")
    p_ack.add_argument("--threads-dir")
    p_ack.add_argument("--agent", help="Agent name (defaults to Team)")
    p_ack.add_argument("--role", help="Agent role — see project's .watercooler/roles.toml or call watercooler_role_details for the active catalog")
    p_ack.add_argument("--title", help="Entry title (default: Ack)")
    p_ack.add_argument("--type", dest="entry_type", default="Note", help="Entry type (Note, Plan, Decision, PR, Closure)")
    p_ack.add_argument("--body", help="Entry body text or @file path (default: ack)")
    p_ack.add_argument("--status", help="Optional status update")
    p_ack.add_argument("--ball", help="Optional ball update (does NOT auto-flip)")

    p_ack.add_argument("--agents-file", help="Agent registry JSON file")
    p_ack.add_argument("--no-sync", action="store_true", help="Skip git commit+push after write")

    p_handoff = sub.add_parser("handoff", help="Flip ball to counterpart and append handoff entry")
    p_handoff.add_argument("topic")
    p_handoff.add_argument("--threads-dir")
    p_handoff.add_argument("--agent", help="Agent performing handoff (defaults to Team)")
    p_handoff.add_argument("--role", default="pm", help="Agent role (default: pm)")
    p_handoff.add_argument("--note", help="Optional custom handoff message")

    p_handoff.add_argument("--agents-file", help="Agent registry JSON file")
    p_handoff.add_argument("--no-sync", action="store_true", help="Skip git commit+push after write")

    p_list = sub.add_parser("list", help="List threads")
    p_list.add_argument("--threads-dir")
    p_list.add_argument("--open-only", action="store_true", help="Show only open threads")
    p_list.add_argument("--closed", action="store_true", help="Show only closed threads")

    p_reindex = sub.add_parser("reindex", help="Rebuild index")
    p_reindex.add_argument("--threads-dir")
    p_reindex.add_argument("--out", help="Optional output file path")
    p_reindex.add_argument("--open-only", action="store_true")
    p_reindex.add_argument("--closed", action="store_true")

    p_search = sub.add_parser("search", help="Search threads")
    p_search.add_argument("query")
    p_search.add_argument("--threads-dir")

    p_unlock = sub.add_parser("unlock", help="Clear advisory lock (debugging)")
    p_unlock.add_argument("topic")
    p_unlock.add_argument("--threads-dir")
    p_unlock.add_argument("--force", action="store_true", help="Remove lock even if active")

    p_check_branches = sub.add_parser("check-branches", help="Comprehensive audit of branch pairing")
    p_check_branches.add_argument("--code-root", help="Path to code repository (default: current directory)")
    p_check_branches.add_argument("--include-merged", action="store_true", help="Include fully merged branches")

    p_check_branch = sub.add_parser("check-branch", help="Validate branch pairing for specific branch")
    p_check_branch.add_argument("branch", help="Branch name to check")
    p_check_branch.add_argument("--code-root", help="Path to code repository (default: current directory)")

    p_merge_branch = sub.add_parser("merge-branch", help="Merge threads branch to main")
    p_merge_branch.add_argument("branch", help="Branch name to merge")
    p_merge_branch.add_argument("--code-root", help="Path to code repository (default: current directory)")
    p_merge_branch.add_argument("--force", action="store_true", help="Skip safety checks")

    p_archive_branch = sub.add_parser("archive-branch", help="Close OPEN threads, merge to main, then delete branch")
    p_archive_branch.add_argument("branch", help="Branch name to archive")
    p_archive_branch.add_argument("--code-root", help="Path to code repository (default: current directory)")
    p_archive_branch.add_argument("--abandon", action="store_true", help="Set OPEN threads to ABANDONED status")
    p_archive_branch.add_argument("--force", action="store_true", help="Skip confirmation prompts")

    p_install_hooks = sub.add_parser("install-hooks", help="Install git hooks for branch pairing validation")
    p_install_hooks.add_argument("--code-root", help="Path to code repository (default: current directory)")
    p_install_hooks.add_argument("--hooks-dir", help="Git hooks directory (default: .git/hooks)")
    p_install_hooks.add_argument("--force", action="store_true", help="Overwrite existing hooks")

    # Roles commands
    p_roles = sub.add_parser("roles", help="Roles management")
    roles_sub = p_roles.add_subparsers(dest="roles_cmd")

    p_roles_init = roles_sub.add_parser(
        "init",
        help="Scaffold .watercooler/roles.toml from bundled defaults",
    )
    p_roles_init.add_argument(
        "--project-path",
        help="Project directory (default: current directory)",
    )
    p_roles_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing roles.toml",
    )

    # Memory-tier migration: stdio ↔ hybrid
    from watercooler.migration.cli import add_migrate_parser
    add_migrate_parser(sub)

    # Config commands
    p_config = sub.add_parser("config", help="Configuration management")
    config_sub = p_config.add_subparsers(dest="config_cmd")

    p_config_init = config_sub.add_parser("init", help="Initialize config file from template")
    p_config_init.add_argument("--user", action="store_true", help="Create user config (~/.watercooler/config.toml)")
    p_config_init.add_argument("--project", action="store_true", help="Create project config (.watercooler/config.toml)")
    p_config_init.add_argument("--force", action="store_true", help="Overwrite existing config")

    p_config_show = config_sub.add_parser("show", help="Show resolved configuration")
    p_config_show.add_argument("--project-path", help="Project directory for config discovery")
    p_config_show.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")
    p_config_show.add_argument("--sources", action="store_true", help="Show config source files")

    p_config_validate = config_sub.add_parser("validate", help="Validate configuration files")
    p_config_validate.add_argument("--project-path", help="Project directory for config discovery")
    p_config_validate.add_argument("--strict", action="store_true", help="Treat warnings as errors")

    # Slack commands
    p_slack = sub.add_parser("slack", help="Slack integration")
    slack_sub = p_slack.add_subparsers(dest="slack_cmd")

    p_slack_setup = slack_sub.add_parser("setup", help="Interactive webhook setup")
    p_slack_setup.add_argument("--webhook-url", help="Webhook URL (skips interactive prompt)")

    p_slack_test = slack_sub.add_parser("test", help="Send a test notification")

    p_slack_status = slack_sub.add_parser("status", help="Show current Slack configuration")

    p_slack_disable = slack_sub.add_parser("disable", help="Disable Slack notifications")

    p_append = sub.add_parser("append-entry", help="Append a structured entry")
    p_append.add_argument("topic")
    p_append.add_argument("--threads-dir")
    p_append.add_argument("--agent", required=True, help="Agent name")
    p_append.add_argument("--role", required=True, help="Agent role — see project's .watercooler/roles.toml or call watercooler_role_details for the active catalog")
    p_append.add_argument("--title", required=True, help="Entry title")
    p_append.add_argument("--type", dest="entry_type", default="Note", help="Entry type (Note, Plan, Decision, PR, Closure)")
    p_append.add_argument("--body", required=True, help="Entry body text or @file path")
    p_append.add_argument("--status", help="Optional status update")
    p_append.add_argument("--ball", help="Optional ball update (auto-flips if not provided)")

    p_append.add_argument("--agents-file", help="Agent registry JSON file")
    p_append.add_argument("--no-sync", action="store_true", help="Skip git commit+push after write")

    p_set_status = sub.add_parser("set-status", help="Update thread status")
    p_set_status.add_argument("topic")
    p_set_status.add_argument("status")
    p_set_status.add_argument("--threads-dir")
    p_set_status.add_argument("--no-sync", action="store_true", help="Skip git commit+push after write")

    p_set_ball = sub.add_parser("set-ball", help="Update ball ownership")
    p_set_ball.add_argument("topic")
    p_set_ball.add_argument("ball")
    p_set_ball.add_argument("--threads-dir")
    p_set_ball.add_argument("--no-sync", action="store_true", help="Skip git commit+push after write")

    p_sync_repair = sub.add_parser("sync-repair", help="Diagnose and fix orphan branch sync issues")
    p_sync_repair.add_argument("--threads-dir", help="Threads directory override")
    p_sync_repair.add_argument("--diagnose", action="store_true", help="Report state without fixing")
    p_sync_repair.add_argument("--dry-run", action="store_true", help="Show what would be done")
    p_sync_repair.add_argument("--regenerate-cache", action="store_true", help="Rebuild manifest + search-index")
    p_sync_repair.add_argument("--migrate", action="store_true", help="One-time cleanup of global derived files")
    p_sync_repair.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    p_sync = sub.add_parser("sync", help="Inspect or flush async git sync queue")
    p_sync.add_argument("--code-path", help="Code repository root (default: current directory)")
    p_sync.add_argument("--threads-dir", help="Threads directory override")
    p_sync.add_argument("--status", action="store_true", help="Show pending queue status without flushing")
    p_sync.add_argument("--now", action="store_true", help="Force an immediate push of pending commits")

    # Memory graph commands
    p_memory = sub.add_parser("memory", help="Memory graph operations")
    memory_sub = p_memory.add_subparsers(dest="memory_cmd")

    p_memory_build = memory_sub.add_parser("build", help="Build memory graph from threads")
    p_memory_build.add_argument("--threads-dir", help="Threads directory (default: ./watercooler)")
    p_memory_build.add_argument("--output", "-o", help="Output file path for graph JSON")
    p_memory_build.add_argument("--no-summaries", action="store_true", help="Skip summary generation")
    p_memory_build.add_argument("--no-embeddings", action="store_true", help="Skip embedding generation")
    p_memory_build.add_argument("--branch", help="Git branch context")

    p_memory_export = memory_sub.add_parser("export", help="Export graph to external format")
    p_memory_export.add_argument("--graph", help="Input graph JSON (builds from threads if not provided)")
    p_memory_export.add_argument("--threads-dir", help="Threads directory (if building)")
    p_memory_export.add_argument("--format", choices=["leanrag", "json"], default="leanrag", help="Export format")
    p_memory_export.add_argument("--output", "-o", required=True, help="Output path (directory for leanrag, file for json)")
    p_memory_export.add_argument("--no-embeddings", action="store_true", help="Exclude embeddings from export")

    p_memory_stats = memory_sub.add_parser("stats", help="Show graph statistics")
    p_memory_stats.add_argument("--graph", help="Graph JSON file (builds from threads if not provided)")
    p_memory_stats.add_argument("--threads-dir", help="Threads directory (if building)")

    # Baseline graph commands (free-tier, local LLM)
    p_baseline = sub.add_parser("baseline-graph", help="Baseline graph operations (free-tier, local LLM)")
    baseline_sub = p_baseline.add_subparsers(dest="baseline_cmd")

    p_baseline_build = baseline_sub.add_parser("build", help="Build baseline graph from threads")
    p_baseline_build.add_argument("--threads-dir", help="Threads directory (default: ./watercooler)")
    p_baseline_build.add_argument("--output", "-o", help="Output directory for graph files")
    p_baseline_build.add_argument("--extractive-only", action="store_true", help="Use extractive summaries only (no LLM)")
    p_baseline_build.add_argument("--skip-closed", action="store_true", help="Skip closed threads")

    p_baseline_stats = baseline_sub.add_parser("stats", help="Show threads statistics")
    p_baseline_stats.add_argument("--threads-dir", help="Threads directory")

    args = ap.parse_args(argv)

    if not args.cmd:
        ap.print_help()
        sys.exit(0)

    if args.cmd == "sync-repair":
        from .path_resolver import resolve_threads_dir
        from .sync_repair import diagnose, repair, format_report

        threads_dir = resolve_threads_dir(args.threads_dir)

        if args.diagnose:
            report = diagnose(threads_dir)
            if args.as_json:
                print(_json.dumps(report.to_dict(), indent=2))
            else:
                print(format_report(report))
        else:
            actions = repair(
                threads_dir,
                dry_run=args.dry_run,
                regenerate_cache=args.regenerate_cache,
                migrate=args.migrate,
            )
            if args.as_json:
                print(_json.dumps({"actions": actions}, indent=2))
            else:
                for action in actions:
                    print(action)
        sys.exit(0)

    if args.cmd == "init-thread":
        from .path_resolver import resolve_threads_dir
        from .commands_graph import init_thread

        threads_dir = resolve_threads_dir(args.threads_dir)
        out = _cli_write_with_sync(
            threads_dir, args.topic, f"init: {args.topic}",
            lambda: init_thread(
                args.topic, threads_dir=threads_dir,
                title=args.title, status=args.status, ball=args.ball,
            ),
            no_sync=args.no_sync,
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "append-entry":
        from ulid import ULID
        from .fs import read_body
        from .path_resolver import resolve_threads_dir
        from .commands_graph import append_entry
        from .agents import _load_agents_registry

        threads_dir = resolve_threads_dir(args.threads_dir)
        body = read_body(args.body)
        registry = _load_agents_registry(args.agents_file) if hasattr(args, 'agents_file') and args.agents_file else None
        out = _cli_write_with_sync(
            threads_dir, args.topic, f"entry: {args.topic} — {args.title}",
            lambda: append_entry(
                args.topic, threads_dir=threads_dir,
                agent=args.agent, role=args.role, title=args.title,
                entry_type=args.entry_type, body=body,
                status=args.status, ball=args.ball,
                registry=registry, entry_id=str(ULID()),
            ),
            no_sync=args.no_sync,
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "set-status":
        from .commands_graph import set_status
        from .path_resolver import resolve_threads_dir

        threads_dir = resolve_threads_dir(args.threads_dir)
        out = _cli_write_with_sync(
            threads_dir, args.topic, f"status: {args.topic} → {args.status}",
            lambda: set_status(args.topic, threads_dir=threads_dir, status=args.status),
            no_sync=args.no_sync,
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "set-ball":
        from .commands_graph import set_ball
        from .path_resolver import resolve_threads_dir

        threads_dir = resolve_threads_dir(args.threads_dir)
        out = _cli_write_with_sync(
            threads_dir, args.topic, f"ball: {args.topic} → {args.ball}",
            lambda: set_ball(args.topic, threads_dir=threads_dir, ball=args.ball),
            no_sync=args.no_sync,
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "sync":
        print(
            "The 'sync' command has been removed. "
            "Thread sync is now handled automatically via the orphan branch worktree.",
            file=sys.stderr,
        )
        sys.exit(0)

    if args.cmd == "list":
        from pathlib import Path
        from .commands import list_threads
        from .path_resolver import resolve_threads_dir

        oo: bool | None = None
        if args.open_only and args.closed:
            oo = None
        elif args.open_only:
            oo = True
        elif args.closed:
            oo = False
        rows = list_threads(threads_dir=resolve_threads_dir(args.threads_dir), open_only=oo)
        for title, status, ball, updated, path, is_new in rows:
            newcol = "NEW" if is_new else ""
            print(f"{updated}\t{status}\t{ball}\t{newcol}\t{title}\t{path}")
        sys.exit(0)

    if args.cmd == "reindex":
        from pathlib import Path
        from .commands import reindex
        from .path_resolver import resolve_threads_dir

        oo: bool | None = True
        if args.open_only and args.closed:
            oo = None
        elif args.closed:
            oo = False
        elif args.open_only:
            oo = True
        out = reindex(threads_dir=resolve_threads_dir(args.threads_dir), out_file=Path(args.out) if args.out else None, open_only=oo)
        print(str(out))
        sys.exit(0)

    if args.cmd == "search":
        from pathlib import Path
        from .commands import search
        from .path_resolver import resolve_threads_dir

        hits = search(threads_dir=resolve_threads_dir(args.threads_dir), query=args.query)
        for p, ln, line in hits:
            print(f"{p}:{ln}: {line}")
        sys.exit(0)

    if args.cmd == "unlock":
        from pathlib import Path
        from .commands import unlock
        from .path_resolver import resolve_threads_dir

        unlock(
            args.topic,
            threads_dir=resolve_threads_dir(args.threads_dir),
            force=args.force
        )
        sys.exit(0)

    if args.cmd == "web-export":
        from pathlib import Path
        from .commands import web_export
        from .path_resolver import resolve_threads_dir

        oo: bool | None = True
        if args.open_only and args.closed:
            oo = None
        elif args.closed:
            oo = False
        elif args.open_only:
            oo = True
        out = web_export(threads_dir=resolve_threads_dir(args.threads_dir), out_file=Path(args.out) if args.out else None, open_only=oo)
        print(str(out))
        sys.exit(0)

    if args.cmd == "say":
        from ulid import ULID
        from .fs import read_body
        from .path_resolver import resolve_threads_dir
        from .commands_graph import say
        from .agents import _load_agents_registry

        threads_dir = resolve_threads_dir(args.threads_dir)
        body = read_body(args.body)
        registry = _load_agents_registry(args.agents_file) if hasattr(args, 'agents_file') and args.agents_file else None
        out = _cli_write_with_sync(
            threads_dir, args.topic, f"say: {args.topic} — {args.title}",
            lambda: say(
                args.topic, threads_dir=threads_dir,
                agent=args.agent, role=args.role, title=args.title,
                entry_type=args.entry_type, body=body,
                status=args.status, ball=args.ball,
                registry=registry, entry_id=str(ULID()),
            ),
            no_sync=args.no_sync,
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "ack":
        from ulid import ULID
        from .fs import read_body
        from .commands_graph import ack
        from .path_resolver import resolve_threads_dir
        from .agents import _load_agents_registry

        threads_dir = resolve_threads_dir(args.threads_dir)
        body = read_body(args.body) if args.body else None
        registry = _load_agents_registry(args.agents_file) if hasattr(args, 'agents_file') and args.agents_file else None
        out = _cli_write_with_sync(
            threads_dir, args.topic, f"ack: {args.topic}",
            lambda: ack(
                args.topic, threads_dir=threads_dir,
                agent=args.agent, role=args.role, title=args.title,
                entry_type=args.entry_type, body=body,
                status=args.status, ball=args.ball,
                registry=registry, entry_id=str(ULID()),
            ),
            no_sync=args.no_sync,
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "handoff":
        from ulid import ULID
        from .commands_graph import handoff
        from .path_resolver import resolve_threads_dir
        from .agents import _load_agents_registry

        threads_dir = resolve_threads_dir(args.threads_dir)
        registry = _load_agents_registry(args.agents_file) if hasattr(args, 'agents_file') and args.agents_file else None
        out = _cli_write_with_sync(
            threads_dir, args.topic, f"handoff: {args.topic}",
            lambda: handoff(
                args.topic, threads_dir=threads_dir,
                agent=args.agent, role=args.role, note=args.note,
                registry=registry, entry_id=str(ULID()),
            ),
            no_sync=args.no_sync,
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "check-branches":
        from pathlib import Path
        from .commands import check_branches

        code_root = Path(args.code_root).resolve() if args.code_root else None
        result = check_branches(code_root=code_root, include_merged=args.include_merged)
        print(result)
        sys.exit(0)

    if args.cmd == "check-branch":
        from pathlib import Path
        from .commands import check_branch

        code_root = Path(args.code_root).resolve() if args.code_root else None
        result = check_branch(args.branch, code_root=code_root)
        print(result)
        sys.exit(0)

    if args.cmd == "merge-branch":
        from pathlib import Path
        from .commands import merge_branch

        code_root = Path(args.code_root).resolve() if args.code_root else None
        result = merge_branch(args.branch, code_root=code_root, force=args.force)
        print(result)
        sys.exit(0)

    if args.cmd == "archive-branch":
        from pathlib import Path
        from .commands import archive_branch

        code_root = Path(args.code_root).resolve() if args.code_root else None
        result = archive_branch(args.branch, code_root=code_root, abandon=args.abandon, force=args.force)
        print(result)
        sys.exit(0)

    if args.cmd == "install-hooks":
        from pathlib import Path
        from .commands import install_hooks

        code_root = Path(args.code_root).resolve() if args.code_root else None
        hooks_dir = Path(args.hooks_dir).resolve() if args.hooks_dir else None
        result = install_hooks(code_root=code_root, hooks_dir=hooks_dir, force=args.force)
        print(result)
        sys.exit(0)

    if args.cmd == "roles":
        roles_cmd = getattr(args, "roles_cmd", None)
        if not roles_cmd:
            p_roles.print_help()
            sys.exit(1)
        if roles_cmd == "init":
            from pathlib import Path
            from .commands import roles_init

            project_path = Path(args.project_path) if args.project_path else Path.cwd()
            sys.exit(roles_init(project_path=project_path, force=args.force))

    if args.cmd == "migrate":
        from watercooler.migration.cli import cmd_migrate
        sys.exit(cmd_migrate(args))

    if args.cmd == "config":
        from pathlib import Path
        import json as json_module
        import shutil

        if not args.config_cmd:
            print("Usage: watercooler config {init|show|validate}")
            sys.exit(0)

        if args.config_cmd == "init":
            from .config_loader import ensure_config_dir, CONFIG_FILENAME

            # Get template path
            template_path = Path(__file__).parent / "templates" / "config.example.toml"
            if not template_path.exists():
                print(f"❌ Template not found: {template_path}", file=sys.stderr)
                sys.exit(1)

            # Determine target (default to user config)
            if args.project:
                config_dir = ensure_config_dir(user=False, project_path=Path.cwd())
                target_path = config_dir / CONFIG_FILENAME
                location = "project"
            else:
                config_dir = ensure_config_dir(user=True)
                target_path = config_dir / CONFIG_FILENAME
                location = "user"

            if target_path.exists() and not args.force:
                print(f"❌ Config already exists: {target_path}", file=sys.stderr)
                print("Use --force to overwrite.", file=sys.stderr)
                sys.exit(1)

            shutil.copy(template_path, target_path)
            print(f"✅ Created {location} config: {target_path}")
            print(f"   Edit this file to customize Watercooler settings.")
            sys.exit(0)

        if args.config_cmd == "show":
            from .config_loader import load_config, get_config_paths, ConfigError

            project_path = Path(args.project_path) if args.project_path else None

            if args.sources:
                paths = get_config_paths(project_path)
                print("Config sources (in priority order):")
                print()
                for name, path in paths.items():
                    if path and path.exists():
                        print(f"  ✓ {name}: {path}")
                    elif path:
                        print(f"  ✗ {name}: {path} (not found)")
                    else:
                        print(f"  - {name}: (not applicable)")
                print()
                print("Environment variables override all file configs.")
                sys.exit(0)

            try:
                config = load_config(project_path)
            except ConfigError as e:
                print(f"❌ Config error: {e}", file=sys.stderr)
                sys.exit(1)

            if args.as_json:
                print(json_module.dumps(config.model_dump(), indent=2))
            else:
                # Use tomlkit for proper TOML output that stays in sync with schema
                try:
                    import tomlkit
                    doc = tomlkit.document()
                    doc.add(tomlkit.comment(" Watercooler Configuration (resolved)"))
                    doc.add(tomlkit.nl())

                    # Convert Pydantic model to dict, using by_alias for 'async' field
                    config_dict = config.model_dump(by_alias=True)

                    def _dict_to_toml_table(d: dict) -> tomlkit.items.Table:
                        """Recursively convert dict to tomlkit table, skipping None values."""
                        t = tomlkit.table()
                        for k, v in d.items():
                            if v is None:
                                continue
                            if isinstance(v, dict):
                                t.add(k, _dict_to_toml_table(v))
                            else:
                                t.add(k, v)
                        return t

                    for section, values in config_dict.items():
                        if isinstance(values, dict):
                            doc.add(section, _dict_to_toml_table(values))
                        elif values is not None:
                            doc.add(section, values)

                    print(tomlkit.dumps(doc))
                except ImportError:
                    # Fallback if tomlkit not installed
                    print("# Watercooler Configuration (resolved)")
                    print("# Note: Install tomlkit for proper TOML formatting")
                    print()
                    print(json_module.dumps(config.model_dump(), indent=2))

            sys.exit(0)

        if args.config_cmd == "validate":
            from .config_loader import load_config, get_config_paths, ConfigError

            project_path = Path(args.project_path) if args.project_path else None
            paths = get_config_paths(project_path)

            errors = []
            warnings = []

            # Check which configs exist
            found_any = False
            for name, path in paths.items():
                if path and path.exists():
                    found_any = True
                    print(f"  ✓ Found: {path}")

            if not found_any:
                warnings.append("No config files found. Using defaults.")

            # Try to load and validate
            try:
                config = load_config(project_path)
                print()
                print("✓ Configuration is valid.")

                # Check for potential issues
                if config.mcp.transport == "http" and config.mcp.port < 1024:
                    warnings.append(f"Port {config.mcp.port} requires root privileges.")

                if config.validation.fail_on_violation:
                    warnings.append("fail_on_violation=true: Invalid entries will cause errors.")

            except ConfigError as e:
                errors.append(str(e))

            # Report
            if warnings:
                print()
                print("Warnings:")
                for w in warnings:
                    print(f"  ⚠ {w}")

            if errors:
                print()
                print("Errors:", file=sys.stderr)
                for e in errors:
                    print(f"  ❌ {e}", file=sys.stderr)
                sys.exit(1)

            if args.strict and warnings:
                print()
                print("--strict: Treating warnings as errors.", file=sys.stderr)
                sys.exit(1)

            sys.exit(0)

    if args.cmd == "slack":
        from .slack_cli import slack_setup, slack_test, slack_status, slack_disable

        if not args.slack_cmd:
            print("Usage: watercooler slack {setup|test|status|disable}")
            sys.exit(0)

        if args.slack_cmd == "setup":
            code = slack_setup(webhook_url=args.webhook_url if hasattr(args, 'webhook_url') else None)
            sys.exit(code)

        if args.slack_cmd == "test":
            code = slack_test()
            sys.exit(code)

        if args.slack_cmd == "status":
            code = slack_status()
            sys.exit(code)

        if args.slack_cmd == "disable":
            code = slack_disable()
            sys.exit(code)

    if args.cmd == "memory":
        from pathlib import Path
        from .path_resolver import resolve_threads_dir

        if not args.memory_cmd:
            print("Usage: watercooler memory {build|export|stats}")
            sys.exit(0)

        if args.memory_cmd == "build":
            MemoryGraph, GraphConfig = _import_memory("MemoryGraph", "GraphConfig")

            threads_dir = resolve_threads_dir(args.threads_dir)
            if not threads_dir.exists():
                print(f"❌ Threads directory not found: {threads_dir}", file=sys.stderr)
                sys.exit(1)

            config = GraphConfig(
                generate_summaries=not args.no_summaries,
                generate_embeddings=not args.no_embeddings,
            )

            print(f"Building memory graph from {threads_dir}...")
            graph = MemoryGraph(config)

            try:
                graph.build(threads_dir, branch_context=args.branch)
            except ImportError as e:
                # Transitive dep missing — watercooler_memory is installed but an
                # optional dependency it needs is absent.  Hard-fail: continuing would
                # produce a corrupt/empty graph and mislead the user.
                print(f"❌ Missing transitive dependency: {e}", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                print(f"❌ Build error: {e}", file=sys.stderr)
                sys.exit(1)

            stats = graph.stats()
            print(f"✅ Built graph: {stats['threads']} threads, {stats['entries']} entries, {stats['chunks']} chunks")

            if args.output:
                output_path = Path(args.output)
                graph.save(output_path)
                print(f"   Saved to: {output_path}")

            sys.exit(0)

        if args.memory_cmd == "export":
            MemoryGraph, GraphConfig = _import_memory("MemoryGraph", "GraphConfig")
            (export_to_leanrag,) = _import_memory("export_to_leanrag", module="watercooler_memory.leanrag_export")

            # Load or build graph
            if args.graph:
                graph_path = Path(args.graph)
                if not graph_path.exists():
                    print(f"❌ Graph file not found: {graph_path}", file=sys.stderr)
                    sys.exit(1)
                graph = MemoryGraph.load(graph_path)
            else:
                threads_dir = resolve_threads_dir(args.threads_dir)
                if not threads_dir.exists():
                    print(f"❌ Threads directory not found: {threads_dir}", file=sys.stderr)
                    sys.exit(1)

                print(f"Building graph from {threads_dir}...")
                config = GraphConfig(
                    generate_summaries=True,
                    generate_embeddings=not args.no_embeddings,
                )
                graph = MemoryGraph(config)
                try:
                    graph.build(threads_dir)
                except ImportError as e:
                    print(f"❌ Missing transitive dependency: {e}", file=sys.stderr)
                    sys.exit(1)
                except Exception as e:
                    print(f"❌ Build error: {e}", file=sys.stderr)
                    sys.exit(1)

            output_path = Path(args.output)

            if args.format == "leanrag":
                manifest = export_to_leanrag(
                    graph, output_path, include_embeddings=not args.no_embeddings
                )
                print(f"✅ Exported to LeanRAG format: {output_path}")
                print(f"   {manifest['statistics']['documents']} documents, {manifest['statistics']['chunks']} chunks")
            else:
                graph.save(output_path)
                print(f"✅ Saved graph JSON: {output_path}")

            sys.exit(0)

        if args.memory_cmd == "stats":
            MemoryGraph, GraphConfig = _import_memory("MemoryGraph", "GraphConfig")

            # Load or build graph
            if args.graph:
                graph_path = Path(args.graph)
                if not graph_path.exists():
                    print(f"❌ Graph file not found: {graph_path}", file=sys.stderr)
                    sys.exit(1)
                graph = MemoryGraph.load(graph_path)
            else:
                threads_dir = resolve_threads_dir(args.threads_dir)
                if not threads_dir.exists():
                    print(f"❌ Threads directory not found: {threads_dir}", file=sys.stderr)
                    sys.exit(1)

                config = GraphConfig(
                    generate_summaries=False,
                    generate_embeddings=False,
                )
                graph = MemoryGraph(config)
                graph.build(threads_dir)

            stats = graph.stats()
            print("Memory Graph Statistics:")
            print(f"  Threads:              {stats['threads']}")
            print(f"  Entries:              {stats['entries']}")
            print(f"  Chunks:               {stats['chunks']}")
            print(f"  Edges:                {stats['edges']}")
            print(f"  Hyperedges:           {stats['hyperedges']}")
            print(f"  Entries w/summaries:  {stats['entries_with_summaries']}")
            print(f"  Entries w/embeddings: {stats['entries_with_embeddings']}")
            print(f"  Chunks w/embeddings:  {stats['chunks_with_embeddings']}")
            sys.exit(0)

    if args.cmd == "baseline-graph":
        from pathlib import Path
        from .path_resolver import resolve_threads_dir

        if not args.baseline_cmd:
            print("Usage: watercooler baseline-graph {build|stats}")
            sys.exit(0)

        if args.baseline_cmd == "build":
            from .baseline_graph import export_all_threads, SummarizerConfig

            threads_dir = resolve_threads_dir(args.threads_dir)
            if not threads_dir.exists():
                print(f"Threads directory not found: {threads_dir}", file=sys.stderr)
                sys.exit(1)

            # Default output to threads_dir/graph/baseline
            if args.output:
                output_dir = Path(args.output)
            else:
                output_dir = threads_dir / "graph" / "baseline"

            config = SummarizerConfig(prefer_extractive=args.extractive_only)

            print(f"Building baseline graph from {threads_dir}...")
            if args.extractive_only:
                print("  Mode: extractive only (no LLM)")
            else:
                print(f"  Mode: LLM ({config.api_base})")
            if args.skip_closed:
                print("  Skipping closed threads")

            manifest = export_all_threads(
                threads_dir, output_dir, config, skip_closed=args.skip_closed
            )

            print()
            print(f"Baseline graph built: {output_dir}")
            print(f"  Threads: {manifest['threads_exported']}")
            print(f"  Entries: {manifest['entries_exported']}")
            print(f"  Nodes:   {manifest['nodes_written']}")
            print(f"  Edges:   {manifest['edges_written']}")
            sys.exit(0)

        if args.baseline_cmd == "stats":
            from .baseline_graph import get_thread_stats

            threads_dir = resolve_threads_dir(args.threads_dir)
            if not threads_dir.exists():
                print(f"Threads directory not found: {threads_dir}", file=sys.stderr)
                sys.exit(1)

            stats = get_thread_stats(threads_dir)
            print("Baseline Graph Statistics:")
            print(f"  Threads dir:          {stats['threads_dir']}")
            print(f"  Total threads:        {stats['total_threads']}")
            print(f"  Total entries:        {stats['total_entries']}")
            print(f"  Avg entries/thread:   {stats['avg_entries_per_thread']:.1f}")
            print()
            print("  Status breakdown:")
            for status, count in stats.get('status_breakdown', {}).items():
                print(f"    {status}: {count}")
            sys.exit(0)

    # default: other commands not yet implemented in L1
    print(f"watercooler {args.cmd}: not yet implemented (L1 stub)")
    sys.exit(0)


if __name__ == "__main__":
    main()
