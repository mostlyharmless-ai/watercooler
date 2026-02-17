#!/usr/bin/env python3
"""Migrate from a separate threads repo to orphan branch mode.

Copies all thread files and graph data from an existing separate
threads repo (the old branch-mirroring architecture) to a single
watercooler/threads orphan branch on the code repo. Tags graph
entries with code_branch metadata parsed from commit footers.

Usage:
    # Dry run (default) — shows what would be migrated
    python scripts/migrate_to_orphan_branch.py /path/to/code-repo /path/to/threads-repo

    # Execute the migration
    python scripts/migrate_to_orphan_branch.py /path/to/code-repo /path/to/threads-repo --execute

    # With verbose output
    python scripts/migrate_to_orphan_branch.py /path/to/code-repo /path/to/threads-repo --execute -v

Prerequisites:
    - The threads repo should have its default branch (main) checked out
    - The code repo must have a git remote configured
    - Git must be installed and on PATH
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


ORPHAN_BRANCH_NAME = "watercooler/threads"
WORKTREE_BASE = Path("~/.watercooler/worktrees").expanduser()


def _run_git(args: list, cwd: Path, verbose: bool = False) -> Optional[str]:
    """Run a git command and return stdout, or None on failure."""
    if verbose:
        print(f"  git {' '.join(args)}", file=sys.stderr)
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if verbose:
            print(f"  git error: {e.stderr.strip()}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("Error: git not found on PATH", file=sys.stderr)
        return None


def _parse_commit_footers(repo_path: Path) -> Dict[str, str]:
    """Parse Code-Branch footers from all commits in the threads repo.

    Builds a mapping of Watercooler-Entry-ID -> Code-Branch by scanning
    commit messages for footer lines.

    Args:
        repo_path: Path to the threads git repository

    Returns:
        Dict mapping entry_id to code_branch
    """
    entry_branch_map: Dict[str, str] = {}

    # Use git log to get all commit messages with footers
    log_output = _run_git(
        ["log", "--all", "--format=%B%x00"],  # NUL-separated messages
        repo_path,
    )
    if not log_output:
        return entry_branch_map

    for message in log_output.split("\x00"):
        entry_id = None
        code_branch = None
        for line in message.split("\n"):
            line = line.strip()
            if line.startswith("Watercooler-Entry-ID:"):
                entry_id = line.split(":", 1)[1].strip()
            elif line.startswith("Code-Branch:"):
                code_branch = line.split(":", 1)[1].strip()

        if entry_id and code_branch:
            entry_branch_map[entry_id] = code_branch

    return entry_branch_map


def _tag_graph_entries(graph_dir: Path, entry_branch_map: Dict[str, str]) -> int:
    """Add code_branch to graph entry nodes that don't have one.

    Modifies entries.jsonl files in-place, adding code_branch from
    the commit footer mapping.

    Args:
        graph_dir: Path to graph/baseline/ directory
        entry_branch_map: Mapping of entry_id -> code_branch

    Returns:
        Number of entries tagged
    """
    tagged = 0
    threads_dir = graph_dir / "threads"
    if not threads_dir.exists():
        return 0

    for topic_dir in threads_dir.iterdir():
        if not topic_dir.is_dir():
            continue
        entries_file = topic_dir / "entries.jsonl"
        if not entries_file.exists():
            continue

        lines = entries_file.read_text().splitlines()
        updated_lines = []
        modified = False

        for line in lines:
            if not line.strip():
                updated_lines.append(line)
                continue
            try:
                node = json.loads(line)
                if "code_branch" not in node:
                    eid = node.get("entry_id", "")
                    if eid in entry_branch_map:
                        node["code_branch"] = entry_branch_map[eid]
                        modified = True
                        tagged += 1
                updated_lines.append(json.dumps(node, ensure_ascii=False))
            except json.JSONDecodeError:
                updated_lines.append(line)

        if modified:
            entries_file.write_text("\n".join(updated_lines) + "\n")

    return tagged


def _worktree_path_for(code_root: Path) -> Path:
    """Compute the worktree path for a code repo."""
    return WORKTREE_BASE / code_root.name


def _orphan_branch_exists(code_root: Path) -> bool:
    """Check if the orphan branch exists (local or remote)."""
    result = _run_git(["branch", "-a", "--list", f"*{ORPHAN_BRANCH_NAME}*"], code_root)
    return bool(result and ORPHAN_BRANCH_NAME in result)


def _create_orphan_branch(code_root: Path, verbose: bool = False) -> bool:
    """Create the orphan branch with an empty initial commit."""
    if verbose:
        print(f"Creating orphan branch '{ORPHAN_BRANCH_NAME}'...", file=sys.stderr)

    wt_path = _worktree_path_for(code_root)
    wt_path.mkdir(parents=True, exist_ok=True)

    # Create orphan branch and worktree in one step
    result = _run_git(
        ["worktree", "add", "--orphan", "-b", ORPHAN_BRANCH_NAME, str(wt_path)],
        code_root,
        verbose=verbose,
    )
    if result is None:
        # Fallback for older git: create orphan branch manually
        if verbose:
            print("  Falling back to manual orphan creation...", file=sys.stderr)
        _run_git(["worktree", "add", "--detach", str(wt_path)], code_root, verbose=verbose)
        _run_git(["checkout", "--orphan", ORPHAN_BRANCH_NAME], wt_path, verbose=verbose)
        _run_git(["rm", "-rf", "."], wt_path, verbose=verbose)

    # Create initial empty commit in the worktree
    _run_git(["commit", "--allow-empty", "-m", "Initialize watercooler threads"], wt_path, verbose=verbose)

    # Push to origin if remote exists
    _run_git(["push", "-u", "origin", ORPHAN_BRANCH_NAME], wt_path, verbose=verbose)

    if verbose:
        print(f"  Orphan branch '{ORPHAN_BRANCH_NAME}' created", file=sys.stderr)
    return True


def _ensure_worktree(code_root: Path, verbose: bool = False) -> Optional[Path]:
    """Ensure the orphan branch worktree exists, creating it if needed."""
    wt_path = _worktree_path_for(code_root)

    # Check if worktree already exists and is valid
    if wt_path.exists() and (wt_path / ".git").exists():
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], wt_path)
        if branch == ORPHAN_BRANCH_NAME:
            return wt_path
        if verbose:
            print(f"  Worktree on wrong branch '{branch}', recreating...", file=sys.stderr)
        _run_git(["worktree", "remove", "--force", str(wt_path)], code_root, verbose=verbose)

    # Check if orphan branch exists
    if not _orphan_branch_exists(code_root):
        try:
            _create_orphan_branch(code_root, verbose=verbose)
        except Exception as e:
            print(f"Error: Failed to create orphan branch: {e}", file=sys.stderr)
            return None
    else:
        # Branch exists but no worktree — create worktree
        wt_path.mkdir(parents=True, exist_ok=True)
        result = _run_git(
            ["worktree", "add", str(wt_path), ORPHAN_BRANCH_NAME],
            code_root,
            verbose=verbose,
        )
        if result is None:
            print(f"Error: Failed to create worktree at {wt_path}", file=sys.stderr)
            return None

    if wt_path.exists() and (wt_path / ".git").exists():
        return wt_path

    return None


def migrate(
    code_path: str,
    threads_repo_path: str,
    dry_run: bool = True,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Migrate from separate threads repo to orphan branch mode.

    Copies all thread files and graph data from the separate threads repo
    to a watercooler/threads orphan branch on the code repo. Tags graph
    entries with code_branch metadata parsed from commit footers.

    Args:
        code_path: Path to the code repository
        threads_repo_path: Path to the existing separate threads repo clone
        dry_run: If True, report what would happen without executing
        verbose: Print progress to stderr

    Returns:
        Dict with migration results
    """
    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "branches_found": 0,
        "threads_copied": 0,
        "graph_files_copied": 0,
        "other_files_copied": 0,
        "entries_tagged": 0,
        "entry_branch_mappings": 0,
        "errors": [],
    }

    # 1. Validate threads repo
    threads_repo = Path(threads_repo_path).expanduser().resolve()
    if not threads_repo.exists():
        result["success"] = False
        result["error"] = f"Threads repo path does not exist: {threads_repo}"
        return result

    if not (threads_repo / ".git").exists():
        git_check = _run_git(["rev-parse", "--git-dir"], threads_repo)
        if git_check is None:
            result["success"] = False
            result["error"] = f"Not a git repository: {threads_repo}"
            return result

    # 2. Validate code repo
    code_root = Path(code_path).expanduser().resolve()
    if not code_root.exists():
        result["success"] = False
        result["error"] = f"Code repo path does not exist: {code_root}"
        return result

    if verbose:
        print(f"Code repo:    {code_root}", file=sys.stderr)
        print(f"Threads repo: {threads_repo}", file=sys.stderr)

    # 3. Count branches in threads repo
    branch_output = _run_git(["branch", "-r", "--list", "origin/*"], threads_repo)
    if branch_output:
        branches = [
            b.strip().replace("origin/", "")
            for b in branch_output.split("\n")
            if b.strip() and "HEAD" not in b
        ]
    else:
        branches = []
    result["branches_found"] = len(branches)

    if verbose:
        print(f"Branches in threads repo: {len(branches)}", file=sys.stderr)

    # 4. Parse commit footers for entry_id -> code_branch mapping
    entry_branch_map = _parse_commit_footers(threads_repo)
    result["entry_branch_mappings"] = len(entry_branch_map)

    if verbose:
        print(f"Entry-branch mappings from footers: {len(entry_branch_map)}", file=sys.stderr)

    # 5. Collect files from the current checkout of threads repo
    skip_patterns = {".git"}
    thread_files: List[Path] = []
    graph_files: List[Path] = []
    other_files: List[Path] = []

    for item in threads_repo.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(threads_repo)
        if any(part in skip_patterns for part in rel.parts):
            continue
        if rel.name.startswith(".migration_checkpoint"):
            continue

        rel_str = str(rel)
        if rel_str.startswith("graph/"):
            graph_files.append(rel)
        elif rel.suffix == ".md":
            thread_files.append(rel)
        else:
            other_files.append(rel)

    result["threads_found"] = len(thread_files)
    result["graph_files_found"] = len(graph_files)
    result["other_files_found"] = len(other_files)

    if verbose:
        print(f"Files found: {len(thread_files)} threads, {len(graph_files)} graph, {len(other_files)} other", file=sys.stderr)

    if dry_run:
        result["success"] = True
        result["thread_files"] = [str(f) for f in thread_files[:50]]
        if len(thread_files) > 50:
            result["thread_files_truncated"] = True
        if branches:
            result["branches"] = branches[:20]
        result["entry_branch_sample"] = dict(list(entry_branch_map.items())[:10])
        return result

    # 6. Create orphan branch worktree on code repo
    wt_dir = _ensure_worktree(code_root, verbose=verbose)
    if wt_dir is None:
        result["success"] = False
        result["error"] = "Failed to create orphan branch worktree on code repo"
        return result
    result["worktree_path"] = str(wt_dir)

    if verbose:
        print(f"Worktree: {wt_dir}", file=sys.stderr)

    # 7. Copy all files to worktree
    for rel in thread_files:
        src = threads_repo / rel
        dst = wt_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result["threads_copied"] += 1
        except (OSError, IOError) as e:
            result["errors"].append(f"Failed to copy {rel}: {e}")

    for rel in graph_files:
        src = threads_repo / rel
        dst = wt_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result["graph_files_copied"] += 1
        except (OSError, IOError) as e:
            result["errors"].append(f"Failed to copy graph/{rel}: {e}")

    for rel in other_files:
        src = threads_repo / rel
        dst = wt_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result["other_files_copied"] += 1
        except (OSError, IOError) as e:
            result["errors"].append(f"Failed to copy {rel}: {e}")

    if verbose:
        print(
            f"Copied: {result['threads_copied']} threads, "
            f"{result['graph_files_copied']} graph, "
            f"{result['other_files_copied']} other",
            file=sys.stderr,
        )

    # 8. Tag graph entries with code_branch from commit footers
    graph_baseline = wt_dir / "graph" / "baseline"
    if graph_baseline.exists() and entry_branch_map:
        tagged = _tag_graph_entries(graph_baseline, entry_branch_map)
        result["entries_tagged"] = tagged
        if verbose:
            print(f"Tagged {tagged} graph entries with code_branch", file=sys.stderr)

    # 9. Commit and push
    _run_git(["add", "-A"], wt_dir, verbose=verbose)

    commit_msg = (
        "Migrate threads from separate repo to orphan branch\n\n"
        f"Threads: {result['threads_copied']}\n"
        f"Graph files: {result['graph_files_copied']}\n"
        f"Entries tagged with code_branch: {result['entries_tagged']}\n"
        f"Source: {threads_repo}\n"
    )
    commit_result = _run_git(["commit", "-m", commit_msg], wt_dir, verbose=verbose)
    if commit_result is None:
        result["commit"] = "nothing_to_commit"
    else:
        result["commit"] = "success"

    push_result = _run_git(["push", "origin", ORPHAN_BRANCH_NAME], wt_dir, verbose=verbose)
    result["push"] = "success" if push_result is not None else "failed"
    if push_result is None:
        result["errors"].append(
            "Push failed. The orphan branch may need manual pushing, "
            "or the remote may not be configured."
        )

    result["success"] = len(result["errors"]) == 0
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate from separate threads repo to orphan branch mode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run (default)
  %(prog)s /path/to/code-repo /path/to/threads-repo

  # Execute migration
  %(prog)s /path/to/code-repo /path/to/threads-repo --execute

  # Verbose dry run
  %(prog)s /path/to/code-repo /path/to/threads-repo -v
        """,
    )
    parser.add_argument("code_path", help="Path to the code repository")
    parser.add_argument("threads_repo_path", help="Path to the existing threads repo clone")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually execute the migration (default is dry run)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print progress to stderr",
    )

    args = parser.parse_args()
    dry_run = not args.execute

    if dry_run:
        print("=== DRY RUN (use --execute to migrate) ===\n", file=sys.stderr)

    result = migrate(
        code_path=args.code_path,
        threads_repo_path=args.threads_repo_path,
        dry_run=dry_run,
        verbose=args.verbose,
    )

    print(json.dumps(result, indent=2))

    if result.get("success"):
        if dry_run:
            print(
                f"\nDry run complete. Would migrate {result.get('threads_found', 0)} threads, "
                f"{result.get('graph_files_found', 0)} graph files.",
                file=sys.stderr,
            )
        else:
            print(
                f"\nMigration complete. Copied {result.get('threads_copied', 0)} threads, "
                f"{result.get('graph_files_copied', 0)} graph files, "
                f"tagged {result.get('entries_tagged', 0)} entries.",
                file=sys.stderr,
            )
        return 0
    else:
        print(f"\nMigration failed: {result.get('error', 'unknown')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
