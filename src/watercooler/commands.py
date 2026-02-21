from __future__ import annotations

from pathlib import Path

from .fs import write, thread_path, lock_path_for_topic, is_closed
from .baseline_graph.writer import get_entries_for_thread
from .baseline_graph.reader import list_threads_from_graph, is_graph_available

try:
    from git import Repo, InvalidGitRepositoryError, GitCommandError
except ImportError:
    Repo = None  # type: ignore
    InvalidGitRepositoryError = Exception  # type: ignore
    GitCommandError = Exception  # type: ignore


def _last_entry_by_from_graph(threads_dir: Path, topic: str) -> str | None:
    """Get the agent of the last entry from graph.

    Returns:
        Agent name of last entry, or None if no entries
    """
    entries = get_entries_for_thread(threads_dir, topic)
    if entries:
        return entries[-1].get("agent", None)
    return None


def list_threads(*, threads_dir: Path, open_only: bool | None = None) -> list[tuple[str, str, str, str, Path, bool]]:
    """Return list of (title, status, ball, updated_iso, path, is_new).

    Uses graph as sole source for topic discovery.
    """
    out: list[tuple[str, str, str, str, Path, bool]] = []
    if not threads_dir.exists():
        return out

    if not is_graph_available(threads_dir):
        import sys
        print(
            "watercooler: graph not yet built — run 'wc reindex' to initialise.",
            file=sys.stderr,
        )
        return out

    graph_threads = list_threads_from_graph(threads_dir, open_only)
    for gt in graph_threads:
        p = thread_path(gt.topic, threads_dir)
        who = (_last_entry_by_from_graph(threads_dir, gt.topic) or "").strip().lower()
        is_new = bool(who and who != (gt.ball or "").strip().lower()) and not is_closed(gt.status)
        out.append((gt.title, gt.status, gt.ball, gt.last_updated, p, is_new))
    return out


def reindex(*, threads_dir: Path, out_file: Path | None = None, open_only: bool | None = True) -> Path:
    """Write a Markdown index summarizing threads."""
    rows = list_threads(threads_dir=threads_dir, open_only=open_only)
    out_path = out_file or (threads_dir / "index.md")
    lines = ["# Watercooler Index", "", "Updated | Status | Ball | NEW | Title | Path", "---|---|---|---|---|---"]
    for title, status, ball, updated, path, is_new in rows:
        rel = path.relative_to(threads_dir)
        newcol = "NEW" if is_new else ""
        lines.append(f"{updated} | {status} | {ball} | {newcol} | {title} | {rel}")
    write(out_path, "\n".join(lines) + "\n")
    return out_path


def list_entries(topic: str, threads_dir: Path) -> list[dict[str, str]]:
    """List parsed entries for a thread topic.

    Args:
        topic: Thread topic identifier.
        threads_dir: Path to threads directory.

    Returns:
        List of dicts with keys: entry_id, title, body, timestamp.
    """
    entries = get_entries_for_thread(threads_dir, topic)
    return [
        {
            "entry_id": e.get("entry_id", ""),
            "title": e.get("title", ""),
            "body": e.get("body", ""),
            "timestamp": e.get("timestamp", ""),
        }
        for e in entries
    ]


def search(*, threads_dir: Path, query: str) -> list[tuple[Path, int, str]]:
    """Case-insensitive search across graph entries; returns (path, line_no, line).

    Searches entry bodies from the graph. Returns results in the same
    format as the legacy .md file grep for backward compatibility.

    Note:
        Line numbers are relative to each entry's body (not file-global).
        This is a behavioral change from the legacy .md grep which returned
        file-level line numbers.
    """
    from .baseline_graph import storage

    q = query.lower()
    hits: list[tuple[Path, int, str]] = []

    if not is_graph_available(threads_dir):
        import sys
        print(
            "watercooler: graph not yet built — run 'wc reindex' to initialise.",
            file=sys.stderr,
        )
        return hits

    graph_dir = storage.get_graph_dir(threads_dir)
    topics = storage.list_thread_topics(graph_dir)

    for topic in topics:
        entries = get_entries_for_thread(threads_dir, topic)
        p = thread_path(topic, threads_dir)
        for entry in entries:
            body = entry.get("body", "")
            if not body:
                continue
            for i, line in enumerate(body.splitlines(), start=1):
                if q in line.lower():
                    hits.append((p, i, line))
    return hits


def web_export(*, threads_dir: Path, out_file: Path | None = None, open_only: bool | None = True) -> Path:
    """Export a simple static HTML index summarizing threads."""
    rows = list_threads(threads_dir=threads_dir, open_only=open_only)
    out_path = out_file or (threads_dir / "index.html")
    tbody = []
    for title, status, ball, updated, path, is_new in rows:
        rel = path.relative_to(threads_dir)
        badge = "<strong style=\"color:#b00\">NEW</strong>" if is_new else ""
        tbody.append(
            f"<tr><td>{updated}</td><td>{status}</td><td>{ball}</td><td>{badge}</td><td>{title}</td><td><a href=\"{rel}\">{rel}</a></td></tr>"
        )
    html = """
<!doctype html>
<html lang="en">
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Watercooler Index</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Calibri,sans-serif;margin:2rem}
  table{border-collapse:collapse;width:100%}
  th,td{border:1px solid #ddd;padding:.5rem;text-align:left}
  th{background:#f5f5f5}
  tr:nth-child(even){background:#fafafa}
  code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
</style>
<h1>Watercooler Index</h1>
<table>
  <thead><tr><th>Updated</th><th>Status</th><th>Ball</th><th>NEW</th><th>Title</th><th>Path</th></tr></thead>
  <tbody>
    BODY
  </tbody>
</table>
</html>
""".replace("BODY", "\n    ".join(tbody))
    write(out_path, html)
    return out_path


def unlock(topic: str, *, threads_dir: Path, force: bool = False) -> None:
    """Clear advisory lock for a topic (debugging tool).

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        force: Remove lock even if it appears active

    This command helps recover from stuck locks during development or debugging.
    Use with caution in production environments.
    """
    import sys
    import time

    lp = lock_path_for_topic(topic, threads_dir)

    print(f"Lock path: {lp}")

    if not lp.exists():
        print("No lock file present.")
        return

    # Read lock metadata
    try:
        txt = lp.read_text(encoding="utf-8").strip()
    except Exception:
        txt = "(unreadable)"

    # Get lock age
    try:
        st = lp.stat()
        age = int(time.time() - st.st_mtime)
    except Exception:
        age = -1

    # Check if stale
    from .lock import AdvisoryLock
    al = AdvisoryLock(lp)
    stale = al._is_stale()

    print(f"Contents: {txt}")
    print(f"Age: {age}s; Stale: {stale}")

    if stale or force:
        try:
            lp.unlink()
            print("Lock removed.")
        except Exception as e:
            sys.exit(f"Failed to remove lock: {e}")
    else:
        sys.exit("Lock appears active; re-run with --force to remove anyway.")


def check_branches(*, code_root: Path | None = None, include_merged: bool = False) -> str:
    """Comprehensive audit of branch pairing across entire repo pair.

    Args:
        code_root: Path to code repository directory (default: current directory)
        include_merged: Include fully merged branches in report

    Returns:
        Human-readable report with synced, code-only, threads-only branches
    """
    if Repo is None:
        return "Error: GitPython not available. Install with: pip install GitPython"

    try:
        from watercooler_mcp.config import resolve_thread_context

        code_path = code_root or Path.cwd()
        context = resolve_thread_context(code_path)

        if not context.code_root or not context.threads_dir:
            return "Error: Unable to resolve code and threads repo paths."

        code_repo = Repo(context.code_root, search_parent_directories=True)
        threads_repo = Repo(context.threads_dir, search_parent_directories=True)

        # Get all branches
        code_branches = {b.name for b in code_repo.heads}
        threads_branches = {b.name for b in threads_repo.heads}

        # Categorize branches
        synced = []
        code_only = []
        threads_only = []
        recommendations = []

        # Find synced branches
        for branch in code_branches & threads_branches:
            try:
                code_sha = code_repo.heads[branch].commit.hexsha[:7]
                threads_sha = threads_repo.heads[branch].commit.hexsha[:7]
                synced.append((branch, code_sha, threads_sha))
            except Exception:
                synced.append((branch, "unknown", "unknown"))

        # Find code-only branches
        for branch in code_branches - threads_branches:
            try:
                commits_ahead = len(list(code_repo.iter_commits(f"main..{branch}"))) if "main" in code_branches else 0
                code_only.append((branch, commits_ahead))
                if commits_ahead == 0 and not include_merged:
                    recommendations.append(f"Code branch '{branch}' is fully merged - consider deleting")
                else:
                    recommendations.append(f"Create threads branch '{branch}' to match code branch")
            except Exception:
                code_only.append((branch, 0))

        # Find threads-only branches
        for branch in threads_branches - code_branches:
            try:
                commits_ahead = len(list(threads_repo.iter_commits(f"main..{branch}"))) if "main" in threads_branches else 0
                threads_only.append((branch, commits_ahead))
                if commits_ahead == 0:
                    recommendations.append(f"Threads branch '{branch}' is fully merged - safe to delete")
                else:
                    recommendations.append(f"Code branch '{branch}' was deleted - merge or delete threads branch")
            except Exception:
                threads_only.append((branch, 0))

        # Build report
        lines = []
        lines.append("Branch Pairing Audit")
        lines.append("=" * 60)
        lines.append("")

        if synced:
            lines.append("✅ Synchronized Branches:")
            for branch, code_sha, threads_sha in synced:
                lines.append(f"  - {branch} (code: {code_sha}, threads: {threads_sha})")
            lines.append("")

        if code_only or threads_only:
            lines.append("⚠️  Drift Detected:")
            lines.append("")

            if code_only:
                lines.append("Code-only branches (no threads counterpart):")
                for branch, commits_ahead in code_only:
                    if commits_ahead > 0 or include_merged:
                        lines.append(f"  - {branch} ({commits_ahead} commits ahead of main)")
                        lines.append(f"    └─ Action: Create threads branch or delete if merged")
                lines.append("")

            if threads_only:
                lines.append("Threads-only branches (no code counterpart):")
                for branch, commits_ahead in threads_only:
                    lines.append(f"  - {branch} ({commits_ahead} commits ahead of main)")
                    if commits_ahead == 0:
                        lines.append(f"    └─ Action: Safe to delete (fully merged)")
                    else:
                        lines.append(f"    └─ Action: Merge to another threads branch or delete")
                lines.append("")

        if recommendations:
            lines.append("Recommendations:")
            for rec in recommendations:
                lines.append(f"  - {rec}")
            lines.append("")

        lines.append("=" * 60)
        lines.append(f"Summary: {len(synced)} synced, {len(code_only)} code-only, {len(threads_only)} threads-only")

        return "\n".join(lines)

    except InvalidGitRepositoryError as e:
        return f"Error: Not a git repository: {str(e)}"
    except Exception as e:
        return f"Error auditing branches: {str(e)}"


def check_branch(branch: str, *, code_root: Path | None = None) -> str:
    """Validate branch pairing for a specific branch.

    Args:
        branch: Branch name to check
        code_root: Path to code repository directory (default: current directory)

    Returns:
        Human-readable validation report
    """
    return f"Branch parity validation has been removed. Branch: {branch}"


def merge_branch(branch: str, *, code_root: Path | None = None, force: bool = False) -> str:
    """Merge threads branch to main with safeguards.

    Args:
        branch: Branch name to merge
        code_root: Path to code repository directory (default: current directory)
        force: Skip safety checks (use with caution)

    Returns:
        Operation result message
    """
    if Repo is None:
        return "Error: GitPython not available. Install with: pip install GitPython"

    try:
        from watercooler_mcp.config import resolve_thread_context

        code_path = code_root or Path.cwd()
        context = resolve_thread_context(code_path)

        if not context.code_root or not context.threads_dir:
            return "Error: Unable to resolve code and threads repo paths."

        threads_repo = Repo(context.threads_dir, search_parent_directories=True)

        if branch not in [b.name for b in threads_repo.heads]:
            return f"Error: Branch '{branch}' does not exist in threads repo."

        if "main" not in [b.name for b in threads_repo.heads]:
            return "Error: 'main' branch does not exist in threads repo."

        # Check for OPEN threads
        if not force:
            threads_repo.git.checkout(branch)
            open_threads = []
            graph_threads = list_threads_from_graph(context.threads_dir, open_only=True)
            for gt in graph_threads:
                open_threads.append(gt.topic)

            if open_threads:
                lines = []
                lines.append(f"⚠️  Warning: {len(open_threads)} OPEN threads found on {branch}:")
                for topic in open_threads:
                    lines.append(f"  - {topic}")
                lines.append("")
                lines.append("Recommended actions:")
                lines.append("1. Close threads: watercooler set-status <topic> CLOSED")
                lines.append("2. Move to main: Cherry-pick threads to threads:main")
                lines.append("3. Force merge: watercooler merge-branch <branch> --force")
                lines.append("")
                lines.append("Proceed? [y/N]")
                return "\n".join(lines)

        # Perform merge
        threads_repo.git.checkout("main")
        try:
            from git import Actor
            # Use watercooler bot identity for automated merges
            author = Actor("Watercooler Bot", "watercooler@watercoolerdev.com")
            env = {
                'GIT_AUTHOR_NAME': author.name,
                'GIT_AUTHOR_EMAIL': author.email,
                'GIT_COMMITTER_NAME': author.name,
                'GIT_COMMITTER_EMAIL': author.email,
            }
            threads_repo.git.merge(branch, '--no-ff', '-m', f"Merge {branch} into main", env=env)
            return f"✅ Merged '{branch}' into 'main' in threads repo."
        except GitCommandError as e:
            error_str = str(e)
            # Check if this is a merge conflict
            if "CONFLICT" in error_str or threads_repo.is_dirty():
                # Detect conflicts in thread files
                conflicted_files = []
                try:
                    for item in threads_repo.index.unmerged_blobs():
                        conflicted_files.append(item.path)
                except Exception:
                    pass
                
                if conflicted_files:
                    conflict_msg = (
                        f"⚠️  Merge conflict detected in {len(conflicted_files)} file(s):\n"
                        f"  {', '.join(conflicted_files)}\n\n"
                        f"Append-only conflict resolution:\n"
                        f"  - Both entries will be preserved in chronological order\n"
                        f"  - Status/Ball conflicts: Higher severity status wins, last entry author gets ball\n"
                        f"  - Manual resolution may be required for complex conflicts\n\n"
                        f"To resolve:\n"
                        f"  1. Review conflicted files\n"
                        f"  2. Keep both entries in chronological order\n"
                        f"  3. Resolve header conflicts (status/ball) manually\n"
                        f"  4. Run: git add <files> && git commit"
                    )
                    return f"Merge conflict: {error_str}\n\n{conflict_msg}"
            
            return f"Error merging branch: {error_str}"

    except InvalidGitRepositoryError as e:
        return f"Error: Not a git repository: {str(e)}"
    except Exception as e:
        return f"Error merging branch: {str(e)}"


def archive_branch(branch: str, *, code_root: Path | None = None, abandon: bool = False, force: bool = False) -> str:
    """Close OPEN threads, merge to main, then delete branch.

    Args:
        branch: Branch name to archive
        code_root: Path to code repository directory (default: current directory)
        abandon: Set OPEN threads to ABANDONED status instead of CLOSED
        force: Skip confirmation prompts

    Returns:
        Operation result message
    """
    if Repo is None:
        return "Error: GitPython not available. Install with: pip install GitPython"

    try:
        from watercooler_mcp.config import resolve_thread_context

        code_path = code_root or Path.cwd()
        context = resolve_thread_context(code_path)

        if not context.code_root or not context.threads_dir:
            return "Error: Unable to resolve code and threads repo paths."

        threads_repo = Repo(context.threads_dir, search_parent_directories=True)

        if branch not in [b.name for b in threads_repo.heads]:
            return f"Error: Branch '{branch}' does not exist in threads repo."

        # Checkout branch and find OPEN threads
        threads_repo.git.checkout(branch)
        open_threads = []
        graph_threads = list_threads_from_graph(context.threads_dir, open_only=True)
        for gt in graph_threads:
            open_threads.append(gt.topic)

        if open_threads:
            status_to_set = "ABANDONED" if abandon else "CLOSED"
            if not force:
                lines = []
                lines.append(f"Found {len(open_threads)} OPEN threads on {branch}:")
                for topic in open_threads:
                    lines.append(f"  - {topic}")
                lines.append("")
                lines.append(f"These will be set to {status_to_set} status.")
                lines.append("Proceed? [y/N]")
                return "\n".join(lines)

            # Close threads (use graph-canonical set_status)
            from .commands_graph import set_status as graph_set_status
            for topic in open_threads:
                try:
                    graph_set_status(
                        topic,
                        threads_dir=context.threads_dir,
                        status=status_to_set,
                    )
                except Exception as e:
                    return f"Error closing thread {topic}: {str(e)}"

            # Commit the status changes
            try:
                from git import Actor
                threads_repo.index.add([f"{topic}.md" for topic in open_threads])
                commit_msg = f"Archive: set {len(open_threads)} threads to {status_to_set}"
                # Use watercooler bot identity for automated commits
                author = Actor("Watercooler Bot", "watercooler@watercoolerdev.com")
                threads_repo.index.commit(commit_msg, author=author, committer=author)
            except Exception as e:
                return f"Error committing status changes: {str(e)}"

        # Merge to main
        if "main" in [b.name for b in threads_repo.heads]:
            threads_repo.git.checkout("main")
            try:
                from git import Actor
                # Use watercooler bot identity for automated merges
                author = Actor("Watercooler Bot", "watercooler@watercoolerdev.com")
                env = {
                    'GIT_AUTHOR_NAME': author.name,
                    'GIT_AUTHOR_EMAIL': author.email,
                    'GIT_COMMITTER_NAME': author.name,
                    'GIT_COMMITTER_EMAIL': author.email,
                }
                threads_repo.git.merge(branch, '--no-ff', '-m', f"Archive {branch} to main", env=env)
            except GitCommandError as e:
                return f"Error merging branch: {str(e)}"

        # Delete branch
        if threads_repo.active_branch.name == branch:
            if "main" in [b.name for b in threads_repo.heads]:
                threads_repo.git.checkout("main")
            else:
                threads_repo.git.checkout('-b', 'main')

        threads_repo.git.branch('-D', branch)

        lines = []
        lines.append(f"✅ Archived branch '{branch}'")
        if open_threads:
            lines.append(f"  - Set {len(open_threads)} threads to {status_to_set}")
        lines.append(f"  - Merged to main")
        lines.append(f"  - Deleted branch")

        return "\n".join(lines)

    except InvalidGitRepositoryError as e:
        return f"Error: Not a git repository: {str(e)}"
    except Exception as e:
        return f"Error archiving branch: {str(e)}"


def install_hooks(*, code_root: Path | None = None, hooks_dir: Path | None = None, force: bool = False) -> str:
    """Install git hooks for branch pairing validation.
    
    Args:
        code_root: Path to code repository directory (default: current directory)
        hooks_dir: Git hooks directory (default: .git/hooks)
        force: Overwrite existing hooks
        
    Returns:
        Installation result message
    """
    try:
        from pathlib import Path
        import shutil
        import stat
        
        code_path = code_root or Path.cwd()
        repo_path = code_path / ".git"
        
        if not repo_path.exists():
            return f"Error: Not a git repository: {code_path}"
        
        hooks_path = hooks_dir or (repo_path / "hooks")
        hooks_path.mkdir(parents=True, exist_ok=True)
        
        # Get template directory
        from .path_resolver import resolve_templates_dir
        templates_dir = resolve_templates_dir()
        
        installed = []
        skipped = []
        
        # Install pre-push hook
        pre_push_src = templates_dir / "pre-push"
        pre_push_dst = hooks_path / "pre-push"
        
        if pre_push_src.exists():
            if pre_push_dst.exists() and not force:
                skipped.append("pre-push (already exists, use --force to overwrite)")
            else:
                shutil.copy2(pre_push_src, pre_push_dst)
                # Make executable
                pre_push_dst.chmod(pre_push_dst.stat().st_mode | stat.S_IEXEC)
                installed.append("pre-push")
        else:
            return f"Error: Template not found: {pre_push_src}"
        
        # Install pre-merge hook
        pre_merge_src = templates_dir / "pre-merge"
        pre_merge_dst = hooks_path / "pre-merge"
        
        if pre_merge_src.exists():
            if pre_merge_dst.exists() and not force:
                skipped.append("pre-merge (already exists, use --force to overwrite)")
            else:
                shutil.copy2(pre_merge_src, pre_merge_dst)
                # Make executable
                pre_merge_dst.chmod(pre_merge_dst.stat().st_mode | stat.S_IEXEC)
                installed.append("pre-merge")
        
        lines = []
        if installed:
            lines.append(f"✅ Installed {len(installed)} hook(s): {', '.join(installed)}")
        if skipped:
            lines.append(f"⏭️  Skipped {len(skipped)} hook(s): {', '.join(skipped)}")
        
        if installed:
            lines.append("")
            lines.append("Hooks will validate branch pairing before git operations.")
            lines.append("To disable, remove hooks from .git/hooks/")
        
        return "\n".join(lines) if lines else "No hooks installed"
        
    except Exception as e:
        return f"Error installing hooks: {str(e)}"
