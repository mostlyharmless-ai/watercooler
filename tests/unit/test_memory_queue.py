"""Unit tests for the memory task queue system."""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from watercooler_mcp.memory_queue import (
    BulkCheckpoint,
    DuplicateTaskError,
    MemoryTask,
    MemoryTaskQueue,
    MemoryTaskWorker,
    TaskNotFoundError,
    TaskStatus,
    TaskType,
    enqueue_memory_task,
    init_memory_queue,
    load_checkpoint,
    save_checkpoint,
)


# ================================================================== #
# MemoryTask
# ================================================================== #


class TestMemoryTask:
    """Task dataclass serialisation and state transitions."""

    def test_create_default(self):
        t = MemoryTask()
        assert t.status == TaskStatus.PENDING
        assert t.task_type == TaskType.SINGLE
        assert t.attempt == 0
        assert t.task_id  # Non-empty auto-generated

    def test_to_dict_roundtrip(self):
        t = MemoryTask(entry_id="E1", topic="foo", backend="graphiti")
        d = t.to_dict()
        t2 = MemoryTask.from_dict(d)
        assert t2.entry_id == "E1"
        assert t2.topic == "foo"
        assert t2.backend == "graphiti"

    def test_json_line_roundtrip(self):
        t = MemoryTask(entry_id="E2", content="hello world")
        line = t.to_json_line()
        t2 = MemoryTask.from_json_line(line)
        assert t2.entry_id == "E2"
        assert t2.content == "hello world"

    def test_from_dict_ignores_unknown_keys(self):
        d = {"entry_id": "E3", "unknown_field": "ignored"}
        t = MemoryTask.from_dict(d)
        assert t.entry_id == "E3"

    def test_mark_running(self):
        t = MemoryTask()
        t.mark_running()
        assert t.status == TaskStatus.RUNNING
        assert t.attempt == 1

    def test_mark_completed(self):
        t = MemoryTask()
        t.mark_running()
        t.mark_completed(episode_uuid="ep-1", entities=["A", "B"], facts=3)
        assert t.status == TaskStatus.COMPLETED
        assert t.episode_uuid == "ep-1"
        assert t.entities_extracted == ["A", "B"]
        assert t.facts_extracted == 3
        assert t.is_terminal

    def test_mark_failed_retry(self):
        t = MemoryTask(max_attempts=3)
        t.mark_running()  # attempt 1
        t.mark_failed("timeout", backoff_base=1.0)
        assert t.status == TaskStatus.PENDING
        assert t.next_retry_at > 0
        assert not t.is_terminal

    def test_mark_failed_dead_letter(self):
        t = MemoryTask(max_attempts=2)
        t.mark_running()  # attempt 1
        t.mark_failed("err1")
        t.mark_running()  # attempt 2
        t.mark_failed("err2")
        assert t.status == TaskStatus.DEAD_LETTER
        assert t.is_terminal

    def test_backoff_increases(self):
        t = MemoryTask(max_attempts=5)
        delays = []
        for i in range(3):
            t.mark_running()
            before = time.time()
            t.mark_failed("err", backoff_base=10.0)
            delays.append(t.next_retry_at - before)
        # Each delay should be roughly double the previous (±jitter)
        assert delays[1] > delays[0] * 1.5
        assert delays[2] > delays[1] * 1.5

    def test_is_ready(self):
        t = MemoryTask()
        assert t.is_ready  # PENDING, no delay
        t.next_retry_at = time.time() + 9999
        assert not t.is_ready  # Delayed
        t.next_retry_at = 0
        t.mark_running()
        assert not t.is_ready  # RUNNING

    def test_dedup_key(self):
        t = MemoryTask(entry_id="E1", backend="graphiti")
        assert t.dedup_key() == "E1:graphiti"


# ================================================================== #
# MemoryTaskQueue
# ================================================================== #


class TestMemoryTaskQueue:
    """Queue operations, persistence, and deduplication."""

    @pytest.fixture
    def queue(self, tmp_path: Path) -> MemoryTaskQueue:
        return MemoryTaskQueue(queue_dir=tmp_path / "q")

    def test_enqueue_dequeue(self, queue: MemoryTaskQueue):
        t = MemoryTask(entry_id="E1")
        queue.enqueue(t)
        assert queue.depth() == 1
        assert queue.pending_count() == 1

        got = queue.dequeue()
        assert got is not None
        assert got.task_id == t.task_id
        assert got.status == TaskStatus.RUNNING
        assert queue.running_count() == 1

    def test_dequeue_empty(self, queue: MemoryTaskQueue):
        assert queue.dequeue() is None

    def test_complete_removes_from_queue(self, queue: MemoryTaskQueue):
        t = MemoryTask(entry_id="E1")
        queue.enqueue(t)
        got = queue.dequeue()
        queue.complete(got.task_id, episode_uuid="ep-1")
        assert queue.depth() == 0

    def test_fail_retries(self, queue: MemoryTaskQueue):
        t = MemoryTask(entry_id="E1", max_attempts=3)
        queue.enqueue(t)
        got = queue.dequeue()
        queue.fail(got.task_id, "err1", backoff_base=0.0)
        # Task should be back in queue as PENDING
        assert queue.depth() == 1
        assert queue.pending_count() == 1

    def test_fail_dead_letters(self, queue: MemoryTaskQueue, tmp_path: Path):
        t = MemoryTask(entry_id="E1", max_attempts=1)
        queue.enqueue(t)
        got = queue.dequeue()
        queue.fail(got.task_id, "fatal")
        # Should be dead-lettered: removed from active, written to DL file
        assert queue.depth() == 0
        dl_file = tmp_path / "q" / "dead_letter.jsonl"
        assert dl_file.exists()
        assert "E1" in dl_file.read_text()

    def test_duplicate_rejected(self, queue: MemoryTaskQueue):
        t1 = MemoryTask(entry_id="E1", backend="graphiti")
        t2 = MemoryTask(entry_id="E1", backend="graphiti")
        queue.enqueue(t1)
        with pytest.raises(DuplicateTaskError):
            queue.enqueue(t2)

    def test_duplicate_allowed(self, queue: MemoryTaskQueue):
        t1 = MemoryTask(entry_id="E1")
        t2 = MemoryTask(entry_id="E1")
        queue.enqueue(t1)
        queue.enqueue(t2, allow_duplicate=True)
        assert queue.depth() == 2

    def test_persistence_survives_reload(self, tmp_path: Path):
        qdir = tmp_path / "q"
        q1 = MemoryTaskQueue(queue_dir=qdir)
        q1.enqueue(MemoryTask(entry_id="E1"))
        q1.enqueue(MemoryTask(entry_id="E2"), allow_duplicate=True)
        assert q1.depth() == 2

        # Simulate restart
        q2 = MemoryTaskQueue(queue_dir=qdir)
        assert q2.depth() == 2

    def test_recover_stale(self, queue: MemoryTaskQueue):
        t = MemoryTask(entry_id="E1")
        queue.enqueue(t)
        got = queue.dequeue()
        # Fake stale by backdating updated_at
        got.updated_at = time.time() - 9999
        queue._tasks[got.task_id] = got

        recovered = queue.recover_stale(stale_seconds=1.0)
        assert recovered == 1
        assert queue.pending_count() == 1

    def test_retry_dead_letters(self, queue: MemoryTaskQueue):
        t = MemoryTask(entry_id="E1", max_attempts=1)
        queue.enqueue(t)
        got = queue.dequeue()
        queue.fail(got.task_id, "err")
        assert queue.depth() == 0

        # Re-enqueue from dead letter
        count = queue.retry_dead_letters()
        assert count == 1
        assert queue.depth() == 1
        assert queue.pending_count() == 1

    def test_status_summary(self, queue: MemoryTaskQueue):
        queue.enqueue(MemoryTask(entry_id="E1"))
        summary = queue.status_summary()
        assert summary["queue_depth"] == 1
        assert summary["by_status"]["pending"] == 1
        assert summary["stats"]["total_enqueued"] == 1

    def test_task_not_found(self, queue: MemoryTaskQueue):
        with pytest.raises(TaskNotFoundError):
            queue.complete("nonexistent")

    def test_stats_persist(self, tmp_path: Path):
        qdir = tmp_path / "q"
        q1 = MemoryTaskQueue(queue_dir=qdir)
        q1.enqueue(MemoryTask(entry_id="E1"))
        got = q1.dequeue()
        q1.complete(got.task_id)

        # Reload and check stats
        q2 = MemoryTaskQueue(queue_dir=qdir)
        assert q2._stats["total_completed"] == 1


# ================================================================== #
# MemoryTaskWorker
# ================================================================== #


class TestMemoryTaskWorker:
    """Worker lifecycle and task processing."""

    @pytest.fixture
    def queue(self, tmp_path: Path) -> MemoryTaskQueue:
        return MemoryTaskQueue(queue_dir=tmp_path / "q")

    @pytest.fixture
    def worker(self, queue: MemoryTaskQueue) -> MemoryTaskWorker:
        return MemoryTaskWorker(queue, poll_interval=0.1, stale_timeout=1.0)

    def test_start_stop(self, worker: MemoryTaskWorker):
        worker.start()
        assert worker.is_running
        ok = worker.stop(timeout=5.0)
        assert ok
        assert not worker.is_running

    def test_processes_task(self, queue: MemoryTaskQueue, worker: MemoryTaskWorker):
        async def mock_executor(task: MemoryTask):
            return {
                "episode_uuid": "ep-1",
                "entities_extracted": ["A"],
                "facts_extracted": 2,
            }

        worker.register_executor("graphiti", mock_executor)
        queue.enqueue(MemoryTask(entry_id="E1", backend="graphiti", content="test"))

        worker.start()
        # Wait for processing
        deadline = time.time() + 5.0
        while queue.depth() > 0 and time.time() < deadline:
            time.sleep(0.1)
        worker.stop()

        assert queue.depth() == 0
        assert queue._stats["total_completed"] == 1

    def test_retries_on_failure(self, queue: MemoryTaskQueue, worker: MemoryTaskWorker):
        call_count = 0

        async def failing_executor(task: MemoryTask):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("transient failure")
            return {"episode_uuid": "ep-2"}

        worker.register_executor("graphiti", failing_executor)

        # Override queue.fail to use zero backoff so retries are immediate
        original_fail = queue.fail
        queue.fail = lambda tid, err, **kw: original_fail(tid, err, backoff_base=0.0)

        queue.enqueue(
            MemoryTask(
                entry_id="E1", backend="graphiti",
                content="test", max_attempts=3,
            ),
        )

        worker.start()
        deadline = time.time() + 10.0
        while queue.depth() > 0 and time.time() < deadline:
            time.sleep(0.1)
        worker.stop()

        assert call_count == 2
        assert queue.depth() == 0
        assert queue._stats["total_completed"] == 1

    def test_dead_letters_after_max_attempts(
        self, queue: MemoryTaskQueue, worker: MemoryTaskWorker,
    ):
        async def always_fail(task: MemoryTask):
            raise RuntimeError("permanent failure")

        worker.register_executor("graphiti", always_fail)
        queue.enqueue(
            MemoryTask(
                entry_id="E1", backend="graphiti",
                content="test", max_attempts=1,
            ),
        )

        worker.start()
        deadline = time.time() + 5.0
        while queue.depth() > 0 and time.time() < deadline:
            time.sleep(0.1)
        worker.stop()

        assert queue.depth() == 0
        assert queue._stats["total_dead_lettered"] == 1

    def test_no_executor_fails_task(
        self, queue: MemoryTaskQueue, worker: MemoryTaskWorker,
    ):
        # No executor registered for "graphiti"
        # Use max_attempts=1 so it dead-letters immediately
        queue.enqueue(
            MemoryTask(
                entry_id="E1", backend="graphiti",
                content="test", max_attempts=1,
            ),
        )

        worker.start()
        deadline = time.time() + 5.0
        while queue.depth() > 0 and time.time() < deadline:
            time.sleep(0.1)
        worker.stop()

        # Should be dead-lettered after single attempt
        assert queue.depth() == 0


# ================================================================== #
# BulkCheckpoint
# ================================================================== #


class TestBulkCheckpoint:
    """Checkpoint serialisation and progress tracking."""

    def test_roundtrip(self, tmp_path: Path):
        ckpt = BulkCheckpoint(
            task_id="T1",
            backend="graphiti",
            total_entries=3,
        )
        ckpt.mark_entry_started("E1", topic="foo")
        ckpt.mark_entry_complete("E1", episode_uuid="ep-1")

        path = tmp_path / "checkpoint.json"
        save_checkpoint(ckpt, path)

        loaded = load_checkpoint(path)
        assert loaded is not None
        assert loaded.task_id == "T1"
        assert loaded.is_entry_complete("E1")
        assert loaded.entries["E1"].episode_uuid == "ep-1"
        assert loaded.completed_entries == 1

    def test_load_missing_returns_none(self, tmp_path: Path):
        assert load_checkpoint(tmp_path / "nope.json") is None

    def test_load_corrupt_returns_none(self, tmp_path: Path):
        path = tmp_path / "bad.json"
        path.write_text("not json")
        assert load_checkpoint(path) is None

    def test_next_pending(self):
        ckpt = BulkCheckpoint(total_entries=2)
        ckpt.mark_entry_started("E1")
        ckpt.mark_entry_complete("E1")
        ckpt.mark_entry_started("E2")
        assert ckpt.next_pending() == "E2"

    def test_mark_entry_failed(self):
        ckpt = BulkCheckpoint()
        ckpt.mark_entry_started("E1")
        ckpt.mark_entry_failed("E1", "kaboom")
        assert ckpt.entries["E1"].status == "failed"
        assert ckpt.entries["E1"].error == "kaboom"


# ================================================================== #
# Module-level convenience API
# ================================================================== #


class TestModuleAPI:
    """Tests for init_memory_queue and enqueue_memory_task."""

    def test_init_creates_singleton(self, tmp_path: Path):
        import watercooler_mcp.memory_queue as mq

        # Reset global state
        mq._queue = None
        mq._worker = None

        q = init_memory_queue(queue_dir=tmp_path / "q", start_worker=False)
        assert q is not None
        assert mq.get_queue() is q

        # Idempotent
        q2 = init_memory_queue(queue_dir=tmp_path / "q2", start_worker=False)
        assert q2 is q  # Same instance

        # Clean up
        mq._queue = None
        mq._worker = None

    def test_enqueue_when_not_initialised(self):
        import watercooler_mcp.memory_queue as mq
        mq._queue = None
        mq._worker = None

        result = enqueue_memory_task(
            entry_id="E1", topic="foo", group_id="g1", content="test",
        )
        assert result is None

    def test_enqueue_returns_task_id(self, tmp_path: Path):
        import watercooler_mcp.memory_queue as mq
        mq._queue = None
        mq._worker = None

        init_memory_queue(queue_dir=tmp_path / "q", start_worker=False)

        tid = enqueue_memory_task(
            entry_id="E1", topic="foo", group_id="g1", content="test",
        )
        assert tid is not None
        assert len(tid) > 0

        # Clean up
        mq._queue = None
        mq._worker = None
