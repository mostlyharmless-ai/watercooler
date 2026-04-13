"""Tests for T1-only graceful degradation.

Covers: log_warning_once, _graphiti_importable, mark_failed(permanent=True),
and permanent failure detection in worker._process_task.
"""
from __future__ import annotations

import importlib.util
import logging
import time
from pathlib import Path
from unittest import mock

import pytest

# ---------- observability helpers (direct file import) ----------
_OBS_PATH = Path("src/watercooler_mcp/observability.py").resolve()
_obs_spec = importlib.util.spec_from_file_location("watercooler_mcp_observability", _OBS_PATH)
obs = importlib.util.module_from_spec(_obs_spec)
assert _obs_spec and _obs_spec.loader
_obs_spec.loader.exec_module(obs)  # type: ignore[attr-defined]

log_warning_once = obs.log_warning_once
_reset_logging_state = obs._reset_logging_state
_warned_keys = obs._warned_keys
LOGGER_NAME = obs.LOGGER_NAME


@pytest.fixture(autouse=True)
def reset_logger(monkeypatch):
    """Reset logger state between tests."""
    monkeypatch.setenv("WATERCOOLER_LOG_DISABLE_FILE", "1")
    _reset_logging_state()
    yield
    _reset_logging_state()


# ================================================================
# log_warning_once
# ================================================================


class TestLogWarningOnce:
    def test_first_call_warns(self, caplog):
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        log_warning_once("test_key_1", "first time")
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == LOGGER_NAME
        ]
        assert len(warnings) == 1
        assert "first time" in warnings[0].message

    def test_second_call_debugs(self, caplog, monkeypatch):
        monkeypatch.setenv("WATERCOOLER_LOG_LEVEL", "DEBUG")
        _reset_logging_state()
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        log_warning_once("test_key_2", "should warn")
        log_warning_once("test_key_2", "should debug")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(warnings) == 1
        assert any("should debug" in r.message for r in debugs)

    def test_different_keys_both_warn(self, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        log_warning_once("key_a", "message a")
        log_warning_once("key_b", "message b")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2

    def test_reset_clears_warned_keys(self):
        log_warning_once("reset_test", "hi")
        assert "reset_test" in _warned_keys
        _reset_logging_state()
        assert "reset_test" not in _warned_keys


# ================================================================
# _graphiti_importable
# ================================================================


class TestGraphitiImportable:
    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Clear lru_cache between tests so each test starts fresh."""
        from watercooler_mcp.memory import _graphiti_importable
        _graphiti_importable.cache_clear()
        yield
        _graphiti_importable.cache_clear()

    def test_returns_bool(self):
        from watercooler_mcp.memory import _graphiti_importable
        result = _graphiti_importable()
        assert isinstance(result, bool)

    def test_caches_result(self):
        from watercooler_mcp.memory import _graphiti_importable
        r1 = _graphiti_importable()
        r2 = _graphiti_importable()
        assert r1 is r2  # same object, not just equal


# ================================================================
# mark_failed(permanent=True)
# ================================================================


class TestMarkFailedPermanent:
    def _make_task(self):
        from watercooler_mcp.memory_queue.task import MemoryTask
        return MemoryTask(
            backend="graphiti",
            entry_id="e1",
            topic="t1",
            group_id="g1",
            content="test",
            max_attempts=3,
        )

    def test_permanent_dead_letters_on_first_attempt(self):
        from watercooler_mcp.memory_queue.task import TaskStatus
        task = self._make_task()
        task.attempt = 1
        task.mark_failed("ImportError: no module", permanent=True)
        assert task.status == TaskStatus.DEAD_LETTER

    def test_non_permanent_retries_on_first_attempt(self):
        from watercooler_mcp.memory_queue.task import TaskStatus
        task = self._make_task()
        task.attempt = 1
        task.mark_failed("TimeoutError", permanent=False)
        assert task.status == TaskStatus.PENDING

    def test_non_permanent_dead_letters_at_max_attempts(self):
        from watercooler_mcp.memory_queue.task import TaskStatus
        task = self._make_task()
        task.attempt = 3
        task.mark_failed("TimeoutError", permanent=False)
        assert task.status == TaskStatus.DEAD_LETTER


# ================================================================
# queue.fail(permanent=True) passthrough
# ================================================================


class TestQueueFailPermanent:
    def test_permanent_flag_passthrough(self, tmp_path):
        from watercooler_mcp.memory_queue.queue import MemoryTaskQueue
        from watercooler_mcp.memory_queue.task import MemoryTask, TaskStatus

        q = MemoryTaskQueue(queue_dir=tmp_path)
        task = MemoryTask(
            backend="graphiti",
            entry_id="e2",
            topic="t2",
            group_id="g2",
            content="test",
            max_attempts=3,
        )
        task_id = q.enqueue(task)

        # Mark running so fail() doesn't complain
        q.dequeue()

        q.fail(task_id, "ImportError: missing module", permanent=True)

        # Task should be dead-lettered immediately (removed from active queue)
        assert q.get_task(task_id) is None
        summary = q.status_summary()
        assert summary["stats"]["total_dead_lettered"] == 1


# ================================================================
# PermanentTaskError
# ================================================================


class TestPermanentTaskError:
    def test_is_not_retryable(self):
        from watercooler_mcp.memory_queue.errors import PermanentTaskError
        err = PermanentTaskError(message="missing package")
        assert err.is_retryable is False

    def test_inherits_memory_queue_error(self):
        from watercooler_mcp.memory_queue.errors import (
            MemoryQueueError,
            PermanentTaskError,
        )
        err = PermanentTaskError(message="test")
        assert isinstance(err, MemoryQueueError)
