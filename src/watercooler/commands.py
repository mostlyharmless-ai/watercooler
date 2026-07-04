# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from .fs import write, thread_path, lock_path_for_topic, is_closed
from .baseline_graph.writer import get_entries_for_thread
from .baseline_graph.reader import list_threads_from_graph, is_graph_available


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

    .. warning:: BREAKING CHANGE

        Line numbers are now relative to each entry's body (not file-global).
        The legacy .md grep returned file-level line numbers. Callers using
        ``line_no`` for file navigation will get entry-local positions.
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


def roles_init(*, project_path: Path, force: bool = False) -> int:
    """Scaffold ``.watercooler/roles.toml`` from the bundled commented stub.

    Thin CLI wrapper over :func:`watercooler.roles_scaffold.scaffold_roles_file`
    — the one scaffolder the MCP ``watercooler_init`` tool also uses, so a human
    and an agent re-initialize to identical bytes.

    Args:
        project_path: Project directory (where ``.watercooler/`` will be created).
        force: When True, re-scaffold even if a file exists (backing it up first).

    Returns:
        0 on success or "already initialized" (idempotent), 1 on error.
    """
    import sys

    from watercooler.roles_scaffold import (
        STATUS_CREATED,
        STATUS_EXISTS,
        scaffold_roles_file,
    )

    result = scaffold_roles_file(project_path, force=force)

    if result.status == STATUS_EXISTS:
        print(f"✅ Roles already initialized: {result.target_path}")
        print("   Use --force to re-scaffold (the current file is backed up first).")
        return 0

    if result.status != STATUS_CREATED:
        print(
            f"❌ Failed to initialize roles at {result.target_path}: {result.error}",
            file=sys.stderr,
        )
        return 1

    if result.backup_path is not None:
        print(f"   Backed up previous roles to: {result.backup_path}")
    print(f"✅ Created project roles: {result.target_path}")
    print("   Uncomment a [roles.<name>] block to customize roles for your project.")
    print("   See docs/ROLES_CREATION.md for the full guide.")
    return 0


def _stop_list_has_hook(entries: list) -> bool:
    """Return True if a watercooler-stop-hook entry exists in a Stop hook list."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cmd = entry.get("command", "")
        if "stop_hook" in cmd or "watercooler-stop-hook" in cmd:
            return True
        for sub in entry.get("hooks", []):
            if isinstance(sub, dict):
                sub_cmd = sub.get("command", "")
                if "stop_hook" in sub_cmd or "watercooler-stop-hook" in sub_cmd:
                    return True
    return False


def setup_stop_hook(
    settings_path: Path | None = None,
    local_settings_path: Path | None = None,
) -> int:
    """Wire watercooler-stop-hook as a Stop hook.

    Checks settings.local.json first for an existing hook, then settings.json.
    Writes to settings.json by default. Returns 0 on success, 1 on error.
    """
    import json
    import os
    import shutil
    import sys
    import tempfile

    # Resolve watercooler-stop-hook. The stop hook is installed alongside the
    # watercooler CLI, so check the sibling bin directory of the running
    # executable first — this covers invocation when the venv is not activated
    # and console scripts are not on PATH.
    _stop_name = "watercooler-stop-hook"
    _self_dir = Path(sys.argv[0]).resolve().parent
    _sibling = _self_dir / _stop_name
    hook_bin: str | None
    if _sibling.is_file() and os.access(_sibling, os.X_OK):
        hook_bin = str(_sibling)
    else:
        hook_bin = shutil.which(_stop_name)
    if not hook_bin:
        print(
            f"❌ {_stop_name} not found (checked {_self_dir} and PATH).\n"
            "   Run 'pip install -e .' or 'uv sync' to install the package first.",
            file=sys.stderr,
        )
        return 1
    hook_bin = str(Path(hook_bin).resolve())

    claude_dir = Path.home() / ".claude"
    _settings_path = settings_path or (claude_dir / "settings.json")
    _local_settings_path = local_settings_path or (claude_dir / "settings.local.json")

    def _has_stop_hook(path: Path) -> bool:
        """Return True if a stop hook already exists in path.

        Raises json.JSONDecodeError if the file exists but contains invalid
        JSON so the caller can surface a clear error rather than silently
        ignoring a malformed higher-priority settings file.
        """
        if not path.exists():
            return False
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            raise  # malformed settings — caller must handle, not silently skip
        except OSError:
            return False  # permission error / race — treat as absent
        if not isinstance(data, dict):
            return False
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            return False
        stop = hooks.get("Stop", [])
        if not isinstance(stop, list):
            return False
        return _stop_list_has_hook(stop)

    # Check settings.local.json first — if hook is there, nothing to do.
    # Fail loudly if the file exists but is malformed: silently skipping it
    # would write settings.json while the broken local file still takes
    # priority, leaving the hook effectively unconfigured.
    try:
        if _has_stop_hook(_local_settings_path):
            print(f"✅ Stop hook already configured in {_local_settings_path}")
            return 0
    except json.JSONDecodeError as e:
        print(
            f"❌ {_local_settings_path} contains invalid JSON: {e}\n"
            f"   Fix or remove it before running setup-stop-hook.",
            file=sys.stderr,
        )
        return 1

    # Load settings.json (create skeleton if missing)
    settings: dict = {}
    if _settings_path.exists():
        try:
            with open(_settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"❌ Failed to read {_settings_path}: {e}", file=sys.stderr)
            return 1

    if not isinstance(settings, dict):
        print(f"❌ {_settings_path} is not a JSON object", file=sys.stderr)
        return 1

    hooks_val = settings.get("hooks")
    if hooks_val is None:
        settings["hooks"] = {}
    elif not isinstance(hooks_val, dict):
        print(
            f"❌ {_settings_path}: 'hooks' key is not an object "
            f"(got {type(hooks_val).__name__})",
            file=sys.stderr,
        )
        return 1

    hooks = settings["hooks"]
    stop_val = hooks.get("Stop", [])
    if not isinstance(stop_val, list):
        print(
            f"❌ {_settings_path}: 'Stop' key is not an array "
            f"(got {type(stop_val).__name__})",
            file=sys.stderr,
        )
        return 1
    stop_hooks: list = stop_val

    # Check if hook already exists in settings.json
    if _stop_list_has_hook(stop_hooks):
        print(f"✅ Stop hook already configured in {_settings_path}")
        return 0

    # Append new hook entry. Claude Code Stop hooks use the nested
    # `{"hooks": [{...}]}` envelope so the stdin payload is piped to the
    # command — the same envelope shape PostCompact requires.
    stop_hooks.append({"hooks": [{"type": "command", "command": hook_bin}]})
    hooks["Stop"] = stop_hooks

    # Write atomically
    _settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path_str = None
    try:
        fd, tmp_path_str = tempfile.mkstemp(
            dir=_settings_path.parent, suffix=".tmp", prefix="settings_"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp_path_str, _settings_path)
    except (OSError, TypeError, ValueError) as e:
        if tmp_path_str:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
        print(f"❌ Failed to write {_settings_path}: {e}", file=sys.stderr)
        return 1

    print(f"✅ Stop hook configured: {hook_bin}")
    print(f"   Written to: {_settings_path}")
    return 0
