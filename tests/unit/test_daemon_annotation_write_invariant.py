"""Worktree-write invariant: daemons commit annotations, never poison the tree.

Enforces ``bug-sync-worktree-poisoning``. A daemon that writes annotations with
a bare ``append_annotation`` (outside ``run_with_sync``) leaves uncommitted
projection files in the served worktree, which blocks the read-path
fast-forward heal (``dirty_mixed``) and silently drifts the worktree behind the
remote. Annotations must instead go through ``daemon_annotate()`` /
``daemon_write_entry(annotation_events=...)`` so they commit + push in the same
transaction.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import watercooler_mcp.daemons as daemons_pkg
from watercooler_mcp.daemons.daemon_write import daemon_annotate

_PATCH_RESOLVE = "watercooler_mcp.config.resolve_thread_context"
_PATCH_RUN_SYNC = "watercooler_mcp.middleware.run_with_sync"
_PATCH_APPLY = "watercooler_mcp.daemons.daemon_write._apply_annotation_events"

# daemon_write.py is the SOLE daemon module sanctioned to touch
# ``append_annotation`` — it applies events inside ``run_with_sync``. Every
# other daemon routes through it.
_ALLOWED_APPEND_ANNOTATION_FILES = {"daemon_write.py"}


# ---------------------------------------------------------------------------
# The keystone guard — a new daemon cannot reintroduce the poison
# ---------------------------------------------------------------------------

def _daemon_module_paths() -> list[Path]:
    # rglob (not glob) so a daemon added under a future subpackage can't
    # silently escape the guard. daemon_write.py is excluded by basename.
    pkg_dir = Path(daemons_pkg.__file__).parent
    return [
        p
        for p in sorted(pkg_dir.rglob("*.py"))
        if p.name not in _ALLOWED_APPEND_ANNOTATION_FILES
    ]


def test_no_daemon_module_references_append_annotation_directly():
    """Any daemon importing/calling ``append_annotation`` fails CI.

    This is the structural backstop: the next daemon that tries to write an
    annotation the old (poisoning) way won't pass the build — it is forced
    through ``daemon_annotate`` / ``daemon_write_entry(annotation_events=...)``.
    (Docstring mentions are string literals, not AST name/attribute nodes, so
    they are not flagged.)
    """
    violations: list[str] = []
    for path in _daemon_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if any(a.name == "append_annotation" for a in node.names):
                    violations.append(f"{path.name}:{node.lineno}: imports append_annotation")
            elif isinstance(node, ast.Name) and node.id == "append_annotation":
                violations.append(f"{path.name}:{node.lineno}: references append_annotation")
            elif isinstance(node, ast.Attribute) and node.attr == "append_annotation":
                violations.append(f"{path.name}:{node.lineno}: references .append_annotation")

    assert not violations, (
        "Daemons must not call append_annotation directly — route annotations "
        "through daemon_write.daemon_annotate() or "
        "daemon_write_entry(annotation_events=...) so they commit inside "
        "run_with_sync (bug-sync-worktree-poisoning). Offenders:\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# daemon_annotate() — the committing annotation-only primitive
# ---------------------------------------------------------------------------

def _mock_thread_context(threads_dir: Path, code_root: Path | None = None):
    from watercooler_mcp.config import ThreadContext

    return ThreadContext(
        code_root=code_root or threads_dir.parent,
        threads_dir=threads_dir,
        code_repo="test/repo",
        code_branch="main",
        code_commit="abc1234",
        code_remote="origin",
        explicit_dir=False,
    )


def _event():
    from watercooler.baseline_graph.annotations import AnnotationEvent

    return AnnotationEvent(
        id="01TESTEVENT0000000000000AA",
        target_id="some-topic",
        target_type="thread",
        kind="tag",
        value="has_learning",
        actor="TestDaemon",
        timestamp="2026-06-19T00:00:00+00:00",
    )


def test_daemon_annotate_rejects_empty_topic():
    result = daemon_annotate("", code_root=Path("/x"), events=[_event()])
    assert result.written is False
    assert "topic" in (result.error or "")


def test_daemon_annotate_rejects_empty_events():
    result = daemon_annotate("topic", code_root=Path("/x"), events=[])
    assert result.written is False
    assert "events" in (result.error or "")


@patch(_PATCH_RESOLVE)
def test_daemon_annotate_applies_events_inside_sync(mock_resolve, tmp_path):
    """Events are applied by the operation run_with_sync wraps — so they land
    in the same commit/push, never as an uncommitted worktree write."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    mock_resolve.return_value = _mock_thread_context(threads_dir, tmp_path)

    applied: list = []

    def _run_sync(ctx, title, operation, **kwargs):
        operation()  # the real run_with_sync invokes the operation, then commits
        kwargs["sync_status"].update(
            {"operation_completed": True, "committed": True, "pushed": True, "error": None}
        )
        return None

    with patch(_PATCH_RUN_SYNC, side_effect=_run_sync), patch(
        _PATCH_APPLY, side_effect=lambda td, topic, evs: applied.extend(evs)
    ):
        result = daemon_annotate(
            "some-topic",
            code_root=tmp_path,
            events=[_event(), _event()],
            agent="TestDaemon",
            agent_spec="learnings",
        )

    assert result.written is True
    assert result.pushed is True
    assert len(applied) == 2


@patch(_PATCH_RESOLVE)
def test_daemon_annotate_reports_unpushed(mock_resolve, tmp_path):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    mock_resolve.return_value = _mock_thread_context(threads_dir, tmp_path)

    def _run_sync(ctx, title, operation, **kwargs):
        operation()
        kwargs["sync_status"].update(
            {"operation_completed": True, "committed": True, "pushed": False, "error": "push failed"}
        )
        return None

    with patch(_PATCH_RUN_SYNC, side_effect=_run_sync), patch(_PATCH_APPLY):
        result = daemon_annotate("some-topic", code_root=tmp_path, events=[_event()])

    assert result.pushed is False
    assert result.written is True
    assert "push failed" in (result.error or "")


@patch(_PATCH_RESOLVE)
def test_daemon_annotate_never_raises_on_resolution_failure(mock_resolve):
    mock_resolve.side_effect = RuntimeError("git discovery failed")
    result = daemon_annotate("topic", code_root=Path("/x"), events=[_event()])
    assert result.written is False
    assert "ThreadContext resolution failed" in (result.error or "")


@patch("watercooler.commands_graph.ack")
@patch(_PATCH_RESOLVE)
def test_daemon_write_entry_applies_annotation_events_in_sync(
    mock_resolve, mock_ack, tmp_path
):
    """The sibling path: daemon_write_entry(annotation_events=...) must apply the
    events inside the same synced operation as the entry — the seam that wires
    a daemon's event list to the commit."""
    from watercooler_mcp.daemons.daemon_write import daemon_write_entry

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    mock_resolve.return_value = _mock_thread_context(threads_dir, tmp_path)
    mock_ack.return_value = None

    applied: list = []

    def _run_sync(ctx, title, operation, **kwargs):
        operation()  # runs _do_write: graph_ack + annotation application
        kwargs["sync_status"].update(
            {"operation_completed": True, "committed": True, "pushed": True, "error": None}
        )
        return None

    with patch(_PATCH_RUN_SYNC, side_effect=_run_sync), patch(
        _PATCH_APPLY, side_effect=lambda td, topic, evs: applied.extend(evs)
    ):
        result = daemon_write_entry(
            "some-topic",
            code_root=tmp_path,
            title="t",
            body="b",
            entry_id="01ENTRY0000000000000000AA",
            annotation_events=[_event(), _event()],
        )

    assert result.written is True
    assert result.pushed is True
    assert len(applied) == 2  # events applied inside the entry's transaction
