from __future__ import annotations

from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

from .fs import write, thread_path, find_thread_path, lock_path_for_topic, utcnow_iso, read_body, is_closed
from .lock import AdvisoryLock
from .baseline_graph.writer import get_thread_from_graph, get_entries_for_thread
from .baseline_graph.reader import list_threads_from_graph, is_graph_available
from .agents import _counterpart_of, _canonical_agent, _default_agent_and_role
from .path_resolver import load_template, resolve_templates_dir
from .templates import _fill_template

try:
    from git import Repo, InvalidGitRepositoryError, GitCommandError
except ImportError:
    Repo = None  # type: ignore
    InvalidGitRepositoryError = Exception  # type: ignore
    GitCommandError = Exception  # type: ignore


def _thread_meta_from_graph(threads_dir: Path, topic: str) -> tuple[str, str, str, str]:
    """Get thread metadata from graph.

    Graph is the sole source of truth. Returns defaults if thread not found.

    Returns:
        Tuple of (title, status, ball, last_updated) or defaults if not found.
    """
    thread = get_thread_from_graph(threads_dir, topic)
    if thread:
        return (
            thread.get("title", topic),
            thread.get("status", "OPEN"),
            thread.get("ball", ""),
            thread.get("last_updated", ""),
        )
    return (topic, "OPEN", "", "")


def _last_entry_by_from_graph(threads_dir: Path, topic: str) -> str | None:
    """Get the agent of the last entry from graph.

    Returns:
        Agent name of last entry, or None if no entries
    """
    entries = get_entries_for_thread(threads_dir, topic)
    if entries:
        return entries[-1].get("agent", None)
    return None


def _header_split(text: str) -> tuple[str, str]:
    """Split markdown text into header and body sections."""
    parts = text.split("\n\n", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _replace_header_line(block: str, key: str, value: str) -> str:
    """Replace or add a header line with given key and value."""
    lines = block.splitlines()
    pref = f"{key}:"
    replaced = False
    for i, ln in enumerate(lines):
        if ln.lower().startswith(pref.lower()):
            lines[i] = f"{key}: {value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _bump_header(text: str, *, status: str | None = None, ball: str | None = None) -> str:
    """Update status and/or ball in markdown header."""
    header, body = _header_split(text)
    if status is not None:
        header = _replace_header_line(header, "Status", status)
    if ball is not None:
        header = _replace_header_line(header, "Ball", ball)
    return header + "\n\n" + (body or "")


def init_thread(
    topic: str,
    *,
    threads_dir: Path,
    title: Optional[str] = None,
    status: str = "OPEN",
    ball: str = "codex",
    body: str | None = None,
    owner: str | None = None,
    participants: str | None = None,
    templates_dir: Path | None = None,
) -> Path:
    """Initialize a new thread using the topic thread template.

    .. deprecated::
        Use ``commands_graph.init_thread`` instead. This legacy version writes
        only to .md files without creating graph data. All runtime callers
        should use the graph-canonical version.

    Args:
        topic: Thread topic identifier
        threads_dir: Directory containing threads
        title: Optional title override
        status: Initial status (default: "OPEN")
        ball: Initial ball owner (default: "codex")
        body: Optional initial body text
        owner: Thread owner (default: "Team")
        participants: Comma-separated list of participants
        templates_dir: Optional templates directory override
    """
    threads_dir.mkdir(parents=True, exist_ok=True)
    tp = thread_path(topic, threads_dir)
    if tp.exists():
        return tp

    lp = lock_path_for_topic(topic, threads_dir)
    with AdvisoryLock(lp, timeout=2, ttl=10, force_break=False):
        if tp.exists():
            return tp

        # Load thread template
        try:
            template = load_template("_TEMPLATE_topic_thread.md", templates_dir)
        except FileNotFoundError:
            # Fallback to simple format if template not found
            hdr_title = title or topic.replace("-", " ").strip()
            now = utcnow_iso()
            content = (
                f"Title: {hdr_title}\n"
                f"Status: {status.upper()}\n"
                f"Ball: {ball}\n"
                f"Updated: {now}\n\n"
                f"# {hdr_title}\n"
            )
            if body:
                content += body
            write(tp, content)
            return tp

        # Fill template
        now = utcnow_iso()
        hdr_title = title or topic.replace("-", " ").strip()
        mapping = {
            "TOPIC": topic,
            "topic": topic,
            "Short title": hdr_title,
            "OWNER": owner or "Team",
            "PARTICIPANTS": participants or "Team, Codex, Claude",
            "NOWUTC": now,
            "UTC": now,
            "BALL": ball,
            "STATUS": status.upper(),  # Always uppercase for consistency
        }
        content = _fill_template(template, mapping)

        # Append body if provided
        if body:
            content = content.rstrip() + "\n\n" + body.rstrip() + "\n"

        write(tp, content)
    return tp


def append_entry(
    topic: str,
    *,
    threads_dir: Path,
    agent: str,
    role: str,
    title: str,
    entry_type: str = "Note",
    body: str,
    status: str | None = None,
    ball: str | None = None,
    templates_dir: Path | None = None,
    registry: dict | None = None,
    user_tag: str | None = None,
    entry_id: str | None = None,
) -> Path:
    """Append a structured entry to a thread.

    .. deprecated::
        Use ``commands_graph.append_entry`` instead. This legacy version
        reads and writes .md directly without graph data.

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        agent: Agent name (will be canonicalized with user tag)
        role: Agent role (planner, critic, implementer, tester, pm, scribe)
        title: Entry title
        entry_type: Entry type (Note, Plan, Decision, PR, Closure)
        body: Entry body text
        status: Optional status update
        ball: Optional ball update (if None, auto-flips to counterpart)
        templates_dir: Optional templates directory
        registry: Optional agent registry
        user_tag: Optional user tag for agent identification (GitHub username)

    Returns:
        Path to updated thread file
    """
    tp = thread_path(topic, threads_dir)
    
    # Initialize thread if it doesn't exist (before acquiring lock)
    if not tp.exists():
        init_thread(topic, threads_dir=threads_dir)
    
    lp = lock_path_for_topic(topic, threads_dir)
    with AdvisoryLock(lp, timeout=2, ttl=10, force_break=False):
        s = tp.read_text(encoding="utf-8")

        # Load entry template
        try:
            template = load_template("_TEMPLATE_entry_block.md", templates_dir)
        except FileNotFoundError:
            # Fallback to simple format
            now = utcnow_iso()
            canonical = _canonical_agent(agent, registry, user_tag=user_tag)
            who = f" by {canonical}"
            entry = f"\n\n---\n\n- Updated: {now}{who}\n\n{body}\n"
            if entry_id:
                entry = entry.rstrip() + f"\n<!-- Entry-ID: {entry_id} -->\n"
            s = _bump_header(s, status=status, ball=ball)
            write(tp, s + entry)
            return tp

        # Fill entry template
        now = utcnow_iso()
        canonical_agent = _canonical_agent(agent, registry, user_tag=user_tag)

        mapping = {
            "UTC": now,
            "AGENT": canonical_agent,
            "TYPE": entry_type,
            "ROLE": role,
            "TITLE": title,
            "BODY": body.rstrip() + "\n",
        }
        filled_entry = _fill_template(template, mapping)

        # If template doesn't have BODY placeholder, append body after template
        if ("{{BODY}}" not in template) and ("<BODY>" not in template) and body.strip():
            filled_entry = filled_entry.rstrip() + "\n\n" + body.rstrip() + "\n"

        # Auto-flip ball if not explicitly provided
        final_ball = ball if ball is not None else _counterpart_of(canonical_agent, registry)

        # Update header
        s = _bump_header(s, status=status, ball=final_ball)

        # Append entry (with optional idempotency marker)
        if entry_id:
            filled_entry = filled_entry.rstrip() + f"\n<!-- Entry-ID: {entry_id} -->\n"
        new_text = s.rstrip() + "\n\n" + filled_entry
        write(tp, new_text)
        return tp


def set_status(topic: str, *, threads_dir: Path, status: str) -> Path:
    """.. deprecated:: Use ``commands_graph.set_status`` instead."""
    tp = thread_path(topic, threads_dir)
    if not tp.exists():
        raise FileNotFoundError(f"Thread '{topic}' not found")
    lp = lock_path_for_topic(topic, threads_dir)
    with AdvisoryLock(lp, timeout=2, ttl=10, force_break=False):
        s = tp.read_text(encoding="utf-8")
        # Normalize status to uppercase for consistency
        s = _bump_header(s, status=status.upper())
        write(tp, s)
    return tp


def set_ball(topic: str, *, threads_dir: Path, ball: str) -> Path:
    """.. deprecated:: Use ``commands_graph.set_ball`` instead."""
    tp = thread_path(topic, threads_dir)

    # Initialize thread if it doesn't exist (before acquiring lock to avoid deadlock)
    if not tp.exists():
        init_thread(topic, threads_dir=threads_dir)

    lp = lock_path_for_topic(topic, threads_dir)
    with AdvisoryLock(lp, timeout=2, ttl=10, force_break=False):
        s = tp.read_text(encoding="utf-8")
        s = _bump_header(s, ball=ball)
        write(tp, s)
    return tp


def list_threads(*, threads_dir: Path, open_only: bool | None = None) -> list[tuple[str, str, str, str, Path, bool]]:
    """Return list of (title, status, ball, updated_iso, path, is_new).

    Uses graph as sole source for topic discovery.
    """
    out: list[tuple[str, str, str, str, Path, bool]] = []
    if not threads_dir.exists():
        return out

    if not is_graph_available(threads_dir):
        return out

    graph_threads = list_threads_from_graph(threads_dir, open_only)
    for gt in graph_threads:
        p = thread_path(gt.topic, threads_dir)
        who = (_last_entry_by_from_graph(threads_dir, gt.topic) or "").strip().lower()
        is_new = bool(who and who != (gt.ball or "").strip().lower()) and not is_closed(gt.status)
        out.append((gt.title, gt.status, gt.ball, gt.last_updated, p, is_new))
    return out


def say(
    topic: str,
    *,
    threads_dir: Path,
    agent: str | None = None,
    role: str | None = None,
    title: str,
    entry_type: str = "Note",
    body: str,
    status: str | None = None,
    ball: str | None = None,
    templates_dir: Path | None = None,
    registry: dict | None = None,
    user_tag: str | None = None,
    entry_id: str | None = None,
) -> Path:
    """Quick team note with auto-ball-flip.

    .. deprecated::
        Use ``commands_graph.say`` instead.

    Convenience wrapper for append_entry that defaults agent to Team
    and auto-flips ball to counterpart unless explicitly provided.

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        agent: Agent name (defaults to Team via _default_agent_and_role)
        role: Agent role (defaults to role from registry)
        title: Entry title (required)
        entry_type: Entry type (default: "Note")
        body: Entry body text
        status: Optional status update
        ball: Optional ball update (if not provided, auto-flips)
        templates_dir: Optional templates directory
        registry: Optional agent registry
        user_tag: Optional user tag for agent identification (GitHub username)
    """
    # Default agent to Team
    default_agent, default_role = _default_agent_and_role(registry)
    final_agent = agent if agent is not None else default_agent
    final_role = role if role is not None else default_role

    return append_entry(
        topic,
        threads_dir=threads_dir,
        agent=final_agent,
        role=final_role,
        title=title,
        entry_type=entry_type,
        body=body,
        status=status,
        ball=ball,  # append_entry will auto-flip if None
        templates_dir=templates_dir,
        registry=registry,
        user_tag=user_tag,
        entry_id=entry_id,
    )


def ack(
    topic: str,
    *,
    threads_dir: Path,
    agent: str | None = None,
    role: str | None = None,
    title: str | None = None,
    entry_type: str = "Note",
    body: str | None = None,
    status: str | None = None,
    ball: str | None = None,
    templates_dir: Path | None = None,
    registry: dict | None = None,
    user_tag: str | None = None,
) -> Path:
    """Acknowledge without auto-flipping ball.

    .. deprecated::
        Use ``commands_graph.ack`` instead.

    Like say() but does NOT auto-flip ball - ball only changes if explicitly provided.
    Default title is "Ack".

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        agent: Agent name (defaults to Team)
        role: Agent role (defaults to role from registry)
        title: Entry title (defaults to "Ack")
        entry_type: Entry type (default: "Note")
        body: Entry body text (defaults to "ack")
        status: Optional status update
        ball: Optional ball update (does NOT auto-flip)
        templates_dir: Optional templates directory
        registry: Optional agent registry
        user_tag: Optional user tag for agent identification (GitHub username)
    """
    # Default agent to Team
    default_agent, default_role = _default_agent_and_role(registry)
    final_agent = agent if agent is not None else default_agent
    final_role = role if role is not None else default_role
    final_title = title if title is not None else "Ack"
    final_body = body if body is not None else "ack"

    # If no ball specified, preserve current ball (don't auto-flip)
    final_ball = ball
    if final_ball is None:
        _, _, current_ball, _ = _thread_meta_from_graph(threads_dir, topic)
        if current_ball:
            final_ball = current_ball  # Preserve current ball

    return append_entry(
        topic,
        threads_dir=threads_dir,
        agent=final_agent,
        role=final_role,
        title=final_title,
        entry_type=entry_type,
        body=final_body,
        status=status,
        ball=final_ball,  # Explicit ball (no auto-flip)
        templates_dir=templates_dir,
        registry=registry,
        user_tag=user_tag,
    )


def handoff(
    topic: str,
    *,
    threads_dir: Path,
    agent: str | None = None,
    role: str = "pm",
    note: str | None = None,
    registry: dict | None = None,
    templates_dir: Path | None = None,
    user_tag: str | None = None,
) -> Path:
    """Flip the Ball to the counterpart and append a handoff entry.

    .. deprecated::
        Use ``commands_graph.handoff`` instead.

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        agent: Agent performing handoff (defaults to Team)
        role: Agent role (default: "pm" for project management)
        note: Optional custom handoff message
        registry: Optional agent registry
        templates_dir: Optional templates directory
        user_tag: Optional user tag for agent identification (GitHub username)
    """
    tp = thread_path(topic, threads_dir)
    if not tp.exists():
        tp = init_thread(
            topic,
            threads_dir=threads_dir,
            templates_dir=templates_dir,
        )

    # Determine current counterpart based on registry and existing ball
    title, status, ball, updated = _thread_meta_from_graph(threads_dir, topic)
    target = _counterpart_of(ball, registry)

    # Default agent to Team
    default_agent, default_role = _default_agent_and_role(registry)
    final_agent = agent if agent is not None else default_agent

    # Create handoff message
    text = note or f"handoff to {target}"
    handoff_title = f"Handoff to {target}"

    # Append structured handoff entry with explicit ball
    return append_entry(
        topic,
        threads_dir=threads_dir,
        agent=final_agent,
        role=role,
        title=handoff_title,
        entry_type="Note",
        body=text,
        ball=target,  # Explicitly set target (no auto-flip needed)
        templates_dir=templates_dir,
        registry=registry,
        user_tag=user_tag,
    )


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
    """
    from .baseline_graph import storage

    q = query.lower()
    hits: list[tuple[Path, int, str]] = []

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
        warnings = []
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
            result_msg = f"✅ Merged '{branch}' into 'main' in threads repo."
            if warnings:
                result_msg += "\n" + "\n".join(warnings)
            return result_msg
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
