"""Enqueue-time group_id derivation for queued memory-sync tasks.

Money-loop guard, enqueue side (incident
bug-hybrid-static-x-repo-cross-tenant-t2-scope): ``sync_to_memory_backend``
must prefer the canonical ``<org>_<repo>`` group derived from the git
remote over the legacy threads_dir-basename form. On the hosted deployment
the basename fallback produced cwd-derived groups (threads under ``/app``
→ group ``app`` → episodes filed into the ``app_t2`` side graph while the
t2_indexer re-bought the same entries for days).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.baseline_graph.sync import (
    register_memory_sync_callback,
    sync_to_memory_backend,
)


@pytest.fixture
def _graphiti_queue_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Route sync_to_memory_backend into the durable-queue branch."""
    monkeypatch.setenv("WATERCOOLER_MEMORY_BACKEND", "graphiti")
    monkeypatch.setenv("WATERCOOLER_MEMORY_QUEUE", "1")

    # A registered callback is required before the queue branch is reached.
    register_memory_sync_callback("graphiti", lambda *a, **k: True)
    try:
        yield
    finally:
        from watercooler_mcp.memory_sync import _graphiti_sync_callback

        register_memory_sync_callback("graphiti", _graphiti_sync_callback)


class _Worker:
    is_running = True

    def has_executor(self, name: str) -> bool:
        return True


def _run_sync(threads_dir: Path, enqueued: list) -> bool:
    def _capture_enqueue(**kwargs):
        enqueued.append(kwargs)
        return "task-1"

    with (
        patch(
            "watercooler_mcp.memory_queue.enqueue_memory_task",
            side_effect=_capture_enqueue,
        ),
        patch(
            "watercooler_mcp.memory_queue.get_worker",
            return_value=_Worker(),
        ),
        patch(
            "watercooler.baseline_graph.sync.is_hybrid_t2_handoff_active",
            return_value=False,
        ),
    ):
        return sync_to_memory_backend(
            threads_dir=threads_dir,
            topic="t",
            entry_id="E1",
            entry_body="body",
        )


class TestEnqueueGroupDerivation:
    def test_git_remote_slug_produces_canonical_group(
        self, tmp_path: Path, _graphiti_queue_env
    ) -> None:
        threads_dir = tmp_path / "app"  # adversarial basename
        threads_dir.mkdir()
        enqueued: list = []
        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            return_value="mostlyharmless-ai/watercooler",
        ):
            ok = _run_sync(threads_dir, enqueued)
        assert ok is True
        assert enqueued, "queue branch was not reached"
        assert enqueued[0]["group_id"] == "mostlyharmless_ai_watercooler_cloud"

    def test_no_remote_falls_back_to_basename_off_hosted(
        self, tmp_path: Path, _graphiti_queue_env
    ) -> None:
        threads_dir = tmp_path / "my-local-repo"
        threads_dir.mkdir()
        enqueued: list = []
        with (
            patch(
                "watercooler.path_resolver.derive_repo_slug",
                side_effect=RuntimeError("no git remote"),
            ),
            patch(
                "watercooler_mcp.auth.is_hosted_mode", return_value=False
            ),
        ):
            ok = _run_sync(threads_dir, enqueued)
        assert ok is True
        assert enqueued
        assert enqueued[0]["group_id"] == "my_local_repo"

    def test_no_remote_under_hosted_fails_closed(
        self, tmp_path: Path, _graphiti_queue_env
    ) -> None:
        """PR #1061 review (P1): a repo-only basename fallback like
        ``watercooler_cloud`` survives the executor's single-token guard
        (it contains an underscore), so hosted must refuse to enqueue at
        all when the <org>/<repo> slug cannot be derived — never a
        repo-only side graph."""
        threads_dir = tmp_path / "watercooler-cloud"
        threads_dir.mkdir()
        enqueued: list = []
        with (
            patch(
                "watercooler.path_resolver.derive_repo_slug",
                side_effect=RuntimeError("no git remote"),
            ),
            patch(
                "watercooler_mcp.auth.is_hosted_mode", return_value=True
            ),
        ):
            ok = _run_sync(threads_dir, enqueued)
        assert ok is False
        assert enqueued == []
