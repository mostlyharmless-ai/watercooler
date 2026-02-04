"""Tests for middleware-level memory sync guarantee.

Validates that sync_entry_to_memory_backend() is called unconditionally
from operation_with_graph_sync() inside run_with_sync(), regardless of
which enrichment path was taken:

- Path A: enrichment not configured (wants_enrichment=False)
- Path B: enrichment services unavailable (llm=False, embed=False)
- Path C: enrichment runs but returns noop
- Path D: enrichment raises an exception
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.middleware import run_with_sync


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FakeContext:
    """Minimal ThreadContext for testing."""
    code_root: Optional[Path]
    threads_dir: Path
    threads_repo_url: Optional[str] = None
    code_repo: Optional[str] = None
    code_branch: Optional[str] = None
    code_commit: Optional[str] = None
    code_remote: Optional[str] = None
    threads_slug: Optional[str] = None
    explicit_dir: bool = False


@pytest.fixture()
def fake_context(tmp_path: Path) -> _FakeContext:
    """Create a minimal ThreadContext with tmp_path directories."""
    code_root = tmp_path / "code"
    code_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    return _FakeContext(code_root=code_root, threads_dir=threads_dir)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_graph_config(*, summaries: bool = False, embeddings: bool = False):
    """Build a mock WatercoolerConfig with the given graph flags."""
    graph = MagicMock()
    graph.generate_summaries = summaries
    graph.generate_embeddings = embeddings
    wc_config = MagicMock()
    wc_config.mcp.graph = graph
    return wc_config


def _run_with_mocked_sync(
    context: _FakeContext,
    *,
    graph_config,
    service_avail: tuple[bool, bool] = (False, False),
    enrich_result=None,
    enrich_side_effect=None,
):
    """Call run_with_sync with all dependencies mocked.

    Returns the mock for sync_entry_to_memory_backend so callers can assert
    on it.
    """
    # Fake sync manager: with_sync(operation, ...) simply calls the operation
    fake_sync = MagicMock()
    fake_sync.with_sync.side_effect = lambda op, *a, **kw: op()

    mock_memory_sync = MagicMock()

    patches = {
        "watercooler_mcp.middleware.get_git_sync_manager_from_context": MagicMock(return_value=fake_sync),
        "watercooler_mcp.middleware.get_watercooler_config": MagicMock(return_value=graph_config),
        "watercooler_mcp.middleware._check_enrichment_services_available": MagicMock(return_value=service_avail),
        "watercooler_mcp.middleware.acquire_parity_lock": MagicMock(return_value=MagicMock()),
        "watercooler_mcp.middleware.acquire_topic_lock": MagicMock(return_value=MagicMock()),
        "watercooler_mcp.middleware.run_preflight": MagicMock(return_value=MagicMock(can_proceed=True, auto_fixed=False)),
        "watercooler_mcp.middleware.read_parity_state": MagicMock(return_value=MagicMock()),
        "watercooler_mcp.middleware.write_parity_state": MagicMock(),
        "watercooler_mcp.middleware._build_commit_footers": MagicMock(return_value=[]),
        "watercooler_mcp.middleware._should_auto_branch": MagicMock(return_value=False),
        # Patch at the source module so the lazy import inside the closure picks it up
        "watercooler.baseline_graph.sync.sync_entry_to_memory_backend": mock_memory_sync,
    }

    # Optionally patch enrich_graph_entry (only needed when enrichment runs)
    if enrich_result is not None or enrich_side_effect is not None:
        enrich_mock = MagicMock()
        if enrich_side_effect is not None:
            enrich_mock.side_effect = enrich_side_effect
        else:
            enrich_mock.return_value = enrich_result
        patches["watercooler.baseline_graph.sync.enrich_graph_entry"] = enrich_mock

    import contextlib

    stack = contextlib.ExitStack()
    for target, mock_obj in patches.items():
        stack.enter_context(patch(target, mock_obj))

    with stack:
        result = run_with_sync(
            context,
            commit_title="test commit",
            operation=lambda: "ok",
            topic="test-topic",
            entry_id="entry-001",
        )

    return result, mock_memory_sync


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMemorySyncGuarantee:
    """Verify sync_entry_to_memory_backend is called after every write path."""

    def test_memory_sync_runs_when_enrichment_not_configured(self, fake_context):
        """Path A: enrichment flags are both False — memory sync still runs."""
        cfg = _make_graph_config(summaries=False, embeddings=False)

        result, mock_mem = _run_with_mocked_sync(
            fake_context,
            graph_config=cfg,
        )

        assert result == "ok"
        mock_mem.assert_called_once_with(
            fake_context.threads_dir, "test-topic", "entry-001",
        )

    def test_memory_sync_runs_when_services_unavailable(self, fake_context):
        """Path B: enrichment wanted but services unreachable — memory sync still runs."""
        cfg = _make_graph_config(summaries=True, embeddings=True)

        result, mock_mem = _run_with_mocked_sync(
            fake_context,
            graph_config=cfg,
            service_avail=(False, False),
        )

        assert result == "ok"
        mock_mem.assert_called_once_with(
            fake_context.threads_dir, "test-topic", "entry-001",
        )

    def test_memory_sync_runs_when_enrichment_is_noop(self, fake_context):
        """Path C: enrichment runs but produces nothing — memory sync still runs."""
        from watercooler.baseline_graph.sync import EnrichmentResult

        cfg = _make_graph_config(summaries=True, embeddings=True)

        result, mock_mem = _run_with_mocked_sync(
            fake_context,
            graph_config=cfg,
            service_avail=(True, True),
            enrich_result=EnrichmentResult.noop(),
        )

        assert result == "ok"
        mock_mem.assert_called_once_with(
            fake_context.threads_dir, "test-topic", "entry-001",
        )

    def test_memory_sync_runs_when_enrichment_raises(self, fake_context):
        """Path D: enrichment raises an exception — memory sync still runs."""
        cfg = _make_graph_config(summaries=True, embeddings=True)

        result, mock_mem = _run_with_mocked_sync(
            fake_context,
            graph_config=cfg,
            service_avail=(True, True),
            enrich_side_effect=RuntimeError("LLM exploded"),
        )

        assert result == "ok"
        mock_mem.assert_called_once_with(
            fake_context.threads_dir, "test-topic", "entry-001",
        )
