"""Phase B step 2 — middleware flip: the commit-receipt wait contract.

Deterministic tests for ``_wait_for_commit_receipt`` (the confirmed-write
contract mapping): a completed receipt -> committed+pushed; a dead-letter
receipt -> PushError (committed locally, push failed); no receipt -> timeout,
marked queued, no raise (the daemon owns it — safe/eventual).
"""

from __future__ import annotations

import pytest

from watercooler_mcp.middleware import _wait_for_commit_receipt
from watercooler_mcp.sync.errors import PushError
from watercooler_mcp.memory_queue.queue import MemoryTaskQueue
from watercooler_mcp.memory_queue.task import MemoryTask


def _q(tmp_path, name):
    return MemoryTaskQueue(queue_dir=tmp_path / name)


def test_completed_receipt_marks_committed_and_pushed(tmp_path):
    q = _q(tmp_path, "q1")
    q.enqueue(MemoryTask(backend="commit", entry_id="E1", topic="t", threads_dir="/x"))
    task = q.dequeue()
    q.complete(task.task_id)  # writes a "completed" receipt

    ss: dict = {}
    assert _wait_for_commit_receipt(q, task.task_id, ss, "t", timeout=1.0) is True
    assert ss["committed"] is True
    assert ss["pushed"] is True


def test_dead_letter_receipt_raises_push_error(tmp_path):
    q = _q(tmp_path, "q2")
    q.enqueue(MemoryTask(backend="commit", entry_id="E2", topic="t", threads_dir="/x"))
    task = q.dequeue()
    q.fail(task.task_id, "push exhausted", permanent=True)  # -> dead_letter receipt

    ss: dict = {}
    with pytest.raises(PushError):
        _wait_for_commit_receipt(q, task.task_id, ss, "t", timeout=1.0)
    assert ss["committed"] is True  # entry safe locally
    assert "error" in ss


def test_no_receipt_times_out_queued_no_raise(tmp_path):
    q = _q(tmp_path, "q3")
    task_id = q.enqueue(
        MemoryTask(backend="commit", entry_id="E3", topic="t", threads_dir="/x")
    )
    q.dequeue()  # RUNNING, no terminal receipt yet

    ss: dict = {}
    # Accepted-mode default: short timeout, the committer never completes it.
    assert _wait_for_commit_receipt(q, task_id, ss, "t", timeout=0.2, poll=0.02) is False
    assert ss.get("queued") is True
    assert "pushed" not in ss  # never claims a push it didn't confirm


def test_confirmed_write_timeout_raises(tmp_path):
    """Confirmed writes (Decision/Closure forced confirm=pushed) must NOT report a
    receipt-wait timeout as a clean success — raise_on_timeout=True surfaces it."""
    q = _q(tmp_path, "q4")
    task_id = q.enqueue(
        MemoryTask(backend="commit", entry_id="E4", topic="t", threads_dir="/x")
    )
    q.dequeue()  # RUNNING, no terminal receipt yet

    ss: dict = {}
    with pytest.raises(PushError):
        _wait_for_commit_receipt(
            q, task_id, ss, "t", timeout=0.2, poll=0.02, raise_on_timeout=True
        )
    assert ss.get("queued") is True       # entry still durable + queued
    assert ss.get("pushed") is not True   # but never falsely confirmed as pushed
