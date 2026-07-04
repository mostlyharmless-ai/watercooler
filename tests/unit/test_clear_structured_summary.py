"""Clear pre-existing poisoned summaries on structured entries (#910).

A structured entry skipped by the summarizer (enrich_structured=False) is never
re-summarized, so a stale (pre-#902) stored summary would persist in the graph and
re-sync to the backend. The enrichment executor now clears it. Covers the
``clear_entry_summary`` graph helper and the executor wiring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from watercooler_mcp.memory_queue.task import MemoryTask


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# clear_entry_summary — graph helper (real round-trip)
# --------------------------------------------------------------------------- #

def test_clear_entry_summary_clears_stamps_and_is_idempotent(tmp_path):
    from watercooler.baseline_graph import storage
    from watercooler.baseline_graph.sync import clear_entry_summary
    from watercooler.baseline_graph.summarizer import SUMMARY_SCHEMA_VERSION

    graph_dir = storage.ensure_graph_dir(tmp_path)
    topic = "onboarding-security"
    entries = {
        "entry:E1": {
            "id": "entry:E1", "type": "entry", "entry_type": "Decision",
            "body": "Decided to revert Phase 1a.", "title": "T",
            "summary": "OAuth2 authentication with JWT tokens, refresh rotation.",
        }
    }
    storage.write_thread_graph(graph_dir, topic, {"topic": topic}, entries, {})

    assert clear_entry_summary(tmp_path, topic, "E1") is True
    node = storage.load_thread_entries_dict(graph_dir, topic)["entry:E1"]
    assert node["summary"] == ""
    assert node.get("summary_schema_version") == SUMMARY_SCHEMA_VERSION

    # Idempotent: nothing left to clear -> False.
    assert clear_entry_summary(tmp_path, topic, "E1") is False
    # Missing entry -> False (safe no-op).
    assert clear_entry_summary(tmp_path, topic, "NOPE") is False


# --------------------------------------------------------------------------- #
# executor wiring
# --------------------------------------------------------------------------- #

def _graph_cfg(*, generate_summaries=True, generate_embeddings=False, enrich_structured=False):
    graph = MagicMock()
    graph.generate_summaries = generate_summaries
    graph.generate_embeddings = generate_embeddings
    graph.enrich_structured = enrich_structured
    cfg = MagicMock()
    cfg.mcp.graph = graph
    return cfg


@patch("watercooler.baseline_graph.sync.sync_to_memory_backend", return_value=True)
@patch("watercooler.baseline_graph.sync.clear_entry_summary", return_value=True)
@patch("watercooler.baseline_graph.writer.get_entry_node_from_graph")
@patch("watercooler_mcp.config.get_watercooler_config")
def test_executor_clears_poisoned_structured_summary(mock_cfg, mock_get, mock_clear, mock_sync):
    from watercooler_mcp.memory_sync import _enrichment_executor_fn

    mock_cfg.return_value = _graph_cfg(generate_summaries=True, enrich_structured=False)
    mock_get.return_value = {
        "entry_type": "Decision", "body": "Decided to revert Phase 1a.",
        "title": "T",
        "summary": "OAuth2 authentication with JWT tokens, refresh rotation.",
    }
    task = MemoryTask(
        backend="enrichment", entry_id="E1",
        topic="onboarding-security", threads_dir="/tmp/wt",
    )
    res = _run(_enrichment_executor_fn(task))

    mock_clear.assert_called_once_with(Path("/tmp/wt"), "onboarding-security", "E1")
    assert res["summary_cleared"] is True
    # The backend received the cleared (empty) summary, not the poison.
    assert mock_sync.call_args.kwargs["entry_summary"] == ""


@patch("watercooler.baseline_graph.sync.sync_to_memory_backend", return_value=True)
@patch("watercooler.baseline_graph.sync.clear_entry_summary", return_value=True)
@patch("watercooler.baseline_graph.sync.enrich_graph_entry")
@patch("watercooler.baseline_graph.writer.get_entry_node_from_graph")
@patch("watercooler_mcp.config.get_watercooler_config")
def test_executor_does_not_clear_ordinary_entry(mock_cfg, mock_get, mock_enrich, mock_clear, mock_sync):
    from watercooler_mcp.memory_sync import _enrichment_executor_fn

    mock_cfg.return_value = _graph_cfg(generate_summaries=True, enrich_structured=False)
    mock_get.return_value = {
        "entry_type": "Note", "body": "b", "title": "T", "summary": "An old summary.",
    }
    mock_enrich.return_value = MagicMock(summary_generated=True, embedding_generated=False)
    task = MemoryTask(
        backend="enrichment", entry_id="E2",
        topic="general-discussion", threads_dir="/tmp/wt",
    )
    res = _run(_enrichment_executor_fn(task))

    # Ordinary entry IS summarized (do_summaries True) -> never the clear path.
    mock_clear.assert_not_called()
    assert "summary_cleared" not in res


@patch("watercooler.baseline_graph.sync.sync_to_memory_backend", return_value=True)
@patch("watercooler.baseline_graph.sync.clear_entry_summary", return_value=True)
@patch("watercooler.baseline_graph.writer.get_entry_node_from_graph")
@patch("watercooler_mcp.config.get_watercooler_config")
def test_executor_preserves_current_version_structured_summary(
    mock_cfg, mock_get, mock_clear, mock_sync
):
    """The staleness gate's False branch: a structured entry carrying a CURRENT
    (v3) summary is NOT cleared — guards legitimate summaries (e.g. written while
    enrich_structured was on, then flipped off)."""
    from watercooler.baseline_graph.summarizer import SUMMARY_SCHEMA_VERSION
    from watercooler_mcp.memory_sync import _enrichment_executor_fn

    mock_cfg.return_value = _graph_cfg(generate_summaries=True, enrich_structured=False)
    current = "A current, grounded summary of the decision."
    mock_get.return_value = {
        "entry_type": "Decision", "body": "b", "title": "T",
        "summary": current, "summary_schema_version": SUMMARY_SCHEMA_VERSION,
    }
    task = MemoryTask(
        backend="enrichment", entry_id="E4",
        topic="onboarding-security", threads_dir="/tmp/wt",
    )
    res = _run(_enrichment_executor_fn(task))

    mock_clear.assert_not_called()           # not stale -> preserved
    assert "summary_cleared" not in res
    assert mock_sync.call_args.kwargs["entry_summary"] == current


@patch("watercooler.baseline_graph.sync.sync_to_memory_backend", return_value=True)
@patch("watercooler.baseline_graph.sync.clear_entry_summary", return_value=True)
@patch("watercooler.baseline_graph.sync.enrich_graph_entry")
@patch("watercooler.baseline_graph.writer.get_entry_node_from_graph")
@patch("watercooler_mcp.config.get_watercooler_config")
def test_executor_does_not_clear_when_structured_summaries_enabled(
    mock_cfg, mock_get, mock_enrich, mock_clear, mock_sync
):
    from watercooler_mcp.memory_sync import _enrichment_executor_fn

    # enrich_structured=True -> structured entries ARE summarized -> no clear.
    mock_cfg.return_value = _graph_cfg(generate_summaries=True, enrich_structured=True)
    mock_get.return_value = {
        "entry_type": "Decision", "body": "b", "title": "T", "summary": "x",
    }
    mock_enrich.return_value = MagicMock(summary_generated=True, embedding_generated=False)
    task = MemoryTask(
        backend="enrichment", entry_id="E3",
        topic="onboarding-security", threads_dir="/tmp/wt",
    )
    _run(_enrichment_executor_fn(task))

    mock_clear.assert_not_called()
