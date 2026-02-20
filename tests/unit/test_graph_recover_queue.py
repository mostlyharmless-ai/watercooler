"""Unit tests for graph_recover queue integration (#139).

Tests cover:
- resolve_recovery_targets() extraction
- graph_recover executor (content validation, sync_thread_to_graph delegation)
- _enqueue_recovery_tasks() dedup and queue-full handling
- _graph_recover_impl() queue vs sync fallback paths
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler.baseline_graph import storage
from watercooler_mcp.memory_queue import (
    DuplicateTaskError,
    MemoryTask,
    MemoryTaskQueue,
    QueueFullError,
)


# ================================================================== #
# resolve_recovery_targets
# ================================================================== #


class TestResolveRecoveryTargets:
    """Tests for the extracted resolve_recovery_targets() function."""

    def test_invalid_mode_returns_error(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        topics, errors = resolve_recovery_targets(tmp_path, mode="bogus")
        assert topics == []
        assert any("Invalid mode" in e for e in errors)

    def test_selective_without_topics_returns_error(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        topics, errors = resolve_recovery_targets(tmp_path, mode="selective")
        assert topics == []
        assert any("requires topics" in e for e in errors)

    def test_selective_filters_to_existing_files(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        (tmp_path / "alpha.md").write_text("# Alpha")
        (tmp_path / "beta.md").write_text("# Beta")

        # Write graph data so topics are discoverable
        graph_dir = storage.ensure_graph_dir(tmp_path)
        for topic in ("alpha", "beta"):
            td = storage.ensure_thread_graph_dir(graph_dir, topic)
            storage.atomic_write_json(td / "meta.json", {
                "id": f"thread:{topic}", "type": "thread", "topic": topic,
                "status": "OPEN", "entry_count": 0,
            })

        topics, errors = resolve_recovery_targets(
            tmp_path, mode="selective", topics=["alpha", "gamma"]
        )
        assert errors == []
        assert topics == ["alpha"]

    def test_selective_no_matching_files(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        (tmp_path / "alpha.md").write_text("# Alpha")

        topics, errors = resolve_recovery_targets(
            tmp_path, mode="selective", topics=["missing"]
        )
        assert topics == []
        assert any("No matching" in e for e in errors)

    def test_all_mode_returns_all_topics(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        (tmp_path / "one.md").write_text("# One")
        (tmp_path / "two.md").write_text("# Two")
        (tmp_path / "three.md").write_text("# Three")

        # Write graph data so topics are discoverable
        graph_dir = storage.ensure_graph_dir(tmp_path)
        for topic in ("one", "two", "three"):
            td = storage.ensure_thread_graph_dir(graph_dir, topic)
            storage.atomic_write_json(td / "meta.json", {
                "id": f"thread:{topic}", "type": "thread", "topic": topic,
                "status": "OPEN", "entry_count": 0,
            })

        topics, errors = resolve_recovery_targets(tmp_path, mode="all")
        assert errors == []
        assert sorted(topics) == ["one", "three", "two"]

    @patch("watercooler.baseline_graph.sync.check_graph_health")
    def test_stale_mode_uses_health_check(self, mock_health, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        (tmp_path / "good.md").write_text("# Good")
        (tmp_path / "stale.md").write_text("# Stale")
        (tmp_path / "broken.md").write_text("# Broken")

        mock_report = MagicMock()
        mock_report.stale_threads = ["stale"]
        mock_report.error_details = {"broken": "some error"}
        mock_health.return_value = mock_report

        topics, errors = resolve_recovery_targets(tmp_path, mode="stale")
        assert errors == []
        assert sorted(topics) == ["broken", "stale"]
        mock_health.assert_called_once_with(tmp_path)

    @patch("watercooler.baseline_graph.sync.check_graph_health")
    def test_stale_mode_nothing_to_recover(self, mock_health, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        mock_report = MagicMock()
        mock_report.stale_threads = []
        mock_report.error_details = {}
        mock_health.return_value = mock_report

        topics, errors = resolve_recovery_targets(tmp_path, mode="stale")
        assert topics == []
        assert errors == []


# ================================================================== #
# graph_recover executor
# ================================================================== #


class TestGraphRecoverExecutor:
    """Tests for _graph_recover_executor_fn in memory_sync."""

    @pytest.mark.anyio
    async def test_valid_payload_calls_sync(self, tmp_path):
        from watercooler_mcp.memory_sync import _graph_recover_executor_fn

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        task = MemoryTask(
            backend="graph_recover",
            topic="my-topic",
            content=json.dumps({
                "schema_version": 1,
                "threads_dir": str(threads_dir),
                "generate_summaries": False,
                "generate_embeddings": True,
            }),
        )

        with patch(
            "watercooler.baseline_graph.sync.sync_thread_to_graph",
            return_value=True,
        ) as mock_sync:
            result = await _graph_recover_executor_fn(task)

        mock_sync.assert_called_once_with(
            threads_dir=threads_dir,
            topic="my-topic",
            generate_summaries=False,
            generate_embeddings=True,
        )
        assert result["recovered"] is True
        assert result["topic"] == "my-topic"

    @pytest.mark.anyio
    async def test_invalid_json_raises(self):
        from watercooler_mcp.memory_sync import _graph_recover_executor_fn

        task = MemoryTask(backend="graph_recover", topic="x", content="not json")
        with pytest.raises(RuntimeError, match="Invalid task content JSON"):
            await _graph_recover_executor_fn(task)

    @pytest.mark.anyio
    async def test_wrong_schema_version_raises(self):
        from watercooler_mcp.memory_sync import _graph_recover_executor_fn

        task = MemoryTask(
            backend="graph_recover",
            topic="x",
            content=json.dumps({"schema_version": 99, "threads_dir": "/tmp"}),
        )
        with pytest.raises(RuntimeError, match="Unsupported schema_version"):
            await _graph_recover_executor_fn(task)

    @pytest.mark.anyio
    async def test_missing_threads_dir_raises(self):
        from watercooler_mcp.memory_sync import _graph_recover_executor_fn

        task = MemoryTask(
            backend="graph_recover",
            topic="x",
            content=json.dumps({"schema_version": 1}),
        )
        with pytest.raises(RuntimeError, match="Missing threads_dir"):
            await _graph_recover_executor_fn(task)

    @pytest.mark.anyio
    async def test_nonexistent_threads_dir_raises(self):
        from watercooler_mcp.memory_sync import _graph_recover_executor_fn

        task = MemoryTask(
            backend="graph_recover",
            topic="x",
            content=json.dumps({
                "schema_version": 1,
                "threads_dir": "/nonexistent/path/12345",
            }),
        )
        with pytest.raises(RuntimeError, match="threads_dir not found"):
            await _graph_recover_executor_fn(task)

    @pytest.mark.anyio
    async def test_sync_failure_raises(self, tmp_path):
        from watercooler_mcp.memory_sync import _graph_recover_executor_fn

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        task = MemoryTask(
            backend="graph_recover",
            topic="fail-topic",
            content=json.dumps({
                "schema_version": 1,
                "threads_dir": str(threads_dir),
            }),
        )

        with patch(
            "watercooler.baseline_graph.sync.sync_thread_to_graph",
            return_value=False,
        ):
            with pytest.raises(RuntimeError, match="sync_thread_to_graph failed"):
                await _graph_recover_executor_fn(task)


# ================================================================== #
# Dedup: entry_id = group_id:topic
# ================================================================== #


class TestGraphRecoverDedup:
    """Verify cross-repo dedup with group_id:topic entry_id scheme."""

    def test_same_topic_different_group_id_both_enqueue(self, tmp_path):
        """Same topic in different repos should not collide."""
        queue = MemoryTaskQueue(queue_dir=tmp_path, max_depth=100)

        task_a = MemoryTask(
            backend="graph_recover",
            entry_id="repo_a:shared-topic",
            topic="shared-topic",
            group_id="repo_a",
        )
        task_b = MemoryTask(
            backend="graph_recover",
            entry_id="repo_b:shared-topic",
            topic="shared-topic",
            group_id="repo_b",
        )

        id_a = queue.enqueue(task_a)
        id_b = queue.enqueue(task_b)
        assert id_a != id_b
        assert queue.depth() == 2

    def test_same_group_same_topic_duplicate_rejected(self, tmp_path):
        """Same group_id:topic should be deduplicated."""
        queue = MemoryTaskQueue(queue_dir=tmp_path, max_depth=100)

        task1 = MemoryTask(
            backend="graph_recover",
            entry_id="repo_a:topic-x",
            topic="topic-x",
            group_id="repo_a",
        )
        task2 = MemoryTask(
            backend="graph_recover",
            entry_id="repo_a:topic-x",
            topic="topic-x",
            group_id="repo_a",
        )

        queue.enqueue(task1)
        with pytest.raises(DuplicateTaskError):
            queue.enqueue(task2)

    def test_same_topic_different_backend_both_enqueue(self, tmp_path):
        """Same entry_id but different backend should both succeed."""
        queue = MemoryTaskQueue(queue_dir=tmp_path, max_depth=100)

        task_recover = MemoryTask(
            backend="graph_recover",
            entry_id="repo_a:topic-x",
            topic="topic-x",
            group_id="repo_a",
        )
        task_graphiti = MemoryTask(
            backend="graphiti",
            entry_id="repo_a:topic-x",
            topic="topic-x",
            group_id="repo_a",
        )

        id1 = queue.enqueue(task_recover)
        id2 = queue.enqueue(task_graphiti)
        assert id1 != id2
        assert queue.depth() == 2


# ================================================================== #
# _graph_recover_impl queue vs sync paths
# ================================================================== #


class TestGraphRecoverImplQueuePath:
    """Tests for _graph_recover_impl routing to queue or sync fallback."""

    def _make_context(self, threads_dir):
        """Build a minimal ThreadContext-like mock."""
        ctx = MagicMock()
        ctx.threads_dir = threads_dir
        ctx.code_root = threads_dir.parent
        return ctx

    @pytest.mark.anyio
    @patch("watercooler_mcp.tools.graph.validation")
    async def test_dry_run_skips_queue(self, mock_validation, tmp_path):
        """dry_run=True should never enqueue, always return dry-run results."""
        from watercooler_mcp.tools.graph import _graph_recover_impl

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "alpha.md").write_text("# Alpha\n")

        ctx_mock = self._make_context(threads_dir)
        mock_validation._require_context.return_value = (None, ctx_mock)

        with patch(
            "watercooler.baseline_graph.sync.resolve_recovery_targets",
            return_value=(["alpha"], []),
        ), patch(
            "watercooler.baseline_graph.sync.recover_graph",
        ) as mock_recover:
            mock_result = MagicMock()
            mock_result.to_dict.return_value = {
                "threads_recovered": 0,
                "entries_parsed": 2,
                "dry_run": True,
            }
            mock_recover.return_value = mock_result

            result = await _graph_recover_impl(
                MagicMock(), code_path="/repo", mode="all", dry_run=True
            )

        output = json.loads(result)
        assert output["dry_run"] is True
        mock_recover.assert_called_once()

    @pytest.mark.anyio
    @patch("watercooler_mcp.tools.graph._get_recover_queue")
    @patch("watercooler_mcp.tools.graph.validation")
    async def test_queue_available_enqueues_tasks(self, mock_validation, mock_get_queue, tmp_path):
        """When queue is available, tasks should be enqueued per topic."""
        from watercooler_mcp.tools.graph import _graph_recover_impl

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        ctx_mock = self._make_context(threads_dir)
        mock_validation._require_context.return_value = (None, ctx_mock)

        mock_queue = MagicMock()
        mock_queue.enqueue.side_effect = lambda t: t.task_id
        mock_queue.status_summary.return_value = {
            "queue_depth": 2,
            "by_status": {"pending": 2},
        }
        mock_worker = MagicMock()
        mock_get_queue.return_value = (mock_queue, mock_worker)

        with patch(
            "watercooler.baseline_graph.sync.resolve_recovery_targets",
            return_value=(["topic-a", "topic-b"], []),
        ), patch(
            "watercooler.path_resolver.derive_group_id",
            return_value="test_repo",
        ):
            result = await _graph_recover_impl(
                MagicMock(), code_path="/repo", mode="stale"
            )

        output = json.loads(result)
        assert output["mode"] == "queued"
        assert output["tasks_enqueued"] == 2
        assert output["topics"] == ["topic-a", "topic-b"]
        assert output["skipped"] == []
        assert "queue_status" in output
        mock_worker.wake.assert_called_once()

    @pytest.mark.anyio
    @patch("watercooler_mcp.tools.graph._get_recover_queue")
    @patch("watercooler_mcp.tools.graph.run_with_graph_sync")
    @patch("watercooler_mcp.tools.graph.validation")
    async def test_no_queue_falls_back_to_sync(
        self, mock_validation, mock_run_sync, mock_get_queue, tmp_path
    ):
        """When queue unavailable, should fall back to synchronous recovery."""
        from watercooler_mcp.tools.graph import _graph_recover_impl

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        ctx_mock = self._make_context(threads_dir)
        mock_validation._require_context.return_value = (None, ctx_mock)
        mock_get_queue.return_value = (None, None)

        mock_run_sync.return_value = {
            "threads_recovered": 3,
            "entries_parsed": 10,
            "errors": [],
        }

        with patch(
            "watercooler.baseline_graph.sync.resolve_recovery_targets",
            return_value=(["a", "b", "c"], []),
        ):
            result = await _graph_recover_impl(
                MagicMock(), code_path="/repo", mode="all"
            )

        output = json.loads(result)
        assert output["threads_recovered"] == 3
        mock_run_sync.assert_called_once()

    @pytest.mark.anyio
    @patch("watercooler_mcp.tools.graph._get_recover_queue")
    @patch("watercooler_mcp.tools.graph.validation")
    async def test_queue_reports_skipped_duplicates(self, mock_validation, mock_get_queue, tmp_path):
        """Duplicate and queue-full topics should appear in skipped list."""
        from watercooler_mcp.tools.graph import _graph_recover_impl

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        ctx_mock = self._make_context(threads_dir)
        mock_validation._require_context.return_value = (None, ctx_mock)

        mock_queue = MagicMock()
        call_count = 0

        def enqueue_side_effect(task):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return task.task_id
            elif call_count == 2:
                raise DuplicateTaskError("dup", existing_task_id="old-id")
            else:
                raise QueueFullError("full")

        mock_queue.enqueue.side_effect = enqueue_side_effect
        mock_queue.status_summary.return_value = {"queue_depth": 1, "by_status": {"pending": 1}}
        mock_get_queue.return_value = (mock_queue, MagicMock())

        with patch(
            "watercooler.baseline_graph.sync.resolve_recovery_targets",
            return_value=(["ok-topic", "dup-topic", "full-topic"], []),
        ), patch(
            "watercooler.path_resolver.derive_group_id",
            return_value="test_repo",
        ):
            result = await _graph_recover_impl(
                MagicMock(), code_path="/repo", mode="all"
            )

        output = json.loads(result)
        assert output["tasks_enqueued"] == 1
        assert output["topics"] == ["ok-topic"]
        assert len(output["skipped"]) == 2
        reasons = {s["reason"] for s in output["skipped"]}
        assert reasons == {"duplicate", "queue_full"}

    @pytest.mark.anyio
    @patch("watercooler_mcp.tools.graph.validation")
    async def test_resolve_errors_returned_directly(self, mock_validation, tmp_path):
        """When resolve_recovery_targets returns errors, they're passed through."""
        from watercooler_mcp.tools.graph import _graph_recover_impl

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        ctx_mock = self._make_context(threads_dir)
        mock_validation._require_context.return_value = (None, ctx_mock)

        with patch(
            "watercooler.baseline_graph.sync.resolve_recovery_targets",
            return_value=([], ["Invalid mode: bogus"]),
        ):
            result = await _graph_recover_impl(
                MagicMock(), code_path="/repo", mode="bogus"
            )

        output = json.loads(result)
        assert "errors" in output
        assert "Invalid mode" in output["errors"][0]

    @pytest.mark.anyio
    @patch("watercooler_mcp.tools.graph.validation")
    async def test_nothing_to_recover(self, mock_validation, tmp_path):
        """When no targets found, return informational message."""
        from watercooler_mcp.tools.graph import _graph_recover_impl

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        ctx_mock = self._make_context(threads_dir)
        mock_validation._require_context.return_value = (None, ctx_mock)

        with patch(
            "watercooler.baseline_graph.sync.resolve_recovery_targets",
            return_value=([], []),
        ):
            result = await _graph_recover_impl(
                MagicMock(), code_path="/repo", mode="stale"
            )

        output = json.loads(result)
        assert "Nothing to recover" in output["message"]

    @pytest.mark.anyio
    @patch("watercooler_mcp.tools.graph._get_recover_queue")
    @patch("watercooler_mcp.tools.graph.validation")
    async def test_all_topics_skipped_as_duplicates(self, mock_validation, mock_get_queue, tmp_path):
        """When every topic is a duplicate, mode should be 'all_skipped' with clear message."""
        from watercooler_mcp.tools.graph import _graph_recover_impl

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        ctx_mock = self._make_context(threads_dir)
        mock_validation._require_context.return_value = (None, ctx_mock)

        mock_queue = MagicMock()
        mock_queue.enqueue.side_effect = DuplicateTaskError("dup", existing_task_id="old")
        mock_queue.status_summary.return_value = {"queue_depth": 2, "by_status": {"pending": 2}}
        mock_get_queue.return_value = (mock_queue, MagicMock())

        with patch(
            "watercooler.baseline_graph.sync.resolve_recovery_targets",
            return_value=(["topic-a", "topic-b"], []),
        ), patch(
            "watercooler.path_resolver.derive_group_id",
            return_value="test_repo",
        ):
            result = await _graph_recover_impl(
                MagicMock(), code_path="/repo", mode="stale"
            )

        output = json.loads(result)
        assert output["mode"] == "all_skipped"
        assert output["tasks_enqueued"] == 0
        assert output["topics"] == []
        assert len(output["skipped"]) == 2
        assert "already queued" in output["message"]
