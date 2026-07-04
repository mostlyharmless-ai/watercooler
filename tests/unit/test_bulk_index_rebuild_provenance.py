"""Tests for ``watercooler_bulk_index(rebuild_index_only=True)``.

The mode must rebuild the entry↔episode provenance index from the graph with
a corpus-wide timestamp→entry_id hint map — queueing nothing and calling no
LLM. Guards: mutual exclusion with the other mode selectors, graphiti-only,
and backend-unavailable degradation.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from watercooler_mcp.tools.memory import _bulk_index_impl


def _result_json(tool_result) -> dict:
    return json.loads(tool_result.content[0].text)


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.entry_episode_index = {"e1": "u1"}  # len() == 1 after rebuild

    def rebuild_entry_episode_index_from_graph(self, timestamp_to_entry_id=None):
        self.calls.append(dict(timestamp_to_entry_id or {}))
        return 42


def test_mode_selectors_mutually_exclusive():
    result = asyncio.run(_bulk_index_impl(
        ctx=None, rebuild_index_only=True, preflight_only=True
    ))
    assert "mutually exclusive" in _result_json(result)["error"]


def test_rebuild_requires_graphiti_backend(tmp_path, monkeypatch):
    with (
        patch("watercooler_mcp.auth.is_hosted_mode", return_value=False),
        patch(
            "watercooler.path_resolver.resolve_threads_dir",
            return_value=tmp_path,
        ),
        patch(
            "watercooler.baseline_graph.storage.list_thread_topics",
            return_value=["t1"],
        ),
    ):
        result = asyncio.run(_bulk_index_impl(
            ctx=None,
            code_path=str(tmp_path),
            backend="leanrag",
            rebuild_index_only=True,
        ))
    assert "graphiti" in _result_json(result)["error"]


def test_rebuild_builds_ts_map_and_calls_backend(tmp_path):
    fake_backend = FakeBackend()
    entries_by_topic = {
        "topic-a": [
            {"entry_id": "01AAAAAAAAAAAAAAAAAAAAAAAA", "timestamp": "2026-01-01T00:00:00+00:00"},
            {"entry_id": "01BBBBBBBBBBBBBBBBBBBBBBBB", "timestamp": "2026-01-02T00:00:00+00:00"},
            {"entry_id": "", "timestamp": "2026-01-03T00:00:00+00:00"},  # skipped
        ],
        "topic-b": [
            {"entry_id": "01CCCCCCCCCCCCCCCCCCCCCCCC", "timestamp": "2026-01-04T00:00:00+00:00"},
        ],
    }

    with (
        patch("watercooler_mcp.auth.is_hosted_mode", return_value=False),
        patch(
            "watercooler.path_resolver.resolve_threads_dir",
            return_value=tmp_path,
        ),
        patch(
            "watercooler.baseline_graph.storage.list_thread_topics",
            return_value=list(entries_by_topic),
        ),
        patch(
            "watercooler.commands.list_entries",
            side_effect=lambda topic, threads_dir: entries_by_topic[topic],
        ),
        patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=SimpleNamespace(database="db_t2"),
        ),
        patch(
            "watercooler_mcp.memory.get_graphiti_backend",
            return_value=fake_backend,
        ),
    ):
        result = asyncio.run(_bulk_index_impl(
            ctx=None,
            code_path=str(tmp_path),
            backend="graphiti",
            rebuild_index_only=True,
        ))

    payload = _result_json(result)
    assert payload["rebuild_index_only"] is True
    assert payload["mode"] == "local"
    assert payload["topics_scanned"] == 2
    assert payload["entries_seen"] == 3  # empty entry_id skipped
    assert payload["timestamp_hints"] == 3
    assert payload["mappings_recovered"] == 42
    assert payload["index_size_after"] == 1
    assert payload["entries_queued"] == 0

    # The backend received the full corpus-wide hint map.
    assert fake_backend.calls == [{
        "2026-01-01T00:00:00+00:00": "01AAAAAAAAAAAAAAAAAAAAAAAA",
        "2026-01-02T00:00:00+00:00": "01BBBBBBBBBBBBBBBBBBBBBBBB",
        "2026-01-04T00:00:00+00:00": "01CCCCCCCCCCCCCCCCCCCCCCCC",
    }]


def test_rebuild_degrades_when_backend_unavailable(tmp_path):
    with (
        patch("watercooler_mcp.auth.is_hosted_mode", return_value=False),
        patch(
            "watercooler.path_resolver.resolve_threads_dir",
            return_value=tmp_path,
        ),
        patch(
            "watercooler.baseline_graph.storage.list_thread_topics",
            return_value=[],
        ),
        patch(
            "watercooler_mcp.memory.load_graphiti_config",
            return_value=None,
        ),
    ):
        result = asyncio.run(_bulk_index_impl(
            ctx=None,
            code_path=str(tmp_path),
            backend="graphiti",
            rebuild_index_only=True,
        ))
    assert _result_json(result)["error"] == "graphiti_backend_unavailable"
