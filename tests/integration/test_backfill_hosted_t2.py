"""Integration tests for scripts/backfill_hosted_t2.py.

Validates:
  - Window filtering (entries outside [since, until] are excluded).
  - Skip-when-already-indexed (entry_episode_index acts as the dedup gate).
  - Dry-run mode reports candidates but does not enqueue.
  - Idempotency — a second run after the first does not re-enqueue.

Tests fabricate a minimal orphan-branch worktree (graph/baseline/threads/
<topic>/entries.jsonl) and a fake entry_episode_index. The
``enqueue_memory_task`` helper is monkey-patched so we don't need a live
queue worker — we only assert on the calls.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Module loader (script is a CLI file, not a package)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backfill_module():
    """Load scripts/backfill_hosted_t2.py as an importable module."""
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "backfill_hosted_t2.py"
    spec = importlib.util.spec_from_file_location(
        "backfill_hosted_t2", script_path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_hosted_t2"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Threads-dir fixture
# ---------------------------------------------------------------------------


def _write_entry(
    threads_dir: Path,
    topic: str,
    entry_id: str,
    timestamp: str,
    *,
    title: str = "Test entry",
    body: str = "Some body content",
    role: str = "implementer",
    entry_type: str = "Note",
    agent: str = "Claude (test)",
    index: int = 0,
) -> None:
    """Append a synthetic entry node to graph/baseline/threads/<topic>/entries.jsonl.

    Also ensures meta.json exists — ``list_thread_topics`` uses ``meta.json``
    presence as the directory-is-a-thread predicate (storage.py L194).
    """
    thread_dir = threads_dir / "graph" / "baseline" / "threads" / topic
    thread_dir.mkdir(parents=True, exist_ok=True)
    meta_path = thread_dir / "meta.json"
    if not meta_path.exists():
        meta_path.write_text(
            json.dumps({"id": f"thread:{topic}", "type": "thread", "topic": topic}),
            encoding="utf-8",
        )
    node = {
        "id": f"entry:{entry_id}",
        "type": "entry",
        "entry_id": entry_id,
        "thread_topic": topic,
        "index": index,
        "agent": agent,
        "role": role,
        "entry_type": entry_type,
        "title": title,
        "timestamp": timestamp,
        "body": body,
    }
    with open(thread_dir / "entries.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(node) + "\n")


@pytest.fixture
def threads_dir(tmp_path: Path) -> Path:
    """Build a synthetic orphan-branch worktree with mixed-window entries.

    Layout:
      tmp/graph/baseline/threads/
        topic-a/entries.jsonl   3 entries: pre-window, in-window, in-window
        topic-b/entries.jsonl   2 entries: in-window, post-window
    """
    # topic-a: 1 before window + 2 in-window
    _write_entry(
        tmp_path,
        "topic-a",
        "01KAAAAAAAAAAAAAAAAAAAAAAA",
        "2026-04-20T10:00:00+00:00",  # before window
        index=0,
        title="pre-window-entry",
    )
    _write_entry(
        tmp_path,
        "topic-a",
        "01KBBBBBBBBBBBBBBBBBBBBBBB",
        "2026-04-26T10:00:00+00:00",  # in window
        index=1,
        title="in-window-a1",
    )
    _write_entry(
        tmp_path,
        "topic-a",
        "01KCCCCCCCCCCCCCCCCCCCCCCC",
        "2026-04-29T10:00:00+00:00",  # in window
        index=2,
        title="in-window-a2",
    )
    # topic-b: 1 in-window + 1 after window
    _write_entry(
        tmp_path,
        "topic-b",
        "01KDDDDDDDDDDDDDDDDDDDDDDD",
        "2026-04-30T10:00:00+00:00",  # in window
        index=0,
        title="in-window-b1",
    )
    _write_entry(
        tmp_path,
        "topic-b",
        "01KEEEEEEEEEEEEEEEEEEEEEEE",
        "2026-05-02T10:00:00+00:00",  # after window
        index=1,
        title="post-window-entry",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers — build args namespace and capture enqueues
# ---------------------------------------------------------------------------


def _make_args(
    backfill_module,
    *,
    threads_dir: Path,
    code_path: str = ".",
    dry_run: bool = False,
    since: str = "2026-04-25",
    until: str = "2026-05-01T12:50:00+00:00",
    limit: int = 0,
    target_group_id: str = "",
    verbose: bool = False,
):
    """Build the argparse.Namespace the script expects."""
    parser = backfill_module._build_parser()
    argv = [
        "--code-path", code_path,
        "--threads-dir", str(threads_dir),
        "--since", since,
        "--until", until,
    ]
    if dry_run:
        argv.append("--dry-run")
    if verbose:
        argv.append("--verbose")
    if target_group_id:
        argv.extend(["--target-group-id", target_group_id])
    if limit:
        argv.extend(["--limit", str(limit)])
    return parser.parse_args(argv)


class _StubIndex:
    """Drop-in replacement for EntryEpisodeIndex with deterministic behaviour."""

    def __init__(self, indexed_entry_ids: List[str]):
        self._indexed = set(indexed_entry_ids)

    def has_any_mapping(self, entry_id: str) -> bool:
        return entry_id in self._indexed


@pytest.fixture
def stub_memory_queue():
    """Install a stub ``watercooler_mcp.memory_queue`` (+ .errors submodule).

    The real ``watercooler_mcp`` package pulls ``fastmcp`` via its
    ``__init__``, which isn't installed in the bare test runner. The stub
    exposes the exact symbols the backfill script imports:

      from watercooler_mcp.memory_queue import get_queue, MemoryTask, VALID_BACKENDS
      from watercooler_mcp.memory_queue.errors import DuplicateTaskError, QueueFullError

    Tests configure ``stub.get_queue_return`` (queue object or None) and
    optionally ``stub.queue_enqueue_side_effect`` (raise / return) to drive
    the script's behaviour.
    """
    stub_pkg = type(sys)("watercooler_mcp")
    stub_mq = type(sys)("watercooler_mcp.memory_queue")
    stub_errors = type(sys)("watercooler_mcp.memory_queue.errors")

    class DuplicateTaskError(Exception):
        pass

    class QueueFullError(Exception):
        pass

    stub_errors.DuplicateTaskError = DuplicateTaskError
    stub_errors.QueueFullError = QueueFullError

    from dataclasses import dataclass, field

    @dataclass
    class MemoryTask:
        backend: str = "graphiti"
        entry_id: str = ""
        topic: str = ""
        group_id: str = ""
        content: str = ""
        title: str = ""
        timestamp: str = ""
        source_description: str = ""
        code_path: str = ""
        max_attempts: int = 3
        xrefs: list = field(default_factory=list)
        tags: list = field(default_factory=list)
        vote_score: int = 0
        pinned: bool = False

    stub_mq.MemoryTask = MemoryTask
    stub_mq.VALID_BACKENDS = {"graphiti", "leanrag"}

    # PR #745 round 3: the script's _truncate_for_queue shim imports
    # truncate_utf8_to_bytes from this module — provide a real impl
    # in the stub so multi-byte tests exercise the same logic the
    # canonical helper would.
    #
    # PR #745 round 5 review (MED): the stub MUST honour the
    # strict-cap guard added to the canonical (round 4) — otherwise
    # stub-based tests can silently pass while production drops the
    # entry via the ``empty_body`` skip path. Mirror the canonical
    # exactly. If the canonical changes, this stub MUST change too.
    def _truncate_utf8(s: str, *, max_bytes: int) -> str:
        if not s:
            return ""
        encoded = s.encode("utf-8")
        if len(encoded) <= max_bytes:
            return s
        truncated = s
        while len(truncated.encode("utf-8")) > max_bytes and len(truncated) > 1:
            over = len(truncated.encode("utf-8")) - max_bytes
            drop = max(1, over // 4)
            truncated = (
                truncated[:-drop] if drop < len(truncated) else truncated[:1]
            )
        while len(truncated.encode("utf-8")) > max_bytes and len(truncated) > 1:
            truncated = truncated[:-1]
        # Strict cap (matches canonical): empty when single remaining
        # codepoint is still over budget.
        if len(truncated.encode("utf-8")) > max_bytes:
            return ""
        return truncated

    stub_mq.truncate_utf8_to_bytes = _truncate_utf8

    state = {"queue": None, "worker": None}

    def get_queue():
        return state["queue"]

    def get_worker():
        return state["worker"]

    stub_mq.get_queue = get_queue
    stub_mq.get_worker = get_worker
    stub_mq.errors = stub_errors

    holder = type("Holder", (), {})()
    holder.module = stub_mq
    holder.errors = stub_errors
    holder.MemoryTask = MemoryTask
    holder.DuplicateTaskError = DuplicateTaskError
    holder.QueueFullError = QueueFullError
    holder.set_queue = lambda q: state.update(queue=q)
    holder.set_worker = lambda w: state.update(worker=w)

    with patch.dict(
        sys.modules,
        {
            "watercooler_mcp": stub_pkg,
            "watercooler_mcp.memory_queue": stub_mq,
            "watercooler_mcp.memory_queue.errors": stub_errors,
        },
    ):
        yield holder


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_window_filter_excludes_out_of_window_entries(
    backfill_module, threads_dir, capsys
):
    """Pre-window and post-window entries must be excluded from candidate set."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=True,
        target_group_id="test_group_t2",
    )
    enqueued: List[Dict[str, Any]] = []

    def _fake_enqueue(**kwargs):
        enqueued.append(kwargs)
        return "task-stub"

    with patch.object(backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])):
        with patch.dict(sys.modules):
            # Force the script's `from watercooler_mcp.memory_queue import enqueue_memory_task`
            # path to a stub module; not strictly needed since dry-run skips
            # enqueue, but keeps the test hermetic.
            rc = backfill_module.run_backfill(args)

    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)["summary"]
    # 3 in-window entries: 01KBBB, 01KCCC, 01KDDD. Pre/post excluded.
    assert payload["candidates_in_window"] == 3
    assert payload["dry_run"] is True
    # Dry-run reports them all as "would-enqueue" since the stub index has no
    # already-indexed entries.
    assert payload["enqueued"] == 3
    assert payload["skipped_already_indexed"] == 0


def test_skip_when_already_indexed(backfill_module, threads_dir, capsys):
    """Entries already in entry_episode_index must not be enqueued."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=True,
        target_group_id="test_group_t2",
    )

    # Mark one of the 3 in-window entries as already indexed.
    already_indexed = ["01KBBBBBBBBBBBBBBBBBBBBBBB"]
    with patch.object(
        backfill_module,
        "_load_entry_episode_index",
        return_value=_StubIndex(already_indexed),
    ):
        rc = backfill_module.run_backfill(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)["summary"]
    assert payload["candidates_in_window"] == 3
    assert payload["skipped_already_indexed"] == 1
    assert payload["enqueued"] == 2  # the remaining two


def test_dry_run_does_not_call_enqueue(backfill_module, threads_dir, capsys):
    """Dry-run mode reports counts but never invokes enqueue_memory_task."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=True,
        target_group_id="test_group_t2",
    )

    # PR #745 review (LOW L2): the prior version of this test patched
    # ``enqueue_memory_task``, which the script no longer calls (it
    # uses ``queue.enqueue()`` directly). The assertion passed
    # vacuously. Now we install a queue stub that DOES record calls
    # and assert it remained empty under dry-run.
    enqueued_tasks: List[Any] = []

    class _SpyQueue:
        def enqueue(self, task):
            enqueued_tasks.append(task)
            return "task-id-" + task.entry_id

    spy = _SpyQueue()

    # Stub package so the dry-run path can import without triggering the
    # real watercooler_mcp/__init__.py (which needs fastmcp).
    stub_pkg = type(sys)("watercooler_mcp")
    stub_mq = type(sys)("watercooler_mcp.memory_queue")
    stub_errors = type(sys)("watercooler_mcp.memory_queue.errors")

    class _Dup(Exception):
        pass

    class _Full(Exception):
        pass

    stub_errors.DuplicateTaskError = _Dup
    stub_errors.QueueFullError = _Full
    stub_mq.errors = stub_errors
    stub_mq.MemoryTask = type("MemoryTask", (), {})
    stub_mq.VALID_BACKENDS = {"graphiti"}
    stub_mq.get_queue = lambda: spy
    stub_mq.get_worker = lambda: None
    # Pass-through truncation: dry-run path doesn't exercise it but the
    # script's _truncate_for_queue shim still imports it.
    stub_mq.truncate_utf8_to_bytes = lambda s, *, max_bytes: s

    with patch.object(
        backfill_module,
        "_load_entry_episode_index",
        return_value=_StubIndex([]),
    ):
        with patch.dict(sys.modules, {
            "watercooler_mcp": stub_pkg,
            "watercooler_mcp.memory_queue": stub_mq,
            "watercooler_mcp.memory_queue.errors": stub_errors,
        }):
            rc = backfill_module.run_backfill(args)

    assert rc == 0
    assert enqueued_tasks == [], (
        "Dry-run must not call queue.enqueue(). The previous test patched "
        "the no-longer-called convenience helper and passed vacuously."
    )

    payload = json.loads(capsys.readouterr().out)["summary"]
    assert payload["dry_run"] is True
    assert payload["enqueued"] == 3


def test_idempotency_second_run_skips_when_index_grows(
    backfill_module, threads_dir, capsys
):
    """A second run after entries land in the index re-skips them.

    Models the post-backfill state: after the first pass enqueues entries
    and the worker processes them, entry_episode_index gains those mappings.
    A re-run must skip them rather than double-enqueue.
    """
    args1 = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=True,
        target_group_id="test_group_t2",
    )
    # First run: nothing indexed yet — all 3 in-window candidates queued.
    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        rc1 = backfill_module.run_backfill(args1)
    payload1 = json.loads(capsys.readouterr().out)["summary"]
    assert rc1 == 0
    assert payload1["enqueued"] == 3
    assert payload1["skipped_already_indexed"] == 0

    # Second run: index now contains the 3 in-window entry_ids.
    args2 = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=True,
        target_group_id="test_group_t2",
    )
    indexed_after_run1 = [
        "01KBBBBBBBBBBBBBBBBBBBBBBB",
        "01KCCCCCCCCCCCCCCCCCCCCCCC",
        "01KDDDDDDDDDDDDDDDDDDDDDDD",
    ]
    with patch.object(
        backfill_module,
        "_load_entry_episode_index",
        return_value=_StubIndex(indexed_after_run1),
    ):
        rc2 = backfill_module.run_backfill(args2)
    payload2 = json.loads(capsys.readouterr().out)["summary"]
    assert rc2 == 0
    assert payload2["candidates_in_window"] == 3
    assert payload2["enqueued"] == 0
    assert payload2["skipped_already_indexed"] == 3


def test_real_run_calls_queue_enqueue_with_graphiti_task(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """Non-dry run must call queue.enqueue() with a graphiti MemoryTask.

    PR #745 review (MED): the script no longer goes through the
    convenience helper ``enqueue_memory_task`` (which collapses
    DuplicateTaskError and QueueFullError into ``None``); it talks to the
    raw queue so the two cases stay distinguishable.
    """
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=False,
        target_group_id="test_group_t2",
    )

    enqueued_tasks: List[Any] = []

    class _StubQueue:
        def enqueue(self, task):  # noqa: D401 — stub
            enqueued_tasks.append(task)
            return "task-id-" + task.entry_id

    stub_memory_queue.set_queue(_StubQueue())

    with patch.object(
        backfill_module,
        "_load_entry_episode_index",
        return_value=_StubIndex([]),
    ):
        rc = backfill_module.run_backfill(args)

    assert rc == 0
    assert len(enqueued_tasks) == 3
    assert {t.backend for t in enqueued_tasks} == {"graphiti"}
    assert {t.group_id for t in enqueued_tasks} == {"test_group_t2"}
    enqueued_ids = {t.entry_id for t in enqueued_tasks}
    assert enqueued_ids == {
        "01KBBBBBBBBBBBBBBBBBBBBBBB",
        "01KCCCCCCCCCCCCCCCCCCCCCCC",
        "01KDDDDDDDDDDDDDDDDDDDDDDD",
    }
    payload = json.loads(capsys.readouterr().out)["summary"]
    assert payload["enqueued"] == 3
    assert payload["dry_run"] is False


def test_real_run_hard_fails_when_queue_unavailable(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """PR #745 review (MED): without --dry-run, a None queue must SystemExit
    rather than silently iterate every candidate as a skip and exit 0."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=False,
        target_group_id="test_group_t2",
    )
    # Stub fixture initialises queue=None; that's the failure mode under test.

    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        with pytest.raises(SystemExit, match="memory queue is not initialised"):
            backfill_module.run_backfill(args)


def test_dry_run_tolerates_uninitialised_queue(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """Dry-run must NOT hard-fail on a None queue — operators run --dry-run
    from inventory-only contexts where the worker isn't initialised."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=True,
        target_group_id="test_group_t2",
    )

    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        rc = backfill_module.run_backfill(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)["summary"]
    assert payload["enqueued"] == 3  # all 3 in-window candidates "would-enqueue"


def test_queue_full_counted_as_error_not_skip(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """PR #745 review (MED): QueueFullError must increment errored, not a
    silent skip. Distinguishes destructive drops from benign duplicates."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=False,
        target_group_id="test_group_t2",
    )
    QueueFullError = stub_memory_queue.QueueFullError

    class _FullQueue:
        def enqueue(self, task):
            raise QueueFullError("capacity exceeded")

    stub_memory_queue.set_queue(_FullQueue())

    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        rc = backfill_module.run_backfill(args)

    payload = json.loads(capsys.readouterr().out)["summary"]
    # Non-zero exit because every candidate errored.
    assert rc != 0
    assert payload["errored"] == 3
    assert payload["enqueued"] == 0
    assert payload["skipped_duplicate"] == 0


def test_real_run_wakes_worker_after_enqueue(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """PR #745 review (MED M1): after each ``queue.enqueue(task)`` the
    script must call ``get_worker().wake()`` so the worker processes
    immediately rather than waiting up to ``poll_interval``. Without
    this, sustained backfill enqueue rate can outpace the worker's
    polling cadence and grow the queue toward ``max_depth`` —
    triggering the very ``QueueFullError`` this PR added detection for.
    """
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=False,
        target_group_id="test_group_t2",
    )

    class _StubQueue:
        def enqueue(self, task):
            return "task-id-" + task.entry_id

    wake_calls: List[Any] = []

    class _StubWorker:
        def wake(self):
            wake_calls.append(True)

    stub_memory_queue.set_queue(_StubQueue())
    stub_memory_queue.set_worker(_StubWorker())

    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        rc = backfill_module.run_backfill(args)

    assert rc == 0
    # 3 in-window candidates → 3 wake() calls (one per enqueue).
    assert len(wake_calls) == 3, (
        f"Expected 3 worker.wake() calls (one per enqueue); got {len(wake_calls)}"
    )


def test_real_run_tolerates_missing_worker(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """get_worker() == None must not break the run — wake is best-effort.

    Pins the contract: even if a deployment swaps the worker out for an
    out-of-process scheduler (no in-process wake target), the backfill
    still completes successfully. The next poll tick picks up the work.
    """
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=False,
        target_group_id="test_group_t2",
    )

    class _StubQueue:
        def enqueue(self, task):
            return "task-id-" + task.entry_id

    stub_memory_queue.set_queue(_StubQueue())
    stub_memory_queue.set_worker(None)  # explicit no-worker case

    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        rc = backfill_module.run_backfill(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)["summary"]
    assert payload["enqueued"] == 3
    assert payload["errored"] == 0


def test_queue_full_reason_tag_is_stable(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """PR #745 review (LOW L3): QueueFullError reason must be a stable
    grep-friendly tag (``error:queue_full``), not the exception's str()
    embedded into the tag. The exception message is logged separately."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=False,
        target_group_id="test_group_t2",
        verbose=True,
    )
    QueueFullError = stub_memory_queue.QueueFullError

    class _FullQueue:
        def enqueue(self, task):
            raise QueueFullError("capacity 5000 exceeded by 1 task")

    stub_memory_queue.set_queue(_FullQueue())

    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ), patch.object(backfill_module.logger, "warning") as mock_warning:
        backfill_module.run_backfill(args)

    # The reason tag in the per-error log must match exactly — no
    # embedded exception text — so operators can grep / aggregate.
    error_calls = [
        c for c in mock_warning.call_args_list
        if c.args and "ERROR" in str(c.args[0])
    ]
    assert error_calls, "Expected at least one ERROR log line"
    for call in error_calls:
        # Format is: "ERROR %s/%s: %s" with reason as third arg.
        reason = call.args[3]
        assert reason == "error:queue_full", (
            f"Reason tag should be the stable string 'error:queue_full' "
            f"(no exception text); got {reason!r}"
        )


def test_duplicate_counted_as_skip_not_error(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """DuplicateTaskError is benign (already in queue/dedup-cache) and
    must increment skipped_duplicate, NOT errored."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=False,
        target_group_id="test_group_t2",
    )
    DuplicateTaskError = stub_memory_queue.DuplicateTaskError

    class _DupQueue:
        def enqueue(self, task):
            raise DuplicateTaskError("already-queued")

    stub_memory_queue.set_queue(_DupQueue())

    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        rc = backfill_module.run_backfill(args)

    payload = json.loads(capsys.readouterr().out)["summary"]
    assert rc == 0  # benign — script returns success
    assert payload["errored"] == 0
    assert payload["enqueued"] == 0
    assert payload["skipped_duplicate"] == 3


def test_limit_caps_inspected_count(backfill_module, threads_dir, capsys):
    """--limit caps how many candidates we inspect/enqueue."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=True,
        limit=1,
        target_group_id="test_group_t2",
    )
    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        rc = backfill_module.run_backfill(args)
    payload = json.loads(capsys.readouterr().out)["summary"]
    assert rc == 0
    assert payload["candidates_in_window"] == 3
    assert payload["inspected"] == 1
    assert payload["enqueued"] == 1


def test_multibyte_body_does_not_skip_as_empty_body(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """End-to-end regression: a multi-byte-only body must NOT be
    classified as ``empty_body`` after truncation.

    Pre-fix, an entry whose body was 100 emoji (~400 bytes) truncated
    to MAX_BODY_BYTES would pass through; but a pathological body
    larger than MAX_BODY_BYTES could degrade to "" when sliced. We
    pin both: write a body well over the cap and verify the entry
    still enqueues (skipped_empty_body=0).
    """
    # Append a topic-c entry with a giant multi-byte body. Use the
    # fixture-style helper to keep the rest consistent.
    _write_entry(
        threads_dir,
        "topic-c",
        "01KFFFFFFFFFFFFFFFFFFFFFFF",
        "2026-04-27T10:00:00+00:00",  # in window
        index=0,
        title="multi-byte-body",
        body="🎉" * 20000,  # ~80 KB — comfortably over MAX_BODY_BYTES (64 KB)
    )

    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=True,
        target_group_id="test_group_t2",
    )
    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        rc = backfill_module.run_backfill(args)

    payload = json.loads(capsys.readouterr().out)["summary"]
    assert rc == 0
    # 3 original in-window + 1 new = 4
    assert payload["candidates_in_window"] == 4
    assert payload["enqueued"] == 4, (
        f"Multi-byte body must not be silently classified as empty_body; "
        f"got summary {payload}"
    )
    assert payload["skipped_empty_body"] == 0


def test_error_reason_tags_are_stable(
    backfill_module, threads_dir, stub_memory_queue, capsys
):
    """PR #745 round 2 review (LOW): all error-class reason tags must
    be stable strings (no embedded exception text). Pin via a generic
    Exception that produces ``error:exception``."""
    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=False,
        target_group_id="test_group_t2",
        verbose=True,
    )

    class _BoomQueue:
        def enqueue(self, task):
            raise RuntimeError("connection 192.168.1.5:6379 reset by peer")

    stub_memory_queue.set_queue(_BoomQueue())

    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ), patch.object(backfill_module.logger, "warning") as mock_warning:
        backfill_module.run_backfill(args)

    error_calls = [
        c for c in mock_warning.call_args_list
        if c.args and "ERROR" in str(c.args[0])
    ]
    assert error_calls, "Expected ERROR log entries from generic exception"
    for call in error_calls:
        # Format: "ERROR %s/%s: %s" with the reason as 4th arg.
        reason = call.args[3]
        assert reason == "error:exception", (
            f"Reason tag must be stable 'error:exception' (no exception "
            f"text embedded); got {reason!r}"
        )


def test_stub_truncate_matches_canonical_strict_cap(stub_memory_queue):
    """PR #745 round 5 review (MED): the integration-test stub for
    ``truncate_utf8_to_bytes`` must implement the same strict-cap
    semantics as the canonical helper. Without this parity, tests
    using the stub could silently pass while production drops
    entries via the ``empty_body`` skip path.

    Pinned cases:
    - input ≤ cap → unchanged
    - 4-byte emoji × 3 with 2-byte cap → empty (single codepoint
      doesn't fit)
    - 4-byte emoji with 4-byte cap → preserved exactly
    - mixed ASCII at sub-codepoint cap → ASCII prefix preserved
    """
    f = stub_memory_queue.module.truncate_utf8_to_bytes
    # Round-trip identity for inputs already within the cap.
    assert f("hello", max_bytes=100) == "hello"
    # Strict cap: 4-byte emoji can't fit in 2 bytes → empty.
    assert f("🎉🎉🎉", max_bytes=2) == ""
    # 4-byte emoji fits exactly in 4-byte cap.
    assert f("🎉", max_bytes=4) == "🎉"
    # ASCII at sub-codepoint cap.
    assert f("abcdef", max_bytes=2) == "ab"
    # Empty input is the empty string.
    assert f("", max_bytes=10) == ""


def test_unparseable_timestamps_surface_in_summary(
    backfill_module, threads_dir, capsys
):
    """PR #745 round 4 review (MED): entries with unparseable timestamps
    must NOT be silently dropped — they should be counted in the
    summary so an operator can spot the gap between T1 row counts and
    candidate counts.
    """
    # Append two entries with unparseable timestamps to topic-c.
    _write_entry(
        threads_dir,
        "topic-c",
        "01KFFFFFFFFFFFFFFFFFFFFFFF",
        "not-a-real-timestamp",
        index=0,
        title="bad-ts-1",
    )
    _write_entry(
        threads_dir,
        "topic-c",
        "01KGGGGGGGGGGGGGGGGGGGGGGG",
        "",  # empty timestamp also fails to parse
        index=1,
        title="bad-ts-2",
    )

    args = _make_args(
        backfill_module,
        threads_dir=threads_dir,
        dry_run=True,
        target_group_id="test_group_t2",
    )
    with patch.object(
        backfill_module, "_load_entry_episode_index", return_value=_StubIndex([])
    ):
        rc = backfill_module.run_backfill(args)

    payload = json.loads(capsys.readouterr().out)["summary"]
    assert rc == 0
    # Original 3 in-window entries pass; 2 unparseable rows counted.
    assert payload["candidates_in_window"] == 3
    assert payload["scan_dropped_unparseable_timestamp"] == 2
    assert payload["scan_dropped_missing_entry_id"] == 0


def test_parse_iso_window_handles_bare_date(backfill_module):
    dt = backfill_module._parse_iso_window("2026-04-25", default="")
    assert dt.year == 2026 and dt.month == 4 and dt.day == 25
    # Naive → coerced to UTC.
    assert dt.tzinfo is not None


def test_parse_iso_window_handles_z_suffix(backfill_module):
    dt = backfill_module._parse_iso_window("2026-05-01T12:50:00Z", default="")
    assert dt.tzinfo is not None
    assert dt.hour == 12 and dt.minute == 50


def test_parse_entry_timestamp_returns_none_for_garbage(backfill_module):
    assert backfill_module._parse_entry_timestamp("") is None
    assert backfill_module._parse_entry_timestamp("not-a-timestamp") is None
