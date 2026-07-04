"""Phase C — write-behind acceptance + tiered backpressure.

Two surfaces:

1. ``_emit_queue_advisory`` — yellow-tier cooperative load-shed advisory (pure).
2. The ``confirm`` branch in ``run_with_sync`` — ``accepted`` returns once the
   entry is durable + the commit is queued (no receipt wait, the write-behind
   win); ``committed``/``pushed`` block on the committer receipt (the Phase B
   contract, preserved for Decisions). Driven against a real temp repo with a
   patched commit queue + a stub committer.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.middleware import _emit_queue_advisory, run_with_sync
from watercooler_mcp.memory_queue.queue import MemoryTaskQueue


# --------------------------------------------------------------------------- #
# 1. _emit_queue_advisory — tiering
# --------------------------------------------------------------------------- #

def _cfg(max_depth=2000, batch_window=5.0, max_batch_size=50):
    return SimpleNamespace(
        commit_queue_max_depth=max_depth,
        batch_window=batch_window,
        max_batch_size=max_batch_size,
    )


def test_advisory_below_threshold_is_silent():
    q = MagicMock()
    q.depth.return_value = 100  # 5% of 2000
    ss: dict = {}
    _emit_queue_advisory(q, _cfg(), ss)
    assert ss == {}  # below 60% — no advisory


def test_advisory_yellow_tier_annotates_lag():
    q = MagicMock()
    q.depth.return_value = 1400  # 70% of 2000 -> yellow
    ss: dict = {}
    _emit_queue_advisory(q, _cfg(), ss)
    assert ss["backpressure"] is True
    assert ss["backpressure_tier"] == "yellow"
    assert ss["queue_depth"] == 1400
    # throughput = 50 / 5s = 10/s -> 1400 / 10 = 140s
    assert ss["est_lag_s"] == 140.0


def test_advisory_red_tier_when_full():
    q = MagicMock()
    q.depth.return_value = 2000  # 100% -> red
    ss: dict = {}
    _emit_queue_advisory(q, _cfg(), ss)
    assert ss["backpressure_tier"] == "red"


def test_advisory_never_raises_on_bad_queue():
    q = MagicMock()
    q.depth.side_effect = RuntimeError("boom")
    ss: dict = {}
    _emit_queue_advisory(q, _cfg(), ss)  # swallowed — advisory must never break a write
    assert "backpressure" not in ss


# --------------------------------------------------------------------------- #
# 2. config defaults
# --------------------------------------------------------------------------- #

def test_sync_config_phase_c_defaults():
    from watercooler.config_schema import SyncConfig

    c = SyncConfig()
    assert c.commit_queue_max_depth == 2000
    assert c.default_confirm == "accepted"


# --------------------------------------------------------------------------- #
# 3. confirm branch in run_with_sync (integration, real repo + stub committer)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Ctx:
    code_root: Optional[Path]
    threads_dir: Path
    code_repo: Optional[str] = None
    code_branch: Optional[str] = None
    code_commit: Optional[str] = None
    code_remote: Optional[str] = None
    explicit_dir: bool = False


def _init_repo(threads_dir: Path):
    from git import Repo

    threads_dir.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(threads_dir)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    (threads_dir / "README.md").write_text("seed\n", encoding="utf-8")
    repo.git.add("-A")
    repo.index.commit("seed")
    return repo


def _config(default_confirm="accepted"):
    sync = SimpleNamespace(
        async_sync=True,
        default_confirm=default_confirm,
        commit_queue_max_depth=2000,
        batch_window=5.0,
        max_batch_size=50,
        max_delay=30.0,
    )
    graph = MagicMock()
    graph.generate_summaries = False
    graph.generate_embeddings = False
    graph.async_enrichment = False  # force inline (mocked) enrichment
    wc = MagicMock()
    wc.mcp.sync = sync
    wc.mcp.graph = graph
    return wc


def _drive(threads_dir, queue, *, confirm, default_confirm="accepted"):
    """Run run_with_sync through the committer flip with everything stubbed but
    the queue + the real repo. Returns (result, sync_status)."""
    committer = MagicMock()
    mgr = MagicMock()
    mgr.get_daemon.side_effect = lambda n: committer if n == "committer" else None

    ctx = _Ctx(code_root=threads_dir.parent / "code", threads_dir=threads_dir)
    ss: dict = {}
    patches = {
        "watercooler_mcp.middleware.get_watercooler_config":
            MagicMock(return_value=_config(default_confirm)),
        "watercooler_mcp.middleware._check_enrichment_services_available":
            MagicMock(return_value=(False, False)),
        "watercooler_mcp.middleware.acquire_topic_lock":
            MagicMock(return_value=MagicMock()),
        "watercooler_mcp.middleware._build_commit_footers":
            MagicMock(return_value=[]),
        "watercooler.baseline_graph.sync.sync_to_memory_backend": MagicMock(),
        "watercooler.baseline_graph.writer.get_entry_node_from_graph":
            MagicMock(return_value={"body": "b", "title": "T", "summary": "",
                                    "timestamp": "2025-01-01T00:00:00Z",
                                    "agent": "claude", "role": "implementer",
                                    "entry_type": "Note"}),
        "watercooler_mcp.daemons.committer.get_commit_queue":
            MagicMock(return_value=queue),
        "watercooler_mcp.daemons.get_daemon_manager":
            MagicMock(return_value=mgr),
    }
    import contextlib

    stack = contextlib.ExitStack()
    for target, m in patches.items():
        stack.enter_context(patch(target, m))
    with stack:
        result = run_with_sync(
            ctx, commit_title="agent: t (topic)", operation=lambda: "ok",
            topic="topic", entry_id="E1", sync_status=ss, confirm=confirm,
        )
    return result, ss


def test_accepted_returns_without_waiting_for_receipt(tmp_path):
    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    queue = MemoryTaskQueue(queue_dir=tmp_path / "cq")

    t0 = time.monotonic()
    result, ss = _drive(threads_dir, queue, confirm="accepted")
    elapsed = time.monotonic() - t0

    assert result == "ok"
    assert ss.get("accepted") is True
    assert ss.get("queued") is True
    assert ss.get("commit_task_id")
    # Write-behind: returned WITHOUT a confirmed receipt (the committer never ran);
    # committed/pushed stay at their un-confirmed default rather than flipping True.
    assert ss.get("committed") is not True
    assert ss.get("pushed") is not True
    # The commit is durably queued for the daemon.
    assert queue.pending_count() == 1
    # Did not block on a receipt timeout.
    assert elapsed < 5.0


def test_pushed_blocks_until_receipt(tmp_path):
    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    queue = MemoryTaskQueue(queue_dir=tmp_path / "cq")

    # Stub committer: drain + complete in the background so the receipt appears.
    stop = threading.Event()

    def drainer():
        while not stop.is_set():
            task = queue.dequeue()
            if task is not None:
                queue.complete(task.task_id)
            else:
                time.sleep(0.02)

    worker = threading.Thread(target=drainer, daemon=True)
    worker.start()
    try:
        result, ss = _drive(threads_dir, queue, confirm="pushed")
    finally:
        stop.set()
        worker.join(timeout=2)

    assert result == "ok"
    # Confirmed-write contract preserved: blocked until the receipt confirmed push.
    assert ss.get("committed") is True
    assert ss.get("pushed") is True
    assert ss.get("accepted") is not True
