"""Phase A (#903) — async enrichment executor.

Unit tests for the queue executor that runs entry enrichment + memory-sync off
the write lock, including the #902 "skip summarizer for structured entries" gate.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.memory_queue.task import MemoryTask


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_task_carries_threads_dir_and_enrichment_dedup_key():
    """The new threads_dir field round-trips and the dedup key is per-entry."""
    t = MemoryTask(
        backend="enrichment", entry_id="E1", topic="t", threads_dir="/tmp/wt"
    )
    assert t.threads_dir == "/tmp/wt"
    assert t.dedup_key() == "E1:enrichment"
    assert MemoryTask.from_json_line(t.to_json_line()).threads_dir == "/tmp/wt"


def test_executor_requires_threads_dir():
    from watercooler_mcp.memory_sync import _enrichment_executor_fn

    task = MemoryTask(backend="enrichment", entry_id="E1", topic="t", threads_dir="")
    with pytest.raises(RuntimeError):
        _run(_enrichment_executor_fn(task))


@patch("watercooler.baseline_graph.sync.sync_to_memory_backend", return_value=True)
@patch("watercooler.baseline_graph.sync.enrich_graph_entry")
@patch("watercooler.baseline_graph.writer.get_entry_node_from_graph")
def test_executor_skips_summary_for_structured_entry(mock_get, mock_enrich, mock_sync):
    """A structured entry (Decision / history-* topic) must NOT be summarized (#902),
    but embeddings + memory-sync still run."""
    from watercooler_mcp.memory_sync import _enrichment_executor_fn

    mock_get.return_value = {
        "entry_type": "Decision", "body": "b", "title": "T", "summary": "",
    }
    er = MagicMock(summary_generated=False, embedding_generated=True)
    mock_enrich.return_value = er

    task = MemoryTask(
        backend="enrichment", entry_id="E1",
        topic="history-seg-views-dispatch", threads_dir="/tmp/wt",
    )
    res = _run(_enrichment_executor_fn(task))

    assert mock_enrich.call_args.kwargs["generate_summaries"] is False
    assert mock_enrich.call_args.kwargs["generate_embeddings"] is True
    assert res["memory_synced"] is True


@patch("watercooler.baseline_graph.sync.sync_to_memory_backend", return_value=True)
@patch("watercooler.baseline_graph.sync.enrich_graph_entry")
@patch("watercooler.baseline_graph.writer.get_entry_node_from_graph")
def test_executor_summarizes_ordinary_note(mock_get, mock_enrich, mock_sync):
    """An ordinary Note on a non-structured topic IS summarized."""
    from watercooler_mcp.memory_sync import _enrichment_executor_fn

    mock_get.return_value = {
        "entry_type": "Note", "body": "b", "title": "T", "summary": "",
    }
    er = MagicMock(summary_generated=True, embedding_generated=True)
    mock_enrich.return_value = er

    task = MemoryTask(
        backend="enrichment", entry_id="E2",
        topic="general-discussion", threads_dir="/tmp/wt",
    )
    _run(_enrichment_executor_fn(task))

    assert mock_enrich.call_args.kwargs["generate_summaries"] is True
