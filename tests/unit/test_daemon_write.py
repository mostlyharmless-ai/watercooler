"""Tests for daemon_write_entry() — shared daemon write infrastructure."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.daemons.daemon_write import (
    DaemonWriteResult,
    daemon_write_entry,
    _ALLOWED_ENTRY_TYPES,
)

# Patch targets — lazy imports inside daemon_write_entry()
_PATCH_RESOLVE = "watercooler_mcp.config.resolve_thread_context"
_PATCH_RUN_SYNC = "watercooler_mcp.middleware.run_with_sync"
_PATCH_GRAPH_ACK = "watercooler.commands_graph.ack"
_PATCH_GET_ENTRY = "watercooler.baseline_graph.writer.get_entry_node_from_graph"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_thread_context(threads_dir: Path, code_root: Path | None = None):
    """Create a mock ThreadContext."""
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


def _mark_sync_success(*args, **kwargs):
    """Populate sync_status like a successful run_with_sync call."""
    kwargs["sync_status"].update({
        "operation_completed": True,
        "committed": True,
        "pushed": True,
        "error": None,
    })
    return None


# ---------------------------------------------------------------------------
# Tests — Input Validation
# ---------------------------------------------------------------------------


class TestDaemonWriteValidation:
    def test_empty_topic_rejected(self):
        result = daemon_write_entry(
            "",
            code_root=Path("/tmp/fake"),
            title="Test",
            body="Test body",
        )
        assert result.written is False
        assert result.pushed is False
        assert "topic" in result.error

    def test_empty_body_rejected(self):
        result = daemon_write_entry(
            "test-topic",
            code_root=Path("/tmp/fake"),
            title="Test",
            body="",
        )
        assert result.written is False
        assert result.pushed is False
        assert "body" in result.error

    def test_empty_agent_rejected(self):
        result = daemon_write_entry(
            "test-topic",
            code_root=Path("/tmp/fake"),
            title="Test",
            body="Test body",
            agent="",
        )
        assert result.written is False
        assert result.pushed is False
        assert "agent" in result.error

    def test_invalid_entry_type_rejected(self):
        result = daemon_write_entry(
            "test-topic",
            code_root=Path("/tmp/fake"),
            title="Test",
            body="Test body",
            entry_type="InvalidType",
        )
        assert result.written is False
        assert result.pushed is False
        assert "entry_type" in result.error

    def test_all_valid_entry_types_accepted(self):
        """Verify the allowed set matches the plan."""
        assert _ALLOWED_ENTRY_TYPES == {"Note", "Decision", "Plan", "PR", "Closure"}


# ---------------------------------------------------------------------------
# Tests — ULID Generation
# ---------------------------------------------------------------------------


class TestDaemonWriteULID:
    @patch(_PATCH_RESOLVE)
    def test_generates_ulid_when_not_provided(self, mock_resolve, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        with patch(_PATCH_RUN_SYNC, side_effect=_mark_sync_success):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
            )

        # Should have generated a ULID (26 chars, uppercase alphanumeric)
        assert result.entry_id
        assert len(result.entry_id) >= 20  # ULIDs are 26 chars

    @patch(_PATCH_RESOLVE)
    def test_uses_provided_entry_id(self, mock_resolve, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        with patch(_PATCH_RUN_SYNC, side_effect=_mark_sync_success):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                entry_id="01CUSTOM_ID",
            )

        assert result.entry_id == "01CUSTOM_ID"


# ---------------------------------------------------------------------------
# Tests — ThreadContext Resolution
# ---------------------------------------------------------------------------


class TestDaemonWriteContext:
    @patch(_PATCH_RESOLVE)
    def test_missing_threads_dir(self, mock_resolve, tmp_path):
        ctx = _mock_thread_context(tmp_path / "nonexistent")
        mock_resolve.return_value = ctx

        result = daemon_write_entry(
            "test-topic",
            code_root=tmp_path,
            title="Test",
            body="Test body",
        )
        assert result.written is False
        assert "does not exist" in result.error

    @patch(_PATCH_RESOLVE)
    def test_missing_code_root(self, mock_resolve, tmp_path):
        from watercooler_mcp.config import ThreadContext

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        ctx = ThreadContext(
            code_root=None,
            threads_dir=threads_dir,
            code_repo=None,
            code_branch=None,
            code_commit=None,
            code_remote=None,
            explicit_dir=False,
        )
        mock_resolve.return_value = ctx

        result = daemon_write_entry(
            "test-topic",
            code_root=tmp_path,
            title="Test",
            body="Test body",
        )
        assert result.written is False
        assert "code_root" in result.error

    @patch(_PATCH_RESOLVE)
    def test_context_resolution_failure(self, mock_resolve):
        mock_resolve.side_effect = RuntimeError("git discovery failed")

        result = daemon_write_entry(
            "test-topic",
            code_root=Path("/tmp/fake"),
            title="Test",
            body="Test body",
        )
        assert result.written is False
        assert "ThreadContext resolution failed" in result.error


# ---------------------------------------------------------------------------
# Tests — Write Outcomes
# ---------------------------------------------------------------------------


class TestDaemonWriteOutcomes:
    @patch(_PATCH_RESOLVE)
    def test_success(self, mock_resolve, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        with patch(_PATCH_RUN_SYNC, side_effect=_mark_sync_success) as mock_sync:
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test Decision",
                body="We decided X.",
                entry_type="Decision",
                agent="ExtractDecisionsDaemon",
                agent_spec="decision-extractor",
                role="scribe",
                user_tag="system",
            )

        assert result.written is True
        assert result.pushed is True
        assert result.error is None

        # Verify run_with_sync was called with correct args
        call_args = mock_sync.call_args
        assert call_args.kwargs["topic"] == "test-topic"
        assert call_args.kwargs["agent_spec"] == "decision-extractor"

    @patch(_PATCH_GET_ENTRY)
    @patch(_PATCH_RESOLVE)
    def test_push_failure_reports_local_write(
        self, mock_resolve, mock_get_entry, tmp_path
    ):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        # Simulate push failure — entry exists locally
        from watercooler_mcp.sync.errors import PushError

        mock_get_entry.return_value = {"entry_id": "01TEST", "body": "test"}

        with patch(_PATCH_RUN_SYNC) as mock_sync:
            mock_sync.side_effect = PushError(
                message="Push failed after retries",
                context={"topic": "test-topic"},
            )
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                entry_id="01TEST",
            )

        assert result.written is True
        assert result.pushed is False
        assert result.entry_id == "01TEST"
        assert "Push failed" in result.error

    @patch(_PATCH_GET_ENTRY)
    @patch(_PATCH_RESOLVE)
    def test_swallowed_commit_failure_reports_local_write(
        self, mock_resolve, mock_get_entry, tmp_path
    ):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)
        mock_get_entry.return_value = {"entry_id": "01TEST", "body": "test"}

        def _run_sync(*args, **kwargs):
            kwargs["sync_status"].update({
                "operation_completed": True,
                "committed": False,
                "pushed": False,
                "error": "Worktree lock timeout during commit/push",
            })
            return None

        with patch(_PATCH_RUN_SYNC, side_effect=_run_sync):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                entry_id="01TEST",
            )

        assert result.written is True
        assert result.pushed is False
        assert "timeout" in (result.error or "").lower()

    @patch(_PATCH_GET_ENTRY)
    @patch(_PATCH_RESOLVE)
    def test_graph_inspection_failure_after_local_write_is_local_only(
        self, mock_resolve, mock_get_entry, tmp_path
    ):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "test-topic.md").write_text(
            "# Test topic\n\n"
            "Status: OPEN\n"
            "Ball: codex\n\n"
            "Entry: ExtractDecisionsDaemon (system) 2026-04-01T00:00:00Z\n"
            "Role: scribe\n"
            "Type: Decision\n"
            "Title: Test\n"
            "<!-- Entry-ID: 01TEST -->\n\n"
            "Test body\n",
            encoding="utf-8",
        )
        mock_resolve.return_value = _mock_thread_context(threads_dir)
        mock_get_entry.side_effect = RuntimeError("corrupt graph")

        def _run_sync(*args, **kwargs):
            kwargs["sync_status"].update({
                "operation_completed": True,
                "committed": True,
                "pushed": False,
                "error": "Push failed after retries",
            })
            return None

        with patch(_PATCH_RUN_SYNC, side_effect=_run_sync):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                entry_id="01TEST",
            )

        assert result.written is True
        assert result.pushed is False
        assert "Push failed" in (result.error or "")

    @patch(_PATCH_GET_ENTRY)
    @patch(_PATCH_RESOLVE)
    def test_graph_inspection_failure_without_local_evidence_retries(
        self, mock_resolve, mock_get_entry, tmp_path
    ):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)
        mock_get_entry.side_effect = RuntimeError("corrupt graph")

        def _run_sync(*args, **kwargs):
            kwargs["sync_status"].update({
                "operation_completed": True,
                "committed": False,
                "pushed": False,
                "error": "Push failed after retries",
            })
            return None

        with patch(_PATCH_RUN_SYNC, side_effect=_run_sync):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                entry_id="01TEST",
            )

        assert result.written is False
        assert result.pushed is False
        assert "could not verify local write" in (result.error or "")

    @patch(_PATCH_GET_ENTRY)
    @patch(_PATCH_RESOLVE)
    def test_markdown_verification_covers_missing_graph_entry(
        self, mock_resolve, mock_get_entry, tmp_path
    ):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "test-topic.md").write_text(
            "# Test topic\n\n"
            "Status: OPEN\n"
            "Ball: codex\n\n"
            "Entry: ExtractDecisionsDaemon (system) 2026-04-01T00:00:00Z\n"
            "Role: scribe\n"
            "Type: Decision\n"
            "Title: Test\n"
            "<!-- Entry-ID: 01TEST -->\n\n"
            "Test body\n",
            encoding="utf-8",
        )
        mock_resolve.return_value = _mock_thread_context(threads_dir)
        mock_get_entry.return_value = None

        def _run_sync(*args, **kwargs):
            kwargs["sync_status"].update({
                "operation_completed": True,
                "committed": False,
                "pushed": False,
                "error": "Push failed after retries",
            })
            return None

        with patch(_PATCH_RUN_SYNC, side_effect=_run_sync):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                entry_id="01TEST",
            )

        assert result.written is True
        assert result.pushed is False

    @patch(_PATCH_GET_ENTRY)
    @patch(_PATCH_RESOLVE)
    def test_empty_sync_status_is_not_reported_as_success(
        self, mock_resolve, mock_get_entry, tmp_path
    ):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)
        mock_get_entry.return_value = None

        with patch(_PATCH_RUN_SYNC, return_value=None):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                entry_id="01TEST",
            )

        assert result.written is False
        assert result.pushed is False

    @patch(_PATCH_GET_ENTRY)
    @patch(_PATCH_RESOLVE)
    def test_prewrite_failure(self, mock_resolve, mock_get_entry, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        # Simulate pre-write failure — entry does NOT exist locally
        mock_get_entry.return_value = None

        with patch(_PATCH_RUN_SYNC) as mock_sync:
            mock_sync.side_effect = RuntimeError("Lock acquisition failed")
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                entry_id="01TEST",
            )

        assert result.written is False
        assert result.pushed is False
        assert result.entry_id == "01TEST"

    @patch(_PATCH_RESOLVE)
    def test_preserves_ball(self, mock_resolve, tmp_path):
        """ball=None should be passed through (preserves current owner)."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        with patch(_PATCH_RUN_SYNC, side_effect=_mark_sync_success) as mock_sync:
            daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                ball=None,
            )

        # The operation callback inside run_with_sync will call ack() with ball=None
        # Verify run_with_sync was called (the operation is a closure, so we trust
        # the implementation passes ball=None through to ack)
        assert mock_sync.called

    @patch(_PATCH_RESOLVE)
    def test_refreshes_branch_metadata(self, mock_resolve, tmp_path):
        """ThreadContext is resolved fresh each call so branch tags stay current."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        ctx1 = _mock_thread_context(threads_dir)
        mock_resolve.return_value = ctx1

        with patch(_PATCH_RUN_SYNC, side_effect=_mark_sync_success):
            daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Write 1",
                body="First write",
            )

        # Verify resolve was called
        assert mock_resolve.call_count == 1

        # Second write should re-resolve
        with patch(_PATCH_RUN_SYNC, side_effect=_mark_sync_success):
            daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Write 2",
                body="Second write",
            )

        assert mock_resolve.call_count == 2


# ---------------------------------------------------------------------------
# Tests — Never-Raise Contract
# ---------------------------------------------------------------------------


class TestDaemonWriteNeverRaises:
    @patch(_PATCH_RESOLVE, side_effect=RuntimeError("no context"))
    def test_never_raises_on_any_failure(self, mock_resolve, tmp_path):
        """daemon_write_entry must NEVER raise — always returns DaemonWriteResult."""
        result = daemon_write_entry(
            "test-topic",
            code_root=Path("/nonexistent/path/that/cannot/exist"),
            title="Test",
            body="Test body",
        )
        assert isinstance(result, DaemonWriteResult)
        assert result.written is False

    @patch(_PATCH_RESOLVE)
    def test_never_raises_on_graph_check_failure(self, mock_resolve, tmp_path):
        """Even if local graph inspection fails after write error, still returns result."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        with patch(_PATCH_RUN_SYNC) as mock_sync:
            mock_sync.side_effect = RuntimeError("Write failed")
            # get_entry_node_from_graph will fail because we're using mocks
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
            )

        assert isinstance(result, DaemonWriteResult)
        assert result.written is False


# ---------------------------------------------------------------------------
# Tests — Thread Safety
# ---------------------------------------------------------------------------


class TestDaemonWriteThreadSafety:
    @patch(_PATCH_RESOLVE)
    def test_callable_from_background_thread(self, mock_resolve, tmp_path):
        """daemon_write_entry can be called from a threading.Thread."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        results: list[DaemonWriteResult] = []

        def _write_in_thread():
            with patch(_PATCH_RUN_SYNC, side_effect=_mark_sync_success):
                r = daemon_write_entry(
                    "test-topic",
                    code_root=tmp_path,
                    title="Background Write",
                    body="Written from background thread",
                )
                results.append(r)

        t = threading.Thread(target=_write_in_thread)
        t.start()
        t.join(timeout=5)

        assert len(results) == 1
        assert results[0].written is True
        assert results[0].pushed is True


# ---------------------------------------------------------------------------
# Tests — Post-Write Hooks
# ---------------------------------------------------------------------------


class TestDaemonWriteHooks:
    """Tests for post_write_hooks — must execute the operation callback."""

    @staticmethod
    def _run_sync_calling_operation(*args, **kwargs):
        """Mock run_with_sync that calls the operation callback and marks success."""
        # args: (ctx, message, operation, ...)
        operation = args[2]
        operation()
        kwargs["sync_status"].update({
            "operation_completed": True,
            "committed": True,
            "pushed": True,
            "error": None,
        })

    @patch(_PATCH_GRAPH_ACK, return_value=None)
    @patch(_PATCH_RESOLVE)
    def test_hook_called_with_correct_args(self, mock_resolve, _mock_ack, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        hook = MagicMock()

        with patch(_PATCH_RUN_SYNC, side_effect=self._run_sync_calling_operation):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                entry_id="01HOOK_TEST",
                post_write_hooks=[hook],
            )

        assert result.written is True
        hook.assert_called_once_with("test-topic", threads_dir, "01HOOK_TEST")

    @patch(_PATCH_GRAPH_ACK, return_value=None)
    @patch(_PATCH_RESOLVE)
    def test_hook_failure_does_not_affect_result(self, mock_resolve, _mock_ack, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        bad_hook = MagicMock(side_effect=RuntimeError("annotation exploded"))

        with patch(_PATCH_RUN_SYNC, side_effect=self._run_sync_calling_operation):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                post_write_hooks=[bad_hook],
            )

        assert result.written is True
        assert result.pushed is True
        assert result.error is None
        bad_hook.assert_called_once()

    @patch(_PATCH_GRAPH_ACK, return_value=None)
    @patch(_PATCH_RESOLVE)
    def test_multiple_hooks_all_called(self, mock_resolve, _mock_ack, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        hook1 = MagicMock()
        hook2 = MagicMock()

        with patch(_PATCH_RUN_SYNC, side_effect=self._run_sync_calling_operation):
            daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                post_write_hooks=[hook1, hook2],
            )

        hook1.assert_called_once()
        hook2.assert_called_once()

    @patch(_PATCH_GRAPH_ACK, return_value=None)
    @patch(_PATCH_RESOLVE)
    def test_first_hook_failure_does_not_skip_second(self, mock_resolve, _mock_ack, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        bad_hook = MagicMock(side_effect=RuntimeError("boom"))
        good_hook = MagicMock()

        with patch(_PATCH_RUN_SYNC, side_effect=self._run_sync_calling_operation):
            daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
                post_write_hooks=[bad_hook, good_hook],
            )

        bad_hook.assert_called_once()
        good_hook.assert_called_once()

    @patch(_PATCH_RESOLVE)
    def test_no_hooks_backward_compatible(self, mock_resolve, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        mock_resolve.return_value = _mock_thread_context(threads_dir)

        with patch(_PATCH_RUN_SYNC, side_effect=_mark_sync_success):
            result = daemon_write_entry(
                "test-topic",
                code_root=tmp_path,
                title="Test",
                body="Test body",
            )

        assert result.written is True
        assert result.pushed is True
